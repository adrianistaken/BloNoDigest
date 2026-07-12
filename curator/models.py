import secrets

from django.db import models
from django.utils import timezone
from django.utils.timezone import now as tz_now


def make_token():
    return secrets.token_urlsafe(32)


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Region(TimestampedModel):
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    center_latitude = models.FloatField(null=True, blank=True)
    center_longitude = models.FloatField(null=True, blank=True)
    default_radius_miles = models.PositiveIntegerField(default=25)
    timezone = models.CharField(max_length=64, default="America/Chicago")
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class Subscriber(TimestampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active"
        UNSUBSCRIBED = "unsubscribed"
        BOUNCED = "bounced"
        COMPLAINED = "complained"

    region = models.ForeignKey(Region, on_delete=models.PROTECT, related_name="subscribers")
    email = models.EmailField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    subscribed_at = models.DateTimeField(default=timezone.now)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    source = models.CharField(max_length=200, blank=True)  # referrer/UTM
    confirmation_token = models.CharField(max_length=64, default=make_token, unique=True)
    unsubscribe_token = models.CharField(max_length=64, default=make_token, unique=True)
    last_email_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["region", "email"], name="unique_subscriber_per_region"),
        ]

    def __str__(self):
        return self.email

    def unsubscribe(self):
        self.status = self.Status.UNSUBSCRIBED
        self.unsubscribed_at = timezone.now()
        self.save(update_fields=["status", "unsubscribed_at", "updated_at"])


class EventSource(TimestampedModel):
    class SourceType(models.TextChoices):
        TICKETMASTER_API = "ticketmaster_api"
        ICS = "ics"
        RSS = "rss"
        JSON_LD = "json_ld"
        HTML_CONFIG = "html_config"
        PLAYWRIGHT_CONFIG = "playwright_config"
        WHEREABOUTS_API = "whereabouts_api"
        MANUAL = "manual"

    class CrawlFrequency(models.TextChoices):
        DAILY = "daily"
        WEEKLY = "weekly"

    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="sources")
    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=100)
    source_type = models.CharField(max_length=30, choices=SourceType.choices)
    url = models.URLField(max_length=1000, blank=True)
    enabled = models.BooleanField(default=True)
    crawl_frequency = models.CharField(
        max_length=20, choices=CrawlFrequency.choices, default=CrawlFrequency.DAILY
    )
    parser_config = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True)
    reliability_score = models.IntegerField(default=0)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    last_error_message = models.TextField(blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["region", "slug"], name="unique_source_slug_per_region"),
        ]

    def __str__(self):
        return f"{self.name} ({self.source_type})"

    @property
    def is_broken(self):
        """Broken = last attempt errored more recently than the last success."""
        if not self.last_error_at:
            return False
        return not self.last_success_at or self.last_error_at > self.last_success_at


class ImportRun(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending"
        RUNNING = "running"
        SUCCESS = "success"
        PARTIAL_SUCCESS = "partial_success"
        FAILED = "failed"

    source = models.ForeignKey(EventSource, on_delete=models.CASCADE, related_name="import_runs")
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    events_found_count = models.IntegerField(default=0)
    events_created_count = models.IntegerField(default=0)
    events_updated_count = models.IntegerField(default=0)
    events_rejected_count = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    raw_log = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.source.name} @ {self.started_at:%Y-%m-%d %H:%M} [{self.status}]"


class RawImportedEvent(models.Model):
    class ExtractionStatus(models.TextChoices):
        EXTRACTED = "extracted"
        NORMALIZED = "normalized"
        REJECTED = "rejected"
        ERROR = "error"

    import_run = models.ForeignKey(ImportRun, on_delete=models.CASCADE, related_name="raw_events")
    source = models.ForeignKey(EventSource, on_delete=models.CASCADE, related_name="raw_events")
    raw_payload_json = models.JSONField(default=dict, blank=True)
    raw_title = models.TextField(blank=True)
    raw_description = models.TextField(blank=True)
    raw_start = models.CharField(max_length=200, blank=True)
    raw_end = models.CharField(max_length=200, blank=True)
    raw_location = models.TextField(blank=True)
    raw_url = models.URLField(max_length=1000, blank=True)
    extraction_status = models.CharField(
        max_length=20, choices=ExtractionStatus.choices, default=ExtractionStatus.EXTRACTED
    )
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.raw_title[:60] or f"raw#{self.pk}"


CATEGORY_CHOICES = [
    "family", "kids", "free", "cheap", "music", "food_drink", "outdoor",
    "arts_culture", "sports", "market", "festival", "date_night",
    "educational", "community", "other",
]


class Event(TimestampedModel):
    class Status(models.TextChoices):
        IMPORTED = "imported"
        NEEDS_REVIEW = "needs_review"
        APPROVED = "approved"
        REJECTED = "rejected"
        EXPIRED = "expired"
        DUPLICATE = "duplicate"

    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="events")
    canonical_title = models.CharField(max_length=500)
    description = models.TextField(blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    time_is_known = models.BooleanField(default=True)  # spec §14: don't invent times
    timezone = models.CharField(max_length=64, default="America/Chicago")
    venue_name = models.CharField(max_length=300, blank=True)
    address_line = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=50, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    price_text = models.CharField(max_length=200, blank=True)
    price_min = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    price_max = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True)
    source_url = models.URLField(max_length=1000, blank=True)
    image_url = models.URLField(max_length=1000, blank=True)
    primary_source = models.ForeignKey(
        EventSource, on_delete=models.SET_NULL, null=True, blank=True, related_name="primary_events"
    )
    categories = models.JSONField(default=list, blank=True)
    tags = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEEDS_REVIEW)
    quality_score = models.IntegerField(default=0)
    duplicate_group_key = models.CharField(max_length=300, blank=True, db_index=True)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )
    editorial_notes = models.TextField(blank=True)
    approved_for_digest = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(default=tz_now)

    class Meta:
        ordering = ["starts_at"]
        indexes = [
            models.Index(fields=["region", "status", "starts_at"]),
        ]

    def __str__(self):
        return f"{self.canonical_title} @ {self.starts_at:%Y-%m-%d}"

    @property
    def missing_data_warnings(self):
        warnings = []
        if not self.venue_name and not self.city and not self.address_line:
            warnings.append("missing location")
        if not self.description:
            warnings.append("missing description")
        if not self.source_url:
            warnings.append("missing source URL")
        if not self.time_is_known:
            warnings.append("time unknown")
        return warnings

    @property
    def location_display(self):
        parts = [p for p in (self.venue_name, self.city) if p]
        return ", ".join(parts) or self.address_line or "Location TBD"


class EventSourceLink(models.Model):
    """The same real-world event seen on an additional source (spec §10/§15)."""

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="source_links")
    source = models.ForeignKey(EventSource, on_delete=models.CASCADE, related_name="event_links")
    source_url = models.URLField(max_length=1000, blank=True)
    source_title = models.CharField(max_length=500, blank=True)
    source_payload_json = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["event", "source", "source_url"], name="unique_event_source_url"),
        ]


DIGEST_SECTIONS = [
    ("top_picks", "Top picks"),
    ("family_friendly", "Family-friendly"),
    ("free_cheap", "Free or cheap"),
    ("date_night", "Date night"),
    ("music_food", "Music, food, and downtown"),
    ("outdoor_markets", "Outdoor, markets, and festivals"),
    ("worth_the_drive", "Worth the drive"),
    ("next_week", "Coming up next week"),
]


class DigestIssue(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft"
        REVIEWED = "reviewed"
        SENT = "sent"
        ARCHIVED = "archived"

    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="digest_issues")
    title = models.CharField(max_length=300)
    subject_line = models.CharField(max_length=300)
    intro_text = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)
    target_start_date = models.DateField()
    target_end_date = models.DateField()
    generated_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-target_start_date"]

    def __str__(self):
        return self.title

    def sections_with_events(self):
        """Ordered [(section_key, label, [DigestEvent...]), ...] skipping empty sections."""
        by_section = {}
        for de in self.digest_events.filter(include_in_email=True).select_related("event").order_by("position"):
            by_section.setdefault(de.section, []).append(de)
        return [(key, label, by_section[key]) for key, label in DIGEST_SECTIONS if key in by_section]


class DigestEvent(models.Model):
    digest_issue = models.ForeignKey(DigestIssue, on_delete=models.CASCADE, related_name="digest_events")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="digest_appearances")
    section = models.CharField(max_length=30, choices=DIGEST_SECTIONS)
    position = models.PositiveIntegerField(default=0)
    custom_blurb = models.TextField(blank=True)
    include_in_email = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["section", "position"]
        constraints = [
            models.UniqueConstraint(fields=["digest_issue", "event"], name="unique_event_per_digest"),
        ]

    BLURB_MAX_CHARS = 170

    @property
    def blurb(self):
        """Admin-written blurbs run verbatim; auto-blurbs get a tight,
        word-boundary cut so email items stay scannable."""
        if self.custom_blurb:
            return self.custom_blurb
        text = (self.event.description or "").strip()
        if len(text) <= self.BLURB_MAX_CHARS:
            return text
        cut = text[: self.BLURB_MAX_CHARS]
        if " " in cut:
            cut = cut[: cut.rfind(" ")]
        return cut.rstrip(".,;:") + "…"


class EmailSend(models.Model):
    class Status(models.TextChoices):
        SENT = "sent"
        FAILED = "failed"

    digest_issue = models.ForeignKey(DigestIssue, on_delete=models.CASCADE, related_name="email_sends")
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name="email_sends")
    email_provider_message_id = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SENT)
    sent_at = models.DateTimeField(default=timezone.now)
    opened_at = models.DateTimeField(null=True, blank=True)
    clicked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class Location(models.Model):
    """Permanent geocoding cache (spec §29). Populated when geocoding is enabled."""

    raw_address = models.CharField(max_length=500, unique=True)
    normalized_address = models.CharField(max_length=500, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    provider = models.CharField(max_length=100, blank=True)
    geocoded_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.raw_address
