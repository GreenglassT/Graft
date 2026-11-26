"""`--devices` and `--device-info` remember what each device said
(`devices-seen.json`), and `--devices` names the remembered ones it didn't
describe (`device-seen`), the configured default always among them: how the
app names a device that isn't connected (spec §5b)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import devices_seen
from graft_ipa.config import load_config
from graft_ipa.devices import DeviceInfo, DeviceListing
from graft_ipa.json_output import JsonEmitter

PHONE = "00008030-0000000000000001"
IPAD = "00008030-0000000000000002"
SPARE = "00008030-0000000000000003"


def _ipad(**fields) -> DeviceInfo:
    described = dict(udid=IPAD, name="iPad Air", model="iPad13,16", ios_version="26.1",
                     connection_type="USB", livecontainer_bundle_id=None)
    described.update(fields)
    return DeviceInfo(**described)


def _devices(cfg, *described: DeviceInfo) -> list[dict]:
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=DeviceListing(list(described), [])):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def test_a_described_device_is_remembered(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)

    _devices(cfg, _ipad())

    assert devices_seen.seen(cfg.state_dir, IPAD) == {
        "name": "iPad Air", "model": "iPad13,16", "ios_version": "26.1",
    }


def test_what_the_phone_did_not_say_is_not_remembered(temp_config_env: Path) -> None:
    """`_describe_device` puts the UDID in for a name and "unknown" for the
    rest when the phone doesn't say: neither is worth keeping."""
    cfg = load_config(temp_config_env)

    _devices(cfg, _ipad(name=IPAD, model="unknown", ios_version="unknown"))

    assert devices_seen.seen(cfg.state_dir, IPAD) == {}


def test_an_absent_default_is_named_from_memory(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    cfg.default_device_udid = PHONE
    devices_seen.remember(cfg.state_dir, PHONE, name="iPhone 15 Pro", model="iPhone16,1",
                          ios_version="26.1")

    events = _devices(cfg, _ipad())

    assert [e["type"] for e in events] == ["device", "device-seen", "devices-end"]
    assert events[1] == {
        "type": "device-seen", "udid": PHONE, "name": "iPhone 15 Pro", "model": "iPhone16,1",
        "ios_version": "26.1", "is_default": True,
    }


def test_an_absent_default_never_seen_is_still_said(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    cfg.default_device_udid = PHONE

    (seen,) = [e for e in _devices(cfg, _ipad()) if e["type"] == "device-seen"]

    assert seen == {"type": "device-seen", "udid": PHONE, "name": None, "model": None,
                    "ios_version": None, "is_default": True}


def test_other_remembered_devices_are_said_too(temp_config_env: Path) -> None:
    """Another device Graft delivered to, unplugged, is named too."""
    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, SPARE, name="Spare iPad")
    _delivered_to(cfg, SPARE)

    (seen,) = [e for e in _devices(cfg, _ipad()) if e["type"] == "device-seen"]

    assert (seen["udid"], seen["name"], seen["is_default"]) == (SPARE, "Spare iPad", False)


def test_a_connected_device_is_not_also_seen(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, IPAD, name="iPad Air")

    assert [e for e in _devices(cfg, _ipad()) if e["type"] == "device-seen"] == []


def test_a_state_folder_that_wont_take_writes_costs_the_names_not_the_listing(
    temp_config_env: Path,
) -> None:
    cfg = load_config(temp_config_env)
    cfg.state_dir = temp_config_env.parent / "not-a-folder"
    cfg.state_dir.write_text("a file where the state folder should be")

    events = _devices(cfg, _ipad())

    assert [e["type"] for e in events] == ["device", "devices-end"]


def test_remembering_that_fails_costs_the_names_not_the_listing(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever goes wrong keeping the names, `--devices` still lists the
    phones: the names are a convenience, the listing is the command."""
    cfg = load_config(temp_config_env)
    cfg.default_device_udid = PHONE

    def broken(*args, **kwargs) -> None:
        raise RuntimeError("devices-seen.json couldn't be kept")

    monkeypatch.setattr(devices_seen, "remember", broken)

    events = _devices(cfg, _ipad())

    assert [e["type"] for e in events] == ["device", "devices-end"]


def test_device_info_remembers_what_the_device_is_called(temp_config_env: Path) -> None:
    from graft_ipa import device_info
    from graft_ipa.device_info import DeviceInfoRecord

    cfg = load_config(temp_config_env)
    record = DeviceInfoRecord(udid=IPAD, total_bytes=None, free_bytes=None,
                              battery_percent=None, battery_charging=None,
                              name="iPad Air", model="iPad13,16", ios_version="26.1")
    with patch.object(device_info, "read_device_info", return_value=record):
        cli_mod._do_device_info(cfg, udid=IPAD, emitter=JsonEmitter(stream=StringIO()))

    assert devices_seen.seen(cfg.state_dir, IPAD) == {
        "name": "iPad Air", "model": "iPad13,16", "ios_version": "26.1",
    }


def _developer_mode_off(monkeypatch: pytest.MonkeyPatch, *, name: str | None = None,
                        model_name: str | None = None) -> None:
    """The phone says Developer Mode is off, naming itself `name` (the
    session's name) and `model_name` (its model or family), or not at all."""
    from graft_ipa import device_info

    monkeypatch.setattr(device_info, "check_developer_mode",
                        lambda udid, *, reveal: device_info.DeveloperModeCheck(
                            on=False, name=name, model_name=model_name))


def test_the_developer_mode_refusal_names_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa import developer_mode
    from graft_ipa.errors import DeveloperModeOffError

    devices_seen.remember(tmp_path, IPAD, name="iPad Air")
    _developer_mode_off(monkeypatch)

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, IPAD)

    assert caught.value.message == "Developer Mode is off on iPad Air"


def test_the_hint_names_the_model_the_session_gave_and_never_a_remembered_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session's model or family ("your iPad"). With none, the plain hint,
    not the last-known name, which may be the one the device gives itself
    ("your Sample's iPad")."""
    from graft_ipa import developer_mode
    from graft_ipa.errors import DeveloperModeOffError

    devices_seen.remember(tmp_path, IPAD, name="Sample's iPad")
    _developer_mode_off(monkeypatch)

    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, IPAD)

    assert caught.value.hint == developer_mode.TURN_ON

    _developer_mode_off(monkeypatch, name="Sample's iPad", model_name="iPad")
    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, IPAD)

    assert caught.value.hint == developer_mode.turn_on("iPad")


# --- what is remembered stays bounded ---------------------------------------------


def _delivered_to(cfg, udid: str) -> None:
    from graft_ipa.build_log import BuildLog

    BuildLog(cfg.state_dir / "log.sqlite").record_delivery(
        app="example", bundle_id="com.example.app", inputs_digest=None, digest_version=None,
        cyan=None, tweaks=[], source_version="1.0", ipa_sha256="0" * 64,
        delivery="livecontainer", lc_bundle_id="com.kdt.livecontainer", remote_name="example.ipa",
        udid=udid,
    )


def test_a_device_graft_has_no_reason_to_name_is_forgotten(temp_config_env: Path) -> None:
    """Kept: what's connected (locked ones too), the default, and devices
    the delivery log records a delivery to. A device merely seen once is
    forgotten at the next listing it isn't in."""
    from graft_ipa.devices import DeviceLookupFailure

    cfg = load_config(temp_config_env)
    cfg.default_device_udid = PHONE
    passer_by, delivered, locked = ("00008030-0000000000000004", "00008030-0000000000000005",
                                    "00008030-0000000000000006")
    for udid in (PHONE, passer_by, delivered, locked):
        devices_seen.remember(cfg.state_dir, udid, name=f"device {udid[-1]}")
    _delivered_to(cfg, delivered)

    buf = StringIO()
    listing = DeviceListing([_ipad()], [DeviceLookupFailure(udid=locked, error="locked")])
    with patch.object(cli_mod, "list_devices", return_value=listing):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))

    assert set(devices_seen.load(cfg.state_dir)) == {IPAD, PHONE, delivered, locked}
    seen = [json.loads(line) for line in buf.getvalue().splitlines()
            if json.loads(line)["type"] == "device-seen"]
    assert passer_by not in {e["udid"] for e in seen}


def test_an_unreadable_delivery_log_forgets_nothing(temp_config_env: Path) -> None:
    cfg = load_config(temp_config_env)
    devices_seen.remember(cfg.state_dir, SPARE, name="Spare iPad")
    (cfg.state_dir / "log.sqlite").write_bytes(b"not a database")

    _devices(cfg, _ipad())

    assert SPARE in devices_seen.load(cfg.state_dir)


def test_the_refusal_remembers_what_the_session_called_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A device never listed is named by its session, and remembered, so the
    refusal and later sentences name it too. A name already remembered stays:
    a session that gave only the device's class ("iPad") mustn't replace it."""
    from graft_ipa import developer_mode
    from graft_ipa.errors import DeveloperModeOffError

    _developer_mode_off(monkeypatch, name="iPad Air")
    with pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, IPAD)

    assert caught.value.message == "Developer Mode is off on iPad Air"
    assert devices_seen.name_of(tmp_path, IPAD) == "iPad Air"

    _developer_mode_off(monkeypatch, name="iPad")
    with pytest.raises(DeveloperModeOffError):
        developer_mode.require_on(tmp_path, IPAD)

    assert devices_seen.name_of(tmp_path, IPAD) == "iPad Air"
