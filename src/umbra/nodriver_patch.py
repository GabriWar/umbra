"""Monkey-patches for nodriver's CDP layer to survive Chrome schema drift.

Two root-cause fixes for bugs that brick the connection on Chrome 146+:

1. ``Connection._listener`` dies if ``Transaction.__call__`` raises while
   parsing a CDP response. The mapper has already popped the future, so the
   awaiter hangs forever AND every subsequent CDP call on that tab hangs
   because no listener is draining the websocket.

2. ``Cookie.from_json`` (Network domain) requires the ``sameParty`` field
   that Chrome 146+ no longer emits. KeyError → triggers (1) → tab is
   permanently broken after a single ``get_cookies()`` returns a non-empty
   list. Sister parser ``CookieParam.from_json`` already uses ``.get()`` —
   this is upstream inconsistency.

The patches are conservative — they preserve the original behavior on the
happy path and only add defensive shims for the failure modes above.

Resilience guarantees
---------------------
- **Idempotent**: ``apply()`` is safe to call any number of times. Each
  individual patch tags its target with ``_umbra_patched = True`` and
  refuses to wrap an already-wrapped function. The module-level ``_APPLIED``
  flag short-circuits repeat calls in normal use.
- **Partial-failure tolerant**: each patch runs in its own try/except. If
  one patch fails (e.g. nodriver internals shifted on a future release),
  the others still apply and umbra logs a warning instead of crashing.
- **Lazy + dependency-safe**: imports nodriver lazily inside ``apply()``
  so a missing nodriver doesn't break import-time consumers — they get a
  warning at apply time and the function returns cleanly.

Apply once at import time from ``umbra.browser`` (already wired).
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("umbra.nodriver_patch")

_APPLIED = False
_FLAG_TX = "_umbra_patched_tx_call"
_FLAG_LISTENER = "_umbra_patched_listener"
_FLAG_COOKIE = "_umbra_patched_cookie_from_json"


def apply() -> bool:
    """Apply nodriver patches. Idempotent + resilient.

    Returns True if everything was already applied or is now applied.
    Returns False if at least one patch failed (umbra still works — the
    failed patch's bug just isn't fixed). Never raises.
    """
    global _APPLIED
    if _APPLIED:
        return True

    try:
        import nodriver.core.connection as conn_mod
        from nodriver.cdp import network as cdp_network
    except ImportError:
        log.warning("nodriver not importable — patches skipped")
        return False

    results = {
        "transaction_call": _safe_apply(_patch_transaction_call, conn_mod),
        "listener": _safe_apply(_patch_listener, conn_mod),
        "cookie_from_json": _safe_apply(_patch_cookie_from_json, cdp_network),
    }
    all_ok = all(results.values())
    if all_ok:
        _APPLIED = True
        log.debug("nodriver patches applied: %s", results)
    else:
        # Don't set _APPLIED — leave room for a retry on next call.
        log.warning("some nodriver patches failed: %s", results)
    return all_ok


def _safe_apply(fn: Any, *args: Any) -> bool:
    """Run a single patch fn, swallow + log any failure. Returns True on success."""
    try:
        skipped = fn(*args)
        # patch fns return True if they successfully patched, False if already patched
        return True if skipped is None else bool(skipped)
    except BaseException as exc:  # noqa: BLE001 — we never want patches to crash umbra
        log.warning("patch %s failed: %s", fn.__name__, exc, exc_info=True)
        return False


def _patch_transaction_call(conn_mod: Any) -> bool:
    """Catch every parser exception and route it to the future.

    Original behavior: re-raises KeyError, lets anything else propagate.
    Either way the listener task crashes because callers don't wrap it.

    Patched: any exception while parsing the CDP response becomes
    ``future.set_exception(...)`` so the awaiter gets a real error instead
    of hanging, and the listener stays alive.
    """
    Transaction = conn_mod.Transaction
    if getattr(Transaction, _FLAG_TX, False):
        return True
    ProtocolException = conn_mod.ProtocolException

    def __call__(self: Any, **response: dict) -> None:
        if "error" in response:
            self.set_exception(ProtocolException(response["error"]))
            return
        try:
            self.__cdp_obj__.send(response.get("result", {}))
        except StopIteration as stop:
            if not self.done():
                self.set_result(stop.value)
        except BaseException as exc:  # noqa: BLE001 — must catch everything
            if not self.done():
                self.set_exception(exc)

    type.__setattr__(Transaction, "__call__", __call__)
    type.__setattr__(Transaction, _FLAG_TX, True)
    return True


def _patch_listener(conn_mod: Any) -> bool:
    """Wrap ``tx(**message)`` so a parser bug can't kill the listener.

    The original listener assumes ``tx(**message)`` never raises. Combined
    with ``self.mapper.pop()`` happening first, any raise nukes the
    listener task — every future CDP call on the connection hangs.

    Patched listener catches the raise, logs it, and marks the future
    failed if our ``Transaction.__call__`` patch didn't already.
    """
    Connection = conn_mod.Connection
    if getattr(Connection, _FLAG_LISTENER, False):
        return True

    import asyncio
    import json
    import websockets

    cdp_util = __import__("nodriver.cdp.util", fromlist=["parse_json_event"])

    async def _listener(self: Any) -> None:
        while True:
            try:
                async with self._lock:
                    raw = await asyncio.wait_for(self.websocket.recv(), 0.05)
            except conn_mod.ProtocolException:
                break
            except websockets.exceptions.ConnectionClosedOK:
                await self.disconnect()
                break
            except websockets.exceptions.ConnectionClosed:
                await self.disconnect()
                break
            except asyncio.TimeoutError:
                await asyncio.sleep(0.05)
                continue
            except BaseException as exc:  # noqa: BLE001
                log.info("websocket recv error: %s", exc, exc_info=True)
                # Don't raise — let the loop retry. If the socket is really
                # dead the next iteration catches ConnectionClosed.
                await asyncio.sleep(0.05)
                continue

            try:
                message = json.loads(raw)
            except Exception as exc:  # noqa: BLE001
                log.debug("malformed CDP frame: %s", exc)
                continue

            if "id" in message:
                tx_id = message["id"]
                tx = self.mapper.pop(tx_id, None)
                if tx is None:
                    log.debug("CDP response for unknown id %s", tx_id)
                    continue
                try:
                    tx(**message)
                except BaseException as exc:  # noqa: BLE001
                    # Patched __call__ should have already routed the error
                    # to the future, but belt-and-suspenders for unpatched
                    # third-party Transaction subclasses.
                    log.info(
                        "CDP response parse failed for %s: %s",
                        getattr(tx, "method", "?"),
                        exc,
                    )
                    if not tx.done():
                        tx.set_exception(exc)
                continue

            try:
                event = cdp_util.parse_json_event(message)
            except Exception as exc:  # noqa: BLE001
                log.debug("event parse failed: %s", exc)
                continue

            callbacks = self.handlers.get(type(event), ())
            for callback in callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback) or asyncio.iscoroutine(callback):
                        try:
                            asyncio.create_task(callback(event, self))
                        except TypeError:
                            asyncio.create_task(callback(event))
                    else:
                        try:
                            callback(event, self)
                        except TypeError:
                            callback(event)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "handler %s raised on %s: %s",
                        callback,
                        type(event).__name__,
                        exc,
                    )

    type.__setattr__(Connection, "_listener", _listener)
    type.__setattr__(Connection, _FLAG_LISTENER, True)
    return True


def _patch_cookie_from_json(cdp_network: Any) -> bool:
    """Make ``Cookie.from_json`` tolerant of missing optional fields.

    Chrome 146+ stopped emitting ``sameParty``. Upstream nodriver hardcodes
    ``bool(json['sameParty'])`` — KeyError on every populated cookie list.

    Patched parser falls back to ``None``/sane defaults for fields that the
    server is allowed to omit (matches the pattern already used in
    ``CookieParam.from_json``).
    """
    Cookie = cdp_network.Cookie
    if getattr(Cookie, _FLAG_COOKIE, False):
        return True

    CookiePriority = cdp_network.CookiePriority
    CookieSourceScheme = cdp_network.CookieSourceScheme
    CookieSameSite = cdp_network.CookieSameSite
    CookiePartitionKey = cdp_network.CookiePartitionKey

    def from_json(cls, json: dict) -> Any:
        return cls(
            name=str(json["name"]),
            value=str(json["value"]),
            domain=str(json["domain"]),
            path=str(json["path"]),
            size=int(json["size"]),
            http_only=bool(json["httpOnly"]),
            secure=bool(json["secure"]),
            session=bool(json["session"]),
            priority=CookiePriority.from_json(json["priority"])
            if json.get("priority") is not None
            else CookiePriority.MEDIUM,
            same_party=bool(json["sameParty"]) if json.get("sameParty") is not None else False,
            source_scheme=CookieSourceScheme.from_json(json["sourceScheme"])
            if json.get("sourceScheme") is not None
            else CookieSourceScheme.UNSET,
            source_port=int(json["sourcePort"]) if json.get("sourcePort") is not None else -1,
            expires=float(json["expires"]) if json.get("expires") is not None else None,
            same_site=CookieSameSite.from_json(json["sameSite"])
            if json.get("sameSite") is not None
            else None,
            partition_key=CookiePartitionKey.from_json(json["partitionKey"])
            if json.get("partitionKey") is not None
            else None,
            partition_key_opaque=bool(json["partitionKeyOpaque"])
            if json.get("partitionKeyOpaque") is not None
            else None,
        )

    type.__setattr__(Cookie, "from_json", classmethod(from_json))
    type.__setattr__(Cookie, _FLAG_COOKIE, True)
    return True
