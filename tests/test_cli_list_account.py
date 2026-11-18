"""`--list --json` says which account the config is for, even with no apps.

The Mac app runs `--list` at launch and on every refresh. The `account` on each
`app` event cannot answer for a config with no apps, which is every fresh
install, so a free Apple ID's Add App sheet would have offered sideload. The
`account` event is `--list`'s first line, once, whatever the app count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from graft_ipa import cli as cli_mod
from graft_ipa.config import load_config


def _events(capsys: pytest.CaptureFixture[str]) -> list[dict]:
    return [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]


def _without_signing(path: Path) -> None:
    raw = yaml.safe_load(path.read_text())
    del raw["signing"]
    path.write_text(yaml.safe_dump(raw))


def _without_apps(path: Path) -> None:
    raw = yaml.safe_load(path.read_text())
    raw["apps"] = {}
    path.write_text(yaml.safe_dump(raw))


@pytest.mark.parametrize(("free", "account"), [(True, "free"), (False, "paid")])
def test_a_config_with_no_apps_still_reports_its_account(
    free: bool, account: str, temp_config_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if free:
        _without_signing(temp_config_env)
    _without_apps(temp_config_env)

    cli_mod._verb_list_apps(cfg=load_config(temp_config_env), json_mode=True)

    assert _events(capsys) == [{"type": "account", "account": account}]


@pytest.mark.parametrize(("free", "account"), [(True, "free"), (False, "paid")])
def test_the_account_comes_first_and_once(
    free: bool, account: str, temp_config_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if free:
        _without_signing(temp_config_env)

    cli_mod._verb_list_apps(cfg=load_config(temp_config_env), json_mode=True)

    events = _events(capsys)
    assert events[0] == {"type": "account", "account": account}
    assert [e["type"] for e in events].count("account") == 1
    assert any(e["type"] == "app" for e in events[1:])

