"""CDP Fetch.authRequired handler — answers proxy auth challenges so we don't
have to bake creds into Chrome's --proxy-server flag (Chrome silently strips
inline auth from that flag).

Install once per tab. The handler stays alive for the tab's lifetime and
answers ALL proxy auth challenges with the supplied creds.
"""
from __future__ import annotations

import logging
from typing import Any

import nodriver as uc
from nodriver import cdp as _cdp

log = logging.getLogger(__name__)


async def install_proxy_auth(tab: Any, username: str, password: str) -> None:
    """Wire CDP Fetch domain to auto-respond to proxy auth challenges.

    Idempotent: calling twice on the same tab replaces the prior handler.
    Safe to call on tabs that will never see proxy auth (no-op cost).
    """

    async def _on_auth(event: _cdp.fetch.AuthRequired) -> None:
        log.info("auth challenge: %s", event.auth_challenge.origin if hasattr(event, "auth_challenge") else "?")
        try:
            await tab.send(_cdp.fetch.continue_with_auth(
                request_id=event.request_id,
                auth_challenge_response=_cdp.fetch.AuthChallengeResponse(
                    response="ProvideCredentials",
                    username=username,
                    password=password,
                ),
            ))
        except Exception:  # noqa: BLE001
            log.exception("proxy auth response failed")

    async def _on_paused(event: _cdp.fetch.RequestPaused) -> None:
        # When patterns isn't set, ALL requests pause. Continue them so the
        # tab doesn't hang waiting on us. Auth challenges flow into _on_auth
        # separately.
        try:
            await tab.send(_cdp.fetch.continue_request(request_id=event.request_id))
        except Exception:  # noqa: BLE001
            pass

    tab.add_handler(_cdp.fetch.AuthRequired, _on_auth)
    tab.add_handler(_cdp.fetch.RequestPaused, _on_paused)
    # patterns=None → all requests pause; we continue them in _on_paused.
    # Auth challenges still fire AuthRequired before the request is unpaused.
    await tab.send(_cdp.fetch.enable(handle_auth_requests=True))
    log.debug("proxy auth installed (user=%s***)", username[:2] if username else "")
