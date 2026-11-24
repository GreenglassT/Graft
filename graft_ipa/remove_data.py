"""What Graft keeps outside Graft.app, and removing it (`graft --remove-data`).

Dragging Graft.app to the Trash removes only the app. `survey` lists every
place Graft and the tools it bundles keep anything else, each with its size
and a plain description, and `remove` removes the items the user chose and
nothing else.

Every location is the one the code that writes it uses, not one found by
looking at the disk:

- Graft's four folders (`data_dirs`): config, cache, state and data, under
  the XDG roots or their defaults, each with the `tweak-build` link the
  rename left beside it. A `tweak-build` folder from before the rename that
  still sits beside `graft` (`data_dirs.migrate_legacy_dir` leaves one when
  both hold something) is an item of its own, off by default.
- The App Store sign-in: ipatool's three items in the login keychain
  (`appstore_account.IPATOOL_ITEMS`, service `ipatool-auth.service`), and its
  session cookies in its state folder. ipatool 2.8.0's
  `prepareStateDirectory` uses `$XDG_STATE_HOME/ipatool`, else
  `$XDG_DATA_HOME/ipatool`, else `~/.ipatool`, and moves `~/.ipatool` to the
  first when it can.
- ipatool's App Store components and Unicorn: Go's `os.UserCacheDir()` plus
  `ipatool`, which is `~/Library/Caches/ipatool`, and the `ipatool-*`
  workspaces it makes in $TMPDIR (`appstore_login.download_folders`).
- UnfairPlay's model: `~/Library/Application Support/unfairplay`, when the
  model folder is UnfairPlay's default (`config._default_unfairplay_models`).
- The .p12 and SSH passwords: Graft's own keychain items, service `graft`
  (`keychain.SERVICE`) or `tweak-build` from before the rename, account
  `p12-<team id>` or `ssh-<UDID>`. Off by default: they take a minute to
  type again, but only if you still have them.
- `~/.local/bin/graft`, Settings › Command-line tool's link, only when it
  points into a Graft.app (`Contents/Helpers/graft`).
- Graft.app's own preferences, `com.greenglasst.graft`. Listed, but the app
  removes them itself (`removable` is false here): `defaults` goes through
  cfprefsd, which keeps the real account's preferences whatever HOME says,
  so nothing here could test it without touching the real ones.
- Its logs folder, `~/Library/Logs/Graft`: the update log the self-update's
  helper writes (`app_update.log_path`), and a from-source install's last
  rebuild output (`RebuildGate.persist`). Only when it is a folder, not a
  link.
- Temporary folders a run that never finished left in $TMPDIR, by the names
  Graft's own `mkdtemp` calls give them, and the rebuild marker a
  from-source install keeps in `~/Library/Caches/Graft`.

Never removed, and listed as kept instead: anything config.yaml points to
outside Graft's folders (`cache_dir`, `built_dir`, `tweaks_dir`, `state_dir`,
the signing files, the App Store Connect key and its profiles), a model
folder set in config.yaml or by `UNFAIRPLAY_MODEL_DIR`, a frida-ios-dump set
by `GRAFT_FRIDA_IOS_DUMP`, a `graft` folder that is a link the user made, and
a `~/.local/bin/graft` that isn't Graft.app's. Those are places the user
chose, and may hold more than Graft put there.

Keychain items are found and removed by service and account only:
`keychain.find_item` and `keychain.first_account` read attributes, never a
secret, so no access dialog, and `keychain.delete_item` (through
`appstore_account.clear_items` for ipatool's) removes one exact item.
Nothing asks the keychain anything while it is locked
(`keychain.login_keychain_locked`), and the first lookup `security` doesn't
answer is the last one asked.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import appstore_account, data_dirs, keychain
from .config import _default_unfairplay_models, _expand, _load_raw
from .errors import ConfigError, GraftError

#: Graft.app's bundle id: its preferences domain.
APP_BUNDLE_ID = "com.greenglasst.graft"

GROUP_FILES = "files"
GROUP_APP_STORE = "app-store"
GROUP_PASSWORDS = "passwords"
GROUP_TERMINAL = "terminal"
GROUP_APP = "app"
GROUP_LEFTOVERS = "leftovers"
GROUP_KEPT = "kept"

#: Each group's heading, in the order the items are listed.
GROUP_TITLES: dict[str, str] = {
    GROUP_FILES: "Graft's folders",
    GROUP_APP_STORE: "App Store",
    GROUP_PASSWORDS: "Passwords",
    GROUP_TERMINAL: "Terminal",
    GROUP_APP: "Graft.app",
    GROUP_LEFTOVERS: "Leftovers",
    GROUP_KEPT: "Not removed",
}

#: `--items all`: every item that is there and on by default.
ALL = "all"

#: The keychain, as the listing's end event reports it.
KEYCHAIN_CHECKED = "checked"
KEYCHAIN_LOCKED = "locked"
KEYCHAIN_NO_ANSWER = "no-answer"

#: Why a keychain item wasn't looked at, or removed.
LOCKED_REASON = "your login keychain is locked: unlock it in Keychain Access, then try again"
NO_ANSWER_REASON = (
    "the keychain didn't answer: unlock it in Keychain Access (or restart this Mac if it "
    "won't unlock), then try again"
)

#: The temp folder `temp` and `ipatool-cache` look in. None: $TMPDIR, read
#: when it is needed. The test suite points it at a scratch folder for every
#: test (conftest), so no test can remove anything from the real one.
TEMP_ROOT: Path | None = None

#: How old a temporary folder must be before it counts as left behind: far
#: past any run that could still be using it (`p12.STALE_COPY_SECONDS`).
STALE_SECONDS = 60 * 60

#: The temporary folders Graft's own code makes, by name, and no other:
#: `mkdtemp`'s eight random characters after one of its prefixes. A build's
#: is `graft-<app>-`, so any `graft-…-`; LiveContainer's signing and install
#: use `lcsign-` and `lcinstall-`.
_TEMP_DIR = re.compile(r"(?:graft-.+-|lcsign-|lcinstall-)[a-z0-9_]{8}")
#: The file the app makes to stop root's enrollment run
#: (`enrollment.cancel_file_path`), if one was left.
_TEMP_FILE = re.compile(r"graft-enroll-cancel\.[0-9a-f]{16}")

#: A keychain account Graft makes: `p12-<team id>` or `ssh-<UDID>`.
_GRAFT_ACCOUNT = re.compile(r"(?:p12|ssh)-[A-Za-z0-9._-]{1,64}")
#: A team id or UDID read from config.yaml or the build log, before it is
#: made into an account to ask about.
_ACCOUNT_PART = re.compile(r"[A-Za-z0-9._-]{1,64}")
#: How many of Graft's keychain items one removal deletes under one service,
#: at most, while looking for ones it had no record of.
_SWEEP_LIMIT = 64


@dataclass
class Item:
    """One thing `--remove-data` lists: what it is, where, and how big."""

    id: str
    group: str
    title: str
    detail: str
    paths: list[Path] = field(default_factory=list)
    #: (service, account) of each keychain item, as found.
    keychain: list[tuple[str, str]] = field(default_factory=list)
    #: Whether anything is there. None: it couldn't be checked (the keychain
    #: was locked or didn't answer).
    present: bool | None = False
    #: Chosen unless the user unchecks it, and what `--items all` takes.
    default: bool = True
    #: Whether this command removes it. False for what the user chose (the
    #: `kept` group) and for Graft.app's preferences, which the app removes.
    removable: bool = True
    #: A caution, such as another program sharing it.
    note: str | None = None
    #: How to remove it by hand, for an item this command doesn't remove.
    how: str | None = None
    size_bytes: int | None = None

    def fields(self) -> dict[str, Any]:
        """The `data-item` event's fields."""
        return {
            "id": self.id,
            "group": self.group,
            "group_title": GROUP_TITLES[self.group],
            "title": self.title,
            "detail": self.detail,
            "note": self.note,
            "how": self.how,
            "paths": [str(p) for p in self.paths],
            "keychain": [{"service": s, "account": a} for s, a in self.keychain],
            "size_bytes": self.size_bytes,
            "present": self.present,
            "default": self.default,
            "removable": self.removable,
        }


@dataclass
class Survey:
    items: list[Item]
    #: `KEYCHAIN_CHECKED`, `KEYCHAIN_LOCKED` or `KEYCHAIN_NO_ANSWER`.
    keychain: str

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)


@dataclass
class Outcome:
    """What removing one item did (or, in a dry run, would do)."""

    id: str
    #: Each path, and each keychain item as "<service> <account>", that is
    #: gone (or would go).
    removed: list[str] = field(default_factory=list)
    #: (what, why) for each that is still there.
    failed: list[tuple[str, str]] = field(default_factory=list)
    freed_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed

    def fields(self, *, dry_run: bool) -> dict[str, Any]:
        """The `data-removed` event's fields."""
        return {
            "id": self.id,
            "ok": self.ok,
            "dry_run": dry_run,
            "removed": self.removed,
            "failed": [{"what": what, "reason": why} for what, why in self.failed],
            "freed_bytes": self.freed_bytes,
        }


# --- Sizes and paths -----------------------------------------------------------


def size_of(path: Path) -> int:
    """Bytes in the files at or under `path`, never following a link (a link
    counts as nothing). 0 for a path that isn't there."""
    try:
        st = os.lstat(path)
    except OSError:
        return 0
    if stat.S_ISLNK(st.st_mode):
        return 0
    if not stat.S_ISDIR(st.st_mode):
        return st.st_size
    total = 0
    stack = [os.fspath(path)]
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        stack.append(entry.path)
                    elif not stat.S_ISLNK(info.st_mode):
                        total += info.st_size
        except OSError:
            continue
    return total


def _there(path: Path) -> bool:
    return os.path.lexists(path)


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _same(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or under it, links resolved on both sides."""
    real, base = os.path.realpath(path), os.path.realpath(folder)
    return real == base or real.startswith(base.rstrip("/") + "/")


def temp_root() -> Path:
    """$TMPDIR, as Graft's `mkdtemp` calls and ipatool use it."""
    if TEMP_ROOT is not None:
        return TEMP_ROOT
    return Path(os.environ.get("TMPDIR") or tempfile.gettempdir())


def _stale_own(entry: os.DirEntry[str], *, now: float, directory: bool) -> bool:
    """A temp entry of this user's, of the wanted kind, untouched for
    `STALE_SECONDS`: a run that is still going is never one."""
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return kind and info.st_uid == os.getuid() and now - info.st_mtime > STALE_SECONDS


def _temp_entries(match: Callable[[str], bool], *, directory: bool) -> list[Path]:
    now = time.time()
    try:
        with os.scandir(temp_root()) as entries:
            return sorted(
                Path(e.path) for e in entries
                if match(e.name) and _stale_own(e, now=now, directory=directory)
            )
    except OSError:
        return []


def ipatool_state_dirs() -> list[Path]:
    """Where ipatool 2.8.0 keeps its session cookies (`cmd/state_directory.go`):
    `~/.ipatool`, and `$XDG_STATE_HOME/ipatool` (else `$XDG_DATA_HOME/ipatool`)
    when that variable is a full path, which it moves `~/.ipatool` to. Both,
    since a move it couldn't make leaves the old one in use."""
    dirs = [Path.home() / ".ipatool"]
    for var in ("XDG_STATE_HOME", "XDG_DATA_HOME"):
        base = os.environ.get(var)
        if base and os.path.isabs(base):
            dirs.append(Path(base) / "ipatool")
            break
    return dirs


def ipatool_cache_dir() -> Path:
    """Go's `os.UserCacheDir()` on macOS, `$HOME/Library/Caches`, plus
    `ipatool`: its Unicorn runtime and App Store assets."""
    return Path.home() / "Library" / "Caches" / "ipatool"


def unfairplay_default_models() -> Path:
    return Path.home() / "Library" / "Application Support" / "unfairplay" / "models"


def graft_link() -> Path:
    """Settings › Command-line tool's link (`CommandLineTool`)."""
    return Path.home() / ".local" / "bin" / "graft"


def into_graft_app(destination: str, *, link: Path) -> bool:
    """Whether a link at `link` to `destination` points at the `graft` inside
    an app bundle (`<name>.app/Contents/Helpers/graft`), as the app's link
    does. A relative destination is read from the link's folder."""
    full = Path(os.path.normpath(link.parent / destination))
    parts = full.parts
    return len(parts) >= 5 and parts[-3:] == ("Contents", "Helpers", "graft") and parts[-4].endswith(".app")


def rebuild_marker() -> Path:
    """`SourceWatch.attemptMarker`: a from-source install's last rebuild
    attempt, under the app's Caches folder."""
    return Path.home() / "Library" / "Caches" / "Graft" / "last-rebuild-attempt"


def preferences_plist() -> Path:
    return Path.home() / "Library" / "Preferences" / f"{APP_BUNDLE_ID}.plist"


def logs_dir() -> Path:
    """`~/Library/Logs/Graft`: the folder `app_update.log_path` writes the
    update log into, and Graft.app its rebuild log. Spelled out here rather
    than imported, so the listing doesn't load the updater; a test holds the
    two together."""
    return Path.home() / "Library" / "Logs" / "Graft"


# --- What config.yaml says ---------------------------------------------------------


def _lenient_config(path: Path) -> dict[str, Any]:
    """config.yaml as a mapping, or {} when there is none or it won't load:
    the listing must work on a machine whose setup never finished or whose
    config is broken, and only the kept folders and the password accounts
    come from it."""
    try:
        return _load_raw(path)
    except ConfigError:
        return {}


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def _path_value(value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = _expand(value)
    return path if path.is_absolute() else None


# --- The keychain, asked carefully -------------------------------------------------


class _Keychain:
    """Asks `security` about items until it shouldn't: nothing while the
    login keychain is locked, and nothing more once a lookup went unanswered,
    so no more than one `keychain.TIMEOUT_SECONDS` wait stands in front of
    the listing."""

    def __init__(self) -> None:
        self.status = KEYCHAIN_LOCKED if keychain.login_keychain_locked() else KEYCHAIN_CHECKED

    @property
    def usable(self) -> bool:
        return self.status == KEYCHAIN_CHECKED

    def find(self, service: str, account: str) -> keychain.Lookup:
        if not self.usable:
            return keychain.Lookup(None)
        look = keychain.find_item(service, account)
        if look.no_answer:
            self.status = KEYCHAIN_NO_ANSWER
        return look

    def first(self, service: str) -> keychain.Lookup:
        if not self.usable:
            return keychain.Lookup(None)
        look = keychain.first_account(service)
        if look.no_answer:
            self.status = KEYCHAIN_NO_ANSWER
        return look


# --- The items -------------------------------------------------------------------------

_FOLDERS: tuple[tuple[str, Callable[[], Path], str, str], ...] = (
    (
        "config", data_dirs.xdg_config_home, "Your setup",
        "config.yaml, the .p12 and provisioning profile Graft copied in when you set it up, "
        "your App Store Connect key and the profiles made with it, and your tweaks. The files "
        "you picked during setup stay where they were.",
    ),
    (
        "cache", data_dirs.xdg_cache_home, "Staged IPAs, archived builds and icons",
        "The decrypted IPAs Graft builds from, the builds it keeps for re-installing, and "
        "cached app icons. Often several GB.",
    ),
    (
        "state", data_dirs.xdg_state_home, "Build history and logs",
        "Build history and delivery records, each build's saved log, repository indexes, "
        "earlier versions of tweaks you updated, and Graft's record of its App Store sign-in.",
    ),
    (
        "data", data_dirs.xdg_data_home, "Tools installed for Graft",
        "frida-ios-dump, for decrypting on a jailbroken device, and tools a pipx install of "
        "Graft uses.",
    ),
)


def _our_legacy_link(root: Path) -> Path | None:
    """`root/tweak-build` when it is the link the rename left (`graft`)."""
    legacy = root / data_dirs.LEGACY_DIRNAME
    try:
        target = os.readlink(legacy)
    except OSError:
        return None
    return legacy if target in (data_dirs.APP_DIRNAME, str(root / data_dirs.APP_DIRNAME)) else None


def _kept(key: str, title: str, path: Path, detail: str, *, how: str | None = None) -> Item:
    return Item(
        id=f"kept-{key}", group=GROUP_KEPT, title=title, detail=detail, paths=[path],
        present=True, default=False, removable=False, how=how,
    )


def _graft_folders(kept: list[Item]) -> list[Item]:
    items: list[Item] = []
    for item_id, root_of, title, detail in _FOLDERS:
        root = root_of()
        path = data_dirs.app_dir(root)
        if path.is_symlink():
            # A link the user made, `graft` or a `tweak-build` used in place
            # (`data_dirs.app_dir`): theirs, and so is what it points to.
            kept.append(_kept(
                item_id, f"{title}, through a link you made", path,
                f"{path} is a link to {os.path.realpath(path)}. Graft leaves the link, and "
                "what it points to, for you to remove.",
            ))
            items.append(Item(id=item_id, group=GROUP_FILES, title=title, detail=detail))
            continue
        paths: list[Path] = []
        if _is_real_dir(path):
            paths.append(path)
        link = _our_legacy_link(root)
        if link is not None:
            paths.append(link)
        items.append(Item(
            id=item_id, group=GROUP_FILES, title=title, detail=detail, paths=paths,
            present=bool(paths),
        ))
    return items


def _legacy_folders() -> Item:
    """`tweak-build` folders from before the rename that aren't in use: each
    sits beside a `graft` that is (`data_dirs.migrate_legacy_dir`'s "both
    exist")."""
    paths = []
    for root in (data_dirs.xdg_config_home(), data_dirs.xdg_cache_home(),
                 data_dirs.xdg_state_home(), data_dirs.xdg_data_home()):
        old = root / data_dirs.LEGACY_DIRNAME
        if _is_real_dir(old) and data_dirs.app_dir(root) != old:
            paths.append(old)
    return Item(
        id="legacy", group=GROUP_FILES, title="Folders from before the rename to Graft",
        detail="tweak-build folders Graft found beside its own and left alone, because both "
        "held files. Anything you still needed from them should already be in Graft's.",
        paths=paths, present=bool(paths), default=False,
    )


def _appstore_signin(kc: _Keychain) -> Item:
    service = appstore_account.IPATOOL_SERVICE
    found: list[tuple[str, str]] = []
    unknown = False
    foreign = False
    record = appstore_account.vouched()
    for name in appstore_account.IPATOOL_ITEMS:
        look = kc.find(service, name)
        if look.exists is None:
            unknown = True
            break
        if look.exists:
            found.append((service, name))
            if name == "account" and (look.created is None or record.get(name) != look.created):
                foreign = True
    paths = [p for p in ipatool_state_dirs() if _there(p)]
    present = True if found or paths else (None if unknown else False)
    note = (
        "If you use ipatool on its own in Terminal, it signs in with these same items, so "
        "removing them signs it out too."
    )
    if foreign:
        note = (
            "Graft's own ipatool didn't make this sign-in: another ipatool did, perhaps one "
            "you use in Terminal, and removing it signs that one out."
        )
    return Item(
        id="appstore-signin", group=GROUP_APP_STORE, title="Your App Store sign-in",
        detail="Three items in your login keychain (service ipatool-auth.service: account, "
        "anisette-v4 and kbsync-v1) and ipatool's session cookies. Graft saves neither your "
        "Apple ID nor your password anywhere.",
        paths=paths, keychain=found, present=present, default=not foreign, note=note,
    )


def _ipatool_cache() -> Item:
    paths = [p for p in [ipatool_cache_dir()] if _there(p)]
    paths += _temp_entries(lambda name: name.startswith("ipatool-"), directory=True)
    return Item(
        id="ipatool-cache", group=GROUP_APP_STORE, title="ipatool's App Store components",
        detail="What ipatool downloads the first time it signs in, about 320 MB: the App "
        "Store's frameworks from a macOS restore image, and its Unicorn emulator. The next "
        "sign-in downloads them again.",
        paths=paths, present=bool(paths),
        note="If you use ipatool on its own, it uses this folder too.",
    )


def _unfairplay_model(raw: dict[str, Any], kept: list[Item]) -> Item:
    configured = _path_value(_section(raw, "appstore").get("unfairplay_models"))
    models = configured or _default_unfairplay_models()
    default = unfairplay_default_models()
    item = Item(
        id="unfairplay-model", group=GROUP_APP_STORE, title="The decryption model",
        detail="What Set Up Decryption made, which UnfairPlay decrypts App Store downloads "
        "with. Setting up decryption again makes another.",
        note="If you use UnfairPlay on its own, it uses this model too.",
    )
    if os.path.normpath(models) == os.path.normpath(default):
        # The `unfairplay` folder around `models`, as the README has always
        # said to remove: Set Up Decryption made both
        # (`enrollment.prepare_model_dir`), and both are UnfairPlay's.
        if _there(default.parent):
            item.paths, item.present = [default.parent], True
    elif _there(models):
        where = "config.yaml's appstore.unfairplay_models" if configured else "UNFAIRPLAY_MODEL_DIR"
        kept.append(_kept(
            "unfairplay-model", "The decryption model, in a folder you chose", models,
            f"{where} points here, so Graft leaves it for you to remove.",
        ))
    return item


def _log_ids(state_dirs: Iterable[Path]) -> tuple[set[str], set[str]]:
    """Team ids and UDIDs the build log names, read-only."""
    teams: set[str] = set()
    udids: set[str] = set()
    queries = (
        (teams, "SELECT DISTINCT signing_team FROM builds"),
        (udids, "SELECT DISTINCT udid FROM builds"),
        (udids, "SELECT DISTINCT udid FROM deliveries"),
        (udids, "SELECT DISTINCT udid FROM lc_installs"),
        (udids, "SELECT DISTINCT udid FROM cert_pushes"),
    )
    for state in state_dirs:
        db = state / "log.sqlite"
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for into, sql in queries:
                try:
                    into.update(str(row[0]) for row in conn.execute(sql) if row[0])
                except sqlite3.Error:
                    continue
        finally:
            conn.close()
    return teams, udids


def _password_accounts(raw: dict[str, Any]) -> list[str]:
    """The accounts Graft's passwords would be under, from what it knows:
    the team in config.yaml's `signing:`, the default device, and the teams
    and devices in the build log."""
    teams: set[str] = set()
    udids: set[str] = set()
    team = _section(raw, "signing").get("team_id")
    if team:
        teams.add(str(team))
    if raw.get("default_device_udid"):
        udids.add(str(raw["default_device_udid"]))
    state_dirs = [data_dirs.state_dir()]
    custom = _path_value(raw.get("state_dir"))
    if custom is not None:
        state_dirs.append(custom)
    log_teams, log_udids = _log_ids(state_dirs)
    teams |= log_teams
    udids |= log_udids
    accounts = [f"p12-{t}" for t in sorted(teams) if _ACCOUNT_PART.fullmatch(t)]
    accounts += [f"ssh-{u}" for u in sorted(udids) if _ACCOUNT_PART.fullmatch(u)]
    return accounts


def _passwords(raw: dict[str, Any], kc: _Keychain) -> Item:
    found: list[tuple[str, str]] = []
    unknown = False
    accounts = _password_accounts(raw)
    for service in (keychain.SERVICE, keychain.LEGACY_SERVICE):
        # The first item under the service, whatever Graft remembers: one
        # it has no record of still shows.
        first = kc.first(service)
        if first.exists is None:
            unknown = True
            break
        if not first.exists:
            # Nothing at all under this service: no account to ask about.
            continue
        if first.account and _GRAFT_ACCOUNT.fullmatch(first.account):
            found.append((service, first.account))
        for account in accounts:
            if (service, account) in found:
                continue
            look = kc.find(service, account)
            if look.exists is None:
                unknown = True
                break
            if look.exists:
                found.append((service, account))
        if unknown:
            break
    return Item(
        id="passwords", group=GROUP_PASSWORDS, title="Your .p12 and SSH passwords",
        detail="The passwords Graft keeps in your login keychain (service graft, or "
        "tweak-build from before the rename): your .p12's, as p12-<team id>, and each "
        "jailbroken device's root password, as ssh-<UDID>. Off unless you choose it.",
        keychain=found,
        present=True if found else (None if unknown else False),
        default=False,
        note="Removing them also removes any others under the service graft whose account "
        "starts p12- or ssh-.",
    )


def _graft_link_item(kept: list[Item]) -> Item:
    link = graft_link()
    item = Item(
        id="graft-link", group=GROUP_TERMINAL, title="graft for Terminal",
        detail="The link Settings › Command-line tool made, to the graft inside Graft.app.",
    )
    try:
        destination = os.readlink(link)
    except OSError:
        destination = None
    if destination is not None and into_graft_app(destination, link=link):
        item.paths, item.present = [link], True
    elif _there(link):
        kept.append(_kept(
            "graft-link", "Another graft for Terminal", link,
            "It isn't a link into Graft.app: a pipx install's, perhaps, or your own.",
            how="pipx uninstall graft-ipa, for a pipx install",
        ))
    return item


def _app_settings() -> Item:
    plist = preferences_plist()
    return Item(
        id="app-settings", group=GROUP_APP, title="Graft.app's own settings",
        detail="What the app remembers between launches: the daily update check's switch "
        "and what it last found, the last version you opened, and choices such as the "
        "delivery mode you last picked.",
        paths=[plist], present=_there(plist), removable=False,
        how=f"Graft.app removes these when you choose them in Help › Remove Graft's Data…; "
        f"in Terminal, defaults delete {APP_BUNDLE_ID}",
    )


def _update_log() -> Item:
    folder = logs_dir()
    paths = [folder] if _is_real_dir(folder) else []
    return Item(
        id="update-log", group=GROUP_APP, title="Update log",
        detail="What Graft wrote each time it updated itself (update.log), and, for an app "
        "built from source, the output of its last rebuild. Nothing reads them but you.",
        paths=paths, present=bool(paths),
    )


def _temp_leftovers() -> Item:
    paths = _temp_entries(lambda name: bool(_TEMP_DIR.fullmatch(name)), directory=True)
    paths += _temp_entries(lambda name: bool(_TEMP_FILE.fullmatch(name)), directory=False)
    marker = rebuild_marker()
    if marker.is_file() and not marker.is_symlink():
        paths.append(marker)
    return Item(
        id="temp", group=GROUP_LEFTOVERS, title="Temporary files left behind",
        detail="Folders a build, a signing or an install made in your temp folder and couldn't "
        "remove because it was stopped, more than an hour old, and a from-source install's "
        "rebuild marker. macOS empties the temp folder itself, eventually.",
        paths=paths, present=bool(paths),
    )


#: config.yaml's keys for folders and files, with what each holds.
_CHOSEN = (
    ("cache_dir", "Staged IPAs"),
    ("built_dir", "Archived builds"),
    ("tweaks_dir", "Tweaks"),
    ("state_dir", "Build history"),
)


def _chosen(raw: dict[str, Any]) -> list[Item]:
    """What config.yaml and the environment point to outside Graft's
    folders: kept, and said so."""
    own = [data_dirs.app_dir(root) for root in (
        data_dirs.xdg_config_home(), data_dirs.xdg_cache_home(),
        data_dirs.xdg_state_home(), data_dirs.xdg_data_home(),
    )]
    candidates: list[tuple[str, str, Path | None, str]] = [
        (key, title, _path_value(raw.get(key)), f"config.yaml's {key}") for key, title in _CHOSEN
    ]
    signing = _section(raw, "signing")
    asc = _section(raw, "app_store_connect")
    candidates += [
        ("signing-cert", "Your .p12", _path_value(signing.get("cert")), "config.yaml's signing.cert"),
        ("signing-profile", "Your provisioning profile", _path_value(signing.get("profile")),
         "config.yaml's signing.profile"),
        ("asc-key", "Your App Store Connect key", _path_value(asc.get("key_file")),
         "config.yaml's app_store_connect.key_file"),
        ("asc-profiles", "Profiles made with App Store Connect",
         _path_value(asc.get("profile_cache_dir")), "config.yaml's app_store_connect.profile_cache_dir"),
        ("frida-ios-dump", "frida-ios-dump", _path_value(os.environ.get("GRAFT_FRIDA_IOS_DUMP")),
         "GRAFT_FRIDA_IOS_DUMP"),
    ]
    items = []
    for key, title, path, where in candidates:
        if path is None or not _there(path) or any(_inside(path, folder) for folder in own):
            continue
        items.append(_kept(
            key, f"{title}, in a place you chose", path,
            f"{where} points here, outside Graft's folders, so Graft leaves it for you to remove.",
        ))
    return items


def survey(config_path: Path) -> Survey:
    """Everything Graft keeps outside Graft.app, with sizes. Changes nothing.

    `config_path` is read leniently, only for the kept folders and the
    password accounts: a missing or broken config.yaml lists everything else
    just the same.
    """
    raw = _lenient_config(config_path)
    kc = _Keychain()
    kept: list[Item] = []
    items = _graft_folders(kept)
    items.append(_legacy_folders())
    items.append(_appstore_signin(kc))
    items.append(_ipatool_cache())
    items.append(_unfairplay_model(raw, kept))
    items.append(_passwords(raw, kc))
    items.append(_graft_link_item(kept))
    items.append(_app_settings())
    items.append(_update_log())
    items.append(_temp_leftovers())
    items += kept + _chosen(raw)
    for item in items:
        if item.paths:
            item.size_bytes = sum(size_of(p) for p in item.paths)
        elif not item.keychain:
            item.size_bytes = 0 if item.present is False else None
    return Survey(items=items, keychain=kc.status)


# --- Choosing --------------------------------------------------------------------------


def choose(found: Survey, spec: str) -> list[Item]:
    """The items `--items` names, in listing order.

    A comma list of ids, and `all`, which is every item that is there,
    removable and on by default; `all,passwords` adds the passwords. An id
    that isn't listed, or names something this command doesn't remove, is
    refused before anything is removed.
    """
    names = [n.strip() for n in spec.split(",") if n.strip()]
    if not names:
        raise ConfigError(
            "--items needs at least one item",
            hint=f"name them as --remove-data lists them, or use --items {ALL}",
        )
    known = {i.id: i for i in found.items}
    wanted: set[str] = set()
    for name in names:
        if name == ALL:
            wanted |= {i.id for i in found.items if i.removable and i.default and i.present is not False}
            continue
        item = known.get(name)
        if item is None:
            ids = ", ".join(i.id for i in found.items if i.removable)
            raise ConfigError(f"--items: there is no item called {name!r}", hint=f"the items are {ids}, or {ALL}")
        if not item.removable:
            raise ConfigError(
                f"--items: {name} isn't something --remove-data removes",
                hint=item.how or "it is in a place you chose; remove it yourself if you want it gone",
            )
        wanted.add(name)
    return [i for i in found.items if i.id in wanted]


# --- Removing --------------------------------------------------------------------------


class RefusedPath(GraftError):
    """A path `remove` must never remove: a root, a home, a shared folder."""


def _never_remove() -> set[str]:
    home = Path.home()
    roots = [
        Path("/"), home, home / "Library", home / "Library" / "Caches",
        home / "Library" / "Application Support", home / "Library" / "Preferences",
        home / "Library" / "Logs",
        home / ".local", home / ".local" / "bin", home / ".local" / "share",
        home / ".local" / "state", home / ".config", home / ".cache", temp_root(),
        data_dirs.xdg_config_home(), data_dirs.xdg_cache_home(),
        data_dirs.xdg_state_home(), data_dirs.xdg_data_home(),
    ]
    out: set[str] = set()
    for root in roots:
        out.add(os.path.normpath(os.path.abspath(root)))
        out.add(os.path.realpath(root))
    return out


def _guard(path: Path) -> None:
    """The last look before anything is removed: a path this module built
    from a root, never the root. Every item's paths are made by joining a
    name to one, so this fails only if a root itself came out wrong (an XDG
    variable set to `/`, or to a home folder)."""
    plain = os.path.normpath(os.path.abspath(path))
    forbidden = _never_remove()
    if not path.is_absolute() or len(Path(plain).parts) < 3 or plain in forbidden:
        raise RefusedPath(f"refusing to remove {path}")
    if not os.path.islink(path) and os.path.realpath(path) in forbidden:
        raise RefusedPath(f"refusing to remove {path}: it is {os.path.realpath(path)}")


def _why(error: BaseException) -> str:
    if isinstance(error, OSError):
        return error.strerror or str(error) or type(error).__name__
    return str(error) or type(error).__name__


def _rmtree(path: Path) -> str | None:
    """Remove the folder `path` and everything in it, never following a
    link. A folder inside made read-only (Go's module cache does that, and
    so does a copied app bundle) refuses to have anything removed from it,
    so a first pass that leaves anything is followed by one that makes every
    folder inside writable by its owner first: they are all going."""
    errors: list[str] = []

    def collect(_function: Any, failed: str, error: Any) -> None:
        exc = error if isinstance(error, BaseException) else error[1]
        if not isinstance(exc, FileNotFoundError):
            errors.append(f"{failed}: {_why(exc)}")

    def once() -> None:
        errors.clear()
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=collect)
        else:  # pragma: no cover - 3.11
            shutil.rmtree(path, onerror=collect)

    once()
    if not _there(path):
        return None
    for folder, _dirs, _files in os.walk(path, followlinks=False):
        try:
            mode = os.lstat(folder).st_mode
            if stat.S_ISDIR(mode) and mode & stat.S_IRWXU != stat.S_IRWXU:
                os.chmod(folder, mode | stat.S_IRWXU)
        except OSError:
            continue
    once()
    if not _there(path):
        return None
    return errors[0] if errors else "it is still there"


def remove_path(path: Path) -> str | None:
    """Remove `path`: a link or a file is unlinked (a link's destination is
    never touched), a folder removed with everything in it. None once it is
    gone, else why it isn't."""
    _guard(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return _why(e)
    if stat.S_ISDIR(st.st_mode):
        return _rmtree(path)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return None
    except OSError as e:
        return _why(e)
    return None


def _remove_paths(outcome: Outcome, paths: Iterable[Path], *, dry_run: bool) -> None:
    for path in paths:
        if not _there(path):
            continue
        size = size_of(path)
        if dry_run:
            outcome.removed.append(str(path))
            outcome.freed_bytes += size
            continue
        try:
            why = remove_path(path)
        except RefusedPath as e:
            outcome.failed.append((str(path), e.message))
            continue
        if why is None:
            outcome.removed.append(str(path))
            outcome.freed_bytes += size
        else:
            outcome.failed.append((str(path), why))
            outcome.freed_bytes += max(0, size - size_of(path))


def _keychain_refused(outcome: Outcome, item: Item) -> bool:
    """Fail `item` whole, touching nothing, when the keychain is locked."""
    if not keychain.login_keychain_locked():
        return False
    targets = [f"{s} {a}" for s, a in item.keychain] or [item.title]
    outcome.failed += [(what, LOCKED_REASON) for what in targets]
    return True


def _remove_signin(item: Item, *, dry_run: bool) -> Outcome:
    outcome = Outcome(item.id)
    if dry_run:
        outcome.removed += [f"{s} {a}" for s, a in item.keychain]
        _remove_paths(outcome, item.paths, dry_run=True)
        return outcome
    if _keychain_refused(outcome, item):
        return outcome
    try:
        gone = appstore_account.clear_items(appstore_account.IPATOOL_ITEMS)
    except appstore_account.ClearFailed as e:
        # The cookies stay too: half a sign-in is worse than a whole one.
        outcome.failed.append((f"{appstore_account.IPATOOL_SERVICE} {e.item}", NO_ANSWER_REASON))
        return outcome
    outcome.removed += [f"{appstore_account.IPATOOL_SERVICE} {name}" for name in gone]
    # Graft's record of the sign-in: it vouches for items that are gone.
    try:
        appstore_account.forget_signed_in()
    except OSError:
        pass
    _remove_paths(outcome, ipatool_state_dirs(), dry_run=False)
    return outcome


def _remove_passwords(item: Item, *, dry_run: bool) -> Outcome:
    outcome = Outcome(item.id)
    if dry_run:
        outcome.removed += [f"{s} {a}" for s, a in item.keychain]
        return outcome
    if _keychain_refused(outcome, item):
        return outcome
    failed: dict[str, str] = {}

    def delete(service: str, account: str) -> bool:
        deleted = keychain.delete_item(service, account)
        what = f"{service} {account}"
        if deleted is None:
            failed[what] = NO_ANSWER_REASON
        elif deleted and what not in outcome.removed:
            outcome.removed.append(what)
        return deleted is not None

    for service, account in item.keychain:
        delete(service, account)
    # Any Graft has no record of: the first under each service, while it is
    # one of Graft's and the keychain removes it.
    for service in (keychain.SERVICE, keychain.LEGACY_SERVICE):
        for _ in range(_SWEEP_LIMIT):
            look = keychain.first_account(service)
            if look.no_answer:
                failed.setdefault(f"{service} items", NO_ANSWER_REASON)
                break
            if not look.exists or not look.account or not _GRAFT_ACCOUNT.fullmatch(look.account):
                break
            if f"{service} {look.account}" in failed or not delete(service, look.account):
                break
    outcome.failed += list(failed.items())
    return outcome


def _remove_temp(item: Item, *, dry_run: bool) -> Outcome:
    outcome = Outcome(item.id)
    _remove_paths(outcome, item.paths, dry_run=dry_run)
    folder = rebuild_marker().parent
    if not dry_run and str(rebuild_marker()) in outcome.removed:
        # The marker's own folder, only when nothing else is in it.
        try:
            folder.rmdir()
        except OSError:
            pass
    return outcome


def remove(items: list[Item], *, dry_run: bool) -> list[Outcome]:
    """Remove each of `items`, keychain items first. In a dry run, nothing is
    touched and each outcome says what would go. One item's failure stops
    nothing else."""
    order = sorted(items, key=lambda i: 0 if i.keychain or i.id == "appstore-signin" else 1)
    outcomes: dict[str, Outcome] = {}
    for item in order:
        if item.id == "appstore-signin":
            outcome = _remove_signin(item, dry_run=dry_run)
        elif item.id == "passwords":
            outcome = _remove_passwords(item, dry_run=dry_run)
        elif item.id == "temp":
            outcome = _remove_temp(item, dry_run=dry_run)
        else:
            outcome = Outcome(item.id)
            _remove_paths(outcome, item.paths, dry_run=dry_run)
        outcomes[item.id] = outcome
    return [outcomes[i.id] for i in items]


def human_size(n: int | None) -> str:
    """`312 MB`: 1000-based, as Finder and the app's `ByteCountFormatter`."""
    if n is None:
        return "?"
    if n < 1000:
        return f"{n} bytes" if n != 1 else "1 byte"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1000
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if n >= 100 else f"{n:.1f} {unit}"
    return f"{n:.1f} TB"  # pragma: no cover - the loop returns
