"""Stealth Chrome launcher.

Wires the umbra stealth pipeline onto a nodriver-managed real Chrome process:

    Chrome flags  →  nodriver/uc patches the binary tells (cdc_, navigator.webdriver)
                  →  umbra stealth payload (Page.addScriptToEvaluateOnNewDocument)
                  →  CDP timezone / locale / UA-client-hints override
                  →  Fetch.enable + tracker blocklist

Why this stack and not just a stealth library: we need every layer because
detectors stack their checks. Chrome flags fix process-level tells, nodriver
fixes the bigger automation properties, our payload fixes the per-page
fingerprint surfaces (canvas/audio/WebGL noise, isTrusted, ICE filter), and
the network layer kills the trackers that would otherwise re-fingerprint.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import subprocess
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import nodriver as uc

from umbra.stealth.blocklist import is_blocked
from umbra.stealth.inject import install as install_stealth

log = logging.getLogger("umbra.browser")


# Silence cosmetic CDP-schema-lag warnings from nodriver.
# Chrome 146+ added new fields (privateNetworkRequestPolicy, etc.) to events
# nodriver hasn't updated its parser for. The events still arrive and parse
# correctly except for those new fields — nodriver logs a KeyError per event
# but no functionality breaks. Suppress at the source so the log stays
# readable. If you hit a real CDP issue, remove this filter to debug.
class _NodriverSchemaFilter(logging.Filter):
    _NOISY_KEYS = ("privateNetworkRequestPolicy",)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        return not any(k in msg for k in self._NOISY_KEYS)


_nodriver_log = logging.getLogger("nodriver.core.connection")
_nodriver_log.addFilter(_NodriverSchemaFilter())


def _detect_chrome_version(chrome_path: str | None) -> str:
    """Run `chrome --version` to get the actual installed Chromium version.

    Returns a string like '146.0.7339.16'. Falls back to '146.0.7339.16' if
    detection fails — keeps us on a recent stable that won't trip
    version-too-old heuristics. We MUST match the version we claim in our
    UA + UA-CH headers to whatever the underlying Chromium ships, otherwise
    detectors compare e.g. UA=146 vs an internal API surface that screams
    "this is actually 145".
    """
    fallback = "146.0.7339.16"
    candidates = [chrome_path] if chrome_path else [
        "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
        "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]
    for c in candidates:
        if not c:
            continue
        try:
            out = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=3)
            m = re.search(r"(\d+)\.(\d+)\.(\d+)\.(\d+)", out.stdout)
            if m:
                return ".".join(m.groups())
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            continue
    log.warning("could not detect Chrome version, falling back to %s", fallback)
    return fallback


def _build_ua(version: str) -> str:
    """Linux Chrome UA matching the detected version. No 'HeadlessChrome'."""
    return (
        f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{version} Safari/537.36"
    )


def _ua_metadata(version: str) -> dict[str, Any]:
    """Build UA-Client-Hints metadata matching the version.

    Sent via CDP `Network.setUserAgentOverride` so getHighEntropyValues()
    returns values consistent with the UA string. Mismatch here is the
    single biggest cross-check detectors run.
    """
    major = version.split(".")[0]
    return {
        "brands": [
            {"brand": "Google Chrome", "version": major},
            {"brand": "Chromium", "version": major},
            {"brand": "Not.A/Brand", "version": "24"},
        ],
        "fullVersionList": [
            {"brand": "Google Chrome", "version": version},
            {"brand": "Chromium", "version": version},
            {"brand": "Not.A/Brand", "version": "24.0.0.0"},
        ],
        "fullVersion": version,
        "platform": "Linux",
        "platformVersion": "6.8.0",
        "architecture": "x86",
        "model": "",
        "mobile": False,
        "bitness": "64",
        "wow64": False,
    }


# Realistic Chrome flag set — every flag here either fixes an automation tell
# OR is a flag a real Chrome install would have. NOT included on purpose:
#   --headless / --headless=new : `headless=new` still has detectable tells in
#       Chrome 120+ (see github.com/berstend/puppeteer-extra/issues/740). Run
#       under Xvfb in containers instead.
#   --disable-gpu : real Chrome has a GPU process. Absence is a tell.
#   --no-sandbox  : added conditionally only when running as root/in Docker.
#   --single-process : real Chrome is multi-process. Major tell.
#   --mute-audio : real Chrome doesn't mute itself.

_BASE_FLAGS: tuple[str, ...] = (
    # The single most important automation-tell fix. Drops the cdc_ markers
    # Chromium adds when launched under DevTools control.
    "--disable-blink-features=AutomationControlled",
    # First-run wizards & defaults UI — never appear on a real used profile.
    "--no-first-run",
    "--no-default-browser-check",
    "--no-service-autorun",
    "--disable-default-apps",
    # Avoid OS-level credential prompts that would freeze the process.
    "--password-store=basic",
    "--use-mock-keychain",
    # Disable features that send telemetry or change behavior in ways
    # detectable to fingerprinting scripts. Big union of what's safe to
    # disable without changing user-visible behavior.
    "--disable-features="
    "Translate,OptimizationHints,MediaRouter,DialMediaRouteProvider,"
    "CalculateNativeWinOcclusion,InterestFeedContentSuggestions,"
    "CertificateTransparencyComponentUpdater,AutofillServerCommunication,"
    "PrivacySandboxSettings4,ChromeWhatsNewUI,SidePanelPinning",
    # Keep network in-process to avoid an extra service worker that some
    # detectors enumerate. Same behavior real Chrome has by default.
    "--enable-features=NetworkService,NetworkServiceInProcess",
    # Behavioral: don't throttle backgrounded tabs (we automate them).
    "--disable-ipc-flooding-protection",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--disable-background-timer-throttling",
    # Color profile consistency — varies per OS, force srgb to avoid leaks.
    "--force-color-profile=srgb",
    # Metrics: opt out without disabling the subsystem (which itself is a tell).
    "--metrics-recording-only",
)

# Headless mode that uses REAL GPU via ANGLE/Vulkan — no SwiftShader fallback.
# Adding `--headless=new` (Chrome 109+) plus the ANGLE backend forces Chrome to
# open offscreen contexts on the actual GPU device. Result: WebGL UNMASKED_*
# returns the host GPU (not "SwiftShader Device"), eliminating the only
# headless tell sannysoft caught us on. Auto-applied when headless=True.
_HEADLESS_GPU_FLAGS: tuple[str, ...] = (
    "--headless=new",
    "--use-gl=angle",
    "--use-angle=vulkan",
    # Fallback for systems without Vulkan: ANGLE auto-degrades to GL
    "--enable-features=VulkanFromANGLE",
)

# Memory-conscious flag set — opt-in via `low_memory=True`. Cuts ~80-150MB of
# resident RAM by disabling features whose absence isn't detectable. Trade-off:
# slightly slower page loads (back/forward cache off, prefetch off). Stealth
# impact: zero — these features being off is plausible for a low-end laptop.
# Things we deliberately do NOT disable here because they ARE detectable:
#   --single-process / --renderer-process-limit=1  → real Chrome is multi-process
#   --js-flags=--max-old-space-size=64             → performance.memory leaks it
#   --disable-gpu                                  → real Chrome has GPU process
_LOW_MEMORY_FLAGS: tuple[str, ...] = (
    "--disable-features="
    "BackForwardCache,InterestFeedV2,GlobalMediaControls,InterestFeedContentSuggestions,"
    "MediaRouter,DialMediaRouteProvider,Translate,OptimizationHints,"
    "WebUITabStrip,SidePanelPinning,ChromeWhatsNewUI",
    # Realistic V8 heap cap (low-end laptop). 512MB is plausible — performance.memory
    # reports it but a "real low-end Chrome" would too. Below ~256MB starts looking
    # constrained; 512 is the sweet spot.
    "--js-flags=--max-old-space-size=512",
    # Kill BFC explicitly (some Chrome versions ignore the disable-features list
    # for this one). 50-100MB savings on tabs that navigate.
    "--disable-back-forward-cache",
    # No prefetch / preload — fewer speculative network requests, less RAM.
    "--disable-features=NetworkPrediction",
    # Disable the partition allocator overhead path (small win).
    "--disable-features=ScrollbarColor,PartitionAllocLargeEmptySlotSpanRing",
)


@dataclass
class StealthOptions:
    """Configuration for a stealth Chrome session."""

    # Identity
    user_agent: str | None = None  # None = let nodriver pick the matching real Chrome UA
    timezone: str | None = None    # IANA, e.g. "America/New_York"
    locale: str = "en-US"
    accept_languages: str = "en-US,en;q=0.9"

    # Network
    proxy: str | None = None       # e.g. "http://user:pass@host:port" or "socks5://..."
    block_trackers: bool = True
    block_resources: tuple[str, ...] = ()  # e.g. ("Image", "Media", "Font") for fast scraping

    # Stealth payload mode:
    #   "minimal" (default) → only fix automation tells, mimic vanilla Chrome.
    #     Best for production bot evasion (CF, DataDome, creepjs ~0%/0%).
    #   "full" → minimal + canvas/audio/WebGL/font/WebRTC per-session noise.
    #     Best for anti-tracking / privacy where uniqueness matters more than
    #     looking-like-vanilla-Chrome.
    stealth_mode: str = "minimal"

    # Process
    headless: bool = False          # default False — most stealth assumes a display
    user_data_dir: str | None = None
    chrome_path: str | None = None  # nodriver auto-detects if None
    extra_args: list[str] = field(default_factory=list)
    window_size: tuple[int, int] = (1920, 1080)

    # Performance / footprint
    #   low_memory=True: cut ~80-150MB resident RAM via plausible feature
    #     disables. Stealth-neutral. Use for parallel session farms or
    #     resource-constrained hosts.
    low_memory: bool = False
    #   ld_preload_shim: path to libumbra_shim.so (built via `make` in
    #     umbra/native/). When set, applied as LD_PRELOAD on the Chrome
    #     subprocess to block dlopen of unused libs (libcups, libsmbclient,
    #     etc) and intercept getenv probes that leak headless presence.
    ld_preload_shim: str | None = None


class StealthBrowser:
    """One Chrome process, one stealth pipeline."""

    def __init__(self, options: StealthOptions | None = None):
        self.options = options or StealthOptions()
        self._browser: uc.Browser | None = None
        self._tabs: list[Any] = []
        self._chrome_version: str | None = None
        self._ua: str | None = None
        self._ua_meta: dict[str, Any] | None = None

    @property
    def browser(self) -> uc.Browser:
        if self._browser is None:
            raise RuntimeError("StealthBrowser not started — call await start() first")
        return self._browser

    async def start(self) -> uc.Browser:
        opts = self.options
        flags: list[str] = list(_BASE_FLAGS) + list(opts.extra_args)

        # Headless mode: force --headless=new with ANGLE/Vulkan so the GPU
        # process uses the host's REAL GPU (not SwiftShader). Eliminates the
        # WebGL Renderer headless tell that sannysoft and creepjs catch.
        if opts.headless:
            flags.extend(_HEADLESS_GPU_FLAGS)

        # Memory-conscious flags (opt-in).
        if opts.low_memory:
            flags.extend(_LOW_MEMORY_FLAGS)

        # Detect actual Chrome version → build matching UA + UA-CH metadata.
        # We MUST pin all three (UA string, UA-CH brands, UA-CH fullVersionList)
        # to the same version, or detectors flag the inconsistency.
        self._chrome_version = _detect_chrome_version(opts.chrome_path)
        self._ua = opts.user_agent or _build_ua(self._chrome_version)
        self._ua_meta = _ua_metadata(self._chrome_version)
        flags.append(f"--user-agent={self._ua}")
        log.info("Chrome %s detected; UA pinned (low_memory=%s)", self._chrome_version, opts.low_memory)

        # LD_PRELOAD shim: block chrome from dlopen'ing libs it doesn't need
        # (libcups, libsmbclient, libsecret, libcanberra, libnotify, libpci...).
        # Cuts another 20-40MB resident RAM. Caller must point this at a built
        # libumbra_shim.so — see umbra/native/ for source + Makefile.
        #
        # Implementation: nodriver calls create_subprocess_exec without an
        # explicit env= parameter. asyncio inherits os.environ in that case
        # by default, BUT in practice LD_PRELOAD does not always propagate
        # to the chrome process tree (asyncio internals or chrome-sandbox
        # scrubbing). We monkey-patch the asyncio spawner to pass env
        # explicitly while a shim is active. Patch is restored on stop().
        if opts.ld_preload_shim:
            existing = os.environ.get("LD_PRELOAD", "")
            new_preload = f"{opts.ld_preload_shim}:{existing}".rstrip(":")
            os.environ["LD_PRELOAD"] = new_preload
            self._orig_subprocess_spawner = asyncio.create_subprocess_exec

            async def _spawn_with_env(*args: Any, **kwargs: Any) -> Any:
                kwargs.setdefault("env", os.environ.copy())
                return await self._orig_subprocess_spawner(*args, **kwargs)

            asyncio.create_subprocess_exec = _spawn_with_env  # type: ignore[assignment]
            log.info("LD_PRELOAD shim: %s", opts.ld_preload_shim)
        if opts.locale:
            flags.append(f"--lang={opts.locale}")
        if opts.proxy:
            flags.append(f"--proxy-server={opts.proxy}")
        if opts.window_size:
            w, h = opts.window_size
            flags.append(f"--window-size={w},{h}")

        # Container/root detection — sandboxing must be off in those envs.
        in_container = os.path.exists("/.dockerenv") or os.environ.get("UMBRA_CONTAINER")
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if in_container or is_root:
            flags.append("--no-sandbox")
            flags.append("--disable-dev-shm-usage")
            log.info("Container/root detected — sandbox disabled")

        config = uc.Config(
            headless=opts.headless,
            user_data_dir=opts.user_data_dir,
            sandbox=not (in_container or is_root),
            browser_executable_path=opts.chrome_path,
            browser_args=flags,
        )
        log.info("Launching Chrome (headless=%s, args=%d)", opts.headless, len(flags))
        self._browser = await uc.start(config=config)
        # Install stealth on the default tab opened at launch. Subsequent
        # navigations on this tab will re-run the payload (CDP guarantees that
        # for addScriptToEvaluateOnNewDocument).
        if self._browser.tabs:
            await self._configure_tab(self._browser.tabs[0])
        return self._browser

    async def new_tab(self, url: str = "about:blank") -> Any:
        """Open a new tab with the stealth pipeline installed BEFORE navigation.

        Flow: open at about:blank → install payload (registers preload hook) →
        navigate to target. The CDP `addScriptToEvaluateOnNewDocument` only
        fires on the NEXT navigation, so we always navigate after install
        (reloading about:blank if no other URL was requested) to guarantee
        the payload has actually run before the caller does anything.
        """
        tab = await self.browser.get("about:blank", new_tab=True)
        await self._configure_tab(tab)
        # Trigger the preload by (re)navigating. For a real target URL this is
        # a normal navigation; for about:blank we reload to fire the hook.
        if url and url != "about:blank":
            await tab.get(url)
        else:
            await tab.reload()
        self._tabs.append(tab)
        return tab

    async def _configure_tab(self, tab: Any) -> None:
        """Install stealth payload + apply CDP overrides on a tab."""
        opts = self.options
        await install_stealth(
            tab,
            timezone=opts.timezone,
            chrome_version=self._chrome_version,
            mode=opts.stealth_mode,  # type: ignore[arg-type]
        )

        cdp = uc.cdp

        # Network.setUserAgentOverride pins UA-CH values that getHighEntropyValues()
        # returns. Without this, the JS-side UA-CH may report a different version
        # from our --user-agent flag (Chrome computes UA-CH from internals, not
        # from the flag). Send before any navigation runs.
        if self._ua and self._ua_meta:
            with contextlib.suppress(Exception):
                await tab.send(cdp.network.set_user_agent_override(
                    user_agent=self._ua,
                    accept_language=opts.accept_languages,
                    user_agent_metadata=cdp.emulation.UserAgentMetadata(**self._ua_meta),
                ))

        if opts.timezone:
            with contextlib.suppress(Exception):
                await tab.send(cdp.emulation.set_timezone_override(timezone_id=opts.timezone))
        if opts.locale:
            with contextlib.suppress(Exception):
                await tab.send(cdp.emulation.set_locale_override(locale=opts.locale))
        if opts.accept_languages:
            with contextlib.suppress(Exception):
                await tab.send(cdp.network.set_extra_http_headers(
                    headers=cdp.network.Headers({"Accept-Language": opts.accept_languages})
                ))

        if opts.block_trackers or opts.block_resources:
            await self._wire_blocking(tab)

        # Console + network buffers — populated by event handlers, drained
        # via driver.utils.get_console_logs / get_network_requests.
        tab._umbra_logs = deque(maxlen=200)
        tab._umbra_requests = deque(maxlen=200)
        await self._wire_observers(tab)

    async def _wire_observers(self, tab: Any) -> None:
        """Register console + network event handlers that fill the buffers."""
        cdp = uc.cdp
        with contextlib.suppress(Exception):
            await tab.send(cdp.runtime.enable())
        with contextlib.suppress(Exception):
            await tab.send(cdp.network.enable())

        def on_console(event: Any) -> None:
            try:
                args = []
                for arg in getattr(event, "args", []) or []:
                    val = getattr(arg, "value", None)
                    if val is None:
                        val = getattr(arg, "description", "")
                    args.append(str(val)[:300])
                tab._umbra_logs.append({
                    "level": getattr(event, "type_", "log"),
                    "text": " ".join(args)[:500],
                    "ts": getattr(event, "timestamp", None),
                })
            except Exception:  # noqa: BLE001
                pass

        def on_request(event: Any) -> None:
            try:
                req = event.request
                tab._umbra_requests.append({
                    "id": str(getattr(event, "request_id", "")),
                    "url": req.url[:300],
                    "method": req.method,
                    "type": str(getattr(event, "type_", "")),
                    "ts": getattr(event, "timestamp", None),
                })
            except Exception:  # noqa: BLE001
                pass

        with contextlib.suppress(Exception):
            tab.add_handler(cdp.runtime.ConsoleAPICalled, on_console)
        with contextlib.suppress(Exception):
            tab.add_handler(cdp.network.RequestWillBeSent, on_request)

    async def _wire_blocking(self, tab: Any) -> None:
        """Enable Fetch.enable interception and drop tracker / blocked-type requests."""
        cdp = uc.cdp
        await tab.send(cdp.fetch.enable())

        block_types = {t.lower() for t in self.options.block_resources}
        check_trackers = self.options.block_trackers

        async def _on_request(event: Any) -> None:
            req = event.request
            url = req.url
            # event.resource_type is a CDP enum (not str); coerce safely.
            rt_obj = getattr(event, "resource_type", None)
            rtype = str(rt_obj).lower() if rt_obj else ""
            try:
                if check_trackers and is_blocked(url):
                    await tab.send(cdp.fetch.fail_request(
                        request_id=event.request_id, error_reason=cdp.network.ErrorReason.BLOCKED_BY_CLIENT
                    ))
                    return
                if rtype and rtype in block_types:
                    await tab.send(cdp.fetch.fail_request(
                        request_id=event.request_id, error_reason=cdp.network.ErrorReason.BLOCKED_BY_CLIENT
                    ))
                    return
                await tab.send(cdp.fetch.continue_request(request_id=event.request_id))
            except Exception as e:  # noqa: BLE001
                log.debug("fetch handler error: %s", e)

        tab.add_handler(cdp.fetch.RequestPaused, _on_request)

    async def stop(self) -> None:
        """Stop the browser process."""
        if self._browser is not None:
            with contextlib.suppress(Exception):
                self._browser.stop()
            self._browser = None
            self._tabs.clear()
        # Restore monkey-patched spawner so other code in this process gets
        # vanilla asyncio behavior again.
        if hasattr(self, "_orig_subprocess_spawner"):
            asyncio.create_subprocess_exec = self._orig_subprocess_spawner  # type: ignore[assignment]
            del self._orig_subprocess_spawner

    async def __aenter__(self) -> "StealthBrowser":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.stop()


@contextlib.asynccontextmanager
async def stealth_browser(**kwargs: Any) -> AsyncIterator[StealthBrowser]:
    """One-shot context-managed stealth Chrome.

    Example:
        async with stealth_browser(timezone="America/New_York") as b:
            tab = await b.new_tab("https://bot.sannysoft.com")
            # ... interact ...
    """
    opts = kwargs.pop("options", None) or StealthOptions(**kwargs)
    browser = StealthBrowser(opts)
    try:
        await browser.start()
        yield browser
    finally:
        await browser.stop()
