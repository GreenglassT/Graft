"""macOS Keychain helpers for the secrets the pipeline needs.

Two of them, both read from (in order):
    1. An environment variable
    2. The login keychain under service=graft

    .p12 password   $<signing.cert_password_env>   account=p12-<team_id>
    device SSH      $GRAFT_SSH_PASS                 account=ssh-<udid>

Storing them in the keychain lets the SwiftUI GUI invoke the CLI without
inheriting a shell-only env var — a Dock-launched app gets the launchd
environment, which has neither. Writing uses `-A` so subsequent reads don't
trigger a per-build Keychain Access prompt.

The SSH entry is keyed by **UDID**, not globally: the jailbroken phone that
dumps and any other device on the bench are different machines with different
root passwords, and a single shared entry would send the wrong one.

Before the rename the service was `tweak-build`. A read that misses under
`graft` looks there too, and copies what it finds across, so passwords stored
before upgrading keep working without being entered again. The old item is
left in place — the pre-rename app may still be installed and reading it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .child_env import child_env
from .errors import GraftError

SERVICE = "graft"

#: The service name before the rename. Only ever read, never written.
LEGACY_SERVICE = "tweak-build"

#: `security`'s exit status for errSecItemNotFound — the one failure that
#: means "there is no such item" rather than "you may not have it".
_ITEM_NOT_FOUND = 44

#: The longest secret `_write` takes, in bytes of its escaped form (`_escape`:
#: UTF-8, with every `"` and `\\` doubled up). `security -i` reads its stdin
#: into a 4096-byte buffer and ends the command after 4095 bytes, running what
#: follows as another command (measured; the 2.0 review's SEC-1). A password
#: chosen by whoever handed out a .p12 could otherwise pick that command.
MAX_SECRET_BYTES = 1024

#: The longest line `_write` sends, newline included: under the 4095 bytes
#: `security -i` reads, with room to spare. With `MAX_SECRET_BYTES` and the
#: account checks no line comes near it; this is the floor under them, so a
#: future caller with a longer account can't reopen the split.
MAX_LINE_BYTES = 3800

#: What the setup and `--set-p12-password` say for a .p12 password over
#: `MAX_SECRET_BYTES`. Never the password, nor any part of it.
P12_PASSWORD_TOO_LONG = (
    f"a .p12 password longer than {MAX_SECRET_BYTES} bytes isn't supported (in UTF-8, so a "
    "character outside ASCII counts 2 to 4, and a \\ or \" counts 2); export the "
    "certificate again with a shorter one."
)

#: How long any `security` call may take. A locked or out-of-sync login
#: keychain shows a SecurityAgent dialog and `security` waits on it, which
#: the app, with no Terminal, would otherwise wait on for ever.
TIMEOUT_SECONDS = 60

NO_ANSWER = "The Keychain didn't answer; unlock it in Keychain Access and try again."


class KeychainNoAnswer(GraftError):
    """`security` ran past `TIMEOUT_SECONDS`: a prompt nobody answered, most often."""

    def __init__(self) -> None:
        super().__init__(NO_ANSWER)


def _run_security(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run` for `security`, unless `GRAFT_NO_KEYCHAIN=1`.

    The switch exists for the test suite: its in-process fake replaces `_run`,
    but a CLI it spawns as a subprocess imports this module afresh and would
    reach the real login keychain. With the switch set every lookup is a
    not-found and every write fails, and `security` is never run.
    """
    if os.environ.get("GRAFT_NO_KEYCHAIN") == "1":
        empty: Any = "" if kwargs.get("text") else b""
        if argv[1:2] == ["find-generic-password"]:
            return subprocess.CompletedProcess(argv, _ITEM_NOT_FOUND, empty, empty)
        message = "GRAFT_NO_KEYCHAIN=1: the keychain is switched off\n"
        return subprocess.CompletedProcess(
            argv, 1, empty, message if kwargs.get("text") else message.encode()
        )
    # Always `child_env()`, and written where the package-wide scan can see
    # it (tests/test_child_env.py): no caller has a reason to hand `security`
    # another environment.
    kwargs.pop("env", None)
    # Never the CLI's own stdin, where `--appstore-login`'s Apple ID and
    # password wait unread (signin-2): only what Graft writes to `security
    # -i` (`input=`), else /dev/null.
    if "input" not in kwargs:
        kwargs.setdefault("stdin", subprocess.DEVNULL)
    # Never a blocked signal mask, which a child inherits: run from the
    # paid setup's undo (`account_setup._signals_held`), `security` held
    # SIGTERM, SIGHUP and SIGINT too, and a Stop couldn't end it
    # (the whole-diff review's cli-4). Only then, so every other run keeps
    # subprocess's fast path.
    if signal.pthread_sigmask(signal.SIG_BLOCK, ()):
        kwargs.setdefault("preexec_fn", _unblock_signals)
    return subprocess.run(argv, **kwargs, env=child_env())


def _unblock_signals() -> None:
    """In `security`'s child, between fork and exec: no signal blocked."""
    signal.pthread_sigmask(signal.SIG_SETMASK, ())


#: How `security` is run. A seam so the test suite can swap in a fake and
#: never read — or, via the copy above, write — a real keychain.
_run = _run_security


def _call(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    """`_run` with `TIMEOUT_SECONDS`: every `security` call goes through here.

    `subprocess.run` kills the child when the time is up; the caller gets
    `KeychainNoAnswer`, whose words say what to do about it.
    """
    try:
        return _run(argv, **kwargs, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise KeychainNoAnswer() from None


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _parse_password_line(stderr: str) -> str | None:
    """The secret in `find-generic-password -g`'s stderr.

    Not `-w`: that prints any secret holding a non-ASCII character, a control
    character or a backslash as bare hex, indistinguishable from a password
    that *is* hex — and the legacy copy would then store the hex as the
    password for good. `-g` says which form it used. Measured on a throwaway
    keychain, its `password:` line is one of:

        password: "<raw>"                 printable ASCII, unescaped — the raw
                                          text may itself contain `"`
        password: 0x<HEX>  "<octal>"      anything else; the hex is exact
        password: 0x<HEX>                 the same with no printable byte at all:
                                          the hex, one space, no rendering
        password:                         empty

    An empty password is `""`: a .p12 exported with a blank password is
    stored that way, and its builds must find it. Anything else, or hex that
    is not UTF-8, is unreadable, and None — never a reason to fall back to the
    legacy item, because the caller decides that from the exit status alone.
    """
    line = next((x for x in stderr.splitlines() if x.startswith("password:")), None)
    if line is None:
        return None
    rest = line[len("password:"):].removeprefix(" ")
    if rest == "":
        return ""
    if rest.startswith("0x"):
        digits = rest[2:].split(" ", 1)[0]
        if not digits or not set(digits) <= _HEX_DIGITS or len(digits) % 2:
            return None
        try:
            return bytes.fromhex(digits).decode("utf-8")
        except UnicodeDecodeError:
            return None
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        return rest[1:-1]
    return None


def _find(service: str, account: str) -> tuple[bool, str | None]:
    """`(missing, secret)` for `service`/`account`.

    `missing` is True only when `security` says there is no such item. Any
    other failure — a denied prompt, a locked keychain, no `security` binary,
    an answer that cannot be read — is `(False, None)`: nothing to use, and
    no grounds to look elsewhere. The callers all fall back to an env var or
    a default, so none of these is an error worth raising from a lookup.
    """
    try:
        proc = _call(
            ["security", "find-generic-password", "-s", service, "-a", account, "-g"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False, None
    if proc.returncode != 0:
        return proc.returncode == _ITEM_NOT_FOUND, None
    return False, _parse_password_line((proc.stderr or b"").decode("utf-8", "replace"))


@dataclass(frozen=True)
class Lookup:
    """What `find_item` found.

    `exists` is True, False, or None when `security` couldn't say (no
    binary, a locked keychain, an answer nobody gave in time, the test
    switch's refusal). `created` is the item's creation date (`cdat`) as
    `security` prints it, when it exists and the answer carried one.
    `no_answer` is the timeout among the Nones: `security` waited
    `TIMEOUT_SECONDS` on a dialog, and asking again would wait as long.
    """

    exists: bool | None
    created: str | None = None
    no_answer: bool = False
    #: The item's account, from `first_account` only.
    account: str | None = None


#: `find-generic-password`'s creation-date line, without `-g`: on stdout,
#: as a 16-byte blob ending in a NUL, so hex first and then quoted
#: (SecurityTool's `print_keychain_item_attributes`):
#:     "cdat"<timedate>=0x32303236313132323134323030305A00  "20261122142000Z\000"
_CDAT_LINE = re.compile(r'^\s*"cdat"<timedate>=(?P<value>.*?)\s*$', re.M)
_CDAT_QUOTED = re.compile(r'"(?P<date>\d{14}Z)(?:\\000)?"')


def _created(stdout: str) -> str | None:
    """The creation date in `find-generic-password`'s attributes, or None.

    The quoted date when it is there; else the value as printed, which is
    still the same for the same item, so a comparison stays sound even if a
    later `security` words it differently. Never a value that says nothing
    (`<NULL>`, empty).
    """
    match = _CDAT_LINE.search(stdout)
    if match is None:
        return None
    value = match.group("value")
    quoted = _CDAT_QUOTED.search(value)
    if quoted is not None:
        return quoted.group("date")
    if not value or value == "<NULL>":
        return None
    return value


def find_item(service: str, account: str) -> Lookup:
    """Whether a generic-password item exists, and when it was made, never
    reading its secret.

    No `-g` and no `-w`, so `security` prints attributes only, and macOS
    raises no access dialog for an item another program made (Task 0, K0).
    """
    try:
        proc = _call(
            ["security", "find-generic-password", "-s", service, "-a", account],
            capture_output=True, check=False,
        )
    except FileNotFoundError:
        return Lookup(None)
    except KeychainNoAnswer:
        return Lookup(None, no_answer=True)
    if proc.returncode == 0:
        out = proc.stdout or b""
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return Lookup(True, _created(text))
    if proc.returncode == _ITEM_NOT_FOUND:
        return Lookup(False)
    return Lookup(None)


# --- Is the login keychain locked? ------------------------------------------
#
# A lookup in a locked login keychain can answer (attributes, no -g) or ask
# for its password; ipatool reading an item asks "ipatool wants to use the
# login keychain", and after a Cancel each of Graft's own lookups asked again
# (the owner's try12 round: six dialogs from one Refresh). So the lock is read
# first, from the Security framework, which never shows UI for it.

_SECURITY_FRAMEWORK = "/System/Library/Frameworks/Security.framework/Security"
_CORE_FOUNDATION = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
#: `SecKeychainStatus`'s kSecUnlockStateStatus bit.
_UNLOCKED = 1


def login_keychain_path() -> Path:
    """The account's login keychain, by its home in the password database,
    never $HOME: a test's temporary HOME, or an odd one, must not hide it
    (the owner's try13 run: SecKeychainCopyDefault goes through $HOME)."""
    import pwd

    return Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library" / "Keychains" / "login.keychain-db"


class _KeychainAPI:
    """The Security framework calls the lock check makes, each returning
    (OSStatus, value). Only `_default_keychain_locked` makes one; tests
    hand `_keychain_locked` a fake instead."""

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        security = ctypes.CDLL(_SECURITY_FRAMEWORK)
        foundation = ctypes.CDLL(_CORE_FOUNDATION)
        self._open = security.SecKeychainOpen
        self._open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
        self._open.restype = ctypes.c_int32
        self._copy_default = security.SecKeychainCopyDefault
        self._copy_default.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self._copy_default.restype = ctypes.c_int32
        self._get_status = security.SecKeychainGetStatus
        self._get_status.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        self._get_status.restype = ctypes.c_int32
        self._release = foundation.CFRelease
        self._release.argtypes = [ctypes.c_void_p]
        self._release.restype = None

    def open(self, path: Path) -> tuple[int, int | None]:
        ref = self._ctypes.c_void_p()
        return self._open(os.fsencode(path), self._ctypes.byref(ref)), ref.value

    def copy_default(self) -> tuple[int, int | None]:
        ref = self._ctypes.c_void_p()
        return self._copy_default(self._ctypes.byref(ref)), ref.value

    def get_status(self, ref: int) -> tuple[int, int]:
        status = self._ctypes.c_uint32(0)
        return self._get_status(self._ctypes.c_void_p(ref), self._ctypes.byref(status)), status.value

    def release(self, ref: int) -> None:
        self._release(self._ctypes.c_void_p(ref))


def _ref_locked(api: Any, found: int, ref: int | None) -> bool | None:
    """`SecKeychainGetStatus` on a reference `SecKeychainOpen` or
    `SecKeychainCopyDefault` answered with, then released. Locked unless its
    kSecUnlockStateStatus bit is set (a status of 2, readable alone, is
    locked). None when either call failed: it can't tell."""
    if found != 0 or not ref:
        return None
    try:
        status, state = api.get_status(ref)
        return None if status != 0 else not state & _UNLOCKED
    finally:
        api.release(ref)


def _keychain_locked(api: Any, login: Path) -> bool | None:
    """Whether a keychain ipatool and `security` would use is locked.

    Two are read, and neither read unlocks anything or asks anything:
    - **The default keychain** (`SecKeychainCopyDefault`), where ipatool
      adds its items: it opens its keyring with no keychain named. Found
      through $HOME, as `security` and ipatool find it, since both inherit
      this process's HOME (`child_env`).
    - **The login keychain at `login`** (`SecKeychainOpen` on the file),
      found by the account's home and never $HOME. A lookup with no
      keychain named searches the whole search list, the login keychain
      included, so a locked one there can ask too.

    Usually they are the same keychain. They differ when someone has made
    another keychain the default (Keychain Access › Make Keychain Default):
    reading only the login keychain then said "unlocked" over a locked
    default, and ipatool asked (the try14 delta audit's kc-path). Under a
    test's temporary HOME the default is "none" (-25307), and the login
    keychain's answer stands alone.

    True when either is locked, False when neither is and at least one
    could be read, and None when neither could: it can't tell.
    """
    answers = [_ref_locked(api, *api.copy_default())]
    if login.is_file():
        answers.append(_ref_locked(api, *api.open(login)))
    if True in answers:
        return True
    return False if False in answers else None


def _default_keychain_locked() -> bool | None:
    """Whether the default keychain or the login keychain (where ipatool's
    items are) is locked, from the Security framework (`_keychain_locked`),
    which shows no UI for it. Deprecated calls, but the only ones that read
    the lock without trying it. None when it can't tell."""
    try:
        api = _KeychainAPI()
    except (OSError, AttributeError):
        return None
    return _keychain_locked(api, login_keychain_path())


#: The lock check. Tests replace it (conftest's `_no_real_keychain`), so no
#: test asks the Security framework about the real login keychain.
_lock_probe: Any = _default_keychain_locked


def login_keychain_locked() -> bool:
    """True only when the login keychain, or a default keychain that isn't
    it, is known to be locked (`_keychain_locked`): then nothing asks the
    Keychain anything (status, a Get or claim, a sign-in or sign-out). False
    when it is unlocked, or the check can't tell, and the lookups' own
    answers decide. With `GRAFT_NO_KEYCHAIN=1` the real check never runs."""
    probe = _lock_probe
    if probe is _default_keychain_locked and os.environ.get("GRAFT_NO_KEYCHAIN") == "1":
        return False
    try:
        return probe() is True
    except Exception:
        return False


#: `find-generic-password`'s account line, when the account is plain text:
#:     "acct"<blob>="p12-ABCDE12345"
#: An account with a byte outside printable ASCII is printed as hex first,
#: and doesn't match: Graft's own accounts never have one.
_ACCT_LINE = re.compile(r'^\s*"acct"<blob>="(?P<account>[^"\n]*)"\s*$', re.M)


def first_account(service: str) -> Lookup:
    """The first item under `service`, with its account, never reading its
    secret.

    `find-generic-password -s <service>` with no `-a`: `security` answers
    with the first item it finds under that service, attributes only, as
    `find_item` asks (no `-g`, no `-w`, so no access dialog). This is how
    `remove_data` finds Graft's own items it has no record of, one at a
    time: it deletes each by service and account (`delete_item`) and asks
    again. `account` is None when there is no item, when `security`
    couldn't say, or when the account isn't printed as plain text.
    """
    try:
        proc = _call(
            ["security", "find-generic-password", "-s", service],
            capture_output=True, check=False,
        )
    except FileNotFoundError:
        return Lookup(None)
    except KeychainNoAnswer:
        return Lookup(None, no_answer=True)
    if proc.returncode == _ITEM_NOT_FOUND:
        return Lookup(False)
    if proc.returncode != 0:
        return Lookup(None)
    out = proc.stdout or b""
    text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
    match = _ACCT_LINE.search(text)
    return Lookup(True, _created(text), account=match.group("account") if match else None)


def item_exists(service: str, account: str) -> bool | None:
    """`find_item(...).exists`: None when `security` can't say."""
    return find_item(service, account).exists


def delete_item(service: str, account: str) -> bool | None:
    """Delete one generic-password item, by its exact service and account.

    True when one was deleted, False when there was none, and None when
    `security` didn't delete it for another reason (a locked keychain, a
    refusal, no answer in time, no binary): the item may well still be
    there. Deleting another program's item raises no dialog (Task 0, K2b).
    """
    try:
        proc = _call(
            ["security", "delete-generic-password", "-s", service, "-a", account],
            capture_output=True, check=False,
        )
    except (FileNotFoundError, KeychainNoAnswer):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == _ITEM_NOT_FOUND:
        return False
    return None


def _read(account: str) -> str | None:
    """The secret stored under `account`, migrating it from the old service.

    The old service is consulted only when the new one has *no such item*.
    A denied prompt or a locked keychain is not that: falling back then would
    read the stale pre-rename password and write it over the newer one the
    user could not unlock this time.

    A failed copy still returns the secret: it was read fine, the caller is
    usually a build that needs it now, and the next read tries the copy again.
    """
    missing, secret = _find(SERVICE, account)
    if not missing:
        return secret
    _, legacy = _find(LEGACY_SERVICE, account)
    if legacy is None:
        return None
    try:
        _write(account, legacy)
    except (subprocess.CalledProcessError, OSError):
        pass
    return legacy


def _escape(value: str) -> str:
    """`value` with `\\` and `"` backslash-escaped: what goes between `_quote`'s quotes."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _quote(value: str) -> str:
    """`value` as one argument to `security -i`: double-quoted, with `\\` and
    `"` backslash-escaped. Measured against real `security -i` on a
    throwaway keychain for spaces, both quote kinds, backslashes (trailing
    too), `$`, backticks, a leading `-`, UTF-8 and the empty string by
    `tests/test_keychain_real.py`, which is opt-in (GRAFT_REAL_KEYCHAIN_TESTS=1,
    with the owner watching) and not part of an ordinary suite run."""
    return '"' + _escape(value) + '"'


def secret_fits(secret: str) -> bool:
    """Whether `secret` is short enough for `_write` (`MAX_SECRET_BYTES`)."""
    return len(_escape(secret).encode("utf-8")) <= MAX_SECRET_BYTES


def _refuse(argv: list[str], why: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, argv, b"", why.encode())


#: How much of a secret, at least, `_redacted` treats as a piece of it.
_PIECE = 4


def _redacted(said: bytes | None, secret: str) -> bytes:
    """What `security` said, with every line that echoes the secret, or a
    piece of it (`_PIECE` characters or more), withheld.

    `security -i` answers a command it can't run with `unknown command
    "<the command>"` and `<the command>: returned N`, and the error that
    carries those words is shown in the app's banner and pasted into bug
    reports. With `MAX_SECRET_BYTES` no command is split so as to echo the
    password, but the error text must never depend on that alone.
    """
    if not said:
        return b""
    text = said.decode("utf-8", "replace")
    size = min(_PIECE, len(secret))
    pieces = {secret[i:i + size] for i in range(len(secret) - size + 1)} if size else set()
    kept = [
        "security: (a line that repeated part of the secret, withheld)"
        if any(piece in line for piece in pieces) else line
        for line in text.splitlines()
    ]
    return ("\n".join(kept) + ("\n" if text.endswith("\n") else "")).encode()


def _exists(account: str, keychain_path: str | None) -> bool:
    """Whether `SERVICE`/`account` is there: its attributes only, never its
    secret, so no access prompt. Any answer but found or not-found is raised
    as `_write`'s failure."""
    argv = ["security", "find-generic-password", "-s", SERVICE, "-a", account]
    if keychain_path is not None:
        argv.append(keychain_path)
    proc = _call(argv, capture_output=True, check=False)
    if proc.returncode in (0, _ITEM_NOT_FOUND):
        return proc.returncode == 0
    raise subprocess.CalledProcessError(proc.returncode, argv, b"", proc.stderr or b"")


def _write(account: str, secret: str, *, keychain_path: str | None = None) -> None:
    """Upsert `secret` under `account` in the login keychain.

    Through `security -i` on stdin, not `add-generic-password -w <secret>` on
    the command line: argv is world-readable (`ps -axww` shows it to every
    user on the Mac), and this runs on ordinary reads too, for the legacy
    copy.

    `-i` reads one command per line, into a 4096-byte buffer, so two things
    end a command early and run the rest as another: a line break or a NUL,
    refused in every value; and length, after 4095 bytes. A secret is capped
    at `MAX_SECRET_BYTES` escaped, and the line that is built is checked
    against `MAX_LINE_BYTES` before `security` runs. Each refusal names what
    was wrong, never the value.

    Failure is a non-zero exit *or* anything on stderr: `-i` reports a failed
    command and carries on, so its exit status alone is not to be trusted.
    Raised as `CalledProcessError` with `security`'s own words in `stderr`
    (`_redacted`: never the secret), which is what the `--set-*-password`
    callers report. No answer within `TIMEOUT_SECONDS` is `KeychainNoAnswer`.

    `-A` lets any app read the item without a Keychain prompt — the GUI runs
    the CLI from a Dock-launched process on every build, and zsign reads it.
    Every item is created with it, so the legacy copy carries the same access
    as the item it copies. An item that is already there keeps the access it
    has (`-U` without `-A`): changing it would ask for the keychain password.

    `keychain_path` names the keychain, as `add-generic-password`'s last
    argument. Only the test that round-trips through a throwaway keychain
    passes it; everything else writes to the default (login) keychain.
    """
    argv = ["security", "-i"]
    # The account and the service too, not only the secret: the account is
    # built from a team id or a UDID, and a team id can come off a
    # downloaded profile. `_quote` makes a quote or a backslash inert inside
    # one argument; a line break, a NUL or length can still end the command.
    values = [("password", secret), ("account", account), ("service", SERVICE)]
    if keychain_path is not None:
        values.append(("keychain", keychain_path))
    for what, value in values:
        if any(c in value for c in "\r\n\0"):
            raise _refuse(argv, f"a Keychain {what} cannot contain a line break or a NUL")
    if not secret_fits(secret):
        raise _refuse(argv, f"a Keychain secret longer than {MAX_SECRET_BYTES} bytes isn't supported")
    # `-U -A` on an item that is already there makes `security` change its
    # access list (`SecKeychainItemSetAccess`), which macOS asks the user to
    # confirm with the keychain's password, and which, refused, fails the
    # command after the password has already been replaced (measured on a
    # throwaway keychain). So an item that exists is updated with `-U` alone,
    # which keeps the access list it was created with; a new one is created
    # with `-A` alone, which never changes an existing item.
    where = f" {_quote(keychain_path)}" if keychain_path is not None else ""

    def line(flag: str) -> bytes:
        return (
            f"add-generic-password -s {_quote(SERVICE)} -a {_quote(account)} "
            f"-w {_quote(secret)} {flag}{where}\n"
        ).encode()

    # Measured before anything runs, the existence check included; `-U` and
    # `-A` are the same length.
    if len(line("-A")) >= MAX_LINE_BYTES:
        raise _refuse(argv, f"the Keychain command would be {len(line('-A'))} bytes, over "
                      f"the {MAX_LINE_BYTES} `security -i` takes in one line")
    command = line("-U" if _exists(account, keychain_path) else "-A")
    proc = _call(argv, input=command, capture_output=True, check=False)
    if proc.returncode != 0 or (proc.stderr or b"").strip():
        raise subprocess.CalledProcessError(
            proc.returncode or 1, argv, _redacted(proc.stdout, secret),
            _redacted(proc.stderr, secret),
        )


def _p12_account(team_id: str) -> str:
    return f"p12-{team_id}"


def _ssh_account(udid: str) -> str:
    return f"ssh-{udid}"


def read_p12_password(team_id: str) -> str | None:
    """Return the stored .p12 password, or None if not set."""
    return _read(_p12_account(team_id))


def write_p12_password(team_id: str, password: str) -> None:
    """Upsert the .p12 password in the login keychain."""
    _write(_p12_account(team_id), password)


def find_p12_password(team_id: str) -> tuple[bool, str | None]:
    """`(missing, secret)` for the team's item under `graft` alone.

    For a caller that must put back what it is about to overwrite
    (`account_setup`'s replace): `missing` says there is no such item, and
    `(False, None)` that there is one this could not read, which nothing can
    restore. Unlike `read_p12_password` it never looks at, or copies from,
    the pre-rename service.
    """
    return _find(SERVICE, _p12_account(team_id))


def delete_p12_password(team_id: str) -> None:
    """Remove the team's .p12 password from the login keychain, if it is there.

    As argv, not through `security -i`: nothing secret is in the command.
    Raised as `CalledProcessError` with `security`'s words, as `_write` is;
    an item that isn't there is not a failure.
    """
    account = _p12_account(team_id)
    argv = ["security", "delete-generic-password", "-s", SERVICE, "-a", account]
    if any(c in account for c in "\r\n\0"):
        raise subprocess.CalledProcessError(
            1, argv, b"", b"a Keychain account cannot contain a line break or a NUL"
        )
    proc = _call(argv, capture_output=True, check=False)
    if proc.returncode not in (0, _ITEM_NOT_FOUND):
        raise subprocess.CalledProcessError(proc.returncode, argv, proc.stdout, proc.stderr)


def read_ssh_password(udid: str) -> str | None:
    """Return the stored root SSH password for `udid`, or None if not set."""
    return _read(_ssh_account(udid))


def write_ssh_password(udid: str, password: str) -> None:
    """Upsert the root SSH password for `udid` in the login keychain."""
    _write(_ssh_account(udid), password)
