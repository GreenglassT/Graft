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
import subprocess
import sys
from pathlib import Path

import pytest

from graft_ipa import host, storefronts


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


# --- this Mac's region (`mac_region`) -----------------------------------------

# Captured at import, before conftest's `_no_real_mac_region` pins the seam.
_real_read_apple_locale = host._read_apple_locale


@pytest.mark.parametrize(
    ("value", "region"),
    [
        ("en_US", "US"),
        ("en_GB\n", "GB"),
        ("zh-Hans_CN", "CN"),
        # A region chosen apart from the language is an `rg` keyword.
        ("en_US@rg=gbzzzz", "GB"),
        ("en_US@calendar=gregorian;rg=dezzzz", "DE"),
        # Latin America has a numeric UN M.49 code, which no App Store answers to.
        ("es_419", None),
        ("en", None),
        ("", None),
    ],
)
def test_the_region_is_read_out_of_apple_locale(value: str, region: str | None) -> None:
    assert host.region_from_apple_locale(value) == region


def test_the_region_is_asked_of_defaults_by_absolute_path() -> None:
    calls: list[tuple[list[str], dict]] = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="en_GB\n", stderr="")

    assert _real_read_apple_locale(run=run) == "en_GB\n"
    argv, kwargs = calls[0]
    # Absolute: the 2.0 acceptance test runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin.
    assert argv == ["/usr/bin/defaults", "read", "-g", "AppleLocale"]
    assert kwargs["timeout"] == host.REGION_TIMEOUT


@pytest.mark.parametrize("outcome", ["exits non-zero", "cannot run", "times out"])
def test_a_region_defaults_cannot_give_is_none(outcome: str) -> None:
    def run(argv, **kwargs):
        if outcome == "cannot run":
            raise OSError("no such file")
        if outcome == "times out":
            raise subprocess.TimeoutExpired(argv, host.REGION_TIMEOUT)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="does not exist")

    assert _real_read_apple_locale(run=run) is None


def test_mac_region_is_the_parsed_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "en_US@rg=gbzzzz")
    assert host.mac_region() == "GB"
    monkeypatch.setattr(host, "_read_apple_locale", lambda: None)
    assert host.mac_region() is None


@pytest.mark.parametrize(
    ("locale", "store"),
    [("es_PR", "US"), ("fr_RE", "FR"), ("da_GL", "DK"), ("sv_AX", "FI"), ("fa_IR", "US"),
     ("en_US@rg=aqzzzz", "US")],
)
def test_mac_region_is_the_store_the_region_shops_in(
    monkeypatch: pytest.MonkeyPatch, locale: str, store: str
) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: locale)
    assert host.mac_region() == store


def test_a_region_with_no_store_is_the_us_store_with_a_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "fa_IR")
    assert host.mac_store() == storefronts.Store("US", note=storefronts.NO_STORE_NOTE)
    monkeypatch.setattr(host, "_read_apple_locale", lambda: "es_PR")
    assert host.mac_store() == storefronts.Store("US")
    monkeypatch.setattr(host, "_read_apple_locale", lambda: None)
    assert host.mac_store() == storefronts.Store("US")


@pytest.mark.parametrize(
    ("store_country", "locale", "stores"),
    [("DE", "en_GB", ("DE",)), (None, "de_DE", ("DE", "US")), (None, "es_PR", ("US",)),
     (None, "fa_IR", ("US",)), (None, None, ("US",)), (None, "en_US", ("US",))],
)
def test_an_apps_store_is_its_own_else_this_macs_mapped_then_the_us(
    monkeypatch: pytest.MonkeyPatch, store_country: str | None, locale: str | None, stores: tuple
) -> None:
    """Combined fix check #21: the US second, because every 1.x lookup asked it."""
    monkeypatch.setattr(host, "_read_apple_locale", lambda: locale)
    assert host.lookup_countries(store_country) == stores


def test_no_test_reads_the_real_region() -> None:
    """conftest's pin is in place: a suite run never depends on the Mac's locale."""
    assert host.mac_region() is None


# --- What needs Apple silicon: the Mac, and which build of Graft ---------------------


def _running(monkeypatch: pytest.MonkeyPatch, flavor: str | None) -> None:
    monkeypatch.setattr(host, "_running_flavor", lambda: flavor)


@pytest.mark.parametrize(("reading", "flavor", "blocker"), [
    (1, None, None),              # Apple silicon, from no bundle (pipx, a checkout)
    (1, "arm64", None),           # the Apple silicon build
    (1, "x86_64", host.INTEL_BUILD),  # the Intel build on Apple silicon
    (None, None, host.INTEL_MAC),     # an Intel Mac, whatever runs
    (None, "x86_64", host.INTEL_MAC),
    (0, "arm64", host.INTEL_MAC),
])
def test_what_needs_apple_silicon_is_blocked_by_the_mac_or_by_the_intel_build(
    monkeypatch: pytest.MonkeyPatch, reading: int | None, flavor: str | None, blocker: str | None
) -> None:
    monkeypatch.setattr(host, "_sysctl_arm64", lambda: reading)
    _running(monkeypatch, flavor)
    assert host.apple_silicon_blocker() == blocker


def test_a_mac_that_cant_be_read_blocks_only_for_the_intel_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Can't tell leaves every message as it was; the Intel build is still
    the Intel build."""
    def fails() -> int:
        raise OSError(errno.EPERM, "denied")

    monkeypatch.setattr(host, "_sysctl_arm64", fails)
    _running(monkeypatch, None)
    assert host.apple_silicon_blocker() is None
    _running(monkeypatch, "x86_64")
    assert host.apple_silicon_blocker() == host.INTEL_BUILD


def test_the_intel_build_says_which_download_to_get() -> None:
    assert host.INTEL_BUILD == "this is the Intel build of Graft; on an Apple silicon Mac, download Graft.zip"


def test_the_running_build_is_read_from_the_bundle_around_this_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Graft.app around the interpreter running this, its Info.plist's
    GraftBundleArch; nothing outside a bundle, or for a plist without it."""
    from graft_ipa import app_update

    from .app_update_fakes import dummy_app

    assert host._running_flavor() is None, "this test's interpreter runs from no bundle"
    for flavor in ("x86_64", "arm64", None):
        where = tmp_path / str(flavor)
        where.mkdir()
        app = dummy_app(where, "2.2", flavor=flavor)
        monkeypatch.setattr(sys, "executable", str(app.joinpath(*app_update.EMBEDDED_PYTHON, "python3.13")))
        assert host._running_flavor() == flavor
    (app / "Contents" / "Info.plist").write_bytes(b"<plist><dict><key>")
    assert host._running_flavor() is None, "a plist that can't be read says nothing"
