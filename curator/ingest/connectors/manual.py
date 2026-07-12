"""Manual connector — admin-created events, fallback only.

Manual events are created directly as Event rows via the dashboard/Django
admin, so an import run over a manual source is a no-op. The connector exists
so every source_type resolves cleanly in the pipeline.
"""

from .base import BaseConnector


class ManualConnector(BaseConnector):
    def fetch_and_extract(self):
        return []
