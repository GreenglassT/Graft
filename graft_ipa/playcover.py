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

import subprocess
import tempfile
import zipfile
from pathlib import Path

from .child_env import child_env

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


def linked_private_frameworks(macho: Path) -> list[str]:
    """Which stubbable iOS private frameworks `macho` links, if any.

    Reads `otool -L` rather than parsing Mach-O by hand: the load commands are
    what dyld will act on, and `otool` is already a hard dependency of the
    verify step.

    Skips the first two lines deliberately. Line one is the file's own path and
    line two is its `LC_ID_DYLIB` -- and that id is
    `/Library/MobileSubstrate/DynamicLibraries/<name>.dylib` for every tweak,
    which contains the substring "Substrate". An earlier survey grepped the
    whole output and reported nearly every tweak as Substrate-linked because of
    it.
    """
    try:
        out = subprocess.run(
            ["otool", "-L", str(macho)], capture_output=True, text=True, check=True,
            env=child_env(),
        ).stdout
    except FileNotFoundError as exc:
        raise PlayCoverPrepError(
            "otool is not installed, so this cannot tell which frameworks "
            f"{macho.name} needs",
            hint="install Xcode's command line tools: xcode-select --install",
        ) from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        # Never `return []` here. An empty list means "nothing to stub", which
        # the build reports as "every injected dylib already links frameworks
        # macOS has" — an assertion about dylibs that were never read. The user
        # would ship an IPA that still dies at launch, having been told the
        # opposite. This function is the only gate on the whole feature.
        raise PlayCoverPrepError(
            f"couldn't read {macho.name}'s linked libraries",
            hint=str(exc)[:200],
        ) from exc

    # Dependency lines are indented; the un-indented ones are the file's own
    # path and, on a fat binary, one `path (architecture x):` header per slice.
    # A fixed `[2:]` drops only the first slice's header and id, so slice two's
    # header re-enters the list — the file reporting itself as a dependency,
    # which is the survey bug this docstring describes. Indentation is
    # arch-count independent.
    deps = [
        line.split()[0]
        for line in out.splitlines()
        if line.startswith((" ", "\t")) and line.strip()
    ]
    # The LC_ID_DYLIB is indented too, so drop any entry equal to this file's
    # own install name.
    own = {line.split()[0] for line in out.splitlines()[1:2] if line.strip()}
    deps = [d for d in deps if d not in own]
    return [n for n in STUBBABLE_PRIVATE_FRAMEWORKS
            if _private_framework_path(n) in deps]


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


def retarget_to_bundled_framework(macho: Path, framework: str) -> None:
    """Point `macho` at `@rpath/<framework>.framework/...` instead of /System.

    Re-signs afterwards, and the honest reason is narrower than it looks.
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
    _run_tool(
        ["install_name_tool", "-change", _private_framework_path(framework),
         f"@rpath/{framework}.framework/{framework}", str(macho)],
        doing=f"pointing {macho.name} at the bundled {framework}.framework",
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
    if "@loader_path" not in _rpaths(macho):
        _run_tool(
            ["install_name_tool", "-add_rpath", "@loader_path", str(macho)],
            doing=f"giving {macho.name} an rpath that reaches the bundle",
        )
    _run_tool(["codesign", "-f", "-s", "-", str(macho)],
              doing=f"re-signing {macho.name}")


def _rpaths(macho: Path) -> list[str]:
    """The LC_RPATH entries already on `macho`."""
    try:
        out = subprocess.run(
            ["otool", "-l", str(macho)], capture_output=True, text=True, check=True,
            env=child_env(),
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return []
    found, lines = [], out.splitlines()
    for i, line in enumerate(lines):
        if "LC_RPATH" in line:
            for follow in lines[i:i + 4]:
                if "path " in follow:
                    found.append(follow.split("path ", 1)[1].split(" (")[0].strip())
                    break
    return found


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
    script = STUB_TOOLS_DIR / "build.sh"
    if not script.is_file():
        raise PlayCoverPrepError(
            f"the stub builder is missing at {script}",
            hint="it ships in this repo under graft_ipa/stubs/preferences-stub/",
        )
    out = dest_dir / f"{framework}.framework"
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
    for macho in sorted(frameworks.glob("*.dylib")):
        for framework in linked_private_frameworks(macho):
            bundled = frameworks / f"{framework}.framework"
            if not bundled.exists():
                build_stub(framework, frameworks)
            retarget_to_bundled_framework(macho, framework)
            fixed.append(f"{macho.name} → bundled {framework}.framework")
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
