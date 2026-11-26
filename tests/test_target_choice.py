"""Which device a build goes to (spec §5b's table), when nothing was picked
for it: the default when it's connected; else the only device connected;
else the person picks. A pick (`--device`) always wins, and must be
connected. Asked of usbmux alone; names come from `devices-seen.json`."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import devices_seen, installer
from graft_ipa.config import Config
from graft_ipa.errors import (
    DeviceNotConnectedError,
    InstallError,
    NoDeviceConnectedError,
    SeveralDevicesError,
)
from graft_ipa.installer import Target

PHONE = "00008030-0000000000000001"
IPAD = "00008030-0000000000000002"
SPARE = "00008030-0000000000000003"


def _cfg(tmp_path: Path, default: str | None) -> Config:
    return Config(
        path=tmp_path / "config.yaml", signing=None, cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built", tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state", apps={}, default_device_udid=default,
    )


@pytest.fixture
def bench(monkeypatch: pytest.MonkeyPatch):
    """Say what usbmux lists: `bench(PHONE, IPAD)`, each over USB unless given
    as `(udid, "network")`."""

    def connect(*devices: str | tuple[str, str]) -> None:
        listed = [d if isinstance(d, tuple) else (d, "usb") for d in devices]
        monkeypatch.setattr(installer, "list_connected_devices_with_transport", lambda: listed)

    return connect


# --- the table -----------------------------------------------------------------


@pytest.mark.parametrize("connected", [[PHONE], [PHONE, IPAD], [IPAD, SPARE, PHONE]])
def test_the_default_when_it_is_connected(tmp_path: Path, bench, connected: list[str]) -> None:
    bench(*connected)

    assert installer.resolve_target(_cfg(tmp_path, PHONE), None) == Target(PHONE, None, "usb")


def test_the_only_other_device_when_the_default_is_not_connected(tmp_path: Path, bench) -> None:
    cfg = _cfg(tmp_path, PHONE)
    devices_seen.remember(cfg.state_dir, PHONE, name="iPhone 15 Pro")
    devices_seen.remember(cfg.state_dir, IPAD, name="iPad Air")
    bench(IPAD)

    target = installer.resolve_target(cfg, None)

    assert target == Target(
        IPAD, "iPad Air", "usb",
        "iPhone 15 Pro, the default, isn't connected: installing to iPad Air",
    )
    assert target.label == "iPad Air"


def test_devices_never_seen_are_named_by_their_udid(tmp_path: Path, bench) -> None:
    bench(IPAD)

    target = installer.resolve_target(_cfg(tmp_path, PHONE), None)

    assert target.reason == f"{PHONE}, the default, isn't connected: installing to {IPAD}"
    assert target.label == IPAD


def test_several_others_and_the_default_missing_asks(tmp_path: Path, bench) -> None:
    cfg = _cfg(tmp_path, PHONE)
    devices_seen.remember(cfg.state_dir, PHONE, name="iPhone 15 Pro")
    bench(IPAD, SPARE)

    with pytest.raises(SeveralDevicesError) as caught:
        installer.resolve_target(cfg, None)

    assert caught.value.message == installer.SEVERAL_CONNECTED
    assert installer.SEVERAL_CONNECTED == "several devices are connected: pick one"
    assert caught.value.hint.startswith("iPhone 15 Pro, the default, isn't connected.")
    assert "--device <UDID>" in caught.value.hint


def test_nothing_connected(tmp_path: Path, bench) -> None:
    bench()

    with pytest.raises(NoDeviceConnectedError) as caught:
        installer.resolve_target(_cfg(tmp_path, PHONE), None)

    assert caught.value.message == installer.NONE_CONNECTED == "no iOS devices connected"


def test_no_default_and_one_device_is_that_device(tmp_path: Path, bench) -> None:
    bench(IPAD)

    assert installer.resolve_target(_cfg(tmp_path, None), None) == Target(IPAD, None, "usb")


def test_no_default_and_several_asks(tmp_path: Path, bench) -> None:
    bench(IPAD, SPARE)

    with pytest.raises(SeveralDevicesError) as caught:
        installer.resolve_target(_cfg(tmp_path, None), None)

    assert caught.value.hint.startswith("no default device is set.")


@pytest.mark.parametrize("default", [PHONE, None])
def test_an_explicit_device_wins(tmp_path: Path, bench, default: str | None) -> None:
    bench(PHONE, (IPAD, "network"))

    assert installer.resolve_target(
        _cfg(tmp_path, default), None, explicit_udid=IPAD
    ) == Target(IPAD, None, "network")


def test_an_explicit_device_that_is_not_connected_still_fails(tmp_path: Path, bench) -> None:
    """Its own type (spec §11.6), for `--update` to say "<label> isn't
    connected"; the message is the one the app's bar quotes."""
    bench(PHONE, IPAD)

    with pytest.raises(DeviceNotConnectedError) as caught:
        installer.resolve_target(_cfg(tmp_path, PHONE), None, explicit_udid=SPARE)

    assert caught.value.message == f"requested device {SPARE} not connected"
    assert (caught.value.udid, caught.value.label) == (SPARE, SPARE), "never seen: its UDID"
    assert isinstance(caught.value, InstallError)
    assert not isinstance(caught.value, SeveralDevicesError | NoDeviceConnectedError)


def test_a_device_that_is_not_connected_is_named_as_last_seen(tmp_path: Path, bench) -> None:
    cfg = _cfg(tmp_path, PHONE)
    devices_seen.remember(cfg.state_dir, SPARE, name="Spare iPad")
    bench(PHONE)

    with pytest.raises(DeviceNotConnectedError) as caught:
        installer.resolve_target(cfg, None, explicit_udid=SPARE)

    assert (caught.value.udid, caught.value.label) == (SPARE, "Spare iPad")
    assert caught.value.message == f"requested device {SPARE} not connected"


def test_several_devices_has_an_exit_code_of_its_own() -> None:
    """Not 8, "installing failed": nothing was installed, and nothing failed
    but the choice of device."""
    from graft_ipa import errors

    assert SeveralDevicesError("x").exit_code == 22 != InstallError("x").exit_code
    assert "SeveralDevicesError" in errors.EXIT_CODES[22]
    assert NoDeviceConnectedError("x").exit_code == DeviceNotConnectedError(SPARE).exit_code == 8


def test_an_explicit_device_with_nothing_connected(tmp_path: Path, bench) -> None:
    bench()

    with pytest.raises(NoDeviceConnectedError):
        installer.resolve_target(_cfg(tmp_path, None), None, explicit_udid=SPARE)


def test_it_asks_usbmux_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No lockdown session: cheap enough for `--update --plan --quick` to ask."""
    import pymobiledevice3.lockdown as lockdown_mod

    from graft_ipa import devices
    from graft_ipa.devices import BriefDevice

    async def no_session(*args, **kwargs):
        raise AssertionError("opened a lockdown session")

    from graft_ipa import device_info

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", no_session)
    monkeypatch.setattr(device_info, "create_using_usbmux", no_session)
    monkeypatch.setattr(devices, "list_devices_brief",
                        lambda: [BriefDevice(udid=IPAD, connection_type="Network")])

    assert installer.resolve_target(_cfg(tmp_path, PHONE), None).transport == "network"


def test_select_device_keeps_the_rule_from_before(bench) -> None:
    """Re-install, `--reconcile` and a dump's lock name still take the first
    connected device, or exactly the one named."""
    bench(IPAD, SPARE)

    assert installer.select_device(None) == (IPAD, "usb")
    assert installer.select_device(SPARE) == (SPARE, "usb")
    with pytest.raises(DeviceNotConnectedError, match=f"requested device {PHONE} not connected"):
        installer.select_device(PHONE)


def test_select_device_with_nothing_connected(bench) -> None:
    bench()

    with pytest.raises(NoDeviceConnectedError, match="no iOS devices connected"):
        installer.select_device(None)


# --- the build -----------------------------------------------------------------

REASON = "iPhone 15 Pro, the default, isn't connected: installing to iPad Air"


def _build(tmp_path: Path, capsys, resolving, *, device=None, skip_install=False):
    """A LiveContainer build with everything but the target stubbed, as
    `test_cli_build_json` builds one. Returns the config, the events, the
    acquire and lc_runner stand-ins, and the InstallError it raised, if any."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.build_log import BuildLog
    from graft_ipa.json_output import JsonEmitter

    from .test_cli_build_json import _stub_config

    cfg = _stub_config(tmp_path)
    error: InstallError | None = None
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "resolve_target", resolving), \
         patch.object(cli_mod, "lc_runner") as lc:
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.output_ipa.write_bytes(b"x")
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        pushed = lc.push_ipa_to_livecontainer.return_value
        pushed.size_bytes, pushed.bundle_id = 1024, "com.kdt.livecontainer"
        pushed.documents_path, pushed.others = "Documents/example.ipa", []
        try:
            cli_mod._build_one(
                cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                dry_run=False, skip_install=skip_install, skip_decrypt_check=False,
                verbose=False, device=device, emitter=JsonEmitter(),
            )
        except InstallError as e:
            error = e
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    return cfg, events, acquire, lc, error


def test_the_build_installs_where_the_table_says_and_says_why(tmp_path: Path, capsys) -> None:
    resolving = MagicMock(return_value=Target(IPAD, "iPad Air", "usb", REASON))

    cfg, events, _, lc, error = _build(tmp_path, capsys, resolving)

    assert error is None
    resolving.assert_called_once_with(cfg, cfg.apps["example"], explicit_udid=None)
    said = events.index({"type": "log", "source": "graft", "line": REASON})
    first_stage = next(i for i, e in enumerate(events) if e["type"] == "stage")
    assert said < first_stage, "said when the target is chosen, not minutes later at stage 5"
    assert lc.check_push_target.call_args.args[0] == IPAD, "the early check asks the same device"
    assert lc.push_ipa_to_livecontainer.call_args.kwargs["udid"] == IPAD


def test_the_default_needs_no_word(tmp_path: Path, capsys) -> None:
    _, events, _, _, _ = _build(
        tmp_path, capsys, MagicMock(return_value=Target(PHONE, "iPhone 15 Pro", "usb")),
    )

    assert not [e for e in events if e.get("source") == "graft"]


def test_a_pick_is_handed_to_the_table(tmp_path: Path, capsys) -> None:
    resolving = MagicMock(return_value=Target(IPAD, None, "usb"))

    cfg, _, _, _, _ = _build(tmp_path, capsys, resolving, device=IPAD)

    resolving.assert_called_once_with(cfg, cfg.apps["example"], explicit_udid=IPAD)


def test_several_devices_stop_the_build_before_it_starts(tmp_path: Path, capsys) -> None:
    resolving = MagicMock(side_effect=SeveralDevicesError(installer.SEVERAL_CONNECTED, hint="pick"))

    _, events, acquire, _, error = _build(tmp_path, capsys, resolving)

    assert isinstance(error, SeveralDevicesError)
    acquire.assert_not_called()
    (end,) = [e for e in events if e["type"] == "build-end"]
    assert "several devices are connected: pick one" in end["error"]


@pytest.mark.parametrize("missing", [
    NoDeviceConnectedError(installer.NONE_CONNECTED),
    DeviceNotConnectedError(IPAD),
], ids=["nothing connected", "the named device not connected"])
def test_no_target_stops_the_build_before_it_starts(tmp_path: Path, capsys, missing) -> None:
    """One resolution per install: a target missing now stops the build here,
    so Plan B's Developer Mode check and stage 4's profile always mean the
    device stage 5 installs on. A `--device` plugged in while cyan ran used
    to reach stage 5 having skipped both."""
    resolving = MagicMock(side_effect=missing)

    _, events, acquire, lc, error = _build(tmp_path, capsys, resolving, device=IPAD)

    assert error is missing
    acquire.assert_not_called()
    resolving.assert_called_once()
    lc.push_ipa_to_livecontainer.assert_not_called()
    (end,) = [e for e in events if e["type"] == "build-end"]
    assert missing.message in end["error"]


def test_the_install_never_resolves_again(tmp_path: Path, capsys) -> None:
    resolving = MagicMock(return_value=Target(IPAD, "iPad Air", "usb"))

    _build(tmp_path, capsys, resolving)

    resolving.assert_called_once()


def test_a_skipped_install_asks_nothing(tmp_path: Path, capsys) -> None:
    resolving = MagicMock(side_effect=AssertionError("resolved a device"))

    _, _, _, _, error = _build(tmp_path, capsys, resolving, skip_install=True)

    assert error is None
    resolving.assert_not_called()
