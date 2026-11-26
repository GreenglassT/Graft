"""The `--device-info` verb: `_do_device_info` / `_verb_device_info`.

`device_info.read_device_info` itself is `tests/test_device_info.py`'s
territory; everything here patches it and checks only what the CLI layer does
with the answer — emit exactly one `device-info` event, always, carrying
`reachable` (an unreachable phone is an ordinary state, not an error, and not
silence either), and resolve the target device the same way `_do_device_apps`
does.

Patched at its source module (`device_info.read_device_info`), not as
`cli_mod.read_device_info` — `cli.py` imports `device_info` lazily, inside
`_do_device_info`, the same reason `test_artwork_sources.py` patches
`pymobiledevice3.lockdown.create_using_usbmux` directly rather than via
`artwork`: a name imported only inside a function is never a module-level
attribute of the importer, so `patch.object(device_info_mod, "read_device_info")`
would raise `AttributeError`. See `_do_device_info`'s own comment for why the
import has to be lazy at all.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from graft_ipa import device_info as device_info_mod
from graft_ipa.config import load_config
from graft_ipa.device_info import DeviceInfoRecord
from graft_ipa.json_output import JsonEmitter


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_a_hit_emits_exactly_one_event_with_all_seven_keys(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    record = DeviceInfoRecord(
        udid="00008030-0000000000000001",
        total_bytes=1024000000000,
        free_bytes=910693560320,
        battery_percent=74,
        battery_charging=True,
    )
    buf = StringIO()
    with patch.object(device_info_mod, "read_device_info", return_value=record) as read:
        cli_mod._do_device_info(
            cfg, udid="00008030-0000000000000001", emitter=JsonEmitter(stream=buf)
        )

    assert read.call_args.args == ("00008030-0000000000000001",)
    events = [e for e in _events(buf.getvalue()) if e["type"] == "device-info"]
    assert len(events) == 1
    event = events[0]
    assert set(event) == {"type", "udid", "reachable", "total_bytes", "free_bytes",
                           "battery_percent", "battery_charging", "developer_mode"}
    assert event["reachable"] is True
    assert event["udid"] == "00008030-0000000000000001"
    assert event["total_bytes"] == 1024000000000
    assert event["free_bytes"] == 910693560320
    assert event["battery_percent"] == 74
    assert event["battery_charging"] is True


def test_an_unreachable_device_still_emits_an_event_saying_so(
    temp_config_env: Path,
) -> None:
    """Silence was the bug. An unreachable phone used to emit nothing at all and
    exit 0, which is the same signal as a crashed CLI, a killed process, or a
    future bug that skips the emit — while every other listing verb here ends
    with a sentinel (`devices-end`, `device-apps-end`, `preflight-end`,
    `repos-end`).

    `reachable: false` with four nulls is a shape the record already tolerated;
    what it adds is a consumer's ability to hold last-known-good across a USB
    blip on a five-second poll instead of blanking a strip that read 81% two
    seconds ago.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(device_info_mod, "read_device_info", return_value=None):
        cli_mod._do_device_info(cfg, udid="UDID", emitter=JsonEmitter(stream=buf))

    events = [e for e in _events(buf.getvalue()) if e["type"] == "device-info"]
    assert events == [
        {
            "type": "device-info",
            "udid": "UDID",
            "reachable": False,
            "total_bytes": None,
            "free_bytes": None,
            "battery_percent": None,
            "battery_charging": None,
            "developer_mode": None,
        }
    ]


def test_an_unreachable_device_also_warns_on_the_channel_the_gui_reads(
    temp_config_env: Path,
) -> None:
    """The `_warn` is pinned, not incidental.

    The previous version of this test asserted only the *absence* of a
    `device-info` event, so deleting the `_warn` call left it green — and with
    it gone the miss is silent on every channel a human has: no `log` event for
    the build panel, no stderr line for a terminal. It is deliberately not
    `carried_by="device-info"` either: suppression is only safe when something
    renders the carrier, and `device-info` is declared in `eventsHandled` ahead
    of its decoder.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(device_info_mod, "read_device_info", return_value=None):
        cli_mod._do_device_info(cfg, udid="UDID", emitter=JsonEmitter(stream=buf))

    logs = [e for e in _events(buf.getvalue()) if e["type"] == "log"]
    assert len(logs) == 1
    assert logs[0]["source"] == "warning"
    assert "UDID" in logs[0]["line"]


def test_no_device_and_no_configured_default_is_a_fatal_exit(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    assert cfg.default_device_udid is None
    with patch.object(device_info_mod, "read_device_info") as read:
        with pytest.raises(typer.Exit):
            cli_mod._do_device_info(cfg, udid=None, emitter=JsonEmitter(stream=StringIO()))
    assert not read.called, "must fail before ever trying to reach a device"


def test_the_device_flag_overrides_the_configured_default(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cfg.default_device_udid = "CONFIGURED-DEFAULT"
    record = DeviceInfoRecord(
        udid="EXPLICIT-UDID",
        total_bytes=1,
        free_bytes=1,
        battery_percent=1,
        battery_charging=False,
    )
    buf = StringIO()
    with patch.object(device_info_mod, "read_device_info", return_value=record) as read:
        cli_mod._do_device_info(cfg, udid="EXPLICIT-UDID", emitter=JsonEmitter(stream=buf))

    assert read.call_args.args == ("EXPLICIT-UDID",)
    events = [e for e in _events(buf.getvalue()) if e["type"] == "device-info"]
    assert events[0]["udid"] == "EXPLICIT-UDID"


def test_no_device_flag_falls_back_to_the_configured_default(temp_config_env: Path) -> None:
    """The other half of `udid or cfg.default_device_udid`, which nothing pinned.

    Every other test in this file either passes a udid or configures no default,
    so replacing that expression with a bare `target = udid` survived all of
    them — and the failure it would ship is `--device-info` with no `--device`
    fatally refusing on a machine that has a default configured, which is the
    ordinary way the GUI will call it.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cfg.default_device_udid = "CONFIGURED-DEFAULT"
    record = DeviceInfoRecord(
        udid="CONFIGURED-DEFAULT",
        total_bytes=2,
        free_bytes=1,
        battery_percent=50,
        battery_charging=False,
    )
    buf = StringIO()
    with patch.object(device_info_mod, "read_device_info", return_value=record) as read:
        cli_mod._do_device_info(cfg, udid=None, emitter=JsonEmitter(stream=buf))

    assert read.call_args.args == ("CONFIGURED-DEFAULT",)
    events = [e for e in _events(buf.getvalue()) if e["type"] == "device-info"]
    assert events[0]["udid"] == "CONFIGURED-DEFAULT"


@pytest.mark.parametrize("answer", [True, False, None])
def test_developer_mode_is_reported(temp_config_env: Path, answer: bool | None) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    record = DeviceInfoRecord(
        udid="UDID", total_bytes=1, free_bytes=1, battery_percent=1,
        battery_charging=False, developer_mode=answer,
    )
    buf = StringIO()
    with patch.object(device_info_mod, "read_device_info", return_value=record):
        cli_mod._do_device_info(cfg, udid="UDID", emitter=JsonEmitter(stream=buf))

    (event,) = [e for e in _events(buf.getvalue()) if e["type"] == "device-info"]
    assert event["developer_mode"] is answer


def test_a_reading_is_remembered_for_the_quick_plan(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa import devices_seen

    cfg = load_config(temp_config_env)
    record = DeviceInfoRecord(
        udid="UDID", total_bytes=1, free_bytes=1, battery_percent=1,
        battery_charging=False, developer_mode=False,
    )
    with patch.object(device_info_mod, "read_device_info", return_value=record):
        cli_mod._do_device_info(cfg, udid="UDID", emitter=JsonEmitter(stream=StringIO()))

    assert devices_seen.seen(cfg.state_dir, "UDID") == {"developer_mode": False}


def test_an_unreachable_phone_forgets_nothing(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa import devices_seen

    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, "UDID", developer_mode=False)
    with patch.object(device_info_mod, "read_device_info", return_value=None):
        cli_mod._do_device_info(cfg, udid="UDID", emitter=JsonEmitter(stream=StringIO()))

    assert devices_seen.seen(cfg.state_dir, "UDID") == {"developer_mode": False}
