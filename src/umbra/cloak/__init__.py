"""CloakBrowser integration — patched-chromium loader.

CloakBrowser (github.com/CloakHQ/CloakBrowser) ships a chromium build with
49-57 C++ source patches against canvas/WebGL/audio/font/GPU/WebRTC/screen/
timing fingerprint surfaces. Native patches beat JS shims because detectors
check the underlying API surface, not just the property values.

License is free for personal + commercial *use* but forbids redistribution —
so this package never bundles the binary. It downloads from upstream GitHub
releases on first spawn, verifies via SHA256SUMS, and caches per-version
under ~/.umbra/cloak/<tag>/.

Public surface:
    resolve_cloak_binary(auto_download=True) -> Path
    cloak_status() -> dict
    CloakUnavailable               (raised on unsupported platforms / DL fail)
"""

from __future__ import annotations

from .loader import (
    CloakUnavailable,
    cloak_status,
    cloak_supported,
    install_latest,
    resolve_cloak_binary,
)

__all__ = [
    "CloakUnavailable",
    "cloak_status",
    "cloak_supported",
    "install_latest",
    "resolve_cloak_binary",
]
