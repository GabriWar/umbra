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
})

# Roles that group structure — useful as landmarks in the rendered tree.
_LANDMARK_ROLES = frozenset({
    "main", "navigation", "banner", "complementary", "contentinfo",
    "form", "search", "region", "article",
})


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
        if self.properties.get("checked") in (True, "true", "mixed"):
            flags.append(f"checked={self.properties['checked']}")
        if flags:
            bits.append("[" + ", ".join(flags) + "]")
        return " ".join(bits)


class AriaDriver:
    """Drive a tab via its accessibility tree."""

    def __init__(self, tab: Any):
        self.tab = tab
        self._index: dict[int, AxNode] = {}
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
                    props[str(pname)] = pval

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

    def render_tree(self, max_items: int = 60) -> str:
        """Pretty-print the current snapshot for an LLM."""
        if not self._index:
            return "(empty — call await snapshot() first)"
        lines = [n.render() for n in list(self._index.values())[:max_items]]
        return "\n".join(lines)

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

    async def click(self, idx: int) -> bool:
        """Activate element by DOM.focus + keyboard. Zero mouse events.

        Direct CDP `DOM.focus(backend_node_id=…)` — no Runtime.callFunctionOn
        round-trip (was buggy w/ object_id type wrapping). Press Enter for
        most roles, Space for checkbox/radio/switch.
        """
        node = self._index.get(idx)
        if not node or not node.backend_node_id:
            return False
        is_toggle = node.role in {"checkbox", "radio", "switch"}
        key = "Space" if is_toggle else "Enter"
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
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("aria click failed at idx=%d: %s", idx, e)
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
        best_idx, best_score = None, 0
        for n in self._index.values():
            if role_hint and n.role != role_hint:
                continue
            haystacks = [n.name, n.value or ""]
            for hay in haystacks:
                if not hay:
                    continue
                hl = hay.lower() if fuzzy else hay
                if fuzzy:
                    if target == hl:
                        return n.idx
                    if target in hl:
                        score = 100 - abs(len(hl) - len(target))
                        if score > best_score:
                            best_idx, best_score = n.idx, score
                else:
                    if hay == text:
                        return n.idx
        return best_idx

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
