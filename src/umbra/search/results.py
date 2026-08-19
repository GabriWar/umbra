"""Result aggregation — port of the relevant bits of searxng `searx/results.py`.

- URL normalization (scheme/host case, www., trailing slash, fragment) →
  the same page from several engines collapses into one result.
- Score: Σ over every (dork, engine) list the hit appears in of
      dork_specificity × dork_order_boost × engine_weight / position
  so "found by many engines near the top of a specific dork" wins
  (searxng: Σ weight/position; crawlee: dork multipliers — combined here).
- Junk-domain drop + per-host cap so one site can't flood the page.
"""

from __future__ import annotations

from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

JUNK_DOMAINS = ("pinterest.com", "quora.com", "answers.com", "ehow.com", "wikihow.com")


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().removeprefix("www.")
    except Exception:
        return ""


def normalize(url: str) -> str:
    p = urlsplit(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    if p.port and p.port not in (80, 443):
        host = f"{host}:{p.port}"
    path = p.path.rstrip("/") or "/"
    return urlunsplit((p.scheme.lower() or "https", host, path, p.query, ""))


def is_junk(url: str) -> bool:
    h = host_of(url)
    return any(h == j or h.endswith("." + j) for j in JUNK_DOMAINS)


def merge(sources: Iterable[tuple[dict[str, Any], int, str, list[dict[str, Any]]]],
          *, weights: dict[str, float], max_results: int, max_per_host: int,
          allowed_domains: list[str] | None = None,
          blocked_domains: list[str] | None = None) -> list[dict[str, Any]]:
    """sources: iterable of (dork, dork_index, engine, hits)."""
    merged: dict[str, dict[str, Any]] = {}
    for dork, di, engine, hits in sources:
        spec = 1 + 0.35 * len(dork["operators"])
        boost = 1 / (1 + di * 0.15)
        w = weights.get(engine, 1.0)
        for pos, h in enumerate(hits, 1):
            url = h.get("url")
            if not url or not url.startswith("http") or is_junk(url):
                continue
            key = normalize(url)
            score = spec * boost * w / pos
            cur = merged.get(key)
            if cur is None:
                cur = merged[key] = {"url": url, "title": h.get("title") or "",
                                     "snippet": h.get("snippet") or "", "score": 0.0,
                                     "engines": [], "dork": dork["query"], "_best": 0.0}
            cur["score"] += score
            if engine not in cur["engines"]:
                cur["engines"].append(engine)
            if score > cur["_best"]:  # keep title/snippet/dork from the strongest hit
                cur["_best"] = score
                cur["dork"] = dork["query"]
                if h.get("title"):
                    cur["title"] = h["title"]
            if len(h.get("snippet") or "") > len(cur["snippet"]):
                cur["snippet"] = h["snippet"]

    allow = [d.lower() for d in allowed_domains or []]
    block = [d.lower() for d in blocked_domains or []]

    def dom_ok(h: str) -> bool:
        if allow and not any(h == d or h.endswith("." + d) for d in allow):
            return False
        return not any(h == d or h.endswith("." + d) for d in block)

    out: list[dict[str, Any]] = []
    per_host: dict[str, int] = {}
    for r in sorted(merged.values(), key=lambda x: -x["score"]):
        h = host_of(r["url"])
        if not dom_ok(h) or per_host.get(h, 0) >= max_per_host:
            continue
        per_host[h] = per_host.get(h, 0) + 1
        r.pop("_best")
        r["score"] = round(r["score"], 3)
        out.append(r)
        if len(out) >= max_results:
            break
    return out
