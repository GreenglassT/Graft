"""What Graft last saw of each device: `devices-seen.json` in the state folder.

Keyed by UDID, one small map of facts per device. A cache of what a device
said, not configuration: nothing in it changes what a command does to a
device, only what Graft can say about one that isn't answering right now.
That is the last Developer Mode reading (`developer_mode.status`), the name,
model and iOS version the device last gave (`name_of`, `label`), which name
it while it's unplugged, and the LiveContainer the last listing found
(`--devices`). `--update --plan --quick` reads them without opening a
session. Only devices Graft has a reason to name are kept: connected, the
default, or delivered to (`forget_others`, which `--devices` calls), so the
file can't grow without bound.

Every fact has one type (`FACTS`), and a reader drops anything else, so a
hand edit or another format reads as "not seen" rather than as an error. A
write that fails is suppressed: the next sighting writes again.

Not locked. Two commands writing at once (the app's `--devices` and
`--device-info`, say) can each read before the other writes, and the second
write then drops the first one's fact. That costs one stale fact until the
device is next seen, which a cache can afford.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .data_dirs import private_write

if TYPE_CHECKING:
    from .devices import DeviceInfo

FILE = "devices-seen.json"
_FORMAT = 1
#: Far above any real bench's file: anything larger isn't one Graft wrote.
_MAX_BYTES = 256 * 1024

#: Every fact kept, and the one type it may have.
FACTS: dict[str, type] = {
    "developer_mode": bool,
    #: What the device last called itself, its model (`iPad13,16`) and its iOS
    #: version, from `--devices` and `--device-info`: how Graft names a device
    #: that isn't connected (spec §5b).
    "name": str,
    "model": str,
    "ios_version": str,
    # The LiveContainer a listing found on the device (`--devices`, a full
    # `--update` plan): what `--update --plan --quick` shows without asking
    # the phone (`update_readiness.QuickProbe`).
    "livecontainer_bundle_id": str,
}


def path(state_dir: Path) -> Path:
    return state_dir / FILE


def load(state_dir: Path) -> dict[str, dict[str, Any]]:
    """Every device remembered, by UDID, with only its well-typed facts."""
    raw = _read(path(state_dir))
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return {}
    if not isinstance(data, dict) or data.get("format") != _FORMAT:
        return {}
    devices = data.get("devices")
    if not isinstance(devices, dict):
        return {}
    found: dict[str, dict[str, Any]] = {}
    for udid, facts in devices.items():
        if not isinstance(udid, str) or not udid or not isinstance(facts, dict):
            continue
        kept = {k: v for k, v in facts.items() if k in FACTS and type(v) is FACTS[k]}
        if kept:
            found[udid] = kept
    return found


def seen(state_dir: Path, udid: str) -> dict[str, Any]:
    """`udid`'s facts, or `{}` when it was never seen."""
    return load(state_dir).get(udid, {})


def forget_others(state_dir: Path, keep: set[str]) -> None:
    """Forget every device not in `keep`, so the file holds only devices Graft
    has a reason to name (`cli._remember_devices` says which). Nothing is
    written when nothing changes, and a write that fails is suppressed."""
    devices = load(state_dir)
    kept = {udid: facts for udid, facts in devices.items() if udid in keep}
    if len(kept) == len(devices):
        return
    body = json.dumps({"format": _FORMAT, "devices": kept}, indent=2, sort_keys=True)
    try:
        private_write(body.encode(), path(state_dir))
    except OSError:
        pass


def name_of(state_dir: Path, udid: str) -> str | None:
    """What `udid` last called itself, or None when Graft was never told."""
    name = seen(state_dir, udid).get("name")
    return name if isinstance(name, str) and name.strip() else None


def label(state_dir: Path, udid: str) -> str:
    """What to call `udid` in a sentence: its last-known name, else its UDID."""
    return name_of(state_dir, udid) or udid


def listing_facts(device: DeviceInfo) -> dict[str, str]:
    """What a listing (`--devices`, or a full `--update` plan's look at the
    phone) learned about `device` worth keeping, without the stand-ins
    `devices._describe_device` puts in when the phone didn't say: its UDID for
    a name, "unknown" for the model and version. And the LiveContainer it
    found, for the daily `--update --plan --quick`, which opens no session of
    its own; none found keeps the last one."""
    facts = {
        "name": device.name if device.name and device.name != device.udid else None,
        "model": device.model if device.model and device.model != "unknown" else None,
        "ios_version": (
            device.ios_version if device.ios_version and device.ios_version != "unknown" else None
        ),
        "livecontainer_bundle_id": device.livecontainer_bundle_id,
    }
    return {k: v for k, v in facts.items() if v}


def remember(state_dir: Path, udid: str, **facts: Any) -> None:
    """Merge `facts` into what is remembered of `udid`.

    A `None` fact is "no answer this time" and leaves the last one. Nothing
    is written when nothing changes. A name not in `FACTS` is a caller's
    mistake, and raises.
    """
    unknown = sorted(set(facts) - set(FACTS))
    if unknown:
        raise ValueError(f"devices_seen doesn't keep {', '.join(unknown)}")
    new = {k: v for k, v in facts.items() if v is not None and type(v) is FACTS[k]}
    if not udid or not new:
        return
    devices = load(state_dir)
    entry = devices.get(udid, {})
    if all(entry.get(k) == v for k, v in new.items()):
        return
    devices[udid] = {**entry, **new}
    body = json.dumps({"format": _FORMAT, "devices": devices}, indent=2, sort_keys=True)
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        private_write(body.encode(), path(state_dir))
    except OSError:
        pass


def _read(file: Path) -> bytes | None:
    """`file`'s bytes when it is a regular file, not a link, of at most
    `_MAX_BYTES`; else None. Non-blocking, so a FIFO there can't hang."""
    try:
        fd = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return None
        data = os.read(fd, _MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    return data if len(data) <= _MAX_BYTES else None
