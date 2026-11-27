"""Run ipatool (Graft.app's own copy, or the one config.yaml or PATH names), and
say what its failures mean.

ipatool (github.com/majd/ipatool) downloads the App Store's *encrypted,
account-personalized* IPA for an app the signed-in Apple ID owns. This module
runs `--version`, `auth info`, `auth revoke`, `download`, `purchase`,
`list-versions` and `get-version-metadata` (the last two for an app's older
versions, `appstore.list_versions`).
`auth login` is `appstore_login`'s: it types the Apple ID, the password and
the 2FA code into ipatool's own prompts under a pseudo-terminal, so none of
them is ever on argv or in the environment, and Graft saves none of them.
`LOGIN_HINT` is how a signed-out hint names it. `purchase` is only ever the
$0 "Get" for an app the iTunes lookup says is free (`appstore.claim`).

Pinned to **2.8.0**, which is what was installed and proven on 2026-11-17.
2.8.1 (Homebrew's current at the time) breaks `download` with "available
storefronts request failed" (majd/ipatool#672); `KNOWN_BAD_VERSIONS` is how status
and errors say so, wherever the binary was found (`locate`).

`run` is injectable for the odd unit test, but the real seam is the path: tests
point it at a fake executable that prints what the real one prints.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import appstore_account, child_env, tools
from .errors import OUT_OF_SPACE, AppStoreError, out_of_space

GOOD_VERSION = "2.8.0"
KNOWN_BAD_VERSIONS = frozenset({"2.8.1"})
RELEASE_URL = f"https://github.com/majd/ipatool/releases/tag/v{GOOD_VERSION}"

#: Where `bad_version_hint` suggests putting a good ipatool when Graft.app's
#: own is bad: anywhere but the old default (`AppStoreToolsConfig.explicit`).
OVERRIDE_PATH = f"~/.local/share/graft/ipatool-{GOOD_VERSION}/ipatool"

Runner = Callable[..., subprocess.CompletedProcess[str]]

_VERSION_RE = re.compile(r"version\s+v?(\S+)")


#: ipatool 2.8 runs every App Store request through an emulator on Unicorn,
#: which it downloads on first use from PyPI into ~/Library/Caches/ipatool,
#: as upstream does; Graft.app bundles none (the combined fix check's
#: medium #2). Offline, that first download fails, and these are the words
#: its errors carry (ipatool 2.8.0's internal/sap/unicorn/engine.go:96 and
#: cache.go:132, wrapped by internal/store/backend.go:221).
UNICORN_FAILURES = ("load Unicorn", "download Unicorn")
UNICORN_FIRST_USE = (
    "ipatool needs to download its App Store emulator (Unicorn, from PyPI) the first time; connect to "
    "the internet and try again."
)


def unicorn_failure(error: str) -> AppStoreError | None:
    """The error to raise when ipatool's `error` is its Unicorn download or
    load failing, else None."""
    if not any(words in error for words in UNICORN_FAILURES):
        return None
    return AppStoreError(UNICORN_FIRST_USE, hint=f"ipatool said: {error}")


#: ipatool 2.8.0 takes the App Store's frameworks, the first time a Mac
#: needs them, from one macOS restore image at an address its source fixes
#: (internal/sap/assets/assets.go:16-19: macOS 27.0.1, 26A434). It reads
#: only the parts it needs, by HTTP range requests, and fetches the key to
#: the image's encrypted parts from wkms-public.apple.com (aea.go). Each of
#: the seven files it takes is checked against a size and SHA-256 its source
#: pins, and its emulator runs only those exact files (internal/store/
#: emulator/runtime.go), so no other image can stand in: moving to another
#: takes a newer ipatool, and so a newer Graft.
#:
#: These are the words its errors carry when that file is gone or changed.
#: An HTTP refusal of the image or of its key (http_range.go:39 and :124,
#: aea.go:276): Apple's host is an S3 origin, which answers a missing file
#: with 403 Forbidden rather than 404 (checked against a made-up path on
#: 2026-11-24). Or an image whose parts are no longer the pinned ones
#: (remote.go:146 and :152, assets.go:237 and :241). A dropped connection
#: or a server error is left out on purpose: trying again can fix those,
#: and a newer Graft can't.
_RESTORE_IMAGE_GONE = re.compile(
    r"(?:open remote image|read remote image range|download Apple FCS key): server returned (?:403|404|410)\b"
    r"|restore image does not contain "
    r"|restore member \S+ does not match the pinned image"
    r"|apple SAP asset \S+ (?:failed integrity verification|has size \d+, expected)",
    re.IGNORECASE,
)
RESTORE_IMAGE_GONE = (
    "ipatool couldn't download the App Store components it needs: Apple no longer serves the "
    "macOS 27.0.1 (26A434) restore image that ipatool 2.8.0 takes them from. A newer Graft will "
    "fix this, and Graft's update notice says when one is out (Help › Check for Updates…, or "
    "`graft --check-update`)."
)


def restore_image_failure(error: str) -> AppStoreError | None:
    """The error to raise when ipatool's `error` says its pinned restore
    image is gone or no longer the one it pins, else None."""
    if _RESTORE_IMAGE_GONE.search(error) is None:
        return None
    return AppStoreError(RESTORE_IMAGE_GONE, hint=f"ipatool said: {error}")


def run_ipatool(
    path: Path, args: list[str], *, run: Runner = subprocess.run, **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run the ipatool at `path` with `args`: the one way this package starts
    ipatool.

    The environment is built here, never by the caller (`env` is refused):
    `child_env`'s, with Graft's secrets out of it. One route for every
    subcommand, so a change to what ipatool is given reaches all of them;
    a `purchase` merged separately once ran with less than the others
    (the combined fix check's medium #1). `tests/test_ipatool_env.py` checks
    that nothing in graft_ipa starts ipatool any other way.

    Its stdin is /dev/null unless the caller hands it one: only `auth
    login` does (a pty, `appstore_login`). `--appstore-login` runs
    `--version` before it reads the Apple ID and the password from stdin,
    and a child that inherited fd 0 could read them first (the whole-diff
    review's signin-2); no other run has a use for it.
    """
    if "env" in kwargs:
        raise TypeError("run_ipatool builds ipatool's environment itself")
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return run([str(path), *args], env=child_env.child_env(), **kwargs)


#: An http(s) URL's query and fragment, up to whitespace or a quote: Go's
#: errors quote the URL they failed on.
_URL_QUERY = re.compile(r"(\bhttps?://[^\s\"'<>?#]*)[?#][^\s\"'<>]*", re.IGNORECASE)


def without_url_queries(text: str) -> str:
    """`text` with every http(s) URL's query string (and fragment) cut to
    `?…`, its host and path kept, so an error still says where it failed.

    ipatool's errors can carry a version's signed download URL: Go's
    transport names the URL a request failed on (`Get "https://iosapps.
    itunes.apple.com/…?accessKey=…": net/http: timeout awaiting response
    headers`), and that query is what lets anyone fetch the file. Applied
    where ipatool's words first become a Python string (`_failure`), and
    again by what explains them, so no message, hint, `--json` event,
    stderr line or file written from one carries it.
    """
    return _URL_QUERY.sub(r"\1?…", text)


def present(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


#: How a Terminal user signs in since 2.0 (`appstore_login`): Graft asks for
#: the Apple ID and the password and types them, and the 2FA code if Apple
#: asks for one, into ipatool's own prompts. None of them is an argument.
#: The app has a button for it.
LOGIN_COMMAND = "graft --appstore-login"
#: The hint wherever ipatool turns out to be signed out.
LOGIN_HINT = (
    "sign in to the App Store in Graft (Settings › App Store), or in Terminal: "
    f"{LOGIN_COMMAND}"
)
#: The hint for a sign-in that expired and needs a code to renew. Status
#: still reads it signed in (`auth info` only reads the keychain), so the
#: Settings row shows "Sign In Again…" beside Sign Out, not "Sign In…" (the
#: fix re-check's problem 13).
RENEW_HINT = (
    "sign in again in Graft (Settings › App Store › Sign In Again…), or in Terminal: "
    f"{LOGIN_COMMAND}"
)
#: The same, with what a first sign-in looks like.
SIGN_IN_HINT = (
    f"{LOGIN_HINT} — the first sign-in on a Mac downloads ipatool's App Store "
    "components and can sit silent for several minutes; that is normal"
)

#: After a denied Keychain dialog ipatool says the item "could not be found
#: in the keyring" (Task 0, K1). It is there; ipatool wasn't let read it.
#: Never shown as "not found" (decision 10).
KEYCHAIN_REFUSED = (
    "macOS's Keychain didn't let Graft's ipatool read the App Store sign-in in your "
    "login keychain"
)
KEYCHAIN_REFUSED_HINT = (
    "ipatool calls this \"not found\", but the sign-in is there: a Keychain dialog was "
    "denied, or another copy of ipatool (Graft 1.x's own) made it, and macOS lets only "
    f"that copy read it without asking. {LOGIN_HINT}; signing in replaces it"
)


#: ipatool said "not found", and `security` couldn't say whether the item is
#: there: neither a refusal nor "signed out" (the 2.0 review's #14).
KEYCHAIN_UNKNOWN = (
    "couldn't tell whether ipatool is signed in: it found no sign-in, and your login "
    "keychain didn't answer when Graft checked"
)
KEYCHAIN_UNKNOWN_HINT = (
    "unlock the login keychain in Keychain Access (or restart this Mac if it won't "
    "unlock), then try again"
)


def keychain_refused_text(path: Path, source: str | None = None) -> tuple[str, str]:
    """The message and hint for the Keychain refusing the ipatool at `path`
    an item that is there. Graft.app's own ipatool gets `KEYCHAIN_REFUSED`:
    its sign-in deletes that item first. Any other (config.yaml's, or one on
    PATH) deletes only the items Graft's record vouches for, and this isn't
    one, so its sign-in saves over the item and macOS may ask; it is named,
    as status names it (`appstore.status`, its `elif legacy:` branch). The
    try14 delta audit, signin-B.

    Graft.app's Settings › App Store is offered too only where Graft.app
    would sign in with this same ipatool: one set in config.yaml, which
    Graft.app reads too (`source`, when the caller knows it; inside
    Graft.app no other ipatool can be the one refused). A pipx `graft`'s
    from PATH or ~/.local/bin isn't Graft.app's: there, Graft.app signs in
    with its own, which deletes the item instead of saving over it, and this
    ipatool still can't read the new one (the try15 delta audit's d15-py-3).
    """
    if is_bundled(path):
        return KEYCHAIN_REFUSED, KEYCHAIN_REFUSED_HINT
    graft_app_runs_it = source == tools.SOURCE_CONFIG or (
        source is None and tools.bundled_tools_dir() is not None
    )
    how = LOGIN_HINT if graft_app_runs_it else f"sign in again with it, in Terminal: {LOGIN_COMMAND}"
    return (
        f"macOS's Keychain didn't let the ipatool at {path} read the App Store sign-in "
        "in your login keychain",
        "ipatool calls this \"not found\", but the sign-in is there: a Keychain dialog was "
        "denied, or another ipatool made it, and macOS lets only that one read it without "
        f"asking. {how[0].upper()}{how[1:]}; that saves over it, and macOS may ask for your "
        "login keychain password while it does",
    )


class KeychainRefusal(AppStoreError):
    """ipatool's "not found" that isn't one: `refused` when the item is there
    (a denied dialog, or another program's item), else `security` couldn't
    say whether it is. `path` is the ipatool that was refused, and `source`
    the rung it was found on, when the caller knows it
    (`keychain_refused_text`)."""

    def __init__(self, *, refused: bool, path: Path, source: str | None = None) -> None:
        if refused:
            message, hint = keychain_refused_text(path, source)
            super().__init__(message, hint=hint)
        else:
            super().__init__(KEYCHAIN_UNKNOWN, hint=KEYCHAIN_UNKNOWN_HINT)
        self.refused = refused


def is_bundled(path: Path) -> bool:
    """Whether `path` is Graft.app's own ipatool (`graft_app_ipatool`)."""
    return graft_app_ipatool(path) is not None


#: Graft.app's bundle identifier. How a `graft` outside the app knows an
#: ipatool inside some Graft.app is that app's own (`graft_app_ipatool`).
GRAFT_APP_BUNDLE_ID = "com.greenglasst.graft"


def _same_file(one: Path, other: Path) -> bool:
    try:
        return os.path.samefile(one, other)
    except OSError:
        return False


def _bundle_id(app: Path) -> str | None:
    try:
        with (app / "Contents" / "Info.plist").open("rb") as f:
            info = plistlib.load(f)
    except Exception:
        # Any way a plist fails to load, a binary one by recursing too, is
        # an app that names no identifier.
        return None
    found = info.get("CFBundleIdentifier") if isinstance(info, dict) else None
    return found if isinstance(found, str) else None


def graft_app_ipatool(path: Path) -> Path | None:
    """Graft.app's own ipatool, by the path Graft runs it by, when `path` is
    that file; else None. The same binary made Graft's items, and the
    Keychain lets it read them, however it is reached.

    - **Inside Graft.app**: the same file as its bundled ipatool, by device
      and inode (`os.path.samefile`). A link to it, a hard link, a spelling
      in another case and the /System/Volumes/Data spelling are all it;
      comparing realpaths missed the last three (the try15 delta audit's
      d15-py-2).
    - **Outside it** (a pipx `graft`, which has no app of its own): the
      ipatool of the Graft.app that `path` really lies in, named by its
      bundle identifier. Graft.app counts that path as its own, and the two
      share the state folder, so a pipx `graft` that didn't deleted Graft's
      items at each sign-in, and Graft.app deleted that one's in turn
      (d15-py-1). A hard link outside the app can't be traced back to it
      from here, and stays what its rung says.
    """
    bundled = tools.bundled_tools_dir()
    if bundled is not None:
        own = bundled / "ipatool"
        return own if present(own) and _same_file(Path(path), own) else None
    real = Path(os.path.realpath(path))
    app = next((p for p in real.parents if p.suffix.lower() == ".app"), None)
    if app is None:
        return None
    own = app / "Contents" / "Helpers" / "bin" / "ipatool"
    if present(own) and _same_file(real, own) and _bundle_id(app) == GRAFT_APP_BUNDLE_ID:
        return own
    return None


def keychain_refusal(error: str, path: Path) -> KeychainRefusal | None:
    """The error to raise when ipatool's "not found" is the Keychain refusing
    the ipatool at `path` an item that is there, or when `security` can't say
    whether it is; else None (a plain "signed out").

    When the refused ipatool is Graft's bundled one, Graft's record of its
    items is forgotten with it: whatever they now are, it can't read them, so
    the next status doesn't ask it (no more dialogs) and the next sign-in
    deletes them first (`appstore_account.needs_clearing`). Any other ipatool
    is the user's own, refused an item it didn't make; that says nothing
    against Graft's record, which stays. It isn't asked about an item the
    record vouches for at all (`appstore_account.graft_signin`), so this is
    one the record doesn't know: another ipatool's, or Graft's own from
    before a record was lost.
    """
    refused = appstore_account.keychain_refused(error)
    if refused is False:
        return None
    if refused and is_bundled(path):
        appstore_account.forget_signed_in()
    return KeychainRefusal(refused=bool(refused), path=path)


def default_path() -> Path:
    """Where Graft looks after the configured path, and where it says to install."""
    return Path.home() / ".local" / "bin" / "ipatool"


def mac_arch() -> str:
    """The release asset's architecture for this Mac: arm64 or amd64.

    Asked of the kernel (`host.apple_silicon`), so a Python running under
    Rosetta still gets the arm64 binary; `platform.machine` only when the
    kernel would not say.
    """
    from . import host

    arm = host.apple_silicon()
    if arm is None:
        arm = platform.machine() in ("arm64", "aarch64")
    return "arm64" if arm else "amd64"


def install_steps(target: Path, arch: str | None = None) -> list[str]:
    """The Terminal commands that put ipatool GOOD_VERSION at `target`.

    The macOS release asset for this Mac's processor (`mac_arch`), unpacked into
    the user's own `$TMPDIR` — the tarball holds `bin/ipatool-<v>-macos-<arch>`
    — made executable, and moved into place. Printed by `--appstore-status`,
    never run by Graft.
    """
    name = f"ipatool-{GOOD_VERSION}-macos-{arch or mac_arch()}"
    url = f"https://github.com/majd/ipatool/releases/download/v{GOOD_VERSION}/{name}.tar.gz"
    return [
        f"mkdir -p {shlex.quote(str(target.parent))}",
        f'curl -fL {url} | tar -xzf - -C "$TMPDIR" bin/{name}',
        f'chmod +x "$TMPDIR/bin/{name}"',
        f'mv "$TMPDIR/bin/{name}" {shlex.quote(str(target))}',
    ]


def install_command(target: Path, arch: str | None = None) -> str:
    """`install_steps` as one command, for the GUI's Copy button."""
    return " && ".join(install_steps(target, arch))


def install_target(located: Path, configured: Path) -> Path:
    """Where installing GOOD_VERSION fixes what `locate` found.

    The located binary itself when it is the configured one or the default one
    (replacing it is the fix); otherwise — a bad one on PATH — the default,
    which `locate` prefers to PATH.
    """
    return located if located in (configured, default_path()) else default_path()


def _resolve(configured: Path, explicit: bool) -> tools.Resolved:
    """`tools.resolve` for ipatool, with Graft.app's own ipatool reached
    another way counted as what it is.

    A configured path, or one found on PATH or in ~/.local/bin, that is
    Graft.app's own ipatool (`graft_app_ipatool`: its own path, as
    `--appstore-status` prints it, or another way to the same file) runs the
    same binary, which the Keychain lets read the items it made. So it is
    the bundled rung, run by its own path, from Graft.app and from a pipx
    `graft` alike. Counted by the rung instead, Graft's own items were
    another program's to it: status and a Get refused them, and a sign-in
    deleted them (the delta audit's signin-A). A copy elsewhere stays what
    its rung says: whether the Keychain takes it for the same program
    depends on its signature, which isn't checked here.
    """
    # PATH is searched by absolute entries only (`tools.which`): the binary
    # found there is handed the signed-in Apple ID's session, and a relative
    # entry would let one in the current directory answer for it.
    r = tools.resolve(
        "ipatool", configured=configured if explicit else None, default=default_path()
    )
    if r.source == tools.SOURCE_BUNDLED or r.path is None:
        return r
    own = graft_app_ipatool(r.path)
    if own is not None:
        return tools.Resolved("ipatool", own, tools.SOURCE_BUNDLED)
    return r


def locate(configured: Path, *, explicit: bool = False) -> Path:
    """Where the ipatool to run is (see `tools.resolve`): the user's own
    choice, then Graft.app's bundled copy, then ~/.local/bin, then PATH.

    `explicit` is whether the user set `configured` on purpose
    (`AppStoreToolsConfig.explicit`); only then does it beat a bundled copy,
    and it is used even when missing. A Homebrew install lives on PATH
    (/opt/homebrew/bin). When nothing is found, `configured` comes back
    unchanged, so "not found at …" names the place the user was told to put it.
    """
    r = _resolve(configured, explicit)
    if r.path is not None and (r.source == tools.SOURCE_CONFIG or r.found):
        return r.path
    return configured


def source_of(configured: Path, *, explicit: bool) -> str:
    """Which rung `locate` answered from ("bundled", "PATH", ...), for status."""
    return _resolve(configured, explicit).source


def install_hint(path: Path) -> str:
    """For an error message: where to install it, and where the commands are.

    The commands themselves are long, and the app shortens a banner's hint
    from the middle; `--appstore-status` prints them whole, and its event
    carries them as `install`.
    """
    return (
        f"install ipatool {GOOD_VERSION} to {path} — `graft --appstore-status` "
        "prints the commands"
    )


def bad_version_hint(
    bad: str, *, path: Path | None = None, configured: Path | None = None,
    source: str | None = None,
) -> str:
    """What to do about a known-bad ipatool, worded to stay true as releases move.

    `path` is where the bad one was found, `configured` is `appstore.ipatool`
    and `source` is the rung `locate` answered from (`source_of`). Installing
    GOOD_VERSION to ~/.local/bin is the fix only where that copy would be run:
    not when the bad one is Graft.app's own (`tools.resolve` runs the bundled
    copy first), and not when it is the configured non-default path (looked at
    before both). Those hints name what does take effect instead.
    """
    head = (
        f"Graft is tested with ipatool {GOOD_VERSION}; {bad} has a download bug "
        f"(majd/ipatool#672)."
    )
    if source == tools.SOURCE_BUNDLED:
        # Not ~/.local/bin/ipatool, the path every other hint installs to: a
        # config naming the old default is read as no choice at all
        # (`AppStoreToolsConfig.explicit`), so the bundled copy still won and
        # the advice could never take effect.
        return (
            f"{head} This copy of Graft.app ships ipatool {bad}, and Graft runs that "
            f"before any other: download Graft again, or put an ipatool {GOOD_VERSION} "
            f"binary from {RELEASE_URL} at {OVERRIDE_PATH} and set appstore.ipatool "
            f"in config.yaml to that path."
        )
    if path is not None and path == configured and configured != default_path():
        return (
            f"{head} appstore.ipatool in config.yaml points at {path}, so that is the one "
            f"Graft runs: replace it with ipatool {GOOD_VERSION} from {RELEASE_URL}, or "
            f"point appstore.ipatool at a {GOOD_VERSION} binary."
        )
    return (
        f"{head} Install ipatool {GOOD_VERSION} from {RELEASE_URL} to "
        "~/.local/bin/ipatool, which Graft uses before one on PATH, or set appstore.ipatool."
    )


def version(path: Path, *, run: Runner = subprocess.run) -> str | None:
    """The version ipatool reports, or None if it isn't there or won't say."""
    if not present(path):
        return None
    try:
        proc = run_ipatool(path, ["--version"], run=run, capture_output=True, text=True,
                           timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _VERSION_RE.search(f"{proc.stdout}\n{proc.stderr}")
    return match.group(1) if match else None


def _json_lines(*streams: str) -> list[dict[str, Any]]:
    """Every line of `streams` that is a JSON object, in order.

    `--format json` is one object per line, but which stream carries the error
    line has not been pinned down across versions — reading both costs nothing
    and a line on the "wrong" one is not a reason to report a blank error.
    """
    out: list[dict[str, Any]] = []
    for stream in streams:
        for line in stream.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


#: `Account.unknown`, and `appstore-status`'s `ipatool.unknown_reason`: why
#: `signed_in` is None. `auth info` hung, crashed or printed no verdict:
UNKNOWN_NO_VERDICT = "no-verdict"
#: the login keychain didn't answer when Graft asked about ipatool's items:
UNKNOWN_KEYCHAIN = "keychain-no-answer"
#: the login keychain is locked, so neither `security` nor ipatool was asked
#: (`keychain.login_keychain_locked`):
UNKNOWN_LOCKED = "keychain-locked"
#: the sign-in is Graft.app's own ipatool's (`appstore_account.graft_signin`),
#: and this ipatool, any other, can't read it without Keychain dialogs, so it
#: wasn't asked (signin-1). Its sentence names the ipatool, so it is
#: `graft_signin_message`, not in `UNKNOWN_MESSAGES`:
UNKNOWN_GRAFT_SIGNIN = "graft-ipatool-signin"
#: `appstore-status`'s `ipatool.unknown_message`: each reason as the sentence
#: the app shows, with what to do.
UNKNOWN_MESSAGES = {
    UNKNOWN_NO_VERDICT: (
        "Couldn't tell whether ipatool is signed in: it gave no answer when Graft asked. "
        "Refresh to ask again."
    ),
    UNKNOWN_KEYCHAIN: (
        "Couldn't tell whether ipatool is signed in: your login keychain didn't answer when "
        "Graft checked. Unlock it in Keychain Access, then refresh."
    ),
    UNKNOWN_LOCKED: (
        "Couldn't tell whether ipatool is signed in: your login keychain is locked. Unlock it "
        "in Keychain Access, then refresh."
    ),
}


def _bundled_ipatool_here() -> bool:
    bundled = tools.bundled_tools_dir()
    return bundled is not None and present(bundled / "ipatool")


def _graft_signin_parts(path: Path, source: str) -> tuple[str, str]:
    """What `graft_signin_message` and `graft_signin_error` say: whose the
    sign-in is and which ipatool can't read it, then what to do. Removing
    appstore.ipatool brings Graft.app's own ipatool back only inside
    Graft.app; a pipx `graft` runs ~/.local/bin's or PATH's then. And
    Graft.app reads the same config.yaml, so from a pipx `graft` with the
    setting, "use Graft.app" alone would meet the same ipatool there: it
    says the setting has to go for Graft.app too (the delta audit's
    signin-C)."""
    where = {tools.SOURCE_CONFIG: ", set in config.yaml,", tools.SOURCE_PATH: ", found on PATH,"}.get(source, "")
    head = (
        f"Graft.app's own ipatool holds this App Store sign-in; the ipatool at {path}{where} can't "
        "read it without Keychain dialogs"
    )
    if source == tools.SOURCE_CONFIG and _bundled_ipatool_here():
        other = "remove appstore.ipatool from config.yaml to use Graft.app's"
    elif source == tools.SOURCE_CONFIG:
        other = (
            "use Graft.app with appstore.ipatool removed from config.yaml: Graft.app runs its own "
            "ipatool, which reads it, unless that setting names another"
        )
    else:
        other = "use Graft.app, whose own ipatool reads it"
    return head, f"sign in again to give that ipatool its own sign-in (it replaces Graft.app's), or {other}"


def graft_signin_message(path: Path, source: str) -> str:
    """`unknown_message` for `UNKNOWN_GRAFT_SIGNIN`: the sentence the app's
    account row shows, naming both ipatools."""
    head, todo = _graft_signin_parts(path, source)
    return f"{head}, so Graft didn't ask it. {todo[0].upper()}{todo[1:]}."


def graft_signin_error(path: Path, source: str) -> AppStoreError:
    """A Get or a claim through that ipatool, refused before it runs."""
    head, todo = _graft_signin_parts(path, source)
    return AppStoreError(head, hint=todo)


#: A Get, a claim or a sign-out with the login keychain locked: refused
#: before anything asks it.
KEYCHAIN_LOCKED = "couldn't tell whether ipatool is signed in: your login keychain is locked"
KEYCHAIN_LOCKED_HINT = "unlock the login keychain in Keychain Access, then try again"


@dataclass(frozen=True)
class Account:
    """What `auth info` said: signed in (True, False, or None for "couldn't
    tell"), as which email, and whether the Keychain refused ipatool its
    sign-in (`keychain_refusal`; then `signed_in` is None). `unknown` says why
    a None that isn't a refusal is one (`UNKNOWN_*`)."""

    signed_in: bool | None
    email: str | None = None
    refused: bool = False
    unknown: str | None = None


def account(path: Path, *, run: Runner = subprocess.run, timeout: float = 30) -> Account:
    """Whether ipatool has an account in the keychain, and its email.

    `signed_in` None is its own answer: an `auth info` that hung, could not
    run, or printed no verdict says nothing about the account, and treating
    it as signed out would send the user to sign in for nothing. Nor is a
    Keychain refusal "signed out", whatever ipatool's words (K1).

    `auth info` prints the account's email and name. The email is kept,
    because Settings shows who is signed in. The name is never kept,
    returned or logged.
    """
    if not present(path):
        return Account(False)
    try:
        proc = run_ipatool(
            path, ["auth", "info", "--format", "json"],
            run=run, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return Account(None, unknown=UNKNOWN_NO_VERDICT)
    verdicts = [obj for obj in _json_lines(proc.stdout, proc.stderr) if "success" in obj]
    if not verdicts:
        return Account(None, unknown=UNKNOWN_NO_VERDICT)
    good = next((obj for obj in verdicts if obj.get("success") is True), None)
    if good is None:
        error = " ".join(str(obj.get("error") or "") for obj in verdicts)
        if (refusal := keychain_refusal(error, path)) is not None:
            if refusal.refused:
                return Account(None, refused=True)
            return Account(None, unknown=UNKNOWN_KEYCHAIN)
        return Account(False)
    email = good.get("email")
    return Account(True, email if isinstance(email, str) and email else None)


def signed_in(path: Path, *, run: Runner = subprocess.run, timeout: float = 30) -> bool | None:
    """`account(path).signed_in`, for the callers that need only yes or no."""
    return account(path, run=run, timeout=timeout).signed_in


def revoke(path: Path, *, run: Runner = subprocess.run) -> None:
    """`auth revoke`: remove ipatool's sign-in from the keychain. Nothing to
    remove counts as signed out."""
    if not present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=install_hint(path))
    try:
        proc = run_ipatool(
            path, ["auth", "revoke", "--format", "json", "--non-interactive"],
            run=run, capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise AppStoreError(f"couldn't run ipatool at {path}: {e}") from e
    verdict = _verdict(proc)
    if verdict is not None and verdict.get("success") is True:
        return
    error = _failure(proc, verdict)
    if "could not be found" in error.lower():
        return
    raise AppStoreError(f"ipatool couldn't sign out: {error}")


#: What ipatool's `Login` says when Apple wants a 2FA code and none was given
#: (plan, ipatool 2.8.0 table). A purchase or a download signs in again by
#: itself when Apple expires the session, with the password ipatool keeps,
#: and `--non-interactive`; if Apple wants a code for that, this is the error.
AUTH_CODE_REQUIRED = "auth code is required"


def _expired(error: str) -> AppStoreError | None:
    """The error for ipatool's "password token is expired", else None.

    Signed in still, as far as Settings can tell (`auth info` reads only the
    keychain), which shows Sign In Again… for it: so that's the advice, not a
    first sign-in's (`SIGN_IN_HINT`, the 2.3.1 review's T2-A2)."""
    lower = error.lower()
    if "token" not in lower or "expired" not in lower:
        return None
    return AppStoreError(f"ipatool's App Store sign-in has expired (ipatool: {error})",
                         hint=RENEW_HINT)


def _renewal_needs_code(error: str, *, hint: str) -> AppStoreError:
    return AppStoreError(
        "ipatool's App Store sign-in has expired, and Apple wants a 2FA code to renew it, "
        f"which only signing in again can give (ipatool: {error})",
        hint=hint,
    )


def explain_purchase(error: str, *, bundle_id: str, path: Path) -> AppStoreError:
    """ipatool's `purchase` errors, in words (pkg/appstore/appstore_purchase.go)."""
    error = without_url_queries(error)
    if (first_use := unicorn_failure(error) or restore_image_failure(error)) is not None:
        return first_use
    lower = error.lower()
    if "paid apps" in lower:
        return AppStoreError(
            f"{bundle_id} isn't free, so ipatool won't get it",
            hint="buy it in the App Store with the same Apple ID",
        )
    if "not in this store" in lower or "not valid for use in the" in lower:
        return AppStoreError(
            f"Apple says this Apple ID belongs to a different country's App Store, so it "
            f"wouldn't hand over {bundle_id} (ipatool: {error})",
            hint=(
                "tap Get for it on an iPhone or iPad signed in to the same Apple ID, then "
                "get it in Graft (majd/ipatool#666)"
            ),
        )
    if AUTH_CODE_REQUIRED in lower:
        return _renewal_needs_code(error, hint=RENEW_HINT)
    if (expired := _expired(error)) is not None:
        return expired
    if "token" in lower or "keyring" in lower or "not logged in" in lower:
        if (refused := keychain_refusal(error, path)) is not None:
            return refused
        return AppStoreError(
            f"ipatool is not signed in to the App Store (ipatool: {error})",
            hint=LOGIN_HINT,
        )
    if "subscription required" in lower:
        return AppStoreError(f"{bundle_id} needs an Apple Arcade subscription")
    if "temporarily unavailable" in lower:
        return AppStoreError(f"{bundle_id} isn't available right now", hint="try again later")
    return AppStoreError(f"ipatool couldn't get {bundle_id}: {error}")


def purchase(path: Path, bundle_id: str, *, run: Runner = subprocess.run) -> bool:
    """`purchase -b BUNDLE`: the App Store's $0 "Get" for the signed-in Apple
    ID. Returns whether it already owned the app.

    ipatool refuses a paid app itself ("purchasing paid apps is not
    supported"). `appstore.claim` checks the price first, so that refusal is a
    second lock, not the only one.
    """
    if not present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=install_hint(path))
    try:
        proc = run_ipatool(
            path, ["purchase", "-b", bundle_id, "--format", "json", "--non-interactive"],
            run=run, capture_output=True, text=True, timeout=180,
        )
    except subprocess.TimeoutExpired as e:
        raise AppStoreError(
            "ipatool's purchase didn't answer within 3 minutes", hint="check the connection and try again"
        ) from e
    except OSError as e:
        raise AppStoreError(f"couldn't run ipatool at {path}: {e}") from e
    verdict = _verdict(proc)
    if verdict is not None and verdict.get("success") is True:
        return verdict.get("alreadyOwned") is True
    raise explain_purchase(_failure(proc, verdict), bundle_id=bundle_id, path=path)


class LicenseRequired(AppStoreError):
    """`download` refused: the Apple ID doesn't own the app (`license is
    required`). Its own class so `appstore.fetched` can turn it into the
    `needs-license` refusal, with the price."""


def account_error(error: str, *, bundle_id: str, path: Path) -> AppStoreError | None:
    """The error to raise when ipatool's `error` isn't about this app or this
    version at all, else None: ipatool's first-use downloads failing, a
    sign-in that expired, is gone or the Keychain refused, or a license the
    Apple ID doesn't have. Every other request this run makes would fail the
    same way, so a version list stops on these (`version_metadata`) rather
    than saying it once per version.

    Matched on words rather than whole sentences: these are ipatool's own Go
    error strings (`license is required`, `password token is expired`, the
    keyring lookup's "could not be found in the keyring"), which are stable
    across 2.x but wrapped differently by different commands.
    """
    error = without_url_queries(error)
    if (first_use := unicorn_failure(error) or restore_image_failure(error)) is not None:
        return first_use
    lower = error.lower()
    if AUTH_CODE_REQUIRED in lower:
        return _renewal_needs_code(error, hint=RENEW_HINT)
    if (expired := _expired(error)) is not None:
        return expired
    if (
        "keyring" in lower
        or "failed to get account" in lower
        or "not logged in" in lower
        or ("token" in lower and ("expired" in lower or "invalid" in lower))
    ):
        if (refused := keychain_refusal(error, path)) is not None:
            return refused
        return AppStoreError(
            f"ipatool is not signed in to the App Store (ipatool: {error})",
            hint=SIGN_IN_HINT,
        )
    if "license" in lower or "not owned" in lower:
        return LicenseRequired(
            f"this Apple ID doesn't own {bundle_id} yet (ipatool: {error})",
            hint=(
                "if it's free, Graft can get it for this Apple ID (Get (free), or "
                "`graft --appstore-claim --app <name>`); or tap Get for it on an iPhone or "
                "iPad signed in to the same Apple ID, then try again"
            ),
        )
    return None


def explain(
    error: str, *, bundle_id: str, path: Path, reported_version: str | None,
    doing: str | None = None,
) -> AppStoreError:
    """Turn one of ipatool's error strings into an error with something to do.

    `account_error` first; then ipatool's own error, said as what it was
    `doing` ("download <bundle id>" unless told otherwise).
    """
    error = without_url_queries(error)
    if (found := account_error(error, bundle_id=bundle_id, path=path)) is not None:
        return found
    if out_of_space(AppStoreError(error)):
        # Go's "no space left on device", writing the download: this Mac's
        # disk, not ipatool's own error (the 2.3.1 review's T2-B6).
        return AppStoreError(OUT_OF_SPACE)
    what = doing or f"download {bundle_id}"
    if reported_version in KNOWN_BAD_VERSIONS:
        # Install advice only for a version known to be bad, and the advice
        # that takes effect for this copy: never "install to <path>" when
        # the path is inside Graft.app, whose seal that breaks. "storefront"
        # alone was 2.8.1's bug, but from any other version it is just
        # ipatool's error (the whole-diff review's get-2).
        return AppStoreError(
            f"ipatool couldn't {what}: {error}",
            hint=bad_version_hint(
                reported_version, path=path,
                source=tools.SOURCE_BUNDLED if is_bundled(path) else None,
            ),
        )
    return AppStoreError(
        f"ipatool couldn't {what}: {error}",
        hint=(
            f"that is ipatool's own error — running the same {'command' if doing else 'download'} "
            "in Terminal may say more"
        ),
    )


def _verdict(proc: subprocess.CompletedProcess[str]) -> dict[str, Any] | None:
    """The last line of `proc`'s output that says `success`, or None."""
    return next((o for o in reversed(_json_lines(proc.stdout, proc.stderr)) if "success" in o), None)


def _failure(proc: subprocess.CompletedProcess[str], verdict: dict[str, Any] | None) -> str:
    """What a failed run said: its verdict's `error`, else its last line,
    with no URL's query (`without_url_queries`). Every command's failure
    is read here."""
    error = str((verdict or {}).get("error") or "")
    if not error:
        tail = (proc.stderr.strip() or proc.stdout.strip()).splitlines()[-1:]
        error = tail[0] if tail else f"exited {proc.returncode} without saying why"
    return without_url_queries(error)


#: How long `list-versions` may take: one App Store request.
LIST_VERSIONS_TIMEOUT = 180
#: How long `get-version-metadata` may take for one version: one App Store
#: request, then the version's Info.plist read out of its IPA by range
#: requests (appstore_get_version_metadata.go). Shorter than the list's: a
#: listing reads up to `appstore.VERSIONS_LIMIT_MAX` of these in a row.
VERSION_METADATA_TIMEOUT = 90

#: An external version id as the App Store gives one: digits. Anything else
#: is dropped from a listing, and refused as `--version-id`, so nothing that
#: reaches ipatool's argv after `--external-version-id` can read as a flag.
_EXTERNAL_ID = re.compile(r"[0-9]{1,20}")


def is_external_version_id(value: str) -> bool:
    """Whether `value` is an id `list_versions` could have listed."""
    return _EXTERNAL_ID.fullmatch(value) is not None


def list_versions(path: Path, bundle_id: str, *, run: Runner = subprocess.run) -> list[str]:
    """`list-versions -b BUNDLE`: the external version ids the App Store
    serves the signed-in Apple ID for `bundle_id`, in the order ipatool gives
    them (cmd/list_versions.go logs `externalVersionIdentifiers`, a list of
    strings, beside `bundleID` and `success`). Ids that aren't numbers are
    dropped (`is_external_version_id`); when ipatool listed some and every
    one was dropped, that is an error, not "no versions".

    Apple answers only for an app the Apple ID has: otherwise `LicenseRequired`.
    """
    if not present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=install_hint(path))
    try:
        proc = run_ipatool(
            path, ["list-versions", "-b", bundle_id, "--format", "json", "--non-interactive"],
            run=run, capture_output=True, text=True, timeout=LIST_VERSIONS_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise AppStoreError(
            "ipatool's list of versions didn't answer within 3 minutes",
            hint="check the connection and try again",
        ) from e
    except OSError as e:
        raise AppStoreError(f"couldn't run ipatool at {path}: {e}") from e
    verdict = _verdict(proc)
    if verdict is None or verdict.get("success") is not True:
        raise explain(
            _failure(proc, verdict), bundle_id=bundle_id, path=path,
            reported_version=version(path, run=run), doing=f"list the versions of {bundle_id}",
        )
    ids = verdict.get("externalVersionIdentifiers")
    if not isinstance(ids, list):
        raise AppStoreError("ipatool reported success but listed no versions")
    usable = [str(i).strip() for i in ids if isinstance(i, (str, int)) and is_external_version_id(str(i).strip())]
    if ids and not usable:
        raise AppStoreError(
            f"ipatool listed {len(ids)} version id(s) for {bundle_id}, and none is an App Store "
            "version id (a number) that Graft can get",
            hint="that is ipatool's own answer — running the same command in Terminal shows what it listed",
        )
    return usable


@dataclass(frozen=True)
class VersionMetadata:
    """What `get-version-metadata` says of one version: its version string
    (the IPA's CFBundleShortVersionString) and its release date as ipatool
    prints it (RFC 3339, or None when it printed none)."""

    version: str
    release_date: str | None


class VersionUnavailable(AppStoreError):
    """`get-version-metadata` failed for this one version, and not for a
    reason every other would share (`account_error`): Apple doesn't serve it
    any more, or its IPA couldn't be read. A listing says so on that version's
    row and goes on."""


class VersionMetadataTimeout(AppStoreError):
    """`get-version-metadata` didn't answer within
    `VERSION_METADATA_TIMEOUT`, or gave up itself on a request that ran out
    of time (`timed_out`). Not `VersionUnavailable`: a dropped connection
    looks like this, and every version after it would wait as long and fail
    the same way, so a listing stops at the first (`appstore.list_versions`)."""


#: How ipatool says a request it made ran out of time, in Go's own words,
#: lower-cased. The bundled patch's reader fails a range request that
#: receives nothing for 30 seconds with `received nothing for 30s: context
#: deadline exceeded` (bundle/patches/ipatool-range-readahead.patch), Go's
#: words for a deadline (context.go), which any other deadline ipatool runs
#: into says too, however ipatool wraps it. The others are net/http's and the network's own:
#: `net/http: timeout awaiting response headers` (ipatool's transport sets
#: ResponseHeaderTimeout), `net/http: TLS handshake timeout`, http.Client's
#: `(Client.Timeout exceeded while awaiting headers)` and `(Client.Timeout
#: or context cancellation while reading body)`, and `i/o timeout` (net.go,
#: a dial or a read past its deadline).
_TIMED_OUT = (
    "context deadline exceeded",
    "timeout awaiting response headers",
    "tls handshake timeout",
    "client.timeout",
    "i/o timeout",
)


def timed_out(error: str) -> bool:
    """Whether ipatool's `error` says a request ran out of time: a stalled
    or dropped connection, which the next version would wait on too."""
    lower = error.lower()
    return any(words in lower for words in _TIMED_OUT)


def version_metadata(
    path: Path, bundle_id: str, external_id: str, *, run: Runner = subprocess.run
) -> VersionMetadata:
    """`get-version-metadata -b BUNDLE --external-version-id ID`: that
    version's version string and release date (cmd/get_version_metadata.go
    logs `externalVersionID`, `displayVersion`, `releaseDate` and `success`).

    Raises `account_error`'s errors as they are, `VersionMetadataTimeout`
    when ipatool doesn't answer in time or says a request of its own ran out
    of time (`timed_out`), and anything else about this version as
    `VersionUnavailable`.
    """
    if not present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=install_hint(path))
    try:
        proc = run_ipatool(
            path,
            ["get-version-metadata", "-b", bundle_id, "--external-version-id", external_id,
             "--format", "json", "--non-interactive"],
            run=run, capture_output=True, text=True, timeout=VERSION_METADATA_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise VersionMetadataTimeout(
            f"version {external_id} didn't answer within {VERSION_METADATA_TIMEOUT} seconds",
            hint="check the connection and try again",
        ) from e
    except OSError as e:
        raise AppStoreError(f"couldn't run ipatool at {path}: {e}") from e
    verdict = _verdict(proc)
    if verdict is None or verdict.get("success") is not True:
        error = _failure(proc, verdict)
        if (found := account_error(error, bundle_id=bundle_id, path=path)) is not None:
            raise found
        if timed_out(error):
            raise VersionMetadataTimeout(
                f"reading version {external_id} timed out (ipatool: {error})",
                hint="check the connection and try again",
            )
        raise VersionUnavailable(f"Apple didn't describe this version (ipatool: {error})")
    shown = verdict.get("displayVersion")
    if not isinstance(shown, str) or not shown.strip():
        raise VersionUnavailable("ipatool described this version without its version number")
    released = verdict.get("releaseDate")
    return VersionMetadata(
        version=shown.strip(),
        release_date=released if isinstance(released, str) and released else None,
    )


def download(
    path: Path, bundle_id: str, out_dir: Path, *, external_version_id: str | None = None,
    run: Runner = subprocess.run,
) -> Path:
    """Download `bundle_id`'s encrypted IPA into `out_dir`; return the file.

    The latest version, or the one `external_version_id` names (an id
    `list_versions` listed: ipatool's `--external-version-id`).

    No timeout: a ~1 GB app on a slow line is legitimately slow, and the GUI's
    Stop is the way out of one that is not.
    """
    if not present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=install_hint(path))
    if external_version_id is not None and not is_external_version_id(external_version_id):
        raise AppStoreError(f"{external_version_id!r} isn't an App Store version id")
    chosen = ["--external-version-id", external_version_id] if external_version_id is not None else []
    try:
        proc = run_ipatool(
            path,
            ["download", "-b", bundle_id, *chosen, "-o", str(out_dir), "--format", "json",
             "--non-interactive"],
            run=run, capture_output=True, text=True,
        )
    except OSError as e:
        raise AppStoreError(f"couldn't run ipatool at {path}: {e}") from e

    verdict = _verdict(proc)
    if verdict is None or verdict.get("success") is not True:
        raise explain(_failure(proc, verdict), bundle_id=bundle_id, path=path,
                      reported_version=version(path, run=run))

    named = verdict.get("output")
    if not isinstance(named, str) or not named.strip():
        # Not `Path("")`: that resolves to the current directory, and the
        # containment check below then misreports this as a file written
        # outside `out_dir`.
        raise AppStoreError("ipatool reported success but named no file")
    output = Path(named)
    root = out_dir.resolve()
    try:
        resolved = output.resolve()
        resolved.relative_to(root)
    except (OSError, ValueError):
        # Everything downstream deletes `out_dir` and trusts that this removes
        # the download with it — the file carries the account's license. One
        # that landed elsewhere would outlive the run, so it is not accepted
        # (and not touched: it is not ours to delete).
        raise AppStoreError(
            f"ipatool wrote the IPA outside {out_dir}: {output}",
            hint="delete that file by hand — it holds this Apple ID's license",
        ) from None
    if not resolved.is_file():
        raise AppStoreError(f"ipatool reported success but {output} does not exist")
    return resolved
