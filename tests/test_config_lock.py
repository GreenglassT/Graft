"""The config lock never hangs a caller and never crashes one.

Every config mutator takes an `flock` on `config.yaml.lock`. Two ways that
could go wrong for the GUI, which runs these as subprocesses:

  * the holder is stuck (SIGSTOP'd, or wedged): a blocking `LOCK_EX` waits
    forever, and so does the GUI's pane — so the wait is bounded and ends in
    a ConfigError that says who is in the way;
  * the filesystem cannot lock at all (NFS without a lock daemon: ENOTSUP,
    ENOLCK): that is no reason to refuse an edit, so it proceeds unlocked,
    as it already does when the lock file cannot be created.
"""

from __future__ import annotations

import errno
import fcntl
import os
import threading
import time
from pathlib import Path

import pytest

from graft_ipa import config as config_mod
from graft_ipa.config import load_config, set_app_tweaks
from graft_ipa.errors import ConfigError

from .config_fixtures import write_test_config


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    write_test_config(path, team_id="ABCDE12345")
    return path


class _HeldElsewhere:
    """The lock held through a descriptor of our own, as another process's
    would be: `flock` conflicts between open file descriptions even within
    one process, and this one is not in the module's re-entrancy record."""

    def __init__(self, path: Path) -> None:
        self.fd = os.open(path.with_name("config.yaml.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)

    def release(self) -> None:
        os.close(self.fd)


def _in_thread(fn, timeout: float = 10.0):
    """Run `fn` and return (finished, result-or-exception). A test of a
    bounded wait must not itself be able to wait forever."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:  # handed back to the test
            box["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), box


def test_a_lock_held_too_long_is_a_config_error_naming_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(config_mod, "_LOCK_TIMEOUT", 0.3)
    holder = _HeldElsewhere(path)
    try:
        started = time.monotonic()
        finished, box = _in_thread(lambda: set_app_tweaks(path, "example", ["a.deb"]))
        elapsed = time.monotonic() - started
    finally:
        holder.release()

    assert finished, "the wait for a held lock never ended"
    error = box.get("error")
    assert isinstance(error, ConfigError), box
    assert error.message == (
        "another graft process is editing the config "
        f"(lock held: {path.with_name('config.yaml.lock')})"
    )
    assert 0.3 <= elapsed < 5
    assert path.read_bytes() == before


def test_a_lock_released_within_the_wait_is_taken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _config(tmp_path)
    monkeypatch.setattr(config_mod, "_LOCK_TIMEOUT", 5.0)
    holder = _HeldElsewhere(path)
    threading.Timer(0.3, holder.release).start()

    finished, box = _in_thread(lambda: set_app_tweaks(path, "example", ["a.deb"]))

    assert finished and "error" not in box, box
    assert load_config(path).get_app("example").tweaks == ["a.deb"]


def test_the_default_wait_is_about_ten_seconds() -> None:
    assert config_mod._LOCK_TIMEOUT == 10.0


@pytest.mark.parametrize("code", [errno.ENOTSUP, errno.ENOLCK], ids=["ENOTSUP", "ENOLCK"])
def test_a_filesystem_that_cannot_lock_still_lets_the_edit_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    path = _config(tmp_path)

    def cannot(fd, op):
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(config_mod.fcntl, "flock", cannot)
    held_during_write: list[bool] = []
    real_write = config_mod._atomic_write_text

    def watched(p, text, **kwargs):
        held_during_write.append(config_mod._config_lock_held(p))
        return real_write(p, text, **kwargs)

    monkeypatch.setattr(config_mod, "_atomic_write_text", watched)

    set_app_tweaks(path, "example", ["a.deb"])

    assert load_config(path).get_app("example").tweaks == ["a.deb"]
    # And nothing claims a lock that was never taken: "held" means held.
    assert held_during_write == [False]
