"""Chrome version detection must never launch Chrome on Windows.

On Windows `chrome.exe --version` prints nothing and opens a visible browser
window, so the version is read from the exe's file version resource instead.
No real Chrome.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from umbra import browser


def _no_launch(*args, **kwargs):
    raise AssertionError(f"Chrome was launched: {args!r}")


def test_windows_reads_exe_version_without_launching(monkeypatch):
    monkeypatch.setattr(browser.os, "name", "nt")
    monkeypatch.setattr(browser.subprocess, "run", _no_launch)
    monkeypatch.setattr(browser, "_windows_exe_version", lambda path: "147.0.1234.56")
    assert browser._detect_chrome_version(r"C:\chrome\chrome.exe") == "147.0.1234.56"


def test_windows_falls_back_without_launching(monkeypatch):
    monkeypatch.setattr(browser.os, "name", "nt")
    monkeypatch.setattr(browser.subprocess, "run", _no_launch)
    monkeypatch.setattr(browser, "_windows_exe_version", lambda path: None)
    assert browser._detect_chrome_version(r"C:\chrome\chrome.exe") == "146.0.7339.16"
    assert browser._detect_chrome_version(None) == "146.0.7339.16"


@pytest.mark.skipif(sys.platform != "win32", reason="reads a Windows version resource")
def test_windows_exe_version_reads_a_real_exe():
    import re

    # python.exe carries a version resource, like chrome.exe.
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", browser._windows_exe_version(sys.executable))
    assert browser._windows_exe_version(r"C:\does\not\exist.exe") is None


def test_other_platforms_still_run_version(monkeypatch):
    monkeypatch.setattr(browser.os, "name", "posix")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Chromium 145.0.1.2\n")

    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    assert browser._detect_chrome_version("/usr/bin/chromium") == "145.0.1.2"
    assert calls == [["/usr/bin/chromium", "--version"]]
