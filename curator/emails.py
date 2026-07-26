"""Digest email rendering and sending (spec §21).

Each subscriber gets an individually rendered message so their unsubscribe
token link is personal. Console backend in dev, SMTP provider in prod.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models import DigestIssue, EmailSend, Subscriber

logger = logging.getLogger("curator.emails")


# Looking Ahead groups by day only at this size; curator sections always do.
DAY_SUBHEAD_THRESHOLD = 6


def _timed_first(events):
    """Chronological, but unknown-time events last — they're stored at
    midnight, and listing them first would misread as "starts early"."""
    return sorted(events, key=lambda de: (not de.event.time_is_known, de.event.starts_at, de.id))


def _day_ordered(events):
    """Strictly day-by-day: days in order, and within each day timed events
    first. (Sorting unknown-time events to the end of the whole section would
    split a day into two runs and duplicate its sub-header.)"""
    return sorted(
        events,
        key=lambda de: (
            timezone.localtime(de.event.starts_at).date(),
            not de.event.time_is_known,
            de.event.starts_at,
            de.id,
        ),
    )


def _day_chunks(events, force=False):
    """[{'date': date|None, 'events': [...]}]. One chunk per day (Boise-style
    day sub-headers); below the threshold a single dateless chunk, unless
    forced — curator sections are always day-grouped."""
    if not force and len(events) < DAY_SUBHEAD_THRESHOLD:
        return [{"date": None, "events": events}]
    chunks = []
    for de in events:
        day = timezone.localtime(de.event.starts_at).date()
        if not chunks or chunks[-1]["date"] != day:
            chunks.append({"date": day, "events": []})
        chunks[-1]["events"].append(de)
    return chunks


def email_layout(issue):
    """(days, sections) for the email.

    Days: the day-by-day spine — the default home of every event, grouped by
    local calendar day. Sections: the curator's ad-hoc sections for this
    issue, in their order, followed by an automatic 'Looking Ahead' for
    unsectioned events past the target window. Weeks differ, so sections
    differ — they're created per issue in the builder."""
    spine = {}
    ahead = []
    by_custom = {}
    digest_events = (
        issue.digest_events.filter(include_in_email=True)
        .select_related("event", "custom_section")
        .order_by("event__starts_at", "id")
    )
    for de in digest_events:
        if de.custom_section_id:
            by_custom.setdefault(de.custom_section_id, []).append(de)
            continue
        day = timezone.localtime(de.event.starts_at).date()
        if day > issue.target_end_date:
            ahead.append(de)
        else:
            spine.setdefault(day, []).append(de)

    days = [{"date": day, "events": _timed_first(spine[day])} for day in sorted(spine)]

    sections = []
    for custom in issue.custom_sections.all():
        events = by_custom.get(custom.pk)
        if not events:
            continue
        events = _day_ordered(events)
        sections.append(
            {
                "key": f"custom-{custom.pk}",
                "custom_pk": custom.pk,
                "label": custom.title,
                "meta": None,  # curator sections: just the title, no badge
                "events": events,
                "chunks": _day_chunks(events, force=True),
            }
        )
    if ahead:
        events = _day_ordered(ahead)
        sections.append(
            {
                "key": "ahead",
                "label": "Looking Ahead",
                "meta": {"glyph": "✦", "bg": "#6F6A60", "fg": "#FAF5E9"},
                "events": events,
                "chunks": _day_chunks(events),
            }
        )
    return days, sections


def featured_pick(days, sections):
    """Pinned event anywhere wins; else the strongest event in the spine."""
    spine_events = [de for day in days for de in day["events"]]
    section_events = [de for group in sections for de in group["events"]]
    for de in spine_events + section_events:
        if de.featured:
            return de
    pool = spine_events or section_events
    return max(pool, key=lambda de: de.event.quality_score) if pool else None


def render_digest(issue, unsubscribe_url, web_version=False):
    """web_version=True renders the public browser page: no view-in-browser
    link or unsubscribe footer, a signup invitation instead."""
    days, sections = email_layout(issue)

    # Pick of the week: spotlighted at the top, removed from wherever it lives
    featured = featured_pick(days, sections)
    if featured:
        days = [
            {**day, "events": [de for de in day["events"] if de.pk != featured.pk]}
            for day in days
        ]
        days = [day for day in days if day["events"]]
        rebuilt = []
        for group in sections:
            remaining = [de for de in group["events"] if de.pk != featured.pk]
            if remaining:
                chunks = _day_chunks(remaining, force=group["key"].startswith("custom-"))
                rebuilt.append({**group, "events": remaining, "chunks": chunks})
        sections = rebuilt

    context = {
        "issue": issue,
        "featured": featured,
        "days": days,
        "sections": sections,
        "unsubscribe_url": unsubscribe_url,
        "site_base_url": settings.SITE_BASE_URL,
        "postal_address": settings.EMAIL_POSTAL_ADDRESS,
        "web_version": web_version,
        "issue_url": settings.SITE_BASE_URL + issue.public_path,
    }
    html = render_to_string("curator/emails/digest.html", context)
    text = render_to_string("curator/emails/digest.txt", context)
    return html, text


def _send_one(issue, to_email, unsubscribe_url):
    html, text = render_digest(issue, unsubscribe_url)
    message = EmailMultiAlternatives(
        subject=issue.subject_line,
        body=text,
        from_email=settings.EMAIL_FROM_ADDRESS,
        to=[to_email],
    )
    message.attach_alternative(html, "text/html")
    message.send()


def send_test_email(issue, to_email=None):
    to_email = to_email or settings.ADMIN_EMAIL
    unsubscribe_url = f"{settings.SITE_BASE_URL}/unsubscribe/test-token/"
    _send_one(issue, to_email, unsubscribe_url)
    return to_email


def send_digest(issue):
    """Send to all active subscribers in the issue's region. Returns (sent, failed)."""
    subscribers = Subscriber.objects.filter(
        region=issue.region, status=Subscriber.Status.ACTIVE
    )
    sent = failed = 0
    now = timezone.now()
    for subscriber in subscribers.iterator():
        unsubscribe_url = f"{settings.SITE_BASE_URL}/unsubscribe/{subscriber.unsubscribe_token}/"
        try:
            _send_one(issue, subscriber.email, unsubscribe_url)
            EmailSend.objects.create(
                digest_issue=issue, subscriber=subscriber, status=EmailSend.Status.SENT
            )
            subscriber.last_email_sent_at = now
            subscriber.save(update_fields=["last_email_sent_at", "updated_at"])
            sent += 1
        except Exception as exc:
            failed += 1
            EmailSend.objects.create(
                digest_issue=issue, subscriber=subscriber, status=EmailSend.Status.FAILED
            )
            logger.error("Digest send failed for %s: %s", subscriber.email, exc)

    issue.status = issue.Status.SENT
    issue.sent_at = now
    # Freeze the public web version as it looks today — the archive is a
    # historical record, not a re-render in whatever the current design is.
    issue.rendered_html, _ = render_digest(issue, unsubscribe_url="", web_version=True)
    issue.save(update_fields=["status", "sent_at", "rendered_html", "updated_at"])
    return sent, failed


def send_welcome_email(subscriber):
    """Best-effort confirmation email on signup; failures never block signup."""
    try:
        # New subscribers can read the latest issue right away instead of
        # waiting until Thursday (also covers post-send signups).
        latest = (
            DigestIssue.objects.filter(status=DigestIssue.Status.SENT)
            .order_by("-target_start_date", "-sent_at")
            .first()
        )
        context = {
            "unsubscribe_url": f"{settings.SITE_BASE_URL}/unsubscribe/{subscriber.unsubscribe_token}/",
            "site_base_url": settings.SITE_BASE_URL,
            "postal_address": settings.EMAIL_POSTAL_ADDRESS,
            "latest_issue_url": settings.SITE_BASE_URL + latest.public_path if latest else "",
        }
        message = EmailMultiAlternatives(
            subject="You're in — BloNo Digest",
            body=render_to_string("curator/emails/welcome.txt", context),
            from_email=settings.EMAIL_FROM_ADDRESS,
            to=[subscriber.email],
        )
        message.attach_alternative(
            render_to_string("curator/emails/welcome.html", context), "text/html"
        )
        message.send()
    except Exception as exc:
        logger.warning("Welcome email failed for %s: %s", subscriber.email, exc)
