"""Which kind of Mac this is, and where it is.

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
import subprocess
import sys
from collections.abc import Callable
from typing import TypeVar

from . import child_env, storefronts

T = TypeVar("T")


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

#: The same callers' phrase for the Intel build of Graft.app on an Apple
#: silicon Mac: it carries neither UnfairPlay nor the PlayCover handover, and
#: the fix is the other download, not anything the Mac lacks.
INTEL_BUILD = "this is the Intel build of Graft; on an Apple silicon Mac, download Graft.zip"


def _running_flavor() -> str | None:
    """The Info.plist `GraftBundleArch` of the Graft.app this graft runs
    from (`app_update.own_bundle`), or None outside one (pipx, a checkout),
    for a plist without it, or for one that can't be read. The seam tests
    pin, as `_sysctl_arm64` is."""
    from . import app_update

    app = app_update.own_bundle()
    if app is None:
        return None
    flavor = (app_update.read_info_plist(app) or {}).get(app_update.FLAVOR_KEY)
    return flavor if isinstance(flavor, str) else None


def apple_silicon_blocker() -> str | None:
    """Why what needs Apple silicon, App Store decryption (UnfairPlay) and
    the PlayCover handover, can't run from this graft, as a clause: on an
    Intel Mac, `INTEL_MAC`; in the Intel build of Graft.app on an Apple
    silicon Mac, `INTEL_BUILD`. None when nothing stands in the way, or
    when the Mac can't be read and this isn't the Intel build: callers change
    what they say only for a reason, so "can't tell" leaves every message as
    it was.

    The one check the decryption and PlayCover refusals ask (`appstore`,
    `cli._do_install_to_mac`), so they can't disagree about which Macs and
    builds they cover."""
    if apple_silicon() is False:
        return INTEL_MAC
    if _running_flavor() == "x86_64":
        return INTEL_BUILD
    return None


#: `defaults` by absolute path. The CLI can run with a bare PATH (the 2.0
#: acceptance test's `/usr/bin:/bin:/usr/sbin:/sbin`), and this needs no other.
DEFAULTS = "/usr/bin/defaults"
REGION_TIMEOUT = 5


def _read_apple_locale(
    *, run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
) -> str | None:
    """This user's `AppleLocale` preference as `defaults` prints it, or None.

    The seam the test suite pins (`conftest.py`), as `_sysctl_arm64` is: no
    test depends on the region of the Mac it runs on.
    """
    try:
        proc = run(
            [DEFAULTS, "read", "-g", "AppleLocale"],
            capture_output=True, text=True, timeout=REGION_TIMEOUT,
            env=child_env.child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def region_from_apple_locale(value: str) -> str | None:
    """The two-letter region in a macOS `AppleLocale` value, upper-cased, or None.

    `en_GB` → GB, `zh-Hans_CN` → CN. A region chosen apart from the language
    (System Settings > General > Language & Region) is an `rg` keyword:
    `en_US@rg=gbzzzz` → GB. A region with no two-letter code (`es_419`, Latin
    America) is None, which the App Store search reads as "use the default".
    """
    text, _, keywords = value.strip().partition("@")
    for keyword in keywords.split(";"):
        key, _, val = keyword.partition("=")
        code = val[:2]
        if key == "rg" and len(code) == 2 and code.isascii() and code.isalpha():
            return code.upper()
    _, sep, region = text.rpartition("_")
    if sep and len(region) == 2 and region.isascii() and region.isalpha():
        return region.upper()
    return None


def _region() -> str | None:
    raw = _read_apple_locale()
    return region_from_apple_locale(raw) if raw else None


def mac_store() -> storefronts.Store:
    """The App Store this Mac's region shops in (`storefronts.for_region`).

    A territory is the store its people use (Puerto Rico: US), and a region
    with no store is the US store with `storefronts.NO_STORE_NOTE`. One that
    can't be read is the US store with nothing to say.
    """
    return storefronts.for_region(_region())


def mac_region() -> str | None:
    """The App Store this Mac's region shops in (GB, DE, ...), always one of
    `storefronts.STOREFRONTS`, or None when the region can't be read.

    The store the App Store search and the free-price check ask when nothing
    names one. Mapped, never the raw region: Apple answers a region with no
    store of its own (PR, RE, IR) with HTTP 400.
    """
    region = _region()
    return None if region is None else storefronts.for_region(region).code


def lookup_countries(store_country: str | None) -> tuple[str, ...]:
    """The stores a lookup of an app asks, in order: its own `store_country`
    alone; else the store this Mac's region shops in (`mac_region`, mapped),
    then the US.

    The US comes second for an app with no store because that is the store
    every 1.x lookup asked: an app added in 1.x has no `store_country`, and
    nor does a link that names no country, so a US-only app on a GB Mac
    would otherwise stop being found (combined fix check #21, #22).
    """
    if store_country:
        return (store_country,)
    return tuple(dict.fromkeys(c for c in (mac_region(), storefronts.DEFAULT) if c))


def look_up(store_country: str | None, ask: Callable[[str], T]) -> tuple[T, str]:
    """`ask(country)` in each store of `lookup_countries(store_country)`,
    in order; the first store that has the app wins, for this lookup only
    (nothing is written). Returns the answer and that store.

    The one fallback chain. The update check (`appstore.check`), the
    free-price check (`appstore._listing`), the artwork rung
    (`artwork.icon_from_app_store`) and an add by a URL that names no store
    all go through this, so no two of them can ask different stores for the
    same app (tests/test_store_country.py).

    Only "no such app in this store" (`itunes.NotInStore`) moves on to the
    next store; any other failure (offline, an HTTP error) is raised at once.
    When no store has the app, the error names every store asked.
    """
    from .itunes import NotInStore

    tried: list[str] = []
    for country in lookup_countries(store_country):
        try:
            return ask(country), country
        except NotInStore as e:
            tried.append(country)
            last = e
    raise last.in_stores(tried)
