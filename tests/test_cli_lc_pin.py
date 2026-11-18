"""Pinning an app to one LiveContainer from the CLI (contract C-LCPIN).

`lc_bundle_id:` could only be set by editing config.yaml, so a phone with two
primary LiveContainers dead-ended a GUI user: every push refused with "pin
one", and nothing in the app could. `--set-app-options --lc-bundle-id <id>`
sets it, `--no-lc-bundle-id` clears it, and `--list` carries the pin on every
`app` event — the key always present, null when unpinned, because the app
shows its LiveContainer picker only when the key exists.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa.cli import app as cli_app
from graft_ipa.config import load_config
from graft_ipa.json_output import APP_OPTION_FIELDS

LC2 = "com.kdt.LiveContainer2.ABCDE12345"


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def _run(*args: str):
    return CliRunner().invoke(cli_app, list(args))


def _pin_on_disk(cfg_path: Path) -> object:
    return yaml.safe_load(cfg_path.read_text())["apps"]["example"].get("lc_bundle_id", "<absent>")


def test_every_app_event_carries_the_key_null_when_unpinned(temp_config_env: Path) -> None:
    result = _run("--json", "--list")

    assert result.exit_code == 0, result.output
    apps = [e for e in _events(result.stdout) if e["type"] == "app"]
    assert apps
    for event in apps:
        assert "lc_bundle_id" in event, "the key must be present even when unpinned"
        assert event["lc_bundle_id"] is None


def test_the_app_event_carries_the_pin(temp_config_env: Path) -> None:
    assert _run("--json", "--set-app-options", "--app", "example",
                "--lc-bundle-id", LC2).exit_code == 0

    result = _run("--json", "--list")

    (event,) = [e for e in _events(result.stdout) if e["type"] == "app" and e["name"] == "example"]
    assert event["lc_bundle_id"] == LC2


def test_setting_the_pin_writes_it_and_reports_it_changed(temp_config_env: Path) -> None:
    result = _run("--json", "--set-app-options", "--app", "example", "--lc-bundle-id", LC2)

    assert result.exit_code == 0, result.output
    (updated,) = [e for e in _events(result.stdout) if e["type"] == "app-updated"]
    assert updated["changed"] == ["lc_bundle_id"]
    assert _pin_on_disk(temp_config_env) == LC2
    assert load_config(temp_config_env).get_app("example").lc_bundle_id == LC2


def test_clearing_the_pin_removes_it_and_reports_it_changed(temp_config_env: Path) -> None:
    assert _run("--set-app-options", "--app", "example", "--lc-bundle-id", LC2).exit_code == 0

    result = _run("--json", "--set-app-options", "--app", "example", "--no-lc-bundle-id")

    assert result.exit_code == 0, result.output
    (updated,) = [e for e in _events(result.stdout) if e["type"] == "app-updated"]
    assert updated["changed"] == ["lc_bundle_id"]
    assert _pin_on_disk(temp_config_env) == "<absent>"
    assert load_config(temp_config_env).get_app("example").lc_bundle_id is None


def test_clearing_a_pin_that_is_not_there_still_reports_it(temp_config_env: Path) -> None:
    """The app reconciles what it asked for against `changed`; "no pin" is
    the state asked for, and it is the state on disk."""
    result = _run("--json", "--set-app-options", "--app", "example", "--no-lc-bundle-id")

    assert result.exit_code == 0, result.output
    (updated,) = [e for e in _events(result.stdout) if e["type"] == "app-updated"]
    assert updated["changed"] == ["lc_bundle_id"]


@pytest.mark.parametrize("pin", [LC2.lower(), "com.kdt.livecontainer", "com.kdt.LiveContainer3"])
def test_any_livecontainer_id_is_accepted_in_any_case(temp_config_env: Path, pin: str) -> None:
    result = _run("--set-app-options", "--app", "example", "--lc-bundle-id", pin)

    assert result.exit_code == 0, result.output
    assert _pin_on_disk(temp_config_env) == pin


@pytest.mark.parametrize("pin", ["com.example.app", "com.example.livecontainer.ABCDE12345", " "])
def test_a_pin_that_is_not_a_livecontainer_is_refused(temp_config_env: Path, pin: str) -> None:
    result = _run("--json", "--set-app-options", "--app", "example", "--lc-bundle-id", pin)

    assert result.exit_code == 2, result.output
    assert "not a LiveContainer bundle id" in result.output
    assert _pin_on_disk(temp_config_env) == "<absent>"


def test_both_flags_at_once_are_refused(temp_config_env: Path) -> None:
    result = _run("--set-app-options", "--app", "example", "--lc-bundle-id", LC2,
                  "--no-lc-bundle-id")

    assert result.exit_code == 2, result.output
    assert _pin_on_disk(temp_config_env) == "<absent>"


@pytest.mark.parametrize("args", [["--lc-bundle-id", LC2], ["--no-lc-bundle-id"]])
def test_the_flags_mean_nothing_without_set_app_options(
    temp_config_env: Path, args: list[str]
) -> None:
    result = _run(*args, "example")

    assert result.exit_code == 2, result.output
    assert args[0] in result.output


def test_the_field_is_in_the_contract(contract: dict) -> None:
    assert "lc_bundle_id" in APP_OPTION_FIELDS
    assert "lc_bundle_id" in contract["app_option_fields"]
    assert {"--lc-bundle-id", "--no-lc-bundle-id"} <= set(contract["flags"])
