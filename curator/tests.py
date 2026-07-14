from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from .digests import generate_digest_issue, pick_section, upcoming_weekend
from .emails import send_digest
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

    def test_unsubscribe_token(self):
        self.client.post("/", {"email": "person@example.com"})
        subscriber = Subscriber.objects.get()
        response = self.client.get(f"/unsubscribe/{subscriber.unsubscribe_token}/")
        self.assertEqual(response.status_code, 200)
        subscriber.refresh_from_db()
        self.assertEqual(subscriber.status, "unsubscribed")

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

    def test_pick_section_worth_the_drive(self):
        event = self._event("Peoria Fest", city="Peoria", categories=["festival"])
        self.assertEqual(pick_section(event), "worth_the_drive")

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

    def test_staff_can_load_dashboard(self):
        make_region()
        User.objects.create_superuser("admin", "admin@example.com", "pass12345")
        self.client.login(username="admin", password="pass12345")
        for url in ("/admin-dashboard/", "/admin-dashboard/sources/", "/admin-dashboard/events/",
                    "/admin-dashboard/digests/", "/admin-dashboard/subscribers/", "/admin-dashboard/import-runs/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
