"""The shared newsletter time style, without guessing missing AM/PM labels."""

import re
from datetime import time


_PERIOD = r"(?:[ap]\s*m|[ap]\.\s*m\.?)"
_CLOCK = rf"(?:[01]?\d|2[0-3])(?::[0-5]\d)?(?:\s*{_PERIOD})?"
_LEFT = r"(?<![\w:./])"
_RIGHT = r"(?![\w:]|\.\d)"
_RANGE_RE = re.compile(rf"{_LEFT}({_CLOCK})\s*(?:[-–—−]|to|until)\s*({_CLOCK}){_RIGHT}", re.I)
_TIME_RE = re.compile(rf"{_LEFT}{_CLOCK}{_RIGHT}", re.I)
_PARTS_RE = re.compile(rf"(\d+)(?::(\d+))?(?:\s*({_PERIOD}))?", re.I)


def format_clock(value):
    """Format a datetime/time whose local hour is already known."""
    minute = f":{value.minute:02d}" if value.minute else ""
    return f"{value.hour % 12 or 12}{minute}{'am' if value.hour < 12 else 'pm'}"


def _parts(value):
    match = _PARTS_RE.fullmatch(value)
    hour, minute, period = match.groups()
    return int(hour), int(minute or 0), re.sub(r"[^apm]", "", (period or "").lower())


def _clock_text(hour, minute, period):
    return f"{hour}{f':{minute:02d}' if minute else ''}{period}"


def _format_range(match):
    left, right = match.groups()
    start_hour, start_minute, start_period = _parts(left)
    end_hour, end_minute, end_period = _parts(right)
    if ((start_period and not 1 <= start_hour <= 12)
            or (end_period and not 1 <= end_hour <= 12)):
        return match.group()
    if not start_period and not end_period:
        # A colon and a 24-hour-only hour make this notation unambiguous.
        if ":" not in left or ":" not in right or not (
            start_hour == 0 or end_hour == 0 or start_hour > 12 or end_hour > 12
        ):
            return match.group()
        start = format_clock(time(start_hour, start_minute))
        end = format_clock(time(end_hour, end_minute))
    else:
        start = (format_clock(time(start_hour, start_minute))
                 if not start_period and ":" in left and (start_hour == 0 or start_hour > 12)
                 else _clock_text(start_hour, start_minute, start_period))
        end = (format_clock(time(end_hour, end_minute))
               if not end_period and ":" in right and (end_hour == 0 or end_hour > 12)
               else _clock_text(end_hour, end_minute, end_period))
    if start[-2:] in ("am", "pm") and start[-2:] == end[-2:]:
        start = start[:-2]
    return f"{start}-{end}"


def _format_single(match):
    hour, minute, period = _parts(match.group())
    if period:
        if not 1 <= hour <= 12:
            return match.group()
        return _clock_text(hour, minute, period)
    if ":" in match.group() and (hour == 0 or hour > 12):
        return format_clock(time(hour, minute))
    return match.group()


def normalize_event_time(value):
    """Compact recognized times; preserve labels, timezones, and unknown times.

    For example: 'Doors 5:00 PM – 8:00 PM CDT' -> 'Doors 5-8pm CDT'.
    Bare '5-8' stays ambiguous rather than acquiring an invented meridiem.
    """
    text = (value or "").strip()
    text = re.sub(r"\bnoon\b", "12pm", text, flags=re.I)
    text = re.sub(r"\bmidnight\b", "12am", text, flags=re.I)
    text = _RANGE_RE.sub(_format_range, text)
    return _TIME_RE.sub(_format_single, text)
