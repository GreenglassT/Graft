"""Storage, battery and Developer Mode for one device, over lockdown.

Two domains, both cheap reads on an already-paired phone. Every field is
independently optional: a locked or busy phone answers some domains and not
others, and one refusal must not blank the whole stat strip — the pane renders
"—" per cell, so partial data beats none.

The `pymobiledevice3` import below is at module scope, unlike `devices.py`,
`device_apps.py` and `artwork.py`'s device rung, which all import it inside an
`async def` instead. That is deliberate here, not an oversight: those three
are imported eagerly by `cli.py` (`from . import (..., artwork, device_apps,
...)`), so a module-scope import in any of them would be paid by every
invocation, including `--describe` and a plain build — that is the whole
reason `pymobiledevice3` is ~0.22s and `--describe` has a ~0.14s floor to
protect. This module is different: `cli.py` does not import it eagerly (see
the comment at its one import site, in `_do_device_info`), so nothing pays
this module's import cost except an invocation that was already about to open
a lockdown session. Keeping the import at module scope here — rather than
copying the other three's function-local style out of habit — is what lets
tests patch `device_info.create_using_usbmux` directly, the same way they
patch every other collaborator in this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.amfi import AmfiService

from . import device_session
from .device_session import PAIR_TIMEOUT_SECONDS, Teardown, closing_best_effort
from .devices import model_or_family_from_lockdown, name_from_lockdown

_DISK = "com.apple.disk_usage"
_BATTERY = "com.apple.mobile.battery"
#: Where Developer Mode is said: pymobiledevice3's own
#: `LockdownServiceProvider.get_developer_mode_status` reads this key. Not
#: called, because it coerces with `bool()` (see `_developer_mode`).
_AMFI = "com.apple.security.mac.amfi"
_DEVELOPER_MODE_KEY = "DeveloperModeStatus"
#: Developer Mode arrived in iOS 16; an earlier phone has none to report.
_DEVELOPER_MODE_SINCE = 16
#: How long each step of a Developer Mode read may take: the one key, and in
#: `check_developer_mode` the session's opening and the reveal as well. A
#: `--device-info` takes 0.4 to 1.1 s on USB, so this is room for a slow
#: Wi-Fi phone, not a wait anyone should see.
DEVELOPER_MODE_SECONDS = 10.0


@dataclass
class DeviceInfoRecord:
    udid: str
    total_bytes: int | None
    free_bytes: int | None
    battery_percent: int | None
    battery_charging: bool | None
    #: True or False on iOS 16 and later; None before, or with no answer.
    developer_mode: bool | None = None
    #: What the device is called (the name `--devices` shows), its model and
    #: its iOS version, for `devices-seen.json`; not in the `device-info`
    #: event. None when the session didn't say.
    name: str | None = None
    model: str | None = None
    ios_version: str | None = None


def read_device_info(udid: str) -> DeviceInfoRecord | None:
    """Storage and battery for `udid`, or None if the phone cannot be reached.

    None means the *device* was unreachable. A reachable phone that declines a
    domain returns a record with those fields None — the caller can tell "no
    phone" from "no answer about storage", and the pane needs to.
    """
    try:
        return asyncio.run(_read_async(udid))
    except Exception:
        # Broad, matching `devices.py` and `artwork.py`: an unreachable phone,
        # a locked one, an expired pairing record and a lockdown version whose
        # exception types we do not model all have the same remedy for the
        # caller — show the pane without these two cells.
        return None


async def _read_async(udid: str) -> DeviceInfoRecord:
    lockdown = await create_using_usbmux(serial=udid, pair_timeout=PAIR_TIMEOUT_SECONDS)
    # What a bare close cost here specifically: the record is built before the
    # teardown runs, so a Wi-Fi phone dropping off the network between the last
    # domain read and `close()` replaced the pending return, the broad catch in
    # `read_device_info` turned it into None, and the CLI emitted
    # `reachable: false` for a phone whose storage and battery it had just read
    # correctly. An exception from the body still propagates, so an unreachable
    # phone still reaches that catch and still becomes None. See
    # `device_session`.
    async with closing_best_effort(lockdown):
        disk = await _domain_dict(lockdown, _DISK)
        battery = await _domain_dict(lockdown, _BATTERY)
        return DeviceInfoRecord(
            udid=udid,
            total_bytes=_as_int(disk.get("TotalDiskCapacity")),
            free_bytes=_as_int(disk.get("AmountDataAvailable")),
            battery_percent=_as_int(battery.get("BatteryCurrentCapacity")),
            battery_charging=_as_bool(battery.get("BatteryIsCharging")),
            developer_mode=await _developer_mode(lockdown),
            # The listing's name (`devices.name_from_lockdown`), without the
            # UDID it falls back to: no name is better kept than that one.
            name=name_from_lockdown(lockdown),
            model=_said(lockdown, "product_type"),
            # From the session's values, not `product_version`, which says
            # "1.0" for a phone that didn't answer.
            ios_version=_text(_values(lockdown).get("ProductVersion")),
        )


@dataclass(frozen=True)
class DeveloperModeCheck:
    """One live read of Developer Mode (`check_developer_mode`)."""

    #: True, False, or None for "don't know".
    on: bool | None
    #: What the device is called (`devices.name_from_lockdown`): "iPad Air",
    #: else the name it gives itself, else "iPad"; or None.
    name: str | None = None
    #: The model's name or the family, never the device's own name
    #: (`devices.model_or_family_from_lockdown`): the hint's "your iPad".
    model_name: str | None = None
    #: The phone showed the Developer Mode switch in Settings this time.
    revealed: bool = False
    #: Why a reveal that was asked for didn't happen, or None.
    reveal_error: str | None = None


def read_developer_mode(udid: str) -> bool | None:
    """Developer Mode on `udid`, read now in a session of its own, or None.

    For `--update`'s readiness (`developer_mode.status`). Reveals nothing:
    `check_developer_mode` without `reveal`.
    """
    return check_developer_mode(udid, reveal=False).on


def check_developer_mode(udid: str, *, reveal: bool) -> DeveloperModeCheck:
    """Developer Mode on `udid`, read now in a session of its own.

    `reveal`, a real build's about to be refused (`developer_mode.require_on`):
    when the answer is False, ask the phone, in the same session, to show the
    Developer Mode switch in Settings, which iOS hides until a development
    app is installed or Xcode connects (AMFI's reveal). Nothing else asks for
    it: not readiness, not `--device-info`, not a plan or the daily check. A
    reveal that fails or takes too long is `reveal_error`, and the reading
    stands.

    `autopair=False`: a phone that hasn't trusted this Mac answers None here
    rather than holding the build on a Trust prompt; the install pairs it, as
    it always has. The opening, the read and the reveal are each bounded by
    `DEVELOPER_MODE_SECONDS`, and the whole runs under `device_session.run`,
    so a Stop lands at the await it is in.
    """
    try:
        return device_session.run(_check_developer_mode_async(udid, reveal))
    except Exception:
        # Broad, as `read_device_info`: whatever stopped the read, the answer
        # is "don't know", and "don't know" never blocks a build.
        return DeveloperModeCheck(on=None)


async def _check_developer_mode_async(udid: str, reveal: bool) -> DeveloperModeCheck:
    # Bounded on its own: a Wi-Fi phone asleep with its connection half-open
    # holds the handshake itself, before any key is asked.
    lockdown = await asyncio.wait_for(
        create_using_usbmux(serial=udid, autopair=False), DEVELOPER_MODE_SECONDS
    )
    async with closing_best_effort(lockdown, within=Teardown()):
        on = await _developer_mode(lockdown)
        name = _device_name(lockdown)
        model_name = model_or_family_from_lockdown(lockdown)
        if not reveal or on is not False:
            return DeveloperModeCheck(on=on, name=name, model_name=model_name)
        try:
            await asyncio.wait_for(
                AmfiService(lockdown).reveal_developer_mode_option_in_ui(),
                DEVELOPER_MODE_SECONDS,
            )
        except Exception as e:
            # TimeoutError included. Here, not in the broad catch above, which
            # would turn the False just read into "don't know" and let the build
            # install an app that won't open.
            reason = "no answer in time" if isinstance(e, TimeoutError) else str(e) or type(e).__name__
            return DeveloperModeCheck(on=on, name=name, model_name=model_name, reveal_error=reason)
        return DeveloperModeCheck(on=on, name=name, model_name=model_name, revealed=True)


def _device_name(lockdown) -> str | None:
    """What `--devices` calls the device (`devices.name_from_lockdown`), or
    None when the session said nothing to name it by. Never raises."""
    return name_from_lockdown(lockdown)


async def _developer_mode(lockdown) -> bool | None:
    """True or False, read from the phone, or None: before iOS 16, a version
    Graft can't read, a refused or unanswered read, or an answer that isn't a
    bool. Never raises, so a refusal can't cost the rest of a reading."""
    if not _has_developer_mode(getattr(lockdown, "product_version", None)):
        return None
    try:
        value = await asyncio.wait_for(
            lockdown.get_value(domain=_AMFI, key=_DEVELOPER_MODE_KEY), DEVELOPER_MODE_SECONDS
        )
    except Exception:
        return None
    # `_as_bool`'s rule: a real bool or nothing. pymobiledevice3's own
    # `get_developer_mode_status` returns `bool(value)`, a verdict made of
    # anything, and a wrong False here stops a build.
    return value if isinstance(value, bool) else None


def _has_developer_mode(version: object) -> bool:
    """Whether a phone reporting `version` has Developer Mode at all: iOS 16
    or later. Its own check rather than pymobiledevice3's
    `has_developer_mode`, which the 10.x releases `pyproject.toml` still
    allows may not have. ASCII digits only: `"١٧"` passes `isdigit`."""
    if not isinstance(version, str):
        return False
    major = version.split(".", 1)[0]
    return major.isascii() and major.isdigit() and int(major) >= _DEVELOPER_MODE_SINCE


def _said(lockdown, attr: str) -> str | None:
    """`lockdown.<attr>` when it is non-empty text, else None (a property
    that raises included): what the session says the device is."""
    try:
        value = getattr(lockdown, attr)
    except Exception:
        return None
    return _text(value)


def _values(lockdown) -> dict:
    """The session's `all_values`, or `{}` when it has none that is a dict."""
    values = getattr(lockdown, "all_values", None)
    return values if isinstance(values, dict) else {}


def _text(value: object) -> str | None:
    """A non-empty string, or None."""
    return value if isinstance(value, str) and value.strip() else None


async def _domain_dict(lockdown, domain: str) -> dict:
    """One lockdown domain as a dict — `{}` if it failed or wasn't one.

    One function rather than the `_as_dict(await _domain(...))` pair this
    replaces: `_domain` already returned None on failure and `_as_dict` already
    mapped None to `{}`, so the two only ever composed, in both callers, in that
    one order. Failure and a non-dict answer land in the same place because the
    caller does the same thing with either — `.get()` per field, and a field it
    cannot read is None.
    """
    try:
        value = await lockdown.get_value(domain=domain)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _as_int(value: object) -> int | None:
    # `isinstance(True, int)` is True in Python, so booleans are excluded
    # explicitly — a battery percent of `True` would be nonsense.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_bool(value: object) -> bool | None:
    """A real bool, or None for anything else — including a missing key.

    Mirrors `_as_int`'s posture rather than coercing, which is what this
    replaced: `bool(battery["BatteryIsCharging"])` turns *anything* into a
    verdict, and `bool("NO")` is True. Guarding the key's presence (`"…" in
    battery`) covered absence but not a null value, so a phone that answered the
    domain and said nothing about charging was reported as "not charging" —
    which the pane draws identically to a phone that really is on battery.

    None is the honest third state here, and `DeviceInfoRecord.battery_charging`
    is already `bool | None` for exactly that reason: every field in this module
    is independently optional because a locked or busy phone answers some
    domains and not others.
    """
    return value if isinstance(value, bool) else None
