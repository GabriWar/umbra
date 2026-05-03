"""Handoff: pop a live remote view of a tab so a human can step in.

Use case: the agent (Claude over MCP) hits a captcha / 2FA / login challenge
/ paywall — anything that needs a real human. Calling `request_user_input`
spins up a tiny WebSocket+HTTP server on a random localhost port, hands back
a URL, the user opens it in their browser, sees a live feed of the headless
tab, clicks/types as needed, hits "I'm done" → control returns to the agent.

How it works:
  - Single websockets-server bound to localhost:<random>. Same port handles
    HTTP fallback (serves the index page) and the WS upgrade.
  - Screenshots: poll CDP `Page.captureScreenshot(format=jpeg, quality=55)`
    at 5fps, push base64 over WS.
  - Input: receive {type, x, y, button, key, ...} over WS, dispatch via
    CDP `Input.dispatch{Mouse,Key}Event`. Coords translated from displayed
    <img> dims to actual viewport pixels.

Deps: websockets (already a transitive dep via nodriver). Pure stdlib else.
Works through SSH tunnels.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import shutil
import socket
import subprocess
from contextlib import suppress
from typing import Any

import websockets
from websockets.asyncio.server import serve as ws_serve

log = logging.getLogger("umbra.handoff")


# Index HTML built once. Sets up canvas-style remote view + input forwarding.
# Note: success message uses textContent (not innerHTML) for safety.
_INDEX_HTML = b"""<!doctype html>
<html><head>
<meta charset="utf-8"/>
<title>umbra handoff</title>
<style>
body { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; background: #0a0a0a; color: #e5e5e5; }
#bar { padding: 10px 14px; background: #1a1a1a; border-bottom: 1px solid #2a2a2a; display: flex; gap: 12px; align-items: center; }
#reason { flex: 1; font-size: 14px; color: #ccc; }
#kbd-hint { font-size: 11px; color: #666; }
button { background: #2ecc71; color: #fff; border: none; padding: 8px 16px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 13px; }
button:hover { background: #27ae60; }
#viewport { position: relative; display: inline-block; user-select: none; }
img { display: block; cursor: crosshair; max-width: 100vw; image-rendering: pixelated; }
.cursor { position: absolute; width: 12px; height: 12px; border: 2px solid #ff3333; border-radius: 50%; pointer-events: none; transform: translate(-50%, -50%); }
#status { position: fixed; bottom: 8px; right: 12px; font-size: 11px; color: #666; }
.ok { color: #2ecc71; }
.dead { color: #e74c3c; }
</style>
</head>
<body>
<div id="bar">
    <span id="reason">handoff active - interact with the page below</span>
    <span id="kbd-hint">click in the viewport, then keys forward</span>
    <button id="done">I'M DONE</button>
</div>
<div id="viewport" tabindex="0">
    <img id="screen" alt="loading..."/>
    <div class="cursor" id="cursor" style="display:none"></div>
</div>
<div id="status">connecting...</div>

<script>
// WS lives on the same auth-token path as this page. Auto-upgrade to wss
// when served over https (cloudflared tunnels are always https).
const wsScheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
const wsPath = location.pathname.replace(/[/]$/, '') + '/ws';
const ws = new WebSocket(`${wsScheme}//${location.host}${wsPath}`);
const img = document.getElementById('screen');
const cursor = document.getElementById('cursor');
const viewport = document.getElementById('viewport');
const status = document.getElementById('status');

ws.onopen = () => { status.textContent = 'connected'; status.className = 'ok'; };
ws.onclose = () => { status.textContent = 'disconnected'; status.className = 'dead'; };
ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === 'frame') {
        img.src = 'data:image/jpeg;base64,' + m.b64;
    } else if (m.type === 'reason') {
        document.getElementById('reason').textContent = m.text;
    }
};

function imgCoords(e) {
    const r = img.getBoundingClientRect();
    return {
        x: Math.round((e.clientX - r.left) * (img.naturalWidth / r.width)),
        y: Math.round((e.clientY - r.top) * (img.naturalHeight / r.height)),
    };
}

img.addEventListener('mousemove', e => {
    const c = imgCoords(e);
    const vr = viewport.getBoundingClientRect();
    cursor.style.display = 'block';
    cursor.style.left = (e.clientX - vr.left) + 'px';
    cursor.style.top = (e.clientY - vr.top) + 'px';
    if (ws.readyState === 1) ws.send(JSON.stringify({type: 'mousemove', x: c.x, y: c.y}));
});
img.addEventListener('mouseleave', () => { cursor.style.display = 'none'; });
img.addEventListener('click', e => {
    const c = imgCoords(e);
    if (ws.readyState === 1) ws.send(JSON.stringify({type: 'click', x: c.x, y: c.y, button: 'left'}));
    viewport.focus();
});
img.addEventListener('contextmenu', e => {
    e.preventDefault();
    const c = imgCoords(e);
    if (ws.readyState === 1) ws.send(JSON.stringify({type: 'click', x: c.x, y: c.y, button: 'right'}));
});
img.addEventListener('dblclick', e => {
    const c = imgCoords(e);
    if (ws.readyState === 1) ws.send(JSON.stringify({type: 'dblclick', x: c.x, y: c.y}));
});
viewport.addEventListener('keydown', e => {
    e.preventDefault();
    if (ws.readyState !== 1) return;
    ws.send(JSON.stringify({type: 'key', key: e.key, code: e.code,
        text: e.key.length === 1 && !e.ctrlKey && !e.metaKey ? e.key : '',
        ctrl: e.ctrlKey, shift: e.shiftKey, alt: e.altKey, meta: e.metaKey}));
});
viewport.addEventListener('wheel', e => {
    e.preventDefault();
    const c = imgCoords(e);
    if (ws.readyState === 1) ws.send(JSON.stringify({type: 'scroll', x: c.x, y: c.y, dx: e.deltaX, dy: e.deltaY}));
}, { passive: false });

document.getElementById('done').onclick = () => {
    if (ws.readyState === 1) ws.send(JSON.stringify({type: 'done'}));
    document.body.replaceChildren();
    const msg = document.createElement('div');
    msg.style.padding = '40px';
    msg.style.fontFamily = 'system-ui';
    msg.style.color = '#2ecc71';
    msg.textContent = 'handed back to umbra. close this tab.';
    document.body.appendChild(msg);
};
viewport.focus();
</script>
</body></html>"""


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _try_cloudflared_tunnel(port: int) -> tuple[subprocess.Popen[Any], str] | None:
    """Try to spawn `cloudflared tunnel --url http://localhost:port`. Returns
    (process, public_url) on success, None if cloudflared isn't installed.

    Uses Cloudflare Quick Tunnels — no signup, no auth, free. Public URL is
    printed by cloudflared on stderr. We tee the FULL cloudflared stderr to
    /tmp/umbra_cloudflared_<pid>.log so connection lifecycle (registered /
    disconnected / etc) is visible for debugging — without this the daemon-
    drain pattern silently swallows critical state.
    """
    if not shutil.which("cloudflared"):
        return None
    # Open a logfile that cloudflared writes directly to. Doesn't go through
    # a Python pipe at all, so no buffer-full deadlock possible.
    import os as _os
    import tempfile as _tempfile
    logfile = _tempfile.NamedTemporaryFile(
        mode="w+", prefix="umbra_cloudflared_", suffix=".log", delete=False,
    )
    # SUBTLE: if the user has a personal authenticated cloudflared setup
    # (~/.cloudflared/<uuid>.json), cloudflared auto-discovers it and tries
    # to mix Quick Tunnel mode with named-tunnel routing. Result: the
    # `*.trycloudflare.com` URL is advertised but requests get routed to the
    # WRONG tunnel and CF returns 404. Force Quick Tunnel mode by spawning
    # cloudflared with a temp HOME so it can't find any local credentials.
    import os as _os
    import tempfile as _tempfile
    isolated_home = _tempfile.mkdtemp(prefix="umbra_cf_home_")
    env = _os.environ.copy()
    env["HOME"] = isolated_home
    env.pop("XDG_CONFIG_HOME", None)
    try:
        proc = subprocess.Popen(
            # `--protocol http2` is more reliable than the default QUIC for
            # sustained WebSocket streams (screenshot frames at N fps). QUIC
            # has aggressive backpressure that drops frames; HTTP2 buffers.
            ["cloudflared", "tunnel", "--url", f"http://127.0.0.1:{port}",
             "--protocol", "http2", "--no-autoupdate"],
            stdout=logfile, stderr=subprocess.STDOUT, text=True, env=env,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("cloudflared spawn failed: %s", e)
        logfile.close()
        return None
    log.info("cloudflared logs → %s (HOME=%s)", logfile.name, isolated_home)

    # Tail the logfile until we find the URL + a "registered connection" line.
    import time as _t
    deadline = _t.time() + 25
    public_url = ""
    saw_connection = False
    pos = 0
    with open(logfile.name) as f:
        while _t.time() < deadline:
            f.seek(pos)
            chunk = f.read()
            pos = f.tell()
            for line in chunk.splitlines():
                if not public_url:
                    m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
                    if m:
                        public_url = m.group(0)
                        log.info("cloudflared got URL, waiting for tunnel registration...")
                if "Registered tunnel connection" in line or "Connection " in line and "registered" in line:
                    saw_connection = True
            if public_url and saw_connection:
                break
            if proc.poll() is not None:
                log.warning("cloudflared exited early; see %s", logfile.name)
                return None
            _t.sleep(0.4)
    logfile.close()
    if not public_url:
        proc.terminate()
        log.warning("cloudflared never printed URL; see %s", logfile.name)
        return None
    if not saw_connection:
        log.warning("cloudflared got URL but no 'registered' line — tunnel may 404")
    # Even after registration, give Cloudflare's edge POPs a moment to fully
    # route. trycloudflare URLs sometimes need 5-10s to be globally reachable.
    _t.sleep(4)
    return proc, public_url


class HandoffSession:
    """One handoff session: live remote view of a tab via WS+HTTP on the same port."""

    def __init__(self, tab: Any, reason: str = "user input needed", fps: int = 3,
                 tunnel: bool = False):
        self.tab = tab
        self.reason = reason
        self.fps = fps
        self.tunnel = tunnel
        self.ws_server: Any = None
        self.done_event = asyncio.Event()
        self.url: str = ""
        self._screenshot_task: asyncio.Task[None] | None = None
        # Auth: random URL-path token. Knowledge of the full URL = auth.
        # 192-bit secret, URL-safe. Anyone hitting any other path gets 404.
        self._auth_token: str = secrets.token_urlsafe(24)
        self._cloudflared_proc: subprocess.Popen[Any] | None = None

    async def start(self) -> str:
        """Start the server, return the URL the user should open.

        URL contains a random 192-bit auth token in the path
        (`/h-<token>/`). Any request to a different path returns 404.
        Knowledge of the URL = auth — share carefully (LAN/internet OK,
        public Twitter not OK).

        If `tunnel=True` and `cloudflared` is on PATH, spawns a Cloudflare
        Quick Tunnel exposing the local port to a public trycloudflare.com
        URL. Useful when umbra runs on a VPS but you're on a different
        machine. Tunnel auto-tears-down on stop()."""
        port = _free_port()
        host = "127.0.0.1" if not self.tunnel else "127.0.0.1"  # cloudflared connects locally

        # Bind handoff to localhost. If tunnel=True we expose the local port
        # via cloudflared, NOT by binding 0.0.0.0 (safer — cloudflared adds TLS).
        bind_host = "127.0.0.1"
        expected_path = f"/h-{self._auth_token}/"

        async def http_fallback(connection: Any, request: Any) -> Any:
            # Path gate: only the auth-token path serves HTML or upgrades to WS.
            req_path = request.path.split("?", 1)[0]
            log.info("handoff request: path=%r upgrade=%r", req_path,
                     request.headers.get("Upgrade", ""))
            # Accept variants: /h-TOKEN, /h-TOKEN/, /h-TOKEN/ws, etc.
            if not req_path.startswith(f"/h-{self._auth_token}"):
                log.warning("handoff 404: path %r doesn't start with /h-%s...",
                            req_path, self._auth_token[:8])
                from websockets.http11 import Response
                from websockets.datastructures import Headers
                body = b"not found"
                return Response(
                    status_code=404, reason_phrase="Not Found",
                    headers=Headers([
                        ("Content-Type", "text/plain"),
                        ("Content-Length", str(len(body))),
                    ]),
                    body=body,
                )
            # If client doesn't request websocket, serve the index page.
            if request.headers.get("Upgrade", "").lower() != "websocket":
                from websockets.http11 import Response
                from websockets.datastructures import Headers
                # Inline the auth token into the page so the WS connect URL
                # in the JS knows it.
                html = _INDEX_HTML.replace(b"__UMBRA_TOKEN__", self._auth_token.encode())
                return Response(
                    status_code=200, reason_phrase="OK",
                    headers=Headers([
                        ("Content-Type", "text/html; charset=utf-8"),
                        ("Content-Length", str(len(html))),
                        # SECURITY: prevent the auth-token URL from leaking via
                        # Referer header if the user navigates away from the
                        # handoff page. no-referrer = no Referer sent at all.
                        ("Referrer-Policy", "no-referrer"),
                        # Block the page from being framed by hostile sites.
                        ("X-Frame-Options", "DENY"),
                        # Disable the page from making cross-origin fetches that
                        # could exfiltrate the URL.
                        ("Cross-Origin-Opener-Policy", "same-origin"),
                    ]),
                    body=html,
                )
            return None  # WS upgrade allowed only on the token path

        self.ws_server = await ws_serve(
            self._ws_handler, bind_host, port,
            process_request=http_fallback,
        )

        # Optional Cloudflare tunnel for public access (VPS → home browser).
        public_base = ""
        if self.tunnel:
            t = _try_cloudflared_tunnel(port)
            if t:
                self._cloudflared_proc, public_base = t
                log.info("cloudflared tunnel up: %s", public_base)
            else:
                log.warning("tunnel=True but cloudflared not installed — falling back to localhost. "
                             "Install: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/")

        base = public_base if public_base else f"http://127.0.0.1:{port}"
        self.url = f"{base}{expected_path}"

        import sys as _sys
        print(f"\n[umbra handoff] OPEN THIS URL: {self.url}\n  reason: {self.reason}\n"
              f"  (auth-by-URL: full path is the secret, share carefully)\n",
              file=_sys.stderr, flush=True)
        log.info("handoff started: %s  (reason: %s)", self.url, self.reason)
        return self.url

    async def wait(self, timeout_s: float = 300) -> bool:
        """Block until user clicks 'I'm done' or timeout. True if completed."""
        try:
            await asyncio.wait_for(self.done_event.wait(), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            log.warning("handoff timed out after %ds", timeout_s)
            return False

    async def stop(self) -> None:
        if self._screenshot_task:
            self._screenshot_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._screenshot_task
        if self.ws_server:
            self.ws_server.close()
            await self.ws_server.wait_closed()
        if self._cloudflared_proc:
            with suppress(Exception):
                self._cloudflared_proc.terminate()
                self._cloudflared_proc.wait(timeout=3)

    async def _ws_handler(self, websocket: Any) -> None:
        """One client connected. Start screenshot loop, handle inputs."""
        with suppress(Exception):
            await websocket.send(json.dumps({"type": "reason", "text": self.reason}))

        self._screenshot_task = asyncio.create_task(self._screenshot_loop(websocket))
        try:
            async for msg in websocket:
                try:
                    data = json.loads(msg)
                except (json.JSONDecodeError, TypeError):
                    continue
                if data.get("type") == "done":
                    self.done_event.set()
                    return
                with suppress(Exception):
                    await self._dispatch_input(data)
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            if self._screenshot_task:
                self._screenshot_task.cancel()

    async def _screenshot_loop(self, websocket: Any) -> None:
        """Stream JPEG screenshots over the websocket at `self.fps` Hz."""
        import nodriver as uc
        cdp = uc.cdp
        interval = 1.0 / max(self.fps, 1)
        while not self.done_event.is_set():
            try:
                shot = await self.tab.send(cdp.page.capture_screenshot(format_="jpeg", quality=45))
                if isinstance(shot, bytes):
                    shot = base64.b64encode(shot).decode("ascii")
                await websocket.send(json.dumps({"type": "frame", "b64": shot}))
            except websockets.exceptions.ConnectionClosed:
                return
            except Exception as e:  # noqa: BLE001
                log.debug("screenshot error: %s", e)
            await asyncio.sleep(interval)

    async def _dispatch_input(self, data: dict[str, Any]) -> None:
        """Translate a user input event to a CDP Input.* call."""
        import nodriver as uc
        cdp = uc.cdp
        t = data.get("type")
        x, y = float(data.get("x", 0)), float(data.get("y", 0))

        if t == "mousemove":
            await self.tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseMoved", x=x, y=y,
            ))
        elif t == "click":
            btn_str = data.get("button", "left")
            btn = cdp.input_.MouseButton(btn_str)
            await self.tab.send(cdp.input_.dispatch_mouse_event(
                type_="mousePressed", x=x, y=y, button=btn, click_count=1,
            ))
            await self.tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseReleased", x=x, y=y, button=btn, click_count=1,
            ))
        elif t == "dblclick":
            btn = cdp.input_.MouseButton("left")
            for _ in range(2):
                await self.tab.send(cdp.input_.dispatch_mouse_event(
                    type_="mousePressed", x=x, y=y, button=btn, click_count=2,
                ))
                await self.tab.send(cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased", x=x, y=y, button=btn, click_count=2,
                ))
        elif t == "key":
            key = data.get("key", "")
            await self.tab.send(cdp.input_.dispatch_key_event(
                type_="keyDown", key=key, code=data.get("code", ""),
                text=data.get("text", ""), unmodified_text=data.get("text", ""),
            ))
            await self.tab.send(cdp.input_.dispatch_key_event(
                type_="keyUp", key=key, code=data.get("code", ""),
            ))
        elif t == "scroll":
            await self.tab.send(cdp.input_.dispatch_mouse_event(
                type_="mouseWheel", x=x, y=y,
                delta_x=float(data.get("dx", 0)), delta_y=float(data.get("dy", 0)),
            ))
