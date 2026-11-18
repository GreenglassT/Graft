"""`graft_ipa.playcover` — making a tweaked IPA loadable on macOS.

These build real Mach-O dylibs with `clang` rather than using fixtures: the
whole subject is load commands, and a fixture that only *looked* like a dylib
would prove nothing about `otool` or `install_name_tool`.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from graft_ipa.playcover import (
    linked_private_frameworks,
    prepare_frameworks_dir,
    prepare_ipa,
    retarget_to_bundled_framework,
)


def _has_ios_sdk() -> bool:
    """Whether `xcrun -sdk iphoneos` can find an SDK.

    Half these cases reach `build_stub`, which shells out to
    `xcrun -sdk iphoneos clang`. Guarding on `which("clang")` alone passes on a
    Command Line Tools install — which has clang and otool but no iOS SDK — and
    the cases then fail with `PlayCoverPrepError: couldn't build the
    Preferences stub` instead of skipping.
    """
    try:
        return subprocess.run(
            ["xcrun", "--sdk", "iphoneos", "--show-sdk-path"],
            capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    shutil.which("clang") is None
    or shutil.which("otool") is None
    or not _has_ios_sdk(),
    reason="needs clang, otool and the iOS SDK to build and read a real dylib",
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


def test_a_dylib_linking_a_stubbable_private_framework_is_found(needy: Path) -> None:
    assert linked_private_frameworks(needy) == ["Preferences"]


def test_a_dylib_linking_nothing_private_is_left_alone(tmp_path: Path) -> None:
    assert linked_private_frameworks(_dylib(tmp_path, "plain")) == []


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


def test_retargeting_points_the_load_command_at_the_bundle(needy: Path) -> None:
    lib = needy
    retarget_to_bundled_framework(lib, "Preferences")
    deps = subprocess.run(
        ["otool", "-L", str(lib)], capture_output=True, text=True, check=True
    ).stdout
    assert "@rpath/Preferences.framework/Preferences" in deps
    assert PREFS not in deps, "the /System path must be gone, not merely joined"
    # And it is no longer reported as needing a stub, which is what makes this
    # idempotent rather than something that must run exactly once.
    assert linked_private_frameworks(lib) == []


def test_retargeting_leaves_the_dylib_signed(needy: Path) -> None:
    """The dylib is loadable afterwards, however that gets achieved.

    Deliberately *not* a test that the explicit `codesign` call matters:
    measured on this toolchain, `install_name_tool` re-signs ad-hoc on its own,
    so deleting that call passes this and every other case. The property worth
    pinning is the outcome -- a patched dylib still verifies -- not which of
    the two things produced it. See `retarget_to_bundled_framework`'s doc.
    """
    retarget_to_bundled_framework(needy, "Preferences")
    r = subprocess.run(["codesign", "-v", str(needy)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


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
