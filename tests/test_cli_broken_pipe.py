"""`graft ... | head` is not a bug in graft.

Piping the CLI into a consumer that stops reading used to produce two
complaints and exit 120:

    error: BrokenPipeError: [Errno 32] Broken pipe
      hint: that isn't a failure graft knows how to explain — ...
    Exception ignored on flushing sys.stdout:
    BrokenPipeError: [Errno 32] Broken pipe

Two separate defects. The first line came from `_unexpected`, the CLI's "no
translation for this" path, which is the wrong thing to say about a reader
hanging up. The second is CPython's: the interpreter flushes `sys.stdout`
during shutdown, *after* every `except` clause has run, and exits 120 when that
flush fails — so no handler in the CLI could catch it, and the status the
handler chose was overwritten by 120 anyway.

Both halves have to be forced, not hoped for. At ordinary volumes a reader that
hangs up never produces EPIPE at all: `--device-apps --json | head -3` emits
~13KB, which fits inside the 64KB pipe buffer, so the writer finishes and exits
before `head` closes anything. The tests below close the read end *before* the
child writes, which makes the first flush fail whatever the volume — EPIPE is
"no reader exists", not "the buffer is full".

The unit tests pin the branch and the narrowing. Only a real subprocess can see
the 120, because only a real subprocess has an interpreter shutdown.
"""

from __future__ import annotations

import errno
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

REPO_ROOT = Path(__file__).parent.parent

# Run the source in *this* checkout, and prove it.
#
# `.venv` here is a symlink to the parent checkout's, and `graft` on PATH
# is a pipx editable install pointing at the parent as well — so a subprocess
# that spawned either would test somebody else's copy of cli.py and pass for
# the wrong reason. `python -c` puts the working directory on `sys.path[0]`
# ahead of any site-packages `.pth`, which is the same mechanism `describe_proc`
# in conftest relies on; the guard below turns that from an assumption into a
# measurement the assertions depend on. Exit 97 rather than a message, because
# stderr is what these tests read, and because a wrong-tree run must not be able
# to masquerade as the exit code they are looking for.
_PROVENANCE = """
import pathlib, sys

import graft_ipa

if pathlib.Path(graft_ipa.__file__).resolve().parents[1] != pathlib.Path(sys.argv[1]).resolve():
    raise SystemExit(97)
"""

_CHILD_CLI = _PROVENANCE + """
from graft_ipa.cli import app

sys.argv = ["graft", *sys.argv[2:]]
app()
"""

# A verb that warns is the realistic way stderr gets written from inside the
# boundary — `_warn` is how the build loop reports a retention sweep that
# failed — but no verb warns unprompted, so the boundary is driven directly.
# What is under test here is the interpreter's shutdown, not the dispatch.
_CHILD_WARNS_TO_STDERR = _PROVENANCE + """
import typer
from graft_ipa import cli

@cli._reported
def _warns():
    cli._warn(None, "the tweaks archive could not be swept")

try:
    _warns()
except typer.Exit as exit_request:
    raise SystemExit(exit_request.exit_code)
"""


def _hermetic_env(home: Path) -> dict[str, str]:
    """The `describe_proc` environment: nothing of the developer's on PATH, and
    XDG roots that have never seen an `--init`, so no verb can reach the real
    ~/.config/graft."""
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "cfg"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
    }


def _spawn_with_dead_stdout(home: Path, *cli_args: str) -> tuple[int, str]:
    """Run the CLI with a stdout pipe nobody holds the read end of.

    The read end is closed in the parent immediately, well before the child has
    finished importing typer, so this neither races nor needs the child to emit
    enough to fill a pipe buffer. Returns (exit status, stderr).
    """
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_CLI, str(REPO_ROOT), *cli_args],
        stdout=write_fd,
        stderr=subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=_hermetic_env(home),
    )
    os.close(write_fd)
    os.close(read_fd)
    assert proc.stderr is not None
    stderr = proc.stderr.read().decode()
    proc.stderr.close()
    proc.wait()
    return proc.returncode, stderr


def _spawn_with_dead_stderr(home: Path, stdout_path: Path) -> int:
    """The mirror image: stderr's reader is gone, stdout is a real file.

    stdout being a *file* is the point — a file cannot break, so this isolates
    the second stream, and the file's size afterwards shows the boundary did
    not go silencing descriptors that were working.
    """
    read_fd, write_fd = os.pipe()
    with stdout_path.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-c", _CHILD_WARNS_TO_STDERR, str(REPO_ROOT)],
            stdout=out,
            stderr=write_fd,
            cwd=str(REPO_ROOT),
            env=_hermetic_env(home),
        )
        os.close(write_fd)
        os.close(read_fd)
        proc.wait()
    return proc.returncode


def _assert_ran_this_worktree(status: int) -> None:
    assert status != 97, (
        "the child imported graft_ipa from outside this worktree — the pipx "
        "editable install or the parent checkout won, and this test measured "
        "nothing"
    )


# `--describe` and `--version` are the two output paths, and they were broken
# differently. `--describe` writes through `JsonEmitter` straight to
# `sys.stdout` and had no handler at all, so it exited 120; `--version` writes
# through rich, which has its own `on_broken_pipe`, and exited 1. Neither needs
# a config or a device, so this stays a test about the CLI and nothing else.
@pytest.mark.parametrize("verb", ["--describe", "--version"])
def test_a_reader_that_hangs_up_is_not_an_internal_error(verb: str, tmp_path: Path) -> None:
    status, stderr = _spawn_with_dead_stdout(tmp_path, verb)

    _assert_ran_this_worktree(status)
    # 120 is the interpreter's, not the CLI's: it means the shutdown flush of
    # sys.stdout failed and buried whatever status the handler picked.
    assert status != 120, f"the interpreter's shutdown flush still fails:\n{stderr}"
    assert status == 141, stderr
    assert stderr == "", f"a hung-up reader should say nothing at all, got:\n{stderr}"


def test_a_hung_up_stderr_is_answered_the_same_way(tmp_path: Path) -> None:
    """The second stream, which produced its own 120 before it was handled.

    Measured while writing this: with only `sys.stdout` in the boundary's
    list, a warning written into a dead stderr still exited 120 on the
    interpreter's shutdown flush.
    """
    out = tmp_path / "stdout.txt"

    status = _spawn_with_dead_stderr(tmp_path, out)

    _assert_ran_this_worktree(status)
    assert status != 120, "the shutdown flush of sys.stderr still fails"
    assert status == 141
    # stdout was a file all along and nothing routes warnings there; if this
    # ever grows content it means the boundary redirected the wrong descriptor
    # or the warning channel moved off stderr.
    assert out.stat().st_size == 0


class _DeadPipe:
    """A stdout whose reader has gone: writes are accepted, flushes are not.

    A real `os.pipe()` would work too, but these tests want to assert that
    *nothing* reached stdout, and a stub can be asked. `fileno` raising is
    faithful as well as convenient — it is what `CliRunner`'s stdout does — and
    it exercises the branch of `_silence` that copes with a stream having no
    descriptor to redirect.

    Deliberately not an `io.TextIOBase`: that base class flushes from its
    finaliser, which turns this stub's whole purpose into a
    `PytestUnraisableExceptionWarning` at some later garbage collection.
    """

    def __init__(self) -> None:
        self.written: list[str] = []

    def write(self, s: str) -> int:
        self.written.append(s)
        return len(s)

    def flush(self) -> None:
        raise BrokenPipeError(errno.EPIPE, os.strerror(errno.EPIPE))

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")


def _boundary_result(exc: BaseException) -> typer.Exit:
    """Drive `_reported` with a callable that raises `exc`; return its Exit."""
    from graft_ipa import cli as cli_mod

    def _raise() -> None:
        raise exc

    with pytest.raises(typer.Exit) as caught:
        cli_mod._reported(_raise)()
    return caught.value


def test_a_hung_up_stdout_exits_141_and_prints_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch itself: a broken stdout, and the status the CLI chose for it."""
    from graft_ipa.errors import EXIT_BROKEN_PIPE

    dead = _DeadPipe()
    monkeypatch.setattr(sys, "stdout", dead)

    exit_request = _boundary_result(BrokenPipeError(errno.EPIPE, os.strerror(errno.EPIPE)))

    assert exit_request.exit_code == EXIT_BROKEN_PIPE == 141
    # Not "no *new* output": the boundary must add nothing at all to a stream
    # whose reader is already gone.
    assert dead.written == []


def test_141_stays_clear_of_the_error_taxonomy() -> None:
    """The whole point of 141 is that it cannot be mistaken for a real failure.

    `GraftError` and its subclasses own 1-16 and the GUI keys on 16, so a
    broken pipe reusing any of those would be indistinguishable from the error
    it collided with — 1, the base class's generic code, most of all.
    """
    from graft_ipa import errors

    taken = {errors.GraftError.exit_code} | {
        cls.exit_code for cls in errors.GraftError.__subclasses__()
    }
    assert len(taken) > 1, "expected to find the error taxonomy"
    assert errors.EXIT_BROKEN_PIPE not in taken


def test_epipe_from_something_that_is_not_our_output_still_reports(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The narrowing, from the other side.

    This package talks SSH to the phone, and a socket whose peer vanished
    mid-push raises `BrokenPipeError` too. That is a genuine failure and has to
    keep the legible `_unexpected` report and the generic exit 1 rather than be
    quietly answered with "your reader hung up". Here `sys.stdout` is pytest's
    capture object and flushes perfectly well, which is exactly the signal the
    boundary discriminates on.
    """
    exit_request = _boundary_result(BrokenPipeError(errno.EPIPE, "Broken pipe"))

    assert exit_request.exit_code == 1
    captured = capsys.readouterr()
    assert "BrokenPipeError" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
