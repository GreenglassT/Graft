"""Whether this Mac has a C toolchain that runs, not merely one that exists.

Every test that runs `clang`, `make`, `lipo` or `git` says so with a marker
from here, so that a Mac with no developer directory (no Command Line Tools,
or an invalid DEVELOPER_DIR) skips it instead of failing it — and instead of
running a /usr/bin shim, which on a real Mac without the tools offers to
install them. `tests/test_toolchain_markers.py` runs the suite's tool-using
files that way and fails on any test that forgot.
"""

from __future__ import annotations

import functools
import shutil
import subprocess

import pytest


@functools.cache
def working_toolchain() -> bool:
    """A `clang` and a `make` that run.

    `shutil.which("clang")` always succeeds on macOS: /usr/bin/clang and
    /usr/bin/make are xcrun shims on every Mac, Command Line Tools or not, so
    with no developer directory (or an invalid DEVELOPER_DIR) a test guarded
    by it failed where it meant to skip. `xcode-select -p` goes first because,
    unlike the shims, it never offers to install the tools.
    """
    try:
        if subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=30).returncode != 0:
            return False
        return all(
            subprocess.run(["xcrun", "--find", tool], capture_output=True, timeout=30).returncode == 0
            for tool in ("clang", "make")
        )
    except FileNotFoundError:  # not macOS: no xcode-select, no shims to be fooled by
        return shutil.which("clang") is not None and shutil.which("make") is not None
    except (OSError, subprocess.TimeoutExpired):
        return False


needs_toolchain = pytest.mark.skipif(not working_toolchain(), reason="needs a working clang and make")


@functools.cache
def working_tool(name: str) -> bool:
    """`name` on PATH is a binary that runs without the Command Line Tools'
    help, or the developer directory can supply it.

    A /usr/bin copy of `git`, `lipo` or `clang` is an xcrun shim, so it is
    asked about the way `working_toolchain` asks: `xcode-select -p`, then
    `xcrun --find`, neither of which offers an install. One anywhere else
    (Homebrew's git) is the tool itself.
    """
    path = shutil.which(name)
    if path is None:
        return False
    if not path.startswith("/usr/bin/"):
        return True
    try:
        if subprocess.run(["xcode-select", "-p"], capture_output=True, timeout=30).returncode != 0:
            return False
        return subprocess.run(["xcrun", "--find", name], capture_output=True, timeout=30).returncode == 0
    except FileNotFoundError:  # not macOS: a /usr/bin tool is a real one
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def needs_tools(*names: str) -> pytest.MarkDecorator:
    """Skip unless every one of `names` runs (`working_tool`)."""
    missing = [n for n in names if not working_tool(n)]
    return pytest.mark.skipif(bool(missing), reason=f"needs a working {', '.join(missing) or 'toolchain'}")
