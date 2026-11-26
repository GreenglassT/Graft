"""`graft --update` through the CLI: its flags, its gates, and the plan."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import keychain
from graft_ipa.config import set_app_options

from .update_fakes import DEVELOPMENT


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


def test_a_plan_is_one_update_plan_event(world) -> None:
    world.store["com.example.app"] = "2.0"

    result = _run(["--json", "--update", "--all", "--plan"])

    assert result.exit_code == 0, result.output
    assert [e["type"] for e in _events(result)] == ["update-plan"], "--plan emits it alone"
    (event,) = _events(result)
    assert event["quick"] is False
    assert [a["app"] for a in event["apps"]] == ["bravo", "example"], "sidebar order"
    assert world.build.calls == [] and world.downloads() == 0, "a plan changes nothing"


@pytest.mark.parametrize("quick", [True, False])
def test_no_plan_reads_the_keychain_or_runs_ipatool(world, monkeypatch, quick: bool) -> None:
    """Spec 3, review #3: a paid account with a development-signed sideload
    app, the case whose build reads the .p12 password, and shims that record
    rather than raise, so nothing that catches can hide a keychain read."""
    asked: list[object] = []

    def security(argv, **kwargs):
        asked.append(list(argv))
        empty = "" if kwargs.get("text") else b""
        return subprocess.CompletedProcess(argv, 44, empty, empty)  # "not found"

    monkeypatch.setattr(keychain, "_run", security)
    monkeypatch.setattr(keychain, "_lock_probe", lambda: asked.append("lock probe") or False)
    set_app_options(world.cfg_path, "example", delivery="sideload")
    world.profile(DEVELOPMENT)
    world.store["com.example.app"] = "2.0"
    assert not world.cfg.free_account

    result = _run(["--json", "--update", "--all", "--plan", *(["--quick"] if quick else [])])

    assert result.exit_code == 0, result.output
    assert asked == [], "no plan reads the keychain"
    assert world.tools.calls("ipatool") == []
    (event,) = _events(result)
    assert next(a for a in event["apps"] if a["app"] == "example")["store"]["to"] == "2.0"


def test_a_quick_plan_through_the_cli_opens_no_session(world) -> None:
    world.store["com.example.app"] = "2.0"

    result = _run(["--json", "--update", "example", "--plan", "--quick"])

    assert result.exit_code == 0, result.output
    (event,) = [e for e in _events(result) if e["type"] == "update-plan"]
    assert event["apps"][0]["readiness"] == "likely"
    assert world.sessions == []


def test_a_plan_for_a_person_is_one_line_per_app(world) -> None:
    world.store["com.example.app"] = "2.0"

    result = _run(["--update", "--all", "--plan"])

    assert result.exit_code == 0, result.output
    assert "Example+: App Store 2.0 · rebuild & install" in result.stdout
    assert "bravo: up to date" in result.stdout


def test_update_needs_app_names_or_all(world) -> None:
    result = _run(["--update"])

    assert result.exit_code == 2
    assert "--update needs the apps to update, or --all" in result.output


def test_names_and_all_together_are_refused(world) -> None:
    assert _run(["--update", "example", "--all", "--plan"]).exit_code == 2


def test_quick_without_plan_is_refused(world) -> None:
    result = _run(["--update", "--all", "--quick"])

    assert result.exit_code == 2
    assert "--quick works only with --plan" in result.output


@pytest.mark.parametrize("flag", ["--plan", "--quick", "--all", "--with-affected"])
def test_its_flags_without_update_are_refused(world, flag: str) -> None:
    result = _run([flag])

    assert result.exit_code == 2
    assert "--update" in result.output


@pytest.mark.parametrize("extra,refusal", [
    (["--app", "example"], "--app is not valid with --update"),
    (["--dry-run"], "--dry-run is not valid with --update"),
    (["--device", "00008130-FAKE00000000PHONE1"], "--device is not valid with --update"),
])
def test_update_refuses_what_it_would_ignore(world, extra: list[str], refusal: str) -> None:
    result = _run(["--update", "example", "--plan", *extra])

    assert result.exit_code == 2
    assert refusal in " ".join(result.output.split())


def test_describe_lists_the_update_flags_and_events(contract: dict) -> None:
    assert {"--update", "--all", "--plan", "--quick", "--with-affected"} <= set(contract["flags"])
    assert {"update-plan", "update-step", "update-stopping", "update-app-end",
            "update-end"} <= set(contract["events"])


# --- --drop-update-target (spec §11.3, "Devices that are gone") ---------------------


def _drop(*extra: str):
    from .update_fakes import OTHER_PHONE

    return _run(["--json", "--drop-update-target", "example", "--device", OTHER_PHONE, *extra])


def test_drop_update_target_leaves_that_device_out_of_the_next_plan(world, monkeypatch) -> None:
    from graft_ipa import devices_seen

    from .update_fakes import OTHER_PHONE, PHONE

    asked: list[object] = []
    monkeypatch.setattr(keychain, "_run", lambda argv, **_k: asked.append(argv))
    world.deliver("example", udid=OTHER_PHONE)
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")

    result = _drop()

    assert result.exit_code == 0, result.output
    assert _events(result) == [{"type": "update-target-dropped", "app": "example",
                                "udid": OTHER_PHONE, "device_name": "Example iPad"}]
    assert asked == [] and world.sessions == [], "no keychain, no device"
    world.store["com.example.app"] = "2.0"
    plan = _run(["--json", "--update", "example", "--plan"])
    (event,) = _events(plan)
    assert [t["udid"] for t in event["apps"][0]["targets"]] == [PHONE]


def test_drop_update_target_for_a_person_says_what_it_did(world) -> None:
    from .update_fakes import OTHER_PHONE

    world.deliver("example", udid=OTHER_PHONE)

    result = _run(["--drop-update-target", "example", "--device", OTHER_PHONE])

    assert result.exit_code == 0, result.output
    assert "example" in result.stdout and OTHER_PHONE in result.stdout


def test_drop_update_target_refuses_a_device_the_app_isnt_on(world) -> None:
    result = _drop()

    assert result.exit_code == 2
    assert "example isn't on that device" in result.output


def test_drop_update_target_refuses_an_unknown_app(world) -> None:
    from .update_fakes import OTHER_PHONE

    result = _run(["--drop-update-target", "nope", "--device", OTHER_PHONE])

    assert result.exit_code == 2
    assert "nope" in result.output


def test_drop_update_target_needs_a_device(world) -> None:
    result = _run(["--drop-update-target", "example"])

    assert result.exit_code == 2
    assert "--device" in result.output


def test_drop_update_target_refuses_app_names_it_would_ignore(world) -> None:
    from .update_fakes import OTHER_PHONE

    world.deliver("example", udid=OTHER_PHONE)

    result = _run(["--drop-update-target", "example", "bravo", "--device", OTHER_PHONE])

    assert result.exit_code == 2
    assert "not valid with --drop-update-target" in " ".join(result.output.split())


def test_describe_lists_the_drop_flag_and_event(contract: dict) -> None:
    assert "--drop-update-target" in contract["flags"]
    assert "update-target-dropped" in contract["events"]
