"""Add the venue sources introduced after the original production seed.

This migration only creates missing rows. Existing source configuration is
left untouched so production edits to URLs, enabled state, notes, or parser
settings are never overwritten by a deployment.
"""

from django.db import migrations


SOURCES = [
    {
        "name": "The Castle Theatre",
        "slug": "castle-theatre",
        "source_type": "html_config",
        "url": "https://thecastletheatre.com/shows",
        "enabled": True,
        "notes": (
            "Music venue (Webflow site). A hidden CMS list (.show-collection-item) carries every "
            "show with full dates and slugs; ticketing is Opendate (no public API). Dates have no "
            "times, so shows land as time-TBD. Verified working 2026-07."
        ),
        "parser_config": {
            "event_card_selector": ".show-collection-item",
            "title_selector": ".show-name",
            "date_selector": ".show-start-date",
            "link_text_selector": ".show-slug",
            "link_template": "https://thecastletheatre.com/shows/{text}",
            "default_venue": "The Castle Theatre",
            "default_city": "Bloomington",
            "default_categories": ["music"],
        },
    },
    {
        "name": "Jazz UpFront",
        "slug": "jazz-upfront",
        "source_type": "html_config",
        "url": "https://www.jazzupfront.com/shows",
        "enabled": True,
        "notes": (
            "Jazz club (Wix events widget, server-rendered). Lists only the next few shows; "
            "dates carry no year or time (year inferred at normalize). Junk rows like "
            "'Reserve a Table' fail date validation and drop out. Verified working 2026-07."
        ),
        "parser_config": {
            "event_card_selector": '[data-hook="side-by-side-item"]',
            "title_selector": 'a[data-hook="title"]',
            "date_selector": '[data-hook="short-date"]',
            "link_selector": 'a[data-hook="title"]',
            "default_venue": "Jazz UpFront",
            "default_city": "Bloomington",
            "default_categories": ["music"],
        },
    },
    {
        "name": "McLean County Museum of History",
        "slug": "mclean-history-museum",
        "source_type": "html_config",
        "url": "https://www.mchistory.org/events",
        "enabled": True,
        "notes": (
            "Museum programs/talks. Clean .event-list-item cards; dates lack a year (inferred) "
            "and times are ranges (start taken). Recurring 'Every Tuesday' cards have no real "
            "date and are skipped by validation. Verified working 2026-07."
        ),
        "parser_config": {
            "event_card_selector": ".event-list-item",
            "title_selector": ".card-title",
            "date_selector": ".d-block:has(.fa-calendar-alt)",
            "time_selector": ".d-block:has(.fa-clock)",
            "time_take_start": True,
            "link_selector": "a",
            "description_selector": ".card-body p",
            "default_venue": "McLean County Museum of History",
            "default_city": "Bloomington",
        },
    },
    {
        "name": "DESTIHL Brewery",
        "slug": "destihl",
        "source_type": "html_config",
        "url": "https://www.destihl.com/live-music",
        "enabled": False,
        "notes": (
            "GoDaddy Website Builder — event content is injected by JS, nothing server-rendered "
            "or structured (checked 2026-07). Needs the Playwright connector or a menu of their "
            "Facebook events. Deferred."
        ),
        "parser_config": {},
    },
    {
        "name": "Downtown Bloomington Association",
        "slug": "downtown-bloomington",
        "source_type": "html_config",
        "url": "https://downtownbloomington.org/",
        "enabled": False,
        "notes": (
            "Site returns 403/404 to non-browser clients depending on UA (checked 2026-07) — "
            "WAF or broken vhost. Their events also syndicate to VisitBN, which we already pull. "
            "Deferred."
        ),
        "parser_config": {},
    },
]


def add_missing_sources(apps, schema_editor):
    Region = apps.get_model("curator", "Region")
    EventSource = apps.get_model("curator", "EventSource")

    region = Region.objects.filter(slug="bloomington-normal").first()
    if region is None:
        return

    for source in SOURCES:
        defaults = {key: value for key, value in source.items() if key != "slug"}
        EventSource.objects.get_or_create(
            region=region,
            slug=source["slug"],
            defaults=defaults,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("curator", "0015_digestevent_custom_time"),
    ]

    operations = [
        # Deliberately irreversible: deleting a source can cascade through its
        # import history and raw events. Reversing code must not delete data.
        migrations.RunPython(add_missing_sources, migrations.RunPython.noop),
    ]
