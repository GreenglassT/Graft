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
import os
import shlex
import shutil
import signal
import tempfile
import threading
import time
import zipfile
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import appstore_account, host, install_hints, ipatool, itunes, keychain, unfairplay
from .config import AppConfig, AppStoreToolsConfig, Config
from .errors import AppStoreError, IPAInspectionError, ITunesError
from .ipa_cache import CachedIPA, find_in_cache
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
    if (not up_present or not enrolled) and host.apple_silicon() is False:
        # Building UnfairPlay and setting up decryption both lead nowhere on
        # an Intel Mac, so neither is offered; the reason is.
        commands.append(f"# App Store decryption can't be set up here: {host.INTEL_MAC}.")
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
    if not enrolled and host.apple_silicon() is not False:
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
    """
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
        update = store_version is not None and (
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
                error=error,
            )
            continue
        if error:
            say(f"{name}: couldn't check the App Store — {error}")
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
    if not ready and host.apple_silicon() is False:
        # Checked only when something is missing, so a working setup never
        # pays for it — and an Intel Mac is told the real reason rather than
        # handed an enrollment command that cannot succeed there.
        raise AppStoreError(
            f"decrypting App Store IPAs needs an Apple silicon Mac, and {host.INTEL_MAC}",
            hint=(
                f"{_INTEL_REASON}. Use an IPA that is already decrypted, or dump "
                "one from a jailbroken device (--decrypt)"
            ),
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


def _ipatool_path(tools: AppStoreToolsConfig) -> Path:
    """The ipatool to run (`ipatool.locate`), refused when it isn't there."""
    path = ipatool.locate(tools.ipatool, explicit="ipatool" in tools.explicit)
    if not ipatool.present(path):
        raise AppStoreError(f"ipatool not found at {path}", hint=ipatool.install_hint(path))
    return path


def _require_signed_in(tools: AppStoreToolsConfig, path: Path) -> str:
    """Not a known-bad ipatool, and signed in. Shared by `fetched` and
    `claim`, so neither starts anything with an ipatool that can't finish it.
    Returns the ipatool's source.

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
        raise ipatool.KeychainRefusal(refused=True, path=path)
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
            "get it on the iPhone from the App Store, and pick another app for Graft",
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
    app_name: str, app: AppConfig, emitter: JsonEmitter | None, cause: AppStoreError
) -> AppStoreError:
    """A download refused for want of a license, as the `needs-license`
    refusal (with the price, so the app can offer Get (free) or Open in App
    Store) and an error whose hint says which of the two applies."""
    listing = _listing(app)
    _refuse_for_licence(emitter, app_name, REFUSED_NEEDS_LICENSE, listing)
    if listing is not None and listing.price == 0:
        hint = (
            "it's free: get it for this Apple ID with Get (free) in Graft, or "
            f"`graft --appstore-claim --app {shlex.quote(app_name)}`, then get it again"
        )
    elif listing is not None and listing.price:
        hint = (
            f"it costs {listing.formatted_price or listing.price}: buy it in the App Store "
            "with the same Apple ID, then get it again"
        )
    else:
        hint = cause.hint
    return AppStoreError(cause.message, hint=hint)


@contextmanager
def fetched(
    cfg: Config,
    app_name: str,
    *,
    emitter: JsonEmitter | None,
    allow_unverified: bool = False,
    again: bool = False,
    say: Say = _say_nothing,
    warn: Say = _say_nothing,
) -> Iterator[Path]:
    """Download `app_name`'s latest version, decrypt it, and yield the decrypted IPA.

    Whether to download at all is the caller's question (`already_staged`).
    `again` says it asked with `--again`, so the Terminal command an
    unverified refusal names keeps it: without it, that command would find the
    version staged and download nothing.

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
            encrypted = ipatool.download(ipa_path, app.bundle_id, work)
        except ipatool.LicenseRequired as e:
            raise _needs_licence(app_name, app, emitter, e) from e
        except AppStoreError as e:
            raise _found_elsewhere(app, e) from e
        _adopt_cache(source, before)
        decrypted = _decrypt(
            tools, encrypted, work,
            app_name=app_name, emitter=emitter, say=say,
            allow_unverified=allow_unverified, warn=warn,
            retry=f"graft --appstore-get --app {shlex.quote(app_name)}" + (" --again" if again else ""),
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
    version = listing.version if listing is not None else None
    if not isinstance(version, str) or not version or _unsafe_in_filename(version):
        return None
    path = cfg.cache_dir / f"{app.bundle_id}_{version}.ipa"
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
    return CachedIPA(path=path, bundle_id=info.bundle_id, version=info.version)


def report_already_staged(
    app_name: str, staged: CachedIPA, *, emitter: JsonEmitter | None, say: Say
) -> None:
    """Say that a get stopped before downloading: `staged` is the App Store's
    current version already (`already_staged`)."""
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
    say(
        f"{app_name}: already have {staged.version}, the App Store's current version "
        f"({staged.path.name}) — nothing downloaded"
    )
    say(f"  to download it again anyway: graft --appstore-get --app {shlex.quote(app_name)} --again")


def claim(cfg: Config, app_name: str, *, emitter: JsonEmitter | None, say: Say) -> bool:
    """Get `app_name`'s free license for the signed-in Apple ID. Returns
    whether it already had one.

    Refuses unless the iTunes lookup, in the app's own store (`_listing`),
    says the price is 0. An unknown price is refused too: "free" is never
    assumed. It does nothing else: the app starts the get itself afterward,
    so a claim can't half-finish a download.
    """
    app = cfg.get_app(app_name)
    _refuse_apples_own(app)
    tools = cfg.appstore
    path = _ipatool_path(tools)
    source = _require_signed_in(tools, path)
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
