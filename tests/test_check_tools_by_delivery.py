"""A paid `--check` asks for the tools the configured apps use (R2).

`--check` on a paid config ran `run_all_checks(cfg)`, whose default delivery is
`sideload`: zsign and ideviceinstaller were required of every paid user. One
whose apps all go into LiveContainer — the setup that keeps logins working —
was shown setup step 3 and "tools missing" on every launch for two tools no
build of theirs runs. The tools now follow the configured apps' deliveries
(or, with no apps yet, the account's default, which for paid is sideload);
`--lc-install` says what it needs when it runs. The signing rows stay: a paid
config's `signing:` section is what `--lc-install` and `--lc-push-cert` sign
with too.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa import cli as cli_mod
from graft_ipa import preflight, tools
from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter


def _paid(tmp_path: Path, apps: dict) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "signing": {"cert": str(tmp_path / "c.p12"), "cert_password_env": "X",
                    "profile": str(tmp_path / "p.mobileprovision"), "team_id": "ABCDE12345"},
        "apps": apps,
    }))
    return path


LC = {"bundle_id": "com.example.lc", "delivery": "livecontainer"}
SIDE = {"bundle_id": "com.example.side", "delivery": "sideload"}
BARE = {"bundle_id": "com.example.bare"}  # no delivery key: sideload on a paid config


@pytest.mark.parametrize(("apps", "expected"), [
    ({"a": LC}, ["livecontainer"]),
    ({"a": LC, "b": SIDE}, ["sideload", "livecontainer"]),
    ({"a": BARE}, ["sideload"]),
    ({}, ["sideload"]),
])
def test_the_deliveries_in_use(tmp_path: Path, apps: dict, expected: list[str]) -> None:
    assert preflight.deliveries_in_use(load_config(_paid(tmp_path, apps))) == expected


def _check_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apps: dict) -> dict[str, dict]:
    monkeypatch.setattr(tools, "which", lambda tool: None)
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [])
    buf = StringIO()
    cli_mod._do_check(load_config(_paid(tmp_path, apps)), emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    return {e["name"]: e for e in events if e["type"] == "preflight-check"}


def test_a_livecontainer_only_paid_config_is_not_asked_for_zsign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = _check_rows(tmp_path, monkeypatch, {"a": LC})

    assert "zsign" not in rows and "ideviceinstaller" not in rows
    assert "cyan" in rows
    # Still paid: the identity --lc-install signs with is still checked.
    assert "signing.cert (.p12)" in rows and ".p12 password" in rows


@pytest.mark.parametrize("apps", [{"a": LC, "b": SIDE}, {"a": BARE}, {}])
def test_a_sideload_app_or_none_yet_still_needs_zsign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, apps: dict
) -> None:
    rows = _check_rows(tmp_path, monkeypatch, apps)

    assert {"cyan", "zsign"} <= set(rows)
    assert "ideviceinstaller" not in rows
    assert rows["zsign"]["ok"] is False and rows["zsign"]["install"]


def test_each_tool_is_listed_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "which", lambda tool: None)
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [])
    cfg = load_config(_paid(tmp_path, {"a": LC, "b": SIDE}))

    names = [r.name for r in preflight.run_all_checks(
        cfg, deliveries=preflight.deliveries_in_use(cfg))]

    assert names.count("cyan") == 1


def test_a_build_still_checks_only_its_own_delivery(tmp_path: Path, monkeypatch) -> None:
    """The per-build pre-flight is unchanged: one app, one mode."""
    monkeypatch.setattr(tools, "which", lambda tool: None)
    cfg = load_config(_paid(tmp_path, {"a": LC, "b": SIDE}))

    names = {r.name for r in preflight.run_all_checks(
        cfg, delivery="livecontainer", will_install=False, app=cfg.apps["a"])}

    assert names == {"cyan"}


def test_no_delivery_needs_otool_any_more():
    for delivery in ("livecontainer", "sideload"):
        assert "otool" not in preflight.required_tools_for(delivery)
    assert "otool" not in preflight.REQUIRED_TOOLS


def test_no_delivery_needs_libimobiledevice():
    for delivery in ("livecontainer", "sideload"):
        assert "ideviceinstaller" not in preflight.required_tools_for(delivery)
    assert "ideviceinstaller" not in preflight.REQUIRED_TOOLS
