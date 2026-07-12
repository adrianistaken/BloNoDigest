"""ICS calendar connector (libraries, cities, universities, parks).

Config: {"url": "https://example.com/events.ics"}  (falls back to source.url)
"""

import icalendar

from ..fetch import fetch_url
from .base import BaseConnector, RawEvent


class ICSConnector(BaseConnector):
    def fetch_and_extract(self):
        url = self.config.get("url") or self.source.url
        response = fetch_url(url)
        calendar = icalendar.Calendar.from_ical(response.content)

        events = []
        for component in calendar.walk("VEVENT"):
            start = component.get("DTSTART")
            end = component.get("DTEND")
            events.append(
                RawEvent(
                    title=str(component.get("SUMMARY", "")),
                    description=str(component.get("DESCRIPTION", "")),
                    start=start.dt if start else None,
                    end=end.dt if end else None,
                    url=str(component.get("URL", "")),
                    location_text=str(component.get("LOCATION", "")),
                    payload={
                        "uid": str(component.get("UID", "")),
                        "summary": str(component.get("SUMMARY", "")),
                        "dtstart": str(start.dt) if start else "",
                        "location": str(component.get("LOCATION", "")),
                    },
                )
            )
        return events
