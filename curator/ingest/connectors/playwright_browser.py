"""Playwright connector — real headless Chrome for pages that build their
event lists with JavaScript or sit behind browser-checking walls (spec §12F).

Use only when static fetch can't see the data: each run launches a browser,
so it's seconds per source instead of milliseconds.

Config:
{
  "url": "https://example.com/events",
  "wait_for_selector": ".event-card",     # wait until events actually render
  "wait_ms": 5000,                          # fallback settle time if no selector

  # Then EITHER reuse the HTML-config extraction:
  "event_card_selector": ".event-card",
  "title_selector": ".event-title",
  "date_selector": ".event-date",
  ...
  # OR omit event_card_selector to extract schema.org JSON-LD instead.
}

Deployment note: the server needs Chromium installed
(`playwright install chromium`), and ~512MB+ RAM headroom.
"""

from ..fetch import validate_url
from .base import BaseConnector
from .html_config import HTMLConfigConnector
from .jsonld import extract_events_from_html

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PAGE_TIMEOUT_MS = 45_000
SELECTOR_TIMEOUT_MS = 30_000
DEFAULT_WAIT_MS = 5_000


class PlaywrightConnector(BaseConnector):
    def fetch_and_extract(self):
        # Lazy import: playwright is heavy and only some deployments need it
        from playwright.sync_api import sync_playwright

        url = self.config.get("url") or self.source.url
        validate_url(url)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(user_agent=self.config.get("user_agent", BROWSER_UA))
                page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                if self.config.get("wait_for_selector"):
                    page.wait_for_selector(
                        self.config["wait_for_selector"], timeout=SELECTOR_TIMEOUT_MS
                    )
                else:
                    page.wait_for_timeout(int(self.config.get("wait_ms", DEFAULT_WAIT_MS)))
                html = page.content()
            finally:
                browser.close()

        if self.config.get("event_card_selector"):
            return HTMLConfigConnector(self.source).extract_from_html(html, base_url=url)
        return extract_events_from_html(html, base_url=url)
