"""Encrypted session persistence — log into a site once, reuse forever.

Usage from MCP tools (`session_save`/`session_load`) or directly:

    from umbra import StealthBrowser
    from umbra.session import Session

    # First run: log in manually (or via handoff), then save
    async with StealthBrowser() as b:
        tab = await b.new_tab("https://github.com/login")
        # ... login flow ...
        await Session.save(tab, name="github-me", passphrase="...")

    # Next run: skip login entirely
    async with StealthBrowser() as b:
        tab = await b.new_tab("https://github.com")
        await Session.load(tab, name="github-me", passphrase="...")
        # already logged in, cookies + localStorage injected

Encryption: Fernet (AES-128-CBC + HMAC-SHA256, time-stamped). Key derived
from passphrase via PBKDF2-HMAC-SHA256 (200k iterations, per-entry salt).
Per-(domain, name) namespacing; multi-account isolation built in.

Storage: ~/.local/share/umbra/sessions/{domain}/{name}.fern  (XDG-compliant)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("umbra.session")

# 200k PBKDF2 rounds — OWASP 2023 recommendation for SHA256.
_KDF_ITERATIONS = 200_000


def _session_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    d = Path(base) / "umbra" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a Fernet-compatible 32-byte urlsafe-base64 key from a passphrase."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_KDF_ITERATIONS)
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]")


def _sanitize_segment(s: str) -> str:
    """Strip every character that's not alnum/dot/dash/underscore.
    Defends against path traversal in user-supplied session names + url hosts.
    Empty result → '_'."""
    cleaned = _SAFE_NAME_RE.sub("_", s).strip("._")
    return cleaned or "_"


def _path_for(name: str, url: str | None = None) -> Path:
    """Build the on-disk path for a session blob.

    `name` is the user-facing label (e.g. "github-me"). If `url` is supplied
    we namespace by its domain so the same name can mean different things on
    different sites. If `url` is None, blobs land in ~/.../sessions/_default/.

    SECURITY: `name` and the resolved `domain` are sanitized to alphanum +
    `._-` only. Without this, name='../../../tmp/pwn' would write outside
    the sessions directory (path traversal).
    """
    domain = "_default"
    if url:
        host = urlparse(url).hostname
        if host:
            domain = host.lower().removeprefix("www.")
    safe_name = _sanitize_segment(name)
    safe_domain = _sanitize_segment(domain)
    full = _session_dir() / safe_domain / f"{safe_name}.fern"
    # Belt + suspenders: resolve and verify still under sessions dir.
    sessions_root = _session_dir().resolve()
    resolved = full.resolve() if full.exists() else (full.parent.resolve() / full.name)
    if not str(resolved).startswith(str(sessions_root)):
        raise ValueError(f"session path escaped sessions dir: {resolved}")
    return full


class Session:
    """Static API — no instance state, sessions live on disk."""

    @staticmethod
    async def save(tab: Any, name: str, passphrase: str) -> Path:
        """Snapshot the tab's cookies + localStorage and write encrypted blob."""
        try:
            from cryptography.fernet import Fernet
        except ImportError as e:
            raise ImportError(
                "umbra.session requires the `cryptography` package. "
                "Install via: pip install umbra-browser[sessions]"
            ) from e

        import nodriver as uc
        cdp = uc.cdp

        # Pull cookies for ALL hosts (Network.getCookies with no urls = all)
        cookies_resp = await tab.send(cdp.network.get_cookies())
        cookies = [
            {
                "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "expires": getattr(c, "expires", -1), "httpOnly": c.http_only,
                "secure": c.secure, "sameSite": str(getattr(c, "same_site", "") or ""),
            }
            for c in cookies_resp
        ]

        # Pull localStorage + sessionStorage for the current origin via JS.
        storage = await tab.evaluate("""(() => {
            const dump = (s) => {
                const o = {};
                for (let i = 0; i < s.length; i++) {
                    const k = s.key(i); o[k] = s.getItem(k);
                }
                return o;
            };
            return JSON.stringify({
                local: dump(localStorage),
                session: dump(sessionStorage),
                origin: location.origin,
            });
        })()""")
        storage_obj = json.loads(storage) if isinstance(storage, str) else {"local": {}, "session": {}, "origin": ""}

        url = await tab.evaluate("location.href")
        payload = {
            "version": 1,
            "saved_at": int(time.time()),
            "url": url,
            "origin": storage_obj.get("origin"),
            "cookies": cookies,
            "localStorage": storage_obj.get("local", {}),
            "sessionStorage": storage_obj.get("session", {}),
        }

        # Per-entry random salt prepended to ciphertext so different blobs from
        # the same passphrase produce different ciphertexts.
        salt = os.urandom(16)
        key = _derive_key(passphrase, salt)
        token = Fernet(key).encrypt(json.dumps(payload).encode("utf-8"))

        path = _path_for(name, url if isinstance(url, str) else None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(salt + token)
        path.chmod(0o600)
        log.info("session saved → %s (%d cookies, %d ls keys)", path, len(cookies), len(payload["localStorage"]))
        return path

    @staticmethod
    async def load(tab: Any, name: str, passphrase: str, *, url_hint: str | None = None) -> dict[str, Any]:
        """Decrypt a saved session and inject into the tab. Returns metadata."""
        try:
            from cryptography.fernet import Fernet, InvalidToken
        except ImportError as e:
            raise ImportError("install umbra-browser[sessions]") from e

        path = _path_for(name, url_hint)
        if not path.exists():
            # Fall back to default-domain location for ambiguous loads
            for candidate in _session_dir().rglob(f"{name}.fern"):
                path = candidate
                break
            else:
                raise FileNotFoundError(f"no saved session named {name!r}")

        blob = path.read_bytes()
        salt, token = blob[:16], blob[16:]
        key = _derive_key(passphrase, salt)
        try:
            payload = json.loads(Fernet(key).decrypt(token).decode("utf-8"))
        except InvalidToken as e:
            raise PermissionError("wrong passphrase") from e

        # Inject cookies via Network.setCookies — bulk operation, single CDP call.
        import nodriver as uc
        cdp = uc.cdp
        cookies_to_set = [
            cdp.network.CookieParam(
                name=c["name"], value=c["value"], domain=c["domain"], path=c.get("path", "/"),
                http_only=c.get("httpOnly", False), secure=c.get("secure", False),
            )
            for c in payload.get("cookies", [])
        ]
        if cookies_to_set:
            await tab.send(cdp.network.set_cookies(cookies=cookies_to_set))

        # Navigate to the saved origin so storage can be set on the right context.
        if payload.get("origin"):
            await tab.get(payload["origin"])
            ls = payload.get("localStorage", {})
            ss = payload.get("sessionStorage", {})
            if ls or ss:
                await tab.evaluate(f"""(() => {{
                    const ls = {json.dumps(ls)};
                    const ss = {json.dumps(ss)};
                    for (const k in ls) localStorage.setItem(k, ls[k]);
                    for (const k in ss) sessionStorage.setItem(k, ss[k]);
                }})()""")

        log.info("session loaded ← %s (%d cookies)", path, len(payload.get("cookies", [])))
        return {
            "name": name, "saved_at": payload.get("saved_at"),
            "origin": payload.get("origin"), "cookies": len(payload.get("cookies", [])),
            "localStorage_keys": len(payload.get("localStorage", {})),
        }

    @staticmethod
    def list_saved() -> list[dict[str, Any]]:
        """Enumerate all saved sessions on disk."""
        out: list[dict[str, Any]] = []
        for f in _session_dir().rglob("*.fern"):
            stat = f.stat()
            out.append({
                "name": f.stem, "domain": f.parent.name,
                "size": stat.st_size, "saved_at": int(stat.st_mtime),
                "path": str(f),
            })
        return out

    @staticmethod
    def delete(name: str, *, url_hint: str | None = None) -> bool:
        """Remove a saved session blob. Returns True if it existed."""
        path = _path_for(name, url_hint)
        if path.exists():
            path.unlink()
            return True
        for candidate in _session_dir().rglob(f"{name}.fern"):
            candidate.unlink()
            return True
        return False
