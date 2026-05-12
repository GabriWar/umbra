"""`umbra-cloak-setup` CLI — manually install / inspect cloak binary.

Usage:
    umbra-cloak-setup                # install latest if missing
    umbra-cloak-setup --force        # re-download even if cached
    umbra-cloak-setup --tag <tag>    # pin to a specific release tag
    umbra-cloak-setup --status       # show what's installed, no network
    umbra-cloak-setup --uninstall    # wipe ~/.umbra/cloak/

Auto-installs on first `spawn()` regardless — this CLI is for pre-warm,
diagnostics, or pinning to a non-latest tag.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
from pathlib import Path

from .loader import (
    CloakUnavailable,
    cache_root,
    cloak_status,
    install_latest,
    resolve_cloak_binary,
)


def _print_status() -> int:
    s = cloak_status()
    print(json.dumps(s, indent=2))
    return 0


def _uninstall() -> int:
    root = cache_root()
    if not root.exists():
        print(f"nothing to remove at {root}")
        return 0
    shutil.rmtree(root)
    print(f"removed {root}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="umbra-cloak-setup", description=__doc__)
    p.add_argument("--force", action="store_true",
                   help="re-download even if cached")
    p.add_argument("--tag", default=None,
                   help="pin to a specific release tag (default: newest with this platform)")
    p.add_argument("--status", action="store_true", help="print status as JSON and exit")
    p.add_argument("--uninstall", action="store_true", help="wipe the cloak cache")
    p.add_argument("--quiet", "-q", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(message)s",
    )

    if args.status:
        return _print_status()
    if args.uninstall:
        return _uninstall()

    try:
        if args.force or args.tag:
            path = install_latest(force=args.force, tag=args.tag)
        else:
            path = resolve_cloak_binary(auto_download=True, tag=args.tag)
    except CloakUnavailable as e:
        print(f"cloak unavailable: {e}", file=sys.stderr)
        return 2

    print(str(path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
