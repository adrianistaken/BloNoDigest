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


def day_groups(issue):
    """Visible digest events grouped for the email: one group per local
    calendar day, then 'Worth the Short Drive' (out-of-area events keep their
    own section), then 'Looking Ahead' for anything past the target window.
    Each group: {"key", "date" (None for the special groups), "label", "events"}."""
    by_day = {}
    drive = []
    ahead = []
    digest_events = (
        issue.digest_events.filter(include_in_email=True)
        .select_related("event")
        .order_by("event__starts_at", "id")
    )
    for de in digest_events:
        if de.section == "worth_the_drive":
            drive.append(de)
            continue
        day = timezone.localtime(de.event.starts_at).date()
        if day > issue.target_end_date:
            ahead.append(de)
        else:
            by_day.setdefault(day, []).append(de)
    # Unknown-time events are stored at midnight; listing them first would
    # misread as "starts early", so they go after the timed events.
    def timed_first(events):
        return sorted(events, key=lambda de: (not de.event.time_is_known, de.event.starts_at, de.id))

    groups = [
        {"key": day.isoformat(), "date": day, "label": None, "events": timed_first(by_day[day])}
        for day in sorted(by_day)
    ]
    if drive:
        groups.append({"key": "worth_the_drive", "date": None, "label": "Worth the Short Drive", "events": drive})
    if ahead:
        groups.append({"key": "ahead", "date": None, "label": "Looking Ahead", "events": ahead})
    return groups


def render_digest(issue, unsubscribe_url, web_version=False):
    """web_version=True renders the public browser page: no view-in-browser
    link or unsubscribe footer, a signup invitation instead."""
    context = {
        "issue": issue,
        "day_groups": day_groups(issue),
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
