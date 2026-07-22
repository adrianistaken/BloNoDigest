"""Public views: landing, signup, thanks, unsubscribe, health, issue archive."""

import logging
import threading
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, connection
from django.db.models import Count, Q
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .emails import render_digest, send_welcome_email
from .forms import SignupForm
from .models import DigestIssue, Region, Subscriber

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


@require_http_methods(["GET", "POST"])
def unsubscribe(request, token):
    """GET shows a confirmation page; only an explicit POST unsubscribes.
    (Also protects against mail scanners that prefetch every link.)"""
    subscriber = get_object_or_404(Subscriber, unsubscribe_token=token)
    just_unsubscribed = False
    if request.method == "POST" and subscriber.status == Subscriber.Status.ACTIVE:
        subscriber.unsubscribe()
        just_unsubscribed = True
    return render(
        request,
        "curator/unsubscribe.html",
        {"subscriber": subscriber, "just_unsubscribed": just_unsubscribed},
    )


def issue_archive(request):
    """Public list of every sent issue, newest first."""
    issues = (
        DigestIssue.objects.filter(status=DigestIssue.Status.SENT)
        .annotate(event_count=Count("digest_events", filter=Q(digest_events__include_in_email=True)))
        .order_by("-target_start_date", "-sent_at")
    )
    return render(request, "curator/issues.html", {"issues": issues})


def issue_page(request, issue_date):
    """Public web version of one issue (also the email's view-in-browser target)."""
    try:
        start = datetime.strptime(issue_date, "%Y-%m-%d").date()
    except ValueError:
        raise Http404
    issues = DigestIssue.objects.filter(target_start_date=start)
    issue = issues.filter(status=DigestIssue.Status.SENT).order_by("-sent_at").first()
    if issue is None and request.user.is_staff:  # staff can proof drafts at the real URL
        issue = issues.order_by("-id").first()
    if issue is None:
        raise Http404
    # Sent issues serve their frozen snapshot; drafts (and legacy issues sent
    # before snapshots existed) render live in the current design.
    if issue.status == DigestIssue.Status.SENT and issue.rendered_html:
        return HttpResponse(issue.rendered_html)
    html, _ = render_digest(issue, unsubscribe_url="", web_version=True)
    return HttpResponse(html)


def preview_latest(request):
    """Legacy hidden preview URL — now just points at the newest public issue."""
    issue = (
        DigestIssue.objects.filter(status=DigestIssue.Status.SENT)
        .order_by("-target_start_date", "-sent_at")
        .first()
    )
    if issue is None and request.user.is_staff:
        issue = DigestIssue.objects.first()
    if issue is None:
        raise Http404
    return redirect(issue.public_path)


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"status": "ok"})
