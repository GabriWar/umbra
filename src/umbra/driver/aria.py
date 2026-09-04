"""Accessibility-tree-first driver — zero mouse telemetry.

Uses the CDP `Accessibility` domain (`getFullAXTree`) to enumerate interactive
elements, then activates them via `element.focus() + keyboard.press(Enter|Space)`
or DOM `el.click()`. No mouse coords are ever emitted by this driver.

Why this defeats behavioral fingerprinting: detection scripts that hash mouse
movement patterns, click position relative to button center, scroll velocity,
or hover heatmaps see literally nothing — there is no pointer interaction.
The accessibility API channel is what screen readers use; sites are legally
obligated to support it under WCAG / ADA / EU Accessibility Act, so blocking
it == blocking disabled users == legal exposure.

Pattern ported from Huzy85/fantoma (MIT). Uses nodriver/CDP instead of
Camoufox/Playwright as the underlying transport.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Any

import nodriver as uc

log = logging.getLogger("umbra.driver.aria")


# AX roles we treat as interactive — match fantoma's set.
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "combobox", "searchbox", "checkbox",
    "radio", "switch", "menuitem", "menuitemcheckbox", "menuitemradio",
    "tab", "option", "slider", "spinbutton", "treeitem",
    # Date pickers and calendar widgets expose their days as gridcells. Left
    # out, the whole calendar was invisible and the only way to pick a date
    # was guessing library-specific CSS classes.
    "gridcell",
})

# Roles that group structure — useful as landmarks in the rendered tree.
_LANDMARK_ROLES = frozenset({
    "main", "navigation", "banner", "complementary", "contentinfo",
    "form", "search", "region", "article",
})

# Roles whose whole point is an on/off position.
_TOGGLE_ROLES = frozenset({"checkbox", "radio", "switch", "menuitemcheckbox",
                            "menuitemradio"})

# Roles that hold a *value* a caller may want to fill — the `only='form'` alias.
_FIELD_ROLES = frozenset({
    "textbox", "searchbox", "combobox", "listbox", "checkbox", "radio",
    "switch", "slider", "spinbutton",
})

# Builds a unique CSS selector for `this`. Shared by selector_for/describe_field
# so an idx always resolves to the same element both report.
_SELECTOR_JS = """
    const esc = s => (window.CSS && CSS.escape) ? CSS.escape(s) : s;
    const unique = sel => {
        try { return document.querySelectorAll(sel).length === 1; }
        catch (e) { return false; }
    };
    const selectorFor = (node) => {
        if (node.id) return '#' + esc(node.id);
        // A name is only a selector if it identifies ONE element. Radio groups
        // and checkbox groups share theirs, so `input[name="size"]` would point
        // at whichever came first — pick the value out of the group instead.
        if (node.name && node.form) {
            const tag = node.tagName.toLowerCase();
            const byName = tag + '[name="' + node.name + '"]';
            if (unique(byName)) return byName;
            if (node.value) {
                const byValue = byName + '[value="' + node.value + '"]';
                if (unique(byValue)) return byValue;
            }
        }
        const path = [];
        let el = node;
        while (el && el.nodeType === 1 && path.length < 6) {
            let part = el.tagName.toLowerCase();
            if (el.id) { path.unshift('#' + esc(el.id)); break; }
            const parent = el.parentElement;
            if (parent) {
                const sibs = Array.from(parent.children)
                    .filter(c => c.tagName === el.tagName);
                if (sibs.length > 1)
                    part += ':nth-of-type(' + (sibs.indexOf(el) + 1) + ')';
            }
            path.unshift(part);
            el = el.parentElement;
        }
        return path.join(' > ');
    };
"""


# Flip a toggle the way `set_field` does: through the prototype's own setter,
# then the events a framework-controlled component listens for.
_FORCE_TOGGLE_JS = """function() {
    const d = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'checked');
    const want = !this.checked;
    if (d && d.set) d.set.call(this, want); else this.checked = want;
    for (const t of ['click', 'input', 'change'])
        this.dispatchEvent(new Event(t, {bubbles: true}));
    return this.checked;
}"""


@dataclass
class AxNode:
    """One element from the accessibility tree."""
    idx: int
    node_id: int
    role: str
    name: str
    value: str | None
    description: str | None
    properties: dict[str, Any]
    backend_node_id: int | None = None

    def render(self) -> str:
        """One-line representation for an LLM prompt."""
        bits = [f"[{self.idx}]", self.role]
        if self.name:
            bits.append(f'"{self.name}"')
        if self.value:
            bits.append(f'(value: "{self.value}")')
        flags = []
        if self.properties.get("required"):
            flags.append("required")
        if self.properties.get("invalid") and self.properties["invalid"] != "false":
            flags.append(f"invalid: \"{self.properties.get('errormessage', '')}\"")
        if self.properties.get("disabled"):
            flags.append("disabled")
        # Always state a toggle's position. Showing the flag only when checked
        # made "unchecked" and "not a toggle at all" render identically, so a
        # caller could not verify a selection from the tree alone.
        if self.role in _TOGGLE_ROLES:
            state = self.properties.get("checked")
            flags.append(f"checked={'true' if state in (True, 'true') else state or 'false'}")
        elif self.properties.get("checked") in (True, "true", "mixed"):
            flags.append(f"checked={self.properties['checked']}")
        if flags:
            bits.append("[" + ", ".join(flags) + "]")
        return " ".join(bits)


class AriaDriver:
    """Drive a tab via its accessibility tree."""

    def __init__(self, tab: Any):
        self.tab = tab
        self._index: dict[int, AxNode] = {}
        self._parent_of: dict[str, str] = {}
        self._node_of_backend: dict[int, str] = {}
        self._cdp = uc.cdp

    async def snapshot(self) -> list[AxNode]:
        """Build a fresh interactive-element index from the AX tree.

        Always fresh — no caching. Pages change too unpredictably (XHR loads,
        modal opens, navigation) and stale snapshots cause silent wrong-element
        clicks. Cost is ~50ms which is acceptable.
        """
        try:
            await self.tab.send(self._cdp.accessibility.enable())
        except Exception:  # noqa: BLE001
            pass

        result = await self.tab.send(self._cdp.accessibility.get_full_ax_tree())
        # nodriver returns a list of AXNode CDP types (or sometimes a dict with
        # a `nodes` key on older versions). Handle both, access via getattr.
        nodes = result if isinstance(result, list) else getattr(result, "nodes", []) or []

        def _val(thing: Any, default: Any = None) -> Any:
            """Pull .value off CDP value-wrapped objects, fall back to thing."""
            if thing is None:
                return default
            if hasattr(thing, "value"):
                return thing.value
            if isinstance(thing, dict):
                return thing.get("value", default)
            return thing

        # Ancestry for EVERY node, interactive or not: `within=` scopes a
        # snapshot to one container, and containers are usually generic divs
        # that never make the interactive list themselves.
        self._parent_of, self._node_of_backend = {}, {}
        for raw in nodes:
            nid = getattr(raw, "node_id", None) or getattr(raw, "nodeId", 0)
            pid = getattr(raw, "parent_id", None) or getattr(raw, "parentId", None)
            bid = (getattr(raw, "backend_dom_node_id", None)
                   or getattr(raw, "backendDOMNodeId", None))
            if nid:
                if pid:
                    self._parent_of[str(nid)] = str(pid)
                if bid:
                    self._node_of_backend[int(bid)] = str(nid)

        interactive: list[AxNode] = []
        for raw in nodes:
            role = _val(getattr(raw, "role", None), "")
            if role not in _INTERACTIVE_ROLES:
                continue
            if getattr(raw, "ignored", False):
                continue

            name = _val(getattr(raw, "name", None), "") or ""
            value = _val(getattr(raw, "value", None), None)
            desc = _val(getattr(raw, "description", None), None)

            props: dict[str, Any] = {}
            for prop in getattr(raw, "properties", None) or []:
                pname = getattr(prop, "name", None) or (prop.get("name") if isinstance(prop, dict) else "")
                pval = _val(getattr(prop, "value", None) if not isinstance(prop, dict) else prop.get("value"))
                if pname:
                    # CDP hands back an AXPropertyName enum, so str() is
                    # "AXPropertyName.CHECKED" — every lookup by "checked"
                    # missed, which is why required/disabled/checked flags
                    # never once showed up in a rendered tree.
                    props[str(pname).rsplit(".", 1)[-1].lower()] = pval

            node_id = getattr(raw, "node_id", None) or getattr(raw, "nodeId", 0)
            backend = getattr(raw, "backend_dom_node_id", None) or getattr(raw, "backendDOMNodeId", None)

            ax = AxNode(
                idx=len(interactive), node_id=int(node_id),
                role=role, name=name or "", value=value,
                description=desc, properties=props,
                backend_node_id=int(backend) if backend else None,
            )
            interactive.append(ax)

        self._index = {n.idx: n for n in interactive}
        return interactive

    def _is_descendant(self, node: AxNode, root_node_id: str) -> bool:
        """Walk an AX node's ancestry looking for `root_node_id`."""
        seen, cur = set(), str(node.node_id)
        while cur and cur not in seen:
            if cur == root_node_id:
                return True
            seen.add(cur)
            cur = self._parent_of.get(cur, "")
        return False

    async def scope_to(self, target: str) -> tuple[str | None, str | None]:
        """Resolve a container to the AX node id that roots its subtree.

        `target` is a CSS selector, or an existing snapshot idx. Returns
        (node_id, error) — the caller reports the error rather than silently
        snapshotting the whole page, which is what makes a scoped snapshot
        trustworthy.
        """
        if target.strip().lstrip("-").isdigit():
            node = self._index.get(int(target))
            if not node:
                return None, f"idx {target} is not in the current snapshot"
            return str(node.node_id), None
        try:
            doc = await self.tab.send(self._cdp.dom.get_document())
            dom_id = await self.tab.send(self._cdp.dom.query_selector(
                node_id=doc.node_id, selector=target))
            if not dom_id:
                return None, f"no element matches {target!r}"
            described = await self.tab.send(self._cdp.dom.describe_node(node_id=dom_id))
            desc = described[0] if isinstance(described, tuple) else described
            backend = getattr(desc, "backend_node_id", None)
        except Exception as e:  # noqa: BLE001
            return None, f"could not resolve {target!r}: {str(e)[:120]}"
        root = self._node_of_backend.get(int(backend)) if backend else None
        if not root:
            return None, (f"{target!r} exists in the DOM but has no accessibility "
                          f"node — scope to a parent, or drop `within`")
        return root, None

    def render_tree(self, max_items: int = 60,
                    only: frozenset[str] | set[str] | None = None,
                    extra: dict[int, dict[str, Any]] | None = None,
                    keep: set[int] | None = None) -> str:
        """Pretty-print the snapshot for an LLM, with run-length grouping.

        Detects repeating cycles of (role, name) signatures across consecutive
        elements and collapses them. Lossless: indexes preserve their meaning,
        and any idx in the range can still be used by `aria_click` / `aria_type`.

        `only` keeps just those roles (indexes are untouched, so a filtered
        tree still drives every idx-based tool); `keep` narrows to a set of
        indexes, which is how a caller scopes to one container's subtree;
        `extra` appends per-idx field detail from `describe_fields`. All
        default off — the unfiltered tree is what a caller gets unless they
        ask for less.

        Output examples:
          [12-77] cycle×13: link("Comments"), link("Permalink"), link("Save"),
                            link("Reply"), link("Report")
            ↳ 66 elements at indexes 12..77 are 13 reps of the cycle of 5

          [3-9] repeat×7: button("delete")
            ↳ 7 identical "delete" buttons at indexes 3..9
        """
        if not self._index:
            return "(no interactive elements — page may still be loading)"

        nodes = [n for n in self._index.values()
                 if (only is None or n.role in only)
                 and (keep is None or n.idx in keep)]
        nodes = nodes[:max_items]

        def sig(n: "AxNode") -> tuple[str, str]:
            return (n.role, n.name or "")

        def render_one(n: "AxNode") -> str:
            line = n.render()
            info = (extra or {}).get(n.idx)
            if info:
                bits = [f"sel={info['selector']}"] if info.get("selector") else []
                # Only worth printing when the AX tree left the node nameless —
                # that is exactly the case a caller cannot otherwise resolve.
                if not n.name and info.get("label"):
                    bits.insert(0, f"label={info['label']}")
                for key in ("type", "placeholder", "pattern", "maxlength"):
                    if info.get(key):
                        bits.append(f"{key}={info[key]}")
                for flag in ("required", "disabled", "readonly"):
                    if info.get(flag):
                        bits.append(flag)
                if info.get("options"):
                    shown = info["options"][:12]
                    total = info.get("options_total", len(info["options"]))
                    more = total - len(shown)
                    bits.append("options=" + ",".join(shown)
                                + (f" (+{more} more)" if more > 0 else ""))
                if bits:
                    line += " {" + " ".join(bits) + "}"
            return line

        def fmt_cycle_item(s: tuple[str, str]) -> str:
            role, name = s
            return f'{role}({name!r})' if name else role

        # Walk and detect runs. Strategy:
        #   for each position i, find the longest cycle of period p where
        #   sig[i:i+p] == sig[i+p:i+2p] == ... for k>=2 reps. Greedy: try
        #   p=1..6 (most real-world repeats are short cycles), pick longest
        #   total run.
        out: list[str] = []
        i = 0
        n = len(nodes)
        while i < n:
            best_p, best_reps = 0, 0
            sigs = [sig(nodes[j]) for j in range(i, min(i + 12, n))]  # sample window
            for p in range(1, min(7, len(sigs))):
                # How many full reps starting at i with period p?
                if i + 2 * p > n:
                    break
                cycle = [sig(nodes[i + k]) for k in range(p)]
                reps = 1
                while i + (reps + 1) * p <= n:
                    nxt = [sig(nodes[i + reps * p + k]) for k in range(p)]
                    if nxt != cycle:
                        break
                    reps += 1
                # Need at least 2 full reps to be worth grouping
                if reps >= 2 and reps * p > best_reps * best_p:
                    best_p, best_reps = p, reps
            # Grouping claims "these are the same thing repeated", and the
            # only evidence for that is a shared NAME. Nameless nodes have
            # none: three unlabelled comboboxes are Subjects, State and City,
            # and collapsing them into `[18-20] repeat×3: combobox` invites
            # the caller to act on the wrong one.
            if best_p and best_reps >= 2 and any(
                    not nodes[i + k].name for k in range(best_p)):
                best_p, best_reps = 0, 0
            # Never collapse a run we have field detail for — the whole point
            # of `extra` is a per-element selector, which a group line drops.
            if best_p and best_reps >= 2 and extra:
                span = range(i, i + best_p * best_reps)
                if any(nodes[j].idx in extra for j in span):
                    best_p, best_reps = 0, 0
            if best_p and best_reps >= 2:
                cycle = [sig(nodes[i + k]) for k in range(best_p)]
                start_idx = nodes[i].idx
                end_idx = nodes[i + best_p * best_reps - 1].idx
                if best_p == 1:
                    out.append(f"[{start_idx}-{end_idx}] repeat×{best_reps}: {fmt_cycle_item(cycle[0])}")
                else:
                    items = ", ".join(fmt_cycle_item(s) for s in cycle)
                    out.append(f"[{start_idx}-{end_idx}] cycle×{best_reps} (period {best_p}): {items}")
                i += best_p * best_reps
            else:
                out.append(render_one(nodes[i]))
                i += 1
        return "\n".join(out)

    async def _resolve_node(self, idx: int) -> Any:
        """Get a remote object handle to the DOM node behind an AX node."""
        if idx not in self._index:
            raise ValueError(f"No element at index {idx} — snapshot first")
        node = self._index[idx]
        if not node.backend_node_id:
            raise ValueError(f"AX node {idx} has no DOM backing — cannot interact")
        result = await self.tab.send(self._cdp.dom.resolve_node(
            backend_node_id=node.backend_node_id,
        ))
        return result

    async def _object_id(self, idx: int) -> str | None:
        """RemoteObjectId for the DOM node behind an AX index, or None."""
        node = self._index.get(idx)
        if not node or not node.backend_node_id:
            return None
        try:
            res = await self.tab.send(self._cdp.dom.resolve_node(
                backend_node_id=self._cdp.dom.BackendNodeId(node.backend_node_id),
            ))
        except Exception as e:  # noqa: BLE001
            log.debug("resolve_node failed at idx=%d: %s", idx, e)
            return None
        # nodriver returns a RemoteObject (or a (RemoteObject, exc) tuple).
        obj = res[0] if isinstance(res, tuple) else res
        return getattr(obj, "object_id", None)

    async def _call_on(self, object_id: str, fn: str) -> Any:
        """Runtime.callFunctionOn(fn, object_id) → plain Python value."""
        res = await self.tab.send(self._cdp.runtime.call_function_on(
            function_declaration=fn, object_id=object_id, return_by_value=True,
        ))
        obj = res[0] if isinstance(res, tuple) else res
        return getattr(obj, "value", None)

    @staticmethod
    def _context_gone(exc: Exception) -> bool:
        """True when a CDP call failed because the page navigated away.

        Chrome tears down the execution context the moment a navigation
        commits, so any Runtime call still in flight fails with one of these.
        On a submit button that is not an error — it is proof the click
        worked — so the caller must be able to tell the two apart.
        """
        msg = str(exc).lower()
        return any(s in msg for s in (
            "cannot find context",
            "execution context was destroyed",
            "inspected target navigated",
            "context with specified id",
        ))

    async def click(self, idx: int) -> dict[str, Any]:
        """Activate element, verifying the activation actually landed.

        Zero mouse telemetry either way — the fallback is `el.click()`, which
        dispatches a trusted-shaped click event without emitting any pointer
        coordinates, so behavioral fingerprinting still sees nothing.

        Why the verification exists: `DOM.focus` + Enter/Space activates native
        controls, but a large class of real-world widgets (React/Vue components
        that bind onClick to a div, custom comboboxes, label-wrapped visually
        hidden radios) ignore synthetic key events entirely. The old
        implementation returned True in exactly that case — reporting success
        while the page never changed, which is worse than failing loudly.

        Strategy: arm a one-shot click listener on the element, try the
        keyboard path, then check whether a click event actually fired. If it
        did not, call `el.click()` directly and re-check.

        Returns {ok, navigated, url?, why?}. A click that navigates (submit
        buttons, links) destroys the execution context the probe lives in —
        that used to surface as a raw `Cannot find context` CDP error even
        though the click had plainly worked, so the caller had to burn a
        `current_state` call to find out whether anything happened.
        """
        node = self._index.get(idx)
        if not node or not node.backend_node_id:
            return {"ok": False, "navigated": False,
                    "why": f"idx {idx} not in the current snapshot — re-snapshot"}
        url_before = None
        with contextlib.suppress(Exception):
            url_before = await self.tab.evaluate("location.href")
        before_checked: bool | None = None
        is_toggle = node.role in {"checkbox", "radio", "switch"}
        key = "Space" if is_toggle else "Enter"

        object_id = await self._object_id(idx)

        if node.role in _TOGGLE_ROLES and object_id:
            before_checked = await self._read_checked(object_id)

        # Does this element plausibly navigate? Asked BEFORE the click, while
        # the context is guaranteed alive, so an ordinary in-page button never
        # pays for the post-click settle below.
        may_navigate = False
        if object_id:
            with contextlib.suppress(Exception):
                may_navigate = bool(await self._call_on(object_id, """function() {
                    const tag = this.tagName;
                    if (tag === 'A') return !!this.href;
                    const t = (this.type || '').toLowerCase();
                    // A <button> in a form defaults to type=submit.
                    if (tag === 'BUTTON') return !!this.form && t !== 'button';
                    if (tag === 'INPUT') return t === 'submit' || t === 'image';
                    return false;
                }"""))

        # Arm the probe. If we can't resolve the node, fall through to the
        # legacy keyboard-only path rather than failing the call outright.
        if object_id:
            with contextlib.suppress(Exception):
                await self._call_on(object_id, """function() {
                    this.__umbraClicked = false;
                    this.__umbraProbe = () => { this.__umbraClicked = true; };
                    this.addEventListener('click', this.__umbraProbe, {capture: true});
                    return true;
                }""")

        try:
            await self.tab.send(self._cdp.dom.focus(
                backend_node_id=self._cdp.dom.BackendNodeId(node.backend_node_id),
            ))
            await self.tab.send(self._cdp.input_.dispatch_key_event(
                type_="keyDown", key=key, code=key,
            ))
            await self.tab.send(self._cdp.input_.dispatch_key_event(
                type_="keyUp", key=key, code=key,
            ))
            keyboard_ok = True
        except Exception as e:  # noqa: BLE001
            log.debug("aria keyboard activation failed at idx=%d: %s", idx, e)
            keyboard_ok = False

        if not object_id:
            # No probe available — preserve old behavior.
            return {"ok": keyboard_ok, "navigated": False}

        async def probe() -> tuple[bool, bool]:
            """(clicked, navigated) — a dead context means we navigated."""
            try:
                got = await self._call_on(
                    object_id, "function() { return !!this.__umbraClicked; }")
            except Exception as e:  # noqa: BLE001
                if self._context_gone(e):
                    return True, True
                log.debug("aria click probe failed at idx=%d: %s", idx, e)
                return False, False
            return bool(got), False

        fired, navigated = await probe()

        if not fired and not navigated:
            # Keyboard did nothing. Escalate to a direct DOM activation, which
            # is what custom widget handlers actually listen for.
            try:
                await self._call_on(object_id, "function() { this.click(); return true; }")
            except Exception as e:  # noqa: BLE001
                if self._context_gone(e):
                    fired, navigated = True, True
            if not navigated:
                fired, navigated = await probe()
                if fired:
                    log.debug("aria click idx=%d needed el.click() fallback", idx)

        if not navigated:
            with contextlib.suppress(Exception):
                await self._call_on(object_id, """function() {
                    if (this.__umbraProbe)
                        this.removeEventListener('click', this.__umbraProbe, {capture: true});
                    delete this.__umbraProbe; delete this.__umbraClicked;
                    return true;
                }""")

        # The probe can also answer BEFORE the navigation commits, in which
        # case nothing threw and the click still took us elsewhere. Compare
        # the location to catch that half of the race — giving the elements
        # that can navigate a brief settle, since the request is often still
        # in flight the instant the click returns. Everything else (toggles,
        # in-page buttons) skips the wait entirely.
        url_after = None
        with contextlib.suppress(Exception):
            url_after = await self.tab.evaluate("location.href")
        if not navigated and url_after == url_before and may_navigate:
            for _ in range(4):
                await asyncio.sleep(0.1)
                try:
                    url_after = await self.tab.evaluate("location.href")
                except Exception as e:  # noqa: BLE001
                    if self._context_gone(e):
                        navigated, fired = True, True
                    break
                if url_after != url_before:
                    break
        if not navigated and url_after and url_before and url_after != url_before:
            navigated, fired = True, True

        # A toggle that reports a click but never changed state is the most
        # expensive lie this tool can tell. Material-style components own
        # their state and ignore a synthetic click on the native input: the
        # click event fires, so `fired` is true, while the box stays as it
        # was. Verify; if it really did not move, write the value the way
        # set_field does — native setter plus input/change, which is what
        # framework-controlled inputs actually listen for.
        toggled: bool | None = None
        if node.role in _TOGGLE_ROLES and object_id and not navigated:
            after = await self._read_checked(object_id)
            if before_checked is not None and after == before_checked:
                with contextlib.suppress(Exception):
                    await self._call_on(object_id, _FORCE_TOGGLE_JS)
                after = await self._read_checked(object_id)
                if after != before_checked:
                    log.debug("aria click idx=%d needed the native-setter path", idx)
            toggled = after
            # For a toggle, "ok" can only mean the toggle moved. A click event
            # that the component swallowed is not a success by any definition
            # the caller cares about.
            fired = after != before_checked

        out: dict[str, Any] = {"ok": bool(fired), "navigated": navigated}
        if toggled is not None:
            out["checked"] = toggled
            if not fired:
                out["why"] = (f"the click fired but {node.role!r} stayed "
                              f"checked={toggled} — the component rejects "
                              f"programmatic input; try set_field with its "
                              f"selector, or click_at on its label")
        if navigated:
            if url_after:
                out["url"] = url_after
            else:
                with contextlib.suppress(Exception):
                    out["url"] = await self.tab.evaluate("location.href")
        elif not fired:
            log.warning(
                "aria click at idx=%d did not activate the element "
                "(neither key press nor el.click() produced a click event)", idx)
            out["why"] = ("element did not activate — neither the key press nor "
                          "el.click() produced a click event; re-snapshot and "
                          "check the idx")
        return out

    async def _read_checked(self, object_id: str) -> bool | None:
        """Current on/off state, however the widget chooses to express it."""
        with contextlib.suppress(Exception):
            return await self._call_on(object_id, """function() {
                if (typeof this.checked === 'boolean') return this.checked;
                const a = this.getAttribute('aria-checked');
                return a === null ? null : a === 'true';
            }""")
        return None

    async def rect(self, idx: int, *, scroll_into_view: bool = True) -> dict[str, Any] | None:
        """Viewport rect + center point for an ARIA index.

        Scrolls the element into view first so the coordinates are actually
        clickable — an off-screen element has a rect, but clicking it lands on
        whatever happens to occupy that spot. Returns None if the element is
        not resolvable or has no box (display:none, detached).
        """
        object_id = await self._object_id(idx)
        if not object_id:
            return None
        fn = """function() {
            %s
            const r = this.getBoundingClientRect();
            if (!r.width && !r.height) return null;
            return {x: r.x, y: r.y, w: r.width, h: r.height,
                    cx: Math.round(r.x + r.width / 2),
                    cy: Math.round(r.y + r.height / 2)};
        }""" % ("this.scrollIntoView({block: 'center', inline: 'center'});"
                if scroll_into_view else "")
        try:
            return await self._call_on(object_id, fn)
        except Exception as e:  # noqa: BLE001
            log.debug("rect failed at idx=%d: %s", idx, e)
            return None

    async def selector_for(self, idx: int) -> str | None:
        """Build a unique CSS selector for the element behind an ARIA index.

        Lets index-based discovery feed selector-based tools (`set_field`)
        without the caller hand-writing a selector and hoping it matches the
        same element the snapshot showed them.
        """
        object_id = await self._object_id(idx)
        if not object_id:
            return None
        fn = "function() {" + _SELECTOR_JS + " return selectorFor(this); }"
        try:
            return await self._call_on(object_id, fn)
        except Exception as e:  # noqa: BLE001
            log.debug("selector_for failed at idx=%d: %s", idx, e)
            return None

    async def describe_field(self, idx: int) -> dict[str, Any] | None:
        """Everything needed to FILL the element behind an ARIA index.

        The AX tree names a control but never says what it accepts: `textbox
        "name@example.com"` hides whether that is type=email, type=tel or a
        date picker, and a combobox hides its option list. Callers that guess
        wrong burn a round-trip on a rejected value, so ship the input type,
        the required flag, the current value and (for selects) the options
        alongside the selector.
        """
        object_id = await self._object_id(idx)
        if not object_id:
            return None
        fn = "function() {" + _SELECTOR_JS + """
            const tag = this.tagName.toLowerCase();
            const out = {selector: selectorFor(this), tag: tag};
            // A react-select renders as a nameless <input> inside a wrapper
            // that carries the identity (<div id="state">), so the AX tree
            // shows bare `combobox` and three of them on a page are
            // indistinguishable. Recover a human label from wherever the
            // markup happens to keep it.
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
            let label = '';
            if (this.labels && this.labels[0]) label = clean(this.labels[0].textContent);
            if (!label) label = clean(this.getAttribute('aria-label'));
            if (!label) {
                const by = this.getAttribute('aria-labelledby');
                const ref = by && document.getElementById(by);
                if (ref) label = clean(ref.textContent);
            }
            if (!label) label = clean(this.placeholder);
            if (!label && this.parentElement) {
                // From the PARENT up: the input carries its own generated id
                // (react-select-3-input), the wrapper carries the meaning
                // (<div id="state">).
                const box = this.parentElement.closest('[id]');
                if (box) label = clean(box.id);
            }
            if (label) out.label = label;
            const type = tag === 'select'
                ? (this.multiple ? 'select-multiple' : 'select')
                : (this.type || '').toLowerCase();
            if (type) out.type = type;
            if (this.required) out.required = true;
            if (this.disabled) out.disabled = true;
            if (this.readOnly) out.readonly = true;
            if (this.maxLength > 0) out.maxlength = this.maxLength;
            if (this.pattern) out.pattern = this.pattern;
            if (this.placeholder) out.placeholder = this.placeholder;
            if (type === 'checkbox' || type === 'radio') out.checked = !!this.checked;
            else if (this.value) out.value = String(this.value).slice(0, 120);
            if (tag === 'select') {
                out.options = Array.from(this.options)
                    .slice(0, 50).map(o => o.text.trim());
                // A select holding ONE dead placeholder and one holding 200
                // countries rendered identically once the list was clipped.
                out.options_total = this.options.length;
            }
            return out;
        }"""
        try:
            res = await self._call_on(object_id, fn)
        except Exception as e:  # noqa: BLE001
            log.debug("describe_field failed at idx=%d: %s", idx, e)
            return None
        return res if isinstance(res, dict) else None

    async def describe_fields(self, idxs: list[int]) -> dict[int, dict[str, Any]]:
        """`describe_field` for many indexes — CDP calls, no MCP round-trips."""
        out: dict[int, dict[str, Any]] = {}
        for i in idxs:
            info = await self.describe_field(i)
            if info:
                out[i] = info
        return out

    async def find_all_by_text(self, text: str, *, role_hint: str | None = None,
                               limit: int = 20) -> list[dict[str, Any]]:
        """Every element matching `text`, best first — not just the top hit.

        `find_by_text` collapses to a single index, which silently picks for you
        when a page has several plausible matches (three "+ Add" buttons, say).
        This returns the candidates so the caller can disambiguate by role or
        surrounding name instead of guessing.
        """
        await self.snapshot()
        target = text.lower().strip()
        scored: list[tuple[int, dict[str, Any]]] = []
        for n in self._index.values():
            if role_hint and n.role != role_hint:
                continue
            for hay in (n.name, n.value or ""):
                if not hay:
                    continue
                hl = hay.lower()
                if target == hl:
                    score = 1000
                elif target in hl:
                    score = 100 - abs(len(hl) - len(target))
                else:
                    continue
                scored.append((score, {"idx": n.idx, "role": n.role,
                                       "name": n.name[:80], "score": score}))
                break
        scored.sort(key=lambda t: -t[0])
        picked = [d for _, d in scored[:limit]]
        # Same-label duplicates are the whole reason to call this instead of
        # find_by_text, and the only question that matters about them is which
        # one is reachable. Answering it here saves an element_rect per
        # candidate just to find that out.
        if len(picked) > 1:
            best = await self._most_reachable([d["idx"] for d in picked])
            for d in picked:
                d["on_screen"] = await self._is_on_screen(d["idx"])
            picked.sort(key=lambda d: (not d.get("on_screen"), -d["score"]))
            for d in picked:
                if d["idx"] == best:
                    d["best"] = True
        return picked

    async def _is_on_screen(self, idx: int) -> bool:
        object_id = await self._object_id(idx)
        if not object_id:
            return False
        with contextlib.suppress(Exception):
            return bool(await self._call_on(object_id, """function() {
                const r = this.getBoundingClientRect();
                return !!(r.width || r.height) && r.bottom > 0 && r.right > 0
                    && r.top < innerHeight && r.left < innerWidth
                    && getComputedStyle(this).visibility !== 'hidden';
            }"""))
        return False

    async def type(self, idx: int, text: str, *, clear: bool = True, jitter: bool = True) -> bool:
        """Focus, optionally clear, then type with humanized log-normal delays."""
        from umbra.stealth.humanizer import keystroke_delay, FAST_TYPIST

        node = self._index.get(idx)
        if not node or not node.backend_node_id:
            return False
        try:
            await self.tab.send(self._cdp.dom.focus(
                backend_node_id=self._cdp.dom.BackendNodeId(node.backend_node_id),
            ))
            if clear:
                # Clearing an ALREADY-empty field is not a no-op on widgets
                # that treat deletion as "remove the previous thing": a
                # react-select multi-input eats the last committed tag, so
                # typing a second value silently destroys the first. Nothing
                # reports it — the tag is just gone a few calls later.
                with contextlib.suppress(Exception):
                    object_id = await self._object_id(idx)
                    if object_id and not await self._call_on(object_id, """function() {
                        return (this.value || this.textContent || '').length > 0;
                    }"""):
                        clear = False
            if clear:
                # Ctrl+A then Delete via CDP — no mouse, no JS eval.
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_="keyDown", key="a", code="KeyA", modifiers=2,
                ))
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_="keyUp", key="a", code="KeyA", modifiers=2,
                ))
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_="keyDown", key="Delete", code="Delete",
                ))
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_="keyUp", key="Delete", code="Delete",
                ))
            prev = ""
            for ch in text:
                if jitter:
                    await asyncio.sleep(keystroke_delay(prev, ch, FAST_TYPIST))
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_="keyDown", text=ch, key=ch, unmodified_text=ch,
                ))
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_="keyUp", key=ch,
                ))
                prev = ch
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("aria type failed at idx=%d: %s", idx, e)
            return False

    # Finds the items of whatever menu is currently open. Roles first, because
    # that is what a well-behaved widget exposes — but real ones disagree:
    # react-select uses role=option, this vintage of select2 uses treeitem,
    # and jQuery UI's multiselect uses a bare <ul> with no roles at all. So
    # fall back to list items inside a visible menu-ish container.
    _MENU_ITEMS_JS = """
        const vis = el => el.offsetParent !== null || el.getClientRects().length;
        const byRole = [...document.querySelectorAll(
            '[role="option"],[role="treeitem"],[role="menuitem"],'
            + '[role="menuitemradio"],[role="menuitemcheckbox"]')].filter(vis);
        const menuish = '[class*="dropdown"],[class*="results"],[class*="menu"],'
            + '[class*="autocomplete"],[class*="options"],[role="listbox"]';
        // Only OVERLAY containers: a dropdown is positioned out of flow, while
        // a site's static nav list also matches "menu" in its class name and
        // would otherwise be read as the open menu's options.
        const overlay = el => getComputedStyle(el).position !== 'static';
        const byList = byRole.length ? [] : [...document.querySelectorAll(menuish)]
            .filter(vis).filter(overlay)
            .flatMap(c => [...c.querySelectorAll('li,[class*="option"],[class*="item"]')])
            .filter(vis)
            .filter(el => !el.querySelector('li'));
        // "No results found" is the widget's empty state, not a choice.
        const EMPTY = /^(no |nenhum|sem )(results?|options?|matches|data)/i;
        const items = (byRole.length ? byRole : byList)
            .filter(el => (el.textContent || '').trim())
            .filter(el => !EMPTY.test((el.textContent || '').trim()));
    """

    # JSON.stringify because a bare array comes back as nodriver's CDP-wrapped
    # [{'type':'string','value':'NCR'}, …] rather than plain strings.
    _OPTIONS_JS = """JSON.stringify((() => {
        %s
        return items.slice(0, 60).map(o => (o.textContent || '').trim());
    })())""" % _MENU_ITEMS_JS

    _ORPHAN_WIDGETS_JS = """JSON.stringify((() => {
        const HINT = /(multiselect|autocomplete|combobox|dropdown|tagsinput|picker|typeahead|chosen|select2|tokenfield)/i;
        const vis = el => (el.offsetParent !== null || el.getClientRects().length)
                       && el.getBoundingClientRect().width > 20;
        const kept = [], out = [];
        for (const el of document.querySelectorAll('div,span,ul')) {
            if (out.length >= 6) break;
            if (el.getAttribute('role')) continue;          // ARIA already has it
            if (el.querySelector('input,select,textarea,button,a[href]')) continue;
            const cls = el.className && el.className.baseVal !== undefined
                ? el.className.baseVal : String(el.className || '');
            if (!HINT.test(cls) && !HINT.test(el.id || '')) continue;
            if (!vis(el)) continue;
            if (kept.some(k => k.contains(el))) continue;   // keep the outermost
            kept.push(el);
            const first = cls.trim().split(/\\s+/)[0];
            const clean = s => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 40);
            // These widgets are usually empty until opened, so their own text
            // says nothing — the surrounding form group's <label> is what
            // tells a caller which field this is.
            let text = clean(el.textContent);
            if (!text) {
                // Climb: in a grid layout the <label> sits in a SIBLING column,
                // so the widget's own container never holds it. Stop at the
                // first ancestor that contains one.
                let up = el.parentElement, lab = null;
                for (let i = 0; i < 4 && up && !lab; i++, up = up.parentElement)
                    lab = up.querySelector('label');
                text = clean(lab && lab.textContent) || clean(el.getAttribute('title'));
            }
            out.push({
                sel: el.id ? '#' + el.id
                    : el.tagName.toLowerCase() + (first ? '.' + first : ''),
                text: text,
            });
        }
        return out;
    })())"""

    async def orphan_widgets(self) -> list[dict[str, str]]:
        """Interactive-looking containers the accessibility tree never exposes.

        jQuery-era widgets are often a bare `<div class="…multiselect">` with
        no role and no native control inside, so they are absent from every
        ARIA snapshot — a caller reading the tree concludes the field does not
        exist and goes hunting through raw DOM. Naming them costs one JS pass.
        """
        with contextlib.suppress(Exception):
            raw = await self.tab.evaluate(self._ORPHAN_WIDGETS_JS)
            res = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(res, list):
                return [r for r in res if isinstance(r, dict)]
        return []

    async def combo_options(self, idx: int, filter_text: str = "",
                            *, timeout_s: float = 3.0) -> dict[str, Any]:
        """Open a custom combobox and list the options it is showing.

        Custom comboboxes (react-select and friends) render their menu only
        while open, so unlike a native `<select>` the choices cannot be read
        from the closed control — they have to be opened first. Typing
        `filter_text` narrows the menu the same way a person would.
        """
        node = self._index.get(idx)
        if not node:
            return {"ok": False, "why": f"idx {idx} not in the current snapshot"}
        # ArrowDown is the ARIA-standard "open the listbox" gesture and the one
        # combobox libraries all bind. A plain click lands on the inner input
        # and often leaves the menu shut, which is why listing came back empty.
        with contextlib.suppress(Exception):
            await self.tab.send(self._cdp.dom.focus(
                backend_node_id=self._cdp.dom.BackendNodeId(node.backend_node_id),
            ))
            for kind in ("keyDown", "keyUp"):
                await self.tab.send(self._cdp.input_.dispatch_key_event(
                    type_=kind, key="ArrowDown", code="ArrowDown",
                    windows_virtual_key_code=40,
                ))
        if filter_text:
            await self.type(idx, filter_text, clear=False, jitter=False)
        options = await self._poll_options(timeout_s)
        if not options:
            # jQuery-era widgets (select2 and friends) open on a real pointer
            # event and ignore both the keyboard gesture and el.click() — they
            # report success while the menu never renders. Fall back to an
            # actual mouse press at the control's own rect before giving up.
            box = await self.rect(idx)
            if box:
                with contextlib.suppress(Exception):
                    btn = self._cdp.input_.MouseButton("left")
                    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                        await self.tab.send(self._cdp.input_.dispatch_mouse_event(
                            type_=kind, x=box["cx"], y=box["cy"],
                            button=btn, click_count=1,
                        ))
                if filter_text:
                    await self.type(idx, filter_text, clear=False, jitter=False)
                options = await self._poll_options(timeout_s)
        return {"ok": bool(options), "options": options}

    async def _poll_options(self, timeout_s: float) -> list[str]:
        """Wait for a menu to render, then read its visible option labels."""
        deadline = asyncio.get_event_loop().time() + timeout_s
        while asyncio.get_event_loop().time() < deadline:
            with contextlib.suppress(Exception):
                raw = await self.tab.evaluate(self._OPTIONS_JS)
                res = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(res, list) and res:
                    return [str(o) for o in res]
            await asyncio.sleep(0.1)
        return []

    async def combo_select(self, idx: int, value: str,
                           *, timeout_s: float = 3.0) -> dict[str, Any]:
        """Pick `value` from a custom combobox: open, filter, click the option.

        The manual version of this is a five-call dance (click, type, snapshot
        to see the menu, click the option, verify) with two races in it: the
        menu needs a moment to render, and the option unmounts the instant it
        is chosen — reading it back in a separate call hits a null element.
        Doing the whole thing in one place removes both races.
        """
        # Name the element we actually touched: the usual cause of an empty
        # menu is a stale idx pointing at a different control, and the error
        # is where that becomes obvious without another snapshot.
        target = await self.selector_for(idx)
        listing = await self.combo_options(idx, value, timeout_s=timeout_s)
        if not listing["ok"]:
            # Nothing showing: either the filter matched nothing (the common
            # case — say so, and show what DOES exist) or this is not a
            # combobox at all.
            # The failed filter is still sitting in the input and would keep
            # the menu empty — wipe it before asking what the menu holds.
            # Escape first: on widgets whose search box is a DIFFERENT element
            # from the one we hold (select2 puts it inside the dropdown),
            # closing and reopening is the only way to reset the filter.
            with contextlib.suppress(Exception):
                for kind in ("keyDown", "keyUp"):
                    await self.tab.send(self._cdp.input_.dispatch_key_event(
                        type_=kind, key="Escape", code="Escape",
                        windows_virtual_key_code=27,
                    ))
            # Written through the native setter so React sees the reset.
            object_id = await self._object_id(idx)
            if object_id:
                with contextlib.suppress(Exception):
                    await self._call_on(object_id, """function() {
                        const d = Object.getOwnPropertyDescriptor(
                            HTMLInputElement.prototype, 'value');
                        if (d && d.set) d.set.call(this, ''); else this.value = '';
                        this.dispatchEvent(new Event('input', {bubbles: true}));
                        return true;
                    }""")
            unfiltered = await self.combo_options(idx, timeout_s=1.0)
            if unfiltered["ok"]:
                return {"ok": False, "target": target,
                        "why": f"no option matches {value!r}",
                        "options": unfiltered["options"]}
            # Autocomplete-style controls show nothing until the typed text
            # matches, so an empty menu here means the text matched nothing —
            # not that the control is the wrong kind.
            # Never blame the idx here: the menu also stays empty when the
            # widget only opens on a real pointer event, or when it filters to
            # nothing. Saying "wrong idx" sent a caller off re-verifying a
            # target that was correct all along.
            return {"ok": False, "target": target,
                    "why": f"the menu on {target or 'this element'} rendered no "
                           f"options for {value!r} — either nothing matches, or "
                           f"this widget is not menu-driven (try aria_click + "
                           f"aria_snapshot, or set_fields if it wraps a <select>)",
                    "options": []}
        want = json.dumps(value)
        picked = await self.tab.evaluate("""(() => {
            %s
            const want = %s.trim().toLowerCase();
            const txt = o => (o.textContent || '').trim();
            const hit = items.find(o => txt(o).toLowerCase() === want)
                     || items.find(o => txt(o).toLowerCase().startsWith(want))
                     || items.find(o => txt(o).toLowerCase().includes(want));
            if (!hit) return null;
            const label = txt(hit);
            // Widgets bind to different events: a plain .click() misses the
            // ones listening for mousedown/mouseup on the item.
            for (const t of ['mousedown', 'mouseup', 'click'])
                hit.dispatchEvent(new MouseEvent(t, {bubbles: true, cancelable: true}));
            return label;
        })()""" % (self._MENU_ITEMS_JS, want))
        if not picked:
            return {"ok": False, "why": f"no option matches {value!r}",
                    "options": listing["options"]}
        return {"ok": True, "picked": str(picked)}

    async def navigate(self, url: str) -> None:
        """Navigate the underlying tab — preserves the stealth payload."""
        await self.tab.get(url)

    # ──────────────────── LLM-friendly convenience methods ────────────────────

    async def find_by_text(self, text: str, *, role_hint: str | None = None,
                           fuzzy: bool = True) -> int | None:
        """Resolve "the button that says X" → ARIA index in one call.

        Refreshes snapshot, scores by name/value match. Returns the best idx
        or None if nothing matches well. fuzzy=True does substring + lowercase
        match; fuzzy=False requires exact name match.
        """
        await self.snapshot()
        target = text.lower().strip() if fuzzy else text.strip()
        exact: list[int] = []
        best_idx, best_score = None, 0
        for n in self._index.values():
            if role_hint and n.role != role_hint:
                continue
            for hay in (n.name, n.value or ""):
                if not hay:
                    continue
                hl = hay.lower() if fuzzy else hay
                if (target == hl) if fuzzy else (hay == text):
                    exact.append(n.idx)
                    break
                if fuzzy and target in hl:
                    score = 100 - abs(len(hl) - len(target))
                    if score > best_score:
                        best_idx, best_score = n.idx, score
        if len(exact) == 1:
            return exact[0]
        if exact:
            # Real pages carry duplicates of the same label — a dialog's button
            # and the one still mounted behind it, or a row that scrolled out
            # of view. Returning whichever came first in the tree hands back an
            # element that cannot be clicked, and the click then "succeeds"
            # while nothing happens. Prefer one the user could actually reach.
            return await self._most_reachable(exact)
        return best_idx

    async def _most_reachable(self, idxs: list[int]) -> int:
        """Of several same-named elements, the one actually on screen."""
        ranked: list[tuple[int, int, int]] = []   # (on_screen, rendered, idx)
        for i in idxs:
            object_id = await self._object_id(i)
            if not object_id:
                ranked.append((0, 0, i))
                continue
            try:
                info = await self._call_on(object_id, """function() {
                    const r = this.getBoundingClientRect();
                    const rendered = !!(r.width || r.height)
                        && getComputedStyle(this).visibility !== 'hidden';
                    const onScreen = rendered
                        && r.bottom > 0 && r.right > 0
                        && r.top < innerHeight && r.left < innerWidth;
                    return {rendered: rendered, onScreen: onScreen};
                }""")
            except Exception:  # noqa: BLE001
                info = None
            ranked.append((int(bool(info and info.get("onScreen"))),
                           int(bool(info and info.get("rendered"))), i))
        ranked.sort(key=lambda t: (-t[0], -t[1], t[2]))
        return ranked[0][2]

    async def fill_form(self, fields: dict[str, str], *,
                        clear_first: bool = True) -> dict[str, list[str]]:
        """Fill multiple fields from a {label: value} dict in one call.

        Resolves each label via fuzzy ARIA-name match. Returns lists of
        filled vs skipped field names — letting the caller see which the
        page didn't have or couldn't be typed into.
        """
        filled: list[str] = []
        skipped: list[str] = []
        await self.snapshot()
        for label, value in fields.items():
            idx = await self.find_by_text(
                label,
                role_hint=None,  # accept textbox/searchbox/combobox/etc.
                fuzzy=True,
            )
            if idx is None:
                skipped.append(label)
                continue
            ok = await self.type(idx, value, clear=clear_first)
            (filled if ok else skipped).append(label)
            # Snapshot can shift after typing (validation messages appear).
            await self.snapshot()
        return {"filled": filled, "skipped": skipped}

    async def current_state(self) -> dict[str, Any]:
        """One-call orientation: URL + title + interactive count + headings + forms.

        All assembled in a SINGLE tab.evaluate() round-trip — was 4 sequential
        calls (~140ms), now ~30ms. Snapshot uses cache if fresh."""
        await self.snapshot()
        raw = await self.tab.evaluate("""JSON.stringify({
            url: location.href,
            title: document.title,
            h1_h2: Array.from(document.querySelectorAll('h1, h2'))
                .filter(h => h.offsetParent !== null)
                .slice(0, 8)
                .map(h => h.textContent.trim().substring(0, 120)),
            forms: Array.from(document.forms).slice(0, 5).map(f => ({
                action: f.action || '',
                fields: Array.from(f.elements).slice(0, 10).map(e => ({
                    name: e.name || e.id || '',
                    type: e.type || e.tagName.toLowerCase(),
                    placeholder: e.placeholder || '',
                })),
            })),
        })""")
        import json as _j
        state = _j.loads(raw) if isinstance(raw, str) else {}
        state["interactive_count"] = len(self._index)
        return state
