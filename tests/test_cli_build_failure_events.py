from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa.build_log import BuildLog
from graft_ipa.config import (
    AppConfig,
    Config,
    CyanOptions,
    SigningConfig,
)
from graft_ipa.errors import GraftError, PreflightError
from graft_ipa.json_output import JsonEmitter
from graft_ipa.preflight import CheckResult


def _cfg(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="X",
            profile=tmp_path / "profile",
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={
            "bravo": AppConfig(
                name="bravo",
                bundle_id="com.bravo.app",
                preferred_version="latest",
                tweaks=["x.deb"],
                cyan=CyanOptions(),
                delivery="livecontainer",
            )
        },
    )


def _build_end_events(out: str) -> list[dict]:
    events = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
    return [e for e in events if e.get("type") == "build-end"]


def test_preflight_failure_emits_build_end_before_raising(tmp_path: Path, capsys) -> None:
    from graft_ipa import cli as cli_mod

    cfg = _cfg(tmp_path)
    failing = [CheckResult(name="cyan", ok=False, hint="install cyan")]
    with patch.object(cli_mod, "run_all_checks", return_value=failing):
        log = BuildLog(tmp_path / "log.sqlite")
        with pytest.raises(PreflightError):
            cli_mod._build_one(
                cfg=cfg, log=log, app_name="bravo", dry_run=False,
                skip_install=False,
                skip_decrypt_check=False, verbose=False, device=None,
                emitter=JsonEmitter(),
            )

    ends = _build_end_events(capsys.readouterr().out)
    assert ends, "expected a build-end event on pre-flight failure"
    assert ends[0]["ok"] is False
    assert "cyan" in (ends[0].get("error") or "")


def test_unexpected_exception_emits_build_end_before_raising(tmp_path: Path, capsys) -> None:
    from graft_ipa import cli as cli_mod

    cfg = _cfg(tmp_path)
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa", side_effect=RuntimeError("boom")):
        log = BuildLog(tmp_path / "log.sqlite")
        with pytest.raises(RuntimeError):
            cli_mod._build_one(
                cfg=cfg, log=log, app_name="bravo", dry_run=False,
                skip_install=False,
                skip_decrypt_check=False, verbose=False, device=None,
                emitter=JsonEmitter(),
            )

    ends = _build_end_events(capsys.readouterr().out)
    assert ends, "expected a build-end event on an unexpected exception"
    assert ends[0]["ok"] is False
    assert "boom" in (ends[0].get("error") or "")


def _events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]


def test_an_unopenable_tee_log_is_reported_as_a_failed_build(
    tmp_path: Path, capsys
) -> None:
    """The per-build tee log used to be opened before `build-start`.

    A state dir that is full or read-only made `log_dir.mkdir()` raise there,
    which is ahead of every guarantee this module makes: no JSON event was ever
    written, `main`'s handler didn't match, and the GUI showed "Failed — exit 1"
    over a completely empty log panel with no hint that the state directory was
    the problem. Opening it inside the envelope is what makes the failure
    reportable, so this asserts on the pair of events, not just the exception.
    """
    from graft_ipa import cli as cli_mod

    cfg = _cfg(tmp_path)
    # A plain file where the build-logs directory belongs: mkdir raises
    # FileExistsError, the same OSError family a read-only or full state dir
    # produces, without needing root or a full disk to arrange.
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "build-logs").write_text("not a directory")

    with patch.object(cli_mod, "run_all_checks", return_value=[]):
        log = BuildLog(cfg.state_dir / "log.sqlite")
        with pytest.raises(GraftError):
            cli_mod._build_one(
                cfg=cfg, log=log, app_name="bravo", dry_run=False,
                skip_install=False,
                skip_decrypt_check=False, verbose=False, device=None,
                emitter=JsonEmitter(),
            )

    events = _events(capsys.readouterr().out)
    assert [e["type"] for e in events if e["type"] in ("build-start", "build-end")] == [
        "build-start",
        "build-end",
    ], "a build that can't open its log still starts and still ends"
    end = next(e for e in events if e["type"] == "build-end")
    assert end["ok"] is False
    assert "build log" in end["error"], end["error"]


def test_a_locked_history_write_cannot_contradict_a_shipped_build(
    tmp_path: Path, capsys
) -> None:
    """`build-end{ok:true}` used to be emitted *before* the history write.

    log.sqlite is the file a concurrent `--reconcile` writes, and its default
    timeout is 5s; when the write raised, control fell into the widened
    `except Exception`, which emitted a SECOND `build-end` saying ok:false. The
    GUI takes the last event it sees, so an app that was installed and running
    on the phone reported "✗ database is locked". One event, emitted last,
    cannot say that — so this pins the count as well as the verdict.
    """
    from graft_ipa import cli as cli_mod

    cfg = _cfg(tmp_path)
    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")
    tweaked = tmp_path / "tweaked.ipa"
    tweaked.write_bytes(b"y")

    log = BuildLog(cfg.state_dir / "log.sqlite")
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(
             BuildLog, "record",
             side_effect=sqlite3.OperationalError("database is locked"),
         ):
        acquire.return_value.path = source
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tweaked
        cyan.return_value.stdout = ""
        cyan.return_value.stderr = ""
        # No exception: a build that shipped must not be failed by bookkeeping.
        cli_mod._build_one(
            cfg=cfg, log=log, app_name="bravo", dry_run=False,
            skip_install=True,
            skip_decrypt_check=True, verbose=False, device=None,
            emitter=JsonEmitter(),
        )

    captured = capsys.readouterr()
    ends = _build_end_events(captured.out)
    assert len(ends) == 1, f"exactly one terminal event per build, got {ends}"
    assert ends[0]["ok"] is True
    assert "database is locked" in captured.err
