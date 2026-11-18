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
import subprocess
from typing import Any

SERVICE = "graft"

#: The service name before the rename. Only ever read, never written.
LEGACY_SERVICE = "tweak-build"

#: `security`'s exit status for errSecItemNotFound — the one failure that
#: means "there is no such item" rather than "you may not have it".
_ITEM_NOT_FOUND = 44


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
    return subprocess.run(argv, **kwargs)


#: How `security` is run. A seam so the test suite can swap in a fake and
#: never read — or, via the copy above, write — a real keychain.
_run = _run_security


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

    Anything else, or hex that is not UTF-8, is unreadable, and None like an
    empty secret — never a reason to fall back to the legacy item, because
    the caller decides that from the exit status alone.
    """
    line = next((x for x in stderr.splitlines() if x.startswith("password:")), None)
    if line is None:
        return None
    rest = line[len("password:"):].removeprefix(" ")
    if rest == "":
        return None
    if rest.startswith("0x"):
        digits = rest[2:].split(" ", 1)[0]
        if not digits or not set(digits) <= _HEX_DIGITS or len(digits) % 2:
            return None
        try:
            return bytes.fromhex(digits).decode("utf-8") or None
        except UnicodeDecodeError:
            return None
    if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
        return rest[1:-1] or None
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
        proc = _run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-g"],
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return False, None
    if proc.returncode != 0:
        return proc.returncode == _ITEM_NOT_FOUND, None
    return False, _parse_password_line((proc.stderr or b"").decode("utf-8", "replace"))


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


def _quote(value: str) -> str:
    """`value` as one argument to `security -i`: double-quoted, with `\\` and
    `"` backslash-escaped. Measured against real `security -i` on a
    throwaway keychain for spaces, both quote kinds, backslashes (trailing
    too), `$`, backticks, a leading `-` and the empty string."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write(account: str, secret: str) -> None:
    """Upsert `secret` under `account` in the login keychain.

    Through `security -i` on stdin, not `add-generic-password -w <secret>` on
    the command line: argv is world-readable (`ps -axww` shows it to every
    user on the Mac), and this runs on ordinary reads too, for the legacy
    copy. A line break in the secret is refused — `-i` reads one command per
    line, so it would end the command early and run the rest as another.

    Failure is a non-zero exit *or* anything on stderr: `-i` reports a failed
    command and carries on, so its exit status alone is not to be trusted.
    Raised as `CalledProcessError` with `security`'s own words in `stderr`,
    which is what the `--set-*-password` callers already report.

    `-A` lets any app read the item without a Keychain prompt — the GUI runs
    the CLI from a Dock-launched process on every build. It is what every
    write here has always used, so the legacy copy carries the same access as
    the item it copies.
    """
    argv = ["security", "-i"]
    if any(c in secret for c in "\r\n\0"):
        raise subprocess.CalledProcessError(
            1, argv, b"", b"a Keychain password cannot contain a line break or a NUL"
        )
    command = (
        f"add-generic-password -s {_quote(SERVICE)} -a {_quote(account)} "
        f"-w {_quote(secret)} -U -A\n"
    )
    proc = _run(argv, input=command.encode(), capture_output=True, check=False)
    if proc.returncode != 0 or (proc.stderr or b"").strip():
        raise subprocess.CalledProcessError(
            proc.returncode or 1, argv, proc.stdout, proc.stderr
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


def read_ssh_password(udid: str) -> str | None:
    """Return the stored root SSH password for `udid`, or None if not set."""
    return _read(_ssh_account(udid))


def write_ssh_password(udid: str, password: str) -> None:
    """Upsert the root SSH password for `udid` in the login keychain."""
    _write(_ssh_account(udid), password)
