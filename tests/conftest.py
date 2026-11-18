"""Shared pytest fixtures for the graft CLI test suite."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .config_fixtures import write_test_config

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _isolated_xdg_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own XDG roots, whether it asked for them or not.

    Not optional since the rename: the CLI's first act is to move a legacy
    `…/tweak-build` directory to `…/graft` and leave a symlink behind. A test
    that invoked `main` with only `--config` pointed somewhere safe, and
    inherited the developer's real XDG roots, would have migrated the
    developer's real config, cache, state and data directories. Tests that set
    their own roots (`temp_config_env`, the subprocess fixtures) override
    these; everything else lands under `tmp_path`.
    """
    for var, name in (
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_DATA_HOME", "xdg-data"),
    ):
        monkeypatch.setenv(var, str(tmp_path / name))


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test talks to an empty fake `security`, never the login keychain.

    Reads used to be harmless here, so a test that reached the signer or the
    pre-flight with no password mocked simply missed. Since the rename a miss
    under `graft` reads the old `tweak-build` item and *writes* a copy — so an
    unmocked read with a team id that exists on the developer's machine would
    add items to their real keychain. `tests/test_keychain.py` installs its
    own seeded fake over this one.
    """
    from graft_ipa import keychain

    from .keychain_fakes import FakeSecurity

    monkeypatch.setattr(keychain, "_run", FakeSecurity())
    # The fake covers this process only. A CLI the suite spawns as a
    # subprocess imports keychain afresh, and this switch is what keeps it
    # off the real `security` too.
    monkeypatch.setenv("GRAFT_NO_KEYCHAIN", "1")


@pytest.fixture
def temp_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG dirs + a config.yaml with one app (`example`). Returns the config path.

    Use this whenever a test needs to invoke the CLI as a subprocess and shouldn't
    pollute the real `~/.config/graft/`.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "cfg" / "graft" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    write_test_config(cfg, team_id="TESTTEAM01")
    return cfg


@pytest.fixture(scope="session")
def describe_proc(
    tmp_path_factory: pytest.TempPathFactory,
) -> subprocess.CompletedProcess[str]:
    """`graft --describe`, run once for the whole session.

    `--describe` prints one deterministic line and exits; three modules assert
    against it and between them were spawning a fresh interpreter eight times
    per run — 1.24s of a 4.7s suite for the same string.

    Session-scoped, so `tmp_path_factory` rather than `tmp_path`. The hermetic
    environment is the point of the fixture and is preserved exactly: a `PATH`
    with nothing of the developer's on it, and XDG roots under a directory that
    has never seen an `--init`. Pointing them at empty paths is what makes a
    config lookup fail loudly instead of silently finding the real
    `~/.config/graft/` — the GUI's contract test runs `--describe` on
    machines that have never run `--init`, and this is how that stays true.
    """
    home = tmp_path_factory.mktemp("describe-home")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "cfg"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
    }
    return subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--describe"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture(scope="session")
def contract(describe_proc: subprocess.CompletedProcess[str]) -> dict:
    """The decoded `cli-contract` payload. Read-only: it is shared session-wide."""
    assert describe_proc.returncode == 0, describe_proc.stderr
    lines = describe_proc.stdout.splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line, got {lines!r}"
    return json.loads(lines[0])
