"""TLS-pinned HTTP client for out-of-band requests.

Wraps `curl_cffi` to make raw HTTP calls that match a target Chrome's JA3 +
JA4 + HTTP/2 SETTINGS frame ordering. Use when:
  - You need to fetch JSON without rendering JS (faster, lighter)
  - You're calling an API directly that's behind TLS-fingerprint detection
  - You're building a hybrid pipeline (browser for nav, raw HTTP for bulk)

Why curl_cffi: it links against curl-impersonate, which patches OpenSSL/BoringSSL
to emit cipher orderings and TLS extensions identical to a real Chrome build.
Stock requests/aiohttp emit Python's TLS fingerprint — instantly detectable
by any TLS-aware WAF (Cloudflare, Akamai, DataDome).

The version we impersonate must match the version we report in our umbra
browser sessions (set in browser.py via `_detect_chrome_version`). Otherwise
the JA3 from this client and the UA from the browser disagree and detectors
flag the inconsistency.

Install:
    pip install curl_cffi
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("umbra.tls")


def _impersonate_for_version(chrome_version: str) -> str:
    """Map a Chrome version to a curl_cffi impersonate target.

    curl_cffi maintains a fixed set of pre-baked Chrome profiles. We pick
    the closest one to the running Chrome's major version. Falls back to
    the latest known good if no match.
    """
    major = int(chrome_version.split(".")[0])
    # curl_cffi 0.7+ supports chrome116 .. chrome131. New profiles ship
    # roughly every 2-3 Chrome releases. Pick the closest <= major.
    SUPPORTED = (131, 124, 120, 116, 110, 107, 104, 101, 100, 99)
    for v in SUPPORTED:
        if major >= v:
            return f"chrome{v}"
    return "chrome131"


def fetch(
    url: str,
    *,
    chrome_version: str = "146.0.7339.16",
    method: str = "GET",
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    proxy: str | None = None,
    timeout: float = 30.0,
    **kwargs: Any,
) -> Any:
    """Make a single HTTP request with Chrome-impersonating TLS.

    Returns a `curl_cffi.requests.Response` (same surface as `requests.Response`).
    Raises ImportError if curl_cffi isn't installed (it's an optional dep).
    """
    try:
        from curl_cffi import requests as cc_requests
    except ImportError as e:
        raise ImportError(
            "curl_cffi is required for TLS-pinned fetches. "
            "Install: pip install curl_cffi"
        ) from e

    impersonate = _impersonate_for_version(chrome_version)
    log.debug("TLS-impersonating %s for %s", impersonate, url)

    return cc_requests.request(
        method=method,
        url=url,
        headers=headers,
        cookies=cookies,
        proxies={"http": proxy, "https": proxy} if proxy else None,
        timeout=timeout,
        impersonate=impersonate,
        **kwargs,
    )


class Session:
    """Persistent curl_cffi session — keeps cookies / connection pool across calls.

    Mirrors curl_cffi.requests.Session but pre-pinned to a Chrome version.

    Use for bulk scraping where many requests hit the same host: the connection
    pool reuses TLS handshakes (faster) and cookies persist (matches a real
    session walking through a site).
    """

    def __init__(self, chrome_version: str = "146.0.7339.16", proxy: str | None = None):
        try:
            from curl_cffi import requests as cc_requests
        except ImportError as e:
            raise ImportError(
                "curl_cffi is required for TLS-pinned sessions. "
                "Install: pip install curl_cffi"
            ) from e
        self._impersonate = _impersonate_for_version(chrome_version)
        self._session = cc_requests.Session(
            impersonate=self._impersonate,
            proxies={"http": proxy, "https": proxy} if proxy else None,
        )

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        return self._session.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._session.get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._session.post(url, **kwargs)

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
