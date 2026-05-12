"""Top-level umbra CLI.

    python -m umbra --setup [--force] [--tag TAG]
    python -m umbra --status
    python -m umbra --uninstall
    python -m umbra server [server-args ...]

`--setup` is the friendly alias for `umbra-cloak-setup` — fetches the
CloakBrowser patched chromium build, sha256-verifies it, caches it under
~/.umbra/cloak/<tag>/, and exits. Spawn auto-installs on first use too,
so this is for pre-warm or pinning a specific tag.
"""

from __future__ import annotations

import sys


_HELP = """\
umbra — stealth Chrome automation

  python -m umbra --setup [--force] [--tag TAG]   install CloakBrowser chromium
  python -m umbra --status                        show cloak install state (no net)
  python -m umbra --uninstall                     wipe ~/.umbra/cloak/
  python -m umbra server [args]                   start the MCP / HTTP server
  python -m umbra --help                          show this help

Equivalent dedicated CLIs (installed by pip):
  umbra-cloak-setup [...]
  umbra-server      [...]
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv or argv[0] in ("-h", "--help", "help"):
        print(_HELP)
        return 0

    # Server passthrough.
    if argv[0] in ("server", "serve"):
        from umbra.server import main as server_main
        # umbra.server.main reads sys.argv directly — rewrite it so its
        # argparse sees only its own args.
        sys.argv = ["umbra-server", *argv[1:]]
        server_main()
        return 0

    # Cloak passthrough — strip a leading "--setup" since the cloak CLI
    # treats install as the default action.
    if argv[0] == "--setup":
        argv = argv[1:]
    from umbra.cloak.__main__ import main as cloak_main
    return cloak_main(argv)


if __name__ == "__main__":
    sys.exit(main())
