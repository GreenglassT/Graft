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
    #: Whether the app listing could say: False when it failed or listed no
    #: apps at all (how a locked phone answers). Only a known None is "no
    #: LiveContainer", the one the app disables Build for (spec §5b).
    livecontainer_known: bool = False


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
        name = name_from_lockdown(lockdown, udid)
        model = _safe_attr(lockdown, "product_type") or "unknown"
        ios_version = _safe_attr(lockdown, "product_version") or "unknown"

        conn_type_raw = getattr(mux_dev, "connection_type", "USB")
        connection_type = "Network" if str(conn_type_raw).lower().startswith("network") else "USB"

        lc_bundle, lc_known = await _livecontainer_on_device_async(lockdown)
        return DeviceInfo(
            udid=udid,
            name=str(name),
            model=str(model),
            ios_version=str(ios_version),
            connection_type=connection_type,
            livecontainer_bundle_id=lc_bundle,
            livecontainer_known=lc_known,
        )


#: A device's family by its product type's prefix (`iPad19,1`), for a device
#: with no name of its own. Only the kinds Graft delivers to.
_FAMILIES = (("iPhone", "iPhone"), ("iPad", "iPad"), ("iPod", "iPod touch"))


def name_from_lockdown(lockdown, udid: str | None = None) -> str | None:
    """What to call the device a lockdown session is open to: the one name
    every place Graft names a connected device starts from.

    The model's name when pymobiledevice3 knows the model (`display_name`,
    "iPhone 15 Pro"), else the name the device gives itself (`DeviceName`),
    else its family from the product type ("iPad"), else `udid` (None when
    not given, for a caller with its own words for "don't know").

    `display_name` comes from a table of models built into pymobiledevice3,
    so a model newer than the table has none. Graft then called it by its
    UDID everywhere, while the device was saying its name all along.

    Only text counts, and nothing is called: a session's values are read
    when it opens (`all_values`), and a stand-in's unset attribute, an async
    function, is no answer. Never raises.
    """
    values = _safe_attr(lockdown, "all_values")
    own_name = _text(values.get("DeviceName")) if isinstance(values, dict) else None
    return _model_name(lockdown) or own_name or _family(lockdown) or udid


def model_or_family_from_lockdown(lockdown) -> str | None:
    """`name_from_lockdown` without the name the device gives itself: the
    model's name, else the family, else None. For the Developer Mode hint,
    "your iPad", which would read oddly as "your Sample's iPad"."""
    return _model_name(lockdown) or _family(lockdown)


def _model_name(lockdown) -> str | None:
    return _text(_safe_attr(lockdown, "display_name"))


def _family(lockdown) -> str | None:
    product_type = _text(_safe_attr(lockdown, "product_type")) or ""
    for prefix, family in _FAMILIES:
        if product_type.startswith(prefix):
            return family
    return None


def _text(value: object) -> str | None:
    """`value` trimmed when it is text with something in it, else None."""
    return (value.strip() or None) if isinstance(value, str) else None


def _safe_attr(obj, attr: str):
    try:
        return getattr(obj, attr)
    except Exception:
        return None


async def _detect_livecontainer_async(lockdown) -> str | None:
    """The LiveContainer a push to this device would use, or None for none
    or for a listing that couldn't tell: `_livecontainer_on_device_async`'s
    first half."""
    return (await _livecontainer_on_device_async(lockdown))[0]


async def _livecontainer_on_device_async(lockdown) -> tuple[str | None, bool]:
    """The LiveContainer on the device, and whether the listing could tell.
    The LiveContainer a push to this device would use, or None for none.

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

    Every failure, the listing's and the close's, reads as no LiveContainer
    that the listing couldn't tell (`None, False`), as does a listing with
    no apps at all: the device is described either way.
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
        return None, False
    if not installed:
        # A locked phone lists no apps at all: no answer about LiveContainer.
        return None, False
    try:
        return lc_discovery.choose(installed)[0], True
    except LiveContainerError:
        roles = lc_discovery.livecontainers(installed)
        primaries = [
            b for b, role in roles.items()
            if role == "primary" or b.lower() == lc_discovery.FAMILY_PREFIX
        ]
        candidates = primaries or list(roles)
        return (candidates[0] if candidates else None), True
