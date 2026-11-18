"""`--prune-builds`: the on-demand half of archive retention.

Every test runs against `temp_config_env`, whose XDG_* redirection puts
built_dir, state_dir and the build log under `tmp_path`. `_sandboxed` asserts
that redirection actually took effect before any test moves a file, so a
regression in the fixture can't quietly point these tests at the user's real
`~/.cache/graft/built/`. The Trash is injected for the same reason —
nothing here may reach `~/.Trash`.
"""

from __future__ import annotations

import json
import sqlite3
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa.build_log import BuildLog
from graft_ipa.config import Config, load_config
from graft_ipa.errors import ConfigError
from graft_ipa.json_output import JsonEmitter


def _sandboxed(cfg: Config, tmp_path: Path) -> Config:
    """Fail loudly if the fixture didn't isolate the directories we mutate."""
    for label, path in (
        ("built_dir", cfg.built_dir),
        ("state_dir", cfg.state_dir),
    ):
        assert tmp_path in path.parents or path == tmp_path, (
            f"{label} escaped the sandbox: {path}"
        )
    return cfg


@pytest.fixture
def cfg(temp_config_env: Path, tmp_path: Path) -> Config:
    return _sandboxed(load_config(temp_config_env), tmp_path)


@pytest.fixture
def trash(tmp_path: Path) -> Path:
    return tmp_path / "Trash"


def _archive(cfg: Config, name: str, size: int = 8) -> Path:
    cfg.built_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.built_dir / name
    path.write_bytes(b"\x00" * size)
    return path


def _events(buf: StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _remaining(cfg: Config) -> list[str]:
    return sorted(p.name for p in cfg.built_dir.iterdir())


def test_keeps_the_newest_n_per_app_version_and_trashes_the_rest(
    cfg: Config, trash: Path
) -> None:
    from graft_ipa import cli as cli_mod

    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert _remaining(cfg) == ["bravo_1.0_20260601T020000.ipa"]
    assert sorted(p.name for p in trash.iterdir()) == [
        "bravo_1.0_20260601T000000.ipa",
        "bravo_1.0_20260601T010000.ipa",
    ]


def test_grouping_does_not_leak_across_apps(cfg: Config, trash: Path) -> None:
    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")
    for stamp in ("20260513T024209", "20260513T104409"):
        _archive(cfg, f"delta_44.7.0_{stamp}.ipa")
    _archive(cfg, "example_21.16.2_20260513T011421.ipa")

    from graft_ipa import cli as cli_mod

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert _remaining(cfg) == [
        "bravo_1.0_20260601T010000.ipa",
        "delta_44.7.0_20260513T104409.ipa",
        "example_21.16.2_20260513T011421.ipa",
    ]


def test_a_rebuild_storm_does_not_evict_the_version_before_it(
    cfg: Config, trash: Path
) -> None:
    """End to end over the real Trash move and the real build log.

    Fourteen rebuilds of one version plus two of the version shipped before it.
    Per-app grouping would trash both 21.15.1 archives — the only rollback
    target in the directory — and clear their `ipa_path`s with them, so the
    assertion covers both halves: the files stay, and so do the Re-install
    pointers that make them reachable from the GUI.
    """
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    ids: dict[str, int] = {}
    plan = [("21.16.2", f"20260602T{h:02d}0000") for h in range(14)]
    plan += [("21.15.1", "20260514T090000"), ("21.15.1", "20260514T100000")]
    for version, stamp in plan:
        path = _archive(cfg, f"example_{version}_{stamp}.ipa")
        ids[f"{version}_{stamp}"] = log.record(
            app="example", bundle_id="com.example.app", version=version,
            tweaks=[], ipa_path=path, signing_team=None, udid=None, success=True,
        )

    cli_mod._do_prune_builds(cfg, keep=2, trash_dir=trash)

    assert _remaining(cfg) == [
        "example_21.15.1_20260514T090000.ipa",
        "example_21.15.1_20260514T100000.ipa",
        "example_21.16.2_20260602T120000.ipa",
        "example_21.16.2_20260602T130000.ipa",
    ]
    trashed = sorted(p.name for p in trash.iterdir())
    assert len(trashed) == 12
    assert all(n.startswith("example_21.16.2_") for n in trashed)

    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        rows = dict(conn.execute("SELECT id, ipa_path FROM builds").fetchall())
    for key in ("21.15.1_20260514T090000", "21.15.1_20260514T100000"):
        assert rows[ids[key]] is not None, f"{key} lost its Re-install pointer"
    assert rows[ids["21.16.2_20260602T000000"]] is None


def test_fewer_than_keep_archives_is_a_no_op(cfg: Config, trash: Path) -> None:
    from graft_ipa import cli as cli_mod

    _archive(cfg, "example_21.16.2_20260513T011421.ipa")
    _archive(cfg, "example_21.16.2_20260513T011455.ipa")

    cli_mod._do_prune_builds(cfg, keep=5, trash_dir=trash)

    assert len(_remaining(cfg)) == 2
    assert not trash.exists() or list(trash.iterdir()) == []


def test_dry_run_removes_nothing_and_reports_the_same_set(
    cfg: Config, trash: Path
) -> None:
    from graft_ipa import cli as cli_mod

    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")
    before = _remaining(cfg)

    dry = StringIO()
    cli_mod._do_prune_builds(
        cfg, keep=1, dry_run=True, emitter=JsonEmitter(stream=dry), trash_dir=trash
    )

    assert _remaining(cfg) == before, "a dry run must not touch the archive"
    assert not trash.exists() or list(trash.iterdir()) == []

    wet = StringIO()
    cli_mod._do_prune_builds(
        cfg, keep=1, emitter=JsonEmitter(stream=wet), trash_dir=trash
    )

    def _doomed(buf: StringIO) -> list[str]:
        return sorted(
            e["filename"] for e in _events(buf) if e["type"] == "build-pruned"
        )

    assert _doomed(dry) == _doomed(wet)
    assert _doomed(dry) == [
        "bravo_1.0_20260601T000000.ipa",
        "bravo_1.0_20260601T010000.ipa",
    ]


def test_dry_run_reports_no_trash_path(cfg: Config, trash: Path) -> None:
    from graft_ipa import cli as cli_mod

    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    buf = StringIO()
    cli_mod._do_prune_builds(
        cfg, keep=1, dry_run=True, emitter=JsonEmitter(stream=buf), trash_dir=trash
    )

    pruned = [e for e in _events(buf) if e["type"] == "build-pruned"]
    assert [e["trash_path"] for e in pruned] == [None]
    assert [e["dry_run"] for e in pruned] == [True]


def test_pruned_rows_lose_their_ipa_path_and_kept_rows_keep_theirs(
    cfg: Config, trash: Path
) -> None:
    """Asserted against a real log.sqlite, not a mock.

    The GUI offers "Re-install" for exactly the rows with a non-null ipa_path,
    so a row still pointing at a trashed archive renders a button that can only
    fail.
    """
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    ids: dict[str, int] = {}
    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        path = _archive(cfg, f"bravo_1.0_{stamp}.ipa")
        ids[stamp] = log.record(
            app="bravo",
            bundle_id="com.bravo.app",
            version="1.0",
            tweaks=[],
            ipa_path=path,
            signing_team="TEAM",
            udid=None,
            success=True,
        )

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        rows = dict(conn.execute("SELECT id, ipa_path FROM builds").fetchall())

    assert rows[ids["20260601T000000"]] is None
    assert rows[ids["20260601T010000"]] is None
    kept = rows[ids["20260601T020000"]]
    assert kept is not None and Path(kept).exists()


def test_pruned_event_names_the_build_rows_it_cleared(
    cfg: Config, trash: Path
) -> None:
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    doomed = _archive(cfg, "bravo_1.0_20260601T000000.ipa")
    _archive(cfg, "bravo_1.0_20260601T010000.ipa")
    build_id = log.record(
        app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
        ipa_path=doomed, signing_team=None, udid=None, success=True,
    )

    buf = StringIO()
    cli_mod._do_prune_builds(
        cfg, keep=1, emitter=JsonEmitter(stream=buf), trash_dir=trash
    )

    pruned = [e for e in _events(buf) if e["type"] == "build-pruned"]
    assert [e["build_ids"] for e in pruned] == [[build_id]]


def test_a_dry_run_leaves_ipa_path_intact(cfg: Config, trash: Path) -> None:
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    doomed = _archive(cfg, "bravo_1.0_20260601T000000.ipa")
    _archive(cfg, "bravo_1.0_20260601T010000.ipa")
    build_id = log.record(
        app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
        ipa_path=doomed, signing_team=None, udid=None, success=True,
    )

    cli_mod._do_prune_builds(cfg, keep=1, dry_run=True, trash_dir=trash)

    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        row = conn.execute(
            "SELECT ipa_path FROM builds WHERE id = ?", (build_id,)
        ).fetchone()
    assert row[0] == str(doomed)


def test_pruning_does_not_create_a_build_log_that_did_not_exist(
    cfg: Config, trash: Path
) -> None:
    from graft_ipa import cli as cli_mod

    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")
    db = cfg.state_dir / "log.sqlite"
    assert not db.exists()

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert not db.exists(), "pruning must not leave an empty history database"


def test_keep_defaults_to_the_configured_archive_keep(
    temp_config_env: Path, tmp_path: Path, trash: Path
) -> None:
    """No `--keep` means "whatever the automatic sweep would have done"."""
    from graft_ipa import cli as cli_mod

    raw = temp_config_env.read_text()
    temp_config_env.write_text(f"archive_keep: 2\n{raw}")
    cfg = _sandboxed(load_config(temp_config_env), tmp_path)
    assert cfg.archive_keep == 2

    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    cli_mod._do_prune_builds(cfg, trash_dir=trash)

    assert _remaining(cfg) == [
        "bravo_1.0_20260601T010000.ipa",
        "bravo_1.0_20260601T020000.ipa",
    ]


def test_a_negative_keep_is_rejected_before_anything_moves(
    cfg: Config, trash: Path
) -> None:
    from graft_ipa import cli as cli_mod

    _archive(cfg, "bravo_1.0_20260601T000000.ipa")

    with pytest.raises(ConfigError, match="--keep"):
        cli_mod._do_prune_builds(cfg, keep=-1, trash_dir=trash)

    assert len(_remaining(cfg)) == 1


def test_summary_event_carries_counts_and_bytes(cfg: Config, trash: Path) -> None:
    from graft_ipa import cli as cli_mod

    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa", size=100)
    _archive(cfg, "hand-built.ipa", size=7)

    buf = StringIO()
    cli_mod._do_prune_builds(
        cfg, keep=1, emitter=JsonEmitter(stream=buf), trash_dir=trash
    )

    summary = next(e for e in _events(buf) if e["type"] == "prune-end")
    assert summary["removed"] == 2
    assert summary["kept"] == 1
    assert summary["bytes_removed"] == 200
    assert summary["unparsed"] == 1
    assert summary["failed"] == 0
    assert summary["keep"] == 1
    assert summary["dry_run"] is False


def test_unparsed_files_are_left_alone(cfg: Config, trash: Path) -> None:
    from graft_ipa import cli as cli_mod

    stranger = _archive(cfg, "com.bravo.app_und3fined.ipa")
    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert stranger.exists()


def test_human_output_warns_that_the_trash_still_holds_the_space(
    cfg: Config, trash: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from graft_ipa import cli as cli_mod

    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    out = capsys.readouterr().out
    assert "Trash" in out
    assert "empty the Trash" in out


def test_the_decrypted_source_ipa_cache_is_never_touched(
    cfg: Config, trash: Path
) -> None:
    """`cache_dir` holds decrypted *source* IPAs, not build output.

    Getting one again means a jailbroken device or another App Store
    download and decrypt, so retention has no business anywhere near them — even
    though they sit one directory over and share the .ipa suffix.
    """
    from graft_ipa import cli as cli_mod

    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    sources = []
    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        # Named exactly like an archive, to prove it's the directory that
        # scopes the sweep and not the filename.
        source = cfg.cache_dir / f"bravo_1.0_{stamp}.ipa"
        source.write_bytes(b"\x00")
        sources.append(source)
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert all(s.exists() for s in sources)
    assert len(_remaining(cfg)) == 1


# --- flag wiring ------------------------------------------------------------


def test_keep_without_prune_builds_is_rejected(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`--keep` means nothing to any other verb.

    Typer would drop it silently, so `graft --keep 3 example` would run
    a full build with the flag discarded.
    """
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(cli_app, ["--keep", "3", "example"])

    assert result.exit_code == 2, result.output
    assert "--keep" in result.output
    assert "--prune-builds" in result.output


def test_prune_builds_dry_run_over_the_real_flag_path(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    monkeypatch.setenv("HOME", str(tmp_path))  # nothing may reach the real ~/.Trash
    cfg = _sandboxed(load_config(temp_config_env), tmp_path)
    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    result = CliRunner().invoke(
        cli_app, ["--json", "--prune-builds", "--keep", "1", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    events = [
        json.loads(line)
        for line in result.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    assert sorted(e["filename"] for e in events if e["type"] == "build-pruned") == [
        "bravo_1.0_20260601T000000.ipa",
        "bravo_1.0_20260601T010000.ipa",
    ]
    assert next(e for e in events if e["type"] == "prune-end")["dry_run"] is True
    assert len(_remaining(cfg)) == 3


def test_an_unmovable_archive_is_reported_without_stranding_the_rest(
    cfg: Config, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError
    from graft_ipa.trash import move_to_trash as real_move

    for stamp in ("20260601T000000", "20260601T010000", "20260601T020000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    def _selectively_broken(path: Path, *, trash_dir: Path | None = None) -> Path:
        if path.name.endswith("20260601T000000.ipa"):
            raise PermissionError(13, "Permission denied")
        return real_move(path, trash_dir=trash_dir)

    monkeypatch.setattr(cli_mod, "move_to_trash", _selectively_broken)

    with pytest.raises(GraftError, match="could not be moved"):
        cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert _remaining(cfg) == [
        "bravo_1.0_20260601T000000.ipa",
        "bravo_1.0_20260601T020000.ipa",
    ]


def test_a_failure_is_described_on_the_json_stream_not_just_counted(
    cfg: Config, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    for stamp in ("20260601T000000", "20260601T010000"):
        _archive(cfg, f"bravo_1.0_{stamp}.ipa")

    def _broken(path: Path, *, trash_dir: Path | None = None) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli_mod, "move_to_trash", _broken)

    buf = StringIO()
    with pytest.raises(GraftError):
        cli_mod._do_prune_builds(
            cfg, keep=1, emitter=JsonEmitter(stream=buf), trash_dir=trash
        )

    events = _events(buf)
    logs = [e for e in events if e["type"] == "log" and e["source"] == "retention"]
    assert len(logs) == 1
    assert "bravo_1.0_20260601T000000.ipa" in logs[0]["line"]
    assert "Permission denied" in logs[0]["line"]
    summary = next(e for e in events if e["type"] == "prune-end")
    assert summary["failed"] == 1 and summary["removed"] == 0
