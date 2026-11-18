from __future__ import annotations

import subprocess
from unittest.mock import patch

from graft_ipa import preflight


def _fake_run(stdout: str):
    def _runner(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
    return _runner


def test_lists_usb_only() -> None:
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run("00008130-AAAA\n")):
        pairs = preflight.list_connected_devices_with_transport()
    assert pairs == [("00008130-AAAA", "usb")]


def test_lists_usb_with_explicit_usb_suffix() -> None:
    # Newer libimobiledevice appends "(USB)" to USB devices (older versions
    # printed the bare UDID). The suffix must be stripped, not baked into the id.
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run("00008130-AAAA (USB)\n")):
        pairs = preflight.list_connected_devices_with_transport()
    assert pairs == [("00008130-AAAA", "usb")]


def test_dedupes_when_both_lines_suffixed() -> None:
    output = "00008130-CCCC (Network)\n00008130-CCCC (USB)\n"
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run(output)):
        pairs = preflight.list_connected_devices_with_transport()
    assert pairs == [("00008130-CCCC", "usb")]


def test_lists_network_only() -> None:
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run("00008130-BBBB (Network)\n")):
        pairs = preflight.list_connected_devices_with_transport()
    assert pairs == [("00008130-BBBB", "network")]


def test_dedupes_to_usb_when_both() -> None:
    output = "00008130-CCCC\n00008130-CCCC (Network)\n"
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run(output)):
        pairs = preflight.list_connected_devices_with_transport()
    assert pairs == [("00008130-CCCC", "usb")]


def test_upgrades_network_to_usb_when_usb_seen_second() -> None:
    """idevice_id sometimes emits the Network line first. We should still prefer USB."""
    output = "00008130-FFFF (Network)\n00008130-FFFF\n"
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run(output)):
        pairs = preflight.list_connected_devices_with_transport()
    assert pairs == [("00008130-FFFF", "usb")]


def test_legacy_helper_returns_udids_only() -> None:
    output = "00008130-DDDD\n00008130-EEEE (Network)\n"
    with patch.object(preflight.shutil, "which", return_value="/usr/bin/idevice_id"), \
         patch.object(preflight.subprocess, "run", side_effect=_fake_run(output)):
        udids = preflight.list_connected_devices()
    assert udids == ["00008130-DDDD", "00008130-EEEE"]


def test_no_idevice_id_returns_empty() -> None:
    with patch.object(preflight.shutil, "which", return_value=None):
        assert preflight.list_connected_devices_with_transport() == []
        assert preflight.list_connected_devices() == []
