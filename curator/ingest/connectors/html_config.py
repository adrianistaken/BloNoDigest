"""HTML config connector — CSS-selector extraction for pages with predictable
markup but no feed or structured data.

Config:
{
  "event_card_selector": ".event-card",
  "title_selector": ".event-title",
  "date_selector": ".event-date",
  "time_selector": ".event-time",
  "location_selector": ".event-location",
  "description_selector": ".event-description",
  "link_selector": "a"
}

Two optional extras handle calendar-grid layouts (e.g. LibraryMarket library
calendars) where events are grouped under day containers:

  "day_container_selector": ".calendar__day",  # each day's wrapper
  "day_date_attr": "data-date"                  # attribute holding YYYY-MM-DD

When set, each card's date comes from its day container's attribute instead of
a per-card date selector, and the date_selector (if any) is treated as the time.

A "{today}" token anywhere in the URL is replaced with the current date in the
region's timezone, so date-parameterized calendar feeds stay current each run.
When "{today}" is present, "fetch_days": N sweeps the feed once per day for the
next N days (single-day feeds like LibraryMarket's need this to cover a range).

Single-venue sources can set "default_venue" / "default_city", filled in when a
card carries no location of its own (a library doesn't repeat its name on every
event).
"""

import time
from datetime import timedelta
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from django.utils import timezone

from bs4 import BeautifulSoup

from ..fetch import fetch_url
from .base import BaseConnector, RawEvent

FETCH_DELAY_SECONDS = 0.5  # politeness between per-day requests
MAX_FETCH_DAYS = 21


class HTMLConfigConnector(BaseConnector):
    def _resolve_url(self, for_date=None):
        url = self.config.get("url") or self.source.url
        if "{today}" in url:
            tz = ZoneInfo(self.source.region.timezone)
            day = for_date or timezone.now().astimezone(tz).date()
            url = url.replace("{today}", day.isoformat())
        return url

    def fetch_and_extract(self):
        fetch_days = self.config.get("fetch_days")
        if fetch_days and "{today}" in (self.config.get("url") or self.source.url):
            return self._fetch_range(min(int(fetch_days), MAX_FETCH_DAYS))
        url = self._resolve_url()
        response = fetch_url(url)
        return self.extract_from_html(response.text, base_url=url)

    def _fetch_range(self, days):
        tz = ZoneInfo(self.source.region.timezone)
        start = timezone.now().astimezone(tz).date()
        events = []
        for offset in range(days):
            url = self._resolve_url(for_date=start + timedelta(days=offset))
            try:
                response = fetch_url(url)
            except Exception:
                continue  # skip a bad day, keep the rest of the range
            events.extend(self.extract_from_html(response.text, base_url=url))
            time.sleep(FETCH_DELAY_SECONDS)
        return events

    def _text(self, node, key):
        selector = self.config.get(key)
        if not selector or node is None:
            return ""
        found = node.select_one(selector)
        return found.get_text(" ", strip=True) if found else ""

    def _card_to_event(self, card, base_url, day_date=None):
        title = self._text(card, "title_selector")
        if not title:
            return None
        if day_date is not None:
            # Grid layout: date comes from the day container; date_selector holds
            # the time (e.g. "All Day", "6:00 PM"). Skip all-day so we never
            # invent a time — normalization keeps it date-only.
            time_text = self._text(card, "date_selector") or self._text(card, "time_selector")
            start = day_date if "all day" in time_text.lower() else f"{day_date} {time_text}".strip()
        else:
            date_text = self._text(card, "date_selector")
            time_text = self._text(card, "time_selector")
            start = f"{date_text} {time_text}".strip() or None

        link = ""
        link_node = card.select_one(self.config.get("link_selector", "a"))
        if link_node and link_node.get("href"):
            link = urljoin(base_url, link_node["href"])

        location_text = self._text(card, "location_selector")
        return RawEvent(
            title=title,
            description=self._text(card, "description_selector"),
            start=start or None,
            url=link,
            venue_name="" if location_text else self.config.get("default_venue", ""),
            city="" if location_text else self.config.get("default_city", ""),
            location_text=location_text,
            payload={"start": str(start), "day_date": str(day_date or "")},
        )

    def extract_from_html(self, html, base_url=""):
        soup = BeautifulSoup(html, "html.parser")
        card_selector = self.config.get("event_card_selector")
        if not card_selector:
            raise ValueError("html_config source needs parser_config.event_card_selector")

        day_container_selector = self.config.get("day_container_selector")
        day_date_attr = self.config.get("day_date_attr", "data-date")

        events = []
        if day_container_selector:
            for day in soup.select(day_container_selector):
                day_date = day.get(day_date_attr, "")
                if not day_date:
                    continue
                for card in day.select(card_selector):
                    event = self._card_to_event(card, base_url, day_date=day_date)
                    if event:
                        events.append(event)
        else:
            for card in soup.select(card_selector):
                event = self._card_to_event(card, base_url)
                if event:
                    events.append(event)
        return events
