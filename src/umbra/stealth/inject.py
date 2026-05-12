"""Inject the stealth payload via Page.addScriptToEvaluateOnNewDocument.

Why `addScriptToEvaluateOnNewDocument` and not `Runtime.evaluate`:
  - Runs BEFORE any page script (Runtime.evaluate runs after page load).
  - Inherited by every frame and iframe automatically.
  - Persists across navigations until removed.
  - Source is not visible to the page (sites that read `document.scripts`
    don't see it).

This is the single most important fix on top of stealth-browser-mcp's stack —
sb-mcp uses Runtime.evaluate which (a) leaves a visible execution trace and
(b) races page scripts that probe immediately on load.
"""

from __future__ import annotations

from importlib import resources
from typing import Any, Literal

PayloadMode = Literal["minimal", "full"]


def load_payload(mode: PayloadMode = "minimal") -> str:
    """Read the bundled stealth payload JS.

    minimal (default): only fixes automation tells nodriver doesn't cover —
        delete cdc_/_phantom/_selenium, doc-element attr strip, optional Intl
        timezone consistency. Result: scores ~0%/0% on creepjs headless+stealth
        (matches vanilla Chrome with no extensions). Use for production stealth.

    full: minimal + per-session canvas/audio/WebGL/UA-CH/font/WebRTC
        randomization. Use for anti-tracking / privacy where uniqueness across
        sessions matters more than mimicking vanilla Chrome.
    """
    fname = "payload.js" if mode == "full" else "payload_minimal.js"
    return resources.files("umbra.stealth").joinpath(fname).read_text()


async def install(
    tab: Any,
    timezone: str | None = None,
    chrome_version: str | None = None,
    mode: PayloadMode = "minimal",
    ua_metadata: dict[str, Any] | None = None,
) -> str:
    """Install the stealth payload on a nodriver Tab.

    Returns the CDP-assigned script identifier (passable to removeScript).

    NB: `Page.enable()` must be called first — nodriver doesn't enable the
    Page domain by default, and `addScriptToEvaluateOnNewDocument` is silently
    a no-op without it (the script registers but never fires on navigation).
    Found the hard way; cost an hour. Don't remove.
    """
    payload = load_payload(mode)
    prefix_parts: list[str] = []
    if timezone:
        prefix_parts.append(f"window.__umbra_tz = {timezone!r};")
    if chrome_version:
        prefix_parts.append(f"window.__umbra_chrome_version = {chrome_version!r};")
    if ua_metadata:
        # Serialize as a JS object literal. json.dumps emits ECMA-valid
        # primitives (strings, numbers, booleans, arrays, objects), so the
        # value is a safe drop-in. Used by the payload to rebuild
        # navigator.userAgentData when cloak's C++ stub clobbers ours.
        import json as _json
        prefix_parts.append(f"window.__umbra_uach = {_json.dumps(ua_metadata)};")
    if prefix_parts:
        payload = "\n".join(prefix_parts) + "\n" + payload

    import nodriver
    cdp = nodriver.cdp  # type: ignore[attr-defined]
    await tab.send(cdp.page.enable())
    result = await tab.send(
        cdp.page.add_script_to_evaluate_on_new_document(source=payload)
    )
    return result  # type: ignore[no-any-return]


async def install_playwright(context: Any, timezone: str | None = None) -> None:
    """Install via Playwright's add_init_script (semantically identical)."""
    payload = load_payload()
    if timezone:
        payload = f"window.__umbra_tz = {timezone!r};\n" + payload
    await context.add_init_script(payload)
