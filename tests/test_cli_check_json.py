from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter
from graft_ipa.preflight import CheckResult


def test_check_json_emits_results_and_end(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake_results = [
        CheckResult(name="cyan", ok=True, detail="/opt/homebrew/bin/cyan"),
        CheckResult(name="zsign", ok=False, detail="not found on PATH", hint="brew install zsign"),
        CheckResult(name="ideviceinstaller", ok=True, detail="/opt/homebrew/bin/ideviceinstaller"),
    ]

    buf = StringIO()
    with patch.object(cli_mod, "run_all_checks", return_value=fake_results):
        cli_mod._do_check(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    checks = [e for e in events if e["type"] == "preflight-check"]
    assert len(checks) == 3
    assert checks[0]["name"] == "cyan"
    assert checks[0]["ok"] is True
    assert checks[1]["ok"] is False
    assert checks[1]["hint"] == "brew install zsign"
    end = [e for e in events if e["type"] == "preflight-end"]
    assert end == [
        {"type": "preflight-end", "ok_count": 2, "fail_count": 1, "account": "paid"}
    ]


def test_check_json_carries_action_key_for_p12_row(temp_config_env: Path) -> None:
    """The GUI matches on action_key, not the display name, so it can't drift silently."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake_results = [
        CheckResult(
            name=".p12 password",
            ok=False,
            detail="(missing)",
            hint="run `graft --set-p12-password`",
            action_key="set-p12-password",
        ),
    ]
    buf = StringIO()
    with patch.object(cli_mod, "run_all_checks", return_value=fake_results):
        cli_mod._do_check(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    check = next(e for e in events if e["type"] == "preflight-check")
    assert check["action_key"] == "set-p12-password"
    assert check["name"] == ".p12 password"


def test_real_p12_row_carries_action_key(temp_config_env: Path) -> None:
    """End-to-end: the real check_signing_files() output exposes action_key."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_check(cfg, emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    p12 = next((e for e in events if e.get("name") == ".p12 password"), None)
    assert p12 is not None, "preflight should always include the .p12 password row"
    assert p12.get("action_key") == "set-p12-password"
