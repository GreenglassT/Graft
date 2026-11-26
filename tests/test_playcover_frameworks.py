"""PlayCover's framework check (spec 5d items 3 and 7).

A tweak that links an iOS framework macOS doesn't have is built, handed over,
and crashes at launch. The check reads the load commands of every
`Frameworks/*.dylib` and `Frameworks/*.framework/<executable>` and finds each
required `/System/Library/{Frameworks,PrivateFrameworks}/X.framework/X` that
Graft doesn't stub and this Mac doesn't have. Only a tweak's binaries refuse;
the app's own warn.

Everything here runs against synthetic Mach-Os and scratch framework roots,
except `TestThisMacsRealRoots`, which only reads `/System`.
"""

from __future__ import annotations

import plistlib
import zipfile
from pathlib import Path

import pytest

from graft_ipa import playcover

from . import macho_builder as mb

SBS = "/System/Library/PrivateFrameworks/SpringBoardServices.framework/SpringBoardServices"
UIKIT = "/System/Library/Frameworks/UIKit.framework/UIKit"
PREFS = "/System/Library/PrivateFrameworks/Preferences.framework/Preferences"
FOUNDATION = "/System/Library/Frameworks/Foundation.framework/Foundation"


def _binary(path: Path, *deps: str, weak: tuple[str, ...] = ()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmds = [mb.dylib_cmd(d) for d in deps] + [mb.dylib_cmd(d, cmd=mb.LC_LOAD_WEAK_DYLIB) for d in weak]
    path.write_bytes(mb.thin(cmds, filetype=mb.MH_DYLIB))
    return path


def _framework(frameworks: Path, name: str, *deps: str, executable: str | None = None) -> Path:
    fw = frameworks / f"{name}.framework"
    exe = executable or name
    fw.mkdir(parents=True)
    (fw / "Info.plist").write_bytes(plistlib.dumps({"CFBundleExecutable": exe}))
    _binary(fw / exe, *deps)
    return fw


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A Mac root with Foundation, and an iOS-support root with UIKit."""
    mac, ios = tmp_path / "mac", tmp_path / "ios"
    (mac / "System/Library/Frameworks/Foundation.framework").mkdir(parents=True)
    (ios / "System/Library/Frameworks/UIKit.framework").mkdir(parents=True)
    (mac / "System/Library/PrivateFrameworks").mkdir(parents=True)
    monkeypatch.setattr(playcover, "FRAMEWORK_ROOTS", (mac, ios))
    return mac, ios


class TestWhatIsUnsupported:
    def test_a_framework_in_neither_root_is_unsupported(self, tmp_path, roots) -> None:
        lib = _binary(tmp_path / "libexample.dylib", FOUNDATION, UIKIT, SBS)
        assert playcover.unsupported_frameworks(lib) == ["SpringBoardServices"]

    def test_a_stubbed_framework_is_not_unsupported(self, tmp_path, roots) -> None:
        assert playcover.unsupported_frameworks(_binary(tmp_path / "l.dylib", PREFS)) == []

    def test_a_weak_link_is_never_unsupported(self, tmp_path, roots) -> None:
        """dyld skips a missing weak library."""
        lib = _binary(tmp_path / "l.dylib", FOUNDATION, weak=(SBS,))
        assert playcover.unsupported_frameworks(lib) == []

    def test_presence_is_the_framework_directory_not_its_binary(self, tmp_path, roots) -> None:
        """Since macOS 11 a system framework's binary is only in the dyld
        shared cache: the directory is there, the file isn't. Foundation in
        the scratch root has no binary, like the real one."""
        mac, _ = roots
        assert not (mac / "System/Library/Frameworks/Foundation.framework/Foundation").exists()
        assert playcover.unsupported_frameworks(_binary(tmp_path / "l.dylib", FOUNDATION)) == []

    def test_a_file_named_like_the_framework_is_not_the_framework(self, tmp_path, roots) -> None:
        mac, _ = roots
        (mac / "System/Library/PrivateFrameworks/SpringBoardServices.framework").write_bytes(b"")
        lib = _binary(tmp_path / "l.dylib", SBS)
        assert playcover.unsupported_frameworks(lib) == ["SpringBoardServices"]

    def test_a_private_framework_found_under_ios_support_is_supported(self, tmp_path, roots) -> None:
        _, ios = roots
        (ios / "System/Library/PrivateFrameworks/SpringBoardServices.framework").mkdir(parents=True)
        assert playcover.unsupported_frameworks(_binary(tmp_path / "l.dylib", SBS)) == []

    def test_usr_lib_is_not_checked(self, tmp_path, roots) -> None:
        """On macOS those live in the shared cache, not on disk. Known limit."""
        lib = _binary(tmp_path / "l.dylib", "/usr/lib/libexample_ios_only.dylib")
        assert playcover.unsupported_frameworks(lib) == []


class TestWhoBroughtIt:
    def test_a_debs_dylibs_and_frameworks_are_its_own(self, tmp_path: Path) -> None:
        from .test_deb_control import _ar, _control_tar, _data_tar

        deb = tmp_path / "example-tweak_1.0.deb"
        deb.write_bytes(_ar([
            ("debian-binary", b"2.0\n"),
            ("control.tar.gz", _control_tar()),
            ("data.tar.gz", _data_tar([
                "./Library/MobileSubstrate/DynamicLibraries/Example.dylib",
                "./Library/MobileSubstrate/DynamicLibraries/Example.plist",
                "./Library/Frameworks/ExampleKit.framework/ExampleKit",
                "./Library/Frameworks/ExampleKit.framework/Info.plist",
                "./Library/PreferenceBundles/ExamplePrefs.bundle/ExamplePrefs",
            ])),
        ]))
        bare = tmp_path / "libother.dylib"
        bare.write_bytes(b"")

        assert playcover.tweak_origins([deb, bare]) == {
            "Example.dylib": "example-tweak_1.0.deb",
            "ExampleKit.framework": "example-tweak_1.0.deb",
            "libother.dylib": "libother.dylib",
        }

    def test_a_deb_it_cannot_read_claims_nothing(self, tmp_path: Path) -> None:
        bad = tmp_path / "broken.deb"
        bad.write_bytes(b"not a deb")
        assert playcover.tweak_origins([bad]) == {}


class TestTheScan:
    def test_a_tweaks_dylib_refuses_and_the_apps_own_warns(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "Frameworks"
        _binary(frameworks / "Example.dylib", SBS)
        _binary(frameworks / "libAppOwn.dylib", SBS)

        found = playcover.scan_frameworks(frameworks, {"Example.dylib": "example.deb"})

        assert [(f.binary, f.framework, f.tweak) for f in found] == [
            ("Example.dylib", "SpringBoardServices", "example.deb"),
            ("libAppOwn.dylib", "SpringBoardServices", None),
        ]
        assert [f.binary for f in found if f.refuses] == ["Example.dylib"]

    def test_a_tweaks_framework_is_scanned_by_its_executable(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "Frameworks"
        _framework(frameworks, "ExampleKit", SBS, executable="ExampleKitBinary")

        found = playcover.scan_frameworks(frameworks, {"ExampleKit.framework": "example.deb"})

        assert [(f.binary, f.framework, f.refuses) for f in found] == [
            ("ExampleKit.framework", "SpringBoardServices", True),
        ]

    def test_an_apps_own_framework_only_warns(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "Frameworks"
        _framework(frameworks, "AppKitOwn", SBS)
        found = playcover.scan_frameworks(frameworks, {"Example.dylib": "example.deb"})
        assert [(f.binary, f.refuses) for f in found] == [("AppKitOwn.framework", False)]

    def test_when_the_tweaks_cant_be_told_nothing_refuses(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "Frameworks"
        _binary(frameworks / "Example.dylib", SBS)
        found = playcover.scan_frameworks(frameworks, None)
        assert [(f.binary, f.refuses) for f in found] == [("Example.dylib", False)]

    def test_a_weak_link_in_a_tweak_is_not_refused(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "Frameworks"
        _binary(frameworks / "Example.dylib", FOUNDATION, weak=(SBS,))
        assert playcover.scan_frameworks(frameworks, {"Example.dylib": "example.deb"}) == []

    def test_a_framework_with_no_readable_binary_is_passed_over(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "Frameworks"
        (frameworks / "Resources.framework").mkdir(parents=True)
        (frameworks / "Odd.framework").mkdir()
        (frameworks / "Odd.framework/Odd").write_bytes(b"!<arch>\n")
        assert playcover.scan_frameworks(frameworks, {}) == []

    def test_a_half_written_framework_info_plist_falls_back_to_its_name(self, tmp_path, roots) -> None:
        """`plistlib` raises ExpatError, no ValueError, for truncated XML."""
        frameworks = tmp_path / "Frameworks"
        fw = frameworks / "ExampleKit.framework"
        fw.mkdir(parents=True)
        (fw / "Info.plist").write_bytes(b'<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundle')
        _binary(fw / "ExampleKit", SBS)

        found = playcover.scan_frameworks(frameworks, {"ExampleKit.framework": "example.deb"})

        assert [(f.binary, f.refuses) for f in found] == [("ExampleKit.framework", True)]

    def test_no_frameworks_folder_is_nothing_to_scan(self, tmp_path, roots) -> None:
        assert playcover.scan_frameworks(tmp_path / "nope", {}) == []


class TestTheWords:
    def test_the_refusal_names_the_tweak_and_the_framework(self) -> None:
        found = [playcover.UnsupportedLink("Example.dylib", "SpringBoardServices", "example.deb")]
        assert playcover.refusal_message(found) == (
            "example.deb needs iOS's SpringBoardServices, which macOS doesn't have, so it "
            "can't run under PlayCover. Phone builds can still use it; to build for this "
            "Mac, remove it from the app first."
        )

    def test_two_frameworks_from_one_tweak_are_one_sentence(self) -> None:
        found = [
            playcover.UnsupportedLink("Example.dylib", "SpringBoardServices", "example.deb"),
            playcover.UnsupportedLink("ExampleKit.framework", "BulletinBoard", "example.deb"),
        ]
        assert playcover.refusal_message(found).startswith(
            "example.deb needs iOS's SpringBoardServices and BulletinBoard, which macOS "
            "doesn't have"
        )

    def test_the_warning_names_the_binary(self) -> None:
        link = playcover.UnsupportedLink("libAppOwn.dylib", "SpringBoardServices", None)
        assert playcover.warning_message(link) == (
            "libAppOwn.dylib in the app needs iOS's SpringBoardServices, which macOS "
            "doesn't have, so the app may not run under PlayCover."
        )


class TestAnArchive:
    def _ipa(self, tmp_path: Path, frameworks: Path) -> Path:
        ipa = tmp_path / "Example.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.writestr("Payload/Example.app/Info.plist", plistlib.dumps({"CFBundleIdentifier": "x"}))
            for path in sorted(frameworks.rglob("*")):
                if path.is_file():
                    zf.write(path, f"Payload/Example.app/Frameworks/{path.relative_to(frameworks)}")
        return ipa

    def test_an_archives_dylibs_and_frameworks_are_read(self, tmp_path, roots) -> None:
        frameworks = tmp_path / "src"
        _binary(frameworks / "Example.dylib", SBS)
        _framework(frameworks, "ExampleKit", SBS)
        _binary(frameworks / "libFine.dylib", FOUNDATION)
        ipa = self._ipa(tmp_path, frameworks)

        found = playcover.check_ipa(ipa, {"Example.dylib": "example.deb"})

        assert [(f.binary, f.refuses) for f in found] == [
            ("Example.dylib", True), ("ExampleKit.framework", False),
        ]

    def test_an_archive_without_frameworks_has_nothing(self, tmp_path, roots) -> None:
        ipa = tmp_path / "Plain.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.writestr("Payload/Plain.app/Info.plist", b"x")
        assert playcover.check_ipa(ipa, None) == []


class TestThisMacsRealRoots:
    """Read-only, against this Mac's own `/System` (spec 5d item 7)."""

    def test_foundation_is_present(self) -> None:
        assert playcover.framework_present("Foundation")

    def test_uikit_is_present_only_under_ios_support(self) -> None:
        assert not Path("/System/Library/Frameworks/UIKit.framework").exists()
        assert playcover.framework_present("UIKit")

    def test_preferences_is_stubbed_not_present(self, tmp_path: Path) -> None:
        assert not playcover.framework_present("Preferences")
        assert playcover.unsupported_frameworks(_binary(tmp_path / "l.dylib", PREFS)) == []

    def test_springboardservices_is_in_neither_root_and_refused(self, tmp_path: Path) -> None:
        """An iOS private framework tweaks link, which this Mac has in
        neither root (listed before choosing it)."""
        assert not playcover.framework_present("SpringBoardServices")
        assert playcover.unsupported_frameworks(_binary(tmp_path / "l.dylib", SBS)) == [
            "SpringBoardServices",
        ]


class TestTheRetargetCoversFrameworks:
    """`prepare_frameworks_dir` retargets a tweak's framework at the stub, as
    it does a dylib (spec 5d item 7): a settings library's framework links
    Preferences."""

    @pytest.fixture
    def retargets(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
        done: list[tuple[str, str, str]] = []

        def build_stub(framework: str, dest: Path) -> Path:
            out = dest / f"{framework}.framework"
            out.mkdir()
            return out

        monkeypatch.setattr(playcover, "build_stub", build_stub)
        monkeypatch.setattr(
            playcover, "retarget_to_bundled_framework",
            lambda binary, framework, *, rpath="@loader_path": done.append(
                (str(binary), framework, rpath)),
        )
        return done

    def test_a_frameworks_binary_is_retargeted_with_an_rpath_one_level_up(
        self, tmp_path: Path, retargets: list
    ) -> None:
        frameworks = tmp_path / "Frameworks"
        _framework(frameworks, "ExampleKit", PREFS)
        _binary(frameworks / "Example.dylib", PREFS)

        fixed = playcover.prepare_frameworks_dir(frameworks)

        assert retargets == [
            (str(frameworks / "Example.dylib"), "Preferences", "@loader_path"),
            (str(frameworks / "ExampleKit.framework/ExampleKit"), "Preferences", "@loader_path/.."),
        ]
        assert fixed == [
            "Example.dylib → bundled Preferences.framework",
            "ExampleKit.framework → bundled Preferences.framework",
        ]

    def test_the_rpath_asked_for_is_the_one_added(self, tmp_path: Path, monkeypatch) -> None:
        ran: list[list[str]] = []
        monkeypatch.setattr(playcover, "install_name_tool", lambda: Path("/fake/install_name_tool"))
        monkeypatch.setattr(playcover, "_run_tool", lambda argv, doing: ran.append(argv))
        binary = _binary(tmp_path / "ExampleKit.framework/ExampleKit", PREFS)

        playcover.retarget_to_bundled_framework(binary, "Preferences", rpath="@loader_path/..")

        assert ["/fake/install_name_tool", "-add_rpath", "@loader_path/..", str(binary)] in ran
