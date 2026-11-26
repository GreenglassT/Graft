"""Devices `graft --update` no longer installs an app on, until it's delivered there again.

An app's update targets are every device in its delivery history (spec
§11.3), and that history keeps devices that were sold, retired or reset: their
targets would wait forever. `--drop-update-target APP --device UDID` (the
app's "Stop updating <app> on <device>") drops one, per app and device.

Each drop remembers the newest delivery of that app to that device when it
was made (the delivery log's row id, which only grows). A later delivery of
**that app** to that device brings the target back; another app's delivery
there doesn't (`cli._delivery_targets` compares the two). Nothing here touches
a device.

`update-dropped-targets.json` in the state folder: read leniently (a missing,
unreadable or foreign file drops nothing, and one that isn't a regular file
or is too big isn't read: `data_dirs.read_small_json`), written owner-only
and whole (`data_dirs.private_write`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import data_dirs

FILENAME = "update-dropped-targets.json"
_FORMAT = 1


def _path(state_dir: Path) -> Path:
    return state_dir / FILENAME


def _load(state_dir: Path) -> dict[str, dict[str, int]]:
    raw = data_dirs.read_small_json(_path(state_dir))
    if not isinstance(raw, dict) or raw.get("format") != _FORMAT:
        return {}
    apps = raw.get("apps")
    if not isinstance(apps, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for app, drops in apps.items():
        if not isinstance(app, str) or not isinstance(drops, dict):
            continue
        kept = {u: n for u, n in drops.items()
                if isinstance(u, str) and u and type(n) is int}
        if kept:
            out[app] = kept
    return out


def _save(state_dir: Path, apps: dict[str, dict[str, Any]]) -> None:
    data = json.dumps({"format": _FORMAT, "apps": apps}, sort_keys=True, indent=1).encode("utf-8")
    state_dir.mkdir(parents=True, exist_ok=True)
    data_dirs.private_write(data, _path(state_dir))


def dropped(state_dir: Path, app_name: str) -> dict[str, int]:
    """`app_name`'s dropped devices: UDID → the newest delivery of the app
    there when it was dropped."""
    return dict(_load(state_dir).get(app_name, {}))


def set_dropped(state_dir: Path, app_name: str, drops: dict[str, int]) -> None:
    """Replace `app_name`'s dropped devices with `drops` (none: forget the
    app). Raises `OSError` when the file can't be written: a drop the person
    asked for that didn't happen must say so."""
    apps = _load(state_dir)
    if drops:
        apps[app_name] = dict(drops)
    elif apps.pop(app_name, None) is None:
        return
    _save(state_dir, apps)
