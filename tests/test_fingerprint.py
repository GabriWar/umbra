"""Fingerprint regression tests — run umbra against the public detector sites.

Not a unit test suite — these are end-to-end smoke checks that boot a real
Chrome and visit the well-known fingerprinting test pages:

  - https://bot.sannysoft.com           (basic webdriver / chrome / plugins)
  - https://abrahamjuliot.github.io/creepjs (deep entropy + lie detection)
  - https://nowsecure.nl                (Cloudflare Turnstile passive)
  - https://browserleaks.com/javascript (JS API surface)

Each test scrapes the page's verdict and asserts a pass. Skipped by default
(real network + browser launch). Run explicitly with:

    pytest umbra/tests/test_fingerprint.py -m e2e -v -s
"""

from __future__ import annotations

import asyncio
import re

import pytest

from umbra import StealthOptions, stealth_browser

pytestmark = pytest.mark.e2e


@pytest.mark.asyncio
async def test_sannysoft_passes() -> None:
    """sannysoft.com runs ~30 checks and renders red/green per row."""
    async with stealth_browser(timezone="America/New_York") as b:
        tab = await b.new_tab("https://bot.sannysoft.com")
        await asyncio.sleep(5)  # let every check finish

        # Each detector row has class "passed" or "failed"
        failed = await tab.evaluate(
            "Array.from(document.querySelectorAll('tr.failed')).map(r => r.cells[0]?.textContent)"
        )
        # We tolerate up to 2 failures (some checks like "broken image dimensions"
        # are environment-dependent and not stealth-relevant).
        assert len(failed or []) <= 2, f"sannysoft failures: {failed}"


@pytest.mark.asyncio
async def test_creepjs_trust_score() -> None:
    """creepjs scores 0-100 based on lie detection + entropy. Real Chrome is ~75-95."""
    async with stealth_browser(timezone="America/New_York") as b:
        tab = await b.new_tab("https://abrahamjuliot.github.io/creepjs")
        await asyncio.sleep(15)  # creepjs is slow

        score_text = await tab.evaluate(
            "document.querySelector('.unblurred .strong')?.textContent || ''"
        )
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", score_text or "")
        score = float(match.group(1)) if match else 0.0
        # Bare-Chrome baseline is ~70%; with payload we want ≥60% (some lie
        # detection items inevitably flag any patched fn).
        assert score >= 60.0, f"creepjs trust score too low: {score} ({score_text!r})"


@pytest.mark.asyncio
async def test_nowsecure_clears_cloudflare() -> None:
    """nowsecure.nl gates on Cloudflare's passive challenge."""
    async with stealth_browser() as b:
        tab = await b.new_tab("https://nowsecure.nl")
        await asyncio.sleep(8)

        title = await tab.evaluate("document.title")
        # Real Chrome reaches the post-challenge page; bots stay on the
        # "Just a moment..." interstitial.
        assert "moment" not in (title or "").lower(), f"stuck on Cloudflare: {title!r}"


@pytest.mark.asyncio
async def test_payload_loaded_idempotently() -> None:
    """Payload should set window.__umbra_stealth_loaded once and only once."""
    async with stealth_browser() as b:
        tab = await b.new_tab("about:blank")
        loaded = await tab.evaluate("window.__umbra_stealth_loaded === true")
        assert loaded, "stealth payload not applied"
        # Force a reload — payload re-runs but guard prevents double-install.
        await tab.reload()
        await asyncio.sleep(0.5)
        seed = await tab.evaluate("window.__umbra_seed")
        assert isinstance(seed, int) and seed > 0, "fingerprint seed missing"
