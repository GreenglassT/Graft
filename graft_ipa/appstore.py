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

**Credentials never pass through here.** `ipatool auth login` takes a password
and a 2FA code; `sudo unfairplay setup` takes root. Both stay Terminal commands
the user types. This module only *reports* whether they have been done — and,
when they have not, names the exact command to run.

**The encrypted download carries the Apple ID's license**, so it lives only in
a 0700 directory under `cache_dir/.appstore-tmp/` and is deleted on success and
on failure alike. The decrypted IPA is made with `--strip-account-data`, and
staging (`provide_ipa.stage`) still removes `iTunesMetadata.plist` and every
`SC_Info/` itself and checks the result, so what reaches the cache identifies
nobody whatever UnfairPlay did.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import host, install_hints, ipatool, itunes, unfairplay
from .config import AppStoreToolsConfig, Config
from .errors import AppStoreError, ITunesError
from .ipa_cache import find_in_cache
from .ipa_inspect import check_cryptid, read_info_plist
from .json_output import JsonEmitter
from .repo import version_key

# Under `cache_dir`, and dotted: `find_in_cache` and `--staged-ipas` glob
# `cache_dir/*.ipa` without recursing, so nothing in here is ever offered as a
# staged IPA, half-written or not.
TMP_DIRNAME = ".appstore-tmp"

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

    `signed_in` is all this says about the Apple ID. `ipatool auth info` prints
    the account's email and name; `ipatool.signed_in` reduces that to a bool
    before it gets here, so neither can reach the wire or a log. It is null
    when `auth info` gave no verdict (hung, crashed): "couldn't tell", which
    must not read as "sign in again".
    """
    ipa_path = ipatool.locate(tools.ipatool)
    ipa_present = ipatool.present(ipa_path)
    ipa_version = ipatool.version(ipa_path) if ipa_present else None
    signed_in = ipatool.signed_in(ipa_path) if ipa_present else False
    up_present = unfairplay.present(tools.unfairplay)
    up_version = unfairplay.version(tools.unfairplay) if up_present else None
    enrolled = unfairplay.enrolled(tools.unfairplay_models)
    up_install: str | None = None

    warning = None
    if ipa_version in ipatool.KNOWN_BAD_VERSIONS:
        warning = ipatool.bad_version_hint(ipa_version, path=ipa_path, configured=tools.ipatool)

    # The commands for whatever is missing, with what each is for as a shell
    # comment above it, so the whole list stays safe to paste. Printed, never
    # run. The install for each tool is also on its entry as `install`, one
    # string, for a Copy button.
    commands: list[str] = []
    ipa_install: str | None = None
    if not ipa_present or warning:
        target = ipatool.install_target(ipa_path, tools.ipatool)
        commands.append(
            f"# {warning}" if warning
            else f"# install ipatool {ipatool.GOOD_VERSION}, the release Graft is tested with, to {target}:"
        )
        commands += ipatool.install_steps(target)
        ipa_install = ipatool.install_command(target)
    if ipa_present and signed_in is False:
        commands.append(ipatool.login_command(ipa_path))
    if ipa_present and signed_in is None:
        # Not the login command — nothing says the account is signed out — but
        # not nothing either: "couldn't tell" needs a next step.
        commands.append(
            f"# run '{ipa_path} auth info' in Terminal to see why ipatool "
            "couldn't say whether it is signed in"
        )
    if (not up_present or not enrolled) and host.apple_silicon() is False:
        # Building UnfairPlay and `sudo unfairplay setup` both lead nowhere on
        # an Intel Mac, so neither is offered; the reason is.
        commands.append(f"# App Store decryption can't be set up here: {host.INTEL_MAC}.")
        commands.append(f"# {_INTEL_REASON}.")
    elif not up_present:
        steps = unfairplay.build_steps(tools.unfairplay)
        commands.append(
            f"# build UnfairPlay {unfairplay.TESTED_TAG} from source to {tools.unfairplay}:"
        )
        if steps[0] == "brew install go":
            note = install_hints.homebrew_note()
            if note:
                commands.append(f"# {note} (or install Go from https://go.dev/dl/)")
        commands += steps
        up_install = " && ".join(steps)
    if not enrolled and host.apple_silicon() is not False:
        commands.append(
            "# enroll once, as root: install an iPhone/iPad app on this Mac from the "
            "App Store, signed in with the same Apple ID, open it once, and put its "
            "name in the path:"
        )
        commands.append(unfairplay.setup_command(tools.unfairplay))

    return {
        "ipatool": {
            "path": str(ipa_path),
            "present": ipa_present,
            "version": ipa_version,
            "signed_in": signed_in,
            "warning": warning,
            "install": ipa_install,
        },
        "unfairplay": {
            "path": str(tools.unfairplay),
            "present": up_present,
            "version": up_version,
            "enrolled": enrolled,
            "model_dir": str(tools.unfairplay_models),
            "install": up_install,
        },
        "setup_commands": commands,
    }


def report_status(cfg: Config, *, emitter: JsonEmitter | None, say: Say) -> None:
    payload = status(cfg.appstore)
    if emitter:
        emitter.emit("appstore-status", **payload)
        return
    ipa, up = payload["ipatool"], payload["unfairplay"]
    if ipa["present"]:
        account = {True: "signed in", False: "NOT signed in"}.get(
            ipa["signed_in"], "couldn't tell whether it is signed in"
        )
        say(f"ipatool     {ipa['version'] or '(unknown version)'}  {ipa['path']}  — {account}")
    else:
        say(f"ipatool     not found at {ipa['path']}")
    if ipa["warning"]:
        say(f"            warning: {ipa['warning']}")
    if up["present"]:
        state = "enrolled" if up["enrolled"] else "NOT enrolled"
        say(f"UnfairPlay  {up['version'] or '(unknown version)'}  {up['path']}  — {state}")
    else:
        say(f"UnfairPlay  not found at {up['path']}")
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
    the US store must not hide the answer for the rest, and "couldn't tell" has
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
            store_version = itunes.lookup_by_bundle_id(app.bundle_id).version
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
    ready = unfairplay.present(tools.unfairplay) and unfairplay.enrolled(tools.unfairplay_models)
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
    if not unfairplay.present(tools.unfairplay):
        raise AppStoreError(
            f"UnfairPlay not found at {tools.unfairplay}",
            hint=unfairplay.install_hint(tools.unfairplay),
        )
    if not unfairplay.enrolled(tools.unfairplay_models):
        raise AppStoreError(
            f"this Mac isn't enrolled for decryption yet (no model in {tools.unfairplay_models})",
            hint=(
                "enroll once from Terminal, with any iPhone/iPad app installed on "
                "this Mac from the App Store under the same Apple ID: "
                f"{unfairplay.setup_command(tools.unfairplay)}"
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
    legitimately say `false`, so the CLI can be told to accept one
    (`--allow-unverified`) with a warning; the GUI never offers that.
    """
    _stage(emitter, say, app_name, "decrypt")
    report = unfairplay.decrypt(
        tools.unfairplay,
        input_ipa=source,
        output_ipa=work / "decrypted.ipa",
        report=work / "unfairplay-report.json",
        model_dir=tools.unfairplay_models,
    )
    if not report.plaintext_verified:
        if not allow_unverified:
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
    if emitter:
        emitter.emit(
            "appstore-decrypted",
            app=app_name,
            encrypted_images=report.encrypted_images,
            decrypted_pages=report.decrypted_pages,
            plaintext_verified=report.plaintext_verified,
        )
    return report.output_path


@contextmanager
def fetched(
    cfg: Config,
    app_name: str,
    *,
    emitter: JsonEmitter | None,
    allow_unverified: bool = False,
    say: Say = _say_nothing,
    warn: Say = _say_nothing,
) -> Iterator[Path]:
    """Download `app_name`'s latest version, decrypt it, and yield the decrypted IPA.

    The caller stages what it is handed inside the `with`; on exit — success,
    failure or Stop — the working directory and everything in it is gone.

    Everything that can be checked locally is checked *before* the download,
    which can be a gigabyte: a missing or unenrolled UnfairPlay, and a signed
    out ipatool, are refusals with the Terminal command in the hint. A download
    error is still classified afterwards, since an expired session passes
    `auth info`.
    """
    app = cfg.get_app(app_name)
    tools = cfg.appstore
    ipa_path = ipatool.locate(tools.ipatool)
    if not ipatool.present(ipa_path):
        raise AppStoreError(
            f"ipatool not found at {ipa_path}", hint=ipatool.install_hint(ipa_path)
        )
    _require_unfairplay(tools)
    found_version = ipatool.version(ipa_path)
    if found_version in ipatool.KNOWN_BAD_VERSIONS:
        raise AppStoreError(
            f"ipatool {found_version} at {ipa_path} is known to fail downloads",
            hint=ipatool.bad_version_hint(found_version, path=ipa_path, configured=tools.ipatool),
        )
    account = ipatool.signed_in(ipa_path)
    if account is None:
        raise AppStoreError(
            "couldn't tell whether ipatool is signed in — `auth info` gave no answer",
            hint=(
                f"run `{shlex.quote(str(ipa_path))} auth info` in Terminal to "
                "see why, then try again"
            ),
        )
    if not account:
        raise AppStoreError(
            "ipatool is not signed in to the App Store",
            hint=(
                f"sign in from Terminal: {ipatool.login_command(ipa_path)} — "
                "the first login can sit silent for ~8 minutes; that is normal"
            ),
        )

    with _stop_cleans_up(), private_workdir(cfg.cache_dir) as work:
        _stage(emitter, say, app_name, "download")
        encrypted = ipatool.download(ipa_path, app.bundle_id, work)
        decrypted = _decrypt(
            tools, encrypted, work,
            app_name=app_name, emitter=emitter, say=say,
            allow_unverified=allow_unverified, warn=warn,
            retry=f"graft --appstore-get --app {shlex.quote(app_name)}",
        )
        _stage(emitter, say, app_name, "stage")
        yield decrypted


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
            return not check_cryptid(binary).is_decrypted


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
