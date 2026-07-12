"""RSS/Atom feed connector.

Config: {"url": "...", "follow_links": false}
When follow_links is true, each item's page is fetched and JSON-LD Event data
is extracted from it (spec §12C) — better dates than the feed's pubDate.
"""

import re
import time

import feedparser
from bs4 import BeautifulSoup

from ..fetch import fetch_url
from .base import BaseConnector, RawEvent
from .jsonld import extract_events_from_html

MAX_LINKED_PAGES = 25  # politeness cap when following item links

# CivicPlus calendar feeds (many city/town sites) bury the real event data in
# the item description: "Event date: July 7, 2026 Event Time: 06:00 AM - ..."
CIVICPLUS_DATE = re.compile(r"Event dates?:\s*([A-Z][a-z]+ \d{1,2}, \d{4})")
CIVICPLUS_TIME = re.compile(r"Event Time:\s*(\d{1,2}:\d{2}\s*[AP]M)", re.IGNORECASE)
CIVICPLUS_LOCATION = re.compile(r"Location:\s*(.*?)(?:Description:|$)", re.DOTALL)
CIVICPLUS_DESCRIPTION = re.compile(r"Description:\s*(.*)$", re.DOTALL)


def parse_civicplus_description(html_description):
    """-> dict with start/location/description when the CivicPlus pattern is
    present, else None."""
    text = BeautifulSoup(html_description or "", "html.parser").get_text(" ", strip=True)
    date_match = CIVICPLUS_DATE.search(text)
    if not date_match:
        return None
    time_match = CIVICPLUS_TIME.search(text)
    location_match = CIVICPLUS_LOCATION.search(text)
    description_match = CIVICPLUS_DESCRIPTION.search(text)
    start = date_match.group(1)
    if time_match:
        start = f"{start} {time_match.group(1)}"
    return {
        "start": start,
        "location_text": (location_match.group(1).strip() if location_match else "")[:500],
        "description": description_match.group(1).strip() if description_match else "",
    }


class RSSConnector(BaseConnector):
    def fetch_and_extract(self):
        url = self.config.get("url") or self.source.url
        response = fetch_url(url)
        feed = feedparser.parse(response.content)
        follow_links = bool(self.config.get("follow_links"))

        events = []
        followed = 0
        for entry in feed.entries:
            link = entry.get("link", "")

            if follow_links and link and followed < MAX_LINKED_PAGES:
                followed += 1
                try:
                    page = fetch_url(link)
                    linked = extract_events_from_html(page.text, base_url=link)
                    if linked:
                        events.extend(linked)
                        time.sleep(0.5)
                        continue
                except Exception:
                    pass  # fall back to the feed item itself

            summary = entry.get("summary", "")
            civicplus = parse_civicplus_description(summary)
            if civicplus:
                events.append(
                    RawEvent(
                        title=entry.get("title", ""),
                        description=civicplus["description"],
                        start=civicplus["start"],
                        url=link,
                        location_text=civicplus["location_text"],
                        payload={k: str(entry.get(k, "")) for k in ("title", "link", "published", "id")},
                    )
                )
                continue

            published = entry.get("published") or entry.get("updated") or ""
            events.append(
                RawEvent(
                    title=entry.get("title", ""),
                    description=summary,
                    start=published,
                    url=link,
                    payload={k: str(entry.get(k, "")) for k in ("title", "link", "published", "id")},
                )
            )
        return events
