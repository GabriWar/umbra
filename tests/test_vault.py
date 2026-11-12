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
    import base64 as _b64
    import html as _html
    assert "[redacted:site]" in guard.redact_text(_html.escape(pw))
    assert "[redacted:site]" in guard.redact_text(_b64.b64encode(pw.encode()).decode())
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
    guard.hot["t0"] = [{"values": ["s3cret-pw"], "t": 1.0}]

    for tool in ("evaluate", "extract_markdown", "screenshot", "aria_snapshot", "har_dump",
                 "handoff_start", "request_user_input", "find_by_text", "wait_for_text"):
        with pytest.raises(ToolError, match="holds a secret"):
            await server._vault_gate(tool, {"tab_id": "t1"})        # same browser, other tab
    with pytest.raises(ToolError):
        await server._vault_gate("navigate", {"tab_id": "t0", "url": "javascript:alert(1)"})
    for url in ("java\tscript:alert(1)", "\x01javascript:alert(1)", " JaVaScRiPt:x",
                "blob:https://a.com/x", "chrome://settings"):
        with pytest.raises(ToolError):
            await server._vault_gate("navigate", {"tab_id": "t0", "url": url})
    for tool, extra in (("wait_for", {"selector": "input[value^=a]"}),
                        ("scroll", {"selector": "input[value^=a]"}),
                        ("element_rect", {"idx": 3})):
        with pytest.raises(ToolError):
            await server._vault_gate(tool, {"tab_id": "t0", **extra})
    await server._vault_gate("scroll", {"tab_id": "t0", "dy": 300})
    await server._vault_gate("wait_for", {"tab_id": "t0", "url_contains": "/done"})
    await server._vault_gate("aria_click", {"tab_id": "t0", "idx": 3})
    await server._vault_gate("navigate", {"tab_id": "t0", "url": "https://a.com"})
    await server._vault_gate("evaluate", {"tab_id": "t9", "expression": "1"})  # other browser


async def test_gate_releases_on_new_page_and_relocks_on_back(fake_tabs, monkeypatch):
    current = {"t": 2.0}

    async def holds(_tab, _values):
        return {"found": False, "t": current["t"]}
    monkeypatch.setattr(server.vault_fill, "holds", holds)
    guard.hot["t0"] = [{"values": ["s3cret-pw"], "t": 1.0}]
    await server._vault_gate("evaluate", {"tab_id": "t0", "expression": "1"})  # new doc: ok
    current["t"] = 1.0  # back/forward cache brings the filled document back
    with pytest.raises(ToolError):
        await server._vault_gate("evaluate", {"tab_id": "t0", "expression": "1"})


def test_only_plain_block_routes_are_safe():
    rule = lambda **k: SimpleNamespace(**{"enabled": True, "capture": 0, **k})  # noqa: E731
    assert guard.route_is_safe(rule(action="block"))
    assert guard.route_is_safe(rule(action="fulfill", enabled=False))
    for unsafe in (rule(action="continue"), rule(action="tee"), rule(action="block", capture=5),
                   rule(action="fulfill"), rule(action="modify"), rule(action="redirect")):
        assert not guard.route_is_safe(unsafe)


async def test_same_document_stays_locked_even_without_value(fake_tabs, monkeypatch):
    # An SPA may keep the password in JS state after clearing the input.
    async def holds(_tab, _values):
        return {"found": False, "t": 1.0}
    monkeypatch.setattr(server.vault_fill, "holds", holds)
    guard.hot["t0"] = [{"values": ["s3cret-pw"], "t": 1.0}]
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


async def test_fill_locks_before_checking_for_agent_code(fake_tabs, monkeypatch):
    store.LocalVault.open("pp-long-enough", create=True).put(
        store.Entry(name="site", origins=["https://a.com"], password="s3cret-pw"))
    store.unlock("pp-long-enough", minutes=5)

    async def state(_tab):
        return {"origin": "https://a.com", "t": 5.0}
    seen = {}

    async def stale(_bid):
        seen["locked_during_check"] = "t0" in guard.hot
        return ["t1 (evaluate ran)"]
    monkeypatch.setattr(server.vault_fill, "page_state", state)
    monkeypatch.setattr(server, "_stale_pages", stale)
    out = await (await server.mcp.get_tool("fill_secret")).fn("t0", "site")
    assert out["ok"] is False and seen["locked_during_check"] is True
    assert "t0" not in guard.hot  # refused fill releases its own lock


# ───────────────────────────── generate ──────────────────────────────

_FAKE_INFISICAL = '''#!{py}
import json, os, sys
state = os.environ["FAKE_INF_DIR"]
open(os.path.join(state, "argv.log"), "a").write(json.dumps(sys.argv[1:]) + "\\n")
a = sys.argv[1:]
if a[:2] == ["secrets", "set"]:
    key, _, src = a[2].partition("=@")
    open(os.path.join(state, key), "w").write(open(src).read())
elif a[:2] == ["secrets", "get"]:
    p = os.path.join(state, a[2])
    print(open(p).read() if os.path.exists(p) else "*not found*")
elif a[:1] == ["secrets"]:
    print(json.dumps([{{"secretKey": k, "secretValue": open(os.path.join(state, k)).read()}}
                      for k in os.listdir(state) if k != "argv.log"]))
'''


@pytest.fixture
def fake_infisical(tmp_path, monkeypatch):
    import sys
    bindir, state = tmp_path / "bin", tmp_path / "inf"
    bindir.mkdir()
    state.mkdir()
    fake = bindir / "infisical"
    fake.write_text(_FAKE_INFISICAL.format(py=sys.executable))
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_INF_DIR", str(state))
    return state


def test_generated_password_shape():
    for _ in range(50):
        p = store.generate_password(20)
        assert len(p) == 20
        assert any(c.islower() for c in p) and any(c.isupper() for c in p)
        assert any(c.isdigit() for c in p) and any(c in store._SYMBOLS for c in p)
    assert len(store.generate_password(4)) == 8


def test_infisical_generate_resolve_list(fake_infisical):
    pw = store.create("inf:APP_WRITER", ["https://app.x.com"], "me+w@x.com", 20)
    e = store.resolve("inf:APP_WRITER")
    assert e.password == pw and e.username == "me+w@x.com" and e.origins == ["https://app.x.com"]
    # Value goes through a file, never argv.
    argv = (fake_infisical / "argv.log").read_text()
    assert pw not in argv and "APP_WRITER=@" in argv
    listed = store.list_public()["entries"]
    assert {"name": "inf:APP_WRITER", "backend": "infisical"}.items() <= listed[0].items()
    assert pw not in json.dumps(listed)
    with pytest.raises(store.VaultError, match="already exists"):
        store.create("inf:APP_WRITER", ["https://app.x.com"], "", 20)


def test_infisical_plain_value_is_not_an_entry(fake_infisical):
    (fake_infisical / "PLAIN").write_text("hunter2")
    with pytest.raises(store.VaultError, match="not an umbra entry"):
        store.resolve("inf:PLAIN")
    with pytest.raises(store.VaultError, match="no infisical secret"):
        store.resolve("inf:MISSING")
    with pytest.raises(store.VaultError, match="letters, digits"):
        store.resolve("inf:a=b")


def test_bw_generate_uses_rbw_generate(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "rbw_available", lambda: True)
    monkeypatch.setattr(store, "bw_names", lambda: ["bw:Other"])
    monkeypatch.setattr(store, "_run", lambda cmd, **_k: seen.append(cmd) or
                        SimpleNamespace(returncode=0, stdout="gen-pass-123\n", stderr=""))
    assert store.create("bw:App", ["https://app.x.com"], "me", 24) == "gen-pass-123"
    assert seen[0] == ["rbw", "generate", "--uri", "https://app.x.com", "--", "24", "App", "me"]
    monkeypatch.setattr(store, "bw_names", lambda: ["bw:App"])
    with pytest.raises(store.VaultError, match="already exists"):
        store.create("bw:App", [], "", 20)


def test_local_generate_needs_unlock():
    with pytest.raises(store.VaultError, match="locked"):
        store.create("app", ["https://app.x.com"], "", 20)
    store.LocalVault.open("pp-long-enough", create=True)
    store.unlock("pp-long-enough", minutes=1)
    pw = store.create("app", ["https://app.x.com"], "me", 20)
    assert store.resolve("app").password == pw
    with pytest.raises(store.VaultError, match="already exists"):
        store.create("app", ["https://app.x.com"], "", 20)


async def test_secret_generate_tool_hides_password_and_writes_file(tmp_path, fake_infisical):
    out_file = tmp_path / "creds" / ".env.users"
    res = await server.mcp.call_tool("secret_generate", {
        "name": "inf:clsi-lim-writer", "origins": ["dev.x.com"],
        "username": "me+w@x.com", "out_file": str(out_file)})
    data = res.structured_content
    pw = store.resolve("inf:clsi-lim-writer").password
    assert data["ok"] is True and data["origins"] == ["https://dev.x.com"]
    assert data["out_keys"] == ["CLSI_LIM_WRITER_USERNAME", "CLSI_LIM_WRITER_PASSWORD"]
    assert pw not in json.dumps(data) and pw not in res.content[0].text
    text = out_file.read_text()
    assert f"CLSI_LIM_WRITER_PASSWORD={pw}\n" in text
    assert "CLSI_LIM_WRITER_USERNAME=me+w@x.com" in text
    assert stat.S_IMODE(out_file.stat().st_mode) == 0o600
    assert guard.redact_text(pw) == "[redacted:inf:clsi-lim-writer]"
    # Second one appends, first stays.
    await server.mcp.call_tool("secret_generate", {
        "name": "inf:other", "origins": ["dev.x.com"], "out_file": str(out_file)})
    assert "CLSI_LIM_WRITER_PASSWORD" in out_file.read_text()
    assert "OTHER_PASSWORD=" in out_file.read_text()


async def test_secret_generate_refuses_without_origin():
    res = await server.mcp.call_tool("secret_generate", {"name": "kr:x", "origins": []})
    assert res.structured_content["ok"] is False


def test_generate_custom_symbols_and_short_length():
    for _ in range(50):
        p = store.generate_password(10, symbols="$")
        assert len(p) == 10 and "$" in p
        assert set(p) <= set(store.string.ascii_letters + store.string.digits + "$")
        assert store.generate_password(8, symbols="").isalnum()
    assert len(store.generate_password(3)) == 8
    with pytest.raises(store.VaultError, match="printable punctuation"):
        store.generate_password(12, symbols="a!")


def test_bw_generate_symbols(monkeypatch):
    seen = []
    monkeypatch.setattr(store, "rbw_available", lambda: True)
    monkeypatch.setattr(store, "bw_names", lambda: [])
    monkeypatch.setattr(store, "_run", lambda cmd, **_k: seen.append(cmd) or
                        SimpleNamespace(returncode=0, stdout="x\n", stderr=""))
    store.create("bw:A", [], "", 16, symbols="")
    assert "--no-symbols" in seen[0]
    with pytest.raises(store.VaultError, match="rbw can only"):
        store.create("bw:B", [], "", 16, symbols="$")
