"""Capabilities an app asks for that the profile it's signed with can't grant.

zsign signs every bundle with the profile's entitlements, not the ones the
app was built to ask for, so whatever the profile doesn't grant is gone on
the device. For most entitlements that is harmless, and two of them zsign
fills in from the profile itself (`application-identifier`,
`keychain-access-groups`). The ones in `CAPABILITIES` are different: each
needs a capability enabled on the App ID the profile was made for, and a
wildcard App ID can have none of them.

Found on a device: a sideloaded app signed with the wildcard profile died at
launch with SIGTRAP in its shared-container getter. Its binary asks for
`com.apple.security.application-groups`; signed without one, the app found
no group container and trapped. The same IPA launched inside LiveContainer on
the same phone and reached its login screen, so LiveContainer evidently
answers a guest app's app-group lookup itself. That is seen, not read in
LiveContainer's source (which isn't in this repo), hence "usually" in the
warning, and only for App Groups: LiveContainer can't give a guest app push,
iCloud or any of the others.

Two kinds of gap, warned differently (`warnings`). App Groups and iCloud
containers are looked up at launch, and an app that gets nil back usually
traps: "it will probably crash at launch". Push, Associated Domains, Sign in
with Apple and HealthKit fail when the feature is used, and nearly every App
Store binary asks for push, which no wildcard profile can grant: one line
saying "those features won't work", so the crash warning keeps its weight.

App Groups are checked group by group: a profile that grants some groups but
not the app's own still leaves the app without its container, and the
warning names the groups missing. iCloud containers are checked the same
way, container by container: a profile with iCloud on that grants
key-value storage, an empty container list or the user's own container
still leaves an app that looks up its vendor's container without it (the
try14 delta audit's d14-cli-4). A profile's `*` matches as Apple's does, at
the end of a value (`group.com.example.*`).

A warning, never a refusal. Nothing here suggests `auto_profile`: App Group
assignment can't be done through the App Store Connect API (`apple_portal`).

The requested entitlements are read from the IPA's own binaries, the main
executable and every extension's, in place (`macho.entitlements_from` on
`ZipFile.open`), before anything is signed. Anything that can't be read, the
IPA, a bundle, a binary or the profile, is no warning: the build's own
stages report a broken IPA or profile, and this only adds a finding.
"""

from __future__ import annotations

import fnmatch
import logging
import plistlib
import shlex
import zipfile
import zlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from . import macho, mobileprovision

#: Every capability this checks: the name developer.apple.com gives it, and
#: the entitlement keys (`fnmatch` patterns) that ask for it. A profile
#: grants a capability when it lists any of its keys, except App Groups and
#: iCloud containers, which are compared value by value (`missing`); an app
#: asks for one when one of its binaries does, with a value that isn't empty
#: or false.
#: `keychain-access-groups` and `application-identifier` are not here: zsign
#: writes those from the profile.
CAPABILITIES: dict[str, tuple[str, ...]] = {
    "App Groups": ("com.apple.security.application-groups",),
    "Push Notifications": ("aps-environment",),
    "iCloud": ("com.apple.developer.icloud-container-identifiers", "com.apple.developer.ubiquity-*"),
    "Associated Domains": ("com.apple.developer.associated-domains",),
    "HealthKit": ("com.apple.developer.healthkit",),
    "Sign in with Apple": ("com.apple.developer.applesignin",),
}
APP_GROUPS = "App Groups"
GROUPS_KEY = "com.apple.security.application-groups"
ICLOUD = "iCloud"
#: The iCloud keys an app looks a container up by at launch. Asked for by
#: one of them, iCloud is a likely crash; by `ubiquity-kvstore-identifier`
#: alone it is key-value storage, which an app without it reads as an empty
#: store: a feature that won't work (the whole-diff review's cli-12).
ICLOUD_CONTAINER_KEYS = (
    "com.apple.developer.icloud-container-identifiers",
    "com.apple.developer.ubiquity-container-identifiers",
)
#: How a gap only the Watch app has is said (cli-11).
ON_THE_WATCH = "for its Watch app"
#: The capabilities an app looks up at launch and usually can't run without.
#: The rest lose a feature.
LIKELY_CRASH = frozenset({APP_GROUPS, "iCloud"})
#: How many missing groups a warning names before "and N more".
MAX_GROUPS_NAMED = 4

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Gap:
    """One capability the app asks for and the profile doesn't grant."""

    name: str
    #: For App Groups: the groups asked for that the profile doesn't grant;
    #: for iCloud, the containers.
    groups: tuple[str, ...] = ()
    #: Said after the name: "(key-value storage only)", `ON_THE_WATCH`.
    note: str = ""
    #: Whether the app usually can't run without it; None: as `LIKELY_CRASH`
    #: has it for the capability.
    crash: bool | None = None

    @property
    def likely_crash(self) -> bool:
        return self.name in LIKELY_CRASH if self.crash is None else self.crash

    def label(self) -> str:
        said = self.name
        if self.groups:
            shown = [_printable(g) for g in self.groups[:MAX_GROUPS_NAMED]]
            if len(self.groups) > MAX_GROUPS_NAMED:
                shown.append(f"{len(self.groups) - MAX_GROUPS_NAMED} more")
            said = f"{self.name} ({_listed(shown)})"
        return f"{said} {self.note}" if self.note else said

#: Where an app's delivery is chosen in Graft.app (EditAppSheet's picker).
APP_DELIVERY_PICKER = "Edit App \u203a Delivery in Graft.app"

#: Far above any real app's bundles (the app and its extensions).
MAX_BUNDLES = 256
#: Far above any real Info.plist, which is a few kilobytes.
MAX_INFO_PLIST = 4 * 1024 * 1024


def asked_for(entitlements: Mapping[str, Any]) -> set[str]:
    """The `CAPABILITIES` names `entitlements` ask for (or, a profile's, grant)."""
    return {
        name
        for name, patterns in CAPABILITIES.items()
        for key, value in entitlements.items()
        if value and any(fnmatch.fnmatchcase(str(key), p) for p in patterns)
    }


def _strings(value: Any) -> list[str]:
    """An entitlement's value as a list of strings: a string alone, or the
    strings of a list."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str)]
    return []


def grants(granted: str, asked: str) -> bool:
    """Whether a profile's value `granted` covers `asked`: the same value,
    or a prefix ending in `*`, which is how a profile's wildcards match."""
    return granted == asked or (granted.endswith("*") and asked.startswith(granted[:-1]))


def missing(requested: Iterable[Mapping[str, Any]], granted: Mapping[str, Any]) -> list[Gap]:
    """What any of `requested` asks for and `granted` doesn't, in
    `CAPABILITIES` order. App Groups by group."""
    requested = list(requested)
    wanted: set[str] = set()
    for entitlements in requested:
        wanted |= asked_for(entitlements)
    have = asked_for(granted)
    gaps = []
    for name in CAPABILITIES:
        if name not in wanted:
            continue
        if name == APP_GROUPS:
            asked = sorted({g for ents in requested for g in _strings(ents.get(GROUPS_KEY))})
            given = _strings(granted.get(GROUPS_KEY))
            lacking = tuple(g for g in asked if not any(grants(x, g) for x in given))
            if lacking or (not asked and name not in have):
                gaps.append(Gap(name, lacking))
        elif name == ICLOUD:
            if (gap := _icloud_gap(requested, granted, name in have)) is not None:
                gaps.append(gap)
        elif name not in have:
            gaps.append(Gap(name))
    return gaps


def _icloud_gap(requested: list[Mapping[str, Any]], granted: Mapping[str, Any], has_icloud: bool) -> Gap | None:
    """iCloud's gap, or None.

    An app that looks containers up at launch (`ICLOUD_CONTAINER_KEYS`)
    needs each of its own: any iCloud key in the profile used to count, so a
    profile with key-value storage, an empty container list or the user's
    own container gave no warning for an app after its vendor's container
    (d14-cli-4). Key-value storage alone is still granted by any iCloud key,
    as before: zsign signs the profile's store in, and the app reads it.
    """
    asks_containers = any(ents.get(key) for ents in requested for key in ICLOUD_CONTAINER_KEYS)
    if not asks_containers:
        return None if has_icloud else Gap(ICLOUD, note="(key-value storage only)", crash=False)
    asked = sorted({c for ents in requested for key in ICLOUD_CONTAINER_KEYS for c in _strings(ents.get(key))})
    if not asked:
        # Asked for by a value that names no container: nothing to compare.
        return None if has_icloud else Gap(ICLOUD)
    given = [c for key in ICLOUD_CONTAINER_KEYS for c in _strings(granted.get(key))]
    lacking = tuple(c for c in asked if not any(grants(x, c) for x in given))
    return Gap(ICLOUD, lacking) if lacking else None


def _watch_only(on_watch: list[Gap], on_phone: list[Gap]) -> list[Gap]:
    """The gaps only the Watch app's extensions have: said as the Watch's
    (`ON_THE_WATCH`), and never a likely crash. A Watch extension that finds
    no group container is no reason the iPhone app won't launch, and
    counting it so gave a false crash warning (cli-11)."""
    phone = {g.name: g for g in on_phone}
    said: list[Gap] = []
    for gap in on_watch:
        note = f"{gap.note} {ON_THE_WATCH}" if gap.note else ON_THE_WATCH
        if gap.name == APP_GROUPS and gap.groups:
            known = phone[APP_GROUPS].groups if APP_GROUPS in phone else ()
            lacking = tuple(g for g in gap.groups if g not in known)
            if lacking:
                said.append(Gap(gap.name, lacking, note=note, crash=False))
        elif gap.name not in phone:
            said.append(Gap(gap.name, gap.groups, note=note, crash=False))
    return said


def missing_for(ipa: Path, profile: Path, *, extensions: bool = True, watch: bool = True) -> list[Gap]:
    """What `ipa`'s binaries ask for that `profile` doesn't grant: the
    iPhone app's gaps, then those only its Watch app has (`_watch_only`).

    Never raises: this only adds a warning, and an IPA or profile it can't
    read (however it fails) is the build's own stages' to report. A
    `--dry-run --skip-decrypt-check` reads nothing else, so an error here
    would have been the build's only failure. `extensions=False` leaves out
    the app's extensions, for a build whose cyan removes them (`-e`), and
    `watch=False` the Watch app's, for one whose cyan removes the Watch app
    (`-w`, `cyan_runner.removes_watch`).
    """
    try:
        granted = mobileprovision.decode(profile).get("Entitlements")
        granted = granted if isinstance(granted, dict) else {}
        on_phone, on_watch = _requested_by_device(ipa, extensions=extensions, watch=watch)
        phone_gaps = missing(on_phone, granted)
        return phone_gaps + _watch_only(missing(on_watch, granted), phone_gaps)
    except Exception as e:  # a finding, never a failure
        _log.debug("capability check skipped for %s: %s: %s", ipa, type(e).__name__, e)
        return []


def requested_entitlements(ipa: Path, *, extensions: bool = True, watch: bool = True) -> list[dict[str, Any]]:
    """The entitlements the app's executable and each extension's are signed
    to ask for, read out of `ipa` without extracting it. A bundle whose
    executable can't be read gives nothing; so does an IPA that can't be."""
    on_phone, on_watch = _requested_by_device(ipa, extensions=extensions, watch=watch)
    return on_phone + on_watch


def _requested_by_device(
    ipa: Path, *, extensions: bool, watch: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`requested_entitlements`, as (the iPhone app's, its Watch app's)."""
    on_phone: list[dict[str, Any]] = []
    on_watch: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(ipa) as zf:
            for member, for_watch in _executables(zf, extensions=extensions, watch=watch):
                if ents := _entitlements_of(zf, member):
                    (on_watch if for_watch else on_phone).append(ents)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile):
        return [], []
    return on_phone, on_watch


def _executables(
    zf: zipfile.ZipFile, *, extensions: bool = True, watch: bool = True,
) -> list[tuple[zipfile.ZipInfo, bool]]:
    """The app's executable, then every extension's (unless `extensions` is
    False), each named by its bundle's Info.plist and present in the
    archive, with whether it is the Watch app's (under `<app>/Watch/`),
    which `watch=False` leaves out."""
    names = zf.namelist()
    app = next((
        name[: -len("/Info.plist")] for name in names
        if name.startswith("Payload/") and name.endswith(".app/Info.plist")
        and name.count("/") == 2
    ), None)
    if app is None:
        return []
    appexes = sorted(
        name[: -len("/Info.plist")] for name in names
        if name.startswith(f"{app}/") and name.endswith(".appex/Info.plist")
    ) if extensions else []
    found: list[tuple[zipfile.ZipInfo, bool]] = []
    for bundle in [app, *appexes][:MAX_BUNDLES]:
        for_watch = bundle.startswith(f"{app}/Watch/")
        if for_watch and not watch:
            continue
        executable = _executable_name(zf, f"{bundle}/Info.plist")
        if executable is None:
            continue
        try:
            found.append((zf.getinfo(f"{bundle}/{executable}"), for_watch))
        except KeyError:
            continue
    return found


def _executable_name(zf: zipfile.ZipFile, info_plist: str) -> str | None:
    """`CFBundleExecutable`, or None. Looked up in the archive by name, never
    on disk, so a name like `../x` reaches no further than a member that is
    literally called that."""
    try:
        member = zf.getinfo(info_plist)
        if member.file_size > MAX_INFO_PLIST:
            return None
        with zf.open(member) as f:
            info = plistlib.loads(f.read(MAX_INFO_PLIST + 1))
    except Exception:
        # A downloaded file's plist, XML or binary; any way it fails to parse
        # (a binary one can recurse) is a bundle that names nothing.
        return None
    name = info.get("CFBundleExecutable") if isinstance(info, dict) else None
    return name if isinstance(name, str) and name else None


def _entitlements_of(zf: zipfile.ZipFile, member: zipfile.ZipInfo) -> dict[str, Any]:
    try:
        with zf.open(member) as f:
            return macho.entitlements_from(f, PurePosixPath(member.filename).name, member.file_size)
    except (macho.MachOError, OSError, EOFError, zipfile.BadZipFile, zlib.error,
            NotImplementedError, RuntimeError):
        # Not a Mach-O, a broken signature, a member that won't decompress
        # (or is encrypted, or uses a method zipfile lacks).
        return {}


def warnings(app_label: str, app_name: str, gaps: list[Gap]) -> list[str]:
    """The warnings for `gaps`: one for those the app will probably crash
    without, one for features that won't work. `app_label` is how the app
    reads in a sentence; `app_name` is its name in config.yaml."""
    crash = [g for g in gaps if g.likely_crash]
    feature = [g for g in gaps if not g.likely_crash]
    said = []
    if crash:
        text = (f"{app_label} asks for {_listed([g.label() for g in crash])}, which the "
                "profile doesn't grant; it will probably crash at launch.")
        if any(g.name == APP_GROUPS for g in crash):
            text += (
                " Apps like this usually run under LiveContainer instead (delivery: "
                "livecontainer): "
                f"`graft --set-app-options --app {shlex.quote(app_name)} --delivery livecontainer`, "
                f"or {APP_DELIVERY_PICKER}."
            )
        said.append(text)
    if feature:
        said.append(f"{app_label} asks for {_listed([g.label() for g in feature])}, which the "
                    "profile doesn't grant; those features won't work.")
    return said


def _printable(text: str) -> str:
    """A group id out of the IPA, without control characters: it is printed
    to a terminal."""
    return "".join(c for c in text if c.isprintable())


def _listed(names: list[str]) -> str:
    if len(names) <= 2:
        return " and ".join(names)
    return ", ".join(names[:-1]) + f" and {names[-1]}"
