"""Graft's data directories, and moving them from where TweakBuild kept them.

Every test here works under `tmp_path` through the XDG variables (the autouse
fixture in conftest sets all four; tests that need a particular layout set
them again). Nothing reads or writes the developer's real home.
"""

from __future__ import annotations

import dataclasses
import errno
import fcntl
import json
import multiprocessing
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli, config, data_dirs, decrypt_runner
from graft_ipa.data_dirs import LEGACY_DIRNAME, migrate_legacy_dir, migrate_legacy_dirs

from .config_fixtures import write_test_config


def _legacy(root: Path, files: dict[str, str] | None = None) -> Path:
    """`root/tweak-build` as a real directory holding `files`."""
    old = root / LEGACY_DIRNAME
    old.mkdir(parents=True)
    for rel, text in (files or {"config.yaml": "signing: {}\n"}).items():
        (old / rel).parent.mkdir(parents=True, exist_ok=True)
        (old / rel).write_text(text)
    return old


# --- the one-directory migration -------------------------------------------


def test_a_legacy_dir_is_moved_and_a_symlink_left_at_the_old_path(tmp_path: Path) -> None:
    old = _legacy(tmp_path, {"config.yaml": "x: 1\n", "profiles/a.mobileprovision": "P"})

    notice = migrate_legacy_dir(tmp_path)

    new = tmp_path / "graft"
    assert new.is_dir() and not new.is_symlink()
    assert (new / "config.yaml").read_text() == "x: 1\n"
    assert (new / "profiles/a.mobileprovision").read_text() == "P"
    assert old.is_symlink()
    assert old.resolve() == new.resolve()
    assert notice is not None
    assert str(old) in notice and str(new) in notice
    assert "symlink" in notice


def test_the_symlink_is_relative_so_it_survives_the_root_moving(tmp_path: Path) -> None:
    """A link to the absolute new path would break if the home directory were
    renamed or restored elsewhere; a sibling link moves with its parent."""
    _legacy(tmp_path)
    migrate_legacy_dir(tmp_path)
    assert os.readlink(tmp_path / LEGACY_DIRNAME) == "graft"


def test_when_both_exist_the_new_one_is_used_and_it_says_so_every_run(tmp_path: Path) -> None:
    """Two real directories is the user's arrangement to sort out, not ours
    to merge — but never a silent one: the old directory may hold the config
    the user thinks is in use."""
    old = _legacy(tmp_path, {"config.yaml": "old\n"})
    new = tmp_path / "graft"
    new.mkdir()
    (new / "config.yaml").write_text("new\n")

    first = migrate_legacy_dir(tmp_path)
    second = migrate_legacy_dir(tmp_path)

    assert first is not None and first == second
    assert first.startswith(f"both {old} and {new} exist; using {new}")
    assert f"{old} is left untouched" in first
    assert not old.is_symlink()
    assert (old / "config.yaml").read_text() == "old\n"
    assert (new / "config.yaml").read_text() == "new\n"
    assert data_dirs.app_dir(tmp_path) == new


def test_an_empty_new_dir_is_replaced_by_the_migration(tmp_path: Path) -> None:
    """Something made `…/graft` before the first `graft` run — a build script's
    `mkdir -p`, a user poking around. Empty, it holds nothing to protect, and
    keeping it would orphan the real config beside it for good."""
    old = _legacy(tmp_path, {"config.yaml": "keep\n"})
    new = tmp_path / "graft"
    new.mkdir()

    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith(f"moved {old} to {new}")
    assert old.is_symlink()
    assert (new / "config.yaml").read_text() == "keep\n"


def test_a_new_dir_holding_only_an_empty_subdir_is_not_empty(tmp_path: Path) -> None:
    """`mkdir -p ~/.config/graft/tweaks` leaves `graft` non-empty. `rmdir`
    refuses it, so the two directories are reported rather than merged."""
    old = _legacy(tmp_path)
    (tmp_path / "graft" / "tweaks").mkdir(parents=True)

    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith("both ")
    assert old.is_dir() and not old.is_symlink()
    assert (tmp_path / "graft" / "tweaks").is_dir()


@pytest.mark.parametrize("errno_", [errno.ENOTEMPTY, errno.EEXIST], ids=["ENOTEMPTY", "EEXIST"])
def test_a_new_dir_the_rename_refuses_is_reported_as_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, errno_: int
) -> None:
    """`rename` onto a directory replaces it only while it is empty, and
    decides that atomically. Refused, the two directories are both real and
    both kept — which is the "both exist" case, not a failed move."""
    old = _legacy(tmp_path)
    new = tmp_path / "graft"
    new.mkdir()

    def refuse(src, dst):
        raise OSError(errno_, os.strerror(errno_))

    monkeypatch.setattr(data_dirs.os, "rename", refuse)
    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith(f"both {old} and {new} exist")
    assert old.is_dir() and not old.is_symlink()
    assert new.is_dir()


def test_a_dangling_new_path_counts_as_existing(tmp_path: Path) -> None:
    """`exists()` follows links, so a broken `graft` symlink would read as
    absent and the rename would land on top of it. `lexists` is the guard."""
    old = _legacy(tmp_path)
    (tmp_path / "graft").symlink_to(tmp_path / "nowhere")

    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith("both ")
    assert old.is_dir() and not old.is_symlink()
    assert (tmp_path / "graft").is_symlink()


def test_our_own_link_beside_the_new_dir_is_silent(tmp_path: Path) -> None:
    """The state every run after a migration sees."""
    _legacy(tmp_path)
    migrate_legacy_dir(tmp_path)
    assert migrate_legacy_dir(tmp_path) is None


def test_a_user_made_link_at_the_old_path_is_followed(tmp_path: Path) -> None:
    """The user moved their files elsewhere and linked `tweak-build` to them.
    Not ours to move — but it is where the data is, so it is what is used."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "config.yaml").write_text("mine\n")
    (tmp_path / LEGACY_DIRNAME).symlink_to(target)

    assert migrate_legacy_dir(tmp_path) is None

    assert not os.path.lexists(tmp_path / "graft")
    assert (target / "config.yaml").read_text() == "mine\n"
    assert data_dirs.app_dir(tmp_path) == tmp_path / LEGACY_DIRNAME
    assert (data_dirs.app_dir(tmp_path) / "config.yaml").read_text() == "mine\n"


def test_a_user_made_link_beside_an_empty_new_dir_is_reported_not_moved(tmp_path: Path) -> None:
    """The empty-dir cleanup is for a real old directory only. A link is the
    user's own arrangement; renaming it into `graft` is not ours to do."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    old = tmp_path / LEGACY_DIRNAME
    old.symlink_to(target)
    (tmp_path / "graft").mkdir()

    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith("both ")
    assert os.readlink(old) == str(target)
    assert _is_plain_dir(tmp_path / "graft")


def _is_plain_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def test_a_user_made_link_beside_a_new_dir_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / LEGACY_DIRNAME).symlink_to(target)
    (tmp_path / "graft").mkdir()
    (tmp_path / "graft" / "config.yaml").write_text("new\n")

    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith("both ")
    assert data_dirs.app_dir(tmp_path) == tmp_path / "graft"


def test_nothing_to_migrate_is_silent(tmp_path: Path) -> None:
    assert migrate_legacy_dir(tmp_path) is None
    assert migrate_legacy_dir(tmp_path / "does-not-exist") is None
    assert not os.path.lexists(tmp_path / "graft")
    assert data_dirs.app_dir(tmp_path) == tmp_path / "graft"


def test_a_legacy_file_rather_than_a_directory_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / LEGACY_DIRNAME).write_text("not a directory")
    assert migrate_legacy_dir(tmp_path) is None
    assert not os.path.lexists(tmp_path / "graft")


def test_a_failed_move_leaves_the_old_dir_untouched_and_keeps_using_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy(tmp_path, {"config.yaml": "keep\n"})

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(data_dirs.os, "rename", refuse)
    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None
    assert notice.startswith(f"could not move {old} to {tmp_path / 'graft'}")
    assert "Permission denied" in notice
    assert notice.endswith(f"still using {old}")
    assert old.is_dir() and not old.is_symlink()
    assert (old / "config.yaml").read_text() == "keep\n"
    assert not os.path.lexists(tmp_path / "graft")
    assert data_dirs.app_dir(tmp_path) == old


def test_a_failed_symlink_moves_the_dir_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moved but not linked would leave every absolute path in config.yaml
    dangling, which is worse than not migrating at all."""
    old = _legacy(tmp_path, {"config.yaml": "keep\n"})

    def refuse(src, dst):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(data_dirs.os, "symlink", refuse)
    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None
    assert notice.startswith(f"could not move {old}")
    assert "Read-only file system" in notice
    assert old.is_dir() and not old.is_symlink()
    assert (old / "config.yaml").read_text() == "keep\n"
    assert not os.path.lexists(tmp_path / "graft")
    assert data_dirs.app_dir(tmp_path) == old


def test_a_failed_symlink_and_failed_move_back_says_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy(tmp_path)
    real_rename = os.rename
    calls: list[tuple[str, str]] = []

    def rename_once(src, dst):
        calls.append((str(src), str(dst)))
        if len(calls) > 1:
            raise OSError(16, "Resource busy")
        real_rename(src, dst)

    def refuse(src, dst):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(data_dirs.os, "rename", rename_once)
    monkeypatch.setattr(data_dirs.os, "symlink", refuse)
    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None
    assert "Read-only file system" in notice and "Resource busy" in notice
    assert str(old) in notice
    # Nothing could be put back, so the data is where the move left it — and
    # that is where the CLI must look.
    assert data_dirs.app_dir(tmp_path) == tmp_path / "graft"


def test_another_process_migrating_first_is_not_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The GUI starts several CLI processes at once. The one that loses the
    rename finds the work done, which is not a failure worth a warning."""
    old = _legacy(tmp_path)
    real_rename, real_symlink = os.rename, os.symlink

    def other_process_wins(src, dst):
        real_rename(src, dst)
        real_symlink("graft", src)
        raise FileNotFoundError(2, "No such file or directory")

    monkeypatch.setattr(data_dirs.os, "rename", other_process_wins)

    assert migrate_legacy_dir(tmp_path) is None
    assert old.is_symlink()
    assert data_dirs.app_dir(tmp_path) == tmp_path / "graft"


def test_a_loser_on_the_normal_path_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ordinary upgrade, with the GUI starting several CLIs at once: this
    one looked at `tweak-build` (a real directory), and before it looked at
    `graft` another process renamed one onto the other and had not yet made
    the link. `graft` present and `tweak-build` gone is a winner mid-flight,
    not two directories."""
    old = _legacy(tmp_path, {"f": "x"})
    new = tmp_path / "graft"
    real_lexists = os.path.lexists
    raced = []

    def winner_moves_between_our_looks(path):
        if not raced and Path(path) == new:
            raced.append(True)
            os.rename(old, new)  # the other process: renamed, link not yet made
        return real_lexists(path)

    monkeypatch.setattr(data_dirs.os.path, "lexists", winner_moves_between_our_looks)
    assert migrate_legacy_dir(tmp_path) is None
    assert raced


def test_a_loser_after_the_winner_linked_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = _legacy(tmp_path, {"f": "x"})
    new = tmp_path / "graft"
    real_lexists = os.path.lexists
    raced = []

    def winner_finishes_between_our_looks(path):
        if not raced and Path(path) == new:
            raced.append(True)
            os.rename(old, new)
            os.symlink("graft", old)
        return real_lexists(path)

    monkeypatch.setattr(data_dirs.os.path, "lexists", winner_finishes_between_our_looks)
    assert migrate_legacy_dir(tmp_path) is None
    assert raced


def test_two_migrations_racing_onto_an_empty_new_dir_end_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both found `graft` empty and `tweak-build` real; the other one renamed
    first and linked. Ours must not undo it: `rmdir` here once deleted the
    winner's directory and renamed the link onto it, leaving `graft -> graft`."""
    old = tmp_path / LEGACY_DIRNAME  # empty, like a never-used share dir
    old.mkdir()
    new = tmp_path / "graft"
    new.mkdir()
    real_rename, real_rmdir = os.rename, os.rmdir
    winner: list[str | None] = []

    def winner_runs_now() -> None:
        # The other process, start to finish, at our first filesystem change —
        # whichever call that is.
        if not winner:
            monkeypatch.setattr(data_dirs.os, "rename", real_rename)
            monkeypatch.setattr(data_dirs.os, "rmdir", real_rmdir)
            winner.append(migrate_legacy_dir(tmp_path))
            monkeypatch.setattr(data_dirs.os, "rename", rename)
            monkeypatch.setattr(data_dirs.os, "rmdir", rmdir)

    def rename(src, dst):
        winner_runs_now()
        return real_rename(src, dst)

    def rmdir(path):
        winner_runs_now()
        return real_rmdir(path)

    monkeypatch.setattr(data_dirs.os, "rename", rename)
    monkeypatch.setattr(data_dirs.os, "rmdir", rmdir)
    loser = migrate_legacy_dir(tmp_path)

    assert winner and winner[0] is not None and winner[0].startswith("moved ")
    assert loser is None
    assert new.is_dir() and not new.is_symlink()
    assert os.readlink(old) == "graft"


def test_both_empty_without_a_race_migrates(tmp_path: Path) -> None:
    old = tmp_path / LEGACY_DIRNAME
    old.mkdir()
    new = tmp_path / "graft"
    new.mkdir()

    notice = migrate_legacy_dir(tmp_path)

    assert notice is not None and notice.startswith("moved ")
    assert new.is_dir() and not new.is_symlink()
    assert os.readlink(old) == "graft"


def _race_worker(root: str, barrier, results) -> None:
    barrier.wait()
    results.put(migrate_legacy_dir(Path(root)))


@pytest.mark.parametrize("layout", ["old-only", "old-full", "both-empty", "old-full-new-empty"])
def test_six_real_processes_racing_end_in_one_migration(tmp_path: Path, layout: str) -> None:
    """Real processes released together, as the GUI does at launch. Every
    round must end with `graft` a real directory holding the data and
    `tweak-build -> graft`, exactly one "moved" notice, and no "both"."""
    ctx = multiprocessing.get_context("fork")
    for i in range(15):
        root = tmp_path / f"r{i}"
        (root / LEGACY_DIRNAME).mkdir(parents=True)
        if "full" in layout:
            (root / LEGACY_DIRNAME / "f").write_text("x")
        if layout in ("both-empty", "old-full-new-empty"):
            (root / "graft").mkdir()
        barrier, results = ctx.Barrier(6), ctx.Queue()
        procs = [ctx.Process(target=_race_worker, args=(str(root), barrier, results)) for _ in range(6)]
        for proc in procs:
            proc.start()
        notices = [results.get(timeout=30) for _ in procs]
        for proc in procs:
            proc.join(timeout=30)

        new, old = root / "graft", root / LEGACY_DIRNAME
        assert new.is_dir() and not new.is_symlink(), (layout, i)
        assert os.readlink(old) == "graft", (layout, i)
        if "full" in layout:
            assert (new / "f").read_text() == "x"
        assert sum(1 for n in notices if n and n.startswith("moved ")) == 1, (layout, i, notices)
        assert not [n for n in notices if n and not n.startswith("moved ")], (layout, i, notices)


# --- absolute paths through the old spelling -------------------------------


def test_paths_spelled_with_the_old_dir_keep_working(tmp_path: Path) -> None:
    """The owner's config.yaml names its cert, profiles, key and cache by
    absolute `~/.config/tweak-build/...` paths. The link is what keeps them."""
    _legacy(tmp_path, {
        "config.yaml": "x: 1\n",
        "AppleDevelopment.p12": "p12",
        "profiles/a.mobileprovision": "P",
    })
    migrate_legacy_dir(tmp_path)

    old = tmp_path / LEGACY_DIRNAME
    assert (old / "AppleDevelopment.p12").read_text() == "p12"
    assert (old / "profiles/a.mobileprovision").read_text() == "P"
    # Written through the old spelling, read through the new one.
    (old / "profiles/b.mobileprovision").write_text("Q")
    assert (tmp_path / "graft/profiles/b.mobileprovision").read_text() == "Q"


def test_the_config_lock_is_one_lock_under_either_spelling(tmp_path: Path) -> None:
    """`config.yaml.lock` sits beside the config. A GUI started with the old
    path and a CLI using the new default must contend for the same lock, or
    two writers could interleave."""
    _legacy(tmp_path)
    migrate_legacy_dir(tmp_path)
    via_old = config._lock_file(tmp_path / LEGACY_DIRNAME / "config.yaml")
    via_new = config._lock_file(tmp_path / "graft" / "config.yaml")

    fd_old = os.open(via_old, os.O_RDWR | os.O_CREAT, 0o600)
    fd_new = os.open(via_new, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd_old, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd_new, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd_old)
        os.close(fd_new)


# --- all four roots ---------------------------------------------------------


def _four_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    roots = {
        "XDG_CONFIG_HOME": tmp_path / "c",
        "XDG_CACHE_HOME": tmp_path / "k",
        "XDG_STATE_HOME": tmp_path / "s",
        "XDG_DATA_HOME": tmp_path / "d",
    }
    for var, root in roots.items():
        monkeypatch.setenv(var, str(root))
    return roots


def test_every_root_is_migrated_with_one_notice_each(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _four_roots(tmp_path, monkeypatch)
    for root in roots.values():
        _legacy(root)

    notices = migrate_legacy_dirs()

    assert len(notices) == 4
    for root in roots.values():
        assert (root / "graft").is_dir()
        assert (root / LEGACY_DIRNAME).is_symlink()
        assert sum(str(root / LEGACY_DIRNAME) in n for n in notices) == 1
    # Done once: a second run has nothing to say.
    assert migrate_legacy_dirs() == []


def test_a_failure_in_one_root_does_not_stop_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _four_roots(tmp_path, monkeypatch)
    for root in roots.values():
        _legacy(root)
    real_rename = os.rename

    def refuse_cache(src, dst):
        if Path(src).parent == roots["XDG_CACHE_HOME"]:
            raise PermissionError(13, "Permission denied")
        real_rename(src, dst)

    monkeypatch.setattr(data_dirs.os, "rename", refuse_cache)
    notices = migrate_legacy_dirs()

    assert len(notices) == 4
    assert sum(n.startswith("could not move") for n in notices) == 1
    assert data_dirs.cache_dir() == roots["XDG_CACHE_HOME"] / LEGACY_DIRNAME
    assert data_dirs.config_dir() == roots["XDG_CONFIG_HOME"] / "graft"
    assert data_dirs.state_dir() == roots["XDG_STATE_HOME"] / "graft"
    assert data_dirs.data_dir() == roots["XDG_DATA_HOME"] / "graft"


def test_xdg_data_home_falls_back_to_local_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert data_dirs.xdg_data_home() == tmp_path / ".local" / "share"
    assert data_dirs.data_dir() == tmp_path / ".local" / "share" / "graft"


# --- the defaults that live under them -------------------------------------


@pytest.mark.parametrize("migrated", [True, False], ids=["migrated", "unmigrated"])
def test_every_default_path_follows_the_directory_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, migrated: bool
) -> None:
    """Under `…/graft` once migrated; under the old dir while a migration has
    not happened (or could not), because that is where the files are."""
    roots = _four_roots(tmp_path, monkeypatch)
    for root in roots.values():
        _legacy(root)
    if migrated:
        migrate_legacy_dirs()
    name = "graft" if migrated else LEGACY_DIRNAME
    c, k, s, d = (roots[v] / name for v in roots)

    assert config.default_config_path() == c / "config.yaml"
    assert config.default_tweaks_dir() == c / "tweaks"
    assert config.default_cache_dir() == k / "ipas"
    assert config.default_built_dir() == k / "built"
    assert config.default_state_dir() == s
    from graft_ipa import artwork

    assert artwork.artwork_dir() == k / "artwork"
    assert config.AppStoreToolsConfig().unfairplay == d / "unfairplay" / "unfairplay"
    monkeypatch.delenv("GRAFT_FRIDA_IOS_DUMP", raising=False)
    assert decrypt_runner.frida_dump_home() == d / "frida-ios-dump"


def test_the_frida_ios_dump_override_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_FRIDA_IOS_DUMP", str(tmp_path / "mine"))
    assert decrypt_runner.frida_dump_home() == tmp_path / "mine"


# --- the CLI runs it -------------------------------------------------------


def _cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    roots = _four_roots(tmp_path, monkeypatch)
    for var, root in roots.items():
        old = root / LEGACY_DIRNAME
        old.mkdir(parents=True)
        if var == "XDG_CONFIG_HOME":
            write_test_config(old / "config.yaml", team_id="TESTTEAM01")
    return roots


def test_the_cli_migrates_before_a_verb_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _cli_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli.app, ["--list"])

    assert result.exit_code == 0, result.output
    for root in roots.values():
        assert (root / LEGACY_DIRNAME).is_symlink()
    assert result.output.count("warning: moved ") == 4
    # And the config it then loaded was the migrated one, via the new path.
    assert "example" in result.output


def test_under_json_the_notice_is_a_startup_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = _cli_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli.app, ["--json", "--list"])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    warnings = [e for e in events if e.get("type") == "log" and e.get("source") == "startup"]
    assert len(warnings) == 4
    assert {w["line"].split(" ")[1] for w in warnings} == {
        str(root / LEGACY_DIRNAME) for root in roots.values()
    }


def test_a_plain_build_migrates_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No verb flag selects nothing, and a build is what runs then — the most
    common first command after upgrading. How it ends (here: an app the
    config does not have) is not the point; that it looked in the new place is."""
    roots = _cli_home(tmp_path, monkeypatch)

    CliRunner().invoke(cli.app, ["no-such-app"])

    for root in roots.values():
        assert (root / LEGACY_DIRNAME).is_symlink()


@pytest.mark.parametrize("flag", ["--describe", "--version"])
def test_describe_and_version_never_migrate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, flag: str
) -> None:
    """Both answer questions about the program, not the user's data — and the
    GUI's contract test runs `--describe` against whatever is on PATH."""
    roots = _cli_home(tmp_path, monkeypatch)

    result = CliRunner().invoke(cli.app, [flag])

    assert result.exit_code == 0, result.output
    for root in roots.values():
        assert not (root / LEGACY_DIRNAME).is_symlink()
        assert not os.path.lexists(root / "graft")


def test_init_migrates_rather_than_starting_a_second_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--init` needs no config, but it writes one. Run before any other verb
    on an upgraded machine it would create `…/graft` beside the old dir, and
    from then on both exist and the old one is ignored for good."""
    roots = _cli_home(tmp_path, monkeypatch)
    seen: list[Path] = []
    # The real `--init` is interactive; stand in for it with something that
    # records where it would have written the config.
    monkeypatch.setattr(cli, "_VERBS", tuple(
        dataclasses.replace(v, run=lambda **_: seen.append(config.default_config_path()))
        if getattr(v, "param", None) == "init" else v
        for v in cli._VERBS
    ))

    result = CliRunner().invoke(cli.app, ["--init"])

    assert result.exit_code == 0, result.output

    assert (roots["XDG_CONFIG_HOME"] / LEGACY_DIRNAME).is_symlink()
    assert seen == [roots["XDG_CONFIG_HOME"] / "graft" / "config.yaml"]
