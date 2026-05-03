"""Session warming — make the browser look like a returning visitor.

Most anti-bot systems score sessions on cookie age, browsing history, and
referer chain. A brand-new browser hitting a sensitive endpoint cold is
~2x more suspicious than the same browser arriving via Google after first
poking around the surrounding web.

Usage:

    from umbra import StealthBrowser
    from umbra.warming import warm_session

    async with StealthBrowser() as b:
        await warm_session(b, profile="general")  # 30-90s of plausible browsing
        # Now the browser has cookies, history, referer chain → less suspicious
        tab = await b.new_tab("https://target-site.com")

Profiles are simple — visit a list of plausible sites, accept cookies if
prompted, scroll a bit, dwell, move on. The point isn't to fool anyone with
a perfect human simulation; it's to look like ANY normal returning user
rather than a fresh sandboxed Chrome. That alone moves the needle on score-
based detectors (CF, DataDome, PerimeterX).

Profile selection: pick something semantically related to your target. If
you're scraping shopping sites, warm via google + reddit + a major retailer.
For news scraping, warm via google + wikipedia + bbc. The cookies + referer
graph build a plausible "user persona" the target site can fingerprint
positively.
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

log = logging.getLogger("umbra.warming")


@dataclass
class WarmingProfile:
    """A plausible browsing pattern."""
    name: str
    sites: tuple[str, ...]
    dwell_range: tuple[float, float] = (4.0, 12.0)
    scroll_count_range: tuple[int, int] = (1, 4)


PROFILES: dict[str, WarmingProfile] = {
    "general": WarmingProfile(
        name="general",
        sites=(
            "https://www.google.com",
            "https://en.wikipedia.org/wiki/Special:Random",
            "https://news.ycombinator.com",
            "https://www.reddit.com/r/popular",
        ),
    ),
    "shopping": WarmingProfile(
        name="shopping",
        sites=(
            "https://www.google.com/search?q=best+headphones+2025",
            "https://www.amazon.com",
            "https://www.reddit.com/r/headphones",
            "https://www.bestbuy.com",
        ),
        dwell_range=(6.0, 14.0),
    ),
    "news": WarmingProfile(
        name="news",
        sites=(
            "https://www.google.com/search?q=latest+news",
            "https://news.google.com",
            "https://www.bbc.com",
            "https://en.wikipedia.org/wiki/Special:Random",
        ),
    ),
    "minimal": WarmingProfile(
        name="minimal",
        sites=("https://www.google.com",),
        dwell_range=(2.0, 4.0),
        scroll_count_range=(0, 1),
    ),
}


async def warm_session(browser, profile: str = "general", *, max_sites: int | None = None) -> None:
    """Warm the browser by visiting plausible sites with realistic dwell.

    Reuses a single tab (real users don't open 5 tabs cold). Cookies and
    history accumulate on the underlying user_data_dir if one was supplied
    to StealthBrowser; otherwise they live in the per-process temp profile.
    """
    prof = PROFILES.get(profile)
    if not prof:
        log.warning("unknown warming profile %r — skipping", profile)
        return

    sites = list(prof.sites)
    if max_sites:
        sites = sites[:max_sites]
    random.shuffle(sites)

    log.info("warming session via %s profile (%d sites)", profile, len(sites))
    tab = await browser.new_tab(sites[0])
    for url in sites[1:]:
        # Dwell on current page — reading time
        await asyncio.sleep(random.uniform(*prof.dwell_range))
        # A few scrolls — looks like reading
        for _ in range(random.randint(*prof.scroll_count_range)):
            try:
                await tab.evaluate(
                    f"window.scrollBy({{ top: {random.randint(300, 900)}, behavior: 'smooth' }})"
                )
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(random.uniform(0.6, 2.2))
        # Navigate to next
        try:
            await tab.get(url)
        except Exception as e:  # noqa: BLE001
            log.debug("warming nav failed for %s: %s", url, e)
            continue
    # Final dwell on last site
    await asyncio.sleep(random.uniform(*prof.dwell_range))
    log.info("warming complete")
    return
