"""No verb may fail as a traceback, and no app may be skipped because another failed.

Two halves of one pattern. Every verb in `main` used to carry its own
`except GraftError: raise _fatal(e)` — and the two that didn't, `--devices`
and `--builds`, were the two that died with a multi-line Python traceback on
stderr. That is not cosmetic: `CLIError.exited(stderr:)` uses stderr as its
`errorDescription`, so the GUI rendered the traceback verbatim as the
user-facing device error. The multi-app build loop had the mirror-image hole:
it caught only `GraftError` while `_build_one` deliberately re-raises
`Exception`, so one app's `OSError` took every app after it down with it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from graft_ipa.errors import RepoError


def _invoke(args: list[str]):
    from graft_ipa.cli import app as cli_app

    return CliRunner().invoke(cli_app, args)


def test_an_unreachable_usbmuxd_reports_an_error_not_a_traceback(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--devices` is what the GUI runs to populate the device picker.

    pymobiledevice3 missing from the pipx venv, or usbmuxd not running, raises
    something that is not a GraftError — and this verb had no handler at
    all, so the raw traceback became the error the user was shown.
    """
    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(
        cli_mod, "list_devices", side_effect=OSError("usbmuxd is not running")
    ):
        result = _invoke(["--json", "--devices"])

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "usbmuxd is not running" in result.stderr
    # The class name survives: for an untranslated failure it is usually the
    # only diagnosis available.
    assert "OSError" in result.stderr


def test_a_broken_build_log_reports_an_error_not_a_traceback(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--builds` was the other verb dispatched with no guard at all."""
    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(
        cli_mod, "_do_builds", side_effect=RuntimeError("log.sqlite is corrupt")
    ):
        result = _invoke(["--json", "--builds"])

    assert result.exit_code == 1
    assert "Traceback" not in result.stderr
    assert "log.sqlite is corrupt" in result.stderr


def test_a_known_error_keeps_its_exit_code_through_the_boundary(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The per-verb handlers are gone; their exit codes must not go with them.

    The GUI keys on specific statuses (16 means "tweak still attached"), so the
    single boundary has to preserve `GraftError.exit_code` rather than
    flattening everything to 1.
    """
    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(
        cli_mod, "_do_repos", side_effect=RepoError("no Release file", hint="add http://")
    ):
        result = _invoke(["--repos"])

    assert result.exit_code == 12  # RepoError
    assert "no Release file" in result.stderr
    assert "add http://" in result.stderr


def test_one_apps_unexpected_failure_does_not_strand_the_rest(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`graft example bravo delta` with an ENOSPC during example.

    The loop caught only GraftError, so bravo and delta were never
    attempted, the exit-code summary never ran, and a --json consumer that
    asked for three apps saw one terminal event and then nothing at all.
    """
    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    attempted: list[str] = []

    def _build(*, app_name: str, **kwargs):
        attempted.append(app_name)
        if app_name == "example":
            raise OSError(28, "No space left on device")

    with patch.object(cli_mod, "_build_one", side_effect=_build):
        result = _invoke(["--json", "example", "bravo", "delta"])

    assert attempted == ["example", "bravo", "delta"], (
        "every app the user named must be attempted"
    )
    assert result.exit_code == 1
    # The summary line for the app that failed, on stderr rather than in the
    # middle of the JSON event stream.
    assert "example failed" in result.stderr


def test_the_multi_app_summary_stays_off_the_json_stream(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """stdout is the event stream; a failure summary printed there is a line the
    GUI's decoder throws away, and it can land mid-stream."""
    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    with patch.object(cli_mod, "_build_one", side_effect=OSError("disk on fire")):
        result = _invoke(["--json", "example"])

    assert "example failed" not in result.stdout
    assert "example failed" in result.stderr
