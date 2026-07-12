"""Digest issue generation (spec §18).

The Thursday digest covers Friday–Sunday, with a small "coming up next week"
section when strong events exist. Drafts are never sent automatically.
"""

from datetime import datetime, time, timedelta

from django.utils import timezone
from zoneinfo import ZoneInfo

from .models import DigestEvent, DigestIssue, Event, Region

MIN_QUALITY_SCORE = 5
NEXT_WEEK_MIN_SCORE = 8
TOP_PICKS_COUNT = 5
MAX_PER_SECTION = 8
MAX_NEXT_WEEK = 5

CORE_CITIES = {"bloomington", "normal"}


def upcoming_weekend(region_tz, today=None):
    """-> (friday, sunday) of the weekend this digest covers."""
    today = today or timezone.now().astimezone(ZoneInfo(region_tz)).date()
    days_until_friday = (4 - today.weekday()) % 7  # Friday itself -> 0
    friday = today + timedelta(days=days_until_friday)
    return friday, friday + timedelta(days=2)


def pick_section(event):
    """First matching section by precedence. None = leave for top-picks pool."""
    categories = set(event.categories or [])
    city = (event.city or "").strip().lower()
    if city and city not in CORE_CITIES:
        return "worth_the_drive"
    if categories & {"family", "kids"}:
        return "family_friendly"
    if categories & {"free", "cheap"}:
        return "free_cheap"
    if "date_night" in categories:
        return "date_night"
    if categories & {"music", "food_drink"}:
        return "music_food"
    if categories & {"outdoor", "market", "festival"}:
        return "outdoor_markets"
    return None


def generate_digest_issue(region_slug, start_date=None):
    """Create a draft DigestIssue from approved events. Returns the issue."""
    region = Region.objects.get(slug=region_slug)
    tz = ZoneInfo(region.timezone)

    if start_date:
        friday = start_date
        sunday = friday + timedelta(days=2)
    else:
        friday, sunday = upcoming_weekend(region.timezone)
    next_week_end = sunday + timedelta(days=7)

    def window(day_from, day_to):
        return (
            datetime.combine(day_from, time.min, tzinfo=tz),
            datetime.combine(day_to + timedelta(days=1), time.min, tzinfo=tz),
        )

    weekend_start, weekend_end = window(friday, sunday)
    base = Event.objects.filter(
        region=region,
        status=Event.Status.APPROVED,
        quality_score__gte=MIN_QUALITY_SCORE,
    )
    weekend_events = list(
        base.filter(starts_at__gte=weekend_start, starts_at__lt=weekend_end).order_by("-quality_score", "starts_at")
    )
    next_start, next_end = window(sunday + timedelta(days=1), next_week_end)
    next_week_events = list(
        base.filter(
            starts_at__gte=next_start,
            starts_at__lt=next_end,
            quality_score__gte=NEXT_WEEK_MIN_SCORE,
        ).order_by("-quality_score", "starts_at")[:MAX_NEXT_WEEK]
    )

    date_range = f"{friday:%B %-d} to {sunday:%-d}" if friday.month == sunday.month else f"{friday:%B %-d} to {sunday:%B %-d}"
    issue = DigestIssue.objects.create(
        region=region,
        title=f"{region.name} Weekend Digest: {friday:%b %-d}–{sunday:%-d}",
        subject_line=f"Bloomington-Normal weekend events: {date_range}",
        intro_text=(
            "Here's what's happening around Bloomington-Normal this weekend — "
            "picked so you don't have to dig through a dozen calendars."
        ),
        status=DigestIssue.Status.DRAFT,
        target_start_date=friday,
        target_end_date=sunday,
        generated_at=timezone.now(),
    )

    top_picks = weekend_events[:TOP_PICKS_COUNT]
    placed = {e.pk for e in top_picks}
    sections = {"top_picks": list(top_picks)}
    for event in weekend_events:
        if event.pk in placed:
            continue
        section = pick_section(event) or "top_picks"
        bucket = sections.setdefault(section, [])
        if len(bucket) < MAX_PER_SECTION:
            bucket.append(event)
            placed.add(event.pk)
    if next_week_events:
        sections["next_week"] = next_week_events

    for section, events in sections.items():
        for position, event in enumerate(events):
            DigestEvent.objects.create(
                digest_issue=issue, event=event, section=section, position=position
            )
    return issue
