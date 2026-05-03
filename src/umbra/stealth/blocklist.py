"""Tracker / ad / fingerprinting domain blocker.

Loads the 3520-entry pgl_yoyo list (sourced from h4ckf0r0day/obscura,
Apache-2.0). HashSet lookup with subdomain fallback — O(depth) per request.

Used by the network interceptor to fail tracker requests before the response
ever hits the page. Two wins:
  - Speed: don't waste bandwidth on analytics/ads
  - Stealth: tracker scripts often re-fingerprint the browser; blocking them
    means fewer signals leave the box.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from urllib.parse import urlparse


@lru_cache(maxsize=1)
def _load_blocklist() -> frozenset[str]:
    """Read the bundled domain list. Cached after first call."""
    text = resources.files("umbra.stealth").joinpath("tracker_domains.txt").read_text()
    return frozenset(
        line.strip().lower()
        for line in text.splitlines()
        if line.strip() and not line.startswith("#")
    )


def is_blocked(url_or_host: str) -> bool:
    """True if the host (or any parent domain) is on the blocklist.

    Accepts either a full URL or a bare hostname. Subdomain match is greedy:
    `ads.tracker.com` matches if `tracker.com` is in the list.
    """
    if not url_or_host:
        return False
    host = url_or_host
    if "://" in url_or_host:
        host = urlparse(url_or_host).hostname or ""
    host = host.lower().strip(".")
    if not host:
        return False
    blocklist = _load_blocklist()
    if host in blocklist:
        return True
    # Walk up the subdomain chain
    parts = host.split(".")
    for i in range(1, len(parts) - 1):
        if ".".join(parts[i:]) in blocklist:
            return True
    return False


def blocklist_size() -> int:
    """Number of root domains in the bundled list."""
    return len(_load_blocklist())
