"""Chrome never outlives the umbra server that started it.

* install_pdeathsig(): Chrome is started with PR_SET_PDEATHSIG = SIGKILL (Linux), so the
  kernel kills it the moment its server process dies, however it ends (SIGKILL, OOM, a
  closed SSH session's SIGHUP). Its renderers and helpers go with it.
* abandoned(): the Chrome trees whose server is already gone (from before this fix, or
  another OS): every process from one up to init (or the user's systemd, a subreaper) is a
  Chrome of the same profile. A browser with a live server, busy or idle, always has that
  server (a Python process) in its ancestry, so it never counts.
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
from pathlib import Path
from typing import Any

_PR_SET_PDEATHSIG = 1
_SIGKILL = 9
_installed = False


_prctl: Any = None


def _set_pdeathsig() -> None:
    # In the child between fork and exec: one libc call (looked up in the parent).
    _prctl(_PR_SET_PDEATHSIG, _SIGKILL, 0, 0, 0)


def _is_chrome(program: Any) -> bool:
    name = Path(str(program)).name.lower()
    return "chrom" in name


def install_pdeathsig() -> None:
    """Wrap asyncio.create_subprocess_exec once: Chrome launches get the death signal.

    Linux only. Wrapping (not replacing) keeps any other wrapper (the LD_PRELOAD shim's)
    working, and a caller's own preexec_fn is never overridden.
    """
    global _installed, _prctl
    if _installed or not sys.platform.startswith("linux"):
        return
    _installed = True
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
    inner = asyncio.create_subprocess_exec

    async def _exec(program: Any, *args: Any, **kwargs: Any) -> Any:
        if _is_chrome(program) and "preexec_fn" not in kwargs:
            kwargs["preexec_fn"] = _set_pdeathsig
        return await inner(program, *args, **kwargs)

    asyncio.create_subprocess_exec = _exec  # type: ignore[assignment]


def abandoned(parent: dict[int, int], data_dir: dict[int, str], comm: dict[int, str]) -> list[int]:
    """The pids of Chrome trees whose server is gone.

    parent: pid -> ppid; data_dir: Chrome pid -> its --user-data-dir; comm: pid -> process
    name (for the subreaper check). A Chrome is abandoned when, walking up, every process
    is a Chrome of the same profile until init (pid 1) or a `systemd` subreaper.
    """
    out = []
    for pid, udir in data_dir.items():
        cur, ok, seen = parent.get(pid, 0), False, set()
        while cur not in seen:
            seen.add(cur)
            if cur <= 1 or comm.get(cur) == "systemd":
                ok = True
                break
            if data_dir.get(cur) != udir:
                break
            cur = parent.get(cur, 0)
        if ok:
            out.append(pid)
    return out
