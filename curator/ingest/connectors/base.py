"""Connector contract.

Every connector turns one EventSource into a list of RawEvent objects.
RawEvent carries best-effort extracted data; normalization happens later in
the pipeline, so connectors stay dumb and per-source.
"""

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class RawEvent:
    title: str = ""
    description: str = ""
    # datetime/date when the source provides structure, string when it doesn't
    start: datetime | date | str | None = None
    end: datetime | date | str | None = None
    url: str = ""
    venue_name: str = ""
    address_line: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    latitude: float | None = None
    longitude: float | None = None
    price_text: str = ""
    price_min: float | None = None
    price_max: float | None = None
    image_url: str = ""
    tags: list = field(default_factory=list)
    location_text: str = ""  # unstructured fallback
    payload: dict = field(default_factory=dict)  # raw source data for debugging

    def raw_fields(self):
        """Values persisted onto RawImportedEvent."""
        return {
            "raw_title": self.title or "",
            "raw_description": (self.description or "")[:20000],
            "raw_start": str(self.start or "")[:200],
            "raw_end": str(self.end or "")[:200],
            "raw_location": self.location_text or ", ".join(
                p for p in (self.venue_name, self.address_line, self.city) if p
            ),
            "raw_url": (self.url or "")[:1000],
            "raw_payload_json": self.payload,
        }


class BaseConnector:
    def __init__(self, source):
        self.source = source
        self.config = source.parser_config or {}

    def fetch_and_extract(self) -> list[RawEvent]:
        raise NotImplementedError
