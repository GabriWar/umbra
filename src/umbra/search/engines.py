"""Search engines — HTML scrapers in the style of searxng's `searx/engines/*`.

Each engine: `async (query, *, page, language, time_range) -> list[Hit]`
where Hit = {url, title, snippet}. Ordered by engine rank (position matters
for scoring). HTTP engines go through curl_cffi with Chrome impersonation
(same JA3 story as `umbra.tls`). Google is browser-only: its HTML endpoint
serves JS-gated / consent-walled pages to HTTP clients, so it's driven
through a live umbra tab (see `google_browser`).

Env:
  BRAVE_API_KEY       optional — `brave` uses the official Web Search API
                      (2k req/mo free; no 429s, richer snippets) instead of
                      scraping search.brave.com.
  UMBRA_SEARXNG_URL   optional — adds a `searxng` engine hitting that instance.
"""

from __future__ import annotations

import asyncio
import base64
import html
import logging
import os
import re
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger("umbra.search")

Hit = dict[str, Any]
Engine = Callable[..., Awaitable[list[Hit]]]

_HDRS = {"accept-language": "en-US,en;q=0.9"}
_IMPERSONATE = "chrome131"
TIMEOUT = float(os.environ.get("UMBRA_SEARCH_TIMEOUT", "12"))
SEARXNG_URL = os.environ.get("UMBRA_SEARXNG_URL", "").rstrip("/")
BRAVE_API_KEY = os.environ.get("BRAVE_API_KEY", "")

_TIME = {  # engine-specific time_range vocab
    "ddg": {"day": "d", "week": "w", "month": "m", "year": "y"},
    "bing": {"day": 'ex1:"ez1"', "week": 'ex1:"ez2"', "month": 'ex1:"ez3"',
             "year": 'ex1:"ez5_{a}_{b}"'},
    "brave": {"day": "pd", "week": "pw", "month": "pm", "year": "py"},
    "google": {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"},
}


def _txt(el: Any) -> str:
    return " ".join(el.text_content().split()) if el is not None else ""


def _first(els: list[Any]) -> Any:
    return els[0] if els else None


async def _http(method: str, url: str, **kw: Any) -> Any:
    from curl_cffi import requests as cc
    kw.setdefault("impersonate", _IMPERSONATE)
    kw.setdefault("timeout", TIMEOUT)
    kw["headers"] = {**_HDRS, **kw.get("headers", {})}
    for attempt in range(3):
        r = await asyncio.to_thread(cc.request, method, url, **kw)
        if r.status_code == 200:
            return r
        if r.status_code not in (202, 429, 503) or attempt == 2:
            raise RuntimeError(f"http {r.status_code}")
        await asyncio.sleep(1.0 * (attempt + 1))  # 202=ddg challenge, 429=brave throttle
    raise RuntimeError("unreachable")


def _doc(html: str) -> Any:
    import lxml.html as LH
    return LH.fromstring(html)


# ── duckduckgo (lite) ───────────────────────────────────────────────────

async def duckduckgo(query: str, *, page: int = 1, language: str = "en",
                     time_range: str | None = None) -> list[Hit]:
    data: dict[str, str] = {"q": query, "kl": f"us-{language}" if language != "en" else "us-en"}
    if time_range:
        data["df"] = _TIME["ddg"][time_range]
    if page > 1:
        data["s"] = str((page - 1) * 20)
        data["dc"] = str((page - 1) * 20 + 1)
    r = await _http("POST", "https://lite.duckduckgo.com/lite/", data=data)
    d = _doc(r.text)
    out: list[Hit] = []
    # Layout: <tr> title-link, <tr> snippet, <tr> url, <tr> spacer — repeat.
    for a in d.xpath("//table[last()]//tr//a[contains(@class,'result-link')]"):
        tr = a.getparent()
        while tr is not None and tr.tag != "tr":
            tr = tr.getparent()
        if tr is None:
            continue
        snip = ""
        nxt = tr.getnext()
        while nxt is not None and not isinstance(nxt.tag, str):  # skip comments
            nxt = nxt.getnext()
        if nxt is not None and nxt.xpath(".//td[contains(@class,'result-snippet')]"):
            snip = _txt(nxt)
        href = a.get("href", "")
        if href.startswith("//duckduckgo.com/l/"):  # redirect wrapper
            href = unquote(parse_qs(urlsplit(href).query).get("uddg", [""])[0])
        if not href.startswith("http"):
            continue
        out.append({"url": href, "title": re.sub(r"^\d+\.\s*", "", _txt(a)), "snippet": snip})
    return out


# ── bing ────────────────────────────────────────────────────────────────

def _bing_unwrap(u: str) -> str:
    m = re.search(r"[?&]u=a1([^&]+)", u)
    if not m:
        return u
    s = m.group(1)
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()
    except Exception:
        return u


async def bing(query: str, *, page: int = 1, language: str = "en",
               time_range: str | None = None) -> list[Hit]:
    params = {"q": query, "setlang": language, "first": str((page - 1) * 10 + 1)}
    if time_range:
        params["filters"] = _TIME["bing"][time_range].format(a="", b="")
    r = await _http("GET", "https://www.bing.com/search", params=params,
                    cookies={"SRCHHPGUSR": f"SRCHLANG={language}", "_EDGE_CD": "m=en-us"})
    d = _doc(r.text)
    out: list[Hit] = []
    for li in d.xpath("//li[contains(@class,'b_algo')]"):
        a = _first(li.xpath(".//h2//a[@href]"))
        if a is None:
            continue
        url = _bing_unwrap(a.get("href"))
        if not url.startswith("http"):
            continue
        snip = _txt(_first(li.xpath(".//p"))) or _txt(_first(li.xpath(".//div[contains(@class,'b_caption')]")))
        out.append({"url": url, "title": _txt(a), "snippet": snip})
    return out


# ── brave ───────────────────────────────────────────────────────────────

async def brave(query: str, *, page: int = 1, language: str = "en",
                time_range: str | None = None) -> list[Hit]:
    params = {"q": query, "source": "web", "offset": str(page - 1)}
    if time_range:
        params["tf"] = _TIME["brave"][time_range]
    r = await _http("GET", "https://search.brave.com/search", params=params,
                    cookies={"safesearch": "off", "useLocation": "0"})
    d = _doc(r.text)
    out: list[Hit] = []
    for e in d.xpath("//div[@data-type='web']"):
        a = _first(e.xpath(".//a[@href]"))
        if a is None or not a.get("href", "").startswith("http"):
            continue
        title = _txt(_first(e.xpath(".//div[contains(@class,'title')]"))) or _txt(a)
        snip = _txt(_first(e.xpath(".//div[contains(@class,'snippet-description')]"))) or \
            _txt(_first(e.xpath(".//div[contains(@class,'generic-snippet')]//div[contains(@class,'content')]")))
        snip = re.sub(r"^\w+ \d+, \d{4} - |^\d+ \w+ ago - ", "", snip)
        out.append({"url": a.get("href"), "title": title, "snippet": snip})
    return out


async def brave_api(query: str, *, page: int = 1, language: str = "en",
                    time_range: str | None = None) -> list[Hit]:
    params = {"q": query, "count": "20", "offset": str(page - 1), "search_lang": language,
              "safesearch": "off", "text_decorations": "0"}
    if time_range:
        params["freshness"] = _TIME["brave"][time_range]
    r = await _http("GET", "https://api.search.brave.com/res/v1/web/search", params=params,
                    headers={"accept": "application/json", "x-subscription-token": BRAVE_API_KEY},
                    impersonate=None)
    return [{"url": x.get("url"), "title": html.unescape(x.get("title") or ""),
             "snippet": html.unescape(x.get("description") or "")}
            for x in (r.json().get("web") or {}).get("results", []) if x.get("url")]


# ── searxng (optional remote instance) ─────────────────────────────────

async def searxng(query: str, *, page: int = 1, language: str = "en",
                  time_range: str | None = None) -> list[Hit]:
    params = {"q": query, "format": "json", "language": language,
              "safesearch": "0", "pageno": str(page)}
    if time_range:
        params["time_range"] = time_range
    r = await _http("GET", f"{SEARXNG_URL}/search", params=params,
                    headers={"accept": "application/json"}, impersonate=None)
    return [{"url": x.get("url"), "title": x.get("title") or "",
             "snippet": x.get("content") or "", "engines": x.get("engines")}
            for x in r.json().get("results", []) if x.get("url")]


# ── google (browser-driven) ─────────────────────────────────────────────

_GOOGLE_JS = r"""
(() => {
  const out = [];
  const seen = new Set();
  for (const h3 of document.querySelectorAll('#search a h3, #rso a h3')) {
    const a = h3.closest('a');
    if (!a) continue;
    let href = a.href || '';
    if (href.startsWith('/url?')) href = new URL(href, location.origin).searchParams.get('q') || '';
    if (!href.startsWith('http') || href.includes('google.com/') || seen.has(href)) continue;
    seen.add(href);
    // snippet: nearest result container → text of the description block
    const box = a.closest('[data-hveid], .g, .MjjYud') || a.parentElement;
    let snip = '';
    if (box) {
      const cand = box.querySelector('[data-sncf], .VwiC3b, [style*="-webkit-line-clamp"]');
      snip = (cand ? cand.innerText : '').trim();
    }
    out.push({url: href, title: h3.innerText.trim(), snippet: snip});
  }
  return JSON.stringify(out);
})()
"""


def google_browser(tab: Any) -> Engine:
    """Build a google engine bound to a live umbra tab (nodriver Tab).
    Queries are paced (1.5–3s apart) — ~5 back-to-back searches trips /sorry/."""
    import random
    import time as _t
    last = [0.0]

    async def google(query: str, *, page: int = 1, language: str = "en",
                     time_range: str | None = None) -> list[Hit]:
        import json
        from urllib.parse import urlencode
        params = {"q": query, "hl": language, "num": "10", "start": str((page - 1) * 10)}
        if time_range:
            params["tbs"] = _TIME["google"][time_range]
        gap = random.uniform(1.5, 3.0) - (_t.monotonic() - last[0])
        if gap > 0:
            await asyncio.sleep(gap)
        last[0] = _t.monotonic()
        await asyncio.wait_for(
            tab.get("https://www.google.com/search?" + urlencode(params)), TIMEOUT)
        for _ in range(20):  # ~4s for results (or consent/captcha) to land
            raw = await tab.evaluate(_GOOGLE_JS)
            hits = json.loads(raw) if isinstance(raw, str) else []
            if hits:
                return hits
            url = await tab.evaluate("location.href")
            if "consent.google" in url or "/sorry/" in url:
                # consent: click "Accept all"/"Reject all"; captcha: give up, caller may handoff
                if "consent" in url:
                    await tab.evaluate(
                        "(()=>{const b=[...document.querySelectorAll('button')]"
                        ".find(b=>/accept all|reject all|aceitar|rejeitar/i.test(b.innerText));"
                        "if(b){b.click();return true}return false})()")
                else:
                    raise RuntimeError("google captcha (/sorry/) — use handoff_start on this tab to solve")
            await asyncio.sleep(0.2)
        return []
    return google


HTTP_ENGINES: dict[str, Engine] = {
    "duckduckgo": duckduckgo, "bing": bing,
    "brave": brave_api if BRAVE_API_KEY else brave,
}
if SEARXNG_URL:
    HTTP_ENGINES["searxng"] = searxng

# Operator kinds each engine honors. DDG lite answers 202 (bot challenge)
# to filetype:/inurl:/intitle: — those dorks are skipped there, not errored.
SUPPORTS: dict[str, set[str]] = {
    "duckduckgo": {"site"},
    "bing": {"site", "filetype", "inurl", "intitle"},
    "brave": {"site", "filetype", "inurl", "intitle"},
    "google": {"site", "filetype", "inurl", "intitle"},
    "searxng": {"site", "filetype", "inurl", "intitle"},
}

# searxng-style per-engine weights (google slightly higher when available)
WEIGHTS = {"google": 1.2, "bing": 1.0, "brave": 1.0, "duckduckgo": 1.0, "searxng": 1.1}
