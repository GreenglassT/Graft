"""Sign in to the App Store by typing into ipatool's own prompts, under a pty.

ipatool 2.8.0 reads the password only from a terminal. Its `cmd/prompt.go`
switches stdin to raw mode (`term.MakeRaw`) for a masked read *before* it
prompts, and that fails on a pipe. The other way it takes a password,
`--password`, puts it on argv, where `ps` shows it to every account on the
Mac. So ipatool gets a pseudo-terminal as its stdin and its stderr, which is
where it prompts, and this module watches for each prompt and types the
answer. Its stdout stays a pipe. Under `--format json` that is where every
log line and the verdict go, so the prompts and the result never mix.

The conversation, from ipatool 2.8.0's `cmd/auth.go`, as Task 0's spikes saw
it on this Mac (S1.1):

1. `enter Apple ID (email address or phone number): ` (a line, canonical
   mode): the Apple ID, then a newline. ipatool asks for it only when `-e` is
   absent, and Graft never passes `-e`, so the Apple ID is never on argv or in
   `ps` either (controller decision 3).
2. `enter password: ` (masked, raw mode): the password, then a carriage return.
3. A log line, "preparing authentication; the first login may take a few
   minutes". The first sign-in on a Mac then downloads ipatool's App Store
   components (about 320 MB) and its Unicorn emulator (from PyPI), and can
   sit silent for many minutes; `waiting` events cover the silence. With both
   cached it is about 20–32 s (S1.3).
4. **Maybe** `enter 2FA code: ` (a line, canonical mode). Apple asks only
   when it doesn't already trust the device; the spikes saw no code asked even
   on a fresh sign-in (decision 8), and a code asked after a *wrong password*
   that Apple never sent (S1.4). That is `needs-code`: the caller reads the
   code (the app sends it on stdin) and it is typed with a newline. ipatool
   asks once, so a wrong code ends the run.
5. The verdict on stdout: `{"success": true, "email": …, "name": …}` or
   `{"success": false, "error": …}`.

A second Apple ID or password prompt, or one for a keyring passphrase (its
file backend, which macOS never uses), ends the run rather than leave it
hanging.

ECHO is cleared on the pty before ipatool starts, so the Apple ID and the
code, typed in canonical mode, are never written back. The masked password
read echoes `*`. Nothing read from the pty is kept beyond the last `_WINDOW`
bytes needed to spot a prompt, and that window is zeroed when the run ends.

The secrets are `secret_input.SecretLine`s. This module types them from a
`memoryview`, never copying them, and wipes the code it is handed; the Apple
ID and the password are the caller's to wipe.

ipatool is started through `ipatool.run_ipatool`, the one route every ipatool
run takes (tests/test_ipatool_env.py).
"""

from __future__ import annotations

import json
import os
import pty
import re
import select
import signal
import subprocess
import termios
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import appstore_account, ipatool, keychain, secret_input
from .errors import AppStoreError, Stopped
from .json_output import JsonEmitter
from .secret_input import SecretLine, SecretTooLong, drop_spaces, is_six_digits, redact

if TYPE_CHECKING:
    from .config import Config

#: ipatool 2.8.0's prompts, byte for byte (cmd/auth.go, cmd/common.go), as
#: recorded by Task 0 (S1.1).
APPLE_ID_PROMPT = b"enter Apple ID (email address or phone number): "
PASSWORD_PROMPT = b"enter password: "
CODE_PROMPT = b"enter 2FA code: "
PASSPHRASE_PROMPT = b"enter passphrase to unlock "
#: Its log line once the password is in (cmd/auth.go).
PREPARING = "preparing authentication; the first login may take a few minutes"

#: `appstore-login`'s `state`. Pinned against `CLIContract.AppStoreLogin`.
STATE_PREPARING = "preparing"
STATE_WAITING = "waiting"
STATE_NEEDS_CODE = "needs-code"
STATE_VERIFYING = "verifying"
STATE_SIGNED_IN = "signed-in"
STATE_FAILED = "failed"

#: `appstore-login {state: "failed"}`'s `reason`.
REASON_WRONG_PASSWORD = "wrong-password"
#: Apple refused the password and the code together, and ipatool can't say
#: which (S1.4: a wrong password and a wrong code give the same text).
REASON_NOT_ACCEPTED = "not-accepted"
REASON_WRONG_CODE = "wrong-code"
REASON_LOCKED = "locked"
REASON_TIMEOUT = "timeout"
REASON_STOPPED = "stopped"
REASON_KEYCHAIN = "keychain"
REASON_NETWORK = "network"
REASON_RATE_LIMITED = "rate-limited"
#: ipatool died with a prompt unanswered: typing into its terminal failed.
REASON_IPATOOL_EXITED = "ipatool-exited"
#: Graft didn't start the sign-in: an older ipatool sign-in in the login
#: keychain couldn't be checked or removed first (decision 10).
REASON_KEYCHAIN_CLEAR = "keychain-clear"
#: ipatool signed in, and Graft couldn't record that it was Graft's own.
REASON_NOT_RECORDED = "not-recorded"
#: The ipatool that would get the password is a version known to be broken.
REASON_BAD_VERSION = "bad-version"
#: The login keychain is locked (`keychain.login_keychain_locked`): nothing
#: was read, looked up or deleted, and ipatool didn't start.
REASON_KEYCHAIN_LOCKED = "keychain-locked"
#: ipatool asked something that looks like a prompt and isn't one of 2.8.0's
#: (another version, or a fork), and waited for an answer Graft can't give.
REASON_UNKNOWN_PROMPT = "unknown-prompt"
#: `--expect-ipatool`: the ipatool that would get the password isn't the one
#: the caller (the sign-in sheet) named.
REASON_IPATOOL_CHANGED = "ipatool-changed"
#: Apple no longer serves the macOS restore image ipatool takes the App
#: Store's frameworks from, the first time (`ipatool.restore_image_failure`).
#: Its own reason, not "network": trying again can't fix it.
REASON_RESTORE_IMAGE = "restore-image-gone"
REASON_OTHER = "other"

#: What the sign-in sheet says for each, in words a person can act on.
MESSAGES: dict[str, str] = {
    REASON_WRONG_PASSWORD: "Apple didn't accept that password. Check it and sign in again.",
    REASON_NOT_ACCEPTED: "Apple didn't accept the password or the code: check both and try again.",
    REASON_WRONG_CODE: (
        "That code isn't six digits. Sign in again and type the six digits Apple shows "
        "on your other devices."
    ),
    REASON_LOCKED: (
        "This Apple ID is locked or disabled. Unlock it at iforgot.apple.com, then sign "
        "in again."
    ),
    REASON_TIMEOUT: (
        "Signing in took too long and was stopped. Check this Mac's connection and "
        "try again."
    ),
    REASON_STOPPED: "Sign-in stopped. Nothing was saved.",
    REASON_KEYCHAIN: "ipatool couldn't save the sign-in in your login keychain.",
    REASON_NETWORK: "Couldn't reach Apple. Check this Mac's connection and try again.",
    REASON_RATE_LIMITED: (
        "Apple is limiting sign-in attempts right now. Wait a few minutes and try again."
    ),
    REASON_IPATOOL_EXITED: "ipatool stopped before it finished signing in. Sign in again.",
    REASON_KEYCHAIN_CLEAR: (
        "Graft couldn't clear an older ipatool sign-in from your login keychain, so it "
        "didn't sign in. Unlock the login keychain in Keychain Access, then sign in again."
    ),
    REASON_NOT_RECORDED: (
        "You're signed in, but Graft couldn't record that the sign-in is its own, so it would "
        "take it for another ipatool's."
    ),
    REASON_BAD_VERSION: "This ipatool is a version known not to work with Graft.",
    REASON_KEYCHAIN_LOCKED: (
        "Your login keychain is locked, so Graft didn't sign in: ipatool keeps the sign-in "
        "there. Unlock it in Keychain Access, then sign in again."
    ),
    REASON_UNKNOWN_PROMPT: (
        "ipatool asked something Graft doesn't know how to answer, so Graft stopped the sign-in."
    ),
    REASON_IPATOOL_CHANGED: (
        "The ipatool that would get your password isn't the one Settings named, so Graft "
        "didn't sign in."
    ),
    REASON_RESTORE_IMAGE: ipatool.RESTORE_IMAGE_GONE,
    REASON_OTHER: "ipatool couldn't sign in.",
}

#: A Stop once the password has gone to ipatool: by then it may have saved
#: something, so not "Nothing was saved" (the 2.0 review's #18).
STOPPED_AFTER_PASSWORD = (
    "Sign-in stopped. If Apple had already accepted it, ipatool may have saved the "
    "sign-in: Settings › App Store shows whether you're signed in."
)
#: A Stop after ipatool said it had signed in, and saved it.
STOPPED_AFTER_SIGN_IN = (
    "Sign-in stopped after ipatool had signed in and saved the sign-in in your login "
    "keychain."
)

#: S1.4's text when no code was typed: then only the password can be wrong.
PASSWORD_NOT_ACCEPTED = "Apple didn't accept the password: check it and try again."

#: How often `waiting` is emitted while ipatool says nothing, and how often
#: its download folders are looked at then (`download_mark`).
HEARTBEAT_SECONDS = 10.0
#: How long ipatool may go with no sign of life: nothing written to its
#: terminal or stdout, nothing typed to it, and no growth in its download
#: folders. Generous on purpose: the first sign-in on a Mac downloads about
#: 320 MB of App Store components and Unicorn while saying nothing (1.x saw
#: about 8 minutes for that), and silence then is normal. The download
#: folders (`download_folders`) count because ipatool 2.8.0 writes what it
#: fetches as it goes, but not all of it: it reads a restore image's
#: filesystem by HTTP range requests, and reading its directory structure
#: writes nothing, so a slow line can still be quiet for minutes between
#: writes. A partial download is never resumed: ipatool starts it again from
#: nothing (`internal/sap/assets`). A sign-in is never stopped as hung in
#: under this; the sheet's Cancel is the way out sooner.
SILENCE_LIMIT_SECONDS = 20 * 60.0
#: How long to wait for the person to type the 2FA code.
CODE_WAIT_SECONDS = 10 * 60.0
#: How long ipatool may sit silent after writing what looks like a prompt
#: (`_asked`) that is none of 2.8.0's, before the run ends as
#: `REASON_UNKNOWN_PROMPT` rather than sit out `SILENCE_LIMIT_SECONDS`. A
#: prompt is one write, so its last bytes come within milliseconds of its
#: first; 2.8.0's are answered the moment they are complete, and any output
#: starts this wait again. 15 s is far past a prompt split across reads, and
#: short enough that the sheet says why at once rather than after 20 minutes.
UNKNOWN_PROMPT_SECONDS = 15.0
#: What looks like a prompt: the last line on ipatool's terminal, unfinished,
#: ending in ": " or "? ".
_PROMPT_LIKE = (b": ", b"? ")
#: Enough of the pty's output to hold the longest prompt.
_WINDOW = 512
#: How long to wait for what a dead ipatool left on stdout.
_DRAIN_SECONDS = 2.0
#: The most files `download_mark` looks at, so a cache that someone filled
#: with other things can't make the look expensive.
_MARK_FILES = 4096

ReadCode = Callable[[float], SecretLine | None]
Say = Callable[[str], None]


class LoginFailed(AppStoreError):
    """A sign-in that didn't work, with its `reason` (one of REASON_*)."""

    def __init__(self, reason: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.reason = reason


#: Patterns in ipatool's error text (lowercased), in the order they are
#: tried. The first match wins, so the keychain comes before "locked" (a
#: locked *keychain* is not a locked Apple ID), and a network failure before
#: a locked or disabled Apple ID: a network error's Go cause is free text
#: (a proxy's reason phrase, say), and "blocked" or "proxy disabled" in it
#: must not send the user to iforgot.apple.com. "locked" matches as a whole
#: word only, never inside "blocked" or "unlocked", and "disabled" only in
#: the phrases ipatool and Apple use for an Apple ID ("account is disabled",
#: "has been disabled for security reasons").
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # S1.4: what ipatool says for a wrong password *and* for a wrong code.
    (REASON_NOT_ACCEPTED, (r"did not complete verification", r"try a fresh 2fa code")),
    (REASON_WRONG_CODE, (r"2fa code must contain",)),
    (REASON_KEYCHAIN, (r"keychain", r"keyring")),
    # ipatool 2.8.0: `apple rate limited authentication`, `rate limited by
    # Apple`, and a Retry-After past its limit (`apple requested a wait
    # longer than`). Before the network rule: a rate limit after three tries
    # is also "after 3 attempts (HTTP 429, 429, 429)".
    (REASON_RATE_LIMITED, (r"rate limited", r"too many requests", r"a wait longer than")),
    (
        REASON_NETWORK,
        (r"transport error", r"after 3 attempts", r"no such host", r"network is unreachable",
         r"timed out", r"connection refused"),
    ),
    (REASON_LOCKED, (r"account is disabled", r"has been disabled", r"\blocked\b")),
    (REASON_WRONG_PASSWORD, (r"incorrect", r"invalid credentials", r"password was entered")),
)
_COMPILED_RULES = tuple(
    (reason, tuple(re.compile(p) for p in patterns)) for reason, patterns in _RULES
)

_HINTS: dict[str, str] = {
    REASON_KEYCHAIN: (
        "if macOS asked whether ipatool may use your keychain, sign in again and "
        "choose Allow"
    ),
    REASON_OTHER: "signing in again may work; `graft --appstore-status` shows the state",
}


def classify(error: str, *, code_typed: bool = True) -> LoginFailed:
    """ipatool's error text as one plain failure. A message that names only
    a category also carries ipatool's own words, so nothing is lost.

    `code_typed` is whether a 2FA code went in. ipatool's "try a fresh 2FA
    code" is also what a wrong *password* gets (S1.4), so it never blames the
    code alone, and without a code it names only the password (decision 9).
    ipatool failing to fetch its Unicorn emulator, the first time, offline,
    says what the rest of Graft says for it (`ipatool.UNICORN_FIRST_USE`).
    Apple no longer serving the restore image ipatool sets itself up from
    says that a newer Graft fixes it (`ipatool.RESTORE_IMAGE_GONE`), before
    any rule could call its HTTP status something else. No URL's query
    reaches a message (`ipatool.without_url_queries`).
    """
    error = ipatool.without_url_queries(error)
    if (first_use := ipatool.unicorn_failure(error)) is not None:
        return LoginFailed(REASON_NETWORK, first_use.message, hint=first_use.hint)
    if (gone := ipatool.restore_image_failure(error)) is not None:
        return LoginFailed(REASON_RESTORE_IMAGE, gone.message, hint=gone.hint)
    lower = error.lower()
    reason = next(
        (reason for reason, patterns in _COMPILED_RULES if any(p.search(lower) for p in patterns)),
        REASON_OTHER,
    )
    message = MESSAGES[reason]
    if reason == REASON_NOT_ACCEPTED and not code_typed:
        message = PASSWORD_NOT_ACCEPTED
    if reason in (REASON_KEYCHAIN, REASON_OTHER):
        message = f"{message} (ipatool: {error})"
    return LoginFailed(reason, message, hint=_HINTS.get(reason))


def _quiet_pty() -> tuple[int, int]:
    master, slave = pty.openpty()
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~(termios.ECHO | termios.ECHONL)
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    return master, slave


def _type(fd: int, data: memoryview | bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


class _Gone(Exception):
    """Typing into ipatool's terminal failed: it has exited (macOS answers a
    write to a pty whose other end is closed with EIO)."""

    def __init__(self, what: str) -> None:
        super().__init__(what)
        self.what = what


def _answer(fd: int, data: memoryview | bytes, what: str) -> None:
    try:
        _type(fd, data)
    except OSError as e:
        raise _Gone(what) from e


def _read(fd: int) -> bytes | None:
    """One chunk, or None at the end. A pty master reports its end as EIO
    once the last slave fd closes; any other read error is an end too: there
    is nothing more to be had from it."""
    try:
        chunk = os.read(fd, 4096)
    except OSError:
        return None
    return chunk or None


def download_folders() -> list[Path]:
    """Where ipatool 2.8.0 writes while a first sign-in downloads its parts,
    in the environment it is given (`child_env`, so this process's HOME and
    TMPDIR):

    - `~/Library/Caches/ipatool` (Go's `os.UserCacheDir`): Unicorn, streamed
      into `unicorn/<version>`, and the macOS assets once fetched, in `sap/`;
    - `$TMPDIR/ipatool-*` (`os.MkdirTemp("", "ipatool-macos-27-*")`, and Go's
      temp dir is $TMPDIR, else /tmp): the workspace the assets are extracted
      into, file by file, as the range requests come in.
    """
    folders = [Path.home() / "Library" / "Caches" / "ipatool"]
    tmp = Path(os.environ.get("TMPDIR") or "/tmp")
    try:
        with os.scandir(tmp) as entries:
            folders += [Path(e.path) for e in entries
                        if e.name.startswith("ipatool-") and e.is_dir(follow_symlinks=False)]
    except OSError:
        pass
    return folders


def download_mark(folders: list[Path] | None = None) -> tuple[int, int, int]:
    """A cheap fingerprint of `download_folders`: how many files, their total
    size and the newest change time. Any difference between two looks is
    ipatool at work. Stats only, never a read, and at most `_MARK_FILES`."""
    count = total = newest = 0
    stack = list(folders if folders is not None else download_folders())
    while stack and count < _MARK_FILES:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    newest = max(newest, info.st_mtime_ns)
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    else:
                        count += 1
                        total += info.st_size
        except OSError:
            continue
    return count, total, newest


def _json_objects(pending: bytearray) -> list[dict[str, Any]]:
    """Every complete JSON-object line in `pending`, consumed from it."""
    found: list[dict[str, Any]] = []
    while (end := pending.find(b"\n")) >= 0:
        line = bytes(pending[:end])
        del pending[: end + 1]
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            found.append(obj)
    return found


#: What a Terminal user is told at each step (the app words its own).
_PLAIN: dict[str, str] = {
    STATE_PREPARING: (
        "signing in. The first sign-in on a Mac downloads ipatool's App Store "
        "components (about 320 MB) and its Unicorn emulator, and can take several "
        "minutes; later ones take under a minute"
    ),
    STATE_NEEDS_CODE: (
        "Apple asked for a 2FA code: type the 6-digit code it shows on your iPhone, iPad "
        "or other Mac. If none appears, the password may be wrong: press Ctrl-C and sign "
        "in again"
    ),
    STATE_VERIFYING: "checking…",
}


def _report(emitter: JsonEmitter | None, say: Say, state: str, **fields: Any) -> None:
    if emitter is not None:
        emitter.emit("appstore-login", state=state, **fields)
        return
    if state == STATE_WAITING:
        say(f"still signing in ({fields.get('elapsed', 0)} s)")
    elif state == STATE_SIGNED_IN:
        say(f"signed in to the App Store as {fields.get('email')}")
    elif state in _PLAIN:
        say(_PLAIN[state])


def _end(proc: subprocess.Popen[bytes]) -> None:
    """Stop ipatool if it is still running: its whole process group, SIGTERM,
    then SIGKILL after two seconds."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    if proc.stdout is not None:
        proc.stdout.close()


def _asked(window: bytearray) -> bytes | None:
    """The unfinished last line of ipatool's terminal output when it looks
    like a prompt (it ends with ": " or "? "), else None."""
    line = bytes(window).rsplit(b"\n", 1)[-1].rsplit(b"\r", 1)[-1]
    return line if line.endswith(_PROMPT_LIKE) else None


def _unknown_prompt(path: Path, version: str | None, asked: bytes, *secrets: SecretLine | None) -> LoginFailed:
    """The `REASON_UNKNOWN_PROMPT` failure: which ipatool, which version, and
    what it asked (printable, short, and never a secret). Each secret comes
    out of the whole line, before and after the unprintable characters do,
    and only then is it cut to 80: a cut first could fall inside the Apple
    ID and leave the rest of it whole (delta audit #6)."""
    text = redact(asked.decode("utf-8", "replace"), *secrets)
    text = redact("".join(c for c in text if c.isprintable()), *secrets)[-80:]
    which = f"version {version}" if version else "a version it didn't say"
    return LoginFailed(
        REASON_UNKNOWN_PROMPT,
        f"{MESSAGES[REASON_UNKNOWN_PROMPT]} The ipatool at {path} ({which}) asked {text!r}, which "
        f"isn't one of the prompts Graft answers. Graft is tested with ipatool {ipatool.GOOD_VERSION}.",
        hint=f"sign in with ipatool {ipatool.GOOD_VERSION}: Graft.app's own, or the one appstore.ipatool "
        "names in config.yaml",
    )


def _typed_email(apple_id: SecretLine) -> str:
    return bytes(apple_id.view).decode("utf-8", "replace")


@dataclass
class Progress:
    """How far a sign-in got, for what a Stop says. Once Apple has the
    password (and the code, when it asked for one) ipatool may have saved the
    sign-in; while it waits for the code it can't have; once it says it
    signed in, it has."""

    password_typed: bool = False
    awaiting_code: bool = False
    signed_in: bool = False

    def may_have_saved(self) -> bool:
        return self.password_typed and not self.awaiting_code


def _drain(out_fd: int, pending: bytearray) -> dict[str, Any] | None:
    """The verdict a dead ipatool left on stdout, if any. A pipe keeps what
    was written to it after the writer exits (a pty does not)."""
    verdict = None
    deadline = time.monotonic() + _DRAIN_SECONDS
    while (left := deadline - time.monotonic()) > 0:
        if not select.select([out_fd], [], [], left)[0]:
            break
        chunk = _read(out_fd)
        if chunk is None:
            break
        pending += chunk
    for obj in _json_objects(pending + b"\n"):
        if "success" in obj:
            verdict = obj
    return verdict


def sign_in(
    path: Path,
    apple_id: SecretLine,
    password: SecretLine,
    read_code: ReadCode,
    *,
    emitter: JsonEmitter | None,
    say: Say,
    heartbeat: float = HEARTBEAT_SECONDS,
    silence_limit: float = SILENCE_LIMIT_SECONDS,
    code_wait: float = CODE_WAIT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    progress: Progress | None = None,
    mark: Callable[[], object] = download_mark,
    version: str | None = None,
    unknown_prompt_wait: float | None = None,
) -> str:
    """Sign ipatool in as `apple_id`; return the email ipatool reports.

    `apple_id` and `password` are typed at ipatool's prompts; wiping them is
    the caller's job. `read_code(timeout)` is called only if ipatool asks for
    a 2FA code, and returns a `SecretLine` this function wipes, or None when
    there is none (stdin closed: the app gave up). Raises `LoginFailed`, and
    on every way out, a Stop included, ipatool is stopped. `progress` is
    updated as it goes, for the caller's Stop. `mark` is the download
    folders' fingerprint (`download_mark`), looked at on each heartbeat while
    ipatool is silent: a change is a sign of life. Something that looks like
    a prompt and isn't one of 2.8.0's, followed by `unknown_prompt_wait`
    seconds of silence (`UNKNOWN_PROMPT_SECONDS`), ends the run as
    `REASON_UNKNOWN_PROMPT`, naming `version` (ipatool's own, for the message).
    """
    unknown_wait = UNKNOWN_PROMPT_SECONDS if unknown_prompt_wait is None else unknown_prompt_wait
    progress = progress if progress is not None else Progress()
    master, slave = _quiet_pty()
    try:
        proc = ipatool.run_ipatool(
            path, ["auth", "login", "--format", "json"],
            run=subprocess.Popen,
            stdin=slave, stderr=slave, stdout=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as e:
        os.close(master)
        raise LoginFailed(REASON_OTHER, f"couldn't run ipatool at {path}: {e.strerror}") from e
    finally:
        os.close(slave)

    assert proc.stdout is not None
    out_fd = proc.stdout.fileno()
    open_fds = [master, out_fd]
    window = bytearray()
    pending = bytearray()
    code: SecretLine | None = None
    verdict: dict[str, Any] | None = None
    typed_apple_id = typed_password = typed_code = False
    gone: str | None = None
    started = last_beat = clock()
    deadline = started + silence_limit
    seen = mark()
    # An unrecognized prompt: what it said, and since when ipatool has been
    # silent after it.
    asked: bytes | None = None
    asked_at = 0.0
    try:
        try:
            while open_fds:
                now = clock()
                if now >= deadline:
                    raise LoginFailed(REASON_TIMEOUT, MESSAGES[REASON_TIMEOUT])
                due = min(deadline, last_beat + heartbeat)
                if asked is not None:
                    due = min(due, asked_at + unknown_wait)
                wait = max(0.0, due - now)
                ready, _, _ = select.select(open_fds, [], [], wait)
                if not ready:
                    if asked is not None and clock() - asked_at >= unknown_wait:
                        raise _unknown_prompt(path, version, asked, apple_id, password, code)
                    if clock() - last_beat >= heartbeat:
                        last_beat = clock()
                        _report(emitter, say, STATE_WAITING, elapsed=int(last_beat - started))
                        # Silent, but downloading its components: alive.
                        if (now_seen := mark()) != seen:
                            seen = now_seen
                            deadline = clock() + silence_limit
                    continue
                # Anything ipatool writes is a sign of life: silence is what the
                # limit is for. It starts an unrecognized prompt's wait again too.
                deadline = clock() + silence_limit
                asked_at = clock()
                # stdout first: what ipatool logged before its next prompt is
                # reported before that prompt is answered ("preparing" before
                # "needs-code"), whichever the kernel says is ready.
                if out_fd in ready:
                    chunk = _read(out_fd)
                    if chunk is None:
                        open_fds.remove(out_fd)
                    else:
                        pending += chunk
                        for obj in _json_objects(pending):
                            if obj.get("message") == PREPARING:
                                _report(emitter, say, STATE_PREPARING)
                            if "success" in obj:
                                verdict = obj
                                if obj.get("success") is True:
                                    progress.signed_in = True
                if master in ready:
                    chunk = _read(master)
                    if chunk is None:
                        open_fds.remove(master)
                    else:
                        window += chunk
                        del window[:-_WINDOW]
                        if APPLE_ID_PROMPT in window:
                            window[:] = bytes(len(window))
                            window.clear()
                            if typed_apple_id:
                                raise LoginFailed(REASON_OTHER, "ipatool asked for the Apple ID twice")
                            _answer(master, apple_id.view, "the Apple ID")
                            _answer(master, b"\n", "the Apple ID")
                            typed_apple_id = True
                        elif PASSWORD_PROMPT in window:
                            window[:] = bytes(len(window))
                            window.clear()
                            if typed_password:
                                raise LoginFailed(
                                    REASON_OTHER, "ipatool asked for the password twice"
                                )
                            progress.password_typed = True
                            _answer(master, password.view, "the password")
                            _answer(master, b"\r", "the password")
                            typed_password = True
                        elif CODE_PROMPT in window:
                            window[:] = bytes(len(window))
                            window.clear()
                            if code is not None:
                                # ipatool 2.8.0 asks once. Another build asking
                                # again gets no second code: the first is wiped
                                # (the `finally`) and the run ends.
                                raise LoginFailed(REASON_OTHER, "ipatool asked for the 2FA code twice")
                            _report(emitter, say, STATE_NEEDS_CODE)
                            progress.awaiting_code = True
                            try:
                                code = read_code(code_wait)
                            except TimeoutError:
                                raise LoginFailed(
                                    REASON_TIMEOUT,
                                    f"Sign-in was stopped after {int(code_wait // 60)} minutes "
                                    "without a 2FA code. If no code appeared on your devices, "
                                    "the password may be wrong: check it and sign in again.",
                                ) from None
                            except SecretTooLong:
                                # A line longer than any code (piped stdin;
                                # a terminal never completes one): not six
                                # digits, said as a failure like any other,
                                # not a crash past `run`'s event (signin-3).
                                # `prompt_line` wiped what it had read.
                                raise LoginFailed(
                                    REASON_WRONG_CODE, MESSAGES[REASON_WRONG_CODE],
                                ) from None
                            if code is None:
                                raise LoginFailed(REASON_STOPPED, MESSAGES[REASON_STOPPED])
                            drop_spaces(code)
                            if not is_six_digits(code):
                                raise LoginFailed(
                                    REASON_WRONG_CODE,
                                    "The code is the six digits Apple shows on your other devices.",
                                )
                            _answer(master, code.view, "the 2FA code")
                            _answer(master, b"\n", "the 2FA code")
                            typed_code = True
                            progress.awaiting_code = False
                            _report(emitter, say, STATE_VERIFYING)
                            last_beat = clock()
                        elif PASSPHRASE_PROMPT in window:
                            raise LoginFailed(
                                REASON_OTHER,
                                "ipatool asked for a keyring passphrase, which Graft doesn't "
                                "answer for it",
                            )
                        # None of 2.8.0's, or one just answered (the window is
                        # empty then): does what is left look like a prompt?
                        asked = _asked(window)
                    # What was typed starts the next silence: the person may
                    # have taken minutes over the code.
                    deadline = clock() + silence_limit
        except _Gone as e:
            gone = e.what
            verdict = _drain(out_fd, pending) or verdict
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        if verdict is None:
            if gone is not None:
                raise LoginFailed(
                    REASON_IPATOOL_EXITED,
                    f"ipatool stopped before it could take {gone} (exit {proc.poll()}). "
                    "Sign in again.",
                )
            raise LoginFailed(
                REASON_OTHER,
                f"ipatool stopped without saying whether it signed in (exit {proc.poll()})",
            )
        if verdict.get("success") is True:
            progress.signed_in = True
            reported = verdict.get("email")
            return reported if isinstance(reported, str) and reported else _typed_email(apple_id)
        raise classify(
            redact(str(verdict.get("error") or "no reason given"), apple_id, password, code),
            code_typed=typed_code,
        )
    finally:
        window[:] = bytes(len(window))
        if code is not None:
            code.wipe()
        _end(proc)
        os.close(master)


# --- The verbs ----------------------------------------------------------------

#: `read_line(prompt, timeout, echo=False)`: one line from stdin as a
#: `SecretLine`, or None at end of input (`secret_input.prompt_line` in the CLI).
ReadLine = Callable[..., SecretLine | None]

#: What a Terminal user is asked. The app sends the same lines unasked.
APPLE_ID_ASK = "Apple ID (email or phone number): "
PASSWORD_ASK = "Apple ID password: "
CODE_ASK = "2FA code: "


def _plausible_apple_id(line: SecretLine) -> bool:
    """Something to type at ipatool's Apple ID prompt: no spaces, no control
    characters. An email or a phone number; Apple judges the rest."""
    return all(byte > 0x20 and byte != 0x7F for byte in line.view)


def _refuse_bad_version(path: Path, tools_cfg: Any, source: str) -> str | None:
    """A known-bad ipatool gets no password, wherever it was found: the same
    check `--appstore-claim` makes (`appstore._require_signed_in`). Before any
    secret is read. Returns the version ipatool reports."""
    found = ipatool.version(path)
    if found in ipatool.KNOWN_BAD_VERSIONS:
        raise LoginFailed(
            REASON_BAD_VERSION,
            f"ipatool {found} at {path} is known not to work with Graft, so Graft didn't sign "
            "in with it",
            hint=ipatool.bad_version_hint(found, path=path, configured=tools_cfg.ipatool, source=source),
        )
    return found


def _refuse_another_ipatool(path: Path, expected: Path) -> None:
    """`--expect-ipatool`: the ipatool found now is the one the caller named
    (both as real paths), or nothing is read and nothing started. The sign-in
    sheet names the ipatool that gets the password from a status loaded
    earlier, and config.yaml may have changed since (the fix re-check's
    problem 10)."""
    if os.path.realpath(path) == os.path.realpath(expected):
        return
    raise LoginFailed(
        REASON_IPATOOL_CHANGED,
        f"{MESSAGES[REASON_IPATOOL_CHANGED]} It would be the ipatool at {path}, and Settings named "
        f"the one at {expected}.",
        hint="open Settings › App Store again to see which ipatool signs in, then sign in",
    )


def _record(source: str) -> None:
    """`mark_signed_in`, its failures said as what they are: ipatool did sign
    in, and Graft couldn't record that the sign-in is its own (the 2.0
    review's #18). Never a raw OSError."""
    try:
        appstore_account.mark_signed_in(source)
    except appstore_account.KeychainUnanswered as e:
        raise LoginFailed(
            REASON_NOT_RECORDED,
            f"{MESSAGES[REASON_NOT_RECORDED]} Your login keychain didn't answer when Graft "
            "looked at the sign-in ipatool had just saved.",
            hint="unlock the login keychain in Keychain Access, then sign in again",
        ) from e
    except OSError as e:
        where = appstore_account.marker_path()
        raise LoginFailed(
            REASON_NOT_RECORDED,
            f"{MESSAGES[REASON_NOT_RECORDED]} It couldn't write {where} "
            f"({e.strerror or type(e).__name__}).",
            hint=f"free some disk space, or fix the permissions of {where.parent}, then sign in again",
        ) from e


def run(
    cfg: Config, *, emitter: JsonEmitter | None, say: Say, read_line: ReadLine,
    expect_ipatool: Path | None = None,
) -> str:
    """`--appstore-login`: the Apple ID and the password from stdin, ipatool
    signed in, the outcome reported. Returns the email ipatool reports.

    stdin carries, in order: the Apple ID, the password, and only after an
    `appstore-login {state: "needs-code"}` event, the 2FA code. None of them is
    ever an argument (controller decision 3). Each is read into a
    `SecretLine` and wiped in the `finally`, the code inside `sign_in`; a Stop
    wipes every one still alive (`secret_input.wipe_all`), wherever it lands.

    With `expect_ipatool` (`--expect-ipatool`), an ipatool found anywhere else
    is refused before anything is read or any ipatool started
    (`REASON_IPATOOL_CHANGED`). A known-bad ipatool is refused before
    anything is read. With Graft.app's
    bundled ipatool, ipatool's items in the login keychain that Graft's
    record doesn't vouch for were made by another ipatool (Graft 1.x's, or
    one installed by hand), which the bundled one can't read without a
    Keychain dialog. With any other ipatool it is the other way round: the
    items the record vouches for are Graft.app's own ipatool's (the
    whole-diff review's signin-1). Either way they are deleted first, by
    their exact names, and if that can't be done the sign-in doesn't start
    (`appstore_account.needs_clearing`; decisions 2 and 10).

    Every failure is first an `appstore-login {state: "failed"}` event and
    then the raise, so the exit status and stderr are what they always are.
    The event carries the failure's `hint` (null when it has none), which
    Terminal prints after the message and the sheet shows under it. A
    Stop (SIGTERM, or Ctrl-C) is reported the same way and still exits as a
    stop; what it says depends on how far ipatool got (`Progress`).
    """
    apple_id: SecretLine | None = None
    password: SecretLine | None = None
    progress = Progress()
    source = ""
    try:
        tools_cfg = cfg.appstore
        explicit = "ipatool" in tools_cfg.explicit
        path = ipatool.locate(tools_cfg.ipatool, explicit=explicit)
        if expect_ipatool is not None:
            _refuse_another_ipatool(path, expect_ipatool)
        if not ipatool.present(path):
            raise AppStoreError(f"ipatool not found at {path}", hint=ipatool.install_hint(path))
        source = ipatool.source_of(tools_cfg.ipatool, explicit=explicit)
        found_version = _refuse_bad_version(path, tools_cfg, source)
        if keychain.login_keychain_locked():
            # Before stdin, and before an older sign-in is looked up or
            # deleted: in a locked keychain each would ask for its password.
            raise LoginFailed(
                REASON_KEYCHAIN_LOCKED, MESSAGES[REASON_KEYCHAIN_LOCKED], hint=ipatool.KEYCHAIN_LOCKED_HINT,
            )
        try:
            apple_id = read_line(APPLE_ID_ASK, None, echo=True)
            if apple_id is None or not len(apple_id):
                raise LoginFailed(
                    REASON_OTHER, "no Apple ID was given: send it as the first line on stdin"
                )
            if not _plausible_apple_id(apple_id):
                raise LoginFailed(
                    REASON_OTHER, "an Apple ID has no spaces or control characters in it"
                )
            password = read_line(PASSWORD_ASK, None)
        except SecretTooLong as e:
            raise LoginFailed(
                REASON_OTHER, "a line on stdin is longer than any Apple ID or password"
            ) from e
        if password is None or not len(password):
            raise LoginFailed(
                REASON_OTHER,
                "no password was given: send it on stdin as the line after the Apple ID",
            )
        try:
            appstore_account.clear_items(appstore_account.needs_clearing(source))
        except (appstore_account.KeychainUnanswered, appstore_account.ClearFailed) as e:
            # The sentence that says what to do, not `security`'s outcome
            # alone, which never did (signin-4).
            raise LoginFailed(REASON_KEYCHAIN_CLEAR, MESSAGES[REASON_KEYCHAIN_CLEAR], hint=e.hint) from e
        signed_in_as = sign_in(
            path, apple_id, password, lambda timeout: read_line(CODE_ASK, timeout),
            emitter=emitter, say=say, progress=progress, version=found_version,
        )
        _record(source)
    except LoginFailed as e:
        _report(emitter, say, STATE_FAILED, reason=e.reason, message=e.message, hint=e.hint)
        raise
    except AppStoreError as e:
        _report(emitter, say, STATE_FAILED, reason=REASON_OTHER, message=e.message, hint=e.hint)
        raise
    except (Stopped, KeyboardInterrupt):
        # First, wherever the Stop landed: a line read and not yet handed back.
        secret_input.wipe_all()
        message = MESSAGES[REASON_STOPPED]
        if progress.signed_in:
            message = STOPPED_AFTER_SIGN_IN
            try:
                appstore_account.mark_signed_in(source)
            except (OSError, AppStoreError):
                pass
        elif progress.may_have_saved():
            message = STOPPED_AFTER_PASSWORD
        _report(emitter, say, STATE_FAILED, reason=REASON_STOPPED, message=message, hint=None)
        raise
    finally:
        for line in (apple_id, password):
            if line is not None:
                line.wipe()
    _report(emitter, say, STATE_SIGNED_IN, email=signed_in_as)
    return signed_in_as


def sign_out(cfg: Config, *, emitter: JsonEmitter | None, say: Say) -> None:
    """`--appstore-logout`: `ipatool auth revoke`, then Graft's record of the
    items revoke removed is dropped (`appstore_account.signed_out`). Not with
    the login keychain locked: revoke deletes from it, which would ask."""
    if keychain.login_keychain_locked():
        raise AppStoreError(
            "your login keychain is locked, so Graft didn't sign out: ipatool's sign-in is there",
            hint=ipatool.KEYCHAIN_LOCKED_HINT,
        )
    tools_cfg = cfg.appstore
    ipatool.revoke(ipatool.locate(tools_cfg.ipatool, explicit="ipatool" in tools_cfg.explicit))
    appstore_account.signed_out()
    if emitter is not None:
        emitter.emit("appstore-logout", signed_out=True)
    else:
        say("signed out of the App Store")
