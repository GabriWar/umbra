"""watch_start: read-only live view that follows the agent.

Headless only, local HTTP server, no tunnel.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import urllib.request

import pytest
import websockets

from umbra import server
from umbra.vault import guard

pytestmark = pytest.mark.e2e

PAGE = b"""<!doctype html><title>p</title>
<button id=b onclick="window.__clicks=(window.__clicks||0)+1"
  style="position:fixed;inset:0;width:100%;height:100%">x</button>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


async def call(tool: str, **args):
    res = await server.mcp.call_tool(tool, args)
    return res.structured_content or json.loads(res.content[0].text)


async def next_msg(ws, kind: str, timeout: float = 10.0) -> dict:
    async def find():
        while True:
            m = json.loads(await ws.recv())
            if m["type"] == kind:
                return m
    return await asyncio.wait_for(find(), timeout)


async def test_watch_follows_agent_and_ignores_input(site):
    a = (await call("spawn", url=f"{site}/a", browser_id="watch-e2e", headless=True))["tab_id"]
    b = (await call("spawn", url=f"{site}/b", browser_id="watch-e2e", headless=True))["tab_id"]
    out = await call("watch_start", tunnel=False, fps=5)
    url = out["url"]
    try:
        assert (await call("watch_start", tunnel=False))["already_running"] is True
        async with websockets.connect(url.replace("http", "ws").rstrip("/") + "/ws",
                                      max_size=None) as ws:
            await next_msg(ws, "view_only")
            for tab, path in ((a, "/a"), (b, "/b")):       # agent touches a, then b
                await call("current_state", tab_id=tab, force_refresh=True)
                for _ in range(15):
                    if (await next_msg(ws, "frame"))["url"].endswith(path):
                        break
                else:
                    pytest.fail(f"watch did not follow the agent to {path}")

            # Input from the viewer never reaches the page...
            await ws.send(json.dumps({"type": "click", "x": 100, "y": 100, "button": "left"}))
            await asyncio.sleep(0.5)
            assert await server._get_tab(b).evaluate("window.__clicks || 0") == 0

            # ...until they take control: then the agent waits and they drive.
            await ws.send(json.dumps({"type": "take"}))
            assert (await next_msg(ws, "control"))["active"] is True
            pending = asyncio.create_task(call("current_state", tab_id=b, force_refresh=True))
            await asyncio.sleep(0.8)
            assert not pending.done()
            await ws.send(json.dumps({"type": "click", "x": 100, "y": 100, "button": "left"}))
            await asyncio.sleep(0.5)
            assert await server._get_tab(b).evaluate("window.__clicks || 0") == 1
            await ws.send(json.dumps({"type": "give"}))
            assert (await next_msg(ws, "control"))["active"] is False
            resumed = await asyncio.wait_for(pending, 10)
            assert "_user_interacted" in resumed
            assert "_user_interacted" not in await call("current_state", tab_id=b,
                                                        force_refresh=True)

            # /done does not end a watch.
            await asyncio.to_thread(urllib.request.urlopen, url.rstrip("/") + "/done")
            assert not server._state["watch"].done_event.is_set()

            # A filled password on screen pauses the stream.
            guard.hot[b] = [{"values": ["x" * 8], "t": -1.0}]
            try:
                async def holds(_tab, _values):
                    return {"found": True, "t": 0.0}
                orig = server.vault_fill.holds
                server.vault_fill.holds = holds
                assert "password" in (await next_msg(ws, "paused"))["text"]
            finally:
                server.vault_fill.holds = orig
                guard.hot.clear()
            await next_msg(ws, "frame")                    # resumes
        # Viewer leaves while driving: the agent is released, not left waiting.
        async with websockets.connect(url.replace("http", "ws").rstrip("/") + "/ws",
                                      max_size=None) as ws2:
            await ws2.send(json.dumps({"type": "take"}))
            assert (await next_msg(ws2, "control"))["active"] is True
        for _ in range(20):
            if not server._human["active"]:
                break
            await asyncio.sleep(0.1)
        assert server._human["active"] is False
        await asyncio.wait_for(call("current_state", tab_id=b, force_refresh=True), 5)
    finally:
        assert (await call("watch_stop"))["stopped"] is True
        await call("close_browser", browser_id="watch-e2e")
