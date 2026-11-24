"""`graft_ipa.playcover` — making a tweaked IPA loadable on macOS.

Most of these build real Mach-O dylibs with `clang` rather than using
fixtures: the whole subject is load commands, and a fixture that only *looked*
like a dylib would prove nothing about the reader or `install_name_tool`. The
few at the end use synthetic load commands, so the reading half is still
covered on a Mac with no clang or iOS SDK.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from graft_ipa import macho, playcover
from graft_ipa.playcover import (
    linked_private_frameworks,
    prepare_frameworks_dir,
    prepare_ipa,
    retarget_to_bundled_framework,
)

from . import macho_builder as mb

#: Only on the tests that build a real dylib. The synthetic load-command tests
#: below need none of this, and must not be skipped on a Mac without it.
#:
#: Asked of the developer directory, as the product asks it
#: (`playcover.developer_dir`), never of `xcrun` or `which("clang")`: both are
#: shims on every Mac, which pop the install dialog where there are no tools.
#: A Command Line Tools install has clang but no iOS SDK, and these cases
#: reach `build_stub`, so the SDK is what decides.
needs_clang_and_sdk = pytest.mark.skipif(
    not playcover.has_ios_sdk(playcover.developer_dir()),
    reason="needs Xcode with the iOS SDK to build a real dylib",
)


PREFS = "/System/Library/PrivateFrameworks/Preferences.framework/Preferences"


def _dylib(tmp_path: Path, name: str, *, install_name: str | None = None,
           link_against: Path | None = None) -> Path:
    """A real, minimal dylib, optionally linked against another one.

    `link_against` records the *linked* library's `install_name` as the load
    command, not its path on disk — which is the only way to produce a
    dependency on a file that does not exist here. Linking the absolute
    /System path directly fails outright:

        ld: library '/System/Library/.../Preferences' not found

    and that is also how the real tweaks acquired this dependency: built
    against a stub or SDK whose install name pointed at the on-device path.
    """
    src = tmp_path / f"{name}.c"
    src.write_text(f"int {name}_sym(void) {{ return 0; }}\n")
    out = tmp_path / f"{name}.dylib"
    cmd = ["clang", "-dynamiclib", "-o", str(out), str(src)]
    if install_name:
        cmd += ["-install_name", install_name]
    if link_against:
        cmd.append(str(link_against))
    subprocess.run(cmd, capture_output=True, check=True)
    return out


@pytest.fixture
def needy(tmp_path: Path) -> Path:
    """A dylib whose load commands name the on-device Preferences path."""
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    fake_prefs = _dylib(sdk, "Preferences", install_name=PREFS)
    return _dylib(tmp_path, "needy", link_against=fake_prefs)


@needs_clang_and_sdk
def test_a_dylib_linking_a_stubbable_private_framework_is_found(needy: Path) -> None:
    assert linked_private_frameworks(needy) == ["Preferences"]


@needs_clang_and_sdk
def test_a_dylib_linking_nothing_private_is_left_alone(tmp_path: Path) -> None:
    assert linked_private_frameworks(_dylib(tmp_path, "plain")) == []


@needs_clang_and_sdk
def test_the_dylibs_own_id_is_never_mistaken_for_a_dependency(tmp_path: Path) -> None:
    """The trap this function exists to avoid, and which really happened.

    A tweak's `LC_ID_DYLIB` is
    `/Library/MobileSubstrate/DynamicLibraries/<name>.dylib` — which contains
    the substring "Substrate". An earlier survey grepped all of `otool -L` and
    concluded nearly every tweak linked Substrate, when it was reading each
    file's own id back at itself. So: give a dylib an install name that would
    match, and require it is still reported as linking nothing.
    """
    src = tmp_path / "selfnamed.c"
    src.write_text("int y(void) { return 0; }\n")
    lib = tmp_path / "selfnamed.dylib"
    subprocess.run(
        ["clang", "-dynamiclib", "-install_name", PREFS, "-o", str(lib), str(src)],
        capture_output=True, check=True,
    )
    assert linked_private_frameworks(lib) == [], "its own id is not a dependency"


@needs_clang_and_sdk
def test_retargeting_points_the_load_command_at_the_bundle(needy: Path) -> None:
    lib = needy
    retarget_to_bundled_framework(lib, "Preferences")
    deps = macho.dylib_names(lib)
    assert "@rpath/Preferences.framework/Preferences" in deps
    assert PREFS not in deps, "the /System path must be gone, not merely joined"
    # And it is no longer reported as needing a stub, which is what makes this
    # idempotent rather than something that must run exactly once.
    assert linked_private_frameworks(lib) == []


@needs_clang_and_sdk
def test_retargeting_leaves_the_dylib_signed(needy: Path) -> None:
    """The dylib is loadable afterward, however that gets achieved.

    Deliberately *not* a test that the explicit `codesign` call matters:
    measured on this toolchain, `install_name_tool` re-signs ad-hoc on its own,
    so deleting that call passes this and every other case. The property worth
    pinning is the outcome -- a patched dylib still verifies -- not which of
    the two things produced it. See `retarget_to_bundled_framework`'s doc.
    """
    retarget_to_bundled_framework(needy, "Preferences")
    r = subprocess.run(["codesign", "-v", str(needy)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@needs_clang_and_sdk
class TestPreparingAFrameworksDir:
    """The directory-level step: find, stub, retarget, report."""

    def test_it_stubs_and_retargets_the_dylib_that_needs_it(
        self, tmp_path: Path, needy: Path
    ) -> None:
        frameworks = tmp_path / "Frameworks"
        frameworks.mkdir()
        shutil.copy(needy, frameworks / "libexample.dylib")
        plain = _dylib(tmp_path, "plain")
        shutil.copy(plain, frameworks / "DTweak.dylib")

        fixed = prepare_frameworks_dir(frameworks)

        assert len(fixed) == 1, f"only the needy one, got {fixed}"
        assert "libexample.dylib" in fixed[0]
        assert (frameworks / "Preferences.framework" / "Preferences").is_file()
        assert linked_private_frameworks(
            frameworks / "libexample.dylib"
        ) == [], "retargeted, so nothing left to fix"

    def test_running_it_twice_changes_nothing_the_second_time(
        self, tmp_path: Path, needy: Path
    ) -> None:
        """Idempotent, because the build pipeline may retry a stage and a
        second pass must not re-stub or re-point an already-correct dylib."""
        frameworks = tmp_path / "Frameworks"
        frameworks.mkdir()
        shutil.copy(needy, frameworks / "libexample.dylib")
        assert prepare_frameworks_dir(frameworks) != []
        assert prepare_frameworks_dir(frameworks) == []

    def test_a_bundle_with_nothing_to_fix_builds_no_stub(self, tmp_path: Path) -> None:
        """The common case — eleven of the twelve tweaks here — must not drag
        in Xcode. `build_stub` shells out to clang, so an unnecessary call is
        both slow and a spurious hard dependency."""
        frameworks = tmp_path / "Frameworks"
        frameworks.mkdir()
        shutil.copy(_dylib(tmp_path, "plain"), frameworks / "SomeTweak.dylib")
        assert prepare_frameworks_dir(frameworks) == []
        assert not (frameworks / "Preferences.framework").exists()

    def test_a_missing_frameworks_dir_is_not_an_error(self, tmp_path: Path) -> None:
        """An app with no injected tweaks has no Frameworks directory, and
        preparing it is a no-op rather than a failure."""
        assert prepare_frameworks_dir(tmp_path / "nope") == []


@needs_clang_and_sdk
class TestPreparingAnIPA:
    """The archive-level entry point."""

    def _ipa(self, tmp_path: Path, *dylibs: Path) -> Path:
        ipa = tmp_path / "app.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.writestr("Payload/App.app/Info.plist", "x")
            for d in dylibs:
                zf.write(d, f"Payload/App.app/Frameworks/{d.name}")
        return ipa

    def test_it_fixes_the_dylib_inside_the_archive(
        self, tmp_path: Path, needy: Path
    ) -> None:
        settings_lib = tmp_path / "libexample.dylib"
        shutil.copy(needy, settings_lib)
        ipa = self._ipa(tmp_path, settings_lib)

        fixed = prepare_ipa(ipa)

        assert len(fixed) == 1
        with zipfile.ZipFile(ipa) as zf:
            names = zf.namelist()
        assert "Payload/App.app/Frameworks/Preferences.framework/Preferences" in names
        # And the *effective* dylib in the archive is the retargeted one.
        out = tmp_path / "check"
        with zipfile.ZipFile(ipa) as zf:
            zf.extractall(out)
        assert linked_private_frameworks(
            out / "Payload/App.app/Frameworks/libexample.dylib"
        ) == []

    def test_an_ipa_with_nothing_to_fix_is_not_rewritten(
        self, tmp_path: Path
    ) -> None:
        """Repacking a 120 MB archive to change nothing is pure cost, so the
        common case must leave the file untouched — byte for byte."""
        ipa = self._ipa(tmp_path, _dylib(tmp_path, "plain"))
        before = ipa.read_bytes()
        assert prepare_ipa(ipa) == []
        assert ipa.read_bytes() == before, "untouched, not merely equivalent"

    def test_an_ipa_with_no_frameworks_at_all_is_a_no_op(self, tmp_path: Path) -> None:
        ipa = self._ipa(tmp_path)
        assert prepare_ipa(ipa) == []

    def test_the_rebuilt_archive_has_no_duplicate_entries(
        self, tmp_path: Path, needy: Path
    ) -> None:
        """The first implementation appended, leaving two members named
        `.../libexample.dylib` and relying on every reader preferring the
        later one. `zipfile` warns about it; PlayCover's unpacker does not, and
        its tie-breaking is not documented here. An IPA carrying a stale copy
        of the very dylib being fixed fails once, on someone else's machine.
        """
        settings_lib = tmp_path / "libexample.dylib"
        shutil.copy(needy, settings_lib)
        ipa = self._ipa(tmp_path, settings_lib)
        prepare_ipa(ipa)
        with zipfile.ZipFile(ipa) as zf:
            names = zf.namelist()
        assert len(names) == len(set(names)), \
            [n for n in names if names.count(n) > 1]

    def test_entries_it_does_not_touch_survive_the_rebuild(
        self, tmp_path: Path, needy: Path
    ) -> None:
        """Rebuilding is the risky half of the fix: everything outside
        Frameworks/ has to come through byte-identical, or the IPA is worse off
        than before it was 'prepared'."""
        settings_lib = tmp_path / "libexample.dylib"
        shutil.copy(needy, settings_lib)
        ipa = self._ipa(tmp_path, settings_lib)
        with zipfile.ZipFile(ipa, "a") as zf:
            zf.writestr("Payload/App.app/embedded.mobileprovision", "profile-bytes")
        prepare_ipa(ipa)
        with zipfile.ZipFile(ipa) as zf:
            assert zf.read("Payload/App.app/Info.plist") == b"x"
            assert zf.read(
                "Payload/App.app/embedded.mobileprovision"
            ) == b"profile-bytes"

    def test_the_patched_dylib_keeps_its_executable_bit(
        self, tmp_path: Path, needy: Path
    ) -> None:
        """A dylib that comes back 0644 is a broken app.

        `ZipFile.extract` does not restore `external_attr`, so the copy on disk
        is 0644 after umask and re-adding it with `write()` re-derives the mode
        from there. Measured before the fix: libexample.dylib went
        0o100755 → 0o100644, while entries outside Frameworks/ — passed through
        with `writestr` and their original ZipInfo — kept theirs. That is why
        `test_entries_it_does_not_touch_survive_the_rebuild` could not see it.
        """
        settings_lib = tmp_path / "libexample.dylib"
        shutil.copy(needy, settings_lib)
        settings_lib.chmod(0o755)
        ipa = self._ipa(tmp_path, settings_lib)
        name = "Payload/App.app/Frameworks/libexample.dylib"
        with zipfile.ZipFile(ipa) as zf:
            before = zf.getinfo(name).external_attr >> 16
        assert before & 0o111, "premise: it went in executable"

        prepare_ipa(ipa)

        with zipfile.ZipFile(ipa) as zf:
            after = zf.getinfo(name).external_attr >> 16
        assert after & 0o111, f"lost +x: {oct(before)} -> {oct(after)}"
        assert after == before, f"mode changed: {oct(before)} -> {oct(after)}"


# ---- synthetic load commands: no clang, SDK or otool needed ----

@pytest.fixture
def no_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError(f"unexpected subprocess: {a[0] if a else k}")
    monkeypatch.setattr(playcover.subprocess, "run", boom)


def test_linked_private_frameworks_reads_load_commands(tmp_path, no_subprocess):
    lib = tmp_path / "libexample.dylib"
    lib.write_bytes(mb.thin([
        mb.dylib_cmd("/Library/MobileSubstrate/DynamicLibraries/libexample.dylib", cmd=mb.LC_ID_DYLIB),
        mb.dylib_cmd("/System/Library/PrivateFrameworks/Preferences.framework/Preferences"),
    ], filetype=mb.MH_DYLIB))
    assert playcover.linked_private_frameworks(lib) == ["Preferences"]


def test_an_unreadable_dylib_is_an_error_never_nothing_to_stub(tmp_path, no_subprocess):
    lib = tmp_path / "libexample.dylib"
    lib.write_bytes(b"not a mach-o")
    with pytest.raises(playcover.PlayCoverPrepError, match=r"couldn't read libexample\.dylib's linked libraries"):
        playcover.linked_private_frameworks(lib)


def test_rpaths_reads_load_commands(tmp_path, no_subprocess):
    lib = tmp_path / "libexample.dylib"
    lib.write_bytes(mb.thin([mb.rpath_cmd("@loader_path")], filetype=mb.MH_DYLIB))
    assert playcover._rpaths(lib) == ["@loader_path"]


# ---- review #41: Xcode is found or named, never shimmed into ----

def _needy_synthetic(frameworks: Path) -> Path:
    frameworks.mkdir(parents=True, exist_ok=True)
    lib = frameworks / "libexample.dylib"
    lib.write_bytes(mb.thin([
        mb.dylib_cmd("/Library/MobileSubstrate/DynamicLibraries/libexample.dylib", cmd=macho.LC_ID_DYLIB),
        mb.dylib_cmd(PREFS),
    ], filetype=mb.MH_DYLIB))
    return lib


def _fake_tool(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    tool = directory / name
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return tool


def test_without_xcode_the_stub_build_says_so_and_runs_nothing(tmp_path, monkeypatch):
    """On a Mac with no Xcode, `xcrun -sdk iphoneos clang` is a shim that pops
    the install dialog and fails; --check had nothing to say about it."""
    _needy_synthetic(tmp_path / "Frameworks")
    monkeypatch.setattr(playcover, "developer_dir", lambda: None)

    def boom(argv, *a, **k):
        raise AssertionError(f"ran {argv}")

    monkeypatch.setattr(playcover.subprocess, "run", boom)

    with pytest.raises(playcover.PlayCoverPrepError, match="needs Xcode with its iOS SDK") as e:
        prepare_frameworks_dir(tmp_path / "Frameworks")
    assert "xcode-select -s" in (e.value.hint or "")


def test_the_command_line_tools_alone_are_not_enough(tmp_path, monkeypatch):
    clt = tmp_path / "CommandLineTools"
    _fake_tool(clt / "usr" / "bin", "install_name_tool")
    monkeypatch.setattr(playcover, "developer_dir", lambda: clt)

    assert not playcover.has_ios_sdk(clt)


def test_a_bundled_install_name_tool_is_run_by_its_path(tmp_path, monkeypatch):
    """Graft.app's is cyan's, built from cctools-port with its fake-sign
    hook compiled out: never the xcrun shim, never Xcode's (decision 9)."""
    contents = tmp_path / "Graft.app" / "Contents"
    helpers = contents / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    bundled = _fake_tool((contents / playcover.BUNDLED_INSTALL_NAME_TOOL).parent, "install_name_tool")
    monkeypatch.setattr(playcover.tools, "bundled_tools_dir", lambda: helpers)
    monkeypatch.setattr(playcover, "developer_dir", lambda: None)
    ran: list[list[str]] = []
    monkeypatch.setattr(playcover.subprocess, "run", lambda argv, **k: ran.append(argv))

    retarget_to_bundled_framework(_needy_synthetic(tmp_path / "F"), "Preferences")

    tools_run = [argv[0] for argv in ran if argv[0] != "codesign"]
    assert tools_run and set(tools_run) == {str(bundled)}


def test_without_a_bundled_copy_the_developer_dir_s_is_used_not_the_shim(tmp_path, monkeypatch):
    dev = tmp_path / "Xcode.app" / "Contents" / "Developer"
    own = _fake_tool(dev / "Toolchains" / "XcodeDefault.xctoolchain" / "usr" / "bin", "install_name_tool")
    monkeypatch.setattr(playcover.tools, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(playcover, "developer_dir", lambda: dev)

    assert playcover.install_name_tool() == own


def test_with_neither_install_name_tool_is_named_with_the_fix(monkeypatch):
    monkeypatch.setattr(playcover.tools, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(playcover, "developer_dir", lambda: None)

    with pytest.raises(playcover.PlayCoverPrepError, match="install_name_tool isn't installed") as e:
        playcover.install_name_tool()
    assert e.value.hint == playcover.XCODE_HINT


def test_the_developer_dir_is_asked_of_xcode_select_not_xcrun(tmp_path, monkeypatch):
    monkeypatch.delenv("DEVELOPER_DIR", raising=False)
    asked: list[list[str]] = []

    def answer(argv, **k):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=f"{tmp_path}\n", stderr="")

    monkeypatch.setattr(playcover.subprocess, "run", answer)

    assert playcover.developer_dir() == tmp_path
    assert asked == [["/usr/bin/xcode-select", "-p"]]


def test_developer_dir_honours_the_environment(tmp_path, monkeypatch, no_subprocess):
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path))
    assert playcover.developer_dir() == tmp_path
    monkeypatch.setenv("DEVELOPER_DIR", str(tmp_path / "nonexistent-dev"))
    assert playcover.developer_dir() is None


def test_developer_dir_may_name_xcode_app_itself_as_xcrun_accepts(tmp_path, monkeypatch, no_subprocess):
    """Fix-check #20: `DEVELOPER_DIR=/Applications/Xcode.app` works for xcrun,
    and so for the tools found under it."""
    xcode = tmp_path / "Xcode.app"
    (xcode / "Contents" / "Developer").mkdir(parents=True)
    monkeypatch.setenv("DEVELOPER_DIR", str(xcode))
    assert playcover.developer_dir() == xcode / "Contents" / "Developer"


def test_graft_apps_prebuilt_stub_is_used_and_nothing_is_built(tmp_path, monkeypatch, no_subprocess):
    """Decision 9: Graft.app carries the stub, built at release, so a Mac
    with no Xcode can still make a PlayCover build."""
    prebuilt = tmp_path / "stubs" / "Preferences.framework"
    prebuilt.mkdir(parents=True)
    (prebuilt / "Preferences").write_bytes(b"stub")
    (prebuilt / "Info.plist").write_text("<plist/>")
    monkeypatch.setattr(playcover, "STUB_TOOLS_DIR", prebuilt.parent)
    monkeypatch.setattr(playcover, "developer_dir", lambda: pytest.fail("asked for Xcode"))
    out = playcover.build_stub("Preferences", tmp_path / "Frameworks")
    assert out == tmp_path / "Frameworks" / "Preferences.framework"
    assert (out / "Preferences").read_bytes() == b"stub" and (out / "Info.plist").is_file()


def test_a_build_with_nothing_to_stub_never_looks_for_xcode(tmp_path, monkeypatch, no_subprocess):
    """Only a tweak linking a private framework needs the tools, so a Mac
    without Xcode still builds every other app for PlayCover."""
    frameworks = tmp_path / "Frameworks"
    frameworks.mkdir()
    (frameworks / "libplain.dylib").write_bytes(mb.thin([
        mb.dylib_cmd("/System/Library/Frameworks/UIKit.framework/UIKit"),
    ], filetype=mb.MH_DYLIB))

    def unasked():
        raise AssertionError("looked for Xcode with nothing to stub")

    monkeypatch.setattr(playcover, "developer_dir", unasked)

    assert prepare_frameworks_dir(frameworks) == []
