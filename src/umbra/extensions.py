"""Optional Chrome extensions, fetched on demand and cached.

Why this exists alongside the network-level blocklist: `Fetch.enable` stops
tracker REQUESTS, which is what fingerprinting cares about. It does nothing
for what is already on the page — ad slots that reserve space, cookie walls,
"subscribe" overlays — and those are what shove a form around while an agent
is clicking by coordinate. A cosmetic blocker lives in the renderer and can
hide them.

Only Manifest V3 extensions are viable: Chrome 127+ refuses MV2, so it has to
be uBlock Origin *Lite*, not the classic build. Its limits are worth knowing —
no dynamic filtering, no element picker, filter lists are compiled in — but
it needs no configuration and its default rule set covers the common noise.

Extensions are unpacked here, never committed: the CRX is pulled from the
Web Store's own update endpoint, exactly what Chrome does behind the scenes.
Nothing is loaded unless a caller asks; the flag also implies a visible
window, since `--load-extension` is ignored under --headless.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("umbra.extensions")

# Web Store ids of the extensions we know how to fetch, keyed by the short
# name a caller passes to spawn(). Adding one is a line here.
KNOWN: dict[str, str] = {
    "ublock-lite": "ddkjiahejlhfcafbddmgiahcphecmpfh",
}

# Chrome's own update URL. `acceptformat=crx3` gets the modern container;
# `prodversion` must be new enough that the store hands back an MV3 build.
_CRX_URL = (
    "https://clients2.google.com/service/update2/crx"
    "?response=redirect&acceptformat=crx2,crx3&prodversion=130.0"
    "&x=id%3D{ext_id}%26installsource%3Dondemand%26uc"
)


def cache_root() -> Path:
    override = os.environ.get("UMBRA_EXT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".umbra" / "extensions"


def _crx_to_zip(blob: bytes) -> bytes:
    """Strip the CRX header — what remains is an ordinary zip.

    CRX2: magic(4) version(4) pubkey_len(4) sig_len(4) pubkey sig zip
    CRX3: magic(4) version(4) header_len(4) header zip
    """
    if blob[:4] != b"Cr24":
        raise ValueError("not a CRX file (bad magic)")
    version = int.from_bytes(blob[4:8], "little")
    if version == 2:
        pk_len = int.from_bytes(blob[8:12], "little")
        sig_len = int.from_bytes(blob[12:16], "little")
        return blob[16 + pk_len + sig_len:]
    if version == 3:
        hdr_len = int.from_bytes(blob[8:12], "little")
        return blob[12 + hdr_len:]
    raise ValueError(f"unsupported CRX version {version}")


# Same endpoint, asking for the update manifest instead of the bytes: an XML
# document whose <updatecheck> carries the current store version. Cheap.
_CHECK_URL = (
    "https://clients2.google.com/service/update2/crx"
    "?response=updatecheck&acceptformat=crx2,crx3&prodversion=130.0"
    "&x=id%3D{ext_id}%26uc"
)


def installed_version(name: str) -> str | None:
    manifest = cache_root() / name / "manifest.json"
    if not manifest.exists():
        return None
    try:
        import json
        return json.loads(manifest.read_text()).get("version")
    except Exception:  # noqa: BLE001
        return None


def latest_version(name: str) -> str | None:
    """Ask the Web Store what version it would hand out right now."""
    ext_id = KNOWN[name]
    req = urllib.request.Request(_CHECK_URL.format(ext_id=ext_id),
                                 headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        xml = resp.read().decode("utf-8", "replace")
    import re
    m = re.search(r'<updatecheck[^>]*\bversion="([^"]+)"', xml)
    return m.group(1) if m else None


def ensure(name: str, *, force: bool = False) -> Path:
    """Return the unpacked directory for `name`, downloading on first use.

    `force` re-downloads over an existing install (the updater's path).
    Raises with a message that says what to do — a spawn that dies on a
    missing extension should tell the caller more than a traceback would.
    """
    ext_id = KNOWN.get(name)
    if ext_id is None:
        raise ValueError(
            f"unknown extension {name!r} — known: {', '.join(sorted(KNOWN))}")
    target = cache_root() / name
    if (target / "manifest.json").exists() and not force:
        return target

    url = _CRX_URL.format(ext_id=ext_id)
    log.info("fetching extension %s (%s) from the Web Store", name, ext_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            blob = resp.read()
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            f"could not download extension {name!r}: {e}. Unpack it by hand "
            f"into {target} (needs a manifest.json at the top level) and retry."
        ) from e

    tmp = target.with_suffix(".partial")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(io.BytesIO(_crx_to_zip(blob))) as zf:
        zf.extractall(tmp)
    if not (tmp / "manifest.json").exists():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"downloaded {name!r} has no manifest.json — refusing it")
    shutil.rmtree(target, ignore_errors=True)
    tmp.rename(target)
    log.info("extension %s unpacked to %s", name, target)
    return target
