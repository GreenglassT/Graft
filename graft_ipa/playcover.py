"""Make a tweaked IPA loadable on macOS under PlayCover.

**Only for Mac builds.** iOS genuinely has the private frameworks this
retargets, so doing it to a phone-bound artifact would swap the real framework
for a stub and break the tweak that wanted it. Nothing here runs unless asked.

The problem this solves, measured 2026-08-18: a cyan-injected IPA dies at
launch with

    DYLD: Library missing
    /System/Library/PrivateFrameworks/Preferences.framework/Preferences
    referenced from .../Frameworks/libexample.dylib

macOS has that framework in neither `/System/Library/PrivateFrameworks` nor
`/System/iOSSupport/...`. Substrate is *not* the issue -- cyan already ships
ElleKit as CydiaSubstrate and injects it, so a cyan build carries its own.

The fix is the one cyan already uses for ElleKit: bundle the framework and
point the dylib at `@rpath`. See `graft_ipa/stubs/preferences-stub/` for why a stub
suffices -- the settings libraries it was written for import three symbols
from Preferences, one class and one ivar.
"""

from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

from . import deb, macho, tools
from .child_env import child_env

#: What to do on a Mac without what a stand-in framework needs. The Command
#: Line Tools are not enough: they have no iOS SDK.
XCODE_HINT = (
    "install Xcode from the App Store and open it once, then run "
    "`sudo xcode-select -s /Applications/Xcode.app/Contents/Developer`; "
    "the Command Line Tools alone have no iOS SDK"
)

#: iOS private frameworks a tweak may link that macOS does not provide, and for
#: which `tools/` has a stub. Keyed by framework name.
#:
#: Only `Preferences` so far, and that is not an accident: of the tweak dylibs
#: surveyed, settings libraries were the only ones linking a *private*
#: framework at all. Everything else links public frameworks, which the
#: iOS-on-Mac runtime provides.
STUBBABLE_PRIVATE_FRAMEWORKS = ("Preferences",)


def _private_framework_path(name: str) -> str:
    return f"/System/Library/PrivateFrameworks/{name}.framework/{name}"


def linked_private_frameworks(binary: Path) -> list[str]:
    """Which stubbable iOS private frameworks `binary` links, if any.

    Reads the load commands directly (`macho.dylib_names`), which are what
    dyld will act on, with no `otool` and no Xcode Command Line Tools.

    `dylib_names` returns dependencies only, never the file's own
    `LC_ID_DYLIB`, which is why there is no line-two skip here any more. That
    id is `/Library/MobileSubstrate/DynamicLibraries/<name>.dylib` for every
    tweak, which contains the substring "Substrate". An earlier survey grepped
    the whole of `otool -L` and reported nearly every tweak as Substrate-linked
    because of it.
    """
    try:
        deps = macho.dylib_names(binary)
    except macho.MachOError as exc:
        # Never `return []` here: an empty list means "nothing to stub", and the
        # build would report every injected dylib fine without having read one.
        # The user would ship an IPA that still dies at launch, having been told
        # the opposite. This function is the only gate on the whole feature.
        raise PlayCoverPrepError(
            f"couldn't read {binary.name}'s linked libraries",
            hint=str(exc)[:200],
        ) from exc
    return [n for n in STUBBABLE_PRIVATE_FRAMEWORKS if _private_framework_path(n) in deps]


#: The roots this Mac keeps the frameworks an iPhone or iPad app can load
#: under: its own, and the iOS support PlayCover's apps run on. Each is read
#: as `<root>/System/Library/{Frameworks,PrivateFrameworks}`. Tests point both
#: at scratch folders.
FRAMEWORK_ROOTS: tuple[Path, ...] = (Path("/"), Path("/System/iOSSupport"))

_FRAMEWORK_KINDS = ("Frameworks", "PrivateFrameworks")

#: A dependency on a system framework, by the framework's name.
_SYSTEM_FRAMEWORK = re.compile(r"^/System/Library/(?:Frameworks|PrivateFrameworks)/([^/]+)\.framework/")


def framework_present(name: str) -> bool:
    """Whether this Mac has `name`.framework in either root.

    **The directory, never its binary.** Since macOS 11 a system framework's
    binary lives only in the dyld shared cache, so
    `Foundation.framework/Foundation` isn't on disk on any Mac. Known limit:
    presence doesn't promise the symbols a tweak uses.
    """
    return any(
        (root / "System" / "Library" / kind / f"{name}.framework").is_dir()
        for root in FRAMEWORK_ROOTS for kind in _FRAMEWORK_KINDS
    )


def unsupported_frameworks(binary: Path) -> list[str]:
    """The system frameworks `binary` can't load without that macOS lacks
    and Graft doesn't stub, by name, in load order.

    Weak links are left out (dyld skips a missing weak library), and so is
    `/usr/lib`: on macOS those libraries are in the shared cache, not on
    disk, so their absence can't be told. Raises `PlayCoverPrepError` for a
    binary whose load commands can't be read.
    """
    try:
        deps = macho.required_dylib_names(binary)
    except macho.MachOError as exc:
        raise PlayCoverPrepError(
            f"couldn't read {binary.name}'s linked libraries",
            hint=str(exc)[:200],
        ) from exc
    found: list[str] = []
    for dep in deps:
        match = _SYSTEM_FRAMEWORK.match(dep)
        if match is None:
            continue
        name = match.group(1)
        if name in STUBBABLE_PRIVATE_FRAMEWORKS or name in found or framework_present(name):
            continue
        found.append(name)
    return found


def tweak_origins(tweaks: Iterable[Path]) -> dict[str, str]:
    """Which names in an app's `Frameworks/` the attached tweaks brought,
    each to the tweak's filename: every `.dylib` and every `.framework` in a
    .deb's data (cyan puts both in `Frameworks/`), and an attached bare
    `.dylib` or `.framework` itself.

    A .deb it can't read (`deb.data_names` None) claims nothing, so its
    binaries are taken for the app's own: they warn instead of refusing.
    """
    origins: dict[str, str] = {}
    for path in tweaks:
        if path.suffix == ".deb":
            for name in deb.data_names(path) or []:
                parts = name.split("/")
                bundle = next((p for p in parts[:-1] if p.endswith(".framework")), None)
                if bundle is not None:
                    origins.setdefault(bundle, path.name)
                elif parts[-1].endswith(".dylib"):
                    origins.setdefault(parts[-1], path.name)
        elif path.suffix in (".dylib", ".framework"):
            origins.setdefault(path.name, path.name)
    return origins


@dataclass(frozen=True)
class UnsupportedLink:
    """One binary in `Frameworks/` needing one framework this Mac lacks."""

    #: Its name in `Frameworks/`: `Example.dylib` or `ExampleKit.framework`.
    binary: str
    framework: str
    #: The tweak that brought it (its filename), or None for the app's own
    #: binary, or one whose origin can't be told.
    tweak: str | None

    @property
    def refuses(self) -> bool:
        """Only a tweak's binary refuses: the app's own legitimately link
        things, and refusing them would block apps PlayCover may run."""
        return self.tweak is not None


def _framework_binary(bundle: Path) -> Path | None:
    """A `.framework`'s executable: its Info.plist's `CFBundleExecutable`,
    else the bundle's own name; None when there's no such file."""
    name = bundle.stem
    try:
        with (bundle / "Info.plist").open("rb") as fh:
            exe = plistlib.load(fh).get("CFBundleExecutable")
        if isinstance(exe, str) and exe and "/" not in exe:
            name = exe
    except (OSError, ValueError, AttributeError, ExpatError):
        # ExpatError, which is no ValueError: XML cut off part way.
        pass
    binary = bundle / name
    return binary if binary.is_file() else None


def _frameworks_binaries(frameworks: Path) -> list[tuple[str, Path]]:
    """Every `*.dylib`, then every `*.framework`'s executable, in
    `frameworks`, by its name there."""
    if not frameworks.is_dir():
        return []
    found = [(p.name, p) for p in sorted(frameworks.glob("*.dylib")) if p.is_file()]
    for bundle in sorted(frameworks.glob("*.framework")):
        if bundle.is_dir() and (binary := _framework_binary(bundle)) is not None:
            found.append((bundle.name, binary))
    return found


def scan_frameworks(frameworks: Path, origins: dict[str, str] | None) -> list[UnsupportedLink]:
    """What in `frameworks` needs an iOS framework this Mac lacks.

    `origins` is `tweak_origins`'s answer, or None when which files the
    tweaks added can't be told (an archive with no build record): nothing
    refuses then. A framework whose executable isn't a readable Mach-O is
    passed over; a `.dylib`'s raises, as `linked_private_frameworks` does.
    """
    found: list[UnsupportedLink] = []
    for name, binary in _frameworks_binaries(frameworks):
        try:
            missing = unsupported_frameworks(binary)
        except PlayCoverPrepError:
            if name.endswith(".framework"):
                continue
            raise
        tweak = origins.get(name) if origins is not None else None
        found += [UnsupportedLink(name, framework, tweak) for framework in missing]
    return found


def check_ipa(ipa: Path, origins: dict[str, str] | None) -> list[UnsupportedLink]:
    """`scan_frameworks` on an archive's `Payload/<App>.app/Frameworks`,
    reading only its dylibs and its frameworks' Info.plists and binaries."""
    with zipfile.ZipFile(ipa) as zf, tempfile.TemporaryDirectory() as td:
        names = zf.namelist()
        app = next((n.split("/")[1] for n in names
                    if n.startswith("Payload/") and n.count("/") >= 2 and n.split("/")[1].endswith(".app")),
                   None)
        if app is None:
            return []
        prefix = f"Payload/{app}/Frameworks/"
        wanted = []
        for name in names:
            if not name.startswith(prefix):
                continue
            rest = name[len(prefix):].split("/")
            if len(rest) == 1 and rest[0].endswith(".dylib"):
                wanted.append(name)
            elif len(rest) == 2 and rest[0].endswith(".framework") and rest[1]:
                wanted.append(name)  # a framework's top level: its Info.plist and binary
        if not wanted:
            return []
        zf.extractall(td, members=wanted)
        return scan_frameworks(Path(td) / "Payload" / app / "Frameworks", origins)


def refusal_message(links: list[UnsupportedLink]) -> str:
    """The refusal: one sentence per tweak, naming what it needs."""
    by_tweak: dict[str, list[str]] = {}
    for link in links:
        if link.tweak is not None and link.framework not in by_tweak.setdefault(link.tweak, []):
            by_tweak[link.tweak].append(link.framework)
    return " ".join(
        f"{tweak} needs iOS's {' and '.join(frameworks)}, which macOS doesn't have, so it "
        "can't run under PlayCover. Phone builds can still use it; to build for this Mac, "
        "remove it from the app first."
        for tweak, frameworks in by_tweak.items()
    )


def warning_message(link: UnsupportedLink) -> str:
    """The warning for an app's own binary, or one whose origin can't be told."""
    return (
        f"{link.binary} in the app needs iOS's {link.framework}, which macOS doesn't "
        "have, so the app may not run under PlayCover."
    )


def developer_dir() -> Path | None:
    """The active Xcode (or Command Line Tools) directory, or None.

    Asked of `DEVELOPER_DIR`, then of `/usr/bin/xcode-select -p`, which only
    reports. Never of an xcrun shim: on a Mac without the tools,
    `/usr/bin/xcrun`, `/usr/bin/install_name_tool` and `/usr/bin/clang` pop
    macOS's "install the command line developer tools" dialog, from a
    GUI-spawned CLI too, and fail; the FileNotFoundError hint in `_run_tool`
    could never show, because the shim always exists.
    """
    env = os.environ.get("DEVELOPER_DIR")
    if env:
        path = Path(env)
        # xcrun takes Xcode.app itself as well as its Contents/Developer.
        if (path / "Contents" / "Developer").is_dir():
            return path / "Contents" / "Developer"
        return path if path.is_dir() else None
    try:
        out = subprocess.run(
            ["/usr/bin/xcode-select", "-p"],
            capture_output=True, text=True, timeout=10, env=child_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    path = Path(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    return path if path is not None and path.is_dir() else None


def has_ios_sdk(dev: Path | None) -> bool:
    """Whether `dev` (`developer_dir`) has an iOS SDK: full Xcode does, the
    Command Line Tools do not."""
    if dev is None:
        return False
    return any((dev / "Platforms" / "iPhoneOS.platform" / "Developer" / "SDKs").glob("iPhoneOS*.sdk"))


def bundled_install_name_tool(machine: str | None = None) -> Path:
    """Graft.app's own install_name_tool, relative to Contents: cyan's, built
    from cctools-port with its fake-sign hook compiled out
    (bundle/patches/cctools-port-no-fake-sign.patch), in the folder cyan runs
    its helpers from, tools/Darwin/<platform.machine()>: arm64 in Graft.zip,
    x86_64 in Graft-Intel.zip (its Python says x86_64 even under Rosetta)."""
    return (Path("Resources/GraftPython/lib/python3.13/site-packages/cyan/tools/Darwin")
            / (machine or platform.machine()) / "install_name_tool")


#: The arm64 bundle's (bundled_install_name_tool).
BUNDLED_INSTALL_NAME_TOOL = bundled_install_name_tool("arm64")


def install_name_tool() -> Path:
    """The `install_name_tool` a retarget runs, by absolute path.

    Graft.app's own copy (bundled_install_name_tool) when this is the app's
    CLI, else the active developer directory's. Not PATH: there it is the
    xcrun shim `developer_dir` explains. Raises `PlayCoverPrepError` naming
    the fix when neither is there.
    """
    bundled = tools.bundled_tools_dir()
    if bundled is not None and tools.executable(bundled.parent.parent / bundled_install_name_tool()):
        return bundled.parent.parent / bundled_install_name_tool()
    dev = developer_dir()
    if dev is not None:
        for candidate in (
            dev / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin" / "install_name_tool",
            dev / "usr" / "bin" / "install_name_tool",
        ):
            if tools.executable(candidate):
                return candidate
    raise PlayCoverPrepError(
        "install_name_tool isn't installed, and a tweak in this build needs it "
        "to run under PlayCover",
        hint=XCODE_HINT,
    )


def _run_tool(argv: list[str], *, doing: str) -> None:
    """Run a Mach-O tool, translating both ways it can fail.

    `check=True` alone raises `CalledProcessError`/`FileNotFoundError`, neither
    of which `_build_one` catches — so a missing `install_name_tool` killed a
    build *after* the download, decrypt and cyan injection with a bare
    traceback, which is exactly what `build_stub`'s docstring says this module
    exists to avoid.
    """
    try:
        subprocess.run(argv, capture_output=True, check=True, env=child_env())
    except FileNotFoundError as exc:
        raise PlayCoverPrepError(
            f"{argv[0]} is not installed, so {doing} is not possible",
            hint="install Xcode's command line tools: xcode-select --install",
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise PlayCoverPrepError(
            f"{doing} failed",
            hint=(exc.stderr or b"").decode(errors="replace").strip()[:200] or None,
        ) from exc
    except OSError as exc:
        raise PlayCoverPrepError(f"{doing} failed: {exc}") from exc


def retarget_to_bundled_framework(binary: Path, framework: str, *, rpath: str = "@loader_path") -> None:
    """Point `binary` at `@rpath/<framework>.framework/...` instead of /System.

    Re-signs afterward, and the honest reason is narrower than it looks.
    **On this toolchain `install_name_tool` already re-signs ad-hoc by itself**
    -- measured: patch a signed dylib, skip the `codesign` below, and
    `codesign -v` still passes. Older `install_name_tool` did invalidate, and a
    dylib with a broken signature inside a bundle is its own launch failure, so
    the call stays as cheap insurance against a toolchain that behaves the old
    way. It is not load bearing here, and a mutation that deletes it does not
    fail any test -- which is the truth rather than a gap to paper over.

    Ad-hoc is the right identity regardless: PlayCover re-signs the whole
    bundle on install.
    """
    tool = str(install_name_tool())
    _run_tool(
        [tool, "-change", _private_framework_path(framework),
         f"@rpath/{framework}.framework/{framework}", str(binary)],
        doing=f"pointing {binary.name} at the bundled {framework}.framework",
    )
    # `@rpath` is meaningless without an LC_RPATH that reaches the bundle, and
    # a tweak dylib injected by cyan carries none of its own — dyld would fail
    # with `Library not loaded: @rpath/...`, the same class of error, now with a
    # stub sitting in the bundle claiming it was fixed. `@loader_path` rather
    # than `@executable_path` because these dylibs sit *in* Frameworks/ beside
    # the framework, and that stays true however the app is launched.
    #
    # A framework's binary sits a level deeper, in `Frameworks/X.framework/`,
    # so its rpath is `@loader_path/..` (`prepare_frameworks_dir`).
    #
    # Adding a duplicate rpath is a warning, not an error, and this runs again
    # on an already-prepared IPA, so an existing one is checked for first.
    if rpath not in _rpaths(binary):
        _run_tool(
            [tool, "-add_rpath", rpath, str(binary)],
            doing=f"giving {binary.name} an rpath that reaches the bundle",
        )
    _run_tool(["codesign", "-f", "-s", "-", str(binary)],
              doing=f"re-signing {binary.name}")


def _rpaths(binary: Path) -> list[str]:
    """The LC_RPATH entries already on `binary`."""
    try:
        return macho.rpaths(binary)
    except macho.MachOError:
        return []


#: Where the stub builder lives.
#:
#: **Inside the package, not a sibling `tools/` directory.** The wheel ships
#: `packages = ["graft_ipa"]` and nothing else, so a `pipx install` put no
#: `tools/` in site-packages at all: `--for-playcover` failed for every
#: non-editable install with "the stub builder is missing at …" plus a hint
#: pointing at a repo the user does not have. It worked here only because the
#: local CLI happens to be an editable install pointing back at the checkout.
STUB_TOOLS_DIR = Path(__file__).resolve().parent / "stubs" / "preferences-stub"


class PlayCoverPrepError(Exception):
    """The IPA could not be made loadable on this Mac."""

    def __init__(self, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


def build_stub(framework: str, dest_dir: Path) -> Path:
    """Build `<framework>.framework` into `dest_dir` and return its path.

    Built on demand rather than committed as a binary: it is 68 KB of Mach-O
    whose only job is to satisfy dyld, and a checked-in binary is a thing
    nobody can review. The source is nineteen lines.

    Needs the iOS SDK. A machine without Xcode gets a sentence saying so
    instead of a clang error, because "no iOS SDK" is a setup problem and
    `clang: error: invalid arch` is not a description of one.
    """
    if framework not in STUBBABLE_PRIVATE_FRAMEWORKS:
        raise PlayCoverPrepError(f"no stub is available for {framework}.framework")
    out = dest_dir / f"{framework}.framework"
    # Graft.app carries the stub, built at release (scripts/graft_bundle.py),
    # so a Mac without Xcode can make a PlayCover build too.
    prebuilt = STUB_TOOLS_DIR / f"{framework}.framework"
    if (prebuilt / framework).is_file():
        shutil.rmtree(out, ignore_errors=True)
        shutil.copytree(prebuilt, out)
        return out
    script = STUB_TOOLS_DIR / "build.sh"
    if not script.is_file():
        raise PlayCoverPrepError(
            f"the stub builder is missing at {script}",
            hint="it ships in this repo under graft_ipa/stubs/preferences-stub/",
        )
    if not has_ios_sdk(developer_dir()):
        # Checked rather than tried: the script's `xcrun` is a shim on a Mac
        # without the tools, and it pops the install dialog before failing.
        raise PlayCoverPrepError(
            f"a tweak in this build links {framework}.framework, which macOS lacks, "
            "and building the stand-in Graft bundles for it needs Xcode with its iOS SDK",
            hint=XCODE_HINT,
        )
    try:
        # `sh script`, not `./script`: whether the +x bit survives a wheel
        # build is not something this should depend on, and an installed copy
        # that is 0644 would fail with a bare PermissionError.
        subprocess.run(["/bin/sh", str(script), str(out)],
                       capture_output=True, check=True, env=child_env())
    except subprocess.CalledProcessError as exc:
        raise PlayCoverPrepError(
            f"couldn't build the {framework} stub",
            hint=(exc.stderr or b"").decode(errors="replace").strip()[:200]
            or "is Xcode with the iOS SDK installed? `xcrun --sdk iphoneos --show-sdk-path`",
        ) from exc
    except OSError as exc:
        raise PlayCoverPrepError(f"couldn't run {script}: {exc}") from exc
    return out


def prepare_frameworks_dir(frameworks: Path) -> list[str]:
    """Make every dylib and framework in `frameworks` loadable on macOS.
    Returns what it fixed.

    A framework is its executable (`CFBundleExecutable`): tweaks ship
    frameworks too, and a settings library's links Preferences. One whose
    executable isn't a readable Mach-O is passed over, as in `scan_frameworks`.

    Operates on an already-extracted `Payload/<App>.app/Frameworks`, not on a
    zip, so the caller owns unpacking and repacking. That keeps this testable
    against a directory and keeps the archive handling — which differs between
    the build pipeline and a one-off — out of it.

    Idempotent: a dylib already pointing at `@rpath` reports no private
    framework, so a second run finds nothing and does nothing.
    """
    if not frameworks.is_dir():
        return []
    fixed: list[str] = []
    for name, binary in _frameworks_binaries(frameworks):
        in_bundle = name.endswith(".framework")
        try:
            needed = linked_private_frameworks(binary)
        except PlayCoverPrepError:
            if in_bundle:
                continue
            raise
        for framework in needed:
            bundled = frameworks / f"{framework}.framework"
            if not bundled.exists():
                build_stub(framework, frameworks)
            retarget_to_bundled_framework(
                binary, framework, rpath="@loader_path/.." if in_bundle else "@loader_path",
            )
            fixed.append(f"{name} → bundled {framework}.framework")
    return fixed


def prepare_ipa(ipa: Path) -> list[str]:
    """Make `ipa` loadable on macOS, in place. Returns what it fixed.

    Touches the archive only when there is something to change: the common case
    is an app whose tweaks all link public frameworks, and rewriting a 120 MB
    IPA to change nothing is pure cost. When there *is* something, the archive
    is rebuilt rather than appended to.

    **Rebuilt, not appended.** Appending a second entry under an existing name
    is smaller and mostly works — `unzip` and `zipfile` both surface the later
    one — but it leaves two members with the same path and relies on every
    reader resolving the collision the same way. PlayCover's unpacker is not
    a reader whose tie-breaking is documented here, and an IPA carrying a stale
    copy of the very dylib being fixed is the kind of thing that fails once, on
    someone else's machine.
    """
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        with zipfile.ZipFile(ipa) as zf:
            infos = zf.infolist()
            members = [i.filename for i in infos if "/Frameworks/" in i.filename]
            if not members:
                return []
            zf.extractall(work, members=members)

        apps = list(work.glob("Payload/*.app/Frameworks"))
        if not apps:
            return []
        frameworks = apps[0]
        fixed = prepare_frameworks_dir(frameworks)
        if not fixed:
            return []

        rel = frameworks.relative_to(work)
        replacements = {
            str(rel / path.relative_to(frameworks)): path
            for path in sorted(frameworks.rglob("*")) if path.is_file()
        }
        rebuilt = ipa.with_suffix(".ipa.playcover-tmp")
        with zipfile.ZipFile(ipa) as src, \
                zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                # Preserve each original entry's own compression and metadata;
                # only the members under Frameworks/ are replaced, and any new
                # ones (the stub) are appended after.
                if info.filename in replacements:
                    # Carry the *original* entry's attributes, not the extracted
                    # file's. `ZipFile.extract` does not restore external_attr,
                    # so the copy on disk is 0644 after umask, and `dst.write`
                    # would re-derive the mode from it — silently stripping +x
                    # from every framework binary in the app. Measured on a
                    # synthetic IPA: libexample.dylib went 0o100755 →
                    # 0o100644 while entries outside Frameworks/, which are
                    # passed through by `writestr`, kept theirs.
                    replaced = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                    replaced.external_attr = info.external_attr
                    replaced.internal_attr = info.internal_attr
                    replaced.create_system = info.create_system
                    replaced.compress_type = info.compress_type
                    dst.writestr(replaced, replacements.pop(info.filename).read_bytes())
                elif not info.is_dir():
                    dst.writestr(info, src.read(info.filename))
            for name, path in sorted(replacements.items()):
                dst.write(path, name)
        rebuilt.replace(ipa)
    return fixed
