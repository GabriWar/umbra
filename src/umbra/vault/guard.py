"""Keeps filled secrets out of everything the agent can read.

Four mechanisms, all process-wide:
  redaction   every tool result, tool error and log line has each filled
              password (raw / JSON-escaped / URL-encoded) replaced by a marker
  hot tabs    while a secret is in a tab's page, tools that can read that
              browser's pages are refused (allowlist below)
  dirty epoch the last time agent-supplied code ran in a browser; a fill is
              refused while any page older than that is still open
  audit       one JSON line per fill attempt, values never included
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

# Shorter values would redact ordinary words and digits out of every result.
_MIN_REDACT_LEN = 4

# variant string → entry name
_variants: dict[str, str] = {}

# tab_id → {"values": [...], "time_origin": float}  (page that holds a secret)
hot: dict[str, dict[str, Any]] = {}

# browser_id → epoch ms of the last agent-code call in that browser
dirty_epoch: dict[str, float] = {}

# Tools that run agent-supplied code (or CSS, which can exfiltrate attribute
# values) inside a page. navigate counts only for javascript: URLs.
INJECTING = frozenset({"evaluate", "inject_css"})

# Route actions that let the agent serve its own content on a real origin.
UNSAFE_ROUTE_ACTIONS = frozenset({"fulfill", "modify", "redirect"})

# What a tab-scoped tool may do while a secret sits in a page of the same
# browser: move, click, type keys, wait, navigate, hand to a human. Nothing
# here returns page content or runs agent code.
ALLOWED_WHILE_HOT = frozenset({
    "fill_secret", "secrets_list",
    "aria_click", "click_at", "press_key", "hover", "scroll", "element_rect",
    "find_by_text", "find_all_by_text", "wait_for", "wait_for_text",
    "navigate", "back", "forward", "reload", "close", "switch_tab", "list_tabs",
    "handoff_start", "handoff_wait", "request_user_input",
})


# Schemes that only ever load a new document. Anything else (javascript:,
# blob:, chrome:, a mangled scheme) is treated as agent code.
_PLAIN_SCHEMES = frozenset({"http", "https", "about", "data"})


def is_injecting(tool: str, args: dict[str, Any]) -> bool:
    if tool in INJECTING:
        return True
    if tool != "navigate":
        return False
    # Parse the way the URL spec does: tab/newline are removed anywhere and
    # C0 controls + space trimmed at both ends ("java\tscript:" is javascript:).
    url = re.sub(r"[\t\n\r]", "", str(args.get("url", "")))
    url = url.strip("".join(chr(c) for c in range(0x21)))
    return urlsplit(url).scheme.lower() not in _PLAIN_SCHEMES


# ───────────────────────────── redaction ─────────────────────────────

def register(value: str, label: str) -> None:
    if not value or len(value) < _MIN_REDACT_LEN:
        return
    for v in {value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)}:
        _variants[v] = label


def redact_text(s: str) -> str:
    if not _variants or not isinstance(s, str):
        return s
    for v in sorted(_variants, key=len, reverse=True):
        if v in s:
            s = s.replace(v, f"[redacted:{_variants[v]}]")
    return s


def redact(obj: Any) -> Any:
    if not _variants:
        return obj
    if isinstance(obj, str):
        return redact_text(obj)
    if isinstance(obj, dict):
        return {redact_text(k) if isinstance(k, str) else k: redact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(redact(x) for x in obj)
    return obj


class RedactingFilter(logging.Filter):
    """Attach to handlers: rewrites the formatted message of every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _variants:
            record.msg = redact_text(record.getMessage())
            record.args = None
        return True


# ─────────────────────────────── audit ───────────────────────────────

def audit_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(base) / "umbra" / "vault-audit.log"


def audit(**event: Any) -> None:
    p = audit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        f.write(json.dumps({"ts": int(time.time()), **event}) + "\n")


# ────────────────────────────── confirm ──────────────────────────────

async def confirm(name: str, origin: str, timeout_s: float = 60) -> tuple[bool, str]:
    """Desktop notification with Approve/Deny. Fails closed."""
    if not shutil.which("notify-send"):
        return False, "entry needs confirmation but notify-send is not installed"
    try:
        proc = await asyncio.create_subprocess_exec(
            "notify-send", "--app-name=umbra", "-u", "critical",
            "-t", str(int(timeout_s * 1000)),
            "-A", "approve=Aprovar", "-A", "deny=Negar",
            "umbra: preencher senha?", f"{name} em {origin}",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return False, f"confirmation failed: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout_s + 5)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "no answer to the confirmation in time"
    if out.decode().strip() == "approve":
        return True, "approved"
    return False, "denied on the desktop"
