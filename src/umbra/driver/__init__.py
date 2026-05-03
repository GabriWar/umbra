"""Drivers — high-level interaction layers on top of a stealth tab.

Two drivers ship by default:

    aria.AriaDriver    — accessibility-tree first, zero mouse coords. Use when
                         the target site uses behavioral fingerprinting (mouse
                         heatmaps, scroll velocity). Slower than CDP click but
                         emits no pointer telemetry.

    interact.CDPDriver — CDP-direct click/type with humanizer-jittered timing.
                         Use when ARIA can't reach the element (custom widgets,
                         drag-and-drop). Mouse coords are emitted but with
                         human-like trajectory + log-normal keystroke delays.
"""

from umbra.driver.aria import AriaDriver
from umbra.driver.interact import CDPDriver

__all__ = ["AriaDriver", "CDPDriver"]
