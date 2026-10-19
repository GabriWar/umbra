"""Page-side work for the vault, run in a CDP isolated world.

An isolated world shares the DOM with the page but not its JS globals, so
neither page scripts nor code the agent injected earlier can patch the
setters, `location` or `performance` this module relies on.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import nodriver as uc

from umbra.vault.store import VaultError

cdp = uc.cdp

# Collects matches from the document, same-origin iframes and open shadow roots.
_DEEP = r"""
const deepAll = (sel) => {
  const out = [];
  const walk = (root) => {
    out.push(...root.querySelectorAll(sel));
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) walk(el.shadowRoot);
    for (const f of root.querySelectorAll('iframe')) {
      try { if (f.contentDocument) walk(f.contentDocument); } catch (e) {}
    }
  };
  walk(document);
  return out;
};
"""

_STATE_JS = "JSON.stringify({origin: location.origin, t: performance.timeOrigin})"

_HOLDS_JS = "(function(vals){" + _DEEP + r"""
  const found = deepAll('input,textarea').some(el => vals.includes(el.value));
  return JSON.stringify({t: performance.timeOrigin, found});
})"""

# Returns field NAMES only; values never leave this function.
_FILL_JS = "(function(a){" + _DEEP + r"""
  if (location.origin !== a.origin) return JSON.stringify({ok: false, why: 'page changed origin'});
  const vis = el => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
  const pick = (sel, auto) => sel ? (deepAll(sel)[0] || null) : (auto().find(vis) || null);
  const kind = el => el && el.tagName === 'INPUT' ? (el.type || 'text').toLowerCase() : '';
  const TEXTY = ['text', 'email', 'tel', 'search', 'url', ''];

  const pwd = pick(a.sel.password, () => deepAll('input[type=password]'));
  const user = pick(a.sel.username, () => {
    const c = deepAll('input').filter(el => TEXTY.includes(kind(el)) && vis(el));
    const tagged = c.filter(el => /\b(username|email)\b/.test(el.autocomplete || ''));
    if (tagged.length) return tagged;
    if (pwd) return c.filter(el => el.form === pwd.form
      && (el.compareDocumentPosition(pwd) & Node.DOCUMENT_POSITION_FOLLOWING)).reverse();
    return c.filter(el => /user|email|login|account|ident/i.test(el.name + ' ' + el.id + ' ' + el.type));
  });
  const otp = pick(a.sel.totp, () => deepAll('input').filter(el =>
    ['text', 'tel', 'number', ''].includes(kind(el))
    && (el.autocomplete === 'one-time-code' || /otp|totp|2fa|mfa|code|token/i.test(el.name + ' ' + el.id))));

  const ok = {
    username: el => TEXTY.includes(kind(el)),
    password: el => kind(el) === 'password',
    totp: el => ['text', 'tel', 'number'].includes(kind(el)),
  };
  const target = {username: user, password: pwd, totp: otp};
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
  const filled = [], missing = [], refused = [];
  for (const f of a.fields) {
    const el = target[f];
    if (!el) { missing.push(f); continue; }
    if (!ok[f](el)) { refused.push(f + ' (field is type ' + (kind(el) || el.tagName.toLowerCase()) + ')'); continue; }
    el.focus();
    setter.call(el, a.values[f]);
    el.dispatchEvent(new Event('input', {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    filled.push(f);
  }
  let submitted = false, submit_why = '';
  if (a.submit && filled.length) {
    const form = (pwd || otp || user || {}).form;
    if (!form) submit_why = 'no <form> around the fields; click the submit button';
    else {
      try { form.requestSubmit ? form.requestSubmit() : form.submit(); submitted = true; }
      catch (e) { submit_why = 'form refused to submit'; }
    }
  }
  return JSON.stringify({ok: filled.length > 0 && !refused.length, filled, missing, refused,
                         submitted, submit_why});
})"""


async def _eval(tab: Any, expression: str, timeout_s: float = 5.0) -> Any:
    async def run() -> Any:
        tree = await tab.send(cdp.page.get_frame_tree())
        ctx = await tab.send(cdp.page.create_isolated_world(
            frame_id=tree.frame.id_, world_name="umbra-vault"))
        remote, exc = await tab.send(cdp.runtime.evaluate(
            expression=expression, context_id=ctx, return_by_value=True))
        # Exception details are dropped on purpose: their text can quote source.
        if exc is not None:
            raise VaultError("vault page script failed")
        return json.loads(remote.value)
    return await asyncio.wait_for(run(), timeout_s)


async def page_state(tab: Any) -> dict[str, Any]:
    """{origin, t}: t is performance.timeOrigin (epoch ms) of the current document."""
    return await _eval(tab, _STATE_JS)


async def holds(tab: Any, values: list[str]) -> dict[str, Any]:
    return await _eval(tab, f"{_HOLDS_JS}({json.dumps(values)})")


async def fill(tab: Any, *, origin: str, fields: list[str], values: dict[str, str],
               selectors: dict[str, str | None], submit: bool) -> dict[str, Any]:
    args = {"origin": origin, "fields": fields, "values": values,
            "sel": selectors, "submit": submit}
    return await _eval(tab, f"{_FILL_JS}({json.dumps(args)})")
