from __future__ import annotations

import datetime
import json
import os
import plistlib
import shutil
import stat
import struct
import zipfile
from pathlib import Path

import pytest

from graft_ipa import ipa_inspect
from graft_ipa.errors import IPAInspectionError
from graft_ipa.ipa_inspect import (
    normalize_exec_bits,
    read_info_plist,
    verify_zip,
)

from . import macho_builder as mb


def _make_ipa(tmp_path: Path, bundle_id: str = "com.example.app", version: str = "1.2.3") -> Path:
    ipa = tmp_path / "fake.ipa"
    info = plistlib.dumps(
        {
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleDisplayName": "Fake",
            "CFBundleExecutable": "Fake",
        }
    )
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    return ipa


def test_read_info_plist(tmp_path: Path) -> None:
    ipa = _make_ipa(tmp_path)
    info = read_info_plist(ipa)
    assert info.bundle_id == "com.example.app"
    assert info.version == "1.2.3"
    assert info.display_name == "Fake"
    assert info.info_plist_path_in_zip.endswith("Info.plist")


def test_a_non_string_executable_is_coerced_to_a_string(tmp_path: Path) -> None:
    """`CFBundleExecutable` has the identical gap `CFBundleDisplayName` and
    `CFBundleIdentifier` did: read with a bare `plist.get(...)`, never
    validated for type. `bundle_id`/`version`/`display_name` were all coerced
    for that reason; this field was not — the finding this pins. Uncoerced,
    a plist `<date>` value (a legal, if unusual, `CFBundleExecutable`) reaches
    `IPAInfoMemo`'s memo entries as a raw `datetime`, which
    `IPAInfoMemo.save()`'s `json.dumps` cannot serialize — see
    `test_ipa_memo.py` and `test_cli_staged_ipas.py` for the crash this
    produces two layers up."""
    ipa = tmp_path / "fake.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": "com.example.app",
        "CFBundleShortVersionString": "1.0",
        "CFBundleDisplayName": "Fake",
        "CFBundleExecutable": datetime.datetime(2020, 1, 1),
    })
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    result = read_info_plist(ipa)
    assert result.executable == "2020-01-01 00:00:00"
    assert isinstance(result.executable, str)


def test_a_falsy_bundle_id_still_raises_even_though_its_string_form_would_not(
    tmp_path: Path,
) -> None:
    """Pins the ordering `read_info_plist`'s own comment on `raw_bundle_id`
    documents but nothing before this test actually exercised: the
    truthiness check has to run on the *raw* plist value, before coercion,
    because `str(0)` is `"0"` — a non-empty, truthy string. A plist with
    `CFBundleIdentifier: 0` is a legal zip and an illegal app; checking the
    coerced form would accept it, and every other test in this file that
    reaches `read_info_plist` uses either a real bundle id or an absent
    `Info.plist` entirely (`test_missing_info_plist`), neither of which can
    tell the two orderings apart. Found by mutation while extracting the
    four fields' coercion into one loop: swapping the guard to check
    `str(raw_bundle_id)` instead of `raw_bundle_id` left the entire suite
    green."""
    ipa = tmp_path / "fake.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": 0,
        "CFBundleShortVersionString": "1.0",
        "CFBundleDisplayName": "Fake",
        "CFBundleExecutable": "Fake",
    })
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("Payload/Fake.app/Info.plist", info)
    with pytest.raises(IPAInspectionError, match="has no CFBundleIdentifier"):
        read_info_plist(ipa)


def test_bad_zip_magic(tmp_path: Path) -> None:
    bad = tmp_path / "not.ipa"
    bad.write_bytes(b"<html>error</html>")
    with pytest.raises(IPAInspectionError):
        verify_zip(bad)


def test_missing_info_plist(tmp_path: Path) -> None:
    ipa = tmp_path / "empty.ipa"
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("README.txt", "nope")
    with pytest.raises(IPAInspectionError):
        read_info_plist(ipa)


# --- stripping extensions that could not be decrypted ---


def _tree(root: Path) -> Path:
    """A minimal extracted-IPA shape: an app, a framework, and two extensions."""
    app = root / "Payload" / "X.app"
    (app / "Frameworks" / "Core.framework").mkdir(parents=True)
    (app / "PlugIns" / "Widget.appex" / "Frameworks" / "Shared.framework").mkdir(parents=True)
    (app / "PlugIns" / "Share.appex").mkdir(parents=True)
    (app / "X").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
    (app / "Frameworks" / "Core.framework" / "Core").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
    (app / "PlugIns" / "Widget.appex" / "Widget").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
    (app / "PlugIns" / "Widget.appex" / "Frameworks" / "Shared.framework" / "Shared").write_bytes(
        b"\xcf\xfa\xed\xfe" + b"\0" * 32
    )
    (app / "PlugIns" / "Share.appex" / "Share").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
    return app


class TestAppexOf:
    def test_an_extension_binary_is_attributed_to_its_appex(self, tmp_path: Path):
        app = _tree(tmp_path)
        found = ipa_inspect.appex_of(app / "PlugIns" / "Share.appex" / "Share", tmp_path)
        assert found == app / "PlugIns" / "Share.appex"

    def test_a_framework_inside_an_extension_belongs_to_that_extension(self, tmp_path: Path):
        """The case a `PlugIns/*.appex/<name>` glob misses. Getting it wrong
        means either stripping a bundle whose own binary was fine, or refusing
        to strip one that is entirely undecryptable."""
        app = _tree(tmp_path)
        nested = app / "PlugIns" / "Widget.appex" / "Frameworks" / "Shared.framework" / "Shared"
        assert ipa_inspect.appex_of(nested, tmp_path) == app / "PlugIns" / "Widget.appex"

    def test_the_apps_own_binary_is_in_no_extension(self, tmp_path: Path):
        app = _tree(tmp_path)
        assert ipa_inspect.appex_of(app / "X", tmp_path) is None

    def test_an_app_framework_is_in_no_extension(self, tmp_path: Path):
        """The safety case: this must never be attributed to an extension, or
        stripping would delete a framework the app needs to launch."""
        app = _tree(tmp_path)
        assert ipa_inspect.appex_of(
            app / "Frameworks" / "Core.framework" / "Core", tmp_path
        ) is None

    def test_it_stops_at_the_extract_root(self, tmp_path: Path):
        """Walking past the root would inspect the caller's temp dir, and a
        `.appex` anywhere above it would produce a nonsense answer."""
        app = _tree(tmp_path)
        assert ipa_inspect.appex_of(app / "X", tmp_path) is None


class TestRepackIPA:
    def test_it_round_trips_through_extract(self, tmp_path: Path):
        src = tmp_path / "src"
        _tree(src)
        ipa = ipa_inspect.repack_ipa(src, tmp_path / "out.ipa")
        back = tmp_path / "back"
        ipa_inspect.extract_ipa(ipa, back)
        original = {p.relative_to(src) for p in src.rglob("*") if p.is_file()}
        restored = {p.relative_to(back) for p in back.rglob("*") if p.is_file()}
        assert original == restored

    def test_a_removed_extension_is_absent_from_the_repack(self, tmp_path: Path):
        """The whole point: what was deleted from the tree must not survive in
        the archive the pipeline then stages and builds."""
        src = tmp_path / "src"
        app = _tree(src)
        shutil.rmtree(app / "PlugIns" / "Share.appex")
        ipa = ipa_inspect.repack_ipa(src, tmp_path / "out.ipa")
        with zipfile.ZipFile(ipa) as zf:
            names = zf.namelist()
        assert not any("Share.appex" in n for n in names)
        assert any("Widget.appex" in n for n in names)
        assert any(n.endswith("Payload/X.app/X") for n in names)


class TestExtensionsToStrip:
    """Which extensions `--strip-encrypted-extensions` is allowed to delete.

    This is the destructive decision in the decrypt path, so the interesting
    assertions are the refusals. Removing an extension costs the built app a
    widget; removing the wrong thing hands back an IPA that signs, installs and
    doesn't run.
    """

    def _res(self, path: Path, decrypted: bool):
        return ipa_inspect.CryptidResult(binary=path, cryptids=[0] if decrypted else [1])

    def test_it_strips_the_extension_a_failed_binary_belongs_to(self, tmp_path: Path):
        from graft_ipa.cli import _extensions_to_strip

        app = _tree(tmp_path)
        left = [self._res(app / "PlugIns" / "Share.appex" / "Share", False)]
        assert _extensions_to_strip(left, tmp_path) == {app / "PlugIns" / "Share.appex"}

    def test_a_nested_framework_takes_its_whole_extension(self, tmp_path: Path):
        """An extension missing only its inner framework is still broken, so the
        bundle goes rather than the one file."""
        from graft_ipa.cli import _extensions_to_strip

        app = _tree(tmp_path)
        nested = app / "PlugIns" / "Widget.appex" / "Frameworks" / "Shared.framework" / "Shared"
        assert _extensions_to_strip([self._res(nested, False)], tmp_path) == {
            app / "PlugIns" / "Widget.appex"
        }

    def test_it_refuses_when_the_apps_own_binary_is_encrypted(self, tmp_path: Path):
        """The safety case. A main binary left encrypted is a failed dump, not
        the extension limit — stripping extensions would produce a signed,
        installable, broken app and hide the real failure."""
        from graft_ipa.cli import _extensions_to_strip

        app = _tree(tmp_path)
        left = [
            self._res(app / "PlugIns" / "Share.appex" / "Share", False),
            self._res(app / "X", False),
        ]
        assert _extensions_to_strip(left, tmp_path) == set()

    def test_it_refuses_when_an_app_framework_is_encrypted(self, tmp_path: Path):
        from graft_ipa.cli import _extensions_to_strip

        app = _tree(tmp_path)
        left = [
            self._res(app / "PlugIns" / "Share.appex" / "Share", False),
            self._res(app / "Frameworks" / "Core.framework" / "Core", False),
        ]
        assert _extensions_to_strip(left, tmp_path) == set()

    def test_several_failed_extensions_all_go(self, tmp_path: Path):
        from graft_ipa.cli import _extensions_to_strip

        app = _tree(tmp_path)
        left = [
            self._res(app / "PlugIns" / "Share.appex" / "Share", False),
            self._res(app / "PlugIns" / "Widget.appex" / "Widget", False),
        ]
        assert _extensions_to_strip(left, tmp_path) == {
            app / "PlugIns" / "Share.appex",
            app / "PlugIns" / "Widget.appex",
        }


class TestRoundTripPreservesModes:
    """The extract/repack pair has to hand back what it was given.

    `--strip-encrypted-extensions` is the only path that rewrites an IPA, and
    the rewritten file is signed and installed. Python's `extractall` does not
    restore permissions — a 0755 entry comes out 0644 — and `ZipFile.write`
    faithfully stores whatever it finds, so the pair silently produced an app
    whose main binary was not executable. It verified cryptid 0, signed
    cleanly, installed, and would not launch.
    """

    def _packed(self, tmp_path: Path, mode: int = 0o755) -> Path:
        src = tmp_path / "src" / "Payload" / "X.app"
        src.mkdir(parents=True)
        binary = src / "X"
        binary.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
        os.chmod(binary, mode)
        (src / "Info.plist").write_bytes(b"plist")
        ipa = tmp_path / "in.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.write(binary, "Payload/X.app/X")
            zf.write(src / "Info.plist", "Payload/X.app/Info.plist")
        return ipa

    def test_extract_restores_the_executable_bit(self, tmp_path: Path):
        ipa = self._packed(tmp_path)
        out = tmp_path / "out"
        ipa_inspect.extract_ipa(ipa, out)
        mode = stat.S_IMODE((out / "Payload/X.app/X").stat().st_mode)
        assert mode & 0o111, f"main binary came out non-executable ({oct(mode)})"

    def test_a_round_trip_keeps_the_binary_executable(self, tmp_path: Path):
        """The end-to-end property. This is what the strip path does."""
        ipa = self._packed(tmp_path)
        out = tmp_path / "out"
        ipa_inspect.extract_ipa(ipa, out)
        repacked = ipa_inspect.repack_ipa(out, tmp_path / "repacked.ipa")
        with zipfile.ZipFile(repacked) as zf:
            mode = (zf.getinfo("Payload/X.app/X").external_attr >> 16) & 0o777
        assert mode & 0o111, f"repacked binary is not executable ({oct(mode)})"

    def test_a_symlink_survives_as_a_symlink(self, tmp_path: Path):
        """`extractall` writes a link as a regular file holding its target, so
        a round trip silently turned links into text files."""
        src = tmp_path / "src" / "Payload" / "X.app"
        src.mkdir(parents=True)
        (src / "real").write_bytes(b"data")
        (src / "link").symlink_to("real")
        packed = ipa_inspect.repack_ipa(tmp_path / "src", tmp_path / "a.ipa")
        out = tmp_path / "out"
        ipa_inspect.extract_ipa(packed, out)
        assert (out / "Payload/X.app/link").is_symlink()
        assert os.readlink(out / "Payload/X.app/link") == "real"

    def test_a_non_executable_file_stays_non_executable(self, tmp_path: Path):
        """Restoring modes must not mean granting them."""
        ipa = self._packed(tmp_path, mode=0o644)
        out = tmp_path / "out"
        ipa_inspect.extract_ipa(ipa, out)
        mode = stat.S_IMODE((out / "Payload/X.app/X").stat().st_mode)
        assert not mode & 0o111


class TestStrippedMarker:
    """A stripped IPA has to be distinguishable from a complete one.

    It is staged under the same canonical `{bundle_id}_{version}.ipa` name, and
    nothing inside differs in a way any consumer inspects — `find_in_cache`
    matches the name and validates the Info.plist, which stripping leaves
    untouched. Without a record beside it, a build days later reuses an app
    with no widgets while reporting an ordinary cache hit.
    """

    def test_a_marked_ipa_reports_what_was_removed(self, tmp_path: Path):
        ipa = tmp_path / "com.x_1.0.ipa"
        ipa.write_bytes(b"PK\x03\x04")
        ipa_inspect.mark_stripped(ipa, ["Payload/X.app/PlugIns/W.appex"])
        assert ipa_inspect.stripped_extensions(ipa) == ["Payload/X.app/PlugIns/W.appex"]

    def test_an_unmarked_ipa_reports_nothing(self, tmp_path: Path):
        ipa = tmp_path / "com.x_1.0.ipa"
        ipa.write_bytes(b"PK\x03\x04")
        assert ipa_inspect.stripped_extensions(ipa) == []

    def test_the_marker_travels_with_the_cache(self, tmp_path: Path):
        """Beside the IPA, named after it — so clearing the cache clears the
        record and there is no orphan state to reconcile."""
        ipa = tmp_path / "com.x_1.0.ipa"
        ipa.write_bytes(b"PK\x03\x04")
        ipa_inspect.mark_stripped(ipa, ["a"])
        assert (tmp_path / ("com.x_1.0.ipa" + ipa_inspect.STRIPPED_SUFFIX)).is_file()

    def test_a_corrupt_marker_does_not_stop_a_build(self, tmp_path: Path):
        """This annotates a build; a bad note must never be able to fail one."""
        ipa = tmp_path / "com.x_1.0.ipa"
        ipa.write_bytes(b"PK\x03\x04")
        (tmp_path / ("com.x_1.0.ipa" + ipa_inspect.STRIPPED_SUFFIX)).write_text("{not json")
        assert ipa_inspect.stripped_extensions(ipa) == []

    @pytest.mark.parametrize("content", ["null", "[]", "3", '"x"'])
    def test_a_marker_that_is_valid_json_but_not_an_object_does_not_stop_a_build(
        self, tmp_path: Path, content: str
    ):
        """Same shape of bug as `stripped_note`'s: `json.loads` accepts all
        four without raising, so `except (OSError, ValueError)` never sees
        them, and the next line — `data.get("removed")` — was an
        AttributeError out of the stage-2 acquire path, on every cache hit."""
        ipa = tmp_path / "com.x_1.0.ipa"
        ipa.write_bytes(b"PK\x03\x04")
        (tmp_path / ("com.x_1.0.ipa" + ipa_inspect.STRIPPED_SUFFIX)).write_text(content)
        assert ipa_inspect.stripped_extensions(ipa) == []


class TestExecutableBits:
    """The device is the authority: an installed app binary is `-rwxr-xr-x`."""

    def _macho(self, path: Path, mode: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        os.chmod(path, mode)
        return path

    def test_the_owner_bit_frida_drops_is_restored(self, tmp_path: Path):
        """frida-ios-dump packages the main binary 0o655 — execute for group and
        other, but not for the owner that runs it."""
        main = self._macho(tmp_path / "Payload/X.app/X", 0o655)
        fixed = normalize_exec_bits(tmp_path)
        assert fixed == [main]
        assert stat.S_IMODE(main.stat().st_mode) == 0o755

    def test_the_bits_extractall_drops_are_restored(self, tmp_path: Path):
        main = self._macho(tmp_path / "Payload/X.app/X", 0o644)
        normalize_exec_bits(tmp_path)
        assert stat.S_IMODE(main.stat().st_mode) == 0o755

    def test_an_already_correct_binary_is_left_alone(self, tmp_path: Path):
        """Reported honestly, so the caller can skip a 70 MB repack it does not
        need."""
        self._macho(tmp_path / "Payload/X.app/X", 0o755)
        assert normalize_exec_bits(tmp_path) == []

    def test_permissions_are_only_ever_added(self, tmp_path: Path):
        """A dump is not the place to decide a file should lose a permission it
        arrived with."""
        main = self._macho(tmp_path / "Payload/X.app/X", 0o764)
        normalize_exec_bits(tmp_path)
        assert stat.S_IMODE(main.stat().st_mode) == 0o775

    def test_frameworks_and_extensions_are_covered_too(self, tmp_path: Path):
        self._macho(tmp_path / "Payload/X.app/X", 0o655)
        self._macho(tmp_path / "Payload/X.app/Frameworks/Y.framework/Y", 0o644)
        self._macho(tmp_path / "Payload/X.app/PlugIns/Z.appex/Z", 0o644)
        assert len(normalize_exec_bits(tmp_path)) == 3

    def test_resources_are_not_made_executable(self, tmp_path: Path):
        """Only Mach-O files. Marking a plist executable would be noise in the
        bundle and noise in the repack decision."""
        plist = tmp_path / "Payload/X.app/Info.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(b"<plist/>")
        os.chmod(plist, 0o644)
        assert normalize_exec_bits(tmp_path) == []
        assert stat.S_IMODE(plist.stat().st_mode) == 0o644


class TestExtensionImportance:
    """Which extensions can be dropped, judged by what they plug into.

    The identifiers here were read off a real device, not remembered — see the
    sweep that produced them.
    """

    def _appex(self, tmp_path: Path, name: str, point: str | None) -> Path:
        appex = tmp_path / f"{name}.appex"
        appex.mkdir(parents=True)
        plist = {"CFBundleIdentifier": f"com.x.{name}"}
        if point is not None:
            plist["NSExtension"] = {"NSExtensionPointIdentifier": point}
        with (appex / "Info.plist").open("wb") as fh:
            plistlib.dump(plist, fh)
        return appex

    def test_a_content_blocker_is_the_app(self, tmp_path: Path):
        """A content blocker. Stripping it left a 0.1 MB IPA reported as decrypted."""
        appex = self._appex(tmp_path, "ContentBlocker", "com.apple.Safari.content-blocker")
        assert ipa_inspect.extension_importance(appex) == "essential"

    def test_a_packet_tunnel_is_the_vpn(self, tmp_path: Path):
        """A VPN app. 0.7 MB of settings screen without it."""
        appex = self._appex(tmp_path, "Tunnel", "com.apple.networkextension.packet-tunnel")
        assert ipa_inspect.extension_importance(appex) == "essential"

    def test_password_autofill_is_a_headline_feature_not_the_app(self, tmp_path: Path):
        """A password manager still shows you your vault; it just cannot fill anything.
        Worth naming loudly, not worth refusing the dump over."""
        appex = self._appex(
            tmp_path, "AutoFill",
            "com.apple.authentication-services-credential-provider-ui",
        )
        assert ipa_inspect.extension_importance(appex) == "major"

    def test_widgets_and_notifications_are_cosmetic(self, tmp_path: Path):
        for point in ("com.apple.widgetkit-extension",
                      "com.apple.usernotifications.service",
                      "com.apple.share-services",
                      "com.apple.usernotifications.content-extension"):
            appex = self._appex(tmp_path / point, "Ext", point)
            assert ipa_inspect.extension_importance(appex) == "cosmetic", point

    def test_an_unreadable_extension_is_treated_as_essential(self, tmp_path: Path):
        """We know nothing about it, and an unknown extension is the wrong one
        to delete on an assumption."""
        appex = tmp_path / "Mystery.appex"
        appex.mkdir()
        assert ipa_inspect.extension_importance(appex) == "essential"

    def test_an_extension_declaring_no_point_is_treated_as_essential(self, tmp_path: Path):
        appex = self._appex(tmp_path, "NoPoint", None)
        assert ipa_inspect.extension_importance(appex) == "essential"


class TestLostCapabilities:
    """What the user is told they lost, in words they will recognize.

    "AutoFillExtension.appex was removed" is a filename. "password
    autofill will not work" is the thing they notice next Tuesday and blame on
    whatever tweak was injected.
    """

    def _appex(self, tmp_path: Path, name: str, point: str) -> Path:
        appex = tmp_path / f"{name}.appex"
        appex.mkdir(parents=True)
        with (appex / "Info.plist").open("wb") as fh:
            plistlib.dump({"NSExtension": {"NSExtensionPointIdentifier": point}}, fh)
        return appex

    def test_autofill_is_named(self, tmp_path: Path):
        appex = self._appex(
            tmp_path, "AutoFill",
            "com.apple.authentication-services-credential-provider-ui",
        )
        assert ipa_inspect.lost_capabilities([appex]) == ["password autofill"]

    def test_a_widget_needs_no_words(self, tmp_path: Path):
        """A missing widget is visibly missing; saying so adds nothing."""
        appex = self._appex(tmp_path, "Widget", "com.apple.widgetkit-extension")
        assert ipa_inspect.lost_capabilities([appex]) == []

    def test_each_capability_is_said_once(self, tmp_path: Path):
        """Apps ship an intents extension and its UI half as two bundles; the
        user has lost Siri once, not twice."""
        a = self._appex(tmp_path / "a", "Intent", "com.apple.intents-service")
        b = self._appex(tmp_path / "b", "IntentUI", "com.apple.intents-service")
        assert ipa_inspect.lost_capabilities([a, b]) == ["Siri and Shortcuts"]

    def test_every_major_extension_point_has_words(self):
        """A tier that means "say this loudly" is useless for an identifier
        with nothing to say."""
        missing = ipa_inspect.MAJOR_EXTENSION_POINTS - set(ipa_inspect.EXTENSION_CAPABILITY)
        assert not missing, f"no plain-language name for {sorted(missing)}"


class TestExtensionKitExtensions:
    """iOS 16 moved where an extension declares what it plugs into.

    App Intents and control extensions carry
    `EXAppExtensionAttributes.EXExtensionPointIdentifier` and no `NSExtension`
    at all. Reading only the classic key made every modern extension look like
    one nothing was known about — and, with unknown treated as essential, that
    refused to dump an app over its App Intents bundle. Taken from the real
    plist on the device.
    """

    def _appex(self, tmp_path: Path, plist: dict) -> Path:
        appex = tmp_path / "Ext.appex"
        appex.mkdir(parents=True)
        with (appex / "Info.plist").open("wb") as fh:
            plistlib.dump(plist, fh)
        return appex

    def test_an_app_intents_extension_is_understood(self, tmp_path: Path):
        appex = self._appex(tmp_path, {
            "EXAppExtensionAttributes": {
                "EXExtensionPointIdentifier": "com.apple.appintents-extension"
            }
        })
        assert ipa_inspect.extension_point(appex) == "com.apple.appintents-extension"
        assert ipa_inspect.extension_importance(appex) == "cosmetic"

    def test_the_classic_key_still_wins_when_both_are_present(self, tmp_path: Path):
        appex = self._appex(tmp_path, {
            "NSExtension": {"NSExtensionPointIdentifier": "com.apple.widgetkit-extension"},
            "EXAppExtensionAttributes": {"EXExtensionPointIdentifier": "com.apple.other"},
        })
        assert ipa_inspect.extension_point(appex) == "com.apple.widgetkit-extension"


class TestNotOverRefusing:
    """The gate exists to stop a gutted app being called a success. Firing it on
    a full app is the same mistake pointing the other way — found by sweeping 26
    apps and watching ordinary apps start refusing.
    """

    def _appex(self, tmp_path: Path, name: str, point: str) -> Path:
        appex = tmp_path / f"{name}.appex"
        appex.mkdir(parents=True)
        with (appex / "Info.plist").open("wb") as fh:
            plistlib.dump({"NSExtension": {"NSExtensionPointIdentifier": point}}, fh)
        return appex

    def test_a_safari_extension_shipped_beside_an_app_is_not_the_app(self, tmp_path: Path):
        """A payments app's extension is a checkout add-on. A *content blocker* is the whole
        product — a different extension point, which is why the two are told
        apart by what they plug into rather than by both being 'Safari'."""
        checkout = self._appex(tmp_path / "p", "SafariExt", "com.apple.Safari.web-extension")
        assert ipa_inspect.extension_importance(checkout) == "major"

        blocker = self._appex(tmp_path / "h", "Blocker", "com.apple.Safari.content-blocker")
        assert ipa_inspect.extension_importance(blocker) == "essential"

    def test_wallet_provisioning_is_a_feature(self, tmp_path: Path):
        appex = self._appex(tmp_path, "Wallet", "com.apple.PassKit.issuer-provisioning")
        assert ipa_inspect.extension_importance(appex) == "major"
        assert ipa_inspect.lost_capabilities([appex]) == ["adding cards to Apple Wallet"]

    def test_a_file_provider_is_a_feature_not_a_verdict(self, tmp_path: Path):
        """Plenty of full apps expose files. Refusing over one would block them
        all to protect a case that may not exist."""
        appex = self._appex(tmp_path, "Files", "com.apple.fileprovider-nonui")
        assert ipa_inspect.extension_importance(appex) == "major"


class TestExtractionStaysInsideItsDirectory:
    """An IPA is an archive from the internet, not a trusted one."""

    def _zip_with(self, path: Path, name: str, *, mode: int, data: bytes) -> Path:
        with zipfile.ZipFile(path, "w") as zf:
            info = zipfile.ZipInfo("Payload/X.app/Info.plist")
            info.external_attr = 0o644 << 16
            zf.writestr(info, plistlib.dumps({
                "CFBundleIdentifier": "com.x", "CFBundleExecutable": "X",
                "CFBundleShortVersionString": "1.0",
            }))
            evil = zipfile.ZipInfo(name)
            evil.create_system = 3
            evil.external_attr = mode << 16
            zf.writestr(evil, data)
        return path

    def test_a_traversing_symlink_entry_does_not_unlink_an_outside_file(
        self, tmp_path: Path
    ):
        """The loop rebuilds paths from the *raw* archive name, which extractall
        has already refused to honour — so this entry named a file outside the
        tree that the loop would have deleted."""
        outside = tmp_path / "precious.txt"
        outside.write_text("do not delete me")
        ipa = self._zip_with(
            tmp_path / "evil.ipa", "../precious.txt",
            mode=stat.S_IFLNK | 0o777, data=b"/etc/passwd",
        )
        ipa_inspect.extract_ipa(ipa, tmp_path / "out")
        assert outside.exists(), "a file outside the extraction directory was removed"
        assert outside.read_text() == "do not delete me"

    def test_a_traversing_entry_does_not_raise_a_bare_oserror(self, tmp_path: Path):
        """The milder shape of the same bug: the path does not exist, and only
        BadZipFile is caught here, so the CLI showed a traceback."""
        ipa = self._zip_with(
            tmp_path / "evil2.ipa", "../../nowhere/x",
            mode=stat.S_IFLNK | 0o777, data=b"target",
        )
        ipa_inspect.extract_ipa(ipa, tmp_path / "out2")  # must not raise


class TestSymlinksAreNotWalkedTwice:
    """`extract_ipa` materialises real symlinks now, and `is_file()` follows
    them."""

    def test_a_linked_binary_is_counted_once(self, tmp_path: Path):
        real = tmp_path / "Payload/X.app/Frameworks/Y.framework/Versions/A/Y"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        main = tmp_path / "Payload/X.app/X"
        main.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        link = tmp_path / "Payload/X.app/Frameworks/Y.framework/Y"
        link.symlink_to("Versions/A/Y")

        found = ipa_inspect.list_macho_in_extract(tmp_path)
        assert sorted(p.name for p in found) == ["X", "Y"]
        assert not any(p.is_symlink() for p in found), (
            "a symlink in the list means otool runs twice on one file and the "
            "reported binary count is inflated"
        )

    def test_the_exec_bit_is_not_set_through_a_link(self, tmp_path: Path):
        real = tmp_path / "Payload/X.app/Frameworks/Y.framework/Versions/A/Y"
        real.parent.mkdir(parents=True)
        real.write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
        os.chmod(real, 0o644)
        link = tmp_path / "Payload/X.app/Frameworks/Y.framework/Y"
        link.symlink_to("Versions/A/Y")

        fixed = ipa_inspect.normalize_exec_bits(tmp_path)
        assert [p.name for p in fixed] == ["Y"] and not fixed[0].is_symlink()
        assert stat.S_IMODE(real.stat().st_mode) == 0o755


def _fat64_with_cryptid(cryptid: int) -> bytes:
    """A minimal fat64 Mach-O: one `fat_arch_64` pointing at a thin arm64
    slice whose only load command is `LC_ENCRYPTION_INFO_64`."""
    cpu_arm64 = 0x0100000C
    # mach_header_64: magic, cputype, cpusubtype, filetype (MH_EXECUTE),
    # ncmds, sizeofcmds, flags, reserved.
    thin = struct.pack("<8I", 0xFEEDFACF, cpu_arm64, 0, 2, 1, 24, 0, 0)
    # encryption_info_command_64: cmd, cmdsize, cryptoff, cryptsize, cryptid, pad.
    thin += struct.pack("<6I", 0x2C, 24, 0, 0, cryptid, 0)
    offset = 0x1000
    # fat_header + fat_arch_64 (cputype, cpusubtype, offset, size, align, reserved).
    fat = struct.pack(">2I", 0xCAFEBABF, 1)
    fat += struct.pack(">iiQQII", cpu_arm64, 0, offset, len(thin), 12, 0)
    return fat.ljust(offset, b"\x00") + thin


class TestFat64BinariesAreChecked:
    """A fat64 binary (`cafebabf`) was missing from the magic set, so an
    encrypted one was never handed to the cryptid check — and stage 2.5 then
    called the IPA decrypted."""

    def test_a_fat64_binary_is_listed(self, tmp_path: Path):
        binary = tmp_path / "Payload/Example.app/Example"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(_fat64_with_cryptid(1))

        found = ipa_inspect.list_macho_in_extract(tmp_path)
        assert found == [binary]

    def test_an_encrypted_fat64_binary_is_reported_encrypted(self, tmp_path: Path):
        binary = tmp_path / "Payload/Example.app/Example"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(_fat64_with_cryptid(1))

        results = ipa_inspect.verify_decrypted(tmp_path)
        assert [r.binary for r in results] == [binary]
        assert results[0].cryptids == [1]
        assert not results[0].is_decrypted


# --- what the sidecar records, and what it deliberately does not claim ---


class TestTheStrippedSidecar:
    """`mark_stripped` / `stripped_note`.

    The distinction the whole class exists for: a sidecar written before this
    field existed knows *nothing* about lost capabilities, and that must not
    read as "nothing was lost". Both sidecars in the real cache today are that
    shape.
    """

    def _ipa(self, tmp_path: Path) -> Path:
        ipa = tmp_path / "com.example.app_1.0.ipa"
        ipa.write_bytes(b"not really a zip")
        return ipa

    def test_it_records_the_capabilities_and_the_importance(self, tmp_path: Path) -> None:
        ipa = self._ipa(tmp_path)
        ipa_inspect.mark_stripped(
            ipa,
            ["Payload/X.app/PlugIns/Intents.appex"],
            lost=["Siri and Shortcuts"],
            importance={"Payload/X.app/PlugIns/Intents.appex": "major"},
        )
        note = ipa_inspect.stripped_note(ipa)
        assert note is not None
        assert note.removed == ["Payload/X.app/PlugIns/Intents.appex"]
        assert note.lost == ["Siri and Shortcuts"]
        assert note.importance == {"Payload/X.app/PlugIns/Intents.appex": "major"}

    def test_an_empty_lost_list_is_recorded_rather_than_omitted(self, tmp_path: Path) -> None:
        """Widgets and share extensions have no name worth saying, so a dump
        that removed only those records `lost: []`. That is an answer."""
        ipa = self._ipa(tmp_path)
        ipa_inspect.mark_stripped(ipa, ["Payload/X.app/PlugIns/Widget.appex"], lost=[])
        note = ipa_inspect.stripped_note(ipa)
        assert note is not None
        assert note.lost == []

    def test_an_old_sidecar_reads_as_unrecorded_not_as_nothing_lost(
        self, tmp_path: Path
    ) -> None:
        """The one that matters. `None` and `[]` must not collapse."""
        ipa = self._ipa(tmp_path)
        (tmp_path / (ipa.name + ipa_inspect.STRIPPED_SUFFIX)).write_text(
            '{"removed": ["Payload/X.app/PlugIns/Widget.appex"]}'
        )
        note = ipa_inspect.stripped_note(ipa)
        assert note is not None
        assert note.removed == ["Payload/X.app/PlugIns/Widget.appex"]
        assert note.lost is None
        assert note.importance == {}

    def test_no_sidecar_is_no_note(self, tmp_path: Path) -> None:
        assert ipa_inspect.stripped_note(self._ipa(tmp_path)) is None

    def test_an_unparseable_sidecar_is_no_note(self, tmp_path: Path) -> None:
        """Same reading `stripped_extensions` gives it, so the add flow and the
        build warning can never disagree about one file."""
        ipa = self._ipa(tmp_path)
        (tmp_path / (ipa.name + ipa_inspect.STRIPPED_SUFFIX)).write_text("{ truncated")
        assert ipa_inspect.stripped_note(ipa) is None

    @pytest.mark.parametrize("content", ["null", "[]", "3", '"x"'])
    def test_a_sidecar_that_is_valid_json_but_not_an_object_is_no_note(
        self, tmp_path: Path, content: str
    ) -> None:
        """`json.loads` accepts all four, so `except (OSError, ValueError)`
        does not catch them, and the next line — `data.get("removed")` — would
        raise `AttributeError` on anything but a dict. Task 3 calls
        `stripped_note` in a loop over every file in the IPA cache with no
        error channel back to the GUI store that consumes it, so one such
        file must degrade this to None like any other unreadable sidecar,
        not abort the whole listing."""
        ipa = self._ipa(tmp_path)
        (tmp_path / (ipa.name + ipa_inspect.STRIPPED_SUFFIX)).write_text(content)
        assert ipa_inspect.stripped_note(ipa) is None

    def test_the_on_disk_shape_is_pinned(self, tmp_path: Path) -> None:
        """Every test above with `lost` present round-trips through
        `mark_stripped`, so renaming a key on disk would pass the whole suite
        while orphaning every sidecar an older build already wrote. This is
        the mirror of the old-shape test above (which feeds JSON by hand
        instead of going through `mark_stripped`), pinned on the new shape
        instead of the old one — apt for a change whose whole subject is
        sidecar vintage."""
        ipa = self._ipa(tmp_path)
        (tmp_path / (ipa.name + ipa_inspect.STRIPPED_SUFFIX)).write_text(
            '{"removed": ["a.appex"], "lost": ["Siri and Shortcuts"], '
            '"importance": {"a.appex": "major"}}'
        )
        note = ipa_inspect.stripped_note(ipa)
        assert note is not None
        assert note.removed == ["a.appex"]
        assert note.lost == ["Siri and Shortcuts"]
        assert note.importance == {"a.appex": "major"}

    def test_an_absent_importance_writes_no_key_at_all(self, tmp_path: Path) -> None:
        """`stripped_note` reads an absent `importance` key and a
        present-but-empty one as the same `{}` (that collapse is deliberate —
        see `StrippedNote`'s docstring), so no assertion made against a
        `StrippedNote` can tell `mark_stripped`'s `if importance:` apart from
        a mutant that always writes `importance or {}`. Reading the sidecar's
        own JSON is the only way to pin what actually reaches disk when no
        importance was computed."""
        ipa = self._ipa(tmp_path)
        ipa_inspect.mark_stripped(ipa, ["a.appex"], lost=[])
        raw = json.loads(
            (tmp_path / (ipa.name + ipa_inspect.STRIPPED_SUFFIX)).read_text()
        )
        assert "importance" not in raw

    def test_stripped_extensions_still_answers_in_paths(self, tmp_path: Path) -> None:
        """The guard on the build path not moving: its two callers read paths
        and must keep reading paths, including from an enriched sidecar."""
        ipa = self._ipa(tmp_path)
        ipa_inspect.mark_stripped(
            ipa, ["Payload/X.app/PlugIns/Intents.appex"], lost=["Siri and Shortcuts"]
        )
        assert ipa_inspect.stripped_extensions(ipa) == [
            "Payload/X.app/PlugIns/Intents.appex"
        ]

    def test_clear_stripped_removes_the_enriched_note_too(self, tmp_path: Path) -> None:
        ipa = self._ipa(tmp_path)
        ipa_inspect.mark_stripped(ipa, ["a.appex"], lost=[], importance={"a.appex": "cosmetic"})
        ipa_inspect.clear_stripped(ipa)
        assert ipa_inspect.stripped_note(ipa) is None


def test_check_cryptid_reads_the_binary_itself(tmp_path, monkeypatch):
    """No otool, no child of any kind: on a Mac without the Command Line
    Tools, /usr/bin/otool is a shim that offers to install them, once per
    binary. The guard used to set `ipa_inspect.subprocess`, which the module
    doesn't import, so nothing tripped it (review #30)."""
    from .tripwires import forbid_child_processes

    p = tmp_path / "Example"
    p.write_bytes(mb.thin([mb.encryption_cmd(1)]))
    app = tmp_path / "Payload" / "Example.app"
    app.mkdir(parents=True)
    (app / "Example").write_bytes(mb.thin([mb.encryption_cmd(1)]))
    forbid_child_processes(monkeypatch, "the decryption check")
    result = ipa_inspect.check_cryptid(p)
    assert result.cryptids == [1]
    assert result.is_decrypted is False
    assert [r.cryptids for r in ipa_inspect.verify_decrypted(tmp_path)] == [[1]]


def test_an_unreadable_binary_is_an_inspection_error_not_decrypted(tmp_path):
    p = tmp_path / "Example"
    p.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 8)  # a 64-bit magic, then truncated
    with pytest.raises(IPAInspectionError, match="couldn't read Example's load commands"):
        ipa_inspect.check_cryptid(p)


JAVA_CLASS = b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64  # cafebabe, then version 52
SWAPPED_FAT = b"\xbe\xba\xfe\xca\x02\x00\x00\x00" + b"\0" * 64


class TestFilesThatOnlyLookLikeAMachO:
    """`list_macho_in_extract` picks files by their first four bytes, and a
    Java `.class` file starts `cafebabe` like a fat binary. otool printed no
    cryptid for one and the build went on; the reader raised on it and failed
    stage 2.5 instead. A file that is no Mach-O at all is skipped now, while a
    real Mach-O the reader can't parse still fails."""

    def _extracted(self, tmp_path: Path, extra: dict[str, bytes]) -> Path:
        ipa = tmp_path / "example.ipa"
        with zipfile.ZipFile(ipa, "w") as zf:
            zf.writestr("Payload/Example.app/Example", mb.thin([mb.encryption_cmd(0)]))
            for name, data in extra.items():
                zf.writestr(f"Payload/Example.app/{name}", data)
        return ipa_inspect.extract_ipa(ipa, tmp_path / "verify")

    def test_a_java_class_file_is_skipped_and_not_counted(self, tmp_path: Path):
        extract = self._extracted(tmp_path, {"Some.class": JAVA_CLASS})
        assert "Some.class" in [p.name for p in ipa_inspect.list_macho_in_extract(extract)]

        results = ipa_inspect.verify_decrypted(extract)

        assert [r.binary.name for r in results] == ["Example"]
        assert all(r.is_decrypted for r in results)

    def test_a_byte_swapped_fat_magic_is_skipped(self, tmp_path: Path):
        extract = self._extracted(tmp_path, {"Resource.bin": SWAPPED_FAT})
        assert "Resource.bin" in [p.name for p in ipa_inspect.list_macho_in_extract(extract)]

        results = ipa_inspect.verify_decrypted(extract)

        assert [r.binary.name for r in results] == ["Example"]

    def test_a_real_mach_o_with_truncated_load_commands_still_fails(self, tmp_path: Path):
        broken = mb.thin([mb.encryption_cmd(1)])[:-4]
        extract = self._extracted(tmp_path, {
            "Some.class": JAVA_CLASS, "Frameworks/Y.framework/Y": broken,
        })

        with pytest.raises(IPAInspectionError, match="couldn't read Y's load commands"):
            ipa_inspect.verify_decrypted(extract)

    def test_check_cryptid_on_a_class_file_still_raises(self, tmp_path: Path):
        """Called directly, it has no "skip": `appstore.main_binary_encrypted`
        decides for itself what an unreadable main executable means."""
        p = tmp_path / "Some.class"
        p.write_bytes(JAVA_CLASS)
        with pytest.raises(IPAInspectionError, match=r"couldn't read Some\.class's load commands"):
            ipa_inspect.check_cryptid(p)


def test_a_universal_static_library_in_the_bundle_is_skipped_not_fatal(tmp_path):
    """A pod that copies its `lipo`-made `.a` into a resource bundle (review
    #18): the check used to refuse the whole IPA over it, where 1.1's otool
    read it as no binary. The app's own binary is still checked."""
    from .test_macho import AR

    app = tmp_path / "Payload" / "Example.app"
    (app / "SDK.bundle").mkdir(parents=True)
    (app / "Example").write_bytes(mb.thin([mb.encryption_cmd(1)]))
    (app / "SDK.bundle" / "libsdk.a").write_bytes(mb.fat([(mb.CPU_ARM64, AR), (mb.CPU_X86_64, AR)]))
    results = ipa_inspect.verify_decrypted(tmp_path)
    assert [(r.binary.name, r.cryptids) for r in results] == [("Example", [1])]


# --- a binary that could not be read was never checked (review #17) -----------------


def _info_plist(executable: str) -> bytes:
    return plistlib.dumps({"CFBundleExecutable": executable, "CFBundleIdentifier": "com.example.app"})


def test_a_binary_stored_without_owner_read_is_still_checked(tmp_path):
    """The decimal-for-octal slip, `S_IFREG | 755` (= 0o1363, owner -wx),
    came out of extraction unreadable, was passed over, and the IPA read as
    "0 Mach-O binaries, all cryptid 0" with its app still encrypted."""
    ipa = tmp_path / "slip.ipa"
    with zipfile.ZipFile(ipa, "w") as zf:
        for name, data in [
            ("Payload/Example.app/Info.plist", _info_plist("Example")),
            ("Payload/Example.app/Example", mb.thin([mb.encryption_cmd(1)])),
            ("Payload/Example.app/Frameworks/Kit.framework/Kit", mb.thin([mb.encryption_cmd(1)])),
        ]:
            entry = zipfile.ZipInfo(name)
            entry.external_attr = (stat.S_IFREG | 755) << 16  # decimal, by mistake
            zf.writestr(entry, data)
    extract = ipa_inspect.extract_ipa(ipa, tmp_path / "x")
    results = ipa_inspect.verify_decrypted(extract)
    assert sorted((r.binary.name, r.cryptids) for r in results) == [("Example", [1]), ("Kit", [1])]


def test_a_binary_that_cannot_be_read_fails_the_check_by_name(tmp_path):
    app = tmp_path / "Payload" / "Example.app"
    kit = app / "Frameworks" / "Kit.framework" / "Kit"
    kit.parent.mkdir(parents=True)
    (app / "Example").write_bytes(mb.thin([mb.encryption_cmd(0)]))
    kit.write_bytes(mb.thin([mb.encryption_cmd(1)]))
    kit.chmod(0o200)
    try:
        with pytest.raises(IPAInspectionError, match="Kit"):
            ipa_inspect.verify_decrypted(tmp_path)
    finally:
        kit.chmod(0o644)


def test_the_main_executable_is_checked_whatever_its_name(tmp_path):
    """A `CFBundleExecutable` with a resource suffix (`Foo.js`) was passed over
    by the listing's name rule."""
    app = tmp_path / "Payload" / "Foo.app"
    app.mkdir(parents=True)
    (app / "Info.plist").write_bytes(_info_plist("Foo.js"))
    (app / "Foo.js").write_bytes(mb.thin([mb.encryption_cmd(1)]))
    (app / "bundle.js").write_text("console.log('a resource, not the binary')")
    results = ipa_inspect.verify_decrypted(tmp_path)
    assert [(r.binary.name, r.cryptids) for r in results] == [("Foo.js", [1])]


# --- every bundle's executable is checked, and none drops out (re-check #9/#10) ---


def _bundle(path: Path, executable: str, binary: bytes | None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "Info.plist").write_bytes(_info_plist(executable))
    if binary is not None:
        (path / executable).write_bytes(binary)


@pytest.mark.parametrize(
    "bundle",
    [
        "PlugIns/Widget.appex",
        "Frameworks/Kit.framework",
        "Watch/Watch.app",
        "Watch/Watch.app/PlugIns/WatchExt.appex",
    ],
)
def test_a_nested_bundle_s_executable_is_checked_whatever_its_name(tmp_path, bundle):
    """Re-check #9: only `Payload/*.app`'s executable was exempt from the
    resource-suffix rule, so an extension, framework or Watch app whose
    binary is named `*.js` and still at cryptid 1 read as decrypted."""
    app = tmp_path / "Payload" / "App.app"
    _bundle(app, "App", mb.thin([mb.encryption_cmd(0)]))
    nested = app / bundle
    _bundle(nested, "Code.js", mb.thin([mb.encryption_cmd(1)]))

    results = ipa_inspect.verify_decrypted(tmp_path)

    by_path = {r.binary.relative_to(tmp_path).as_posix(): r.cryptids for r in results}
    assert by_path[f"Payload/App.app/{bundle}/Code.js"] == [1]
    assert by_path["Payload/App.app/App"] == [0]


def test_a_main_executable_the_reader_cannot_take_is_an_error_not_decrypted(tmp_path):
    """Re-check #10: 17 encrypted arm64 slices is more than the reader takes
    (`MAX_FAT_ARCHS`), so it was NotMachO and dropped: `[]`, which read as
    "0 binaries, cryptid 0", while otool reads cryptid 1."""
    slice_ = mb.thin([mb.encryption_cmd(1)])
    app = tmp_path / "Payload" / "App.app"
    _bundle(app, "App", mb.fat([(0x0100000C, slice_)] * 17))

    with pytest.raises(IPAInspectionError, match=r"App\.app.*App"):
        ipa_inspect.verify_decrypted(tmp_path)


def test_an_extension_executable_that_is_no_mach_o_is_an_error(tmp_path):
    app = tmp_path / "Payload" / "App.app"
    _bundle(app, "App", mb.thin([mb.encryption_cmd(0)]))
    _bundle(app / "PlugIns" / "Widget.appex", "Widget", b"\xca\xfe\xba\xbe" + b"\0" * 60)

    with pytest.raises(IPAInspectionError, match="Widget"):
        ipa_inspect.verify_decrypted(tmp_path)


@pytest.mark.parametrize("bundle", ["", "PlugIns/Widget.appex/"])
def test_a_declared_executable_that_is_missing_is_an_error(tmp_path, bundle):
    app = tmp_path / "Payload" / "App.app"
    _bundle(app, "App", mb.thin([mb.encryption_cmd(0)]))
    if bundle:
        _bundle(app / bundle, "Widget", None)
    else:
        (app / "App").unlink()

    with pytest.raises(IPAInspectionError, match="no such file"):
        ipa_inspect.verify_decrypted(tmp_path)


def test_an_executable_that_is_a_link_is_checked_at_its_target(tmp_path):
    """The second route #10 named: `App -> App.js` in the same bundle. The
    listing skips the link, and the suffix rule skipped the target."""
    app = tmp_path / "Payload" / "App.app"
    _bundle(app, "App", None)
    (app / "App.js").write_bytes(mb.thin([mb.encryption_cmd(1)]))
    (app / "App").symlink_to("App.js")

    results = ipa_inspect.verify_decrypted(tmp_path)

    assert [(r.binary.name, r.cryptids) for r in results] == [("App.js", [1])]


def test_an_executable_linked_out_of_the_bundle_is_an_error(tmp_path):
    outside = tmp_path / "elsewhere"
    outside.write_bytes(mb.thin([mb.encryption_cmd(0)]))
    app = tmp_path / "x" / "Payload" / "App.app"
    _bundle(app, "App", None)
    (app / "App").symlink_to(outside)

    with pytest.raises(IPAInspectionError, match="outside"):
        ipa_inspect.verify_decrypted(tmp_path / "x")
