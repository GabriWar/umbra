"""Intent classification + template-driven Google-dork expansion.

Port of crawlee `dork-templates.ts` / `dork-generator.ts` fallback path —
no LLM: inside an MCP server the agent *is* the LLM and can hand us its own
dork list; this is the zero-cost default when it doesn't.
"""

from __future__ import annotations

from typing import Any

INTENTS = ("code", "docs", "pdf", "dataset", "forum", "news", "firmware", "generic")

TEMPLATES: dict[str, dict[str, Any]] = {
    "code": dict(
        triggers=["code", "source", "implementation", "library", "repo", "snippet", "example"],
        sites=["github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sourceforge.net"],
        filetype=[], inurl=["blob/main", "blob/master", "src", "tree"], intitle=[],
        negative=["-pinterest.com", "-w3schools.com"], max_variants=5),
    "docs": dict(
        triggers=["docs", "documentation", "manual", "reference", "api", "guide", "tutorial"],
        sites=["readthedocs.io", "docs.rs", "developer.mozilla.org"],
        filetype=[], inurl=["docs", "documentation", "reference", "api"],
        intitle=["documentation", "reference"],
        negative=["-pinterest.com"], max_variants=5),
    "pdf": dict(
        triggers=["paper", "whitepaper", "rfc", "spec", "specification", "thesis"],
        sites=["arxiv.org", "ieee.org", "acm.org", "rfc-editor.org", "ietf.org"],
        filetype=["pdf"], inurl=[], intitle=[],
        negative=["-amazon.com", "-ebay.com"], max_variants=4),
    "dataset": dict(
        triggers=["dataset", "data", "csv", "corpus", "benchmark"],
        sites=["kaggle.com", "huggingface.co", "data.gov", "zenodo.org", "archive.org"],
        filetype=["csv", "json", "parquet"], inurl=["dataset", "data"], intitle=[],
        negative=[], max_variants=4),
    "forum": dict(
        triggers=["forum", "discussion", "thread", "reddit", "hackernews", "stackoverflow"],
        sites=["reddit.com", "news.ycombinator.com", "stackoverflow.com",
               "serverfault.com", "superuser.com", "unix.stackexchange.com"],
        filetype=[], inurl=[], intitle=[],
        negative=["-pinterest.com"], max_variants=5),
    "news": dict(
        triggers=["news", "announced", "launched", "released", "today", "yesterday", "recent"],
        sites=[], filetype=[], inurl=["news", "press", "blog"], intitle=[],
        negative=["-pinterest.com"], max_variants=4),
    "firmware": dict(
        triggers=["firmware", "bios", "uefi", "rom", "ucode", "microcode", "blob", "bin", "mec"],
        sites=["github.com", "gitlab.com", "archive.org", "xda-developers.com", "4pda.to"],
        filetype=["bin", "rom", "fw", "cap", "uefi", "ifd"],
        inurl=["releases", "downloads", "firmware"], intitle=["release notes", "changelog"],
        negative=["-pinterest.com", '-"buy now"', "-amazon.com", "-ebay.com"], max_variants=6),
    "generic": dict(
        triggers=[], sites=[], filetype=[], inurl=[], intitle=[],
        negative=["-pinterest.com"], max_variants=3),
}


def classify_intent(query: str) -> str:
    q = query.lower()
    best, best_score = "generic", 0
    for intent, tpl in TEMPLATES.items():
        score = sum(1 for t in tpl["triggers"] if t in q)
        if score > best_score:
            best, best_score = intent, score
    return best


def operators_of(q: str) -> list[str]:
    toks = [t.strip("()") for t in q.split()]
    return [t for t in toks if ":" in t or t.startswith("-")]


def build_dorks(query: str, intent: str, max_variants: int | None = None) -> list[dict[str, Any]]:
    """[{query, operators}] — most specific first, raw query always last as baseline."""
    tpl = TEMPLATES[intent]
    n = min(max_variants or tpl["max_variants"], 8)
    neg = " ".join(tpl["negative"])
    out: list[dict[str, Any]] = []

    def add(q: str, ops: list[str]) -> None:
        out.append({"query": " ".join(q.split()), "operators": ops})

    if tpl["sites"]:
        sites = tpl["sites"][:4]
        add(f"{query} ({' OR '.join('site:' + s for s in sites)}) {neg}", [f"site:{s}" for s in sites])
    for ft in tpl["filetype"][:2]:
        add(f"{query} filetype:{ft} {neg}", [f"filetype:{ft}"])
    for iu in tpl["inurl"][:2]:
        add(f"{query} inurl:{iu} {neg}", [f"inurl:{iu}"])
    for it in tpl["intitle"][:1]:
        add(f'{query} intitle:"{it}" {neg}', [f'intitle:"{it}"'])
    out = out[:n]
    if not any(d["query"] == query for d in out):
        out.append({"query": query, "operators": []})
    return out


def _host(url: str) -> str:
    from urllib.parse import urlsplit
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def hit_satisfies(operators: list[str], hit: dict) -> bool:
    """Client-side dork enforcement. Engines (bing especially) silently drop
    operators they dislike and return generic results — those would then
    outrank real hits because they sit alone at position 1 of a "specific"
    dork. Check the cheap ones: site:/-site:/filetype:/inurl:/intitle:/-domain.
    site: is OR-semantics across the dork's site: operators."""
    from urllib.parse import urlsplit
    url = hit.get("url") or ""
    host = _host(url)
    path = urlsplit(url).path.lower()
    title = (hit.get("title") or "").lower()
    sites: list[str] = []
    for op in operators:
        neg = op.startswith("-")
        o = op[1:] if neg else op
        if ":" not in o:  # -"buy now" style negative phrase — can't verify, skip
            continue
        k, v = o.split(":", 1)
        v = v.strip('"').lower()
        if k == "site":
            if neg:
                if host == v or host.endswith("." + v):
                    return False
            else:
                sites.append(v)
        elif k == "filetype" and not neg:
            if not path.endswith("." + v):
                return False
        elif k == "inurl" and not neg:
            if v not in url.lower():
                return False
        elif k == "intitle" and not neg:
            if v not in title:
                return False
    if sites and not any(host == v or host.endswith("." + v) for v in sites):
        return False
    # bare "-domain.com" negatives
    for op in operators:
        if op.startswith("-") and ":" not in op and "." in op and not op.startswith('-"'):
            v = op[1:].lower()
            if host == v or host.endswith("." + v):
                return False
    return True
