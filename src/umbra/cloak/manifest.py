"""Platform → CloakBrowser asset mapping + release metadata fetch.

CloakBrowser publishes GitHub releases under CloakHQ/CloakBrowser. Each release
ships per-platform archives + a SHA256SUMS file:

    cloakbrowser-linux-x64.tar.gz
    cloakbrowser-linux-arm64.tar.gz
    cloakbrowser-windows-x64.zip
    SHA256SUMS

macOS arm64 ships under separate tags (linux/win typically lead by a version
or two). No mac-x64 binary, no win-arm64. Those platforms fall through to
stock chromium with a warning.

We cache the GitHub API response 24h to stay under the 60 req/h anon limit.
Set GITHUB_TOKEN in env to lift the limit (5k/h).
"""

from __future__ import annotations

import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GH_API_RELEASES = "https://api.github.com/repos/CloakHQ/CloakBrowser/releases"
USER_AGENT = "umbra-browser-cloak-loader"

# How long to trust a cached release manifest. 24h is a sane balance — cloak
# pushes new chromium builds every few weeks, and 60 req/h anon is plenty for
# any sane usage pattern with this TTL.
MANIFEST_TTL_SECONDS = 24 * 3600


@dataclass(frozen=True)
class PlatformKey:
    """Identifies the (os, arch) we'll resolve a cloak asset for."""

    os: str   # "linux" | "darwin" | "windows"
    arch: str  # "x64" | "arm64"

    @property
    def key(self) -> str:
        return f"{self.os}-{self.arch}"


@dataclass(frozen=True)
class CloakAsset:
    """One resolved cloak release asset, ready to download."""

    tag: str           # e.g. "chromium-v146.0.7680.177.4"
    asset_name: str    # e.g. "cloakbrowser-linux-x64.tar.gz"
    asset_url: str
    sha256_url: str    # URL of SHA256SUMS for this tag
    platform: PlatformKey

    @property
    def archive_kind(self) -> str:
        if self.asset_name.endswith(".tar.gz"):
            return "tar.gz"
        if self.asset_name.endswith(".zip"):
            return "zip"
        raise ValueError(f"unknown archive kind: {self.asset_name}")


# Platforms cloak builds for. Anything else → fail-soft to stock.
SUPPORTED: dict[str, str] = {
    "linux-x64":   "cloakbrowser-linux-x64.tar.gz",
    "linux-arm64": "cloakbrowser-linux-arm64.tar.gz",
    "windows-x64": "cloakbrowser-windows-x64.zip",
    # macOS arm64 is published under separate tags — we resolve per-platform
    # by scanning releases instead of relying on "latest" containing it.
    "darwin-arm64": "cloakbrowser-darwin-arm64.tar.gz",
}


def detect_platform() -> PlatformKey | None:
    """Detect host platform. Returns None on unsupported combos."""
    sys_os = sys.platform
    if sys_os.startswith("linux"):
        os_name = "linux"
    elif sys_os == "darwin":
        os_name = "darwin"
    elif sys_os in ("win32", "cygwin"):
        os_name = "windows"
    else:
        return None

    mach = platform.machine().lower()
    if mach in ("x86_64", "amd64"):
        arch = "x64"
    elif mach in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        return None

    pk = PlatformKey(os=os_name, arch=arch)
    if pk.key not in SUPPORTED:
        return None
    return pk


def _http_get_json(url: str, timeout: float = 15.0) -> Any:
    """GET a JSON URL, sending GITHUB_TOKEN if present."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return json.loads(resp.read())


def _load_cached(cache_path: Path) -> list[dict[str, Any]] | None:
    try:
        st = cache_path.stat()
    except FileNotFoundError:
        return None
    if time.time() - st.st_mtime > MANIFEST_TTL_SECONDS:
        return None
    try:
        return json.loads(cache_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _save_cached(cache_path: Path, data: list[dict[str, Any]]) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(cache_path)


def fetch_releases(cache_path: Path, force: bool = False) -> list[dict[str, Any]]:
    """Fetch (and cache) the recent releases list from GitHub.

    We pull the list rather than `/releases/latest` because cloak publishes
    per-platform on staggered tags — the absolute newest tag may only contain
    linux/win, with mac arm64 on a slightly older tag. Scanning the list lets
    us pick the newest tag that has *our* platform's asset.
    """
    if not force:
        cached = _load_cached(cache_path)
        if cached is not None:
            return cached
    data = _http_get_json(f"{GH_API_RELEASES}?per_page=20")
    if not isinstance(data, list):
        raise RuntimeError(f"unexpected GitHub releases payload: {type(data).__name__}")
    _save_cached(cache_path, data)
    return data


def resolve_asset(
    releases: list[dict[str, Any]],
    pk: PlatformKey,
    tag: str | None = None,
) -> CloakAsset:
    """Pick the right release for this platform.

    Strategy: walk releases newest-first, return the first one that has both
    our platform's asset AND a SHA256SUMS file. If `tag` is given, require
    that specific tag.
    """
    asset_name = SUPPORTED[pk.key]
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        rel_tag = rel.get("tag_name") or ""
        if tag and rel_tag != tag:
            continue
        assets = {a["name"]: a for a in rel.get("assets", []) if isinstance(a, dict)}
        if asset_name not in assets or "SHA256SUMS" not in assets:
            continue
        return CloakAsset(
            tag=rel_tag,
            asset_name=asset_name,
            asset_url=assets[asset_name]["browser_download_url"],
            sha256_url=assets["SHA256SUMS"]["browser_download_url"],
            platform=pk,
        )
    if tag:
        raise LookupError(f"tag {tag!r} has no asset for {pk.key}")
    raise LookupError(f"no release found with asset for {pk.key}")


def parse_sha256sums(text: str, asset_name: str) -> str:
    """Extract the hex hash for `asset_name` from a SHA256SUMS file."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: "<hex>  <name>" (two spaces) or "<hex> *<name>" (binary mode)
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        hex_hash, name = parts[0], parts[1].lstrip("*").strip()
        if name == asset_name:
            if len(hex_hash) != 64 or not all(c in "0123456789abcdefABCDEF" for c in hex_hash):
                raise ValueError(f"malformed sha256 for {asset_name}: {hex_hash!r}")
            return hex_hash.lower()
    raise LookupError(f"{asset_name} not present in SHA256SUMS")
