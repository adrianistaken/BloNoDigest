"""Public views: landing, signup, thanks, unsubscribe, health, hidden preview."""

import logging
import threading

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .emails import send_welcome_email
from .forms import SignupForm
from .models import IMAGE_SECTIONS, DigestIssue, Region, Subscriber

logger = logging.getLogger("curator.views")

SIGNUP_RATE_LIMIT = 10  # per IP per hour


def _client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")


@require_http_methods(["GET", "POST"])
def landing(request):
    form = SignupForm(request.POST) if request.method == "POST" else SignupForm()

    if request.method == "POST" and form.is_valid():
        if form.cleaned_data["website"]:
            return redirect("thanks")  # honeypot hit: pretend success, store nothing

        ip_key = f"signup-rate:{_client_ip(request)}"
        attempts = cache.get(ip_key, 0)
        if attempts >= SIGNUP_RATE_LIMIT:
            form.add_error("email", "Too many signups from your network. Try again later.")
        else:
            cache.set(ip_key, attempts + 1, 3600)
            region = Region.objects.get(slug=settings.DEFAULT_REGION_SLUG)
            email = form.cleaned_data["email"].lower().strip()
            try:
                subscriber, created = Subscriber.objects.get_or_create(
                    region=region,
                    email=email,
                    defaults={"source": form.cleaned_data.get("source", "")[:200]},
                )
            except IntegrityError:  # double-click race: another request created it
                subscriber, created = Subscriber.objects.get(region=region, email=email), False
            if not created and subscriber.status != Subscriber.Status.ACTIVE:
                subscriber.status = Subscriber.Status.ACTIVE
                subscriber.subscribed_at = timezone.now()
                subscriber.unsubscribed_at = None
                subscriber.save(update_fields=["status", "subscribed_at", "unsubscribed_at", "updated_at"])
            if created:
                # Fire-and-forget: the visitor's page load never waits on a
                # mail server (send_welcome_email logs its own failures).
                if settings.EMAIL_SEND_ASYNC:
                    threading.Thread(
                        target=send_welcome_email, args=(subscriber,), daemon=True
                    ).start()
                else:
                    send_welcome_email(subscriber)
            return redirect("thanks")

    return render(request, "curator/landing.html", {"form": form})


def thanks(request):
    return render(request, "curator/thanks.html")


def unsubscribe(request, token):
    subscriber = get_object_or_404(Subscriber, unsubscribe_token=token)
    if subscriber.status == Subscriber.Status.ACTIVE:
        subscriber.unsubscribe()
    return render(request, "curator/unsubscribe.html", {"subscriber": subscriber})


def preview_latest(request):
    """Hidden browser preview of the most recently sent digest (spec §24)."""
    issue = DigestIssue.objects.filter(status=DigestIssue.Status.SENT).first()
    if issue is None and request.user.is_staff:
        issue = DigestIssue.objects.first()
    if issue is None:
        raise Http404
    return render(
        request,
        "curator/emails/digest.html",
        {
            "issue": issue,
            "sections": issue.sections_with_events(),
            "image_sections": IMAGE_SECTIONS,
            "unsubscribe_url": "#",
            "site_base_url": settings.SITE_BASE_URL,
            "postal_address": settings.EMAIL_POSTAL_ADDRESS,
        },
    )


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})
