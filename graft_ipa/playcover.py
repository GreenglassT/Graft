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
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import macho, tools
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


def retarget_to_bundled_framework(binary: Path, framework: str) -> None:
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
    # Adding a duplicate rpath is a warning, not an error, and this runs again
    # on an already-prepared IPA, so an existing one is checked for first.
    if "@loader_path" not in _rpaths(binary):
        _run_tool(
            [tool, "-add_rpath", "@loader_path", str(binary)],
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
    """Make every dylib in `frameworks` loadable on macOS. Returns what it fixed.

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
    for binary in sorted(frameworks.glob("*.dylib")):
        for framework in linked_private_frameworks(binary):
            bundled = frameworks / f"{framework}.framework"
            if not bundled.exists():
                build_stub(framework, frameworks)
            retarget_to_bundled_framework(binary, framework)
            fixed.append(f"{binary.name} → bundled {framework}.framework")
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
