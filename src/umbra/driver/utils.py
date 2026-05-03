"""Tab-level helpers: thin wrappers around CDP for things the MCP server
exposes but that don't belong on a specific driver.

Each function takes the nodriver Tab as the first arg. Stateless. State that
needs accumulation (console logs, network requests) lives on `tab._umbra_*`
deques populated by handlers registered in browser.py:_configure_tab.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import re
from typing import Any

import nodriver as uc

log = logging.getLogger("umbra.driver.utils")
cdp = uc.cdp


# ────────────────────────── navigation / history ──────────────────────────

async def back(tab: Any) -> bool:
    """Go back in history. Returns True if there was history to go back to."""
    try:
        history = await tab.send(cdp.page.get_navigation_history())
        # history.entries[history.current_index - 1] is the previous page
        idx = history.current_index
        entries = history.entries
        if idx > 0:
            await tab.send(cdp.page.navigate_to_history_entry(entry_id=entries[idx - 1].id))
            return True
    except Exception as e:  # noqa: BLE001
        log.debug("back failed: %s", e)
    return False


async def forward(tab: Any) -> bool:
    """Go forward. Returns True if there was forward history."""
    try:
        history = await tab.send(cdp.page.get_navigation_history())
        idx = history.current_index
        entries = history.entries
        if idx + 1 < len(entries):
            await tab.send(cdp.page.navigate_to_history_entry(entry_id=entries[idx + 1].id))
            return True
    except Exception as e:  # noqa: BLE001
        log.debug("forward failed: %s", e)
    return False


async def reload(tab: Any, *, hard: bool = False) -> None:
    """Reload the page. hard=True bypasses cache."""
    await tab.send(cdp.page.reload(ignore_cache=hard))


# ─────────────────────────────── input ────────────────────────────────

async def press_key(tab: Any, key: str, *, modifiers: list[str] | None = None) -> None:
    """Press a single key (Enter, Escape, Tab, etc) without coord math.

    `modifiers` is a list like ['ctrl', 'shift']. Works for keyboard shortcuts.
    """
    mod_mask = 0
    if modifiers:
        for m in modifiers:
            mod_mask |= {"alt": 1, "ctrl": 2, "meta": 4, "shift": 8}.get(m.lower(), 0)
    text = key if len(key) == 1 else ""
    await tab.send(cdp.input_.dispatch_key_event(
        type_="keyDown", key=key, code=key, text=text,
        unmodified_text=text, modifiers=mod_mask,
    ))
    await tab.send(cdp.input_.dispatch_key_event(
        type_="keyUp", key=key, code=key, modifiers=mod_mask,
    ))


async def click_at(tab: Any, x: float, y: float, *, button: str = "left",
                    click_count: int = 1, humanize: bool = True) -> None:
    """Pixel click at (x, y). humanize=True (default) uses CDPDriver bezier path
    + log-normal segment timing — defeats mouse-trajectory fingerprinting.
    humanize=False = instant straight click (faster, more detectable).

    Per-tab CDPDriver caches the mouse position so successive humanized clicks
    interpolate from where we left off, matching real mouse continuity.
    """
    btn_enum = cdp.input_.MouseButton(button)
    if humanize:
        # Lazy-init a per-tab CDPDriver to preserve mouse-position state.
        drv = getattr(tab, "_umbra_cdp", None)
        if drv is None:
            from umbra.driver.interact import CDPDriver
            drv = CDPDriver(tab)
            tab._umbra_cdp = drv
        await drv.click(x, y, button=button)
        return
    # Direct path (no humanization) — for cases like rapid captcha tile clicks
    # where the agent IS the variability source.
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_="mouseMoved", x=x, y=y,
    ))
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_="mousePressed", x=x, y=y, button=btn_enum, click_count=click_count,
    ))
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_="mouseReleased", x=x, y=y, button=btn_enum, click_count=click_count,
    ))


async def paste_text(tab: Any, text: str) -> None:
    """Instant paste via CDP Input.insertText — much faster than per-char typing.

    Bypasses keystroke timing entirely (no humanizer). Use for trusted contexts
    where speed matters (forms with long pre-validated input, dev workflows).
    Detectable as non-keystroke input on sites that hash typing cadence."""
    await tab.send(cdp.input_.insert_text(text=text))


async def hover(tab: Any, x: float, y: float) -> None:
    """Mouse-hover at (x, y) without clicking. Triggers :hover CSS, dropdowns, tooltips."""
    await tab.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=x, y=y))


async def select_option(tab: Any, selector: str, value: str) -> bool:
    """Set a <select> element's value + dispatch change. Cleanest cross-browser path."""
    result = await tab.evaluate(f"""(() => {{
        const el = document.querySelector({selector!r});
        if (!el || el.tagName !== 'SELECT') return false;
        el.value = {value!r};
        el.dispatchEvent(new Event('change', {{bubbles: true}}));
        el.dispatchEvent(new Event('input', {{bubbles: true}}));
        return el.value === {value!r};
    }})()""")
    return bool(result)


async def get_response_body(tab: Any, request_id: str) -> dict[str, Any]:
    """Pull the full HTTP response body for a given request_id (from get_network_requests).

    Returns {body, base64Encoded}. Body is decoded if base64Encoded=False, else
    raw base64. Caller decodes binary as needed."""
    try:
        resp = await tab.send(cdp.network.get_response_body(
            request_id=cdp.network.RequestId(request_id),
        ))
        # nodriver returns (body, base64Encoded) tuple
        if isinstance(resp, tuple) and len(resp) == 2:
            return {"body": resp[0], "base64": bool(resp[1])}
        return {"body": str(resp), "base64": False}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def upload_file(tab: Any, selector: str, paths: list[str]) -> bool:
    """Set <input type=file>'s files to one or more local paths.

    Bypasses the OS file picker entirely (CDP DOM.setFileInputFiles). Works
    for both single and multi-file inputs. Paths must be absolute and exist
    on the umbra server's filesystem (NOT the agent's). Returns False if the
    selector didn't match a file input."""
    # Resolve the file input element to a backend node id
    js = f"(() => {{ const el = document.querySelector({selector!r}); if (!el || el.tagName !== 'INPUT' || el.type !== 'file') return null; return el; }})()"
    obj = await tab.evaluate(js)
    if not obj:
        return False
    # Use DOM.setFileInputFiles via the element's CSS path. Since we already
    # confirmed the element exists, find it again via DOM API for the node id.
    try:
        doc = await tab.send(cdp.dom.get_document())
        root = await tab.send(cdp.dom.query_selector(node_id=doc.node_id, selector=selector))
        if not root:
            return False
        await tab.send(cdp.dom.set_file_input_files(files=paths, node_id=root))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("upload_file failed: %s", e)
        return False


async def setup_downloads(tab: Any, download_dir: str) -> None:
    """Configure the browser to save downloads to a specific directory + auto-allow.

    Without this, clicks that trigger a download go nowhere (CDP default is to
    deny). Pass an absolute path that already exists. Files appear with their
    server-side filename. Pair with wait_for_download to know when one finished."""
    import os as _os
    _os.makedirs(download_dir, exist_ok=True)
    await tab.send(cdp.browser.set_download_behavior(
        behavior="allow", download_path=download_dir,
    ))


async def wait_for_download(tab: Any, download_dir: str, *, timeout_s: float = 60.0,
                             min_bytes: int = 1) -> dict[str, Any]:
    """Poll `download_dir` for a new file appearing + stable size. Returns
    {path, size_bytes, name} or {error}.

    'Stable size' = file size unchanged for 500ms (download finished writing)."""
    import os as _os
    deadline = asyncio.get_event_loop().time() + timeout_s
    seen_at_start = set(_os.listdir(download_dir)) if _os.path.exists(download_dir) else set()

    while asyncio.get_event_loop().time() < deadline:
        try:
            current = set(_os.listdir(download_dir))
            new_files = [f for f in current - seen_at_start if not f.endswith(".crdownload")]
            if new_files:
                # Pick the newest
                full_paths = [_os.path.join(download_dir, f) for f in new_files]
                full_paths.sort(key=lambda p: _os.path.getmtime(p), reverse=True)
                target = full_paths[0]
                # Wait until size is stable (write finished)
                size1 = _os.path.getsize(target)
                if size1 < min_bytes:
                    await asyncio.sleep(0.3)
                    continue
                await asyncio.sleep(0.5)
                size2 = _os.path.getsize(target)
                if size1 == size2:
                    return {"path": target, "size_bytes": size2, "name": _os.path.basename(target)}
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.3)
    return {"error": "timeout", "waited_s": timeout_s}


async def wait_for_text(tab: Any, text: str, *, case_sensitive: bool = False,
                         timeout_s: float = 30.0, selector: str = "body") -> dict[str, Any]:
    """Poll until `text` appears in the page (or selector subtree). Returns
    {ok, found_in_ms} or {ok: False, why: 'timeout'}.

    Handles AJAX-loaded content where wait_for(selector=...) doesn't fit."""
    deadline = asyncio.get_event_loop().time() + timeout_s
    start = asyncio.get_event_loop().time()
    js_search = (
        f"(document.querySelector({selector!r})?.innerText || '')"
        + ("" if case_sensitive else ".toLowerCase()")
        + f".includes({text.lower() if not case_sensitive else text!r})"
    )
    while asyncio.get_event_loop().time() < deadline:
        try:
            if await tab.evaluate(js_search):
                return {"ok": True, "found_in_ms": int((asyncio.get_event_loop().time() - start) * 1000)}
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.25)
    return {"ok": False, "why": "timeout"}


async def memory_metrics(tab: Any) -> dict[str, Any]:
    """Performance counters: JS heap, DOM nodes, layout count, etc."""
    try:
        await tab.send(cdp.performance.enable())
        metrics = await tab.send(cdp.performance.get_metrics())
        return {m.name: m.value for m in metrics}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


async def clear_cookies(tab: Any) -> int:
    """Wipe ALL cookies from the browser context. Returns count cleared."""
    cookies = await tab.send(cdp.network.get_cookies())
    n = len(cookies)
    await tab.send(cdp.network.clear_browser_cookies())
    return n


async def block_urls(tab: Any, patterns: list[str]) -> None:
    """Network-level URL block list (CDP Network.setBlockedURLs).

    `patterns` are wildcard strings — `*googletag*`, `*.gif`, `https://ads.example.com/*`.
    Replaces any previous block list (pass empty list to clear). Cheap; fires
    pre-request, no overhead per allowed request."""
    await tab.send(cdp.network.set_blocked_ur_ls(urls=patterns))


async def set_extra_headers(tab: Any, headers: dict[str, str]) -> None:
    """Inject headers into every outgoing request from this tab. CDP-level."""
    await tab.send(cdp.network.set_extra_http_headers(
        headers=cdp.network.Headers(headers),
    ))


async def set_viewport(tab: Any, width: int, height: int, *,
                        device_scale_factor: float = 1.0, mobile: bool = False) -> None:
    """Change the viewport size mid-session via CDP Emulation.setDeviceMetricsOverride."""
    await tab.send(cdp.emulation.set_device_metrics_override(
        width=width, height=height,
        device_scale_factor=device_scale_factor, mobile=mobile,
    ))


async def drag(tab: Any, x1: float, y1: float, x2: float, y2: float, *,
                steps: int = 25, button: str = "left") -> None:
    """Humanized mouse drag — press at (x1,y1), bezier-interpolated path to (x2,y2),
    release. Use for slider captchas, drag-drop, range sliders."""
    import math, random
    from umbra.driver.interact import _human_path
    btn_enum = cdp.input_.MouseButton(button)

    # Move into start position with a short approach path
    path_in = list(_human_path((x1 - 30, y1 - 20), (x1, y1), 6))
    for px, py in path_in:
        await tab.send(cdp.input_.dispatch_mouse_event(type_="mouseMoved", x=px, y=py))
        await asyncio.sleep(random.uniform(0.008, 0.020))
    # Press
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_="mousePressed", x=x1, y=y1, button=btn_enum, click_count=1,
    ))
    await asyncio.sleep(random.lognormvariate(-2.7, 0.25))
    # Drag along bezier with mousePressed-style moves (button still held)
    for px, py in _human_path((x1, y1), (x2, y2), steps):
        await tab.send(cdp.input_.dispatch_mouse_event(
            type_="mouseMoved", x=px, y=py, button=btn_enum,
        ))
        await asyncio.sleep(random.uniform(0.012, 0.030))
    # Release
    await asyncio.sleep(random.lognormvariate(-2.5, 0.3))
    await tab.send(cdp.input_.dispatch_mouse_event(
        type_="mouseReleased", x=x2, y=y2, button=btn_enum, click_count=1,
    ))


async def scroll(tab: Any, *, dy: int = 600, dx: int = 0,
                 to_bottom: bool = False, selector: str | None = None) -> None:
    """Scroll the page.

    - selector=".x" → scrollIntoView on that element
    - to_bottom=True → scroll to document.body.scrollHeight
    - else → scroll by dx, dy from current position
    """
    if selector:
        await tab.evaluate(
            f"document.querySelector({selector!r})?.scrollIntoView({{behavior:'smooth', block:'center'}})"
        )
        return
    if to_bottom:
        await tab.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return
    await tab.evaluate(f"window.scrollBy({dx}, {dy})")


# ────────────────────────────── waits ─────────────────────────────────

async def wait_for(tab: Any, *, selector: str | None = None,
                    url_contains: str | None = None,
                    network_idle_ms: int = 0,
                    timeout_s: float = 30.0) -> dict[str, Any]:
    """Multi-mode wait. Returns {ok, why}.

    Provide ONE of selector / url_contains / network_idle_ms (timeout-only is OK).
    network_idle_ms = wait until N ms of no in-flight network requests.
    """
    deadline = asyncio.get_event_loop().time() + timeout_s
    if selector:
        while asyncio.get_event_loop().time() < deadline:
            try:
                found = await tab.evaluate(f"!!document.querySelector({selector!r})")
                if found:
                    return {"ok": True, "why": "selector_found"}
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.2)
        return {"ok": False, "why": "selector_timeout"}
    if url_contains:
        while asyncio.get_event_loop().time() < deadline:
            try:
                cur = await tab.evaluate("location.href")
                if isinstance(cur, str) and url_contains in cur:
                    return {"ok": True, "why": "url_match", "url": cur}
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.2)
        return {"ok": False, "why": "url_timeout"}
    if network_idle_ms > 0:
        # Cheap approximation: poll Performance entries; truly idle when no
        # new network resource entry appears for `network_idle_ms`.
        last_count = -1
        idle_since = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() < deadline:
            try:
                count = await tab.evaluate("performance.getEntriesByType('resource').length")
                if count == last_count:
                    if (asyncio.get_event_loop().time() - idle_since) * 1000 >= network_idle_ms:
                        return {"ok": True, "why": "network_idle"}
                else:
                    last_count = count
                    idle_since = asyncio.get_event_loop().time()
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.15)
        return {"ok": False, "why": "network_idle_timeout"}
    # No condition → just sleep timeout_s
    await asyncio.sleep(timeout_s)
    return {"ok": True, "why": "timeout_only"}


# ────────────────────────── extraction / inspection ───────────────────────

async def extract_text(tab: Any, *, selector: str = "body",
                       max_chars: int = 5000) -> str:
    """selector → innerText, capped to max_chars with truncation marker."""
    raw = await tab.evaluate(
        f"document.querySelector({selector!r})?.innerText || ''"
    )
    if not isinstance(raw, str):
        return ""
    if len(raw) > max_chars:
        return raw[:max_chars] + f"...[+{len(raw) - max_chars}c]"
    return raw


async def extract_links(tab: Any, *, max_links: int = 80,
                        same_origin_only: bool = False) -> list[dict[str, str]]:
    """List visible links on the page."""
    import json as _j
    js = """JSON.stringify(
        Array.from(document.querySelectorAll('a[href]'))
            .filter(a => a.offsetParent !== null && a.href)
            .slice(0, %d)
            .map(a => ({
                text: (a.innerText || a.title || '').trim().substring(0, 100),
                url: a.href,
            }))
    )""" % max_links
    raw = await tab.evaluate(js)
    links = _j.loads(raw) if isinstance(raw, str) else []
    if same_origin_only:
        origin_raw = await tab.evaluate("location.origin")
        origin = origin_raw if isinstance(origin_raw, str) else ""
        links = [l for l in links if l["url"].startswith(origin)]
    return links


async def grep_text(tab: Any, pattern: str, *, selector: str = "body",
                    max_matches: int = 30, context_chars: int = 60) -> list[dict[str, Any]]:
    """Regex-search the page text. Returns list of {match, context, line_no}."""
    raw = await tab.evaluate(f"document.querySelector({selector!r})?.innerText || ''")
    if not isinstance(raw, str):
        return []
    try:
        rx = re.compile(pattern, re.MULTILINE | re.IGNORECASE)
    except re.error as e:
        return [{"error": f"bad regex: {e}"}]
    out: list[dict[str, Any]] = []
    for m in rx.finditer(raw):
        if len(out) >= max_matches:
            break
        start = max(0, m.start() - context_chars)
        end = min(len(raw), m.end() + context_chars)
        ctx = raw[start:end].replace("\n", " ")
        line_no = raw[:m.start()].count("\n") + 1
        out.append({
            "match": m.group(0)[:200],
            "context": ctx,
            "line": line_no,
        })
    return out


# Shared JS helper — recursively pierce open shadow DOM and same-origin iframes
# while walking. Closed shadow roots and cross-origin iframes are unreachable
# from page JS (only CDP DOM.getDocument+pierce can; we document this).
_PIERCE_JS = r"""
function __umbraPierce(root, sel, limit) {
    const out = [];
    const visited = new WeakSet();
    function walk(node) {
        if (out.length >= limit) return;
        if (!node || visited.has(node)) return;
        visited.add(node);
        if (node.nodeType === 1) {
            try { if (node.matches && node.matches(sel)) out.push(node); } catch(e) {}
            // Open shadow root
            if (node.shadowRoot) walk(node.shadowRoot);
            // Same-origin iframe
            if (node.tagName === 'IFRAME') {
                try {
                    const doc = node.contentDocument;
                    if (doc) walk(doc);
                } catch(e) { /* cross-origin: skip */ }
            }
        }
        const kids = node.children || (node.childNodes ? Array.from(node.childNodes).filter(n => n.nodeType === 1) : []);
        for (let i = 0; i < kids.length; i++) walk(kids[i]);
    }
    walk(root);
    return out;
}
"""


async def dom_query(tab: Any, selector: str, *, max_results: int = 30,
                     pierce: bool = True) -> list[dict[str, Any]]:
    """querySelectorAll → list of element details. Like devtools $$().

    pierce=True (default) walks into open shadow roots + same-origin iframes
    so component libraries (Lit, Stencil) and same-origin embeds are visible.
    Closed shadow roots and cross-origin iframes remain unreachable
    (browser security; only solvable via CDP-level pierce in future)."""
    import json as _j
    if pierce:
        js = _PIERCE_JS + """JSON.stringify(__umbraPierce(document, %s, %d).map(el => {
            const r = el.getBoundingClientRect();
            return {
                tag: el.tagName.toLowerCase(),
                id: el.id || null,
                cls: el.className && typeof el.className === 'string' ? el.className.substring(0, 80) : null,
                text: (el.innerText || '').trim().substring(0, 100),
                value: el.value !== undefined ? String(el.value).substring(0, 80) : null,
                href: el.href || null,
                in_shadow: !!el.getRootNode().host,
                visible: el.offsetParent !== null,
                rect: r.width > 0 ? {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)} : null,
            };
        }))""" % (repr(selector), max_results)
    else:
        js = """JSON.stringify(
            Array.from(document.querySelectorAll(%s)).slice(0, %d).map(el => {
                const r = el.getBoundingClientRect();
                return {
                    tag: el.tagName.toLowerCase(),
                    id: el.id || null,
                    cls: el.className && typeof el.className === 'string' ? el.className.substring(0, 80) : null,
                    text: (el.innerText || '').trim().substring(0, 100),
                    value: el.value !== undefined ? String(el.value).substring(0, 80) : null,
                    href: el.href || null,
                    visible: el.offsetParent !== null,
                    rect: r.width > 0 ? {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)} : null,
                };
            })
        )""" % (repr(selector), max_results)
    raw = await tab.evaluate(js)
    return _j.loads(raw) if isinstance(raw, str) else []


async def extract_text_pierced(tab: Any, *, selector: str = "body",
                                 max_chars: int = 5000) -> str:
    """innerText of selector PIERCED through shadow + same-origin iframes.

    For body: returns top-level body text + all open-shadow + same-origin
    iframe text concatenated with separators. For specific selector: piercing
    finds it across shadow boundaries too."""
    js = _PIERCE_JS + """(() => {
        const els = __umbraPierce(document, %s, 1);
        if (els.length === 0) return '';
        const root = els[0];
        const parts = [root.innerText || ''];
        // Walk root's shadow + iframe descendants for additional text
        function gatherSubtree(node) {
            if (!node) return;
            if (node.nodeType === 1) {
                if (node.shadowRoot) parts.push(node.shadowRoot.textContent || '');
                if (node.tagName === 'IFRAME') {
                    try {
                        const doc = node.contentDocument;
                        if (doc) parts.push(doc.body?.innerText || '');
                    } catch(e) {}
                }
            }
            for (const c of (node.children || [])) gatherSubtree(c);
        }
        gatherSubtree(root);
        return parts.filter(Boolean).join('\\n---\\n');
    })()""" % repr(selector)
    raw = await tab.evaluate(js)
    if not isinstance(raw, str):
        return ""
    if len(raw) > max_chars:
        return raw[:max_chars] + f"...[+{len(raw) - max_chars}c]"
    return raw


async def inspect_element(tab: Any, selector: str) -> dict[str, Any]:
    """Full attribute dump of one element. Like devtools' Elements panel."""
    import json as _j
    js = """JSON.stringify((() => {
        const el = document.querySelector(%s);
        if (!el) return null;
        const attrs = {};
        for (const a of el.attributes) attrs[a.name] = a.value;
        const cs = window.getComputedStyle(el);
        const r = el.getBoundingClientRect();
        return {
            tag: el.tagName.toLowerCase(),
            attrs,
            text: (el.innerText || '').substring(0, 500),
            html: el.outerHTML.substring(0, 1500),
            rect: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)},
            visible: el.offsetParent !== null,
            computed: {
                display: cs.display,
                visibility: cs.visibility,
                color: cs.color,
                bg: cs.backgroundColor,
                font: cs.font,
                position: cs.position,
                zIndex: cs.zIndex,
            },
        };
    })())""" % repr(selector)
    raw = await tab.evaluate(js)
    return _j.loads(raw) if isinstance(raw, str) else None


async def inject_css(tab: Any, css: str) -> None:
    """Inject a <style> block. Persists for the page lifetime."""
    await tab.evaluate(f"""(() => {{
        const s = document.createElement('style');
        s.textContent = {css!r};
        document.head.appendChild(s);
    }})()""")


def _compact_markdown(md: str) -> str:
    """Lossless markdown squeeze. Goals:
    - Strip zero-width / BOM / soft-hyphen / control chars (invisible bytes)
    - Collapse 3+ newlines → 2 (max one blank line between blocks)
    - Strip per-line trailing whitespace
    - Remove empty markdown links: [text]() → text, [](url) → ''
    - Remove HTML comments (<!-- ... -->)
    - Strip leading/trailing whitespace from doc
    Preserves: code blocks, tables, list structure, links, images, headings.
    """
    import re as _re
    # 1. Invisible chars
    md = _re.sub(r"[​‌‍⁠­﻿]", "", md)
    # 2. HTML comments (markdownify can leak these from source)
    md = _re.sub(r"<!--.*?-->", "", md, flags=_re.DOTALL)
    # 3. Empty links
    md = _re.sub(r"\[([^\]]+)\]\(\)", r"\1", md)        # [text]()  → text
    md = _re.sub(r"\[\s*\]\([^)]+\)", "", md)           # [](url)   → drop
    md = _re.sub(r"\[\s*\]\[\s*[^\]]*\]", "", md)       # [][ref]   → drop
    # 4. Per-line trailing whitespace
    md = _re.sub(r"[ \t]+$", "", md, flags=_re.MULTILINE)
    # 5. 3+ newlines → 2
    md = _re.sub(r"\n{3,}", "\n\n", md)
    # 6. Tabs at start of non-code lines → 2 spaces (markdownify quirk)
    md = _re.sub(r"^\t+", lambda m: "  " * len(m.group()), md, flags=_re.MULTILINE)
    return md.strip()


async def extract_markdown(tab: Any, *, selector: str | None = None,
                            content_only: bool = True,
                            include_links: bool = True,
                            max_chars: int = 20000) -> dict[str, Any]:
    """Convert page HTML → clean Markdown (firecrawl-style).

    Two stages:
      1. Extract main content via Mozilla Readability heuristics (skip nav/ads/footer)
         — only when `content_only=True` and `selector` is None.
      2. Convert HTML → Markdown via `markdownify` (well-maintained, handles
         tables, code blocks, lists, links, images).

    `selector`: convert just that subtree (overrides content_only).
    `include_links`: keep `[text](url)` (default) or strip to plain text.

    Returns {markdown, title, byline?, content_html_len}.
    Requires `pip install umbra-browser[markdown]` (markdownify, readability-lxml)."""
    try:
        from markdownify import markdownify as _md
    except ImportError:
        return {"error": "markdownify not installed",
                "install_hint": "pip install markdownify  (or: pip install umbra-browser[markdown])"}

    # Get the relevant HTML
    if selector:
        html = await tab.evaluate(
            f"document.querySelector({selector!r})?.outerHTML || ''"
        )
        title = await tab.evaluate("document.title")
        byline = None
    elif content_only:
        try:
            from readability import Document
        except ImportError:
            return {"error": "readability-lxml not installed",
                    "install_hint": "pip install readability-lxml  (or: pip install umbra-browser[markdown])"}
        full_html = await tab.evaluate("document.documentElement.outerHTML")
        doc = Document(full_html or "")
        html = doc.summary()
        title = doc.title()
        byline = doc.short_title() if hasattr(doc, "short_title") else None
        # Fallback: readability gives up on list-pages (HN, reddit, search results)
        # — returns ~nothing. Drop to whole-body if readability output is too small.
        if not html or len(html) < 200:
            html = await tab.evaluate("document.body.outerHTML")
            content_only = False  # for the response metadata
    else:
        html = await tab.evaluate("document.body.outerHTML")
        title = await tab.evaluate("document.title")
        byline = None

    if not html:
        return {"error": "no content"}

    md_kwargs: dict[str, Any] = {
        "heading_style": "ATX",       # # H1 instead of ===
        "bullets": "-",                # consistent bullet
        "code_language": "",           # don't guess
        "strip": [] if include_links else ["a"],
    }
    md = _md(html, **md_kwargs)
    md = _compact_markdown(md)

    if len(md) > max_chars:
        md = md[:max_chars] + f"\n\n...[+{len(md) - max_chars}c, raise max_chars]"

    return {
        "markdown": md,
        "title": title,
        "byline": byline,
        "source_html_len": len(html),
    }


async def clone_element(tab: Any, selector: str) -> dict[str, Any]:
    """Approximate-pixel clone — DOM subtree + computed styles + referenced assets.

    Captures: outerHTML, computed CSS for the element + every descendant,
    extracted url(...) refs from styles. Builds a self-contained `doc` string
    that can be rendered standalone (assets remain as remote URLs unless caller
    inlines them).

    NOT pixel-perfect: skips ::before/::after pseudo styles, doesn't resolve
    CSS variables, doesn't inline assets. Good enough for component lifting,
    bug repro, visual snapshots. ~80% fidelity for ~5% of the implementation
    cost of a true clone."""
    import json as _j
    js = f"""JSON.stringify((() => {{
        const root = document.querySelector({_j.dumps(selector)});
        if (!root) return null;

        const styleRules = [];
        const assets = new Set();
        let counter = 0;

        function visit(el) {{
            const uid = 'u' + (counter++);
            el.setAttribute('data-umbra-c', uid);
            const cs = window.getComputedStyle(el);
            const props = [];
            for (let k = 0; k < cs.length; k++) {{
                const p = cs[k];
                const v = cs.getPropertyValue(p);
                // Heuristic: skip values that are likely default for this prop
                // (saves ~70% of style bytes vs dumping every property).
                if (!v) continue;
                if (v === 'normal' || v === 'none' || v === 'auto') continue;
                if (v === '0px' && !p.startsWith('border')) continue;
                if (v === 'rgba(0, 0, 0, 0)' && p.includes('background')) continue;
                props.push(p + ':' + v);
                // URL extraction
                const m = v.matchAll(/url\\(['"]?([^'")]+)['"]?\\)/g);
                for (const match of m) assets.add(match[1]);
            }}
            if (props.length) {{
                styleRules.push('[data-umbra-c="' + uid + '"]{{' + props.join(';') + '}}');
            }}
            for (const c of el.children) visit(c);
        }}
        visit(root);
        const html = root.outerHTML;
        // Strip the temp data attributes from the live DOM so the page is
        // unchanged after clone.
        document.querySelectorAll('[data-umbra-c]').forEach(e => e.removeAttribute('data-umbra-c'));

        return {{
            tag: root.tagName.toLowerCase(),
            html: html,
            css: styleRules.join('\\n'),
            assets: [...assets],
            element_count: counter,
        }};
    }})())"""
    raw = await tab.evaluate(js)
    if not raw:
        return {"error": "selector not found"}
    result = _j.loads(raw) if isinstance(raw, str) else raw
    if not result:
        return {"error": "selector not found"}
    doc = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<style>'
        '* { box-sizing: border-box; }\n'
        + result["css"]
        + '</style></head><body>' + result["html"] + '</body></html>'
    )
    return {
        "tag": result.get("tag"),
        "html": result.get("html"),
        "css": result.get("css"),
        "assets": result.get("assets", []),
        "element_count": result.get("element_count"),
        "doc": doc,
    }


# ────────────────────────────── screenshots ───────────────────────────────

async def screenshot(tab: Any, *, fmt: str = "jpeg", quality: int = 70,
                     full_page: bool = False) -> str:
    """Capture page → base64-encoded image string."""
    kwargs: dict[str, Any] = {"format_": fmt}
    if fmt == "jpeg":
        kwargs["quality"] = quality
    if full_page:
        kwargs["capture_beyond_viewport"] = True
    shot = await tab.send(cdp.page.capture_screenshot(**kwargs))
    if isinstance(shot, bytes):
        shot = base64.b64encode(shot).decode("ascii")
    return shot


async def screenshot_region(tab: Any, x: float, y: float, w: float, h: float,
                            *, fmt: str = "jpeg", quality: int = 80) -> str:
    """Capture only a rectangular region. Use for captcha tiles."""
    kwargs: dict[str, Any] = {
        "format_": fmt,
        "clip": cdp.page.Viewport(x=x, y=y, width=w, height=h, scale=1),
    }
    if fmt == "jpeg":
        kwargs["quality"] = quality
    shot = await tab.send(cdp.page.capture_screenshot(**kwargs))
    if isinstance(shot, bytes):
        shot = base64.b64encode(shot).decode("ascii")
    return shot


# ───────────────────────── console / network buffers ──────────────────────
# These read from deques populated by handlers in browser.py:_configure_tab.
# If the tab wasn't configured (raw nodriver tab), returns empty.

def get_console_logs(tab: Any, *, max_n: int = 50) -> list[dict[str, Any]]:
    buf: Any = getattr(tab, "_umbra_logs", None)
    if buf is None:
        return []
    return list(buf)[-max_n:]


def get_network_requests(tab: Any, *, max_n: int = 50) -> list[dict[str, Any]]:
    buf: Any = getattr(tab, "_umbra_requests", None)
    if buf is None:
        return []
    return list(buf)[-max_n:]


def clear_buffers(tab: Any) -> None:
    if hasattr(tab, "_umbra_logs"):
        tab._umbra_logs.clear()
    if hasattr(tab, "_umbra_requests"):
        tab._umbra_requests.clear()
