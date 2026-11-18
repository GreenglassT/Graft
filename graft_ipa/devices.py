"""Device discovery via pymobiledevice3 (USB + network)."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

from .device_session import closing_best_effort


@dataclass
class DeviceInfo:
    udid: str
    name: str
    model: str
    ios_version: str
    connection_type: str  # "USB" | "Network"
    livecontainer_bundle_id: str | None


@dataclass
class DeviceLookupFailure:
    """A device usbmux enumerated that we then failed to describe."""

    udid: str
    error: str


@dataclass
class DeviceListing:
    devices: list[DeviceInfo]
    failures: list[DeviceLookupFailure]


def list_devices() -> DeviceListing:
    """Return all connected iOS devices with metadata, plus the ones that failed.

    Returns a listing rather than a bare list so a device that answers usbmux
    and then raises while being described cannot be dropped without the caller
    noticing. It used to be: the filter discarded it, the Devices tab said "No
    devices connected" and the picker came up empty, while the idevice_id paths
    in pre-flight and `select_device` still saw the phone — so the visible
    conclusion was a bad cable or a lost trust, and re-pairing was the fix
    people reached for.
    """
    return asyncio.run(_list_devices_async())


async def _list_devices_async() -> DeviceListing:
    from pymobiledevice3.usbmux import list_devices as mux_list

    mux_devices = await mux_list()
    seen: set[str] = set()
    unique: list[tuple[str, object]] = []
    for mux_dev in mux_devices:
        udid = _mux_udid(mux_dev)
        if not udid or udid in seen:
            continue
        seen.add(udid)
        unique.append((udid, mux_dev))

    results = await asyncio.gather(
        *(_describe_device(mux_dev, udid) for udid, mux_dev in unique),
        return_exceptions=True,
    )
    devices: list[DeviceInfo] = []
    failures: list[DeviceLookupFailure] = []
    for (udid, _mux_dev), result in zip(unique, results, strict=True):
        if isinstance(result, DeviceInfo):
            devices.append(result)
        else:
            failures.append(DeviceLookupFailure(udid=udid, error=_failure_text(result)))
    return DeviceListing(devices=devices, failures=failures)


@dataclass
class BriefDevice:
    """A device as usbmux alone can describe it.

    Deliberately not a `DeviceInfo` with empty strings. Every other field on
    that type comes from a lockdown session, and a half-populated one invites a
    caller to read a name that is not there. This type says what it knows.
    """

    udid: str
    connection_type: str


def list_devices_brief() -> list[BriefDevice]:
    """Which devices are on the bus, without opening a lockdown session.

    Measured at 0.05s in-process against 0.70-0.92s for `list_devices` with two
    phones attached — the difference is entirely the per-device lockdown
    handshake. That gap is what lets the GUI poll for *change* cheaply and pay
    for a full describe only when the set actually moves.
    """
    return asyncio.run(_list_devices_brief_async())


async def _list_devices_brief_async() -> list[BriefDevice]:
    from pymobiledevice3.usbmux import list_devices as mux_list

    seen: set[str] = set()
    out: list[BriefDevice] = []
    for mux_dev in await mux_list():
        udid = _mux_udid(mux_dev)
        if not udid or udid in seen:
            continue
        seen.add(udid)
        conn = getattr(mux_dev, "connection_type", "USB")
        out.append(
            BriefDevice(
                udid=udid,
                connection_type="Network" if str(conn).lower().startswith("network") else "USB",
            )
        )
    return out


def _mux_udid(mux_dev) -> str | None:
    return getattr(mux_dev, "serial", None) or getattr(mux_dev, "udid", None)


def _failure_text(result: object) -> str:
    if isinstance(result, BaseException):
        text = str(result).strip()
        return f"{type(result).__name__}: {text}" if text else type(result).__name__
    return repr(result)


async def _describe_device(mux_dev, udid: str) -> DeviceInfo:
    from pymobiledevice3.lockdown import create_using_usbmux

    # Deliberately unguarded: a lockdown failure here is the exact event that
    # used to make a connected phone disappear. Letting it propagate turns it
    # into a reported DeviceLookupFailure instead of a silent `return None`,
    # which `_list_devices_async` could not tell apart from "not a device".
    #
    # `closing_best_effort` is not in tension with that: it is a `finally`, so
    # a lockdown failure inside the block still propagates and still becomes a
    # `DeviceLookupFailure`. Only the teardown is tolerated — see that module
    # for what a bare close cost here, which was the disappearing-phone defect
    # this file exists to end, reintroduced one line from the end of the
    # function that fixed it.
    lockdown = await create_using_usbmux(serial=udid)
    async with closing_best_effort(lockdown):
        name = _safe_attr(lockdown, "display_name") or _safe_attr(lockdown, "device_name") or udid
        model = _safe_attr(lockdown, "product_type") or "unknown"
        ios_version = _safe_attr(lockdown, "product_version") or "unknown"

        conn_type_raw = getattr(mux_dev, "connection_type", "USB")
        connection_type = "Network" if str(conn_type_raw).lower().startswith("network") else "USB"

        lc_bundle = await _detect_livecontainer_async(lockdown)
        return DeviceInfo(
            udid=udid,
            name=str(name),
            model=str(model),
            ios_version=str(ios_version),
            connection_type=connection_type,
            livecontainer_bundle_id=lc_bundle,
        )


def _safe_attr(obj, attr: str):
    try:
        return getattr(obj, attr)
    except Exception:
        return None


_LC_PREFIX = "com.kdt.livecontainer"


async def _detect_livecontainer_async(lockdown) -> str | None:
    """Find the installed LiveContainer bundle id (handles team-id suffixes).

    Asks InstallationProxy for *only* the CFBundleIdentifier attribute so we
    skip downloading every installed app's icon and entitlement plist.
    """
    try:
        from pymobiledevice3.services.installation_proxy import InstallationProxyService
        proxy = InstallationProxyService(lockdown=lockdown)
    except Exception:
        return None
    # This service opens its own connection, so `_describe_device` closing the
    # lockdown session leaves this one live — the other half of that leak, and
    # not a consequence of it. Measured under `-X dev` against the test phone,
    # one `list_devices()` call: five unclosed resources before either fix,
    # three with the lockdown closed alone, none with both.
    async with closing_best_effort(proxy):
        try:
            call = proxy.browse(attributes=["CFBundleIdentifier"])
            apps = await call if inspect.isawaitable(call) else call
        except Exception:
            apps = None
    for entry in apps or []:
        bid = entry.get("CFBundleIdentifier") if isinstance(entry, dict) else None
        if bid and bid.startswith(_LC_PREFIX):
            return bid
    return None
