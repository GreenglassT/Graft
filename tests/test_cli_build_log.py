from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa.build_log import BuildLog
from graft_ipa.config import load_config
from graft_ipa.errors import GraftError


def _seed_with_log(cfg, app: str, tmp_path: Path, content: str) -> tuple[int, Path]:
    log_dir = cfg.state_dir / "build-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{app}-test.jsonl"
    log_file.write_text(content)

    bl = BuildLog(cfg.state_dir / "log.sqlite")
    build_id = bl.record(
        app=app, bundle_id=f"com.example.{app}", version="1.0",
        tweaks=[], ipa_path=None,
        signing_team=None, udid=None, success=True,
        log_path=log_file,
    )
    return build_id, log_file


def test_build_log_streams_saved_file(temp_config_env: Path, tmp_path: Path, capsys) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    payload = '{"type":"build-start","app":"x"}\n{"type":"build-end","ok":true}\n'
    build_id, _ = _seed_with_log(cfg, "x", tmp_path, payload)

    cli_mod._do_build_log(cfg, build_id)
    out = capsys.readouterr().out
    assert out == payload


def test_build_log_errors_when_log_path_missing(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    bl = BuildLog(cfg.state_dir / "log.sqlite")
    build_id = bl.record(
        app="x", bundle_id="x", version="1", tweaks=[],
        ipa_path=None, signing_team=None, udid=None, success=True,
        log_path=None,
    )

    with pytest.raises(GraftError, match="no captured log"):
        cli_mod._do_build_log(cfg, build_id)


def test_build_log_errors_when_file_missing(temp_config_env: Path, tmp_path: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    build_id, log_file = _seed_with_log(cfg, "x", tmp_path, "data\n")
    log_file.unlink()

    with pytest.raises(GraftError, match="saved log missing"):
        cli_mod._do_build_log(cfg, build_id)


def test_build_log_errors_for_unknown_id(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    BuildLog(cfg.state_dir / "log.sqlite")  # create empty db
    with pytest.raises(GraftError, match="no build with id"):
        cli_mod._do_build_log(cfg, 12345)

def _build_cfg(tmp_path: Path):
    from graft_ipa.config import (
        AppConfig,
        Config,
        CyanOptions,
        SigningConfig,
    )

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
            "yt": AppConfig(
                name="yt",
                bundle_id="com.example.app",
                preferred_version="latest",
                tweaks=[],
                cyan=CyanOptions(),
                delivery="livecontainer",
            )
        },
    )


def test_the_saved_log_is_a_complete_record_of_the_build(tmp_path: Path, capsys) -> None:
    """The tee file must open with `build-start` and close with `build-end`.

    Both ends are load-bearing and neither is free. The tee is opened *after*
    the build starts — a state dir that won't open has to be reportable as a
    build-end, and opening the file first is what made that failure invisible —
    so `build-start` is replayed into the file; `BuildLogView` renders a
    replayed log from that event and one without it has no header. The handle
    is closed *after* the terminal event, or every saved log would stop one
    event short and replay as a build that never finished.
    """
    import json
    from unittest.mock import patch

    from graft_ipa import cli as cli_mod
    from graft_ipa.json_output import JsonEmitter

    cfg = _build_cfg(tmp_path)
    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")
    tweaked = tmp_path / "tweaked.ipa"
    tweaked.write_bytes(b"y")

    log = BuildLog(cfg.state_dir / "log.sqlite")
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan:
        acquire.return_value.path = source
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tweaked
        cyan.return_value.stdout = ""
        cyan.return_value.stderr = ""
        cli_mod._build_one(
            cfg=cfg, log=log, app_name="yt", dry_run=False,
            skip_install=True,
            skip_decrypt_check=True, verbose=False, device=None,
            emitter=JsonEmitter(), trash_dir=tmp_path / "Trash",
        )
    capsys.readouterr()

    with log._connect() as conn:
        saved = conn.execute("SELECT log_path FROM builds").fetchone()[0]
    events = [
        json.loads(line) for line in Path(saved).read_text().splitlines() if line.strip()
    ]
    assert events[0]["type"] == "build-start"
    assert events[0]["app"] == "yt"
    assert events[-1]["type"] == "build-end"
    assert events[-1]["ok"] is True
