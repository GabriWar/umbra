"""Full request-interception graph over CDP Fetch.

One `RouteEngine` per tab. Owns the `Fetch.enable` lifecycle, dispatches paused
events through a priority-ordered rule list, and supports rich match DSL plus
every action the CDP Fetch domain exposes.

Match DSL (any-of fields = AND):
    url_pattern        substring on URL (cheap, default)
    url_regex          re.fullmatch on URL
    method             GET/POST/...
    resource_type      Document/XHR/Fetch/Script/Stylesheet/Image/Font/Media/...
    header_match       {header_name_lower: regex} — re.search per header
    status_min/max     response-stage filter (forces response-stage interception)
    times              auto-disable after N matches

Actions:
    block       fail_request(error_reason=...)            custom reason → chaos
    fulfill     fulfill_request(status, headers, body) — never hits network
    continue    continue_request(...)                    request rewrite (url/method/post_data/headers)
    modify      response-stage: getResponseBody → body_replace [[regex, repl], ...] OR
                  body/body_b64 outright replace; status/headers optional override
    tee         pass through unchanged but capture into per-rule buffer (spy mode);
                  forces response-stage interception so body is captured
    redirect    fulfill with status (default 302) + Location header to `new_url`

Per-rule extras:
    delay_ms    asyncio.sleep before action — latency injection / chaos test
    priority    higher fires first; tie-break by insertion order
    capture     int — keep last N (req, resp) pairs in rule.captures for debugging
    enabled     bool — pause without delete

HAR record: capture every paused req+resp pair into a HAR-1.2-shaped buffer.
HAR replay: load HAR JSON, install a fallthrough that serves from it (method+url
key, optional `loose` URL-only fallback for query-string drift).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────
# Rule
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class Rule:
    id: str
    action: str  # block | fulfill | continue | modify | tee | redirect
    # Match DSL
    url_pattern: str | None = None
    url_regex: str | None = None
    method: str | None = None
    resource_type: str | None = None
    header_match: dict[str, str] | None = None
    status_min: int | None = None
    status_max: int | None = None
    # Action params (block)
    error_reason: str = "BlockedByClient"
    # Action params (fulfill / modify / redirect)
    status: int | None = None
    headers: dict[str, str] | None = None
    body: str | None = None
    body_b64: str | None = None
    # Action params (continue — request rewrite)
    new_url: str | None = None
    new_method: str | None = None
    new_post_data: str | None = None
    # Action params (modify — response rewrite)
    body_replace: list[list[str]] | None = None
    # Misc
    delay_ms: int = 0
    times: int | None = None
    priority: int = 0
    capture: int = 0  # max captures stored
    enabled: bool = True
    # Runtime
    hits: int = 0
    captures: list[dict[str, Any]] = field(default_factory=list)
    _re_url: re.Pattern[str] | None = field(default=None, repr=False)
    _re_headers: dict[str, re.Pattern[str]] | None = field(default=None, repr=False)
    _re_body: list[tuple[re.Pattern[str], str]] | None = field(default=None, repr=False)
    _insert_seq: int = 0  # insertion order tiebreaker

    def __post_init__(self) -> None:
        if self.url_regex:
            self._re_url = re.compile(self.url_regex)
        if self.header_match:
            self._re_headers = {k.lower(): re.compile(v) for k, v in self.header_match.items()}
        if self.body_replace:
            self._re_body = [(re.compile(p), r) for p, r in self.body_replace]

    @property
    def primary_stage(self) -> str:
        """Which Fetch stage the rule's action runs on."""
        if self.action in ("modify", "tee"):
            return "Response"
        if self.status_min is not None or self.status_max is not None:
            return "Response"
        return "Request"

    @property
    def needs_response_stage(self) -> bool:
        """True if engine must enable Response-stage interception for this rule.
        capture > 0 forces response-stage even for request-stage actions so we
        can read the body after the request resolves."""
        return self.primary_stage == "Response" or self.capture > 0

    def matches_request(self, url: str, method: str, rtype: str, headers: dict[str, str]) -> bool:
        if not self.enabled:
            return False
        if self.times is not None and self.hits >= self.times:
            return False
        if self.url_pattern and self.url_pattern not in url:
            return False
        if self._re_url and not self._re_url.fullmatch(url):
            return False
        if self.method and self.method.upper() != method.upper():
            return False
        if self.resource_type and self.resource_type.lower() != rtype.lower():
            return False
        if self._re_headers:
            lc = {k.lower(): v for k, v in headers.items()}
            for hname, pat in self._re_headers.items():
                if hname not in lc or not pat.search(lc[hname]):
                    return False
        return True

    def matches_response(self, status: int) -> bool:
        if self.status_min is not None and status < self.status_min:
            return False
        if self.status_max is not None and status > self.status_max:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id, "action": self.action,
            "enabled": self.enabled, "hits": self.hits,
            "priority": self.priority,
        }
        for k in ("url_pattern", "url_regex", "method", "resource_type",
                  "header_match", "status_min", "status_max",
                  "status", "headers", "body", "body_b64",
                  "new_url", "new_method", "new_post_data", "body_replace",
                  "delay_ms", "times", "capture"):
            v = getattr(self, k)
            if v is None:
                continue
            if v == "" or v == [] or v == {}:
                continue
            if k == "delay_ms" and v == 0:
                continue
            if k == "capture" and v == 0:
                continue
            out[k] = v
        if self.action == "block" and self.error_reason != "BlockedByClient":
            out["error_reason"] = self.error_reason
        if self.captures:
            out["captures_held"] = len(self.captures)
        return out


# ─────────────────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────────────────

class RouteEngine:
    """Per-tab interception engine. Lazy CDP wire-up on first rule."""

    def __init__(self, tab: Any, *, tracker_block: bool = True,
                  block_resource_types: set[str] | None = None) -> None:
        self.tab = tab
        self.rules: list[Rule] = []
        self._next_id = 0
        self._next_seq = 0
        self._enabled_stages: set[str] = set()  # 'Request' / 'Response'
        # Inherited blocking (replaces browser._wire_blocking when this engine takes over)
        self.tracker_block = tracker_block
        self.block_resource_types: set[str] = {t.lower() for t in (block_resource_types or set())}
        self.tracker_blocks_count = 0
        self.resource_type_blocks_count = 0
        # HAR record
        self.har_recording = False
        self.har_entries: list[dict[str, Any]] = []
        self._har_pending: dict[str, dict[str, Any]] = {}
        # HAR replay corpus
        self.har_replay_index: dict[tuple[str, str], dict[str, Any]] = {}
        self.har_replay_loose_index: dict[str, dict[str, Any]] = {}
        # Tracks request_id → rule_id for rules that need cross-stage data
        # (e.g., capture > 0 on a request-stage action — body comes at response).
        self._request_owners: dict[str, str] = {}
        # Handler installed flag
        self._handler_installed = False

    # ── public API ─────────────────────────────────────────────────────

    def new_rule(self, **kwargs: Any) -> Rule:
        rid = kwargs.pop("id", None) or f"r{self._next_id}"
        self._next_id += 1
        rule = Rule(id=rid, **kwargs)
        rule._insert_seq = self._next_seq
        self._next_seq += 1
        self.rules.append(rule)
        self._sort()
        return rule

    def _sort(self) -> None:
        # priority desc, insertion seq asc
        self.rules.sort(key=lambda r: (-r.priority, r._insert_seq))

    def remove(self, rule_id: str) -> bool:
        for i, r in enumerate(self.rules):
            if r.id == rule_id:
                self.rules.pop(i)
                return True
        return False

    def find(self, rule_id: str) -> Rule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def clear(self) -> int:
        n = len(self.rules)
        self.rules.clear()
        return n

    def list_dicts(self) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self.rules]

    async def ensure_wired(self) -> None:
        """Install handler + Fetch.enable with patterns covering current rule needs."""
        import nodriver as uc
        cdp = uc.cdp

        need_response = (
            any(r.needs_response_stage and r.enabled for r in self.rules)
            or bool(self.har_replay_index)
            or self.har_recording
        )
        wanted: set[str] = {"Request"}
        if need_response:
            wanted.add("Response")

        if wanted == self._enabled_stages and self._handler_installed:
            return

        patterns = []
        for stage in sorted(wanted):
            patterns.append(cdp.fetch.RequestPattern(
                url_pattern="*", request_stage=cdp.fetch.RequestStage(stage),
            ))
        await self.tab.send(cdp.fetch.enable(patterns=patterns))
        self._enabled_stages = wanted

        if not self._handler_installed:
            self.tab.add_handler(cdp.fetch.RequestPaused, self._on_paused)
            self._handler_installed = True
            # Tell the legacy browser._wire_blocking handler (if installed) to
            # defer to us — prevents double-fire / "Invalid state" errors.
            self.tab._umbra_route_engine_owns = True

    async def disable(self) -> None:
        import nodriver as uc
        with contextlib.suppress(Exception):
            await self.tab.send(uc.cdp.fetch.disable())
        self._enabled_stages.clear()

    # ── HAR ────────────────────────────────────────────────────────────

    def har_dump(self) -> dict[str, Any]:
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "umbra", "version": "0.6"},
                "entries": list(self.har_entries),
            }
        }

    def har_load(self, data: dict[str, Any], *, loose: bool = False) -> int:
        entries = data.get("log", {}).get("entries", [])
        n = 0
        for e in entries:
            req = e.get("request", {})
            url = req.get("url")
            method = (req.get("method") or "GET").upper()
            if not url:
                continue
            self.har_replay_index[(method, url)] = e
            if loose:
                self.har_replay_loose_index[url] = e
            n += 1
        return n

    def har_clear_replay(self) -> int:
        n = len(self.har_replay_index)
        self.har_replay_index.clear()
        self.har_replay_loose_index.clear()
        return n

    # ── handler ────────────────────────────────────────────────────────

    async def _on_paused(self, event: Any) -> None:
        import nodriver as uc
        cdp = uc.cdp
        tab = self.tab
        rid = event.request_id
        req = event.request
        url = req.url
        method = req.method
        rtype_obj = getattr(event, "resource_type", None)
        rtype = str(rtype_obj).rsplit(".", 1)[-1] if rtype_obj else ""
        headers = dict(getattr(req, "headers", {}) or {})
        is_response = getattr(event, "response_status_code", None) is not None
        status = getattr(event, "response_status_code", 0) or 0

        # ── HAR record (request stage: open pending entry) ───────────
        if self.har_recording and not is_response:
            self._har_pending[rid] = {
                "_t0": time.perf_counter(),
                "startedDateTime": _iso_now(),
                "request": _build_har_request(req, headers),
            }

        # ── inherited blocking (request-stage only, preempts user rules) ──
        if not is_response:
            from umbra.stealth.blocklist import is_blocked
            if self.tracker_block and is_blocked(url):
                self.tracker_blocks_count += 1
                with contextlib.suppress(Exception):
                    await tab.send(cdp.fetch.fail_request(
                        request_id=rid,
                        error_reason=cdp.network.ErrorReason.BLOCKED_BY_CLIENT,
                    ))
                # drop pending HAR (request never completed)
                self._har_pending.pop(rid, None)
                return
            if self.block_resource_types and rtype.lower() in self.block_resource_types:
                self.resource_type_blocks_count += 1
                with contextlib.suppress(Exception):
                    await tab.send(cdp.fetch.fail_request(
                        request_id=rid,
                        error_reason=cdp.network.ErrorReason.BLOCKED_BY_CLIENT,
                    ))
                self._har_pending.pop(rid, None)
                return

        # ── select first matching rule (primary-stage match) ─────────
        wanted_stage = "Response" if is_response else "Request"
        chosen: Rule | None = None
        for rule in self.rules:
            if rule.primary_stage != wanted_stage:
                continue
            if not rule.matches_request(url, method, rtype, headers):
                continue
            if is_response and not rule.matches_response(status):
                continue
            chosen = rule
            break

        # ── HAR record (capture body BEFORE dispatch may fulfill) ────
        if is_response and self.har_recording:
            await self._capture_response_for_har(rid, event)

        # ── per-rule capture (also pre-dispatch for body fidelity) ───
        captured_body: tuple[str, bool] | None = None
        if chosen and chosen.capture and is_response:
            captured_body = await _try_get_body(tab, rid)

        # ── dispatch ─────────────────────────────────────────────────
        if chosen:
            chosen.hits += 1
            if chosen.delay_ms:
                await asyncio.sleep(chosen.delay_ms / 1000)
            try:
                await self._dispatch(chosen, event, is_response, captured_body)
            except Exception as e:  # noqa: BLE001
                await self._passthrough(event, is_response)
                _record_capture(chosen, url, method, headers, status, captured_body, error=str(e))
                return
            # Cross-stage capture: request-stage rule with capture > 0 means we
            # also want the body when response arrives. Record ownership so the
            # response-stage handler captures for this rule. We deliberately do
            # NOT record an empty request-stage capture for these — the
            # response-stage handler will produce the full entry once body is in.
            if not is_response and chosen.capture and chosen.action in ("continue", "tee"):
                self._request_owners[rid] = chosen.id
            elif chosen.capture:
                _record_capture(chosen, url, method, headers, status, captured_body)
            return

        # ── cross-stage capture (response stage, owner from request stage) ─
        if is_response:
            owner_id = self._request_owners.pop(rid, None)
            if owner_id:
                owner = self.find(owner_id)
                if owner and owner.capture:
                    body = await _try_get_body(tab, rid)
                    _record_capture(owner, url, method, headers, status, body)
                await self._passthrough(event, is_response)
                return

        # ── HAR replay (request stage fallthrough) ───────────────────
        if not is_response and self.har_replay_index:
            entry = (self.har_replay_index.get((method.upper(), url))
                     or self.har_replay_loose_index.get(url))
            if entry:
                try:
                    await self._serve_from_har(rid, entry)
                except Exception:  # noqa: BLE001
                    await self._passthrough(event, is_response)
                return

        # ── default: pass through ────────────────────────────────────
        await self._passthrough(event, is_response)

    async def _passthrough(self, event: Any, is_response: bool) -> None:
        import nodriver as uc
        cdp = uc.cdp
        rid = event.request_id
        with contextlib.suppress(Exception):
            if is_response:
                await self.tab.send(cdp.fetch.continue_response(request_id=rid))
            else:
                await self.tab.send(cdp.fetch.continue_request(request_id=rid))

    async def _dispatch(self, rule: Rule, event: Any, is_response: bool,
                          captured_body: tuple[str, bool] | None) -> None:
        import nodriver as uc
        cdp = uc.cdp
        tab = self.tab
        rid = event.request_id

        if rule.action == "block":
            # block at response stage: synthesize 5xx via fulfill (Chrome rejects
            # several ErrorReasons at response stage, fulfill is more reliable)
            if is_response:
                hdrs = [cdp.fetch.HeaderEntry(name=k, value=v)
                         for k, v in (rule.headers or {"content-type": "text/plain"}).items()]
                await tab.send(cdp.fetch.fulfill_request(
                    request_id=rid, response_code=rule.status or 502,
                    response_headers=hdrs, body=base64.b64encode(b"blocked by route").decode(),
                ))
                return
            reason = _coerce_error_reason(rule.error_reason)
            await tab.send(cdp.fetch.fail_request(request_id=rid, error_reason=reason))
            return

        if rule.action == "fulfill":
            body_b64 = rule.body_b64 or base64.b64encode((rule.body or "").encode()).decode()
            hdrs = [cdp.fetch.HeaderEntry(name=k, value=v) for k, v in (rule.headers or {}).items()]
            await tab.send(cdp.fetch.fulfill_request(
                request_id=rid, response_code=rule.status or 200,
                response_headers=hdrs, body=body_b64,
            ))
            return

        if rule.action == "redirect":
            target = rule.new_url or ""
            hdrs_dict = {"location": target}
            if rule.headers:
                hdrs_dict.update(rule.headers)
            hdrs = [cdp.fetch.HeaderEntry(name=k, value=v) for k, v in hdrs_dict.items()]
            await tab.send(cdp.fetch.fulfill_request(
                request_id=rid, response_code=rule.status or 302,
                response_headers=hdrs, body=base64.b64encode(b"").decode(),
            ))
            return

        if rule.action == "continue":
            if is_response:
                # response-stage continue: only headers/status overrideable
                kwargs: dict[str, Any] = {"request_id": rid}
                if rule.status is not None:
                    kwargs["response_code"] = rule.status
                if rule.headers:
                    kwargs["response_headers"] = [cdp.fetch.HeaderEntry(name=k, value=v)
                                                    for k, v in rule.headers.items()]
                await tab.send(cdp.fetch.continue_response(**kwargs))
                return
            kwargs = {"request_id": rid}
            if rule.new_url:
                kwargs["url"] = rule.new_url
            if rule.new_method:
                kwargs["method"] = rule.new_method
            if rule.new_post_data is not None:
                kwargs["post_data"] = base64.b64encode(rule.new_post_data.encode()).decode()
            if rule.headers:
                # CDP `continueRequest.headers` REPLACES — merge w/ original to
                # avoid stripping Accept/User-Agent/etc. User keys win on collision.
                orig_headers = dict(getattr(event.request, "headers", {}) or {})
                orig_headers.update(rule.headers)
                kwargs["headers"] = [cdp.fetch.HeaderEntry(name=k, value=v)
                                      for k, v in orig_headers.items()]
            await tab.send(cdp.fetch.continue_request(**kwargs))
            return

        if rule.action == "tee":
            # Pure spy — pass through unchanged. Body capture happened pre-dispatch.
            await self._passthrough(event, is_response)
            return

        if rule.action == "modify":
            # response-stage only by virtue of needs_response_stage
            body_str = ""
            if captured_body is not None:
                raw, is_b64 = captured_body
                if is_b64:
                    try:
                        body_str = base64.b64decode(raw).decode("utf-8")
                    except (ValueError, UnicodeDecodeError):
                        body_str = ""
                else:
                    body_str = raw
            else:
                got = await _try_get_body(tab, rid)
                if got is not None:
                    raw, is_b64 = got
                    body_str = base64.b64decode(raw).decode("utf-8", errors="replace") if is_b64 else raw

            if rule._re_body:
                for pat, repl in rule._re_body:
                    body_str = pat.sub(repl, body_str)
            if rule.body is not None:
                body_str = rule.body
            elif rule.body_b64 is not None:
                body_str = base64.b64decode(rule.body_b64).decode("utf-8", errors="replace")

            new_status = rule.status if rule.status is not None else (
                getattr(event, "response_status_code", None) or 200)
            base_headers: dict[str, str] = {}
            for h in (getattr(event, "response_headers", None) or []):
                base_headers[h.name] = h.value
            if rule.headers:
                base_headers.update(rule.headers)
            hdrs = [cdp.fetch.HeaderEntry(name=k, value=v) for k, v in base_headers.items()]
            await tab.send(cdp.fetch.fulfill_request(
                request_id=rid, response_code=new_status,
                response_headers=hdrs,
                body=base64.b64encode(body_str.encode("utf-8", errors="replace")).decode(),
            ))
            return

        # unknown action → pass through
        await self._passthrough(event, is_response)

    async def _serve_from_har(self, rid: str, entry: dict[str, Any]) -> None:
        import nodriver as uc
        cdp = uc.cdp
        resp = entry.get("response", {}) or {}
        status = resp.get("status", 200)
        content = resp.get("content", {}) or {}
        text = content.get("text", "")
        if content.get("encoding") == "base64":
            body_b64 = text
        else:
            body_b64 = base64.b64encode(text.encode("utf-8", errors="replace")).decode()
        hdrs = [cdp.fetch.HeaderEntry(name=h["name"], value=h["value"])
                for h in (resp.get("headers") or []) if "name" in h]
        await self.tab.send(cdp.fetch.fulfill_request(
            request_id=rid, response_code=status,
            response_headers=hdrs, body=body_b64,
        ))

    async def _capture_response_for_har(self, rid: str, event: Any) -> None:
        pending = self._har_pending.pop(rid, None)
        if pending is None:
            # response without paired request (Fetch attached late) — synthesize stub
            pending = {
                "_t0": time.perf_counter(),
                "startedDateTime": _iso_now(),
                "request": _build_har_request(event.request,
                                                dict(getattr(event.request, "headers", {}) or {})),
            }
        body = await _try_get_body(self.tab, rid)
        body_text, body_b64_flag = ("", False)
        if body is not None:
            raw, is_b64 = body
            body_text, body_b64_flag = raw, is_b64
        resp_headers_list = [{"name": h.name, "value": h.value}
                              for h in (getattr(event, "response_headers", None) or [])]
        status = getattr(event, "response_status_code", 0) or 0
        elapsed_ms = (time.perf_counter() - pending.pop("_t0", time.perf_counter())) * 1000
        pending["time"] = elapsed_ms
        pending["response"] = {
            "status": status, "statusText": "",
            "httpVersion": "HTTP/1.1",
            "headers": resp_headers_list, "cookies": [],
            "content": {
                "size": len(body_text), "mimeType": _mime_from_headers(resp_headers_list),
                "text": body_text,
                **({"encoding": "base64"} if body_b64_flag else {}),
            },
            "redirectURL": "", "headersSize": -1, "bodySize": -1,
        }
        pending["cache"] = {}
        pending["timings"] = {"send": 0, "wait": elapsed_ms, "receive": 0}
        self.har_entries.append(pending)


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────

_ERROR_REASON_ALIASES = {
    "blocked": "BlockedByClient",
    "blockedbyclient": "BlockedByClient",
    "blockedbyresponse": "BlockedByResponse",
    "failed": "Failed",
    "aborted": "Aborted",
    "timedout": "TimedOut",
    "timeout": "TimedOut",
    "accessdenied": "AccessDenied",
    "connectionclosed": "ConnectionClosed",
    "connectionreset": "ConnectionReset",
    "connectionrefused": "ConnectionRefused",
    "connectionaborted": "ConnectionAborted",
    "connectionfailed": "ConnectionFailed",
    "namenotresolved": "NameNotResolved",
    "internetdisconnected": "InternetDisconnected",
    "addressunreachable": "AddressUnreachable",
}


def _coerce_error_reason(reason: str) -> Any:
    import nodriver as uc
    canonical = _ERROR_REASON_ALIASES.get(reason.lower().replace("_", ""), reason)
    return uc.cdp.network.ErrorReason(canonical)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _mime_from_headers(headers: list[dict[str, str]]) -> str:
    for h in headers:
        if h.get("name", "").lower() == "content-type":
            return h.get("value", "").split(";")[0].strip()
    return ""


def _build_har_request(req: Any, headers: dict[str, str]) -> dict[str, Any]:
    post = getattr(req, "post_data", None)
    return {
        "method": req.method, "url": req.url,
        "httpVersion": "HTTP/1.1",
        "headers": [{"name": k, "value": v} for k, v in headers.items()],
        "queryString": [], "cookies": [],
        "headersSize": -1,
        "bodySize": len(post) if post else -1,
        "postData": ({"mimeType": "application/octet-stream", "text": post}
                      if post else None),
    }


async def _try_get_body(tab: Any, rid: str) -> tuple[str, bool] | None:
    """Best-effort getResponseBody. Returns (body, is_b64) or None on failure."""
    import nodriver as uc
    try:
        body, is_b64 = await tab.send(uc.cdp.fetch.get_response_body(request_id=rid))
        return body, is_b64
    except Exception:  # noqa: BLE001
        return None


def _record_capture(rule: Rule, url: str, method: str, headers: dict[str, str],
                     status: int, body: tuple[str, bool] | None,
                     *, error: str | None = None) -> None:
    if rule.capture <= 0:
        return
    entry = {
        "ts": _iso_now(),
        "url": url, "method": method, "status": status,
        "request_headers": headers,
    }
    if body is not None:
        raw, is_b64 = body
        entry["body"] = raw
        if is_b64:
            entry["body_encoding"] = "base64"
    if error:
        entry["error"] = error
    rule.captures.append(entry)
    if len(rule.captures) > rule.capture:
        rule.captures = rule.captures[-rule.capture:]


def load_har_text(text: str) -> dict[str, Any]:
    return json.loads(text)
