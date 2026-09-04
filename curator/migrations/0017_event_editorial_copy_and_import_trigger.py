from django.db import migrations, models


EDITORIAL_MAP = {
    "custom_title": "editorial_title",
    "custom_time": "editorial_time",
    "custom_location": "editorial_location",
    "custom_price": "editorial_price",
    "custom_blurb": "editorial_description",
}


def move_draft_copy_to_events(apps, schema_editor):
    """Keep existing draft edits, but make them reusable by future drafts."""
    DigestEvent = apps.get_model("curator", "DigestEvent")
    Event = apps.get_model("curator", "Event")

    appearances = (
        DigestEvent.objects.filter(digest_issue__status__in=["draft", "reviewed"])
        .order_by("created_at", "pk")
    )
    for appearance in appearances.iterator():
        updates = {}
        clears = {}
        for old_field, new_field in EDITORIAL_MAP.items():
            value = getattr(appearance, old_field, "")
            if value:
                updates[new_field] = value
                clears[old_field] = ""
        if updates:
            Event.objects.filter(pk=appearance.event_id).update(**updates)
            DigestEvent.objects.filter(pk=appearance.pk).update(**clears)


class Migration(migrations.Migration):
    dependencies = [
        ("curator", "0016_add_new_event_sources"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="editorial_description",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="event",
            name="editorial_location",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="event",
            name="editorial_price",
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name="event",
            name="editorial_time",
            field=models.CharField(blank=True, max_length=50),
        ),
        migrations.AddField(
            model_name="event",
            name="editorial_title",
            field=models.CharField(blank=True, max_length=300),
        ),
        migrations.AddField(
            model_name="importrun",
            name="trigger",
            field=models.CharField(
                choices=[("scheduled", "Scheduled"), ("manual", "Manual")],
                default="scheduled",
                max_length=20,
            ),
        ),
        migrations.RunPython(move_draft_copy_to_events, migrations.RunPython.noop),
    ]
