"""Storage and battery for one device, over lockdown.

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

from .device_session import PAIR_TIMEOUT_SECONDS, closing_best_effort

_DISK = "com.apple.disk_usage"
_BATTERY = "com.apple.mobile.battery"


@dataclass
class DeviceInfoRecord:
    udid: str
    total_bytes: int | None
    free_bytes: int | None
    battery_percent: int | None
    battery_charging: bool | None


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
        )


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
