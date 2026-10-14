"""Compatibility entry point for the retired scheduled digest job.

Keep old Railway services harmless until they are removed. Drafts are now
created only through the dashboard's manual Generate draft action.
"""

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Retired scheduled job; generate digest drafts manually in the dashboard"

    def add_arguments(self, parser):
        # Accept legacy arguments so an old scheduled invocation exits cleanly.
        parser.add_argument("--region", default=settings.DEFAULT_REGION_SLUG)
        parser.add_argument("--start")

    def handle(self, *args, **options):
        self.stdout.write(
            "Automatic digest draft generation is disabled. "
            "Use Generate draft on the dashboard's Digests page when ready."
        )
