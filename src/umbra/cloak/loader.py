"""Download + verify + cache CloakBrowser chromium binaries.

Cache layout:
    ~/.umbra/cloak/
      manifest.json                   # GitHub releases response, 24h TTL
      .lock                           # flock for concurrent installs
      <tag>/                          # extracted release tree
        chrome[.exe]                  # entry point used by nodriver
        ...
      <tag>.partial/                  # extraction-in-progress (renamed on success)

The binary name inside the archive varies per build (cloak repackages upstream
chromium so the launcher is sometimes `chrome`, sometimes `chrome.exe`, and
on linux sometimes wrapped in a `chrome-linux` subdirectory). After extract
we scan the tree for the first executable named like a chromium entry point
and persist its path in <tag>/.entry so subsequent resolves don't re-scan.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .manifest import (
    CloakAsset,
    PlatformKey,
    USER_AGENT,
    detect_platform,
    fetch_releases,
    parse_sha256sums,
    resolve_asset,
)

log = logging.getLogger("umbra.cloak")


class CloakUnavailable(RuntimeError):
    """Raised when cloak can't be resolved (unsupported platform / DL fail).

    Callers should catch this and fall back to stock chromium.
    """


# Override-able for testing.
def cache_root() -> Path:
    override = os.environ.get("UMBRA_CLOAK_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".umbra" / "cloak"


def cloak_supported() -> bool:
    """Cheap check: would this platform potentially have a cloak build?"""
    return detect_platform() is not None


# ---------------------------------------------------------------------------
# flock — cross-platform, no new deps
# ---------------------------------------------------------------------------

@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Best-effort exclusive lock so concurrent spawns serialize installs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            # LK_LOCK blocks (retries) for ~10s then raises. Good enough.
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except OSError as e:
                raise CloakUnavailable(f"could not acquire install lock: {e}") from e
            try:
                yield
            finally:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# ---------------------------------------------------------------------------
# HTTP download with sha256 streaming
# ---------------------------------------------------------------------------

def _http_get_text(url: str, timeout: float = 30.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read().decode("utf-8", errors="replace")


def _download_with_sha256(url: str, dest: Path, timeout: float = 600.0) -> str:
    """Stream `url` → `dest`, return hex sha256. Caller verifies."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    h = hashlib.sha256()
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:  # noqa: S310
        while True:
            chunk = resp.read(1024 * 256)
            if not chunk:
                break
            h.update(chunk)
            f.write(chunk)
    tmp.replace(dest)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# archive extraction
# ---------------------------------------------------------------------------

# Names we'll accept as the chromium entry point inside an extracted archive.
_ENTRY_CANDIDATES = (
    "chrome", "chromium",
    "chrome.exe", "chromium.exe",
    "Chromium",  # mac .app interior
)


def _is_within(base: Path, target: Path) -> bool:
    """Guard against tar/zip path traversal."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _extract(archive: Path, dest: Path, kind: str) -> None:
    """Extract tar.gz or zip into dest, refusing any entry outside dest."""
    dest.mkdir(parents=True, exist_ok=True)
    if kind == "tar.gz":
        with tarfile.open(archive, "r:gz") as tf:
            for m in tf.getmembers():
                out = dest / m.name
                if not _is_within(dest, out):
                    raise CloakUnavailable(f"unsafe tar entry: {m.name!r}")
            # filter='data' is the safe extraction mode (Python 3.12+ /
            # default in 3.14). Rejects setuid bits, special files, etc.
            # Our own _is_within guard above catches path traversal too.
            tf.extractall(dest, filter="data")
    elif kind == "zip":
        with zipfile.ZipFile(archive, "r") as zf:
            for n in zf.namelist():
                out = dest / n
                if not _is_within(dest, out):
                    raise CloakUnavailable(f"unsafe zip entry: {n!r}")
            zf.extractall(dest)
    else:
        raise CloakUnavailable(f"unknown archive kind: {kind}")


def _find_entry(root: Path) -> Path | None:
    """Find chromium executable inside the extracted tree.

    Prefer top-level `chrome[.exe]`; fall back to first match below.
    """
    # Top-level fast path.
    for name in _ENTRY_CANDIDATES:
        cand = root / name
        if cand.is_file():
            return cand
    # Recursive — pick the shallowest match for determinism.
    matches: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.name in _ENTRY_CANDIDATES:
            matches.append(p)
    if not matches:
        return None
    matches.sort(key=lambda p: (len(p.parts), str(p)))
    return matches[0]


# ---------------------------------------------------------------------------
# public surface
# ---------------------------------------------------------------------------

def _existing_install(pk: PlatformKey, tag: str | None = None) -> Path | None:
    """Return a usable cached binary for this platform, or None."""
    root = cache_root()
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir() or child.name.endswith(".partial"):
            continue
        if tag and child.name != tag:
            continue
        entry_file = child / ".entry"
        if entry_file.is_file():
            try:
                p = Path(entry_file.read_text().strip())
                if p.is_file():
                    candidates.append(p)
                    continue
            except OSError:
                pass
        # Fall back to a fresh scan in case .entry was lost.
        found = _find_entry(child)
        if found and found.is_file():
            try:
                entry_file.write_text(str(found))
            except OSError:
                pass
            candidates.append(found)
    if not candidates:
        return None
    # Newest tag wins — sort by parent dir name (release tags are version-y
    # and sort lexicographically the way we want for cloak's scheme).
    candidates.sort(key=lambda p: p.parent.name, reverse=True)
    return candidates[0]


def install_latest(
    *,
    force: bool = False,
    tag: str | None = None,
) -> Path:
    """Download + cache the latest (or pinned) cloak build for this platform.

    Returns the path to the cached chrome executable. Raises CloakUnavailable
    on unsupported platform or any download/verify failure.
    """
    pk = detect_platform()
    if pk is None:
        raise CloakUnavailable(
            f"no CloakBrowser build for platform {sys.platform}/{os.uname().machine if hasattr(os, 'uname') else '?'}"
        )

    root = cache_root()
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock"

    with _file_lock(lock_path):
        # Re-check inside lock — another process may have just installed.
        if not force:
            existing = _existing_install(pk, tag=tag)
            if existing is not None:
                return existing

        manifest_path = root / "manifest.json"
        try:
            releases = fetch_releases(manifest_path, force=force)
        except Exception as e:  # noqa: BLE001
            raise CloakUnavailable(f"could not fetch cloak release manifest: {e}") from e

        try:
            asset: CloakAsset = resolve_asset(releases, pk, tag=tag)
        except LookupError as e:
            raise CloakUnavailable(str(e)) from e

        target_dir = root / asset.tag
        if target_dir.is_dir() and not force:
            existing = _find_entry(target_dir)
            if existing:
                (target_dir / ".entry").write_text(str(existing))
                return existing

        log.info("downloading CloakBrowser %s (%s) ...", asset.tag, asset.asset_name)

        # Download archive + SHA256SUMS into a temp scratch dir, then verify.
        with tempfile.TemporaryDirectory(prefix="umbra-cloak-", dir=str(root)) as tmpd:
            tmp = Path(tmpd)
            archive_path = tmp / asset.asset_name
            try:
                got_hash = _download_with_sha256(asset.asset_url, archive_path)
                sums_text = _http_get_text(asset.sha256_url)
            except Exception as e:  # noqa: BLE001
                raise CloakUnavailable(f"download failed: {e}") from e

            expected = parse_sha256sums(sums_text, asset.asset_name)
            if got_hash != expected:
                raise CloakUnavailable(
                    f"sha256 mismatch for {asset.asset_name}: "
                    f"expected {expected}, got {got_hash}"
                )

            # Extract into <tag>.partial then atomically rename to <tag>.
            partial = root / f"{asset.tag}.partial"
            if partial.exists():
                shutil.rmtree(partial, ignore_errors=True)
            try:
                _extract(archive_path, partial, asset.archive_kind)
            except Exception as e:  # noqa: BLE001
                shutil.rmtree(partial, ignore_errors=True)
                raise CloakUnavailable(f"extract failed: {e}") from e

            entry = _find_entry(partial)
            if entry is None:
                shutil.rmtree(partial, ignore_errors=True)
                raise CloakUnavailable(
                    f"no chromium entry point found in {asset.asset_name}"
                )

            # chmod +x for posix.
            if sys.platform != "win32":
                try:
                    entry.chmod(entry.stat().st_mode | 0o111)
                except OSError:
                    pass

            if target_dir.exists():
                shutil.rmtree(target_dir, ignore_errors=True)
            partial.rename(target_dir)
            # Re-locate the entry under the renamed parent.
            rel = entry.relative_to(partial)
            final_entry = target_dir / rel
            (target_dir / ".entry").write_text(str(final_entry))
            log.info("CloakBrowser installed at %s", final_entry)
            return final_entry


def resolve_cloak_binary(
    *,
    auto_download: bool = True,
    tag: str | None = None,
) -> Path:
    """Return path to a usable cloak chromium binary.

    Order of resolution:
      1. UMBRA_CLOAK_BINARY env var (explicit path)
      2. existing cached install for this platform
      3. if auto_download: install_latest()
      4. raise CloakUnavailable
    """
    env_path = os.environ.get("UMBRA_CLOAK_BINARY")
    if env_path:
        p = Path(env_path).expanduser()
        if p.is_file():
            return p
        raise CloakUnavailable(f"UMBRA_CLOAK_BINARY={env_path!r} does not exist")

    pk = detect_platform()
    if pk is None:
        raise CloakUnavailable("platform not supported by CloakBrowser")

    existing = _existing_install(pk, tag=tag)
    if existing is not None:
        return existing

    if not auto_download:
        raise CloakUnavailable("no cached cloak install (auto_download=False)")

    return install_latest(tag=tag)


def cloak_status() -> dict[str, object]:
    """Snapshot of cloak install state — used by MCP `cloak_status` tool."""
    pk = detect_platform()
    installed: list[dict[str, str]] = []
    root = cache_root()
    if root.is_dir():
        for child in sorted(root.iterdir(), reverse=True):
            if not child.is_dir() or child.name.endswith(".partial"):
                continue
            entry_file = child / ".entry"
            entry_path: Path | None = None
            if entry_file.is_file():
                try:
                    entry_path = Path(entry_file.read_text().strip())
                except OSError:
                    entry_path = None
            if entry_path is None or not entry_path.is_file():
                entry_path = _find_entry(child)
            if entry_path:
                installed.append({"tag": child.name, "path": str(entry_path)})
    env_override = os.environ.get("UMBRA_CLOAK_BINARY")
    kill_switch = bool(os.environ.get("UMBRA_NO_CLOAK"))
    return {
        "platform": pk.key if pk else None,
        "supported": pk is not None,
        "kill_switch": kill_switch,
        "env_binary": env_override,
        "cache_root": str(root),
        "installed": installed,
    }
