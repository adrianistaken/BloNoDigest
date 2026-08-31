"""Small, server-side OpenAI integration for editorial assistance."""

import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


class AIShorteningError(Exception):
    """A safe-to-display failure from the event description shortener."""


def shorten_event_description(description: str) -> str:
    """Turn scraped event copy into a concise, editable newsletter draft."""
    api_key = settings.OPENAI_API_KEY
    if not api_key:
        raise AIShorteningError(
            "AI shortening is not configured. Add OPENAI_API_KEY to the server environment."
        )

    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.OPENAI_EVENT_SHORTEN_MODEL,
                "instructions": settings.OPENAI_EVENT_SHORTEN_INSTRUCTIONS,
                "input": description,
                "max_output_tokens": 300,
                "reasoning": {"effort": "minimal"},
                "store": False,
            },
            timeout=settings.OPENAI_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except requests.Timeout as exc:
        raise AIShorteningError("The AI request timed out. Try again.") from exc
    except requests.RequestException as exc:
        logger.warning("OpenAI event-shortening request failed: %s", exc)
        raise AIShorteningError("OpenAI could not shorten this description. Try again.") from exc
    except ValueError as exc:
        logger.warning("OpenAI returned invalid JSON for event shortening")
        raise AIShorteningError("OpenAI returned an unreadable response. Try again.") from exc

    text = payload.get("output_text", "").strip()
    if not text:
        # The REST response normally exposes output_text, but accept the expanded
        # output shape as a fallback so this does not depend on an SDK helper.
        text = "".join(
            content.get("text", "")
            for item in payload.get("output", [])
            for content in item.get("content", [])
            if content.get("type") == "output_text"
        ).strip()
    if not text:
        logger.warning("OpenAI event-shortening response contained no output text")
        raise AIShorteningError("OpenAI returned an empty description. Try again.")
    return text
