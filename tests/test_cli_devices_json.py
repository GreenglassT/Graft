from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from graft_ipa.config import load_config
from graft_ipa.devices import DeviceInfo, DeviceListing, DeviceLookupFailure
from graft_ipa.json_output import JsonEmitter


def _listing(devices=(), failures=()) -> DeviceListing:
    return DeviceListing(devices=list(devices), failures=list(failures))


def test_devices_json_emits_records(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    fake_devices = [
        DeviceInfo(
            udid="00008130-001A2B3C0123456D",
            name="Test iPhone 13 Pro",
            model="iPhone14,2",
            ios_version="26.0.1",
            connection_type="USB",
            livecontainer_bundle_id="com.kdt.livecontainer.ABCDE12345",
        ),
        DeviceInfo(
            udid="00008101-000ABCDEF0123456",
            name="Spare iPad",
            model="iPad13,1",
            ios_version="17.5",
            connection_type="Network",
            livecontainer_bundle_id=None,
        ),
    ]

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=_listing(fake_devices)):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    devices = [e for e in events if e["type"] == "device"]
    assert len(devices) == 2
    assert devices[0]["udid"] == "00008130-001A2B3C0123456D"
    assert devices[0]["name"] == "Test iPhone 13 Pro"
    assert devices[0]["livecontainer_bundle_id"] == "com.kdt.livecontainer.ABCDE12345"
    assert devices[0]["connection_type"] == "USB"
    assert devices[1]["livecontainer_bundle_id"] is None
    end = [e for e in events if e["type"] == "devices-end"]
    assert end == [{"type": "devices-end", "count": 2, "failed": 0}]


def test_devices_json_emits_end_when_no_devices(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=_listing()):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert events == [{"type": "devices-end", "count": 0, "failed": 0}]


def test_devices_json_reports_a_device_that_could_not_be_described(
    temp_config_env: Path,
) -> None:
    """A dropped device has to leave a trace on the wire.

    The JSON stream is the GUI's only view of enumeration. When the sole
    connected phone fails to describe, `devices-end count=0` alone is
    indistinguishable from an unplugged cable, which is what sent users off
    re-pairing a device that was attached and trusted the whole time.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    listing = _listing(
        failures=[
            DeviceLookupFailure(
                udid="00008130-001A2B3C0123456D",
                error="ConnectionFailedError: lockdown refused",
            )
        ]
    )
    with patch.object(cli_mod, "list_devices", return_value=listing):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    errors = [e for e in events if e["type"] == "device-error"]
    assert errors == [
        {
            "type": "device-error",
            "udid": "00008130-001A2B3C0123456D",
            "error": "ConnectionFailedError: lockdown refused",
        }
    ]
    end = next(e for e in events if e["type"] == "devices-end")
    assert end["count"] == 0
    assert end["failed"] == 1


def test_devices_failure_is_warned_about_on_stderr(
    temp_config_env: Path, capsys
) -> None:
    """Also warn outside the JSON stream, for the plain-CLI user and for a GUI
    that only knows the old event vocabulary."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    listing = _listing(
        failures=[DeviceLookupFailure(udid="00008130-DEAD", error="OSError: no such device")]
    )
    with patch.object(cli_mod, "list_devices", return_value=listing):
        cli_mod._do_devices(cfg)

    err = capsys.readouterr().err
    assert "00008130-DEAD" in err
    assert "no such device" in err


def test_the_configured_default_is_marked_on_the_device_that_is_it(
    temp_config_env: Path,
) -> None:
    """The GUI needs this and had been spawning `--settings` at launch for it.

    `_do_devices` already loads `cfg` and never reads it, so the answer is
    free here — and a fact about a device belongs on that device's event
    rather than on a second verb the caller has to correlate.
    """
    from graft_ipa import cli as cli_mod

    fake = [
        DeviceInfo(
            udid="PHONE-A", name="A", model="iPhone16,1", ios_version="26.1",
            connection_type="USB", livecontainer_bundle_id=None,
        ),
        DeviceInfo(
            udid="PHONE-B", name="B", model="iPhone14,2", ios_version="16.3.1",
            connection_type="Network", livecontainer_bundle_id=None,
        ),
    ]
    cfg = load_config(temp_config_env)
    cfg.default_device_udid = "PHONE-B"
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=_listing(fake)):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    devices = [e for e in events if e["type"] == "device"]
    assert [(d["udid"], d["is_default"]) for d in devices] == [
        ("PHONE-A", False),
        ("PHONE-B", True),
    ]


def test_no_configured_default_marks_nothing(temp_config_env: Path) -> None:
    """Not "the first device wins" — a config with no default has none, and
    the GUI must be able to tell that from "this one is it"."""
    from graft_ipa import cli as cli_mod

    fake = [
        DeviceInfo(
            udid="PHONE-A", name="A", model="iPhone16,1", ios_version="26.1",
            connection_type="USB", livecontainer_bundle_id=None,
        )
    ]
    cfg = load_config(temp_config_env)
    cfg.default_device_udid = None
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=_listing(fake)):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert [e["is_default"] for e in events if e["type"] == "device"] == [False]


def test_a_default_naming_a_phone_that_is_not_attached_marks_nothing(
    temp_config_env: Path,
) -> None:
    """A stale `default_device_udid` is ordinary — the phone is just unplugged."""
    from graft_ipa import cli as cli_mod

    fake = [
        DeviceInfo(
            udid="PHONE-A", name="A", model="iPhone16,1", ios_version="26.1",
            connection_type="USB", livecontainer_bundle_id=None,
        )
    ]
    cfg = load_config(temp_config_env)
    cfg.default_device_udid = "PHONE-GONE"
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=_listing(fake)):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert [e["is_default"] for e in events if e["type"] == "device"] == [False]


def test_the_brief_listing_does_not_gain_the_field(temp_config_env: Path) -> None:
    """`--brief` is a change-detector, not a description.

    It opens no lockdown session and answers only "which UDIDs are on the
    bus". Its two-field shape is pinned by a Swift decode test, and the GUI's
    poll compares UDID sets — a default flag there would be a field nothing
    reads on a path whose whole point is being cheap.

    `_do_devices_brief` takes no `cfg` and no `brief` flag — it is a wholly
    separate function from `_do_devices`, not a branch inside it (see its own
    docstring), so there is nothing here for `cfg.default_device_udid` to
    reach even if it were set.
    """
    from graft_ipa import cli as cli_mod
    from graft_ipa.devices import BriefDevice

    buf = StringIO()
    with patch.object(
        cli_mod, "list_devices_brief",
        return_value=[BriefDevice(udid="PHONE-A", connection_type="USB")],
    ):
        cli_mod._do_devices_brief(emitter=JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    brief = [e for e in events if e["type"] == "device-brief"]
    assert brief == [
        {"type": "device-brief", "udid": "PHONE-A", "connection_type": "USB"}
    ]
