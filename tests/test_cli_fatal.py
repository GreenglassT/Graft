"""_fatal must print to stderr so subprocess consumers (the GUI) can surface
the error via result.stderr without parsing the JSON-events stdout stream."""

from __future__ import annotations

import pytest

from graft_ipa.cli import _fatal
from graft_ipa.errors import RepoError


def test_fatal_writes_message_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    exit_ = _fatal(RepoError("no Release file at https://x", hint="add http://"))
    captured = capsys.readouterr()
    assert "no Release file" in captured.err
    assert "add http://" in captured.err
    assert "no Release file" not in captured.out
    assert exit_.exit_code == 12  # RepoError exit code


def test_fatal_omits_hint_block_when_no_hint(capsys: pytest.CaptureFixture[str]) -> None:
    _fatal(RepoError("naked error"))
    captured = capsys.readouterr()
    assert "naked error" in captured.err
    assert "hint" not in captured.err.lower()
