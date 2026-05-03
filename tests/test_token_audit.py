"""Token-cost audit: hammer every umbra MCP tool, measure ms + bytes + tokens
both compressed (default `_compact`) and uncompressed (verbosity='full').

Approach: monkey-patch `server._compact` to run twice per call — once with
filtering (the response actually returned to the agent) and once as identity
(what would have shipped without `_compact`). Records both byte+token sizes
without re-invoking the tool.

Tokens via tiktoken `cl100k_base` (close-enough proxy for Claude tokenization;
exact tokenizer is provider-internal). Fallback: chars / 4.

Coverage: every tool in the registry that doesn't require human interaction.

Run:
    UMBRA_CONTAINER=1 .venv/bin/python tests/test_token_audit.py

Output: per-tool table + summary aggregates + JSON dump to /tmp/umbra_token_audit.json.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import statistics
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SRC = _HERE.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
os.environ.setdefault("UMBRA_CONTAINER", "1")

from umbra import server  # noqa: E402

# ─── tokenizer ──────────────────────────────────────────────────────────
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    def count_tokens(s: str) -> int:
        return len(_ENC.encode(s, disallowed_special=()))
    TOK_MODE = "tiktoken/cl100k_base"
except Exception:
    def count_tokens(s: str) -> int:
        return max(1, len(s) // 4)
    TOK_MODE = "fallback chars/4"

# ─── monkey-patch _compact to measure both versions ─────────────────────
_orig_compact = server._compact
_pending_measurement: dict[str, int | str] = {}

def _measuring_compact(obj, *, max_str=2000, max_list=80):
    raw = obj
    compact = _orig_compact(obj, max_str=max_str, max_list=max_list)
    raw_json = json.dumps(raw, default=str, separators=(",", ":"))
    cmp_json = json.dumps(compact, default=str, separators=(",", ":"))
    _pending_measurement["full_bytes"] = len(raw_json)
    _pending_measurement["cmp_bytes"] = len(cmp_json)
    _pending_measurement["full_tokens"] = count_tokens(raw_json)
    _pending_measurement["cmp_tokens"] = count_tokens(cmp_json)
    return compact

server._compact = _measuring_compact

RESULTS: list[dict] = []


async def call(tool_name, **kwargs):
    tool = await server.mcp.get_tool(tool_name)
    return await tool.fn(**kwargs)


async def t(label, coro, *, expect_ok=True):
    """Run one call, record time + bytes + tokens (both compressed/full)."""
    _pending_measurement.clear()
    t0 = time.perf_counter()
    err = None
    try:
        result = await coro()
        ok = result is not None
    except Exception as e:  # noqa: BLE001
        err = str(e)[:200]
        result = None
        ok = not expect_ok
    ms = int((time.perf_counter() - t0) * 1000)
    row = {
        "label": label,
        "ok": ok,
        "ms": ms,
        "full_bytes": _pending_measurement.get("full_bytes", 0),
        "cmp_bytes": _pending_measurement.get("cmp_bytes", 0),
        "full_tokens": _pending_measurement.get("full_tokens", 0),
        "cmp_tokens": _pending_measurement.get("cmp_tokens", 0),
        "saved_pct": (
            int(100 * (1 - _pending_measurement["cmp_tokens"] / _pending_measurement["full_tokens"]))
            if _pending_measurement.get("full_tokens") else 0
        ),
        "err": err,
    }
    RESULTS.append(row)
    status = "PASS" if ok else "FAIL"
    print(f"  {status:4s} [{ms:5d}ms full={row['full_tokens']:5d}t cmp={row['cmp_tokens']:5d}t saved={row['saved_pct']:3d}%] {label}", flush=True)
    if err:
        print(f"    ERR: {err}", flush=True)


async def main():
    print(f"=== umbra token audit (tokenizer: {TOK_MODE}) ===", flush=True)

    # ─── boot ──────────────────────────────────────────────────────────
    await t("spawn(default)", lambda: call("spawn", url="https://example.com"))
    await asyncio.sleep(1.5)

    # ─── A. browser ────────────────────────────────────────────────────
    print("\n--- A. browser ---", flush=True)
    await t("list_tabs", lambda: call("list_tabs"))
    await t("list_browsers", lambda: call("list_browsers"))
    await t("switch_tab", lambda: call("switch_tab", tab_id="t0"))
    await t("navigate(httpbin/forms)", lambda: call("navigate", tab_id="t0", url="https://httpbin.org/forms/post"))
    await asyncio.sleep(1)
    await t("reload", lambda: call("reload", tab_id="t0"))
    await asyncio.sleep(1)
    await t("back", lambda: call("back", tab_id="t0"))
    await asyncio.sleep(0.7)
    await t("forward", lambda: call("forward", tab_id="t0"))
    await asyncio.sleep(0.7)

    # ─── B. ARIA ───────────────────────────────────────────────────────
    print("\n--- B. ARIA ---", flush=True)
    await t("aria_snapshot", lambda: call("aria_snapshot", tab_id="t0"))
    await t("aria_snapshot(dedup)", lambda: call("aria_snapshot", tab_id="t0"))
    await t("aria_snapshot(force)", lambda: call("aria_snapshot", tab_id="t0", force_refresh=True))
    await t("current_state", lambda: call("current_state", tab_id="t0"))
    await t("find_by_text", lambda: call("find_by_text", tab_id="t0", text="Customer"))
    await t("aria_click(0)", lambda: call("aria_click", tab_id="t0", idx=0))
    await t("aria_type", lambda: call("aria_type", tab_id="t0", idx=1, text="x", humanize=False))
    await t("fill_form", lambda: call("fill_form", tab_id="t0", fields={"Customer name": "X"}))

    # ─── C. input ──────────────────────────────────────────────────────
    print("\n--- C. input ---", flush=True)
    await t("press_key(Tab)", lambda: call("press_key", tab_id="t0", key="Tab"))
    await t("scroll(200)", lambda: call("scroll", tab_id="t0", dy=200))
    await t("paste_text", lambda: call("paste_text", tab_id="t0", text="x"))
    await t("hover", lambda: call("hover", tab_id="t0", x=200, y=200))
    await t("click_at", lambda: call("click_at", tab_id="t0", x=100, y=100))
    await t("drag", lambda: call("drag", tab_id="t0", x1=50, y1=50, x2=200, y2=200))
    await t("wait_for(timeout)", lambda: call("wait_for", tab_id="t0", timeout_s=0.3))

    # ─── D. extraction ─────────────────────────────────────────────────
    print("\n--- D. extraction ---", flush=True)
    await t("extract_text", lambda: call("extract_text", tab_id="t0", max_chars=2000))
    await t("extract_links", lambda: call("extract_links", tab_id="t0", max_links=20))
    await t("grep_text", lambda: call("grep_text", tab_id="t0", pattern="Customer", max_matches=5))
    await t("dom_query(input)", lambda: call("dom_query", tab_id="t0", selector="input", max_results=20))
    await t("inspect_element", lambda: call("inspect_element", tab_id="t0", selector="legend"))
    await t("extract_markdown", lambda: call("extract_markdown", tab_id="t0", max_chars=5000))
    await t("clone_element", lambda: call("clone_element", tab_id="t0", selector="form", max_doc_chars=4000))

    # ─── E. visual ─────────────────────────────────────────────────────
    print("\n--- E. visual ---", flush=True)
    await t("screenshot(q40)", lambda: call("screenshot", tab_id="t0", quality=40))
    await t("screenshot_region", lambda: call("screenshot_region", tab_id="t0", x=0, y=0, w=200, h=100, quality=40))

    # ─── F. JS ─────────────────────────────────────────────────────────
    print("\n--- F. JS ---", flush=True)
    await t("evaluate", lambda: call("evaluate", tab_id="t0", expression="1+1"))
    await t("inject_css", lambda: call("inject_css", tab_id="t0", css="body{outline:2px solid red}"))

    # ─── G. devtools ───────────────────────────────────────────────────
    print("\n--- G. devtools ---", flush=True)
    await t("get_console_logs", lambda: call("get_console_logs", tab_id="t0"))
    await t("get_network_requests", lambda: call("get_network_requests", tab_id="t0", max_n=10))
    await t("memory_metrics", lambda: call("memory_metrics", tab_id="t0"))
    await t("get_cookies", lambda: call("get_cookies", tab_id="t0"))
    await t("set_cookies", lambda: call("set_cookies", tab_id="t0", cookies=[{"name": "u", "value": "v", "domain": ".httpbin.org"}]))
    await t("clear_cookies", lambda: call("clear_cookies", tab_id="t0"))
    await t("clear_logs", lambda: call("clear_logs", tab_id="t0"))

    # ─── H. stealth/meta ───────────────────────────────────────────────
    print("\n--- H. stealth/meta ---", flush=True)
    await t("rotate_fingerprint", lambda: call("rotate_fingerprint", tab_id="t0"))
    await t("set_verbosity(compact)", lambda: call("set_verbosity", level="compact"))

    # ─── I. interception (NEW) ─────────────────────────────────────────
    print("\n--- I. interception (NEW route_/har_) ---", flush=True)
    await t("route_add(fulfill)", lambda: call("route_add", tab_id="t0", action="fulfill",
                                                   url_pattern="/never-fires-x", status=200, body="x"))
    await t("route_add(continue+headers)", lambda: call("route_add", tab_id="t0", action="continue",
                                                          url_pattern="/never-fires-y",
                                                          headers={"x-spy": "1"}))
    await t("route_add(modify)", lambda: call("route_add", tab_id="t0", action="modify",
                                                url_pattern="/never-fires-z",
                                                body_replace=[["foo", "bar"]]))
    await t("route_add(tee)", lambda: call("route_add", tab_id="t0", action="tee",
                                              url_pattern="/never-fires-t", capture=5))
    await t("route_add(redirect)", lambda: call("route_add", tab_id="t0", action="redirect",
                                                  url_pattern="/never-fires-r",
                                                  new_url="https://example.com"))
    await t("route_add(block+5xx)", lambda: call("route_add", tab_id="t0", action="block",
                                                  status_min=500))
    await t("route_add_many(3 rules)", lambda: call("route_add_many", tab_id="t0", rules=[
        {"action": "block", "url_pattern": "doubleclick.net"},
        {"action": "block", "url_pattern": "googletagmanager"},
        {"action": "fulfill", "url_pattern": "/heartbeat", "status": 204},
    ]))
    await t("route_list", lambda: call("route_list", tab_id="t0"))
    await t("route_set_enabled(off)", lambda: call("route_set_enabled", tab_id="t0", rule_id="r0", enabled=False))
    await t("route_block_set", lambda: call("route_block_set", tab_id="t0", trackers=True))
    await t("route_captures(empty)", lambda: call("route_captures", tab_id="t0", rule_id="r3"))
    await t("route_remove(r0)", lambda: call("route_remove", tab_id="t0", rule_id="r0"))
    await t("route_remove(all)", lambda: call("route_remove", tab_id="t0", all=True))

    await t("har_record_start", lambda: call("har_record_start", tab_id="t0"))
    await t("navigate(httpbin/get for HAR)",
            lambda: call("navigate", tab_id="t0", url="https://httpbin.org/get"))
    await asyncio.sleep(1)
    await t("har_record_stop", lambda: call("har_record_stop", tab_id="t0"))
    await t("har_dump(inline)", lambda: call("har_dump", tab_id="t0"))
    await t("har_dump(disk)", lambda: call("har_dump", tab_id="t0", path="/tmp/umbra_audit.har"))
    await t("har_replay_load", lambda: call("har_replay_load", tab_id="t0", path="/tmp/umbra_audit.har"))
    await t("har_clear", lambda: call("har_clear", tab_id="t0", recording=True, replay=True))

    # legacy compat
    await t("dynamic_hook(legacy)", lambda: call("dynamic_hook", tab_id="t0",
                                                  url_pattern="/never-fires-l", action="block"))

    # ─── J. files ──────────────────────────────────────────────────────
    print("\n--- J. files/net ---", flush=True)
    await t("setup_downloads", lambda: call("setup_downloads", tab_id="t0", download_dir="/tmp/umbra_dl_audit"))
    await t("wait_for_download(timeout)", lambda: call("wait_for_download", tab_id="t0",
                                                        download_dir="/tmp/umbra_dl_audit", timeout_s=1))
    await t("block_urls", lambda: call("block_urls", tab_id="t0", patterns=["*ads*"]))
    await t("set_extra_headers", lambda: call("set_extra_headers", tab_id="t0", headers={"X-A": "1"}))
    await t("set_viewport", lambda: call("set_viewport", tab_id="t0", width=1280, height=720))

    # ─── K. TLS ────────────────────────────────────────────────────────
    print("\n--- K. TLS fetch ---", flush=True)
    with contextlib.suppress(Exception):
        await t("tls_fetch", lambda: call("tls_fetch", url="https://httpbin.org/get", max_chars=500))

    # ─── L. sessions ───────────────────────────────────────────────────
    print("\n--- L. sessions ---", flush=True)
    await t("session_save", lambda: call("session_save", tab_id="t0", name="audit", passphrase="x"))
    await t("session_list", lambda: call("session_list"))
    await t("session_load", lambda: call("session_load", tab_id="t0", name="audit", passphrase="x"))
    await t("session_delete", lambda: call("session_delete", name="audit"))

    # ─── M. multi-browser ──────────────────────────────────────────────
    print("\n--- M. multi-browser ---", flush=True)
    await t("spawn(alice)", lambda: call("spawn", url="https://example.com", browser_id="alice"))
    await asyncio.sleep(1)
    await t("close_browser(alice)", lambda: call("close_browser", browser_id="alice"))

    # ─── N. batch ──────────────────────────────────────────────────────
    print("\n--- N. batch ---", flush=True)
    await t("batch(5 reads)", lambda: call("batch", calls=[
        {"tool": "current_state", "args": {"tab_id": "t0", "force_refresh": True}},
        {"tool": "extract_links", "args": {"tab_id": "t0", "max_links": 5, "force_refresh": True}},
        {"tool": "list_tabs", "args": {}},
        {"tool": "evaluate", "args": {"tab_id": "t0", "expression": "document.title"}},
        {"tool": "memory_metrics", "args": {"tab_id": "t0", "force_refresh": True}},
    ]))

    # ─── teardown ──────────────────────────────────────────────────────
    print("\n--- teardown ---", flush=True)
    await t("close(t0)", lambda: call("close", tab_id="t0"))
    await t("kill_all", lambda: call("kill_all"))

    # ─── summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 80)
    print(f"  umbra token audit — {len(RESULTS)} calls — tokenizer: {TOK_MODE}")
    print("=" * 80)
    full_tokens = [r["full_tokens"] for r in RESULTS if r["full_tokens"]]
    cmp_tokens = [r["cmp_tokens"] for r in RESULTS if r["cmp_tokens"]]
    full_bytes = [r["full_bytes"] for r in RESULTS if r["full_bytes"]]
    cmp_bytes = [r["cmp_bytes"] for r in RESULTS if r["cmp_bytes"]]
    ms_all = [r["ms"] for r in RESULTS]
    if full_tokens and cmp_tokens:
        print(f"  TOTAL tokens:  full={sum(full_tokens):>8}  cmp={sum(cmp_tokens):>8}"
              f"   saved={int(100*(1-sum(cmp_tokens)/sum(full_tokens))):>3}%")
        print(f"  TOTAL bytes:   full={sum(full_bytes):>8}  cmp={sum(cmp_bytes):>8}"
              f"   saved={int(100*(1-sum(cmp_bytes)/sum(full_bytes))):>3}%")
        print(f"  per-call tokens (cmp): median={int(statistics.median(cmp_tokens)):>4}  "
              f"max={max(cmp_tokens):>5}  min={min(cmp_tokens):>3}")
        print(f"  per-call ms:    median={int(statistics.median(ms_all)):>4}  "
              f"max={max(ms_all):>5}  min={min(ms_all):>3}")
    print()
    print(f"  TOP 10 BIGGEST tools (compressed tokens):")
    for r in sorted(RESULTS, key=lambda x: -x["cmp_tokens"])[:10]:
        print(f"    {r['cmp_tokens']:>5}t  {r['saved_pct']:>3}% saved  {r['ms']:>5}ms  {r['label']}")
    print()
    print(f"  TOP 10 SLOWEST:")
    for r in sorted(RESULTS, key=lambda x: -x["ms"])[:10]:
        print(f"    {r['ms']:>5}ms  {r['cmp_tokens']:>5}t  {r['label']}")

    out_path = Path("/tmp/umbra_token_audit.json")
    out_path.write_text(json.dumps({
        "tokenizer": TOK_MODE,
        "total_calls": len(RESULTS),
        "total_full_tokens": sum(full_tokens) if full_tokens else 0,
        "total_cmp_tokens": sum(cmp_tokens) if cmp_tokens else 0,
        "results": RESULTS,
    }, indent=2))
    print(f"\n  written: {out_path}")


asyncio.run(main())
