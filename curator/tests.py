from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from .digests import generate_digest_issue, pick_section, upcoming_weekend
from .emails import render_digest, send_digest
from .ingest.categorize import categorize
from .ingest.connectors.base import RawEvent
from .ingest.connectors.html_config import HTMLConfigConnector
from .ingest.connectors.jsonld import extract_events_from_html
from .ingest.connectors.rss import parse_civicplus_description
from .ingest.dedupe import title_similarity, upsert_or_dedupe_event
from .ingest.normalize import (
    is_valid_event,
    normalize_datetime,
    normalize_event,
    normalize_title,
    normalize_url,
    parse_location_text,
    strip_html,
)
from .ingest.score import score_event
from .models import DigestIssue, EmailSend, Event, EventSource, Region, Subscriber

CT = ZoneInfo("America/Chicago")


def make_region():
    return Region.objects.create(
        name="Bloomington-Normal Area", slug="bloomington-normal", timezone="America/Chicago"
    )


def make_source(region, **kwargs):
    defaults = {"name": "Test Source", "slug": "test-source", "source_type": "ics", "url": "https://example.com/events.ics"}
    defaults.update(kwargs)
    return EventSource.objects.create(region=region, **defaults)


class NormalizeTests(TestCase):
    def test_title_whitespace_and_site_name(self):
        self.assertEqual(
            normalize_title("  Farmers   Market | Visit BN ", site_name="Visit BN"),
            "Farmers Market",
        )

    def test_strip_html(self):
        self.assertEqual(strip_html("<p>Live <b>music</b>\n tonight</p>"), "Live music tonight")

    def test_url_tracking_params_removed(self):
        url = "https://example.com/e/1?utm_source=x&utm_campaign=y&id=5&fbclid=abc"
        self.assertEqual(normalize_url(url), "https://example.com/e/1?id=5")

    def test_datetime_with_time(self):
        dt, known = normalize_datetime("July 11, 2026 9:00 AM", "America/Chicago")
        self.assertTrue(known)
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour), (2026, 7, 11, 9))
        self.assertEqual(dt.tzinfo.key, "America/Chicago")

    def test_date_only_never_invents_time(self):
        dt, known = normalize_datetime(date(2026, 7, 11), "America/Chicago")
        self.assertFalse(known)
        self.assertEqual(dt.hour, 0)
        dt2, known2 = normalize_datetime("July 11, 2026", "America/Chicago")
        self.assertFalse(known2)

    def test_unparseable_date(self):
        dt, known = normalize_datetime("call for details", "America/Chicago")
        self.assertIsNone(dt)

    def test_yearless_dates_infer_nearest_occurrence(self):
        from django.utils import timezone as djtz

        today = djtz.localdate()
        # venue-style "Sat, Jul 25" (no year): assume this year when recent/upcoming
        dt, known = normalize_datetime("Sat, Jul 25", "America/Chicago")
        self.assertIsNotNone(dt)
        self.assertIn(dt.year, (today.year, today.year + 1))
        self.assertEqual((dt.month, dt.day), (7, 25))
        self.assertFalse(known)
        # a month/day far in the past rolls to next year
        past = today - timedelta(days=120)
        dt2, _ = normalize_datetime(past.strftime("%B %d"), "America/Chicago")
        self.assertEqual(dt2.year, today.year + 1)
        # fragments with no real month/day still fail
        self.assertIsNone(normalize_datetime("Every Tuesday at 5:00pm", "America/Chicago")[0])
        self.assertIsNone(normalize_datetime("Available for select events", "America/Chicago")[0])

    def test_parse_location_text(self):
        parsed = parse_location_text("Miller Park Zoo, 1020 S Morris Ave, Bloomington, IL")
        self.assertEqual(parsed["venue_name"], "Miller Park Zoo")
        self.assertEqual(parsed["address_line"], "1020 S Morris Ave")
        self.assertEqual(parsed["city"], "Bloomington")
        self.assertEqual(parsed["state"], "IL")

    def test_validation_requires_core_fields(self):
        region = make_region()
        source = make_source(region)
        raw = RawEvent(title="Concert", start="July 11, 2026 7pm", url="https://x.com/e", city="Normal")
        normalized = normalize_event(raw, source, region)
        self.assertTrue(is_valid_event(normalized)[0])

        missing_location = normalize_event(
            RawEvent(title="Concert", start="July 11, 2026 7pm", url="https://x.com/e"), source, region
        )
        ok, reason = is_valid_event(missing_location)
        self.assertFalse(ok)
        self.assertIn("location", reason)


class CategorizeTests(TestCase):
    def test_family_and_free(self):
        cats = categorize("Storytime for kids", "Fun crafts for kids", price_text="Free")
        self.assertIn("family", cats)
        self.assertIn("kids", cats)
        self.assertIn("free", cats)

    def test_cheap(self):
        cats = categorize("Trivia night", "come play", price_min=10)
        self.assertIn("cheap", cats)

    def test_date_night_requires_evening_and_not_kids(self):
        evening = datetime(2026, 7, 11, 19, 0, tzinfo=CT)
        cats = categorize("Jazz concert", "live jazz downtown", starts_at=evening)
        self.assertIn("date_night", cats)
        kid_cats = categorize("Kids concert", "family show for children", starts_at=evening)
        self.assertNotIn("date_night", kid_cats)

    def test_fallback_other(self):
        self.assertEqual(categorize("Quarterly gathering", ""), ["other"])


class DedupeTests(TestCase):
    def setUp(self):
        self.region = make_region()
        self.source_a = make_source(self.region, slug="a", name="Source A")
        self.source_b = make_source(self.region, slug="b", name="Source B")
        self.starts = timezone.now() + timedelta(days=3)

    def _normalized(self, source, **overrides):
        base = {
            "region": self.region,
            "canonical_title": "Downtown Farmers Market",
            "description": "Local vendors and produce.",
            "starts_at": self.starts,
            "ends_at": None,
            "time_is_known": True,
            "timezone": "America/Chicago",
            "venue_name": "Downtown Bloomington",
            "address_line": "",
            "city": "Bloomington",
            "state": "IL",
            "postal_code": "",
            "latitude": None,
            "longitude": None,
            "price_text": "Free",
            "price_min": 0,
            "price_max": None,
            "source_url": f"https://{source.slug}.example.com/market",
            "image_url": "",
            "primary_source": source,
            "tags": [],
        }
        base.update(overrides)
        return base

    def test_title_similarity(self):
        self.assertGreater(
            title_similarity("Downtown Farmers Market", "Downtown Farmers Market - Official Tickets"), 0.88
        )

    def test_same_source_reimport_updates(self):
        action1, event1 = upsert_or_dedupe_event(self._normalized(self.source_a), self.source_a, RawEvent())
        action2, event2 = upsert_or_dedupe_event(
            self._normalized(self.source_a, description="Local vendors, produce, and food trucks."),
            self.source_a,
            RawEvent(),
        )
        self.assertEqual(action1, "created")
        self.assertEqual(action2, "updated")
        self.assertEqual(event1.pk, event2.pk)
        self.assertEqual(Event.objects.count(), 1)

    def test_high_confidence_merges_with_source_link(self):
        _, canonical = upsert_or_dedupe_event(self._normalized(self.source_a), self.source_a, RawEvent())
        action, merged = upsert_or_dedupe_event(
            self._normalized(self.source_b, description="Local vendors and produce plus live music all morning."),
            self.source_b,
            RawEvent(payload={"src": "b"}),
        )
        self.assertEqual(action, "merged")
        self.assertEqual(merged.pk, canonical.pk)
        self.assertEqual(Event.objects.count(), 1)
        self.assertEqual(canonical.source_links.count(), 1)
        merged.refresh_from_db()
        self.assertIn("live music", merged.description)  # richer description kept

    def test_medium_confidence_flags_for_review(self):
        _, canonical = upsert_or_dedupe_event(self._normalized(self.source_a), self.source_a, RawEvent())
        action, flagged = upsert_or_dedupe_event(
            self._normalized(
                self.source_b,
                canonical_title="Farmers Market Downtown Blm",
                venue_name="",
                starts_at=self.starts + timedelta(hours=1),
                source_url="https://b.example.com/fm",
            ),
            self.source_b,
            RawEvent(),
        )
        self.assertEqual(action, "flagged")
        self.assertEqual(flagged.duplicate_of_id, canonical.pk)
        self.assertEqual(Event.objects.count(), 2)


class ScoreTests(TestCase):
    def test_rich_weekend_event_outscores_vague_one(self):
        region = make_region()
        saturday = timezone.now() + timedelta(days=(5 - timezone.now().weekday()) % 7 + 7)
        rich = Event.objects.create(
            region=region,
            canonical_title="Downtown Bloomington Farmers Market",
            description="Local vendors, produce, food, and a good low-effort Saturday morning option.",
            starts_at=saturday.replace(hour=9),
            venue_name="Downtown Bloomington",
            city="Bloomington",
            source_url="https://example.com/market",
            price_text="Free",
            categories=["market", "free"],
        )
        vague = Event.objects.create(
            region=region,
            canonical_title="Meeting",
            starts_at=saturday.replace(hour=9),
            time_is_known=False,
            source_url="https://example.com/meeting",
        )
        self.assertGreater(score_event(rich), 10)
        self.assertLess(score_event(vague), score_event(rich) - 8)


class ConnectorExtractionTests(TestCase):
    def test_jsonld_extraction_with_graph_and_offers(self):
        html = """
        <html><head><script type="application/ld+json">
        {"@context": "https://schema.org", "@graph": [
          {"@type": "MusicEvent", "name": "Jazz Night",
           "startDate": "2026-07-11T19:00:00-05:00",
           "url": "/events/jazz-night",
           "location": {"@type": "Place", "name": "The Castle Theatre",
             "address": {"streetAddress": "209 E Washington St", "addressLocality": "Bloomington", "addressRegion": "IL"}},
           "offers": {"price": "12.00"}}
        ]}
        </script></head><body></body></html>
        """
        events = extract_events_from_html(html, base_url="https://example.com/page")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.title, "Jazz Night")
        self.assertEqual(event.venue_name, "The Castle Theatre")
        self.assertEqual(event.city, "Bloomington")
        self.assertEqual(event.url, "https://example.com/events/jazz-night")
        self.assertEqual(event.price_min, 12.0)

    def test_html_config_extraction(self):
        region = make_region()
        source = make_source(
            region,
            source_type="html_config",
            parser_config={
                "event_card_selector": ".event-card",
                "title_selector": ".event-title",
                "date_selector": ".event-date",
                "location_selector": ".event-location",
                "link_selector": "a",
            },
        )
        html = """
        <div class="event-card">
          <a href="/e/1"><span class="event-title">Movie in the Park</span></a>
          <span class="event-date">July 10, 2026 8:30 PM</span>
          <span class="event-location">Miller Park, Bloomington</span>
        </div>
        """
        events = HTMLConfigConnector(source).extract_from_html(html, base_url="https://example.com")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].title, "Movie in the Park")
        self.assertEqual(events[0].url, "https://example.com/e/1")
        self.assertIn("July 10, 2026", events[0].start)

    def test_html_config_day_container_dates(self):
        """LibraryMarket-style grid: date on the day container, time on the card."""
        region = make_region()
        source = make_source(
            region,
            source_type="html_config",
            parser_config={
                "day_container_selector": ".calendar__day",
                "day_date_attr": "data-date",
                "event_card_selector": "article.event-card",
                "title_selector": ".lc-event__title",
                "date_selector": ".lc-event__date",
                "link_selector": ".lc-event__link",
            },
        )
        html = """
        <div class="calendar__day" data-date="2026-07-11">
          <article class="event-card">
            <h3 class="lc-event__title"><a class="lc-event__link" href="/event/lego-club">Lego Club</a></h3>
            <div class="lc-event__date">2:00 PM</div>
          </article>
          <article class="event-card">
            <h3 class="lc-event__title"><a class="lc-event__link" href="/event/book-sale">Book Sale</a></h3>
            <div class="lc-event__date">All Day</div>
          </article>
        </div>
        """
        events = HTMLConfigConnector(source).extract_from_html(html, base_url="https://lib.example.com")
        self.assertEqual(len(events), 2)
        timed = next(e for e in events if e.title == "Lego Club")
        self.assertEqual(timed.start, "2026-07-11 2:00 PM")
        self.assertEqual(timed.url, "https://lib.example.com/event/lego-club")
        all_day = next(e for e in events if e.title == "Book Sale")
        self.assertEqual(all_day.start, "2026-07-11")  # no invented time

    def test_html_config_link_template_and_time_range_start(self):
        """Webflow-style hidden lists (slug text, no anchor) and time ranges."""
        region = make_region()
        source = make_source(
            region,
            source_type="html_config",
            parser_config={
                "event_card_selector": ".show-item",
                "title_selector": ".show-name",
                "date_selector": ".show-date",
                "time_selector": ".show-time",
                "time_take_start": True,
                "link_text_selector": ".show-slug",
                "link_template": "https://venue.example.com/shows/{text}",
            },
        )
        html = """
        <div class="show-item">
          <div class="show-name">Big Band Night</div>
          <div class="show-date">November 1, 2026</div>
          <div class="show-time">7:30pm – 10:00pm</div>
          <div class="show-slug">big-band-night-01-nov</div>
        </div>
        """
        events = HTMLConfigConnector(source).extract_from_html(html)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].url, "https://venue.example.com/shows/big-band-night-01-nov")
        self.assertEqual(events[0].start, "November 1, 2026 7:30pm")  # range start only

    def test_jsonld_image_extraction(self):
        html = """
        <script type="application/ld+json">
        {"@type": "Event", "name": "Art Fair", "startDate": "2026-07-11",
         "location": {"name": "Museum"}, "image": ["/img/fair.jpg"]}
        </script>
        """
        events = extract_events_from_html(html, base_url="https://example.com/events")
        self.assertEqual(events[0].image_url, "https://example.com/img/fair.jpg")

    def test_whereabouts_occurrence_expansion(self):
        from .ingest.connectors.whereabouts import expand_item

        item = {
            "_id": "abc",
            "type": "RECURRING",
            "startDate": "2026-07-11",
            "occurrences": ["2026-07-11", "2026-07-18", "2026-09-05"],
            "ticketUrl": "https://example.com/market",
            "price": "",
            "title": {"en": "Farmers Market"},
            "description": {"en": "Local vendors."},
            "schedules": [{"allDay": False, "timeSlots": [{"from": "07:30", "to": "12:00"}]}],
            "tags": {"global": [{"name": {"en": "Outdoor"}, "tagGroup": {"key": "FOOD_DRINK"}}]},
            "eventLocations": [{
                "venue": {"en": "Downtown Bloomington"},
                "contact": {"address": {
                    "line1": "115 E Washington St", "city": "Bloomington",
                    "subdivision": "Illinois", "postalCode": "61701",
                    "location": {"coordinates": [-88.99, 40.48]},
                }},
            }],
            "eventOrganizer": {},
        }
        events = list(expand_item(item, "2026-07-09", "2026-07-30", fallback_url="https://visitbn.org/events/"))
        self.assertEqual(len(events), 2)  # 09-05 falls outside the window
        first = events[0]
        self.assertEqual(first.title, "Farmers Market")
        self.assertEqual(first.start, "2026-07-11 07:30")
        self.assertEqual(first.end, "2026-07-11 12:00")
        self.assertEqual(first.venue_name, "Downtown Bloomington")
        self.assertEqual(first.state, "IL")
        self.assertEqual(first.latitude, 40.48)
        self.assertIn("Outdoor", first.tags)

    def test_civicplus_rss_description(self):
        description = (
            "<strong>Event date:</strong> July 10, 2026 <br>"
            "<strong>Event Time: </strong>06:00 PM - 09:00 PM<br>"
            "<strong>Location:</strong> <br>Uptown Circle, Normal, IL 61761<br>"
            "<strong>Description:</strong> Live music on the circle."
        )
        parsed = parse_civicplus_description(description)
        self.assertEqual(parsed["start"], "July 10, 2026 06:00 PM")
        self.assertIn("Normal", parsed["location_text"])
        self.assertIn("Live music", parsed["description"])


@override_settings(EMAIL_SEND_ASYNC=False)  # synchronous sends so mail.outbox is assertable
class SignupTests(TestCase):
    def setUp(self):
        self.region = make_region()

    def test_signup_creates_subscriber_and_redirects(self):
        response = self.client.post("/", {"email": "person@example.com", "website": ""})
        self.assertRedirects(response, "/thanks/")
        subscriber = Subscriber.objects.get(email="person@example.com")
        self.assertEqual(subscriber.status, "active")
        self.assertEqual(subscriber.region, self.region)
        self.assertEqual(len(mail.outbox), 1)  # welcome email
        welcome = mail.outbox[0]
        html, mimetype = welcome.alternatives[0]
        self.assertEqual(mimetype, "text/html")
        self.assertIn("You're in.", html)
        self.assertIn(subscriber.unsubscribe_token, html)
        self.assertIn(subscriber.unsubscribe_token, welcome.body)  # text version too

    def test_honeypot_stores_nothing(self):
        response = self.client.post("/", {"email": "bot@example.com", "website": "spam.biz"})
        self.assertRedirects(response, "/thanks/")
        self.assertEqual(Subscriber.objects.count(), 0)

    def test_duplicate_signup_is_idempotent(self):
        self.client.post("/", {"email": "person@example.com"})
        self.client.post("/", {"email": "Person@Example.com"})
        self.assertEqual(Subscriber.objects.count(), 1)

    def test_unsubscribe_requires_confirmation_click(self):
        self.client.post("/", {"email": "person@example.com"})
        subscriber = Subscriber.objects.get()
        url = f"/unsubscribe/{subscriber.unsubscribe_token}/"

        # GET (or a link-prefetching mail scanner) must NOT unsubscribe
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Yes, unsubscribe me")
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.status, "active")

        # the explicit confirmation POST does
        response = self.client.post(url)
        self.assertContains(response, "You're unsubscribed")
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.status, "unsubscribed")

        # revisiting afterwards shows the already-unsubscribed state
        self.assertContains(self.client.get(url), "Already unsubscribed")

    def test_resubscribe_after_unsubscribe(self):
        self.client.post("/", {"email": "person@example.com"})
        subscriber = Subscriber.objects.get()
        subscriber.unsubscribe()
        self.client.post("/", {"email": "person@example.com"})
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.status, "active")

    def test_health(self):
        self.assertEqual(self.client.get("/health/").json()["status"], "ok")


@override_settings(DEFAULT_REGION_SLUG="bloomington-normal")
class DigestTests(TestCase):
    def setUp(self):
        self.region = make_region()
        self.friday, self.sunday = upcoming_weekend("America/Chicago")

    def _event(self, title, day_offset=0, hour=10, status="approved", score=10, **kwargs):
        starts = datetime.combine(self.friday, datetime.min.time(), tzinfo=CT) + timedelta(
            days=day_offset, hours=hour
        )
        kwargs.setdefault("city", "Bloomington")
        return Event.objects.create(
            region=self.region,
            canonical_title=title,
            starts_at=starts,
            status=status,
            quality_score=score,
            source_url="https://example.com/e",
            **kwargs,
        )

    def test_generation_places_events_and_excludes_junk(self):
        self._event("Farmers Market", day_offset=1, hour=9, categories=["market", "free"])
        self._event("Jazz Night", day_offset=0, hour=19, categories=["music", "date_night"])
        self._event("Rejected thing", status="rejected")
        self._event("Low quality", score=2)
        self._event("Next week concert", day_offset=5, hour=19, score=12, categories=["music"])

        issue = generate_digest_issue("bloomington-normal")
        titles = [de.event.canonical_title for de in issue.digest_events.all()]
        self.assertIn("Farmers Market", titles)
        self.assertIn("Jazz Night", titles)
        self.assertIn("Next week concert", titles)
        self.assertNotIn("Rejected thing", titles)
        self.assertNotIn("Low quality", titles)
        next_week = issue.digest_events.get(event__canonical_title="Next week concert")
        self.assertEqual(next_week.section, "next_week")

    def test_auto_blurb_is_concise_but_custom_runs_verbatim(self):
        from .models import DigestEvent

        long_description = "word " * 100
        event = self._event("Wordy Event", day_offset=1, description=long_description.strip())
        issue = generate_digest_issue("bloomington-normal")
        de = issue.digest_events.get(event=event)
        self.assertLessEqual(len(de.blurb), DigestEvent.BLURB_MAX_CHARS + 1)
        self.assertTrue(de.blurb.endswith("…"))
        de.custom_blurb = "x" * 300
        self.assertEqual(len(de.blurb), 300)  # admin's own words never truncated

    def test_blurb_density_and_sentence_cuts(self):
        from .models import DigestEvent

        sentenced = self._event(
            "Sentence Event",
            day_offset=1,
            description=(
                "First sentence here. Second sentence is a bit longer than the first. "
                "This third sentence is deliberately long enough to push the whole "
                "description well past the one hundred forty character budget."
            ),
        )
        gig = self._event(
            "Bar Gig", day_offset=1, hour=21, categories=["music"],
            description="Promo copy " * 30,
        )
        issue = generate_digest_issue("bloomington-normal")

        de = issue.digest_events.get(event=sentenced)
        # auto blurb ends at a sentence boundary — complete sentences, no "…"
        self.assertEqual(de.blurb_source, "auto")
        self.assertTrue(de.blurb.endswith("the first."))
        self.assertNotIn("…", de.blurb)
        self.assertLessEqual(len(de.blurb), DigestEvent.BLURB_MAX_CHARS)

        de_gig = issue.digest_events.get(event=gig)
        de_gig.section = "music_nightlife"
        de_gig.save(update_fields=["section"])
        # one-liner section: scraped copy hidden, curator words always shown
        self.assertEqual(de_gig.blurb, "")
        self.assertEqual(de_gig.blurb_source, "light")
        de_gig.custom_blurb = "Loud, sweaty, and worth it."
        self.assertEqual(de_gig.blurb, "Loud, sweaty, and worth it.")
        self.assertEqual(de_gig.blurb_source, "custom")

    def test_display_price_override_and_hide(self):
        event = self._event(
            "Tote Bag Workshop", day_offset=1, hour=18,
            price_text="$40 per person/$75 per couple Participants must register on the website.",
        )
        issue = generate_digest_issue("bloomington-normal")
        de = issue.digest_events.get(event=event)
        # defaults to the source's (messy) price text
        self.assertIn("$40 per person", de.display_price)
        # curator override runs verbatim
        de.custom_price = "$40 ($75/couple)"
        self.assertEqual(de.display_price, "$40 ($75/couple)")
        # a lone dash hides the price entirely
        de.custom_price = "-"
        self.assertEqual(de.display_price, "")
        html, _ = render_digest(issue, "#")
        self.assertNotIn("Participants must register", de.display_price)

    def test_custom_time_override_saves_and_renders(self):
        event = self._event("Evening Fights", day_offset=1, hour=19)
        issue = generate_digest_issue("bloomington-normal")
        de = issue.digest_events.get(event=event)
        self.assertEqual(de.display_time, "7:00 PM")

        User.objects.create_superuser("timekeeper", "time@example.com", "pass12345")
        self.client.login(username="timekeeper", password="pass12345")
        page = self.client.get(f"/admin-dashboard/digests/{issue.pk}/")
        self.assertContains(
            page,
            "https://chatgpt.com/g/g-6a7878b8b06c8191b384997f9ed902b2-blonodigest-event-summarizer",
        )
        self.client.post(
            f"/admin-dashboard/digests/{issue.pk}/",
            {
                "action": "set_blurb",
                "digest_event_id": de.pk,
                "custom_time": "Doors 6:00 PM; fights 7:00 PM",
            },
        )
        de.refresh_from_db()
        self.assertEqual(de.custom_time, "Doors 6:00 PM; fights 7:00 PM")
        self.assertEqual(de.display_time, "Doors 6:00 PM; fights 7:00 PM")
        html, text = render_digest(issue, "#")
        self.assertIn("Doors 6:00 PM; fights 7:00 PM", html)
        self.assertIn("Doors 6:00 PM; fights 7:00 PM", text)

        de.custom_time = "-"
        self.assertEqual(de.display_time, "")

    def test_display_title_truncates_unless_custom(self):
        from .models import DigestEvent

        long_title = (
            "Exhibit Opening: More Than a Game! A Community History of Baseball "
            "& Softball presented by BEER NUTS Brand Snacks & The Shirk Family"
        )
        event = self._event(long_title, day_offset=1)
        issue = generate_digest_issue("bloomington-normal")
        de = issue.digest_events.get(event=event)
        self.assertLessEqual(len(de.display_title), DigestEvent.TITLE_MAX_CHARS + 1)
        self.assertTrue(de.display_title.endswith("…"))
        de.custom_title = "More Than a Game! Baseball & Softball Exhibit Opening"
        self.assertEqual(de.display_title, de.custom_title)  # curator title verbatim
        short = self._event("Jazz Night", day_offset=1)
        de_short = issue.digest_events.model(digest_issue=issue, event=short, section="top_picks")
        self.assertEqual(de_short.display_title, "Jazz Night")  # short titles untouched

    def test_display_location_drops_core_city_keeps_others(self):
        from .models import DigestEvent

        local = self._event("Museum Day", day_offset=1, venue_name="McLean County Museum of History")
        away = self._event("Chicago Show", day_offset=1, city="Chicago", venue_name="Joy District")
        bare = self._event("Somewhere Fest", day_offset=1)  # city only, no venue
        issue = generate_digest_issue("bloomington-normal")

        de_local = issue.digest_events.get(event=local)
        self.assertEqual(de_local.display_location, "McLean County Museum of History")
        de_away = issue.digest_events.get(event=away)
        self.assertEqual(de_away.display_location, "Joy District, Chicago")
        de_bare = issue.digest_events.get(event=bare)
        self.assertEqual(de_bare.display_location, "Bloomington")
        de_local.custom_location = "The History Museum, downtown"
        self.assertEqual(de_local.display_location, "The History Museum, downtown")

    def test_pick_section_worth_the_drive(self):
        event = self._event("Peoria Fest", city="Peoria", categories=["festival"])
        self.assertEqual(pick_section(event), "worth_the_drive")

    def test_email_layout_days_default_and_looking_ahead(self):
        from .emails import email_layout

        self._event("Evening Show", day_offset=0, hour=20, categories=["music"], score=6)
        self._event("Morning Market", day_offset=0, hour=8, categories=["market"], score=6)
        mystery = self._event("Mystery Time Gala", day_offset=0, hour=0, categories=["music"], score=6)
        Event.objects.filter(pk=mystery.pk).update(time_is_known=False)
        self._event("Peoria Fest", day_offset=1, city="Peoria", categories=["festival"], score=6)
        self._event("Next week concert", day_offset=5, hour=19, score=12, categories=["music"])
        issue = generate_digest_issue("bloomington-normal")

        days, sections = email_layout(issue)
        # everything defaults to its day — no automatic category sections
        self.assertEqual([d["date"] for d in days], [self.friday, self.friday + timedelta(days=1)])
        self.assertIn(
            "Peoria Fest", [de.event.canonical_title for de in days[1]["events"]]
        )
        # only the automatic Looking Ahead section exists (no custom sections yet)
        self.assertEqual([g["key"] for g in sections], ["ahead"])
        self.assertEqual(
            [de.event.canonical_title for de in sections[0]["events"]], ["Next week concert"]
        )
        # within a day: chronological, unknown-time events last (midnight is
        # a storage artifact, not a real start time)
        self.assertEqual(
            [de.event.canonical_title for de in days[0]["events"]],
            ["Morning Market", "Evening Show", "Mystery Time Gala"],
        )

    def test_featured_pick_and_day_subheads(self):
        from .emails import DAY_SUBHEAD_THRESHOLD, email_layout

        star = self._event("Headline Act", day_offset=1, hour=19, score=40)
        self._event("Runner Up", day_offset=0, hour=19, score=20)
        for i in range(DAY_SUBHEAD_THRESHOLD + 4):
            self._event(f"Family Thing {i}", day_offset=i % 3, hour=9 + i % 10, categories=["family"], score=6)
        issue = generate_digest_issue("bloomington-normal")

        with self.settings(EMAIL_POSTAL_ADDRESS="(address pending PO Box)"):
            html, text = render_digest(issue, "https://example.com/unsub")
        # the strongest spine event is spotlighted and not repeated below
        self.assertIn("Pick of the week", html)
        self.assertEqual(html.count("Headline Act"), 1)
        self.assertIn("PICK OF THE WEEK", text)
        self.assertNotIn("Go make a weekend of it.", html)
        self.assertNotIn("Go make a weekend of it.", text)
        self.assertNotIn("address pending PO Box", html)
        self.assertNotIn("address pending PO Box", text)
        # spine day headers render for the weekend days
        self.assertIn(self.friday.strftime("%A, %B"), html)

        # pinning any event overrides the auto choice; only one pin at a time
        runner = issue.digest_events.get(event__canonical_title="Runner Up")
        User.objects.create_superuser("pinner", "p@example.com", "pass12345")
        self.client.login(username="pinner", password="pass12345")
        self.client.post(
            f"/admin-dashboard/digests/{issue.pk}/",
            {"action": "set_featured", "digest_event_id": runner.pk},
        )
        html, _ = render_digest(issue, "https://example.com/unsub")
        # the pinned event renders first (in the spotlight card at the top)
        self.assertLess(html.find("Runner Up"), html.find("Headline Act"))
        # unpin: falls back to the auto (highest-score) choice
        self.client.post(
            f"/admin-dashboard/digests/{issue.pk}/",
            {"action": "set_featured", "digest_event_id": runner.pk},
        )
        html, _ = render_digest(issue, "https://example.com/unsub")
        self.assertLess(html.find("Headline Act"), html.find("Runner Up"))

        # a curator section holding many events gets day sub-headers; the
        # spotlighted event is excluded from wherever it lives
        section = issue.custom_sections.create(title="Family stuff", position=0)
        issue.digest_events.filter(event__canonical_title__startswith="Family Thing").update(
            custom_section=section
        )
        days, sections = email_layout(issue)
        family = sections[0]
        self.assertTrue(all(c["date"] for c in family["chunks"]))
        self.assertGreater(len(family["chunks"]), 1)
        dates = [c["date"] for c in family["chunks"]]
        self.assertEqual(dates, sorted(dates))

    def test_custom_sections_create_place_delete(self):
        from .emails import email_layout

        event = self._event("Puzzle Palooza", day_offset=1, hour=15, categories=["family"])
        self._event("Jazz Night", day_offset=1, hour=19, categories=["music"])
        issue = generate_digest_issue("bloomington-normal")
        de = issue.digest_events.get(event=event)

        User.objects.create_superuser("curator2", "c2@example.com", "pass12345")
        self.client.login(username="curator2", password="pass12345")
        url = f"/admin-dashboard/digests/{issue.pk}/"

        # create a section for this issue
        self.client.post(url, {"action": "create_section", "title": "Rainy day plans"})
        section = issue.custom_sections.get()
        self.assertEqual(section.title, "Rainy day plans")
        # while still empty it must render as a drop target in the builder
        # (empty sections are only hidden from the email, not the builder)
        page = self.client.get(url).content.decode()
        self.assertIn(f'data-section="custom-{section.pk}"', page)

        # place an event in it — it leaves its day and renders under the section
        self.client.post(
            url,
            {"action": "set_placement", "digest_event_id": de.pk, "custom_section_id": section.pk},
        )
        days, sections = email_layout(issue)
        self.assertEqual(sections[0]["label"], "Rainy day plans")
        self.assertIsNone(sections[0]["meta"])  # curator sections have no badge
        self.assertIn("Puzzle Palooza", [d.event.canonical_title for d in sections[0]["events"]])
        # curator sections are day-grouped even with a single event
        self.assertTrue(all(c["date"] for c in sections[0]["chunks"]))
        day_titles = [d.event.canonical_title for day in days for d in day["events"]]
        self.assertNotIn("Puzzle Palooza", day_titles)
        html, _ = render_digest(issue, "#")
        self.assertIn("Rainy day plans", html)

        # empty placement returns the event to its day
        self.client.post(
            url, {"action": "set_placement", "digest_event_id": de.pk, "custom_section_id": ""}
        )
        de.refresh_from_db()
        self.assertIsNone(de.custom_section)

        # deleting a section drops its events back to their days
        self.client.post(
            url,
            {"action": "set_placement", "digest_event_id": de.pk, "custom_section_id": section.pk},
        )
        self.client.post(url, {"action": "delete_section", "section_id": section.pk})
        self.assertEqual(issue.custom_sections.count(), 0)
        days, sections = email_layout(issue)
        self.assertEqual(sections, [])
        self.assertIn(
            "Puzzle Palooza",
            [d.event.canonical_title for day in days for d in day["events"]],
        )

    def test_removed_event_leaves_email_and_restore_returns_it(self):
        from .emails import email_layout

        event = self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        self._event("Jazz Night", day_offset=1, hour=19, categories=["music"])
        issue = generate_digest_issue("bloomington-normal")
        de = issue.digest_events.get(event=event)

        User.objects.create_superuser("curator", "c@example.com", "pass12345")
        self.client.login(username="curator", password="pass12345")
        self.client.post(
            f"/admin-dashboard/digests/{issue.pk}/",
            {"action": "remove", "digest_event_id": de.pk},
        )
        titles = [d.event.canonical_title for day in email_layout(issue)[0] for d in day["events"]]
        self.assertNotIn("Farmers Market", titles)
        self.client.post(
            f"/admin-dashboard/digests/{issue.pk}/",
            {"action": "restore", "digest_event_id": de.pk},
        )
        titles = [d.event.canonical_title for day in email_layout(issue)[0] for d in day["events"]]
        self.assertIn("Farmers Market", titles)

    def test_section_days_stay_in_order_with_unknown_time_events(self):
        from .emails import email_layout

        self._event("Sat Timed Show", day_offset=1, hour=18, categories=["music"])
        untimed = self._event("Sat All-Day Fest", day_offset=1, hour=0, categories=["music"])
        Event.objects.filter(pk=untimed.pk).update(time_is_known=False)
        self._event("Sun Matinee", day_offset=2, hour=14, categories=["music"])
        issue = generate_digest_issue("bloomington-normal")
        section = issue.custom_sections.create(title="Music", position=0)
        issue.digest_events.update(custom_section=section)

        _, sections = email_layout(issue)
        chunks = sections[0]["chunks"]
        # one chunk per day, in order — the timeless Saturday event must not
        # split Saturday into a second run after Sunday
        self.assertEqual(len(chunks), 2)
        self.assertEqual([c["date"] for c in chunks], sorted(c["date"] for c in chunks))
        self.assertEqual(
            [de.event.canonical_title for de in chunks[0]["events"]],
            ["Sat Timed Show", "Sat All-Day Fest"],  # timed first within the day
        )

    def test_section_presets_quick_add_save_and_delete(self):
        from .models import SectionPreset

        self._event("Jazz Night", day_offset=1, hour=19, categories=["music"])
        issue = generate_digest_issue("bloomington-normal")
        User.objects.create_superuser("presets", "pr@example.com", "pass12345")
        self.client.login(username="presets", password="pass12345")
        url = f"/admin-dashboard/digests/{issue.pk}/"

        # starter presets are seeded and shown as quick-add chips
        self.assertTrue(SectionPreset.objects.filter(title="Music").exists())
        self.assertContains(self.client.get(url), "Quick add:")

        # clicking a chip creates a section with that title (same action)
        self.client.post(url, {"action": "create_section", "title": "Music"})
        self.assertTrue(issue.custom_sections.filter(title="Music").exists())
        # chip hides once the section exists on this issue
        self.assertNotContains(self.client.get(url), "+ Music")

        # creating with "save as quick-add" adds a reusable preset (no dupes)
        self.client.post(
            url, {"action": "create_section", "title": "Rooftop patios", "save_preset": "on"}
        )
        self.assertTrue(SectionPreset.objects.filter(title="Rooftop patios").exists())
        self.client.post(
            url, {"action": "create_section", "title": "rooftop PATIOS", "save_preset": "on"}
        )
        self.assertEqual(
            SectionPreset.objects.filter(title__iexact="rooftop patios").count(), 1
        )

        # deleting a preset removes the chip but not sections already created
        preset = SectionPreset.objects.get(title="Rooftop patios")
        self.client.post(url, {"action": "delete_preset", "preset_id": preset.pk})
        self.assertFalse(SectionPreset.objects.filter(title="Rooftop patios").exists())
        self.assertTrue(issue.custom_sections.filter(title="Rooftop patios").exists())

    def test_move_section_reorders_email(self):
        from .emails import email_layout

        first = self._event("Taco Crawl", day_offset=1, hour=17, categories=["food_drink"])
        second = self._event("Punk Show", day_offset=1, hour=21, categories=["music"])
        issue = generate_digest_issue("bloomington-normal")
        food = issue.custom_sections.create(title="Food", position=0)
        music = issue.custom_sections.create(title="Music", position=1)
        issue.digest_events.filter(event=first).update(custom_section=food)
        issue.digest_events.filter(event=second).update(custom_section=music)

        User.objects.create_superuser("orderer", "o@example.com", "pass12345")
        self.client.login(username="orderer", password="pass12345")
        url = f"/admin-dashboard/digests/{issue.pk}/"
        # the delete button lives in the same form as the hidden move action;
        # the submitted button must win — moving up must NOT delete
        self.client.post(
            url, {"action": "move_section", "section_id": music.pk, "direction": "up"}
        )
        _, sections = email_layout(issue)
        self.assertEqual([g["label"] for g in sections], ["Music", "Food"])
        # moving the top section up is a no-op, not an error
        self.client.post(
            url, {"action": "move_section", "section_id": music.pk, "direction": "up"}
        )
        _, sections = email_layout(issue)
        self.assertEqual([g["label"] for g in sections], ["Music", "Food"])
        self.assertEqual(issue.custom_sections.count(), 2)

    def test_public_issue_page_and_archive(self):
        self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        issue = generate_digest_issue("bloomington-normal")
        path = issue.public_path

        # drafts are not public
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertNotContains(self.client.get("/issues/"), "Weekend of")

        issue.status = DigestIssue.Status.SENT
        issue.save(update_fields=["status"])
        page = self.client.get(path)
        self.assertEqual(page.status_code, 200)
        body = page.content.decode()
        self.assertIn("Farmers Market", body)
        # web version: no unsubscribe or view-in-browser, signup invite instead
        self.assertNotIn("Unsubscribe", body)
        self.assertNotIn("View in browser", body)
        self.assertIn("Get the digest", body)

        archive = self.client.get("/issues/")
        self.assertContains(archive, "Weekend of")
        self.assertContains(archive, path)

        # garbage dates 404 instead of erroring
        self.assertEqual(self.client.get("/issues/not-a-date/").status_code, 404)

    def test_email_version_links_to_browser_view(self):
        self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        issue = generate_digest_issue("bloomington-normal")
        html, text = render_digest(issue, "https://example.com/unsub")
        self.assertIn("View in browser", html)
        self.assertIn(issue.public_path, html)
        self.assertIn("Unsubscribe", html)  # email keeps its footer
        self.assertIn(issue.public_path, text)

    def test_event_titles_are_underlined_links_without_details_cta(self):
        self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        issue = generate_digest_issue("bloomington-normal")

        html, _ = render_digest(issue, "https://example.com/unsub")

        self.assertIn('text-decoration:underline', html)
        self.assertNotIn('Details&nbsp;', html)
        self.assertNotIn('>Details ', html)

    def test_welcome_email_links_latest_sent_issue(self):
        from .emails import send_welcome_email

        self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        issue = generate_digest_issue("bloomington-normal")
        issue.status = DigestIssue.Status.SENT
        issue.save(update_fields=["status"])
        subscriber = Subscriber.objects.create(region=self.region, email="new@example.com")

        send_welcome_email(subscriber)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn(issue.public_path, mail.outbox[0].alternatives[0][0])
        self.assertIn(issue.public_path, mail.outbox[0].body)

    def test_archive_page_frozen_at_send_until_deliberate_refresh(self):
        event = self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        issue = generate_digest_issue("bloomington-normal")
        Subscriber.objects.create(region=self.region, email="a@example.com")
        send_digest(issue)
        issue.refresh_from_db()
        self.assertIn("Farmers Market", issue.rendered_html)

        # later edits (or redesigns) must NOT change the historical page...
        de = issue.digest_events.get(event=event)
        de.custom_title = "Renamed After Send"
        de.save(update_fields=["custom_title"])
        page = self.client.get(issue.public_path).content.decode()
        self.assertIn("Farmers Market", page)
        self.assertNotIn("Renamed After Send", page)

        # ...until the curator deliberately re-renders it (e.g. after a bug fix)
        User.objects.create_superuser("freezer", "f@example.com", "pass12345")
        self.client.login(username="freezer", password="pass12345")
        self.client.post(
            f"/admin-dashboard/digests/{issue.pk}/", {"action": "refresh_snapshot"}
        )
        page = self.client.get(issue.public_path).content.decode()
        self.assertIn("Renamed After Send", page)

    def test_send_digest_records_and_marks_sent(self):
        self._event("Farmers Market", day_offset=1, hour=9, categories=["market"])
        issue = generate_digest_issue("bloomington-normal")
        Subscriber.objects.create(region=self.region, email="a@example.com")
        Subscriber.objects.create(region=self.region, email="b@example.com")
        Subscriber.objects.create(region=self.region, email="gone@example.com", status="unsubscribed")

        sent, failed = send_digest(issue)
        self.assertEqual((sent, failed), (2, 0))
        self.assertEqual(len(mail.outbox), 2)
        self.assertEqual(EmailSend.objects.filter(status="sent").count(), 2)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "sent")
        self.assertIn("unsubscribe", mail.outbox[0].alternatives[0][0].lower())


class AutomationPanelTests(TestCase):
    def test_humanize_cron(self):
        from .automations import humanize_cron

        daily = humanize_cron("0 9 * * *")
        self.assertIn("every day", daily)
        self.assertIn("09:00 UTC", daily)
        weekly = humanize_cron("0 11 * * 4")
        self.assertIn("every Thursday", weekly)
        self.assertIn("11:00 UTC", weekly)
        self.assertEqual(humanize_cron("bad input"), "cron: bad input (UTC)")

    def test_home_shows_automation_schedules(self):
        make_region()
        User.objects.create_superuser("admin", "admin@example.com", "pass12345")
        self.client.login(username="admin", password="pass12345")
        response = self.client.get("/admin-dashboard/")
        self.assertContains(response, "Automations")
        self.assertContains(response, "Nightly event import")
        self.assertContains(response, "every Thursday")


class DashboardAuthTests(TestCase):
    def test_dashboard_requires_staff(self):
        make_region()
        response = self.client.get("/admin-dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])

    def test_events_sorting_and_count(self):
        region = make_region()
        base = timezone.now() + timedelta(days=2)
        low = Event.objects.create(
            region=region, canonical_title="Low Score", starts_at=base,
            quality_score=2, source_url="https://x.com/a", city="Normal",
        )
        high = Event.objects.create(
            region=region, canonical_title="High Score", starts_at=base + timedelta(hours=1),
            quality_score=15, source_url="https://x.com/b", city="Bloomington",
        )
        User.objects.create_superuser("admin2", "a2@example.com", "pass12345")
        self.client.login(username="admin2", password="pass12345")

        response = self.client.get("/admin-dashboard/events/?sort=score&dir=desc")
        events = list(response.context["page"])
        self.assertEqual([e.pk for e in events], [high.pk, low.pk])
        self.assertEqual(response.context["result_count"], 2)

        response = self.client.get("/admin-dashboard/events/?sort=score&dir=asc")
        events = list(response.context["page"])
        self.assertEqual([e.pk for e in events], [low.pk, high.pk])

        response = self.client.get("/admin-dashboard/events/?sort=evil_column")
        self.assertEqual(response.status_code, 200)  # bad sort keys fall back safely

    def test_staff_can_load_dashboard(self):
        make_region()
        User.objects.create_superuser("admin", "admin@example.com", "pass12345")
        self.client.login(username="admin", password="pass12345")
        for url in ("/admin-dashboard/", "/admin-dashboard/sources/", "/admin-dashboard/events/",
                    "/admin-dashboard/digests/", "/admin-dashboard/subscribers/", "/admin-dashboard/import-runs/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
