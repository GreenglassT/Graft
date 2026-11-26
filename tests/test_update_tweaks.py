"""The update's tweak step (spec 3.2): download to a temporary folder, then
move it into the library and re-point every app in one config write, and a
Stop waits for that move and write."""

from __future__ import annotations

import json
import os
import signal
import time
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa import update_tweaks
from graft_ipa.canister import CanisterError
from graft_ipa.deb import control_fields
from graft_ipa.errors import ConfigError, Stopped
from graft_ipa.json_output import JsonEmitter
from graft_ipa.stop_deferral import StopGate
from graft_ipa.updates import Availability, TweakStatus

from .update_fakes import deb


def _aged(path: Path, days: float) -> Path:
    when = time.time() - days * 24 * 60 * 60
    os.utime(path, (when, when))
    return path


def test_a_download_gets_a_folder_of_its_own_that_is_gone_afterward(tmp_path: Path) -> None:
    with update_tweaks.workdir(tmp_path) as work:
        assert work.parent == tmp_path / update_tweaks.TMP_DIRNAME
        assert work.name.startswith("tweak-") and work.is_dir()
        (work / "partial.deb").write_bytes(b"x")

    assert not work.exists()


def test_the_folder_is_gone_after_a_failure_too(tmp_path: Path) -> None:
    with pytest.raises(OSError), update_tweaks.workdir(tmp_path) as work:
        raise OSError("download failed")

    assert not work.exists()


def test_a_folder_a_killed_update_left_is_swept_a_day_later(tmp_path: Path) -> None:
    """A SIGKILL (the app's Force Stop) runs no `finally`: the next update
    clears what it left, as `appstore.private_workdir` does for a get."""
    root = tmp_path / update_tweaks.TMP_DIRNAME
    old = root / "tweak-old"
    old.mkdir(parents=True)
    (old / "half.deb").write_bytes(b"x")
    _aged(old, 2)
    recent = root / "tweak-recent"
    recent.mkdir()
    not_ours = root / "kept-old"
    not_ours.mkdir()
    _aged(not_ours, 2)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.deb").write_bytes(b"x")
    link = root / "tweak-link"
    link.symlink_to(elsewhere)
    _aged(elsewhere, 2)

    with update_tweaks.workdir(tmp_path):
        pass

    assert not old.exists(), "a day-old folder of ours is swept"
    assert recent.is_dir(), "one that may belong to a running update isn't"
    assert not_ours.is_dir(), "only the folders this module makes"
    assert (elsewhere / "keep.deb").is_file(), "a link is never followed"


# --- one tweak update (spec 3.2) ----------------------------------------------


def _status(version: str = "1.1") -> TweakStatus:
    return TweakStatus(filename="a-tweak.deb", package_id="com.example.atweak", installed="1.0",
                       available=Availability(version, "canister", "com.example.atweak"))


def _update(world, *, gate: StopGate | None = None) -> tuple[update_tweaks.Updated, list[dict]]:
    out = StringIO()
    cfg = world.cfg
    gate = gate or StopGate(lambda *_: None)
    done = update_tweaks.update(cfg, _status(), ["example", "bravo"], gate=gate,
                                emitter=JsonEmitter(stream=out))
    return done, [json.loads(line) for line in out.getvalue().splitlines()]


def test_the_download_waits_in_a_folder_of_its_own_then_every_app_moves_to_it(world) -> None:
    done, events = _update(world)

    cfg = world.cfg
    (_target, dest_dir), = world.fetches
    assert dest_dir.parent == cfg.tweaks_dir / update_tweaks.TMP_DIRNAME
    assert not dest_dir.exists(), "the download's folder is gone"
    assert done.new_filename == "atweak-tweak_1.1.deb" and (cfg.tweaks_dir / done.new_filename).is_file()
    assert {name: a.tweaks for name, a in cfg.apps.items()} == {
        "example": ["atweak-tweak_1.1.deb"], "bravo": ["atweak-tweak_1.1.deb", "b-tweak.deb"],
    }
    assert (cfg.tweaks_dir / "a-tweak.deb").is_file(), "the old build stays"
    assert (cfg.state_dir / "tweak-rollback" / "a-tweak.deb").is_file()
    (event,) = events
    assert event["type"] == "tweak-updated" and event["apps"] == ["example", "bravo"]


def test_a_failed_download_changes_nothing(world) -> None:
    world.fetch_error = CanisterError("download failed: 503")
    before = world.cfg_path.read_text()

    with pytest.raises(CanisterError):
        _update(world)

    cfg = world.cfg
    assert world.cfg_path.read_text() == before
    assert not (cfg.state_dir / "tweak-rollback" / "a-tweak.deb").exists()
    assert list((cfg.tweaks_dir / update_tweaks.TMP_DIRNAME).iterdir()) == []


def test_a_write_that_fails_repoints_no_app_and_takes_the_new_file_back(world, monkeypatch) -> None:
    def failing(*_a, **_k):
        raise ConfigError("another graft process is editing the config")

    monkeypatch.setattr(update_tweaks, "repoint_tweak", failing)

    with pytest.raises(ConfigError):
        _update(world)

    cfg = world.cfg
    assert {a.tweaks[0] for a in cfg.apps.values()} == {"a-tweak.deb"}
    assert not (cfg.tweaks_dir / "atweak-tweak_1.1.deb").exists()


@pytest.mark.usefixtures("stop_safety_net")
def test_a_stop_during_the_write_waits_and_every_app_is_repointed(world, monkeypatch) -> None:
    real = update_tweaks.repoint_tweak

    def stopped_midway(*a, **k):
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGTERM)  # a second Stop too: the write is never cut short
        return real(*a, **k)

    monkeypatch.setattr(update_tweaks, "repoint_tweak", stopped_midway)
    said: list = []

    with StopGate(lambda app, step: said.append((app, step))).installed() as gate:
        with pytest.raises(Stopped):
            _update(world, gate=gate)

    assert said == [(None, "tweaks")]
    assert {a.tweaks[0] for a in world.cfg.apps.values()} == {"atweak-tweak_1.1.deb"}


def test_a_repo_that_reuses_the_filename_replaces_it_in_place(world, monkeypatch) -> None:
    monkeypatch.setattr(
        update_tweaks.canister, "fetch_deb",
        lambda _t, dest, *_a, **_k: deb(dest / "a-tweak.deb", "com.example.atweak", "1.1"),
    )
    before = world.cfg_path.read_text()

    done, _events = _update(world)

    assert done.replaced_in_place and done.apps == ["example", "bravo"]
    assert world.cfg_path.read_text() == before, "no config write"
    assert control_fields(world.cfg.tweaks_dir / "a-tweak.deb")["version"] == "1.1", "the new build"


def test_an_update_sweeps_what_a_killed_one_left(world) -> None:
    root = world.cfg.tweaks_dir / update_tweaks.TMP_DIRNAME
    old = root / "tweak-old"
    old.mkdir(parents=True)
    _aged(old, 2)

    _update(world)

    assert not old.exists()
