"""Archive retention as it runs automatically at the end of a build.

`_build_one` is driven directly with stubbed pipeline stages. Every path it
touches — built_dir, state_dir, the injected Trash — lives under `tmp_path`;
nothing here reads the real `~/.cache/graft/` or `~/.Trash`.
"""

from __future__ import annotations

import json
import time
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


def _shipped(cfg: Config, name: str) -> Path:
    """An archive an earlier build delivered: it has a history row, so it
    has a Re-install button and a keep slot (`plan_prune`'s `reinstallable`).
    A bare `_archive` with no row is what a failed delivery leaves."""
    path = _archive(cfg, name)
    BuildLog(cfg.state_dir / "log.sqlite").record(
        app="example", bundle_id="com.example.app", version="1.0", tweaks=[],
        ipa_path=path, signing_team=None, udid=None, success=True,
    )
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
        _shipped(cfg, f"example_1.0_{stamp}.ipa")

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
        _shipped(cfg, f"example_1.0_{stamp}.ipa")

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


# --- the retention lock, across processes --------------------------------------
#
# A build shares `retention.lock` from the moment it archives its IPA until its
# history row is written; a sweep holds it alone. So a sweep in another process
# can never trash this build's archive while it is being delivered, or before
# the row that names it exists — and this build's own sweep, which runs after
# it has let go, skips rather than waits when another build is delivering.


def test_a_build_holds_its_archive_until_its_history_row_is_written(tmp_path: Path) -> None:
    """A sweep in another process, run at the moment the row is being written,
    is refused and the archive is still there."""
    from graft_ipa import cli as cli_mod

    from .retention_procs import sweep_from_another_process

    cfg = _cfg(tmp_path, archive_keep=0)
    trash = tmp_path / "Trash"
    seen: dict = {}
    real_record = cli_mod._record_build_row

    def record(log, **row):
        seen["sweep"] = sweep_from_another_process(cfg, trash=trash, home=tmp_path)
        seen["archive_there"] = Path(row["ipa_path"]).exists()
        return real_record(log, **row)

    with patch.object(cli_mod, "_record_build_row", record):
        _run_build(cfg, tmp_path, trash=trash)

    sweep = seen["sweep"]
    assert sweep.returncode == 1, sweep.stdout + sweep.stderr
    assert "refused: a build is delivering" in sweep.stdout, sweep.stdout
    assert seen["archive_there"], "another process trashed the archive before its row was written"


def test_a_sideload_install_runs_inside_the_archive_hold(tmp_path: Path) -> None:
    """Stage 5 installs the archive itself, over `device_install` now rather
    than ideviceinstaller. A sweep in another process while the IPA is going
    to the phone is refused, and the archive being installed is still there."""
    import dataclasses
    import types

    from graft_ipa import cli as cli_mod
    from graft_ipa import installer

    from .retention_procs import sweep_from_another_process

    cfg = _cfg(tmp_path, archive_keep=0)
    cfg.apps["example"] = dataclasses.replace(cfg.apps["example"], delivery="sideload")
    trash = tmp_path / "Trash"
    source = tmp_path / "in.ipa"
    source.write_bytes(b"x")
    tweaked = tmp_path / "tweaked.ipa"
    tweaked.write_bytes(b"y")
    seen: dict = {}

    def zsign(_cfg, _tweaked, signed, **_):
        signed.write_bytes(b"signed")
        return types.SimpleNamespace(profile_notes=[], stdout="", stderr="",
                                     team_id="TEAM", cert_subject="x", expiry="y")

    def install_ipa(ipa, *, udid, on_progress=None):
        seen["ipa"], seen["udid"] = ipa, udid
        seen["sweep"] = sweep_from_another_process(cfg, trash=trash, home=tmp_path)
        seen["archive_there"] = Path(ipa).exists()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "run_zsign", side_effect=zsign), \
         patch.object(cli_mod, "select_device", return_value=("u1", "usb")), \
         patch.object(installer.device_install, "install_ipa", side_effect=install_ipa):
        acquire.return_value.path = source
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tweaked
        cyan.return_value.stdout = ""
        cyan.return_value.stderr = ""
        cli_mod._build_one(
            cfg=cfg,
            log=BuildLog(cfg.state_dir / "log.sqlite"),
            app_name="example",
            dry_run=False,
            skip_install=False,
            skip_decrypt_check=True,
            verbose=False,
            device=None,
            emitter=None,
            trash_dir=trash,
        )

    assert seen["udid"] == "u1"
    assert Path(seen["ipa"]).parent == cfg.built_dir, "stage 5 installed something other than the archive"
    sweep = seen["sweep"]
    assert sweep.returncode == 1, sweep.stdout + sweep.stderr
    assert "refused: a build is delivering" in sweep.stdout, sweep.stdout
    assert seen["archive_there"], "another process trashed the archive mid-install"


def test_a_builds_own_sweep_skips_when_another_build_is_delivering(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It does not wait for the other build, which may be minutes from done:
    one line says so, and the next build's sweep cleans up."""
    from graft_ipa import cli as cli_mod

    from .retention_procs import held_by_another_process

    monkeypatch.setattr(cli_mod, "_RETENTION_LOCK_TIMEOUT", 3.0)
    cfg = _cfg(tmp_path, archive_keep=0)
    trash = tmp_path / "Trash"
    older = [_archive(cfg, f"example_1.0_{s}.ipa") for s in ("20260101T000000", "20260102T000000")]

    with held_by_another_process(cfg, "sh", home=tmp_path):
        started = time.monotonic()
        _run_build(cfg, tmp_path, trash=trash, emitter=JsonEmitter())
        took = time.monotonic() - started

    assert took < 1.5, f"the build waited {took:.2f}s for another build's delivery"
    assert all(p.exists() for p in older)
    assert not trash.exists() or list(trash.iterdir()) == []
    captured = capsys.readouterr()
    events = [json.loads(line) for line in captured.out.splitlines() if line.strip().startswith("{")]
    assert not [e for e in events if e["type"] in ("prune-end", "build-pruned")]
    notes = [e["line"] for e in events if e["type"] == "log" and e["source"] == "warning"]
    assert notes == ["another build is delivering; old archives are cleaned up next time"], notes
    end = next(e for e in events if e["type"] == "build-end")
    assert end["ok"] is True
    assert [e["type"] for e in events][-1] == "build-end"

    # Next time, nobody delivering: the older archives go.
    _run_build(cfg, tmp_path, trash=trash)
    assert not any(p.exists() for p in older)
