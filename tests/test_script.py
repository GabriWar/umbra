"""script tool: loops/variables over the tool surface, sandbox, gates, parallelism.

No browser except the e2e test at the bottom (headless, data: URLs only).
"""

from __future__ import annotations

import json
import time

import pytest
from fastmcp.exceptions import ToolError

from umbra import server


async def run(code: str, **kw):
    res = await server.mcp.call_tool("script", {"code": code, **kw})
    return res.structured_content or json.loads(res.content[0].text)


async def test_loop_print_return():
    out = await run(
        "seen = []\n"
        "for i in range(3):\n"
        "    b = await t.list_browsers()\n"
        "    seen.append(i)\n"
        "    print('step', i, {'n': i})\n"
        "return {'seen': seen, 'm': re.findall(r'\\d', 'a1b2'), 'j': json.loads('[1]')}\n")
    assert out["ok"] is True
    assert out["result"] == {"seen": [0, 1, 2], "m": ["1", "2"], "j": [1]}
    assert out["log"] == ["step 0 {\"n\": 0}", "step 1 {\"n\": 1}", "step 2 {\"n\": 2}"]
    assert out["calls"] == 3


@pytest.mark.parametrize("code, why", [
    ("import os", "Import"),
    ("from os import path", "ImportFrom"),
    ("x = (1).__class__", "__class__"),
    ("x = _secret", "'_'"),
    ("x = '{0.x}'.format(1)", "format"),
    ("c = t.list_browsers()\nx = c.cr_frame", "cr_frame"),
    ("g = (i for i in [1])\nx = g.gi_frame", "gi_frame"),
    ("class A: pass", "ClassDef"),
    ("def f(_a): return 1", "'_'"),
])
async def test_sandbox_refuses(code, why):
    with pytest.raises(ToolError, match=why):
        await run(code)


@pytest.mark.parametrize("code", [
    "return re.enum",                 # module attribute → sys
    "return json.decoder",
    "return open('/etc/passwd')",
    "return getattr(t, 'x')",
    "await t.script(code='1')",       # no nesting
])
async def test_sandbox_runtime_failures(code):
    out = await run(code)
    assert out["ok"] is False


async def test_positional_args_still_hit_the_gates(monkeypatch):
    seen = []

    async def gate(tool, args):
        if tool == "script":
            return
        seen.append((tool, args))
        raise ToolError("gated")

    monkeypatch.setattr(server, "_vault_gate", gate)
    out = await run("await t.navigate('t9', 'https://x.com')")
    assert out["ok"] is False and "gated" in out["error"] and "line 1" in out["error"]
    assert seen == [("navigate", {"tab_id": "t9", "url": "https://x.com"})]


async def test_gather_runs_concurrently():
    t0 = time.perf_counter()
    out = await run(
        "async def one(i):\n"
        "    await sleep(0.3)\n"
        "    return i\n"
        "return await gather(*[one(i) for i in range(5)])\n")
    assert out["result"] == [0, 1, 2, 3, 4]
    assert time.perf_counter() - t0 < 1.0


async def test_timeout_and_error_line():
    out = await run("await sleep(5)", timeout_s=1)
    assert out["ok"] is False and "timed out" in out["error"]
    out = await run("x = 1\ny = {}['nope']")
    assert out["ok"] is False and out["error"].startswith("line 2: KeyError")


async def test_bad_args_and_syntax():
    out = await run("await t.list_browsers(bogus=1)")
    assert out["ok"] is False and "list_browsers" in out["error"]
    out = await run("for")
    assert out["ok"] is False and "SyntaxError" in out["error"]


@pytest.mark.e2e
async def test_parallel_browsers_e2e():
    code = (
        "async def one(i):\n"
        "    b = await t.spawn(url=f'data:text/html,<h1>page{i}</h1>', browser_id=f'script-e2e-{i}', headless=True)\n"
        "    r = await t.extract_text(b['tab_id'], selector='h1')\n"
        "    await t.close_browser(browser_id=f'script-e2e-{i}')\n"
        "    return r['text']\n"
        "return await gather(one(0), one(1))\n")
    out = await run(code, timeout_s=120)
    assert out["ok"] is True, out
    assert out["result"] == ["page0", "page1"]
