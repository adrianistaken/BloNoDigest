"""JSON-LD structured data connector (schema.org Event).

Config:
{
  "url": "https://example.com/events",
  "follow_event_links": true,
  "link_selector": ".event-card a"
}

Handles single objects, arrays, and @graph structures. `extract_events_from_html`
is reused by the RSS connector for linked pages.
"""

import json
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..fetch import fetch_url
from .base import BaseConnector, RawEvent

MAX_FOLLOWED_LINKS = 30


def _iter_jsonld_objects(node):
    """Yield every dict in a JSON-LD document, walking arrays and @graph."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_jsonld_objects(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_jsonld_objects(item)


def _is_event_type(obj):
    type_value = obj.get("@type", "")
    if isinstance(type_value, list):
        return any("Event" in str(t) for t in type_value)
    return "Event" in str(type_value)


def _text(value):
    """JSON-LD values may be strings, dicts with @value, or lists."""
    if isinstance(value, list):
        return _text(value[0]) if value else ""
    if isinstance(value, dict):
        return str(value.get("@value") or value.get("name") or "")
    return str(value) if value is not None else ""


def _parse_price(offers):
    price_text, price_min, price_max = "", None, None
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    if not isinstance(offers, dict):
        return price_text, price_min, price_max

    def to_float(v):
        try:
            return float(str(v).replace("$", "").replace(",", ""))
        except (TypeError, ValueError):
            return None

    price = to_float(offers.get("price"))
    low = to_float(offers.get("lowPrice"))
    high = to_float(offers.get("highPrice"))
    if price is not None:
        price_min = price_max = price
        price_text = "Free" if price == 0 else f"${price:g}"
    elif low is not None or high is not None:
        price_min, price_max = low, high
        if low is not None and high is not None:
            price_text = f"${low:g}-${high:g}"
    return price_text, price_min, price_max


def _event_from_jsonld(obj, base_url=""):
    location = obj.get("location") or {}
    if isinstance(location, list):
        location = location[0] if location else {}
    if isinstance(location, str):
        location = {"name": location}

    address = location.get("address") or {}
    if isinstance(address, str):
        address = {"streetAddress": address}
    geo = location.get("geo") or {}

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    price_text, price_min, price_max = _parse_price(obj.get("offers"))
    url = _text(obj.get("url"))
    if url and base_url:
        url = urljoin(base_url, url)

    # image: string, list of strings, or ImageObject {"url": ...}
    image = obj.get("image")
    if isinstance(image, list):
        image = image[0] if image else ""
    if isinstance(image, dict):
        image = image.get("url", "")
    image_url = str(image or "")
    if image_url and base_url:
        image_url = urljoin(base_url, image_url)

    return RawEvent(
        title=_text(obj.get("name")),
        description=_text(obj.get("description")),
        start=_text(obj.get("startDate")) or None,
        end=_text(obj.get("endDate")) or None,
        url=url or base_url,
        venue_name=_text(location.get("name")),
        address_line=_text(address.get("streetAddress")),
        city=_text(address.get("addressLocality")),
        state=_text(address.get("addressRegion")),
        postal_code=_text(address.get("postalCode")),
        latitude=to_float(geo.get("latitude")),
        longitude=to_float(geo.get("longitude")),
        price_text=price_text,
        price_min=price_min,
        price_max=price_max,
        image_url=image_url[:1000],
        payload={k: obj.get(k) for k in ("@type", "name", "startDate", "endDate", "url") if k in obj},
    )


def extract_events_from_html(html, base_url=""):
    """All schema.org Events found in a page's ld+json blocks."""
    soup = BeautifulSoup(html, "html.parser")
    events = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in _iter_jsonld_objects(data):
            if _is_event_type(obj) and _text(obj.get("name")):
                events.append(_event_from_jsonld(obj, base_url=base_url))
    return events


class JSONLDConnector(BaseConnector):
    def fetch_and_extract(self):
        url = self.config.get("url") or self.source.url
        response = fetch_url(url)
        events = extract_events_from_html(response.text, base_url=url)

        if self.config.get("follow_event_links") and self.config.get("link_selector"):
            soup = BeautifulSoup(response.text, "html.parser")
            seen = {e.url for e in events if e.url}
            links = []
            for a in soup.select(self.config["link_selector"]):
                href = a.get("href")
                if href:
                    absolute = urljoin(url, href)
                    if absolute not in seen and absolute not in links:
                        links.append(absolute)
            for link in links[:MAX_FOLLOWED_LINKS]:
                try:
                    page = fetch_url(link)
                    events.extend(extract_events_from_html(page.text, base_url=link))
                    time.sleep(0.5)
                except Exception:
                    continue
        return events
