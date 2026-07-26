"""Starter quick-add presets — the categories from the curation discussions.
All fully deletable from the builder; this is a convenience seed, not a
fixed taxonomy."""

from django.db import migrations

STARTERS = [
    "Music",
    "Family Fun",
    "Food & Markets",
    "Outdoors",
    "Sports",
    "Worth the Short Drive",
    "Hidden Gem",
]


def forwards(apps, schema_editor):
    SectionPreset = apps.get_model("curator", "SectionPreset")
    for title in STARTERS:
        SectionPreset.objects.get_or_create(title=title)


def backwards(apps, schema_editor):
    SectionPreset = apps.get_model("curator", "SectionPreset")
    SectionPreset.objects.filter(title__in=STARTERS).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("curator", "0013_sectionpreset"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
