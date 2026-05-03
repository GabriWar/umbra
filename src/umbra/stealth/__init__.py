"""Stealth subpackage — payload, blocklist, humanizer."""

from umbra.stealth.blocklist import blocklist_size, is_blocked
from umbra.stealth.humanizer import (
    FAST_TYPIST,
    SLOW_TYPIST,
    TypingProfile,
    keystroke_delay,
    think_pause,
    type_with_jitter,
)
from umbra.stealth.inject import install, install_playwright, load_payload


class Humanizer:
    """Convenience class wrapping the keystroke functions (fantoma-style API)."""

    def __init__(self, profile: TypingProfile = FAST_TYPIST):
        self.profile = profile

    def delay(self, prev: str, curr: str) -> float:
        return keystroke_delay(prev, curr, self.profile)


__all__ = [
    "FAST_TYPIST",
    "SLOW_TYPIST",
    "Humanizer",
    "TypingProfile",
    "blocklist_size",
    "install",
    "install_playwright",
    "is_blocked",
    "keystroke_delay",
    "load_payload",
    "think_pause",
    "type_with_jitter",
]
