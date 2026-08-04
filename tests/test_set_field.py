"""Field-setting + dead-connection detection.

The browser-backed tests drive a real page because the whole point of
`set_field` is behavior the DOM only exhibits for real: native prototype
setters, event dispatch order, and `<select>` option matching.
"""

from __future__ import annotations

import asyncio

import pytest

from umbra import stealth_browser
from umbra.driver import utils as tab_utils
from umbra.server import _is_dead_connection


FORM = """data:text/html,
<form>
  <input name="text" type="text">
  <input name="check" type="checkbox">
  <input name="radio_a" type="radio" name="grp" value="a">
  <textarea name="note"></textarea>
  <select name="lang">
    <option value="en_basic">I can read a little</option>
    <option value="en_fluent">I speak fluently</option>
  </select>
  <div id="rich" contenteditable="true"></div>
</form>
<script>
  // Record dispatched events so we can assert frameworks would see them.
  window.__seen = [];
  for (const el of document.querySelectorAll('input,textarea,select')) {
    for (const t of ['input','change'])
      el.addEventListener(t, e => window.__seen.push(el.name + ':' + t));
  }
</script>
"""


@pytest.fixture(scope="module")
def loop():
    lp = asyncio.new_event_loop()
    yield lp
    lp.close()


@pytest.fixture(scope="module")
def tab(loop):
    ctx = stealth_browser()

    async def _start():
        br = await ctx.__aenter__()
        t = await br.new_tab(FORM)
        await asyncio.sleep(0.4)
        return t

    t = loop.run_until_complete(_start())
    yield loop, t
    loop.run_until_complete(ctx.__aexit__(None, None, None))


def test_text_input_sets_value_and_fires_events(tab):
    loop, t = tab
    res = loop.run_until_complete(
        tab_utils.set_field(t, 'input[name="text"]', "hello"))
    assert res["ok"], res
    assert res["value"] == "hello"
    seen = loop.run_until_complete(t.evaluate("JSON.stringify(window.__seen)"))
    assert "text:input" in seen and "text:change" in seen


def test_checkbox_accepts_boolean(tab):
    loop, t = tab
    res = loop.run_until_complete(
        tab_utils.set_field(t, 'input[name="check"]', True))
    assert res["ok"] and res["checked"] is True
    # Idempotent: setting the same value again must not toggle it back off.
    again = loop.run_until_complete(
        tab_utils.set_field(t, 'input[name="check"]', True))
    assert again["checked"] is True


def test_select_matches_by_visible_text_not_just_value(tab):
    loop, t = tab
    res = loop.run_until_complete(
        tab_utils.set_field(t, 'select[name="lang"]', "I speak fluently"))
    assert res["ok"], res
    assert res["value"] == "en_fluent"


def test_select_reports_options_when_no_match(tab):
    loop, t = tab
    res = loop.run_until_complete(
        tab_utils.set_field(t, 'select[name="lang"]', "Klingon"))
    assert not res["ok"]
    # The failure has to be actionable — list what WAS available.
    assert any("fluently" in o for o in res["options"])


def test_contenteditable(tab):
    loop, t = tab
    res = loop.run_until_complete(tab_utils.set_field(t, "#rich", "typed"))
    assert res["ok"] and res["kind"] == "contenteditable"


def test_missing_selector_is_explicit(tab):
    loop, t = tab
    res = loop.run_until_complete(tab_utils.set_field(t, "#nope", "x"))
    assert not res["ok"] and "no element" in res["why"]


def test_set_fields_batches_in_one_pass(tab):
    loop, t = tab
    res = loop.run_until_complete(tab_utils.set_fields(t, {
        'input[name="text"]': "batched",
        'textarea[name="note"]': "note body",
        'select[name="lang"]': "en_basic",
        'input[name="check"]': False,
    }))
    assert res["ok"], res
    assert not res["failed"]
    assert len(res["set"]) == 4
    val = loop.run_until_complete(
        t.evaluate('document.querySelector(\'textarea[name="note"]\').value'))
    assert val == "note body"


def test_set_fields_reports_partial_failure(tab):
    loop, t = tab
    res = loop.run_until_complete(tab_utils.set_fields(t, {
        'input[name="text"]': "ok",
        "#does-not-exist": "nope",
    }))
    assert not res["ok"]
    assert res["set"] == ['input[name="text"]']
    assert res["failed"] == ["#does-not-exist"]


@pytest.mark.parametrize("exc,expected", [
    (ConnectionRefusedError(111, "Connect call failed"), True),
    (ConnectionResetError(), True),
    (OSError(111, "Connect call failed"), True),
    (RuntimeError("Connect call failed ('127.0.0.1', 56437)"), True),
    (ValueError("unknown tab_id"), False),
    (RuntimeError("element not found"), False),
])
def test_dead_connection_detection(exc, expected):
    assert _is_dead_connection(exc) is expected
