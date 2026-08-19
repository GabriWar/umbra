"""umbra.search — self-contained meta-search (no searxng server needed).

    dorks (intent templates or caller-supplied)
      × engines (duckduckgo / bing / brave over curl_cffi; google via a live tab)
      → concurrent fan-out → merge/score/dedupe → compact ranked list
"""

from __future__ import annotations

import asyncio
from typing import Any

from .dorks import INTENTS, build_dorks, classify_intent, hit_satisfies, operators_of
from .engines import HTTP_ENGINES, PROXY, SUPPORTS, WEIGHTS, Engine, google_browser
from .results import merge

__all__ = ["INTENTS", "search", "HTTP_ENGINES", "google_browser"]

DEFAULT_ENGINES = ("duckduckgo", "bing", "brave")


async def search(query: str, *, dorks: list[str] | None = None, intent: str | None = None,
                 auto_dork: bool = True, engines: list[str] | None = None,
                 google_tab: Any = None, max_results: int = 10, max_per_host: int = 3,
                 language: str = "en", time_range: str | None = None, page: int = 1,
                 allowed_domains: list[str] | None = None,
                 blocked_domains: list[str] | None = None,
                 proxy: str | None = None,
                 per_engine_concurrency: int = 1) -> dict[str, Any]:
    """`proxy`: URL (http/https/socks5, creds inline) used by every HTTP engine
    call in this search. Google uses whatever proxy its tab was spawned with."""
    PROXY.set(proxy)
    intent = intent or classify_intent(query)
    if dorks:
        dl = [{"query": q, "operators": operators_of(q)} for q in dorks]
        if query not in dorks:
            dl.append({"query": query, "operators": []})
    elif auto_dork:
        dl = build_dorks(query, intent)
    else:
        dl = [{"query": query, "operators": []}]

    names = list(engines or DEFAULT_ENGINES)
    eng: dict[str, Engine] = {}
    for n in names:
        if n == "google":
            if google_tab is None:
                raise ValueError("engine 'google' needs a live tab: spawn() first, then pass tab_id")
            eng[n] = google_browser(google_tab)
        elif n in HTTP_ENGINES:
            eng[n] = HTTP_ENGINES[n]
        else:
            raise ValueError(f"unknown engine {n!r}; have {sorted(HTTP_ENGINES) + ['google']}")

    # One in-flight request per engine host by default (brave 429s / ddg 202s
    # on parallel bursts); engines still run in parallel with each other.
    sems = {n: asyncio.Semaphore(1 if n == "google" else per_engine_concurrency) for n in eng}

    async def run(d: dict[str, Any], di: int, n: str) -> tuple[dict[str, Any], int, str, Any]:
        async with sems[n]:
            try:
                hits = await eng[n](d["query"], page=page, language=language, time_range=time_range)
            except Exception as e:  # keep going; report per-source
                hits = e
        return d, di, n, hits

    def supported(d: dict[str, Any], n: str) -> bool:
        kinds = {op.lstrip("-").split(":", 1)[0] for op in d["operators"] if ":" in op}
        if n == "duckduckgo" and ("(" in d["query"] or '"' in d["query"]):
            return False  # ddg lite 202s on OR-groups / quoted negatives
        return kinds <= SUPPORTS.get(n, kinds)

    done = await asyncio.gather(*(run(d, di, n) for di, d in enumerate(dl)
                                  for n in eng if supported(d, n)))
    sources, errors, counts = [], [], {n: 0 for n in eng}
    for d, di, n, hits in done:
        if isinstance(hits, BaseException):
            errors.append(f"{n} {d['query']!r}: {hits}")
        else:
            hits = [h for h in hits if hit_satisfies(d["operators"], h)]
            counts[n] += len(hits)
            sources.append((d, di, n, hits))
    if not sources:
        raise RuntimeError("all engines failed: " + "; ".join(errors[:3]))

    return {
        "query": query, "intent": intent, "dorks": [d["query"] for d in dl],
        "engines": counts, "proxy": bool(proxy),
        "results": merge(sources, weights=WEIGHTS, max_results=max_results,
                         max_per_host=max_per_host, allowed_domains=allowed_domains,
                         blocked_domains=blocked_domains),
        "errors": errors,
    }
