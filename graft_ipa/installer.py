"""Wrap ideviceinstaller for IPA installation (USB or Wi-Fi)."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .errors import InstallError
from .preflight import list_connected_devices_with_transport


@dataclass
class InstallResult:
    udid: str | None
    stdout: str
    stderr: str


def select_device(preferred_udid: str | None) -> tuple[str | None, str]:
    """Pick a device to target. Returns (udid, transport) where transport ∈ {"usb","network"}.

    When `preferred_udid` is set, that exact UDID must be connected.
    Otherwise the first connected device is chosen.
    """
    devices = list_connected_devices_with_transport()
    if not devices:
        raise InstallError(
            "no iOS devices connected",
            hint="plug the iPhone in via USB and tap 'Trust this Computer', or enable Wi-Fi sync in Finder",
        )
    if preferred_udid:
        for udid, transport in devices:
            if udid == preferred_udid:
                return udid, transport
        connected = ", ".join(u for u, _ in devices)
        raise InstallError(
            f"requested device {preferred_udid} not connected",
            hint=f"connected devices: {connected}",
        )
    udid, transport = devices[0]
    return udid, transport


def install(
    ipa: Path,
    udid: str | None = None,
    transport: str = "usb",
    *,
    dry_run: bool = False,
) -> InstallResult:
    if shutil.which("ideviceinstaller") is None:
        raise InstallError(
            "ideviceinstaller not found in PATH",
            hint="brew install ideviceinstaller",
        )

    cmd = ["ideviceinstaller"]
    if udid:
        cmd += ["-u", udid]
        if transport == "network":
            cmd += ["--network"]
    cmd += ["install", str(ipa)]

    if dry_run:
        return InstallResult(udid=udid, stdout=" ".join(cmd), stderr="")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise InstallError("ideviceinstaller not found in PATH") from e

    combined = (proc.stdout or "") + (proc.stderr or "")
    ok = proc.returncode == 0 and (
        "Install: Complete" in combined or ("Installing" in combined and "ERROR" not in combined)
    )
    if not ok:
        raise InstallError(
            f"ideviceinstaller exited with code {proc.returncode}",
            hint=_install_hint(combined),
        )

    return InstallResult(udid=udid, stdout=proc.stdout, stderr=proc.stderr)


def _install_hint(output: str) -> str:
    if "ApplicationVerificationFailed" in output:
        return (
            "ApplicationVerificationFailed — the device couldn't verify the signature. "
            "Common causes: the .p12 and .mobileprovision are from different teams, "
            "or the profile doesn't include this device's UDID."
        )
    if "MismatchedApplicationIdentifierEntitlement" in output:
        return (
            "MismatchedApplicationIdentifierEntitlement — your provisioning profile "
            "doesn't allow this bundle id. Use a wildcard profile or a per-app profile "
            "for this bundle id."
        )
    if "DeviceLocked" in output:
        return "the iPhone is locked — unlock it and re-run."
    if "Could not connect to lockdownd" in output:
        return (
            "lockdownd connection refused — disconnect/reconnect the cable, ensure the "
            "device is paired (idevicepair pair), and retry."
        )
    if "PackageInspectionFailed" in output:
        return "PackageInspectionFailed — the IPA is malformed. Try re-signing."
    return output.strip() or "re-run with --verbose for full output"
