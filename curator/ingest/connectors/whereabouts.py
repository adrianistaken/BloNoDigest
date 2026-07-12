"""Whereabouts.tech event widget API connector.

Tourism sites like VisitBN render their events through an embedded
whereabouts.tech widget. The widget fetches events from a public GraphQL
endpoint authenticated only by the site's public organization id, so we can
query it directly — no browser needed, and richer data than the page shows
(descriptions, coordinates, tags, recurrence occurrences).

Config:
{
  "organization_id": "69a0684e1cf08c23e95b2cdb",   # from the widget's requests
  "embed_url": "https://www.visitbn.org/events/",  # sent as wa-embed-url + used as fallback link
  "days_ahead": 21
}
"""

import json
from datetime import timedelta

import requests
from django.conf import settings
from django.utils import timezone
from zoneinfo import ZoneInfo

from .base import BaseConnector, RawEvent

API_URL = "https://api.prod.next.whereabouts.tech/graphql/public"
DEFAULT_DAYS_AHEAD = 21

QUERY = """
query EventMany($startDate: String!, $endDate: String!, $organizationId: ID, $useOrgEventWidgetSettings: Boolean) {
  eventMany(startDate: $startDate, endDate: $endDate, organizationId: $organizationId, useOrgEventWidgetSettings: $useOrgEventWidgetSettings) {
    _id
    type
    startDate
    endDate
    occurrences
    ticketUrl
    price
    title { en }
    description { en }
    images { _id isVideo }
    schedules { allDay timeSlots { from to endsNextDay } }
    tags { global { name { en } tagGroup { key } } }
    eventLocations { venue { en } contact { address { line1 city subdivision postalCode location { coordinates } } } }
    eventOrganizer { venue { en } contact { address { line1 city subdivision postalCode location { coordinates } } } }
  }
}
"""


def _lang(value):
    """Multilingual {en: ...} -> plain string."""
    return (value or {}).get("en", "") or ""


# The widget serves its images from this Cloudinary account; asset _ids like
# "prod/abc123" plug straight into the URL. c_lfill,g_auto = smart-cropped fill.
IMAGE_CDN = "https://res.cloudinary.com/whereabouts-next/image/upload/f_auto/c_lfill,g_auto,h_240,w_360/v1/"


def _image_url(item):
    for image in item.get("images") or []:
        if image.get("_id") and not image.get("isVideo"):
            return IMAGE_CDN + image["_id"]
    return ""


def expand_item(item, window_start_iso, window_end_iso, fallback_url=""):
    """One API event (possibly recurring) -> RawEvents, one per occurrence
    inside the window."""
    title = _lang(item.get("title"))
    if not title:
        return

    locations = item.get("eventLocations") or []
    location = locations[0] if locations else (item.get("eventOrganizer") or {})
    venue = _lang(location.get("venue"))
    address = (location.get("contact") or {}).get("address") or {}
    coordinates = (address.get("location") or {}).get("coordinates") or [None, None]
    state = (address.get("subdivision") or "").strip()
    if state.lower() == "illinois":
        state = "IL"

    schedules = item.get("schedules") or []
    schedule = schedules[0] if schedules else {}
    slots = schedule.get("timeSlots") or []
    slot = slots[0] if slots else {}
    all_day = bool(schedule.get("allDay")) or not slot.get("from")

    tags = []
    for tag in (item.get("tags") or {}).get("global", []) or []:
        name = _lang(tag.get("name"))
        group = ((tag.get("tagGroup") or {}).get("key") or "").replace("_", " ").title()
        for value in (name, group):
            if value and value not in tags:
                tags.append(value)

    occurrences = [o for o in (item.get("occurrences") or []) if o] or [item.get("startDate")]
    for occurrence in occurrences:
        if not occurrence or not (window_start_iso <= occurrence <= window_end_iso):
            continue
        yield RawEvent(
            title=title,
            description=_lang(item.get("description")),
            start=occurrence if all_day else f"{occurrence} {slot['from']}",
            end=f"{occurrence} {slot['to']}" if (not all_day and slot.get("to")) else None,
            url=item.get("ticketUrl") or fallback_url,
            venue_name=venue,
            address_line=address.get("line1") or "",
            city=address.get("city") or "",
            state=state,
            postal_code=address.get("postalCode") or "",
            longitude=coordinates[0],
            latitude=coordinates[1],
            price_text=(item.get("price") or "").strip(),
            image_url=_image_url(item),
            tags=tags,
            payload={"_id": item.get("_id"), "type": item.get("type"), "occurrence": occurrence},
        )


class WhereaboutsConnector(BaseConnector):
    def fetch_and_extract(self):
        organization_id = self.config.get("organization_id")
        if not organization_id:
            raise ValueError("whereabouts_api source needs parser_config.organization_id")
        embed_url = self.config.get("embed_url") or self.source.url
        days_ahead = int(self.config.get("days_ahead", DEFAULT_DAYS_AHEAD))

        tz = ZoneInfo(self.source.region.timezone)
        window_start = timezone.now().astimezone(tz).date()
        window_end = window_start + timedelta(days=days_ahead)

        response = requests.post(
            API_URL,
            json={
                "query": QUERY,
                "variables": {
                    "startDate": window_start.isoformat(),
                    "endDate": window_end.isoformat(),
                    "organizationId": organization_id,
                    "useOrgEventWidgetSettings": True,
                },
            },
            headers={
                "User-Agent": settings.INGEST_USER_AGENT,
                "Content-Type": "application/json",
                "Authorization": f"Bearer {organization_id}",
                "wa-type": "eventWidget",
                "wa-embed-url": embed_url,
                "metadata": json.dumps({"type": "ORGANIZATION", "entityId": organization_id}),
            },
            timeout=settings.INGEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errors"):
            raise RuntimeError(f"Whereabouts API errors: {str(data['errors'])[:500]}")

        events = []
        for item in data.get("data", {}).get("eventMany", []) or []:
            events.extend(
                expand_item(item, window_start.isoformat(), window_end.isoformat(), fallback_url=embed_url)
            )
        return events
