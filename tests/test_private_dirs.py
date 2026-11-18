"""Graft's own directories are 0700, and what it writes into them is 0600.

They hold the .p12 and profiles, staged IPAs, archived builds and every build's
event log, and they were 0755 / 0644: readable by every user on the Mac.
Startup now creates the four `…/graft` directories 0700 and tightens existing
ones, and build logs, archives and staged IPAs are created owner-only. Nothing
outside those four directories is ever chmodded.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import data_dirs

from .test_free_account_build import _build, free_config  # noqa: F401 — the fixture
from .test_lc_discovery import fake_device

ROOTS = ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    out = []
    for var in ROOTS:
        root = tmp_path / var.lower()
        root.mkdir(mode=0o755)
        root.chmod(0o755)
        monkeypatch.setenv(var, str(root))
        out.append(root)
    return out


def test_missing_app_dirs_are_created_owner_only(roots: list[Path]) -> None:
    data_dirs.secure_app_dirs()
    for root in roots:
        assert _mode(root / "graft") == 0o700, root


def test_existing_app_dirs_are_tightened(roots: list[Path]) -> None:
    for root in roots:
        (root / "graft").mkdir(mode=0o755)
        (root / "graft").chmod(0o755)
    data_dirs.secure_app_dirs()
    for root in roots:
        assert _mode(root / "graft") == 0o700, root


def test_nothing_outside_the_app_dirs_is_touched(roots: list[Path], tmp_path: Path) -> None:
    """The XDG roots are shared with every other program, and a `graft` that is
    a link the user made points somewhere that is theirs."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o755)
    (roots[3] / "graft").symlink_to(elsewhere)
    inside = roots[0] / "graft" / "tweaks"
    inside.mkdir(parents=True)
    inside.chmod(0o755)

    data_dirs.secure_app_dirs()

    for root in roots:
        assert _mode(root) == 0o755, root
    assert _mode(elsewhere) == 0o755
    assert (roots[3] / "graft").is_symlink()
    # Only the top of each: what is inside is private by being inside it.
    assert _mode(inside) == 0o755


def test_a_user_linked_legacy_dir_is_still_followed(roots: list[Path], tmp_path: Path) -> None:
    """`app_dir` uses a `tweak-build` link the user made while `graft` is absent;
    creating `graft` there would orphan it."""
    mine = tmp_path / "my-config"
    mine.mkdir()
    (roots[0] / data_dirs.LEGACY_DIRNAME).symlink_to(mine)
    data_dirs.secure_app_dirs()
    assert not (roots[0] / "graft").exists()
    assert data_dirs.config_dir() == roots[0] / data_dirs.LEGACY_DIRNAME


def test_the_cli_secures_them_at_startup(temp_config_env: Path, tmp_path: Path) -> None:
    config_dir = temp_config_env.parent
    config_dir.chmod(0o755)
    result = CliRunner().invoke(cli_mod.app, ["--json", "--settings"])
    assert result.exit_code == 0, result.output
    assert _mode(config_dir) == 0o700
    for var in ("XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert _mode(Path(os.environ[var]) / "graft") == 0o700, var


def test_describe_creates_nothing(roots: list[Path]) -> None:
    """`--describe` describes the program, not the user's files."""
    result = CliRunner().invoke(cli_mod.app, ["--describe"])
    assert result.exit_code == 0, result.output
    for root in roots:
        assert not (root / "graft").exists(), root


def test_a_builds_log_and_archive_are_owner_only(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    fake_device(monkeypatch, ["com.kdt.livecontainer.FGHIJ67890"])
    events = _build(free_config, tmp_path)
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True, events[-1]

    (archive,) = free_config.built_dir.glob("*.ipa")
    assert _mode(archive) == 0o600
    logs = list(free_config.state_dir.rglob("*.jsonl"))
    assert logs, "the build saved no event log"
    for log in logs:
        assert _mode(log) == 0o600, log
