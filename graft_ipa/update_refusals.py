"""Versions an Apple ID was refused, so Update stops offering them every day.

A get the App Store refuses with `needs-license` (the Apple ID doesn't have
the app) would be refused again each day the plan offered it. So the run
remembers the app and the version (`remember`), and the plan says "Your Apple
ID doesn't have <version>; get it in the App Store" instead of a get
(`update_plan.store_step`). The tweak and install parts are still offered.

Forgotten by a later successful get of that app, by Update or by
`--appstore-get` (`forget`), and by the plan once the App Store has a newer
version than the one refused. Keyed by the app's name in Graft, and only while
its bundle id is the same. Nothing here names the Apple ID.

`update-refusals.json` in the state folder: read leniently (a missing,
unreadable or foreign file is no memory, and one that isn't a regular file
or is too big isn't read: `data_dirs.read_small_json`), written owner-only
and whole (`data_dirs.private_write`); a write that fails is dropped, since
this only saves the plan a sentence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import data_dirs
from .config import AppConfig

FILENAME = "update-refusals.json"
_FORMAT = 1


def _path(state_dir: Path) -> Path:
    return state_dir / FILENAME


def _load(state_dir: Path) -> dict[str, dict[str, Any]]:
    raw = data_dirs.read_small_json(_path(state_dir))
    if not isinstance(raw, dict) or raw.get("format") != _FORMAT:
        return {}
    apps = raw.get("apps")
    if not isinstance(apps, dict):
        return {}
    return {k: v for k, v in apps.items() if isinstance(k, str) and isinstance(v, dict)}


def _save(state_dir: Path, apps: dict[str, dict[str, Any]]) -> None:
    data = json.dumps({"format": _FORMAT, "apps": apps}, sort_keys=True, indent=1).encode("utf-8")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        data_dirs.private_write(data, _path(state_dir))
    except OSError:
        pass


def refused_version(state_dir: Path, app: AppConfig) -> str | None:
    """The version `app` was refused, or None."""
    entry = _load(state_dir).get(app.name)
    if not entry or entry.get("bundle_id") != app.bundle_id:
        return None
    version = entry.get("version")
    return version if isinstance(version, str) and version else None


def remember(state_dir: Path, app: AppConfig, version: str) -> None:
    apps = _load(state_dir)
    apps[app.name] = {"bundle_id": app.bundle_id, "version": version}
    _save(state_dir, apps)


def forget(state_dir: Path, app_name: str) -> None:
    apps = _load(state_dir)
    if apps.pop(app_name, None) is not None:
        _save(state_dir, apps)
