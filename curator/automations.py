"""Surface the deploy-level cron schedules inside the dashboard.

The schedules are defined in railway.*-cron.json files (config-as-code, so
Railway and this repo can't drift apart). We read those same files and render
them in plain English so the admin always sees when the robots run — without
having to remember Railway exists.
"""

import json
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger("curator.automations")

CRON_JOBS = [
    {
        "name": "Nightly event import",
        "file": "railway.import-cron.json",
        "what": "Fetches every enabled source, refreshes the event roster, flags broken sources.",
    },
    {
        "name": "Thursday digest draft",
        "file": "railway.digest-cron.json",
        "what": "Assembles a draft from events you've approved. Never sends anything.",
    },
]

DAY_NAMES = {
    "0": "Sunday", "1": "Monday", "2": "Tuesday", "3": "Wednesday",
    "4": "Thursday", "5": "Friday", "6": "Saturday", "7": "Sunday",
}


def humanize_cron(expression, tz_name="America/Chicago"):
    """'0 9 * * *' -> 'every day at ~4:00 AM Central (09:00 UTC)'.
    Supports the two shapes we use: daily and weekly at a fixed UTC time."""
    try:
        minute, hour, day_of_month, month, day_of_week = expression.split()
        if day_of_month != "*" or month != "*":
            return f"cron: {expression} (UTC)"
        utc_dt = datetime.combine(
            timezone.now().date(), time(int(hour), int(minute)), tzinfo=ZoneInfo("UTC")
        )
        local = utc_dt.astimezone(ZoneInfo(tz_name))
        local_text = local.strftime("%I:%M %p").lstrip("0")
        utc_text = f"{int(hour):02d}:{int(minute):02d} UTC"
        day = DAY_NAMES.get(day_of_week)
        if day:
            return f"every {day} at ~{local_text} Central ({utc_text})"
        return f"every day at ~{local_text} Central ({utc_text})"
    except Exception:
        return f"cron: {expression} (UTC)"


def get_automations():
    """-> [{name, what, schedule, file}] read from the repo's cron configs."""
    automations = []
    for job in CRON_JOBS:
        path = settings.BASE_DIR / job["file"]
        schedule = None
        try:
            config = json.loads(path.read_text())
            expression = config.get("deploy", {}).get("cronSchedule", "")
            if expression:
                schedule = humanize_cron(expression)
        except FileNotFoundError:
            pass
        except Exception as exc:
            logger.warning("Could not read %s: %s", job["file"], exc)
        automations.append({**job, "schedule": schedule})
    return automations
