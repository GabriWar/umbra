"""Stealth regression tests — runs against the public detector pages.

Catches drift if Chrome updates change a fingerprint or nodriver lags. Skipped
by default (each test boots Chrome, hits the network, takes 10-40s).

Run explicitly:
    pytest umbra/tests/test_stealth_regression.py -m e2e -v -s

Thresholds tuned 2026-05 against Chrome 146 + nodriver 0.48. If a check
regresses, that's a stealth-quality regression worth investigating.
"""

from __future__ import annotations

import asyncio
import os

import pytest

os.environ.setdefault("UMBRA_CONTAINER", "1")

from umbra import StealthOptions, stealth_browser  # noqa: E402

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_sannysoft_full_pass() -> None:
    """bot.sannysoft.com — must be 29+/31, only known-design failures allowed.

    Baseline expectations:
    - stock chromium + JS shim: 30+/31, 1 environmental fail max.
    - cloak chromium (default): 29/31 — WebGL Vendor/Renderer report
      "no webgl context" because cloak's C++ patch strips the
      uniquely-identifying GPU strings on purpose. This is an intentional
      surface cut, not a regression. Anything else failing IS a regression.
    """
    from umbra.detection import sannysoft_score
    async with stealth_browser(headless=True, low_memory=True) as b:
        s = await sannysoft_score(b)
        cloak_active = getattr(b, "_cloak_active", False)
    assert s["passed"] >= 29, f"sannysoft regressed: {s}"
    # Whitelist cloak's intentional WebGL strip; anything else = regression.
    CLOAK_EXPECTED = {"WebGL Vendor", "WebGL Renderer"}
    unexpected = [
        f for f in s["failures"]
        if not (cloak_active and f["name"] in CLOAK_EXPECTED)
    ]
    assert len(unexpected) <= 1, f"unexpected sannysoft failures: {unexpected}"


@pytest.mark.asyncio
async def test_creepjs_no_headless_or_stealth_detected() -> None:
    """creepjs detected_headless and stealth must stay at/near 0."""
    from umbra.detection import creepjs_score
    async with stealth_browser(headless=True, low_memory=True) as b:
        c = await creepjs_score(b, wait_s=30)
    detected_headless = c.get("detected_headless") or 0
    stealth = c.get("stealth") or 0
    # Real Chrome (no extensions) baseline: 0% / 0%. Allow up to 5% drift.
    assert detected_headless <= 5, f"creepjs headless % regressed: {c}"
    assert stealth <= 5, f"creepjs stealth % regressed: {c}"
    assert c.get("chromium") is True, f"creepjs lost chromium identity: {c}"


@pytest.mark.asyncio
async def test_chrome_version_pinned_to_ua_ch() -> None:
    """UA string version must match userAgentData major version (consistency)."""
    async with stealth_browser(headless=True) as b:
        tab = await b.new_tab("https://example.com")
        ua = await tab.evaluate("navigator.userAgent")
        major_in_ua = ua.split("Chrome/")[1].split(".")[0]
        uach_brands_json = await tab.evaluate(
            "JSON.stringify(navigator.userAgentData?.brands || [])"
        )
        import json as _j
        brands = _j.loads(uach_brands_json) if isinstance(uach_brands_json, str) else []
        chromium_version = next(
            (b["version"] for b in brands if b["brand"] == "Chromium"), None
        )
        assert chromium_version == major_in_ua, (
            f"UA-CH mismatch: UA={major_in_ua} vs UA-CH brands.Chromium={chromium_version}"
        )


@pytest.mark.asyncio
async def test_no_automation_tells() -> None:
    """`'webdriver' in window` and friends must be false (delete-only, not shadow)."""
    async with stealth_browser(headless=True) as b:
        tab = await b.new_tab("https://example.com")
        for tell in ("webdriver", "callPhantom", "_phantom", "phantom",
                      "_selenium", "callSelenium", "_Selenium_IDE_Recorder"):
            present = await tab.evaluate(f'"{tell}" in window')
            assert not present, f"window.{tell} present (defineProperty creates property — should delete only)"
