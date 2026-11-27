"""Temporary copies of the signing identity, and directories made mid-migration (R9, R10).

- R9: zsign is handed a re-encrypted copy of the identity in a 0700
  `graft-zsign-*` directory that `p12.private_identity` removes however the
  block is left. Not on SIGKILL (the GUI escalates to one two seconds after a
  Stop), and a second SIGTERM arriving during the removal raised out of it
  half done. The removal now runs with SIGTERM and SIGHUP held, and stale
  copies are swept at startup.
- R10: `secure_app_dirs` created whatever `app_dir` answered when it did not
  exist, and mid-migration (another CLI renaming `tweak-build` to `graft`)
  that could be a fresh `tweak-build`, which then broke the other's symlink.
  It only ever creates `graft` now.
"""

from __future__ import annotations

import os
import signal
import tempfile
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import data_dirs, p12

from .test_lc_sign import REAL_PASSWORD, _make_p12


def _aged_dir(path: Path, hours: float) -> Path:
    path.mkdir(parents=True)
    (path / "identity.p12").write_bytes(b"a re-encrypted identity")
    then = time.time() - hours * 3600
    os.utime(path, (then, then))
    return path


def test_stale_identity_copies_are_swept(tmp_path: Path) -> None:
    stale = [_aged_dir(tmp_path / "graft-zsign-abc123", 3), _aged_dir(tmp_path / "graft-p12-x", 3)]
    fresh = _aged_dir(tmp_path / "graft-zsign-now", 0.01)  # another run signing right now
    other = _aged_dir(tmp_path / "somebody-elses-dir", 3)
    plain = tmp_path / "graft-zsign-a-file"
    plain.write_text("not a directory")

    removed = p12.sweep_stale_copies(tmp_path)

    assert sorted(removed) == sorted(stale)
    assert not any(p.exists() for p in stale)
    assert fresh.exists() and other.exists() and plain.exists()


def test_a_stale_certificate_copy_for_livecontainer_is_swept(tmp_path: Path) -> None:
    """`--lc-push-cert` writes the .p12 into an `lcdoc-*` folder to push it
    (`UsbmuxDevice.push_document`), and a SIGKILL left it there (the 2.3.1
    review's A5). The startup sweep takes it, once it's older than any push."""
    stale = _aged_dir(tmp_path / "lcdoc-abc123", 3)
    fresh = _aged_dir(tmp_path / "lcdoc-now", 0.01)  # another run pushing right now

    removed = p12.sweep_stale_copies(tmp_path)

    assert removed == [stale]
    assert not stale.exists() and fresh.exists()


def test_the_certificate_push_writes_where_the_sweep_looks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tied to the code that makes it, so a renamed folder can't slip past
    the sweep."""
    from graft_ipa import lc_manage

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    seen: list[str] = []

    def running(_self, _udid, _work):
        seen.extend(p.name for p in tmp_path.iterdir())
        seen.extend(f.name for d in tmp_path.iterdir() for f in d.iterdir())

    monkeypatch.setattr(lc_manage.UsbmuxDevice, "_sandbox", lambda *_a: None)
    monkeypatch.setattr(lc_manage.UsbmuxDevice, "_run", running)

    lc_manage.UsbmuxDevice().push_document("u1", "com.example.lc", lc_manage.CERT_NAME, b"p12")

    folder, name = seen
    assert folder.startswith(p12.COPY_PREFIXES)
    assert name == lc_manage.CERT_NAME
    assert list(tmp_path.iterdir()) == [], "removed however the push ends, but a SIGKILL"


def test_startup_sweeps_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scratch = tmp_path / "tmp"
    stale = _aged_dir(scratch / "graft-zsign-left", 3)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    cfg = tmp_path / "xdg-config" / "graft" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(yaml.safe_dump({"apps": {}}))

    result = CliRunner().invoke(cli_mod.app, ["--list"])

    assert result.exit_code == 0, result.output
    assert not stale.exists()


def test_the_copy_is_removed_with_a_stop_held(tmp_path: Path, monkeypatch) -> None:
    """A second Stop during the removal waits until it is done."""
    src, _ = _make_p12(tmp_path)
    password = REAL_PASSWORD
    seen: dict[str, object] = {}
    real_rmtree = p12.shutil.rmtree

    def rmtree(path, *a, **k):
        seen["held"] = signal.pthread_sigmask(signal.SIG_BLOCK, set())
        real_rmtree(path, *a, **k)

    monkeypatch.setattr(p12.shutil, "rmtree", rmtree)
    before = signal.pthread_sigmask(signal.SIG_BLOCK, set())

    with p12.private_identity(src, password, prefix="graft-test-") as (copy, _one_time):
        work = copy.parent

    assert not work.exists()
    assert {signal.SIGTERM, signal.SIGHUP} <= set(seen["held"])
    assert signal.pthread_sigmask(signal.SIG_BLOCK, set()) == before, "the mask is restored"


def test_secure_app_dirs_never_creates_a_legacy_dir(tmp_path: Path, monkeypatch) -> None:
    """The race, frozen: `app_dir` answered `tweak-build` (it was a directory a
    moment ago), and another CLI has since renamed it away."""
    monkeypatch.setattr(data_dirs, "app_dir", lambda root: root / data_dirs.LEGACY_DIRNAME)

    data_dirs.secure_app_dirs()

    for root in data_dirs._roots():
        assert not os.path.lexists(root / data_dirs.LEGACY_DIRNAME)


def test_secure_app_dirs_still_creates_graft(tmp_path: Path) -> None:
    data_dirs.secure_app_dirs()

    for root in data_dirs._roots():
        made = root / data_dirs.APP_DIRNAME
        assert made.is_dir() and (made.stat().st_mode & 0o777) == 0o700
