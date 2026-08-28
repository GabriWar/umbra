"""Keep the cloak build and the optional extensions current — sensibly.

Both are cached forever once fetched, and neither has a channel that would
refresh it: the cloak loader only ever resolves what is already on disk, and
an unpacked extension is outside the Web Store's auto-update. Left alone,
the stealth browser drifts behind upstream Chromium — precisely the kind of
version skew detectors cross-check — and the blocker's filter lists age.

The policy here is deliberately dull:

  * Check at most once every UMBRA_UPDATE_EVERY_DAYS (default 7; 0 = never).
    A check is one metadata request per component, not a download.
  * Download only when the remote version differs from the installed one,
    and only in the background after a spawn has already returned. The
    current browser never waits; the NEXT spawn picks the new build up.
  * Install atomically (each loader already stages to `.partial` and
    renames), then prune older cloak builds — they are ~150 MB each.

State lives in ~/.umbra/updates.json so the cadence survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("umbra.updates")

_running: asyncio.Task[Any] | None = None


def _every_seconds() -> float:
    raw = os.environ.get("UMBRA_UPDATE_EVERY_DAYS", "7")
    try:
        days = float(raw)
    except ValueError:
        days = 7.0
    return max(days, 0.0) * 86400


def state_path() -> Path:
    return Path(os.environ.get("UMBRA_HOME", str(Path.home() / ".umbra"))) / "updates.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(state_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def _save_state(st: dict[str, Any]) -> None:
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=1))


# ── cloak ──────────────────────────────────────────────────────────────────

def _cloak_installed_tag() -> str | None:
    from umbra.cloak.loader import _existing_install, detect_platform
    pk = detect_platform()
    if pk is None:
        return None
    path = _existing_install(pk)
    # <cache>/<tag>/<extracted tree>/chrome — the tag is the dir under root.
    if not path:
        return None
    from umbra.cloak.loader import cache_root
    try:
        rel = path.relative_to(cache_root())
        return rel.parts[0]
    except ValueError:
        return None


def _cloak_latest_tag() -> str | None:
    from umbra.cloak.loader import cache_root, detect_platform
    from umbra.cloak.manifest import fetch_releases, resolve_asset
    pk = detect_platform()
    if pk is None:
        return None
    releases = fetch_releases(cache_root() / "manifest.json")
    return resolve_asset(releases, pk).tag


def _cloak_update(latest: str) -> str:
    from umbra.cloak.loader import cache_root, install_latest
    # Pin the tag: unpinned, install_latest is satisfied by ANY cached build
    # and hands back the stale one without touching the network.
    path = install_latest(tag=latest)
    new_tag = path.relative_to(cache_root()).parts[0]
    # Prune everything that is not the build we just installed.
    for d in cache_root().iterdir():
        if d.is_dir() and d.name != new_tag and d.name.startswith("chromium-"):
            shutil.rmtree(d, ignore_errors=True)
            log.info("pruned old cloak build %s", d.name)
    return new_tag


# ── extensions ─────────────────────────────────────────────────────────────

def _ext_names_installed() -> list[str]:
    from umbra.extensions import KNOWN, installed_version
    return [n for n in KNOWN if installed_version(n)]


# ── the check ──────────────────────────────────────────────────────────────

def check(*, force: bool = False, download: bool = True) -> dict[str, Any]:
    """Synchronous: compare installed vs latest, optionally install. Returns
    a per-component report. Safe to call from a thread."""
    st = load_state()
    now = time.time()
    every = _every_seconds()
    if not force:
        if every == 0:
            return {"skipped": "disabled (UMBRA_UPDATE_EVERY_DAYS=0)"}
        if now - st.get("last_check", 0) < every:
            return {"skipped": f"checked {int((now - st['last_check']) / 3600)}h ago",
                    **{k: v for k, v in st.items() if k != "last_check"}}

    report: dict[str, Any] = {}

    # cloak — only if a build is installed at all; we never pull one in
    # unasked (spawn's own prompt/fallback owns first install).
    try:
        installed = _cloak_installed_tag()
        if installed:
            latest = _cloak_latest_tag()
            entry = {"installed": installed, "latest": latest}
            if latest and latest != installed and download:
                got = _cloak_update(latest)
                entry["installed"] = got
                entry["updated"] = got != installed
            report["cloak"] = entry
    except Exception as e:  # noqa: BLE001
        report["cloak"] = {"error": str(e)[:200]}

    # extensions — same rule, only ones already on disk.
    from umbra.extensions import ensure, installed_version, latest_version
    for name in _ext_names_installed():
        try:
            have, want = installed_version(name), latest_version(name)
            entry = {"installed": have, "latest": want}
            if want and want != have and download:
                ensure(name, force=True)
                entry["installed"] = installed_version(name)
                entry["updated"] = True
            report[name] = entry
        except Exception as e:  # noqa: BLE001
            report[name] = {"error": str(e)[:200]}

    st.update(report)
    st["last_check"] = now
    _save_state(st)
    return report


def kick_background_check() -> bool:
    """Fire the periodic check off the spawn path. Returns whether one started."""
    global _running
    if _every_seconds() == 0:
        return False
    st = load_state()
    if time.time() - st.get("last_check", 0) < _every_seconds():
        return False
    if _running and not _running.done():
        return False

    async def _run() -> None:
        try:
            rep = await asyncio.to_thread(check)
            changed = {k: v for k, v in rep.items()
                       if isinstance(v, dict) and v.get("updated")}
            if changed:
                log.info("updated in background — next spawn uses: %s", changed)
        except Exception as e:  # noqa: BLE001
            log.warning("background update check failed: %s", e)

    _running = asyncio.get_event_loop().create_task(_run())
    return True
