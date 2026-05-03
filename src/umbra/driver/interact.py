"""CDP click/type driver with humanized mouse + keystroke timing.

Use this when the ARIA driver can't reach the element (drag-and-drop, custom
canvas widgets, hover-only menus). Mouse coordinates are emitted, but the
trajectory is bezier-curve interpolated with log-normal segment timing —
matches a real human's "approximate-then-correct" hand motion.

Compared to nodriver's default click (single instant move + click), this
adds:
  - Multi-segment movement with control-point jitter (3-point bezier)
  - Per-segment dwell time sampled from a log-normal
  - 1-3 mouseMoved events between mousedown/mouseup (the "settle" jitter
    real fingers exhibit)
  - Press-release delay sampled from human keystroke distribution

Costs ~150-400ms per click vs ~20ms for instant. Worth it on sites that
hash mousemove sequences (reCAPTCHA v3, PerimeterX, DataDome).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
from typing import Any

import nodriver as uc

from umbra.stealth.humanizer import FAST_TYPIST, keystroke_delay

log = logging.getLogger("umbra.driver.interact")


def _bezier(p0: tuple[float, float], p1: tuple[float, float], p2: tuple[float, float], steps: int):
    """Quadratic bezier — yields (x, y) for `steps` interpolation points."""
    for i in range(1, steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t ** 2 * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t ** 2 * p2[1]
        yield x, y


def _human_path(start: tuple[float, float], end: tuple[float, float], steps: int = 18):
    """Generate a bezier trajectory with a randomized control point off-axis."""
    sx, sy = start
    ex, ey = end
    mx, my = (sx + ex) / 2, (sy + ey) / 2
    dist = math.hypot(ex - sx, ey - sy)
    # Control-point offset perpendicular to the path, magnitude scales w/ distance
    angle = math.atan2(ey - sy, ex - sx) + math.pi / 2
    offset = (random.random() - 0.5) * dist * 0.3
    cx = mx + math.cos(angle) * offset
    cy = my + math.sin(angle) * offset
    return _bezier((sx, sy), (cx, cy), (ex, ey), steps)


class CDPDriver:
    """Lower-level driver: real mouse coords, but human-shaped."""

    def __init__(self, tab: Any):
        self.tab = tab
        self._cdp = uc.cdp
        self._mouse_x: float = 100.0
        self._mouse_y: float = 100.0

    async def _move_mouse(self, x: float, y: float, *, steps: int = 18) -> None:
        """Move the mouse along a humanized bezier path to (x, y)."""
        path = list(_human_path((self._mouse_x, self._mouse_y), (x, y), steps))
        # Total move time scales with distance; per-step delay log-normal-ish.
        for px, py in path:
            await self.tab.send(self._cdp.input_.dispatch_mouse_event(
                type_="mouseMoved", x=px, y=py,
            ))
            # 8-25ms per micro-step (real cursor sample rate is ~125Hz = 8ms,
            # but we want some variability).
            await asyncio.sleep(random.uniform(0.008, 0.025))
        self._mouse_x, self._mouse_y = x, y

    async def click(self, x: float, y: float, *, button: str = "left") -> None:
        """Move + click with human dwell at the destination."""
        btn_enum = self._cdp.input_.MouseButton(button)
        await self._move_mouse(x, y)
        # Settle jitter — real fingers don't land perfectly still.
        for _ in range(random.randint(1, 3)):
            jx = x + random.uniform(-1.5, 1.5)
            jy = y + random.uniform(-1.5, 1.5)
            await self.tab.send(self._cdp.input_.dispatch_mouse_event(
                type_="mouseMoved", x=jx, y=jy,
            ))
            await asyncio.sleep(random.uniform(0.012, 0.040))
        # Press → tiny dwell → release. Mean ~85ms, log-normal.
        await self.tab.send(self._cdp.input_.dispatch_mouse_event(
            type_="mousePressed", x=x, y=y, button=btn_enum, click_count=1,
        ))
        await asyncio.sleep(random.lognormvariate(-2.5, 0.3))
        await self.tab.send(self._cdp.input_.dispatch_mouse_event(
            type_="mouseReleased", x=x, y=y, button=btn_enum, click_count=1,
        ))

    async def type(self, text: str, *, profile=FAST_TYPIST) -> None:
        """Type into the currently-focused element with humanized timing."""
        prev = ""
        for ch in text:
            await asyncio.sleep(keystroke_delay(prev, ch, profile))
            await self.tab.send(self._cdp.input_.dispatch_key_event(
                type_="keyDown", text=ch, key=ch, unmodified_text=ch,
            ))
            await self.tab.send(self._cdp.input_.dispatch_key_event(
                type_="keyUp", key=ch,
            ))
            prev = ch

    async def scroll(self, dx: float = 0, dy: float = 200) -> None:
        """Scroll via wheel event with realistic delta."""
        await self.tab.send(self._cdp.input_.dispatch_mouse_event(
            type_="mouseWheel", x=self._mouse_x, y=self._mouse_y,
            delta_x=dx, delta_y=dy,
        ))
