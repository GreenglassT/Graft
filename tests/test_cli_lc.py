"""The four LiveContainer verbs as the GUI invokes them, and the build guard.

`lc_manage`'s logic is tested in `test_lc_*.py`; here only the CLI seam:
argv reaches the right function with the right values, events come out on
stdout as JSON, and verb-only flags are held to their verbs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import lc_manage


def _invoke(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _events(output: str) -> list[dict]:
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    """Replace the four `run_*` entry points with recorders that emit one
    representative event each, through the events object the CLI built."""
    seen: list[tuple[str, dict]] = []

    def status(cfg, *, udid, channel, events):
        seen.append(("status", {"udid": udid, "channel": channel}))
        events.status(**lc_manage.unreachable_payload(udid or "none", "x"))

    def install(cfg, *, udid, instances, channel, force, events):
        seen.append(("install", {"udid": udid, "instances": instances,
                                 "channel": channel, "force": force}))
        events.installed(udid=udid, bundle_id="b", instance=1, version="1", channel="nightly")

    def push(cfg, *, udid, events):
        seen.append(("push", {"udid": udid}))
        events.cert_pushed(udid=udid, bundle_id="b", path="Documents/AppleDevelopment.p12")

    def clean(cfg, *, udid, events, cert_only=False):
        seen.append(("clean", {"udid": udid, **({"cert_only": True} if cert_only else {})}))
        events.cleaned(udid=udid, bundle_id="b", removed=[], kept=0)

    monkeypatch.setattr(lc_manage, "run_status", status)
    monkeypatch.setattr(lc_manage, "run_install", install)
    monkeypatch.setattr(lc_manage, "run_push_cert", push)
    monkeypatch.setattr(lc_manage, "run_clean", clean)
    return seen


def test_lc_status_for_one_device(temp_config_env: Path, calls) -> None:
    result = _invoke(["--json", "--lc-status", "--device", "U1", "--channel", "stable"])

    assert result.exit_code == 0, result.output
    assert calls == [("status", {"udid": "U1", "channel": "stable"})]
    assert [e["type"] for e in _events(result.output)] == ["lc-status"]


def test_lc_install_passes_every_flag(temp_config_env: Path, calls) -> None:
    result = _invoke(["--json", "--lc-install", "--device", "U1", "--instances", "2,3",
                      "--channel", "nightly", "--force"])

    assert result.exit_code == 0, result.output
    assert calls == [("install", {"udid": "U1", "instances": "2,3",
                                  "channel": "nightly", "force": True})]
    assert _events(result.output)[0]["type"] == "lc-installed"


def test_lc_install_without_force_is_not_forced(temp_config_env: Path, calls) -> None:
    _invoke(["--json", "--lc-install", "--device", "U1"])

    assert calls[0][1]["force"] is False


@pytest.mark.parametrize(("flag", "kind", "event"), [
    ("--lc-push-cert", "push", "lc-cert-pushed"),
    ("--lc-clean", "clean", "lc-cleaned"),
])
def test_the_document_verbs(temp_config_env: Path, calls, flag: str, kind: str, event: str) -> None:
    result = _invoke(["--json", flag, "--device", "U1"])

    assert result.exit_code == 0, result.output
    assert calls == [(kind, {"udid": "U1"})]
    assert [e["type"] for e in _events(result.output)] == [event]


def test_without_json_a_human_gets_lines_not_events(temp_config_env: Path, calls) -> None:
    result = _invoke(["--lc-push-cert", "--device", "U1"])

    assert result.exit_code == 0, result.output
    assert _events(result.output) == []
    assert "certificate copied to b Documents/AppleDevelopment.p12" in result.output


@pytest.mark.parametrize("args", [
    ["--lc-status", "--force"],
    ["--lc-status", "--instances", "1"],
    ["--lc-push-cert", "--channel", "stable"],
    ["--lc-clean", "--force"],
])
def test_verb_only_flags_are_held_to_their_verbs(temp_config_env: Path, calls, args) -> None:
    result = _invoke([*args, "--device", "U1"])

    assert result.exit_code == 2, result.output
    assert calls == []


def test_bad_instances_and_channel_are_refused_before_anything_runs(temp_config_env: Path) -> None:
    """The real `run_install`, not the recorder: the parse happens first."""
    for args, said in (
        (["--instances", "1,4"], "--instances takes a comma list of 1, 2 and 3"),
        (["--channel", "beta"], "--channel must be nightly or stable"),
    ):
        result = _invoke(["--lc-install", "--device", "U1", *args])
        assert result.exit_code == 2, result.output
        assert said in " ".join(result.output.split())


def test_a_sideloaded_livecontainer_app_does_not_build(temp_config_env: Path) -> None:
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["apps"] = {"livecontainer": {
        "bundle_id": "com.kdt.livecontainer", "delivery": "sideload", "tweaks": [],
        "preferred_version": "latest",
    }}
    temp_config_env.write_text(yaml.safe_dump(raw))

    result = _invoke(["--json", "--dry-run", "livecontainer"])

    ends = [e for e in _events(result.output) if e["type"] == "build-end"]
    assert len(ends) == 1 and ends[0]["ok"] is False
    assert ends[0]["error"].startswith(
        "livecontainer: building LiveContainer through the sideload path"
    )


def test_lc_clean_cert_only(temp_config_env: Path, calls) -> None:
    result = _invoke(["--json", "--lc-clean", "--cert-only", "--device", "U1"])

    assert result.exit_code == 0, result.output
    assert calls == [("clean", {"udid": "U1", "cert_only": True})]


def test_cert_only_belongs_to_lc_clean(temp_config_env: Path, calls) -> None:
    result = _invoke(["--lc-push-cert", "--cert-only", "--device", "U1"])

    assert result.exit_code == 2, result.output
    assert calls == []


# ---- every verb runs with SIGTERM turned into a clean exit ----
#
# The keychain clean-up in `temp_keychain` only runs on SIGTERM if the verb
# entered `exit_on_termination` — and deleting that `with` from a handler
# kept every other test here green. So each verb is asked, from inside the
# `run_*` it calls, what SIGTERM and SIGHUP would do right now.


@pytest.mark.parametrize(("args", "target"), [
    (["--lc-status"], "run_status"),
    (["--lc-install"], "run_install"),
    (["--lc-push-cert"], "run_push_cert"),
    (["--lc-clean"], "run_clean"),
])
def test_each_verb_runs_with_termination_turned_into_a_clean_exit(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], target: str
) -> None:
    import signal

    seen: dict[str, object] = {}

    def record(*_a, **_k):
        seen["term"] = signal.getsignal(signal.SIGTERM)
        seen["hup"] = signal.getsignal(signal.SIGHUP)

    monkeypatch.setattr(lc_manage, target, record)
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))

    result = _invoke([*args, "--device", "U1"])

    assert result.exit_code == 0, result.output
    assert seen["term"] not in (signal.SIG_DFL, before[0]), "SIGTERM would kill without cleanup"
    assert seen["hup"] not in (signal.SIG_DFL, before[1])
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before
