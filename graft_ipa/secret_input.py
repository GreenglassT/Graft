"""Secrets read from stdin into memory this process can wipe.

`--appstore-login` is handed an Apple ID password and, later, a 2FA code on
stdin. Both go to one place, ipatool's prompt (`appstore_login`), and this
module is how they are held on the way.

What it does, and what that buys:

- **Read straight into a `bytearray`**, one byte per `os.readv`. Nothing here
  makes a `bytes` or `str` copy of the secret. Those are immutable, so a copy
  could not be wiped, and the allocator would free it without zeroing. One
  byte at a time also stops a read from swallowing the next line (the code
  comes down the same stdin minutes later).
- **Wiped in place** (`SecretLine.wipe`, the same length, so no reallocation)
  on every way out: success, failure, timeout, Stop. The caller holds the line
  in a `with` or a `finally`. A Stop is a signal handler raising, and it can
  land between any two steps, including after `read_line` has filled a line
  and before the caller has it (in `read_line`'s own `finally`, or between its
  `return` and the caller's assignment). So every line that isn't wiped yet is
  also in a module-level set, and `wipe_all` zeroes them: the sign-in's Stop
  handler calls it, and so does an `atexit` hook for any other way out of
  Python. Wiping takes a line out of the set.
- **Compared without decoding.** `redact` uses `bytes.replace`, which takes a
  `memoryview`, and `is_six_digits` indexes the buffer, so the secret never
  becomes text. Nothing calls `int()` on it either, since a `ValueError` would
  quote the value.

What it cannot do, said plainly:

- The kernel's pipe and pty buffers hold the bytes until they are read. That
  is out of Python's reach, and the read consumes them.
- A SIGKILL, or `os._exit`, skips every `finally` and `atexit`. The pages go
  back to the kernel, which zeroes them before another process gets them, but
  not at once.
- Python has no `mlock`, so a page holding the buffer can be swapped out.
  macOS encrypts swap.
- The other processes in the chain hold their own copies:
  - the app's text field (`SecretField`), cleared as soon as it is sent;
  - ipatool, which keeps the password as a Go string and **saves it in its own
    login-keychain item** (`ipatool-auth.service` / `account`) so it can sign
    in again when Apple expires the session. That is ipatool 2.8.0's design
    (`pkg/appstore/account.go`, `Account.Password`), not Graft's, and not
    something Graft can turn off.
"""

from __future__ import annotations

import atexit
import os
import select
import sys
import termios
import time
from collections.abc import Callable

#: The longest line accepted. An Apple ID password is at most 32 characters;
#: this leaves room for UTF-8 and refuses a runaway stream.
MAX_LINE = 1024

_LF, _CR, _SPACE, _TAB = 0x0A, 0x0D, 0x20, 0x09


class SecretTooLong(ValueError):
    pass


#: Every `SecretLine` not wiped yet, by identity: what `wipe_all` zeroes. A
#: strong reference, so a line dropped by a Stop isn't freed un-zeroed: it
#: waits here for `wipe_all`.
_LIVE: dict[int, SecretLine] = {}


class SecretLine:
    """One line of secret input. `view` is a window on it; `wipe` zeroes it."""

    __slots__ = ("_buf", "_len")

    def __init__(self, capacity: int = MAX_LINE) -> None:
        self._buf = bytearray(capacity)
        self._len = 0
        _LIVE[id(self)] = self

    def __len__(self) -> int:
        return self._len

    @property
    def view(self) -> memoryview:
        return memoryview(self._buf)[: self._len]

    def wipe(self) -> None:
        # The same length, so the bytearray is overwritten where it is rather
        # than reallocated (which would leave the old block un-zeroed). Out of
        # `_LIVE` only once it is zero.
        self._buf[:] = bytes(len(self._buf))
        self._len = 0
        _LIVE.pop(id(self), None)

    def __enter__(self) -> SecretLine:
        return self

    def __exit__(self, *exc: object) -> None:
        self.wipe()

    def __repr__(self) -> str:
        return f"<SecretLine {self._len} bytes>"


def wipe_all() -> int:
    """Wipe every `SecretLine` that isn't wiped yet; return how many there
    were. For a Stop, which can drop a line where no `finally` holds it."""
    lines = list(_LIVE.values())
    for line in lines:
        line.wipe()
    return len(lines)


atexit.register(wipe_all)


def read_line(
    fd: int,
    *,
    timeout: float | None = None,
    clock: Callable[[], float] = time.monotonic,
    capacity: int = MAX_LINE,
) -> SecretLine | None:
    """One line from `fd`, without its line ending, or None at end of input.

    Raises `TimeoutError` when nothing completes a line within `timeout`
    seconds, and `SecretTooLong` past `capacity`. Either way, what was read
    so far is wiped first.
    """
    line = SecretLine(capacity)
    deadline = None if timeout is None else clock() + timeout
    view = memoryview(line._buf)
    try:
        while True:
            if deadline is not None:
                remaining = deadline - clock()
                if remaining <= 0:
                    raise TimeoutError("no input in time")
                if not select.select([fd], [], [], remaining)[0]:
                    continue
            if line._len == capacity:
                raise SecretTooLong(f"a line longer than {capacity} bytes")
            if os.readv(fd, [view[line._len : line._len + 1]]) == 0:
                if line._len == 0:
                    line.wipe()
                    return None
                break
            if line._buf[line._len] == _LF:
                line._buf[line._len] = 0
                break
            line._len += 1
        if line._len and line._buf[line._len - 1] == _CR:
            line._len -= 1
            line._buf[line._len] = 0
        return line
    except BaseException:
        line.wipe()
        raise
    finally:
        view.release()


def prompt_line(
    fd: int, prompt: str, *, timeout: float | None = None, echo: bool = False
) -> SecretLine | None:
    """`read_line`, and in a terminal, a prompt on stderr with echo off.

    In the app, stdin is a pipe and nothing is printed. A person running
    `graft --appstore-login` in Terminal gets a prompt and no echo, as with
    `getpass`, which returns an immutable `str` and so can't be used here.
    `echo` leaves the terminal's echo on: the Apple ID, which isn't secret
    but is still kept off argv (controller decision 3).
    """
    if not os.isatty(fd):
        return read_line(fd, timeout=timeout)
    sys.stderr.write(prompt)
    sys.stderr.flush()
    if echo:
        return read_line(fd, timeout=timeout)
    saved = termios.tcgetattr(fd)
    quiet = list(saved)
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(fd, termios.TCSAFLUSH, quiet)
    try:
        return read_line(fd, timeout=timeout)
    finally:
        termios.tcsetattr(fd, termios.TCSAFLUSH, saved)
        sys.stderr.write("\n")
        sys.stderr.flush()


def redact(text: str, *secrets: SecretLine | None) -> str:
    """`text` with every copy of each secret replaced, matched as bytes."""
    data = text.encode("utf-8", "surrogateescape")
    for secret in secrets:
        if secret is not None and len(secret):
            data = data.replace(secret.view, b"[redacted]")
    return data.decode("utf-8", "surrogateescape")


def drop_spaces(line: SecretLine) -> None:
    """Remove spaces and tabs in place ("482 913" → "482913"), as ipatool's
    own `normalizeAuthCode` does before it checks the code."""
    kept = 0
    for i in range(line._len):
        byte = line._buf[i]
        if byte not in (_SPACE, _TAB):
            line._buf[kept] = byte
            kept += 1
    for i in range(kept, line._len):
        line._buf[i] = 0
    line._len = kept


def is_six_digits(line: SecretLine) -> bool:
    return len(line) == 6 and all(0x30 <= byte <= 0x39 for byte in line.view)
