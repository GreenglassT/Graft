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


def _ipa(
    cache: Path, bundle_id: str = "com.example.app", version: str = "1.0", *,
    build: str = "100", frameworks: dict[str, bytes] | None = None, name: str | None = None,
) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / (name or f"{bundle_id.replace('/', '_')}_{version}.ipa")
    info = plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleVersion": build,
        "CFBundleDisplayName": "Fake",
        "CFBundleExecutable": "Fake",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
        for member, data in (frameworks or {}).items():
            zf.writestr(f"Payload/Fake.app/Frameworks/{member}", data)
    return path


def _cfg(cache: Path, built: Path | None = None) -> SimpleNamespace:
    # `built_dir` defaults beside the cache and is not created: the verb has to
    # cope with it being absent, which is the state on any machine that has
    # never run a build.
    root = cache.parent
    tweaks = root / "tweaks"
    return SimpleNamespace(
        cache_dir=cache, built_dir=built or root / "built", state_dir=root / "state",
        tweaks_dir=tweaks, tweak_path=lambda name: tweaks / name,
    )


@pytest.fixture(autouse=True)
def _playcover_here(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PlayCover's folders, as scratch ones, and no waiting: the install wait
    and the settings wait each look once. A test that wants settings points
    `PLAYCOVER_SETTINGS_DIR` at a folder it made; without one, the settings
    step has nothing to do. Returns the Applications folder."""
    from graft_ipa import playcover_handoff

    apps = tmp_path / "PlayCover" / "Applications"
    monkeypatch.setattr(cli_mod, "PLAYCOVER_APPS_DIR", apps)
    monkeypatch.setattr(cli_mod, "PLAYCOVER_SETTINGS_DIR", tmp_path / "PlayCover" / "no-settings")
    monkeypatch.setattr(playcover_handoff, "INSTALL_WAIT_S", 0)
    monkeypatch.setattr(playcover_handoff, "SETTINGS_WAIT_S", 0)
    return apps


def _install(apps: Path, bundle_id: str = "com.example.app", version: str = "1.0",
             build: str = "100") -> None:
    """What PlayCover does once it has the file: its own copy of the app."""
    info = apps / f"{bundle_id}.app" / "Info.plist"
    info.parent.mkdir(parents=True, exist_ok=True)
    info.write_bytes(plistlib.dumps({
        "CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version,
        "CFBundleVersion": build,
    }))


def test_the_events_are_registered() -> None:
    assert {"handed-to-playcover", "playcover-installed",
            "playcover-install-unconfirmed"} <= EVENT_TYPES


def test_it_opens_the_file_with_playcovers_bundle_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    assert event["display_name"] == "Fake"


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


def test_playcover_missing_hint_names_the_releases_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Graft doesn't include PlayCover, so the hint says where it comes from:
    PlayCover's official releases page. `open`'s own words still follow, and
    the message and exit code are unchanged, so the error's shape is too."""
    from graft_ipa import host

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)  # Apple silicon
    cache = tmp_path / "cache"
    path = _ipa(cache)
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr=OPEN_MISSING_BUNDLE_STDERR,
        )
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(_cfg(cache), filename=path.name, emitter=None)
    assert cli_mod.PLAYCOVER_RELEASES_URL == "https://github.com/PlayCover/PlayCover/releases"
    assert e.value.message == "PlayCover is not installed, or could not launch"
    assert e.value.exit_code == 2
    assert e.value.hint.startswith(
        "get PlayCover from https://github.com/PlayCover/PlayCover/releases and install it"
    )
    assert f"`open -b {PLAYCOVER_ID}` exited 1: {OPEN_MISSING_BUNDLE_STDERR.strip()}" in e.value.hint


def test_playcover_missing_in_json_mode_prints_no_event_and_the_hint_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The JSON contract is unchanged: a failed handoff puts no event on
    stdout, and its error and hint go to stderr through `_fatal`, with exit 2,
    which is where the app reads them. `open` is stubbed: nothing here asks
    LaunchServices or launches anything."""
    from graft_ipa import host

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)  # Apple silicon
    cache = tmp_path / "cache"
    path = _ipa(cache)
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr=OPEN_MISSING_BUNDLE_STDERR,
        )
        with pytest.raises(ConfigError) as e:
            cli_mod._verb_install_to_mac(
                cfg=_cfg(cache), install_to_mac=path.name, json_mode=True
            )
        exit_ = cli_mod._fatal(e.value)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert exit_.exit_code == 2
    err = " ".join(captured.err.split())  # Rich wraps to the terminal's width
    assert "error: PlayCover is not installed, or could not launch" in err
    assert "hint: get PlayCover from https://github.com/PlayCover/PlayCover/releases" in err


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


def test_in_the_intel_build_a_failed_handoff_says_which_download_to_get(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Intel build of Graft on an Apple silicon Mac has no PlayCover
    handover: the fix is Graft.zip, not installing PlayCover."""
    from graft_ipa import host

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)  # Apple silicon
    monkeypatch.setattr(host, "_running_flavor", lambda: "x86_64")
    cache = tmp_path / "cache"
    path = _ipa(cache)
    with patch.object(cli_mod.subprocess, "run") as run:
        run.return_value = SimpleNamespace(
            returncode=1, stdout="", stderr=OPEN_MISSING_BUNDLE_STDERR,
        )
        with pytest.raises(ConfigError) as e:
            cli_mod._do_install_to_mac(_cfg(cache), filename=path.name, emitter=None)
    assert e.value.message == ("PlayCover can't be handed this build here: this is the Intel build "
                               "of Graft; on an Apple silicon Mac, download Graft.zip")
    assert "get PlayCover" not in (e.value.hint or "")


class TestTheInstallAndTheSettings:
    """After `open`: the wait for PlayCover's install, then the app's two
    PlayCover settings (spec 5d items 1, 2 and 5)."""

    def _hand_over(self, tmp_path: Path, *, during=None, path: Path | None = None,
                   emitter: bool = True) -> list[dict]:
        cache = tmp_path / "cache"
        path = path or _ipa(cache)

        def opened(argv, **_):
            if during is not None:
                during()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        buf = StringIO()
        with patch.object(cli_mod.subprocess, "run", side_effect=opened):
            cli_mod._do_install_to_mac(
                _cfg(cache), filename=path.name,
                emitter=JsonEmitter(stream=buf) if emitter else None,
            )
        return _events(buf.getvalue())

    def _settings(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        settings = tmp_path / "App Settings"
        settings.mkdir()
        monkeypatch.setattr(cli_mod, "PLAYCOVER_SETTINGS_DIR", settings)
        return settings

    def test_the_handoff_comes_at_once_and_then_the_install(
        self, tmp_path: Path, _playcover_here: Path
    ) -> None:
        events = self._hand_over(tmp_path, during=lambda: _install(_playcover_here))

        assert [e["type"] for e in events] == ["handed-to-playcover", "playcover-installed"]
        assert events[1] == {
            "type": "playcover-installed", "bundle_id": "com.example.app", "version": "1.0",
            "display_name": "Fake", "settings_disabled": [], "settings_restored": [],
        }

    def test_an_install_that_doesnt_land_is_unconfirmed_not_a_failure(self, tmp_path: Path) -> None:
        events = self._hand_over(tmp_path)
        assert events[-1] == {
            "type": "playcover-install-unconfirmed", "bundle_id": "com.example.app",
            "waited_s": 0, "display_name": "Fake",
            "settings_disabled": [], "settings_restored": [],
        }

    def test_an_earlier_install_of_the_same_build_is_not_this_one(
        self, tmp_path: Path, _playcover_here: Path
    ) -> None:
        _install(_playcover_here)
        old = _playcover_here / "com.example.app.app" / "Info.plist"
        import os
        import time
        os.utime(old, (time.time() - 60, time.time() - 60))
        events = self._hand_over(tmp_path)
        assert events[-1]["type"] == "playcover-install-unconfirmed"

    def test_the_first_handoff_turns_both_settings_off_and_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _playcover_here: Path
    ) -> None:
        """Both default to on, and on a non-game both break it: PlayChain
        crashed apps the first time they saved a login, and keymapping makes
        every click miss. Keys this doesn't know survive."""
        settings = self._settings(tmp_path, monkeypatch)

        def playcover_installs() -> None:
            (settings / "com.example.app.plist").write_bytes(plistlib.dumps(
                {"playChain": True, "keymapping": True, "windowWidth": 1470}))
            _install(_playcover_here)

        events = self._hand_over(tmp_path, during=playcover_installs)

        after = plistlib.loads((settings / "com.example.app.plist").read_bytes())
        assert after == {"playChain": False, "keymapping": False, "windowWidth": 1470}
        assert events[-1]["settings_disabled"] == ["keymapping", "playChain"]
        assert events[-1]["settings_restored"] == []

    def test_the_persons_own_settings_are_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _playcover_here: Path
    ) -> None:
        """Measured with PlayCover 3.1.0: a reinstall leaves the file alone,
        so keymapping turned on for a game stays on."""
        settings = self._settings(tmp_path, monkeypatch)
        plist = settings / "com.example.app.plist"
        plist.write_bytes(plistlib.dumps({"playChain": False, "keymapping": True}))

        events = self._hand_over(tmp_path, during=lambda: _install(_playcover_here))

        assert plistlib.loads(plist.read_bytes()) == {"playChain": False, "keymapping": True}
        assert events[-1]["settings_disabled"] == []
        assert events[-1]["settings_restored"] == []

    def test_a_setting_playcover_rewrote_is_put_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _playcover_here: Path
    ) -> None:
        settings = self._settings(tmp_path, monkeypatch)
        plist = settings / "com.example.app.plist"
        plist.write_bytes(plistlib.dumps({"playChain": False, "keymapping": True}))

        def rewrites() -> None:
            plist.write_bytes(plistlib.dumps({"playChain": True, "keymapping": True}))
            _install(_playcover_here)

        events = self._hand_over(tmp_path, during=rewrites)

        assert plistlib.loads(plist.read_bytes()) == {"playChain": False, "keymapping": True}
        assert events[-1]["settings_restored"] == ["playChain"]

    def test_a_settings_file_it_cannot_read_never_fails_the_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings(tmp_path, monkeypatch)
        (settings / "com.example.app.plist").write_bytes(b"this is not a plist")

        events = self._hand_over(tmp_path)

        assert [e["type"] for e in events] == ["handed-to-playcover", "playcover-install-unconfirmed"]
        assert events[-1]["settings_disabled"] == []

    def test_the_console_says_each_step(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _playcover_here: Path
    ) -> None:
        settings = self._settings(tmp_path, monkeypatch)

        def playcover_installs() -> None:
            (settings / "com.example.app.plist").write_bytes(plistlib.dumps(
                {"playChain": True, "keymapping": True}))
            _install(_playcover_here)

        with cli_mod.console.capture() as cap:
            self._hand_over(tmp_path, during=playcover_installs, emitter=False)
        out = " ".join(cap.get().split())

        assert "✓ handed com.example.app_1.0.ipa to PlayCover" in out
        assert "waiting for PlayCover to install Fake…" in out
        assert "✓ PlayCover installed Fake 1.0. Open it from PlayCover." in out
        assert ("Graft turned off PlayCover's keymapping and PlayChain for Fake. For a game, "
                "turn keymapping back on in PlayCover; Graft keeps it from then on.") in out

    def test_the_console_says_when_it_isnt_confirmed(self, tmp_path: Path) -> None:
        with cli_mod.console.capture() as cap:
            self._hand_over(tmp_path, emitter=False)
        out = " ".join(cap.get().split())
        assert ("PlayCover hasn't finished installing Fake. Check PlayCover's window; if the "
                "install failed there, graft --install-to-mac com.example.app_1.0.ipa tries again."
                ) in out

    def test_a_stop_ends_the_wait_never_the_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_safety_net
    ) -> None:
        """The app's Stop is a SIGTERM. The handoff already happened and
        stands; the wait ends, the settings step doesn't run, and the run
        exits as stopped (143)."""
        import os
        import signal
        import time

        from graft_ipa import playcover_handoff
        from graft_ipa.errors import Stopped

        def waiting(*_a, **_k) -> bool:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(2)
            raise AssertionError("the Stop didn't end the wait")

        monkeypatch.setattr(playcover_handoff, "await_install", waiting)
        settled: list[float] = []

        def settle(*_a, wait, **_k):
            settled.append(wait)
            return playcover_handoff.Settled()

        monkeypatch.setattr(playcover_handoff, "settle", settle)

        cache = tmp_path / "cache"
        path = _ipa(cache)
        buf = StringIO()
        with patch.object(cli_mod.subprocess, "run") as run, pytest.raises(Stopped) as e:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            cli_mod._do_install_to_mac(_cfg(cache), filename=path.name, emitter=JsonEmitter(stream=buf))
        assert e.value.code == 143
        assert [ev["type"] for ev in _events(buf.getvalue())] == ["handed-to-playcover"], \
            "the handoff was said before the wait, and no ending after the Stop"
        assert settled == [0], "only what's there already: no waiting after a Stop"

    def _stopped(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, during=None) -> list[dict]:
        import os
        import signal
        import time

        from graft_ipa import playcover_handoff
        from graft_ipa.errors import Stopped

        def waiting(*_a, **_k) -> bool:
            os.kill(os.getpid(), signal.SIGTERM)
            time.sleep(2)
            raise AssertionError("the Stop didn't end the wait")

        monkeypatch.setattr(playcover_handoff, "await_install", waiting)
        cache = tmp_path / "cache"
        path = _ipa(cache)
        buf = StringIO()

        def opened(argv, **_):
            if during is not None:
                during()
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(cli_mod.subprocess, "run", side_effect=opened), \
                pytest.raises(Stopped) as e:
            cli_mod._do_install_to_mac(_cfg(cache), filename=path.name, emitter=JsonEmitter(stream=buf))
        assert e.value.code == 143
        return _events(buf.getvalue())

    def test_a_stop_still_turns_off_what_playcover_has_already_written(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_safety_net
    ) -> None:
        """A first handoff stopped during the wait: PlayCover has written the
        app's settings, both on. Left so, PlayChain crashes the app's first
        login; so the defaults go on now, and the notice with them."""
        settings = self._settings(tmp_path, monkeypatch)
        plist = settings / "com.example.app.plist"

        events = self._stopped(tmp_path, monkeypatch, during=lambda: plist.write_bytes(
            plistlib.dumps({"keymapping": True, "playChain": True})))

        assert plistlib.loads(plist.read_bytes()) == {"keymapping": False, "playChain": False}
        assert events[1:] == [{
            "type": "log", "source": "playcover",
            "line": "Graft turned off PlayCover's keymapping and PlayChain for Fake. For a game, "
                    "turn keymapping back on in PlayCover; Graft keeps it from then on.",
        }]

    def test_a_settle_that_breaks_after_a_stop_never_hides_the_stop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop_safety_net
    ) -> None:
        from graft_ipa import playcover_handoff

        def broken(*_a, **_k):
            raise RuntimeError("anything")

        monkeypatch.setattr(playcover_handoff, "settle", broken)
        events = self._stopped(tmp_path, monkeypatch)
        assert [e["type"] for e in events] == ["handed-to-playcover"]

    def test_a_snapshot_that_breaks_never_stops_the_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _playcover_here: Path
    ) -> None:
        """The settings are kept around the handoff, never a reason not to
        make it (the 2.3.1 review's A3: a RecursionError out of a settings
        file nested too deep). Nothing known is remembered, so the settle
        after it changes nothing."""
        from graft_ipa import playcover_handoff

        settings = self._settings(tmp_path, monkeypatch)
        plist = settings / "com.example.app.plist"

        def broken(*_a, **_k):
            raise RecursionError("maximum recursion depth exceeded")

        def playcover_installs() -> None:
            plist.write_bytes(plistlib.dumps({"playChain": True, "keymapping": True}))
            _install(_playcover_here)

        monkeypatch.setattr(playcover_handoff, "snapshot", broken)
        events = self._hand_over(tmp_path, during=playcover_installs)

        assert [e["type"] for e in events] == ["handed-to-playcover", "playcover-installed"]
        assert (events[-1]["settings_disabled"], events[-1]["settings_restored"]) == ([], [])
        assert plistlib.loads(plist.read_bytes()) == {"playChain": True, "keymapping": True}

    def test_a_settle_that_breaks_never_fails_the_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _playcover_here: Path
    ) -> None:
        from graft_ipa import playcover_handoff

        def broken(*_a, **_k):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(playcover_handoff, "settle", broken)
        events = self._hand_over(tmp_path, during=lambda: _install(_playcover_here))

        assert [e["type"] for e in events] == ["handed-to-playcover", "playcover-installed"]
        assert (events[-1]["settings_disabled"], events[-1]["settings_restored"]) == ([], [])

    def test_a_bundle_id_no_path_may_be_built_from_is_handed_over_and_never_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dropped IPA's CFBundleIdentifier can be anything. The handoff
        still happens (PlayCover decides what to make of it); Graft only
        won't look for it, and says so."""
        settings = self._settings(tmp_path, monkeypatch)
        path = _ipa(tmp_path / "cache", bundle_id="../../escape", name="odd.ipa")

        events = self._hand_over(tmp_path, path=path)

        assert [e["type"] for e in events] == [
            "handed-to-playcover", "log", "playcover-install-unconfirmed",
        ]
        assert events[1]["source"] == "playcover"
        assert "can't follow" in events[1]["line"]
        assert events[-1]["waited_s"] == 0
        assert list(settings.iterdir()) == []
        assert not [p for p in tmp_path.rglob("escape*")], "nothing was made where it pointed"


class TestTheFrameworkCheck:
    """`--install-to-mac` reads the archive's frameworks before the handoff
    (spec 5d items 3 and 7), any archive, including one built before 2.3."""

    SBS = "/System/Library/PrivateFrameworks/SpringBoardServices.framework/SpringBoardServices"

    @pytest.fixture(autouse=True)
    def _roots(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from graft_ipa import playcover

        root = tmp_path / "roots"
        (root / "System/Library/Frameworks/Foundation.framework").mkdir(parents=True)
        monkeypatch.setattr(playcover, "FRAMEWORK_ROOTS", (root,))

    def _needy(self) -> bytes:
        from . import macho_builder as mb

        return mb.thin([mb.dylib_cmd(self.SBS)], filetype=mb.MH_DYLIB)

    def _tweak_deb(self, tweaks: Path) -> None:
        from .test_deb_control import _ar, _control_tar, _data_tar

        tweaks.mkdir(parents=True, exist_ok=True)
        (tweaks / "example.deb").write_bytes(_ar([
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", _control_tar()),
            ("data.tar.gz", _data_tar(["./Library/MobileSubstrate/DynamicLibraries/Example.dylib"])),
        ]))

    def _built(self, tmp_path: Path, *, recorded: bool) -> tuple[SimpleNamespace, Path]:
        from graft_ipa.build_log import BuildLog

        cfg = _cfg(tmp_path / "cache", tmp_path / "built")
        (tmp_path / "cache").mkdir(parents=True, exist_ok=True)
        archive = _ipa(cfg.built_dir, frameworks={"Example.dylib": self._needy()},
                       name="com.example.app_1.0_20261125T000000.ipa")
        self._tweak_deb(cfg.tweaks_dir)
        if recorded:
            BuildLog(cfg.state_dir / "log.sqlite").record(
                app="example", bundle_id="com.example.app", version="1.0", tweaks=["example.deb"],
                ipa_path=archive, signing_team=None, udid=None, success=True, target="mac",
            )
        return cfg, archive

    def test_a_tweak_needing_a_framework_macos_lacks_is_refused_before_the_handoff(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graft_ipa.errors import GraftError

        monkeypatch.setattr(cli_mod, "_handoff_root", lambda: tmp_path)
        cfg, archive = self._built(tmp_path, recorded=True)
        with patch.object(cli_mod.subprocess, "run") as run:
            with pytest.raises(GraftError) as e:
                cli_mod._do_install_to_mac(cfg, filename=archive.name, emitter=None)
        run.assert_not_called()
        # By its control Name, as the Tweaks library shows it (2.3.1 §3.4).
        assert e.value.message.startswith(
            "Example Tweak needs iOS's SpringBoardServices, which macOS doesn't have, so it "
            "can't run under PlayCover."
        )
        assert not list(tmp_path.glob("graft-playcover-*")), "the copy nobody was handed is gone"

    def test_a_check_that_breaks_is_grafts_own_error_and_leaves_no_copy(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from xml.parsers.expat import ExpatError

        from graft_ipa import playcover
        from graft_ipa.errors import GraftError

        monkeypatch.setattr(cli_mod, "_handoff_root", lambda: tmp_path)
        monkeypatch.setattr(playcover, "check_ipa", lambda *_a: (_ for _ in ()).throw(ExpatError("no element found")))
        cfg, archive = self._built(tmp_path, recorded=True)
        with patch.object(cli_mod.subprocess, "run") as run:
            with pytest.raises(GraftError) as e:
                cli_mod._do_install_to_mac(cfg, filename=archive.name, emitter=None)
        run.assert_not_called()
        assert e.value.message.startswith(f"couldn't check {archive.name} for PlayCover")
        assert not list(tmp_path.glob("graft-playcover-*")), "nobody was handed the copy"

    def test_an_archive_whose_tweaks_cant_be_told_only_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No build row names it, so which files a tweak added can't be told."""
        monkeypatch.setattr(cli_mod, "_handoff_root", lambda: tmp_path)
        cfg, archive = self._built(tmp_path, recorded=False)
        buf = StringIO()
        with patch.object(cli_mod.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            cli_mod._do_install_to_mac(cfg, filename=archive.name, emitter=JsonEmitter(stream=buf))
        run.assert_called_once()
        events = _events(buf.getvalue())
        assert events[0] == {
            "type": "log", "source": "warning",
            "line": "Example.dylib in the app needs iOS's SpringBoardServices, which macOS "
                    "doesn't have, so the app may not run under PlayCover.",
        }
        assert events[1]["type"] == "handed-to-playcover"

    def test_a_staged_dump_only_warns(self, tmp_path: Path) -> None:
        cache = tmp_path / "cache"
        staged = _ipa(cache, frameworks={"libAppOwn.dylib": self._needy()})
        buf = StringIO()
        with patch.object(cli_mod.subprocess, "run") as run:
            run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
            cli_mod._do_install_to_mac(_cfg(cache), filename=staged.name, emitter=JsonEmitter(stream=buf))
        assert [e["type"] for e in _events(buf.getvalue())][:2] == ["log", "handed-to-playcover"]


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

        with patch.object(cli_mod.subprocess, "run", side_effect=opened) as run:
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

    # A failed handoff leaves no copy (the 2.3.1 review's T2-B4): the private
    # copy is a whole IPA in the temp folder, and nobody was handed it.

    def _fails(self, tmp_path: Path, *, opened=None, expect=Exception):
        path = _ipa(tmp_path / "built")
        cache = tmp_path / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        answer = opened or (lambda argv, **_: SimpleNamespace(returncode=0, stdout="", stderr=""))
        with patch.object(cli_mod.subprocess, "run", side_effect=answer) as run, \
                pytest.raises(expect) as e:
            cli_mod._do_install_to_mac(_cfg(cache, path.parent), filename=path.name, emitter=None)
        return run, e.value

    def test_a_failed_open_leaves_no_copy(self, tmp_path: Path, _handoffs: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
        from graft_ipa import host

        monkeypatch.setattr(host, "apple_silicon_blocker", lambda: None)
        _, error = self._fails(
            tmp_path, expect=ConfigError,
            opened=lambda argv, **_: SimpleNamespace(returncode=1, stdout="", stderr=OPEN_MISSING_BUNDLE_STDERR),
        )
        assert error.message == "PlayCover is not installed, or could not launch"
        assert list(_handoffs.iterdir()) == [], "nobody was handed the copy"

    def test_an_archive_it_cannot_read_leaves_no_copy(
        self, tmp_path: Path, _handoffs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graft_ipa.errors import IPAInspectionError

        def unreadable(_ipa):
            raise IPAInspectionError("no Payload/*.app/Info.plist in the archive")

        monkeypatch.setattr(cli_mod, "read_info_plist", unreadable)
        run, _ = self._fails(tmp_path, expect=IPAInspectionError)
        run.assert_not_called()
        assert list(_handoffs.iterdir()) == []

    def test_a_scrub_that_breaks_leaves_no_copy(
        self, tmp_path: Path, _handoffs: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken(*_a, **_k):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(cli_mod, "_without_account_data", broken)
        run, _ = self._fails(tmp_path, expect=OSError)
        run.assert_not_called()
        assert list(_handoffs.iterdir()) == []

    def test_a_stop_during_the_copy_is_a_stop_and_leaves_no_copy(
        self, tmp_path: Path, _handoffs: Path, monkeypatch: pytest.MonkeyPatch, stop_safety_net
    ) -> None:
        """The GUI's Stop is a SIGTERM. Outside `exit_on_termination` it
        killed graft mid-copy (exit -15), and the half-written copy stayed."""
        import os
        import signal

        from graft_ipa.errors import Stopped

        real_copy = cli_mod.shutil.copyfile

        def copying(src, dst):
            real_copy(src, dst)
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("the Stop didn't end the copy")

        monkeypatch.setattr(cli_mod.shutil, "copyfile", copying)
        run, _ = self._fails(tmp_path, expect=Stopped)
        run.assert_not_called()
        assert list(_handoffs.iterdir()) == []

    def test_a_stop_during_the_scrub_is_a_stop_and_leaves_no_copy(
        self, tmp_path: Path, _handoffs: Path, monkeypatch: pytest.MonkeyPatch, stop_safety_net
    ) -> None:
        import os
        import signal

        from graft_ipa.errors import Stopped

        def scrubbing(*_a, **_k):
            os.kill(os.getpid(), signal.SIGTERM)
            raise AssertionError("the Stop didn't end the scrub")

        monkeypatch.setattr(cli_mod, "_without_account_data", scrubbing)
        run, _ = self._fails(tmp_path, expect=Stopped)
        run.assert_not_called()
        assert list(_handoffs.iterdir()) == []

    def test_the_copy_handed_over_stays_for_playcover(self, tmp_path: Path, _handoffs: Path) -> None:
        """`open` returns before PlayCover reads it: only a later handoff's
        sweep removes it."""
        handed = self._hand_over(tmp_path, _ipa(tmp_path / "built"))
        assert handed.is_file()
        assert [p.name for p in _handoffs.iterdir()] == [handed.parent.name]

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
