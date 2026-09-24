from datetime import datetime, time

from django import forms
from django.forms.utils import from_current_timezone

from .models import CATEGORY_CHOICES, Event


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
