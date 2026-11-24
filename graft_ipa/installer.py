"""Install IPAs on a device (USB or Wi-Fi) through `device_install`."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import device_install
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
    """Install `ipa` on `udid` (USB or Wi-Fi; usbmux reaches both).

    `transport` is accepted for the callers' sake and no longer changes
    anything: there is no `--network` switch to pass.
    """
    if dry_run:
        return InstallResult(
            udid=udid, stdout=f"would install {ipa.name} on {udid or 'the default device'}", stderr="",
        )
    if udid is None:
        udid, _ = select_device(None)
    try:
        device_install.install_ipa(ipa, udid=udid)
    except device_install.DeviceInstallError as e:
        raise InstallError("installing on the device failed", hint=_install_hint(e.output)) from e
    return InstallResult(udid=udid, stdout="Install: Complete", stderr="")


def connection_hint(output: str) -> str | None:
    """What to do about a failure to reach or pair with the phone, named in
    `output` by its pymobiledevice3 exception (`<ExceptionName>: …`), or None
    when `output` names none of them.

    Matched by name rather than by class: `output` is text by the time it
    gets here (`DeviceInstallError`, or an error's message), and the names are
    the same in every pymobiledevice3 release graft supports.
    """
    if "PasswordRequiredError" in output:
        return "the device is locked or hasn't trusted this Mac — unlock it, tap Trust if asked, and re-run."
    # Before the generic pairing case: both names end in "PairingError", or
    # nearly, and each has a remedy of its own.
    if "PairingDialogResponsePendingError" in output:
        return (
            "the phone is still asking whether to trust this Mac — unlock it, tap Trust "
            "on the 'Trust This Computer?' prompt, and re-run."
        )
    if "UserDeniedPairingError" in output:
        return (
            "the phone was told not to trust this Mac — unplug it and plug it back in, "
            "then re-run and tap Trust on the prompt."
        )
    if "NotPairedError" in output or "PairingError" in output:
        return "this Mac isn't trusted by the device — unlock it, tap Trust on the prompt, and re-run."
    if "DeviceNotFoundError" in output or "NoDeviceConnectedError" in output:
        return "the device is not connected — plug it in by USB (or keep it on the same Wi-Fi) and re-run."
    # The phone went away mid-transfer: a cable pulled, a Wi-Fi-synced phone
    # asleep or off the network. pymobiledevice3 raises the first; a socket
    # that dies under asyncio can surface as the bare OSErrors.
    if any(name in output for name in (
        "ConnectionTerminatedError", "ConnectionResetError", "ConnectionAbortedError",
        "BrokenPipeError",
    )):
        return (
            "the connection to the phone dropped — check the cable (or that the phone is "
            "awake and on the same Wi-Fi), keep it unlocked, and re-run."
        )
    if "PhoneWentSilentError" in output:
        return (
            "the phone stopped answering mid-transfer with the connection still open, "
            "which is how a locked phone or a Wi-Fi-synced one gone to sleep looks — "
            "wake and unlock it (USB is steadier than Wi-Fi), and re-run; anything left "
            "staged on the phone is removed by the next install."
        )
    if "ConnectionFailedToUsbmuxdError" in output:
        return (
            "macOS's usbmuxd, which every connection to an iPhone goes through, didn't "
            "answer — unplug the phone and plug it back in, and re-run; if it persists, "
            "restart the Mac."
        )
    if "ConnectionFailedError" in output:
        return (
            "the phone refused the connection, which is how a Wi-Fi-synced phone that "
            "has gone to sleep answers — wake and unlock it (or plug it in by USB), and re-run."
        )
    if "StartServiceError" in output:
        return (
            "the phone wouldn't start the service the install needs — unlock it and "
            "re-run; if it persists, restart the phone."
        )
    return None


def _install_hint(output: str) -> str:
    connection = connection_hint(output)
    if connection is not None:
        return connection
    # Before the generic codes below: this reason may arrive wrapped in one.
    if "InvalidCompanionAppBundleIdentifier" in output:
        return (
            "InvalidCompanionAppBundleIdentifier — the IPA has an Apple Watch app that "
            "still names the old bundle id. Keep the bundle id, or let Graft remove the "
            "Watch app (it does when it renames)."
        )
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
    if "PackageInspectionFailed" in output:
        return "PackageInspectionFailed — the IPA is malformed. Try re-signing."
    if _names_no_reason(output, "AppInstallError"):
        # The install stream ended without "Complete" and without a reason.
        return (
            "the install stopped without the phone saying why — keep it unlocked and "
            "connected, and re-run."
        )
    return output.strip() or "re-run with --verbose for full output"


def _names_no_reason(output: str, name: str) -> bool:
    """`output` is `name` alone, or with only pymobiledevice3's stock text for it
    (11.x fills an empty message from the class docstring; 10.x leaves it empty)."""
    head, _, rest = output.strip().partition(":")
    return head == name and rest.strip() in ("", "Installing, upgrading or removing the app failed.")
