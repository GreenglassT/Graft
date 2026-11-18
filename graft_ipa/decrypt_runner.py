"""Pull a decrypted IPA off a jailbroken device.

The second half of on-device decryption. `device_apps` names the encrypted
apps; this reads one back after iOS has decrypted its FairPlay pages in memory
and writes out a cryptid-0 IPA — the thing the build pipeline's stage 2.5
demands and that Apple does not distribute.

Two backends, chosen for one real difference — spawn vs. attach:

  - **bagbak** spawns the app fresh over Frida's own transport (no device SSH),
    then reads its pages. Convenient, but it races the kernel: the main
    binary's pages aren't decrypted until the app has executed, so a spawn dump
    is hit-or-miss on the main executable.
  - **frida-ios-dump** attaches to an *already-running* app, whose main binary
    is decrypted by the time you dump it, and pulls the bundle over SSH. It
    needs OpenSSH on the device and the app open — more setup, in exchange for
    the reliability bagbak lacks on that one binary.

`decrypt_app(backend=…)` is the seam; each backend is its own dumper.

The output lands in `cache_dir` under the canonical `<bundle_id>_<version>.ipa`
name — exactly where `provide_ipa` stages a hand-supplied IPA and where
`acquire_ipa`'s `find_in_cache` looks first. So a dump feeds the existing
build → inject → push pipeline with nothing downstream needing to change.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import re
import shlex
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import data_dirs, install_hints
from .child_env import child_env
from .child_env import which as child_which
from .errors import EXIT_DECRYPT, GraftError


class DecryptError(GraftError):
    exit_code = EXIT_DECRYPT


class AppWillNotRunError(DecryptError):
    """The app could not be got onto the screen, so no dump could start.

    Distinct because it ends the escalation ladder. Switching backends is the
    right move when a *dump* fails, and useless when the app itself quits on
    launch — bagbak launches it the same way and meets the same wall, having
    spent a minute and opened the crashing app a third time in front of the
    user.
    """


class StillEncryptedError(DecryptError):
    """The dump ran, but some binaries came back with cryptid set.

    A distinct type, and one that carries *what* was left encrypted, because the
    answer differs entirely by which: leftovers inside `.appex` bundles are a
    structural limit that another attempt with the same tool will reproduce
    exactly, while an encrypted main binary or framework means the dump itself
    came back incomplete and the other backend may well succeed. The escalation
    ladder has to tell those apart, and re-parsing the human sentence to do it
    would make the message's wording load-bearing.
    """

    def __init__(
        self, message: str, *, hint: str | None = None,
        paths: list[str] | None = None, extensions_only: bool = False,
    ) -> None:
        super().__init__(message, hint=hint)
        self.paths = paths or []
        self.extensions_only = extensions_only


@dataclass
class DecryptResult:
    ipa_path: Path
    bundle_id: str
    stdout: str


# The two backends, and why both exist — they differ by *coverage*, not by
# reliability, which is the thing to get right before choosing between them.
#
#   frida-ios-dump  attaches to the already-running app and decrypts the
#                   modules loaded into its process. Reliable for the main
#                   binary and the frameworks. Cannot touch app extensions at
#                   all: an .appex runs in its own process, so it is never
#                   enumerated and is copied from disk still encrypted.
#                   Measured on SmartLife: 36 binaries, 11 left encrypted, all
#                   of them PlugIns/*.appex.
#
#   bagbak          attaches to every extension process too, so it is the only
#                   one that can produce a complete bundle — and that is
#                   exactly why it is the flaky one, since iOS reaps
#                   short-lived extension processes mid-dump. It also *spawns*
#                   the app, which is why its main binary is hit-or-miss.
#
# So an app with encrypted extensions can only ever be completed by bagbak,
# and an app without them is better served by frida-ios-dump.
BAGBAK = "bagbak"
FRIDA = "frida-ios-dump"

#: The bagbak install command, the one copy every hint uses. Pinned: bagbak
#: 5.x bundles frida 17, which cannot talk to the 16.x frida-server this
#: project needs (see the pin in pyproject.toml).
BAGBAK_INSTALL = "npm install -g bagbak@4.0.3"

#: bagbak's argv and its transport both changed at 5.0, and the two majors need
#: *different frida majors on the device* — which is the constraint that
#: actually decides which one you can run:
#:
#:   bagbak 4.x   frida ^16   argv `-D <udid> -o <out> <bundle>`, pulls over SSH
#:                            (SSH_USERNAME/SSH_PASSWORD, default mobile:alpine)
#:   bagbak 5.x   frida ^17   argv `-D <udid> -o <dir> <bundle> <mode>`, no SSH
#:
#: frida-ios-dump's agent uses pre-17 APIs, so this project pins frida <17 and
#: the device runs a 16.x frida-server. bagbak 5.x therefore cannot connect to
#: it at all ("please ensure that major versions match"), and 4.x is the one
#: that works. Detected rather than assumed, so a later move to frida 17 —
#: which would mean dropping frida-ios-dump — does not silently build 4.x argv
#: for a 5.x binary.
_BAGBAK_SSH_MAJOR = 4

#: A fixed frida-ios-dump location, for tests; None means `frida_dump_home()`
#: works it out.
FRIDA_DUMP_HOME: Path | None = None

#: Where frida-ios-dump's launcher reads the device's root password from.
FRIDA_DUMP_PASSWORD_ENV = "GRAFT_FRIDA_DUMP_SSH_PASSWORD"

#: dump.py takes its SSH password as `-P <password>`, and a command line is
#: readable by every user on the Mac (`ps -axww`) for the whole dump. So it is
#: never started that way: this runs as `python -c`, takes the password out of
#: the environment (so nothing dump.py starts inherits it either), puts `-P`
#: into `sys.argv` in-process, and runs dump.py as the script it expects to be.
#: `ps` shows the exec argv — `python -c <this> dump.py -o … <bundle>` — only.
FRIDA_DUMP_LAUNCHER = (
    "import os, runpy, sys\n"
    "script = sys.argv[1]\n"
    f"password = os.environ.pop({FRIDA_DUMP_PASSWORD_ENV!r}, None)\n"
    "sys.argv = [script, *(['-P', password] if password is not None else []), *sys.argv[2:]]\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(script)))\n"
    "runpy.run_path(script, run_name='__main__')\n"
)


def frida_dump_home() -> Path:
    """Where the frida-ios-dump clone and its venv live.

    `$GRAFT_FRIDA_IOS_DUMP` for a user who cloned it elsewhere, otherwise
    `frida-ios-dump` in Graft's data directory. Asked at use rather than fixed
    at import: the data directory is only `…/graft` once the CLI has migrated
    it, which happens after this module is imported.
    """
    if FRIDA_DUMP_HOME is not None:
        return FRIDA_DUMP_HOME
    override = os.environ.get("GRAFT_FRIDA_IOS_DUMP")
    if override:
        return Path(override)
    return data_dirs.data_dir() / "frida-ios-dump"


def iproxy_install_hint() -> str:
    """How to get iproxy, which every SSH-backed dump tunnels through."""
    return install_hints.with_prerequisites(
        "brew install libimobiledevice", macports="sudo port install libimobiledevice"
    )


#: frida-ios-dump's own repository, and the last commit whose dump.py speaks
#: frida 16: its parent is the 2026-10-14 rewrite for Frida 17 (cdaec46), which
#: needs a 17.x frida-server and an `npm install`. This project pins frida 16,
#: so a plain clone of master would not run against the device.
FRIDA_IOS_DUMP_REPO = "https://github.com/AloneMonkey/frida-ios-dump"
FRIDA_IOS_DUMP_FRIDA16_COMMIT = "56e99b2138fc213fa759b3aeb9717a1fb4ec6a59"


#: The other way to set frida-ios-dump up, said beside the command, never in it.
FRIDA_IOS_DUMP_ELSEWHERE = "or set GRAFT_FRIDA_IOS_DUMP to a copy you already have"


def frida_ios_dump_setup_command() -> str:
    """The setup, as one shell command: clone, pin, venv, and what dump.py
    imports. Only the command, so a Copy button copies something that runs.

    Not `pip install -r requirements.txt`: that file lists `enum34`, which
    replaces the standard library's `enum` on any Python 3 and breaks the
    venv. dump.py imports frida, paramiko, scp and tqdm, and nothing else
    outside the standard library.
    """
    home = frida_dump_home()
    venv = home / ".venv"
    q = shlex.quote
    return (
        f"git clone {FRIDA_IOS_DUMP_REPO} {q(str(home))} && "
        f"git -C {q(str(home))} checkout {FRIDA_IOS_DUMP_FRIDA16_COMMIT} && "
        f"python3 -m venv {q(str(venv))} && "
        f"{q(str(venv / 'bin' / 'pip'))} install 'frida==16.*' paramiko scp tqdm"
    )


def frida_ios_dump_setup_hint() -> str:
    """The command and the alternative, for a person reading a hint."""
    return f"{frida_ios_dump_setup_command()} — {FRIDA_IOS_DUMP_ELSEWHERE}"


def bagbak_available() -> str | None:
    """The bagbak executable, or None — the preflight for the bagbak backend.

    On absolute PATH entries only (`child_env.which`): 4.x is handed the
    device's root password, so a `bagbak` in the current directory must not be
    the one that gets it.
    """
    return child_which("bagbak")


def bagbak_major(_cache: dict[str, int | None] = {}) -> int | None:  # noqa: B006 — the dict is the memo
    """bagbak's major version, or None if it can't be determined.

    Cached because it costs a Node startup (~200ms) and cannot change inside a
    run. Both majors answer `--version`; anything unparseable returns None and
    the caller falls back to the newest argv, on the grounds that a bagbak we
    can't identify is more likely to be newer than 4.x.
    """
    if "v" in _cache:
        return _cache["v"]
    _cache["v"] = None
    exe = bagbak_available()
    if exe:
        try:
            proc = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30,
                                  env=child_env())
            m = re.search(r"(\d+)\.\d+", (proc.stdout or "") + (proc.stderr or ""))
            if m:
                _cache["v"] = int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    return _cache["v"]


def frida_ios_dump_available() -> Path | None:
    """The frida-ios-dump venv python, or None — the preflight for that backend.

    Both the clone's `dump.py` and its venv interpreter have to exist; a clone
    without its dependencies installed would fail deep inside paramiko with a
    far worse message than "it isn't set up".
    """
    home = frida_dump_home()
    script = home / "dump.py"
    python = home / ".venv" / "bin" / "python"
    return python if script.is_file() and python.is_file() else None


_PROBE_TIMEOUT = 6


def _import_paramiko():
    """Import paramiko if it is installed, or None — with its logger muzzled.

    paramiko logs connection failures at ERROR and installs no handler, so
    Python's last-resort handler prints the exception *and a full traceback* to
    stderr. That is not a diagnostic here: this module's whole job is to turn a
    failed probe into one sentence, and a phone whose sshd has stopped was
    printing thirty lines of paramiko internals above that sentence. In the GUI
    it is worse than noise — stderr is what the error banner shows.

    Silenced before the import rather than after, so a `paramiko` that is not
    installed still leaves the logger configured, and so this is testable
    without it.
    """
    log = logging.getLogger("paramiko")
    if not any(isinstance(h, logging.NullHandler) for h in log.handlers):
        # A NullHandler is enough: `callHandlers` falls back to `lastResort`
        # only when it finds *no* handler in the chain. An application that
        # deliberately configures logging still gets the records.
        log.addHandler(logging.NullHandler())
    try:
        import paramiko
    except ImportError:
        return None
    return paramiko

def _free_port() -> int:
    """A local port nothing is listening on.

    Bind-then-release: the kernel hands out a port that is free *now*, which is
    the best available answer — there is no way to hold it and also give it to
    iproxy. The window is narrow and the caller checks afterwards that iproxy
    actually got it, so losing the race is caught rather than believed.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]

def _wait_for_port(port: int) -> bool:
    """Wait for iproxy to bind, rather than sleeping a guessed interval."""
    import time

    deadline = time.monotonic() + _PROBE_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.1)
    return False

def _with_tunnel(udid: str | None, work):
    """Run `work(port)` over a short-lived iproxy tunnel to the device's SSH.

    Its own tunnel per call rather than a shared one: this runs a handful of
    times at most, and a tunnel that outlives its caller is a background process
    nobody owns. `-u` pins the phone, which matters with two on the wire — an
    unpinned tunnel would happily answer for the wrong one.
    """
    iproxy = shutil.which("iproxy")
    if iproxy is None:
        return None
    # An ephemeral port, not the dump's fixed 2222.
    #
    # Sharing that port made this probe answer for the wrong phone: if anything
    # already held it — a dump in flight, a leaked tunnel, the user's own
    # iproxy — the new one exited with EADDRINUSE (stderr is DEVNULL, so
    # silently) while `_wait_for_port` happily connected to the *other*
    # listener. The card could then say "authenticated as root" about a stock
    # handset because the jailbroken one answered, which is the most damaging
    # direction available. Binding our own port also stops a probe tearing down
    # the tunnel a live dump is using.
    port = _free_port()
    args = [iproxy, f"{port}:22"] + (["-u", udid] if udid else [])
    tunnel = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=child_env())
    try:
        if not _wait_for_port(port):
            return None
        # It bound *and* it is still ours: an iproxy that died on bind would
        # leave `_wait_for_port` answering from whatever took the port instead.
        if tunnel.poll() is not None:
            return None
        return work(port)
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=3)
        except subprocess.TimeoutExpired:
            tunnel.kill()


class DumpInProgressError(DecryptError):
    """Another dump already owns this device."""


@contextlib.contextmanager
def device_dump_lock(lock_path: Path, udid: str | None):
    """Hold `lock_path` for the length of a dump, or refuse to start.

    One dump per device, because the device enforces it whether we do or not: a
    dump launches its app to the *foreground*, and a phone has one foreground.
    Two at once was tested — the second app took the screen, the first dump hung
    waiting for an attach that could no longer work, and the second fell through
    to bagbak and timed out. Two failures and three minutes, from two commands
    that each looked reasonable.

    `flock`, so the lock dies with the process that holds it. A stale lock file
    after a crash or a kill would be worse than no lock: nothing would run until
    someone found the file.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("w")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise DumpInProgressError(
                f"another dump is already running for {udid or 'this device'}",
                hint="a dump puts its app on screen, and the phone has one screen — "
                "so they cannot overlap. Wait for the first to finish, or stop it.",
            ) from None
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        handle.close()


def preferred_backend(usable: list[str] | None = None) -> str | None:
    """Which backend to open a dump with, given the ones that can run.

    One function, because two of them drifted. The readiness card recommended
    bagbak whenever nothing was on screen, while the CLI opened with
    frida-ios-dump regardless — so the card described a dump the tool was not
    going to perform.

    frida-ios-dump wins whenever it is usable. It attaches to the live process,
    which is the only reliable way to decrypt a main binary, and "the app has to
    already be on screen" stopped being a caveat once the dump learned to launch
    it (`ensure_frontmost`). What it costs is app extensions, which it can never
    read; those are dropped and reported, and bagbak remains available for the
    user who would rather try to keep them.
    """
    have = usable if usable is not None else available_backends()
    if FRIDA in have:
        return FRIDA
    if BAGBAK in have:
        return BAGBAK
    return None


def available_backends() -> list[str]:
    """Which decrypt backends are actually usable on this host."""
    out = []
    if bagbak_available():
        out.append(BAGBAK)
    if frida_ios_dump_available():
        out.append(FRIDA)
    return out


def build_command(
    bundle_id: str, output_dir: Path, *, udid: str | None, mode: str = "all",
    major: int | None = None,
) -> list[str]:
    """The bagbak argv, for whichever major is installed.

    4.x has no `mode` positional at all — it dumps the full app bundle to an
    .ipa by default, which is what 5.x calls `all`. Passing "all" to it would
    be read as a second bundle id. `major` is injected for the tests; None
    means ask the installed binary.

    `bagbak -D <udid> -o <dir> <bundle-id> [mode]`. `-D` pins the device rather
    than letting `-U` guess, which matters here because two phones are on the
    wire and only one is the jailbroken dump target.

    `-o` is a *directory*, and bagbak names the .ipa inside it. Passing a
    filename tripped an internal extension assertion in `main` mode; a directory
    is the form the tool documents and it sidesteps that.

    `mode` defaults to `all`, which is the only mode that produces a full app
    bundle (`Payload/App.app/…`) — the thing the build pipeline injects into.
    `main` writes a bare `binaries.zip` with no `Payload`, useful for reverse
    engineering but not for a build.

    `all` is also the fragile one, and unavoidably so: it dumps every app
    extension, and extensions are short-lived processes iOS suspends and reaps
    the moment the app backgrounds during the dump — bagbak loses the pid
    mid-attach and fails the whole run. It is hit-or-miss per app: an app with
    no extensions, or robust ones, dumps cleanly; one with a resistant
    extension (a share or widget) may need several tries or not dump at all.
    That is a bagbak/frida limitation, surfaced honestly rather than hidden.
    """
    cmd = ["bagbak"]
    if udid:
        cmd += ["-D", udid]
    cmd += ["-o", str(output_dir), bundle_id]
    if (major if major is not None else bagbak_major()) != _BAGBAK_SSH_MAJOR:
        cmd.append(mode)
    return cmd


def mode_is_supported(major: int | None = None) -> bool:
    """Whether the installed bagbak has a `mode` at all.

    4.x does not: it dumps the whole bundle and has no positional for this. So
    `--decrypt-mode main` is inert there — and 4.x is the major that can talk to
    a 16.x frida-server, which makes "inert" the normal case. The caller says so
    rather than printing "dumping with bagbak (main)" and then dumping
    everything, which is minutes and hundreds of megabytes the user did not ask
    for with nothing anywhere admitting the flag was dropped.
    """
    return (major if major is not None else bagbak_major()) != _BAGBAK_SSH_MAJOR


def bagbak_env(udid: str | None, major: int | None = None) -> dict[str, str]:
    """Extra environment for a bagbak run: SSH credentials, on the majors that use them.

    4.x pulls the bundle over SSH (`ssh2`) and reads `SSH_USERNAME` /
    `SSH_PASSWORD`, defaulting to **mobile:alpine** — note the user, which is
    not the `root` frida-ios-dump defaults to. It insists on both or neither,
    so they are always set together.

    5.x dumps over Frida's own transport and ignores these entirely; setting
    them there would be harmless but misleading, so it doesn't.

    The password comes from the same per-device Keychain entry frida-ios-dump
    uses, so a phone whose root password was changed is set up once for both
    backends rather than once each.
    """
    if (major if major is not None else bagbak_major()) != _BAGBAK_SSH_MAJOR:
        return {}
    return {
        "SSH_USERNAME": os.environ.get("GRAFT_SSH_USER", "root"),
        "SSH_PASSWORD": resolve_ssh_password(udid),
    }


# bagbak prints progress to stderr; these two lines are the ones worth turning
# into a real error rather than a wall of frida output.
_NO_FRIDA_RE = re.compile(r"unable to connect|failed to spawn|need frida|ECONNREFUSED", re.I)
# The failure that made bagbak look permanently broken: bagbak 5.x bundles
# frida 17, the device runs a 16.x frida-server (which this project pins to,
# because frida-ios-dump's agent uses pre-17 APIs), and the two majors cannot
# speak to each other. Distinct from "no frida-server" — the jailbreak is
# running fine — so "open Dopamine" would be wrong advice, and the raw form is
# a Node uncaught-exception dump.
_FRIDA_VERSION_RE = re.compile(
    r"ensure that major versions match|unable to communicate with remote frida", re.I
)
_NOT_INSTALLED_RE = re.compile(r"(app|application|bundle).{0,20}not found|no such app", re.I)
# bagbak has to *launch* the app to read its decrypted pages, and iOS refuses
# to launch anything while the device is locked. Seen for real on the first
# dump: the message is an FBS launch error, not anything about Frida.
_LOCKED_RE = re.compile(r"could not be,? unlocked|device.{0,20}locked|passcode", re.I)
# An extension process died before bagbak could finish attaching — the flaky
# case the user knows well. "Unable to find process with pid N", usually after
# bagbak's own "Retry 2/3 for …extension".
_EXT_FLAKY_RE = re.compile(r"find process with pid|retry \d+/\d+ for .*extension", re.I)
# bagbak's catch-all, and it hides the most common cause. bagbak has to *launch*
# the app; iOS refuses to launch anything while the device is locked, and
# bagbak reports only "Timeout was reached" — the FBS error naming the lock is
# swallowed, so `_LOCKED_RE` never sees it. Confirmed by spawning the same app
# through frida directly, which says it outright: "Unable to launch <app>
# because the device was not, or could not be, unlocked."
#
# Listed after the specific patterns so a real extension or frida failure still
# wins; this only speaks when nothing else did.
# A timeout that arrives *after* the bundle downloaded, which the locked-phone
# hint below cannot explain: measured 2026-08-18 on com.example.game,
# `--decrypt-check` printed `OK App on screen: Flow Free` and `✓ ready`, and
# bagbak logged "downloaded 550 files and 61 folders" / "app bundle downloaded"
# before hanging. A locked phone never gets that far.
#
# **This matches bagbak's boilerplate, not a diagnosis.** Its text opens "For
# timeout error, there is an edge case that some apps..." — advice printed on
# every timeout, not a finding about this app. An earlier version of this branch
# reported it as fact ("<app> has an app extension bagbak cannot dump") and was
# immediately contradicted: a second, unrelated app produced the identical
# paragraph. So this branch may only say what is certain — the download finished
# and the dump then stalled — and list both causes without picking one.
_EXT_MIN_OS_RE = re.compile(
    r"minimum os version requirement|can ?not run on your device", re.I
)
_TIMEOUT_RE = re.compile(r"timeout was reached|timed? ?out", re.I)


def decrypt_app(
    bundle_id: str,
    output_dir: Path,
    *,
    udid: str | None,
    backend: str = BAGBAK,
    mode: str = "all",
    dry_run: bool = False,
    on_line=None,
) -> DecryptResult:
    """Dump `bundle_id` into `output_dir` with the chosen backend.

    Returns the .ipa the backend wrote. The caller (`_do_decrypt`) verifies it
    is cryptid 0 before staging — a backend's job is to produce a file, not to
    promise it decrypted.
    """
    if backend == FRIDA:
        _refuse_if_frida_dump_would_target_another_device(udid)
        return _dump_frida_ios_dump(
            bundle_id, output_dir, udid=udid, dry_run=dry_run, on_line=on_line
        )
    return _dump_bagbak(
        bundle_id, output_dir, udid=udid, mode=mode, dry_run=dry_run, on_line=on_line
    )


def _refuse_if_frida_dump_would_target_another_device(udid: str | None) -> None:
    """Refuse when frida-ios-dump would dump a different phone than asked.

    **`dump.py` has no device parameter.** It takes `devices[0]` from frida's
    USB list — no argv, env var or config steers it — so `--device` is silently
    ignored by this backend alone. One phone attached and that is invisible;
    two and it dumps whichever sorts first.

    Measured 2026-08-18 with an iPad and an iPhone attached: frida's order was
    `[0] iPad, [1] iPhone`, so every dump aimed at the iPhone went to the iPad,
    found no such app, fell through to `device.spawn`, and reported only
    `connection closed`. The jailbreak, SSH auth, both frida versions and the
    app being frontmost were each verified healthy first — none of them was it.
    Unplugging the iPad fixed it on the first try.

    Only fires when the requested udid **is** in the USB list and is not first.
    An unknown udid is left alone deliberately: it may be a network device, or
    a fixture, and neither is this function's business — guessing there is what
    made an earlier version of this fail twelve unit tests that never touch a
    phone.

    Refuses rather than patching the vendored clone, which is third-party and
    re-fetched. bagbak honours `--device` and stays available.
    """
    if not udid:
        return
    try:
        frida = _import_frida("choose the device for frida-ios-dump")
        usb = [d for d in frida.enumerate_devices() if d.type == "usb"]
    except Exception:
        return  # Reaching frida at all is the dump's problem, not this guard's.
    ids = [d.id for d in usb]
    if udid not in ids or ids[0] == udid:
        return
    first = usb[0]
    raise DecryptError(
        f"frida-ios-dump would dump {first.name}, not the device you asked for",
        hint=(
            f"it always takes frida's first USB device ({first.name}, {first.id}) "
            f"and has no way to be pointed at {udid} — unplug the other device, or "
            "use bagbak, which honours --device"
        ),
    )

def _run_streaming(
    cmd: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
    on_line=None,
) -> tuple[int, str]:
    """Run `cmd`, handing each output line to `on_line` as it arrives.

    Streamed rather than captured because these dumps take a minute or more and
    the interesting question while one runs is "is it progressing or stuck" —
    which `capture_output=True` can only answer after it has already finished.
    bagbak in particular prints its per-file download progress, so a live view
    is the difference between a hung dump and a slow one.

    stderr is folded into stdout: both tools narrate on stderr and report
    results on stdout, interleaved, and reordering them would make the
    transcript harder to read than either stream alone.
    """
    proc = subprocess.Popen(
        # A caller that hands over credentials builds them into `child_env`
        # itself; with none, the child still gets the scrubbed environment.
        cmd, cwd=cwd, env=child_env() if env is None else env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        # `errors="replace"`, because strict decoding turns one stray byte into
        # a crash. Device-side filenames are not guaranteed UTF-8 and bagbak
        # echoes them per file, so a single odd character in an app's bundle
        # would raise UnicodeDecodeError mid-dump — losing the dump *and*,
        # before the `finally` below, leaving bagbak running.
        text=True, errors="replace", bufsize=1,
    )
    collected: list[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            collected.append(line)
            if on_line:
                on_line(line.rstrip("\n"))
        proc.wait()
    finally:
        # Anything escaping the loop — a decode error, or `on_line` raising
        # because the GUI closed the pipe it writes events to — must not leave
        # the backend running. A detached bagbak keeps a frida session open on
        # the app it spawned, so the *next* dump fails with a timeout that
        # names nothing and the user has to find and kill it by hand.
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    return proc.returncode, "".join(collected)


def _dump_bagbak(
    bundle_id: str,
    output_dir: Path,
    *,
    udid: str | None,
    mode: str = "all",
    dry_run: bool = False,
    on_line=None,
) -> DecryptResult:
    """Dump via bagbak — spawns the app and dumps it (4.x pulls over SSH, 5.x over Frida).

    Best-effort diagnosis of the failures worth naming — no frida-server, an app
    that is not installed, a locked device — because bagbak's own output for
    those is a Node stack trace, and "unlock the phone" is more use than one.
    """
    if bagbak_available() is None:
        raise DecryptError(
            "bagbak not found in PATH",
            hint=f"install it with: {BAGBAK_INSTALL} (needs Node)",
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = build_command(bundle_id, output_dir, udid=udid, mode=mode)
    if dry_run:
        return DecryptResult(ipa_path=output_dir, bundle_id=bundle_id, stdout=" ".join(cmd))

    try:
        returncode, combined = _run_streaming(
            cmd, env=child_env(bagbak_env(udid)), on_line=on_line,
            # `cwd` is not incidental. bagbak 4.x makes a `.bagbak` scratch
            # directory *in its current working directory*, so where it runs
            # decides whether it can run at all:
            #
            #   from a shell   the CWD is the user's project, and bagbak
            #                  quietly litters a `.bagbak/` into it
            #   from the GUI   a Dock-launched app inherits `/` as its CWD,
            #                  which is not writable — so every dump died on
            #                  `ENOENT: mkdir '.bagbak'` before touching the
            #                  device, and only from the app
            #
            # Pointing it at the output directory — a temp dir the caller
            # already made and deletes — fixes both: nothing to clean up, and
            # nowhere unwritable to land.
            cwd=str(output_dir),
        )
    except FileNotFoundError as e:
        raise DecryptError("bagbak not found in PATH") from e

    # `.ipa` and `.zip`: bagbak's `all` mode writes a `.ipa`, but `main` mode
    # names its archive `<bundle>-<version>-main.zip`. Both are zip containers;
    # whether one is a usable app bundle is the caller's `read_info_plist` to
    # decide, not this glob's.
    produced = sorted(output_dir.glob("*.ipa")) + sorted(output_dir.glob("*.zip"))
    if returncode != 0 or not produced:
        if _LOCKED_RE.search(combined):
            raise DecryptError(
                f"the device is locked, so {bundle_id} could not be launched to dump it",
                hint="unlock the phone (and keep it awake), then retry",
            )
        if _FRIDA_VERSION_RE.search(combined):
            installed = bagbak_major()
            raise DecryptError(
                f"bagbak's frida cannot talk to the frida-server on the device "
                f"(bagbak {installed or '?'}.x)",
                hint="bagbak 5.x bundles frida 17; this project pins frida 16 because "
                "frida-ios-dump's agent needs pre-17 APIs, so the device runs a 16.x "
                f"frida-server and the majors don't match. Install the matching bagbak: "
                f"{BAGBAK_INSTALL}",
            )
        if _NO_FRIDA_RE.search(combined):
            raise DecryptError(
                f"couldn't reach frida-server on the device to dump {bundle_id}",
                hint="open the Dopamine app to (re)start the jailbreak, then retry",
            )
        if _EXT_FLAKY_RE.search(combined):
            raise DecryptError(
                f"an app extension of {bundle_id} could not be dumped",
                hint="iOS reaped the extension mid-dump — keep the phone unlocked and "
                "awake and retry; some apps' extensions resist bagbak and won't dump",
            )
        if _NOT_INSTALLED_RE.search(combined):
            raise DecryptError(
                f"{bundle_id} is not installed on the device",
                hint="install it from the App Store first, then dump it",
            )
        # Before the generic timeout, because this *is* a timeout and the
        # generic hint sends people to unlock a phone that is already unlocked.
        if _EXT_MIN_OS_RE.search(combined):
            raise DecryptError(
                f"bagbak timed out dumping {bundle_id} after downloading the bundle",
                hint="two known causes, and bagbak cannot tell them apart: an app "
                "extension declaring a minimum iOS newer than this phone (bagbak "
                "prints that possibility on every timeout, so it is not evidence "
                "about this app), or a phone that locked or slept mid-dump. Keep it "
                "awake and retry; if it repeats, try frida-ios-dump, which does not "
                "touch extensions",
            )
        if _TIMEOUT_RE.search(combined):
            raise DecryptError(
                f"bagbak timed out dumping {bundle_id}",
                hint="most often this is a locked phone: bagbak has to launch the app "
                "and iOS won't launch anything while the device is locked, but bagbak "
                "reports only a timeout. Unlock it, keep it awake, and retry. Failing "
                "that, an extension that can't run on this iOS version will also hang "
                "the dump — check the app's extensions against the device's version.",
            )
        tail = combined.strip().splitlines()[-8:]
        raise DecryptError(
            f"bagbak failed to dump {bundle_id} (exit {returncode})",
            hint="\n".join(tail) if tail else None,
        )

    return DecryptResult(ipa_path=produced[0], bundle_id=bundle_id, stdout=combined)


#: The stock jailbreak root password. Only a last resort — a device whose
#: password was changed (which is the right thing to do) needs a real one.
DEFAULT_SSH_PASSWORD = "alpine"


def resolve_ssh_password(udid: str | None) -> str:
    """The root SSH password for `udid`: env, then Keychain, then the default.

    Env first so a one-off run can override without touching stored state, the
    same precedence `signer.resolve_p12_password` uses for the .p12.

    The Keychain step is what makes this usable from the GUI at all: a
    Dock-launched app inherits the launchd environment, which has no
    `GRAFT_SSH_PASS` and no way to acquire one — so an env-only credential meant
    decryption worked from a shell and silently failed from the app.

    Keyed by UDID because the password belongs to a *device*. Falling back to
    the shared default when nothing is stored keeps a stock jailbreak working
    with no setup, and `_dump_frida_ios_dump` turns the resulting auth failure
    into a hint naming this.
    """
    env = os.environ.get("GRAFT_SSH_PASS")
    if env:
        return env
    if udid:
        from . import keychain

        stored = keychain.read_ssh_password(udid)
        if stored:
            return stored
    return DEFAULT_SSH_PASSWORD


# frida-ios-dump talks to the device over SSH on this forwarded local port.
# 2222 is its own default; iproxy binds it to the device's port 22.
_FRIDA_SSH_NO_ROUTE_RE = re.compile(
    r"connection refused|no route to host|timed out|refused", re.I
)
# Split out from "can't reach it", because the two need opposite advice: one
# says install OpenSSH, the other says the SSH server answered fine and the
# password is wrong. Lumped together, a device with a changed root password —
# the common, correct case — was told to install software it already had.
# Deliberately *not* matching a bare "permission denied": scp reports an
# unreadable path on the device that way ("scp: /var/containers/…/Foo: Permission
# denied"), which is a rootless-jailbreak path problem, not a rejected login.
# Matching it sent the user to reset a password that was already correct and
# threw away the line naming the file — the only clue there was.
_FRIDA_SSH_AUTH_RE = re.compile(
    r"authentication failed|permission denied \(publickey|auth.{0,10}fail|bad password",
    re.I,
)


def _import_frida(need: str):
    """Import frida, or raise a DecryptError naming what needed it.

    The `import frida` / ImportError→DecryptError block is identical for every
    frida caller bar the noun, so it lives here — one copy of the reinstall hint.
    """
    try:
        import frida
    except ImportError as e:
        raise DecryptError(
            f"the frida module is needed to {need}",
            hint="reinstall graft so its `frida` dependency is present",
        ) from e
    return frida


def _frida_device(frida, udid: str | None):
    """Select the target device — the named one, or the sole USB device.

    Beside `_import_frida` so a change to the connect (the 10s timeout, a new
    selection case) lands once. Called *inside* each caller's own try, because
    the callers disagree on what a connection failure means: `app_is_running`
    reads it as "not running", `frontmost_app` as an error worth a hint.
    """
    return frida.get_device(udid, timeout=10) if udid else frida.get_usb_device(timeout=10)


def app_is_running(bundle_id: str, udid: str | None) -> bool:
    """Whether `bundle_id` has a live process on the device.

    frida-ios-dump attaches to a running app and only *spawns* as a fallback —
    and a spawn is the very thing that leaves the main binary encrypted, since
    the app hasn't executed enough for the kernel to decrypt its pages. So this
    is a real precondition, not a nicety: the whole reason to use this backend
    is to attach to an app that is already running.
    """
    frida = _import_frida("check the app is running")
    try:
        device = _frida_device(frida, udid)
        for app in device.enumerate_applications():
            if app.identifier == bundle_id and app.pid:
                return True
    except Exception:
        return False
    return False


#: How long to wait for a launched app to reach the foreground, and how long to
#: let it settle once there. The launch itself takes about a second on an
#: iPhone 13; the settle is for frida-ios-dump, which decrypts the modules the
#: process has actually *loaded*, and an app one frame old has loaded fewer of
#: its frameworks than one that has drawn its first screen.
_FOREGROUND_TIMEOUT = 15.0
_FOREGROUND_SETTLE = 2.5


#: Why an app is not on screen. Four answers, because they need four different
#: responses and collapsing them produced the wrong one: "unlock the phone" was
#: said about Ticketmaster, which launches and kills itself within a second on a
#: jailbroken device, with the phone wide awake the whole time.
LAUNCH_OK = "ok"
LAUNCH_REFUSED = "refused"        # the launch itself did not take
LAUNCH_DIED = "died"              # it started, then exited on its own
LAUNCH_SLOW = "never-fronted"     # alive, but never came to the front
LAUNCH_DIED_ONCE = "died-once"    # died under frida; the second launcher was unreachable


def ensure_frontmost(bundle_id: str, udid: str | None, *, on_line=None) -> str:
    """Put `bundle_id` on screen, launching it if it is not, and say what happened.

    This is what makes a dump one action instead of two. Attaching to a live,
    foreground app is the only reliable way to get a decrypted main binary — but
    it used to mean the user had to pick up the phone, open the app, and come
    back, because a dump of a backgrounded app fails: iOS *freezes* it, so it
    keeps its pid and passes a "is it running" check while the attach times out.

    Two ways to launch it, tried in that order:

      frida spawn+resume   a real launch, about a second, and needs nothing but
                           the frida connection the dump already has.
      uiopen over SSH      the device's own launcher, with no frida present at
                           startup. An app that inspects its process for a
                           debugger *as it boots* sees a clean one this way.

    Already-frontmost apps are left strictly alone — respawning one would kill
    and relaunch it, throwing away whatever state made the user open it.

    Returns a `LAUNCH_*` reason rather than raising, because the caller phrases
    the error and these four need four different sentences.
    """
    if frontmost_app(udid) == bundle_id:
        return LAUNCH_OK

    frida = _import_frida("launch the app to dump it")
    if on_line:
        on_line(f"launching {bundle_id} on the device")
    try:
        device = _frida_device(frida, udid)
        pid = device.spawn([bundle_id])
        device.resume(pid)
    except Exception:
        # A locked phone refuses the launch, as does an app that is not
        # installed. Both are the caller's to describe.
        return LAUNCH_REFUSED

    outcome = _wait_for_foreground(bundle_id, udid, pid=pid)
    if outcome != LAUNCH_DIED:
        return outcome

    # It killed itself under a spawn. Worth one clean launch — some apps only
    # look for a debugger while they boot, and `uiopen` gives them a boot with
    # no frida in it.
    if on_line:
        on_line(f"{bundle_id} exited on launch — retrying with the device's own launcher")
    if not _launch_via_uiopen(bundle_id, udid):
        # The second launcher was never reached — no SSH, no credential, no
        # `uiopen`. Saying LAUNCH_DIED here claimed the app "quit twice, once
        # from the device's own launcher" about an attempt that never happened,
        # and ended the ladder on that word, so bagbak — which launches over
        # frida's transport and needs no device SSH — was never tried.
        return LAUNCH_DIED_ONCE
    return _wait_for_foreground(bundle_id, udid)


def _wait_for_foreground(bundle_id: str, udid: str | None, *, pid: int | None = None) -> str:
    """Watch for `bundle_id` to reach the front, and notice if it dies instead.

    Liveness is polled as well as the frontmost app, because "this app refuses to
    run" and "this app is slow to start" look identical from a timeout and want
    opposite advice.

    By pid when we spawned it, by bundle id when the device's own launcher did —
    which is not a detail: passing no pid there meant the second launch could not
    be seen to die, so Ticketmaster, which quits within a second every time, was
    reported as "still running, just slow".

    A missing process is only a death after two consecutive misses. One is what a
    launch that has not registered yet looks like.
    """
    misses = 0
    deadline = time.time() + _FOREGROUND_TIMEOUT
    while time.time() < deadline:
        time.sleep(0.5)
        # `frontmost_app` *raises* when frida is unreachable, and this polls it
        # thirty times over a phone that was just told to spawn an app — so one
        # blip (a Wi-Fi device leaving usbmux, frida-server busy over the spawn)
        # aborted the dump with "open the Dopamine app to (re)start the
        # jailbreak" about a healthy jailbreak. The liveness probes beside this
        # already swallow the same exception; a poll is not the place to decide
        # the device is gone, and if it really is, the dump says so properly.
        try:
            on_screen = frontmost_app(udid)
        except DecryptError:
            on_screen = None
        if on_screen == bundle_id:
            time.sleep(_FOREGROUND_SETTLE)
            return LAUNCH_OK
        running = (
            _pid_alive(pid, udid) if pid is not None
            else _app_pid(bundle_id, udid) is not None
        )
        misses = 0 if running else misses + 1
        if misses >= 2:
            return LAUNCH_DIED
    return LAUNCH_SLOW


def _app_pid(bundle_id: str, udid: str | None) -> int | None:
    """The live pid of `bundle_id`, or None if it is not running."""
    try:
        frida = _import_frida("check the app is still running")
        device = _frida_device(frida, udid)
        for app in device.enumerate_applications():
            if app.identifier == bundle_id:
                return app.pid or None
    except Exception:
        return None
    return None


def _pid_alive(pid: int, udid: str | None) -> bool:
    try:
        frida = _import_frida("check the app is still running")
        device = _frida_device(frida, udid)
        return any(p.pid == pid for p in device.enumerate_processes())
    except Exception:
        # Can't tell; assume it is alive rather than report a death we did not
        # observe.
        return True


def _launch_via_uiopen(bundle_id: str, udid: str | None) -> bool:
    """Launch through the device's own launcher, over SSH. False if we can't.

    `uiopen` ships with the jailbreak (`/var/jb/usr/bin/uiopen`) and is what a
    tap on the icon amounts to — no frida, no injection, nothing for a startup
    check to find. Verified on this device: it foregrounds an app in about a
    second.
    """
    # The same credentials the dump itself uses, in the same order — a key when
    # `GRAFT_SSH_KEY` names one, a password otherwise. Sending only the password
    # meant a key-configured phone (whose stored password is the "alpine"
    # default, so the `if not password` guard never fired) failed at auth, and
    # the caller then reported the app as having died a second time.
    key_path = os.environ.get("GRAFT_SSH_KEY")
    password = None if key_path else resolve_ssh_password(udid)
    user = os.environ.get("GRAFT_SSH_USER", "root")

    def work(port: int) -> bool:
        paramiko = _import_paramiko()
        if paramiko is None:
            return False
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                "127.0.0.1", port=port, username=user,
                password=password, key_filename=key_path,
                timeout=_PROBE_TIMEOUT, allow_agent=False,
                look_for_keys=bool(key_path),
            )
            # `shlex.quote` because a bundle id is device data, not ours.
            client.exec_command(f"uiopen --bundleid {shlex.quote(bundle_id)}",
                                timeout=_PROBE_TIMEOUT)[1].read()
            return True
        except Exception:
            return False
        finally:
            client.close()

    return bool(_with_tunnel(udid, work))


def frontmost_app(udid: str | None) -> str | None:
    """Bundle id of the app on screen right now, or None if there is none.

    The precondition `app_is_running` checks is "has a live pid" — but iOS
    *freezes* a backgrounded app without killing it, so a suspended app keeps
    its pid and passes that check while a frida attach against it times out.
    The frontmost app is the one guaranteed foreground-active, so targeting it
    is what makes the attach reliable. It also drops the bundle id the user
    would otherwise type: open the app, dump what's frontmost.

    Returns None for the home or lock screen (no frontmost app) — a normal
    state the caller turns into "open an app first", distinct from the device
    being unreachable, which is an error worth its own hint.
    """
    frida = _import_frida("read the foreground app")
    try:
        device = _frida_device(frida, udid)
        app = device.get_frontmost_application()
    except Exception as e:
        raise DecryptError(
            "couldn't reach frida-server on the device to read the foreground app",
            hint="open the Dopamine app to (re)start the jailbreak, then retry",
        ) from e
    return app.identifier if app else None


def _launch_failure(bundle_id: str, outcome: str) -> tuple[str, str]:
    """The message and hint for a launch that did not end on screen.

    Three failures that look identical from a timeout and want opposite advice.
    Saying "unlock the phone" for all of them was wrong about the interesting
    one: Ticketmaster exits within a second of launching on a jailbroken device,
    with the phone awake the whole time, and being told to unlock it sends the
    user hunting for a lock that is not there.
    """
    if outcome == LAUNCH_DIED_ONCE:
        return (
            f"{bundle_id} started and then exited on its own",
            "the app quit within a second of launching under frida. The device's "
            "own launcher would have been the second opinion, but it could not be "
            "reached — check SSH on the phone (`--decrypt-check`). Apps that refuse "
            "to run on a jailbroken device quit exactly like this.",
        )
    if outcome == LAUNCH_DIED:
        return (
            f"{bundle_id} started and then exited on its own",
            "the app quit within a second of launching, twice — once under frida "
            "and once from the device's own launcher. Apps that refuse to run on a "
            "jailbroken device do exactly this, and there is no dump of an app that "
            "will not run. Check whether it opens normally by tapping its icon.",
        )
    if outcome == LAUNCH_SLOW:
        return (
            f"{bundle_id} launched but never reached the foreground",
            "it is still running, so this is not a crash — something is in front of "
            "it (a system alert, an incoming call) or it is unusually slow to start. "
            "Clear the screen and retry.",
        )
    return (
        f"{bundle_id} could not be launched on the device",
        "unlock the phone and keep it awake — iOS will not launch an app while the "
        "device is locked. If it is unlocked, check the app is still installed.",
    )


def _dump_frida_ios_dump(
    bundle_id: str, output_dir: Path, *, udid: str | None, dry_run: bool = False,
    on_line=None,
) -> DecryptResult:
    """Dump via frida-ios-dump — attaches to the *running* app, pulls over SSH.

    Reliable where bagbak isn't for the main binary, at the cost of two things
    the device must have: OpenSSH (from Sileo) and the target app already open.
    This owns the iproxy tunnel for the dump's lifetime — bind a free local port
    to the device's port 22, run, tear down — so the user never manages it.
    """
    python = frida_ios_dump_available()
    if python is None:
        raise DecryptError(
            "frida-ios-dump is not set up",
            hint=frida_ios_dump_setup_hint(),
        )
    iproxy = shutil.which("iproxy")
    if iproxy is None:
        raise DecryptError(
            "iproxy not found (needed to reach the device over SSH)",
            hint=iproxy_install_hint(),
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    out_ipa = output_dir / f"{bundle_id}.ipa"
    script = frida_dump_home() / "dump.py"
    # SSH creds, in the order a caller can override them. A key (GRAFT_SSH_KEY)
    # takes precedence over a password, since a device whose root password was
    # changed — which is common and good — is otherwise unreachable. These are
    # the device's own SSH login, not secrets of ours to hardcode.
    ssh_user = os.environ.get("GRAFT_SSH_USER", "root")
    ssh_key = os.environ.get("GRAFT_SSH_KEY")
    # A key is a path and goes on argv; a password never does (see
    # `FRIDA_DUMP_LAUNCHER`), so it is handed over in the child's environment.
    ssh_env = {} if ssh_key else {FRIDA_DUMP_PASSWORD_ENV: resolve_ssh_password(udid)}

    def build_cmd(port: int) -> list[str]:
        argv = [
            str(python), "-c", FRIDA_DUMP_LAUNCHER, str(script),
            "-o", str(out_ipa),
            "-H", "localhost", "-p", str(port),
            "-u", ssh_user,
        ]
        if ssh_key:
            argv += ["-K", ssh_key]
        return [*argv, bundle_id]

    if dry_run:
        # The launcher's source is noise in a rendered command; what it does
        # with the password is the part worth showing.
        shown = [a for a in build_cmd(_free_port()) if a not in ("-c", FRIDA_DUMP_LAUNCHER)]
        if not ssh_key:
            shown.insert(-1, f"-P <from ${FRIDA_DUMP_PASSWORD_ENV}, set in-process>")
        return DecryptResult(ipa_path=out_ipa, bundle_id=bundle_id, stdout=" ".join(shown))

    outcome = ensure_frontmost(bundle_id, udid, on_line=on_line)
    if outcome != LAUNCH_OK:
        message, hint = _launch_failure(bundle_id, outcome)
        # `LAUNCH_SLOW` stays an ordinary failure: the app *is* running, and the
        # other backend may yet get it on screen. The other two are about the
        # app or the phone, and no backend changes either.
        # What ends the ladder is a launch the other backend cannot improve on.
        # A death seen through *both* launchers is one, and so is a refused
        # launch — bagbak spawns over frida's transport too, so a locked phone
        # or a missing app stops it at exactly the same wall. A slow app is
        # running, and one that died with the second launcher merely unreachable
        # has not been shown to refuse anything; both stay ordinary failures.
        cls = (
            DecryptError if outcome in (LAUNCH_SLOW, LAUNCH_DIED_ONCE)
            else AppWillNotRunError
        )
        raise cls(message, hint=hint)

    # The port is chosen *here*, not before the launch. `_free_port` only proves
    # a port was free at the moment it asked, and `ensure_frontmost` can spend
    # eighteen seconds between the two — during which its own uiopen tunnel, or
    # another dump, can take the number this one is holding on paper.
    port = _free_port()
    cmd = build_cmd(port)
    tunnel_cmd = [iproxy, f"{port}:22"]
    if udid:
        tunnel_cmd += ["-u", udid]
    tunnel = subprocess.Popen(tunnel_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=child_env())
    try:
        # Confirmed rather than slept through. A `time.sleep(1.5)` cannot tell a
        # bound tunnel from an iproxy that exited on EADDRINUSE — silently, its
        # stderr being DEVNULL — after which dump.py talks to whatever else holds
        # the port and the run dies advising the user to install an OpenSSH they
        # already have. Same check `_with_tunnel` makes for every probe.
        if not _wait_for_port(port) or tunnel.poll() is not None:
            raise DecryptError(
                "could not open an SSH tunnel to the device",
                hint="iproxy could not bind a local port for the dump — check that "
                "no other dump is running and that usbmuxd is up "
                "(`brew services list`, or replug the phone)",
            )
        returncode, combined = _run_streaming(
            cmd, cwd=str(frida_dump_home()), env=child_env(ssh_env), on_line=on_line
        )
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=5)
        except subprocess.TimeoutExpired:
            tunnel.kill()

    produced = sorted(output_dir.glob("*.ipa"))
    if returncode != 0 or not produced:
        if _FRIDA_SSH_AUTH_RE.search(combined):
            raise DecryptError(
                f"the device rejected the SSH login while dumping {bundle_id}",
                hint="store the phone's root password with `graft "
                "--set-ssh-password` (it is kept in the Keychain, per device, so "
                "the GUI can use it too), or set GRAFT_SSH_KEY to a key file",
            )
        if _FRIDA_SSH_NO_ROUTE_RE.search(combined):
            raise DecryptError(
                f"couldn't reach the device over SSH to dump {bundle_id}",
                hint="install OpenSSH on the phone from Sileo — this backend needs it",
            )
        tail = combined.strip().splitlines()[-8:]
        raise DecryptError(
            f"frida-ios-dump failed to dump {bundle_id} (exit {returncode})",
            hint="\n".join(tail) if tail else None,
        )

    return DecryptResult(ipa_path=produced[0], bundle_id=bundle_id, stdout=combined)
