"""A Stop is not a failure (contract C-STOP, R1).

The GUI's Stop is a SIGTERM. During zsign, `exit_on_termination` turned it
into `SystemExit(143)`, and the build envelope recorded that like any other
exception: a failed history row, and `build-end {ok: false, error: "143"}`. The
app showed "Last build failed / 143" for a build the user cancelled. And
outside zsign SIGTERM's default disposition killed the process where it stood,
so a Stop during cyan or the push left no `build-end` at all.

Now the whole build runs with SIGTERM (and SIGHUP) raised as `Stopped`, and
the envelope reads `Stopped` and Ctrl-C's `KeyboardInterrupt` as a stop: no
history row, and `build-end {ok: false, stopped: true, error: null}`.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import lc_manage
from graft_ipa.build_log import BuildLog
from graft_ipa.errors import GraftError, Stopped
from graft_ipa.json_output import JsonEmitter

from .test_cli_build_json import _stub_config

ROOT = Path(__file__).resolve().parent.parent


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


def _envelope(tmp_path: Path, raised: BaseException | None) -> tuple[list[dict], BuildLog]:
    log = BuildLog(tmp_path / "log.sqlite")
    buf = StringIO()
    try:
        with cli_mod._BuildReport(JsonEmitter(stream=buf), app_name="example",
                                  bundle_id="com.example.app", delivery="livecontainer") as rep:
            rep.log = log
            rep.version = "1.0"
            if raised is not None:
                raise raised
    except BaseException as exc:
        assert exc is raised
    return _events(buf.getvalue()), log


@pytest.mark.parametrize("raised", [Stopped(signal.SIGTERM), Stopped(signal.SIGHUP),
                                    KeyboardInterrupt()])
def test_a_stop_ends_the_build_as_stopped_and_records_no_row(tmp_path, raised) -> None:
    events, log = _envelope(tmp_path, raised)

    end = events[-1]
    assert end["type"] == "build-end"
    assert end["ok"] is False
    assert end["stopped"] is True
    assert "error" in end and end["error"] is None
    assert log.recent() == [], "a cancelled build is not a failed one"


def test_a_failure_is_still_a_failure(tmp_path) -> None:
    events, log = _envelope(tmp_path, GraftError("cyan exited with code 1"))

    end = events[-1]
    assert end["ok"] is False and end["stopped"] is False
    assert end["error"] == "cyan exited with code 1"
    assert [r["success"] for r in log.recent()] == [0]


def test_a_success_says_it_was_not_stopped(tmp_path) -> None:
    events, _ = _envelope(tmp_path, None)

    assert events[-1]["ok"] is True and events[-1]["stopped"] is False


def test_a_plain_system_exit_is_not_a_stop(tmp_path) -> None:
    """Only the signal handler's `Stopped` means the user stopped it."""
    events, log = _envelope(tmp_path, SystemExit(1))

    assert events[-1]["stopped"] is False
    assert len(log.recent()) == 1


def test_termination_raises_stopped_with_the_shells_exit_status() -> None:
    with pytest.raises(Stopped) as exc, lc_manage.exit_on_termination():
        os.kill(os.getpid(), signal.SIGTERM)

    assert isinstance(exc.value, SystemExit)
    assert exc.value.code == 128 + signal.SIGTERM


def _build_with(tmp_path: Path, cyan_side_effect) -> tuple[list[dict], BuildLog]:
    cfg = _stub_config(tmp_path)
    log = BuildLog(tmp_path / "log.sqlite")
    buf = StringIO()
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan_side_effect):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        with pytest.raises(Stopped):
            cli_mod._build_one(cfg=cfg, log=log, app_name="example", dry_run=True,
                               skip_install=True, skip_decrypt_check=True, verbose=False,
                               device=None, emitter=JsonEmitter(stream=buf))
    return _events(buf.getvalue()), log


def test_a_stop_during_cyan_is_a_stopped_build(tmp_path) -> None:
    events, log = _build_with(tmp_path, Stopped(signal.SIGTERM))

    assert events[-1]["type"] == "build-end" and events[-1]["stopped"] is True
    assert log.recent() == []


@pytest.mark.parametrize("args", [["example"], ["--reinstall-build", "1"]])
def test_every_build_runs_with_termination_raised_as_stopped(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    """The handler, asked from inside the build: deleting the `with` from a
    verb would otherwise leave every other test here green."""
    from typer.testing import CliRunner

    seen: dict[str, object] = {}

    def record(*_a, **_k):
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["hup"] = signal.getsignal(signal.SIGHUP)

    monkeypatch.setattr(cli_mod, "_build_one", record)
    monkeypatch.setattr(cli_mod, "_do_reinstall_build", record)
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))

    result = CliRunner().invoke(cli_mod.app, args)

    assert result.exit_code == 0, result.output
    assert seen["term"] not in (signal.SIG_DFL, before[0])
    assert seen["hup"] not in (signal.SIG_DFL, before[1])
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before


_CHILD = r'''
import os, signal, sys, time
from pathlib import Path
from unittest.mock import MagicMock
from graft_ipa import cli as cli_mod
from tests.test_cli_build_json import _stub_config
from graft_ipa.config import AppConfig, CyanOptions

tmp = Path(sys.argv[1])
cfg = _stub_config(tmp)
cfg.apps["bravo"] = AppConfig(name="bravo", bundle_id="com.bravo.app", preferred_version="latest",
                              tweaks=[], cyan=CyanOptions(), delivery="livecontainer")
cli_mod.run_all_checks = lambda *a, **k: []
acquired = MagicMock()
acquired.path, acquired.version = tmp / "in.ipa", "1.0"
cli_mod.acquire_ipa = lambda *a, **k: acquired

def cyan(*a, **k):
    os.kill(os.getpid(), signal.SIGTERM)  # the GUI's Stop, while cyan runs
    time.sleep(10)

cli_mod.run_cyan = cyan
sys.exit(cli_mod._verb_build_apps(
    cfg=cfg, apps=["example", "bravo"], dry_run=False, skip_install=True, for_playcover=False,
    skip_decrypt_check=True, verbose=False, device=None, json_mode=True,
))
'''


def test_a_real_sigterm_mid_build_ends_stopped_and_attempts_nothing_more(tmp_path) -> None:
    """A real child and a real signal: sent to the test process it would take
    the run with it. The first app ends stopped, the second never starts, the
    history has no row, and the exit status is the shell's 143."""
    script = tmp_path / "child.py"
    script.write_text(_CHILD)
    state = tmp_path / "state"
    proc = subprocess.run(
        [sys.executable, "-B", str(script), str(tmp_path)],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1",
             "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "xdg-state")},
    )

    assert proc.returncode == 128 + signal.SIGTERM, proc.stderr
    events = _events(proc.stdout)
    assert [e["type"] for e in events if e["type"].startswith("build-")] == [
        "build-start", "build-end"
    ], "the second app must not be attempted"
    end = next(e for e in events if e["type"] == "build-end")
    assert (end["ok"], end["stopped"], end["error"]) == (False, True, None)
    with sqlite3.connect(state / "log.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM builds").fetchone()[0] == 0
