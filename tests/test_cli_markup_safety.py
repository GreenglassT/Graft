"""F6 — exception text must not be parsed as Rich markup.

`console.print` interprets `[...]` in the string it is given. On rich 15.0.0 a
closing-tag-shaped token raises `MarkupError`, and an unrecognised opening tag
like `[foo]` is deleted from the output without a word. Every one of this
module's error and warning renderings interpolated an exception, a path or a
subprocess's stderr into exactly such a string.

Where that lands decides how bad it is:

  * from `_record_history`, the warning aborts `__exit__` before `build-end` —
    the one invariant `_BuildReport` exists to guarantee, lost while reporting
    that the history database was locked;
  * from `_reported`'s handler, `_fatal` raising turns the CLI's single error
    boundary back into the traceback it was written to replace;
  * from `_report_app_failure`, it escapes the `except` that keeps one app's
    failure from taking the rest of a multi-app build down.

Escaped rather than `markup=False` at all three: the prefixes (`[red]error:`,
`[yellow]warning:`) are markup this module means, and turning markup off would
print the tags literally. Escaping the interpolated span keeps the colour and
makes only the untrusted half inert. The structured half — the `log` event, the
`build-end` error field — carries the original text, because JSON has no markup
and a consumer diffing the two should see the same string.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from graft_ipa.errors import GraftError, RepoError
from graft_ipa.json_output import JsonEmitter

# A closing tag with no opener: `MarkupError` on rich 15. Reachable from a real
# subprocess stderr, a filename, or a hint quoting either.
CLOSING_TAG = "unterminated [/section] in the manifest"
# An opening tag rich does not recognise: silently deleted, so the message the
# user reads is missing a word and nothing says so.
SWALLOWED_TAG = "could not read [config] block"


def test_warn_survives_markup_shaped_text(capsys: pytest.CaptureFixture[str]) -> None:
    from graft_ipa.cli import _warn

    _warn(JsonEmitter(), CLOSING_TAG)

    captured = capsys.readouterr()
    assert "[/section]" in captured.err
    # The event keeps the original, unescaped, so a machine consumer and a
    # human are reading the same sentence.
    event = json.loads(captured.out.splitlines()[0])
    assert event["line"] == CLOSING_TAG


def test_warn_does_not_swallow_bracketed_words(capsys: pytest.CaptureFixture[str]) -> None:
    from graft_ipa.cli import _warn

    _warn(None, SWALLOWED_TAG)

    assert "[config]" in capsys.readouterr().err


def test_fatal_survives_markup_shaped_text(capsys: pytest.CaptureFixture[str]) -> None:
    from graft_ipa.cli import _fatal

    exit_ = _fatal(RepoError(CLOSING_TAG, hint=f"try {SWALLOWED_TAG}"))

    captured = capsys.readouterr()
    assert "[/section]" in captured.err
    assert "[config]" in captured.err
    assert exit_.exit_code == 12


def test_report_app_failure_survives_markup_shaped_text(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from graft_ipa.cli import _report_app_failure

    _report_app_failure(
        "example", GraftError(CLOSING_TAG, hint=SWALLOWED_TAG), emitter=JsonEmitter()
    )

    captured = capsys.readouterr()
    assert "[/section]" in captured.err
    assert "[config]" in captured.err
    # Under --json this is duplicate detail; stdout is the event stream.
    assert captured.out == ""


def test_an_app_named_like_a_tag_does_not_break_the_failure_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The app name comes from argv, so it is untrusted too."""
    from graft_ipa.cli import _report_app_failure

    _report_app_failure("[/red]", GraftError("nope"), emitter=None)

    assert "[/red]" in capsys.readouterr().out


def test_a_build_end_still_follows_a_markup_shaped_history_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole reason this matters, end to end.

    A locked log.sqlite whose driver puts a bracket in its message used to
    raise MarkupError out of `_record_history`'s warning, which aborted
    `__exit__` before the terminal event. The build had shipped; the GUI was
    left with a `build-start` and silence.
    """
    from unittest.mock import MagicMock

    from graft_ipa import cli as cli_mod

    log = MagicMock()
    log.record.side_effect = RuntimeError(CLOSING_TAG)

    emitter = JsonEmitter()
    with cli_mod._BuildReport(
        emitter, app_name="example", bundle_id="com.example.app", delivery="sideload"
    ) as report:
        report.log = log

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["build-start", "log", "build-end"]
    assert events[-1]["ok"] is True


# ---------------------------------------------------------------------------
# …and the success line that runs after the write it announces
# ---------------------------------------------------------------------------
#
# The three sites above are error boundaries. These are the opposite: the happy
# path, printed once config.yaml or tweaks_dir has already been changed. What
# makes them the same defect is `_reported` — it catches the MarkupError and
# renders `_fatal`, so the process exits non-zero on a mutation that succeeded.
# The GUI reads that as "adding the repo failed" about a repo it now has.
#
# The text is third-party in every case: a repo's `Origin:` field, an App Store
# listing's name, a .deb filename chosen by whoever built it, a URL from argv.


def test_a_repo_whose_name_looks_like_a_tag_is_still_reported_as_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`Origin:` is whatever the repo's Release file says."""
    from unittest.mock import MagicMock, patch

    from graft_ipa import cli as cli_mod

    cfg = MagicMock()
    cfg.state_dir = tmp_path / "state"
    cfg.path = tmp_path / "config.yaml"
    release = MagicMock()
    release.name, release.origin = CLOSING_TAG, "x"

    with patch.object(cli_mod.repo, "normalize_url", return_value="https://repo.example/"), \
         patch.object(cli_mod.repo, "fetch_release", return_value=release), \
         patch.object(cli_mod.repo, "fetch_packages", return_value="Package: a\n"), \
         patch.object(cli_mod.repo, "parse_packages", return_value=[]), \
         patch.object(cli_mod, "add_repo") as add:
        cli_mod._do_add_repo(cfg, "https://repo.example/")

    assert add.called, "the repo was never written — this test would prove nothing"
    assert "[/section]" in capsys.readouterr().out


def test_an_app_whose_itunes_name_looks_like_a_tag_is_still_reported_as_added(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`info.name` comes from the iTunes lookup API."""
    from unittest.mock import MagicMock, patch

    from graft_ipa import cli as cli_mod

    cfg = MagicMock()
    cfg.path = tmp_path / "config.yaml"
    info = MagicMock()
    info.name, info.bundle_id, info.track_id = SWALLOWED_TAG, "com.x.y", 1
    info.version = "1.0"

    with patch.object(cli_mod.itunes, "parse_id_from_url", return_value=1), \
         patch.object(cli_mod.itunes, "lookup", return_value=info), \
         patch.object(cli_mod, "add_app", return_value="app") as add:
        cli_mod._do_add_app_url(cfg, "1")

    assert add.called
    assert "[config]" in capsys.readouterr().out


def test_a_deb_filename_that_looks_like_a_tag_still_reports_the_download(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The filename is chosen by whoever built the .deb, not by this process.

    An opening tag, not a closing one: a filename cannot contain `/`, so this
    site's hazard is the quiet half — rich deletes `[bold]` and the line names a
    file the user will not find in `tweaks_dir`, with nothing saying a word.
    """
    from unittest.mock import MagicMock, patch

    from graft_ipa import cli as cli_mod

    cfg = MagicMock()
    cfg.tweaks_dir = tmp_path / "tweaks"
    cfg.path = tmp_path / "config.yaml"
    dest = tmp_path / "tweaks" / "tweak[bold].deb"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"x" * 10)

    with patch.object(cli_mod.canister, "fetch_deb", return_value=dest):
        cli_mod._do_fetch(cfg, "com.example.tweak")

    # Whitespace-normalised: rich wraps at the console width, and a tmp_path is
    # long enough to break the filename across lines. Both prints, because both
    # name the file and either one alone would leave the other unpinned.
    assert "".join(capsys.readouterr().out.split()).count("tweak[bold].deb") == 2
