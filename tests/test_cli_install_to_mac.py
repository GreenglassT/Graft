"""The `--install-to-mac` verb: `_do_install_to_mac`.

A handoff, not an install. PlayCover re-signs ad-hoc and rewrites iOS
entitlements as macOS sandbox ones — verified by comparing a raw `Payload/X.app`
(`TeamIdentifier=ABCDE12345`, `application-identifier`) against PlayCover's
output (adhoc, `com.apple.security.app-sandbox`). Doing that ourselves would be
a second, worse copy of the one thing PlayCover is for.

So these tests are about what we hand over and what we refuse to hand over,
never about the install itself. `subprocess.run` is patched throughout; nothing
here launches PlayCover.
"""

from __future__ import annotations

import json
import plistlib
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa.errors import ConfigError
from graft_ipa.json_output import EVENT_TYPES, JsonEmitter

PLAYCOVER_ID = "io.playcover.PlayCover"

#: What `open -b` actually prints for a bundle id nothing has registered —
#: measured with `open -b com.example.nonexistent <file>`, not invented. An
#: earlier draft of `test_playcover_missing_fails_naming_it` quoted "Unable to
#: find application with bundle identifier", a sentence `open` never emits;
#: fixing only the production code's comment and leaving this fixture wrong
#: would have meant neither side could ever catch the other drifting.
OPEN_MISSING_BUNDLE_STDERR = (
    "LSCopyApplicationURLsForBundleIdentifier() failed while trying to "
    f"determine the application with bundle identifier {PLAYCOVER_ID}.\n"
)


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _ipa(cache: Path, bundle_id: str = "com.example.app", version: str = "1.0") -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{bundle_id}_{version}.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleDisplayName": "Fake",
        "CFBundleExecutable": "Fake",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    return path


def _cfg(cache: Path, built: Path | None = None) -> SimpleNamespace:
    # `built_dir` defaults beside the cache and is not created: the verb has to
    # cope with it being absent, which is the state on any machine that has
    # never run a build.
    return SimpleNamespace(cache_dir=cache, built_dir=built or cache.parent / "built")


def test_the_event_is_registered() -> None:
    assert "handed-to-playcover" in EVENT_TYPES


def test_it_opens_the_file_with_playcovers_bundle_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Somewhere with no settings dir, so the PlayCover-settings step returns at
    # once. Without this the case pays the full poll timeout waiting for a
    # plist that a stubbed `open` never creates — 25s of the suite's runtime,
    # measured, for no coverage.
    monkeypatch.setattr(
        cli_mod, "PLAYCOVER_SETTINGS_DIR", tmp_path / "no-playcover-here"
    )
    cache = tmp_path / "cache"
    path = _ipa(cache)
    buf = StringIO()
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        cli_mod._do_install_to_mac(
            _cfg(cache), filename=path.name, emitter=JsonEmitter(stream=buf)
        )
    argv = run.call_args[0][0]
    assert argv[:3] == ["open", "-b", PLAYCOVER_ID]
    assert argv[-1] == str(path)

    event = _events(buf.getvalue())[0]
    assert event["type"] == "handed-to-playcover"
    assert event["filename"] == path.name
    assert event["bundle_id"] == "com.example.app"


def test_a_filename_not_in_the_cache_fails_without_opening_anything(
    tmp_path: Path,
) -> None:
    """Pins what the `target.is_file()` guard actually buys.

    Drop that guard and `read_info_plist` still raises on the nonexistent
    file — `pytest.raises(Exception)` alone cannot tell the two apart. What
    the guard buys is a *named* refusal: `ConfigError` (exit 2, not
    `IPAInspectionError`'s 5) with a sentence naming the filename and
    `cache_dir` and the `--staged-ipas` hint, all pinned here so a version
    that let the read fail through instead is caught.
    """
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    with patch.object(cli_mod.subprocess, "run") as run:
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(
                _cfg(cache), filename="not-here.ipa", emitter=None
            )
    run.assert_not_called()
    assert e.value.exit_code == 2
    assert "not-here.ipa" in e.value.message
    assert str(cache) in e.value.message
    assert "--staged-ipas" in (e.value.hint or "")


def test_a_path_outside_the_cache_is_refused(tmp_path: Path) -> None:
    """A *real* IPA outside `cache_dir`, not a missing one.

    If the filename guard were dropped, `target.is_file()` would resolve
    `cache_dir / "../elsewhere.ipa"` right to it and the verb would sail on to
    hand it to PlayCover — so unlike a merely-absent path, this is the case
    where a missing guard and a present one actually disagree on whether
    `subprocess.run` gets called at all.
    """
    cache = tmp_path / "cache"
    cache.mkdir(parents=True)
    outside = _ipa(tmp_path, bundle_id="com.someone.else")
    with patch.object(cli_mod.subprocess, "run") as run:
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(
                _cfg(cache), filename=f"../{outside.name}", emitter=None
            )
    run.assert_not_called()
    assert e.value.exit_code == 2
    assert "not a filename" in e.value.message


def test_playcover_missing_fails_naming_it(tmp_path: Path) -> None:
    """`open -b` on an unregistered bundle id exits non-zero with its own
    message (`OPEN_MISSING_BUNDLE_STDERR`, measured — see that constant's own
    comment). That must reach the user as a sentence naming PlayCover, not as
    a bare non-zero exit from a command they never typed.

    Asserted against `e.value.message` specifically, not `str(e.value)`:
    `GraftError.__str__` appends the hint, and the hint already carries
    `open`'s raw stderr verbatim (see `_do_install_to_mac`) — so a "PlayCover"
    check against the stringified exception would pass even if the *message*
    were replaced by a raw pass-through of that stderr.

    And an exact match, not a substring — `OPEN_MISSING_BUNDLE_STDERR` itself
    contains the literal bundle id `io.playcover.PlayCover`, so a bare
    `"PlayCover" in e.value.message` is satisfied by the raw stderr
    pass-through too (its bundle-id mention lexically contains the word) and
    would not have caught that mutation. Only an exact match to the named
    sentence tells the two apart.
    """
    cache = tmp_path / "cache"
    path = _ipa(cache)
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(
            returncode=1, stdout="",
            stderr=OPEN_MISSING_BUNDLE_STDERR,
        )
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(_cfg(cache), filename=path.name, emitter=None)
    assert e.value.message == "PlayCover is not installed, or could not launch"


def test_on_an_intel_mac_a_failed_handoff_says_playcover_needs_apple_silicon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The release app is universal, so it runs on Intel Macs, where PlayCover
    cannot run at all. "Not installed, or could not launch" sends that user off
    to install something that will never work; the reason is the processor."""
    from graft_ipa import host

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: None)  # no such sysctl: Intel
    cache = tmp_path / "cache"
    path = _ipa(cache)
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr=OPEN_MISSING_BUNDLE_STDERR,
        )
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(_cfg(cache), filename=path.name, emitter=None)
    assert "Apple silicon" in e.value.message
    assert "Intel" in e.value.message


def test_it_turns_off_the_two_settings_that_make_a_handed_over_app_unusable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both default to True and both have to go.

    `playChain` crashes the app in PlayTools' keychain shim on its first
    credential write; `keymapping` translates the mouse into mapped touch, so
    every click on a non-game silently misses. Measured on a real handoff:
    with only the first fixed the app ran fine and could not be clicked, which
    is why this asserts on *both* rather than on "settings were touched".

    Keys this does not know about must survive — PlayCover owns this file and
    a round-trip that dropped its other settings would be worse than not
    writing at all.
    """
    cache = tmp_path / "cache"
    path = _ipa(cache)
    settings = tmp_path / "App Settings"
    settings.mkdir()
    plist = settings / "com.example.app.plist"
    with plist.open("wb") as fh:
        plistlib.dump(
            {"playChain": True, "keymapping": True, "windowWidth": 1470}, fh
        )
    monkeypatch.setattr(cli_mod, "PLAYCOVER_SETTINGS_DIR", settings)

    buf = StringIO()
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        cli_mod._do_install_to_mac(
            _cfg(cache), filename=path.name, emitter=JsonEmitter(stream=buf)
        )

    with plist.open("rb") as fh:
        after = plistlib.load(fh)
    assert after["playChain"] is False
    assert after["keymapping"] is False
    assert after["windowWidth"] == 1470, "PlayCover's other settings survive"

    event = _events(buf.getvalue())[0]
    assert "playChain" in event["settings_disabled"]
    assert "keymapping" in event["settings_disabled"]


def test_a_handoff_still_succeeds_when_playcover_never_writes_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The settings step is a courtesy, never a precondition.

    PlayCover's container layout is undocumented and can move; the design doc's
    §6 refused to depend on it at all. Depending on it *softly* is the reversal
    recorded there, and this is the case that keeps it soft: a missing file
    leaves the handoff true, because the handoff was true.
    """
    cache = tmp_path / "cache"
    path = _ipa(cache)
    settings = tmp_path / "App Settings"
    settings.mkdir()  # exists, but PlayCover writes nothing into it
    monkeypatch.setattr(cli_mod, "PLAYCOVER_SETTINGS_DIR", settings)
    monkeypatch.setattr(cli_mod, "_tame_playcover_settings", lambda _bid, **_k: None)

    buf = StringIO()
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        cli_mod._do_install_to_mac(
            _cfg(cache), filename=path.name, emitter=JsonEmitter(stream=buf)
        )
    event = _events(buf.getvalue())[0]
    assert event["type"] == "handed-to-playcover"
    assert "settings_disabled" not in event, "absent, not empty or false"


def test_a_settings_file_it_cannot_read_never_fails_the_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "never fatal" promise, which is the whole basis for depending on
    PlayCover's container at all.

    Pins it against a *corrupt* file rather than a missing one: the missing
    case returns early and would pass with no exception handling whatsoever.
    Found by mutation — deleting the handler entirely breaks this case and
    nothing else.

    Note the mutation that *looks* right here is not: narrowing the handler to
    `except ValueError` changes nothing, because `plistlib.InvalidFile
    Exception` subclasses `ValueError`. That reads as a surviving mutation and
    is really a no-op, which is its own small trap — check what an exception
    actually inherits before concluding a handler is untested.
    """
    cache = tmp_path / "cache"
    path = _ipa(cache)
    settings = tmp_path / "App Settings"
    settings.mkdir()
    (settings / "com.example.app.plist").write_bytes(b"this is not a plist")
    monkeypatch.setattr(cli_mod, "PLAYCOVER_SETTINGS_DIR", settings)

    buf = StringIO()
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        cli_mod._do_install_to_mac(
            _cfg(cache), filename=path.name, emitter=JsonEmitter(stream=buf)
        )

    event = _events(buf.getvalue())[0]
    assert event["type"] == "handed-to-playcover", "the handoff still happened"
    assert "settings_disabled" not in event, "and does not claim otherwise"


class TestReachingABuiltIPA:
    """`--install-to-mac` resolves in `built_dir` as well as `cache_dir`.

    Without this the verb could only ever hand over the *untweaked* dump — a
    strange gap in a feature whose point is running your apps on this Mac, and
    exactly the artifact `--for-playcover` exists to replace.
    """

    @pytest.fixture(autouse=True)
    def _handoffs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "tmpdir"
        root.mkdir()
        monkeypatch.setattr(cli_mod, "_handoff_root", lambda: root)
        return root

    def _hand_over(self, tmp_path: Path, path: Path, *, during=None):
        cache = tmp_path / "cache"
        cache.mkdir(parents=True, exist_ok=True)

        def opened(argv, **_):
            if during is not None:
                during(Path(argv[-1]))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod, "PLAYCOVER_SETTINGS_DIR", tmp_path / "no-playcover"), \
                patch.object(cli_mod.subprocess, "run", side_effect=opened) as run:
            cli_mod._do_install_to_mac(
                _cfg(cache, path.parent), filename=path.name, emitter=None
            )
        return Path(run.call_args[0][0][-1])

    def test_a_built_ipa_is_found_and_handed_over(self, tmp_path: Path, _handoffs: Path) -> None:
        path = _ipa(tmp_path / "built")  # only in built_dir, not staged

        handed = self._hand_over(tmp_path, path)

        assert handed.name == path.name
        assert _handoffs in handed.parents, "PlayCover got a private copy"
        assert handed.read_bytes()[:2] == b"PK"

    def test_a_sweep_after_the_handoff_cannot_take_the_file_playcover_reads(
        self, tmp_path: Path
    ) -> None:
        """Review #10: `open` returns before PlayCover reads the file, so a
        clean-up that moved the archive in that window left PlayCover a
        missing file. It reads a copy no sweep looks at."""
        path = _ipa(tmp_path / "built")

        handed = self._hand_over(tmp_path, path)
        path.unlink()  # what a sweep at archive_keep: 1 does to the archive

        assert handed.is_file()

    def test_the_copy_is_made_under_a_share_of_the_retention_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _ipa(tmp_path / "built")
        cfg = _cfg(tmp_path / "cache", path.parent)
        seen: list[bool] = []
        real_copy = cli_mod.shutil.copyfile

        def copying(src, dst):
            sweep = cli_mod._RetentionLock(cfg)
            seen.append(sweep.acquire(exclusive=True, wait=0))
            sweep.release()
            return real_copy(src, dst)

        monkeypatch.setattr(cli_mod.shutil, "copyfile", copying)
        self._hand_over(tmp_path, path)

        assert seen == [False], "a sweep could take the lock while the archive was copied"
        after = cli_mod._RetentionLock(cfg)
        assert after.acquire(exclusive=True, wait=0), "the share outlived the copy"
        after.release()

    def test_the_archive_itself_is_left_as_it_was_built(self, tmp_path: Path) -> None:
        """The account data comes out of the copy PlayCover gets; the archive
        was rewritten in place before."""
        path = _ipa(tmp_path / "built")
        with zipfile.ZipFile(path, "a") as zf:
            zf.writestr("iTunesMetadata.plist", plistlib.dumps({"appleId": "buyer@example.invalid"}))
        before = path.read_bytes()

        handed = self._hand_over(tmp_path, path)

        assert path.read_bytes() == before
        with zipfile.ZipFile(handed) as zf:
            assert "iTunesMetadata.plist" not in zf.namelist()

    def test_old_handoff_copies_are_cleared_and_recent_ones_kept(
        self, tmp_path: Path, _handoffs: Path
    ) -> None:
        import os
        import time

        stale = _handoffs / "graft-playcover-old"
        fresh = _handoffs / "graft-playcover-new"
        for d in (stale, fresh):
            d.mkdir()
            (d / "x.ipa").write_bytes(b"x")
        long_ago = time.time() - 2 * 24 * 3600
        os.utime(stale, (long_ago, long_ago))

        self._hand_over(tmp_path, _ipa(tmp_path / "built"))

        assert not stale.exists()
        assert fresh.exists(), "PlayCover may not have read a recent one yet"

    def test_a_staged_dump_is_still_handed_over_where_it_is(
        self, tmp_path: Path, _handoffs: Path
    ) -> None:
        """Only a built archive is at retention's mercy."""
        staged = _ipa(tmp_path / "cache")
        with patch.object(cli_mod, "PLAYCOVER_SETTINGS_DIR", tmp_path / "nope"), \
                patch.object(cli_mod.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            cli_mod._do_install_to_mac(
                _cfg(staged.parent, tmp_path / "built"), filename=staged.name, emitter=None
            )
        assert run.call_args[0][0][-1] == str(staged)
        assert list(_handoffs.iterdir()) == []

    def test_the_cache_wins_when_both_hold_the_name(self, tmp_path: Path) -> None:
        """`cache_dir` is what the Cache pane lists, so a bare filename most
        likely means that one. Pinned because the fallback is written as an
        `if not is_file()` and inverting that order is a one-line edit no other
        case would notice."""
        cache = tmp_path / "cache"
        built = tmp_path / "built"
        staged = _ipa(cache)
        _ipa(built)  # same filename, different directory
        with patch.object(cli_mod, "PLAYCOVER_SETTINGS_DIR", tmp_path / "nope"), \
                patch.object(cli_mod.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            cli_mod._do_install_to_mac(
                _cfg(cache, built), filename=staged.name, emitter=None
            )
        assert run.call_args[0][0][-1] == str(staged), "the staged one, not the build"

    def test_a_name_in_neither_place_names_both(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        cache.mkdir(parents=True)
        built = tmp_path / "built"
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(
                _cfg(cache, built), filename="nowhere.ipa", emitter=None
            )
        assert str(cache) in e.value.message
        assert str(built) in e.value.message, "both places, so the user can look"
