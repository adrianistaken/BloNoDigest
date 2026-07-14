"""Digest email rendering and sending (spec §21).

Each subscriber gets an individually rendered message so their unsubscribe
token link is personal. Console backend in dev, SMTP provider in prod.
"""

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils import timezone

from .models import IMAGE_SECTIONS, EmailSend, Subscriber

logger = logging.getLogger("curator.emails")


def render_digest(issue, unsubscribe_url):
    context = {
        "issue": issue,
        "sections": issue.sections_with_events(),
        "image_sections": IMAGE_SECTIONS,
        "unsubscribe_url": unsubscribe_url,
        "site_base_url": settings.SITE_BASE_URL,
        "postal_address": settings.EMAIL_POSTAL_ADDRESS,
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
    issue.save(update_fields=["status", "sent_at", "updated_at"])
    return sent, failed


def send_welcome_email(subscriber):
    """Best-effort confirmation email on signup; failures never block signup."""
    try:
        context = {
            "unsubscribe_url": f"{settings.SITE_BASE_URL}/unsubscribe/{subscriber.unsubscribe_token}/",
            "site_base_url": settings.SITE_BASE_URL,
            "postal_address": settings.EMAIL_POSTAL_ADDRESS,
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
