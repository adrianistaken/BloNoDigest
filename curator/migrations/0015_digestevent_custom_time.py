from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("curator", "0014_seed_section_presets"),
    ]

    operations = [
        migrations.AddField(
            model_name="digestevent",
            name="custom_time",
            field=models.CharField(blank=True, max_length=50),
        ),
    ]
