"""nodriver_patch must fit the installed nodriver, not only the one it was written for.

nodriver 0.50 rewrote the connection layer. Installing umbra's 0.48-era
listener over it left every CDP call unanswered: Chrome launched, then
``spawn`` hung forever.
"""
import nodriver.core.connection as conn_mod
from nodriver.cdp import network

from umbra import nodriver_patch


def test_apply_reports_success():
    assert nodriver_patch.apply() is True


def test_listener_left_alone_on_new_connection_layer():
    nodriver_patch.apply()
    if nodriver_patch._legacy_connection(conn_mod):
        assert getattr(conn_mod.Connection, nodriver_patch._FLAG_LISTENER, False)
    else:
        assert not getattr(conn_mod.Connection, nodriver_patch._FLAG_LISTENER, False)
        assert conn_mod.Connection._listener.__module__ == conn_mod.__name__


def test_cookie_without_same_party_parses():
    nodriver_patch.apply()
    cookie = network.Cookie.from_json({
        "name": "a", "value": "b", "domain": "example.com", "path": "/",
        "size": 2, "httpOnly": False, "secure": True, "session": True,
    })
    assert cookie.name == "a"
    assert cookie.source_port == -1


def test_tab_get_does_not_reattach():
    """0.50's Tab.get re-attached on every navigation, orphaning Fetch interception."""
    import asyncio
    import nodriver.core.tab as tab_mod

    nodriver_patch.apply()
    if nodriver_patch._legacy_connection(conn_mod):
        return
    calls = []

    class FakeTab:
        browser = object()
        attached = True

        async def send(self, cmd):
            calls.append("send")

        async def attach(self):
            calls.append("attach")

    asyncio.run(tab_mod.Tab.get(FakeTab(), "https://example.com"))
    assert calls == ["send"]
