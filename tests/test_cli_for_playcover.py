"""`--for-playcover`: the flag, its refusal, and the branch it drives.

All of this was unreachable by the suite when it was written — `grep -rn
for_playcover tests/` found it only in a dispatch-registry docstring, so
deleting the whole `if for_playcover and not dry_run:` block passed
everything, including the `raise GraftError` that is the only thing
between a stub-build failure and an unreadable traceback.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import playcover
from graft_ipa.build_log import BuildLog
from graft_ipa.config import (
    AppConfig,
    Config,
    CyanOptions,
    SigningConfig,
)
from graft_ipa.errors import ConfigError, GraftError, PreflightError
from graft_ipa.json_output import JsonEmitter
from graft_ipa.preflight import CheckResult


class TestTheRefusal:
    """A --for-playcover artifact is Mac-only, so it must not be installable
    on a phone by the same command that built it."""

    def test_it_refuses_without_skip_install(self) -> None:
        with pytest.raises(ConfigError) as e:
            cli_mod.main.__wrapped__(  # type: ignore[attr-defined]
                ctx=None, apps=[], for_playcover=True, skip_install=False,
            )
        assert "only this Mac can run" in e.value.message
        assert "--skip-install" in (e.value.hint or "")

    def test_it_allows_the_pair(self) -> None:
        """The refusal must be about the *combination*, not the flag: pairing
        it with --skip-install has to get past this check, or the feature is
        unreachable rather than guarded."""
        try:
            cli_mod.main.__wrapped__(  # type: ignore[attr-defined]
                ctx=None, apps=[], for_playcover=True, skip_install=True,
            )
        except ConfigError as e:  # pragma: no cover - only on regression
            assert "only this Mac can run" not in e.message, e.message
        except Exception:
            pass  # anything later is not this check's business


class TestThePreparationBranch:
    """`_build_one`'s use of `playcover.prepare_ipa`."""

    def test_a_prep_failure_becomes_a_readable_error(self, tmp_path: Path) -> None:
        """`PlayCoverPrepError` carries its own hint, and dropping it loses the
        one sentence that says what to install. Anything not translated reaches
        the user as a traceback after minutes of downloading and injecting."""
        boom = playcover.PlayCoverPrepError(
            "couldn't build the Preferences stub", hint="is Xcode installed?"
        )
        with patch.object(playcover, "prepare_ipa", side_effect=boom), \
                patch.object(playcover, "check_ipa", return_value=[]):
            with pytest.raises(GraftError) as e:
                cli_mod._prepare_for_playcover(tmp_path / "example-tweaked.ipa", None, tweaks=[])
        assert "example-tweaked.ipa" in e.value.message
        assert "couldn't build the Preferences stub" in e.value.message
        assert e.value.hint == "is Xcode installed?", "the hint survives"

    def test_what_it_fixed_is_reported(self, tmp_path: Path) -> None:
        with patch.object(playcover, "prepare_ipa",
                          return_value=["libexample.dylib → bundled X"]), \
                patch.object(playcover, "check_ipa", return_value=[]):
            fixed = cli_mod._prepare_for_playcover(tmp_path / "a.ipa", None, tweaks=[])
        assert fixed == ["libexample.dylib → bundled X"]

    def test_nothing_to_fix_is_not_a_failure(self, tmp_path: Path) -> None:
        with patch.object(playcover, "prepare_ipa", return_value=[]), \
                patch.object(playcover, "check_ipa", return_value=[]):
            assert cli_mod._prepare_for_playcover(tmp_path / "a.ipa", None, tweaks=[]) == []

    def test_a_tweak_needing_a_framework_macos_lacks_refuses_the_build(self, tmp_path: Path) -> None:
        """Spec 5d item 3: refused before the handoff, naming the tweak and
        the framework, and before anything is retargeted."""
        link = playcover.UnsupportedLink("Example.dylib", "SpringBoardServices", "example.deb")
        with patch.object(playcover, "check_ipa", return_value=[link]), \
                patch.object(playcover, "prepare_ipa") as prepare:
            with pytest.raises(GraftError) as e:
                cli_mod._prepare_for_playcover(tmp_path / "a.ipa", None, tweaks=[])
        prepare.assert_not_called()
        assert e.value.message == playcover.refusal_message([link])

    def test_the_apps_own_binary_only_warns_and_the_build_goes_on(self, tmp_path: Path) -> None:
        link = playcover.UnsupportedLink("libAppOwn.dylib", "SpringBoardServices", None)
        buf = StringIO()
        with patch.object(playcover, "check_ipa", return_value=[link]), \
                patch.object(playcover, "prepare_ipa", return_value=[]) as prepare:
            cli_mod._prepare_for_playcover(tmp_path / "a.ipa", JsonEmitter(stream=buf), tweaks=[])
        prepare.assert_called_once()
        events = [json.loads(x) for x in buf.getvalue().splitlines() if x.strip()]
        assert {"type": "log", "source": "warning", "line": playcover.warning_message(link)} in events

    def test_the_check_is_told_which_files_the_apps_tweaks_brought(self, tmp_path: Path) -> None:
        bare = tmp_path / "libexample.dylib"
        bare.write_bytes(b"")
        with patch.object(playcover, "check_ipa", return_value=[]) as check, \
                patch.object(playcover, "prepare_ipa", return_value=[]):
            cli_mod._prepare_for_playcover(tmp_path / "a.ipa", None, tweaks=[bare])
        assert check.call_args[0][1] == {"libexample.dylib": "libexample.dylib"}

    def test_an_unreadable_tweak_binary_is_a_readable_error(self, tmp_path: Path) -> None:
        boom = playcover.PlayCoverPrepError("couldn't read Example.dylib's linked libraries", hint="h")
        with patch.object(playcover, "check_ipa", side_effect=boom):
            with pytest.raises(GraftError) as e:
                cli_mod._prepare_for_playcover(tmp_path / "a.ipa", None, tweaks=[])
        assert "couldn't read Example.dylib's linked libraries" in e.value.message

    def test_a_check_that_breaks_is_a_readable_error(self, tmp_path: Path) -> None:
        """Any failure of the check, a half-written plist's ExpatError
        included, ends the Mac build with Graft's own error, not a traceback."""
        from xml.parsers.expat import ExpatError

        with patch.object(playcover, "check_ipa", side_effect=ExpatError("no element found")):
            with pytest.raises(GraftError) as e:
                cli_mod._prepare_for_playcover(tmp_path / "a.ipa", None, tweaks=[])
        assert e.value.message.startswith("couldn't check a.ipa for PlayCover")

    def test_the_build_passes_the_apps_tweaks(self) -> None:
        """`_build_one` is too long a road to run to this step here; this pins
        the call site's argument instead."""
        import inspect

        src = inspect.getsource(cli_mod._build_one)
        assert ("_prepare_for_playcover(\n                    tweaked, emitter, "
                "tweaks=[cfg.tweak_path(t) for t in app_cfg.tweaks],") in src


class TestTheRecordedTarget:
    """What the build row says, which is what the GUI branches on."""

    def test_a_playcover_build_is_recorded_as_mac(self, tmp_path: Path) -> None:
        log = BuildLog(tmp_path / "log.sqlite")
        cli_mod._record_build_row(
            log, app="example", bundle_id="com.example.app", version="1.0",
            tweaks=[], ipa_path=None, log_path=None, signing_team=None,
            udid=None, success=True, error=None, for_playcover=True,
        )
        assert log.recent(app=None, limit=1)[0]["target"] == "mac"

    def test_an_ordinary_build_is_recorded_as_device(self, tmp_path: Path) -> None:
        log = BuildLog(tmp_path / "log.sqlite")
        cli_mod._record_build_row(
            log, app="example", bundle_id="com.example.app", version="1.0",
            tweaks=[], ipa_path=None, log_path=None, signing_team=None,
            udid=None, success=True, error=None, for_playcover=False,
        )
        assert log.recent(app=None, limit=1)[0]["target"] == "device"


def test_the_builds_listing_carries_the_target(tmp_path: Path) -> None:
    """Pins that `--builds --json` emits the field at all.

    The GUI reads it to decide which action a row gets, and `_do_builds` builds
    its own dict per row rather than passing `recent()`'s through — so the field
    has to be added there by hand, and dropping that line is invisible to every
    other test. Normalization of old rows is `_target_of`'s job, tested against
    it directly."""
    state = tmp_path / "state"
    state.mkdir()
    log = BuildLog(state / "log.sqlite")
    log.record(
        app="example", bundle_id="com.example.app", version="1.0",
        tweaks=[], ipa_path=None, log_path=None, signing_team=None,
        udid=None, success=True, error=None, target="mac",
    )
    buf = StringIO()
    cli_mod._do_builds(
        SimpleNamespace(state_dir=state), app=None, limit=10,
        emitter=JsonEmitter(stream=buf),
    )
    row = next(json.loads(x) for x in buf.getvalue().splitlines() if x.strip())
    assert row["target"] == "mac"



def _cfg(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12", cert_password_env="X",
            profile=tmp_path / "profile", team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={
            "bravo": AppConfig(
                name="bravo", bundle_id="com.bravo.app",
                preferred_version="latest", tweaks=[], cyan=CyanOptions(),
                delivery="livecontainer",
            )
        },
    )


def _target_of_a_build(tmp_path: Path, *, for_playcover: bool) -> str:
    """Run `_build_one` far enough to write its history row, and read the target.

    Stopped at pre-flight, which is the earliest failure that still records:
    `report.log` is assigned before the checks run, so the envelope writes a
    row on the way out. That is the whole chain — argv flag to `_build_one` to
    `_BuildReport` to `_record_build_row` — which the unit tests above do not
    touch, because they call the last link directly.
    """
    log = BuildLog(tmp_path / "log.sqlite")
    fail = [CheckResult(name="cyan", ok=False, hint="stop here")]
    with patch.object(cli_mod, "run_all_checks", return_value=fail):
        with pytest.raises(PreflightError):
            cli_mod._build_one(
                cfg=_cfg(tmp_path), log=log, app_name="bravo", dry_run=False,
                skip_install=True, skip_decrypt_check=True,
                verbose=False, device=None, for_playcover=for_playcover,
            )
    return log.recent(app=None, limit=1)[0]["target"]


class TestTheFlagReachesTheRow:
    """`--for-playcover` arrives at `_build_one`, but the write is three layers
    down in `_BuildReport.__exit__`. Passing `False` at any of those layers
    would leave every test above green and every Mac build offered to a phone."""

    def test_a_playcover_build_records_mac(self, tmp_path: Path) -> None:
        assert _target_of_a_build(tmp_path, for_playcover=True) == "mac"

    def test_an_ordinary_build_records_device(self, tmp_path: Path) -> None:
        assert _target_of_a_build(tmp_path, for_playcover=False) == "device"


class TestTheTerminalTable:
    """`--builds` without `--json`. A person picks a row by eye and feeds its id
    to `--reinstall`, which pushes to a phone, so the table has to say which
    rows are Mac-only."""

    def _table(self, target: str) -> str:
        record = {
            "timestamp": "2026-08-18T00:00:00", "app": "example", "version": "1.0",
            "tweaks": [], "success": True, "error": None, "target": target,
        }
        with cli_mod.console.capture() as cap:
            cli_mod._print_builds([record])
        return cap.get()

    def test_a_mac_row_says_mac(self) -> None:
        out = self._table("mac")
        assert "mac" in out
        assert "device" not in out, "one row, one target"

    def test_a_device_row_says_device(self) -> None:
        out = self._table("device")
        assert "device" in out
        # Guards the reverse mistake: a cell hard-coded to `mac`, or a header
        # that leaks the word into every row.
        assert "mac" not in out

