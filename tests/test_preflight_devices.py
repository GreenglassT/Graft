"""Pre-flight and install find devices through pymobiledevice3, not `idevice_id`.

Everything else in Graft talks to the phone through pymobiledevice3's usbmux
client, but the device check shelled out to libimobiledevice's
`idevice_id -l -n`: a free Apple ID had to install libimobiledevice for one
check, and a CLI started with a bare PATH could never see a device at all.
Both now ask usbmuxd the way the Devices tab does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from graft_ipa import preflight, tools

PACKAGE = Path(__file__).resolve().parent.parent / "graft_ipa"


class _Mux:
    def __init__(self, serial: str, connection_type: str = "USB") -> None:
        self.serial = serial
        self.connection_type = connection_type


def _bus(*devices: _Mux):
    async def mux_list():
        return list(devices)

    return patch("pymobiledevice3.usbmux.list_devices", mux_list)


def test_lists_a_usb_device() -> None:
    with _bus(_Mux("00008130-AAAA")):
        assert preflight.list_connected_devices_with_transport() == [("00008130-AAAA", "usb")]


def test_lists_a_network_device() -> None:
    with _bus(_Mux("00008130-BBBB", "Network")):
        assert preflight.list_connected_devices_with_transport() == [("00008130-BBBB", "network")]


def test_a_device_on_both_transports_is_listed_once_as_usb() -> None:
    """USB is the more reliable way to move a large IPA, whichever usbmuxd
    happened to list first."""
    with _bus(_Mux("00008130-CCCC", "Network"), _Mux("00008130-DDDD"), _Mux("00008130-CCCC")):
        assert preflight.list_connected_devices_with_transport() == [
            ("00008130-CCCC", "usb"), ("00008130-DDDD", "usb"),
        ]


def test_the_plain_listing_is_udids_only() -> None:
    with _bus(_Mux("00008130-DDDD"), _Mux("00008130-EEEE", "Network")):
        assert preflight.list_connected_devices() == ["00008130-DDDD", "00008130-EEEE"]


def test_no_usbmuxd_is_no_devices() -> None:
    async def unreachable():
        raise ConnectionRefusedError("usbmuxd")

    with patch("pymobiledevice3.usbmux.list_devices", unreachable):
        assert preflight.list_connected_devices_with_transport() == []
        assert preflight.list_connected_devices() == []


def test_a_bare_path_still_sees_the_device() -> None:
    """No libimobiledevice, nothing on PATH, no subprocess at all."""
    def no_subprocess(*_a, **_k):
        raise AssertionError("the device check ran a subprocess")

    with patch.object(tools, "which", return_value=None), \
         patch.object(subprocess, "run", side_effect=no_subprocess), \
         _bus(_Mux("00008130-FFFF")):
        assert preflight.list_connected_devices() == ["00008130-FFFF"]


def test_nothing_in_the_package_runs_idevice_id() -> None:
    for path in PACKAGE.glob("*.py"):
        assert '"idevice_id"' not in path.read_text(), path
