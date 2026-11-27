"""A full disk, said as one, where errors are shown (the 2.3.1 review's T2-B6).

Staging, a tweak update, ipatool's download, the hold's config write and the
LiveContainer certificate's temporary copy each met a full disk with their
own raw words: "[Errno 28] No space left on device", sometimes under
"that isn't a failure graft knows how to explain", or ipatool's "no space
left on device" under "that is ipatool's own error". Not every site is
taught it: `errors.presented` maps one, once, where errors are shown, the
CLI's error boundary, a build's `build-end` and an update's results, and
ipatool's own words are mapped where ipatool's errors are explained.

An error that already says what to do about the space (an export's "free
some space on the disk …") keeps its words, and the device's own words
("No space left on device" from an install or a dump) are the device's,
not this Mac's.
"""

from __future__ import annotations

import errno
import json
import shutil
import sqlite3
from io import StringIO
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import errors, ipatool, update_run
from graft_ipa.errors import (
    OUT_OF_SPACE,
    AppStoreError,
    GraftError,
    InstallError,
    IPAValidationError,
    presented,
)
from graft_ipa.json_output import JsonEmitter
from graft_ipa.lc_runner import LiveContainerError


def _enospc(path: str = "/private/var/empty/Example/cache/x.ipa") -> OSError:
    return OSError(errno.ENOSPC, "No space left on device", path)


def _raised_from(error: GraftError, cause: BaseException) -> GraftError:
    try:
        raise error from cause
    except GraftError as e:
        return e


# --- What counts ---------------------------------------------------------------


@pytest.mark.parametrize("exc", [
    _enospc(),
    OSError(errno.EDQUOT, "Disc quota exceeded"),
    # Staging, as provide_ipa says it, its OSError's words in the message.
    IPAValidationError("failed to copy IPA into /private/var/empty/Example/cache: "
                       "[Errno 28] No space left on device"),
    # A tweak update, as update_run says it.
    GraftError("couldn't update example.deb: [Errno 28] No space left on device"),
    # Raised from the OSError, with words of its own that say nothing of it.
    _raised_from(GraftError("couldn't stage the IPA", hint="try again"), _enospc()),
    # The delivery log, as SQLite says it.
    sqlite3.OperationalError("database or disk is full"),
    # A device error raised from this Mac's own OSError: the Mac's.
    _raised_from(LiveContainerError("couldn't push the certificate"), _enospc()),
    # copytree's, which gathers each file's error as text, with no errno.
    shutil.Error([("a", "b", "[Errno 28] No space left on device: 'b'")]),
], ids=["enospc", "edquot", "staging", "tweak-update", "from-enospc", "sqlite", "lc-from-enospc",
        "copytree"])
def test_a_full_disk_is_said_as_one(exc: BaseException) -> None:
    shown = presented(exc)

    assert isinstance(shown, GraftError)
    assert (shown.message, shown.hint) == (OUT_OF_SPACE, None)
    assert str(shown) == "Your Mac is out of disk space: free some and press Try Again"


def test_it_keeps_the_error_s_exit_status() -> None:
    staging = IPAValidationError("failed to copy IPA: [Errno 28] No space left on device")
    assert presented(staging).exit_code == IPAValidationError.exit_code == 14
    assert presented(_enospc()).exit_code == 1


@pytest.mark.parametrize("exc", [
    # Says what to do about the space already: an export's own (EX2).
    _raised_from(GraftError("couldn't write /Volumes/Stick/x.ipa: No space left on device",
                            hint="free some space on the disk /Volumes/Stick is on, or choose "
                                 "another folder"), _enospc()),
    # The device's own words: its disk, not this Mac's.
    InstallError("installing failed: No space left on device"),
    LiveContainerError("couldn't push x.ipa: No space left on device"),
    # Not a full disk.
    OSError(errno.EACCES, "Permission denied"),
    GraftError("something else", hint="do this"),
    ValueError("No space"),
], ids=["explained", "install", "lc-push", "eacces", "other", "value-error"])
def test_anything_else_is_left_as_it_is(exc: BaseException) -> None:
    assert presented(exc) is exc


def test_once_said_it_stays_said() -> None:
    once = presented(_enospc())
    assert presented(once) is once


# --- Where it's shown -----------------------------------------------------------


def test_the_cli_s_error_boundary_says_it(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare OSError at the boundary: the hold's config write, the
    LiveContainer certificate's temporary copy."""
    def full(*_a, **_k):
        raise _enospc()

    monkeypatch.setattr(cli_mod, "_do_staged_ipas", full)

    result = CliRunner().invoke(cli_mod.app, ["--json", "--staged-ipas"])

    assert result.exit_code == 1
    said = " ".join(result.stderr.split())
    assert f"error: {OUT_OF_SPACE}" in said
    assert "Errno" not in said and "explain" not in said


def test_the_cli_s_error_boundary_keeps_a_graft_error_s_exit_status(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def full(*_a, **_k):
        raise IPAValidationError("failed to copy IPA: [Errno 28] No space left on device")

    monkeypatch.setattr(cli_mod, "_do_staged_ipas", full)

    result = CliRunner().invoke(cli_mod.app, ["--json", "--staged-ipas"])

    assert result.exit_code == 14
    assert f"error: {OUT_OF_SPACE}" in " ".join(result.stderr.split())


def test_a_build_s_end_says_it() -> None:
    buf = StringIO()
    with pytest.raises(OSError):
        with cli_mod._BuildReport(JsonEmitter(stream=buf), app_name="example",
                                  bundle_id="com.example.app", delivery="livecontainer"):
            raise _enospc()

    (end,) = [e for e in map(json.loads, buf.getvalue().splitlines()) if e["type"] == "build-end"]
    assert (end["ok"], end["error"]) == (False, OUT_OF_SPACE)


def test_a_multi_app_build_s_summary_line_says_it() -> None:
    with cli_mod.console.capture() as cap:
        cli_mod._report_app_failure("example", _enospc(), emitter=None)
    assert f"example failed: {OUT_OF_SPACE}" in " ".join(cap.get().split())


def test_an_update_s_results_say_it() -> None:
    """Every update step's error goes through `_message`: a tweak's, the
    get's, the build's, an install's."""
    assert update_run._message(_enospc()) == OUT_OF_SPACE
    assert update_run._message(
        GraftError("couldn't update example.deb: [Errno 28] No space left on device")
    ) == OUT_OF_SPACE


# --- ipatool's own words --------------------------------------------------------


def test_ipatool_s_full_disk_is_said_as_one_not_as_ipatool_s_own_error() -> None:
    """Go's error for ENOSPC, lowercase, as ipatool's download writes it."""
    err = ipatool.explain(
        "write /private/var/empty/Example/T/x.ipa: no space left on device",
        bundle_id="com.example.app", path=Path("/nonexistent/ipatool"), reported_version="2.8.0",
    )

    assert isinstance(err, AppStoreError)
    assert (err.message, err.hint) == (OUT_OF_SPACE, None)
    assert err.exit_code == 20


def test_the_words_are_matched_without_regard_to_case() -> None:
    assert errors.out_of_space(GraftError("tool said: NO SPACE LEFT ON DEVICE"))
