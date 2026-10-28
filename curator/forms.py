from datetime import datetime, time
from io import BytesIO

from django import forms
from django.forms.utils import from_current_timezone
from PIL import Image, UnidentifiedImageError

from .models import CATEGORY_CHOICES, Event
from .time_formatting import normalize_event_time


class SignupForm(forms.Form):
    email = forms.EmailField(max_length=254)
    # Honeypot: humans never see it, bots fill it
    website = forms.CharField(required=False, widget=forms.HiddenInput)
    source = forms.CharField(required=False, max_length=200, widget=forms.HiddenInput)


class EventForm(forms.ModelForm):
    categories = forms.MultipleChoiceField(
        choices=[(c, c) for c in CATEGORY_CHOICES],
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )

    class Meta:
        model = Event
        fields = [
            "canonical_title", "description", "starts_at", "ends_at", "time_is_known",
            "venue_name", "address_line", "city", "state", "postal_code",
            "price_text", "price_min", "price_max", "source_url", "image_url",
            "categories", "status", "quality_score", "editorial_notes",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "editorial_notes": forms.Textarea(attrs={"rows": 2}),
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}),
        }


class EventCopyForm(forms.ModelForm):
    """Curator-owned newsletter copy that survives future source imports."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.initial["editorial_time"] = normalize_event_time(self.initial.get("editorial_time"))
        self.fields["editorial_time"].widget.attrs["placeholder"] = "5-8pm or 10:30am-5pm"

    def clean_editorial_time(self):
        return normalize_event_time(self.cleaned_data["editorial_time"])

    class Meta:
        model = Event
        fields = [
            "editorial_title",
            "editorial_time",
            "editorial_location",
            "editorial_price",
            "editorial_description",
        ]
        labels = {
            "editorial_title": "Newsletter title",
            "editorial_time": "Display time",
            "editorial_location": "Display location",
            "editorial_price": "Display price",
            "editorial_description": "Newsletter description",
        }
        widgets = {
            "editorial_description": forms.Textarea(attrs={"rows": 5}),
        }


class ManualEventForm(EventForm):
    start_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    start_time = forms.TimeField(
        required=False, widget=forms.TimeInput(attrs={"type": "time"}),
        help_text="Leave blank if the time is not known.",
    )
    status = forms.ChoiceField(
        choices=[(Event.Status.APPROVED, "Approved"), (Event.Status.NEEDS_REVIEW, "Needs review")],
        initial=Event.Status.APPROVED,
        help_text="Approved events are eligible for future digest drafts. Existing drafts are unchanged.",
    )

    class Meta(EventForm.Meta):
        fields = [
            "canonical_title", "description", "start_date", "start_time", "ends_at",
            "venue_name", "address_line", "city", "state", "postal_code",
            "price_text", "source_url", "image_url", "categories", "status", "editorial_notes",
        ]
        labels = {"canonical_title": "Event title", "ends_at": "End date and time", "source_url": "Event link"}
        help_texts = {"source_url": "Optional — no website or imported source is required."}

    def clean(self):
        data = super().clean()
        if data.get("start_date"):
            try:
                starts_at = from_current_timezone(datetime.combine(
                    data["start_date"], data.get("start_time") or time.min,
                ))
            except forms.ValidationError as exc:
                self.add_error("start_time", exc)
            else:
                self.instance.starts_at = starts_at
                self.instance.time_is_known = data.get("start_time") is not None
                if data.get("ends_at") and data["ends_at"] < starts_at:
                    self.add_error("ends_at", "The end must be on or after the start.")
        return data


class QuickEventForm(EventCopyForm):
    """Create a manual event directly from its newsletter copy."""

    start_date = forms.DateField(
        label="Event date",
        widget=forms.DateInput(attrs={"type": "date"}),
        help_text="Required to place the event in upcoming digests.",
    )
    editorial_title = forms.CharField(max_length=300, label="Newsletter title")

    class Meta(EventCopyForm.Meta):
        fields = [
            "start_date", "editorial_title", "editorial_time",
            "editorial_location", "editorial_price", "source_url", "editorial_description",
        ]
        labels = {**EventCopyForm.Meta.labels, "source_url": "Event link"}
        help_texts = {"source_url": "Optional — link to event details or tickets."}


class QuickEventDraftForm(forms.Form):
    """Validate text and an optional screenshot before sending them to AI."""

    MAX_IMAGE_BYTES = 10 * 1024 * 1024
    MAX_IMAGE_PIXELS = 25_000_000
    IMAGE_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}

    source_text = forms.CharField(required=False, max_length=20_000)
    source_image = forms.FileField(required=False)

    def clean_source_image(self):
        upload = self.cleaned_data.get("source_image")
        if upload is None:
            return None
        if upload.size > self.MAX_IMAGE_BYTES:
            raise forms.ValidationError("Use a screenshot no larger than 10 MB.")
        content = upload.read(self.MAX_IMAGE_BYTES + 1)
        if len(content) > self.MAX_IMAGE_BYTES:
            raise forms.ValidationError("Use a screenshot no larger than 10 MB.")
        try:
            with Image.open(BytesIO(content), formats=list(self.IMAGE_TYPES)) as image:
                if image.width * image.height > self.MAX_IMAGE_PIXELS:
                    raise forms.ValidationError("This screenshot is too large. Crop it to the event details and try again.")
                if getattr(image, "is_animated", False):
                    raise forms.ValidationError("Use a still screenshot, not an animated image.")
                mime_type = self.IMAGE_TYPES[image.format]
                image.verify()
            # Some formats only detect truncated pixel data when decoded.
            with Image.open(BytesIO(content), formats=list(self.IMAGE_TYPES)) as image:
                image.load()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError,
                Image.DecompressionBombError) as exc:
            raise forms.ValidationError("Use a readable PNG, JPEG, or WebP screenshot.") from exc
        upload.content_type = mime_type
        upload.seek(0)
        return upload

    def clean(self):
        data = super().clean()
        if not self.errors and not data.get("source_text") and not data.get("source_image"):
            raise forms.ValidationError("Paste event text or add a screenshot first.")
        return data
