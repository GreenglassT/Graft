"""Graft updating itself: `graft --download-update` and `graft --install-update`.

**Download.** When GitHub's latest release (`release_check`) is newer than the
running Graft.app, `--download-update` downloads that release's zip of this
build of Graft into Graft's cache (`updates_dir`): Graft.zip for the Apple
silicon build, Graft-Intel.zip for the Intel one (`own_flavor`, `ASSETS`). A
release without it has no update for this Mac (`NOT_FOR_THIS_MAC`); the
other build's zip is never fetched in its place. The file is nothing until it
has passed every check below, cheapest first:

- its size and SHA-256 are the ones GitHub's API lists for the asset (its
  `digest`, "sha256:…");
- every entry stays inside one Graft.app: no absolute path, no "..", no link
  that points out of the bundle and no entry stored through a link
  (`check_listing`);
- ditto unpacks it into a folder of its own, owner-only;
- its Info.plist names Graft's bundle id, and a CFBundleShortVersionString
  newer than the running app's;
- its Info.plist's LSMinimumSystemVersion is this Mac's macOS or older
  (`check_macos`). One that needs a newer macOS is no update for this Mac
  (`NOT_FOR_THIS_MAC`), like a release without this build's zip, and this
  Mac stays on the Graft it has. That zip isn't downloaded again until this
  Mac has that macOS (`MACOS_NOTE`), and `--check-update` offers it no more
  (`noted_refusal`);
- it is the same build as the running app (`check_architecture`): its
  Info.plist says so (`FLAVOR_KEY`), and its main executable and its
  embedded Python have code for that processor, read off their Mach-O
  headers (`macho.architectures`), never with `lipo` or `file`;
- `codesign --verify --deep --strict`: every file is as it was signed;
- `requirement(team)`: a Developer ID certificate of the team that signed
  the Graft.app this CLI runs from (`own_team`) AND Graft's bundle id, so
  another app from the same team doesn't pass, nor Graft from anyone else;
- a notarization ticket stapled to it (`codesign -dvv`), which Gatekeeper
  accepts (`spctl -a -t exec`: "Notarized Developer ID").

Any failure deletes what was downloaded and unpacked, and the app shows the
release page instead.

**Whose updates.** The team an update must be signed by is the running
Graft's own, read from its signature at each download and install
(`own_team`), so the source names no team and a fork signed by someone else
updates only from that someone. A Graft with no Developer ID team (ad hoc,
or a development build signed "Apple Development") refuses to update
itself, and says why.

Not `xcrun stapler validate`: on a Mac without the developer tools,
`/usr/bin/stapler` and `xcrun` are stubs that put up macOS's "install the
command line developer tools" dialog. codesign reads the stapled ticket, and
spctl is Gatekeeper checking it against the code, with no developer tools.
spctl checks the ticket on the Mac when it is stapled, which is why the
stapled check comes first: a download without one never gets as far as
asking Apple.

**Install.** `--install-update` checks again that the app can be replaced
where it is, then starts `HELPER`, a /bin/sh script that outlives Graft. It
waits for Graft to quit, moves the new app beside the old one, checks it there
once more (signature, requirement, notarization, version, build), moves the old
Graft.app to the Trash and the new one into its place, registers it with
Launch Services, and opens it when asked to. A failure at any step leaves or
puts back the old app. It writes what it did to `~/Library/Logs/Graft/update.log`.

/bin/sh rather than Python because the CLI's Python lives inside the bundle
the helper moves to the Trash; the script is passed as an argument, so
nothing it runs is read from that bundle.

What this can't see: whether macOS lets the helper replace the app. Graft's
own location can be writable and a privacy setting (App Management) still
refuse the move. The helper then puts everything back and logs it, and the
app, next launched, finds it is still the old version and says so
(`UpdateStore`).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import plistlib
import posixpath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import data_dirs, macho, release_check
from .child_env import child_env
from .data_dirs import cache_dir
from .enrollment import DEVELOPER_ID_CA, DEVELOPER_ID_LEAF
from .errors import GraftError
from .trash import default_trash_dir

BUNDLE_ID = "com.greenglasst.graft"
APP_NAME = "Graft.app"

#: Info.plist's word for which download a Graft.app is: "arm64" or "x86_64".
#: The release script writes it before signing, so it is sealed with the
#: rest of the plist. It names the bundle's build, not the processor of the
#: Mac it runs on, nor of the process asking: the app's own executable is
#: universal in both builds, so a Swift process on Apple silicon runs as
#: arm64 even inside the Intel build, and only the bundle can say which one
#: it is.
FLAVOR_KEY = "GraftBundleArch"
#: Info.plist's word for the oldest macOS a Graft.app runs on ("14.0"), which
#: Xcode writes from the app's MACOSX_DEPLOYMENT_TARGET.
MACOS_KEY = "LSMinimumSystemVersion"
ARM64 = "arm64"
X86_64 = "x86_64"
#: Each build's zip on a release. Graft.zip keeps meaning arm64: the 2.0.2 and
#: 2.1 updaters ask for it by that name.
ASSETS = {ARM64: "Graft.zip", X86_64: "Graft-Intel.zip"}
#: What each build is for, in a sentence.
MACS = {ARM64: "Macs with Apple silicon", X86_64: "Intel Macs"}

#: A Developer ID team, as codesign names it: ten capitals and digits.
TEAM_RE = re.compile(r"[A-Z0-9]{10}", re.ASCII)
#: Where Graft.app keeps the interpreter its CLI runs on, under the bundle:
#: Contents/Resources/GraftPython/bin/python3.13 (`own_bundle`).
EMBEDDED_PYTHON = ("Contents", "Resources", "GraftPython", "bin")
#: The embedded interpreter's name there: python3.<minor>. A download's is
#: found by this, not by this release's own minor version, so the first
#: release on a newer Python is still read (`embedded_interpreter`).
INTERPRETER_RE = re.compile(r"python3\.[0-9]{1,3}", re.ASCII)


def requirement(team: str) -> str:
    """What a downloaded Graft must satisfy, for codesign's `-R`: Apple's
    Developer ID chain, `team`, and Graft's identifier. The team alone would
    let any other app from that team through; the identifier alone, anyone's
    app that called itself Graft. The Developer ID markers keep out an Apple
    Development or Mac App Store certificate of the same team. `team` must
    be one (`TEAM_RE`): it goes into the requirement's text."""
    if TEAM_RE.fullmatch(team) is None:
        raise ValueError(f"not a Developer ID team: {team!r}")
    return (
        f'anchor apple generic and identifier "{BUNDLE_ID}" '
        f'and certificate leaf[subject.OU] = "{team}" '
        f"and certificate 1[field.{DEVELOPER_ID_CA}] exists "
        f"and certificate leaf[field.{DEVELOPER_ID_LEAF}] exists"
    )

#: Where a release's files are served from. The asset's URL must start with
#: this; the redirect to GitHub's download host is followed from there.
DOWNLOAD_PREFIX = "https://github.com/GreenglassT/Graft/releases/download/"
#: The schemes a download may use, on every hop. A test serving fixtures from
#: a local server adds "http"; nothing else does.
SCHEMES = frozenset({"https"})
MAX_REDIRECTS = 5
#: Graft.zip is about 150 MB. Anything past these is not one.
MAX_ZIP_BYTES = 1 << 30
MAX_UNPACKED_BYTES = 3 << 30
#: Seconds to connect, then between bytes.
TIMEOUT = (10, 60)
HEADERS = {
    # Generic, as for the check (`release_check.USER_AGENT`).
    "User-Agent": release_check.USER_AGENT,
    "Accept": "application/octet-stream",
    # The bytes as GitHub stores them: they are what the digest is of.
    "Accept-Encoding": "identity",
}
#: How long a download waits for another one to finish with the cache folder.
LOCK_WAIT = 30.0
#: The note a download refused for the macOS it needs leaves in
#: `updates_dir`: the release's version, the SHA-256 GitHub lists for its zip,
#: and the macOS that zip needs. The app asks again at each launch, and Check
#: for Updates… offers Try Again, as for a release without this build's zip;
#: the note answers each of them before the zip is downloaded again. A newer
#: macOS, another zip or another release downloads afresh, and the next
#: download that goes ahead deletes the note (`_clear`).
MACOS_NOTE = "needs-macos.json"

CODESIGN = "/usr/bin/codesign"
SPCTL = "/usr/sbin/spctl"
DITTO = "/usr/bin/ditto"
PLISTBUDDY = "/usr/libexec/PlistBuddy"
OPEN = "/usr/bin/open"
MV = "/bin/mv"
LSREGISTER = (
    "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "LaunchServices.framework/Support/lsregister"
)
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
#: Seconds any one of the tools above may take. A deep verify of Graft.app
#: takes about a second.
TOOL_TIMEOUT = 300

#: The helper's wait for Graft to quit, in fifths of a second: a minute. A
#: Graft still running after that keeps its old version, and the helper
#: changes nothing.
WAIT_TICKS = 300

#: Why an update failed, the events' `reason`. The first four are the
#: check's own (`release_check`).
OFFLINE = release_check.OFFLINE
TIMED_OUT = release_check.TIMED_OUT
HTTP = release_check.HTTP
MALFORMED = release_check.MALFORMED
NOT_NEWER = "not-newer"
NO_ASSET = "no-asset"
#: The latest release has no zip of this build (`ASSETS`), or its zip needs a
#: newer macOS than this Mac has (`NeedsNewerMacOS`): no update for this Mac.
NOT_FOR_THIS_MAC = "not-for-this-mac"
DIGEST = "digest"
ARCHIVE = "archive"
BUNDLE = "bundle"
#: The download is another build of Graft, or its code isn't for this build's
#: processor (`check_architecture`).
ARCHITECTURE = "architecture"
SIGNATURE = "signature"
REQUIREMENT_FAILED = "requirement"
NOTARIZATION = "notarization"
TRANSLOCATED = "translocated"
NOT_WRITABLE = "not-writable"
NOT_GRAFT = "not-graft"
NOT_STAGED = "not-staged"
#: The running Graft has no Developer ID team to trust updates from.
NO_TEAM = "no-team"
#: The running Graft's Info.plist and its own Python disagree about which
#: build it is (`own_flavor`), so it can't tell which zip is its update.
MIXED_BUILD = "mixed-build"
DISK = "disk"
BUSY = "busy"
HELPER_FAILED = "helper"

#: The verification steps after the download (whose SHA-256 is checked as
#: it arrives), the `update-progress` event's `check`, in order.
CHECKS = ("archive", "bundle", "architecture", "signature", "requirement", "notarization")


class UpdateError(GraftError):
    """The update can't go ahead. `reason` is one of the names above; the
    hint is where to download it by hand."""

    def __init__(self, message: str, *, reason: str, url: str | None = None,
                 hint: str | None = None) -> None:
        self.url = url or release_check.RELEASES_PAGE + "/latest"
        super().__init__(message, hint=hint or f"download it from {self.url}")
        self.reason = reason


class NeedsNewerMacOS(UpdateError):
    """The update needs a newer macOS than this Mac has (`check_macos`):
    no update for this Mac (`NOT_FOR_THIS_MAC`). `minimum` is the macOS it
    needs, as its Info.plist says it."""

    def __init__(self, *, version: str, minimum: str, macos: str, current: str,
                 url: str | None = None) -> None:
        super().__init__(
            f"Graft {version} needs macOS {minimum} or later, and this Mac has macOS {macos}, "
            f"so there's no update for this Mac. It stays on Graft {current}.",
            reason=NOT_FOR_THIS_MAC, url=url,
            hint=f"Graft {version} can update this Mac once it has macOS {minimum} or later",
        )
        self.minimum = minimum


@dataclass(frozen=True)
class Asset:
    """The release's zip of this build (`ASSETS`), as GitHub's API lists it."""

    #: Its file name: Graft.zip or Graft-Intel.zip.
    name: str
    url: str
    size: int
    #: Lowercase hex, without GitHub's "sha256:".
    sha256: str


@dataclass(frozen=True)
class Staged:
    """A download that passed every check: `path` is the Graft.app to install."""

    version: str
    path: Path
    #: The release's page, for the app's link if the install fails later.
    url: str


@dataclass(frozen=True)
class Scheduled:
    version: str
    log: Path


@dataclass(frozen=True)
class Ran:
    """What one tool run said: codesign and spctl say it on stderr, so
    `output` is both streams."""

    returncode: int
    output: str


#: Runs a tool and says what it said. The real one is `run_tool`; a test
#: passes its own.
Run = Callable[[list[str]], Ran]
Progress = Callable[[dict[str, Any]], None]


def run_tool(argv: list[str]) -> Ran:
    """Run one of the system tools above, with nothing on stdin, the
    environment without Graft's secrets (`child_env`), and only the system's
    folders on PATH. A tool that won't start or won't finish reads as a
    failure, which every caller treats as "not verified"."""
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            errors="replace", timeout=TOOL_TIMEOUT, check=False,
            env=child_env({"PATH": SYSTEM_PATH, "LANG": "en_US.UTF-8"}),
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return Ran(127, str(e))
    return Ran(done.returncode, done.stdout + done.stderr)


def updates_dir() -> Path:
    """Where downloads are kept until they are installed: owner-only, under
    Graft's cache."""
    return cache_dir() / "updates"


def log_path() -> Path:
    """The helper's log, beside the app's own rebuild log."""
    return Path.home() / "Library" / "Logs" / "Graft" / "update.log"


# --- The release ------------------------------------------------------------


_DIGEST_RE = re.compile(r"sha256:([0-9a-f]{64})", re.ASCII)


def release_asset(data: dict[str, Any], *, flavor: str, url: str | None = None) -> Asset:
    """The release's zip of the `flavor` build (`ASSETS`), from GitHub's
    answer about it: its download URL (under `DOWNLOAD_PREFIX`), size and
    digest. Raises `UpdateError` when any of them is missing or isn't one.

    Only that build's zip, by its exact name: a release without it is
    `NOT_FOR_THIS_MAC`, never the other build's zip in its place, which an
    Intel Mac couldn't run and which would turn an Apple silicon Mac's
    Intel build into the other one. (An Intel build on Apple silicon is
    told in the app to download Graft.zip itself; its updater keeps it the
    build it is.)"""
    name = ASSETS[flavor]
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise UpdateError(
            "GitHub's answer about the latest release didn't list its files.",
            reason=MALFORMED, url=url,
        )
    found = [a for a in assets if isinstance(a, dict) and a.get("name") == name]
    if not found:
        raise UpdateError(
            f"The latest release has no {name}, the download for {MACS[flavor]}, so there's "
            "no update for this Mac yet.",
            reason=NOT_FOR_THIS_MAC, url=url,
        )
    if len(found) != 1:
        raise UpdateError(
            f"The latest release lists {name} more than once, so Graft can't tell which to "
            "download.",
            reason=NO_ASSET, url=url,
        )
    (asset,) = found
    link, size, digest = asset.get("browser_download_url"), asset.get("size"), asset.get("digest")
    if not (isinstance(link, str) and link.startswith(DOWNLOAD_PREFIX) and link.endswith("/" + name)):
        raise UpdateError(
            f"The latest release's {name} isn't on Graft's release page.",
            reason=NO_ASSET, url=url,
        )
    if asset.get("state", "uploaded") != "uploaded" or isinstance(size, bool) \
            or not isinstance(size, int) or not 0 < size <= MAX_ZIP_BYTES:
        raise UpdateError(
            f"GitHub lists the latest release's {name} without a usable size.",
            reason=NO_ASSET, url=url,
        )
    match = _DIGEST_RE.fullmatch(digest) if isinstance(digest, str) else None
    if match is None:
        # No digest, no download: there would be nothing to check the bytes
        # against before unpacking them.
        raise UpdateError(
            f"GitHub lists no SHA-256 for the latest release's {name}, so Graft can't "
            "check the download.",
            reason=DIGEST, url=url,
        )
    return Asset(name=name, url=link, size=size, sha256=match.group(1))


# --- Where it can be installed ------------------------------------------------


@dataclass(frozen=True)
class BundleInfo:
    bundle_id: str | None
    version: str | None


def read_info_plist(app: Path) -> dict[str, Any] | None:
    """`app`'s Info.plist, or None for one that isn't there, isn't readable,
    isn't a dictionary or is too big to be one. Every caller refuses a
    bundle whose plist is None.

    Any exception from plistlib is "not readable": its failures are not a
    closed set. Besides its own InvalidFileException and ValueError, XML
    that isn't well formed raises expat's ExpatError, and a `<date>` that
    isn't one AttributeError (as in `macho`'s entitlements)."""
    plist = app / "Contents" / "Info.plist"
    try:
        if plist.is_symlink() or plist.stat().st_size > 1 << 20:
            return None
        with plist.open("rb") as f:
            data = plistlib.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def bundle_info(app: Path) -> BundleInfo:
    """What `app`'s Info.plist says it is. Nones for a plist that isn't
    there, isn't readable or is too big to be one."""
    data = read_info_plist(app)
    if data is None:
        return BundleInfo(None, None)
    bundle_id, version = data.get("CFBundleIdentifier"), data.get("CFBundleShortVersionString")
    return BundleInfo(
        bundle_id if isinstance(bundle_id, str) else None,
        version if isinstance(version, str) else None,
    )


def is_translocated(app: Path) -> bool:
    """Whether macOS is running `app` from a read-only copy of a quarantined
    download (App Translocation), which nothing can replace: it lives under
    a folder of that name until the user moves the app."""
    return "/AppTranslocation/" in str(app)


def check_installable(app: Path) -> None:
    """Raise `UpdateError` unless `app` is a Graft.app this user can replace
    where it is. Asked before a download, so none is wasted, and again
    before the install."""
    if not app.is_absolute():
        raise UpdateError(f"{app} isn't a full path to Graft.app.", reason=NOT_GRAFT)
    if is_translocated(app):
        raise UpdateError(
            "Graft can't update itself while macOS runs it from a temporary copy of the "
            "download. Move Graft to your Applications folder, open it from there, and it can.",
            reason=TRANSLOCATED,
        )
    if app.is_symlink() or not app.is_dir() or app.suffix != ".app" \
            or bundle_info(app).bundle_id != BUNDLE_ID:
        raise UpdateError(f"{app} isn't Graft.app, so Graft won't replace it.", reason=NOT_GRAFT)
    if not (os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)):
        raise UpdateError(
            f"Graft can't update itself in {app.parent}: this account can't change that "
            "folder. Download the new version from the release page instead.",
            reason=NOT_WRITABLE,
        )


# --- Whose updates -------------------------------------------------------------


def own_bundle(executable: str | None = None) -> Path | None:
    """The Graft.app this CLI's own code runs from: the bundle around
    `sys.executable` (or `executable`), which Graft.app runs as
    `EMBEDDED_PYTHON`/python3.13. None for a graft outside one (pipx, a
    checkout). Not resolved, as in `tools.bundled_tools_dir`: the launcher
    runs the interpreter by its path in the bundle."""
    exe = Path(executable if executable is not None else sys.executable)
    if not exe.is_absolute() or len(exe.parents) <= len(EMBEDDED_PYTHON):
        return None
    app = exe.parents[len(EMBEDDED_PYTHON)]
    if app.suffix != ".app" or exe.parent.relative_to(app).parts != EMBEDDED_PYTHON:
        return None
    return app


def own_team(run: Run, *, app: Path | None = None) -> str:
    """The Developer ID team an update must be signed by: the one that
    signed the Graft.app this CLI runs from (`own_bundle`; `app` is a test's
    stand-in for it). Raises `UpdateError` (`NO_TEAM`) when there is no such
    bundle, its signature doesn't verify, or it isn't Graft signed with a
    Developer ID: ad hoc, or a development build's "Apple Development".

    Not `--app-path`'s bundle, which only says where to install: a caller
    naming another signed app there would choose whose updates to trust.
    That bundle must verify first, so a changed one can't name a team; and
    then satisfy `requirement` for the team it names, so the team is a
    Developer ID one and the app is Graft."""
    app = app if app is not None else own_bundle()
    if app is None:
        raise UpdateError(
            "Only Graft.app updates itself, and this graft doesn't run from inside it.",
            reason=NO_TEAM,
        )
    if run([CODESIGN, "--verify", "--deep", "--strict", str(app)]).returncode != 0:
        raise UpdateError(
            f"The signature of the Graft.app this runs from ({app}) doesn't check out, so "
            "Graft can't tell whose updates to trust, and doesn't update itself. Download "
            "the new version from the release page.",
            reason=NO_TEAM,
        )
    shown = run([CODESIGN, "-dv", "--verbose=2", str(app)])
    named = [line.partition("=")[2].strip() for line in shown.output.splitlines()
             if line.startswith("TeamIdentifier=")]
    team = named[0] if shown.returncode == 0 and len(named) == 1 else ""
    if TEAM_RE.fullmatch(team) is None \
            or run([CODESIGN, "--verify", "--strict", "-R", "=" + requirement(team), str(app)]).returncode != 0:
        raise UpdateError(
            "This Graft isn't signed with a Developer ID (it's a development or unsigned "
            "build), so it can't tell whose updates to trust, and doesn't update itself. "
            "Download the new version from the release page.",
            reason=NO_TEAM,
        )
    return team


# --- Which build ---------------------------------------------------------------


class MixedBuild(ValueError):
    """A Graft.app that doesn't say, or doesn't agree with itself, which build
    it is (`bundle_flavor`). The message says what disagrees, as a clause."""


def _architectures(path: Path, what: str) -> list[str]:
    """`macho.architectures` of the file `what` names, inside a bundle; a
    `MixedBuild` when there's no such regular file or it isn't a Mach-O.
    Never through a link: a bundle's own code is its own files."""
    if path.is_symlink() or not path.is_file():
        raise MixedBuild(f"it has no {what}")
    try:
        return macho.architectures(path)
    except macho.MachOError as e:
        raise MixedBuild(f"its {what} isn't a program Graft can read ({e})") from e


def embedded_interpreter(app: Path, name: str | None = None) -> Path:
    """`app`'s embedded Python, under `EMBEDDED_PYTHON`: `name` there when
    it's known (the running Graft's own, `own_flavor`), else the one entry
    there named python3.<digits> (`INTERPRETER_RE`), whatever the minor
    version. Raises `MixedBuild` for none, more than one, or a folder on
    the way that is a link: a bundle's own code is its own files (the file
    itself is checked by `_architectures`).

    By pattern, not by this release's python3.13: a download on a newer
    Python refused as another build would be remembered as refused
    (`ARCHITECTURE`), and every Graft would stay on this version."""
    here = app
    for part in EMBEDDED_PYTHON:
        here = here / part
        if here.is_symlink():
            raise MixedBuild("its embedded Python is reached through a link")
    if name is not None:
        return here / name
    try:
        found = sorted(entry for entry in here.iterdir() if INTERPRETER_RE.fullmatch(entry.name))
    except OSError:
        found = []
    if not found:
        raise MixedBuild("it has no embedded Python")
    if len(found) > 1:
        raise MixedBuild(f"it has more than one embedded Python ({', '.join(p.name for p in found)})")
    return found[0]


def bundle_flavor(app: Path, *, interpreter: str | None = None) -> str:
    """Which build of Graft `app` is, ARM64 or X86_64: its Info.plist's
    `FLAVOR_KEY`, once its embedded Python (`embedded_interpreter`, by
    `interpreter`'s name when given) has code for that processor. Raises
    `MixedBuild` otherwise: no readable Info.plist, a key that names
    neither build, or a Python that is missing, unreadable or built for
    another processor. A disagreement is never settled by guessing which of
    the two is right.

    No key means ARM64: every Graft.app made before the key existed was the
    Apple silicon build (2.0.2 and 2.1 were arm64 only). A Debug or Xcode
    build has no key either, and no embedded Python, so it is a
    `MixedBuild`; it never updates itself anyway (`own_bundle`).

    The Python, not `platform.machine()` or the host (`host.apple_silicon`):
    the question is what this bundle is, and an Intel build on an Apple
    silicon Mac is still the Intel build, whatever the Mac or Rosetta says."""
    data = read_info_plist(app)
    if data is None:
        raise MixedBuild("it has no Info.plist Graft can read")
    said = data.get(FLAVOR_KEY, ARM64)
    if not isinstance(said, str) or said not in ASSETS:
        raise MixedBuild(f"its Info.plist names no build Graft knows ({FLAVOR_KEY} is {said!r})")
    has = _architectures(embedded_interpreter(app, interpreter), "embedded Python")
    if said not in has:
        raise MixedBuild(
            f"its Info.plist says it is the build for {MACS[said]} ({said}), and its embedded "
            f"Python has code only for {', '.join(has)}"
        )
    return said


def own_flavor(app: Path | None = None) -> str:
    """Which build the Graft.app this CLI runs from is (`own_bundle`,
    `bundle_flavor`): the build an update has to be. Its Python is the one
    running this, by name (`sys.executable`), so its version is known. `app`
    is a test's stand-in for the bundle, whose Python is found as a
    download's is. Raises `UpdateError`: `NO_TEAM` outside a bundle,
    `MIXED_BUILD` when the bundle disagrees with itself, so it can't tell
    which zip is its update."""
    interpreter = None
    if app is None:
        app = own_bundle()
        interpreter = Path(sys.executable).name
    if app is None:
        raise UpdateError(
            "Only Graft.app updates itself, and this graft doesn't run from inside it.",
            reason=NO_TEAM,
        )
    try:
        return bundle_flavor(app, interpreter=interpreter)
    except MixedBuild as e:
        raise UpdateError(
            f"This Graft.app is mixed up: {e}. So it can't tell which download is its update. "
            "Download Graft again from the release page and replace this copy.",
            reason=MIXED_BUILD,
        ) from e


# --- Which macOS ---------------------------------------------------------------


#: A macOS version as Info.plist and `platform.mac_ver` write it: "14",
#: "14.6", "13.7.1".
_MACOS_RE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,2}", re.ASCII)


def macos_version(text: object) -> tuple[int, int, int] | None:
    """`text` as numbers to compare, "14" as (14, 0, 0); None for anything
    that isn't a macOS version (`_MACOS_RE`). By number: 14.10 is newer
    than 14.9."""
    if not isinstance(text, str) or _MACOS_RE.fullmatch(text) is None:
        return None
    parts = [int(part) for part in text.split(".")] + [0, 0]
    return parts[0], parts[1], parts[2]


def running_macos() -> str | None:
    """This Mac's macOS version ("14.6.1"), or None when it can't be read.
    The seam the tests set (`FakeTools.macos`).

    10.16 is never one Graft runs on: it is what macOS tells a program that
    asks as an old one would (SYSTEM_VERSION_COMPAT), so it reads as "can't
    tell", not as a Mac too old for every update."""
    import platform

    said = platform.mac_ver()[0]
    version = macos_version(said)
    return said if version is not None and version[0] >= 11 else None


def _too_old(macos: str, minimum: str) -> bool:
    """Whether a Mac on `macos` is older than `minimum`. False when either
    can't be read."""
    have, need = macos_version(macos), macos_version(minimum)
    return have is not None and need is not None and have < need


# --- Download -----------------------------------------------------------------


def _check_url(url: str, *, release_url: str) -> None:
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in SCHEMES:
        raise UpdateError(
            "The download was sent somewhere that isn't a secure address, so Graft stopped.",
            reason=HTTP, url=release_url,
        )


def _fetch(asset: Asset, dest: Path, *, release_url: str, progress: Progress) -> None:
    """Download `asset` to `dest` (created here, owner-only), hashing as it
    goes, and raise `UpdateError` unless the bytes are exactly the size and
    SHA-256 GitHub listed."""
    # Function-local, as in every module here that speaks HTTP
    # (tests/test_lazy_imports.py).
    import requests

    def said(received: int) -> None:
        progress({"step": "download", "received": received, "total": asset.size, "check": None})

    digest = hashlib.sha256()
    received = 0
    url = asset.url
    session = requests.Session()
    # As for the check: no credentials from the environment (a `~/.netrc`
    # login for github.com would be sent), but its proxy.
    session.trust_env = False
    try:
        with session:
            for _hop in range(MAX_REDIRECTS + 1):
                _check_url(url, release_url=release_url)
                resp = session.get(
                    url, headers=HEADERS, timeout=TIMEOUT, stream=True, allow_redirects=False,
                    proxies=requests.utils.get_environ_proxies(url),
                )
                with resp:
                    if resp.is_redirect:
                        url = requests.compat.urljoin(url, resp.headers["Location"])
                        continue
                    if resp.status_code != 200:
                        raise UpdateError(
                            f"GitHub answered the download with HTTP {resp.status_code}. Try "
                            "again later.",
                            reason=HTTP, url=release_url,
                        )
                    said(0)
                    step = max(asset.size // 100, 1 << 20)
                    next_said = step
                    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as out:
                        for chunk in resp.iter_content(chunk_size=1 << 16):
                            received += len(chunk)
                            if received > asset.size:
                                raise UpdateError(
                                    f"The download is bigger than the {asset.name} GitHub "
                                    "lists, so Graft stopped it and deleted it.",
                                    reason=DIGEST, url=release_url,
                                )
                            digest.update(chunk)
                            out.write(chunk)
                            if received >= next_said:
                                said(received)
                                next_said = received + step
                    break
            else:
                raise UpdateError(
                    "The download was redirected too many times, so Graft stopped.",
                    reason=HTTP, url=release_url,
                )
    except requests.RequestException as e:
        raise UpdateError(
            "Couldn't download the new Graft. Check this Mac's internet connection, then try "
            "again.",
            reason=OFFLINE, url=release_url,
        ) from e
    except OSError as e:
        raise _disk_error(e, release_url) from e
    if received != asset.size:
        raise UpdateError(
            "The download stopped before it finished. Graft will try again later.",
            reason=OFFLINE, url=release_url,
        )
    said(received)
    if digest.hexdigest() != asset.sha256:
        raise UpdateError(
            f"The download doesn't match the SHA-256 GitHub lists for {asset.name}, so Graft "
            "deleted it.",
            reason=DIGEST, url=release_url,
        )


def _disk_error(e: OSError, release_url: str) -> UpdateError:
    if e.errno == errno.ENOSPC:
        return UpdateError(
            "There isn't enough free space on this Mac to download the new Graft.",
            reason=DISK, url=release_url,
        )
    return UpdateError(
        f"Couldn't write the download to Graft's cache: {e.strerror or e}.",
        reason=DISK, url=release_url,
    )


# --- Verify -------------------------------------------------------------------


def check_listing(zip_path: Path, *, release_url: str | None = None) -> int:
    """The total unpacked size of `zip_path`, once every entry is known to
    land inside one Graft.app; raises `UpdateError` (archive) otherwise.

    Asked before anything is unpacked. ditto writes a link entry as a link,
    so an entry stored "through" one, or a link out of the bundle, could
    put a file anywhere this user can write. codesign's `--strict` would
    refuse the bundle afterward, but by then the file would be written.
    """

    def refuse(why: str) -> UpdateError:
        return UpdateError(
            f"The downloaded {zip_path.name} {why}, so Graft deleted it.",
            reason=ARCHIVE, url=release_url,
        )

    try:
        with zipfile.ZipFile(zip_path) as z:
            infos = z.infolist()
            links: list[str] = []
            total = 0
            for info in infos:
                name = info.filename
                parts = name.rstrip("/").split("/")
                if name.startswith("/") or "\\" in name or parts[0] != APP_NAME \
                        or any(p in ("", ".", "..") for p in parts):
                    raise refuse(f"has a file outside {APP_NAME} ({name!r})")
                if stat.S_ISLNK(info.external_attr >> 16):
                    if info.file_size > 4096:
                        raise refuse(f"has a link too long to be one ({name!r})")
                    target = z.read(info).decode("utf-8", "strict")
                    landed = posixpath.normpath(posixpath.join(posixpath.dirname(name.rstrip("/")), target))
                    if target.startswith("/") or not (landed == APP_NAME or landed.startswith(APP_NAME + "/")):
                        raise refuse(f"has a link out of {APP_NAME} ({name!r})")
                    links.append(name.rstrip("/"))
                total += info.file_size
            for info in infos:
                name = info.filename.rstrip("/")
                if any(name.startswith(link + "/") for link in links):
                    raise refuse(f"has a file inside a link ({info.filename!r})")
    except (zipfile.BadZipFile, UnicodeDecodeError, OSError, EOFError) as e:
        raise refuse("isn't a zip Graft can read") from e
    if total > MAX_UNPACKED_BYTES:
        raise refuse("unpacks to more than a Graft.app could be")
    return total


def check_bundle(app: Path, *, newer_than: str, release_url: str | None = None) -> str:
    """`app`'s version, once its Info.plist says it is Graft and newer than
    `newer_than`; raises `UpdateError` otherwise. The plist is untrusted
    here: what makes it Graft's is the signature checks that follow."""
    info = bundle_info(app)
    if info.bundle_id != BUNDLE_ID:
        raise UpdateError(
            f"The downloaded app isn't Graft (its bundle id is {info.bundle_id!r}), so Graft "
            "deleted it.",
            reason=BUNDLE, url=release_url,
        )
    if release_check.parse_version(info.version) is None:
        raise UpdateError(
            "The downloaded Graft doesn't say which version it is, so Graft deleted it.",
            reason=BUNDLE, url=release_url,
        )
    assert info.version is not None
    if not release_check.is_newer(info.version, newer_than):
        raise UpdateError(
            f"The downloaded Graft is {info.version}, which isn't newer than {newer_than}, so "
            "Graft deleted it.",
            reason=NOT_NEWER, url=release_url,
        )
    return info.version


def check_macos(app: Path, *, macos: str | None, version: str, current: str,
                release_url: str | None = None) -> None:
    """`app` runs on this Mac's macOS, `macos` (`running_macos`): its
    Info.plist's `MACOS_KEY` is that version or older. Raises
    `NeedsNewerMacOS` when it needs a newer one, with `version`, the
    download's, and `current`, the running Graft's, for its sentence; and
    `UpdateError` (`BUNDLE`) for a key that is there and no version.

    No key is no floor, as it is for macOS. A Mac whose macOS can't be read
    (None) is let through, as every update was before this check: that is
    no reason to stop them all. The plist is untrusted here, as in
    `check_bundle`; what this rules out is a genuine Graft for a newer macOS,
    which passes every signature check and then doesn't open."""
    minimum = (read_info_plist(app) or {}).get(MACOS_KEY)
    if minimum is None:
        return
    if macos_version(minimum) is None:
        # Cut short: the sentence reaches the JSON and the app's banner, and
        # the plist may hold anything up to a megabyte.
        shown = repr(minimum)
        if len(shown) > 40:
            shown = shown[:39] + "…"
        raise UpdateError(
            f"The downloaded Graft doesn't say which macOS it needs in a way Graft can read "
            f"({MACOS_KEY} is {shown}), so Graft deleted it.",
            reason=BUNDLE, url=release_url,
        )
    if macos is not None and _too_old(macos, minimum):
        raise NeedsNewerMacOS(version=version, minimum=minimum, macos=macos, current=current,
                              url=release_url)


def check_architecture(app: Path, *, flavor: str, release_url: str | None = None) -> None:
    """`app` is the `flavor` build of Graft, the running one's (`own_flavor`):
    its Info.plist says so and its embedded Python agrees (`bundle_flavor`),
    and its main executable (CFBundleExecutable) has code for that processor
    too. Raises `UpdateError` (`ARCHITECTURE`) otherwise.

    Read off the Mach-O headers in Python (`macho.architectures`). Not
    `lipo` or `file`: on a Mac without the developer tools `/usr/bin/lipo`
    is a stub that offers to install them. The plist and the files are
    untrusted until the signature checks that follow; what this rules out is
    a genuine Graft of the other build, which passes every one of those."""

    def refuse(why: str) -> UpdateError:
        return UpdateError(
            f"The downloaded Graft {why}, so Graft deleted it.", reason=ARCHITECTURE, url=release_url,
        )

    try:
        found = bundle_flavor(app)
    except MixedBuild as e:
        raise refuse(f"is mixed up: {e}") from e
    if found != flavor:
        raise refuse(f"is the build for {MACS[found]}, and this Graft is the build for {MACS[flavor]}")
    name = (read_info_plist(app) or {}).get("CFBundleExecutable")
    if not isinstance(name, str) or name in ("", ".", "..") or "/" in name or "\0" in name:
        raise refuse("doesn't name its main executable")
    try:
        has = _architectures(app / "Contents" / "MacOS" / name, "main executable")
    except MixedBuild as e:
        raise refuse(f"is broken: {e}") from e
    if flavor not in has:
        raise refuse(f"has no code for {MACS[flavor]} in its main executable ({', '.join(has)})")


def check_signature(app: Path, run: Run, *, release_url: str | None = None) -> None:
    """Every file in `app` is as it was signed, nested code included."""
    if run([CODESIGN, "--verify", "--deep", "--strict", str(app)]).returncode != 0:
        raise UpdateError(
            "The downloaded Graft's signature is broken or missing, so Graft deleted it.",
            reason=SIGNATURE, url=release_url,
        )


def check_requirement(app: Path, run: Run, *, team: str, release_url: str | None = None) -> None:
    """`app` is signed with `team`'s Developer ID, as Graft
    (`requirement`)."""
    if run([CODESIGN, "--verify", "--strict", "-R", "=" + requirement(team), str(app)]).returncode != 0:
        raise UpdateError(
            "The downloaded app isn't signed by Graft's developer, so Graft deleted it.",
            reason=REQUIREMENT_FAILED, url=release_url,
        )


def check_notarization(app: Path, run: Run, *, release_url: str | None = None) -> None:
    """A notarization ticket is stapled to `app`, and Gatekeeper accepts it
    as notarized Developer ID code."""
    stapled = run([CODESIGN, "-dvv", str(app)])
    if stapled.returncode != 0 or "Notarization Ticket=stapled" not in stapled.output.splitlines():
        raise UpdateError(
            "The downloaded Graft has no notarization ticket from Apple, so Graft deleted it.",
            reason=NOTARIZATION, url=release_url,
        )
    gatekeeper = run([SPCTL, "-a", "-t", "exec", "-vv", str(app)])
    if gatekeeper.returncode != 0 or "source=Notarized Developer ID" not in gatekeeper.output.splitlines():
        raise UpdateError(
            "macOS's Gatekeeper didn't accept the downloaded Graft as notarized, so Graft "
            "deleted it.",
            reason=NOTARIZATION, url=release_url,
        )


def verify_app(app: Path, *, newer_than: str, team: str, flavor: str, run: Run,
               progress: Progress, release_url: str | None = None,
               macos: str | None = None) -> str:
    """Every check of an unpacked Graft.app, in order; its version once all
    pass, signed by `team` (`own_team`), the `flavor` build (`own_flavor`)
    and for `macos` or an older one (`running_macos` when None). `progress`
    hears each one as it starts: the macOS it needs is part of `bundle`, so
    the app reads no check it doesn't know."""

    def checking(name: str) -> None:
        progress({"step": "verify", "received": None, "total": None, "check": name})

    checking("bundle")
    version = check_bundle(app, newer_than=newer_than, release_url=release_url)
    check_macos(app, macos=macos if macos is not None else running_macos(), version=version,
                current=newer_than, release_url=release_url)
    checking("architecture")
    check_architecture(app, flavor=flavor, release_url=release_url)
    checking("signature")
    check_signature(app, run, release_url=release_url)
    checking("requirement")
    check_requirement(app, run, team=team, release_url=release_url)
    checking("notarization")
    check_notarization(app, run, release_url=release_url)
    return version


# --- The whole download -------------------------------------------------------


@contextlib.contextmanager
def _locked(root: Path, *, wait: float = LOCK_WAIT) -> Iterator[None]:
    """One download at a time in `root`: a second waits up to `wait`
    seconds (a canceled one may still be cleaning up), then gives up."""
    fd = os.open(root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise UpdateError(
                        "Another download of Graft is already running.", reason=BUSY,
                    ) from None
                time.sleep(0.2)
        yield
    finally:
        os.close(fd)


def _clear(root: Path) -> None:
    """Every earlier download in `root`, ready or not: the one about to
    start replaces it. The app forgets a ready one before it asks for a new
    one (`UpdateStore`), so nothing is left pointing here."""
    for entry in root.iterdir():
        if entry.name == ".lock":
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()


def _noted_minimum(root: Path, *, version: str, sha256: str) -> str | None:
    """The macOS `MACOS_NOTE` in `root` says the release `version`'s zip,
    by its `sha256`, needs; None when there is no such note, it is about
    another zip, or it isn't one.

    Read as Graft's other small state files are (`data_dirs.read_small_json`):
    a regular file only, not through a link, and never waiting on a FIFO,
    which `--check-update` would otherwise hang on (the 2.3.1 review's A4)."""
    note = data_dirs.read_small_json(root / MACOS_NOTE)
    if not isinstance(note, dict) or note.get("version") != version or note.get("sha256") != sha256:
        return None
    minimum = note.get("minimum")
    return minimum if macos_version(minimum) is not None else None


def _note_minimum(root: Path, *, version: str, sha256: str, minimum: str) -> None:
    """Leave `MACOS_NOTE` in `root`. Best effort: without it the next try
    downloads the zip again and refuses it again, as it always did.

    Owner-only, into a fresh file renamed into place
    (`data_dirs.private_write`), never opened through a link planted at
    its name (the 2.3.1 review's A4)."""
    note = json.dumps({"version": version, "sha256": sha256, "minimum": minimum}).encode()
    with contextlib.suppress(OSError):
        data_dirs.private_write(note, root / MACOS_NOTE)


def noted_refusal(data: dict[str, Any], release: release_check.Release,
                  current: str) -> NeedsNewerMacOS | None:
    """For `--check-update`: the refusal a download of the latest release
    (`release`, from GitHub's answer `data`) would meet again, when it is
    newer than `current`, `MACOS_NOTE` says its zip of this build needs a
    newer macOS, and this Mac still has an older one. None otherwise: no
    note, a note about another zip or release, a macOS that can't be read,
    or a graft outside Graft.app, which has no build of its own
    (`own_flavor`) and never downloads one. Reads the note and nothing
    else: no download, no lock."""
    if not release_check.is_newer(release.version, current):
        return None
    macos = running_macos()
    if macos is None:
        return None
    try:
        asset = release_asset(data, flavor=own_flavor(), url=release.url)
    except UpdateError:
        return None
    minimum = _noted_minimum(updates_dir(), version=release.version, sha256=asset.sha256)
    if minimum is None or not _too_old(macos, minimum):
        return None
    return NeedsNewerMacOS(version=release.version, minimum=minimum, macos=macos,
                           current=current, url=release.url)


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def download_update(current: str, app: Path, *, progress: Progress, run: Run | None = None) -> Staged:
    """Download the latest release's zip of this build (`own_flavor`) when it
    is newer than `current`, unpack it and check it (the module docstring).
    Raises `UpdateError`, having deleted whatever it downloaded.

    `run` is `run_tool` unless a test passes its stand-in, looked up here
    rather than as a default so the verb, which passes none, gets a test's
    patch of the module's own. This Mac's macOS is `running_macos`'s, looked
    up the same way."""
    run = run or run_tool
    macos = running_macos()
    check_installable(app)
    # Before anything is downloaded: a Graft that can't say whose updates to
    # trust has no use for one.
    team = own_team(run)
    # Which zip is this Graft's update, asked of the bundle, not of the Mac
    # (`bundle_flavor`): an Intel build stays the Intel build.
    flavor = own_flavor()
    try:
        data = release_check.latest_release_json()
        release = release_check.release_from(data)
    except release_check.UpdateCheckError as e:
        raise UpdateError(e.message, reason=e.reason) from e
    if not release_check.is_newer(release.version, current):
        raise UpdateError(
            f"Graft {current} is up to date: the latest release is {release.version}.",
            reason=NOT_NEWER, url=release.url,
        )
    asset = release_asset(data, flavor=flavor, url=release.url)

    def report(fields: dict[str, Any]) -> None:
        # Every `update-progress` names the version it is about.
        progress({"step": fields["step"], "version": release.version, "received": fields["received"],
                  "total": fields["total"], "check": fields["check"]})

    root = _private_dir(updates_dir())
    with _locked(root):
        # Refused already for the macOS it needs: no update for this Mac,
        # answered as a release without this build's zip is, before any of
        # it is downloaded again.
        minimum = _noted_minimum(root, version=release.version, sha256=asset.sha256)
        if minimum is not None and macos is not None and _too_old(macos, minimum):
            raise NeedsNewerMacOS(version=release.version, minimum=minimum, macos=macos,
                                  current=current, url=release.url)
        _clear(root)
        try:
            free = shutil.disk_usage(root).free
        except OSError:
            free = None
        if free is not None and free < asset.size * 4:
            raise UpdateError(
                f"There isn't enough free space on this Mac to download Graft {release.version} "
                f"(it needs about {asset.size * 4 // (1 << 20)} MB).",
                reason=DISK, url=release.url,
            )
        # `.noindex`: Spotlight leaves it alone, so the unpacked app isn't
        # offered in Spotlight or Open With while it waits here.
        work = Path(tempfile.mkdtemp(prefix="Graft-", suffix=".noindex", dir=root))
        try:
            zip_path = work / asset.name
            # The digest is checked as the bytes arrive (`_fetch`).
            _fetch(asset, zip_path, release_url=release.url, progress=report)
            report({"step": "verify", "received": None, "total": None, "check": "archive"})
            unpacked = check_listing(zip_path, release_url=release.url)
            if free is not None and shutil.disk_usage(root).free < unpacked + (64 << 20):
                raise UpdateError(
                    f"There isn't enough free space on this Mac to unpack Graft {release.version}.",
                    reason=DISK, url=release.url,
                )
            into = _private_dir(work / "app")
            if run([DITTO, "-x", "-k", str(zip_path), str(into)]).returncode != 0:
                raise UpdateError(
                    f"Couldn't unpack the downloaded {asset.name}, so Graft deleted it.",
                    reason=ARCHIVE, url=release.url,
                )
            zip_path.unlink()
            staged = into / APP_NAME
            if staged.is_symlink() or not staged.is_dir():
                raise UpdateError(
                    f"The downloaded {asset.name} has no {APP_NAME} in it, so Graft deleted it.",
                    reason=ARCHIVE, url=release.url,
                )
            version = verify_app(staged, newer_than=current, team=team, flavor=flavor, run=run,
                                 progress=report, release_url=release.url, macos=macos)
        except BaseException as e:
            # A failed check, a stop (SIGTERM, `lc_manage.exit_on_termination`)
            # or anything else: nothing half-checked stays behind.
            shutil.rmtree(work, ignore_errors=True)
            if isinstance(e, NeedsNewerMacOS):
                _note_minimum(root, version=release.version, sha256=asset.sha256,
                              minimum=e.minimum)
            raise
    return Staged(version=version, path=staged, url=release.url)


def download_event(current: str, *, staged: Staged | None = None,
                   error: UpdateError | None = None) -> dict[str, Any]:
    """The `update-download-end` event's fields: the same keys whether the
    download worked or not."""
    if error is not None:
        return {
            "ok": False, "current": current, "version": None, "app_path": None,
            "url": error.url, "reason": error.reason, "message": error.message,
        }
    assert staged is not None
    return {
        "ok": True, "current": current, "version": staged.version,
        "app_path": str(staged.path), "url": staged.url, "reason": None, "message": None,
    }


# --- Install ------------------------------------------------------------------


#: The helper. Everything it needs comes in its environment (`helper_env`),
#: so a test can run it on dummy bundles with stand-in tools; with a
#: variable missing it stops at once (`set -u`) and changes nothing.
HELPER = r"""
set -u
# All of them, before anything is touched: one missing partway through would
# stop the script with the new app half moved.
: "${GRAFT_UPDATE_PID:?}" "${GRAFT_UPDATE_APP:?}" "${GRAFT_UPDATE_STAGED:?}" \
  "${GRAFT_UPDATE_VERSION:?}" "${GRAFT_UPDATE_ARCH:?}" "${GRAFT_UPDATE_RELAUNCH:?}" "${GRAFT_UPDATE_REQUIREMENT:?}" \
  "${GRAFT_UPDATE_LOG:?}" "${GRAFT_UPDATE_TRASH:?}" "${GRAFT_UPDATE_WAIT_TICKS:?}" \
  "${GRAFT_UPDATE_CODESIGN:?}" "${GRAFT_UPDATE_SPCTL:?}" "${GRAFT_UPDATE_PLISTBUDDY:?}" \
  "${GRAFT_UPDATE_LSREGISTER:?}" "${GRAFT_UPDATE_OPEN:?}" "${GRAFT_UPDATE_MV:?}"
log=$GRAFT_UPDATE_LOG
app=$GRAFT_UPDATE_APP
staged=$GRAFT_UPDATE_STAGED
version=$GRAFT_UPDATE_VERSION
parent=$(dirname "$app")
copy="$parent/.Graft-update-$$.app"

# Each line's time has its offset from UTC: the app reads the log at its next
# launch (UpdateStore.installLogged), and this script's zone is the CLI's,
# which has any TZ the user's shell profile sets, where the app's may not.
# Always a line of its own: a tool whose output the log keeps may end it
# without a newline, and the line after it ran on from it.
say() {
  if [ -s "$log" ] && [ -n "$(tail -c 1 "$log" 2>/dev/null)" ]; then
    printf '\n' >>"$log" 2>/dev/null
  fi
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" >>"$log" 2>/dev/null
}

reopen() {
  if [ "$GRAFT_UPDATE_RELAUNCH" = 1 ] && [ -d "$app" ]; then
    "$GRAFT_UPDATE_OPEN" "$app" >>"$log" 2>&1 || say "couldn't relaunch $app"
  fi
}

give_up() {
  say "not installed: $*"
  if [ -e "$copy" ] || [ -L "$copy" ]; then rm -rf "$copy"; fi
  reopen
  exit 1
}

say "waiting for Graft (pid $GRAFT_UPDATE_PID) to quit, to install Graft $version at $app"
ticks=0
while kill -0 "$GRAFT_UPDATE_PID" 2>/dev/null; do
  ticks=$((ticks + 1))
  if [ "$ticks" -gt "$GRAFT_UPDATE_WAIT_TICKS" ]; then
    # Still running, so nothing to reopen: it keeps its version.
    say "not installed: Graft (pid $GRAFT_UPDATE_PID) didn't quit"
    exit 1
  fi
  sleep 0.2
done

# Beside the old app first: the checks then run on the copy that is
# installed, and the swap below is one rename in one folder.
if [ -e "$copy" ] || [ -L "$copy" ]; then give_up "$copy is in the way"; fi
"$GRAFT_UPDATE_MV" "$staged" "$copy" >>"$log" 2>&1 || give_up "couldn't move the download beside $app"
"$GRAFT_UPDATE_CODESIGN" --verify --deep --strict -R "=$GRAFT_UPDATE_REQUIREMENT" "$copy" >>"$log" 2>&1 \
  || give_up "it isn't signed as Graft by Graft's developer"
"$GRAFT_UPDATE_CODESIGN" -dvv "$copy" 2>&1 | grep -qx 'Notarization Ticket=stapled' \
  || give_up "it has no notarization ticket"
"$GRAFT_UPDATE_SPCTL" -a -t exec -vv "$copy" 2>&1 | grep -qx 'source=Notarized Developer ID' \
  || give_up "Gatekeeper didn't accept it"
found=$("$GRAFT_UPDATE_PLISTBUDDY" -c 'Print :CFBundleShortVersionString' "$copy/Contents/Info.plist" 2>/dev/null)
[ "$found" = "$version" ] || give_up "it is version '$found', not $version"
# The build the CLI checked (FLAVOR_KEY), as the plist the signature above
# sealed says; one without the key is the Apple silicon build. Another build
# of the same version passes every check above: this one keeps it out.
built=$("$GRAFT_UPDATE_PLISTBUDDY" -c 'Print :GraftBundleArch' "$copy/Contents/Info.plist" 2>/dev/null) \
  || built=arm64
[ "$built" = "$GRAFT_UPDATE_ARCH" ] || give_up "it is the build for '$built', not $GRAFT_UPDATE_ARCH"

mkdir -p "$GRAFT_UPDATE_TRASH" || give_up "couldn't make the Trash"
trashed="$GRAFT_UPDATE_TRASH/Graft.app"
n=1
while [ -e "$trashed" ] || [ -L "$trashed" ]; do
  n=$((n + 1))
  trashed="$GRAFT_UPDATE_TRASH/Graft $n.app"
done
"$GRAFT_UPDATE_MV" "$app" "$trashed" >>"$log" 2>&1 || give_up "couldn't move $app to the Trash"
if ! "$GRAFT_UPDATE_MV" "$copy" "$app" >>"$log" 2>&1; then
  if [ ! -e "$app" ] && "$GRAFT_UPDATE_MV" "$trashed" "$app" >>"$log" 2>&1; then
    say "put the old Graft back at $app"
  else
    say "couldn't put the old Graft back: it is in the Trash at $trashed"
  fi
  give_up "couldn't move Graft $version to $app"
fi

# Launch Services: forget the old copy and take the new one. The -u works:
# it finds the old record by the bundle itself, though that record still
# names the path the bundle was moved from. It lasts only until something
# asks Launch Services for that bundle's icon, which registers it again where
# it is now; Finder draws that icon when it shows the Trash, and a Dock tile
# kept for Graft may too. So lsregister -dump can list the copy in the Trash
# again later, until the Trash is emptied. Tried on throwaway unsigned
# bundles (2026-11-24): after a move and a -u, reading the bundle's
# Info.plist (PlistBuddy), codesign -dvvv, and its type or kind left it
# unregistered; NSWorkspace's icon for it registered it at once. A second
# -u, after the relaunch or on a retry, would be undone the same way by the
# next look, so there isn't one. The relaunch below opens the new app by
# its path, not by its bundle id. What the -u says isn't kept: it is no
# news (the copy is in the Trash either way), and on the 2.3 update it was
# "failed to scan …: -10814 from spotlight", with no newline.
"$GRAFT_UPDATE_LSREGISTER" -u "$trashed" >/dev/null 2>&1
"$GRAFT_UPDATE_LSREGISTER" -f "$app" >>"$log" 2>&1 || say "Launch Services didn't register $app"
rmdir "$(dirname "$staged")" "$(dirname "$(dirname "$staged")")" 2>/dev/null
say "installed Graft $version at $app; the old one is in the Trash at $trashed"
reopen
exit 0
"""


@dataclass(frozen=True)
class HelperTools:
    """The programs the helper runs, by absolute path: the system's, or a
    test's stand-ins."""

    codesign: str = CODESIGN
    spctl: str = SPCTL
    plistbuddy: str = PLISTBUDDY
    lsregister: str = LSREGISTER
    open: str = OPEN
    mv: str = MV


def helper_env(*, pid: int, app: Path, staged: Path, version: str, flavor: str, relaunch: bool,
               log: Path, trash: Path, team: str, tools: HelperTools = HelperTools(),
               wait_ticks: int = WAIT_TICKS) -> dict[str, str]:
    """The helper's own variables. `spawn_detached` puts them over the
    CLI's environment without its secrets (`child_env`), so these win.
    `team` is whose signature the copy must carry (`own_team`), and
    `flavor` which build it must be (`own_flavor`)."""
    if flavor not in ASSETS:
        raise ValueError(f"not a build of Graft: {flavor!r}")
    return {
        "PATH": SYSTEM_PATH,
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "GRAFT_UPDATE_PID": str(pid),
        "GRAFT_UPDATE_APP": str(app),
        "GRAFT_UPDATE_STAGED": str(staged),
        "GRAFT_UPDATE_VERSION": version,
        "GRAFT_UPDATE_ARCH": flavor,
        "GRAFT_UPDATE_RELAUNCH": "1" if relaunch else "0",
        "GRAFT_UPDATE_REQUIREMENT": requirement(team),
        "GRAFT_UPDATE_LOG": str(log),
        "GRAFT_UPDATE_TRASH": str(trash),
        "GRAFT_UPDATE_WAIT_TICKS": str(wait_ticks),
        "GRAFT_UPDATE_CODESIGN": tools.codesign,
        "GRAFT_UPDATE_SPCTL": tools.spctl,
        "GRAFT_UPDATE_PLISTBUDDY": tools.plistbuddy,
        "GRAFT_UPDATE_LSREGISTER": tools.lsregister,
        "GRAFT_UPDATE_OPEN": tools.open,
        "GRAFT_UPDATE_MV": tools.mv,
    }


#: The helper's argv: the script itself, so nothing is read from the bundle
#: it replaces, and a name for `ps`.
HELPER_ARGV = ["/bin/sh", "-c", HELPER, "graft-update-helper"]

Spawn = Callable[[list[str], dict[str, str]], None]


def spawn_detached(argv: list[str], env: dict[str, str]) -> None:
    """Start `argv` so it outlives this CLI and Graft: its own session, no
    inherited descriptors, and nothing on the standard streams. The app
    reads the CLI's stdout to its end, so a helper still holding it open
    would keep the app waiting for a helper that is waiting for the app.
    `env` goes over the environment without Graft's secrets (`child_env`)."""
    subprocess.Popen(
        argv, env=child_env(env), cwd="/", stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        close_fds=True,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def schedule_install(staged: Path, app: Path, *, pid: int, relaunch: bool,
                     spawn: Spawn | None = None, run: Run | None = None) -> Scheduled:
    """Start the helper that installs `staged` over `app` once `pid` (Graft)
    has quit. Raises `UpdateError`, having started nothing, unless `app` can
    be replaced (`check_installable`), the running Graft has a team to trust
    (`own_team`), `staged` is a download this CLI made (under `updates_dir`),
    newer than `app` and the running Graft's build (`check_architecture`),
    and `pid` is running. `spawn` and `run` are
    `spawn_detached` and `run_tool` unless a test passes its own
    (`download_update`'s `run`)."""
    spawn = spawn or spawn_detached
    run = run or run_tool
    check_installable(app)
    team = own_team(run)
    flavor = own_flavor()
    root = updates_dir()
    if not staged.is_absolute() or staged.is_symlink() or staged.name != APP_NAME \
            or not staged.is_dir() or root.resolve() not in staged.resolve().parents:
        raise UpdateError(
            f"{staged} isn't a Graft that --download-update downloaded and checked.",
            reason=NOT_STAGED,
        )
    new, old = bundle_info(staged), bundle_info(app)
    if new.bundle_id != BUNDLE_ID or release_check.parse_version(new.version) is None:
        raise UpdateError(f"{staged} isn't Graft.", reason=NOT_STAGED)
    assert new.version is not None
    if release_check.parse_version(old.version) is not None:
        assert old.version is not None
        if not release_check.is_newer(new.version, old.version):
            raise UpdateError(
                f"The Graft at {app} is already {old.version}, so {new.version} isn't an update.",
                reason=NOT_NEWER,
            )
    # Again here, though the download was checked: the cache is this user's
    # to change, and the helper re-reads only the plist (`HELPER`).
    check_architecture(staged, flavor=flavor)
    if pid <= 1 or not _alive(pid):
        raise UpdateError(f"No process {pid} is running to wait for.", reason=HELPER_FAILED)
    log = log_path()
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        spawn(HELPER_ARGV, helper_env(
            pid=pid, app=app, staged=staged, version=new.version, flavor=flavor, relaunch=relaunch,
            log=log, trash=default_trash_dir(), team=team,
        ))
    except OSError as e:
        raise UpdateError(
            f"Couldn't start the installer: {e.strerror or e}.", reason=HELPER_FAILED,
        ) from e
    return Scheduled(version=new.version, log=log)


def install_event(*, version: str | None = None, log: Path | None = None, relaunch: bool,
                  error: UpdateError | None = None) -> dict[str, Any]:
    """The `update-install` event's fields: the same keys either way."""
    if error is not None:
        return {
            "ok": False, "version": None, "log": None, "relaunch": relaunch,
            "url": error.url, "reason": error.reason, "message": error.message,
        }
    return {
        "ok": True, "version": version, "log": str(log) if log else None, "relaunch": relaunch,
        "url": None, "reason": None, "message": None,
    }
