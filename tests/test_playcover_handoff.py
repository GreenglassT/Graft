"""Around a handoff to PlayCover: the person's settings, and the install wait
(spec 5d items 1, 2 and 5).

Measured with PlayCover 3.1.0: it writes `App Settings/<bundle id>.plist`
with `keymapping` and `playChain` on when it first installs an app, and leaves
an existing file alone on a reinstall. So Graft keeps the person's values,
puts them back if a PlayCover version rewrites them, and applies its defaults
(both off) only on an app's first handoff.

Every test here uses a scratch settings folder, a scratch state folder and a
scratch Applications folder, never PlayCover's container.
"""

from __future__ import annotations

import json
import os
import plistlib
import time
from pathlib import Path

import pytest

from graft_ipa import playcover_handoff as ph

BID = "com.example.app"


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    settings = tmp_path / "App Settings"
    settings.mkdir()
    return settings, tmp_path / "state"


def _write(settings: Path, values: dict, *, fmt=plistlib.FMT_XML) -> Path:
    path = settings / f"{BID}.plist"
    path.write_bytes(plistlib.dumps(values, fmt=fmt))
    return path


def _read(settings: Path) -> dict:
    return plistlib.loads((settings / f"{BID}.plist").read_bytes())


def _remember(state: Path, values: dict, bundle_id: str = BID) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / ph.MEMORY_FILE).write_text(json.dumps({"format": 1, "apps": {bundle_id: values}}))


def _memory(state: Path) -> dict:
    return json.loads((state / ph.MEMORY_FILE).read_text())["apps"]


def _no_sleep(_s: float) -> None:
    raise AssertionError("waited for a file that was already there")


class TestTheFileExistedBefore:
    def test_the_persons_values_are_left_alone(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False, "windowWidth": 1470})

        before = ph.snapshot(settings, state, BID)
        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert settled == ph.Settled(disabled=[], restored=[])
        assert _read(settings) == {"keymapping": True, "playChain": False, "windowWidth": 1470}

    def test_they_are_remembered_before_the_handoff(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False})
        ph.snapshot(settings, state, BID)
        assert _memory(state) == {BID: {"keymapping": True, "playChain": False}}

    def test_a_value_playcover_changed_is_put_back(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False, "windowWidth": 1470})
        before = ph.snapshot(settings, state, BID)
        _write(settings, {"keymapping": True, "playChain": True, "windowWidth": 1470})  # a rewrite

        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert settled == ph.Settled(disabled=[], restored=["playChain"])
        assert _read(settings) == {"keymapping": True, "playChain": False, "windowWidth": 1470}

    def test_the_file_wins_over_the_memory(self, dirs) -> None:
        """The memory is only the fallback for a file that's missing or
        unreadable: the file is the person's latest choice."""
        settings, state = dirs
        _remember(state, {"keymapping": True, "playChain": True})
        _write(settings, {"keymapping": False, "playChain": False})
        before = ph.snapshot(settings, state, BID)
        _write(settings, {"keymapping": True, "playChain": True})

        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert settled.restored == ["keymapping", "playChain"]
        assert _read(settings) == {"keymapping": False, "playChain": False}

    def test_a_binary_plist_stays_binary(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": False, "playChain": False}, fmt=plistlib.FMT_BINARY)
        before = ph.snapshot(settings, state, BID)
        _write(settings, {"keymapping": True, "playChain": False}, fmt=plistlib.FMT_BINARY)

        ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert (settings / f"{BID}.plist").read_bytes().startswith(b"bplist")
        assert _read(settings)["keymapping"] is False

    def test_a_file_that_turns_unreadable_is_left_and_never_raises(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False})
        before = ph.snapshot(settings, state, BID)
        (settings / f"{BID}.plist").write_bytes(b"half written")

        assert ph.settle(settings, state, BID, before, sleep=_no_sleep) == ph.Settled([], [])


class TestNoFileBefore:
    def _install(self, settings: Path, values: dict | None = None) -> None:
        _write(settings, values or {"keymapping": True, "playChain": True, "windowWidth": 1470})

    def test_the_first_handoff_turns_both_off_and_says_so(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        self._install(settings)

        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert settled == ph.Settled(disabled=["keymapping", "playChain"], restored=[])
        assert _read(settings) == {"keymapping": False, "playChain": False, "windowWidth": 1470}

    def test_only_the_keys_actually_turned_off_are_named(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        self._install(settings, {"keymapping": True, "playChain": False})
        assert ph.settle(settings, state, BID, before, sleep=_no_sleep).disabled == ["keymapping"]

    def test_the_defaults_are_remembered_so_the_notice_is_first_handoff_only(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        self._install(settings)
        ph.settle(settings, state, BID, before, sleep=_no_sleep)
        (settings / f"{BID}.plist").unlink()  # removed from PlayCover

        again = ph.snapshot(settings, state, BID)
        self._install(settings)

        assert ph.settle(settings, state, BID, again, sleep=_no_sleep) == ph.Settled([], [])
        assert _read(settings)["keymapping"] is False

    def test_remembered_values_are_applied_and_not_reported(self, dirs) -> None:
        settings, state = dirs
        _remember(state, {"keymapping": True, "playChain": False})
        before = ph.snapshot(settings, state, BID)
        self._install(settings)

        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert settled == ph.Settled(disabled=[], restored=[])
        assert _read(settings) == {"keymapping": True, "playChain": False, "windowWidth": 1470}

    def test_an_unreadable_file_falls_back_to_the_memory(self, dirs) -> None:
        settings, state = dirs
        _remember(state, {"keymapping": True, "playChain": False})
        (settings / f"{BID}.plist").write_bytes(b"not a plist")
        before = ph.snapshot(settings, state, BID)
        self._install(settings)

        ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert _read(settings) == {"keymapping": True, "playChain": False, "windowWidth": 1470}

    def test_it_waits_for_playcover_to_write_the_file(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        naps: list[float] = []

        def nap(seconds: float) -> None:
            naps.append(seconds)
            if len(naps) == 3:
                self._install(settings)

        settled = ph.settle(settings, state, BID, before, sleep=nap)

        assert len(naps) == 3
        assert settled.disabled == ["keymapping", "playChain"]

    def test_a_file_that_never_comes_is_nothing_and_never_raises(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        now = [0.0]

        def nap(seconds: float) -> None:
            now[0] += seconds

        settled = ph.settle(settings, state, BID, before, wait=5, sleep=nap, clock=lambda: now[0])

        assert settled == ph.Settled([], [])
        assert 5 <= now[0] <= 6

    def test_no_settings_folder_at_all_returns_at_once(self, tmp_path: Path) -> None:
        settings, state = tmp_path / "nowhere", tmp_path / "state"
        before = ph.snapshot(settings, state, BID)
        assert ph.settle(settings, state, BID, before, sleep=_no_sleep) == ph.Settled([], [])


class TestTheMemory:
    def test_other_apps_are_kept(self, dirs) -> None:
        settings, state = dirs
        _remember(state, {"keymapping": True, "playChain": True}, bundle_id="com.example.other")
        _write(settings, {"keymapping": False, "playChain": False})
        ph.snapshot(settings, state, BID)
        assert set(_memory(state)) == {BID, "com.example.other"}

    def test_a_memory_it_cannot_read_is_no_memory(self, dirs) -> None:
        settings, state = dirs
        state.mkdir()
        (state / ph.MEMORY_FILE).write_text("{not json")
        before = ph.snapshot(settings, state, BID)
        _write(settings, {"keymapping": True, "playChain": True})
        assert ph.settle(settings, state, BID, before, sleep=_no_sleep).disabled == [
            "keymapping", "playChain",
        ]

    def test_a_memory_that_cannot_be_written_never_raises(self, dirs, monkeypatch) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False})

        def refuse(*_a, **_k):
            raise OSError("read-only")

        monkeypatch.setattr(ph.data_dirs, "private_write", refuse)
        assert ph.snapshot(settings, state, BID).existed

    def test_it_is_owner_only(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False})
        ph.snapshot(settings, state, BID)
        assert (state / ph.MEMORY_FILE).stat().st_mode & 0o077 == 0


class TestTheWords:
    def test_both_keys(self) -> None:
        assert ph.disabled_notice(["keymapping", "playChain"], "Example") == (
            "Graft turned off PlayCover's keymapping and PlayChain for Example. For a game, "
            "turn keymapping back on in PlayCover; Graft keeps it from then on."
        )

    def test_only_playchain_leaves_out_the_game_sentence(self) -> None:
        assert ph.disabled_notice(["playChain"], "Example") == (
            "Graft turned off PlayCover's PlayChain for Example."
        )

    def test_only_keymapping(self) -> None:
        assert ph.disabled_notice(["keymapping"], "Example") == (
            "Graft turned off PlayCover's keymapping for Example. For a game, turn keymapping "
            "back on in PlayCover; Graft keeps it from then on."
        )

    def test_restored(self) -> None:
        assert ph.restored_line(["keymapping", "playChain"], "Example") == (
            "Graft put back your PlayCover keymapping and PlayChain settings for Example."
        )
        assert ph.restored_line(["playChain"], "Example") == (
            "Graft put back your PlayCover PlayChain setting for Example."
        )


class TestTheInstallWait:
    """`await_install`: PlayCover installed the app when its
    `Applications/<bundle id>.app/Info.plist` carries the handed IPA's
    version and build and was written after the handoff began."""

    def _installed(self, apps: Path, version: str = "2.0", build: str = "200") -> Path:
        info = apps / f"{BID}.app" / "Info.plist"
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_bytes(plistlib.dumps({
            "CFBundleIdentifier": BID, "CFBundleShortVersionString": version, "CFBundleVersion": build,
        }))
        return info

    def _clock(self) -> tuple[list[float], object, object]:
        now = [0.0]

        def nap(seconds: float) -> None:
            now[0] += seconds

        return now, nap, (lambda: now[0])

    def test_the_new_version_landing_is_the_install(self, tmp_path: Path) -> None:
        apps = tmp_path / "Applications"
        naps: list[float] = []

        def nap(seconds: float) -> None:
            naps.append(seconds)
            if len(naps) == 2:
                self._installed(apps)

        assert ph.await_install(
            apps, BID, version="2.0", build="200", began=time.time() - 1, sleep=nap,
        )
        assert len(naps) == 2

    def test_an_earlier_install_of_the_same_version_is_not_this_one(self, tmp_path: Path) -> None:
        """A reinstall of the same build: the file already says 2.0 (200), but
        it was written before the handoff began."""
        apps = tmp_path / "Applications"
        self._installed(apps)
        now, nap, clock = self._clock()

        assert not ph.await_install(
            apps, BID, version="2.0", build="200", began=time.time() + 1,
            timeout=120, sleep=nap, clock=clock,
        )
        assert 120 <= now[0] <= 121

    def test_a_kept_archive_date_still_counts_as_new(self, tmp_path: Path) -> None:
        """An unzip that keeps the archive's dates leaves the mtime in the
        past; the file's ctime is when it was written."""
        apps = tmp_path / "Applications"
        began = time.time() - 1
        info = self._installed(apps)
        os.utime(info, (began - 86400, began - 86400))

        assert ph.await_install(apps, BID, version="2.0", build="200", began=began, sleep=_no_sleep)

    @pytest.mark.parametrize("version,build", [("1.9", "200"), ("2.0", "199")])
    def test_another_version_or_build_is_not_the_install(self, tmp_path: Path, version, build) -> None:
        apps = tmp_path / "Applications"
        self._installed(apps, version=version, build=build)
        _, nap, clock = self._clock()
        assert not ph.await_install(
            apps, BID, version="2.0", build="200", began=time.time() - 1,
            timeout=3, sleep=nap, clock=clock,
        )

    def test_nothing_there_waits_out_the_timeout(self, tmp_path: Path) -> None:
        now, nap, clock = self._clock()
        assert not ph.await_install(
            tmp_path / "nowhere", BID, version="2.0", build="200", began=time.time(),
            timeout=120, sleep=nap, clock=clock,
        )
        assert 120 <= now[0] <= 121

    def test_a_half_written_info_plist_is_not_yet_the_install(self, tmp_path: Path) -> None:
        apps = tmp_path / "Applications"
        info = apps / f"{BID}.app" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(b"bplist00 half")
        _, nap, clock = self._clock()
        assert not ph.await_install(
            apps, BID, version="2.0", build="200", began=time.time() - 1,
            timeout=2, sleep=nap, clock=clock,
        )

    def test_the_wait_is_two_minutes(self) -> None:
        assert ph.INSTALL_WAIT_S == 120


def test_the_handed_archives_version_and_build(tmp_path: Path) -> None:
    import zipfile

    ipa = tmp_path / "x.ipa"
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("Payload/X.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": BID, "CFBundleShortVersionString": "2.0", "CFBundleVersion": "200",
        }))
        zf.writestr("Payload/X.app/PlugIns/W.appex/Info.plist", plistlib.dumps({
            "CFBundleShortVersionString": "9", "CFBundleVersion": "9",
        }))
    assert ph.archive_versions(ipa) == ("2.0", "200")


class TestAnUntrustedBundleId:
    """The bundle id comes from the handed IPA's Info.plist, which a dropped
    IPA can set to anything. Nothing keyed on it may reach outside
    PlayCover's folders, or follow a link planted there."""

    @pytest.mark.parametrize("bundle_id", [
        "../../escape", "..", "com..example", "/etc/x", "com/example", ".hidden", "-x",
        "com.example app", "com.example\n", "", "a" * 256,
    ])
    def test_these_are_refused(self, bundle_id: str) -> None:
        assert not ph.safe_bundle_id(bundle_id)

    @pytest.mark.parametrize("bundle_id", ["com.example.app", "com.example-app.Plus2", "A1"])
    def test_these_are_bundle_ids(self, bundle_id: str) -> None:
        assert ph.safe_bundle_id(bundle_id)

    def test_a_traversing_bundle_id_writes_nothing_outside_the_settings_folder(
        self, tmp_path: Path
    ) -> None:
        settings = tmp_path / "Container" / "App Settings"
        settings.mkdir(parents=True)
        state = tmp_path / "state"
        bid = "../../escape"
        outside = tmp_path / "escape.plist"
        outside.write_bytes(plistlib.dumps({"keymapping": True, "playChain": True}))
        before_bytes = outside.read_bytes()

        before = ph.snapshot(settings, state, bid)
        settled = ph.settle(settings, state, bid, before, sleep=_no_sleep)

        assert before == ph.Before(existed=False, values=None)
        assert settled == ph.Settled()
        assert outside.read_bytes() == before_bytes
        assert sorted(p.name for p in tmp_path.iterdir()) == ["Container", "escape.plist"]

    def test_a_traversing_bundle_id_is_never_confirmed_or_waited_for(self, tmp_path: Path) -> None:
        apps = tmp_path / "Container" / "Applications"
        apps.mkdir(parents=True)
        planted = tmp_path / "escape.app" / "Info.plist"
        planted.parent.mkdir()
        planted.write_bytes(plistlib.dumps({"CFBundleShortVersionString": "2.0", "CFBundleVersion": "200"}))

        assert not ph.await_install(
            apps, "../../escape", version="2.0", build="200", began=time.time() - 1, sleep=_no_sleep,
        )

    def test_a_settings_file_linked_elsewhere_is_left_alone(self, dirs, tmp_path: Path) -> None:
        settings, state = dirs
        outside = tmp_path / "outside.plist"
        outside.write_bytes(plistlib.dumps({"keymapping": True, "playChain": True}))
        before_bytes = outside.read_bytes()
        link = settings / f"{BID}.plist"
        link.symlink_to(outside)

        before = ph.snapshot(settings, state, BID)
        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep)

        assert settled == ph.Settled()
        assert link.is_symlink(), "nothing was done to it"
        assert outside.read_bytes() == before_bytes

    def test_a_link_whose_target_appears_during_the_install_is_left_alone(
        self, dirs, tmp_path: Path
    ) -> None:
        """Followed, the first-handoff branch would read the outside file,
        remember what it says and rewrite the link's name with defaults."""
        settings, state = dirs
        outside = tmp_path / "outside.plist"
        link = settings / f"{BID}.plist"
        link.symlink_to(outside)  # dangling at the snapshot
        before = ph.snapshot(settings, state, BID)
        outside.write_bytes(plistlib.dumps({"keymapping": True, "playChain": True}))
        before_bytes = outside.read_bytes()

        settled = ph.settle(settings, state, BID, before, sleep=_no_sleep, wait=0)

        assert settled == ph.Settled()
        assert link.is_symlink()
        assert outside.read_bytes() == before_bytes
        assert not (state / ph.MEMORY_FILE).exists(), "nothing read through it was remembered"

    def test_a_write_replaces_a_link_rather_than_following_it(self, dirs) -> None:
        settings, _ = dirs
        target = settings / "other.plist"
        target.write_bytes(plistlib.dumps({"keymapping": True}))
        link = settings / f"{BID}.plist"
        link.symlink_to(target)

        assert ph._write_keys(link, {"keymapping": True}, plistlib.FMT_XML, {"keymapping": False})

        assert not link.is_symlink()
        assert _read(settings) == {"keymapping": False}
        assert plistlib.loads(target.read_bytes()) == {"keymapping": True}, "the link's target is untouched"


# ---- review fixes (2026-11-26) ----

#: What a settings file or Info.plist looks like halfway through being
#: written: `plistlib` raises `xml.parsers.expat.ExpatError` for it, which is
#: no `ValueError`.
HALF_XML = b'<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>keymapping</key>'


class TestHalfWrittenXML:
    def test_a_half_written_settings_file_is_unreadable_not_an_error(self, dirs) -> None:
        settings, state = dirs
        (settings / f"{BID}.plist").write_bytes(HALF_XML)
        assert ph.snapshot(settings, state, BID).existed is False

    def test_settling_against_a_racing_write_never_raises(self, dirs) -> None:
        settings, state = dirs
        _write(settings, {"keymapping": True, "playChain": False})
        before = ph.snapshot(settings, state, BID)
        (settings / f"{BID}.plist").write_bytes(HALF_XML)
        assert ph.settle(settings, state, BID, before, sleep=_no_sleep) == ph.Settled()

    def test_a_first_install_mid_write_is_waited_out(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        (settings / f"{BID}.plist").write_bytes(HALF_XML)
        naps: list[float] = []

        def nap(seconds: float) -> None:
            naps.append(seconds)
            _write(settings, {"keymapping": True, "playChain": True})

        assert ph.settle(settings, state, BID, before, sleep=nap).disabled == ["keymapping", "playChain"]
        assert len(naps) == 1

    def test_a_half_written_installed_info_plist_is_not_yet_the_install(self, tmp_path: Path) -> None:
        apps = tmp_path / "Applications"
        info = apps / f"{BID}.app" / "Info.plist"
        info.parent.mkdir(parents=True)
        info.write_bytes(HALF_XML)
        now = [0.0]

        def nap(seconds: float) -> None:
            now[0] += seconds

        assert not ph.await_install(
            apps, BID, version="2.0", build="200", began=time.time() - 1,
            timeout=2, sleep=nap, clock=lambda: now[0],
        )


class TestFirstHandoffDefaultsAreNeverLost:
    """A first handoff whose settle doesn't finish (a Stop during the wait,
    the settings wait running out, no settings folder yet, a write that
    fails) must not leave PlayCover's own defaults to be adopted as the
    person's choice next time (review #1)."""

    def test_a_first_handoff_that_never_settles_is_finished_by_the_next(self, dirs) -> None:
        settings, state = dirs
        ph.snapshot(settings, state, BID)  # first handoff; then a Stop, no settle
        _write(settings, {"keymapping": True, "playChain": True, "windowWidth": 1470})  # PlayCover's

        again = ph.snapshot(settings, state, BID)
        settled = ph.settle(settings, state, BID, again, sleep=_no_sleep)

        assert settled.disabled == ["keymapping", "playChain"], "the notice, once"
        assert _read(settings) == {"keymapping": False, "playChain": False, "windowWidth": 1470}

    def test_without_a_settings_folder_the_defaults_wait_for_one(self, tmp_path: Path) -> None:
        settings, state = tmp_path / "App Settings", tmp_path / "state"
        before = ph.snapshot(settings, state, BID)
        assert ph.settle(settings, state, BID, before, sleep=_no_sleep) == ph.Settled()

        settings.mkdir()
        _write(settings, {"keymapping": True, "playChain": True})
        again = ph.snapshot(settings, state, BID)
        assert ph.settle(settings, state, BID, again, sleep=_no_sleep).disabled == [
            "keymapping", "playChain",
        ]

    def test_once_applied_the_persons_later_choice_is_kept(self, dirs) -> None:
        settings, state = dirs
        before = ph.snapshot(settings, state, BID)
        _write(settings, {"keymapping": True, "playChain": True})
        assert ph.settle(settings, state, BID, before, sleep=_no_sleep).disabled
        _write(settings, {"keymapping": True, "playChain": False})  # a game: keymapping back on

        again = ph.snapshot(settings, state, BID)
        settled = ph.settle(settings, state, BID, again, sleep=_no_sleep)

        assert settled == ph.Settled()
        assert _read(settings) == {"keymapping": True, "playChain": False}

    def test_a_remembered_app_is_never_pending(self, dirs) -> None:
        settings, state = dirs
        _remember(state, {"keymapping": True, "playChain": False})
        ph.snapshot(settings, state, BID)  # no file: the memory's values, not the defaults
        _write(settings, {"keymapping": True, "playChain": True})
        again = ph.snapshot(settings, state, BID)
        assert ph.settle(settings, state, BID, again, sleep=_no_sleep) == ph.Settled()
