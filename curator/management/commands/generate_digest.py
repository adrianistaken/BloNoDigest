"""Weekly digest draft job (spec §22). Schedule Wednesday night / Thursday
morning. Never sends — the admin reviews and sends from the dashboard.

    python manage.py generate_digest
"""

from datetime import date

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from curator.digests import generate_digest_issue
from curator.models import Region


class Command(BaseCommand):
    help = "Generate a draft digest issue from approved events"

    def add_arguments(self, parser):
        parser.add_argument("--region", default=settings.DEFAULT_REGION_SLUG)
        parser.add_argument("--start", help="Weekend start date YYYY-MM-DD (default: upcoming Friday)")

    def handle(self, *args, **options):
        start = date.fromisoformat(options["start"]) if options["start"] else None
        try:
            issue = generate_digest_issue(options["region"], start_date=start)
        except Region.DoesNotExist:
            raise CommandError(f"No region {options['region']!r}")
        event_count = issue.digest_events.count()
        self.stdout.write(
            self.style.SUCCESS(
                f"Draft created: {issue.title!r} with {event_count} events "
                f"({issue.target_start_date} – {issue.target_end_date}). Review it in the dashboard."
            )
        )
        if event_count == 0:
            self.stdout.write(self.style.WARNING("No approved events matched the window — approve events first."))
