"""Resend HTTP API email backend.

Sends via https://api.resend.com instead of SMTP. Cloud hosts commonly
throttle or block outbound SMTP ports; the HTTPS API is faster and always
reachable. Activated automatically in settings when EMAIL_PROVIDER_API_KEY
looks like a Resend key ("re_...").
"""

import logging

import requests
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger("curator.emails")

API_URL = "https://api.resend.com/emails"
TIMEOUT_SECONDS = 15


class ResendAPIBackend(BaseEmailBackend):
    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            payload = {
                "from": message.from_email,
                "to": list(message.to),
                "subject": message.subject,
                "text": message.body,
            }
            for content, mimetype in getattr(message, "alternatives", None) or []:
                if mimetype == "text/html":
                    payload["html"] = content
            try:
                response = requests.post(
                    API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.EMAIL_PROVIDER_API_KEY}"},
                    timeout=TIMEOUT_SECONDS,
                )
                response.raise_for_status()
                sent += 1
            except Exception as exc:
                logger.error("Resend API send failed for %s: %s", message.to, exc)
                if not self.fail_silently:
                    raise
        return sent
