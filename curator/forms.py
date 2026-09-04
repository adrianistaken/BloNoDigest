from django import forms

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
