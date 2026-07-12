"""Safe outbound HTTP for connectors.

Spec §27: validate URLs before fetching (SSRF), set timeouts, limit redirects,
identify politely with a user agent.
"""

import ipaddress
import socket
from urllib.parse import urlparse

import requests
from django.conf import settings


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
    """GET a validated public URL with timeout, redirect limit, and polite UA."""
    validate_url(url)
    session = requests.Session()
    session.max_redirects = settings.INGEST_MAX_REDIRECTS
    response = session.get(
        url,
        timeout=settings.INGEST_TIMEOUT_SECONDS,
        headers={"User-Agent": settings.INGEST_USER_AGENT},
    )
    response.raise_for_status()
    return response
