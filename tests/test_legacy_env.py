"""Variables renamed with the project are not read under their old names, but
someone who still sets one is told which name to use instead."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from graft_ipa import cli
from graft_ipa.legacy_env import RENAMED, legacy_env_notices

from .config_fixtures import write_test_config


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    for old, new in RENAMED.items():
        monkeypatch.delenv(old, raising=False)
        monkeypatch.delenv(new, raising=False)


def test_the_renames() -> None:
    assert RENAMED == {
        "TB_SSH_KEY": "GRAFT_SSH_KEY",
        "TB_SSH_USER": "GRAFT_SSH_USER",
        "TB_SSH_PASS": "GRAFT_SSH_PASS",
        "TB_FRIDA_IOS_DUMP": "GRAFT_FRIDA_IOS_DUMP",
        "TWEAK_BUILD_LC_GOLDEN": "GRAFT_LC_GOLDEN",
    }


@pytest.mark.parametrize(("old", "new"), list(RENAMED.items()))
def test_an_old_name_alone_is_named_with_its_replacement(
    monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    monkeypatch.setenv(old, "x")
    assert legacy_env_notices() == [f"{old} is set but no longer read; set {new} instead"]


def test_nothing_is_said_when_the_new_name_is_set_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_SSH_KEY", "/old")
    monkeypatch.setenv("GRAFT_SSH_KEY", "/new")
    assert legacy_env_notices() == []


def test_nothing_is_said_when_nothing_old_is_set() -> None:
    assert legacy_env_notices() == []


def test_an_empty_old_variable_is_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TB_SSH_USER", "")
    assert legacy_env_notices() == []


def test_the_old_value_is_not_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Warned about, not quietly used: a fallback would keep the old name
    working forever and nobody would ever move off it."""
    from graft_ipa import decrypt_runner

    monkeypatch.setenv("TB_FRIDA_IOS_DUMP", "/old/place")
    assert str(decrypt_runner.frida_dump_home()) != "/old/place"


def _config(tmp_path, monkeypatch):
    cfg_root = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))
    write_test_config(cfg_root / "graft" / "config.yaml", team_id="TESTTEAM01")


def test_the_cli_warns_on_stderr(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(tmp_path, monkeypatch)
    monkeypatch.setenv("TB_SSH_KEY", "/home/me/.ssh/id")

    result = CliRunner().invoke(cli.app, ["--list"])

    assert result.exit_code == 0, result.output
    assert "warning: TB_SSH_KEY is set but no longer read; set GRAFT_SSH_KEY instead" in result.output


def test_under_json_it_is_a_warning_event(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _config(tmp_path, monkeypatch)
    monkeypatch.setenv("TB_SSH_PASS", "pw")

    result = CliRunner().invoke(cli.app, ["--json", "--list"])

    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert {"type": "log", "source": "warning",
            "line": "TB_SSH_PASS is set but no longer read; set GRAFT_SSH_PASS instead"} in events
