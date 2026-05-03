"""Keystroke timing humanizer.

Combines fantoma's Killourhy-Maxion key-pair model (same-hand vs alt-hand vs
same-finger pair classes) with a log-normal jitter envelope on top of each
delay. Why log-normal: human inter-keystroke intervals follow a heavy-tailed
distribution (most keys ~80-150ms, occasional 400ms+ pauses for thinking).
A uniform random in [40, 200]ms doesn't reproduce that tail and is itself a
fingerprint signature.

Default profile: ~60-70 WPM (fast typist). Override via `Humanizer(profile=...)`.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

# Same-hand pairs (e.g. 'er', 'we'): both keys on the same hand, fast transition.
_LEFT = set("qwertasdfgzxcvb12345`~!@#$%")
_RIGHT = set("yuiophjklnm67890-=[]\\;',./^&*()")

# Same-finger pairs are slower because the finger has to lift and reposition.
_SAME_FINGER_PAIRS = frozenset({
    "ed", "de", "ce", "ec", "rf", "fr", "tg", "gt", "ws", "sw",
    "uj", "ju", "ik", "ki", "ol", "lo", "mn", "nm", "hy", "yh",
    "az", "za", "qa", "aq", "px", "xp",
})


@dataclass(frozen=True)
class TypingProfile:
    """Mean & sigma for log-normal delay (in seconds) per pair class.

    The log-normal is parameterised by ln(target_mean) and ln(target_sigma).
    Drawing exp(N(mu, sigma)) gives the heavy right tail real typists exhibit.
    """

    same_hand_mu: float = -2.5   # exp(-2.5) ≈ 0.082s
    same_hand_sigma: float = 0.35
    alt_hand_mu: float = -2.3    # exp(-2.3) ≈ 0.100s
    alt_hand_sigma: float = 0.40
    same_finger_mu: float = -1.9 # exp(-1.9) ≈ 0.150s
    same_finger_sigma: float = 0.45
    space_extra_mu: float = -3.5 # additional ~0.030s on top of class delay
    space_extra_sigma: float = 0.5
    micro_hesitation_prob: float = 0.04  # 4% chance of 200-600ms pause
    micro_hesitation_range: tuple[float, float] = (0.2, 0.6)


FAST_TYPIST = TypingProfile()
SLOW_TYPIST = TypingProfile(
    same_hand_mu=-2.1, alt_hand_mu=-1.9, same_finger_mu=-1.5,
    micro_hesitation_prob=0.08,
)


def _classify_pair(prev: str, curr: str) -> str:
    pair = (prev + curr).lower()
    if pair in _SAME_FINGER_PAIRS:
        return "same_finger"
    pl, cl = prev.lower(), curr.lower()
    if (pl in _LEFT and cl in _RIGHT) or (pl in _RIGHT and cl in _LEFT):
        return "alt_hand"
    return "same_hand"


def keystroke_delay(prev: str, curr: str, profile: TypingProfile = FAST_TYPIST) -> float:
    """Sample a single inter-keystroke interval in seconds."""
    if not prev or not curr:
        # Cold start — single random draw from same-hand distribution.
        return random.lognormvariate(profile.same_hand_mu, profile.same_hand_sigma)

    cls = _classify_pair(prev, curr)
    if cls == "same_finger":
        delay = random.lognormvariate(profile.same_finger_mu, profile.same_finger_sigma)
    elif cls == "alt_hand":
        delay = random.lognormvariate(profile.alt_hand_mu, profile.alt_hand_sigma)
    else:
        delay = random.lognormvariate(profile.same_hand_mu, profile.same_hand_sigma)

    if curr == " ":
        delay += random.lognormvariate(profile.space_extra_mu, profile.space_extra_sigma)

    if random.random() < profile.micro_hesitation_prob:
        lo, hi = profile.micro_hesitation_range
        delay += random.uniform(lo, hi)

    # Clamp to a sane range. Real typists rarely hit <30ms or >2s without
    # a deliberate think-pause (already covered by micro_hesitation).
    return max(0.025, min(delay, 2.0))


def type_with_jitter(send_char, text: str, profile: TypingProfile = FAST_TYPIST) -> None:
    """Type `text` one character at a time via `send_char`, sleeping between.

    `send_char` is a callable taking one character — typically wraps
    `nodriver.Tab.send(cdp.input.dispatch_key_event(...))` or Playwright's
    `page.keyboard.type(char)`.
    """
    prev = ""
    for ch in text:
        send_char(ch)
        time.sleep(keystroke_delay(prev, ch, profile))
        prev = ch


def think_pause(min_s: float = 0.4, max_s: float = 1.8) -> None:
    """Sleep for a realistic 'looking at the page' duration.

    Uniform inside the band — these are deliberate decision pauses, not
    keystroke timing. Use BEFORE clicking submit, AFTER navigating.
    """
    time.sleep(random.uniform(min_s, max_s))
