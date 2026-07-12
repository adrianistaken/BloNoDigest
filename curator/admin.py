from django.contrib import admin

from .models import (
    DigestEvent,
    DigestIssue,
    EmailSend,
    Event,
    EventSource,
    EventSourceLink,
    ImportRun,
    Location,
    RawImportedEvent,
    Region,
    Subscriber,
)


@admin.register(Region)
class RegionAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "timezone", "is_active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ("email", "region", "status", "subscribed_at", "source")
    list_filter = ("status", "region")
    search_fields = ("email",)


@admin.register(EventSource)
class EventSourceAdmin(admin.ModelAdmin):
    list_display = ("name", "source_type", "url", "enabled", "last_success_at", "last_error_at")
    list_filter = ("source_type", "enabled", "region")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ("source", "status", "started_at", "events_found_count", "events_created_count")
    list_filter = ("status", "source")


@admin.register(RawImportedEvent)
class RawImportedEventAdmin(admin.ModelAdmin):
    list_display = ("raw_title", "source", "extraction_status", "created_at")
    list_filter = ("extraction_status", "source")
    search_fields = ("raw_title",)


class EventSourceLinkInline(admin.TabularInline):
    model = EventSourceLink
    extra = 0


@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("canonical_title", "starts_at", "venue_name", "status", "quality_score")
    list_filter = ("status", "region", "approved_for_digest")
    search_fields = ("canonical_title", "venue_name")
    inlines = [EventSourceLinkInline]


class DigestEventInline(admin.TabularInline):
    model = DigestEvent
    extra = 0


@admin.register(DigestIssue)
class DigestIssueAdmin(admin.ModelAdmin):
    list_display = ("title", "region", "status", "target_start_date", "sent_at")
    inlines = [DigestEventInline]


@admin.register(EmailSend)
class EmailSendAdmin(admin.ModelAdmin):
    list_display = ("digest_issue", "subscriber", "status", "sent_at")


admin.site.register(Location)
