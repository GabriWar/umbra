"""In-process stealth self-test.

Runs the public detector pages (sannysoft, creepjs) against the current tab
and returns parsed scores. Use as an MCP tool to verify stealth quality
BEFORE committing to a sensitive action — "if creepjs.stealth > 20%, run
warm_session first, otherwise proceed".

Cost: each run loads a real page with full JS, ~5-30s per check. Cache the
result for the session unless you've changed something stealth-relevant.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

log = logging.getLogger("umbra.detection")


async def sannysoft_score(browser: Any) -> dict[str, Any]:
    """Run bot.sannysoft.com and return pass/fail counts + failure list."""
    tab = await browser.new_tab("https://bot.sannysoft.com/")
    await asyncio.sleep(7)  # let all checks finish
    raw = await tab.evaluate("""JSON.stringify(Array.from(document.querySelectorAll('table tr')).map(r => {
        const tds = r.querySelectorAll('td');
        return tds.length >= 2 ? {n: tds[0].textContent.trim(), c: (tds[1].className||'').trim(), r: (tds[1].textContent||'').trim().substring(0,80)} : null;
    }).filter(Boolean))""")
    rows = json.loads(raw) if isinstance(raw, str) else []
    passed = sum(1 for r in rows[:35] if "passed" in r["c"])
    failed = sum(1 for r in rows[:35] if "failed" in r["c"])
    failures = [{"name": r["n"], "result": r["r"]} for r in rows[:35] if "failed" in r["c"]]
    await tab.close()
    return {"passed": passed, "failed": failed, "total": passed + failed, "failures": failures}


async def creepjs_score(browser: Any, *, wait_s: int = 30) -> dict[str, Any]:
    """Run abrahamjuliot.github.io/creepjs and return key headless/stealth scores."""
    tab = await browser.new_tab("https://abrahamjuliot.github.io/creepjs/")
    await asyncio.sleep(wait_s)
    txt = await tab.evaluate("document.body.innerText")
    if not isinstance(txt, str):
        await tab.close()
        return {"error": "could not read page"}

    out: dict[str, Any] = {}
    for pat, label in [
        (r"(\d+)\s*%\s*trust", "trust"),
        (r"(\d+)\s*%\s*like\s*headless", "like_headless"),
        (r"(\d+)\s*%\s*headless\s*:", "detected_headless"),
        (r"(\d+)\s*%\s*stealth", "stealth"),
    ]:
        m = re.search(pat, txt, re.IGNORECASE)
        out[label] = int(m.group(1)) if m else None

    out["chromium"] = bool(re.search(r"chromium\s*:\s*true", txt, re.IGNORECASE))
    # Also expose the user-facing FP ID for diff tracking across sessions.
    fp = re.search(r"FP\s*ID:\s*([a-f0-9]{16,})", txt)
    out["fp_id"] = fp.group(1) if fp else None
    await tab.close()
    return out


async def full_check(browser: Any) -> dict[str, Any]:
    """Convenience: run both sannysoft + creepjs, summarize."""
    s = await sannysoft_score(browser)
    c = await creepjs_score(browser)
    verdict = "good"
    if s["failed"] > 1 or (c.get("detected_headless") or 0) > 10 or (c.get("stealth") or 0) > 10:
        verdict = "suspicious — recommend warming session before sensitive nav"
    if s["failed"] > 3 or (c.get("detected_headless") or 0) > 30:
        verdict = "BAD — visible automation tells, fix stealth config first"
    return {
        "sannysoft": s, "creepjs": c, "verdict": verdict,
        "summary": (
            f"sannysoft {s['passed']}/{s['total']}, "
            f"creepjs headless={c.get('detected_headless')}% "
            f"stealth={c.get('stealth')}%"
        ),
    }
