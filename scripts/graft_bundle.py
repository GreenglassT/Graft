#!/usr/bin/env python3
"""Stage everything Graft.app bundles, outside any .app, for scripts/graft-release.

Run with the repo venv's Python (it imports graft_ipa.macho):

    .venv/bin/python scripts/graft_bundle.py stage <staging-dir>

`<staging-dir>` comes out shaped like `Graft.app/Contents`:
Resources/GraftPython, Resources/Licenses, Helpers/graft, Helpers/bin/*,
Frameworks/libssl.3.dylib, Frameworks/libcrypto.3.dylib, Frameworks/ossl-modules/,
Frameworks/libplist-2.0.dylib, plus `macho-list.txt` (every Mach-O to sign,
deepest first) at its root. cyan's five helpers (ldid, lipo, otool,
install_name_tool, insert_dylib) are built from pinned source here, in place
of the prebuilt copies its archive carries.

Nothing here runs code from inside a .app, and nothing is downloaded without
a SHA-256 check against bundle/pins.toml.
"""

from __future__ import annotations

import csv
import email.message
import email.parser
import functools
import gzip
import hashlib
import io
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def default_download_cache() -> Path:
    """~/Library/Caches/graft-release/bundle-cache: one per user, outside
    every checkout. A cache inside the checkout started empty in each new
    worktree, so a release from one fetched some 350 MB again. Sharing it
    trusts nothing new: fetch() checks a cached file's SHA-256 on every use,
    and Go checks its modules against go.sum (and `go mod verify`)."""
    return Path.home() / "Library" / "Caches" / "graft-release" / "bundle-cache"


def cache_dirs() -> tuple[Path, Path]:
    """(downloads, build products) caches: GRAFT_BUNDLE_CACHE, else
    default_download_cache(); and GRAFT_BUNDLE_WORK, else build/bundle-work,
    which graft-release sets so a stage run from a fresh export keeps the
    checkout's builds. Builds stay per checkout: each stage rewrites
    folders there (pip-reports/, tool-licences/) that two checkouts staging
    at once would trample."""
    out = []
    for var, default in (("GRAFT_BUNDLE_CACHE", default_download_cache()),
                         ("GRAFT_BUNDLE_WORK", REPO / "build" / "bundle-work")):
        value = os.environ.get(var)
        if value and not os.path.isabs(value):
            raise BundleError(f"{var} must be an absolute path, not {value!r}")
        out.append(Path(value) if value else default)
    return out[0], out[1]


class BundleError(Exception):
    """Staging can't continue. The message says what to fix."""


CACHE, WORK = cache_dirs()

sys.path.insert(0, str(REPO))
from graft_ipa import macho  # noqa: E402


@dataclass(frozen=True)
class Pin:
    name: str
    url: str
    sha256: str
    license: str
    #: What components.json reports; empty only for a Pin built in a test.
    version: str = ""
    #: The binaries built from this source, each its own components.json
    #: entry; empty means one entry, named after the pin.
    builds: tuple[str, ...] = ()
    #: The one file taken out of the archive, and that file's own SHA-256.
    member: str = ""
    member_sha256: str = ""
    #: The pin whose archive is this binary's source, for the source archive.
    source: str = ""

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


def load_pins(path: Path) -> dict[str, Pin]:
    data = tomllib.loads(path.read_text())
    for name, v in data.items():
        missing = {"url", "sha256", "license", "version"} - set(v)
        if missing:
            raise BundleError(f"{path.name}: [{name}] has no {', '.join(sorted(missing))}")
    pins = {
        name: Pin(name=name, url=v["url"], sha256=v["sha256"].lower(), license=v["license"],
                  version=v["version"], builds=tuple(v.get("builds", ())), member=v.get("member", ""),
                  member_sha256=v.get("member_sha256", "").lower(), source=v.get("source", ""))
        for name, v in data.items()
    }
    for pin in pins.values():
        if bool(pin.member) != bool(pin.member_sha256):
            raise BundleError(f"{path.name}: [{pin.name}] needs both member and member_sha256, or neither")
        if pin.source and pin.source not in pins:
            raise BundleError(f"{path.name}: [{pin.name}] names source {pin.source!r}, which isn't pinned")
    return pins


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


#: How many times a download or a PyPI lookup is tried before the build stops.
NETWORK_ATTEMPTS = 4


def _urlopen(url: str, *, timeout: float):
    """`urlopen(url)`, tried again after a dropped network: a DNS failure, a
    refused or timed-out connection, a TLS handshake that never finished.
    An HTTP answer (404 and the rest) is final, and so is the last failure.
    Whatever comes back is checked by the caller (a pin's SHA-256, PyPI's
    own digest), so a retry can't change what is accepted."""
    req = urllib.request.Request(url, headers={"User-Agent": "graft-release"})
    for attempt in range(1, NETWORK_ATTEMPTS + 1):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == NETWORK_ATTEMPTS:
                raise
            wait = 10 * attempt
            print(f"  network error ({e}); trying {url} again in {wait} s", file=sys.stderr)
            time.sleep(wait)
    raise AssertionError("unreachable")


def _download(url: str, dest: Path) -> None:
    with _urlopen(url, timeout=120) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def fetch(pin: Pin, cache: Path = CACHE) -> Path:
    """The pinned file, downloaded once into `cache` and always checked."""
    cache.mkdir(parents=True, exist_ok=True)
    dest = cache / pin.filename
    if dest.exists() and _sha256(dest) == pin.sha256:
        return dest
    # A partial file of this download's own: the cache is shared between
    # checkouts, so two releases may fetch the same file at once.
    fd, raw = tempfile.mkstemp(prefix=f"{dest.name}.", suffix=".part", dir=cache)
    os.close(fd)
    part = Path(raw)
    try:
        _download(pin.url, part)
        got = _sha256(part)
        if got != pin.sha256:
            raise BundleError(f"{pin.name}: SHA-256 mismatch for {pin.url}: got {got}, pinned {pin.sha256}")
        part.chmod(0o644)  # mkstemp's 0600 would differ from every other cached file
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def extract(archive: Path, dest: Path) -> Path:
    """Unpack a .tar.gz or .zip into `dest`, refusing members outside it."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()

    def inside(name: str) -> None:
        if not (root / name).resolve().is_relative_to(root):
            raise BundleError(f"{archive.name}: member {name!r} would land outside {dest}")

    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for n in zf.namelist():
                inside(n)
            zf.extractall(dest)
    else:
        with tarfile.open(archive) as tf:
            for m in tf.getmembers():
                inside(m.name)
                if m.issym():
                    # A symlink's target is relative to the directory holding it.
                    inside(str(Path(m.name).parent / m.linkname))
                elif m.islnk():
                    # A hard link's target is a member name: relative to the archive root.
                    inside(m.linkname)
            tf.extractall(dest, filter="tar")
    return dest


OPENSSL_LIBS = ("libssl.3.dylib", "libcrypto.3.dylib")

#: The prefix OpenSSL is configured with. It is compiled into libcrypto as the
#: default config, engine and provider directories, so it must not be the
#: build directory: that would ship the build machine's home path. Nothing is
#: ever installed there (`make install_sw` uses DESTDIR), but libcrypto reads
#: <prefix>/ssl/openssl.cnf at start-up unless OPENSSL_CONF says otherwise,
#: so Graft runs the bundled zsign, and the bundled cyan (whose ldid links
#: this libcrypto), with OPENSSL_CONF=/dev/null
#: (graft_ipa.child_env.BUNDLED_OPENSSL_CONF; review 2.0, bundle #7). zsign
#: also gets OPENSSL_MODULES, and the install names are rewritten.
OPENSSL_BAKED_PREFIX = "/opt/graft/openssl"


def openssl_repoint_plan(zsign: Path) -> list[tuple[str, str]]:
    """(old, new) install names that move zsign's OpenSSL into Frameworks.

    zsign's release binary links Homebrew's openssl@3. Under the hardened
    runtime that fails (a different Team ID), and on a Mac without Homebrew
    it can't load at all. Anything linking an OpenSSL other than major 3 is
    refused: the bundled build is 3.x.
    """
    plan = []
    for name in macho.dylib_names(zsign):
        leaf = name.rsplit("/", 1)[-1]
        if leaf.startswith(("libssl.", "libcrypto.")):
            if leaf not in OPENSSL_LIBS:
                raise BundleError(f"zsign links {leaf}; the bundle ships OpenSSL 3 ({', '.join(OPENSSL_LIBS)})")
            plan.append((name, f"@executable_path/../../Frameworks/{leaf}"))
    return plan


#: The developer tools the stage runs itself, by path: the system's, which
#: run the active Xcode's (DEVELOPER_DIR passes through _build_env), never
#: whatever the release shell's PATH finds first (review 2.0, bundle #6).
CLANG, NM, LIPO, INSTALL_NAME_TOOL = "/usr/bin/clang", "/usr/bin/nm", "/usr/bin/lipo", "/usr/bin/install_name_tool"


def _run(*args: str | Path, cwd: Path | None = None) -> None:
    """A tool, by its path, in the clean build environment (_build_env)."""
    subprocess.run([str(a) for a in args], cwd=cwd, env=_build_env(), check=True)


def openssl_steps(destdir: str, jobs: list[str]) -> list[list[str]]:
    """Run in openssl-3.5.9/, with the release machine's environment."""
    return [
        ["./Configure", "darwin64-arm64-cc", "shared", "no-tests", "no-docs",
         f"--prefix={OPENSSL_BAKED_PREFIX}", f"--openssldir={OPENSSL_BAKED_PREFIX}/ssl", f"-mmacosx-version-min={MACOS_MIN}"],
        ["make", *jobs],
        ["make", "install_sw", f"DESTDIR={destdir}"],
    ]


def build_openssl(pin: Pin, work: Path = WORK) -> Path:
    """OpenSSL built from its pinned source: the installed prefix, holding
    lib/libssl.3.dylib, lib/libcrypto.3.dylib, lib/ossl-modules/legacy.dylib,
    include/ and the source's LICENSE.txt.

    Built like cyan's helpers (review #22, #27, #29, #31): under _build_env,
    not the release machine's whole environment, in a temporary directory
    (its Makefiles don't survive a space in a path), and cached by its
    source, its steps and the toolchain, so a changed Configure line or
    compiler is a new build that SOURCES.md describes truly.
    """
    def build(tmp: Path, out: Path) -> None:
        src = _unpack(pin, tmp)
        _build_steps(openssl_steps(str(tmp / "dest"), _jobs()), src)
        built = tmp / "dest" / OPENSSL_BAKED_PREFIX.lstrip("/")
        shutil.copytree(built, out / "prefix", symlinks=True)
        shutil.copy2(src / "LICENSE.txt", out / "prefix" / "LICENSE.txt")
    key = build_key(pin.sha256, openssl_steps("$DESTDIR", []), toolchain_id())
    return _cached_build("openssl", key, work, build) / "prefix"


def stage_openssl_and_zsign(prefix: Path, zsign_src: Path, contents: Path) -> None:
    """libssl, libcrypto and the legacy provider into Frameworks; zsign repointed."""
    fw = contents / "Frameworks"
    (fw / "ossl-modules").mkdir(parents=True, exist_ok=True)
    for lib in OPENSSL_LIBS:
        shutil.copy2(prefix / "lib" / lib, fw / lib)
        _run(INSTALL_NAME_TOOL, "-id", f"@rpath/{lib}", fw / lib)
    # libssl links libcrypto by its build-time path; point it next door.
    for old in macho.dylib_names(fw / "libssl.3.dylib"):
        if old.endswith("/libcrypto.3.dylib"):
            _run(INSTALL_NAME_TOOL, "-change", old, "@loader_path/libcrypto.3.dylib", fw / "libssl.3.dylib")
    legacy = fw / "ossl-modules" / "legacy.dylib"
    shutil.copy2(prefix / "lib" / "ossl-modules" / "legacy.dylib", legacy)
    for old in macho.dylib_names(legacy):
        if old.endswith("/libcrypto.3.dylib"):
            _run(INSTALL_NAME_TOOL, "-change", old, "@loader_path/../libcrypto.3.dylib", legacy)
    bindir = contents / "Helpers" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    zsign = bindir / "zsign"
    shutil.copy2(zsign_src, zsign)
    zsign.chmod(0o755)
    for old, new in openssl_repoint_plan(zsign):
        _run(INSTALL_NAME_TOOL, "-change", old, new, zsign)


# ------------------------------------------------- cyan's helpers, from source

#: cyan's own helpers, in cyan/tools/Darwin/arm64: the names cyan's code runs
#: them by (cyan.tbhtypes.executable). Each is built from pinned source.
CYAN_HELPERS = ("ldid", "insert_dylib", "lipo", "install_name_tool", "otool")

#: Where cyan runs them from (tbhutils.get_tools_dir), relative to Contents.
CYAN_TOOLS = Path("Resources/GraftPython/lib/python3.13/site-packages/cyan/tools/Darwin/arm64")
#: cyan's iOS frameworks, relative to Contents: see CYAN_FRAMEWORKS.
CYAN_EXTRAS = Path("Resources/GraftPython/lib/python3.13/site-packages/cyan/extras")

#: libplist, for ldid, in Frameworks as a dylib of its own: LGPL-2.1, so a
#: user can swap in their own build of it.
LIBPLIST = "libplist-2.0.dylib"
LIBCRYPTO = "libcrypto.3.dylib"
#: The prefixes compiled into libplist and cctools, like OpenSSL's: neutral,
#: never installed into (DESTDIR), never read at run time.
LIBPLIST_BAKED_PREFIX = "/opt/graft/libplist"
CCTOOLS_BAKED_PREFIX = "/opt/graft/cctools"

#: The oldest macOS Graft.app supports. 14.0 is the floor the app's Swift sets
#: (Observation and onChange(of:initial:)); everything the stage builds targets
#: it too, and verify-bundle checks Info.plist promises exactly this.
MACOS_MIN = "14.0"
#: Every helper built from source targets this, as the rest of the bundle does.
TARGET = ("-arch", "arm64", f"-mmacosx-version-min={MACOS_MIN}")
#: Room in ldid's header for the @loader_path install names it gets after the link.
HEADERPAD = "-Wl,-headerpad_max_install_names"
#: What a staged helper may link besides the bundle's own libraries.
SYSTEM_LIBS = ("/usr/lib/", "/System/")

#: License files copied out of each source tree into Licenses/, as `<pin>-<file>`.
HELPER_LICENCES = {
    "libplist": ("COPYING.LESSER", "COPYING"),
    "ldid": ("COPYING",),
    "cctools": ("APPLE_LICENSE",),
    "insert_dylib": ("LICENSE",),
}


@functools.cache
def toolchain_id() -> str:
    """The compiler and SDK every build here uses, as _build_env finds them:
    part of each build's cache key, so a new Xcode is a new build (review
    #31), and what SOURCES.md says built them (#27)."""
    def first(*argv: str) -> str:
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=60, env=_build_env()).stdout
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        return out.splitlines()[0].strip() if out.strip() else "unknown"
    return f"{first('clang', '--version')}, macOS SDK {first('xcrun', '--show-sdk-version')}"


def _build_env() -> dict[str, str]:
    """What a from-source build runs with: the system's tools and nothing
    else, so nothing from Homebrew or this Mac's PATH is compiled or linked
    in, and no CFLAGS, LDFLAGS or PKG_CONFIG_PATH of the release machine's."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", "/"),
           "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "LANG": "C", "MACOSX_DEPLOYMENT_TARGET": MACOS_MIN}
    if os.environ.get("DEVELOPER_DIR"):
        env["DEVELOPER_DIR"] = os.environ["DEVELOPER_DIR"]
    return env


def _jobs() -> list[str]:
    return [f"-j{os.cpu_count() or 4}"]


def _flags(*extra: str) -> str:
    return " ".join((*TARGET, *extra))


# The build commands, as data: the builds below run them, the cache key
# covers them (with placeholders for the build's own paths), and the source
# archive can print exactly what ran. `jobs` is make's -j.


def libplist_steps(destdir: str, jobs: list[str]) -> list[list[str]]:
    """Run in libplist-2.8.0/."""
    return [
        ["./configure", f"--prefix={LIBPLIST_BAKED_PREFIX}", "--enable-shared", "--disable-static",
         "--without-cython", "--without-tools", "--without-tests", "CC=clang", "CXX=clang++",
         f"CFLAGS={_flags('-O2')}", f"CXXFLAGS={_flags('-O2')}", f"LDFLAGS={_flags()}"],
        ["make", *jobs],
        ["make", "install", f"DESTDIR={destdir}"],
    ]


def ldid_steps(libplist: str, openssl: str, jobs: list[str]) -> list[list[str]]:
    """Run in ldid's source; `libplist` and `openssl` hold include/ and the dylibs."""
    return [["make", *jobs, "ldid", "CXX=clang++", f"CXXFLAGS={_flags('-O2')}", f"LDFLAGS={_flags(HEADERPAD)}",
             f"LIBPLIST_INCLUDES=-I{libplist}/include", f"LIBPLIST_LIBS={libplist}/{LIBPLIST}",
             f"LIBCRYPTO_INCLUDES=-I{openssl}/include", f"LIBCRYPTO_LIBS={openssl}/lib/{LIBCRYPTO}"]]


def cctools_steps(jobs: list[str]) -> list[list[str]]:
    """Run in cctools-port's cctools/. Only libstuff and libmacho, then the
    three tools: no ld64, no LTO, libtapi or xar."""
    return [
        ["./configure", f"--prefix={CCTOOLS_BAKED_PREFIX}", "--disable-lto-support", "--disable-tapi-support",
         "--disable-xar-support", "CC=clang", "CXX=clang++",
         f"CFLAGS={_flags('-O2')}", f"CXXFLAGS={_flags('-O2')}", f"LDFLAGS={_flags()}"],
        ["make", *jobs, "-C", "libstuff"],
        ["make", *jobs, "-C", "libmacho"],
        ["make", *jobs, "-C", "misc", "lipo", "install_name_tool"],
        ["make", *jobs, "-C", "otool"],
    ]


def insert_dylib_steps() -> list[list[str]]:
    """Run in insert_dylib's source: its one file. (The output goes beside it:
    `insert_dylib` at the top is the source directory.)"""
    return [["clang", *TARGET, "-O2", "-o", "insert_dylib/insert_dylib", "insert_dylib/main.c"]]


#: Patches applied to a pin's unpacked source before it is built, in order:
#: files in bundle/patches/, each applied with `patch -p1` from the source's
#: top directory (PATCH_ARGV). Each one's SHA-256 is part of its build's cache
#: key, and SOURCES.md names it.
PATCHES_DIR = REPO / "bundle" / "patches"
PATCHES: dict[str, tuple[str, ...]] = {
    # cctools-port's FAKE_SIGN hook runs `system("codesign -s - -f <file>")`,
    # unquoted, after every change install_name_tool makes to an ARM Mach-O.
    "cctools": ("cctools-port-no-fake-sign.patch",),
}
#: How a patch is applied: exactly (no fuzz), and never twice.
PATCH_ARGV = ("/usr/bin/patch", "-p1", "--forward", "--batch", "--fuzz=0", "-i")


def patch_hashes(name: str, patches: Path | None = None) -> list[tuple[str, str]]:
    """(file, SHA-256) of each patch for pin `name`, in the order applied.
    A patch PATCHES names that isn't there is refused."""
    patches = patches or PATCHES_DIR
    out = []
    for p in PATCHES.get(name, ()):
        path = patches / p
        if not path.is_file():
            raise BundleError(f"{name}: its patch {path.relative_to(REPO) if path.is_relative_to(REPO) else path} "
                              "is missing")
        out.append((p, _sha256(path)))
    return out


def apply_patches(name: str, src: Path, patches: Path | None = None) -> None:
    """Apply pin `name`'s patches to its unpacked source tree `src`."""
    patches = patches or PATCHES_DIR
    for p, _ in patch_hashes(name, patches):
        subprocess.run([*PATCH_ARGV, str(patches / p)], cwd=src, env=_build_env(), check=True)


#: This file's own SHA-256, in every build's cache key: what it does with a
#: build's products after the build ran (vendor.tar.gz, the module notices,
#: the checks on them) is part of what is cached, so any change to it is a
#: new build, never a stale product reused (review 2.0, bundle #5).
BUILDER = _sha256(Path(__file__).resolve())


def build_key(*parts: object) -> str:
    """A cache key over source hashes and build steps, and BUILDER: a change
    to any of them is a new build, never a stale one reused."""
    return hashlib.sha256(json.dumps([BUILDER, *parts], sort_keys=True).encode()).hexdigest()


def _cached_build(name: str, key: str, work: Path, build: Callable[[Path, Path], None]) -> Path:
    """`work/<name>-<key[:12]>`: what `build(tmp, out)` left in `out`, built
    once per key.

    The build runs in a fresh temporary directory, never under `work`:
    autotools and these Makefiles don't survive a space in a path, and this
    repo usually lives in one. A build whose products name that temporary
    directory, or the downloads cache (where Go's module cache is, outside
    it), is refused. The finished tree is renamed into place whole, so one
    that exists is complete.
    """
    done = work / f"{name}-{key[:12]}"
    if done.is_dir():
        return done
    part = work / f"{name}-{key[:12]}.part"
    with tempfile.TemporaryDirectory(prefix=f"graft-{name}-") as raw:
        tmp = Path(raw).resolve()
        if " " in str(tmp):
            raise BundleError(f"{name} can't build under a path with a space: {tmp} (set TMPDIR)")
        out = tmp / "out"
        out.mkdir()
        build(tmp, out)
        needles = {raw.encode(), str(tmp).encode(), str(CACHE).encode()}
        leaks = [str(p.relative_to(out)) for p in sorted(out.rglob("*"))
                 if p.is_file() and not p.is_symlink() and any(n in p.read_bytes() for n in needles)]
        if leaks:
            raise BundleError(f"{name}: the build left its build directory's or the download cache's path in "
                              f"{', '.join(leaks)}")
        shutil.rmtree(part, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        shutil.copytree(out, part, symlinks=True)
    part.replace(done)
    return done


def _unpack(pin: Pin, tmp: Path) -> Path:
    """The pinned archive's one top-level directory, unpacked under tmp/src."""
    tops = list(extract(fetch(pin), tmp / "src").iterdir())
    if len(tops) != 1 or not tops[0].is_dir():
        raise BundleError(f"{pin.name}: expected one top-level directory in {pin.filename}, found {tops}")
    return tops[0]


def _build_steps(steps: list[list[str]], cwd: Path) -> None:
    for step in steps:
        subprocess.run(step, cwd=cwd, env=_build_env(), check=True)


def build_libplist(pin: Pin, work: Path = WORK) -> Path:
    """libplist, shared: out/libplist-2.0.dylib, out/include, its COPYING files."""
    def build(tmp: Path, out: Path) -> None:
        src = _unpack(pin, tmp)
        _build_steps(libplist_steps(str(tmp / "dest"), _jobs()), src)
        installed = tmp / "dest" / LIBPLIST_BAKED_PREFIX.lstrip("/")
        shutil.copy2((installed / "lib" / LIBPLIST).resolve(), out / LIBPLIST)
        shutil.copytree(installed / "include", out / "include")
        for f in HELPER_LICENCES["libplist"]:
            shutil.copy2(src / f, out / f)
    return _cached_build("libplist", build_key(pin.sha256, libplist_steps("$DESTDIR", []), toolchain_id()), work, build)


def build_ldid(pin: Pin, libplist: Path, openssl: Path, work: Path = WORK) -> Path:
    """ldid, linked to `libplist` (build_libplist's) and the bundled OpenSSL's
    libcrypto. Its cache key covers both: they are build_libplist's and
    build_openssl's own cache directories, named by their keys."""
    def build(tmp: Path, out: Path) -> None:
        src = _unpack(pin, tmp)
        # Copies without a space in their path, for ldid's Makefile.
        plist, ossl = tmp / "libplist", tmp / "openssl"
        shutil.copytree(libplist, plist, symlinks=True)
        shutil.copytree(openssl / "include", ossl / "include")
        (ossl / "lib").mkdir()
        shutil.copy2(openssl / "lib" / LIBCRYPTO, ossl / "lib" / LIBCRYPTO)
        _build_steps(ldid_steps(str(plist), str(ossl), _jobs()), src)
        shutil.copy2(src / "ldid", out / "ldid")
        for f in HELPER_LICENCES["ldid"]:
            shutil.copy2(src / f, out / f)
    key = build_key(pin.sha256, libplist.name, openssl.parent.name, ldid_steps("$LIBPLIST", "$OPENSSL", []),
                    toolchain_id())
    return _cached_build("ldid", key, work, build)


#: What no helper built from source may import: each starts a shell.
SHELL_IMPORTS = ("_system", "_popen")


def imported_symbols(path: Path) -> set[str]:
    """The undefined (imported) symbols of a Mach-O, every slice's."""
    r = subprocess.run([NM, "-u", "-j", str(path)], capture_output=True, text=True, env=_build_env(), check=True)
    return {line.strip() for line in r.stdout.splitlines() if line.strip() and not line.endswith(":")}


def check_no_shell(path: Path, name: str) -> None:
    found = sorted(imported_symbols(path) & set(SHELL_IMPORTS))
    if found:
        raise BundleError(f"the built {name} imports {', '.join(found)}, which start a shell")


def build_cctools(pin: Pin, work: Path = WORK) -> Path:
    """lipo, install_name_tool and otool from cctools-port, patched
    (PATCHES), and APPLE_LICENSE. A tool importing system() or popen() is
    refused."""
    def build(tmp: Path, out: Path) -> None:
        top = _unpack(pin, tmp)
        apply_patches("cctools", top)
        src = top / "cctools"
        _build_steps(cctools_steps(_jobs()), src)
        for tool in pin.builds:
            shutil.copy2(src / ("otool" if tool == "otool" else "misc") / tool, out / tool)
            check_no_shell(out / tool, tool)
        for f in HELPER_LICENCES["cctools"]:
            shutil.copy2(src / f, out / f)
    key = build_key(pin.sha256, pin.builds, cctools_steps([]), patch_hashes("cctools"), toolchain_id())
    return _cached_build("cctools", key, work, build)


def build_insert_dylib(pin: Pin, work: Path = WORK) -> Path:
    def build(tmp: Path, out: Path) -> None:
        src = _unpack(pin, tmp)
        _build_steps(insert_dylib_steps(), src)
        shutil.copy2(src / "insert_dylib" / "insert_dylib", out / "insert_dylib")
        for f in HELPER_LICENCES["insert_dylib"]:
            shutil.copy2(src / f, out / f)
    return _cached_build("insert_dylib", build_key(pin.sha256, insert_dylib_steps(), toolchain_id()), work, build)


# ---------------------------------------------------- ipatool, from source

#: ipatool is built from its pinned source as upstream has it. Unicorn, the
#: GPL-2.0 library its App Store emulator runs on, is not bundled: ipatool
#: downloads it on first use, as upstream does, from PyPI into
#: ~/Library/Caches/ipatool, checking the wheel's and the library's SHA-256
#: and the library's again before every load (internal/sap/unicorn/cache.go).
#: Shipping it beside ipatool, whose binary holds Apache-2.0 and LGPL-3.0
#: code, would distribute that combination (the combined fix check's
#: medium #2; the owner chose this, option (a)).

#: The only entitlements in the bundle, by path under Contents, each a file in
#: bundle/entitlements/: ipatool's two. Unicorn needs MAP_JIT memory for its
#: code translator, which the hardened runtime grants only with allow-jit;
#: and the Unicorn ipatool downloads is ad hoc signed, which library
#: validation would refuse, so ipatool alone has disable-library-validation.
#: Never allow-dyld-environment-variables: DYLD_INSERT_LIBRARIES stays blocked.
ENTITLEMENTS_DIR = REPO / "bundle" / "entitlements"
ENTITLED = {"Helpers/bin/ipatool": "ipatool.plist"}
#: What ipatool.plist grants, exactly.
IPATOOL_ENTITLEMENTS = {"com.apple.security.cs.allow-jit": True,
                        "com.apple.security.cs.disable-library-validation": True}

#: The Go module proxy and checksum database `go mod vendor` downloads from;
#: every module is also checked against ipatool's own go.sum.
GOPROXY = "https://proxy.golang.org"
GOSUMDB = "sum.golang.org"


def ipatool_steps(version: str) -> list[list[str]]:
    """Run in ipatool's source, after its patch, with go_env(IPATOOL_ENV). The
    first copies the modules into vendor/ (from the module cache, downloading
    what it lacks, each checked against go.sum); the second re-hashes what
    the cache holds of them (GO_VERIFY); the third builds offline from
    vendor/ alone, with no build path recorded."""
    return [
        ["go", "mod", "vendor"],
        GO_VERIFY,
        ["go", "build", "-mod=vendor", "-trimpath", "-buildvcs=false",
         f"-ldflags=-X github.com/majd/ipatool/v2/cmd.version={version}", "-o", "ipatool", "."],
    ]


#: After `go mod vendor`. The module cache is shared between builds and
#: checkouts (go_modcache), and Go checks a cached module only by the hash
#: it recorded when it downloaded it (the `.ziphash`, against go.sum), never
#: the zip or the unpacked files `go mod vendor` copies. This re-hashes both
#: against that record, so a cached module altered since its download stops
#: the build, as a fresh download that failed go.sum would.
GO_VERIFY = ["go", "mod", "verify"]


#: Every Go build here: Go told to use exactly the pinned toolchain, and only
#: the module proxy and checksum database above.
GO_HERMETIC = {
    "GOTOOLCHAIN": "local", "GOFLAGS": "", "GOWORK": "off", "GOENV": "off",
    "GOPROXY": GOPROXY, "GOSUMDB": GOSUMDB, "GONOSUMDB": "", "GOPRIVATE": "", "GOINSECURE": "",
}

#: ipatool's build settings, as upstream's release workflow has them (cgo on)
#: but for the oldest macOS Graft supports.
IPATOOL_ENV = {
    "CGO_ENABLED": "1", "CC": "clang", "CGO_CFLAGS": f"-mmacosx-version-min={MACOS_MIN}",
    "CGO_LDFLAGS": f"-mmacosx-version-min={MACOS_MIN}", "GOOS": "darwin", "GOARCH": "arm64", **GO_HERMETIC,
}


def go_modcache(cache: Path | None = None) -> Path:
    """The Go module cache every build here shares (its download cache is
    its cache/download/): in the downloads cache (`cache`, CACHE unless
    given), so a release fetches each module once per machine, not once per
    build. Go makes it read-only; `chmod -R u+w` it before deleting it by
    hand. CACHE is read at call time, so a test can point it elsewhere."""
    return (cache or CACHE) / "go-modcache"


def go_env(go: Path, tmp: Path, settings: dict[str, str], modcache: Path | None = None) -> dict[str, str]:
    """_build_env(), plus `go`'s bin first on PATH, a build's `settings`
    (IPATOOL_ENV, UNFAIRPLAY_ENV), the module cache `modcache` (go_modcache()
    unless given), and Go's other state, GOPATH (where the checksum
    database's latest tree head is kept), the build cache and HOME, in
    `tmp`: never the release machine's own."""
    env = {**_build_env(), **settings}
    env["PATH"] = f"{go / 'bin'}:{env['PATH']}"
    for var, d in (("GOPATH", "gopath"), ("GOCACHE", "gocache"), ("HOME", "home")):
        (tmp / d).mkdir(exist_ok=True)
        env[var] = str(tmp / d)
    modcache = modcache or go_modcache()
    modcache.mkdir(parents=True, exist_ok=True)
    env["GOMODCACHE"] = str(modcache)
    return env


def _module_dir(module: str) -> str:
    """A module path as the module cache spells it: each capital letter as "!x"."""
    return re.sub(r"[A-Z]", lambda m: "!" + m[0].lower(), module)


def _go_modules_record(modcache: Path, pin: Pin) -> Path:
    """Where a Go tool's build records the modules it vendored from
    `modcache`, by the source built (its pin's hash and patches): the plan
    (download_plan) reads it to tell whether a build would download any. In
    the cache itself, so whatever removes the cache removes it too."""
    source = hashlib.sha256(json.dumps([pin.sha256, patch_hashes(pin.name)]).encode()).hexdigest()
    return modcache / ".graft-release" / f"{pin.name}-{source[:16]}.txt"


def _module_zip(modcache: Path, module: str, version: str) -> Path:
    return modcache / "cache" / "download" / _module_dir(module) / "@v" / f"{version}.zip"


def record_go_modules(modcache: Path, pin: Pin, modules_txt: Path) -> None:
    """After a Go tool's modules are vendored and verified: each module
    vendor/modules.txt names whose zip the cache holds, one `<module>
    <version>` per line. (modules.txt also names requirements no package
    came from, which Go never downloads.)"""
    modules = sorted(f"{m} {v}" for m, v in _modules_txt_modules(modules_txt.read_text())
                     if _module_zip(modcache, m, v).is_file())
    record = _go_modules_record(modcache, pin)
    record.parent.mkdir(exist_ok=True)
    record.write_text("".join(f"{m}\n" for m in modules))


def _modules_txt_modules(text: str) -> set[tuple[str, str]]:
    """(module, version) of each `# module version` line of vendor/modules.txt
    (a replacement counts as the module it is replaced with)."""
    found = set()
    for line in text.splitlines():
        if line.startswith("# "):
            old, _, new = line[2:].partition(" => ")
            module = new.split() if new else old.split()
            if len(module) == 2:
                found.add((module[0], module[1]))
    return found


def go_modules_cached(modcache: Path, pin: Pin) -> bool:
    """Whether a build of `pin` would find every module it needs in
    `modcache`: an earlier build of the same source recorded them, and each
    one's zip is still there."""
    record = _go_modules_record(modcache, pin)
    if not record.is_file():
        return False
    return all(_module_zip(modcache, m, v).is_file()
               for m, v in (line.split() for line in record.read_text().splitlines() if line.strip()))


#: A Go module's or Rust crate's license and notice files, at its root, by
#: name (`is_licence_name`).
_MODULE_LICENCE = re.compile(r"(UN)?LICEN[CS]E|COPYING|COPYLEFT|NOTICE|PATENTS|COPYRIGHT|LEGAL", re.IGNORECASE)
#: Source code, whatever its name starts with: go-sdk's copyright_test.go is
#: a test, not a notice (RC audit #6).
_SOURCE_CODE = re.compile(r"\.(go|rs|c|cc|cpp|h|hpp|s|py|sh|js|ts)$", re.IGNORECASE)


def is_licence_name(name: str) -> bool:
    """Whether a file at a module's or crate's root is a license or notice file, by its name."""
    return bool(_MODULE_LICENCE.match(name)) and not _SOURCE_CODE.search(name)


def _not_licence_files(folder: str, names: list[str]) -> set[str]:
    """For copytree: drop the files a cached modules folder holds that aren't
    license files (`is_licence_name`), kept by an earlier rule."""
    return {n for n in names if (Path(folder) / n).is_file() and not is_licence_name(n)}


def go_version_m(go: Path, binary: Path, env: dict[str, str]) -> str:
    """A Go binary's build info, as `go version -m` reads it from the binary itself."""
    return subprocess.run([str(go / "bin" / "go"), "version", "-m", str(binary)], capture_output=True, text=True,
                          env=env, check=True).stdout


def compiled_modules(go: Path, binary: Path, env: dict[str, str]) -> list[tuple[str, str]]:
    """(module, version) of every module compiled into a Go binary."""
    return go_deps(go_version_m(go, binary, env))


def go_build_info(version_m: str) -> dict[str, str]:
    """`go version -m`'s answer as name -> value: `go` (the toolchain),
    `path` (the main package), `mod` (its module and version) and each
    build setting by its key (`-trimpath`, `CGO_ENABLED`, `vcs.revision`...)."""
    lines = version_m.splitlines()
    info = {"go": lines[0].rsplit(": ", 1)[-1].strip()} if lines else {}
    for line in lines[1:]:
        f = line.split("\t")[1:]
        if f[:1] == ["path"] and len(f) > 1:
            info["path"] = f[1]
        elif f[:1] == ["mod"] and len(f) > 2:
            info["mod"] = f"{f[1]} {f[2]}"
        elif f[:1] == ["build"] and len(f) > 1:
            key, _, value = f[1].partition("=")
            info[key] = value
    return info


def go_deps(version_m: str) -> list[tuple[str, str]]:
    """(module, version) of each `dep` line `go version -m` printed, with a
    `=>` replacement taken as the module actually compiled in."""
    found = []
    lines = version_m.splitlines()
    for i, line in enumerate(lines):
        f = line.split()
        if f and f[0] == "dep":
            nxt = lines[i + 1].split() if i + 1 < len(lines) else []
            found.append((nxt[1], nxt[2]) if nxt[:1] == ["=>"] else (f[1], f[2]))
    return found


def _is_licence_text(data: bytes) -> bool:
    """A file that states a license, not one that only points elsewhere."""
    text = data.decode(errors="replace").lower()
    return len(text) >= 100 and any(w in text for w in (
        "permission", "license", "licence", "copyright", "public domain", "unlicense", "warranty"))


def module_licences(modcache: Path, modules: list[tuple[str, str]], dest: Path) -> dict[str, list[str]]:
    """Copy each module's license files from the module cache into
    dest/<module>@<version>/. A module with none that holds license text is
    refused: its notice would be missing from the download."""
    found: dict[str, list[str]] = {}
    for module, version in modules:
        root = modcache / f"{_module_dir(module)}@{version}"
        files = sorted(f for f in root.iterdir() if f.is_file() and is_licence_name(f.name)) \
            if root.is_dir() else []
        if not any(_is_licence_text(f.read_bytes()) for f in files):
            raise BundleError(f"Go module {module} {version} has no license file at its root "
                              f"({', '.join(f.name for f in files) or 'none'})")
        out = dest / f"{module}@{version}"
        out.mkdir(parents=True)
        for f in files:
            shutil.copy2(f, out / f.name)
        found[f"{module}@{version}"] = [f.name for f in files]
    return found


#: The Go programs in Helpers/bin, each built here from source with its
#: modules vendored (build_ipatool, build_unfairplay).
GO_TOOLS = ("ipatool", "unfairplay")


def vendored_modules(vendor_tar: Path) -> set[tuple[str, str]]:
    """(module, version) of each module whose source a vendored tree
    (`go mod vendor`'s vendor/, as vendor.tar.gz) holds: listed in its
    modules.txt, with files under vendor/<its path>/. A replaced module
    counts as its replacement too, which Go vendors under the original's path."""
    with tarfile.open(vendor_tar) as tf:
        files = [m.name for m in tf.getmembers() if m.isfile()]
        listed = tf.extractfile("vendor/modules.txt").read().decode() if "vendor/modules.txt" in files else ""
    held = set()
    for line in listed.splitlines():
        if not line.startswith("# "):
            continue
        old, _, new = line[2:].partition(" => ")
        path = old.split()[0]
        if not any(f.startswith(f"vendor/{path}/") for f in files):
            continue
        for module in (old.split(), new.split()):
            if len(module) == 2:
                held.add((module[0], module[1]))
    return held


def go_module_problems(modules: list[tuple[str, str]], vendor_tar: Path, notices: Path) -> list[str]:
    """Each (module, version) compiled into a Go binary (from its own build
    info) that lacks what the release owes it: its source in `vendor_tar`,
    the vendored tree the source archive carries, or a license text in
    `notices`/<module>@<version>/. The stage refuses a build with any; so
    does verify-bundle, for the binaries in the app and their Licenses/."""
    held = vendored_modules(vendor_tar)
    problems = []
    for module, version in modules:
        if (module, version) not in held:
            problems.append(f"{module} {version}: no source in {vendor_tar.name}")
        folder = notices / f"{module}@{version}"
        texts = [f for f in folder.iterdir() if f.is_file() and is_licence_name(f.name)] if folder.is_dir() else []
        if not any(_is_licence_text(f.read_bytes()) for f in texts):
            problems.append(f"{module} {version}: no license notice in {notices.name}/")
    return problems


#: A license text's copyleft family, by its title: the SPDX prefix a license
#: expression names it by. The LGPL's text names the GPL too, so the
#: narrower families come first.
_COPYLEFT_TITLES = (("AGPL", "GNU AFFERO GENERAL PUBLIC LICENSE"), ("LGPL", "GNU LESSER GENERAL PUBLIC LICENSE"),
                    ("LGPL", "GNU LIBRARY GENERAL PUBLIC LICENSE"), ("GPL", "GNU GENERAL PUBLIC LICENSE"),
                    ("MPL", "MOZILLA PUBLIC LICENSE"), ("EPL", "ECLIPSE PUBLIC LICENSE"),
                    ("EUPL", "EUROPEAN UNION PUBLIC LICENCE"))


#: What may follow a copyleft title on its line further down a file: its
#: version ("Version 3", "- v 2.0", "v. 1.2"), as a title line has it.
_TITLE_VERSION = re.compile(r",?\s*(?:-\s*)?(?:VERSION|V\.?)\s*\d")


def licence_text_family(text: str) -> str | None:
    """The copyleft family a license text's title names, or None for any
    other license. The title is looked for in the first 800 characters,
    where a license text puts it, and then on any line that starts with it,
    in capitals or with its version after it, as a text after a long
    preamble has it (the delta audit's d14-bundle-2: retry.v1's title sits at
    129, but one past 800 was missed). Never anywhere in the text: MPL-2.0's
    own names the GPL, the LGPL and the AGPL in its body, and the LGPL's
    names the GPL."""
    head = " ".join(text.split())[:800].upper()
    found = next((family for family, title in _COPYLEFT_TITLES if title in head), None)
    if found is not None:
        return found
    for line in text.splitlines():
        words = " ".join(line.split())
        upper = words.upper()
        for family, title in _COPYLEFT_TITLES:
            if upper.startswith(title) and (words == upper or _TITLE_VERSION.match(upper, len(title))):
                return family
    return None


#: Words only a permissive license's text has: what lets a license file
#: beside a copyleft one count as a choice (`permissive_text`).
_PERMISSIVE_TEXTS = (("MIT", "permission is hereby granted, free of charge"),
                     ("BSD", "redistribution and use in source and binary forms"),
                     ("Apache-2.0", "apache license version 2.0"), ("Apache-2.0", "apache license, version 2.0"),
                     ("ISC", "permission to use, copy, modify, and/or distribute this software for any purpose"),
                     ("ISC", "permission to use, copy, modify, and distribute this software for any purpose"),
                     ("Unlicense", "this is free and unencumbered software released into the public domain"),
                     ("CC0-1.0", "cc0 1.0 universal"), ("BSL-1.0", "boost software license"),
                     ("Zlib", "altered source versions must be plainly marked as such"))


def permissive_text(text: str) -> str | None:
    """The permissive license a text is, by words only its text has, or
    None: for a copyleft text, and for one nothing here recognizes, which
    can't be called permissive."""
    if licence_text_family(text) is not None:
        return None
    flat = " ".join(text.split()).lower()
    return next((name for name, words in _PERMISSIVE_TEXTS if words in flat), None)


#: The files at a module's root that can offer a license of their own
#: (LICENSE-MIT beside a GPL LICENSE): a NOTICE, PATENTS, COPYRIGHT, LEGAL
#: or COPYLEFT file adds to a module's license, never offers another one.
_LICENCE_CHOICE = re.compile(r"(UN)?LICEN[CS]E|COPYING", re.IGNORECASE)


def licence_names(family: str, licence: str) -> bool:
    """Whether an SPDX expression names a license of `family` (GPL, not
    LGPL) in every choice it offers, read clause by clause
    (`licence_expression`): "MIT OR GPL-3.0-only" lets the GPL be chosen
    away, so it doesn't. One that doesn't parse names nothing."""
    tree = licence_expression(licence)
    return tree is not None and not licence_offers(
        tree, lambda lic: re.search(rf"(?<![A-Za-z]){family}(?![A-Za-z])", lic) is None)


def go_copyleft_problems(notices: Path, licence: str) -> list[str]:
    """Each Go module in `notices` (<module>@<version>/, its license files)
    under a copyleft license with no permissive choice, which `licence`, the
    expression components.json gives its tool, doesn't name in every choice
    it offers (review 2.0, bundle #9). Go modules state no SPDX license, so
    the text tells. Their source is in the source archive whatever the
    license (go_module_problems).

    A choice is a license file (`_LICENCE_CHOICE`) whose text is a
    permissive license's (`permissive_text`). Any other file with license
    text used to count as one, so a GPL module with a NOTICE, COPYRIGHT or
    PATENTS file passed (the delta audit's d14-bundle-2)."""
    problems = []
    for folder in sorted({f.parent for f in notices.rglob("*") if f.is_file() and "@" in f.parent.name}):
        texts = [(g.name, g.read_text(errors="replace")) for g in folder.iterdir()
                 if g.is_file() and is_licence_name(g.name) and _is_licence_text(g.read_bytes())]
        families = {family for _, t in texts if (family := licence_text_family(t)) is not None}
        if not families or any(_LICENCE_CHOICE.match(name) and permissive_text(t) for name, t in texts):
            continue
        for family in sorted(families):
            if not licence_names(family, licence):
                named = re.search(rf"(?<![A-Za-z]){family}(?![A-Za-z])", licence) is not None
                problems.append(f"{folder.relative_to(notices).as_posix()}: {family}, which its tool's license "
                                f"({licence}) " + ("names only as one choice" if named else "doesn't name"))
    return problems


def _check_go_copyleft(tool: str, notices: Path, licence: str) -> None:
    problems = go_copyleft_problems(notices, licence)
    if problems:
        raise BundleError(f"{tool} holds Go modules under a copyleft license its pin's license doesn't name: "
                          + "; ".join(problems))


def check_go_modules(name: str, modules: list[tuple[str, str]], out: Path) -> None:
    """A Go build's `out` (vendor.tar.gz, modules/) must cover every module
    compiled into it."""
    problems = go_module_problems(modules, out / "vendor.tar.gz", out / "modules")
    if problems:
        raise BundleError(f"{name}: a module it holds has no notice or no source to ship: " + "; ".join(problems))


def deterministic_tar_gz(src: Path, dest: Path, prefix: str, rename: Callable[[str], str] = str) -> None:
    """`src` as a .tar.gz with sorted members under `prefix`, no owner and no
    time: the same tree always gives the same bytes. `rename` gives each
    member's path (relative to `src`) as it goes in."""
    members = sorted(((rename(p.relative_to(src).as_posix()), p) for p in src.rglob("*")), key=lambda m: m[0])
    names = [name for name, _ in members]
    if len(set(names)) != len(names):
        raise BundleError(f"{src.name}: two members would have one name in {dest.name}")
    with dest.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, p in members:
            if p.is_symlink():
                raise BundleError(f"{p.relative_to(src)}: a symlink in the vendored modules")
            info = tf.gettarinfo(str(p), f"{prefix}/{name}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            info.mode = 0o755 if p.is_dir() else 0o644
            info.pax_headers = {}
            with (p.open("rb") if p.is_file() else io.BytesIO()) as f:
                tf.addfile(info, f if p.is_file() else None)


def _vendored_paths(modules_txt: str) -> list[str]:
    """Every module and package path vendor/modules.txt names, as Go spells
    them, longest first. Two that differ only in case are refused: a Mac's
    disk holds them in one folder, so one isn't what Go vendored."""
    paths = set()
    for line in modules_txt.splitlines():
        if line.startswith("# "):
            old = line[2:].partition(" => ")[0].split()
            paths.update(old[:1])
        elif line.strip() and not line.startswith("#"):
            paths.add(line.strip())
    by_case: dict[str, set[str]] = {}
    for path in paths:
        by_case.setdefault(path.lower(), set()).add(path)
    clash = sorted(sorted(group) for group in by_case.values() if len(group) > 1)
    if clash:
        raise BundleError(f"vendor/modules.txt names one path two ways: {clash[0][0]} and {clash[0][1]}")
    return sorted(paths, key=lambda p: (-len(p), p))


def vendor_tar_gz(vendor: Path, dest: Path) -> None:
    """A `go mod vendor` tree as deterministic_tar_gz writes it, with each
    path spelled as vendor/modules.txt spells it. On a Mac's case-insensitive
    disk, modules whose paths differ only in case share one folder (ipatool's
    github.com/1password/onepassword-sdk-go lands in the github.com/1Password/
    that connect-sdk-go made), which a case-sensitive disk keeps apart: the
    archive must, or a rebuild from it there can't find the module."""
    canonical = _vendored_paths((vendor / "modules.txt").read_text())

    def spelled(rel: str) -> str:
        low = rel.lower()
        for path in canonical:
            if low == path.lower() or low.startswith(path.lower() + "/"):
                return path + rel[len(path):]
        return rel
    deterministic_tar_gz(vendor, dest, "vendor", spelled)


def build_ipatool(pin: Pin, go_pin: Pin, work: Path = WORK) -> Path:
    """ipatool, from its pinned source (and its patches, if any), with the pinned Go: a
    directory holding `ipatool`, `vendor.tar.gz` (its modules as `go mod
    vendor` wrote them, for the source archive), `modules/` (each compiled-in
    module's license files), `LICENSE` (ipatool's) and Go's LICENSE and
    PATENTS as `go-LICENSE` and `go-PATENTS`."""
    def build(tmp: Path, out: Path) -> None:
        go = extract(fetch(go_pin), tmp / "go") / "go"
        src = _unpack(pin, tmp)
        apply_patches("ipatool", src)
        modcache = go_modcache()
        env = go_env(go, tmp, IPATOOL_ENV, modcache)
        for step in ipatool_steps(pin.version):
            subprocess.run(step, cwd=src, env=env, check=True)
        record_go_modules(modcache, pin, src / "vendor" / "modules.txt")
        shutil.copy2(src / "ipatool", out / "ipatool")
        check_no_shell(out / "ipatool", "ipatool")
        vendor_tar_gz(src / "vendor", out / "vendor.tar.gz")
        modules = compiled_modules(go, out / "ipatool", env)
        module_licences(modcache, modules, out / "modules")
        check_go_modules("ipatool", modules, out)
        shutil.copy2(src / "LICENSE", out / "LICENSE")
        for f in ("LICENSE", "PATENTS"):
            shutil.copy2(go / f, out / f"go-{f}")
    return _cached_build("ipatool", ipatool_key(pin, go_pin), work, build)


def ipatool_key(pin: Pin, go_pin: Pin) -> str:
    """build_ipatool's cache key."""
    return build_key(pin.sha256, go_pin.sha256, ipatool_steps(pin.version), IPATOOL_ENV, patch_hashes("ipatool"),
                     toolchain_id())


def stage_ipatool(pins: dict[str, Pin], contents: Path, licences: Path, work: Path = WORK) -> Path:
    """ipatool into Helpers/bin; its licenses into `licences`: ipatool's,
    Go's, and every compiled-in Go module's under ipatool-modules/. No
    Unicorn: ipatool downloads that itself. Returns build_ipatool's directory."""
    built = build_ipatool(pins["ipatool"], pins["go"], work)
    bindir = contents / "Helpers" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built / "ipatool", bindir / "ipatool")
    (bindir / "ipatool").chmod(0o755)
    _check_system_only(bindir / "ipatool", contents)
    shutil.copy2(built / "LICENSE", licences / "ipatool-LICENSE")
    for f in ("go-LICENSE", "go-PATENTS"):
        shutil.copy2(built / f, licences / f)
    # ipatool's build is cached, its modules/ with it: filtered here, so a
    # build cached before is_licence_name still ships only license files.
    shutil.copytree(built / "modules", licences / "ipatool-modules", ignore=_not_licence_files)
    _check_go_copyleft("ipatool", licences / "ipatool-modules", pins["ipatool"].license)
    return built


# ------------------------------------------------- UnfairPlay, from source

#: UnfairPlay runs as root in Set Up Decryption, so it is built here from its
#: pinned source, with the pinned Go, rather than taken prebuilt (owner,
#: 2026-11-23). Its module and the package its binary is built from, as the
#: binary's Go build info names them (verify-bundle checks it).
UNFAIRPLAY_MODULE = "github.com/DrWhax/unfairplay"
UNFAIRPLAY_PACKAGE = f"{UNFAIRPLAY_MODULE}/cmd/unfairplay"
#: Its license and notice files, at its source's top, as its release carries them.
UNFAIRPLAY_LICENCES = ("LICENSE", "NOTICE", "LICENSE-DARKPHOENIX.txt", "LICENSE-MACOSFAIRFREE.txt")

#: UnfairPlay's build settings, as its release workflow has them (no cgo),
#: and git told to read no configuration of the release machine's (no
#: credential helper: nothing that could ask for a password or a keychain)
#: and never to prompt.
UNFAIRPLAY_ENV = {"CGO_ENABLED": "0", "GOOS": "darwin", "GOARCH": "arm64", **GO_HERMETIC,
                  "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}


def unfairplay_steps(version: str, repo: str, commit: str) -> list[list[str]]:
    """Run in the pinned archive's top directory with go_env(UNFAIRPLAY_ENV).

    Upstream's release builds in a depth-1 checkout of the tag, so its
    binary's Go build info carries the commit (vcs.revision) and the tag's
    version. Here the files are the pinned archive's own: git fetches only the
    tag's commit from `repo` and makes it HEAD and the index, without
    touching them, so Go stamps that revision, and says vcs.modified=true if
    the archive isn't exactly the commit's tree (unfairplay_build_problems
    refuses that). vendor/, which `go mod vendor` writes (each module checked
    against go.sum and the checksum database, then re-hashed in the module
    cache by GO_VERIFY), is kept out of git's status. The build is
    upstream's (.github/workflows/release.yml: `-trimpath`, `-ldflags "-s -w
    -X main.version=<tag>"`, ./cmd/unfairplay), offline from vendor/ alone."""
    tag = f"refs/tags/v{version}"
    return [
        ["git", "init", "-q"],
        ["git", "-c", "credential.helper=", "fetch", "-q", "--depth=1", "--no-tags", repo, f"+{tag}:{tag}"],
        ["git", "update-ref", "--no-deref", "HEAD", commit],
        ["git", "read-tree", "HEAD"],
        ["/bin/sh", "-c", "echo /vendor/ >> .git/info/exclude"],
        ["go", "mod", "vendor"],
        GO_VERIFY,
        ["go", "build", "-mod=vendor", "-trimpath", "-buildvcs=true",
         f"-ldflags=-s -w -X main.version=v{version}", "-o", "unfairplay", "./cmd/unfairplay"],
    ]


def unfairplay_build_problems(version_m: str, pin: Pin, go_pin: Pin) -> list[str]:
    """What is wrong with a built UnfairPlay's Go build info (`go version -m`)
    for the pinned source built as unfairplay_steps does: the pinned Go (not
    upstream's go1.26.0), the pinned module at its tag and commit, unmodified,
    and upstream's settings. The stage refuses a build with any; so does
    verify-bundle, for the binary in the app."""
    want = {"go": f"go{go_pin.version}", "path": UNFAIRPLAY_PACKAGE, "mod": f"{UNFAIRPLAY_MODULE} v{pin.version}",
            "-trimpath": "true", **{k: UNFAIRPLAY_ENV[k] for k in ("CGO_ENABLED", "GOOS", "GOARCH")},
            "vcs": "git", "vcs.revision": source_commit(pin)[1], "vcs.modified": "false"}
    info = go_build_info(version_m)
    return [f"{key}: {info[key] if key in info else 'not recorded'}, not {value}"
            for key, value in want.items() if info.get(key) != value]


def archive_commit(archive: Path) -> str | None:
    """The commit a GitHub `archive/<commit>.tar.gz` is of: `git archive`
    writes it as the pax global header's comment (`git get-tar-commit-id`)."""
    with tarfile.open(archive) as tf:
        return tf.pax_headers.get("comment")


def source_commit(pin: Pin) -> tuple[str, str]:
    """(git repository, commit) of a pin that is GitHub's archive of one commit."""
    m = re.fullmatch(r"(https://[^/]+/[^/]+/[^/]+)/archive/([0-9a-f]{40})\.tar\.gz", pin.url)
    if m is None:
        raise BundleError(f"{pin.name}'s pin names no commit: {pin.url} is not an archive/<commit>.tar.gz")
    return f"{m[1]}.git", m[2]


def check_archive_commit(pin: Pin, archive: Path) -> str:
    """The commit `pin`'s URL names, which its archive must say it is: so the
    revision built is the one pinned, not only the bytes."""
    commit = source_commit(pin)[1]
    got = archive_commit(archive)
    if got != commit:
        raise BundleError(f"{pin.name}: {archive.name} is the archive of {got or 'no commit'}, not of {commit}, "
                          "the commit its pin names")
    return commit


def build_unfairplay(pin: Pin, go_pin: Pin, work: Path = WORK) -> Path:
    """UnfairPlay, from its pinned source, unpatched, with the pinned Go: a
    directory holding `unfairplay`, `vendor.tar.gz` (its modules as `go mod
    vendor` wrote them, for the source archive), `modules/` (each
    compiled-in module's license files) and its UNFAIRPLAY_LICENCES."""
    def build(tmp: Path, out: Path) -> None:
        check_archive_commit(pin, fetch(pin))
        go = extract(fetch(go_pin), tmp / "go") / "go"
        src = _unpack(pin, tmp)
        modcache = go_modcache()
        env = go_env(go, tmp, UNFAIRPLAY_ENV, modcache)
        for step in unfairplay_steps(pin.version, *source_commit(pin)):
            subprocess.run(step, cwd=src, env=env, check=True)
        record_go_modules(modcache, pin, src / "vendor" / "modules.txt")
        shutil.copy2(src / "unfairplay", out / "unfairplay")
        version_m = go_version_m(go, out / "unfairplay", env)
        problems = unfairplay_build_problems(version_m, pin, go_pin)
        if problems:
            raise BundleError("the built unfairplay's build info isn't the pinned source's, built as planned "
                              "(vcs.modified: the pinned archive isn't the commit's tree): " + "; ".join(problems))
        check_no_shell(out / "unfairplay", "unfairplay")
        vendor_tar_gz(src / "vendor", out / "vendor.tar.gz")
        module_licences(modcache, go_deps(version_m), out / "modules")
        check_go_modules("unfairplay", go_deps(version_m), out)
        for f in UNFAIRPLAY_LICENCES:
            shutil.copy2(src / f, out / f)
    return _cached_build("unfairplay", unfairplay_key(pin, go_pin), work, build)


def unfairplay_key(pin: Pin, go_pin: Pin) -> str:
    """build_unfairplay's cache key."""
    return build_key(pin.sha256, go_pin.sha256, unfairplay_steps(pin.version, *source_commit(pin)), UNFAIRPLAY_ENV,
                     toolchain_id())


def stage_unfairplay(pins: dict[str, Pin], contents: Path, licences: Path, work: Path = WORK) -> Path:
    """UnfairPlay into Helpers/bin, where the release signs it with identifier
    `unfairplay` (the root step's requirement); its license and notice files
    into `licences` as `unfairplay-<file>`, and every compiled-in Go module's
    under unfairplay-modules/. Go's own are stage_ipatool's. Returns
    build_unfairplay's directory."""
    built = build_unfairplay(pins["unfairplay"], pins["go"], work)
    bindir = contents / "Helpers" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(built / "unfairplay", bindir / "unfairplay")
    (bindir / "unfairplay").chmod(0o755)
    _check_system_only(bindir / "unfairplay", contents)
    for f in UNFAIRPLAY_LICENCES:
        shutil.copy2(built / f, licences / f"unfairplay-{f}")
    shutil.copytree(built / "modules", licences / "unfairplay-modules", ignore=_not_licence_files)
    _check_go_copyleft("unfairplay", licences / "unfairplay-modules", pins["unfairplay"].license)
    return built


def build_cyan_helpers(pins: dict[str, Pin], openssl: Path, work: Path = WORK,
                       licences: Path | None = None) -> dict[str, Path]:
    """Each of cyan's five helpers, and libplist's dylib, built from their
    pins (outside any .app, cached by source hash): name -> built file.
    `licences`, if given, gets each source's license files as `<pin>-<file>`."""
    plist = build_libplist(pins["libplist"], work)
    ldid = build_ldid(pins["ldid"], plist, openssl, work)
    cctools = build_cctools(pins["cctools"], work)
    idylib = build_insert_dylib(pins["insert_dylib"], work)
    built = {"ldid": ldid / "ldid", "insert_dylib": idylib / "insert_dylib", "libplist": plist / LIBPLIST,
             **{tool: cctools / tool for tool in pins["cctools"].builds}}
    if licences is not None:
        for name, out in (("libplist", plist), ("ldid", ldid), ("cctools", cctools), ("insert_dylib", idylib)):
            for f in HELPER_LICENCES[name]:
                shutil.copy2(out / f, licences / f"{name}-{f}")
    return built


def ldid_link_plan(ldid: Path, contents: Path) -> list[tuple[str, str]]:
    """(old, new) install names that point the built ldid at Frameworks'
    libcrypto and libplist, relative to where ldid sits (@loader_path: cyan
    runs it by its path inside the app). Anything else ldid links that isn't
    the system's is refused: the bundle doesn't carry it."""
    to_fw = f"@loader_path/{os.path.relpath(contents / 'Frameworks', ldid.parent)}"
    plan = []
    for name in macho.dylib_names(ldid):
        leaf = name.rsplit("/", 1)[-1]
        if name.startswith(SYSTEM_LIBS):
            continue
        if leaf == LIBCRYPTO:
            new = f"{to_fw}/{LIBCRYPTO}"
        elif re.fullmatch(r"libplist-2\.0(\.\d+)*\.dylib", leaf):
            new = f"{to_fw}/{LIBPLIST}"
        else:
            raise BundleError(f"ldid links {name}, which the bundle doesn't carry")
        if name != new:
            plan.append((name, new))
    return plan


def check_ldid_links(ldid: Path, contents: Path) -> None:
    """The staged ldid links the system's libraries and Frameworks' libcrypto
    and libplist, and nothing else."""
    to_fw = f"@loader_path/{os.path.relpath(contents / 'Frameworks', ldid.parent)}"
    want = {f"{to_fw}/{LIBCRYPTO}", f"{to_fw}/{LIBPLIST}"}
    names = macho.dylib_names(ldid)
    other = [n for n in names if n not in want and not n.startswith(SYSTEM_LIBS)]
    missing = sorted(want - set(names))
    if other or missing:
        raise BundleError(f"the staged ldid links {other or 'nothing unexpected'} and lacks {missing or 'nothing'}")


def _check_system_only(path: Path, contents: Path) -> None:
    other = [n for n in macho.dylib_names(path) if not n.startswith(SYSTEM_LIBS)]
    if other:
        raise BundleError(f"{path.relative_to(contents)} links {', '.join(other)}, which the bundle doesn't carry")


def cyan_original_hashes(archive: Path) -> dict[str, str]:
    """name -> SHA-256 of each prebuilt helper cyan's pinned archive carries
    in cyan/tools/Darwin/arm64: the bytes check_originals_gone() keeps out of
    the stage. All five must be there, or that check would be empty."""
    found = {}
    with zipfile.ZipFile(archive) as z:
        for n in z.namelist():
            parts = n.split("/")
            if parts[-1] in CYAN_HELPERS and parts[-5:-1] == ["cyan", "tools", "Darwin", "arm64"]:
                found[parts[-1]] = hashlib.sha256(z.read(n)).hexdigest()
    missing = [n for n in CYAN_HELPERS if n not in found]
    if missing:
        raise BundleError(f"{archive.name} has no cyan/tools/Darwin/arm64/{', '.join(missing)}")
    return found


def check_originals_gone(contents: Path, originals: dict[str, str]) -> None:
    """Refuse a stage holding any of cyan's prebuilt helpers, by their bytes,
    anywhere and under any name."""
    by_hash = {sha: name for name, sha in originals.items()}
    left = [f"{p.relative_to(contents)} (cyan's prebuilt {by_hash[sha]})" for p in sorted(contents.rglob("*"))
            if p.is_file() and not p.is_symlink() and (sha := _sha256(p)) in by_hash]
    if left:
        raise BundleError(f"cyan's prebuilt helpers are still in the stage: {', '.join(left)}")


def stage_cyan_helpers(built: dict[str, Path], contents: Path, originals: dict[str, str]) -> None:
    """The builds in place of cyan's five helpers, under the names cyan runs;
    libplist into Frameworks; ldid pointed at Frameworks' libcrypto and
    libplist. Then nothing of cyan's prebuilt five may be left."""
    fw = contents / "Frameworks"
    fw.mkdir(parents=True, exist_ok=True)
    lib = fw / LIBPLIST
    shutil.copy2(built["libplist"], lib)
    _run(INSTALL_NAME_TOOL, "-id", f"@rpath/{LIBPLIST}", lib)
    _check_system_only(lib, contents)
    tools = contents / CYAN_TOOLS
    for name in CYAN_HELPERS:
        dest = tools / name
        if not dest.is_file():
            raise BundleError(f"cyan has no {CYAN_TOOLS / name} to replace, and that is where it runs {name} from")
        dest.unlink()
        shutil.copy2(built[name], dest)
        dest.chmod(0o755)
        if name != "ldid":
            _check_system_only(dest, contents)
    ldid = tools / "ldid"
    plan = ldid_link_plan(ldid, contents)
    if plan:
        _run(INSTALL_NAME_TOOL, *(arg for old, new in plan for arg in ("-change", old, new)), ldid)
    check_ldid_links(ldid, contents)
    check_originals_gone(contents, originals)


# ------------------------------------------- PlayCover's Preferences stub

#: The stub's source, and where Graft.app carries it built, relative to
#: Contents: beside its builder, which playcover.build_stub tries first.
STUB_SOURCE = REPO / "graft_ipa" / "stubs" / "preferences-stub"
STUB_IN_APP = Path("Resources/GraftPython/lib/python3.13/site-packages/graft_ipa/stubs/preferences-stub/"
                   "Preferences.framework")


def stub_steps() -> list[list[str]]:
    """build.sh's compile, run in a copy of the stub's source. No `codesign`:
    it is iOS code, which the release never signs, and PlayCover signs it on
    install."""
    return [["xcrun", "-sdk", "iphoneos", "clang", "-arch", "arm64", "-dynamiclib",
             "-install_name", "@rpath/Preferences.framework/Preferences",
             "-framework", "UIKit", "-framework", "Foundation", "-mios-version-min=11.0",
             "-o", "Preferences.framework/Preferences", "stub.m"]]


def build_preferences_stub(work: Path | None = None) -> Path:
    """Preferences.framework, built with the release Mac's Xcode (its iOS
    SDK), cached by its source and steps."""
    def build(tmp: Path, out: Path) -> None:
        src = tmp / "src"
        src.mkdir()
        for f in ("stub.m", "Info.plist"):
            shutil.copy2(STUB_SOURCE / f, src / f)
        (src / "Preferences.framework").mkdir()
        _build_steps(stub_steps(), src)
        shutil.copy2(src / "Info.plist", src / "Preferences.framework" / "Info.plist")
        shutil.copytree(src / "Preferences.framework", out / "Preferences.framework")
    key = build_key(_sha256(STUB_SOURCE / "stub.m"), _sha256(STUB_SOURCE / "Info.plist"), stub_steps(), toolchain_id())
    return _cached_build("preferences-stub", key, work or WORK, build) / "Preferences.framework"


def stage_preferences_stub(contents: Path, work: Path | None = None) -> Path:
    staged = contents / STUB_IN_APP
    shutil.copytree(build_preferences_stub(work), staged)
    if not macho.is_ios(staged / "Preferences"):
        raise BundleError(f"{STUB_IN_APP}/Preferences isn't iOS code")
    return staged


# ------------------------------------------------------------ the Python stage

#: Removed from the staged Python: nothing in the bundle runs them. Paths
#: relative to GraftPython.
PRUNE = (
    "bin/pip*", "bin/idle*", "bin/pydoc*", "bin/2to3*", "bin/python3*-config",
    "include",
    # Tcl/Tk: its script libraries, its dylibs, and the one module linking them.
    "lib/tcl*", "lib/tk*", "lib/itcl*", "lib/thread*", "lib/libtcl*", "lib/libtk*",
    "lib/python3.13/lib-dynload/_tkinter*",
    # Pillow's Tk bridge: its init imports tkinter, so without Tk it can never
    # load. PIL imports it only from ImageTk, lazily.
    "lib/python3.13/site-packages/PIL/_imagingtk*",
    "lib/python3.13/ensurepip", "lib/python3.13/idlelib", "lib/python3.13/tkinter",
    "lib/python3.13/turtledemo", "lib/python3.13/test", "lib/python3.13/*/tests",
    "lib/python3.13/site-packages/pip", "lib/python3.13/site-packages/pip-*.dist-info",
    # cyan runs tools/<platform.system()>/<platform.machine()> (tbhutils.get_tools_dir),
    # which for this arm64-only Python on a Mac is always tools/Darwin/arm64. iOS
    # is chosen only when machine() names an iPhone or iPad.
    "lib/python3.13/site-packages/cyan/tools/Darwin/x86_64",
    "lib/python3.13/site-packages/cyan/tools/Darwin/iOS",
    "lib/python3.13/site-packages/cyan/tools/Linux",
    # What notarization might object to (the review's critic): CPython's LLVM
    # bitcode object, and remotezip2's stray top-level test package, a zip
    # inside the app. Nothing imports either.
    "lib/python3.13/config-3.13-*/python.o",
    "lib/python3.13/site-packages/test_data",
    # Man pages and pkg-config files: nothing at run time reads them, and
    # half of them were symlinks, which an unarchiver may write as files.
    "share", "lib/pkgconfig",
    # Upstream wheel content nothing in the app loads (RC audit #2, #7), about
    # 38 MB on disk. Wintun's Windows DLLs, with their header and WireGuard's
    # prebuilt-binaries license, which isn't open source: pytun_pmd3 loads
    # them only on win32 (wintun.py, which stays). jedi's type stubs, read
    # only by IPython's completer, which nothing in the app starts.
    # pycryptodome's self-test suite and other packages' own tests.
    # pykdebugparser's README GIFs, and pymobiledevice3's developer scripts
    # and docs theme, both installed as top-level folders nothing imports.
    # Notes for coding agents in typer and fastapi. IPython/testing stays:
    # IPython.core imports IPython.testing.skipdoctest.
    "lib/python3.13/site-packages/pytun_pmd3/wintun",
    "lib/python3.13/site-packages/jedi/third_party",
    "lib/python3.13/site-packages/Crypto/SelfTest",
    "lib/python3.13/site-packages/*/tests", "lib/python3.13/site-packages/*/*/tests",
    "lib/python3.13/site-packages/ifaddr/test_ifaddr.py",
    "lib/python3.13/site-packages/annotated_types/test_cases.py",
    "lib/python3.13/site-packages/gifs", "lib/python3.13/site-packages/misc",
    "lib/python3.13/site-packages/overrides",
    "lib/python3.13/site-packages/*/.agents",
)


#: The only thing kept in GraftPython/bin: the interpreter the launchers
#: exec. Every console script pip installed there (graft, cyan,
#: pymobiledevice3, ...) has a shebang hard-coded to the staging path, so it
#: would break once moved; the launchers in Contents/Helpers replace the two
#: the app uses. `python` and `python3` were symlinks nothing runs.
BIN_KEEP = {"python3.13"}


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()


def _is_windows_binary(path: Path) -> bool:
    """A PE file (MZ, and "PE\\0\\0" where its header says), or one named .dll or .exe."""
    if path.suffix.lower() in (".dll", ".exe"):
        return True
    with path.open("rb") as f:
        head = f.read(64)
        if len(head) < 64 or head[:2] != b"MZ":
            return False
        f.seek(int.from_bytes(head[0x3c:0x40], "little"))
        return f.read(4) == b"PE\0\0"


def windows_binaries(root: Path) -> list[Path]:
    """Every Windows binary under `root`: nothing on a Mac loads one."""
    return sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink() and _is_windows_binary(p))


def prune_python(py: Path) -> None:
    for p in (py / "bin").iterdir():
        if p.name not in BIN_KEEP:
            _remove(p)
    for pattern in PRUNE:
        for p in py.glob(pattern):
            _remove(p)
    links = sorted(str(p.relative_to(py)) for p in py.rglob("*") if p.is_symlink())
    if links:
        raise BundleError(f"symlinks left in GraftPython, which an unarchiver may write as files: {', '.join(links)}")
    # RC audit #2: a wheel's Windows DLLs came under a license nothing else in
    # the inventory had. One a new wheel brings stops the stage, to be looked
    # at and pruned.
    windows = windows_binaries(py)
    if windows:
        raise BundleError("Windows binaries left in GraftPython (prune them, after checking their license): "
                          + ", ".join(str(p.relative_to(py)) for p in windows))


def prune_foreign_rpaths(contents: Path) -> list[tuple[Path, str]]:
    """Delete every absolute LC_RPATH outside the system's folders (another
    machine's build folder, like Pillow's libjpeg's /Users/runner/...): it
    names someone's path and can never resolve on a user's Mac. Returns them."""
    deleted = []
    for p in signed_machos(contents):
        for rpath in macho.rpaths(p):
            if rpath.startswith("/") and not rpath.startswith(SYSTEM_LIBS):
                _run(INSTALL_NAME_TOOL, "-delete_rpath", rpath, p)
                deleted.append((p, rpath))
    return deleted


def macho_files(contents: Path) -> list[Path]:
    """Every Mach-O under `contents` to sign, deepest first; symlinks never.

    Mach-O is decided by `graft_ipa.macho`, which reads the headers: a
    file merely starting with 0xCAFEBABE (a Java class) is NotMachO, and
    skipped. Any other MachOError is a real Mach-O that can't be read, and
    is refused: skipped, it would sit unsigned inside the signed app.
    """
    found = []
    for p in contents.rglob("*"):
        if p.is_symlink() or not p.is_file():
            continue
        try:
            macho.load_commands(p)
        except macho.NotMachO:
            continue
        except macho.MachOError as e:
            raise BundleError(f"{p.relative_to(contents)} looks like a Mach-O but can't be read: {e}") from e
        found.append(p)
    return sorted(found, key=lambda p: (-len(p.relative_to(contents).parts), str(p)))


def signed_machos(contents: Path) -> list[Path]:
    """The Mach-O files graft-release signs: macho_files() but iOS code
    (macho.is_ios). cyan's iOS frameworks go into users' IPAs as cyan's
    archive carries them, never re-signed with the release Developer ID
    (review #6, #34); verify-bundle checks they are byte for byte the same."""
    return [p for p in macho_files(contents) if not macho.is_ios(p)]


#: CPython's shared library, 17 MB. python-build-standalone links the
#: interpreter statically and ships this only for programs that embed Python.
LIBPYTHON = Path("Resources/GraftPython/lib/libpython3.13.dylib")


def prune_unlinked_libpython(contents: Path) -> list[Path]:
    """Remove libpython from the stage unless a staged Mach-O links it.

    Decided from the load commands of every Mach-O under `contents`, not
    assumed: a dependency built against libpython would fail to load without
    it. Returns the files that link it (relative to `contents`); empty means
    it was removed.
    """
    lib = contents / LIBPYTHON
    linkers = [p.relative_to(contents) for p in macho_files(contents)
               if p != lib and any(n.rsplit("/", 1)[-1] == lib.name for n in macho.dylib_names(p))]
    if not linkers:
        lib.unlink(missing_ok=True)
    return linkers


def _archs(path: Path) -> list[str]:
    return subprocess.run([LIPO, "-archs", str(path)], capture_output=True, text=True, env=_build_env(),
                          check=True).stdout.split()


def thin_to_arm64(contents: Path) -> list[Path]:
    """Cut every universal Mach-O under `contents` down to its arm64 slice;
    returns the files thinned.

    Everything the app bundles is arm64 only (verify-bundle checks it); only
    the app's own executable is universal, and graft-release builds that.
    Some wheels are universal2 (pycryptodome, bcrypt, PyNaCl, ...). A file
    with no arm64 slice is refused: it could never run in the app.
    """
    thinned = []
    for p in signed_machos(contents):  # iOS code stays as shipped
        archs = _archs(p)
        if archs == ["arm64"]:
            continue
        if "arm64" not in archs:
            raise BundleError(f"{p.relative_to(contents)} is {' '.join(archs)}, with no arm64 slice")
        _run(LIPO, p, "-thin", "arm64", "-output", p)
        thinned.append(p)
    return thinned


#: The path compiled .pyc files record for their sources, in place of the
#: staging directory, which is in the release machine's home folder. Python
#: replaces it with the real path when it imports one (_imp._fix_co_filename).
PYC_PREFIX = "/Applications/Graft.app/Contents/Resources/GraftPython"

#: graft-ipa's runtime dependencies, and the build backends for the two
#: projects stage_python builds from source (graft and cyan). Both from
#: scripts/lock-bundle, both installed with --require-hashes.
RUNTIME_LOCK = REPO / "bundle" / "requirements.lock"
BUILD_LOCK = REPO / "bundle" / "build-requirements.lock"

#: What the bundled CLI imports, checked straight after staging.
IMPORT_CHECK = "import graft_ipa, cryptography, pymobiledevice3, paramiko, yaml, rich, cyan"


def _norm(name: str) -> str:
    """A distribution name as PEP 503 compares them."""
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_names(lock: Path) -> list[str]:
    """The (normalized) names a pip-compile lock pins, in order."""
    return [_norm(m.group(1)) for line in lock.read_text().splitlines()
            if (m := re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==", line))]


def _pip_env() -> dict[str, str]:
    """No pip settings of the release machine's: its PIP_* variables and pip.conf."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PIP_", "PYTHON"))}
    env["PIP_CONFIG_FILE"] = os.devnull
    return env


#: The one platform the bundled wheels are chosen for, whatever macOS the
#: release Mac runs: pip would otherwise rank wheels by the running macOS and
#: could bundle one built for a newer macOS than MACOS_MIN (review #36).
WHEEL_TARGET = ("--platform", f"macosx_{MACOS_MIN.replace('.', '_')}_arm64", "--python-version", "3.13",
                "--implementation", "cp", "--abi", "cp313")
_PIP = ("-I", "-m", "pip")
# --retries and --timeout: pip's defaults (5, 15 s) gave up on a slow or
# briefly dropped network and ended the whole build.
_QUIET_PIP = ("--disable-pip-version-check", "--retries", "10", "--timeout", "60")


def pip_cache(cache: Path | None = None) -> Path:
    """pip's own cache (`--cache-dir`) for the locked wheels, in the
    downloads cache (`cache`, CACHE unless given), so a release downloads
    each wheel once per machine. With --no-cache-dir every build fetched
    each one twice: hash-checking mode makes `pip install --dry-run` download
    whole wheels (pip 26's _fetch_metadata_only skips metadata-only fetching
    when hashes are required), and `pip download` fetched them again.

    It trusts nothing new. pip checks every file against the lock's
    --require-hashes, whether it came from the network or from here
    (operations/prepare.py, get_http_url), and refuses a mismatch. Only
    wheels are fetched (--only-binary=:all:), so pip's cache of wheels it
    built from source stays empty. CACHE is read at call time, so a test
    can point it elsewhere."""
    return (cache or CACHE) / "pip"


#: Which locked wheels pip's cache holds, by SHA-256, written after each
#: locked install from that install's plan. The hotspot check reads it
#: offline, before pip has resolved anything.
WHEEL_RECORD = "graft-wheels.json"


def pip_cache_body(cache_dir: Path, url: str) -> Path:
    """Where pip keeps the body of the file it fetched from `url`: under
    http-v2/, named by the SHA-224 of the URL with its scheme and host
    lowercased and its fragment dropped. pip 26's SafeFileCache and its
    vendored cachecontrol's CacheController.cache_url; the bundled Python's
    pip is pinned with it (pins.toml's [python]), and a pip that keeps them
    elsewhere only makes the plan count those wheels as still to download."""
    parts = urllib.parse.urlsplit(url)
    key = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path or '/'}" + (
        f"?{parts.query}" if parts.query else "")
    hashed = hashlib.sha224(key.encode()).hexdigest()
    return cache_dir / "http-v2" / Path(*hashed[:5]) / f"{hashed}.body"


def cached_wheels(cache_dir: Path | None = None) -> dict[str, dict]:
    """SHA-256 -> {name, version, url, mb} of each wheel the record lists
    whose body pip's cache still holds."""
    cache_dir = cache_dir or pip_cache()
    try:
        record = json.loads((cache_dir / WHEEL_RECORD).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {sha: e for sha, e in record.items()
            if isinstance(e, dict) and isinstance(e.get("url"), str) and pip_cache_body(cache_dir, e["url"]).is_file()}


def record_cached_wheels(plan: Path, cache_dir: Path | None = None) -> int:
    """Add each file a locked install's `plan` (its --dry-run report) chose,
    and pip's cache now holds, to the record; returns how many it holds.
    Written whole and then renamed: two releases may stage at once."""
    cache_dir = cache_dir or pip_cache()
    known = cached_wheels(cache_dir)
    for item in json.loads(plan.read_text())["install"]:
        info = item["download_info"]
        sha = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
        body = pip_cache_body(cache_dir, info["url"])
        if sha and body.is_file():
            known[sha] = {"name": _norm(item["metadata"]["name"]), "version": item["metadata"]["version"],
                          "url": info["url"], "mb": round(body.stat().st_size / 1e6, 2)}
    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f"{WHEEL_RECORD}.", suffix=".part", dir=cache_dir)
    with os.fdopen(fd, "w") as f:
        json.dump(known, f, indent=1, sort_keys=True)
    os.replace(raw, cache_dir / WHEEL_RECORD)
    return len(known)


def _report_files(report: Path) -> dict[str, tuple[str, str]]:
    """normalized name -> (version, sha256) of each file a pip report lists."""
    out = {}
    for item in json.loads(report.read_text())["install"]:
        sha = ((item["download_info"].get("archive_info") or {}).get("hashes") or {}).get("sha256", "")
        out[_norm(item["metadata"]["name"])] = (item["metadata"]["version"], sha)
    return out


def check_same_files(plan: Path, installed: Path) -> None:
    """Every file pip installed is the one the plan chose for WHEEL_TARGET (macOS MACOS_MIN)."""
    want, got = _report_files(plan), _report_files(installed)
    wrong = [f"{n} {v} ({sha[:12]})" for n, (v, sha) in sorted(got.items()) if want.get(n) != (v, sha)]
    if wrong:
        raise BundleError(f"pip installed files the plan for macOS {MACOS_MIN} ({WHEEL_TARGET[1]}) didn't choose: "
                          + ", ".join(wrong))


def install_locked(py: Path, lock: Path, plan: Path, scratch: Path, env: dict[str, str]) -> None:
    """Install a hash-locked set as chosen for WHEEL_TARGET, not for this Mac.

    pip resolves the lock for that platform (`--dry-run`, its report is
    `plan`, which components.json is made from), downloads those files, then
    installs from them alone, with no index; the files installed must be the
    plan's.

    The two steps that reach the index share pip's cache (`pip_cache`), so
    the download takes what the plan's resolution fetched, and a later build
    takes both from it; the install from the downloaded files needs none.
    Every step keeps --require-hashes."""
    target = [*WHEEL_TARGET, "--only-binary=:all:", "--require-hashes", "--no-deps"]
    scratch.mkdir(parents=True, exist_ok=True)
    wheels = scratch / "wheels"
    cache = pip_cache()
    cached = ("--cache-dir", str(cache))
    try:
        subprocess.run([str(py), *_PIP, "install", *_QUIET_PIP, *cached, "--dry-run", "--ignore-installed",
                        "--report", str(plan), *target, "-r", str(lock)], check=True, env=env)
        subprocess.run([str(py), *_PIP, "download", *_QUIET_PIP, *cached, "-d", str(wheels), *target,
                        "-r", str(lock)], check=True, env=env)
    except subprocess.CalledProcessError as e:
        # pip never swaps a file that fails its hash for a fresh download, so
        # a damaged cached wheel stops every build until the cache goes.
        raise BundleError(f"pip couldn't resolve or download {lock.name} (its output is above). If it says a file "
                          f"doesn't match its hash, and the lock is right, delete pip's cache, {cache}, "
                          "and build again") from e
    installed = scratch / "installed.json"
    subprocess.run([str(py), *_PIP, "install", *_QUIET_PIP, "--no-cache-dir", "--no-warn-script-location",
                    "--no-compile", "--no-index", "--find-links", str(wheels), "--only-binary=:all:",
                    "--require-hashes", "--no-deps", "--report", str(installed), "-r", str(lock)],
                   check=True, env=env)
    check_same_files(plan, installed)
    record_cached_wheels(plan, cache)


def stage_python(pins: dict[str, Pin], contents: Path, reports: Path | None = None) -> Path:
    """CPython + locked deps + graft + cyan in contents/Resources/GraftPython, pruned and compiled.

    Every `pip install` writes its installation report (`--report`) into
    `reports`, outside the stage: each distribution's exact file and SHA-256,
    which write_components() turns into components.json. None: a temporary
    directory, discarded.
    """
    resources = contents / "Resources"
    gp = resources / "GraftPython"
    if gp.exists():
        raise BundleError(f"{gp} already exists")
    resources.mkdir(parents=True, exist_ok=True)
    unpack = resources / ".python-unpack"
    shutil.rmtree(unpack, ignore_errors=True)
    extract(fetch(pins["python"]), unpack)
    (unpack / "python").rename(gp)
    unpack.rmdir()

    py = gp / "bin" / "python3.13"
    # --no-compile: compileall below compiles everything, once.
    pip = [str(py), "-I", "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
           "--no-warn-script-location", "--no-compile"]
    env = _pip_env()
    with tempfile.TemporaryDirectory(prefix="graft-stage-python-") as tmp:
        tmp = Path(tmp)
        reports = reports if reports is not None else tmp / "reports"
        reports.mkdir(parents=True, exist_ok=True)

        def report(name: str) -> list[str]:
            return ["--report", str(reports / f"{name}.json")]

        # Wheels only: an sdist would compile against whatever this Mac has
        # installed (Homebrew's libraries, say), which a user's Mac doesn't. The
        # build backends go first; the build lock is constrained to the runtime
        # one, so the second install never swaps a version the first put in.
        for name, lock in (("1-build-lock", BUILD_LOCK), ("2-runtime-lock", RUNTIME_LOCK)):
            install_locked(py, lock, reports / f"{name}.json", tmp / name, env)
        # graft, from this checkout (the source being released), and cyan, from
        # its pinned archive, are built with the backends just installed. Build
        # isolation would have pip fetch each backend from PyPI, unpinned and
        # unhashed; --no-index stops pip reaching an index for anything at all.
        # graft goes through a wheel so that what is installed is one file with
        # a hash, like everything else in components.json.
        isolated = ["--no-deps", "--no-build-isolation", "--check-build-dependencies", "--no-index"]
        subprocess.run([str(py), "-I", "-m", "pip", "wheel", "--no-cache-dir", "--disable-pip-version-check",
                        *isolated, "-w", str(tmp / "wheel"), str(REPO)], check=True, env=env)
        wheels = sorted((tmp / "wheel").glob("*.whl"))
        if len(wheels) != 1:
            raise BundleError(f"building graft's wheel left {len(wheels)} wheels: {[w.name for w in wheels]}")
        local = [*pip, *isolated]
        subprocess.run([*local, *report("3-graft"), str(wheels[0])], check=True, env=env)
        subprocess.run([*local, *report("4-cyan"), str(fetch(pins["cyan"]))], check=True, env=env)
    # The backends were only for building; nothing at run time imports them.
    runtime = set(lock_names(RUNTIME_LOCK))
    build_only = [n for n in lock_names(BUILD_LOCK) if n not in runtime]
    subprocess.run([str(py), "-I", "-m", "pip", "uninstall", "--yes", "--disable-pip-version-check",
                    *build_only], check=True, env=env)
    # Every install was --no-deps: pip resolved nothing, so this is the one
    # check that what is installed satisfies everyone's requirements (#30).
    subprocess.run([str(py), "-I", "-m", "pip", "check", "--disable-pip-version-check"], check=True, env=env)
    # PEP 610 records where graft and cyan were installed from: file:// URLs
    # on this machine. Nothing reads them, and they'd ship the path.
    for f in gp.glob("lib/python3.13/site-packages/*.dist-info/direct_url.json"):
        f.unlink()

    # Pip can't be pruned before it has installed everything.
    prune_python(gp)
    # Precompile everything: the standalone build ships three .pyc, and the
    # launcher runs with -B, so nothing may need writing at run time. -f
    # because running pip already wrote .pyc for the stdlib it imported, and
    # those record the staging path.
    # unchecked-hash (review #40): code signing doesn't seal mtimes, and with
    # timestamp .pyc a copy that reset them would recompile everything on
    # every run, -B never writing the result back.
    subprocess.run([str(py), "-I", "-m", "compileall", "-q", "-f", "-j0", "-o", "0",
                    "--invalidation-mode", "unchecked-hash",
                    "-s", str(gp), "-p", PYC_PREFIX, str(gp / "lib" / "python3.13")], check=True)
    check_python(py)
    return gp


def check_python(py: Path) -> str:
    """Run the staged Python, outside any .app, the way the launcher does
    (-I -B): everything the CLI imports, then `graft --version`. Returns its output."""
    with tempfile.TemporaryDirectory(prefix="graft-pycheck-") as cwd:
        subprocess.run([str(py), "-I", "-B", "-c", IMPORT_CHECK], cwd=cwd, check=True)
        out = subprocess.run([str(py), "-I", "-B", "-m", "graft_ipa", "--version"],
                             cwd=cwd, check=True, capture_output=True, text=True).stdout
    if not out.startswith("graft "):
        raise BundleError(f"the staged CLI's --version printed {out!r}")
    return out.strip()


#: What a launcher prints and exits with when the bundle itself is broken (its
#: Python missing or unrunnable), as bundle/launcher.c defines them. The app
#: maps the pair to its "Graft.app is damaged" screen (review #18).
LAUNCHER_DAMAGED_PREFIX = "graft: Graft.app is damaged: "
LAUNCHER_DAMAGED_EXIT = 126

#: Each compiled launcher: its path under Contents, the module it runs, and
#: the embedded Python relative to the launcher's own (symlink-resolved) dir.
LAUNCHERS = {
    "Helpers/graft": ("graft_ipa", "../Resources/GraftPython/bin/python3.13"),
    "Helpers/bin/cyan": ("cyan", "../../Resources/GraftPython/bin/python3.13"),
}


def build_launchers(contents: Path) -> None:
    for rel, (module, py_rel) in LAUNCHERS.items():
        out = contents / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([
            CLANG, "-O2", "-arch", "arm64", f"-mmacosx-version-min={MACOS_MIN}", "-Wall", "-Werror",
            f'-DMODULE="{module}"', f'-DPY_REL="{py_rel}"',
            str(REPO / "bundle" / "launcher.c"), "-o", str(out),
        ], env=_build_env(), check=True)


#: License texts that what's bundled doesn't carry, as `<owner>-LICENSE`:
#: zsign's release tarball's (MIT), from its repository's LICENSE at the
#: pinned tag; and three Python packages' that ship none (bpylist2,
#: enum-compat, loguru), from upstream at the installed version, which each
#: file's first line names. cyan-extras/ holds cyan's iOS frameworks' (see
#: CYAN_FRAMEWORKS).
VENDORED_LICENSES = REPO / "bundle" / "licenses"
GPL_TEXT = REPO / "bundle" / "COPYING-GPL-3.0.txt"

#: License and notice files, by name.
_LICENSE_FILE = re.compile(r"(LICEN[CS]E|NOTICE|COPYING)", re.IGNORECASE)

#: What Graft.app is distributed under, as INDEX.txt and SOURCES.md say it
#: (and the READMEs, THIRD_PARTY_NOTICES.md and the About box should): an
#: aggregate of separate programs, never one license for the whole app. And
#: what it is not: Unicorn, which ipatool downloads for itself.
LICENCE_STATEMENT = (
    "Graft.app is an aggregate of separate programs and libraries, each under its own license; no one "
    "license covers the app as a whole. Graft's own Python program (the graft command-line tool and the "
    "Python packages it runs, pymobiledevice3 among them) is distributed as a whole under GPL-3.0-or-later, "
    "while each package in it keeps its own license, so paramiko and pylzss stay LGPL. Graft's own source "
    "code is MIT. The other components keep their own licenses, among them AGPL-3.0 (ldid), APSL-2.0 "
    "(cctools), LGPL (libplist and a module inside ipatool), MPL-2.0 "
    "(a module inside ipatool), Apache-2.0, BSD and MIT. Unicorn is not part of Graft.app: the bundled "
    "ipatool downloads Unicorn 2.1.4 (GPL-2.0) from PyPI on first use, as upstream ipatool does, checked "
    "against the SHA-256 its source pins.")


def _fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def _the_binary(unpacked: Path, tool: str) -> Path:
    """The one Mach-O in an unpacked release named `tool` or `tool-<suffix>`."""
    found = []
    for p in sorted(unpacked.rglob("*")):
        if p.is_symlink() or not p.is_file() or not (p.name == tool or p.name.startswith(f"{tool}-")):
            continue
        try:
            macho.load_commands(p)
        except macho.MachOError:
            continue
        found.append(p)
    if len(found) != 1:
        raise BundleError(f"{tool}: expected one {tool} binary in its release, found {len(found)}: {found}")
    return found[0]


def vendored_licences(pins: dict[str, Pin], work: Path = WORK) -> Path:
    """A fresh `work/tool-licences`, where the tools' license files gather,
    starting with the vendored texts that belong to a pin (zsign's).
    stage_ipatool, stage_unfairplay and the rest of stage() add theirs."""
    licences = _fresh(work / "tool-licences")
    for f in sorted(VENDORED_LICENSES.iterdir()):
        if f.name.removesuffix("-LICENSE") in pins:      # a Python package's goes in python-packages/
            shutil.copy2(f, licences / f.name)
    return licences


# ------------------------------------------- licenses for code linked in

#: Pins that are license texts only: nothing from them is in the app.
LICENCE_TEXT_PINS = {"python-build-standalone", "hacl"}

#: What python-build-standalone can link into python3.13, each told by a
#: symbol only it defines (a regular expression on the defined symbols of
#: python3.13 and lib-dynload), with its license text: a LICENSE.<x>.txt in
#: PBS's repository, or "hacl" for HACL*'s, from its own source (the hacl pin).
PYTHON_STATIC = {
    "bzip2": (r"^_BZ2_bzCompress$", "LICENSE.bzip2.txt"),
    "libffi": (r"^_ffi_call$", "LICENSE.libffi.txt"),
    "mpdecimal": (r"^_mpd_qadd$", "LICENSE.mpdecimal.txt"),
    "liblzma": (r"^_lzma_code$", "LICENSE.liblzma.txt"),
    "expat": (r"XML_ParserCreate$", "LICENSE.expat.txt"),
    "sqlite": (r"^_sqlite3_open$", "LICENSE.sqlite.txt"),
    "libuuid": (r"^_uuid_generate", "LICENSE.libuuid.txt"),
    "openssl": (r"^_SSL_new$", "LICENSE.openssl-3.txt"),
    "zstd": (r"^_ZSTD_compress$", "LICENSE.zstd.txt"),
    "ncurses": (r"^_wborder$", "LICENSE.ncurses.txt"),
    "libedit": (r"^_el_init$", "LICENSE.libedit.txt"),
    "tcl": (r"^_Tcl_Init$", "LICENSE.tcl.txt"),
    "zlib": (r"^_deflate$", "LICENSE.zlib.txt"),
    "bdb": (r"^___db_", "LICENSE.bdb.txt"),
    "hacl": (r"Hacl_Hash_SHA2", "hacl"),
}


def defined_symbols(paths: list[Path]) -> set[str]:
    """Every symbol `paths` define, local ones too: a static library's code
    keeps its names in the symbol table even when nothing exports them."""
    names = set()
    for path in paths:
        out = subprocess.run([NM, str(path)], capture_output=True, text=True, env=_build_env(), check=True).stdout
        names |= {f[2] for line in out.splitlines() if len(f := line.split()) == 3 and f[1] in "TtDdSsBb"}
    return names


def python_static_libraries(gp: Path) -> list[str]:
    """Which of PYTHON_STATIC the staged python3.13 and its lib-dynload contain."""
    symbols = defined_symbols([gp / "bin" / "python3.13", *sorted((gp / "lib" / "python3.13" / "lib-dynload").glob("*.so"))])
    return [lib for lib, (marker, _) in PYTHON_STATIC.items() if any(re.search(marker, s) for s in symbols)]


def hacl_licence(source: bytes) -> str:
    """HACL*'s MIT license: the comment its source files open with."""
    text = source.decode()
    if not text.startswith("/*") or "*/" not in text:
        raise BundleError("HACL*'s source doesn't open with its license comment")
    body = text[2:text.index("*/")]
    return "\n".join(line.strip().removeprefix("*").removeprefix(" ").rstrip() for line in body.splitlines()).strip() + "\n"


def python_static_licences(pins: dict[str, Pin], gp: Path, dest: Path) -> list[tuple[str, str]]:
    """Each library python-build-standalone linked into the staged Python,
    with its license text as `python-<library>-LICENSE.txt` in `dest`
    (review #5, #8). Returns (library, file)."""
    found = []
    pbs = pins["python-build-standalone"]
    with tarfile.open(fetch(pbs)) as tf:
        top = tf.getnames()[0].split("/", 1)[0]
        for lib in python_static_libraries(gp):
            source = PYTHON_STATIC[lib][1]
            if source == "hacl":
                text = hacl_licence(fetch(pins["hacl"]).read_bytes()).encode()
            else:
                try:
                    member = tf.extractfile(f"{top}/{source}")
                except KeyError:
                    member = None
                if member is None:
                    raise BundleError(f"python3.13 contains {lib}, but {pbs.filename} has no {source}")
                text = member.read()
            if not _is_licence_text(text):
                raise BundleError(f"{lib}'s license ({source}) holds no license text")
            (dest / f"python-{lib}-LICENSE.txt").write_bytes(text)
            found.append((lib, f"python-{lib}-LICENSE.txt"))
    return found


#: A license text a Rust crate that ships none of its own may fall back to,
#: when its SPDX expression offers that license (bundle/licenses/spdx/).
SPDX_FILES = {"Apache-2.0": REPO / "bundle" / "licenses" / "spdx" / "Apache-2.0.txt"}


def _crate_checksums(sdist: Path) -> dict[tuple[str, str], str]:
    """(crate, version) -> SHA-256 from every Cargo.lock in an sdist."""
    sums = {}
    with tarfile.open(sdist) as tf:
        for m in tf.getmembers():
            if m.isfile() and m.name.endswith("/Cargo.lock"):
                for pkg in tomllib.loads(tf.extractfile(m).read().decode()).get("package", []):
                    if "checksum" in pkg:
                        sums[(pkg["name"], pkg["version"])] = pkg["checksum"]
    return sums


def fetch_crate(name: str, version: str, sha256: str, cache: Path | None = None) -> Path:
    """A crate from crates.io, checked against its Cargo.lock checksum."""
    return fetch(Pin(name=name, url=f"https://static.crates.io/crates/{name}/{name}-{version}.crate", sha256=sha256,
                     license="", version=version), (cache or CACHE) / "crates")


#: A crates.io crate's source path, as a compiled Rust module keeps it (its
#: panic and file!() locations): `.../registry/src/<index>/<crate>-<version>/`.
_CARGO_SOURCE = re.compile(rb"/registry/src/[^/\x00]+/([A-Za-z0-9_-]+?)-(\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]*)?)/")
#: A crate Rust's standard library vendors, by the source path std keeps:
#: `/rust/deps/<crate>-<version>/` (dlmalloc, rustc-demangle).
_RUST_DEPS = re.compile(rb"/rust/deps/([A-Za-z0-9_-]+?)-\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.+-]*)?/")
#: Rust's standard library, in every Rust binary, from no crates.io crate.
RUST_STD_CRATES = frozenset({"core", "alloc", "std", "compiler_builtins", "panic_abort", "panic_unwind",
                             "unwind", "std_detect", "proc_macro", "test"})

#: The crates.io crates inside ipatool's embedded 1Password SDK core, pinned
#: with their checksums: that core ships prebuilt, with no Cargo.lock.
IPATOOL_CORE_CRATES = REPO / "bundle" / "ipatool-core-crates.lock"

_WASM_MAGIC = b"\0asm\x01\0\0\0"


def _leb128(data: bytes, i: int) -> tuple[int, int]:
    """The unsigned LEB128 number at `data[i]`, and the index after it."""
    value = 0
    for shift in range(0, 35, 7):
        if i >= len(data):
            raise ValueError("a number runs past the end")
        byte = data[i]
        i += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, i
    raise ValueError("a number longer than 32 bits")


def wasm_custom_sections(data: bytes) -> list[tuple[str, bytes]]:
    """(name, contents) of every custom section of every WebAssembly module
    `data` holds (ipatool embeds its 1Password SDK core as one), each module
    read section by section from its header, until a section doesn't parse."""
    found = []
    start = data.find(_WASM_MAGIC)
    while start >= 0:
        i = start + len(_WASM_MAGIC)
        try:
            while i < len(data):
                size, body = _leb128(data, i + 1)
                if data[i] > 13 or body + size > len(data):
                    break
                if data[i] == 0:
                    length, at = _leb128(data, body)
                    if at + length > body + size:
                        break
                    found.append((data[at:at + length].decode("utf-8", "replace"), data[at + length:body + size]))
                i = body + size
        except ValueError:
            pass
        start = data.find(_WASM_MAGIC, start + 1)
    return found


def _wasm_function_names(section: bytes) -> list[str]:
    """The function names a `name` section holds (its subsection 1)."""
    names, i = [], 0
    try:
        while i < len(section):
            size, body = _leb128(section, i + 1)
            if section[i] == 1:
                count, j = _leb128(section, body)
                for _ in range(count):
                    _, j = _leb128(section, j)
                    length, j = _leb128(section, j)
                    names.append(section[j:j + length].decode("utf-8", "replace"))
                    j += length
            i = body + size
    except ValueError:
        pass
    return names


#: Legacy Rust mangling's escapes (rustc-demangle's).
_LEGACY_ESCAPES = {"$LT$": "<", "$GT$": ">", "$u20$": " ", "$C$": ",", "$RF$": "&", "$BP$": "*", "$LP$": "(",
                   "$RP$": ")", "$SP$": "@", "$u5b$": "[", "$u5d$": "]", "$u7b$": "{", "$u7d$": "}",
                   "$u3b$": ";", "$u27$": "'", "$u22$": '"', "$u21$": "!", "$u2b$": "+", "$u3d$": "="}
#: The crate a Rust path starts from: a name and `::`, with no path before it.
_PATH_ROOT = re.compile(r"(?<![\w:])([a-z_][a-z0-9_]*)::")


def _rust_path(symbol: str) -> str | None:
    """A legacy-mangled Rust function name (`_ZN…E`) as the path it spells,
    without its hash: `<reqwest::error::Error as core::fmt::Display>::fmt`."""
    m = re.fullmatch(r"_ZN(.+)E(?:\.llvm\.\d+)?", symbol, re.S)
    if not m:
        return None
    parts, rest = [], m[1]
    while rest:
        digits = re.match(r"\d+", rest)
        if not digits or digits.end() + int(digits[0]) > len(rest):
            return None
        start = digits.end()
        parts.append(rest[start:start + int(digits[0])])
        rest = rest[start + int(digits[0]):]
    if parts and re.fullmatch(r"h[0-9a-f]{16}", parts[-1]):
        parts.pop()
    text = "::".join(p[1:] if p.startswith("_$") else p for p in parts)
    for escape, char in _LEGACY_ESCAPES.items():
        text = text.replace(escape, char)
    return text.replace("..", "::")


#: The crate that wrote a wasm-bindgen program, as the program ends:
#: `<length><crate>-<hash>`.
_BINDGEN_CRATE = re.compile(rb"(?=([\x12-\x7f])([A-Za-z][A-Za-z0-9_-]*-[0-9a-f]{16})(?![A-Za-z0-9_-]))")


def _bindgen_entries(section: bytes) -> tuple[set[str], set[str]]:
    """(wasm-bindgen versions, crates) a `__wasm_bindgen_unstable` section
    records: each entry is the schema wasm-bindgen wrote it with, its
    version among them, then the program one crate generated, which ends
    with that crate's name and a hash."""
    versions, crates, i = set(), set(), 0
    while i + 4 <= len(section):
        length = int.from_bytes(section[i:i + 4], "little")
        schema, i = section[i + 4:i + 4 + length], i + 4 + length
        if i + 4 > len(section):
            break
        length = int.from_bytes(section[i:i + 4], "little")
        program, i = section[i + 4:i + 4 + length], i + 4 + length
        try:
            version = json.loads(schema).get("version")
        except (ValueError, AttributeError):
            version = None
        if isinstance(version, str):
            versions.add(version)
        named = [m[2] for m in _BINDGEN_CRATE.finditer(program) if m[1][0] == len(m[2])]
        if named:
            crates.add(named[-1].decode().rsplit("-", 1)[0])
    return versions, crates


def crates_named(path: Path) -> set[tuple[str, str]]:
    """(crate, version) of every crates.io crate whose source path `path`, a
    compiled file, names: the Rust code in it, though not every crate (one
    with nothing that panics may leave no path); and wasm-bindgen at the
    version its section in an embedded WebAssembly module records."""
    data = path.read_bytes()
    named = {(m[1].decode(), m[2].decode()) for m in _CARGO_SOURCE.finditer(data)}
    for name, section in wasm_custom_sections(data):
        if name == "__wasm_bindgen_unstable":
            named |= {("wasm-bindgen", v) for v in _bindgen_entries(section)[0]}
    return named


def crates_compiled(path: Path) -> set[str]:
    """The crates compiled into a WebAssembly module `path` embeds (ipatool's
    1Password SDK core: delta audit #1), by library name (`md5` for md-5,
    `_` for `-`): the root of every Rust path in its `name` section's
    function names (`<reqwest::error::Error as core::fmt::Display>::fmt`
    shows reqwest and core), and each crate whose wasm-bindgen program its
    `__wasm_bindgen_unstable` section holds. Never Rust's standard library or
    a crate it vendors (one named only under /rust/deps/). Only legacy
    mangling is read: a core built with v0 names would show only its source
    paths, so every crate a lock pins by name would then fail as gone."""
    data = path.read_bytes()
    crates: set[str] = set()
    for name, section in wasm_custom_sections(data):
        if name == "name":
            for symbol in _wasm_function_names(section):
                if (spelled := _rust_path(symbol)) is not None:
                    crates |= set(_PATH_ROOT.findall(spelled))
        elif name == "__wasm_bindgen_unstable":
            crates |= {c.replace("-", "_") for c in _bindgen_entries(section)[1]}
    if not crates:
        return crates
    registry = {m[1].decode().replace("-", "_") for m in _CARGO_SOURCE.finditer(data)}
    vendored = {m[1].decode().replace("-", "_") for m in _RUST_DEPS.finditer(data)} - registry
    return crates - RUST_STD_CRATES - vendored


def _compiled_modules(dist: Path) -> list[Path]:
    """The compiled modules (.so, .dylib) a .dist-info's RECORD lists."""
    record = dist / "RECORD"
    if not record.is_file():
        return []
    site = dist.parent
    return [site / row[0] for row in csv.reader(record.read_text().splitlines())
            if row and row[0].endswith((".so", ".dylib")) and not row[0].startswith("..")
            and (site / row[0]).is_file()]


def _sbom_crates(dist: Path) -> list[tuple[str, str, str]]:
    """(crate, version, SPDX license) of each Rust crate a wheel's CycloneDX SBOMs list."""
    crates = []
    for sbom in sorted((dist / "sboms").glob("*.json")) if (dist / "sboms").is_dir() else []:
        for c in json.loads(sbom.read_text()).get("components", []):
            if str(c.get("purl", "")).startswith("pkg:cargo/"):
                crates.append((c["name"], c["version"],
                               " ".join(str(lic.get("expression") or (lic.get("license") or {}).get("id") or "")
                                        for lic in c.get("licenses", []))))
    return crates


def _crate_licence(tf: tarfile.TarFile, crate: str, version: str) -> str:
    """The `license` a .crate's Cargo.toml states, or ""."""
    try:
        member = tf.extractfile(f"{crate}-{version}/Cargo.toml")
        return str(tomllib.loads(member.read().decode()).get("package", {}).get("license") or "") if member else ""
    except (KeyError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return ""


def crate_licence(crate: str, version: str, checksum: str, stated: str = "") -> str:
    """A crate's license: `stated` (its SBOM's or lock's), else its own
    Cargo.toml's, from the crate fetched and checked against `checksum`."""
    if stated:
        return stated
    with tarfile.open(fetch_crate(crate, version, checksum)) as tf:
        return _crate_licence(tf, crate, version)


def copyleft_only_crates(crates: list[tuple[str, str, str]], archived: set[tuple[str, str]] = frozenset()
                         ) -> list[str]:
    """`crate version (license)` of each (crate, version, license) whose
    license offers no permissive choice (copyleft_only) and whose source the
    source archive doesn't carry (`archived`): review 2.0, bundle #9."""
    return [f"{c} {v} ({lic})" for c, v, lic in crates if copyleft_only(lic) and (c, v) not in archived]


def _crate_licence_folder(crate: str, version: str, checksum: str, spdx: str, owner: Path, where: str,
                          licence_file: Path | None = None) -> str:
    """`owner/<crate>@<version>/`: the crate's license files, from the crate
    fetched from crates.io and checked against `checksum`. A crate with no
    license file takes `licence_file` (its repository's, which a lock entry
    names, whose first line must name this crate and version); else the text
    of an SPDX license it offers (SPDX_FILES), its own Cargo.toml's when
    `spdx` is empty; one with none of these is refused. Returns the folder
    relative to owner's parent."""
    folder = owner / f"{crate}@{version}"
    folder.mkdir(parents=True)
    with tarfile.open(fetch_crate(crate, version, checksum)) as tf:
        for m in tf.getmembers():
            leaf = m.name.split("/", 1)[-1]
            if m.isfile() and "/" not in leaf and is_licence_name(leaf):
                (folder / leaf).write_bytes(tf.extractfile(m).read())
        spdx = spdx or _crate_licence(tf, crate, version)
    if not any(_is_licence_text(f.read_bytes()) for f in folder.iterdir()) and licence_file is not None:
        head = licence_file.read_text().split("\n", 1)[0]
        if not re.search(rf"\b{re.escape(crate)} {re.escape(version)}\b", head):
            said = re.search(rf"\b{re.escape(crate)} \d+(?:\.\d+)*", head)
            raise BundleError(f"{licence_file.name} is for {said[0] if said else repr(head)}, but the lock pins "
                              f"{crate} {version}: take the license text at {version}")
        shutil.copy2(licence_file, folder / "LICENSE")
    if not any(_is_licence_text(f.read_bytes()) for f in folder.iterdir()):
        fallback = next((SPDX_FILES[k] for k in SPDX_FILES if re.search(rf"\b{re.escape(k)}\b", spdx)), None)
        if fallback is None:
            raise BundleError(f"Rust crate {crate} {version} (in {where}) ships no license text, "
                              f"and offers no license Graft has a text for ({spdx or 'none stated'})")
        shutil.copy2(fallback, folder / f"{fallback.stem}.txt")
    return str(folder.relative_to(owner.parent))


def rust_crate_licences(contents: Path, locked: dict[str, tuple[str, set[str]]], dest: Path) -> dict[str, list[str]]:
    """The license files of every Rust crate compiled into a bundled wheel,
    `dest/<package>/<crate>@<version>/` (review #8, RC audit #1).

    Which crates: the wheel's CycloneDX SBOM (pydantic_core, qh3,
    cryptography), else, for a compiled module that names crates.io sources
    and ships no SBOM (bcrypt), every crate its sdist's Cargo.lock pins,
    build-only ones too. Each crate is fetched from crates.io, checked against
    its checksum in that Cargo.lock (the sdist itself checked against
    bundle/requirements.lock). A crate the module names that the SBOM or lock
    doesn't list is refused, and so is a Rust module with neither. Returns
    dist name -> crate folders."""
    out: dict[str, list[str]] = {}
    for dist, meta in _installed_dists(contents):
        name, version = meta["Name"], meta["Version"]
        named = set().union(*(crates_named(f) for f in _compiled_modules(dist)))
        sbom = _sbom_crates(dist)
        if not sbom and not named:
            continue
        sums = _crate_checksums(fetch(sdist_pin(name, version, "", locked)))
        if sbom:
            # One not in the lock is a member of the package's own workspace:
            # its license is the package's.
            crates = [(c, v, spdx) for c, v, spdx in sbom if (c, v) in sums]
        elif sums:
            crates = [(c, v, "") for c, v in sorted(sums)]
        else:
            raise BundleError(f"{name} {version}'s compiled module holds Rust crates "
                              f"({', '.join(f'{c} {v}' for c, v in sorted(named))}), but the wheel has no SBOM and "
                              "its sdist no Cargo.lock to say which: find their licenses another way")
        unlisted = sorted(named - {(c, v) for c, v, _ in crates})
        if unlisted:
            raise BundleError(f"{name} {version}'s compiled module holds Rust crates its "
                              f"{'SBOM' if sbom else 'Cargo.lock'} doesn't list: "
                              + ", ".join(f"{c} {v}" for c, v in unlisted))
        # The source archive carries the package's sdist, not the crates it
        # was built from: a crate whose license wants its source can't ship.
        only = copyleft_only_crates([(c, v, crate_licence(c, v, sums[(c, v)], spdx)) for c, v, spdx in crates])
        if only:
            raise BundleError(f"{name} {version}'s compiled module holds Rust crates under a copyleft license with no "
                              f"permissive choice, whose source the source archive doesn't carry: {', '.join(only)}")
        out[_norm(name)] = [_crate_licence_folder(c, v, sums[(c, v)], spdx, dest / _norm(name), name)
                            for c, v, spdx in crates]
    return out


def _core_lock(lock: Path) -> dict:
    return tomllib.loads(lock.read_text())


def pinned_crates(lock: Path = IPATOOL_CORE_CRATES) -> dict[tuple[str, str], dict]:
    """(crate, version) -> its [[package]] in a pinned crate lock."""
    return {(p["name"], p["version"]): p for p in _core_lock(lock).get("package", [])}


def _lib(package: dict) -> str:
    """A pinned crate's library name, as its code's paths spell it."""
    return package.get("lib") or package["name"].replace("-", "_")


def _seen_by_name(package: dict) -> bool:
    """Whether the lock pins this crate from the core's names alone (`seen =
    "name"`), its version not one the binary carries; else from its source
    path (the default) or wasm-bindgen's section (`"bindgen"`)."""
    return package.get("seen", "path") == "name"


def _not_own(lock: Path):
    """A filter on crate names: not the core's maker's own crates (the
    lock's `own-prefix`), which come with its license, not from crates.io."""
    prefix = _core_lock(lock).get("own-prefix")
    return lambda name: not (prefix and name.startswith(prefix))


def binary_crate_licences(binary: Path, owner: str, dest: Path, lock: Path = IPATOOL_CORE_CRATES) -> list[str]:
    """The license files of every crates.io crate `binary` holds (ipatool's
    embedded 1Password SDK core: RC audit #1, delta audit #1),
    `dest/<owner>/<crate>@<version>/`, each crate checked against its
    checksum in `lock`. Which crates: each source path the binary names, at
    its version (`crates_named`), and each crate its WebAssembly names show
    (`crates_compiled`), at the version `lock` gives it. A crate it holds that
    `lock` doesn't pin is refused, and so is one `lock` pins that it doesn't
    show the way the lock says: the list stays the binary's."""
    pinned, mine = pinned_crates(lock), _not_own(lock)
    versioned, names = crates_named(binary), {n for n in crates_compiled(binary) if mine(n)}
    libs = {_lib(p) for p in pinned.values()}
    unpinned = ([f"{c} {v}" for c, v in sorted(versioned - set(pinned))]
                + [f"{n} (named in the core)" for n in sorted(names - libs)])
    stale = [f"{c} {v}" for (c, v), p in sorted(pinned.items())
             if (c, v) not in versioned and not (_seen_by_name(p) and _lib(p) in names)]
    if unpinned or stale:
        raise BundleError(f"{binary.name} holds Rust crates {lock.relative_to(REPO) if lock.is_relative_to(REPO) else lock} "
                          "doesn't match: " + "; ".join(part for part in (
                              unpinned and "not pinned: " + ", ".join(unpinned),
                              stale and "pinned but not in it: " + ", ".join(stale)) if part)
                          + ". List them again (each one's crates.io checksum and license)")
    # core_crate_sources archives those the lock calls copyleft-only; one
    # whose own Cargo.toml is, with no license in the lock, would be missed.
    only = copyleft_only_crates([(c, v, crate_licence(c, v, p["checksum"], p.get("license", "")))
                                 for (c, v), p in sorted(pinned.items())],
                                {(c, v) for c, v, _ in core_crate_sources(lock)})
    if only:
        raise BundleError(f"{binary.name} holds Rust crates under a copyleft license with no permissive choice "
                          f"whose source the source archive doesn't carry: {', '.join(only)}; give each its license "
                          f"in {lock.name}")
    return [_crate_licence_folder(c, v, p["checksum"], p.get("license", ""), dest / owner, owner,
                                  VENDORED_LICENSES / p["license-file"] if "license-file" in p else None)
            for (c, v), p in sorted(pinned.items())]


def crate_notices_needed(files: list[Path], lock: Path = IPATOOL_CORE_CRATES) -> dict[Path, set[str]]:
    """The `<crate>@<version>` license folders each of `files` needs: one for
    each crates.io source path it names, and for each crate the names in an
    embedded WebAssembly module show, the version(s) `lock` pins it at (or
    `<name> (no version pinned in <lock>)`)."""
    pinned, mine = pinned_crates(lock), _not_own(lock)
    by_lib: dict[str, set[str]] = {}
    for (c, v), p in pinned.items():
        by_lib.setdefault(_lib(p), set()).add(f"{c}@{v}")
    needed = {}
    for f in files:
        need = {f"{c}@{v}" for c, v in crates_named(f)}
        for name in filter(mine, crates_compiled(f)):
            need |= by_lib.get(name, {f"{name} (no version pinned in {lock.name})"})
        if need:
            needed[f] = need
    return needed


def crate_notice_problems(contents: Path, files: list[Path], lock: Path = IPATOOL_CORE_CRATES,
                          needed: dict[Path, set[str]] | None = None) -> list[str]:
    """Each crate one of `files` holds (`crate_notices_needed`) whose license
    folder, at that version, isn't in Licenses/rust-crates/ (under any
    package or tool): its notice is missing."""
    lic = contents / "Resources" / "Licenses" / "rust-crates"
    have = {p.name for p in lic.glob("*/*") if p.is_dir()}
    problems = []
    for f, need in (crate_notices_needed(files, lock) if needed is None else needed).items():
        if missing := sorted(need - have):
            problems.append(f"{f.relative_to(contents)} holds Rust crates with no license in Licenses/rust-crates/: "
                            + ", ".join(missing))
    return problems


def _dist_licence(meta) -> str | None:
    """A package's license as its metadata states it: License-Expression, a
    one-line License, its license classifiers, else the first line of a
    License field holding the whole text ("MIT License")."""
    if meta.get("License-Expression"):
        return meta["License-Expression"].strip()
    lic = (meta.get("License") or "").strip()
    if lic.upper() == "UNKNOWN":
        lic = ""
    if lic and "\n" not in lic and len(lic) <= 80:
        return lic
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return " / ".join(classifiers)
    first = lic.splitlines()[0].strip() if lic else ""
    return first if first and len(first) <= 80 else None


def _dist_licence_files(dist: Path) -> list[tuple[Path, Path]]:
    """(file, path to give it) for every license file a .dist-info carries:
    everything under licenses/ (PEP 639), else LICENSE*/COPYING*/NOTICE* beside METADATA."""
    found = []
    for sub in ("licenses", "license_files", "LICENSES"):
        if (dist / sub).is_dir():
            found += [(f, f.relative_to(dist / sub)) for f in sorted((dist / sub).rglob("*")) if f.is_file()]
    found += [(f, Path(f.name)) for f in sorted(dist.iterdir()) if f.is_file() and _LICENSE_FILE.match(f.name)]
    return found


def _installed_dists(contents: Path) -> list[tuple[Path, email.message.Message]]:
    """Each distribution installed in the staged Python: its .dist-info and METADATA."""
    site = contents / "Resources" / "GraftPython" / "lib" / "python3.13" / "site-packages"
    return [(dist, email.parser.HeaderParser().parsestr((dist / "METADATA").read_text(errors="replace")))
            for dist in sorted(site.glob("*.dist-info"), key=lambda d: d.name.lower())]


@dataclass(frozen=True)
class Framework:
    """One of cyan's iOS frameworks, as its license was checked."""
    # (Field order is positional in CYAN_FRAMEWORKS below.)
    #: CFBundleIdentifier and CFBundleShortVersionString, from its Info.plist.
    bundle_id: str
    version: str
    license: str
    #: Its license text: a file in bundle/licenses/cyan-extras.
    text: str
    #: What it is: the upstream release and repository.
    upstream: str
    #: The SHA-256 of its executable as the license audit saw it, and the
    #: cyan commit whose archive it was in then (context only: the hash is
    #: the pin).
    sha256: str
    audited_at: str

    @property
    def upstream_version(self) -> str:
        """The release it is ("1.17" for Cephei, whose every Info.plist says
        1.0.0): the version components.json gives (fix-check #24)."""
        return self.upstream.split(",", 1)[0].rsplit(" ", 1)[-1]


#: The cyan commit the five frameworks below were audited at.
_AUDITED_AT = "04a40f2aa65ec9cb36127f74d52ae4ef71377c50"

#: Every framework in cyan/extras, which cyan injects into an IPA when a
#: tweak needs one. They ship prebuilt, as cyan's archive carries them. Each
#: is listed with its upstream license at the version shipped; each text's
#: header says where it is from and how that version was told. The stage
#: refuses anything else in cyan/extras, so a new cyan pin can't bring in a
#: binary whose license nobody has looked up.
#:
#: Each executable is pinned by its SHA-256, which is stricter than pinning
#: the cyan commit: a new cyan whose frameworks are byte for byte the same
#: stages as before, and any changed binary stops the stage. The bundle id
#: and version alone can't tell: every Cephei release's Info.plist says 1.0.0.
CYAN_FRAMEWORKS = {
    "Cephei.framework": Framework(
        "ws.hbang.common", "1.0.0", "Apache-2.0", "libcephei-LICENSE",
        "Cephei 1.17, https://github.com/hbang/libcephei",
        "7b3b0056c992fc271e5de582998376e1c449af98df83a611106eca3589f8fed9", _AUDITED_AT),
    "CepheiPrefs.framework": Framework(
        "ws.hbang.common.prefs", "1.0.0", "Apache-2.0", "libcephei-LICENSE",
        "Cephei 1.17, https://github.com/hbang/libcephei",
        "f6375b84c82dec2fb3a9ddd6777d49ca7d8a8c386a1e1d8082d38059548a2160", _AUDITED_AT),
    "CepheiUI.framework": Framework(
        "ws.hbang.common.UI", "1.0.0", "Apache-2.0", "libcephei-LICENSE",
        "Cephei 1.17, https://github.com/hbang/libcephei",
        "e03360befca500620670c33073b5f11f176356564a947ba1226c1ed8012b04ab", _AUDITED_AT),
    "CydiaSubstrate.framework": Framework(
        "ellekit", "1.1.3", "BSD-3-Clause", "ellekit-LICENSE",
        "ElleKit 1.1.3, https://github.com/tealbathingsuit/ellekit",
        "bca859c9413cb85ca0d3ecfca494c61d67e4acd5846660e618526a152963965b", _AUDITED_AT),
    "Orion.framework": Framework(
        "dev.theos.orion", "1.0.2", "MIT", "orion-LICENSE",
        "Orion 1.0.2, https://github.com/theos/orion",
        "290889165a4d156a41ed29c88015083e40b5101153e52fa462f2a25d4942a4e8", _AUDITED_AT),
}
CYAN_EXTRAS_LICENSES = VENDORED_LICENSES / "cyan-extras"


@dataclass(frozen=True)
class StagedFramework:
    #: Its folder's name in cyan/extras ("Orion.framework").
    name: str
    known: Framework
    #: Its executable, in the stage.
    binary: Path
    #: CFBundleShortVersionString, from its Info.plist.
    version: str

    @property
    def licence_file(self) -> str:
        """Where its license text goes, relative to Licenses/."""
        return f"cyan-extras/{self.name.removesuffix('.framework')}-LICENSE"


def cyan_frameworks(contents: Path) -> list[StagedFramework]:
    """Each framework in the staged cyan/extras, checked against CYAN_FRAMEWORKS.

    Refused: anything there CYAN_FRAMEWORKS doesn't name; a framework whose
    Info.plist gives another bundle id or version than the ones its license
    was checked for; one with no executable, or an executable other than the
    one audited (by SHA-256); and any other Mach-O inside one.
    """
    extras = contents / CYAN_EXTRAS
    if not extras.is_dir():
        return []
    found = []
    for d in sorted(extras.iterdir()):
        where = f"cyan/extras/{d.name}"
        known = CYAN_FRAMEWORKS.get(d.name)
        if known is None or d.is_symlink() or not d.is_dir():
            raise BundleError(f"{where} is not one of the frameworks whose license is known "
                              f"({', '.join(CYAN_FRAMEWORKS)}). Find its upstream license at the version "
                              "cyan ships, and add it to CYAN_FRAMEWORKS with its text in "
                              "bundle/licenses/cyan-extras/")
        try:
            info = plistlib.loads((d / "Info.plist").read_bytes())
        except (OSError, plistlib.InvalidFileException) as e:
            raise BundleError(f"{where}/Info.plist can't be read: {e}") from e
        got = (info.get("CFBundleIdentifier"), info.get("CFBundleShortVersionString"))
        if got != (known.bundle_id, known.version):
            raise BundleError(f"{where} is {got[0]} {got[1]}, but its license was checked for "
                              f"{known.bundle_id} {known.version}: take the license text at this version")
        exe = str(info.get("CFBundleExecutable") or "")
        binary = d / exe
        machos = macho_files(d)
        if not exe or binary not in machos:
            raise BundleError(f"{where} has no executable " + (f"{exe} (a Mach-O)" if exe else
                                                              "(its Info.plist names none)"))
        sha = _sha256(binary)
        if sha != known.sha256:
            raise BundleError(f"{where}/{exe} is not the binary whose license was audited: its SHA-256 is "
                              f"{sha}, and the audit (at cyan {known.audited_at}) saw {known.sha256}. The "
                              "license audit must be redone for the new binary before CYAN_FRAMEWORKS "
                              "can pin it")
        others = [str(p.relative_to(d)) for p in machos if p != binary]
        if others:
            raise BundleError(f"{where} holds Mach-O files besides its executable, whose license "
                              f"isn't known: {', '.join(others)}")
        found.append(StagedFramework(d.name, known, binary, got[1]))
    return found


def cyan_archive_member(cyan: Pin, rel: Path) -> str:
    """`rel` (a path inside cyan's package) as a member of cyan's pinned
    archive: GitHub's `archive/<commit>.zip` holds the tree under
    `<repo>-<commit>/`."""
    m = re.fullmatch(r"https://github\.com/[^/]+/([^/]+)/archive/([0-9a-f]{40})\.zip", cyan.url)
    if m is None:
        raise BundleError(f"cyan's pin is not a GitHub archive/<commit>.zip: {cyan.url}")
    if m[2] != cyan.version:
        raise BundleError(f"cyan's pin is version {cyan.version}, but its url is commit {m[2]}'s archive")
    return f"{m[1]}-{m[2]}/cyan/{rel.as_posix()}"


#: What each license folder holds, for INDEX.txt.
FOLDER_NOTES = {
    "ipatool-modules": "each Go module compiled into ipatool, at its version (golang-lru is MPL-2.0; retry.v1 is "
                       "LGPL-3.0 with a static-linking exception; mpb is under the Unlicense, and xi2/xz is in "
                       "the public domain; the rest BSD, MIT or Apache-2.0)",
    "unfairplay-modules": "each Go module compiled into UnfairPlay, at its version",
}


def _folder_note(name: str, folder: Path) -> str:
    """What INDEX.txt says a license folder holds: FOLDER_NOTES', or for
    rust-crates/ the packages and tools it has crates for."""
    if name != "rust-crates":
        return FOLDER_NOTES.get(name, "license files")
    owners = sorted(p.name for p in folder.iterdir() if p.is_dir())
    named = ", ".join(owners[:-1]) + (" and " if len(owners) > 1 else "") + owners[-1] if owners else "nothing"
    return (f"each Rust crate compiled into {named}, by package or tool and crate"
            + (" (ipatool's are the crates.io crates its embedded 1Password SDK core shows it holds, by "
               "source path, function name or wasm-bindgen record: 1Password publishes no Cargo.lock for it, so a "
               "crate that left none of these isn't seen, and a version the core doesn't carry is inferred, as "
               "bundle/ipatool-core-crates.lock in the source archive says)"
               if "ipatool" in owners else ""))


def _index_origin(name: str, pins: dict[str, Pin]) -> str:
    """What INDEX.txt says a pin is to the app: the file bundled, the source
    built here, or the toolchain (RC audit #3)."""
    url = pins[name].url
    kind = pin_kind(name, pins)
    if kind == "built-from-source":
        return f"built here from {url}"
    if kind == "toolchain":
        return f"the toolchain a binary is built with, {url} (its standard library is in that binary)"
    return url


def collect_licenses(contents: Path, pins: dict[str, Pin], extra_dir: Path) -> Path:
    """Resources/Licenses: the GPL text, every Python package's license files,
    the tools' (`extra_dir`), each of cyan's iOS frameworks' (cyan-extras/),
    and INDEX.txt naming every component."""
    lic = contents / "Resources" / "Licenses"
    lic.mkdir(parents=True, exist_ok=True)
    shutil.copy2(GPL_TEXT, lic / "COPYING-GPL-3.0.txt")

    packages, graft = [], []
    project = _norm(tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["name"])
    for dist, meta in _installed_dists(contents):
        name, version = meta["Name"], meta["Version"]
        if _norm(name) == project:
            graft.append(f"{name}-{version}")
        files = _dist_licence_files(dist)
        upstream = VENDORED_LICENSES / f"{_norm(name)}-LICENSE"
        from_upstream = not files and upstream.is_file()
        if from_upstream:
            head = upstream.read_text().split("\n", 1)[0]
            if not head.startswith(f"# {_norm(name)} {version}."):
                raise BundleError(f"{upstream.relative_to(REPO)} is for {head.removeprefix('# ').split('.')[0]!r}, "
                                  f"but {name} {version} is installed: take the license text at {version}")
            files = [(upstream, Path("LICENSE"))]
        for src, rel in files:
            out = lic / "python-packages" / f"{name}-{version}" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, out)
        stated = _dist_licence(meta)
        if stated is None:
            stated = "not stated in its metadata" + ("; see its license file" if files else "")
        if from_upstream:
            note = " (the package ships no license file; LICENSE is from its upstream repository)"
        else:
            note = "" if files else " (the package ships no license file)"
        packages.append(f"{name} {version} — {stated}{note}")

    extras = sorted(f.name for f in extra_dir.iterdir() if f.is_file())
    for name in extras:
        shutil.copy2(extra_dir / name, lic / name)
    folders = sorted(f.name for f in extra_dir.iterdir() if f.is_dir())
    for name in folders:
        shutil.copytree(extra_dir / name, lic / name)
    frameworks = cyan_frameworks(contents)
    for fw in frameworks:
        out = lic / fw.licence_file
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CYAN_EXTRAS_LICENSES / fw.known.text, out)

    lines = [
        "Graft.app: bundled components and their licenses",
        "",
        LICENCE_STATEMENT,
        "The GPL-3.0 text is COPYING-GPL-3.0.txt, in this folder.",
        "",
        "Components (component — license — the file bundled, or what it is built from):",
        *(f"{name} — {pin.license} — {_index_origin(name, pins)}" for name, pin in pins.items()
          if name not in LICENCE_TEXT_PINS),
        f"cyan's helpers ({', '.join(CYAN_HELPERS)}) — built from the ldid, cctools and insert_dylib "
        "sources above (ldid linking libplist and OpenSSL), in place of the prebuilt copies cyan's archive carries",
        *([f"{STUB_COMPONENT} — MIT — built here from Graft's own source (graft_ipa/stubs/preferences-stub/), "
           "iOS code PlayCover puts in an app it installs; its license is Graft's, in "
           + (f"python-packages/{graft[0]}/" if graft else "python-packages/graft-ipa-<version>/")]
          if (contents / STUB_IN_APP / "Preferences").is_file() else []),
        *(["cyan's iOS frameworks (cyan/extras), which cyan injects into an IPA when a tweak needs one, "
           "prebuilt as cyan's archive carries them (framework — license — what it is — its license text, "
           "in this folder):",
           *(f"  {fw.name} — {fw.known.license} — {fw.known.upstream} — {fw.licence_file}" for fw in frameworks)]
          if frameworks else []),
        "",
        "License texts only, nothing from them is in the app: the licenses of code linked into python3.13, "
        "below (component — license — where the text comes from):",
        *(f"  {name} — {pin.license} — {pin.url}" for name, pin in pins.items() if name in LICENCE_TEXT_PINS),
        "",
        "License and notice files for those components, in this folder:",
        *(f"  {name}" for name in extras),
        *(f"  {name}/ — {_folder_note(name, extra_dir / name)}" for name in folders),
        "",
        "Code linked into a binary above has its notice here too: python-<library>-LICENSE.txt for each library "
        "python-build-standalone builds into python3.13 (" + ", ".join(
            sorted(f.name.removeprefix("python-").removesuffix("-LICENSE.txt") for f in lic.glob("python-*-LICENSE.txt")))
        + "); ipatool-modules/ and unfairplay-modules/ for the Go modules in each; rust-crates/<package>/ for the "
        "Rust crates in a package's compiled module, and rust-crates/ipatool/ for those ipatool's embedded "
        "1Password SDK core shows it holds.",
        "",
        "components.json, in this folder, lists all of the above with each one's exact file and SHA-256.",
        "",
        "Python packages (name version — license; their license files are in python-packages/<name>-<version>/):",
        *packages,
        "",
    ]
    (lic / "INDEX.txt").write_text("\n".join(lines))
    return lic


#: Pins whose artifact is source code the stage compiles, rather than files it ships.
BUILT_FROM_SOURCE = {"openssl", "libplist", "ldid", "cctools", "insert_dylib", "ipatool", "unfairplay"}
#: Pins that are not themselves in the app: the toolchain a binary is built
#: with, and the source of a binary that is (`source` in pins.toml).
TOOLCHAINS = {"go"}


def pin_kind(name: str, pins: dict[str, Pin]) -> str:
    if name in BUILT_FROM_SOURCE:
        return "built-from-source"
    if name in TOOLCHAINS:
        return "toolchain"
    if name in LICENCE_TEXT_PINS:
        return "licence-text"
    if any(p.source == name for p in pins.values()):
        return "source"
    return "artifact"


def _downloads(reports: Path) -> dict[str, tuple[str, str, str | None]]:
    """name → (version, url, sha256) for every distribution in the pip
    installation reports (`pip install --report`) under `reports`."""
    found = {}
    for f in sorted(reports.glob("*.json")):
        for item in json.loads(f.read_text())["install"]:
            info = item["download_info"]
            sha = ((info.get("archive_info") or {}).get("hashes") or {}).get("sha256")
            found[_norm(item["metadata"]["name"])] = (item["metadata"]["version"], info["url"], sha)
    return found


def component_manifest(contents: Path, pins: dict[str, Pin], reports: Path) -> list[dict[str, str]]:
    """Every pinned artifact and every installed Python package, each with the
    exact file it came from, that file's SHA-256, and its license.

    A package pip has no hash for, or one installed from a local file that is
    neither a pin nor graft's own wheel, is refused: the manifest would
    otherwise vouch for something nothing checked, or name this machine.
    """
    entries = [{"name": built, "version": pin.version, "kind": pin_kind(name, pins),
                "source_url": pin.url, "sha256": pin.sha256, "license": pin.license}
               for name, pin in pins.items() for built in pin.builds or (name,)]
    downloads = _downloads(reports)
    pinned = {pin.sha256: pin for pin in pins.values()}
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    for dist, meta in _installed_dists(contents):
        name, version = meta["Name"], meta["Version"]
        got_version, url, sha = downloads.get(_norm(name), (None, None, None))
        if sha is None or got_version != version:
            raise BundleError(f"{name} {version} is installed, but no pip report records the file it came "
                              f"from with a SHA-256 ({dist.name})")
        pin = pinned.get(sha)
        if not url.startswith("https://"):
            if pin is not None:
                url = pin.url
            elif _norm(name) == _norm(project["name"]):
                url = project["urls"]["Homepage"]
            else:
                raise BundleError(f"{name} {version} was installed from a local file that is no pinned artifact")
        licence = _dist_licence(meta) or (pin.license if pin else None)
        if licence is None:
            licence = "not stated in its metadata" + ("; see its license file" if _dist_licence_files(dist) else "")
        entries.append({"name": name, "version": version, "kind": "python-package",
                        "source_url": url, "sha256": sha, "license": licence})
    stub = contents / STUB_IN_APP / "Preferences"
    if stub.is_file():
        entries.append({"name": STUB_COMPONENT, "version": project["version"], "kind": "built-from-source",
                        "source_url": f"{project['urls']['Homepage']}#graft_ipa/stubs/preferences-stub",
                        "sha256": _sha256(stub), "license": "MIT"})
    # cyan's iOS frameworks: each binary as staged, which is as cyan's archive
    # carries it, and as it ships: graft-release never signs iOS code.
    frameworks = cyan_frameworks(contents)
    if frameworks:
        cyan = pins["cyan"]
        package = contents / CYAN_EXTRAS.parent
        for fw in frameworks:
            entries.append({"name": fw.name, "version": fw.known.upstream_version, "bundle_version": fw.version,
                            "kind": "artifact",
                            "source_url": f"{cyan.url}#{cyan_archive_member(cyan, fw.binary.relative_to(package))}",
                            "sha256": _sha256(fw.binary), "license": fw.known.license,
                            "cyan_commit": cyan.version})
    return entries


#: The stub's components.json name, which verify-bundle reads its hash by.
STUB_COMPONENT = "Preferences.framework (PlayCover stub)"


#: Where a pin's license files are in Licenses/, when not `<pin>-*`.
LICENCE_PREFIX = {"hacl": "python-hacl-"}


def licence_files_for(entry: dict, lic: Path, pins: dict[str, Pin]) -> list[str]:
    """The files in Licenses/ (`lic`) that carry a components.json entry's
    license and notices, for verify-bundle (review #53)."""
    def files(pattern: str) -> list[str]:
        return sorted(str(p.relative_to(lic)) for p in lic.glob(pattern) if p.is_file())

    name, kind = entry["name"], entry["kind"]
    if kind == "licence-text":
        return []
    if kind == "python-package":
        return files(f"python-packages/{name}-{entry['version']}/**/*") + files(f"rust-crates/{_norm(name)}/**/*")
    if "cyan_commit" in entry:
        return files(f"cyan-extras/{name.removesuffix('.framework')}-LICENSE")
    if name == STUB_COMPONENT:  # Graft's own code
        return files("python-packages/graft-ipa-*/**/*")
    owner = next((n for n, pin in pins.items() if name in (pin.builds or (n,))), name)
    if owner == "cyan":
        return files("python-packages/cyan-*/**/*")
    prefix = LICENCE_PREFIX.get(owner, f"{owner}-")
    return files(f"{prefix}*") + files(f"{owner}-modules/**/*") + files(f"rust-crates/{owner}/**/*")


def write_components(contents: Path, pins: dict[str, Pin], reports: Path) -> Path:
    """Resources/Licenses/components.json, from component_manifest()."""
    out = contents / "Resources" / "Licenses" / "components.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    entries = component_manifest(contents, pins, reports)
    for entry in entries:
        entry["licence_files"] = licence_files_for(entry, out.parent, pins)
    out.write_text(json.dumps(entries, indent=2) + "\n")
    return out


def _build_paths(dest: Path) -> set[str]:
    """What names the release machine and must not appear in anything staged:
    this checkout, the stage, the account's real home folder (from pwd, not
    $HOME, review #59), and its username, bare when long enough not to turn
    up in random bytes."""
    import pwd
    me = pwd.getpwuid(os.getuid())
    paths = {str(REPO), str(dest), str(dest.resolve()), f"/Users/{me.pw_name}/"}
    if len(Path(me.pw_dir).parts) >= 3:          # /Users/<name>, not / or /var/root-like oddities
        paths.add(me.pw_dir)
    if len(me.pw_name) >= 5:
        paths.add(me.pw_name)
    return paths


def check_no_build_paths(dest: Path) -> None:
    """Refuse a stage whose files name this checkout, the stage, or the home
    folder: the download is public, and those name the person who built it."""
    needles = [p.encode() for p in _build_paths(dest)]
    leaks = [p.relative_to(dest) for p in sorted(dest.rglob("*"))
             if p.is_file() and not p.is_symlink() and any(n in p.read_bytes() for n in needles)]
    if leaks:
        shown = ", ".join(str(p) for p in leaks[:10])
        raise BundleError(f"{len(leaks)} staged file(s) name the release machine's paths: {shown}")


def stage(dest: Path, work: Path = WORK) -> list[Path]:
    """Everything Graft.app bundles, staged in `dest` (shaped like Contents),
    plus dest/macho-list.txt. Returns the Mach-O files to sign, deepest first."""
    if dest.exists() and any(dest.iterdir()):
        raise BundleError(f"{dest} is not empty; stage into a new directory")
    dest.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    pins = load_pins(REPO / "bundle" / "pins.toml")

    reports = _fresh(work / "pip-reports")
    gp = stage_python(pins, dest, reports)
    # Before minutes of building: a framework in cyan/extras whose license
    # isn't known stops the stage here (collect_licenses checks again).
    cyan_frameworks(dest)
    prefix = build_openssl(pins["openssl"], work)
    zsign = _the_binary(extract(fetch(pins["zsign"]), _fresh(work / "zsign-release")), "zsign")
    stage_openssl_and_zsign(prefix, zsign, dest)
    licences = vendored_licences(pins, work)
    stage_ipatool(pins, dest, licences, work)
    stage_unfairplay(pins, dest, licences, work)
    python_static_licences(pins, gp, licences)
    rust_crate_licences(dest, lock_hashes(RUNTIME_LOCK), licences / "rust-crates")
    binary_crate_licences(dest / "Helpers" / "bin" / "ipatool", "ipatool", licences / "rust-crates")
    helpers = build_cyan_helpers(pins, prefix, work, licences)
    stage_cyan_helpers(helpers, dest, cyan_original_hashes(fetch(pins["cyan"])))
    shutil.copy2(prefix / "LICENSE.txt", licences / "openssl-LICENSE.txt")
    shutil.copy2(gp / "lib" / "python3.13" / "LICENSE.txt", licences / "python-LICENSE.txt")
    build_launchers(dest)
    stage_preferences_stub(dest, work)
    # Every Mach-O is staged by now, so this sees anything that could link it.
    linkers = prune_unlinked_libpython(dest)
    print(f"{LIBPYTHON.name}: " + (f"kept, linked by {', '.join(map(str, linkers))}" if linkers
                                   else "removed (no staged Mach-O links it)"))
    print(f"thinned to arm64: {len(thin_to_arm64(dest))} universal Mach-O files")
    for path, rpath in prune_foreign_rpaths(dest):
        print(f"deleted rpath {rpath} from {path.relative_to(dest)}")
    collect_licenses(dest, pins, licences)
    missing = crate_notice_problems(dest, macho_files(dest))
    if missing:
        raise BundleError("Rust crates without their license notices: " + "; ".join(missing))
    write_components(dest, pins, reports)
    check_no_build_paths(dest)

    found = signed_machos(dest)
    (dest / "macho-list.txt").write_text("".join(f"{p.relative_to(dest)}\n" for p in found))
    write_entitlements_list(dest, found)
    return found


def write_entitlements_list(dest: Path, found: list[Path]) -> None:
    """dest/entitlements.txt: `<path under Contents>\t<file in bundle/entitlements>`
    for each Mach-O graft-release signs with entitlements (ENTITLED). Each must
    be a staged Mach-O, and its entitlements file must exist."""
    staged = {str(p.relative_to(dest)) for p in found}
    for rel, plist in ENTITLED.items():
        if rel not in staged:
            raise BundleError(f"{rel} has entitlements (ENTITLED) but isn't a staged Mach-O")
        if not (ENTITLEMENTS_DIR / plist).is_file():
            raise BundleError(f"{rel}'s entitlements file bundle/entitlements/{plist} is missing")
    (dest / "entitlements.txt").write_text("".join(f"{rel}\t{plist}\n" for rel, plist in sorted(ENTITLED.items())))


# ------------------------------------------------------------ the source archive

#: Licenses whose terms want the source of what's distributed, as
#: components.json words them: GPL, LGPL, AGPL, MPL, EPL, APSL, and frida's
#: wxWindows Library License (the LGPL with an exception).
#: GPL, LGPL and AGPL however written (GPL-3.0, GPLv3, GPLv2+, LGPLv3+),
#: MPL, EPL, EUPL and APSL, and the long names (review #33: the word boundary
#: after GPL missed "GPLv3", and EUPL was absent).
_COPYLEFT = re.compile(r"(?<![A-Za-z])(?:A|L)?GPL(?:v\d|(?![A-Za-z]))|(?<![A-Za-z])(?:MPL|EPL|EUPL|APSL)(?![A-Za-z])|"
                       r"General Public Licen[cs]e|Mozilla Public|Eclipse Public|European Union Public|"
                       r"Apple Public Source|wxWindows", re.IGNORECASE)


def is_copyleft(licence: str) -> bool:
    return bool(_COPYLEFT.search(licence))


def needs_source(licence: str) -> bool:
    """Whether a package's source must ship: copyleft, or a license its
    metadata doesn't state, which nothing here can call permissive (#33)."""
    return is_copyleft(licence) or licence.startswith("not stated")


#: Left out of the archive's copy of Graft's source, as out of every public
#: export: internal planning notes, and tweak sources that aren't part of the app.
SOURCE_EXCLUDES = ("docs/superpowers", "tweaks-src")


def graft_source_tar(dest: Path, prefix: str, repo: Path = REPO, commit: str | None = None) -> tuple[str, int]:
    """`git archive` of `commit` (graft-release's, recorded before it built
    from an export of it) or else of HEAD into `dest` (a tar), under `prefix`,
    without SOURCE_EXCLUDES. Returns the git tree it archived (archived_tree)
    and the commit's time, never the commit: that is the private
    repository's, which never exists on GitHub (review 2.0, bundle #8). For
    HEAD, a checkout with any change, untracked files included, is refused:
    hatchling packs untracked files into the wheel, and git archive doesn't
    (review #9)."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout

    if commit is None:
        dirty = git("status", "--porcelain").splitlines()
        if dirty:
            raise BundleError("the checkout has changes, so HEAD isn't what was built; commit them (or remove "
                              "untracked files) and build again: " + ", ".join(line[3:] for line in dirty[:10]))
        commit = "HEAD"
    else:
        r = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{commit}^{{commit}}"], capture_output=True)
        if r.returncode != 0:
            raise BundleError(f"{commit} is not a commit in {repo}")
    commit, when = git("log", "-1", "--format=%H %ct", commit).split()
    git("archive", "--format=tar", f"--prefix={prefix}", "-o", str(dest), commit, "--", ".",
        *(f":(exclude){p}" for p in SOURCE_EXCLUDES))
    tree, files = archived_tree(repo, commit)
    check_archived_tree(files, dest, prefix, git("rev-parse", "--show-object-format").strip())
    return tree, int(when)


def archived_tree(repo: Path, commit: str) -> tuple[str, dict[str, tuple[str, str]]]:
    """The git tree of `commit` without SOURCE_EXCLUDES, and its files, each
    path -> (mode, object id): the tree a public export of the commit, made
    with the same exclusions, commits, so `git rev-parse HEAD^{tree}` there
    gives the same hash. Written into a throwaway index and object folder:
    nothing is added to `repo`."""
    common = subprocess.run(["git", "-C", str(repo), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                            check=True, capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="graft-tree-") as tmp:
        (Path(tmp) / "objects").mkdir()     # git won't find the repository without it
        env = {**os.environ, "GIT_INDEX_FILE": f"{tmp}/index", "GIT_OBJECT_DIRECTORY": f"{tmp}/objects",
               "GIT_ALTERNATE_OBJECT_DIRECTORIES": f"{common}/objects"}

        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True,
                                  env=env).stdout
        git("read-tree", commit)
        git("rm", "-r", "-q", "--cached", "--ignore-unmatch", "--", *SOURCE_EXCLUDES)
        tree = git("write-tree").strip()
        files = {}
        for line in git("ls-tree", "-r", "-z", "--full-tree", tree).split("\0"):
            if "\t" in line:
                meta, path = line.split("\t", 1)
                mode, _, oid = meta.split()
                files[path] = (mode, oid)
    return tree, files


def git_blob_id(data: bytes, object_format: str = "sha1") -> str:
    """The id git gives a blob holding `data`, in the repository's object
    format (sha1, or sha256)."""
    return hashlib.new(object_format, b"blob %d\0" % len(data) + data).hexdigest()


def check_archived_tree(files: dict[str, tuple[str, str]], tar: Path, prefix: str, object_format: str = "sha1") -> None:
    """The archive holds exactly the tree's files (`archived_tree`), each
    with the tree's contents and mode, so it is the tree SOURCES.md names.

    By name: an export-ignore, which drops a file, makes them differ. By
    contents: an export-subst, which rewrites a file's git Format
    placeholders and can expand one to the private commit, keeps every name
    and used to pass (the delta audit's d14-bundle-4). git archive applies
    it from a committed .gitattributes and from the checkout's own
    .git/info/attributes, which nothing reviews; a smudge filter or an
    end-of-line conversion would differ the same way. Each regular file's
    bytes are hashed as a git blob, each symlink's target too, and compared
    with the tree's object ids; the executable bit with its mode."""
    archived: dict[str, tuple[str, str]] = {}
    with tarfile.open(tar) as tf:
        for m in tf.getmembers():
            if m.isdir():
                continue
            if m.issym():
                got = ("120000", git_blob_id(m.linkname.encode(), object_format))
            elif m.isfile():
                got = ("100755" if m.mode & 0o111 else "100644", git_blob_id(tf.extractfile(m).read(), object_format))
            else:
                got = ("not a file", "")
            archived[m.name.removeprefix(prefix)] = got
    if archived.keys() != files.keys():
        raise BundleError(f"git archive wrote other files than the tree holds (an export-ignore?): "
                          f"{sorted(archived.keys() ^ files.keys())[:10]}")
    changed = sorted(path for path, entry in files.items() if archived[path] != entry)
    if changed:
        raise BundleError(f"git archive wrote {len(changed)} file(s) whose contents or mode aren't the tree's (an "
                          "export-subst, in .gitattributes or .git/info/attributes, can put the private commit "
                          f"in them; or a filter, or an end-of-line conversion): {changed[:10]}")


def lock_hashes(lock: Path) -> dict[str, tuple[str, set[str]]]:
    """Each package a pip-compile lock pins: normalized name -> (version, every SHA-256 it allows)."""
    found: dict[str, tuple[str, set[str]]] = {}
    current = None
    for line in lock.read_text().splitlines():
        if m := re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)", line):
            current = _norm(m.group(1))
            found[current] = (m.group(2), set())
        elif not line.startswith(" "):
            current = None
        if current:
            found[current][1].update(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
    return found


def _pypi_sdist(name: str, version: str) -> tuple[str, str] | None:
    """(URL, SHA-256) of the one source distribution PyPI has for
    name==version, or None when it has none (a wheel-only release)."""
    with _urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as r:
        files = json.load(r)["urls"]
    sdists = [f for f in files if f["packagetype"] == "sdist"]
    if not sdists:
        return None
    if len(sdists) != 1:
        raise BundleError(f"PyPI lists {len(sdists)} source distributions for {name} {version}")
    return sdists[0]["url"], sdists[0]["digests"]["sha256"]


class NoSdist(BundleError):
    """PyPI has no source distribution for a release."""


def sdist_pin(name: str, version: str, licence: str, locked: dict[str, tuple[str, set[str]]]) -> Pin:
    """The source distribution of a bundled package, as a Pin: PyPI names the
    file, and bundle/requirements.lock must already allow its SHA-256 (pip-compile
    --generate-hashes lists every file of the version), so fetch() checks it."""
    key = _norm(name)
    if locked.get(key, ("",))[0] != version:
        raise BundleError(f"{name} {version} is bundled but bundle/requirements.lock doesn't pin it")
    found = _pypi_sdist(key, version)
    if found is None:
        raise NoSdist(f"PyPI has no source distribution for {name} {version}")
    url, sha = found
    if sha not in locked[key][1]:
        raise BundleError(f"{name} {version}: PyPI's source distribution {url.rsplit('/', 1)[-1]} (SHA-256 {sha}) "
                          "isn't one of the hashes bundle/requirements.lock allows; re-lock with scripts/lock-bundle")
    if not url.startswith("https://files.pythonhosted.org/"):
        raise BundleError(f"{name} {version}: PyPI serves its source distribution from {url}")
    return Pin(name=key, url=url, sha256=sha, license=licence, version=version)


def _unfairplay_recipe(pin: Pin) -> tuple[str, list[str], list[list[str]], str]:
    repo, commit = source_commit(pin)
    return ("the archive's top directory, unpatched, with the Go toolchain below first on PATH",
            ["Helpers/bin/unfairplay"], unfairplay_steps(pin.version, repo, commit),
            "With these variables set: " + ", ".join(f"`{k}={v}`" for k, v in UNFAIRPLAY_ENV.items())
            + f". The build is upstream's release build (its `.github/workflows/release.yml`), with the Go "
            "below where upstream's used Go 1.26.0. Upstream builds in a checkout of the tag; here the files are "
            f"this archive's own, and git fetches only the tag's commit (`{commit}`), made HEAD and the index "
            "without touching them, so Go records that commit in the binary's build info (`go version -m`: "
            "`vcs.revision`, and `vcs.modified=false` only if the archive is exactly the commit's tree). The "
            "release refuses a binary whose build info says otherwise. `go mod vendor` downloads each module "
            f"from {GOPROXY}, checked against UnfairPlay's `go.sum` and the checksum database, and `go mod "
            "verify` re-hashes the module cache's copies; `pinned/unfairplay/vendor.tar.gz` is the `vendor/` "
            "it wrote, which the build then reads offline. To build offline, unpack that into the archive's top "
            "directory, leave out the git steps, `go mod vendor` and `go mod verify`, and build with "
            "`-buildvcs=false`: the same code, with no VCS stamp in its build "
            "info. `unfairplay` goes in as built and is signed with the hardened runtime, the identifier "
            "`unfairplay` (the administrator step checks its copy of UnfairPlay against it) and no entitlements.")


def _recipes(ipatool_version: str = "", unfairplay: Pin | None = None,
             ) -> dict[str, tuple[str, list[str], list[list[str]], str]]:
    """Per built-from-source pin: where its steps run, what it becomes in the
    app, the steps (as the build runs them, but -j and its own paths), and
    what is done to the result."""
    tools = str(CYAN_TOOLS)
    to_fw = f"@loader_path/{os.path.relpath('Frameworks', tools)}"
    return {
        **({"unfairplay": _unfairplay_recipe(unfairplay)} if unfairplay else {}),
        "openssl": ("the archive's top directory",
                    ["Frameworks/libssl.3.dylib", "Frameworks/libcrypto.3.dylib", "Frameworks/ossl-modules/legacy.dylib"],
                    openssl_steps("$DESTDIR", []),
                    "From `$DESTDIR/opt/graft/openssl/lib`, each library gets `install_name_tool -id @rpath/<name>`, "
                    "and libssl's and legacy.dylib's link to libcrypto becomes `@loader_path/libcrypto.3.dylib` "
                    "(`@loader_path/../libcrypto.3.dylib`). zsign, a prebuilt release, has its links to OpenSSL "
                    "changed to `@executable_path/../../Frameworks/<name>`."),
        "libplist": ("the archive's top directory", [f"Frameworks/{LIBPLIST}"],
                     libplist_steps("$DESTDIR", []),
                     f"`$DESTDIR/opt/graft/libplist/lib/{LIBPLIST}` (the file that link points at) is copied into "
                     f"Frameworks and given `install_name_tool -id @rpath/{LIBPLIST}`. It stays a separate library, "
                     "so you can replace it with your own build: work on a copy of Graft.app. ldid is signed with "
                     "the hardened runtime, whose library validation loads only libraries signed by the release "
                     "team, so after replacing it, re-sign ldid ad hoc, which drops the runtime: "
                     f"`codesign --force --sign - {tools}/ldid`. The same goes for pylzss's `lzss` module (LGPL-3.0) "
                     "and the `python3.13` that loads it. Last, re-seal the whole app ad hoc, "
                     "`codesign --force --sign - Graft.app`, or macOS reports it as damaged. That replaces the "
                     "release team's signature, so Set Up Decryption can no longer check its copy of UnfairPlay "
                     "against the team, and runs it only after you press Run Unchecked Copy as Root…."),
        "ldid": ("the archive's top directory", [f"{tools}/ldid"],
                 ldid_steps("$LIBPLIST", "$OPENSSL", []),
                 f"`$LIBPLIST` holds libplist's `include/` and `{LIBPLIST}` from its build above; `$OPENSSL` holds "
                 f"OpenSSL's `include/` and `lib/{LIBCRYPTO}`. `install_name_tool -change` then points ldid's two "
                 f"links at `{to_fw}/{LIBCRYPTO}` and `{to_fw}/{LIBPLIST}`."),
        "cctools": ("`cctools/` in the archive's top directory",
                    [f"{tools}/lipo", f"{tools}/install_name_tool", f"{tools}/otool"],
                    cctools_steps([]),
                    "`misc/lipo`, `misc/install_name_tool` and `otool/otool` go in as built."),
        "insert_dylib": ("the archive's top directory", [f"{tools}/insert_dylib"],
                         insert_dylib_steps(), "`insert_dylib/insert_dylib` goes in as built."),
        "ipatool": ("the archive's top directory, unpatched, with the Go toolchain below first on PATH",
                    ["Helpers/bin/ipatool"], ipatool_steps(ipatool_version),
                    "With these variables set: " + ", ".join(f"`{k}={v}`" for k, v in IPATOOL_ENV.items())
                    + f". `go mod vendor` downloads each module from {GOPROXY}, checked against ipatool's "
                    "`go.sum` and the checksum database, and `go mod verify` re-hashes the module cache's "
                    "copies; `pinned/ipatool/vendor.tar.gz` is the `vendor/` it wrote, which the build then reads "
                    "offline (leave out both `go mod` steps). `ipatool` goes in as built and is signed with "
                    "two entitlements (`bundle/entitlements/ipatool.plist`): `com.apple.security.cs.allow-jit`, "
                    "for Unicorn's code translator, and `com.apple.security.cs.disable-library-validation`, so "
                    "it can load the Unicorn it downloads, which is ad hoc signed. Unicorn is not in Graft.app: "
                    "on first use ipatool downloads Unicorn 2.1.4's macOS wheel from PyPI into "
                    "`~/Library/Caches/ipatool`, checked against the SHA-256s its source pins "
                    "(`internal/sap/unicorn/artifact.go`), as upstream ipatool does. The 1Password SDK module "
                    "carries a prebuilt WebAssembly core (`internal/wasm/core.wasm`), built by 1Password from "
                    "source it doesn't publish, with no Cargo.lock; `graft/bundle/ipatool-core-crates.lock` lists "
                    "the crates.io crates the core shows it holds, by source path, function name or wasm-bindgen "
                    "record (its header says how a version the core doesn't carry was inferred, and that a crate "
                    "that left none of these can't be seen), and the source of each under a copyleft license is "
                    "here: "
                    + (", ".join(f"`pinned/ipatool/core-crates/{c}-{v}.crate` ({lic})"
                                 for c, v, lic in core_crate_sources()) or "none") + "."),
    }


def shell_line(step: list[str]) -> str:
    """A step as a shell line to copy: quoted, but $PLACEHOLDERS still expand."""
    def arg(a: str) -> str:
        if "$" not in a:
            return shlex.quote(a)
        return '"' + re.sub(r'(["\\`])', r"\\\1", a) + '"'
    return " ".join(arg(a) for a in step)


def _toolchain() -> str:
    """What SOURCES.md says built the binaries: toolchain_id(), the one every
    build's cache key carries, and the macOS it ran on."""
    try:
        macos = subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True, timeout=30,
                               env=_build_env()).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        macos = "unknown"
    return f"{toolchain_id()}, on macOS {macos}"


def _unbuilt_reason(pin: Pin) -> str:
    """Why an archive with no build recipe is in the source archive. cyan's
    license is the Unlicense: it is here because the app installs it from
    its pinned zip, which this archive is without `cyan/tools/` (combined
    fix check #11)."""
    if pin.name == "cyan":
        return ("Not built: cyan is a Python package, installed into the app's Python from its pinned zip, so "
                "this archive, that zip without the files left out, is its source.")
    if is_copyleft(pin.license):
        return "Not built: its archive is here because its license is copyleft."
    return "Not built: its archive is here as the source of what the app holds from it."


def sources_md(top: str, tree: str, pins: dict[str, Pin], archived: list[Pin], sdists: list[Pin],
               no_sdist: list[dict] = (), repacked: dict[str, tuple[str, list[str]]] | None = None) -> str:
    """SOURCES.md: for each binary in the app, what it was built from and how."""
    recipes = _recipes(pins["ipatool"].version if "ipatool" in pins else "", pins.get("unfairplay"))
    lines = [
        f"# {top}",
        "",
        "The source for Graft.app's download: Graft's own, every source archive a bundled binary was built from, "
        "the archive cyan is installed from, and the source distribution of every bundled Python package that "
        f"publishes one (those that don't are listed below). {LICENCE_STATEMENT} Inside the app, "
        "`Contents/Resources/Licenses/components.json` lists every component with the exact file it came from "
        "and that file's SHA-256.",
        "",
        "## Graft",
        "",
        f"`graft/` is Graft's source, git tree `{tree}` (`git archive`, without "
        f"{' and '.join(f'`{p}`' for p in SOURCE_EXCLUDES)}: planning notes and tweak sources, not part of the "
        "app). Graft's public repository holds the same tree without them: in it, `git rev-parse "
        "<release commit>^{tree}` gives that hash.",
        "",
        "- `scripts/graft-release` builds the app: `xcodebuild -scheme Graft -configuration Release "
        "ARCHS=\"arm64 x86_64\"` (see the script for every setting), then signs and checks it.",
        "- `scripts/graft_bundle.py stage` stages everything else: the CLI as a wheel built from this source, "
        "its Python and dependencies, the launchers (`clang` from `bundle/launcher.c`), the tools, and the "
        "builds below.",
        "",
        "## Built from source",
        "",
        f"Built for arm64 and macOS {MACOS_MIN} or later with {_toolchain()}. The commands run as shown, plus `-j` for make. "
        "Each runs with only `/usr/bin:/bin:/usr/sbin:/sbin` on PATH and "
        f"`MACOSX_DEPLOYMENT_TARGET={MACOS_MIN}`. In the app, every binary is then signed with the hardened runtime.",
    ]
    for pin in archived:
        where, becomes, steps, then = recipes.get(pin.name, ("", [], [], _unbuilt_reason(pin)))
        lines += [
            "",
            f"### {pin.name}",
            "",
            f"- Version: {pin.version}",
            f"- License: {pin.license}",
        ]
        if pin.name in (repacked or {}):
            sha, left = repacked[pin.name]
            tools = sorted({n.rsplit("/", 1)[-1] for n in left})
            platforms = sorted({"/".join(n.split("/")[-3:-1]) for n in left})
            lines += [
                f"- Archive: `pinned/{pin.name}/{archived_filename(pin)}`, SHA-256 `{sha}`: <{pin.url}> (SHA-256 "
                f"`{pin.sha256}`) without `{CYAN_LEFT_OUT}`. Every other file keeps its contents, times and "
                "mode, and the zip keeps its comment; only the compression differs, so its SHA-256 isn't the pin's",
                f"- Left out: `{CYAN_LEFT_OUT}`, {len(left)} prebuilt executables ({', '.join(tools)}, for "
                f"{', '.join(platforms)}) that upstream built with no source for those builds. Graft.app carries "
                "none of them: it builds cyan's five Darwin arm64 helpers from the ldid, cctools and insert_dylib "
                "sources here, in their place, and removes the other platforms'.",
            ]
        else:
            lines.append(f"- Archive: `pinned/{pin.name}/{pin.filename}`, SHA-256 `{pin.sha256}`, from <{pin.url}>")
        if becomes:
            lines.append(f"- In the app: {', '.join(f'`Contents/{b}`' for b in becomes)}")
        patched = patch_hashes(pin.name)
        if patched:
            lines += [f"- Patched first, in the archive's top directory, with Graft's "
                      f"{'patch' if len(patched) == 1 else 'patches'} (`$GRAFT` is this archive's `graft/`): "
                      + ", ".join(f"`bundle/patches/{p}` (SHA-256 `{sha}`)" for p, sha in patched),
                      "", "```sh", *(shell_line([*PATCH_ARGV, f"$GRAFT/bundle/patches/{p}"]) for p, _ in patched),
                      "```", ""]
        if steps:
            lines += [f"- In {where}:", "", "```sh", *(shell_line(step) for step in steps), "```", ""]
        lines.append(then)
    lines += [
        "",
        "### Preferences.framework (PlayCover's stub)",
        "",
        f"- In the app: `Contents/{STUB_IN_APP}`, iOS code, so not signed by the release (PlayCover signs it "
        "on install).",
        "- In `graft/graft_ipa/stubs/preferences-stub/` (Graft's own source, MIT), with the release Mac's Xcode "
        "and its iOS SDK, then its `Info.plist` copied in:",
        "",
        "```sh",
        "mkdir Preferences.framework",
        *(shell_line(step) for step in stub_steps()),
        "```",
        "",
        "## Python packages",
        "",
        "The app installs every package as a wheel (each wheel's SHA-256 is in components.json). These are the "
        "same versions' source distributions from PyPI, each matching a hash in `bundle/requirements.lock`; "
        "graft's own is `graft/` and cyan's is its pinned archive above.",
        "",
        "| Package | Version | License | Source distribution | SHA-256 |",
        "|---|---|---|---|---|",
        *(f"| {p.name} | {p.version} | {p.license} | `sdists/{p.filename}` | `{p.sha256}` |" for p in sdists),
        "",
        *([f"PyPI has no source distribution for these, all under permissive licenses; each one's source is at "
           f"its project's repository: {', '.join(e['name'] + ' ' + e['version'] for e in no_sdist)}.", ""]
          if no_sdist else []),
        "## Not built here",
        "",
        "These are upstream release files, with their URLs in components.json: "
        + ", ".join(f"{n} {p.version} ({p.license})" for n, p in pins.items()
                    if pin_kind(n, pins) == "artifact" and n != "cyan")
        + ". Those under a copyleft license have their source above. cyan's iOS frameworks go in as cyan's "
        "archive carries them. " + ", ".join(
            f"{n} {p.version} ({p.license}) is the toolchain a binary above is built with"
            for n, p in pins.items() if n in TOOLCHAINS) + ".",
        "",
    ]
    return "\n".join(lines)


def _anonymous(info: tarfile.TarInfo, mtime: int) -> tarfile.TarInfo:
    """No owner of the release machine's in the archive, and none of the pax
    headers a member was read with: tarfile hands each member of `git
    archive`'s tar the global header's `comment`, the private commit, which
    writing the member would carry on (review 2.0, bundle #8). Any a member
    needs, a long path, tarfile writes again from the member itself."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = mtime
    info.pax_headers = {}
    return info


#: An SPDX license expression's operators and parentheses, which split it
#: into its licenses. Cargo's old "MIT/Apache-2.0" is an OR. A word only
#: counts as an operator on its own: GPL-3.0-or-later is one license.
_SPDX_TOKEN = re.compile(r"\s*(\(|\)|/|(?<![\w.+-])(?:AND|OR|WITH)(?![\w.+-]))\s*", re.IGNORECASE)

#: A parsed expression: one license (its id, or the free text a license's
#: place holds), or ("and" | "or", the parts).
Licence = str | tuple[str, list["Licence"]]


def licence_expression(licence: str) -> Licence | None:
    """`licence` read clause by clause, or None when it isn't an expression
    (an empty part, a parenthesis left open). WITH binds tightest and
    AND before OR, as SPDX has it. Cargo's "/" binds tighter than AND here:
    "A/B AND C" is read as "(A OR B) AND C", which never makes C optional.
    An exception after WITH is dropped: it grants more, never asks for more.
    Free text with no operator ("GNU General Public License v3") stays one
    license, so `is_copyleft` sees it whole."""
    parts = _SPDX_TOKEN.split(licence)
    tokens = [(p.upper() if p.isalpha() else p) if i % 2 else p.strip() for i, p in enumerate(parts)]
    tokens = [t for i, t in enumerate(tokens) if i % 2 or t]
    at = 0

    def peek() -> str | None:
        return tokens[at] if at < len(tokens) else None

    def take() -> str:
        nonlocal at
        if at >= len(tokens):
            raise ValueError("the expression ends early")
        at += 1
        return tokens[at - 1]

    def operator(token: str | None) -> bool:
        return token in ("(", ")", "/", "AND", "OR", "WITH")

    def joined(kind: str, parts: list[Licence]) -> Licence:
        return parts[0] if len(parts) == 1 else (kind, parts)

    def atom() -> Licence:
        token = take()
        if token == "(":
            inner = either()
            if take() != ")":
                raise ValueError("a parenthesis left open")
            return inner
        if operator(token):
            raise ValueError(f"{token} where a license goes")
        return token

    def with_exception() -> Licence:
        lic = atom()
        if peek() == "WITH":
            take()
            if operator(take()):
                raise ValueError("WITH without an exception")
        return lic

    def chain(kind: str, op: str, part: Callable[[], Licence]) -> Licence:
        parts = [part()]
        while peek() == op:
            take()
            parts.append(part())
        return joined(kind, parts)

    def slashed() -> Licence:
        return chain("or", "/", with_exception)

    def both() -> Licence:
        return chain("and", "AND", slashed)

    def either() -> Licence:
        return chain("or", "OR", both)

    try:
        tree = either()
        if at != len(tokens):
            raise ValueError(f"{tokens[at]} out of place")
    except ValueError:
        return None
    return tree


def licence_offers(tree: Licence, ok: Callable[[str], bool]) -> bool:
    """Whether the expression `tree` can be met with only licenses that are
    `ok`: an OR by any of its parts, an AND by all of them."""
    if isinstance(tree, str):
        return ok(tree)
    kind, parts = tree
    return (any if kind == "or" else all)(licence_offers(p, ok) for p in parts)


def copyleft_only(licence: str) -> bool:
    """A crate's license that offers no permissive choice: an expression
    that can't be met without a copyleft license, read clause by clause, so
    "(MIT OR Apache-2.0) AND MPL-2.0" is copyleft-only and "MPL-2.0 OR MIT"
    isn't. An OR anywhere used to count as a choice, which let a copyleft
    license ANDed beside one through (the delta audit's d14-bundle-3). One
    that doesn't parse, and names a copyleft license, is copyleft-only: its
    source is then archived, or the stage refuses it."""
    if not is_copyleft(licence):
        return False
    tree = licence_expression(licence)
    return tree is None or not licence_offers(tree, lambda lic: not is_copyleft(lic))


def core_crate_sources(lock: Path = IPATOOL_CORE_CRATES) -> list[tuple[str, str, str]]:
    """(crate, version, license) of each crate in ipatool's 1Password SDK core
    whose source must ship with it (`copyleft_only`): MPL-2.0's nom-supreme."""
    return [(c, v, p["license"]) for (c, v), p in sorted(pinned_crates(lock).items())
            if copyleft_only(p.get("license", ""))]


#: What cyan's archive goes into the source archive without: the prebuilt
#: helpers it carries for five platforms, which upstream built with no source
#: for those builds (review 2.0, bundle #1). Graft.app carries none of them:
#: stage_cyan_helpers builds its five from the sources here, and PRUNE
#: removes the rest.
CYAN_LEFT_OUT = "cyan/tools/"


def cyan_source_zip(archive: Path, dest: Path) -> list[str]:
    """cyan's pinned zip, repacked into `dest` without CYAN_LEFT_OUT, with
    its comment (the upstream commit). Every other member keeps its order,
    name and contents, its times (the DOS time and the extra fields, where
    the extended timestamp is) and its attributes (the system it was made on,
    and its external and internal attributes), so unzip gives each file and
    folder the mode it gives the pinned zip's. Only the compression is
    Python's own, so the same zip always gives the same bytes, but not the
    pin's. Returns the files left out.

    GitHub's zip makes plain files and folders on MS-DOS with no Unix mode
    (`external_attr` 0, or 0x10 for a folder), executables on Unix. A fresh
    ZipInfo says Unix, and `writestr` stores an `external_attr` of 0 as
    rw-------, so unzip made the files -rw------- and the folders mode 000
    (the delta audit's d14-bundle-1). The attributes live in the central
    directory, which `ZipFile` writes from the ZipInfo when it closes, so
    the member's own are put back after `writestr`."""
    left = []
    with zipfile.ZipFile(archive) as src, zipfile.ZipFile(dest, "w") as out:
        for info in src.infolist():
            if info.filename.split("/", 1)[-1].startswith(CYAN_LEFT_OUT):
                if not info.is_dir():
                    left.append(info.filename)
                continue
            copy = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            copy.create_system = info.create_system
            copy.extra = info.extra
            copy.internal_attr = info.internal_attr
            copy.compress_type = zipfile.ZIP_STORED if info.is_dir() else zipfile.ZIP_DEFLATED
            out.writestr(copy, src.read(info))
            copy.external_attr = info.external_attr
        out.comment = src.comment
    return left


def archived_filename(pin: Pin) -> str:
    """A pin's archive's name under pinned/<pin>/ in the source archive."""
    return f"{pin.version}-without-tools.zip" if pin.name == "cyan" else pin.filename


def built_sources(pins: dict[str, Pin]) -> list[tuple[Path, str]]:
    """Source the stage produces or fetches beside the pins, as (file, its
    path under pinned/): ipatool's and UnfairPlay's vendored Go modules, from
    their cached builds, and each copyleft crate in ipatool's 1Password SDK
    core (`core_crate_sources`)."""
    pinned = pinned_crates()
    return [(build_ipatool(pins["ipatool"], pins["go"]) / "vendor.tar.gz", "ipatool/vendor.tar.gz"),
            (build_unfairplay(pins["unfairplay"], pins["go"]) / "vendor.tar.gz", "unfairplay/vendor.tar.gz"),
            *((fetch_crate(c, v, pinned[(c, v)]["checksum"]), f"ipatool/core-crates/{c}-{v}.crate")
              for c, v, _ in core_crate_sources())]


def write_sources(components: Path, out: Path, lock: Path = RUNTIME_LOCK, *, commit: str | None = None,
                  repo: Path = REPO) -> list[str]:
    """`out` (Graft-<version>-sources.tar.gz): Graft's source at HEAD, the
    archive of every pin built from source or under a copyleft license, the
    sdist of every copyleft Python package in `components` (components.json),
    and SOURCES.md. Returns the members' names."""
    top = out.name.removesuffix(".tar.gz")
    pins = load_pins(REPO / "bundle" / "pins.toml")
    entries = json.loads(components.read_text())
    # cyan is a Python package installed from its pinned archive: that is its source.
    archived = [pin for name, pin in pins.items()
                if (name in BUILT_FROM_SOURCE or is_copyleft(pin.license) or name == "cyan") and not pin.source
                and name not in LICENCE_TEXT_PINS]
    locked = lock_hashes(lock)
    project = _norm(tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["name"])
    # Review #58: the source of every bundled package, not only copyleft ones.
    sdists, no_sdist = [], []
    for e in entries:
        if e["kind"] != "python-package" or _norm(e["name"]) in (project, "cyan"):
            continue
        try:
            sdists.append(sdist_pin(e["name"], e["version"], e["license"], locked))
        except NoSdist:
            if needs_source(e["license"]):
                raise
            no_sdist.append(e)
    part = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory(prefix="graft-sources-") as tmp:
        files, repacked = [], {}
        for pin in archived:
            path = fetch(pin)
            if pin.name == "cyan":
                repack = Path(tmp) / archived_filename(pin)
                left = cyan_source_zip(path, repack)
                path = repack
                repacked[pin.name] = (_sha256(repack), left)
            files.append((path, f"{top}/pinned/{pin.name}/{archived_filename(pin)}"))
        files += [(path, f"{top}/pinned/{name}") for path, name in built_sources(pins)]
        files += [(fetch(pin), f"{top}/sdists/{pin.filename}") for pin in sdists]
        source = Path(tmp) / "graft.tar"
        tree, when = graft_source_tar(source, f"{top}/graft/", repo=repo, commit=commit)
        md = sources_md(top, tree, pins, archived, sdists, no_sdist, repacked).encode()
        names = []
        with part.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=when) as gz, \
                tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tf:
            with tarfile.open(source) as git_tar:
                for m in git_tar.getmembers():
                    tf.addfile(_anonymous(m, m.mtime), git_tar.extractfile(m) if m.isfile() else None)
                    names.append(m.name)
            for path, name in files:
                tf.add(path, arcname=name, filter=lambda i: _anonymous(i, when))
                names.append(name)
            info = _anonymous(tarfile.TarInfo(f"{top}/SOURCES.md"), when)
            info.size, info.mode = len(md), 0o644
            tf.addfile(info, io.BytesIO(md))
            names.append(info.name)
    part.replace(out)
    return names


# ------------------------------------------------- downloads, and a phone's hotspot

#: The gateway an iPhone's Personal Hotspot gives a Mac on it, over USB,
#: Wi-Fi or Bluetooth alike.
HOTSPOT_GATEWAY = "172.20.10.1"
#: Set to 1, a release downloads over a hotspot without asking.
ALLOW_HOTSPOT_ENV = "GRAFT_ALLOW_HOTSPOT"

#: Roughly how many MB each pinned file is, for the hotspot warning only:
#: pins.toml records no sizes, so these are the files pinned on 2026-11-23,
#: rounded. A pin not named here shows as a size not known. Nothing checks
#: anything against them.
APPROX_MB = {"go": 65, "openssl": 53, "python": 25, "cyan": 9, "ipatool": 6, "cctools": 3, "unfairplay": 1,
             "libplist": 0.5, "python-build-standalone": 0.5, "zsign": 0.2, "ldid": 0.1, "insert_dylib": 0.1,
             "hacl": 0.1}
#: Likewise for the rest: each Go tool's modules (the zips `go mod vendor`
#: fetches, a guess), and an average source distribution and crate (98
#: source distributions came to 96 MB, 369 crates to 54 MB).
GO_MODULES_MB = {"ipatool": 100, "unfairplay": 1}
SDIST_MB, CRATE_MB = 1.0, 0.15
#: An average locked wheel, for one pip's cache doesn't hold: the two locks'
#: 105 wheels came to about 100 MB for 2.0.1, a few large (cryptography,
#: pydantic-core) and most small.
WHEEL_MB = 1.0


@dataclass(frozen=True)
class Download:
    what: str
    #: Approximate size; None when not known.
    mb: float | None
    #: Whether the download cache keeps it: False for what every build fetches again.
    kept: bool = True


def _cached_sdists(cache: Path) -> set[tuple[str, str]]:
    """(normalized name, version) of each source distribution in `cache`, by its file name."""
    found = set()
    for f in cache.iterdir() if cache.is_dir() else ():
        if m := re.fullmatch(r"(.+)-([^-]+)\.(?:tar\.gz|zip|tar\.bz2)", f.name):
            found.add((_norm(m[1]), m[2]))
    return found


def _go_build_done(tool: str, pins: dict[str, Pin], work: Path) -> bool:
    key = {"ipatool": ipatool_key, "unfairplay": unfairplay_key}[tool]
    try:
        return (work / f"{tool}-{key(pins[tool], pins['go'])[:12]}").is_dir()
    except BundleError:
        return False


def uncached_wheels(locks: tuple[Path, ...], cache: Path | None = None) -> list[str]:
    """`name version` of each package the `locks` pin with no wheel in pip's
    cache: none the record (`cached_wheels`) lists, with its body still
    there, has a SHA-256 the lock allows for it."""
    have = set(cached_wheels(pip_cache(cache)))
    wanted: dict[tuple[str, str], set[str]] = {}
    for lock in locks:
        for name, (version, hashes) in lock_hashes(lock).items():
            wanted.setdefault((name, version), set()).update(hashes)
    return [f"{n} {v}" for (n, v), hashes in sorted(wanted.items()) if not hashes & have]


def download_plan(pins: dict[str, Pin], cache: Path | None = None, work: Path | None = None,
                  lock: Path = RUNTIME_LOCK, crates: Path = IPATOOL_CORE_CRATES,
                  build_lock: Path = BUILD_LOCK) -> list[Download]:
    """What a release would still download, worked out offline from what
    `cache` (CACHE) and `work` (WORK) hold: each pinned file not cached (or
    cached with another hash); each Go tool's modules, unless its build is
    cached or an earlier build of the same source recorded them all in the
    module cache; the source archive's source distributions (one per package
    in the runtime lock, matched by name and version) and the crates
    ipatool's 1Password SDK core pins; and the wheels of the build and
    runtime locks that pip's cache doesn't hold (`uncached_wheels`). Not
    counted: small lookups (PyPI's JSON and index pages, the Swift packages
    Xcode resolves, UnfairPlay's tag) and the crates named only in a wheel's
    SBOM, known after staging."""
    cache, work = cache or CACHE, work or WORK
    plan = []
    for name, pin in pins.items():
        f = cache / pin.filename
        if not (f.is_file() and _sha256(f) == pin.sha256):
            plan.append(Download(f"{name} {pin.version}", APPROX_MB.get(name)))
    for tool in ("ipatool", "unfairplay"):
        if tool in pins and "go" in pins and not _go_build_done(tool, pins, work) \
                and not go_modules_cached(go_modcache(cache), pins[tool]):
            plan.append(Download(f"{tool}'s Go modules", GO_MODULES_MB.get(tool)))
    have = _cached_sdists(cache)
    sdists = sorted(f"{n} {v}" for n, (v, _) in lock_hashes(lock).items() if (n, v) not in have)
    if sdists:
        plan.append(Download(f"{len(sdists)} source distribution(s) for the source archive: "
                             + ", ".join(sdists[:5]) + (", ..." if len(sdists) > 5 else ""), len(sdists) * SDIST_MB))
    missing = sorted(f"{c} {v}" for c, v in pinned_crates(crates) if not (cache / "crates" / f"{c}-{v}.crate").is_file())
    if missing:
        plan.append(Download(f"{len(missing)} Rust crate(s) for their licenses", len(missing) * CRATE_MB))
    wheels = uncached_wheels((build_lock, lock), cache)
    if wheels:
        plan.append(Download(f"{len(wheels)} Python wheel(s) the app bundles: " + ", ".join(wheels[:5])
                             + (", ..." if len(wheels) > 5 else ""), len(wheels) * WHEEL_MB))
    return plan


def default_route() -> tuple[str, str] | None:
    """(gateway, interface) of the default route, as `route -n get default`
    says; None when there is no default route or route couldn't say."""
    try:
        r = subprocess.run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, timeout=10,
                           env=_build_env())
    except (OSError, subprocess.SubprocessError):
        return None
    fields = dict(line.strip().split(": ", 1) for line in r.stdout.splitlines() if ": " in line)
    if r.returncode != 0 or not (fields.get("gateway") or fields.get("interface")):
        return None
    return fields.get("gateway", ""), fields.get("interface", "")


def _mb(mb: float | None) -> str:
    return "     ? MB" if mb is None else ("    <1 MB" if mb < 1 else f"{mb:6.0f} MB")


def check_hotspot(pins: dict[str, Pin], *, interactive: bool | None = None,
                  answer: Callable[[], str] | None = None) -> bool:
    """Whether a release may go on downloading. On a phone's hotspot (the
    default route through HOTSPOT_GATEWAY), with anything a cache would keep
    still to download, it says what and roughly how much, then asks; with
    GRAFT_ALLOW_HOTSPOT=1 it goes on, and with no terminal to ask on it
    stops. On any other network, or with everything cached but what every
    build fetches anyway (`Download.kept`), it goes on, saying so on a
    hotspot. The wheels are cached too now (`pip_cache`), so they count."""
    route = default_route()
    if route is None or route[0] != HOTSPOT_GATEWAY:
        return True
    on = f"the default route goes through {HOTSPOT_GATEWAY} on {route[1] or 'an unknown interface'}"
    plan = download_plan(pins)
    big = [d for d in plan if d.kept]
    if not big:
        every = "; ".join(d.what for d in plan)
        print(f"note: this Mac is on a phone's hotspot ({on}); everything is cached"
              + (f" but {every}" if every else ""), file=sys.stderr)
        return True
    known = sum(d.mb for d in big if d.mb is not None)
    unknown = sum(d.mb is None for d in big)
    lines = [f"warning: this Mac is on a phone's hotspot ({on}), and this release still has to download:",
             *(f"  {_mb(d.mb)}  {d.what}" for d in plan),
             f"  about {known:.0f} MB in all" + (f", plus {unknown} of a size not known" if unknown else "") + ".",
             f"  What it downloads stays in {CACHE} for the next release."]
    print("\n".join(lines), file=sys.stderr)
    if os.environ.get(ALLOW_HOTSPOT_ENV) == "1":
        print(f"  {ALLOW_HOTSPOT_ENV}=1: downloading over the hotspot.", file=sys.stderr)
        return True
    if interactive is None:
        interactive = sys.stdin.isatty()
    if not interactive:
        print(f"error: not downloading that over a hotspot without asking, and there's no terminal to ask on. "
              f"Join another network, or set {ALLOW_HOTSPOT_ENV}=1 to go ahead.", file=sys.stderr)
        return False
    print("Download it over the hotspot? [y/N] ", end="", file=sys.stderr, flush=True)
    reply = (answer or sys.stdin.readline)()
    if reply.strip().lower() in ("y", "yes"):
        return True
    print(f"Stopped before downloading. Join another network, or set {ALLOW_HOTSPOT_ENV}=1.", file=sys.stderr)
    return False


#: The interpreter the locks are resolved for and the stage runs under: the
#: bundled CPython's version, on arm64. pip-compile evaluates markers for the
#: Python it runs on, so a lock made on 3.14 or under Rosetta could drop a
#: dependency the bundled 3.13 needs (critic: lock-bundle's interpreter).
BUNDLED_PYTHON = (3, 13)


def check_interpreter(version: tuple[int, ...] | None = None, machine: str | None = None,
                      implementation: str | None = None) -> None:
    import platform
    version = tuple(version or sys.version_info[:2])
    machine = machine or platform.machine()
    implementation = implementation or sys.implementation.name
    if (version[:2], machine, implementation) != (BUNDLED_PYTHON, "arm64", "cpython"):
        raise BundleError(f"this is {implementation} {'.'.join(map(str, version[:2]))} on {machine}; the bundle's "
                          f"locks and stage need CPython {'.'.join(map(str, BUNDLED_PYTHON))} on arm64, as Graft.app "
                          "runs: rebuild .venv with that Python (not under Rosetta)")


def main(argv: list[str]) -> int:
    usage = ("usage: graft_bundle.py stage <staging-dir> | sources <components.json> "
             "<Graft-<version>-sources.tar.gz> [--commit SHA --repo CHECKOUT] | check-python | hotspot-check")
    try:
        if argv == ["check-python"]:
            check_interpreter()
        elif argv == ["hotspot-check"]:
            return 0 if check_hotspot(load_pins(REPO / "bundle" / "pins.toml")) else 1
        elif len(argv) == 2 and argv[0] == "stage":
            dest = Path(argv[1]).absolute()
            found = stage(dest)
            print(f"staged {dest}: {len(found)} Mach-O files to sign, listed in macho-list.txt")
        elif len(argv) in (3, 7) and argv[0] == "sources" and argv[2].endswith(".tar.gz") and (
                len(argv) == 3 or (argv[3], argv[5]) == ("--commit", "--repo")):
            out = Path(argv[2]).absolute()
            commit, repo = (argv[4], Path(argv[6])) if len(argv) == 7 else (None, REPO)
            names = write_sources(Path(argv[1]), out, commit=commit, repo=repo)
            print(f"wrote {out}: {len(names)} entries")
        else:
            print(usage, file=sys.stderr)
            return 2
    except BundleError as e:
        print(f"graft_bundle: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
