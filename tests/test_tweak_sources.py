"""Tweak sources kept in a local `tweaks-src/` carry their own tests. Run them.

A tweak source can ship an Objective-C harness under `tests/`, compiled with
plain `clang` and no Theos — deliberately, so its arithmetic can be checked
without an iOS toolchain or a device. That only helps if something actually
runs it: left alone, it runs when a person remembers to `make test` in that
directory, which is the same as not running.

`tweaks-src/` is the owner's private checkout and is not part of the public
repository, so without it this whole module skips.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

TWEAKS_SRC = Path(__file__).resolve().parent.parent / "tweaks-src"

if not TWEAKS_SRC.is_dir():
    pytest.skip("no tweaks-src/ checkout here", allow_module_level=True)


def _suites() -> list[Path]:
    """Every `tweaks-src/*/tests/Makefile`, so a new tweak's tests are picked
    up by existing here rather than by being added to a list."""
    return sorted(TWEAKS_SRC.glob("*/tests/Makefile"))


def test_the_discovery_finds_something() -> None:
    """Guards the glob itself. Without this, a `tweaks-src` that moved or a
    typo'd pattern would make every case below vacuously pass by iterating
    nothing — which is exactly how a parametrised suite goes quiet."""
    assert _suites(), f"no tweak test suites found under {TWEAKS_SRC}"


@pytest.mark.skipif(shutil.which("clang") is None, reason="needs clang")
@pytest.mark.parametrize("makefile", _suites(), ids=lambda p: p.parent.parent.name)
def test_a_tweaks_own_tests_pass(makefile: Path) -> None:
    # A private BIN, because the Makefile's default is the fixed shared path
    # a fixed shared path under `/tmp`: two runs in parallel — the practice in this repo —
    # race on it, one clang truncating the binary while the other execs it, and
    # the resulting ETXTBSY has nothing to do with either change.
    #
    # `timeout` because a hung `make` would otherwise hang the whole suite with
    # no indication of which target stalled.
    with tempfile.TemporaryDirectory() as td:
        result = subprocess.run(
            ["make", "test", f"BIN={Path(td) / 'harness'}"],
            cwd=makefile.parent, capture_output=True, text=True, timeout=300,
        )
    assert result.returncode == 0, result.stdout + result.stderr
    # `make` reports its own success; the C harness reports the assertions.
    # Checking only the exit code would pass a Makefile whose test target had
    # been emptied.
    # The harness's own report, not just make's exit code: an emptied test
    # target exits 0. Requires the word next to a count or "all", so a Makefile
    # that merely echoes "passed" does not satisfy it either.
    output = (result.stdout + result.stderr).lower()
    assert re.search(r"(all .*passed|\d+ .*passed|passed[.!]?$)", output, re.M), output
