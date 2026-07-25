"""Remap DigestEvent.section values to the category-based section taxonomy.

Straight renames get a static map; the retired free_cheap section (price is a
tag, not a subject) is re-placed from each event's categories, mirroring
digests.pick_section.
"""

from django.db import migrations

STATIC_MAP = {
    "family_friendly": "family_fun",
    "music_food": "music_nightlife",
    "date_night": "music_nightlife",
    "outdoor_markets": "outdoors_active",
}

REVERSE_MAP = {
    "family_fun": "family_friendly",
    "music_nightlife": "music_food",
    "outdoors_active": "outdoor_markets",
    "food_markets": "outdoor_markets",
    "arts_community": "top_picks",
}


def _section_from_categories(categories):
    categories = set(categories or [])
    if categories & {"family", "kids"}:
        return "family_fun"
    if categories & {"music", "date_night"}:
        return "music_nightlife"
    if categories & {"food_drink", "market", "festival"}:
        return "food_markets"
    if categories & {"outdoor", "sports"}:
        return "outdoors_active"
    if categories & {"arts_culture", "community", "educational"}:
        return "arts_community"
    return "top_picks"


def forwards(apps, schema_editor):
    DigestEvent = apps.get_model("curator", "DigestEvent")
    for old, new in STATIC_MAP.items():
        DigestEvent.objects.filter(section=old).update(section=new)
    for de in DigestEvent.objects.filter(section="free_cheap").select_related("event"):
        de.section = _section_from_categories(de.event.categories)
        de.save(update_fields=["section"])


def backwards(apps, schema_editor):
    DigestEvent = apps.get_model("curator", "DigestEvent")
    for new, old in REVERSE_MAP.items():
        DigestEvent.objects.filter(section=new).update(section=old)


class Migration(migrations.Migration):
    dependencies = [
        ("curator", "0007_digestissue_rendered_html"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
