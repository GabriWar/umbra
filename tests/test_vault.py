"""Vault: stores, origin rules, TOTP, redaction, unlock socket, tool gate.

No browser here: page-side behavior is covered by test_vault_fill.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError
from fastmcp.tools.base import ToolResult
from mcp.types import TextContent

from umbra import server
from umbra.vault import guard, store, unlock


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own data dir, fast KDF, clean process-wide guard state per test."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(store, "_KDF_ITERATIONS", 1000)
    guard._variants.clear()
    guard.hot.clear()
    guard.dirty_epoch.clear()
    store.lock()
    yield
    guard._variants.clear()
    guard.hot.clear()
    guard.dirty_epoch.clear()
    store.lock()


# ─────────────────────────────── origins ───────────────────────────────

@pytest.mark.parametrize("raw,want", [
    ("github.com", "https://github.com"),
    ("https://GitHub.com/login?x=1", "https://github.com"),
    ("https://github.com:443", "https://github.com"),
    ("http://localhost:8080/a", "http://localhost:8080"),
    ("*.github.com", "https://*.github.com"),
])
def test_normalize_origin(raw, want):
    assert store.normalize_origin(raw) == want


@pytest.mark.parametrize("origin,patterns,ok", [
    ("https://github.com", ["https://github.com"], True),
    ("http://github.com", ["https://github.com"], False),          # scheme matters
    ("https://gist.github.com", ["https://github.com"], False),    # no implicit subdomains
    ("https://gist.github.com", ["https://*.github.com"], True),
    ("https://github.com", ["https://*.github.com"], False),       # wildcard is not the apex
    ("https://evilgithub.com", ["https://*.github.com"], False),
    ("https://github.com.evil.com", ["https://github.com"], False),
    ("https://github.com:8443", ["https://github.com"], False),
    ("null", ["https://github.com"], False),                        # data:/opaque origins
])
def test_origin_allowed(origin, patterns, ok):
    assert store.origin_allowed(origin, patterns) is ok


# ──────────────────────────────── TOTP ─────────────────────────────────

def test_totp_rfc6238_vector():
    # RFC 6238 appendix B, SHA1 seed "12345678901234567890", T=59 → 94287082.
    seed = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert store.totp_now(f"otpauth://totp/x?secret={seed}&digits=8", at=59) == "94287082"
    assert store.totp_now(seed.lower(), at=59) == "287082"


# ───────────────────────────── local vault ─────────────────────────────

def test_local_vault_roundtrip_and_wrong_passphrase():
    v = store.LocalVault.open("correct horse battery", create=True)
    v.put(store.Entry(name="github", origins=["https://github.com"],
                      username="me", password="hunter2-long"))
    path = store.vault_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "hunter2-long" not in path.read_text()

    again = store.LocalVault.open("correct horse battery")
    assert again.entries()["github"].password == "hunter2-long"
    with pytest.raises(store.VaultError, match="wrong passphrase"):
        store.LocalVault.open("wrong")


def test_resolve_needs_unlock_and_expires(monkeypatch):
    store.LocalVault.open("pp-long-enough", create=True).put(
        store.Entry(name="site", origins=["https://a.com"], password="s3cret-pw"))
    with pytest.raises(store.VaultError, match="locked"):
        store.resolve("site")
    store.unlock("pp-long-enough", minutes=1)
    assert store.resolve("site").password == "s3cret-pw"

    real = store.time.time
    monkeypatch.setattr(store.time, "time", lambda: real() + 120)
    with pytest.raises(store.VaultError, match="locked"):
        store.resolve("site")


def test_list_public_never_shows_values(monkeypatch):
    monkeypatch.setattr(store, "keyring_available", lambda: False)
    monkeypatch.setattr(store, "rbw_available", lambda: False)
    store.LocalVault.open("pp-long-enough", create=True).put(
        store.Entry(name="site", origins=["https://a.com"], username="me@x.com",
                    password="s3cret-pw", totp="GEZDGNBV"))
    assert store.list_public()["local"]["state"] == "locked"
    store.unlock("pp-long-enough", minutes=5)
    out = json.dumps(store.list_public())
    assert "site" in out and "s3cret-pw" not in out and "me@x.com" not in out
    assert "GEZDGNBV" not in out


# ───────────────────────────── Bitwarden ──────────────────────────────

def test_parse_bitwarden_export():
    doc = {"encrypted": False, "items": [
        {"type": 1, "name": "GitHub", "login": {
            "username": "a", "password": "pa-1111", "totp": None,
            "uris": [{"uri": "https://github.com/login"}, {"uri": "androidapp://x"}]},
         "fields": [{"name": "umbra-confirm", "value": "true"}]},
        {"type": 1, "name": "GitHub", "login": {"username": "b", "password": "pb-2222",
                                                "uris": []}},
        {"type": 2, "name": "a note"},
    ]}
    entries = store.parse_bitwarden_export(json.dumps(doc))
    assert [e.name for e in entries] == ["GitHub", "GitHub/b"]
    assert entries[0].origins[0] == "https://github.com" and entries[0].confirm
    assert not entries[1].confirm
    with pytest.raises(store.VaultError, match="encrypted"):
        store.parse_bitwarden_export(json.dumps({"encrypted": True}))


def test_rbw_backend_with_fake_binary(tmp_path, monkeypatch):
    # Shape of `rbw get --raw` as of rbw 1.x. Not verified against a real rbw.
    raw = {"name": "GitHub", "data": {"username": "me", "password": "bw-pass-1",
                                      "totp": None, "uris": [{"uri": "https://github.com"}]},
           "fields": [], "notes": None}
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "rbw"
    fake.write_text("#!/bin/sh\n"
                    'case "$1" in\n'
                    "  unlocked) exit 0;;\n"
                    "  list) printf 'GitHub\\tme\\nMail\\ta\\nMail\\tb\\n';;\n"
                    f"  get) echo '{json.dumps(raw)}';;\n"
                    "esac\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    assert store.bw_names() == ["bw:GitHub", "bw:Mail::a", "bw:Mail::b"]
    e = store.resolve("bw:GitHub")
    assert e.password == "bw-pass-1" and e.origins == ["https://github.com"]


@pytest.mark.parametrize("name", ["bw:--clipboard::GitHub", "bw:GitHub::--field",
                                  "kr:--help", "-x"])
def test_flag_like_names_rejected(name):
    with pytest.raises(store.VaultError, match="may not start"):
        store.resolve(name)


def test_rbw_gets_end_of_options_marker(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "_run", lambda cmd, **_k: seen.append(cmd) or
                        SimpleNamespace(returncode=1, stdout="", stderr="nope"))
    with pytest.raises(store.VaultError):
        store.bw_get("GitHub")
    assert seen[0][:4] == ["rbw", "get", "--raw", "--"]


# ───────────────────────────── redaction ─────────────────────────────

def test_redaction_variants_and_nesting():
    pw = 'p@ss w"rd/1'
    guard.register(pw, "site")
    assert guard.redact_text(f"x {pw} y") == "x [redacted:site] y"
    assert "[redacted:site]" in guard.redact_text("pwd=p%40ss%20w%22rd%2F1")       # quote
    assert "[redacted:site]" in guard.redact_text("pwd=p%40ss+w%22rd%2F1")         # quote_plus
    assert "[redacted:site]" in guard.redact_text(json.dumps({"p": pw}))           # JSON body
    out = guard.redact({"a": [pw, {"b": pw}], "n": 3})
    assert pw not in json.dumps(out) and out["n"] == 3


def test_short_values_not_redacted():
    guard.register("abc", "x")
    assert guard.redact_text("abc") == "abc"


def test_log_filter_redacts():
    guard.register("log-secret-99", "site")
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "got %s", ("log-secret-99",), None)
    guard.RedactingFilter().filter(rec)
    assert rec.getMessage() == "got [redacted:site]"


def test_audit_has_no_values():
    guard.audit(entry="site", origin="https://a.com", result="filled")
    p = guard.audit_path()
    assert stat.S_IMODE(p.stat().st_mode) == 0o600
    assert json.loads(p.read_text())["entry"] == "site"


# ──────────────────────────── unlock socket ────────────────────────────

async def test_unlock_refuses_listener_that_is_not_umbra(monkeypatch):
    # Any same-user process can create vault-<pid>.sock; the CLI must check
    # the real peer before sending the passphrase.
    store.LocalVault.open("pp-long-enough", create=True)
    monkeypatch.setattr(unlock, "is_umbra_server", lambda _pid: False)
    srv = await unlock.serve()
    try:
        res = await asyncio.to_thread(unlock.broadcast,
                                      {"op": "unlock", "passphrase": "pp-long-enough"})
        assert "not an umbra server" in res[0][1]["error"]
        assert store.unlocked_vault() is None
    finally:
        await unlock.stop(srv)


def test_is_umbra_server_reads_real_argv():
    assert unlock.is_umbra_server(os.getpid()) is False  # this is pytest


async def test_unlock_socket_roundtrip(monkeypatch):
    monkeypatch.setattr(unlock, "is_umbra_server", lambda _pid: True)
    store.LocalVault.open("pp-long-enough", create=True).put(
        store.Entry(name="site", origins=["https://a.com"], password="s3cret-pw"))
    srv = await unlock.serve()
    try:
        bad = await asyncio.to_thread(unlock.broadcast, {"op": "unlock", "passphrase": "nope"})
        assert bad[0][1] == {"ok": False, "error": "wrong passphrase"}
        good = await asyncio.to_thread(unlock.broadcast,
                                       {"op": "unlock", "passphrase": "pp-long-enough", "minutes": 5})
        assert good[0][1]["ok"] and store.unlocked_vault() is not None
        status = await asyncio.to_thread(unlock.broadcast, {"op": "status"})
        assert status[0][1] == {"ok": True, "unlocked": True}
        assert "s3cret" not in json.dumps([bad, good, status])
        await asyncio.to_thread(unlock.broadcast, {"op": "lock"})
        assert store.unlocked_vault() is None
    finally:
        await unlock.stop(srv)
    sock_dir = unlock.socket_dir()
    assert stat.S_IMODE(sock_dir.stat().st_mode) == 0o700


# ──────────────────────────── server gate ────────────────────────────

@pytest.fixture
def fake_tabs(monkeypatch):
    """Two tabs in one browser, one in another; no Chrome involved."""
    tabs = {"t0": {"browser_id": "b1", "tab": object()},
            "t1": {"browser_id": "b1", "tab": object()},
            "t9": {"browser_id": "b2", "tab": object()}}
    monkeypatch.setitem(server._state, "tabs", tabs)
    return tabs


async def test_gate_blocks_readers_while_secret_in_page(fake_tabs, monkeypatch):
    async def holds(_tab, _values):
        return {"found": True, "t": 1.0}
    monkeypatch.setattr(server.vault_fill, "holds", holds)
    guard.hot["t0"] = {"values": ["s3cret-pw"], "t": 1.0}

    for tool in ("evaluate", "extract_markdown", "screenshot", "aria_snapshot", "har_dump"):
        with pytest.raises(ToolError, match="holds a secret"):
            await server._vault_gate(tool, {"tab_id": "t1"})        # same browser, other tab
    with pytest.raises(ToolError):
        await server._vault_gate("navigate", {"tab_id": "t0", "url": "javascript:alert(1)"})
    for url in ("java\tscript:alert(1)", "\x01javascript:alert(1)", " JaVaScRiPt:x",
                "blob:https://a.com/x", "chrome://settings"):
        with pytest.raises(ToolError):
            await server._vault_gate("navigate", {"tab_id": "t0", "url": url})
    await server._vault_gate("aria_click", {"tab_id": "t0", "idx": 3})
    await server._vault_gate("navigate", {"tab_id": "t0", "url": "https://a.com"})
    await server._vault_gate("evaluate", {"tab_id": "t9", "expression": "1"})  # other browser


async def test_gate_releases_once_page_replaced(fake_tabs, monkeypatch):
    async def holds(_tab, _values):
        return {"found": False, "t": 2.0}  # new document, value gone
    monkeypatch.setattr(server.vault_fill, "holds", holds)
    guard.hot["t0"] = {"values": ["s3cret-pw"], "t": 1.0}
    await server._vault_gate("evaluate", {"tab_id": "t0", "expression": "1"})
    assert "t0" not in guard.hot


async def test_same_document_stays_locked_even_without_value(fake_tabs, monkeypatch):
    # An SPA may keep the password in JS state after clearing the input.
    async def holds(_tab, _values):
        return {"found": False, "t": 1.0}
    monkeypatch.setattr(server.vault_fill, "holds", holds)
    guard.hot["t0"] = {"values": ["s3cret-pw"], "t": 1.0}
    with pytest.raises(ToolError):
        await server._vault_gate("evaluate", {"tab_id": "t0", "expression": "1"})


async def test_evaluate_stamps_dirty_epoch(fake_tabs):
    await server._vault_gate("evaluate", {"tab_id": "t1", "expression": "1"})
    assert guard.dirty_epoch["b1"] > 0 and "b2" not in guard.dirty_epoch


async def test_middleware_redacts_results_and_errors():
    guard.register("mw-secret-77", "site")
    mw = server._VaultMiddleware()
    ctx = SimpleNamespace(message=SimpleNamespace(name="list_browsers", arguments={}))

    async def ok_next(_ctx):
        return ToolResult(content=[TextContent(type="text", text="x mw-secret-77")],
                                 structured_content={"v": "mw-secret-77"})
    res = await mw.on_call_tool(ctx, ok_next)
    assert res.content[0].text == "x [redacted:site]"
    assert res.structured_content == {"v": "[redacted:site]"}

    async def bad_next(_ctx):
        raise RuntimeError("boom mw-secret-77")
    with pytest.raises(ToolError, match=r"boom \[redacted:site\]"):
        await mw.on_call_tool(ctx, bad_next)


async def test_fill_secret_refuses_wrong_origin(fake_tabs, monkeypatch):
    store.LocalVault.open("pp-long-enough", create=True).put(
        store.Entry(name="site", origins=["https://a.com"], password="s3cret-pw"))
    store.unlock("pp-long-enough", minutes=5)

    async def state(_tab):
        return {"origin": "https://evil.com", "t": 5.0}
    monkeypatch.setattr(server.vault_fill, "page_state", state)
    out = await (await server.mcp.get_tool("fill_secret")).fn("t0", "site")
    assert out["ok"] is False and "not an allowed origin" in out["why"]
    log_line = guard.audit_path().read_text()
    assert "refused" in log_line and "s3cret-pw" not in log_line
    assert not guard.hot
