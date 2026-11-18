"""`host.apple_silicon`: which kind of Mac this is, seen through Rosetta.

The release app is universal, so it runs on Intel Macs too, and two features
cannot work there at all: PlayCover runs iPhone and iPad apps only on Apple
silicon, and UnfairPlay enrolls with one of those apps. Those paths ask this
module so they can say so instead of failing with a message about something
else.

`conftest.py` pins the answer to Apple silicon for every other test; these
replace the reader underneath it.
"""

from __future__ import annotations

import errno
import sys

import pytest

from graft_ipa import host


@pytest.mark.parametrize(
    ("reading", "expected"),
    [
        (1, True),    # Apple silicon, natively or under Rosetta
        (0, False),
        (None, False),  # no such sysctl: an Intel Mac
    ],
)
def test_the_arm64_sysctl_decides(
    monkeypatch: pytest.MonkeyPatch, reading: int | None, expected: bool
) -> None:
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: reading)
    assert host.apple_silicon() is expected


def test_a_failed_read_is_dont_know_not_intel(monkeypatch: pytest.MonkeyPatch) -> None:
    """None, never False: the callers only change their message on False, so
    a sysctl that could not be read must leave the ordinary message alone."""

    def fails() -> int | None:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(host, "_sysctl_arm64", fails)
    assert host.apple_silicon() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="sysctlbyname is macOS-only")
def test_the_real_reader_answers_on_this_mac() -> None:
    """Through the generic reader, which the suite does not pin: on any Mac it
    reads 0 or 1, or None where the name does not exist."""
    assert host._read_sysctl_int("hw.optional.arm64") in (0, 1, None)
    assert host._read_sysctl_int("hw.no.such.name.graft") is None


#: The reader itself, taken at import — before `conftest.py`'s autouse fixture
#: pins it to 1 for every test. Through the pin, `_sysctl_arm64` is never run,
#: and a typo in the name it asks for ("hw.optional.arm65") passed the suite.
REAL_SYSCTL_ARM64 = host._sysctl_arm64


def _apple_silicon_hardware() -> bool:
    """Asked independently of the code under test: the machine this process
    runs as, and whether Rosetta is translating it."""
    import platform
    import subprocess

    if platform.machine() == "arm64":
        return True
    translated = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"], capture_output=True, text=True
    )
    return translated.stdout.strip() == "1"


@pytest.mark.skipif(sys.platform != "darwin", reason="sysctlbyname is macOS-only")
def test_the_unpinned_arm64_reader_is_right_about_this_mac(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = 1 if _apple_silicon_hardware() else None
    assert REAL_SYSCTL_ARM64() == expected
    monkeypatch.setattr(host, "_sysctl_arm64", REAL_SYSCTL_ARM64)
    assert host.apple_silicon() is (expected == 1)
