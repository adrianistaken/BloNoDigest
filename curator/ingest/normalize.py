"""Normalization rules (spec §14): every RawEvent becomes one consistent dict
shaped like the Event model, or is rejected by `is_valid_event`.
"""

import re
from datetime import date, datetime, time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from dateutil import parser as dateparser

MAX_DESCRIPTION_CHARS = 2000
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "ref", "igshid")

# Towns in and around the region (spec §7) — used to spot a city name inside
# unstructured location strings.
REGION_CITIES = [
    "Bloomington", "Normal", "Towanda", "Hudson", "Carlock", "Downs", "Heyworth",
    "Le Roy", "Lexington", "Danvers", "Colfax", "El Paso", "Mackinaw",
]

FREE_PATTERNS = re.compile(r"\b(free|no cost|complimentary|no charge)\b", re.IGNORECASE)


def normalize_title(title, site_name=""):
    title = re.sub(r"\s+", " ", (title or "")).strip()
    # Remove a trailing "| Site Name" / "- Site Name" the source appends to every title
    if site_name:
        pattern = re.compile(
            r"\s*[|\-–—:]\s*" + re.escape(site_name) + r"\s*$", re.IGNORECASE
        )
        title = pattern.sub("", title)
    return title[:500]


def strip_html(text):
    if not text:
        return ""
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def normalize_description(text):
    text = strip_html(text)
    if len(text) > MAX_DESCRIPTION_CHARS:
        cut = text[:MAX_DESCRIPTION_CHARS]
        text = cut[: cut.rfind(" ")] + "…" if " " in cut else cut
    return text


def normalize_url(url):
    if not url:
        return ""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return ""
    query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not any(k.lower().startswith(t) or k.lower() == t for t in TRACKING_PARAMS)
    ]
    return urlunparse(parsed._replace(query=urlencode(query), fragment=""))[:1000]


def normalize_datetime(value, region_tz):
    """-> (aware datetime | None, time_is_known: bool). Never invents a time:
    date-only inputs become midnight with time_is_known=False."""
    tz = ZoneInfo(region_tz)
    if value is None or value == "":
        return None, False

    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=tz), False
    else:
        text = str(value).strip()
        try:
            dt = dateparser.parse(text, fuzzy=True, default=datetime(1900, 1, 1))
        except (ValueError, OverflowError):
            return None, False
        if dt.year == 1900:  # parser found no real date, only fragments
            return None, False
        # Heuristic: if the string never mentions a time, treat it as date-only
        has_time = bool(re.search(r"\d{1,2}:\d{2}|\d{1,2}\s*(am|pm)|T\d{2}", text, re.IGNORECASE))
        if not has_time:
            return datetime.combine(dt.date(), time.min, tzinfo=tz), False

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt.astimezone(tz), True


def parse_location_text(text):
    """Best-effort split of an unstructured 'Venue, 123 Main St, Bloomington, IL'
    string into venue / address / city. Never invents data (spec §14)."""
    result = {"venue_name": "", "address_line": "", "city": "", "state": ""}
    text = re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        return result

    parts = [p.strip() for p in text.split(",") if p.strip()]
    remaining = []
    for part in parts:
        state_zip = re.match(r"^(IL|Illinois)(?:\s+(\d{5}))?$", part, re.IGNORECASE)
        if state_zip:
            result["state"] = "IL"
            continue
        if re.match(r"^\d{5}$", part):
            continue  # bare ZIP: not useful as venue/city
        if not result["city"] and any(part.lower() == c.lower() for c in REGION_CITIES):
            result["city"] = part
        else:
            remaining.append(part)

    if remaining:
        # A leading street number means it's an address, not a venue name
        if re.match(r"^\d+\s", remaining[0]):
            result["address_line"] = ", ".join(remaining)[:300]
        else:
            result["venue_name"] = remaining[0][:300]
            if len(remaining) > 1:
                result["address_line"] = ", ".join(remaining[1:])[:300]
    return result


def detect_price(price_text, description):
    """-> (price_text, price_min) using free/cost words when the source gave no
    structured price."""
    if price_text:
        return price_text, None
    for text in (description or "",):
        match = re.search(r"\$\s?(\d+(?:\.\d{2})?)", text)
        if FREE_PATTERNS.search(text):
            return "Free", 0
        if match:
            return f"${match.group(1)}", None
    return "", None


def normalize_event(raw, source, region):
    """RawEvent -> dict shaped like the Event model."""
    starts_at, time_known = normalize_datetime(raw.start, region.timezone)
    ends_at, _ = normalize_datetime(raw.end, region.timezone)

    venue_name = (raw.venue_name or "").strip()[:300]
    address_line = (raw.address_line or "").strip()[:300]
    city = (raw.city or "").strip()[:100]
    state = (raw.state or "").strip()[:50]
    if not (venue_name or address_line or city) and raw.location_text:
        parsed = parse_location_text(raw.location_text)
        venue_name = parsed["venue_name"]
        address_line = parsed["address_line"]
        city = parsed["city"]
        state = state or parsed["state"]

    description = normalize_description(raw.description)
    price_text = (raw.price_text or "").strip()[:200]
    price_min = raw.price_min
    if not price_text:
        price_text, detected_min = detect_price(price_text, description)
        if price_min is None:
            price_min = detected_min

    return {
        "region": region,
        "canonical_title": normalize_title(raw.title, site_name=source.name),
        "description": description,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "time_is_known": time_known,
        "timezone": region.timezone,
        "venue_name": venue_name,
        "address_line": address_line,
        "city": city,
        "state": state,
        "postal_code": (raw.postal_code or "").strip()[:20],
        "latitude": raw.latitude,
        "longitude": raw.longitude,
        "price_text": price_text,
        "price_min": price_min,
        "price_max": raw.price_max,
        "source_url": normalize_url(raw.url) or normalize_url(source.url),
        "image_url": normalize_url(raw.image_url),
        "primary_source": source,
        "tags": list(raw.tags or []),
    }


def is_valid_event(normalized):
    """Spec §14 required fields: title, start, source URL, some location."""
    if not normalized["canonical_title"]:
        return False, "missing title"
    if normalized["starts_at"] is None:
        return False, "missing/unparseable start date"
    if not normalized["source_url"]:
        return False, "missing source URL"
    if not (normalized["venue_name"] or normalized["city"] or normalized["address_line"]):
        return False, "missing location"
    return True, ""
