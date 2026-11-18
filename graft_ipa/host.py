"""Which kind of Mac this is.

The release app is universal, so it also runs on Intel Macs, where two
features cannot work at all: PlayCover runs iPhone and iPad apps only on Apple
silicon, and UnfairPlay enrolls with one of those apps. Those code paths ask
`apple_silicon()` so that, on an Intel Mac, they say so instead of failing with
a message about something else.

Asked of the kernel, not of `platform.machine()`: a Python running under
Rosetta reports `x86_64` on an Apple silicon Mac, and pipx can easily have
picked an x86_64 interpreter. `hw.optional.arm64` is 1 on Apple silicon
whichever architecture the asking process runs as, and does not exist on Intel.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys


def _read_sysctl_int(name: str) -> int | None:
    """The integer sysctl `name`, or None when this kernel has no such name.

    Raises OSError for any other failure, which `apple_silicon` reads as
    "can't tell" rather than as an answer.
    """
    if sys.platform != "darwin":
        raise OSError(errno.ENOSYS, "sysctlbyname is macOS-only")
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    value = ctypes.c_int(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, 0) != 0:
        err = ctypes.get_errno()
        if err == errno.ENOENT:
            return None
        raise OSError(err, os.strerror(err))
    return value.value


def _sysctl_arm64() -> int | None:
    """The seam the test suite pins (see `conftest.py`)."""
    return _read_sysctl_int("hw.optional.arm64")


def apple_silicon() -> bool | None:
    """True on an Apple silicon Mac (Rosetta included), False on an Intel Mac,
    None when it could not be read.

    Callers change what they say only on False, so "can't tell" leaves every
    message as it was.
    """
    try:
        return _sysctl_arm64() == 1
    except OSError:
        return None


#: The phrase both callers use, so the two messages cannot drift apart.
INTEL_MAC = "this Mac has an Intel processor"
