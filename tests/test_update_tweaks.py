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

from graft_ipa import canister, tweak_sources, update_tweaks, updates
from graft_ipa.canister import CanisterError
from graft_ipa.errors import ConfigError, Stopped
from graft_ipa.json_output import JsonEmitter
from graft_ipa.stop_deferral import StopGate
from graft_ipa.tweak_sources import TweakFileError, UntrustedSourceError
from graft_ipa.updates import TweakStatus

from .update_fakes import REPO_ID, REPO_URL, deb, new_name


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

NEW = new_name("com.example.atweak", "1.1")


def _status(world) -> TweakStatus:
    """`a-tweak.deb`'s status, as the update's plan reads it: 1.1 published by
    its own source (`update_fakes`), verified unless the world says not."""
    from graft_ipa import cli as cli_mod

    world.published["com.example.atweak"] = "1.1"
    cfg = world.cfg
    record = next(r for r in cli_mod._collect_tweak_records(cfg) if r["filename"] == "a-tweak.deb")
    (status,) = updates.statuses(cfg, [record])
    assert status.update_available
    return status


def _update(world, *, gate: StopGate | None = None,
            trusted: bool = False) -> tuple[update_tweaks.Updated, list[dict]]:
    out = StringIO()
    status = _status(world)
    cfg = world.cfg
    gate = gate or StopGate(lambda *_: None)
    done = update_tweaks.update(cfg, status, ["example", "bravo"], gate=gate,
                                emitter=JsonEmitter(stream=out), trusted=trusted)
    return done, [json.loads(line) for line in out.getvalue().splitlines()]


def test_the_download_waits_in_a_folder_of_its_own_then_every_app_moves_to_it(world) -> None:
    done, events = _update(world)

    cfg = world.cfg
    (target, dest_dir), = world.fetches
    assert target == f"{REPO_URL}/debs/com.example.atweak_1.1.deb", "from its own source"
    assert dest_dir.parent == cfg.tweaks_dir / update_tweaks.TMP_DIRNAME
    assert not dest_dir.exists(), "the download's folder is gone"
    assert done.new_filename == NEW and (cfg.tweaks_dir / NEW).is_file()
    assert {name: a.tweaks for name, a in cfg.apps.items()} == {
        "example": [NEW], "bravo": [NEW, "b-tweak.deb"],
    }
    assert (cfg.tweaks_dir / "a-tweak.deb").is_file(), "the old build stays"
    assert (cfg.state_dir / "tweak-rollback" / "a-tweak.deb").is_file()
    (event,) = events
    assert event["type"] == "tweak-updated" and event["apps"] == ["example", "bravo"]
    assert event["source"] == REPO_URL


def test_the_new_file_s_source_is_recorded(world) -> None:
    _update(world)

    cfg = world.cfg
    sha = tweak_sources.file_sha256(cfg.tweaks_dir / NEW)
    assert tweak_sources.recorded_for(cfg.state_dir, NEW, sha) == \
        tweak_sources.Source(tweak_sources.CANISTER, REPO_ID, REPO_URL)


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
    assert not (cfg.tweaks_dir / NEW).exists()


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
    assert {a.tweaks[0] for a in world.cfg.apps.values()} == {NEW}


# --- 2.3.1 A2: the repo never names the library's file --------------------------


def test_a_download_named_like_another_tweak_never_overwrites_it(world, monkeypatch) -> None:
    """The finding, as verified: updating a-tweak.deb with a fetched name of
    b-tweak.deb overwrote b-tweak.deb, and bravo built with it."""
    cfg = world.cfg
    theirs = (cfg.tweaks_dir / "b-tweak.deb").read_bytes()
    monkeypatch.setattr(
        canister, "fetch_deb",
        lambda _t, dest, *_a, **_k: deb(dest / "b-tweak.deb", "com.example.atweak", "1.1"),
    )

    done, _events = _update(world)

    cfg = world.cfg
    assert (cfg.tweaks_dir / "b-tweak.deb").read_bytes() == theirs
    assert done.new_filename == NEW and not done.replaced_in_place
    assert {name: a.tweaks for name, a in cfg.apps.items()} == {
        "example": [NEW], "bravo": [NEW, "b-tweak.deb"],
    }


def test_a_different_file_under_graft_s_name_is_refused(world) -> None:
    cfg = world.cfg
    deb(cfg.tweaks_dir / NEW, "com.example.atweak", "1.1", name="Someone else's build")
    there = (cfg.tweaks_dir / NEW).read_bytes()
    before = world.cfg_path.read_text()

    with pytest.raises(TweakFileError, match="already in your tweaks library"):
        _update(world)

    assert (cfg.tweaks_dir / NEW).read_bytes() == there
    assert world.cfg_path.read_text() == before, "no app re-pointed"
    assert not (cfg.state_dir / "tweak-rollback" / "a-tweak.deb").exists()


def test_the_same_file_already_in_the_library_is_used(world, monkeypatch) -> None:
    """Fetched by hand already, and attached alongside: the update re-points
    to it rather than refusing its own bytes."""
    cfg = world.cfg
    held = deb(cfg.tweaks_dir / NEW, "com.example.atweak", "1.1").read_bytes()

    def same_bytes(_t, dest, *_a, **_k):
        path = dest / "download.deb"
        path.write_bytes(held)
        return path

    monkeypatch.setattr(canister, "fetch_deb", same_bytes)

    done, _events = _update(world)

    assert done.new_filename == NEW
    assert world.cfg.apps["example"].tweaks == [NEW]
    assert (world.cfg.tweaks_dir / NEW).read_bytes() == held


def test_a_download_that_isnt_a_deb_changes_nothing(world, monkeypatch) -> None:
    def html(_t, dest, *_a, **_k):
        path = dest / "a.deb"
        path.write_bytes(b"<html>503</html>")
        return path

    monkeypatch.setattr(canister, "fetch_deb", html)
    before = world.cfg_path.read_text()

    with pytest.raises(TweakFileError, match=r"isn't a \.deb"):
        _update(world)

    cfg = world.cfg
    assert world.cfg_path.read_text() == before
    assert sorted(p.name for p in cfg.tweaks_dir.iterdir() if not p.name.startswith(".")) == \
        ["a-tweak.deb", "b-tweak.deb"]


def test_a_download_of_another_package_changes_nothing(world, monkeypatch) -> None:
    monkeypatch.setattr(
        canister, "fetch_deb",
        lambda _t, dest, *_a, **_k: deb(dest / "x.deb", "com.example.btweak", "1.1"),
    )
    with pytest.raises(TweakFileError, match=r"com\.example\.btweak"):
        _update(world)
    assert {a.tweaks[0] for a in world.cfg.apps.values()} == {"a-tweak.deb"}


# --- an unverified source needs the person's confirmation -----------------------


def test_an_unverified_update_is_refused_before_anything_is_downloaded(world) -> None:
    world.serve_insecure("com.example.atweak")
    before = world.cfg_path.read_text()

    with pytest.raises(UntrustedSourceError, match="doesn't use HTTPS"):
        _update(world)

    assert world.fetches == []
    assert world.cfg_path.read_text() == before


def test_a_confirmed_unverified_update_runs_and_records_its_source(world) -> None:
    world.serve_insecure("com.example.atweak")

    done, _events = _update(world, trusted=True)

    cfg = world.cfg
    assert done.new_filename == NEW
    sha = tweak_sources.file_sha256(cfg.tweaks_dir / NEW)
    assert tweak_sources.recorded_for(cfg.state_dir, NEW, sha).url.startswith("http://")


def test_an_update_sweeps_what_a_killed_one_left(world) -> None:
    root = world.cfg.tweaks_dir / update_tweaks.TMP_DIRNAME
    old = root / "tweak-old"
    old.mkdir(parents=True)
    _aged(old, 2)

    _update(world)

    assert not old.exists()
