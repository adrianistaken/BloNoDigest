"""Safe outbound HTTP for connectors.

Spec §27: validate URLs before fetching (SSRF), set timeouts, limit redirects,
identify politely with a user agent.
"""

import ipaddress
import logging
import socket
import time
from urllib.parse import urlparse

import requests
from django.conf import settings

logger = logging.getLogger("curator.ingest")
RETRYABLE_STATUSES = {405, 429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3


class UnsafeURLError(Exception):
    pass


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeURLError(f"Refusing non-http(s) URL: {url}")
    host = parsed.hostname
    if not host:
        raise UnsafeURLError(f"URL has no host: {url}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UnsafeURLError(f"Cannot resolve host {host}: {e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise UnsafeURLError(f"Refusing private/loopback address for {host}")


def fetch_url(url: str) -> requests.Response:
    """GET a validated URL, retrying short-lived blocking and server errors."""
    validate_url(url)
    session = requests.Session()
    session.max_redirects = settings.INGEST_MAX_REDIRECTS
    headers = {
        "User-Agent": settings.INGEST_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = session.get(
                url,
                timeout=settings.INGEST_TIMEOUT_SECONDS,
                headers=headers,
            )
            if response.status_code not in RETRYABLE_STATUSES or attempt == MAX_ATTEMPTS:
                response.raise_for_status()
                return response
            retry_after = response.headers.get("Retry-After", "")
            delay = float(retry_after) if retry_after.isdigit() else 1.5 * (2 ** (attempt - 1))
            logger.warning(
                "Fetch attempt %d/%d returned HTTP %d for %s; retrying in %.1fs",
                attempt, MAX_ATTEMPTS, response.status_code, url, delay,
            )
            time.sleep(min(delay, 10))
        except (requests.ConnectionError, requests.Timeout):
            if attempt == MAX_ATTEMPTS:
                raise
            delay = 1.5 * (2 ** (attempt - 1))
            logger.warning(
                "Fetch attempt %d/%d could not reach %s; retrying in %.1fs",
                attempt, MAX_ATTEMPTS, url, delay,
                exc_info=True,
            )
            time.sleep(min(delay, 10))

    raise RuntimeError("Fetch retry loop ended unexpectedly")
