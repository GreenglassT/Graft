"""The 'tap Trust' prompt reaches the app live, not only after a failed wait.

`device_session.say_trust_prompt` writes the prompt to stderr, which a terminal
user sees at once, but the app reads stderr only after a non-zero exit. So a
phone that has never trusted this Mac sat behind a silent 60 s wait in the app,
with the instruction that would have ended it showing only once it had failed.
Under --json, every verb's run is wrapped so the prompt is also a `log` event
(source "device") on stdout, which the build log and the LiveContainer pane
show as it arrives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli, device_session
from graft_ipa.devices import DeviceListing

from .test_free_account_build import free_config  # noqa: F401 (the fixture)


def test_listeners_hear_the_prompt_only_while_registered(capsys: pytest.CaptureFixture[str]) -> None:
    heard: list[str] = []
    with device_session.trust_prompt_listener(heard.append):
        device_session.say_trust_prompt()
    device_session.say_trust_prompt()
    assert len(heard) == 1
    assert device_session.TRUST_PROMPT in heard[0]
    assert capsys.readouterr().err.count(device_session.TRUST_PROMPT) == 2, "stderr always gets it"


def test_a_listener_that_raises_does_not_stop_the_prompt(capsys: pytest.CaptureFixture[str]) -> None:
    def broken(_line: str) -> None:
        raise RuntimeError("listener failed")

    with device_session.trust_prompt_listener(broken):
        device_session.say_trust_prompt()
    assert device_session.TRUST_PROMPT in capsys.readouterr().err


def _prompting_listing() -> DeviceListing:
    device_session.say_trust_prompt()
    return DeviceListing(devices=[], failures=[])


def _events(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def test_under_json_the_prompt_is_a_live_device_log_event(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "list_devices", _prompting_listing)
    result = CliRunner().invoke(cli.app, ["--json", "--devices"])
    assert result.exit_code == 0, result.output
    prompts = [e for e in _events(result.stdout) if e.get("type") == "log" and e.get("source") == "device"]
    assert len(prompts) == 1, result.stdout
    assert device_session.TRUST_PROMPT in prompts[0]["line"]


def test_without_json_there_is_no_event_and_no_listener_left_behind(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "list_devices", _prompting_listing)
    result = CliRunner().invoke(cli.app, ["--devices"])
    assert result.exit_code == 0, result.output
    assert not _events(result.stdout)
    assert device_session._TRUST_LISTENERS == [], "the dispatch removes what it registered"


# --- during a build: live, and in the build's saved log (re-check #22, #40) ------


def _trust_prompted_build(free_config, monkeypatch):
    """A free-account build whose cyan stage, inside the build envelope,
    meets a phone asking for trust, as a push or install would."""
    from .test_free_account_build import LC
    from .test_lc_discovery import fake_device

    fake_device(monkeypatch, [LC])
    run_cyan = cli.run_cyan

    def cyan(*a, **k):
        device_session.say_trust_prompt()
        return run_cyan(*a, **k)

    monkeypatch.setattr(cli, "run_cyan", cyan)
    return CliRunner().invoke(cli.app, ["--json", "example"])


def _device_lines(events: list[dict]) -> list[str]:
    return [e["line"] for e in events if e.get("type") == "log" and e.get("source") == "device"]


def test_a_build_streams_the_prompt_live(free_config, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Re-check #40: only the verb dispatch was covered; dropping the
    wrapper around the build left the suite green."""
    result = _trust_prompted_build(free_config, monkeypatch)

    events = _events(result.stdout)
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True, result.output
    lines = _device_lines(events)
    assert len(lines) == 1 and device_session.TRUST_PROMPT in lines[0]


def test_the_prompt_is_in_the_build_s_saved_log(free_config, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    """Re-check #22: the live event came from the dispatch's own emitter,
    which has no tee, so a build replayed from Builds had no trace of it."""
    result = _trust_prompted_build(free_config, monkeypatch)
    assert result.exit_code == 0, result.output

    (saved,) = (free_config.state_dir / "build-logs").glob("*.jsonl")
    replayed = _events(saved.read_text())
    lines = _device_lines(replayed)
    assert len(lines) == 1 and device_session.TRUST_PROMPT in lines[0]
    # Where it happened in the build, not after its end.
    assert replayed[-1]["type"] == "build-end"
    assert device_session._TRUST_LISTENERS == [], "the build removes what it registered"


def test_a_build_that_fails_after_the_prompt_keeps_it_and_cleans_up(
    free_config, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The reviewer's case: the wait ends in a failure, and Builds replays
    the failure. The prompt that preceded it is in that replay too."""
    from .test_free_account_build import LC
    from .test_lc_discovery import fake_device

    fake_device(monkeypatch, [LC])

    def cyan(*a, **k):
        device_session.say_trust_prompt()
        raise RuntimeError("the phone never answered")

    monkeypatch.setattr(cli, "run_cyan", cyan)
    result = CliRunner().invoke(cli.app, ["--json", "example"])

    assert result.exit_code != 0
    (saved,) = (free_config.state_dir / "build-logs").glob("*.jsonl")
    replayed = _events(saved.read_text())
    assert len(_device_lines(replayed)) == 1
    assert replayed[-1]["type"] == "build-end" and replayed[-1]["ok"] is False
    assert device_session._TRUST_LISTENERS == []
