"""umbra — the most-stealth browser automation stack we could build.

Layered on real Chrome (via nodriver) so the TLS, microtask timing, and
HTTP/2 fingerprints match a real human's browser. Stealth additions:

  - Pre-document stealth payload patches the per-page surfaces (canvas,
    audio, WebGL, plugins, isTrusted, ICE, Intl) — Object.defineProperty
    only, never replaces a global. Source: this package's stealth/payload.js.
  - 3520-domain tracker blocklist (sourced from h4ckf0r0day/obscura).
  - ARIA-tree-first driver — interacts via the accessibility API, zero
    mouse coords for sites that hash behavioral telemetry.
  - CDP driver with bezier-trajectory mouse + log-normal keystroke jitter
    for cases where ARIA can't reach.

Quickstart:

    import asyncio
    from umbra import stealth_browser

    async def main():
        async with stealth_browser(timezone="America/New_York") as b:
            tab = await b.new_tab("https://bot.sannysoft.com")
            await asyncio.sleep(3)
            png = await tab.save_screenshot("sannysoft.png")

    asyncio.run(main())
"""

from umbra.browser import StealthBrowser, StealthOptions, stealth_browser
from umbra.driver import AriaDriver, CDPDriver
from umbra.warming import warm_session

__all__ = [
    "StealthBrowser",
    "StealthOptions",
    "stealth_browser",
    "AriaDriver",
    "CDPDriver",
    "warm_session",
]

# Lazy submodules — kept out of the eager import list because they pull
# heavy optional deps (cryptography for session, curl_cffi for tls).
# Use:  from umbra.session import Session
#       from umbra.tls import Session as TlsSession
#       from umbra.handoff import HandoffSession
#       from umbra.detection import full_check
__version__ = "0.1.0"
