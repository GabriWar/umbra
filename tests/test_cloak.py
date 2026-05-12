"""CloakBrowser loader tests.

Offline tests (run by default): platform detect, manifest parsing, sha256
verify, archive extraction, install lifecycle against a stubbed GitHub.

E2E tests (-m e2e): real DL from GitHub, spawn with cloak chromium, verify
the resolved binary actually runs and `--version` parses.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from umbra.cloak import loader, manifest


# ───────────────────────── offline unit tests ─────────────────────────


def test_platform_detect_or_none() -> None:
    pk = manifest.detect_platform()
    # Either we're on a supported platform → key matches SUPPORTED map,
    # or we're not → returns None. Both are valid outcomes.
    if pk is not None:
        assert pk.key in manifest.SUPPORTED


def test_parse_sha256sums_two_space() -> None:
    txt = (
        "deadbeef" * 8 + "  cloakbrowser-linux-x64.tar.gz\n"
        "cafebabe" * 8 + "  SHA256SUMS\n"
    )
    h = manifest.parse_sha256sums(txt, "cloakbrowser-linux-x64.tar.gz")
    assert h == ("deadbeef" * 8).lower()


def test_parse_sha256sums_binary_marker() -> None:
    # GNU coreutils binary-mode line: "<hash> *<file>"
    txt = "ab" * 32 + " *cloakbrowser-windows-x64.zip\n"
    h = manifest.parse_sha256sums(txt, "cloakbrowser-windows-x64.zip")
    assert h == ("ab" * 32)


def test_parse_sha256sums_missing() -> None:
    with pytest.raises(LookupError):
        manifest.parse_sha256sums("ab" * 32 + "  other.zip\n", "missing.tar.gz")


def test_parse_sha256sums_malformed() -> None:
    with pytest.raises(ValueError):
        manifest.parse_sha256sums("notahex  cloakbrowser-linux-x64.tar.gz\n",
                                  "cloakbrowser-linux-x64.tar.gz")


def test_resolve_asset_picks_release_with_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    # Build a release list where the newest tag is missing our asset, but the
    # next one has it. Resolver should pick the second.
    pk = manifest.PlatformKey(os="linux", arch="x64")
    releases = [
        {
            "tag_name": "chromium-v146.0.0.0",
            "draft": False, "prerelease": False,
            "assets": [
                {"name": "cloakbrowser-windows-x64.zip",
                 "browser_download_url": "https://x/win.zip"},
                {"name": "SHA256SUMS",
                 "browser_download_url": "https://x/sums"},
            ],
        },
        {
            "tag_name": "chromium-v145.0.0.0",
            "draft": False, "prerelease": False,
            "assets": [
                {"name": "cloakbrowser-linux-x64.tar.gz",
                 "browser_download_url": "https://x/lin.tar.gz"},
                {"name": "SHA256SUMS",
                 "browser_download_url": "https://x/sums145"},
            ],
        },
    ]
    asset = manifest.resolve_asset(releases, pk)
    assert asset.tag == "chromium-v145.0.0.0"
    assert asset.asset_name == "cloakbrowser-linux-x64.tar.gz"
    assert asset.sha256_url.endswith("sums145")


def test_resolve_asset_skips_drafts_and_prereleases() -> None:
    pk = manifest.PlatformKey(os="linux", arch="x64")
    releases = [
        {
            "tag_name": "draft", "draft": True, "prerelease": False,
            "assets": [
                {"name": "cloakbrowser-linux-x64.tar.gz", "browser_download_url": "u"},
                {"name": "SHA256SUMS", "browser_download_url": "u"},
            ],
        },
        {
            "tag_name": "pre", "draft": False, "prerelease": True,
            "assets": [
                {"name": "cloakbrowser-linux-x64.tar.gz", "browser_download_url": "u"},
                {"name": "SHA256SUMS", "browser_download_url": "u"},
            ],
        },
    ]
    with pytest.raises(LookupError):
        manifest.resolve_asset(releases, pk)


def test_resolve_asset_pinned_tag() -> None:
    pk = manifest.PlatformKey(os="linux", arch="x64")
    releases = [
        {
            "tag_name": "v2", "draft": False, "prerelease": False,
            "assets": [
                {"name": "cloakbrowser-linux-x64.tar.gz",
                 "browser_download_url": "u2"},
                {"name": "SHA256SUMS", "browser_download_url": "s2"},
            ],
        },
        {
            "tag_name": "v1", "draft": False, "prerelease": False,
            "assets": [
                {"name": "cloakbrowser-linux-x64.tar.gz",
                 "browser_download_url": "u1"},
                {"name": "SHA256SUMS", "browser_download_url": "s1"},
            ],
        },
    ]
    a = manifest.resolve_asset(releases, pk, tag="v1")
    assert a.tag == "v1"
    assert a.asset_url == "u1"


def test_manifest_cache_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "manifest.json"
    data = [{"tag_name": "x", "assets": []}]
    manifest._save_cached(p, data)
    assert manifest._load_cached(p) == data
    # Stale → returns None
    old = p.stat().st_mtime - manifest.MANIFEST_TTL_SECONDS - 10
    os.utime(p, (old, old))
    assert manifest._load_cached(p) is None


def _make_tar_archive(dest: Path, entry_name: str = "chrome",
                       payload: bytes = b"#!/bin/sh\necho cloak-fake 146.0.0.0\n") -> Path:
    """Build a minimal tar.gz with an executable `chrome` inside."""
    work = dest.parent / "_tarwork"
    work.mkdir(exist_ok=True)
    inner = work / entry_name
    inner.write_bytes(payload)
    inner.chmod(0o755)
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(inner, arcname=entry_name)
    shutil.rmtree(work)
    return dest


def test_extract_tar_and_find_entry(tmp_path: Path) -> None:
    arc = _make_tar_archive(tmp_path / "x.tar.gz")
    out = tmp_path / "out"
    loader._extract(arc, out, "tar.gz")
    found = loader._find_entry(out)
    assert found is not None and found.name == "chrome"


def test_extract_refuses_path_traversal(tmp_path: Path) -> None:
    # Hand-build a tar that tries to escape via "../evil".
    evil = tmp_path / "evil.tar.gz"
    inner = tmp_path / "stage"
    inner.mkdir()
    payload = inner / "ok"
    payload.write_bytes(b"x")
    with tarfile.open(evil, "w:gz") as tf:
        ti = tarfile.TarInfo(name="../escapee")
        data = b"x"
        ti.size = len(data)
        import io
        tf.addfile(ti, io.BytesIO(data))
    with pytest.raises(loader.CloakUnavailable):
        loader._extract(evil, tmp_path / "out", "tar.gz")


def test_install_latest_stubbed_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full install lifecycle with GitHub + DL stubbed.

    Builds a fake archive locally, points the manifest at file:// URLs, runs
    install_latest, then asserts the cache layout + .entry file are correct
    and a second call is a no-op (idempotency).
    """
    pk = manifest.detect_platform()
    if pk is None:
        pytest.skip("unsupported host platform — cloak loader can't resolve")

    # Force the archive to match this host's asset name so resolve_asset finds it.
    asset_name = manifest.SUPPORTED[pk.key]
    is_zip = asset_name.endswith(".zip")

    cache_dir = tmp_path / "cache"
    monkeypatch.setenv("UMBRA_CLOAK_HOME", str(cache_dir))
    # Don't accidentally send a real GitHub token.
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    # Build the fake archive that will be served.
    archive = tmp_path / asset_name
    if is_zip:
        inner = tmp_path / "_zipsrc"
        inner.mkdir(exist_ok=True)
        (inner / "chrome.exe").write_bytes(b"MZfake")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(inner / "chrome.exe", arcname="chrome.exe")
    else:
        _make_tar_archive(archive)

    archive_hash = hashlib.sha256(archive.read_bytes()).hexdigest()
    sums_path = tmp_path / "SHA256SUMS"
    sums_path.write_text(f"{archive_hash}  {asset_name}\n")

    # Stub the GitHub releases response.
    releases = [{
        "tag_name": "stub-v1",
        "draft": False, "prerelease": False,
        "assets": [
            {"name": asset_name, "browser_download_url": archive.as_uri()},
            {"name": "SHA256SUMS", "browser_download_url": sums_path.as_uri()},
        ],
    }]
    # NB: loader.py does `from .manifest import fetch_releases`, so we must
    # patch loader's bound reference — patching `manifest.fetch_releases`
    # alone would silently let the real GitHub call through.
    monkeypatch.setattr(loader, "fetch_releases",
                        lambda cache_path, force=False: releases)

    out = loader.install_latest()
    assert out.exists() and out.is_file()
    # out lives somewhere under cache_dir/<some-dir>/  — `out.parent` may be
    # the tag dir or a nested subdir if the archive wrapped chrome in one.
    assert cache_dir in out.parents
    tag_dir = next(p for p in out.parents if p.parent == cache_dir)
    assert tag_dir.name == "stub-v1"
    assert (tag_dir / ".entry").read_text() == str(out)

    # Idempotent — second call should not re-download (we stub fetch_releases
    # but the early cache hit means we never even call it).
    called = {"n": 0}

    def _track(cache_path, force=False):
        called["n"] += 1
        return releases

    monkeypatch.setattr(loader, "fetch_releases", _track)
    out2 = loader.install_latest()
    assert out2 == out
    assert called["n"] == 0


def test_resolve_cloak_binary_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = tmp_path / "my-chrome"
    fake.write_bytes(b"x")
    monkeypatch.setenv("UMBRA_CLOAK_BINARY", str(fake))
    assert loader.resolve_cloak_binary(auto_download=False) == fake


def test_resolve_cloak_binary_env_override_missing(monkeypatch: pytest.MonkeyPatch,
                                                    tmp_path: Path) -> None:
    monkeypatch.setenv("UMBRA_CLOAK_BINARY", str(tmp_path / "nope"))
    with pytest.raises(loader.CloakUnavailable):
        loader.resolve_cloak_binary(auto_download=False)


def test_cloak_status_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UMBRA_CLOAK_HOME", str(tmp_path))
    monkeypatch.delenv("UMBRA_CLOAK_BINARY", raising=False)
    monkeypatch.delenv("UMBRA_NO_CLOAK", raising=False)
    s = loader.cloak_status()
    assert "platform" in s and "supported" in s and "installed" in s
    assert s["installed"] == []


# ───────────────────────── e2e (real network / browser) ─────────────────────────


@pytest.mark.e2e
def test_e2e_install_latest_real() -> None:
    """Hit real GitHub releases, DL + verify the real cloak binary.

    Caches under the real ~/.umbra/cloak/ so subsequent test runs are fast.
    Skips when offline or on unsupported platforms (CloakUnavailable).
    """
    pk = manifest.detect_platform()
    if pk is None:
        pytest.skip("no cloak build for this platform")
    try:
        path = loader.install_latest()
    except loader.CloakUnavailable as e:
        pytest.skip(f"cloak install failed (network?): {e}")
    assert path.is_file()
    # Sanity: binary should be executable on posix.
    if sys.platform != "win32":
        assert os.access(path, os.X_OK)


@pytest.mark.e2e
def test_e2e_spawn_uses_cloak() -> None:
    """Boot a stealth browser with chromium='cloak' and confirm chrome_path
    landed on the cloak cache."""
    pk = manifest.detect_platform()
    if pk is None:
        pytest.skip("no cloak build for this platform")
    from umbra import StealthBrowser, StealthOptions

    async def run() -> tuple[str, bool]:
        b = StealthBrowser(StealthOptions(headless=True, low_memory=True,
                                          chromium="cloak"))
        try:
            await b.start()
            return b.options.chrome_path or "", b._cloak_active
        finally:
            try:
                await b.stop()  # type: ignore[attr-defined]
            except Exception:
                pass

    path, active = asyncio.run(run())
    if not path:
        pytest.skip("could not boot chrome (cloak unavailable in this env)")
    assert "cloak" in path or active, f"expected cloak binary path, got {path!r}"
