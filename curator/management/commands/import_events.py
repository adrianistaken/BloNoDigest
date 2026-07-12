"""Daily ingestion job (spec §22). Schedule via cron / Railway cron / etc:

    python manage.py import_events
    python manage.py import_events --region bloomington-normal --source normal-library
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from curator.ingest.importer import import_region_events, import_source
from curator.models import EventSource, Region


class Command(BaseCommand):
    help = "Fetch events from all enabled sources for a region"

    def add_arguments(self, parser):
        parser.add_argument("--region", default=settings.DEFAULT_REGION_SLUG)
        parser.add_argument("--source", help="Only import this source slug")

    def handle(self, *args, **options):
        region_slug = options["region"]
        if options["source"]:
            try:
                source = EventSource.objects.get(region__slug=region_slug, slug=options["source"])
            except EventSource.DoesNotExist:
                raise CommandError(f"No source {options['source']!r} in region {region_slug!r}")
            runs = [import_source(source)]
        else:
            try:
                runs = import_region_events(region_slug)
            except Region.DoesNotExist:
                raise CommandError(f"No region {region_slug!r}. Run `manage.py seed_region` first.")

        for run in runs:
            style = self.style.SUCCESS if run.status == "success" else self.style.WARNING
            self.stdout.write(
                style(
                    f"{run.source.name}: {run.status} "
                    f"(found={run.events_found_count} created={run.events_created_count} "
                    f"updated={run.events_updated_count} rejected={run.events_rejected_count})"
                )
            )
            if run.error_message:
                self.stdout.write(self.style.ERROR(f"  error: {run.error_message[:300]}"))
