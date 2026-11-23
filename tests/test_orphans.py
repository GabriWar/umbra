"""Chrome never outlives its umbra server; live browsers (busy or idle) are never touched."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from umbra.orphans import abandoned


def test_only_trees_adopted_by_init_or_systemd_are_abandoned():
    parent = {
        50: 1,     # a live umbra server (python)
        100: 50,   # its browser: idle or busy, it has a live server above
        101: 100,  # ... and its renderer
        200: 1,    # a browser adopted by init: its server died
        201: 200,  # ... its zygote
        202: 201,  # ... a renderer under the zygote
        300: 7,    # adopted by the user's systemd (a subreaper)
        400: 60,   # started from a shell that is still alive: not ours to judge
        60: 1,
    }
    comm = {50: "python3", 60: "bash", 7: "systemd", 100: "chrome", 101: "chrome", 200: "chrome", 201: "chrome", 202: "chrome", 300: "chrome", 400: "chrome"}
    data_dir = {100: "/tmp/uc_a", 101: "/tmp/uc_a", 200: "/tmp/uc_b", 201: "/tmp/uc_b", 202: "/tmp/uc_b", 300: "/tmp/uc_c", 400: "/tmp/uc_d"}
    assert sorted(abandoned(parent, data_dir, comm)) == [200, 201, 202, 300]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="PR_SET_PDEATHSIG is Linux")
def test_chrome_dies_when_its_server_is_killed(tmp_path):
    # A stand-in "chromium" (the wrapper only acts on chrome-looking programs) that
    # would sleep for a minute.
    fake = tmp_path / "chromium"
    fake.write_text("#!/bin/sh\nexec sleep 60\n")
    fake.chmod(0o755)
    pidfile = tmp_path / "pid"
    # The "server": installs the wrapper, starts the fake chrome, reports its pid, waits.
    server = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import asyncio, time
        from umbra.orphans import install_pdeathsig
        install_pdeathsig()
        async def main():
            p = await asyncio.create_subprocess_exec({str(fake)!r})
            open({str(pidfile)!r}, "w").write(str(p.pid))
            await asyncio.sleep(60)
        asyncio.run(main())
    """)])
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text():
                break
            time.sleep(0.05)
        child = int(pidfile.read_text())
        os.kill(child, 0)  # running
        server.kill()      # SIGKILL: no handler, no cleanup code runs
        server.wait()
        for _ in range(100):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            # A zombie until init reaps it: dead all the same.
            if open(f"/proc/{child}/stat").read().split(") ")[1].startswith("Z"):
                break
            time.sleep(0.05)
        else:
            os.kill(child, 9)
            pytest.fail("the chrome outlived its server")
    finally:
        server.kill()


def test_the_sweep_kills_only_abandoned_trees_and_their_dirs(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from umbra import server

    prefix = str(tmp_path / "uc_")
    live, gone = prefix + "live", prefix + "gone"
    os.mkdir(live)
    os.mkdir(gone)
    ps = "\n".join([
        "  50     1 python3 /usr/bin/python -m umbra.server",
        f" 100    50 chrome chrome --user-data-dir={live}",
        f" 200     1 chrome chrome --user-data-dir={gone}",
        f" 201   200 chrome chrome --type=zygote --user-data-dir={gone}",
    ])
    monkeypatch.setattr(server, "_UC_PREFIX", prefix)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=ps))
    monkeypatch.setattr(server.time, "sleep", lambda _s: None)
    killed: list[int] = []
    monkeypatch.setattr(server.os, "kill", lambda pid, _sig: killed.append(pid))
    res = server._sweep_abandoned()
    assert sorted(killed) == [200, 201]
    assert os.path.isdir(live) and not os.path.exists(gone)
    assert res["removed_dirs"] == [gone]
