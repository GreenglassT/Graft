"""`--prune-builds`: the on-demand half of archive retention.

Every test runs against `temp_config_env`, whose XDG_* redirection puts
built_dir, state_dir and the build log under `tmp_path`. `_sandboxed` asserts
that redirection actually took effect before any test moves a file, so a
regression in the fixture can't quietly point these tests at the user's real
`~/.cache/graft/built/`. The Trash is injected for the same reason —
nothing here may reach `~/.Trash`.
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import time
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
    newer = _archive(cfg, "bravo_1.0_20260601T010000.ipa")
    build_id, _ = (
        log.record(
            app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
            ipa_path=path, signing_team=None, udid=None, success=True,
        )
        for path in (doomed, newer)
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


# --- a build in another process ---------------------------------------------
#
# A build archives its IPA and only then pushes or installs it, which can take
# minutes, and `--reinstall-build` re-ships an archive of any age. Both share
# `retention.lock` while that archive is in flight, and every sweep holds it
# alone: so at `archive_keep: 0` an on-demand sweep (the app's "Clean up old
# builds…", or a Terminal) can no longer move an archive mid-delivery to the
# Trash. It waits, then refuses. These hold the lock from a real second
# process (`tests/retention_procs.py`).


def test_a_sweep_cannot_trash_an_archive_while_another_process_shares_the_lock(
    cfg: Config, trash: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    from .retention_procs import held_by_another_process

    delivering = _archive(cfg, "bravo_1.0_20260601T000000.ipa")
    monkeypatch.setattr(cli_mod, "_RETENTION_LOCK_TIMEOUT", 0.3)

    with held_by_another_process(cfg, "sh", home=tmp_path):
        with pytest.raises(GraftError, match="a build is delivering"):
            cli_mod._do_prune_builds(cfg, keep=0, trash_dir=trash)
        assert delivering.exists(), "the sweep trashed an archive a build was delivering"
        assert not trash.exists() or list(trash.iterdir()) == []

    # The build is done with it: the same sweep now takes it.
    cli_mod._do_prune_builds(cfg, keep=0, trash_dir=trash)
    assert _remaining(cfg) == []


def test_prune_builds_gives_up_cleanly_after_its_wait(
    temp_config_env: Path, cfg: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It waits a while for the build to finish, then says why it won't, with a
    non-zero exit and nothing moved — no traceback, no half-done sweep."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    from .retention_procs import held_by_another_process

    # Nothing may reach the real ~/.Trash, even if the lock fails to hold.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(cli_mod, "_RETENTION_LOCK_TIMEOUT", 0.6)
    delivering = _archive(cfg, "bravo_1.0_20260601T000000.ipa")

    with held_by_another_process(cfg, "sh", home=tmp_path):
        started = time.monotonic()
        result = CliRunner().invoke(cli_mod.app, ["--json", "--prune-builds", "--keep", "0"])
        took = time.monotonic() - started

    assert result.exit_code == 1, result.output
    said = " ".join(result.stderr.split())
    assert "a build is delivering an archived IPA, or another clean-up of old builds is running" in said, said
    assert "try again when it has finished" in said, said
    assert "Traceback" not in result.output
    assert not [line for line in result.stdout.splitlines() if '"prune-end"' in line]
    assert delivering.exists()
    assert 0.6 <= took < 10, f"gave up after {took:.2f}s, not after its wait"


def test_an_archive_no_one_holds_is_pruned_however_new(cfg: Config, trash: Path) -> None:
    """No more "written in the last ten minutes" guess: the lock says whether
    a build still has it, so a fresh archive nobody holds is like any other,
    and `prune-end` no longer reports a `recent` count."""
    from graft_ipa import cli as cli_mod

    _archive(cfg, "bravo_1.0_20260601T000000.ipa")
    _archive(cfg, "bravo_1.0_20260601T010000.ipa")

    buf = StringIO()
    cli_mod._do_prune_builds(cfg, keep=0, emitter=JsonEmitter(stream=buf), trash_dir=trash)

    assert _remaining(cfg) == []
    summary = next(e for e in _events(buf) if e["type"] == "prune-end")
    assert summary["removed"] == 2
    assert "recent" not in summary


def test_the_sweep_after_a_build_still_keeps_only_what_it_was_told(
    cfg: Config, trash: Path
) -> None:
    """A build's own sweep excludes its own archive by path, and
    `archive_keep: 0` still means "keep nothing but the build just made"."""
    from graft_ipa import cli as cli_mod

    older = _archive(cfg, "bravo_1.0_20260601T000000.ipa")
    mine = _archive(cfg, "bravo_1.0_20260601T010000.ipa")

    cli_mod._prune_archives(cfg, keep=0, dry_run=False, exclude=[mine], trash_dir=trash)

    assert _remaining(cfg) == [mine.name]
    assert [p.name for p in trash.iterdir()] == [older.name]


def test_a_second_sweep_waits_for_the_first(
    cfg: Config, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two sweeps at once planned the same removals; the second then failed on
    every file the first had already moved, and exited 1 saying archives
    "could not be moved". They now take turns on one lock."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    _archive(cfg, "bravo_1.0_20260601T000000.ipa")
    monkeypatch.setattr(cli_mod, "_RETENTION_LOCK_TIMEOUT", 0.2)

    lock = cfg.cache_dir / ".locks" / "retention.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("w") as other_sweep:
        fcntl.flock(other_sweep, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(GraftError, match="another clean-up of old builds is running"):
            cli_mod._do_prune_builds(cfg, keep=0, trash_dir=trash)
        assert len(_remaining(cfg)) == 1, "it moved files while another sweep held the lock"

    # Released: the same sweep now runs.
    cli_mod._do_prune_builds(cfg, keep=0, trash_dir=trash)
    assert _remaining(cfg) == []


def _built(log: BuildLog, path: Path, *, delivered: bool) -> int:
    """A history row as the build envelope writes it: a build whose push or
    install failed after archiving has no ipa_path (`_record_history`)."""
    return log.record(
        app="example", bundle_id="com.example.app", version="1.0", tweaks=[],
        ipa_path=path if delivered else None, signing_team=None, udid=None,
        success=delivered, error=None if delivered else "no iOS devices connected",
    )


def test_archives_from_failed_deliveries_never_outrank_the_re_installable_one(
    cfg: Config, trash: Path
) -> None:
    """Finding #1: one build that shipped, then two whose phone wasn't
    plugged in. The two later ones archived before they failed, so their
    files are newest — and ranking on the filename alone kept those two and
    trashed the only archive with a Re-install button, clearing its row for
    good. The re-installable archive takes a slot first; the default keep's
    second goes to the newest of the others (re-check #15), never ahead of it."""
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    good = _archive(cfg, "example_1.0_20261119T100000.ipa")
    good_id = _built(log, good, delivered=True)
    for stamp in ("20261119T110000", "20261119T120000"):
        _built(log, _archive(cfg, f"example_1.0_{stamp}.ipa"), delivered=False)
    assert cfg.archive_keep == 2

    cli_mod._do_prune_builds(cfg, trash_dir=trash)

    assert _remaining(cfg) == [good.name, "example_1.0_20261119T120000.ipa"]
    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        (kept,) = conn.execute("SELECT ipa_path FROM builds WHERE id = ?", (good_id,)).fetchone()
    assert kept == str(good), "the re-installable build lost its Re-install pointer"

    cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert _remaining(cfg) == [good.name], "at keep 1 a failed delivery took the only slot"


def test_a_failed_delivery_never_displaces_a_re_installable_archive(
    cfg: Config, trash: Path
) -> None:
    """keep: 2 with two re-installable builds and a newer orphan: both
    re-installable archives keep their slots, and the orphan, which holds
    none, goes."""
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    older = _archive(cfg, "example_1.0_20261119T090000.ipa")
    newer = _archive(cfg, "example_1.0_20261119T100000.ipa")
    for path in (older, newer):
        _built(log, path, delivered=True)
    orphan = _archive(cfg, "example_1.0_20261119T110000.ipa")
    _built(log, orphan, delivered=False)

    cli_mod._do_prune_builds(cfg, keep=2, trash_dir=trash)

    assert _remaining(cfg) == [older.name, newer.name]
    assert [p.name for p in trash.iterdir()] == [orphan.name]


def test_a_build_s_own_sweep_still_spares_its_archive_before_its_row_counts(
    cfg: Config, trash: Path
) -> None:
    """`exclude` vetoes removal whatever the history says, so a build's own
    archive survives its sweep even at keep: 0."""
    from graft_ipa import cli as cli_mod

    BuildLog(cfg.state_dir / "log.sqlite")
    mine = _archive(cfg, "example_1.0_20261119T120000.ipa")

    cli_mod._prune_archives(cfg, keep=0, dry_run=False, exclude=[mine], trash_dir=trash)

    assert _remaining(cfg) == [mine.name]


def test_a_failed_move_gives_the_row_its_archive_back(
    cfg: Config, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review #5: the row was cleared before the move and left cleared when
    the move failed, so an archive still on disk lost its Re-install button."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    log = BuildLog(cfg.state_dir / "log.sqlite")
    paths = [_archive(cfg, f"bravo_1.0_{s}.ipa") for s in ("20260601T000000", "20260601T010000")]
    ids = [
        log.record(app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
                   ipa_path=p, signing_team=None, udid=None, success=True)
        for p in paths
    ]

    def _broken(path: Path, *, trash_dir: Path | None = None) -> Path:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(cli_mod, "move_to_trash", _broken)

    with pytest.raises(GraftError, match="could not be moved"):
        cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        rows = dict(conn.execute("SELECT id, ipa_path FROM builds").fetchall())
    assert rows == {ids[0]: str(paths[0]), ids[1]: str(paths[1])}


def test_a_history_naming_none_of_the_archives_keeps_the_newest_n(
    cfg: Config, trash: Path
) -> None:
    """Re-check #15, the reviewer's case: a state directory reset, then one
    build that failed pre-flight, which leaves a log.sqlite with a single
    row naming no archive. `--keep 10` then trashed all six archives, while
    with no log.sqlite it said "nothing to prune"."""
    from graft_ipa import cli as cli_mod

    log = BuildLog(cfg.state_dir / "log.sqlite")
    _built(log, cfg.built_dir / "never-archived.ipa", delivered=False)
    names = [
        _archive(cfg, f"{app}_1.0_{stamp}.ipa").name
        for app in ("example", "other")
        for stamp in ("20261119T100001", "20261119T100002", "20261119T100003")
    ]

    cli_mod._do_prune_builds(cfg, keep=10, trash_dir=trash)

    assert _remaining(cfg) == sorted(names)


# --- re-check #28: a history it can't read refuses the clean-up ----------------


def test_a_history_that_cannot_be_read_moves_nothing(
    cfg: Config, trash: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planning without it is #1's loss: a failed delivery's archive outranks
    the re-installable one, which is trashed and its row cleared."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import GraftError

    log = BuildLog(cfg.state_dir / "log.sqlite")
    good = _archive(cfg, "example_1.0_20261119T100000.ipa")
    good_id = _built(log, good, delivered=True)
    for stamp in ("20261119T110000", "20261119T120000"):
        _built(log, _archive(cfg, f"example_1.0_{stamp}.ipa"), delivered=False)
    before = _remaining(cfg)

    def busy(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(BuildLog, "reinstallable_paths", busy)

    with pytest.raises(GraftError, match="couldn't read the build history") as raised:
        cli_mod._do_prune_builds(cfg, keep=1, trash_dir=trash)

    assert "nothing was moved" in raised.value.hint
    assert _remaining(cfg) == before
    with sqlite3.connect(cfg.state_dir / "log.sqlite") as conn:
        (kept,) = conn.execute("SELECT ipa_path FROM builds WHERE id = ?", (good_id,)).fetchone()
    assert kept == str(good)
