"""Get an app from the App Store and decrypt it on this Mac.

One of the three ways a source IPA reaches the cache (the others are
`--provide-ipa` and `--decrypt`), a Mac-only pipeline proven by hand on
2026-11-17 for Example, Bravo and Delta:

1. **ipatool** downloads the App Store's encrypted, account-personalized IPA
   (`ipatool.py`);
2. **UnfairPlay** decrypts it offline with a model recovered once from this
   Mac's App Store enrollment (`unfairplay.py`);
3. the decrypted IPA is staged in the cache exactly as `--provide-ipa` stages
   one (`provide_ipa.py`), and the normal build takes it from there.

This module is the part in between: the private working directory, the rule
for what counts as a good decrypt, and the events the GUI shows progress from.

**Credentials pass through, and are never kept.** Since 2.0 the app signs
in for the user (`appstore_login`). The Apple ID, the password and the 2FA
code come in on stdin, are typed into ipatool's own prompts under a
pseudo-terminal, and are wiped. They never reach argv, the environment, an
event (but the signed-in email, which Settings shows), a log or a file of
Graft's. (ipatool itself keeps its sign-in, password included, in the login
keychain.) Enrolling runs a script as root only behind macOS's administrator
prompt, whose password Graft never sees, and root works only in a folder of
its own (`enrollment`). This module reports whether both are done, and names
the Terminal command for whatever isn't.

**The encrypted download carries the Apple ID's license**, so it lives only in
a 0700 directory under `cache_dir/.appstore-tmp/` and is deleted on success and
on failure alike. The decrypted IPA is made with `--strip-account-data`, and
staging (`provide_ipa.stage`) still removes `iTunesMetadata.plist` and every
`SC_Info/` itself and checks the result, so what reaches the cache identifies
nobody whatever UnfairPlay did.
"""

from __future__ import annotations

import glob
import json
import os
import shlex
import shutil
import signal
import stat
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import appstore_account, host, install_hints, ipatool, itunes, keychain, unfairplay
from .config import AppConfig, AppStoreToolsConfig, Config, swap_preferred_version
from .errors import AppStoreError, IPAInspectionError, ITunesError
from .ipa_cache import CachedIPA, find_in_cache, staged_name
from .ipa_inspect import (
    account_data_in,
    check_cryptid,
    extract_ipa,
    read_info_plist,
    stripped_note,
    verify_decrypted,
)
from .json_output import JsonEmitter
from .repo import version_key
from .tools import SOURCE_BUNDLED

# Under `cache_dir`, and dotted: `find_in_cache` and `--staged-ipas` glob
# `cache_dir/*.ipa` without recursing, so nothing in here is ever offered as a
# staged IPA, half-written or not.
TMP_DIRNAME = ".appstore-tmp"

# `appstore-refused`'s `reason` for a decrypt UnfairPlay could not verify
# against Apple's page hashes (C-UNVERIFIED). The app offers "Use it anyway…"
# when it reads this word, and re-runs the same get or drop with
# `--allow-unverified` behind a confirmation. Pinned against the Swift
# literal (`CLIContract.AppStoreRefusal.unverified`) by
# tests/test_cli_appstore.py.
REFUSED_UNVERIFIED = "unverified"

# `appstore-refused`'s `reason` for a get the Apple ID has no license for. The
# event carries `price`, `formatted_price` and `track_id` from the iTunes
# lookup (null when it failed); the app offers Get (free) for a price of 0 and
# Open in App Store otherwise. Pinned against `CLIContract.AppStoreRefusal`.
REFUSED_NEEDS_LICENSE = "needs-license"
# `--appstore-claim` on an app that isn't free. Same fields.
REFUSED_PAID = "paid"
# `--appstore-claim --expect-email` while ipatool is signed in as another
# Apple ID. The event carries `email`, the one signed in now; the app says
# the Apple ID changed and reads status again. Nothing is purchased.
REFUSED_ACCOUNT_CHANGED = "account-changed"


class AccountChanged(AppStoreError):
    """ipatool is signed in as `email`, not the Apple ID `--expect-email`
    named (the try14 delta audit's D14-APP-1 and F5)."""

    def __init__(self, email: str, expected: str) -> None:
        super().__init__(
            f"ipatool is now signed in to the App Store as {email}, not {expected}, so Graft "
            "didn't add the app to either Apple ID's purchases",
            hint=(
                f"sign in as {expected} first ({ipatool.LOGIN_COMMAND}), or leave out "
                "--expect-email to get it for the Apple ID signed in now"
            ),
        )
        self.email = email


def same_apple_id(one: str, other: str) -> bool:
    """Whether two Apple ID emails name the same account. Apple IDs aren't
    case-sensitive, and `auth info` may print one in another case than the
    status the app read it from."""
    return one.strip().casefold() == other.strip().casefold()

# A working directory older than this belongs to a run that was killed outright
# (SIGKILL, power loss) rather than stopped — the only exits that skip the
# `finally`. A day is far longer than any real download, so a sweep can never
# take a directory out from under a run still in progress.
STALE_AFTER_SECONDS = 24 * 60 * 60

Say = Callable[[str], None]


def _say_nothing(_: str) -> None:
    pass


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def status(tools: AppStoreToolsConfig) -> dict[str, Any]:
    """Whether this Mac can get and decrypt App Store apps, and what's missing.

    `signed_in` and `email` are what this says about the Apple ID: `ipatool
    auth info` prints the account's email and name, and `ipatool.account`
    keeps only the email, which Settings shows. `signed_in` is null when `auth
    info` gave no verdict (hung, crashed): "couldn't tell", which must not
    read as "sign in again".

    `legacy_signin` is a sign-in Graft's ipatool can't read: keychain items
    another ipatool made (Graft 1.x's own, or one that signed in since;
    `appstore_account.legacy_signin`), which aren't asked about at all, since
    asking shows a Keychain dialog (Task 0, K1); or items the Keychain just
    refused it (`ipatool.keychain_refusal`). Either way the answer is to sign
    in again, which replaces them.

    `unknown_reason` says why a `signed_in` of null is one, when it isn't a
    `legacy_signin`: `ipatool.UNKNOWN_NO_VERDICT`, or
    `ipatool.UNKNOWN_KEYCHAIN` when `security` couldn't say whether those
    items are there, or `ipatool.UNKNOWN_LOCKED` when the login keychain is
    locked (`keychain.login_keychain_locked`), read first, or
    `ipatool.UNKNOWN_GRAFT_SIGNIN` when the ipatool is any but Graft.app's
    own and the sign-in is that one's (`appstore_account.graft_signin`,
    signin-1). In the last three ipatool isn't asked either, and with the
    lock, nor is `security`: asking is the dialog. `unknown_message` says it
    as a sentence (`ipatool.UNKNOWN_MESSAGES`, or for the last,
    `ipatool.graft_signin_message`, which names both), else is null.
    """
    ipa_explicit = "ipatool" in tools.explicit
    ipa_path = ipatool.locate(tools.ipatool, explicit=ipa_explicit)
    ipa_source = ipatool.source_of(tools.ipatool, explicit=ipa_explicit)
    ipa_present = ipatool.present(ipa_path)
    ipa_version = ipatool.version(ipa_path) if ipa_present else None
    locked = ipa_present and keychain.login_keychain_locked()
    asks = ipa_present and not locked
    # At most one of these asks `security`: the first only for the bundled
    # ipatool, the second only for any other.
    legacy_state = appstore_account.legacy_signin(ipa_source) if asks else False
    graft_state = appstore_account.graft_signin(ipa_source) if asks else False
    acct = (
        ipatool.Account(None, unknown=ipatool.UNKNOWN_LOCKED) if locked
        else ipatool.Account(None, unknown=ipatool.UNKNOWN_KEYCHAIN)
        if legacy_state is None or graft_state is None
        else ipatool.Account(signed_in=None) if legacy_state
        else ipatool.Account(None, unknown=ipatool.UNKNOWN_GRAFT_SIGNIN) if graft_state
        else ipatool.account(ipa_path) if ipa_present
        else ipatool.Account(signed_in=False)
    )
    legacy = bool(legacy_state) or acct.refused
    signed_in = acct.signed_in
    unknown_reason = acct.unknown if signed_in is None and not legacy else None
    unknown_message = (
        ipatool.graft_signin_message(ipa_path, ipa_source)
        if unknown_reason == ipatool.UNKNOWN_GRAFT_SIGNIN
        else ipatool.UNKNOWN_MESSAGES.get(unknown_reason) if unknown_reason
        else None
    )
    up = unfairplay.resolve_tool(tools)
    up_present = unfairplay.present(up.path)
    up_version = unfairplay.version(up.path) if up_present else None
    model = unfairplay.model_state(tools.unfairplay_models)
    enrolled = model.enrolled
    up_install: str | None = None

    warning = None
    if ipa_version in ipatool.KNOWN_BAD_VERSIONS:
        warning = ipatool.bad_version_hint(
            ipa_version, path=ipa_path, configured=tools.ipatool, source=ipa_source
        )
    setting_note = ipatool_setting_note(tools, ipa_source)

    # The commands for whatever is missing, with what each is for as a shell
    # comment above it, so the whole list stays safe to paste. Printed, never
    # run. The install for each tool is also on its entry as `install`, one
    # string, for a Copy button.
    commands: list[str] = []
    ipa_install: str | None = None
    if warning and ipa_source == SOURCE_BUNDLED:
        # Graft.app's own copy runs before anything an install could put in
        # place, so there is no command to offer: the warning says what helps.
        commands.append(f"# {warning}")
    elif not ipa_present or warning:
        target = ipatool.install_target(ipa_path, tools.ipatool)
        commands.append(
            f"# {warning}" if warning
            else f"# install ipatool {ipatool.GOOD_VERSION}, the release Graft is tested with, to {target}:"
        )
        commands += ipatool.install_steps(target)
        ipa_install = ipatool.install_command(target)
    if legacy and ipa_source == SOURCE_BUNDLED:
        commands.append(
            "# Graft's ipatool can't read the App Store sign-in in your login keychain "
            "(Graft 1.x's own ipatool made it, or a Keychain dialog was denied): sign in "
            "once with Graft's (in Graft.app: Settings › App Store), which replaces it:"
        )
        commands.append(ipatool.LOGIN_COMMAND)
    elif legacy:
        # Any other ipatool, refused an item Graft's record doesn't know
        # (signin-1's text): not "Graft's", and its sign-in clears only what
        # the record vouches for, so macOS may ask while it saves over this.
        commands.append(
            f"# the ipatool at {ipa_path} can't read the App Store sign-in in your login keychain "
            "(another ipatool made it, or a Keychain dialog was denied): sign in again with it; "
            "macOS may ask for your login keychain password while it saves over that sign-in:"
        )
        commands.append(ipatool.LOGIN_COMMAND)
    elif ipa_present and unknown_reason == ipatool.UNKNOWN_GRAFT_SIGNIN:
        commands.append(f"# {unknown_message}")
        commands.append(ipatool.LOGIN_COMMAND)
    elif ipa_present and signed_in is False:
        commands.append(ipatool.LOGIN_COMMAND)
    elif ipa_present and unknown_reason == ipatool.UNKNOWN_LOCKED:
        commands.append(
            "# your login keychain is locked, so Graft couldn't tell whether ipatool is signed "
            f"in: {ipatool.KEYCHAIN_LOCKED_HINT}"
        )
    elif ipa_present and unknown_reason == ipatool.UNKNOWN_KEYCHAIN:
        commands.append(
            "# your login keychain didn't answer when Graft checked it for ipatool's "
            f"sign-in: {ipatool.KEYCHAIN_UNKNOWN_HINT}"
        )
    elif ipa_present and signed_in is None:
        # Not the login command — nothing says the account is signed out — but
        # not nothing either: "couldn't tell" needs a next step.
        commands.append(
            f"# run '{ipa_path} auth info' in Terminal to see why ipatool "
            "couldn't say whether it is signed in"
        )
    # Asked only when something is missing, as in `_require_unfairplay`.
    blocker = host.apple_silicon_blocker() if (not up_present or not enrolled) else None
    if blocker is not None:
        # Building UnfairPlay and setting up decryption both lead nowhere on
        # an Intel Mac, or in the Intel build of Graft, so neither is
        # offered; the reason is.
        commands.append(f"# App Store decryption can't be set up here: {blocker}.")
        if blocker == host.INTEL_MAC:
            commands.append(f"# {_INTEL_REASON}.")
    elif not up_present:
        steps = unfairplay.build_steps(up.path)
        commands.append(
            f"# build UnfairPlay {unfairplay.TESTED_TAG} from source to {up.path}:"
        )
        if steps[0] == "brew install go":
            note = install_hints.homebrew_note()
            if note:
                commands.append(f"# {note} (or install Go from https://go.dev/dl/)")
        commands += steps
        up_install = " && ".join(steps)
    if not enrolled and blocker is None:
        if model.problem:
            commands.append(f"# {model.problem}")
        # Never the bundled UnfairPlay under sudo: root runs it only inside the
        # enrollment script, from its own checked copy (`enrollment`).
        commands.append(
            "# set up decryption once: install an iPhone or iPad app on this Mac from "
            f"the Mac App Store and open it once ({unfairplay.SAME_APPLE_ID}); then, "
            "in Graft.app, Settings › App Store › Set Up Decryption…; in Terminal, run "
            "this with that app's name in the path, run the script it prints with "
            "`sudo /bin/sh`, then `graft --appstore-enroll-finish <the folder it prints>`:"
        )
        commands.append(unfairplay.ENROLL_COMMAND)

    if warning and setting_note:
        # Only beside a warning: the app reads any `warning` as "not ready",
        # and a working bundled copy is ready. Here the setting being passed
        # over is the reason the bad copy runs, so it is part of the warning.
        warning = f"{warning} ({setting_note})"
    # Why the tool config.yaml names isn't the one that runs, on the event
    # the app keeps (delta audit #7): a configured path that is gone, or,
    # for ipatool, the 1.x default spelled out, unless the warning says it.
    ipa_note = missing_path_note(tools, "ipatool", ipa_source) or (None if warning else setting_note)
    return {
        "ipatool": {
            "path": str(ipa_path),
            "source": ipa_source,
            "present": ipa_present,
            "version": ipa_version,
            "signed_in": signed_in,
            "email": acct.email,
            "legacy_signin": legacy,
            "unknown_reason": unknown_reason,
            "unknown_message": unknown_message,
            "warning": warning,
            "install": ipa_install,
            "note": ipa_note,
        },
        "unfairplay": {
            "path": str(up.path),
            "source": up.source,
            "present": up_present,
            "version": up_version,
            "enrolled": enrolled,
            "model_dir": str(tools.unfairplay_models),
            "model_problem": model.problem,
            "install": up_install,
            "note": missing_path_note(tools, "unfairplay", up.source),
        },
        "setup_commands": commands,
    }


def _where(tool: dict[str, Any]) -> str:
    """A found tool's path, marked when it is Graft.app's own copy."""
    return f"{tool['path']} (bundled)" if tool["source"] == SOURCE_BUNDLED else tool["path"]


def ipatool_setting_note(tools: AppStoreToolsConfig, source: str) -> str | None:
    """Why the configured `appstore.ipatool` isn't the one that runs, when it
    is the 1.x default spelled out and Graft.app's own copy won (re-check #12).

    That spelling counts as no choice (`AppStoreToolsConfig.explicit`), by
    design: a 1.x config wrote it out without anyone choosing it. But the
    README and every install command put ipatool there, so a user who put a
    good one there on purpose and named it had their setting skipped without
    a word.
    """
    if "ipatool" not in tools.spelled_default or source != SOURCE_BUNDLED:
        return None
    return (
        "appstore.ipatool is ~/.local/bin/ipatool, the place Graft 1.x used by "
        "default, so it counts as no choice and Graft.app's own ipatool runs "
        "instead; to use another ipatool, put it anywhere else (for "
        f"{ipatool.GOOD_VERSION}, {ipatool.OVERRIDE_PATH}) and set appstore.ipatool "
        "to that path"
    )


#: How `missing_path_note` names each tool.
_TOOL_NAMES = {"ipatool": "ipatool", "unfairplay": "UnfairPlay"}


def missing_path_note(tools: AppStoreToolsConfig, key: str, source: str) -> str | None:
    """Why the `appstore.<key>` config.yaml sets isn't the one that runs, when
    nothing is there any more and Graft.app's own copy ran instead (RC audit
    #20). `tools.resolve` lets a configured path lose to the bundled copy only
    then. A configured ipatool that is there and is the bundled one, by its
    path or a link (`ipatool._resolve`, the delta audit's signin-A), is
    bundled too, and isn't this case.
    """
    if key not in tools.explicit or source != SOURCE_BUNDLED or getattr(tools, key).exists():
        return None
    name = _TOOL_NAMES[key]
    return (
        f"appstore.{key} in config.yaml is {getattr(tools, key)}, which isn't there any "
        f"more, so Graft.app's own {name} runs instead; remove appstore.{key} from "
        "config.yaml to stop this note"
    )


def report_status(cfg: Config, *, emitter: JsonEmitter | None, say: Say) -> None:
    payload = status(cfg.appstore)
    note = ipatool_setting_note(cfg.appstore, payload["ipatool"]["source"])
    gone = {key: missing_path_note(cfg.appstore, key, payload[key]["source"])
            for key in ("ipatool", "unfairplay")}
    if emitter:
        if note and note not in (payload["ipatool"]["warning"] or ""):
            emitter.emit("log", source="warning", line=note)
        for line in gone.values():
            if line:
                emitter.emit("log", source="warning", line=line)
        emitter.emit("appstore-status", **payload)
        return
    ipa, up = payload["ipatool"], payload["unfairplay"]
    if ipa["present"]:
        if ipa["legacy_signin"] and ipa["source"] == SOURCE_BUNDLED:
            account = "its sign-in is one Graft's ipatool can't read (Graft 1.x's?): sign in again"
        elif ipa["legacy_signin"]:
            account = "its sign-in is one this ipatool can't read (another ipatool's?): sign in again"
        elif ipa["signed_in"] and ipa["email"]:
            account = f"signed in as {ipa['email']}"
        else:
            account = {True: "signed in", False: "NOT signed in"}.get(
                ipa["signed_in"], "couldn't tell whether it is signed in"
            )
            if ipa.get("unknown_reason") == ipatool.UNKNOWN_KEYCHAIN:
                account += " (the login keychain didn't answer)"
            elif ipa.get("unknown_reason") == ipatool.UNKNOWN_LOCKED:
                account += " (the login keychain is locked)"
            elif ipa.get("unknown_reason") == ipatool.UNKNOWN_GRAFT_SIGNIN:
                account += " (Graft.app's own ipatool holds the sign-in)"
        say(f"ipatool     {ipa['version'] or '(unknown version)'}  {_where(ipa)}  — {account}")
    else:
        say(f"ipatool     not found at {ipa['path']}")
    if ipa["warning"]:
        say(f"            warning: {ipa['warning']}")
    elif note:
        say(f"            note: {note}")
    if gone["ipatool"]:
        say(f"            note: {gone['ipatool']}")
    if up["present"]:
        state = "enrolled" if up["enrolled"] else "NOT enrolled"
        say(f"UnfairPlay  {up['version'] or '(unknown version)'}  {_where(up)}  — {state}")
        if up["model_problem"]:
            say(f"            {up['model_problem']}")
    else:
        say(f"UnfairPlay  not found at {up['path']}")
    if gone["unfairplay"]:
        say(f"            note: {gone['unfairplay']}")
    say(f"            models: {up['model_dir']}")
    if payload["setup_commands"]:
        say("")
        say("To finish setting up, run in Terminal:")
        for command in payload["setup_commands"]:
            say(f"  {command}")


# ---------------------------------------------------------------------------
# Version check
# ---------------------------------------------------------------------------


def check(
    cfg: Config, app_names: list[str], *, emitter: JsonEmitter | None, say: Say
) -> None:
    """One `appstore-version` per app: the App Store's version against the cache's.

    Every app gets its event. A lookup that fails is reported on that app's
    event (`store_version: null`, `error`), never raised: one app missing from
    its store must not hide the answer for the rest, and "couldn't tell" has
    to stay distinguishable from "not asked".

    `update_available` is true when nothing is cached, too — there is something
    to get. `cached_version` is the file a build would use (`find_in_cache`,
    which honours a pinned `preferred_version`), so the comparison is against
    what the next build would actually ship.

    `held_version` is the version the app is held at (`held_version`), or
    null. A held app is never told of an update: holding it is how the user
    said they don't want the newer one yet. Get latest lets go
    (`release_hold`). `held_version_id` is the build held, when a get by id
    set the hold, and `held_build` which of that version's builds it is
    (`held_build`), when the App Store has more than one.

    `setup_missing` is why no get can run on this Mac yet (`setup_missing`),
    the same on every app's event: null when one can, and on a Mac where no
    App Store request runs, whose own reason is the one to give.
    """
    setup = None if store_requests_blocker() is not None else setup_missing(cfg)
    for name in app_names:
        app = cfg.get_app(name)
        store_version: str | None = None
        error: str | None = None
        try:
            # The one chain every lookup shares (`host.look_up`).
            store_version = host.look_up(
                app.store_country,
                lambda country, app=app: itunes.lookup_by_bundle_id(app.bundle_id, country=country),
            )[0].version
        except ITunesError as e:
            error = e.message
        cached = find_in_cache(cfg, app)
        cached_version = cached.version if cached else None
        held = held_version(app)
        build = held_build(cfg, app)
        update = held is None and store_version is not None and (
            cached_version is None or version_key(store_version) > version_key(cached_version)
        )
        if emitter:
            emitter.emit(
                "appstore-version",
                app=name,
                bundle_id=app.bundle_id,
                store_version=store_version,
                cached_version=cached_version,
                update_available=update,
                held_version=held,
                # The build held, when a get by id set the hold; and which of
                # its version's builds that is, when there are several.
                held_version_id=held_version_id(app),
                held_build=asdict(build) if build is not None else None,
                setup_missing=setup,
                error=error,
            )
            continue
        if error:
            say(f"{name}: couldn't check the App Store — {error}")
        elif held is not None:
            label = f" (build {build.number} of {build.count})" if build is not None else ""
            say(
                f"{name}: held at {held}{label} (cached {cached_version or 'nothing'}), "
                f"{store_version} on the App Store"
            )
        elif update:
            say(f"{name}: {store_version} on the App Store, cached {cached_version or 'nothing'} — update available")
        else:
            say(f"{name}: {store_version} on the App Store, cached {cached_version} — up to date")


# ---------------------------------------------------------------------------
# The private working directory
# ---------------------------------------------------------------------------


def _sweep_stale(root: Path) -> None:
    cutoff = time.time() - STALE_AFTER_SECONDS
    for child in root.iterdir():
        try:
            if child.is_dir() and not child.is_symlink() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


@contextmanager
def private_workdir(cache_dir: Path) -> Iterator[Path]:
    """A 0700 directory under `cache_dir` that is gone when the block exits.

    `mkdtemp` creates it 0700; the parent is tightened too, since its listing
    alone says which apps this Mac has been getting.
    """
    root = cache_dir / TMP_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    _sweep_stale(root)
    work = Path(tempfile.mkdtemp(prefix="get-", dir=root))
    try:
        yield work
    finally:
        # SIGTERM blocked for the removal itself. Inside a get the Stop
        # handler is still installed here, and a second Stop arriving
        # mid-`rmtree` would raise out of it and leave the license-bearing
        # download half-deleted; blocked, it is delivered — and raised — the
        # moment the directory is gone.
        blocked = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
        try:
            shutil.rmtree(work, ignore_errors=True)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, blocked)


class _Stopped(AppStoreError):
    pass


#: What a get raises when it is stopped (`_stop_cleans_up`), for a caller that
#: runs one inside a larger job (`update_run`) and must tell a Stop from a
#: failure.
GetStopped = _Stopped


@contextmanager
def _stop_cleans_up() -> Iterator[None]:
    """Make SIGTERM unwind through the `finally`s instead of killing in place.

    The GUI's Stop is a SIGTERM (`ProcessHandle.stop`), escalated to SIGKILL
    two seconds later. Python's default disposition dies where it stands, which
    here would leave a license-bearing IPA in the cache directory. Raising
    instead lets `private_workdir` delete it; `subprocess.run` kills its child
    on the way out, and the GUI already SIGTERMs the descendants itself.

    Entered *outside* `private_workdir` so it is still installed while that
    cleans up, and `private_workdir` blocks SIGTERM around the removal itself:
    a second Stop then raises the moment the directory is gone, not halfway
    through deleting it.

    Scoped to the get, and restored after: the rest of the CLI deliberately
    installs no handler. Only the main thread may install one, and tests that
    run elsewhere just get the default.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stop(signum: int, frame: Any) -> None:
        # No claim about the cache: a Stop can land after `os.replace` has
        # already put the new IPA in place.
        raise _Stopped("stopped before finishing — the download and its working files were removed")

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


# ---------------------------------------------------------------------------
# Getting and decrypting
# ---------------------------------------------------------------------------


def _stage(emitter: JsonEmitter | None, say: Say, app_name: str, stage: str) -> None:
    if emitter:
        emitter.emit("appstore-stage", app=app_name, stage=stage)
        return
    say({"download": "downloading…", "decrypt": "decrypting…", "stage": "staging…"}[stage])


#: Why App Store decryption cannot be set up on an Intel Mac, said the same way
#: by the refusal and by `--appstore-status`.
_INTEL_REASON = (
    "UnfairPlay enrolls with an iPhone or iPad app installed on this Mac from "
    "the App Store, and only Apple silicon Macs can install those"
)


def _require_unfairplay(tools: AppStoreToolsConfig) -> None:
    up = unfairplay.resolve_tool(tools)
    ready = unfairplay.present(up.path) and unfairplay.enrolled(tools.unfairplay_models)
    # Checked only when something is missing, so a working setup never pays
    # for it — and an Intel Mac, or the Intel build of Graft, is told the real
    # reason rather than handed an UnfairPlay build or an enrollment command
    # that cannot succeed there.
    blocker = host.apple_silicon_blocker() if not ready else None
    if blocker == host.INTEL_MAC:
        raise AppStoreError(
            f"decrypting App Store IPAs needs an Apple silicon Mac, and {host.INTEL_MAC}",
            hint=(
                f"{_INTEL_REASON}. Use an IPA that is already decrypted, or dump "
                "one from a jailbroken device (--decrypt)"
            ),
        )
    if blocker is not None:
        raise AppStoreError(
            f"decrypting App Store IPAs can't be done here: {blocker}",
            hint="Use an IPA that is already decrypted, or dump one from a jailbroken device (--decrypt)",
        )
    if not unfairplay.present(up.path):
        raise AppStoreError(
            f"UnfairPlay not found at {up.path}",
            hint=unfairplay.install_hint(up.path),
        )
    model = unfairplay.model_state(tools.unfairplay_models)
    if model.problem:
        raise AppStoreError(f"this Mac's decryption model can't be used: {model.problem}")
    if not model.enrolled:
        raise AppStoreError(
            f"this Mac isn't enrolled for decryption yet (no model in {tools.unfairplay_models})",
            hint=(
                "set it up once with any iPhone or iPad app installed on this Mac from the "
                f"Mac App Store ({unfairplay.SAME_APPLE_ID}): {unfairplay.SET_UP_DECRYPTION}"
            ),
        )


def _decrypt(
    tools: AppStoreToolsConfig,
    source: Path,
    work: Path,
    *,
    app_name: str,
    emitter: JsonEmitter | None,
    say: Say,
    allow_unverified: bool,
    warn: Say,
    retry: str,
) -> Path:
    """Decrypt `source` into `work` and apply the verify rule. Returns the IPA.

    `PlaintextVerified: true` is the proof — every page matched Apple's own
    pre-encryption hashes — and is required. Older apps without those tables
    legitimately say `false`, so the caller can be told to accept one
    (`--allow-unverified`) with a warning.

    The refusal is also an event, `appstore-refused` with `reason:
    "unverified"`, emitted just before the raise (C-UNVERIFIED). The GUI
    offers "Use it anyway…" off that event, behind a confirmation, and re-runs
    the same get or drop with `--allow-unverified` — it never reads the
    message or the hint, whose Terminal command is for a Terminal user. It
    used to say the GUI never offers this; the owner reversed that on
    2026-11-18.
    """
    _stage(emitter, say, app_name, "decrypt")
    up = unfairplay.resolve_tool(tools)
    report = unfairplay.decrypt(
        up.path,
        input_ipa=source,
        output_ipa=work / "decrypted.ipa",
        report=work / "unfairplay-report.json",
        model_dir=tools.unfairplay_models,
    )
    if not report.plaintext_verified:
        if not allow_unverified:
            if emitter:
                emitter.emit("appstore-refused", app=app_name, reason=REFUSED_UNVERIFIED)
            raise AppStoreError(
                f"UnfairPlay decrypted {source.name} but couldn't verify it against "
                "Apple's own page hashes (PlaintextVerified: false) — refusing to stage it",
                hint=(
                    "older apps without those hashes legitimately report this. "
                    f"To accept it anyway, run in Terminal: {retry} --allow-unverified"
                ),
            )
        warn(
            f"{app_name}: staging a decrypt UnfairPlay could not verify against "
            "Apple's page hashes (--allow-unverified)"
        )
    _refuse_still_encrypted(report.output_path, work, source=source)
    if emitter:
        emitter.emit(
            "appstore-decrypted",
            app=app_name,
            encrypted_images=report.encrypted_images,
            decrypted_pages=report.decrypted_pages,
            plaintext_verified=report.plaintext_verified,
        )
    return report.output_path


#: What a refusal of UnfairPlay's output says to do.
_STILL_ENCRYPTED_HINT = (
    "try again; if it fails the same way, UnfairPlay can't decrypt this app on this Mac yet: "
    "use an IPA that is already decrypted, or dump one from a jailbroken device (--decrypt)"
)

#: What a refusal says when Graft's own unpacking of UnfairPlay's output, for
#: the check, failed on this Mac: a full disk most likely, never UnfairPlay.
_UNPACK_FAILED_HINT = (
    "the check unpacks the whole app into Graft's cache folder, so it needs about the app's "
    "size again in free disk space: fix what the error names (free some space if the disk is "
    "full), then try again"
)

#: What a refusal says when a member of UnfairPlay's output won't inflate:
#: UnfairPlay copies every member it doesn't decrypt as it is, so the IPA it
#: was given is most likely damaged (the try15 delta audit, d15-py-5).
_DAMAGED_HINT = (
    "UnfairPlay copies what it doesn't decrypt as it is, so the IPA it was given is most likely "
    "damaged: try again with a fresh copy of it"
)


def _damaged(source: Path, why: str) -> str:
    return (
        f"UnfairPlay decrypted {source.name}, but Graft couldn't unpack the decrypted IPA to check it "
        f"for encryption: it's damaged, or packed in a way Graft can't read ({why}) — refusing to stage it"
    )


def _refuse_still_encrypted(ipa: Path, work: Path, *, source: Path) -> None:
    """Every binary in UnfairPlay's output read for its cryptid, as the
    build's stage 2.5 reads them (`verify_decrypted`: each Mach-O, and every
    bundle's declared executable whatever it is), and anything still
    encrypted, or that can't be read, refused before it is staged.

    UnfairPlay decrypts extensions, frameworks and Watch apps in the same
    pass as the app (its README), so a cryptid left anywhere is its output
    being wrong, and staging trusted its report alone: a report saying
    PlaintextVerified with 0 pages passed, and stage 2.5 then only warned
    (the whole-diff review's get-5). The output is extracted inside `work`,
    and that copy is gone before this returns, whatever ends it, a Stop
    included.

    An `OSError` is this Mac failing Graft's own unpacking or reading of the
    copy, ENOSPC most likely (the copy is the app's whole unpacked size), so
    it is said as that, never as UnfairPlay's (the delta audit's get5-hint).
    """
    check = work / "cryptid-check"
    unpacked = False
    try:
        extract_ipa(ipa, check)
        unpacked = True
        results = verify_decrypted(check)
    except IPAInspectionError as e:
        if not unpacked:
            # `extract_ipa`'s own: a zip it couldn't read (BadZipFile, a bad
            # CRC or header), the same damage as a bad deflate stream below.
            raise AppStoreError(_damaged(source, e.message), hint=_DAMAGED_HINT) from e
        raise AppStoreError(
            f"UnfairPlay decrypted {source.name}, but Graft couldn't check its output for encryption "
            f"({e.message}) — refusing to stage it",
            hint=_STILL_ENCRYPTED_HINT,
        ) from e
    except OSError as e:
        raise AppStoreError(
            f"UnfairPlay decrypted {source.name}, but Graft couldn't unpack the decrypted IPA to check it "
            f"for encryption: {e.strerror or e} — refusing to stage it",
            hint=_UNPACK_FAILED_HINT,
        ) from e
    except (zlib.error, EOFError, NotImplementedError) as e:
        # What zipfile raises past BadZipFile (which `extract_ipa` wraps):
        # a damaged deflate stream, a truncated member, a compression
        # method it can't read. Said, with a hint, rather than reaching
        # cli's "isn't a failure graft knows how to explain".
        raise AppStoreError(_damaged(source, str(e)), hint=_DAMAGED_HINT) from e
    finally:
        shutil.rmtree(check, ignore_errors=True)
    still = [r.binary.relative_to(check).as_posix() for r in results if not r.is_decrypted]
    if still:
        raise AppStoreError(
            f"UnfairPlay reported {source.name} decrypted, but {len(still)} of its binaries "
            f"{'is' if len(still) == 1 else 'are'} still encrypted ({', '.join(still)}) — refusing "
            "to stage it",
            hint=_STILL_ENCRYPTED_HINT,
        )


#: Why no App Store request is made where decryption can't run, before the
#: reason (`host.apple_silicon_blocker`).
STORE_NEEDS = "App Store sign-in and downloads need a Mac with Apple silicon and Graft.zip"
STORE_NEEDS_HINT = (
    "Searching the App Store and adding an app by name still work. Use an IPA that is already "
    "decrypted, or dump one from a jailbroken device (--decrypt)"
)


#: Why a get can't run on this Mac yet (`setup_missing`), as Settings › App
#: Store's rows would say: no UnfairPlay or no enrollment, or Graft's own
#: ipatool with no sign-in on its record. `update_plan`'s skipped reasons too.
DECRYPTION_NOT_SET_UP = "decryption isn't set up on this Mac"
NOT_SIGNED_IN = "not signed in to the App Store"


def setup_missing(cfg: Config) -> str | None:
    """Why a get can't run on this Mac yet, or None when it may: what
    `fetched` would refuse first, as far as files can tell.

    No keychain, no ipatool: UnfairPlay and its enrollment are files, and
    for Graft's own ipatool, whether it signed in is on Graft's record of its
    sign-in (`appstore_account.vouched`), without which a get refuses (signed
    out, or another ipatool's sign-in it can't read). Any other ipatool may
    be signed in in Terminal, which no file says: the get finds out. Nor is a
    known-bad ipatool version here: telling needs `ipatool --version`.

    `update_plan.store_step` skips the get with it, and `check` reports it
    (`appstore-version`'s `setup_missing`), which the app pane reads as it
    appears.
    """
    store_tools = cfg.appstore
    up = unfairplay.resolve_tool(store_tools)
    if not unfairplay.present(up.path) or not unfairplay.enrolled(store_tools.unfairplay_models):
        return DECRYPTION_NOT_SET_UP
    source = ipatool.source_of(store_tools.ipatool, explicit="ipatool" in store_tools.explicit)
    if source == SOURCE_BUNDLED and "account" not in appstore_account.vouched():
        return NOT_SIGNED_IN
    return None


def store_requests_blocker() -> AppStoreError | None:
    """The refusal of any ipatool App Store request (sign-in, a get, a free
    claim, an app's older versions) on an Intel Mac or in the Intel build
    of Graft.app (`host.apple_silicon_blocker`), or None where they run.

    Each of those runs ipatool's Unicorn emulator, whose code buffer on
    x86_64 is mapped writable and executable without MAP_JIT, which the
    bundled ipatool isn't entitled to (allow-jit only), so the request would
    likely crash it. And nothing one is for can be decrypted there. Searching
    and adding an app by name use the public iTunes API (`itunes`), and a
    status read (`auth info`) makes no Store request: neither is refused."""
    blocker = host.apple_silicon_blocker()
    if blocker is None:
        return None
    return AppStoreError(f"{STORE_NEEDS}: {blocker}", hint=STORE_NEEDS_HINT)


def _ipatool_path(tools: AppStoreToolsConfig) -> Path:
    """The ipatool to run for an App Store request (`ipatool.locate`):
    refused where no Store request may run (`store_requests_blocker`), and
    when it isn't there. A get, a claim and the older versions all start
    here, before ipatool is run at all."""
    if (refusal := store_requests_blocker()) is not None:
        raise refusal
    path = ipatool.locate(tools.ipatool, explicit="ipatool" in tools.explicit)
    if not ipatool.present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=ipatool.install_hint(path))
    return path


def _require_signed_in(
    tools: AppStoreToolsConfig, path: Path, *, expect_email: str | None = None
) -> str:
    """Not a known-bad ipatool, and signed in. Shared by `fetched` and
    `claim`, so neither starts anything with an ipatool that can't finish it.
    Returns the ipatool's source.

    With `expect_email` (a claim's `--expect-email`), signed in as that
    Apple ID, else `AccountChanged`, before anything is deleted or bought.

    A 1.x sign-in is refused without asking ipatool, which would show a
    Keychain dialog (`appstore_account`), and a Keychain refusal is said as
    one, never as ipatool's "not found" (decision 10). The two other items
    a download reads are seen to as well, with the bundled ipatool: another
    program's `anisette-v4` stops it here (`appstore_account.foreign_device`),
    and another's `kbsync-v1`, a cache, is deleted
    (`appstore_account.clear_foreign_cache`)."""
    source = ipatool.source_of(tools.ipatool, explicit="ipatool" in tools.explicit)
    found_version = ipatool.version(path)
    if found_version in ipatool.KNOWN_BAD_VERSIONS:
        raise AppStoreError(
            f"ipatool {found_version} at {path} is known to fail downloads",
            hint=ipatool.bad_version_hint(
                found_version, path=path, configured=tools.ipatool, source=source,
            ),
        )
    if keychain.login_keychain_locked():
        # Before any lookup or `auth info`: in a locked keychain each can ask
        # for its password.
        raise AppStoreError(ipatool.KEYCHAIN_LOCKED, hint=ipatool.KEYCHAIN_LOCKED_HINT)
    legacy = appstore_account.legacy_signin(source)
    graft = appstore_account.graft_signin(source)
    if legacy is None or graft is None:
        # Not asked of ipatool either: if these are another ipatool's items,
        # asking is the dialog.
        raise AppStoreError(
            "couldn't tell whether ipatool is signed in: your login keychain didn't answer "
            "when Graft checked it for ipatool's sign-in",
            hint=ipatool.KEYCHAIN_UNKNOWN_HINT,
        )
    if legacy:
        raise AppStoreError(
            "the App Store sign-in in your login keychain was made by another ipatool "
            "(Graft 1.x's, or one you use in Terminal), and Graft's ipatool can't read it",
            hint=f"{ipatool.LOGIN_HINT}; signing in replaces it",
        )
    if graft:
        # Any other ipatool, and the sign-in is Graft.app's own (signin-1).
        raise ipatool.graft_signin_error(path, source)
    found = ipatool.account(path)
    if found.refused:
        raise ipatool.KeychainRefusal(refused=True, path=path, source=source)
    if found.unknown == ipatool.UNKNOWN_KEYCHAIN:
        raise AppStoreError(ipatool.KEYCHAIN_UNKNOWN, hint=ipatool.KEYCHAIN_UNKNOWN_HINT)
    account = found.signed_in
    if account is None:
        raise AppStoreError(
            "couldn't tell whether ipatool is signed in — `auth info` gave no answer",
            hint=(
                f"run `{shlex.quote(str(path))} auth info` in Terminal to "
                "see why, then try again"
            ),
        )
    if not account:
        raise AppStoreError("ipatool is not signed in to the App Store", hint=ipatool.SIGN_IN_HINT)
    if expect_email is not None:
        # Graft.app's Get (free) confirmation named an Apple ID, from a status
        # read made as the button was pressed. The purchase goes to whoever
        # ipatool is signed in as now, and a sign-in changed in Terminal while
        # the confirmation was open would otherwise get it.
        if found.email is None:
            raise AppStoreError(
                f"couldn't tell which Apple ID ipatool is signed in as (`auth info` gave no email), "
                f"so Graft didn't add the app to {expect_email}'s purchases",
                hint=f"run `{shlex.quote(str(path))} auth info` in Terminal to see why, then try again",
            )
        if not same_apple_id(found.email, expect_email):
            raise AccountChanged(found.email, expect_email)
    device = appstore_account.foreign_device(source)
    if device is None:
        raise AppStoreError(
            "couldn't tell whose App Store device item (anisette-v4) is in your login keychain: it "
            "didn't answer when Graft checked",
            hint=ipatool.KEYCHAIN_UNKNOWN_HINT,
        )
    if device:
        maker, user = (
            ("another ipatool", "Graft's ipatool") if source == SOURCE_BUNDLED
            else ("Graft.app's own ipatool", f"the ipatool at {path}")
        )
        raise AppStoreError(
            f"the App Store device item in your login keychain (anisette-v4) was made by {maker}, "
            f"and {user} can't use it without a Keychain dialog",
            # The sign-in is Graft's (`account` just read signed in), so
            # Settings shows Sign In Again…, not Sign In… (delta audit #8).
            hint=f"{ipatool.RENEW_HINT}; signing in replaces it",
        )
    try:
        appstore_account.clear_foreign_cache(source)
    except appstore_account.KeychainUnanswered:
        raise AppStoreError(
            "couldn't tell whose App Store download cache (kbsync-v1) is in your login keychain: it "
            "didn't answer when Graft checked",
            hint=ipatool.KEYCHAIN_UNKNOWN_HINT,
        ) from None
    except appstore_account.ClearFailed:
        raise AppStoreError(
            "Graft couldn't remove another ipatool's App Store download cache (kbsync-v1) from your "
            "login keychain, so it didn't start: Graft's ipatool would need a Keychain dialog to read it",
            hint=appstore_account.CLEAR_CACHE_HINT,
        ) from None
    return source


def _adopt_cache(source: str, before: keychain.Lookup | None) -> None:
    """`appstore_account.adopt_cache` after a successful run; a failure to
    record costs only a cache refill (it is deleted before the next Get)."""
    try:
        appstore_account.adopt_cache(source, before)
    except (appstore_account.KeychainUnanswered, OSError):
        pass


def apples_own(bundle_id: str) -> bool:
    """Whether `bundle_id` is one of Apple's own apps (`com.apple.*`, any case)."""
    return bundle_id.casefold().startswith("com.apple.")


def _refuse_apples_own(app: AppConfig) -> None:
    """Refuse a Get or a claim of one of Apple's own apps, before ipatool runs.

    They come first in a generic search ("notes", "calculator"), so they are
    the row a stranger picks. iOS ships them itself, and nobody but Apple can
    register or sign a `com.apple` bundle id, so a download would only end in
    a build that fails later with an error that never says why.
    """
    if apples_own(app.bundle_id):
        raise AppStoreError(
            f"{app.bundle_id} is one of Apple's own apps, which Graft doesn't get from the App Store",
            hint="iOS ships Apple's apps itself, and only Apple can sign a com.apple bundle id: "
            "get it on the iPhone or iPad from the App Store, and pick another app for Graft",
        )


def _found_elsewhere(app: AppConfig, error: AppStoreError) -> AppStoreError:
    """`error`, for a download ipatool couldn't make, with a hint naming the
    store the app was found in when it has one (`store_country`).

    ipatool downloads from the signed-in Apple ID's own storefront, and Graft
    can't see which that is, so an app found in another store may not be sold
    in it. Only ipatool's own download failures (`ipatool couldn't download
    ...`) get it: a sign-in or install problem is not about the store.
    """
    if not app.store_country or not error.message.startswith(f"ipatool couldn't download {app.bundle_id}"):
        return error
    found = (
        f"this app was found in the {app.store_country} App Store; "
        "your Apple ID may be in a different one"
    )
    return AppStoreError(error.message, hint=f"{error.hint}. Also, {found}" if error.hint else found)


def _listing(app: AppConfig) -> itunes.ITunesApp | None:
    """`app`'s listing in its own store, or None when the lookup failed. None
    is "couldn't tell", never "free".

    The store is the chain every lookup shares (`host.look_up`): the one the
    app was found in, else this Mac's region's, then the US. Never
    the US by default when something better is known: an app can be free in
    one store and paid in another, and Graft claims only what it can see is
    free where the Apple ID shops.
    """
    try:
        return host.look_up(
            app.store_country, lambda country: itunes.lookup_by_bundle_id(app.bundle_id, country=country)
        )[0]
    except ITunesError:
        return None


def _refuse_for_licence(
    emitter: JsonEmitter | None, app_name: str, reason: str, listing: itunes.ITunesApp | None
) -> None:
    if emitter:
        emitter.emit(
            "appstore-refused", app=app_name, reason=reason,
            price=listing.price if listing else None,
            formatted_price=listing.formatted_price if listing else None,
            track_id=listing.track_id if listing else None,
        )


def _needs_licence(
    app_name: str, app: AppConfig, emitter: JsonEmitter | None, cause: AppStoreError,
    version_id: str | None = None,
) -> AppStoreError:
    """A download refused for want of a license, as the `needs-license`
    refusal (with the price, so the app can offer Get (free) or Open in App
    Store) and an error whose hint says which of the two applies. For a get
    of one version (`version_id`), the hint names that version's command: a
    plain get, followed literally, would get the latest and let go of the
    hold."""
    listing = _listing(app)
    _refuse_for_licence(emitter, app_name, REFUSED_NEEDS_LICENSE, listing)
    quoted = shlex.quote(app_name)
    again = (
        f"get that version again with `graft --appstore-get --app {quoted} --version-id {version_id}`"
        if version_id else "get it again"
    )
    if listing is not None and listing.price == 0:
        hint = (
            "it's free: get it for this Apple ID with Get (free) in Graft, or "
            f"`graft --appstore-claim --app {quoted}`, then {again}"
        )
    elif listing is not None and listing.price:
        hint = (
            f"it costs {listing.formatted_price or listing.price}: buy it in the App Store "
            f"with the same Apple ID, then {again}"
        )
    elif version_id:
        hint = f"{cause.hint}; then {again}" if cause.hint else f"then {again}"
    else:
        hint = cause.hint
    return AppStoreError(cause.message, hint=hint)


def _old_version_gone(app_name: str, app: AppConfig, error: AppStoreError) -> AppStoreError:
    """`error`, for a download of a chosen version ipatool couldn't make,
    with a hint that says Apple keeps only some old versions. Only ipatool's
    own download failures get it, as with `_found_elsewhere`."""
    if not error.message.startswith(f"ipatool couldn't download {app.bundle_id}"):
        return error
    return AppStoreError(
        error.message,
        hint=(
            "Apple doesn't serve every old version, and this one may be gone: pick another "
            f"(graft --appstore-versions --app {shlex.quote(app_name)}), or get the latest"
        ),
    )


@contextmanager
def fetched(
    cfg: Config,
    app_name: str,
    *,
    emitter: JsonEmitter | None,
    allow_unverified: bool = False,
    again: bool = False,
    version_id: str | None = None,
    say: Say = _say_nothing,
    warn: Say = _say_nothing,
) -> Iterator[Path]:
    """Download `app_name`'s latest version, or the one `version_id` names
    (an id `list_versions` listed), decrypt it, and yield the decrypted IPA.

    Whether to download at all is the caller's question (`already_staged`,
    `already_staged_version`). `again` says it asked with `--again`, so the
    Terminal command an unverified refusal names keeps it: without it, that
    command would find the version staged and download nothing.

    The caller stages what it is handed inside the `with`; on exit — success,
    failure or Stop — the working directory and everything in it is gone.

    Everything that can be checked locally is checked *before* the download,
    which can be a gigabyte: a missing or unenrolled UnfairPlay, and a signed
    out ipatool, are refusals with the Terminal command in the hint. A download
    error is still classified afterward, since an expired session passes
    `auth info`.
    """
    app = cfg.get_app(app_name)
    _refuse_apples_own(app)
    tools = cfg.appstore
    ipa_path = _ipatool_path(tools)
    _require_unfairplay(tools)
    source = _require_signed_in(tools, ipa_path)

    with _stop_cleans_up(), private_workdir(cfg.cache_dir) as work:
        _stage(emitter, say, app_name, "download")
        before = appstore_account.cache_before(source)
        try:
            encrypted = ipatool.download(ipa_path, app.bundle_id, work, external_version_id=version_id)
        except ipatool.LicenseRequired as e:
            raise _needs_licence(app_name, app, emitter, e, version_id=version_id) from e
        except AppStoreError as e:
            raise _found_elsewhere(app, _old_version_gone(app_name, app, e) if version_id else e) from e
        _adopt_cache(source, before)
        decrypted = _decrypt(
            tools, encrypted, work,
            app_name=app_name, emitter=emitter, say=say,
            allow_unverified=allow_unverified, warn=warn,
            retry=f"graft --appstore-get --app {shlex.quote(app_name)}"
            + (f" --version-id {version_id}" if version_id else "")
            + (" --again" if again else ""),
        )
        _stage(emitter, say, app_name, "stage")
        yield decrypted


def _unsafe_in_filename(value: str) -> bool:
    return any(bad in value for bad in ("/", "\\", "\0", ".."))


def already_staged(cfg: Config, app: AppConfig) -> CachedIPA | None:
    """The staged IPA a get of `app` would only download again, or None.

    A get fetches the App Store's current version, whatever
    `preferred_version` pins, and stages it as `<bundle id>_<version>.ipa`
    (`provide_ipa.provide`). When that file is already there as a get leaves
    one, downloading it again spends the app's whole size and a decrypt to put
    the same file back: its own Info.plist names this app and that version,
    its main binary is decrypted (`main_binary_encrypted`), it holds no
    account data (`ipa_inspect.account_data_in`), and it isn't a partial dump
    (`ipa_inspect.stripped_note`), which a get would replace with the whole
    app. A copy under any other name isn't one a get staged, and the build
    prefers the canonical file once there is one (`find_in_cache`).

    The current version is the listing's (`_listing`), from the same stores
    `--appstore-check` asks. They are asked only when this app has some IPA
    staged under a canonical name: with none, there is nothing to compare.

    **Fails open.** None, and the get downloads as it always has, when the
    lookup fails or names no version, and when the file can't be read to
    say. Apple's own apps get None as well, so `fetched` refuses them as
    before.
    """
    if apples_own(app.bundle_id):
        return None
    if not any(cfg.cache_dir.glob(f"{glob.escape(app.bundle_id)}_*.ipa")):
        return None
    listing = _listing(app)
    return _staged_as_get(cfg, app, listing.version if listing is not None else None)


def already_staged_version(cfg: Config, app: AppConfig, external_id: str) -> CachedIPA | None:
    """The staged IPA a get of version `external_id` would only download
    again, or None.

    That build's own file (`ipa_cache.staged_name` with the id), as a get of
    it leaves one. Or the plain `<bundle id>_<version>.ipa`, but only while no
    other version read for this app has the same number (`same_version_ids`):
    with two builds of 2026.42.0, a plain file can be either, and a get of
    build 1 must not hold the app at what may be build 2. The plain file comes
    back with no id, so the hold it leads to is by display version, as before.

    **Fails open**, as `already_staged` does: an id no listing has read, or
    whose file can't be read to say, downloads.

    **Known limit:** "no other version read" is only what has been read. A
    twin just past the page listed (the 11th row of the sheet's ten, or the
    next row after `--limit 1`) isn't in `known`, so the plain file is taken
    for this build and the hold is by display version, which is what every
    hold was before 2.3, on a file that may be the other build. Reading a row
    past each page's edge would catch the usual case (twins are listed side by
    side) for a request per page, and still miss a twin further off; the
    README's Get an older version notes say to Show More first instead.
    """
    if apples_own(app.bundle_id):
        return None
    known = _VersionCache.load(cfg.cache_dir).known(app.bundle_id)
    meta = known.get(external_id)
    if meta is None:
        return None
    own = _staged_as_get(cfg, app, meta.version, external_id=external_id)
    if own is not None:
        return own
    if len(same_version_ids(known, meta.version)) > 1:
        return None
    return _staged_as_get(cfg, app, meta.version)


def same_version_ids(known: dict[str, ipatool.VersionMetadata], version: str) -> list[str]:
    """Every version id read for an app (`_VersionCache.known`) whose version
    is `version`, trimmed as the Older Versions sheet compares them, oldest
    (lowest) first. Two or more are builds that share a number, counted as
    `AppStoreVersionsStore.duplicateBuilds` counts them."""
    shown = version.strip()
    return sorted(
        (i for i, m in known.items() if ipatool.is_external_version_id(i) and m.version.strip() == shown),
        key=int,
    )


def _staged_as_get(
    cfg: Config, app: AppConfig, version: str | None, *, external_id: str | None = None,
) -> CachedIPA | None:
    """The file a get of `version` stages (`ipa_cache.staged_name`, with the
    build's id for a get by id), when it is there as a get leaves one (see
    `already_staged`), else None."""
    if not isinstance(version, str) or not version or _unsafe_in_filename(version):
        return None
    path = cfg.cache_dir / staged_name(app.bundle_id, version, external_id)
    if not path.is_file() or stripped_note(path) is not None:
        return None
    try:
        info = read_info_plist(path)
        if (info.bundle_id, info.version) != (app.bundle_id, version):
            return None
        if account_data_in(path) or main_binary_encrypted(path):
            return None
    except Exception:
        # Whatever the reason a file can't be read (not a zip, a malformed
        # plist, a damaged member, a read error), "can't say" downloads, as
        # `IPAInfoMemo.info_for` reads it as "skip this candidate".
        return None
    return CachedIPA(
        path=path, bundle_id=info.bundle_id, version=info.version, external_version_id=external_id,
    )


def staged_as_get(cfg: Config, app: AppConfig, version: str | None) -> CachedIPA | None:
    """`version` of `app`, staged as a get stages it, or None: for a caller
    that already knows the App Store's version and must not look it up again
    (`update_run`, after the plan's batched lookup). Apple's own apps: None."""
    if apples_own(app.bundle_id):
        return None
    return _staged_as_get(cfg, app, version)


def report_already_staged(
    app_name: str, staged: CachedIPA, *, emitter: JsonEmitter | None, say: Say,
    version_id: str | None = None,
) -> None:
    """Say that a get stopped before downloading: `staged` is the App Store's
    current version already (`already_staged`), or the version `version_id`
    names (`already_staged_version`)."""
    if emitter:
        emitter.emit(
            "appstore-already-staged",
            app=app_name,
            bundle_id=staged.bundle_id,
            version=staged.version,
            filename=staged.path.name,
            path=str(staged.path),
        )
        return
    chosen = f" --version-id {version_id}" if version_id else ""
    which = "" if version_id else ", the App Store's current version"
    say(f"{app_name}: already have {staged.version}{which} ({staged.path.name}) — nothing downloaded")
    say(f"  to download it again anyway: graft --appstore-get --app {shlex.quote(app_name)}{chosen} --again")


# ---------------------------------------------------------------------------
# Older versions, and holding an app at one
# ---------------------------------------------------------------------------

#: What `preferred_version` says when nothing is pinned.
LATEST = "latest"

#: How many versions `--appstore-versions` reads the details of when not
#: told (`--limit`), and the most one run will. Each is one ipatool run and
#: one App Store request that reads that version's Info.plist out of its
#: IPA: seconds apiece.
VERSIONS_LIMIT = 10
VERSIONS_LIMIT_MAX = 50

#: What each version id was read to be, per bundle id, in `cache_dir`. An id
#: names one build for good, so what it says never changes and nothing here
#: expires; the list of ids is asked for afresh each time. Dotted and not an
#: .ipa, like `ipa_memo`'s file beside it: nothing that lists the cache offers
#: it, and Remove Graft's Data's Staged IPAs takes it with the folder.
VERSIONS_CACHE = ".appstore-versions.json"
_VERSIONS_CACHE_FORMAT = 1
#: The most of `VERSIONS_CACHE` that is read. A row is under 100 bytes, so
#: this is tens of thousands of versions; anything bigger isn't one Graft
#: wrote, and is no cache.
_VERSIONS_CACHE_MAX = 4 << 20


class _VersionCache:
    """`VERSIONS_CACHE`, read once and written after each version read, so a
    listing stopped halfway keeps what it had read. Anything that won't
    read as this shape is an empty cache, and a write that fails costs only
    a read again next time.

    It also says, per bundle id, which version the last read stalled on
    (`stalled`), if the last read stalled: Try Again is a run of its own,
    and starts at that version (`list_versions`).

    Read without following a link, and only from a regular file of at most
    `_VERSIONS_CACHE_MAX` bytes: a FIFO there would wait forever for a
    writer, and a link to /dev/zero would never end. Written whole to a new
    file (0600) that replaces it, so a reader never sees half of one and a
    link there is replaced, never written through."""

    def __init__(
        self, path: Path, apps: dict[str, dict[str, dict[str, Any]]], stalled: dict[str, str] | None = None
    ) -> None:
        self.path = path
        self.apps = apps
        self.stalled = stalled if stalled is not None else {}

    @classmethod
    def load(cls, cache_dir: Path) -> _VersionCache:
        path = cache_dir / VERSIONS_CACHE
        raw = cls._read(path)
        try:
            data = json.loads(raw) if raw is not None else None
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or data.get("format") != _VERSIONS_CACHE_FORMAT:
            data = {}
        apps, stalled = data.get("apps"), data.get("stalled")
        return cls(
            path,
            apps if isinstance(apps, dict) else {},
            {bundle: ident for bundle, ident in stalled.items() if isinstance(ident, str)}
            if isinstance(stalled, dict) else {},
        )

    @staticmethod
    def _read(path: Path) -> bytes | None:
        """`path`'s bytes when it is a regular file, not a link, of at most
        `_VERSIONS_CACHE_MAX` bytes; else None."""
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > _VERSIONS_CACHE_MAX:
                return None
            chunks: list[bytes] = []
            size = 0
            while size <= _VERSIONS_CACHE_MAX:
                chunk = os.read(fd, _VERSIONS_CACHE_MAX + 1 - size)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
        except OSError:
            return None
        finally:
            os.close(fd)
        return b"".join(chunks) if size <= _VERSIONS_CACHE_MAX else None

    def known(self, bundle_id: str) -> dict[str, ipatool.VersionMetadata]:
        rows = self.apps.get(bundle_id)
        if not isinstance(rows, dict):
            return {}
        found: dict[str, ipatool.VersionMetadata] = {}
        for external_id, row in rows.items():
            if not isinstance(row, dict):
                continue
            shown, released = row.get("version"), row.get("release_date")
            if isinstance(shown, str) and shown and (released is None or isinstance(released, str)):
                found[external_id] = ipatool.VersionMetadata(version=shown, release_date=released)
        return found

    def remember(self, bundle_id: str, external_id: str, meta: ipatool.VersionMetadata) -> None:
        """What `external_id` was read to be. The read answered, so it
        wasn't a stall either."""
        rows = self.apps.get(bundle_id)
        if not isinstance(rows, dict):
            rows = self.apps[bundle_id] = {}
        rows[external_id] = {"version": meta.version, "release_date": meta.release_date}
        self.stalled.pop(bundle_id, None)
        self._write()

    def last_read(self, bundle_id: str, *, stalled_on: str | None) -> None:
        """How `bundle_id`'s last version read ended, when nothing about the
        version is remembered: it stalled on `stalled_on`, or (None) it
        answered, with an error of its own."""
        if self.stalled.get(bundle_id) == stalled_on:
            return
        if stalled_on is None:
            del self.stalled[bundle_id]
        else:
            self.stalled[bundle_id] = stalled_on
        self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".appstore-versions-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"format": _VERSIONS_CACHE_FORMAT, "apps": self.apps, "stalled": self.stalled}, f)
                os.replace(tmp, self.path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError:
            pass


def newest_first(ids: list[str]) -> list[str]:
    """`ids`, once each, newest first. The App Store numbers its version ids
    as it issues them, so the newest is the highest, whatever order
    `list-versions` gave them in."""
    return sorted(dict.fromkeys(ids), key=int, reverse=True)


def held_version(app: AppConfig) -> str | None:
    """The version `app` is held at: its `preferred_version` pin, or None
    for `latest`. A get of a chosen version sets it (`hold`); a get of the
    latest lets go (`release_hold`). A pin set by hand is a hold too: either
    way, builds use that version."""
    return None if app.preferred_version == LATEST else app.preferred_version


def held_version_id(app: AppConfig) -> str | None:
    """The App Store version id of the build `app` is held at
    (`preferred_version_id`), or None: not held, held by hand, or held before
    2.3. With it, builds use that build's own file (`ipa_cache.admits`)."""
    return None if app.preferred_version == LATEST else app.preferred_version_id


@dataclass(frozen=True)
class HeldBuild:
    """Which of a version's builds a hold names: build `number` of `count`,
    from the oldest."""

    number: int
    count: int


def held_build(cfg: Config, app: AppConfig) -> HeldBuild | None:
    """For an app held by build whose version the App Store has more than one
    build of (as far as `--appstore-versions` has read), which one. None for
    every other app, and when no listing has read the held build."""
    held_id = held_version_id(app)
    if held_id is None:
        return None
    known = _VersionCache.load(cfg.cache_dir).known(app.bundle_id)
    meta = known.get(held_id)
    if meta is None:
        return None
    same = same_version_ids(known, meta.version)
    if len(same) < 2 or held_id not in same:
        return None
    return HeldBuild(number=same.index(held_id) + 1, count=len(same))


def list_versions(
    cfg: Config,
    app_name: str,
    *,
    offset: int = 0,
    limit: int = VERSIONS_LIMIT,
    emitter: JsonEmitter | None,
    say: Say,
) -> None:
    """The versions the App Store serves this Apple ID for `app_name`,
    newest first: one `appstore-release` each, then `appstore-releases-end`.

    `list-versions` gives only ids, and each id's version and date is a
    request of its own (`ipatool.version_metadata`), so those are read for
    the `limit` versions from `offset` on, and for no others. What was read
    is remembered (`_VersionCache`), so every row whose id was read once
    says its version, and a page already read costs nothing. A version Apple
    won't describe says so on its row (`error`), and the rest go on. One
    that doesn't answer in time stops the listing, which fails: a dropped
    connection would otherwise wait out every version on the page in turn.

    Try Again (the same page, run again) starts at the version that stalled,
    so one that stalls every time would stop the list there for good. When
    the last version read for this app stalled on this same one, it's said on
    its row as one Apple won't describe is, and the rest go on. Any other
    read in between, of another version, starts the count again.

    Checked as a get is first (`_require_signed_in`), and refused the same
    ways: Apple's own apps, ipatool missing or signed out, the login keychain
    locked. Only apps this Apple ID has: anything else is the get's
    `needs-license` refusal.
    """
    app = cfg.get_app(app_name)
    _refuse_apples_own(app)
    tools = cfg.appstore
    ipa_path = _ipatool_path(tools)
    source = _require_signed_in(tools, ipa_path)
    before = appstore_account.cache_before(source)
    try:
        ids = newest_first(ipatool.list_versions(ipa_path, app.bundle_id))
    except ipatool.LicenseRequired as e:
        raise _needs_licence(app_name, app, emitter, e) from e
    cache = _VersionCache.load(cfg.cache_dir)
    known = cache.known(app.bundle_id)
    held = held_version(app)
    held_id = held_version_id(app)
    if not emitter:
        say(f"{app_name} ({app.bundle_id}): {len(ids)} version(s) on the App Store for this Apple ID, newest first")
    for index, external_id in enumerate(ids):
        meta = known.get(external_id)
        error: str | None = None
        if meta is None and offset <= index < offset + limit:
            try:
                meta = ipatool.version_metadata(ipa_path, app.bundle_id, external_id)
            except ipatool.VersionUnavailable as e:
                error = e.message
                cache.last_read(app.bundle_id, stalled_on=None)
            except ipatool.LicenseRequired as e:
                raise _needs_licence(app_name, app, emitter, e) from e
            except ipatool.VersionMetadataTimeout as e:
                if cache.stalled.get(app.bundle_id) != external_id:
                    cache.last_read(app.bundle_id, stalled_on=external_id)
                    raise AppStoreError(
                        f"the list of {app_name}'s versions stopped: {e.message}",
                        hint=f"{e.hint}; the versions read so far are remembered, and if this one "
                        "stalls again, the list goes on without it",
                    ) from e
                error = f"Timed out twice in a row, so the list went on without it: {e.message}"
            else:
                cache.remember(app.bundle_id, external_id, meta)
                # So a later row's `staged` counts this one's number too.
                known[external_id] = meta
        staged = meta is not None and _is_staged(cfg, app, meta.version, external_id, known)
        if emitter:
            emitter.emit(
                "appstore-release",
                app=app_name,
                bundle_id=app.bundle_id,
                external_id=external_id,
                index=index,
                newest=index == 0,
                version=meta.version if meta else None,
                release_date=meta.release_date if meta else None,
                staged=staged,
                error=error,
            )
            continue
        is_held = meta is not None and (
            external_id == held_id if held_id is not None else meta.version == held
        )
        notes = [n for n, on in (("newest", index == 0), ("staged", staged), ("held", is_held)) if on]
        shown = f"{meta.version:<12} {(meta.release_date or '')[:10]:<10}" if meta else f"{'?':<12} {'':<10}"
        say(f"  {shown}  {external_id}" + (f"  ({', '.join(notes)})" if notes else "")
            + (f"  — {error}" if error else ""))
    _adopt_cache(source, before)
    if emitter:
        emitter.emit(
            "appstore-releases-end", app=app_name, bundle_id=app.bundle_id,
            count=len(ids), offset=offset, limit=limit, held_version=held,
            held_version_id=held_id,
        )
        return
    quoted = shlex.quote(app_name)
    if offset + limit < len(ids):
        say(f"more: graft --appstore-versions --app {quoted} --offset {offset + limit}")
    if ids:
        say(f"get one: graft --appstore-get --app {quoted} --version-id <id>")


def _is_staged(
    cfg: Config, app: AppConfig, version: str, external_id: str,
    known: dict[str, ipatool.VersionMetadata],
) -> bool:
    """Whether a listing calls `external_id`'s row staged: its build's own
    file is in the cache, or the plain file for its version is and no other
    row read so far has the same number. Names only, for a listing: a get
    still checks the file itself (`_staged_as_get`).

    "So far" is the known limit `already_staged_version` describes: a twin
    not yet read, past this page or further down this one on a first listing,
    isn't counted, and the plain file is called this row's."""
    if _unsafe_in_filename(version):
        return False
    if (cfg.cache_dir / staged_name(app.bundle_id, version, external_id)).is_file():
        return True
    plain = cfg.cache_dir / staged_name(app.bundle_id, version)
    return plain.is_file() and len(same_version_ids(known, version)) <= 1


def hold(
    cfg: Config, app_name: str, version: str, *, version_id: str | None = None,
    emitter: JsonEmitter | None, say: Say, warn: Say = _say_nothing,
) -> None:
    """Hold `app_name` at `version`: pin its `preferred_version`, so its
    builds use that version's IPA (`find_in_cache`) and `--appstore-check`
    doesn't call a newer one an update. After a get of a chosen version.

    `version_id` is the build, when the get staged that build's own file
    (`preferred_version_id`): its builds then use exactly that build. None
    when the get found the plain file of a version with one build."""
    _set_hold(cfg, app_name, version, version_id, emitter=emitter, say=say, warn=warn)


def release_hold(
    cfg: Config, app_name: str, *, emitter: JsonEmitter | None, say: Say, warn: Say = _say_nothing,
) -> None:
    """Let go of `app_name`'s hold, if it has one: back to `latest`, and no
    build. After a get of the latest, which is what the user asked for by
    pressing it."""
    _set_hold(cfg, app_name, None, None, emitter=emitter, say=say, warn=warn)


def _hold_changed_warning(
    app_name: str, found: tuple[str, str | None], target: tuple[str, str | None],
) -> str:
    """Why a get left the hold as it found it: `found` is what the config
    said by then, `target` what the get would have written.

    When the version is what changed, that is all it says. When it is the
    same version held another way (by version, or another build of it),
    naming the version twice said nothing: the build is what differs."""
    (found_version, found_id), (version, version_id) = found, target
    if found_version != version or version == LATEST:
        instead = f"holding it at {version}" if version != LATEST else f"setting it back to {LATEST}"
        return (
            f"{app_name}'s preferred version changed to {found_version} while this get ran, "
            f"so Graft kept {found_version} instead of {instead}"
        )
    now = (
        f"a build of {version} (App Store version id {found_id})" if found_id is not None
        else f"{version} by version"
    )
    instead = (
        f"the build this get asked for (App Store version id {version_id})" if version_id is not None
        else f"{version} by version"
    )
    return f"{app_name}'s hold changed to {now} while this get ran, so Graft kept it instead of holding {instead}"


def let_go_of_the_build(
    cfg: Config, app_name: str, version: str, *, emitter: JsonEmitter | None, warn: Say,
) -> None:
    """After a drop, `--provide-ipa` or `--decrypt` staged `version` of
    `app_name` under its plain name: when the app is held by build at that
    version, hold it there by version instead, so builds use the IPA just
    given (`ipa_cache.admits` takes only the held build's own file, and
    nothing says the plain one is that build).

    Said with `appstore-hold` (`version_id` null, `previous` the same
    version) and a warning, so the person knows the build is no longer
    named. An app not held by build, or held at another version, is left
    as it is. `cfg` was read before the IPA was staged, which can take
    minutes for a decrypt: the hold is swapped against it, and one changed
    since (another get) is left alone, without a word.
    """
    app = cfg.get_app(app_name)
    held_id = held_version_id(app)
    if held_id is None or app.preferred_version != version:
        return
    expected = (version, held_id)
    if swap_preferred_version(cfg.path, app_name, expected=expected, new=(version, None)) != expected:
        return
    if emitter:
        emitter.emit(
            "appstore-hold", app=app_name, bundle_id=app.bundle_id,
            version=version, version_id=None, previous=version,
        )
    warn(
        f"{app_name}: this IPA may not be the build of {version} it was held at "
        f"(App Store version id {held_id}), so Graft now holds it at {version} by version, "
        f"and builds use this IPA"
    )


def _set_hold(
    cfg: Config, app_name: str, version: str | None, version_id: str | None, *,
    emitter: JsonEmitter | None, say: Say, warn: Say,
) -> None:
    """Write the hold, and say so with `appstore-hold`, only when it changes:
    `version` and `version_id` are the new ones (null when let go),
    `previous` the old version.

    `cfg` was read before the get started, and a download can take minutes.
    The config is read again as the hold is written (`swap_preferred_version`),
    and only the two hold keys are written. When they were changed meanwhile
    (Edit App, another get), that later choice stays: the get warns and holds
    nothing.
    """
    app = cfg.get_app(app_name)
    expected = (app.preferred_version, held_version_id(app))
    target = (version, version_id) if version is not None else (LATEST, None)
    found = swap_preferred_version(cfg.path, app_name, expected=expected, new=target)
    if found == target:
        return
    if found != expected:
        warn(_hold_changed_warning(app_name, found, target))
        return
    previous = held_version(app)
    if emitter:
        emitter.emit(
            "appstore-hold", app=app_name, bundle_id=app.bundle_id,
            version=version, version_id=version_id, previous=previous,
        )
    elif version is not None:
        build = f" (App Store version id {version_id})" if version_id else ""
        say(
            f"{app_name}: held at {version}{build}. Builds use it, and Graft won't call a newer "
            f"version an update, until a get of the latest (graft --appstore-get --app {shlex.quote(app_name)})"
        )
    else:
        say(f"{app_name}: no longer held at {previous}; builds use the newest staged version")


def claim(
    cfg: Config, app_name: str, *, emitter: JsonEmitter | None, say: Say,
    expect_email: str | None = None,
) -> bool:
    """Get `app_name`'s free license for the signed-in Apple ID. Returns
    whether it already had one.

    Refuses unless the iTunes lookup, in the app's own store (`_listing`),
    says the price is 0. An unknown price is refused too: "free" is never
    assumed. It does nothing else: the app starts the get itself afterward,
    so a claim can't half-finish a download.

    With `expect_email` (`--expect-email`, which Graft.app sends with
    the Apple ID its confirmation named), it also refuses unless ipatool is
    signed in as that Apple ID: the `account-changed` refusal.
    """
    app = cfg.get_app(app_name)
    _refuse_apples_own(app)
    tools = cfg.appstore
    path = _ipatool_path(tools)
    try:
        source = _require_signed_in(tools, path, expect_email=expect_email)
    except AccountChanged as e:
        if emitter:
            emitter.emit("appstore-refused", app=app_name, reason=REFUSED_ACCOUNT_CHANGED, email=e.email)
        raise
    listing = _listing(app)
    if listing is None or listing.price is None:
        raise AppStoreError(
            f"couldn't check {app.bundle_id}'s price on the App Store",
            hint="Graft gets an app only when it can see the app is free; try again, or get it in the App Store",
        )
    if listing.price != 0:
        _refuse_for_licence(emitter, app_name, REFUSED_PAID, listing)
        raise AppStoreError(
            f"{listing.name} costs {listing.formatted_price or listing.price}: Graft gets only free apps",
            hint="buy it in the App Store with the same Apple ID, then get it in Graft",
        )
    before = appstore_account.cache_before(source)
    already = ipatool.purchase(path, app.bundle_id)
    _adopt_cache(source, before)
    if emitter:
        emitter.emit("appstore-claimed", app=app_name, bundle_id=app.bundle_id, already_owned=already)
    else:
        say(f"{app_name}: {'already owned by' if already else 'added to'} this Apple ID")
    return already


def main_binary_encrypted(ipa: Path) -> bool:
    """Whether `ipa`'s main executable still has a non-zero cryptid.

    Only the main binary, extracted alone: the question is "did this IPA come
    straight from the App Store", and the main executable answers it without
    inflating a gigabyte bundle. An executable that is missing or not a Mach-O
    reads as not encrypted — this cannot judge it, and the build's own stage
    2.5 check over every binary remains the authority.
    """
    info = read_info_plist(ipa)
    app_dir = info.info_plist_path_in_zip.rsplit("/", 1)[0]
    member = f"{app_dir}/{info.executable}"
    with zipfile.ZipFile(ipa) as zf:
        try:
            entry = zf.getinfo(member)
        except KeyError:
            return False
        with tempfile.TemporaryDirectory(prefix="graft-cryptid-") as scratch:
            binary = Path(scratch) / "main"
            with zf.open(entry) as src, binary.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            try:
                return not check_cryptid(binary).is_decrypted
            except IPAInspectionError:
                # Not a Mach-O the reader can parse: the docstring's "cannot
                # judge it". `otool` printed no cryptid and exited 0 for these,
                # so this held by accident until the reader raised. Stage 2.5
                # still refuses any file with a Mach-O header it cannot read.
                return False


@contextmanager
def decrypted_if_encrypted(
    cfg: Config,
    app_name: str,
    source: Path,
    *,
    emitter: JsonEmitter | None,
    allow_unverified: bool = False,
    say: Say = _say_nothing,
    warn: Say = _say_nothing,
) -> Iterator[Path]:
    """Yield `source` itself, or a decrypted copy of it when it is still encrypted.

    The `--provide-ipa` half of the feature. An encrypted drop used to be
    cached as-is and the build carried an encrypted binary forward with only a
    warning at stage 2.5; now it goes through the
    same decrypt as `fetched`, or — when UnfairPlay is missing or this Mac is
    not enrolled — is refused with the setup command, so a file the build would
    only reject later is never cached at all.

    An IPA that is already decrypted is yielded untouched: no events, no
    working directory, no UnfairPlay.
    """
    if not main_binary_encrypted(source):
        yield source
        return
    tools = cfg.appstore
    _require_unfairplay(tools)
    with _stop_cleans_up(), private_workdir(cfg.cache_dir) as work:
        decrypted = _decrypt(
            tools, source, work,
            app_name=app_name, emitter=emitter, say=say,
            allow_unverified=allow_unverified, warn=warn,
            retry=(
                f"graft --provide-ipa {shlex.quote(str(source))} "
                f"--app {shlex.quote(app_name)}"
            ),
        )
        _stage(emitter, say, app_name, "stage")
        yield decrypted
