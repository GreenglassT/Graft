"""Turning an app's auto-provisioning on and off from the CLI (C-AUTOPROFILE).

`auto_profile: true` under an app makes its first build register an App ID
and a provisioning profile through App Store Connect. It could only be set by
editing config.yaml; `--set-app-options --auto-profile` / `--no-auto-profile`
sets it, modeled on `--lc-bundle-id` / `--no-lc-bundle-id`, and reports
`auto_profile` in `changed` so the app's Edit sheet can reconcile it.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from graft_ipa.cli import app as cli_app
from graft_ipa.config import load_config, remove_signing_section
from graft_ipa.json_output import APP_OPTION_FIELDS


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def _run(*args: str):
    return CliRunner().invoke(cli_app, list(args))


def _on_disk(cfg_path: Path) -> object:
    return yaml.safe_load(cfg_path.read_text())["apps"]["example"].get("auto_profile", "<absent>")


def _with_key(cfg_path: Path) -> None:
    raw = yaml.safe_load(cfg_path.read_text())
    raw["app_store_connect"] = {"key_id": "K", "issuer_id": "I", "key_file": "/k.p8"}
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_auto_profile_is_an_app_option_field() -> None:
    assert "auto_profile" in APP_OPTION_FIELDS


def test_turning_it_on_writes_it_and_reports_it_changed(temp_config_env: Path) -> None:
    _with_key(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert result.exit_code == 0, result.output
    (event,) = [e for e in _events(result.stdout) if e["type"] == "app-updated"]
    assert event["changed"] == ["auto_profile"]
    assert _on_disk(temp_config_env) is True
    assert load_config(temp_config_env).apps["example"].auto_profile is True


def test_turning_it_off_removes_the_key(temp_config_env: Path) -> None:
    _with_key(temp_config_env)
    _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    result = _run("--json", "--set-app-options", "--app", "example", "--no-auto-profile")

    assert result.exit_code == 0, result.output
    (event,) = [e for e in _events(result.stdout) if e["type"] == "app-updated"]
    assert event["changed"] == ["auto_profile"]
    assert _on_disk(temp_config_env) == "<absent>", "off is the default, so it is not written"


def test_the_list_carries_it(temp_config_env: Path) -> None:
    _with_key(temp_config_env)
    _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    result = _run("--json", "--list")

    (app,) = [e for e in _events(result.stdout) if e["type"] == "app" and e["name"] == "example"]
    assert app["auto_profile"] is True


def test_other_fields_leave_it_alone(temp_config_env: Path) -> None:
    _with_key(temp_config_env)
    _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    _run("--json", "--set-app-options", "--app", "example", "--compression-level", "3")

    assert _on_disk(temp_config_env) is True


def test_a_free_config_cannot_turn_it_on(temp_config_env: Path) -> None:
    remove_signing_section(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert result.exit_code == 2, result.output
    assert "needs a paid Apple Developer Program membership" in " ".join(result.stderr.split())
    assert _on_disk(temp_config_env) == "<absent>"


def test_a_free_config_can_still_turn_it_off(temp_config_env: Path) -> None:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["example"]["auto_profile"] = True
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    remove_signing_section(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--no-auto-profile")

    assert result.exit_code == 0, result.output
    assert _on_disk(temp_config_env) == "<absent>"


def test_turning_it_on_without_a_key_warns_and_still_saves(temp_config_env: Path) -> None:
    """A key can come after; the warning says the build needs one first."""
    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert result.exit_code == 0, result.output
    assert _on_disk(temp_config_env) is True
    warnings = [e["line"] for e in _events(result.stdout)
                if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 1 and "App Store Connect" in warnings[0], warnings


def test_turning_it_on_with_a_key_does_not_warn(temp_config_env: Path) -> None:
    _with_key(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert not [e for e in _events(result.stdout) if e["type"] == "log"]


def test_the_flag_belongs_to_set_app_options(temp_config_env: Path) -> None:
    result = _run("--json", "--set-settings", "--archive-keep", "3", "--auto-profile")

    assert result.exit_code == 2
    assert "--auto-profile is not valid with --set-settings" in " ".join(result.stderr.split())


# LiveContainer delivery never signs, so `auto_profile` there is inert: saved
# (it may be set ahead of a switch to sideload), but said to do nothing.


def _deliver_by_livecontainer(cfg_path: Path) -> None:
    raw = yaml.safe_load(cfg_path.read_text())
    raw["apps"]["example"]["delivery"] = "livecontainer"
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def _warnings(stdout: str) -> list[str]:
    return [e["line"] for e in _events(stdout) if e["type"] == "log" and e["source"] == "warning"]


def test_turning_it_on_for_a_livecontainer_app_warns_and_still_saves(temp_config_env: Path) -> None:
    _with_key(temp_config_env)
    _deliver_by_livecontainer(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert result.exit_code == 0, result.output
    assert _on_disk(temp_config_env) is True, "a warning, not a refusal"
    (event,) = [e for e in _events(result.stdout) if e["type"] == "app-updated"]
    assert event["changed"] == ["auto_profile"]
    (warning,) = _warnings(result.stdout)
    assert "LiveContainer" in warning and "sideload" in warning, warning
    assert "LiveContainer" in " ".join(result.stderr.split()), "the terminal gets it too"


def test_switching_to_livecontainer_in_the_same_call_warns(temp_config_env: Path) -> None:
    _with_key(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example",
                  "--delivery", "livecontainer", "--auto-profile")

    assert result.exit_code == 0, result.output
    (warning,) = _warnings(result.stdout)
    assert "LiveContainer" in warning, warning


def test_switching_to_sideload_in_the_same_call_does_not_warn(temp_config_env: Path) -> None:
    _with_key(temp_config_env)
    _deliver_by_livecontainer(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example",
                  "--delivery", "sideload", "--auto-profile")

    assert result.exit_code == 0, result.output
    assert _warnings(result.stdout) == []


def test_a_livecontainer_app_with_no_key_gets_only_the_livecontainer_warning(
    temp_config_env: Path,
) -> None:
    """No key is not why it does nothing, and its builds do not fail for it."""
    _deliver_by_livecontainer(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert result.exit_code == 0, result.output
    (warning,) = _warnings(result.stdout)
    assert "LiveContainer" in warning and "App Store Connect" not in warning, warning


def test_turning_it_off_for_a_livecontainer_app_does_not_warn(temp_config_env: Path) -> None:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["example"]["auto_profile"] = True
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    _deliver_by_livecontainer(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--no-auto-profile")

    assert result.exit_code == 0, result.output
    assert _warnings(result.stdout) == []


# --- a per-app `profile:` outranks auto_profile (review #22) -------------------


def _with_override(cfg_path: Path, profile: str = "~/old.mobileprovision") -> None:
    raw = yaml.safe_load(cfg_path.read_text())
    raw["apps"]["example"]["delivery"] = "sideload"
    raw["apps"]["example"]["profile"] = profile
    cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_turning_it_on_under_a_profile_override_says_it_has_no_effect(
    temp_config_env: Path,
) -> None:
    """The override wins in `_plan_profile`, so this saved, reported
    changed=[auto_profile], and every build kept signing with old.mobileprovision."""
    _with_key(temp_config_env)
    _with_override(temp_config_env)

    result = _run("--json", "--set-app-options", "--app", "example", "--auto-profile")

    assert result.exit_code == 0, result.output
    warnings = [e["line"] for e in _events(result.stdout)
                if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 1 and "profile:" in warnings[0], warnings
    assert "old.mobileprovision" in warnings[0]


def test_the_list_names_the_override(temp_config_env: Path) -> None:
    _with_override(temp_config_env)

    result = _run("--json", "--list")

    (app,) = [e for e in _events(result.stdout) if e["type"] == "app"]
    assert app["profile_override"].endswith("old.mobileprovision")


def test_the_list_says_null_without_one(temp_config_env: Path) -> None:
    result = _run("--json", "--list")

    (app,) = [e for e in _events(result.stdout) if e["type"] == "app"]
    assert app["profile_override"] is None


def test_clearing_the_key_does_not_name_an_app_that_signs_with_its_own_profile(
    temp_config_env: Path,
) -> None:
    _with_key(temp_config_env)
    _with_override(temp_config_env)
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"]["example"]["auto_profile"] = True
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))

    result = _run("--json", "--clear-portal")

    assert result.exit_code == 0, result.output
    assert not [e for e in _events(result.stdout)
                if e["type"] == "log" and "example" in e.get("line", "")]
