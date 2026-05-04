"""Proxy pool — multi-provider rotation w/ health tracking, sticky sessions,
geo filters. Plug into StealthBrowser via StealthOptions.proxy_pool.

Granularity: per-browser. Chrome locks proxy per-process; for parallel
distinct egress IPs use multiple browsers each picking from the pool.

Auth model: creds are kept OUT of the Chrome --proxy-server flag (Chrome
silently strips inline auth). Pool returns (clean_url, creds) — caller wires
creds into a CDP Fetch.authRequired handler (see proxy_auth.install).
"""
from __future__ import annotations

import asyncio
import contextlib
import csv
import io
import json
import random
import time
import urllib.parse as _up
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

RotationStrategy = Literal[
    "round_robin", "random", "least_used", "best_health", "sticky_browser"
]

# Lower bound on rolling success rate; entries below this are skipped during
# pick() unless every entry is below (then we pick the best of the worst —
# better than failing the spawn outright).
_MIN_HEALTH_DEFAULT = 0.3


@dataclass
class ProxyEntry:
    """One proxy. URL kept w/o creds; auth carried separately so we can wire
    it through CDP Fetch.authRequired (Chrome flag strips inline auth)."""

    url: str                              # http://gateway:port (no creds)
    username: str | None = None
    password: str | None = None
    country: str | None = None            # ISO-3166-1 alpha-2 if known
    tags: tuple[str, ...] = ()
    # Sticky-session template: provider-specific username extension that pins
    # egress to a specific exit. e.g. Bright Data uses
    # "user-session-{sid}-country-{cc}". `pick()` substitutes {sid} w/ a
    # generated session id and {cc} w/ entry.country.
    session_template: str | None = None

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # Rolling success rate over last _HEALTH_WINDOW probes.
    _health_window: list[bool] = field(default_factory=list, repr=False)
    last_used: float = 0.0
    in_use_by: str | None = None          # browser_id holding this entry
    sticky_session_id: str | None = None  # set on pick if session_template

    @property
    def health(self) -> float:
        if not self._health_window:
            return 1.0
        return sum(self._health_window) / len(self._health_window)

    def report(self, ok: bool) -> None:
        self._health_window.append(ok)
        if len(self._health_window) > 20:
            self._health_window.pop(0)

    def effective_username(self) -> str | None:
        """Username w/ session template applied. None if no creds."""
        if not self.username:
            return None
        if self.session_template and self.sticky_session_id:
            return self.session_template.format(
                sid=self.sticky_session_id,
                cc=self.country or "",
                user=self.username,
            )
        return self.username

    def chrome_flag_url(self) -> str:
        """URL safe to feed to --proxy-server (creds stripped)."""
        p = _up.urlparse(self.url)
        # Drop any user-info that snuck into url field.
        netloc = p.hostname or ""
        if p.port:
            netloc += f":{p.port}"
        return _up.urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))

    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "url": self.url,
            "country": self.country,
            "tags": list(self.tags),
            "session_template": self.session_template,
            "health": round(self.health, 3),
            "last_used": self.last_used,
            "in_use_by": self.in_use_by,
            "sticky_session_id": self.sticky_session_id,
        }
        if self.username:
            d["username"] = self.username if not redact else self.username[:2] + "***"
        if self.password:
            d["password"] = "***" if redact else self.password
        return d


def parse_proxy_url(raw: str) -> ProxyEntry:
    """Parse `http://user:pass@host:port` or `http://host:port` into ProxyEntry.

    Also tolerant of:
      - `host:port` (defaults to http://)
      - `socks5://...`, `https://...`
      - trailing `#country=US,tags=residential,sticky=true,session_template=...`
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("empty proxy url")

    meta: dict[str, str] = {}
    if "#" in raw:
        raw, meta_part = raw.split("#", 1)
        for kv in meta_part.split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                meta[k.strip()] = v.strip()

    # Common provider export format: `host:port:user:pass` (4 colon parts).
    # Detect when scheme is absent and we see exactly 4 colon-separated parts
    # where the second is numeric.
    if "://" not in raw:
        parts = raw.split(":")
        if len(parts) == 4 and parts[1].isdigit():
            host, port, user, pwd = parts
            raw = f"http://{user}:{pwd}@{host}:{port}"
        else:
            raw = f"http://{raw}"
    p = _up.urlparse(raw)
    if not p.hostname:
        raise ValueError(f"no host in proxy url {raw!r}")
    scheme = p.scheme or "http"
    if scheme not in ("http", "https", "socks4", "socks5"):
        raise ValueError(f"unsupported proxy scheme {scheme!r}")
    netloc = p.hostname
    if p.port:
        netloc += f":{p.port}"
    clean = _up.urlunparse((scheme, netloc, "", "", "", ""))

    tags = tuple(t.strip() for t in meta.get("tags", "").split("|") if t.strip())
    return ProxyEntry(
        url=clean,
        username=p.username,
        password=p.password,
        country=meta.get("country") or None,
        tags=tags,
        session_template=meta.get("session_template") or None,
    )


class ProxyPool:
    """Async-safe proxy pool. Rotation across multiple browsers.

    Lifecycle per browser:
        entry = await pool.acquire(browser_id, country=...)
        # ... use entry.chrome_flag_url(), entry.effective_username(), entry.password
        pool.report(entry.id, ok=True)   # call after each operation
        await pool.release(browser_id)   # frees sticky-binding for re-pick
    """

    def __init__(
        self,
        rotation: RotationStrategy = "round_robin",
        *,
        min_health: float = _MIN_HEALTH_DEFAULT,
    ) -> None:
        self.rotation = rotation
        self.min_health = min_health
        self._entries: list[ProxyEntry] = []
        self._rr_idx = 0
        self._lock = asyncio.Lock()
        self._sticky: dict[str, str] = {}  # browser_id -> entry_id

    # ── mutation ──────────────────────────────────────────────────────────
    def add(self, entry: ProxyEntry) -> ProxyEntry:
        if any(e.id == entry.id for e in self._entries):
            raise ValueError(f"duplicate entry id {entry.id}")
        self._entries.append(entry)
        return entry

    def add_url(self, url: str, **kw: Any) -> ProxyEntry:
        e = parse_proxy_url(url)
        for k, v in kw.items():
            if hasattr(e, k):
                setattr(e, k, v)
        return self.add(e)

    def remove(self, entry_id: str) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != entry_id]
        # purge sticky bindings pointing at it
        self._sticky = {b: eid for b, eid in self._sticky.items() if eid != entry_id}
        return len(self._entries) < before

    def clear(self) -> int:
        n = len(self._entries)
        self._entries.clear()
        self._sticky.clear()
        self._rr_idx = 0
        return n

    @property
    def entries(self) -> list[ProxyEntry]:
        return list(self._entries)

    # ── selection ─────────────────────────────────────────────────────────
    def _candidates(
        self, *, country: str | None, tag: str | None, exclude_in_use: bool
    ) -> list[ProxyEntry]:
        out = []
        for e in self._entries:
            if exclude_in_use and e.in_use_by:
                continue
            if country and (e.country or "").upper() != country.upper():
                continue
            if tag and tag not in e.tags:
                continue
            out.append(e)
        return out

    def _pick_strategy(self, cands: list[ProxyEntry]) -> ProxyEntry:
        if self.rotation == "random":
            return random.choice(cands)
        if self.rotation == "least_used":
            return min(cands, key=lambda e: (e.last_used, e.id))
        if self.rotation == "best_health":
            return max(cands, key=lambda e: (e.health, -e.last_used))
        # round_robin (default) + sticky_browser fall back to RR for the actual pick
        idx = self._rr_idx % len(cands)
        self._rr_idx = (self._rr_idx + 1) % max(1, len(cands))
        return cands[idx]

    async def acquire(
        self,
        browser_id: str,
        *,
        country: str | None = None,
        tag: str | None = None,
        exclusive: bool = True,
    ) -> ProxyEntry:
        """Pick + bind an entry to a browser.

        exclusive=True (default): one browser per entry until release. Best
        for parallel sessions where u want distinct egress IPs. Set False
        for serial/shared use (rotation can re-hand the same entry).
        """
        async with self._lock:
            # Sticky strategy: same browser → same entry across acquires.
            if self.rotation == "sticky_browser" and browser_id in self._sticky:
                eid = self._sticky[browser_id]
                for e in self._entries:
                    if e.id == eid:
                        e.last_used = time.time()
                        e.in_use_by = browser_id
                        return e

            cands = self._candidates(
                country=country, tag=tag, exclude_in_use=exclusive
            )
            if not cands and exclusive:
                # All in use — fall back to non-exclusive pick (better than
                # failing the spawn).
                cands = self._candidates(
                    country=country, tag=tag, exclude_in_use=False
                )
            if not cands:
                raise RuntimeError(
                    f"no proxies match country={country!r} tag={tag!r}"
                )

            healthy = [e for e in cands if e.health >= self.min_health]
            chosen_pool = healthy or cands  # all-bad → still pick best
            picked = self._pick_strategy(chosen_pool)
            picked.last_used = time.time()
            picked.in_use_by = browser_id
            if picked.session_template:
                picked.sticky_session_id = uuid.uuid4().hex[:12]
            if self.rotation == "sticky_browser":
                self._sticky[browser_id] = picked.id
            return picked

    async def release(self, browser_id: str) -> None:
        async with self._lock:
            for e in self._entries:
                if e.in_use_by == browser_id:
                    e.in_use_by = None
                    e.sticky_session_id = None

    def report(self, entry_id: str, ok: bool) -> None:
        for e in self._entries:
            if e.id == entry_id:
                e.report(ok)
                return

    # ── serialization ─────────────────────────────────────────────────────
    def to_dict(self, *, redact: bool = True) -> dict[str, Any]:
        return {
            "rotation": self.rotation,
            "min_health": self.min_health,
            "entries": [e.to_dict(redact=redact) for e in self._entries],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ProxyPool:
        pool = cls(
            rotation=d.get("rotation", "round_robin"),
            min_health=d.get("min_health", _MIN_HEALTH_DEFAULT),
        )
        for raw in d.get("entries", []):
            pool.add(ProxyEntry(**{k: v for k, v in raw.items() if k in {
                "id", "url", "username", "password", "country", "tags",
                "session_template",
            } and v is not None}))
        return pool

    # ── loaders (multi-format, "any provider") ────────────────────────────
    def load_lines(self, text: str | Iterable[str]) -> int:
        """Each line: a proxy URL (see parse_proxy_url). Comments (#) ignored
        when at line start. Returns count loaded."""
        if isinstance(text, str):
            lines = text.splitlines()
        else:
            lines = list(text)
        n = 0
        for ln in lines:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            with contextlib.suppress(ValueError):
                self.add(parse_proxy_url(ln))
                n += 1
        return n

    def load_json(self, text: str) -> int:
        """JSON array of {url, username?, password?, country?, tags?, session_template?}.
        Or full pool dict from to_dict()."""
        data = json.loads(text)
        if isinstance(data, dict) and "entries" in data:
            other = ProxyPool.from_dict(data)
            for e in other._entries:
                with contextlib.suppress(ValueError):
                    self.add(e)
            return len(other._entries)
        n = 0
        for item in data:
            if isinstance(item, str):
                e = parse_proxy_url(item)
            else:
                url = item.pop("url")
                e = parse_proxy_url(url)
                for k, v in item.items():
                    if hasattr(e, k) and v is not None:
                        if k == "tags" and isinstance(v, list):
                            v = tuple(v)
                        setattr(e, k, v)
            with contextlib.suppress(ValueError):
                self.add(e)
                n += 1
        return n

    def load_csv(self, text: str) -> int:
        """CSV w/ header. Recognized columns: url, username, password,
        country, tags (pipe-sep), session_template."""
        rdr = csv.DictReader(io.StringIO(text))
        n = 0
        for row in rdr:
            url = row.get("url") or row.get("URL")
            if not url:
                continue
            e = parse_proxy_url(url)
            for k in ("username", "password", "country", "session_template"):
                v = row.get(k)
                if v:
                    setattr(e, k, v)
            tags = row.get("tags") or ""
            if tags:
                e.tags = tuple(t.strip() for t in tags.split("|") if t.strip())
            with contextlib.suppress(ValueError):
                self.add(e)
                n += 1
        return n

    def load_file(self, path: str | Path) -> int:
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        suffix = p.suffix.lower()
        if suffix == ".json":
            return self.load_json(text)
        if suffix == ".csv":
            return self.load_csv(text)
        return self.load_lines(text)

    def __len__(self) -> int:
        return len(self._entries)

    def __repr__(self) -> str:
        return f"ProxyPool(n={len(self)}, rotation={self.rotation!r})"
