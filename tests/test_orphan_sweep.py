"""Startup/shutdown orphan sweep must respect browsers of other live servers.

Fake process table, no real Chrome.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

from umbra import server
from umbra.vault import unlock


@pytest.fixture
def world(tmp_path, monkeypatch):
    prefix = str(tmp_path / "uc_")
    me = os.getpid()
    old = time.time() - 3600

    def mkdir(name: str, mtime: float = old) -> str:
        d = prefix + name
        os.mkdir(d)
        os.utime(d, (mtime, mtime))
        return d

    dirs = {n: mkdir(n) for n in ("other", "orphan", "bashkid", "mine", "stale")}
    dirs["fresh"] = mkdir("fresh", time.time())
    ps = "\n".join([
        "  50     1 /usr/bin/python -m umbra.server",          # another live server
        "  60     1 bash",                                       # not a server
        f" 100    50 chrome --user-data-dir={dirs['other']}",
        f" 101   100 chrome --type=renderer --user-data-dir={dirs['other']}",
        f" 200     1 chrome --user-data-dir={dirs['orphan']}",
        f" 300    60 chrome --user-data-dir={dirs['bashkid']}",
        f" 400 {me} chrome --user-data-dir={dirs['mine']}",
    ])
    monkeypatch.setattr(server, "_UC_PREFIX", prefix)
    monkeypatch.setattr(server.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout=ps))
    monkeypatch.setattr(unlock, "is_umbra_server", lambda pid: pid == 50)
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    killed: list[int] = []
    monkeypatch.setattr(server.os, "kill", lambda pid, _sig: killed.append(pid))
    return dirs, killed


def test_startup_sweep_spares_other_live_servers(world):
    dirs, killed = world
    res = server._kill_orphan_chromes()
    assert sorted(killed) == [200, 300]                  # parent dead / not a server
    assert os.path.isdir(dirs["other"])                  # other server's browser + renderer
    assert os.path.isdir(dirs["mine"])
    assert os.path.isdir(dirs["fresh"])                  # maybe mid-spawn elsewhere
    for gone in ("orphan", "bashkid", "stale"):
        assert not os.path.exists(dirs[gone])
    assert set(res["removed_dirs"]) == {dirs["orphan"], dirs["bashkid"], dirs["stale"]}


def test_shutdown_sweep_cleans_only_own_leftovers(world):
    dirs, killed = world
    server._kill_orphan_chromes(include_self=True)
    assert 400 in killed and 100 not in killed and 101 not in killed
    assert not os.path.exists(dirs["mine"])
    assert os.path.isdir(dirs["other"])
