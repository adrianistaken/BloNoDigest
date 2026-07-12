"""Quality scoring (spec §17) — a rough rank for digest candidates.
Admin can always override by approving/rejecting."""

import re

from django.utils import timezone

VAGUE_TITLE_PATTERN = re.compile(
    r"^(event|meeting|activity|program|closed|closure|holiday|reminder|tbd|save the date)s?\b",
    re.IGNORECASE,
)

# Facility-operations noise common in city calendars (pool hours, closures)
ADMIN_NOISE_PATTERN = re.compile(
    r"\b(hours|closing|closed|closure|maintenance|deadline|registration open)\b", re.IGNORECASE
)


def score_event(event, source_link_count=0):
    score = 0
    title = event.canonical_title or ""
    now = timezone.now()

    vague_title = len(title) < 8 or bool(VAGUE_TITLE_PATTERN.match(title))
    if title and not vague_title:
        score += 3
    if event.starts_at and event.time_is_known:
        score += 3
    if event.venue_name or event.address_line or event.city:
        score += 2
    else:
        score -= 4
    if event.source_url:
        score += 2
    if len(event.description) >= 40:
        score += 2
    if event.price_text or event.price_min is not None:
        score += 2

    categories = set(event.categories or [])
    if event.starts_at and event.starts_at.weekday() >= 4:  # Fri/Sat/Sun
        score += 2
    if categories & {"family", "kids"}:
        score += 2
    if categories & {"free", "cheap"}:
        score += 2
    if categories & {"market", "festival"}:
        score += 2
    if source_link_count > 0:
        score += 1
    if event.image_url:
        score += 1

    if not event.starts_at:
        score -= 5
    elif event.starts_at < now:
        end = event.ends_at or event.starts_at
        score -= 5 if end < now else 0  # already ended vs. in progress
    if vague_title:
        score -= 3
    if ADMIN_NOISE_PATTERN.search(title):
        score -= 4
    if event.primary_source and event.primary_source.reliability_score < 0:
        score -= 2
    if event.duplicate_of_id:
        score -= 2  # duplicate uncertainty pending review

    return score
