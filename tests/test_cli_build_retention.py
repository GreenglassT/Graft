"""Archive retention as it runs automatically at the end of a build.

`_build_one` is driven directly with stubbed pipeline stages. Every path it
touches — built_dir, state_dir, the injected Trash — lives under `tmp_path`;
nothing here reads the real `~/.cache/graft/` or `~/.Trash`.
"""

from __future__ import annotations

import json
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
from graft_ipa.errors import CyanError
from graft_ipa.json_output import JsonEmitter


def _cfg(tmp_path: Path, *, archive_keep: int) -> Config:
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
            "example": AppConfig(
                name="example",
                bundle_id="com.example.app",
                preferred_version="latest",
                tweaks=[],
                cyan=CyanOptions(),
                delivery="livecontainer",
            )
        },
        archive_keep=archive_keep,
    )


def _archive(cfg: Config, name: str) -> Path:
    cfg.built_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.built_dir / name
    path.write_bytes(b"\x00" * 8)
    return path


def _run_build(
    cfg: Config,
    tmp_path: Path,
    *,
    trash: Path,
    emitter: JsonEmitter | None = None,
    cyan_error: bool = False,
) -> None:
    """Drive `_build_one` with every external stage stubbed out.

    `skip_install` and `skip_decrypt_check` keep the device and the Mach-O
    inspection out of it — the archive step and what follows it is the whole
    point here.
    """
    from graft_ipa import cli as cli_mod

    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")
    tweaked = tmp_path / "tweaked.ipa"
    tweaked.write_bytes(b"y")

    cyan_patch = (
        patch.object(cli_mod, "run_cyan", side_effect=CyanError("cyan blew up"))
        if cyan_error
        else patch.object(cli_mod, "run_cyan")
    )
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         cyan_patch as cyan:
        acquire.return_value.path = source
        acquire.return_value.version = "1.0"
        if not cyan_error:
            cyan.return_value.output_ipa = tweaked
            cyan.return_value.stdout = ""
            cyan.return_value.stderr = ""
        cli_mod._build_one(
            cfg=cfg,
            log=BuildLog(cfg.state_dir / "log.sqlite"),
            app_name="example",
            dry_run=False,
            skip_install=True,
            skip_decrypt_check=True,
            verbose=False,
            device=None,
            emitter=emitter,
            trash_dir=trash,
        )


def test_a_successful_build_prunes_older_archives(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, archive_keep=2)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000", "20260103T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")

    _run_build(cfg, tmp_path, trash=trash)

    remaining = sorted(p.name for p in cfg.built_dir.iterdir())
    # keep=2 counts the fresh archive: it is the newest, so the policy keeps it
    # and one predecessor. Being excluded only vetoes removal — it buys no
    # extra slot.
    assert len(remaining) == 2
    assert "example_1.0_20260103T000000.ipa" in remaining
    assert sorted(p.name for p in trash.iterdir()) == [
        "example_1.0_20260101T000000.ipa",
        "example_1.0_20260102T000000.ipa",
    ]


def test_the_archive_the_build_just_produced_is_never_pruned(tmp_path: Path) -> None:
    """Even at `archive_keep: 0`, which would otherwise take everything."""
    cfg = _cfg(tmp_path, archive_keep=0)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")

    _run_build(cfg, tmp_path, trash=trash)

    remaining = [p.name for p in cfg.built_dir.iterdir()]
    assert len(remaining) == 1
    fresh = cfg.built_dir / remaining[0]

    # The surviving archive is the one this build recorded, and its build row
    # still points at it — the Re-install path stays intact.
    with BuildLog(cfg.state_dir / "log.sqlite")._connect() as conn:
        paths = [r[0] for r in conn.execute("SELECT ipa_path FROM builds").fetchall()]
    assert paths == [str(fresh)]


def test_a_failed_build_prunes_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, archive_keep=1)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000", "20260103T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")
    before = sorted(p.name for p in cfg.built_dir.iterdir())

    with pytest.raises(CyanError):
        _run_build(cfg, tmp_path, trash=trash, cyan_error=True)

    assert sorted(p.name for p in cfg.built_dir.iterdir()) == before
    assert not trash.exists() or list(trash.iterdir()) == []


def test_a_dry_run_build_prunes_nothing(tmp_path: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = _cfg(tmp_path, archive_keep=1)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000", "20260103T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")
    before = sorted(p.name for p in cfg.built_dir.iterdir())

    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "lc_runner"):
        acquire.return_value.path = source
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = source
        cyan.return_value.stdout = ""
        cyan.return_value.stderr = ""
        cli_mod._build_one(
            cfg=cfg,
            log=BuildLog(cfg.state_dir / "log.sqlite"),
            app_name="example",
            dry_run=True,
            skip_install=True,
            skip_decrypt_check=True,
            verbose=False,
            device=None,
            emitter=None,
            trash_dir=trash,
        )

    assert sorted(p.name for p in cfg.built_dir.iterdir()) == before


def test_the_prune_is_visible_in_the_build_event_stream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A build that quietly deletes archives is worse than one that grows.

    The prune events also land ahead of build-end, so a consumer that treats
    build-end as terminal still sees them.
    """
    cfg = _cfg(tmp_path, archive_keep=2)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")

    _run_build(cfg, tmp_path, trash=trash, emitter=JsonEmitter())

    out = capsys.readouterr().out
    events = [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]
    types = [e["type"] for e in events]

    pruned = [e for e in events if e["type"] == "build-pruned"]
    assert [e["filename"] for e in pruned] == ["example_1.0_20260101T000000.ipa"]
    summary = next(e for e in events if e["type"] == "prune-end")
    assert summary["removed"] == 1 and summary["keep"] == 2
    assert types.index("prune-end") < types.index("build-end")


def test_the_prune_is_visible_in_human_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = _cfg(tmp_path, archive_keep=1)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")

    _run_build(cfg, tmp_path, trash=trash)

    out = capsys.readouterr().out
    assert "example_1.0_20260101T000000.ipa" in out
    assert "empty the Trash" in out


def test_a_retention_failure_does_not_fail_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Housekeeping runs inside the build's own try/except.

    Without its own guard, a Trash that can't be written would be recorded as a
    failed build — telling the user the install never happened when it did.
    """
    from graft_ipa import cli as cli_mod

    cfg = _cfg(tmp_path, archive_keep=1)
    trash = tmp_path / "Trash"
    for stamp in ("20260101T000000", "20260102T000000"):
        _archive(cfg, f"example_1.0_{stamp}.ipa")

    def _boom(*args, **kwargs):
        raise RuntimeError("trash on fire")

    monkeypatch.setattr(cli_mod, "plan_prune", _boom)

    _run_build(cfg, tmp_path, trash=trash)

    # stderr, not stdout: stdout is the JSON event stream under --json, and a
    # warning printed there is one the GUI structurally cannot show. `_warn`
    # sends every warning to stderr and (with an emitter) to a `log` event.
    assert "archive retention failed" in capsys.readouterr().err
    with BuildLog(cfg.state_dir / "log.sqlite")._connect() as conn:
        successes = [
            r[0] for r in conn.execute("SELECT success FROM builds").fetchall()
        ]
    assert successes == [1], "the build itself succeeded and must be logged that way"
