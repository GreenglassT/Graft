"""Enrolling this Mac for decryption: which installed apps can do it, and
(`enroll_command`) the exact command that does.

UnfairPlay v0.4.0's `setup --app` takes "a native Mac App Store-installed
.app" (cmd/unfairplay/setup.go). Reading its code
(pkg/fairplay/enrollment_capture.go, installed_ipa.go), it accepts one when:

1. the path is a directory named `*.app`;
2. it has a `Wrapper/` holding an inner `*.app`. That is how macOS installs
   an iPhone or iPad app from the Mac App Store (Task 0, S2.5: `Wrapper/`
   holds `BundleMetadata.plist`, `iTunesMetadata.plist` and `<App>.app`).
   Without `Wrapper/` the path is read as the bundle itself, and a native Mac
   app then fails at step 4, because its license isn't at the bundle root;
3. the inner app's Info.plist names a bundle identifier and an executable;
4. the inner app's `SC_Info/` holds exactly one `.sinf`, its license (S2.5:
   one, beside `.supf`/`.supp`/`.supx` files, which aren't licenses);
5. its executables are still encrypted: "already-decrypted bundles fail
   closed" (S2.5: the main executable's cryptid is 1).

`check` tests exactly those, so the app offers only what setup will take. It
also tests one thing UnfairPlay can't: whether the app has been opened once,
which its README asks for. Opening one leaves `~/Library/Containers/<bundle
id>` behind (`opened`; S2.5). Symlinks inside the bundle, which setup also
refuses, are rare enough in an App Store app to leave to setup's own error.
"""

from __future__ import annotations

import os
import plistlib
import pwd
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from . import data_dirs, macho, tools, unfairplay
from .child_env import child_env
from .errors import AppStoreError

if TYPE_CHECKING:
    from .config import AppStoreToolsConfig

#: Where the Mac App Store installs apps, and the only folder root will take
#: one from (`trusted_location`). Tests replace it.
SCAN_DIRS: tuple[Path, ...] = (Path("/Applications"),)

NO_CANDIDATES = (
    "No installed app can enroll this Mac yet: install any free iPhone or iPad app "
    "from the Mac App Store, open it once, then check again."
)


class NotACandidate(AppStoreError):
    pass


@dataclass(frozen=True)
class Candidate:
    path: Path
    name: str
    bundle_id: str
    opened: bool
    #: The iPhone or iPad app inside `Wrapper/`: what UnfairPlay reads, and
    #: what root copies (`root_script`).
    inner: Path


def check(app: Path, *, home: Path | None = None) -> Candidate:
    """`app` as a candidate, or `NotACandidate` with the reason in words."""
    if app.is_symlink() or not app.is_dir() or app.suffix.lower() != ".app":
        raise NotACandidate(f"{app} is not an app")
    wrapper = app / "Wrapper"
    if not wrapper.is_dir():
        raise NotACandidate(
            f"{app.name} is not an iPhone or iPad app installed from the Mac App Store"
        )
    inner = next(
        (p for p in sorted(wrapper.iterdir()) if p.is_dir() and p.suffix.lower() == ".app"),
        None,
    )
    if inner is None:
        raise NotACandidate(f"{app.name}'s Wrapper holds no app")
    try:
        info = plistlib.loads((inner / "Info.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException, ValueError) as e:
        raise NotACandidate(f"{app.name}'s Info.plist can't be read") from e
    bundle_id, executable = info.get("CFBundleIdentifier"), info.get("CFBundleExecutable")
    if not (isinstance(bundle_id, str) and bundle_id and isinstance(executable, str)
            and executable not in ("", ".", "..") and "/" not in executable):
        raise NotACandidate(f"{app.name}'s Info.plist names no bundle id or executable")
    try:
        sinfs = [p for p in (inner / "SC_Info").iterdir()
                 if p.is_file() and p.suffix.lower() == ".sinf"]
    except OSError:
        sinfs = []
    if len(sinfs) != 1:
        raise NotACandidate(
            f"{app.name} has {len(sinfs)} App Store licenses where UnfairPlay needs exactly one"
        )
    try:
        encrypted = any(cryptid != 0 for cryptid in macho.cryptids(inner / executable))
    except (OSError, macho.MachOError):
        encrypted = False
    if not encrypted:
        raise NotACandidate(f"{app.name}'s app binary isn't encrypted, so it can't enroll this Mac")
    home = Path.home() if home is None else home
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem
    return Candidate(
        path=app, name=str(name), bundle_id=bundle_id,
        opened=(home / "Library" / "Containers" / bundle_id).is_dir(), inner=inner,
    )


def candidates(dirs: Iterable[Path] | None = None, *, home: Path | None = None) -> list[Candidate]:
    """Every app in `dirs` (one level down) that `check` accepts, and that
    the root script will take (`trusted_location`): opened ones first, then
    by name."""
    found: list[Candidate] = []
    for root in SCAN_DIRS if dirs is None else dirs:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() != ".app":
                continue
            try:
                trusted_location(entry)
                found.append(check(entry, home=home))
            except NotACandidate:
                continue
            except OSError:
                continue
    return sorted(found, key=lambda c: (not c.opened, c.name.lower()))


# --- The command ---------------------------------------------------------------
#
# Root never writes into, or runs from, anything this user can write to. A
# user-writable folder handed to a root `chown -R` is a hard link (macOS has
# no protected_hardlinks) or a symlink away from re-owning any root file on the
# Data volume; a root exec of a binary in a user-owned Graft.app can be
# swapped between any check and the run. So root works in a folder it makes
# itself, runs its own copy of UnfairPlay (checked against Graft.app's team
# signature in a team-signed Graft.app; otherwise printed only with the
# user's explicit consent: `root_helper`), checks the app it reads, and only
# then gives that folder, whose every file its own UnfairPlay wrote, to the
# user. The user's model folder is filled afterward, by `finish`, as the
# user.

#: Where root makes its work folder: `mktemp -d` under a root-owned, sticky
#: directory, so the folder is root:wheel 0700 from the first instant.
WORK_TEMPLATE = "/private/var/tmp/graft-enroll.XXXXXXXX"
#: The only folder an app to enroll with may be in, and who must own it: the
#: Mac App Store installs into /Applications as root (S2.5). Tests replace both.
TRUSTED_ROOT = Path("/Applications")
TRUSTED_OWNER = 0
#: The bundled UnfairPlay's code-signing identifier, which scripts/graft-release
#: sets explicitly (--identifier) and scripts/verify-bundle checks the root
#: step's requirement against on the built app.
UNFAIRPLAY_IDENTIFIER = "unfairplay"

#: The script's exit statuses for its own refusals and stops, so the app can
#: say which.
EXIT_NOT_IN_APPLICATIONS = 64
EXIT_NOT_ROOTS = 65
EXIT_WRITABLE = 66
#: Stopped because the cancel file (`EnrollCommand.cancel_file`) appeared.
EXIT_CANCELLED = 67
#: Stopped because the run went past its time limit (`time_limit`).
EXIT_TIME_LIMIT = 68
#: Stopped before anything was copied: neither of `FAIRPLAY_STATE_DIRS`
#: holds this Mac's FairPlay state (`root_script`).
EXIT_NO_FAIRPLAY_STATE = 69

#: The folders UnfairPlay's capture reads this Mac's FairPlay state from,
#: looking for a file named `SC Info.sidb` in any case (v0.4.0's
#: pkg/fairplay enrollment_capture.go). macOS can leave them without one,
#: as after a restart in the owner's run, and opening an iPhone or iPad
#: app installed from the Mac App Store writes it again. Root looks there
#: before it copies anything (`root_script`). Tests replace it.
FAIRPLAY_STATE_DIRS: tuple[Path, ...] = (Path("/private/var/db/fpsd/SC Info"), Path("/Users/Shared/SC Info"))

#: How long root's whole run may take (every step after `mktemp`: the
#: copies, the checks and UnfairPlay setup) for an app of up to
#: `LARGE_APP_BYTES`; a larger app gets more (`time_limit`). Tests replace it.
#:
#: What the time goes on, from UnfairPlay v0.4.0's source (d7503082, the
#: pinned binary's vcs.revision). Setup's emulation and model search don't
#: depend on the app's size: the plan's S2.2 run took 10 s in all. What does
#: is the validation archive: `installedValidationIPA` zips every file of
#: the inner app with Deflate (pkg/fairplay/installed_ipa.go), on one core.
#: The rest is cheap: about four SHA-256 passes over that archive and
#: DecryptIPA's raw copy of each member (`zip.Writer.Copy`). Measured on an
#: M4 with Go's own archive/zip, as setup uses it: 60 MB/s of input for a
#: 611 MB mix of binaries and code in 9,888 files, and 100 MB/s for 1 GB that
#: doesn't compress; the hashing and the copy added 3 s per GB. An M1 core
#: is about 0.6 of an M4's, so about 36 MB/s. 900 s is then a 6 GB app at
#: `SETUP_BYTES_PER_SECOND` (a third of the M1 estimate) plus 300 s for
#: everything else. A run still going then is stuck, and Cancel stops one
#: sooner.
TIME_LIMIT_SECONDS = 900
#: The app size `TIME_LIMIT_SECONDS` is enough for ...
LARGE_APP_BYTES = 6_000_000_000
#: ... and one more second for each this many bytes past it.
SETUP_BYTES_PER_SECOND = 10_000_000
#: The most any app gets: 141 GB at that rate.
TIME_LIMIT_MAX_SECONDS = 4 * 60 * 60

_TEAM_RE = re.compile(r"^[A-Z0-9]{10}$")

#: Why root can't check the UnfairPlay it would run (`RootHelper.unverified_kind`):
#: Graft.app's signature has no team (an ad hoc build) ...
UNVERIFIED_AD_HOC = "ad-hoc"
#: ... or this CLI isn't Graft.app's own (pipx, or a Debug build running the
#: repo's CLI), so the UnfairPlay is whatever `unfairplay.resolve_tool` finds.
UNVERIFIED_NOT_BUNDLED = "not-bundled"
#: `appstore-refused`'s `reason` when `--appstore-enroll-command` won't print
#: an unverified script without `ALLOW_FLAG`.
REFUSED_UNVERIFIED_HELPER = "unverified-helper"
ALLOW_FLAG = "--allow-unverified-helper"


class AdHoc(Enum):
    """`app_team`'s answer for a signature that names no team: an ad hoc
    build. A value of its own, never None, so a failed lookup can't pass
    for one."""

    AD_HOC = "ad-hoc"


AD_HOC = AdHoc.AD_HOC


@dataclass(frozen=True)
class RootHelper:
    """The UnfairPlay the root script copies and runs (`path`), and whether
    root can check its copy: against Graft.app's `team`, or, with no team,
    why not (`unverified_kind`). Exactly one of the two is set."""

    path: Path
    team: str | None = None
    unverified_kind: str | None = None
    #: Graft.app's own certificate is Developer ID (`app_is_developer_id`),
    #: so root wants its UnfairPlay copy signed with one too.
    developer_id: bool = False

    def __post_init__(self) -> None:
        if self.team is None and self.unverified_kind not in (UNVERIFIED_AD_HOC, UNVERIFIED_NOT_BUNDLED):
            raise ValueError(f"an unchecked helper needs a known reason, not {self.unverified_kind!r}")
        if self.team is not None and self.unverified_kind is not None:
            raise ValueError("a helper checked against a team has no reason to be unchecked")
        if self.developer_id and self.team is None:
            raise ValueError("only a helper checked against a team can be checked as Developer ID")

    @property
    def unverified_reason(self) -> str | None:
        """Why root can't check it, as the sentence the app and the CLI show."""
        if self.unverified_kind == UNVERIFIED_AD_HOC:
            return (
                "This copy of Graft has no developer-team signature, so root can't check "
                "UnfairPlay's signature before running it."
            )
        if self.unverified_kind == UNVERIFIED_NOT_BUNDLED:
            return (
                f"The UnfairPlay root would run is {self.path}, not Graft.app's own copy, "
                "and nothing checks its signature."
            )
        return None


@dataclass(frozen=True)
class EnrollCommand:
    app: Path
    #: The user's own model folder, which `finish` fills, as the user. The
    #: root script never names it.
    model_dir: Path
    #: The `/bin/sh` script root runs. Its last line of output is the work
    #: folder to hand to `finish`.
    script: str
    #: The UnfairPlay the script copies and runs as root.
    helper: Path
    #: Whether the script checks its copy of UnfairPlay against Graft.app's
    #: team signature (`requirement`). False when root can't:
    #: `unverified_kind` says why, and `unverified_reason` says it in words.
    #: The CLI prints such a script only with `ALLOW_FLAG`.
    verified: bool
    requirement: str | None
    unverified_kind: str | None
    unverified_reason: str | None
    #: The file whose existence stops the run (`EXIT_CANCELLED`), before
    #: UnfairPlay starts or while it runs. The app creates it to cancel; the
    #: CLI never does, and root only tests for it (`root_script`).
    cancel_file: Path
    #: How long root's whole run may take (`time_limit`, `EXIT_TIME_LIMIT`).
    time_limit_seconds: int


def app_bytes(inner: Path) -> int:
    """The size of every regular file in `inner`, measured as the user and
    never through a link: what UnfairPlay setup zips (`TIME_LIMIT_SECONDS`).
    What can't be read counts as nothing.

    `os.fwalk`, not `os.walk`: walk reads its top through a link, so an
    `inner` that is one (which `check` accepts, and root then refuses on its
    copy) was measured at its target, every file under it walked (delta
    audit try13, #4). fwalk without `follow_symlinks` walks no link, its top
    included: it reads a folder only after checking that what it opened is
    what it looked at (lstat, open, fstat)."""
    total = 0
    try:
        for _, _, names, fd in os.fwalk(inner):
            for name in names:
                try:
                    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    total += st.st_size
    except OSError:
        pass
    return total


def time_limit(size: int) -> int:
    """How long root's run may take for an app of `size` bytes
    (`app_bytes`): `TIME_LIMIT_SECONDS`, and past `LARGE_APP_BYTES` a second
    more for each `SETUP_BYTES_PER_SECOND`, at most `TIME_LIMIT_MAX_SECONDS`."""
    extra = max(0, int(size) - LARGE_APP_BYTES)
    return min(TIME_LIMIT_MAX_SECONDS, TIME_LIMIT_SECONDS + -(-extra // SETUP_BYTES_PER_SECOND))


def _no_control_characters(text: str, what: str) -> None:
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise AppStoreError(
            f"{what} has a control character in it, which Graft won't pass to a command "
            "run as root",
            hint="rename it, or enroll with another app",
        )


def _owner(uid: int) -> str:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return f"user {uid}"


#: Apple's marker extensions: on a Developer ID Application certificate (the
#: leaf), and on the Developer ID CA that issues it (certificate 1).
DEVELOPER_ID_LEAF = "1.2.840.113635.100.6.1.13"
DEVELOPER_ID_CA = "1.2.840.113635.100.6.2.6"


def requirement(team: str | None, *, developer_id: bool = False) -> str | None:
    """The code requirement root checks its copy of UnfairPlay against: an
    Apple-issued certificate for `team`, and UnfairPlay's identifier; and,
    for a Graft.app signed with Developer ID (`app_is_developer_id`),
    Apple's Developer ID markers, so an Apple Development certificate on the
    same team doesn't satisfy it (review 2.0, bundle #10). A development
    build's requirement goes without them, so it still enrolls. None without
    a team: then nothing is checked, and `EnrollCommand.verified` says so."""
    if team is None:
        return None
    if not _TEAM_RE.match(team):
        raise AppStoreError(f"{team!r} isn't a team identifier")
    found = (
        f'anchor apple generic and identifier "{UNFAIRPLAY_IDENTIFIER}" '
        f'and certificate leaf[subject.OU] = "{team}"'
    )
    if developer_id:
        found += (f" and certificate 1[field.{DEVELOPER_ID_CA}] exists"
                  f" and certificate leaf[field.{DEVELOPER_ID_LEAF}] exists")
    return found


def running_app() -> Path | None:
    """The Graft.app this CLI runs from (`tools.bundled_tools_dir`), or None."""
    helpers = tools.bundled_tools_dir()
    return None if helpers is None else helpers.parent.parent.parent


_TEAM_LOOKUP_HINT = (
    "Graft sets up decryption only once it knows whether root can check UnfairPlay's "
    "signature; try again, and if it keeps failing, download Graft again"
)


def app_team(app: Path) -> str | AdHoc:
    """The team identifier in `app`'s own signature, read at run time
    (`codesign -dv`), so a re-signed build still works.

    Three answers, never a guess: the team; `AD_HOC`, when codesign read the
    signature and it names no team (`TeamIdentifier=not set`); or
    `AppStoreError` for anything else: codesign missing, timing out or
    failing, or printing no TeamIdentifier line Graft can read. A failed
    lookup used to read as ad hoc, which turned a team-signed Graft.app's
    script into one that runs its copy of UnfairPlay unchecked (review #28).
    """
    try:
        proc = subprocess.run(
            ["/usr/bin/codesign", "-dv", "--verbose=2", str(app)],
            capture_output=True, text=True, errors="replace", timeout=30, env=child_env(),
        )
    except subprocess.TimeoutExpired:
        raise AppStoreError(
            f"codesign took too long to read {app.name}'s signature", hint=_TEAM_LOOKUP_HINT
        ) from None
    except (OSError, subprocess.SubprocessError) as e:
        raise AppStoreError(f"couldn't read {app.name}'s signature: {e}", hint=_TEAM_LOOKUP_HINT) from e
    if proc.returncode != 0:
        said = proc.stderr.strip().splitlines()
        why = said[-1] if said else f"codesign exited {proc.returncode}"
        raise AppStoreError(f"couldn't read {app.name}'s signature: {why}", hint=_TEAM_LOOKUP_HINT)
    found = re.findall(r"^TeamIdentifier=(.*)$", proc.stderr, re.M)
    if len(found) != 1:
        raise AppStoreError(f"codesign didn't say which team signed {app.name}", hint=_TEAM_LOOKUP_HINT)
    team = found[0].strip()
    if team == "not set":
        return AD_HOC
    if _TEAM_RE.match(team):
        return team
    raise AppStoreError(
        f"{app.name}'s signature names a team Graft can't read: {team!r}", hint=_TEAM_LOOKUP_HINT
    )


def app_is_developer_id(app: Path) -> bool:
    """Whether `app`'s own signing certificate is Developer ID: its leaf
    carries Apple's Developer ID Application marker and certificate 1, the
    CA that issued it, the Developer ID CA's. Read from the certificates
    `codesign -d --extract-certificates` writes out of the signature. A read
    that fails is an error, never "not Developer ID", which would loosen
    root's check (as #28's failed team lookup did)."""
    from cryptography import x509

    def has(der: Path, oid: str) -> bool:
        try:
            cert = x509.load_der_x509_certificate(der.read_bytes())
            return any(ext.oid.dotted_string == oid for ext in cert.extensions)
        except (OSError, ValueError) as e:
            raise AppStoreError(f"couldn't read {app.name}'s signing certificate: {e}",
                                hint=_TEAM_LOOKUP_HINT) from e

    with tempfile.TemporaryDirectory(prefix="graft-certs-") as tmp:
        prefix = Path(tmp) / "cert"
        try:
            proc = subprocess.run(
                ["/usr/bin/codesign", "-d", f"--extract-certificates={prefix}", str(app)],
                capture_output=True, text=True, errors="replace", timeout=30, env=child_env(),
            )
        except subprocess.TimeoutExpired:
            raise AppStoreError(f"codesign took too long to read {app.name}'s certificates",
                                hint=_TEAM_LOOKUP_HINT) from None
        except (OSError, subprocess.SubprocessError) as e:
            raise AppStoreError(f"couldn't read {app.name}'s certificates: {e}", hint=_TEAM_LOOKUP_HINT) from e
        leaf, issuer = Path(f"{prefix}0"), Path(f"{prefix}1")
        if proc.returncode != 0 or not leaf.is_file():
            said = proc.stderr.strip().splitlines()
            raise AppStoreError(f"couldn't read {app.name}'s certificates: "
                                + (said[-1] if said else f"codesign exited {proc.returncode} and wrote none"),
                                hint=_TEAM_LOOKUP_HINT)
        return has(leaf, DEVELOPER_ID_LEAF) and issuer.is_file() and has(issuer, DEVELOPER_ID_CA)


def root_helper(cfg: AppStoreToolsConfig) -> RootHelper:
    """The UnfairPlay the root script copies and runs, and how root checks it.

    A CLI that runs from Graft.app uses only Graft.app's own
    `Contents/Helpers/bin/unfairplay`, whatever config.yaml says: a path set
    there is one this user can write, and root would run it (review #1).
    Root checks its copy against Graft.app's team, read from Graft.app's own
    signature (`app_team`), and a lookup that fails is an error, never
    "unchecked" (#28). An ad hoc Graft.app has no team, so its helper is
    `UNVERIFIED_AD_HOC`.

    A CLI that isn't Graft.app's (pipx, or a Debug build running the repo's
    CLI) runs what `unfairplay.resolve_tool` finds, which no Graft signature
    vouches for: `UNVERIFIED_NOT_BUNDLED`. Decrypt and status still resolve
    through `unfairplay.resolve_tool`; only the root step is pinned to the
    bundle.
    """
    bundled = tools.bundled_tools_dir()
    if bundled is not None:
        helper = bundled / "unfairplay"
        if not unfairplay.present(helper):
            raise AppStoreError(f"Graft.app is damaged: {helper} is missing", hint="Download Graft again.")
        app = bundled.parent.parent.parent  # Graft.app (`running_app`)
        team = app_team(app)
        if team is AD_HOC:
            return RootHelper(helper, unverified_kind=UNVERIFIED_AD_HOC)
        return RootHelper(helper, team=team, developer_id=app_is_developer_id(app))
    up = unfairplay.resolve_tool(cfg)
    if not unfairplay.present(up.path):
        raise AppStoreError(f"UnfairPlay not found at {up.path}", hint=unfairplay.install_hint(up.path))
    return RootHelper(up.path, unverified_kind=UNVERIFIED_NOT_BUNDLED)


def trusted_location(app: Path) -> None:
    """Refuse `app` unless it is where root will accept it: a real folder
    directly or deeper under /Applications, owned by root, writable by nobody
    else. The root script checks the same again, at run time."""
    if app.is_symlink():
        raise NotACandidate(f"{app} is a symlink; Graft enrolls only with the app itself")
    resolved = app.resolve()
    if not resolved.is_relative_to(TRUSTED_ROOT):
        raise NotACandidate(
            f"{app.name} isn't in {TRUSTED_ROOT}, where the Mac App Store installs apps"
        )
    st = os.lstat(resolved)
    if st.st_uid != TRUSTED_OWNER:
        raise NotACandidate(
            f"{app.name} belongs to {_owner(st.st_uid)}, not the system, so it isn't the "
            "copy the Mac App Store installed"
        )
    if st.st_mode & 0o022:
        raise NotACandidate(f"{app.name} can be changed by other users")


def quote(text: str) -> str:
    """`text` as one single-quoted `/bin/sh` word, always quoted (`'` written
    `'"'"'`), so nothing in it is ever read as syntax."""
    return "'" + text.replace("'", "'\"'\"'") + "'"


def _refuse(status: int, why: str) -> str:
    """The script's `{ echo …; exit N; }` for one of its own refusals."""
    return f"{{ echo {quote('graft: ' + why)} >&2; exit {int(status)}; }}"


_CANCELLED_EARLY = "stopped: the enrollment was canceled before UnfairPlay started"
_CANCELLED = "stopped: the enrollment was canceled while UnfairPlay ran"
_NOT_IN_APPLICATIONS = "the app is not in the Applications folder"
_NOT_ROOTS = "the app is not the system's"
_WRITABLE = "others can change the app"
_COPY_NOT_ROOTS = "root's copy of the app is not the system's"
_COPY_WRITABLE = "others could change the app root copied"
_COPY_NO_PLIST = "the app root copied has no Info.plist it can read"
_COPY_ESCAPES = "the app root copied names an executable outside it"
_COPY_NO_EXECUTABLE = "the app root copied has no executable file where it says"
_NO_FAIRPLAY_STATE = (
    "this Mac has no SC Info.sidb, the App Store state UnfairPlay reads: "
    "launch the app once, then set up again"
)

#: The file in `FAIRPLAY_STATE_DIRS` that UnfairPlay's capture needs.
FAIRPLAY_STATE_FILE = "SC Info.sidb"


def _any_case(name: str) -> str:
    """`name` as a `/bin/sh` pattern that matches it in any case: each
    ASCII letter a bracket of both, a letter, digit, `.`, `_` or `-` as it
    is, and anything else single-quoted (`quote`)."""
    return "".join(
        f"[{c.upper()}{c.lower()}]" if c.isascii() and c.isalpha()
        else c if c.isascii() and (c.isdigit() or c in "._-")
        else quote(c)
        for c in name
    )


def root_script(
    *,
    unfairplay: Path,
    app: Path,
    inner: str,
    uid: int,
    gid: int,
    requirement: str | None,
    cancel_file: Path,
    time_limit: int,
) -> str:
    """The `/bin/sh` script run as root, every value in it a single-quoted
    literal (`quote`): no path, however crafted, can end an argument or start
    a command. Every tool by its absolute path. On any failure the trap
    stops UnfairPlay, waits for it, and removes root's folder; nothing else
    was touched.

    UnfairPlay reads only files from the app it is given, and opens the
    bundle again by path several times; it never runs it (v0.4.0's
    pkg/fairplay enrollment_capture.go and installed_ipa.go). /Applications
    is admin-writable, so after root's checks the app could be swapped
    (review #34). So root clones `inner`, the app inside `Wrapper/` that
    `check` read, into $D, checks the clone again (root's, not writable by
    others; an executable named in its Info.plist that is a plain file inside
    it), and points UnfairPlay at the clone, which nobody else can change.

    Before it copies anything, root looks for this Mac's FairPlay state,
    which UnfairPlay's capture reads (`FAIRPLAY_STATE_DIRS`): a regular file
    named `SC Info.sidb`, in any case, in either folder. Without one, setup
    would run to its end and only then fail ("canonical input has no SIDB
    state"), so root stops first, with `EXIT_NO_FAIRPLAY_STATE`. A folder
    that isn't there is no state, never a failure of its own under `set -e`.
    The look is the shell's own (a glob, a `case` on each name, then `[ -h ]`
    and `[ -f ]`), so there is no child for the watchdog to kill should it
    block; but it only lists two fixed folders, never the admin-writable
    app, opens no file in them and follows no link it finds there.

    UnfairPlay runs in the background, its output to a file in $D rather
    than the pipe the script was given, so a Graft that quits mid-setup
    can't kill it with SIGPIPE.

    A watchdog covers the whole run, from just after the traps: every copy
    and check before UnfairPlay (any of which a swapped file can block: a
    FIFO for `/bin/cp` or plutil to open) as well as setup itself. Once a
    second it tests for `cancel_file` (`EXIT_CANCELLED`) and the time
    (`time_limit` seconds, `EXIT_TIME_LIMIT`). When either comes it leaves a
    marker in $D and sends the script TERM, which ends a `wait` for
    UnfairPlay at once; a shell waiting for a command in the foreground
    takes the trap only when that command ends, so a second later the
    watchdog kills whatever the script is running (its children, and theirs)
    until the script is gone. That holds in every shell /bin/sh can be only
    for a command in the foreground: zsh, as sh, takes TERM at once while it
    reads a command substitution, and runs `leave`, which stops the
    watchdog, so a step blocked inside `$(...)` was left running, orphaned,
    holding the script's stderr, and the admin run never ended (delta audit
    #3). So no step that can block runs inside one: realpath, stat and
    plutil write to `$D/got` in the foreground, and only `/bin/cat` of that
    file (root's, in root's 0700 folder, so never a FIFO) is substituted.
    Killing the script's process group instead was the other way, but
    `do shell script`, its osascript fallback and sudo each give the script
    a group of their own making, which a kill could reach past the script
    into. The watchdog is started from a command
    substitution, so it is not one of the script's own children and never
    kills itself, and `$$` in it is still the script. The EXIT trap
    (`leave`) first stops the watchdog, then UnfairPlay (waiting for it),
    removes $D, and says from the marker which stop it was, with its own
    exit status, whatever status the killed step left. The script itself
    never waits for the watchdog, so nothing the watchdog meets can hold the
    script up.
    """
    q = quote
    if int(time_limit) < 1:
        raise ValueError(f"a time limit of {time_limit} seconds stops UnfairPlay before it starts")
    limit = int(time_limit)
    state = " ".join(f"{q(str(folder))}/*" for folder in FAIRPLAY_STATE_DIRS)
    over_time = f"stopped: the enrollment ran for more than {limit} seconds"
    cancel, over = f'"$D/stop-{EXIT_CANCELLED}"', f'"$D/stop-{EXIT_TIME_LIMIT}"'
    # The script's own Cancel checks: a marker, then out through `leave`,
    # which says why (only it does, so a stop is said once).
    stop_here = f"if cancelled; then : >{cancel}; exit {EXIT_CANCELLED}; fi"
    lines = [
        "set -eu",
        "umask 077",
        f"D=$(/usr/bin/mktemp -d {q(WORK_TEMPLATE)})",
        # UnfairPlay's pid while it runs; whether it has been started; the
        # watchdog's pid.
        "P=",
        "U=",
        "W=",
        # The one place the cancel file is named. Root only ever tests that
        # something exists there (`[ -e ]`): it never opens it, follows it
        # into anything, writes or removes it. So whatever is put there, a
        # file, a link, a folder, can only stop the run, which is what the
        # file is for, and the user could stop it anyway.
        f"cancelled() {{ [ -e {q(str(cancel_file))} ]; }}",
        # UnfairPlay ($1) and what it started. Its only children are
        # /usr/bin/sw_vers and /usr/sbin/ioreg (pkg/fairplay
        # enrollment_capture.go), read-only queries, so they are KILLed
        # first, while still its children; then UnfairPlay gets TERM, a
        # short grace, and KILL.
        'stop() { /usr/bin/pkill -KILL -P "$1" 2>/dev/null || :; /bin/kill -TERM "$1" 2>/dev/null || :; '
        '/bin/sleep 1; /usr/bin/pkill -KILL -P "$1" 2>/dev/null || :; /bin/kill -KILL "$1" 2>/dev/null || :; }',
        # On any way out but success. First nothing more can interrupt it,
        # and the watchdog goes, by the shell's own kill (no child for the
        # watchdog to meet). Then UnfairPlay is stopped and waited for (its
        # job notices dropped) before its folder goes, and a stop the
        # watchdog or a Cancel check marked is said, with its own status.
        "leave() { trap '' HUP INT PIPE TERM; "
        'if [ -n "$W" ]; then kill -KILL "$W" 2>/dev/null || :; fi; '
        'if [ -n "$P" ]; then stop "$P" 2>/dev/null; wait "$P" 2>/dev/null || :; fi; '
        f"X=; if [ -e {cancel} ]; then X={EXIT_CANCELLED}; elif [ -e {over} ]; then X={EXIT_TIME_LIMIT}; fi; "
        'if [ -n "$X" ] && [ -f "$D/log" ]; then /bin/cat "$D/log" >&2 || :; fi; '
        '/usr/bin/chflags -R 0 "$D" 2>/dev/null || :; /bin/rm -rf "$D"; '
        f'case "$X" in {EXIT_CANCELLED}) if [ -n "$U" ]; then echo {q("graft: " + _CANCELLED)} >&2 || :; '
        f"else echo {q('graft: ' + _CANCELLED_EARLY)} >&2 || :; fi; exit {EXIT_CANCELLED} ;; "
        f"{EXIT_TIME_LIMIT}) echo {q('graft: ' + over_time)} >&2 || :; exit {EXIT_TIME_LIMIT} ;; esac; }}",
        "trap leave EXIT",
        # A signal ends the script through `leave` too (a Ctrl-C under sudo,
        # a reader gone from stdout, the watchdog), not past it.
        "trap 'exit 129' HUP",
        "trap 'exit 130' INT",
        "trap 'exit 141' PIPE",
        "trap 'exit 143' TERM",
        # A Cancel pressed while macOS's password dialog was up: nothing runs.
        stop_here,
        # The watchdog, for the whole run from here: once a second the
        # cancel file, then the time. Then the script gets TERM, and
        # whatever it is still running in the foreground a second later is
        # killed, until the script is gone. Its pid comes out of the command
        # substitution, so `leave` can kill it; it is not the script's child.
        "W=$( ( trap - EXIT HUP INT PIPE TERM",
        "  T=0",
        '  while kill -0 "$$" 2>/dev/null; do',
        f"    if cancelled; then : >{cancel}; break; fi",
        f'    if [ "$T" -ge {limit} ]; then : >{over}; break; fi',
        "    /bin/sleep 1",
        "    T=$((T + 1))",
        "  done",
        '  kill -TERM "$$" 2>/dev/null || exit 0',
        '  while /bin/sleep 1 && kill -0 "$$" 2>/dev/null; do',
        '    for C in $(/usr/bin/pgrep -P "$$"); do',
        '      /usr/bin/pkill -KILL -P "$C" 2>/dev/null || :; kill -KILL "$C" 2>/dev/null || :',
        "    done",
        "  done",
        ') </dev/null >/dev/null 2>&1 & echo "$!" )',
        # This Mac's FairPlay state, before anything is copied: a regular
        # file named SC Info.sidb, in any case, in either folder. Each
        # name is matched as a string (`case`), so a folder that isn't there
        # (its `/*` left as it is) or a file named like the pattern is no
        # state; `[ ! -h ]` comes first, so a link there is never followed.
        "I=",
        f"for F in {state}; do case \"${{F##*/}}\" in {_any_case(FAIRPLAY_STATE_FILE)}) "
        'if [ ! -h "$F" ] && [ -f "$F" ]; then I=1; fi ;; esac; done',
        f'[ -n "$I" ] || {_refuse(EXIT_NO_FAIRPLAY_STATE, _NO_FAIRPLAY_STATE)}',
        # Copied first, then the *copy* checked and run: nothing can swap it.
        f'/bin/cp {q(str(unfairplay))} "$D/unfairplay"',
    ]
    if requirement is not None:
        lines.append(f'/usr/bin/codesign --verify --strict -R {q("=" + requirement)} "$D/unfairplay"')
    root = q(str(TRUSTED_ROOT))
    owner = int(TRUSTED_OWNER)
    lines += [
        # What a step that can block prints goes to $D/got, read after it
        # ends: never through `$(...)`, which zsh leaves at once on TERM.
        f'/bin/realpath {q(str(app))} >"$D/got"',
        'A=$(/bin/cat "$D/got")',
        f'case "$A" in {root}/*) ;; *) {_refuse(EXIT_NOT_IN_APPLICATIONS, _NOT_IN_APPLICATIONS)} ;; esac',
        f'[ -d "$A" ] && /usr/bin/stat -f %u "$A" >"$D/got" && [ "$(/bin/cat "$D/got")" = {owner} ] || '
        f"{_refuse(EXIT_NOT_ROOTS, _NOT_ROOTS)}",
        '/usr/bin/stat -f %Sp "$A" >"$D/got"',
        f'case "$(/bin/cat "$D/got")" in ?????w*|????????w*) {_refuse(EXIT_WRITABLE, _WRITABLE)} ;; esac',
        # UnfairPlay gets nothing of the caller's environment (review #2):
        # its work folder is `os.MkdirTemp("", …)`, which is $TMPDIR, and
        # the caller's is a folder this user owns. So its TMPDIR and HOME are
        # root's own folders in $D, root 0700 (umask 077), and SUDO_UID and
        # SUDO_GID, which make it chown what it wrote, are gone. It reads no
        # HOME when given --model-dir; the folder is there for sw_vers and
        # ioreg, the tools it runs, should they look.
        '/bin/mkdir "$D/tmp" "$D/home" "$D/App.app" "$D/App.app/Wrapper"',
        # Root's own copy of what UnfairPlay reads: a clone (APFS
        # copy-on-write, cp -c), so a many-GB app costs metadata, not its
        # size; /Applications and /private/var/tmp are both on the Data
        # volume. -p keeps owner and mode, which are checked next; flags
        # come too, and are cleared so the copy can be removed. Its Wrapper
        # holds only this app, so UnfairPlay can only pick it.
        f'/bin/cp -c -R -p "$A/Wrapper/"{q(inner)} "$D/App.app/Wrapper/App.app"',
        '/usr/bin/chflags -R 0 "$D/App.app"',
        'C="$D/App.app/Wrapper/App.app"',
        f'[ -d "$C" ] && [ ! -h "$C" ] && /usr/bin/stat -f %u "$C" >"$D/got" && '
        f'[ "$(/bin/cat "$D/got")" = {owner} ] || {_refuse(EXIT_NOT_ROOTS, _COPY_NOT_ROOTS)}',
        '/usr/bin/stat -f %Sp "$C" >"$D/got"',
        f'case "$(/bin/cat "$D/got")" in ?????w*|????????w*) {_refuse(EXIT_WRITABLE, _COPY_WRITABLE)} ;; esac',
        # The executable UnfairPlay copies is $C/<CFBundleExecutable>: a
        # name, never a path, so it stays inside the copy.
        f'/usr/bin/plutil -extract CFBundleExecutable raw -o - "$C/Info.plist" >"$D/got" || '
        f"{_refuse(EXIT_NOT_ROOTS, _COPY_NO_PLIST)}",
        'E=$(/bin/cat "$D/got")',
        f"case \"$E\" in ''|.|..|*/*) {_refuse(EXIT_NOT_ROOTS, _COPY_ESCAPES)} ;; esac",
        f'[ -f "$C/$E" ] && [ ! -h "$C/$E" ] || {_refuse(EXIT_NOT_ROOTS, _COPY_NO_EXECUTABLE)}',
        # Not started at all once Cancel is pressed.
        stop_here,
        "U=1",
        '/usr/bin/env -i PATH=/usr/bin:/bin:/usr/sbin:/sbin HOME="$D/home" TMPDIR="$D/tmp/" '
        '"$D/unfairplay" setup --app "$D/App.app" --model-dir "$D/models" </dev/null >"$D/log" 2>&1 &',
        "P=$!",
        # `wait`'s status taken in an || list, which set -e lets through. A
        # Cancel or the time limit ends the wait (the watchdog's TERM), and
        # `leave` stops UnfairPlay, with its job notices dropped there. Not
        # redirected here: zsh runs the trap, and so `leave`, inside this
        # command's redirections, and its message would go with them.
        "S=0",
        'wait "$P" || S=$?',
        "P=",
        'kill -KILL "$W" || :',
        "W=",
        # A stop the watchdog marked just as UnfairPlay ended is still one
        # (`leave` says which).
        f"if [ -e {cancel} ] || [ -e {over} ]; then exit 1; fi",
        # Its JSON report to stderr: stdout is only the folder's path.
        '/bin/cat "$D/log" >&2',
        # UnfairPlay's own failure, never mistaken for one of the script's
        # (every EXIT_*, 64 to 69).
        'case "$S" in 0) ;; 6[4-9]) exit 1 ;; *) exit "$S" ;; esac',
        '/bin/rm -rf "$D/unfairplay" "$D/App.app" "$D/tmp" "$D/home" "$D/log" "$D/got"',
        # Everything in $D was written by root's own UnfairPlay; -h re-owns a
        # link itself, never what it points at.
        f'/usr/sbin/chown -R -h {q(f"{int(uid)}:{int(gid)}")} "$D"',
        "trap - EXIT",
        'echo "$D"',
    ]
    return "\n".join(lines) + "\n"


def cancel_file_path() -> Path:
    """A path for the app to create to stop root's run (`root_script`): a
    fresh random name, in this user's temp folder ($TMPDIR, owner-only on
    macOS), else Graft's state folder. Never created here: the app makes it
    (O_CREAT|O_EXCL) to cancel, and removes it afterward."""
    name = f"graft-enroll-cancel.{secrets.token_hex(8)}"
    tmp = os.environ.get("TMPDIR", "")
    if tmp and os.path.isabs(tmp) and os.path.isdir(tmp) and not any(
        ord(c) < 0x20 or ord(c) == 0x7F for c in tmp
    ):
        return Path(tmp) / name
    state = data_dirs.state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state / name


def prepare_model_dir(model_dir: Path, *, uid: int) -> None:
    """Make `model_dir` (mode 0700, owned by this user), the folder `finish`
    moves the model into. Root never touches it."""
    model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(model_dir)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise AppStoreError(
            f"{model_dir} is not a plain folder", hint="move it aside, then set up decryption again"
        )
    if st.st_uid != uid:
        raise AppStoreError(
            f"{model_dir} belongs to {_owner(st.st_uid)}, not you",
            hint="move it aside, then set up decryption again",
        )
    os.chmod(model_dir, 0o700)


def enroll_command(
    helper: RootHelper,
    app: Path,
    model_dir: Path,
    *,
    cancel_file: Path | None = None,
    uid: int | None = None,
    gid: int | None = None,
    home: Path | None = None,
) -> EnrollCommand:
    """The script that enrolls this Mac with `app`, run as root, copying and
    running `helper` (`root_helper`). `cancel_file` stops it
    (`cancel_file_path` picks one when it is None).

    The only place it is built. The app runs it behind the macOS
    administrator prompt (`AdminShell`); a Terminal user runs it with `sudo
    /bin/sh`. Then `finish`, as the user, moves the model into `model_dir`.

    Checked first, as the user, so a refusal is a sentence rather than a
    root script's exit status: every path absolute and free of control
    characters; the app resolved, passing `check`, and where root will take
    it (`trusted_location`); the model folder this user's (`prepare_model_dir`).
    The root script checks the app again itself.
    """
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    cancel_file = cancel_file_path() if cancel_file is None else cancel_file
    for path, what in ((helper.path, "UnfairPlay's path"), (app, "the app's path"),
                       (model_dir, "the model folder's path"), (cancel_file, "the cancel file's path")):
        _no_control_characters(str(path), what)
        if not path.is_absolute():
            raise AppStoreError(f"{what} must be a full path, got {path}")
    trusted_location(app)
    resolved = app.resolve()
    found = check(resolved, home=home).inner
    inner = found.name
    _no_control_characters(inner, f"{resolved.name}'s inner app")
    prepare_model_dir(model_dir, uid=uid)
    needed = requirement(helper.team, developer_id=helper.developer_id)
    limit = time_limit(app_bytes(found))
    return EnrollCommand(
        app=resolved,
        model_dir=model_dir,
        script=root_script(
            unfairplay=helper.path, app=resolved, inner=inner, uid=uid, gid=gid, requirement=needed,
            cancel_file=cancel_file, time_limit=limit,
        ),
        helper=helper.path,
        verified=needed is not None,
        requirement=needed,
        unverified_kind=helper.unverified_kind,
        unverified_reason=helper.unverified_reason,
        cancel_file=cancel_file,
        time_limit_seconds=limit,
    )


# --- Afterward, as the user ----------------------------------------------------


def _work_name_ok(work: Path) -> bool:
    prefix, x = WORK_TEMPLATE.rstrip("X"), len(WORK_TEMPLATE) - len(WORK_TEMPLATE.rstrip("X"))
    text = str(work)
    return (
        text.startswith(prefix) and len(text) == len(prefix) + x
        and text[len(prefix):].isalnum() and work.is_absolute()
    )


def _plain_folder(path: Path, uid: int, what: str) -> None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise AppStoreError(f"{what} isn't there: {path}") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise AppStoreError(f"{what} is not a plain folder: {path}")
    if st.st_uid != uid:
        raise AppStoreError(f"{what} belongs to {_owner(st.st_uid)}, not you: {path}")


def _read_model(path: Path, uid: int) -> bytes:
    """One file root's UnfairPlay wrote, read without following a link and
    refused unless it is a regular file, with one link, that is this user's."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as e:
        raise AppStoreError(f"{path.name} isn't a plain file ({e.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise AppStoreError(f"{path.name} isn't a plain file")
        if st.st_nlink != 1:
            raise AppStoreError(f"{path.name} has {st.st_nlink} links; a model has one")
        if st.st_uid != uid:
            raise AppStoreError(f"{path.name} belongs to {_owner(st.st_uid)}, not you")
        chunks = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def finish(work: Path, model_dir: Path, *, uid: int | None = None) -> list[str]:
    """Move the model root's UnfairPlay made in `work` into `model_dir`, as
    this user; returns the files' names.

    `work` must be the folder the root script printed: one `WORK_TEMPLATE`
    names, a plain folder this user now owns. Each file is opened without
    following links and must be a regular file with one link, this user's;
    all are read and checked before any is written. They are staged beside
    their place, 0600 (`data_dirs.private_stage`), then renamed in, so a
    failure leaves nothing half-written. `work` is removed only after.
    """
    uid = os.getuid() if uid is None else uid
    if not _work_name_ok(work):
        raise AppStoreError(f"{work} isn't a folder Graft's enrollment made")
    leftover = f"what UnfairPlay made is still in {work}; delete it, then set up decryption again"
    try:
        _plain_folder(work, uid, "the enrollment's folder")
        models = work / "models"
        _plain_folder(models, uid, "the enrollment's model folder")
        names = sorted(os.listdir(models))
        payloads = [(name, _read_model(models / name, uid)) for name in names]
        if not payloads:
            raise AppStoreError("UnfairPlay finished but left no model")
        prepare_model_dir(model_dir, uid=uid)
    except AppStoreError as e:
        raise AppStoreError(e.message, hint=leftover) from e
    staged: list[tuple[Path, Path]] = []
    try:
        for name, data in payloads:
            dest = model_dir / name
            staged.append((data_dirs.private_stage(data, dest), dest))
        for tmp, dest in staged:
            os.replace(tmp, dest)
    except OSError as e:
        for tmp, _ in staged:
            tmp.unlink(missing_ok=True)
        raise AppStoreError(f"couldn't save the model in {model_dir}: {e.strerror}", hint=leftover) from e
    shutil.rmtree(work)
    return names
