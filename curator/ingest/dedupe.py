"""Deduplication (spec §15): deterministic + fuzzy matching.

The same event shows up on VisitBN, the venue site, the city calendar, and
Ticketmaster. High-confidence duplicates are merged into one canonical Event
(extra sources become EventSourceLinks); medium-confidence matches create a
new Event flagged with duplicate_of so the admin reviews instead of the code
guessing. Raw imports are never deleted.
"""

import re
from datetime import timedelta
from difflib import SequenceMatcher

from django.utils import timezone

from ..models import Event, EventSourceLink

NOISE_WORDS = {
    "event", "events", "tickets", "official", "the", "a", "an", "at", "in",
    "presents", "with", "and",
}

HIGH_CONFIDENCE_SIMILARITY = 0.88
MEDIUM_CONFIDENCE_SIMILARITY = 0.78
MEDIUM_CONFIDENCE_WINDOW = timedelta(hours=2)

# Fields copied onto an existing Event when the same source re-imports it
UPDATABLE_FIELDS = [
    "description", "starts_at", "ends_at", "time_is_known", "venue_name",
    "address_line", "city", "state", "postal_code", "latitude", "longitude",
    "price_text", "price_min", "price_max", "image_url",
]


def title_key(title):
    words = re.findall(r"[a-z0-9]+", (title or "").lower())
    return " ".join(w for w in words if w not in NOISE_WORDS)


def title_similarity(a, b):
    """Max of direct and word-order-insensitive similarity, so 'Farmers Market
    Downtown' matches 'Downtown Farmers Market'."""
    key_a, key_b = title_key(a), title_key(b)
    direct = SequenceMatcher(None, key_a, key_b).ratio()
    sorted_a = " ".join(sorted(key_a.split()))
    sorted_b = " ".join(sorted(key_b.split()))
    token_sorted = SequenceMatcher(None, sorted_a, sorted_b).ratio()
    return max(direct, token_sorted)


def duplicate_group_key(title, starts_at):
    return f"{title_key(title)[:240]}|{starts_at.date().isoformat()}"


def _same_venue(event, normalized):
    venue_a = (event.venue_name or "").strip().lower()
    venue_b = (normalized["venue_name"] or "").strip().lower()
    addr_a = (event.address_line or "").strip().lower()
    addr_b = (normalized["address_line"] or "").strip().lower()
    return (venue_a and venue_a == venue_b) or (addr_a and addr_a == addr_b)


def _same_city(event, normalized):
    city_a = (event.city or "").strip().lower()
    city_b = (normalized["city"] or "").strip().lower()
    return bool(city_a) and city_a == city_b


def find_duplicate(normalized):
    """-> (confidence, event) where confidence is 'high'|'medium'|None.

    Candidates: same region, start date within a day (covers the 2h window
    across midnight)."""
    starts_at = normalized["starts_at"]
    candidates = Event.objects.filter(
        region=normalized["region"],
        starts_at__date__gte=starts_at.date() - timedelta(days=1),
        starts_at__date__lte=starts_at.date() + timedelta(days=1),
    ).exclude(status=Event.Status.REJECTED)

    best = (None, None, 0.0)
    for event in candidates:
        similarity = title_similarity(event.canonical_title, normalized["canonical_title"])
        if similarity < MEDIUM_CONFIDENCE_SIMILARITY:
            continue
        same_date = event.starts_at.date() == starts_at.date()
        close_start = abs(event.starts_at - starts_at) <= MEDIUM_CONFIDENCE_WINDOW

        if similarity >= HIGH_CONFIDENCE_SIMILARITY and same_date and _same_venue(event, normalized):
            return "high", event
        if close_start and (_same_city(event, normalized) or _same_venue(event, normalized)):
            if similarity > best[2]:
                best = ("medium", event, similarity)
    return best[0], best[1]


def _merge_into(canonical, normalized, source, raw):
    """Attach a new source sighting to an existing canonical event, keeping the
    richer description and filling any blanks."""
    EventSourceLink.objects.get_or_create(
        event=canonical,
        source=source,
        source_url=normalized["source_url"],
        defaults={
            "source_title": normalized["canonical_title"][:500],
            "source_payload_json": raw.payload,
        },
    )
    changed = []
    if len(normalized["description"]) > len(canonical.description):
        canonical.description = normalized["description"]
        changed.append("description")
    for field in ("venue_name", "address_line", "city", "postal_code", "price_text", "image_url"):
        if not getattr(canonical, field) and normalized[field]:
            setattr(canonical, field, normalized[field])
            changed.append(field)
    for field in ("latitude", "longitude", "price_min", "price_max"):
        if getattr(canonical, field) is None and normalized[field] is not None:
            setattr(canonical, field, normalized[field])
            changed.append(field)
    canonical.last_seen_at = timezone.now()
    canonical.save(update_fields=changed + ["last_seen_at", "updated_at"])
    return canonical


def upsert_or_dedupe_event(normalized, source, raw):
    """-> (action, event) with action in 'created'|'updated'|'merged'|'flagged'.

    updated = same source re-imported its own event
    merged  = high-confidence duplicate from another source
    flagged = medium confidence; new event created pointing duplicate_of at
              the candidate for admin review
    """
    group_key = duplicate_group_key(normalized["canonical_title"], normalized["starts_at"])

    # Same source re-importing the same event: refresh in place.
    existing = (
        Event.objects.filter(primary_source=source)
        .filter(source_url=normalized["source_url"], starts_at__date=normalized["starts_at"].date())
        .first()
        or Event.objects.filter(primary_source=source, duplicate_group_key=group_key).first()
    )
    if existing:
        for field in UPDATABLE_FIELDS:
            setattr(existing, field, normalized[field])
        existing.canonical_title = normalized["canonical_title"]
        existing.duplicate_group_key = group_key
        existing.last_seen_at = timezone.now()
        existing.save()
        return "updated", existing

    confidence, candidate = find_duplicate(normalized)
    if confidence == "high":
        return "merged", _merge_into(candidate, normalized, source, raw)

    event = Event.objects.create(
        duplicate_group_key=group_key,
        status=Event.Status.NEEDS_REVIEW,
        duplicate_of=candidate if confidence == "medium" else None,
        **normalized,
    )
    return ("flagged" if confidence == "medium" else "created"), event
