"""Around a handoff to PlayCover: the person's settings, and the install wait.

**The settings** (spec 5d item 1). PlayCover keeps each app's settings in
`App Settings/<bundle id>.plist` inside its container, and two of them decide
whether a handed-over app is usable at all: `playChain` crashed apps the first
time they saved a login, and `keymapping` makes a non-game's clicks miss
(`cli.PLAYCOVER_SETTINGS_DIR` has the history). Measured with PlayCover 3.1.0:
it writes the file with both on when it first installs an app, and leaves an
existing file alone on a reinstall. So:

- `snapshot`, before the handoff, reads the file and remembers its two values
  in Graft's state folder (`playcover-settings.json`, by bundle id). The
  memory is the fallback for a file that's missing or unreadable.
- `settle`, after the install: when the file was there, the person's values
  are left, and any PlayCover changed anyway are put back (`restored`). When
  it wasn't (a first install, or the app was removed from PlayCover), it
  waits for PlayCover to write one, then applies the remembered values, or on
  the app's first handoff Graft's defaults, both off (`disabled`, the keys
  actually turned off: the only case reported as a notice). A first handoff
  stays marked as one in the memory until its defaults are applied, so one
  that was stopped before `settle` finished is finished by the next.

Only those two keys; the rest of the file is never touched. Nothing here
raises: the handoff happened either way. No per-app "this is a game" switch:
turning keymapping on in PlayCover is that switch, and Graft keeps it.

**The install wait** (spec 5d item 5, `await_install`): after `open -b`,
PlayCover installs on its own, and the app it installs lands in
`Applications/<bundle id>.app` inside its container. Graft waits, up to two
minutes, for that copy to carry the handed version and build, written after
the handoff began.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

from . import data_dirs

#: The two settings Graft keeps, in the order its sentences name them.
SETTINGS_KEYS = ("keymapping", "playChain")

#: An app's first handoff: both off.
DEFAULTS = {"keymapping": False, "playChain": False}

#: How PlayCover's own window names each.
KEY_NAMES = {"keymapping": "keymapping", "playChain": "PlayChain"}

#: The memory, in Graft's state folder: `{"format": 1, "apps": {bundle id:
#: {key: bool, "defaults_pending": true}}}`. Read leniently (only a regular
#: file of a sane size: `data_dirs.read_small_json`), written owner-only and
#: whole.
MEMORY_FILE = "playcover-settings.json"
_FORMAT = 1

#: Set on an app's first handoff and cleared only once `settle` has applied
#: Graft's defaults: a first handoff whose settle never finished (a Stop
#: during the wait, the settings wait running out, no settings folder yet, a
#: write that failed) would otherwise leave PlayCover's own defaults, both on,
#: to be taken for the person's choice by the next handoff.
_PENDING = "defaults_pending"

#: How long `settle` waits for PlayCover to write an app's settings file.
SETTINGS_WAIT_S = 10.0
_POLL_S = 0.5

Values = dict[str, bool]


@dataclass(frozen=True)
class Before:
    """What `snapshot` found."""

    #: The settings file was there and readable before the handoff.
    existed: bool
    #: The values to keep: the file's (over the memory's, key by key), else
    #: the memory's; None on an app's first handoff.
    values: Values | None


@dataclass(frozen=True)
class Settled:
    """What `settle` changed, by key, in `SETTINGS_KEYS` order."""

    #: Turned off as Graft's defaults, on an app's first handoff.
    disabled: list[str] = field(default_factory=list)
    #: Put back to the person's value after PlayCover changed it.
    restored: list[str] = field(default_factory=list)


#: What a bundle id may be before a path is built from it: letters, digits,
#: hyphens and dots, starting with a letter or digit, no `..`. The bundle id
#: is the handed IPA's own `CFBundleIdentifier`, which a dropped IPA can set
#: to anything, `../../Library/X` included.
_SAFE_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,254}")


def safe_bundle_id(bundle_id: str) -> bool:
    """Whether a path may be built from `bundle_id`."""
    return bool(_SAFE_BUNDLE_ID.fullmatch(bundle_id)) and ".." not in bundle_id


def _inside(folder: Path, name: str) -> Path | None:
    """`folder / name`, or None when that would land anywhere but directly in
    `folder`, a link to elsewhere included."""
    path = folder / name
    try:
        if path.resolve().parent != folder.resolve():
            return None
    except (OSError, RuntimeError):
        return None
    return path


def _settings_path(settings_dir: Path, bundle_id: str) -> Path | None:
    """The app's settings file, or None for a bundle id no path may be built
    from (`safe_bundle_id`) or a file that leads out of the folder."""
    if not safe_bundle_id(bundle_id):
        return None
    return _inside(settings_dir, f"{bundle_id}.plist")


def _read_plist(path: Path) -> tuple[dict[str, Any], int] | None:
    """The settings file and its format, or None when it's missing or
    unreadable."""
    try:
        data = path.read_bytes()
        settings = plistlib.loads(data)
    except (OSError, ValueError, ExpatError, RecursionError):
        # ExpatError, which is no ValueError: XML cut off mid-write, as a
        # file PlayCover is writing right now reads. RecursionError: a binary
        # plist nested deeper than Python's recursion limit, which plistlib
        # reads a frame a level (the 2.3.1 review's A3).
        return None
    if not isinstance(settings, dict):
        return None
    fmt = plistlib.FMT_BINARY if data.startswith(b"bplist") else plistlib.FMT_XML
    return settings, fmt


def _values_of(settings: dict[str, Any]) -> Values:
    return {k: settings[k] for k in SETTINGS_KEYS if isinstance(settings.get(k), bool)}


def _write_keys(path: Path, settings: dict[str, Any], fmt: int, changes: Values) -> bool:
    """`changes` into the file, every other key as it was. False when it
    couldn't be written.

    Into a new file beside it, renamed over it: a rename replaces a link
    planted at `path` rather than writing through it. The file keeps its mode.
    """
    tmp: str | None = None
    try:
        data = plistlib.dumps({**settings, **changes}, fmt=fmt)
        try:
            mode = path.lstat().st_mode & 0o777 if not path.is_symlink() else 0o644
        except OSError:
            mode = 0o644
        fd, tmp = tempfile.mkstemp(prefix=".graft-", suffix=".plist", dir=path.parent)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = None
    except (OSError, TypeError, ValueError, OverflowError):
        return False
    finally:
        if tmp is not None:
            Path(tmp).unlink(missing_ok=True)
    return True


def _load_memory(state_dir: Path) -> dict[str, dict[str, bool]]:
    raw = data_dirs.read_small_json(state_dir / MEMORY_FILE)
    if not isinstance(raw, dict) or raw.get("format") != _FORMAT or not isinstance(raw.get("apps"), dict):
        return {}
    return {
        bundle_id: {**_values_of(entry), **({_PENDING: True} if entry.get(_PENDING) is True else {})}
        for bundle_id, entry in raw["apps"].items()
        if isinstance(bundle_id, str) and isinstance(entry, dict)
    }


def remembered(state_dir: Path, bundle_id: str) -> Values | None:
    """The values remembered for `bundle_id`, or None."""
    return _values_of(_load_memory(state_dir).get(bundle_id, {})) or None


def remember(state_dir: Path, bundle_id: str, values: Values, *, pending: bool = False) -> None:
    """Remember `values` for `bundle_id`, clearing its first-handoff marker
    unless `pending`. A write that fails is dropped."""
    apps = _load_memory(state_dir)
    entry: dict[str, bool] = {**_values_of(apps.get(bundle_id, {})), **values}
    if pending:
        entry[_PENDING] = True
    apps[bundle_id] = entry
    data = json.dumps({"format": _FORMAT, "apps": apps}, sort_keys=True, indent=1).encode("utf-8")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        data_dirs.private_write(data, state_dir / MEMORY_FILE)
    except OSError:
        pass


def snapshot(settings_dir: Path, state_dir: Path, bundle_id: str) -> Before:
    """Before the handoff: the app's settings as they are, remembered."""
    path = _settings_path(settings_dir, bundle_id)
    if path is None:
        return Before(existed=False, values=None)
    entry = _load_memory(state_dir).get(bundle_id, {})
    memory = _values_of(entry) or None
    found = _read_plist(path)
    if entry.get(_PENDING) or (found is None and memory is None):
        # The app's first handoff, or one whose first never had its defaults
        # applied: whatever the file says now is PlayCover's, not the person's.
        remember(state_dir, bundle_id, {}, pending=True)
        return Before(existed=False, values=None)
    if found is None:
        return Before(existed=False, values=memory)
    values = {**(memory or {}), **_values_of(found[0])}
    if values:
        remember(state_dir, bundle_id, values)
    return Before(existed=True, values=values)


def settle(
    settings_dir: Path, state_dir: Path, bundle_id: str, before: Before, *,
    wait: float = SETTINGS_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Settled:
    """After the install: keep, put back, or apply the app's two settings.
    Never raises. Does nothing for a bundle id no path may be built from, or
    a settings file that leads out of the folder."""
    path = _settings_path(settings_dir, bundle_id)
    if path is None:
        return Settled()
    if before.existed:
        found = _read_plist(path)
        if found is None or not before.values:
            return Settled()
        settings, fmt = found
        changes = {k: v for k, v in before.values.items() if settings.get(k) is not v}
        if not changes or not _write_keys(path, settings, fmt, changes):
            return Settled()
        return Settled(restored=[k for k in SETTINGS_KEYS if k in changes])

    # No settings folder at all: PlayCover has installed nothing here, and
    # there's nothing to wait for.
    if not settings_dir.is_dir():
        return Settled()
    deadline = clock() + wait
    while (found := _read_plist(path)) is None:
        if clock() >= deadline:
            return Settled()
        sleep(_POLL_S)
    # Asked again: a link may have been planted while this waited.
    if _settings_path(settings_dir, bundle_id) is None:
        return Settled()
    settings, fmt = found
    target = before.values if before.values is not None else DEFAULTS
    changes = {k: v for k, v in target.items() if settings.get(k) is not v}
    if changes and not _write_keys(path, settings, fmt, changes):
        return Settled()
    # Clears the first-handoff marker: the defaults are applied now.
    remember(state_dir, bundle_id, {**_values_of(settings), **target})
    if before.values is not None:
        return Settled()
    return Settled(disabled=[k for k in SETTINGS_KEYS if changes.get(k) is False])


def _named(keys: list[str]) -> str:
    return " and ".join(KEY_NAMES[k] for k in SETTINGS_KEYS if k in keys)


def disabled_notice(keys: list[str], app: str) -> str:
    """What `disabled` says, in the CLI and the app (spec 5d item 2)."""
    line = f"Graft turned off PlayCover's {_named(keys)} for {app}."
    if "keymapping" in keys:
        line += (" For a game, turn keymapping back on in PlayCover; Graft keeps it from "
                 "then on.")
    return line


def restored_line(keys: list[str], app: str) -> str:
    """What `restored` says: a dim line, not a notice."""
    noun = "settings" if len(keys) > 1 else "setting"
    return f"Graft put back your PlayCover {_named(keys)} {noun} for {app}."


#: How long `--install-to-mac` waits for PlayCover to finish installing.
INSTALL_WAIT_S = 120.0


def archive_versions(ipa: Path) -> tuple[str | None, str | None]:
    """The archive's app's `CFBundleShortVersionString` and
    `CFBundleVersion`, each None when absent. Raises for an archive with no
    app's Info.plist, as `read_info_plist` does, which runs first."""
    import zipfile

    with zipfile.ZipFile(ipa) as zf:
        name = next(n for n in zf.namelist()
                    if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2)
        info = plistlib.loads(zf.read(name))

    def text(key: str) -> str | None:
        value = info.get(key)
        return str(value) if value is not None else None

    return text("CFBundleShortVersionString"), text("CFBundleVersion")


def _installed(info: Path, version: str | None, build: str | None, began: float) -> bool:
    try:
        st = info.stat()
        # ctime as well as mtime: an unzip that keeps the archive's dates
        # leaves the mtime in the past, but the file was written now.
        if max(st.st_mtime, st.st_ctime) < began:
            return False
        plist = plistlib.loads(info.read_bytes())
    except Exception:
        # Anything: this is polled while PlayCover writes the file, and a
        # half-written one (ExpatError for XML) is only "not yet".
        return False
    if not isinstance(plist, dict):
        return False

    def text(key: str) -> str | None:
        value = plist.get(key)
        return str(value) if value is not None else None

    return text("CFBundleShortVersionString") == version and text("CFBundleVersion") == build


def await_install(
    apps_dir: Path, bundle_id: str, *, version: str | None, build: str | None, began: float,
    timeout: float = INSTALL_WAIT_S,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Wait for PlayCover to install the handed app: True once
    `<apps_dir>/<bundle id>.app/Info.plist` carries the handed archive's
    version and build and was written at or after `began` (wall-clock time,
    taken as the handoff began), False when `timeout` runs out first.

    Neither answer is a failure: PlayCover shows its own progress, and an
    install still running at the timeout may yet finish. A Stop ends the
    wait from outside (the CLI's SIGTERM handling), never the handoff.

    False at once, without waiting, for a bundle id no path may be built
    from (`safe_bundle_id`) or an app folder that leads out of `apps_dir`.
    """
    if not safe_bundle_id(bundle_id) or _inside(apps_dir, f"{bundle_id}.app") is None:
        return False
    info = apps_dir / f"{bundle_id}.app" / "Info.plist"
    deadline = clock() + timeout
    while not _installed(info, version, build, began):
        if clock() >= deadline:
            return False
        sleep(_POLL_S)
    return True
