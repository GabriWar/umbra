"""fill_secret end to end, through the MCP call path (middleware included).

Headless only: spawns its own browser_id with headless=True and serves the
login page from a local HTTP server, so nothing opens on screen and nothing
leaves the machine.
"""

from __future__ import annotations

import http.server
import json
import threading
from urllib.parse import parse_qs

import pytest
from fastmcp.exceptions import ToolError

from umbra import server
from umbra.vault import guard, store

pytestmark = pytest.mark.e2e

PASSWORD = "Tr0ub4dor&3-e2e"

LOGIN = b"""<!doctype html><title>login</title>
<script>
  // Main-world tampering the isolated world must bypass.
  window.__setterCalls = 0;
  const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
  Object.defineProperty(HTMLInputElement.prototype, 'value', {
    get() { return d.get.call(this); },
    set(v) { window.__setterCalls++; d.set.call(this, v); }, configurable: true});
</script>
<form method=post action=/done>
  <input name=user type=email autocomplete=username>
  <input name=pass type=password>
  <input id=note name=note type=text>
  <button>go</button>
</form>"""


class _Handler(http.server.BaseHTTPRequestHandler):
    received: dict[str, list[str]] = {}

    def do_GET(self):  # noqa: N802
        self._send(LOGIN)

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        _Handler.received = parse_qs(body)
        # Echo the password back, like a careless site would.
        self._send(f"<title>done</title><p>you sent {body}</p>".encode())

    def _send(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):
        pass


@pytest.fixture(scope="module")
def site():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def vault(tmp_path, monkeypatch, site):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(store, "_KDF_ITERATIONS", 1000)
    store.LocalVault.open("pp-long-enough", create=True).put(store.Entry(
        name="site", origins=[site], username="me@example.com", password=PASSWORD))
    store.unlock("pp-long-enough", minutes=5)
    yield
    store.lock()
    guard.hot.clear()
    guard.dirty_epoch.clear()
    guard._variants.clear()


async def call(tool: str, **args):
    res = await server.mcp.call_tool(tool, args)
    return res.structured_content or json.loads(res.content[0].text)


async def test_fill_secret_flow(site, vault):
    spawned = await call("spawn", url=f"{site}/login", browser_id="vault-e2e", headless=True)
    tab = spawned["tab_id"]
    try:
        # Agent JS ran while the login page was open → refuse until reload.
        await call("evaluate", tab_id=tab, expression="1")
        out = await call("fill_secret", tab_id=tab, name="site")
        assert out["ok"] is False and "agent code" in out["why"]

        await call("reload", tab_id=tab)
        out = await call("fill_secret", tab_id=tab, name="site")
        assert out["ok"] is True and out["filled"] == ["username", "password"]
        assert PASSWORD not in json.dumps(out)

        # Secret in the page: readers refused, clicks/keys still allowed.
        for tool, extra in (("evaluate", {"expression": "1"}), ("extract_text", {}),
                            ("screenshot", {}), ("aria_snapshot", {}),
                            ("wait_for", {"selector": "form", "timeout_s": 2}),
                            ("find_by_text", {"text": "go"})):
            with pytest.raises(ToolError, match="holds a secret"):
                await call(tool, tab_id=tab, **extra)
        await call("wait_for", tab_id=tab, network_idle_ms=100, timeout_s=2)

        # Type rule: a password never goes into a visible text field.
        out = await call("fill_secret", tab_id=tab, name="site", fields=["password"],
                         password_selector="#note")
        assert out["ok"] is False and out["refused"]

        out = await call("fill_secret", tab_id=tab, name="site", submit=True)
        assert out["submitted"] is True
        done = await call("wait_for", tab_id=tab, url_contains="/done", timeout_s=10)
        assert done["ok"] is True
        assert _Handler.received["pass"] == [PASSWORD]
        assert _Handler.received["user"] == ["me@example.com"]

        # New document: readers work again, and the echoed password is redacted.
        text = json.dumps(await call("extract_text", tab_id=tab))
        assert PASSWORD not in text and "[redacted:site]" in text
    finally:
        await call("close_browser", browser_id="vault-e2e")


async def test_fill_secret_refuses_other_origin(site, vault):
    other = site.replace("127.0.0.1", "localhost")
    spawned = await call("spawn", url=f"{other}/login", browser_id="vault-e2e2", headless=True)
    try:
        out = await call("fill_secret", tab_id=spawned["tab_id"], name="site")
        assert out["ok"] is False and "not an allowed origin" in out["why"]
        assert not guard.hot
    finally:
        await call("close_browser", browser_id="vault-e2e2")


async def test_rewriting_route_blocks_fill(site, vault):
    spawned = await call("spawn", url=f"{site}/login", browser_id="vault-e2e4", headless=True)
    tab = spawned["tab_id"]
    try:
        rule = await call("route_add", tab_id=tab, action="continue", url_pattern="/done",
                          new_url="http://127.0.0.1:9/elsewhere")
        out = await call("fill_secret", tab_id=tab, name="site")
        assert out["ok"] is False and any("route rules" in p for p in out["pages"])
        await call("route_remove", tab_id=tab, rule_id=rule["installed"]["id"])
        out = await call("fill_secret", tab_id=tab, name="site")
        assert out["ok"] is True
    finally:
        await call("close_browser", browser_id="vault-e2e4")


async def test_isolated_world_bypasses_page_setter(site, vault):
    spawned = await call("spawn", url=f"{site}/login", browser_id="vault-e2e3", headless=True)
    tab = spawned["tab_id"]
    try:
        out = await call("fill_secret", tab_id=tab, name="site")
        assert out["ok"] is True
        page = server._get_tab(tab)
        guard.hot.clear()  # read the page directly, past the gate, for this check
        assert await page.evaluate("window.__setterCalls") == 0
    finally:
        await call("close_browser", browser_id="vault-e2e3")
