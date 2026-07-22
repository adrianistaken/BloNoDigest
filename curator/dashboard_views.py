"""Internal review dashboard (spec §19/§23). Staff-only, utilitarian."""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_POST

from .automations import get_automations
from .digests import generate_digest_issue, upcoming_weekend
from .emails import day_groups, render_digest, send_digest, send_test_email
from .forms import EventForm
from .ingest.importer import import_source
from .models import (
    DigestEvent,
    DigestIssue,
    Event,
    EventSource,
    ImportRun,
    Region,
    Subscriber,
)


def _default_region():
    return Region.objects.get(slug=settings.DEFAULT_REGION_SLUG)


@staff_member_required
def home(request):
    region = _default_region()
    sources = region.sources.all()
    last_run = ImportRun.objects.filter(source__region=region).first()
    latest_digest = region.digest_issues.first()
    automations = get_automations()
    for automation in automations:
        if "import" in automation["file"]:
            automation["last_evidence"] = last_run.started_at if last_run else None
        else:
            automation["last_evidence"] = latest_digest.generated_at if latest_digest else None
    context = {
        "region": region,
        "subscriber_count": region.subscribers.filter(status=Subscriber.Status.ACTIVE).count(),
        "needs_review_count": region.events.filter(status=Event.Status.NEEDS_REVIEW).count(),
        "approved_count": region.events.filter(status=Event.Status.APPROVED).count(),
        "broken_sources": [s for s in sources if s.is_broken],
        "source_count": sources.count(),
        "last_run": last_run,
        "latest_digest": latest_digest,
        "automations": automations,
    }
    return render(request, "dashboard/home.html", context)


@staff_member_required
def sources(request):
    region = _default_region()
    source_list = (
        region.sources.all()
        .annotate(event_count=Count("primary_events", distinct=True))
        .order_by("name")
    )
    return render(request, "dashboard/sources.html", {"sources": source_list, "region": region})


@staff_member_required
@require_POST
def run_source_import(request, source_id):
    source = get_object_or_404(EventSource, pk=source_id)
    run = import_source(source)
    level = messages.SUCCESS if run.status in ("success", "partial_success") else messages.ERROR
    messages.add_message(
        request, level,
        f"{source.name}: {run.status} — found {run.events_found_count}, "
        f"created {run.events_created_count}, updated {run.events_updated_count}, "
        f"rejected {run.events_rejected_count}."
        + (f" Error: {run.error_message[:200]}" if run.error_message else ""),
    )
    return redirect("dashboard:sources")


@staff_member_required
def import_runs(request):
    runs = ImportRun.objects.select_related("source").all()[:100]
    return render(request, "dashboard/import_runs.html", {"runs": runs})


@staff_member_required
def events(request):
    region = _default_region()
    queryset = region.events.select_related("primary_source", "duplicate_of").all()

    status = request.GET.get("status", "")
    if status:
        queryset = queryset.filter(status=status)

    quick = request.GET.get("filter", "")
    today = timezone.now().date()
    friday, sunday = upcoming_weekend(region.timezone)
    if quick == "this_weekend":
        queryset = queryset.filter(starts_at__date__gte=friday, starts_at__date__lte=sunday)
    elif quick == "next_week":
        queryset = queryset.filter(
            starts_at__date__gt=sunday, starts_at__date__lte=sunday + timedelta(days=7)
        )
    elif quick == "upcoming":
        queryset = queryset.filter(starts_at__date__gte=today)
    elif quick == "duplicates":
        queryset = queryset.filter(Q(duplicate_of__isnull=False) | Q(duplicates__isnull=False)).distinct()
    elif quick == "missing_location":
        queryset = queryset.filter(venue_name="", city="", address_line="")
    elif quick == "missing_time":
        queryset = queryset.filter(time_is_known=False)

    source_slug = request.GET.get("source", "")
    if source_slug:
        queryset = queryset.filter(primary_source__slug=source_slug)

    # Column sorting: ?sort=<key>&dir=asc|desc, whitelisted to real fields
    sortable = {
        "title": "canonical_title",
        "when": "starts_at",
        "where": "city",
        "source": "primary_source__name",
        "score": "quality_score",
        "status": "status",
    }
    sort = request.GET.get("sort", "when")
    if sort not in sortable:
        sort = "when"
    direction = "desc" if request.GET.get("dir") == "desc" else "asc"
    order = sortable[sort] if direction == "asc" else f"-{sortable[sort]}"
    queryset = queryset.order_by(order, "starts_at")

    # Prebuilt header links that preserve filters and flip direction on re-click
    base_params = {"status": status, "filter": quick, "source": source_slug}
    filter_query = "&".join(f"{k}={v}" for k, v in base_params.items() if v)
    sort_columns = {}
    for key in sortable:
        next_dir = "desc" if (sort == key and direction == "asc") else "asc"
        query = f"sort={key}&dir={next_dir}"
        sort_columns[key] = {
            "url": f"?{filter_query}&{query}" if filter_query else f"?{query}",
            "arrow": ("▲" if direction == "asc" else "▼") if sort == key else "",
        }

    page = Paginator(queryset, 50).get_page(request.GET.get("page"))
    context = {
        "page": page,
        "result_count": page.paginator.count,
        "sort_columns": sort_columns,
        "statuses": Event.Status.choices,
        "sources": region.sources.order_by("name"),
        "current": {"status": status, "filter": quick, "source": source_slug},
    }
    return render(request, "dashboard/events.html", context)


@staff_member_required
def event_detail(request, event_id):
    event = get_object_or_404(
        Event.objects.select_related("primary_source", "duplicate_of"), pk=event_id
    )
    if request.method == "POST":
        form = EventForm(request.POST, instance=event)
        if form.is_valid():
            updated = form.save(commit=False)
            updated.approved_for_digest = updated.status == Event.Status.APPROVED
            updated.save()
            messages.success(request, "Event saved.")
            return redirect("dashboard:event_detail", event_id=event.pk)
    else:
        form = EventForm(instance=event)

    possible_duplicates = (
        Event.objects.filter(region=event.region, duplicate_group_key=event.duplicate_group_key)
        .exclude(pk=event.pk)
        if event.duplicate_group_key else Event.objects.none()
    )
    context = {
        "event": event,
        "form": form,
        "possible_duplicates": list(possible_duplicates) + list(event.duplicates.all()),
        "source_links": event.source_links.select_related("source"),
    }
    return render(request, "dashboard/event_detail.html", context)


@staff_member_required
@require_POST
def event_action(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    action = request.POST.get("action")
    if action == "approve":
        event.status = Event.Status.APPROVED
        event.approved_for_digest = True
        messages.success(request, f"Approved: {event.canonical_title}")
    elif action == "reject":
        event.status = Event.Status.REJECTED
        event.approved_for_digest = False
        messages.info(request, f"Rejected: {event.canonical_title}")
    elif action == "mark_duplicate":
        event.status = Event.Status.DUPLICATE
        event.approved_for_digest = False
        messages.info(request, f"Marked duplicate: {event.canonical_title}")
    event.save()
    return redirect(request.POST.get("next") or "dashboard:events")


@staff_member_required
def digests(request):
    region = _default_region()
    if request.method == "POST":
        issue = generate_digest_issue(region.slug)
        messages.success(
            request, f"Draft generated with {issue.digest_events.count()} events."
        )
        return redirect("dashboard:digest_detail", issue_id=issue.pk)
    return render(
        request, "dashboard/digests.html", {"issues": region.digest_issues.all()}
    )


@staff_member_required
def digest_detail(request, issue_id):
    issue = get_object_or_404(DigestIssue, pk=issue_id)

    if request.method == "POST":
        action = request.POST.get("action")
        if action in ("set_blurb", "toggle_drive", "remove", "restore"):
            _digest_event_action(request, issue, action)
        elif action == "update_meta":
            issue.subject_line = request.POST.get("subject_line", issue.subject_line)[:300]
            issue.intro_text = request.POST.get("intro_text", issue.intro_text)
            issue.media_enabled = request.POST.get("media_enabled") == "on"
            issue.media_url = request.POST.get("media_url", "").strip()[:1000]
            issue.media_alt = request.POST.get("media_alt", "").strip()[:300]
            issue.media_caption = request.POST.get("media_caption", "").strip()[:300]
            issue.media_link = request.POST.get("media_link", "").strip()[:1000]
            if request.POST.get("media_placement") in dict(DigestIssue.MediaPlacement.choices):
                issue.media_placement = request.POST["media_placement"]
            issue.save()
            messages.success(request, "Digest details saved.")
        elif action == "send_test":
            to = send_test_email(issue, request.POST.get("test_email") or None)
            messages.success(request, f"Test email sent to {to}.")
        elif action == "refresh_snapshot":
            if issue.status == DigestIssue.Status.SENT:
                issue.rendered_html, _ = render_digest(issue, unsubscribe_url="", web_version=True)
                issue.save(update_fields=["rendered_html", "updated_at"])
                messages.success(request, "Public page re-rendered in the current design.")
            else:
                messages.error(request, "Only sent issues have a public page snapshot.")
        elif action == "send_final":
            if issue.status == DigestIssue.Status.SENT:
                messages.error(request, "This issue was already sent.")
            else:
                sent, failed = send_digest(issue)
                messages.success(request, f"Digest sent to {sent} subscribers ({failed} failed).")
        return redirect("dashboard:digest_detail", issue_id=issue.pk)

    context = {
        "issue": issue,
        # Same grouping the email uses, so the builder mirrors what readers see
        "day_groups": day_groups(issue),
        "removed": issue.digest_events.filter(include_in_email=False).select_related("event"),
        "active_subscriber_count": issue.region.subscribers.filter(
            status=Subscriber.Status.ACTIVE
        ).count(),
    }
    return render(request, "dashboard/digest_detail.html", context)


def _digest_event_action(request, issue, action):
    digest_event = get_object_or_404(
        DigestEvent, pk=request.POST.get("digest_event_id"), digest_issue=issue
    )
    if action == "set_blurb":
        digest_event.custom_title = request.POST.get("custom_title", "").strip()[:300]
        digest_event.custom_location = request.POST.get("custom_location", "").strip()[:300]
        digest_event.custom_blurb = request.POST.get("custom_blurb", "")
        digest_event.save(update_fields=["custom_title", "custom_location", "custom_blurb"])
    elif action == "toggle_drive":
        # The email only distinguishes 'worth_the_drive' from everything else,
        # so the return target just needs to be any non-drive section.
        digest_event.section = (
            "top_picks" if digest_event.section == "worth_the_drive" else "worth_the_drive"
        )
        digest_event.save(update_fields=["section"])
    elif action == "remove":
        digest_event.include_in_email = False
        digest_event.save(update_fields=["include_in_email"])
    elif action == "restore":
        digest_event.include_in_email = True
        digest_event.save(update_fields=["include_in_email"])


@staff_member_required
@xframe_options_sameorigin
def digest_preview(request, issue_id):
    """Raw email HTML, loaded in an iframe on the digest page."""
    issue = get_object_or_404(DigestIssue, pk=issue_id)
    html, _ = render_digest(issue, unsubscribe_url="#")
    return HttpResponse(html)


@staff_member_required
def subscribers(request):
    region = _default_region()
    queryset = region.subscribers.order_by("-subscribed_at")
    counts = {
        "active": queryset.filter(status=Subscriber.Status.ACTIVE).count(),
        "unsubscribed": queryset.filter(status=Subscriber.Status.UNSUBSCRIBED).count(),
        "total": queryset.count(),
    }
    return render(
        request,
        "dashboard/subscribers.html",
        {"counts": counts, "recent": queryset[:100]},
    )
