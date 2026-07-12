"""Seed the Bloomington-Normal region and its starter source registry.

Idempotent — safe to re-run. Sources verified working at seed time are
enabled; the rest are seeded disabled with notes on what to fix, so they're
visible in the dashboard as work items rather than silent failures.
"""

from django.core.management.base import BaseCommand

from curator.models import EventSource, Region

SOURCES = [
    {
        "name": "Town of Normal Calendar",
        "slug": "town-of-normal",
        "source_type": "rss",
        "url": "https://www.normalil.gov/RSSFeed.aspx?ModID=58&CID=All-calendar.xml",
        "enabled": True,
        "notes": "CivicPlus calendar RSS. Verified working 2026-07. Event date/time/location are inside the description HTML.",
    },
    {
        "name": "Eventbrite Bloomington",
        "slug": "eventbrite-bloomington",
        "source_type": "json_ld",
        "url": "https://www.eventbrite.com/d/il--bloomington/events/",
        "enabled": True,
        "notes": "Server-rendered JSON-LD event list. Verified working 2026-07.",
    },
    {
        "name": "Visit Bloomington-Normal",
        "slug": "visit-bn",
        "source_type": "whereabouts_api",
        "url": "https://www.visitbn.org/events/",
        "enabled": True,
        "notes": "Region tourism calendar via its whereabouts.tech widget's public GraphQL API (no browser needed). Verified working 2026-07. organization_id is VisitBN's public widget id.",
        "parser_config": {
            "organization_id": "69a0684e1cf08c23e95b2cdb",
            "embed_url": "https://www.visitbn.org/events/",
            "days_ahead": 21,
        },
    },
    {
        "name": "City of Bloomington Calendar",
        "slug": "city-of-bloomington",
        "source_type": "rss",
        "url": "https://www.cityblm.org/RSSFeed.aspx?ModID=58&CID=All-calendar.xml",
        "enabled": False,
        "notes": "Hard-blocked: Akamai WAF returns 403 to all non-browser clients incl. browser UAs (rechecked 2026-07, domain now bloomingtonil.gov). Needs a Playwright fetch or a polite allowlist request to the city. Deferred.",
    },
    {
        "name": "Bloomington Public Library",
        "slug": "bloomington-library",
        "source_type": "html_config",
        "url": "https://www.bloomingtonlibrary.org/events/feed/html?_wrapper_format=lc_calendar_feed&current_date={today}&ongoing_events=hide",
        "enabled": True,
        "notes": "LibraryMarket calendar HTML feed (one day per request). Verified working 2026-07. {today}+fetch_days sweep the next 2 weeks; dates read from day containers; venue defaulted since cards omit it.",
        "parser_config": {
            "fetch_days": 14,
            "default_venue": "Bloomington Public Library",
            "default_city": "Bloomington",
            "day_container_selector": ".calendar__day",
            "day_date_attr": "data-date",
            "event_card_selector": "article.event-card",
            "title_selector": ".lc-event__title",
            "date_selector": ".lc-event__date",
            "link_selector": ".lc-event__link",
        },
    },
    {
        "name": "Normal Public Library",
        "slug": "normal-library",
        "source_type": "html_config",
        "url": "https://www.normalpl.org/events/feed/html?_wrapper_format=lc_calendar_feed&current_date={today}&ongoing_events=hide",
        "enabled": True,
        "notes": "LibraryMarket calendar HTML feed (one day per request). Verified working 2026-07. Same platform as Bloomington library.",
        "parser_config": {
            "fetch_days": 14,
            "default_venue": "Normal Public Library",
            "default_city": "Normal",
            "day_container_selector": ".calendar__day",
            "day_date_attr": "data-date",
            "event_card_selector": "article.event-card",
            "title_selector": ".lc-event__title",
            "date_selector": ".lc-event__date",
            "link_selector": ".lc-event__link",
        },
    },
    {
        "name": "Illinois State University Events",
        "slug": "isu-events",
        "source_type": "ics",
        "url": "https://events.illinoisstate.edu/events/?ical=1",
        "enabled": True,
        "notes": "ISU now runs The Events Calendar (WordPress) with a public ICS export. Verified working 2026-07.",
        "parser_config": {},
    },
    {
        "name": "WGLT Datebook",
        "slug": "wglt-datebook",
        "source_type": "html_config",
        "url": "https://www.wglt.org/datebook-arts-music",
        "enabled": False,
        "notes": "Arts/music articles rather than a structured calendar. Configure selectors if useful.",
    },
    {
        "name": "Ticketmaster (Bloomington area)",
        "slug": "ticketmaster-bn",
        "source_type": "ticketmaster_api",
        "url": "https://app.ticketmaster.com/discovery/v2/events.json",
        "enabled": True,
        "notes": "Ticketmaster Discovery API; requires TICKETMASTER_API_KEY in .env (key active 2026-07). Covers Grossinger Motors Arena, BCPA, and other ticketed venues within 25 miles.",
        "parser_config": {"city": "Bloomington", "stateCode": "IL", "radius": 25, "unit": "miles"},
    },
    {
        "name": "Manual events",
        "slug": "manual",
        "source_type": "manual",
        "url": "",
        "enabled": True,
        "notes": "Fallback for admin-created events. Create events in Django admin with this as primary source.",
    },
]


class Command(BaseCommand):
    help = "Seed the Bloomington-Normal region and starter event sources"

    def handle(self, *args, **options):
        region, created = Region.objects.update_or_create(
            slug="bloomington-normal",
            defaults={
                "name": "Bloomington-Normal Area",
                "description": (
                    "Bloomington, Normal, Towanda, Hudson, Carlock, Downs, Heyworth, "
                    "and nearby locations worth the short drive."
                ),
                "center_latitude": 40.4842,
                "center_longitude": -88.9937,
                "default_radius_miles": 25,
                "timezone": "America/Chicago",
                "is_active": True,
            },
        )
        self.stdout.write(self.style.SUCCESS(f"Region {'created' if created else 'updated'}: {region.name}"))

        for spec in SOURCES:
            source, created = EventSource.objects.update_or_create(
                region=region,
                slug=spec["slug"],
                defaults={
                    "name": spec["name"],
                    "source_type": spec["source_type"],
                    "url": spec["url"],
                    "enabled": spec["enabled"],
                    "notes": spec["notes"],
                    "parser_config": spec.get("parser_config", {}),
                },
            )
            marker = "+" if created else "·"
            state = "enabled" if source.enabled else "disabled"
            self.stdout.write(f"  {marker} {source.name} ({source.source_type}, {state})")

        self.stdout.write(self.style.SUCCESS("Done. Next: python manage.py import_events"))
