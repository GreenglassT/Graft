"""Device discovery via pymobiledevice3 (USB + network)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .device_session import PAIR_TIMEOUT_SECONDS, closing_best_effort


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
    devices connected" and the picker came up empty, while pre-flight and
    `select_device` (then on libimobiledevice's `idevice_id`, now on
    `list_devices_brief`) still saw the phone — so the visible conclusion was a
    bad cable or a lost trust, and re-pairing was the fix people reached for.
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

    # One entry per phone, where usbmuxd first listed it — but USB if it is
    # on both transports, whichever came first: USB is the reliable way to
    # move a large IPA, and `select_device` hands this transport to the
    # install.
    by_udid: dict[str, BriefDevice] = {}
    for mux_dev in await mux_list():
        udid = _mux_udid(mux_dev)
        if not udid:
            continue
        conn = getattr(mux_dev, "connection_type", "USB")
        kind = "Network" if str(conn).lower().startswith("network") else "USB"
        if udid not in by_udid:
            by_udid[udid] = BriefDevice(udid=udid, connection_type=kind)
        elif kind == "USB":
            by_udid[udid].connection_type = "USB"
    return list(by_udid.values())


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
    lockdown = await create_using_usbmux(serial=udid, pair_timeout=PAIR_TIMEOUT_SECONDS)
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


async def _detect_livecontainer_async(lockdown) -> str | None:
    """The LiveContainer a push to this device would use, or None for none.

    Asked of `lc_discovery`, the one discovery the push, `--lc-status` and
    `--check` share: every application type (TrollStore and a jailbreak
    register LiveContainer as a System app), bundle ids only, matched by
    pattern in any case, and `choose`'s pick when there are several. This had
    its own case-sensitive prefix over User apps, first listed wins: a phone
    with only `com.kdt.LiveContainer2.<TEAM>` showed "no LC" in the sidebar,
    and with two installed it named whichever the phone listed first.

    Unpinned, since a device belongs to no app. Where `choose` needs a pin to
    decide (two primaries), the answer is the first one its refusal names:
    the sidebar's question is whether there is a LiveContainer, and the push
    says for itself which pin it needs.

    Every failure, the listing's and the close's, reads as "not installed":
    the device is described either way.
    """
    from . import lc_discovery
    from .lc_runner import LiveContainerError

    # `installed_bundle_ids` closes the InstallationProxy service it opens,
    # which `_describe_device` closing the lockdown session does not: the
    # other half of that leak. Measured under `-X dev` against the test phone,
    # one `list_devices()` call: five unclosed resources before either fix,
    # three with the lockdown closed alone, none with both.
    try:
        installed = await lc_discovery.installed_bundle_ids(lockdown)
    except Exception:
        return None
    try:
        return lc_discovery.choose(installed)[0]
    except LiveContainerError:
        roles = lc_discovery.livecontainers(installed)
        primaries = [
            b for b, role in roles.items()
            if role == "primary" or b.lower() == lc_discovery.FAMILY_PREFIX
        ]
        candidates = primaries or list(roles)
        return candidates[0] if candidates else None
