"""Capabilities an app's binaries ask for that its signing profile can't grant.

From the device test of the release build: a sideloaded app signed with a
wildcard profile died at launch with SIGTRAP. Its binary asks for
`com.apple.security.application-groups`, a wildcard profile grants no app
group, and zsign signs the profile's entitlements, so the app found no
group container. Nothing warned. Every IPA and profile here is built at
test time.
"""

from __future__ import annotations

import plistlib
import zipfile
from pathlib import Path

import pytest

from graft_ipa import capabilities

from . import macho_builder as mb
from .signing_fixtures import make_identity

GROUP = "group.com.example.shared"
APP_GROUPS = {"com.apple.security.application-groups": [GROUP]}
#: What every App Store binary asks for, and zsign rewrites from the profile.
REWRITTEN = {
    "application-identifier": "VENDOR1234.com.example.app",
    "keychain-access-groups": ["VENDOR1234.com.example.app"],
    "com.apple.developer.team-identifier": "VENDOR1234",
}


def names(gaps) -> list[str]:
    return [g.name for g in gaps]


def _binary(ents: dict | None) -> bytes:
    if ents is None:
        return mb.thin([mb.encryption_cmd(0)])
    return mb.signed(
        [mb.encryption_cmd(0)],
        mb.entitlements_signature(plistlib.dumps(ents, fmt=plistlib.FMT_XML)),
    )


def make_ipa(
    path: Path,
    main: dict | None,
    *,
    appexes: dict[str, dict | None] | None = None,
    main_binary: bytes | None = None,
    watch_appexes: dict[str, dict | None] | None = None,
) -> Path:
    """`Payload/Example.app` whose executable asks for `main` (None: an
    unsigned one), with one `PlugIns/<name>.appex` per entry of `appexes`,
    and one `Watch/ExampleWatch.app/PlugIns/<name>.appex` per entry of
    `watch_appexes`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        app = "Payload/Example.app"
        z.writestr(f"{app}/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": "com.example.app", "CFBundleExecutable": "Example"}
        ))
        z.writestr(f"{app}/Example", main_binary if main_binary is not None else _binary(main))
        for name, ents in (appexes or {}).items():
            appex = f"{app}/PlugIns/{name}.appex"
            z.writestr(f"{appex}/Info.plist", plistlib.dumps(
                {"CFBundleIdentifier": f"com.example.app.{name}", "CFBundleExecutable": name}
            ))
            z.writestr(f"{appex}/{name}", _binary(ents))
        for name, ents in (watch_appexes or {}).items():
            watch = f"{app}/Watch/ExampleWatch.app"
            z.writestr(f"{watch}/Info.plist", plistlib.dumps(
                {"CFBundleIdentifier": "com.example.app.watchkitapp", "CFBundleExecutable": "ExampleWatch"}
            ))
            appex = f"{watch}/PlugIns/{name}.appex"
            z.writestr(f"{appex}/Info.plist", plistlib.dumps(
                {"CFBundleIdentifier": f"com.example.app.watchkitapp.{name}", "CFBundleExecutable": name}
            ))
            z.writestr(f"{appex}/{name}", _binary(ents))
    return path


def wildcard_profile(tmp_path: Path) -> Path:
    """The fixture profile: `application-identifier` `<team>.*` and nothing else."""
    return make_identity(tmp_path / "wild").profile


def profile_granting(tmp_path: Path, entitlements: dict) -> Path:
    profile = wildcard_profile(tmp_path)
    plist = plistlib.loads(profile.read_bytes())
    plist["Entitlements"].update(entitlements)
    profile.write_bytes(plistlib.dumps(plist))
    return profile


def test_a_wildcard_profile_lacks_the_app_groups_the_app_asks_for(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, **APP_GROUPS})
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == ["App Groups"]


def test_a_profile_that_grants_app_groups_lacks_nothing(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, **APP_GROUPS})
    profile = profile_granting(tmp_path, {"com.apple.security.application-groups": [GROUP]})
    assert names(capabilities.missing_for(ipa, profile)) == []


def test_what_zsign_rewrites_from_the_profile_is_never_flagged(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", REWRITTEN)
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == []


def test_an_extension_that_asks_is_flagged_too(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", REWRITTEN, appexes={
        "Widget": {**REWRITTEN, **APP_GROUPS},
        "Notify": {"aps-environment": "production"},
    })
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == [
        "App Groups", "Push Notifications",
    ]


@pytest.mark.parametrize(
    ("asked", "name"),
    [
        ({"aps-environment": "production"}, "Push Notifications"),
        ({"com.apple.developer.icloud-container-identifiers": ["iCloud.com.example"]}, "iCloud"),
        ({"com.apple.developer.ubiquity-kvstore-identifier": "VENDOR1234.com.example"}, "iCloud"),
        ({"com.apple.developer.associated-domains": ["applinks:example.com"]}, "Associated Domains"),
        ({"com.apple.developer.healthkit": True}, "HealthKit"),
        ({"com.apple.developer.applesignin": ["Default"]}, "Sign in with Apple"),
    ],
)
def test_each_capability_a_wildcard_profile_cannot_have(
    tmp_path: Path, asked: dict, name: str
) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, **asked})
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == [name]


def test_icloud_is_granted_by_any_of_its_keys(tmp_path: Path) -> None:
    """A profile with iCloud lists some of its keys; the app may ask for
    others (`ubiquity-kvstore-identifier`). The capability is there."""
    ipa = make_ipa(tmp_path / "x.ipa", {
        "com.apple.developer.ubiquity-kvstore-identifier": "VENDOR1234.com.example",
    })
    profile = profile_granting(tmp_path, {
        "com.apple.developer.icloud-container-identifiers": ["iCloud.com.example"],
    })
    assert names(capabilities.missing_for(ipa, profile)) == []


def test_asking_for_an_empty_list_is_not_asking(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", {"com.apple.security.application-groups": []})
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == []


def test_an_unsigned_binary_asks_for_nothing(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", None, appexes={"Widget": None})
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == []


@pytest.mark.parametrize(
    "binary",
    [
        pytest.param(mb.signed([], mb.entitlements_signature(b"<plist><dict><key>")),
                     id="malformed-plist"),
        pytest.param(mb.signed([], b"\0" * 64, datasize=1 << 30), id="signature-past-eof"),
        pytest.param(mb.signed([], mb.entitlements_signature(
            b"<plist><dict><key>com.apple.security.application-groups</key><string>"
            + b"x" * (2 * 1024 * 1024) + b"</string></dict></plist>")), id="oversized"),
        pytest.param(b"#!/bin/sh\n", id="not-a-macho"),
        pytest.param(b"", id="empty"),
    ],
)
def test_a_binary_that_cannot_be_read_is_no_warning_and_no_crash(
    tmp_path: Path, binary: bytes
) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", None, main_binary=binary)
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == []


def test_one_unreadable_bundle_does_not_hide_another(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", None, main_binary=b"junk",
                   appexes={"Widget": APP_GROUPS})
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == ["App Groups"]


@pytest.mark.parametrize("what", ["not-a-zip", "missing", "no-app"])
def test_an_ipa_that_cannot_be_read_is_no_warning(tmp_path: Path, what: str) -> None:
    ipa = tmp_path / "x.ipa"
    if what == "not-a-zip":
        ipa.write_bytes(b"not a zip")
    elif what == "no-app":
        with zipfile.ZipFile(ipa, "w") as z:
            z.writestr("Payload/readme.txt", "hi")
    assert names(capabilities.missing_for(ipa, wildcard_profile(tmp_path))) == []


@pytest.mark.parametrize("what", ["missing", "garbage"])
def test_a_profile_that_cannot_be_read_is_no_warning(tmp_path: Path, what: str) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", APP_GROUPS)
    profile = tmp_path / "p.mobileprovision"
    if what == "garbage":
        profile.write_bytes(b"\x30\x80 not a profile")
    assert names(capabilities.missing_for(ipa, profile)) == []


# ---- group by group ----


def _groups(tmp_path: Path, asked: list[str], granted) -> list:
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, "com.apple.security.application-groups": asked})
    profile = profile_granting(tmp_path, {"com.apple.security.application-groups": granted})
    return capabilities.missing_for(ipa, profile)


def test_a_profile_granting_another_group_still_lacks_the_apps_own(tmp_path: Path) -> None:
    """Combined fix check #28: the profile has App Groups, but not the app's
    group, so the app still gets no container for it."""
    assert _groups(tmp_path, [GROUP], ["group.someone.else"]) == [
        capabilities.Gap("App Groups", (GROUP,)),
    ]


def test_only_the_groups_the_profile_lacks_are_named(tmp_path: Path) -> None:
    gaps = _groups(tmp_path, [GROUP, "group.com.example.two"], [GROUP])
    assert gaps == [capabilities.Gap("App Groups", ("group.com.example.two",))]


@pytest.mark.parametrize("granted", [["group.com.example.*"], ["*"], [GROUP], GROUP],
                         ids=["prefix-wildcard", "star", "exact", "a-string"])
def test_a_profile_value_covers_a_group_as_apple_matches_it(tmp_path: Path, granted) -> None:
    assert _groups(tmp_path, [GROUP], granted) == []


@pytest.mark.parametrize("granted", [["group.com.*.shared"], ["group.com.example.shared.*"],
                                     ["group.com.example.share"], [7]],
                         ids=["star-inside", "longer-prefix", "shorter", "not-a-string"])
def test_a_value_that_does_not_cover_the_group_is_no_grant(tmp_path: Path, granted) -> None:
    assert names(_groups(tmp_path, [GROUP], granted)) == ["App Groups"]


def test_extensions_cyan_removes_are_not_asked_about(tmp_path: Path) -> None:
    """A build with strip_extensions: cyan -e removes PlugIns, so a widget's
    App Groups ask never reaches the device."""
    ipa = make_ipa(tmp_path / "x.ipa", REWRITTEN, appexes={"Widget": {**REWRITTEN, **APP_GROUPS}})
    profile = wildcard_profile(tmp_path)
    assert names(capabilities.missing_for(ipa, profile)) == ["App Groups"]
    assert capabilities.missing_for(ipa, profile, extensions=False) == []


# ---- never raises ----


def _lzma_ipa(path: Path) -> Path:
    """The executable stored ZIP_LZMA, with its LZMA properties corrupted:
    zipfile's own reader raises LZMAError opening it (combined fix check #26)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Example.app/Info.plist", plistlib.dumps(
            {"CFBundleIdentifier": "com.example.app", "CFBundleExecutable": "Example"}))
        z.writestr("Payload/Example.app/Example", _binary({**REWRITTEN, **APP_GROUPS}),
                   compress_type=zipfile.ZIP_LZMA)
    data = bytearray(path.read_bytes())
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("Payload/Example.app/Example")
    name_len, extra_len = int.from_bytes(data[info.header_offset + 26:info.header_offset + 28], "little"), \
        int.from_bytes(data[info.header_offset + 28:info.header_offset + 30], "little")
    body = info.header_offset + 30 + name_len + extra_len
    data[body + 4:body + 10] = b"\xff" * 6
    path.write_bytes(bytes(data))
    return path


def test_a_corrupt_lzma_member_is_no_warning_and_no_raise(tmp_path: Path) -> None:
    import lzma

    ipa = _lzma_ipa(tmp_path / "x.ipa")
    with zipfile.ZipFile(ipa) as z, pytest.raises(lzma.LZMAError):
        z.read("Payload/Example.app/Example")  # premise: reading it raises
    assert capabilities.missing_for(ipa, wildcard_profile(tmp_path)) == []


def test_any_failure_inside_the_check_is_skipped_with_a_debug_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", APP_GROUPS)

    def boom(*_a, **_k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(capabilities, "_requested_by_device", boom)
    with caplog.at_level("DEBUG", logger="graft_ipa.capabilities"):
        assert capabilities.missing_for(ipa, wildcard_profile(tmp_path)) == []
    assert "capability check skipped" in caplog.text and "UnicodeDecodeError" in caplog.text


def test_a_profile_with_a_malformed_date_is_no_warning(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", APP_GROUPS)
    profile = wildcard_profile(tmp_path)
    profile.write_bytes(profile.read_bytes().replace(
        b"<key>ExpirationDate</key>", b"<key>ExpirationDate</key><date>soon</date><key>Was</key>", 1))
    assert capabilities.missing_for(ipa, profile) == []


# ---- the words ----


def _warnings(missing: list) -> list[str]:
    return capabilities.warnings("Example", "example", [
        g if isinstance(g, capabilities.Gap) else capabilities.Gap(g) for g in missing])


def test_a_feature_gap_is_one_line_that_says_the_features_wont_work() -> None:
    """Combined fix check #29: push is asked for by nearly every App Store
    app, and no wildcard profile grants it; it is no reason to expect a crash."""
    assert _warnings(["Push Notifications", "Sign in with Apple"]) == [
        "Example asks for Push Notifications and Sign in with Apple, which the profile doesn't "
        "grant; those features won't work."
    ]


@pytest.mark.parametrize("name", ["Push Notifications", "Associated Domains", "HealthKit",
                                  "Sign in with Apple"])
def test_each_feature_gap_never_says_crash(name: str) -> None:
    (line,) = _warnings([name])
    assert "crash" not in line and line.endswith("those features won't work.")


def test_icloud_alone_will_probably_crash_with_no_livecontainer_advice() -> None:
    assert _warnings(["iCloud"]) == [
        "Example asks for iCloud, which the profile doesn't grant; it will probably crash at launch."
    ]


def test_app_groups_name_the_missing_groups_and_the_livecontainer_route() -> None:
    (line,) = _warnings([capabilities.Gap("App Groups", (GROUP,))])
    assert line.startswith(
        f"Example asks for App Groups ({GROUP}), which the profile doesn't grant; "
        "it will probably crash at launch."
    )
    assert "usually run under LiveContainer instead (delivery: livecontainer)" in line
    assert "graft --set-app-options --app example --delivery livecontainer" in line
    assert "Edit App \u203a Delivery in Graft.app" in line


def test_both_kinds_are_two_lines_crash_first() -> None:
    crash, feature = _warnings(["App Groups", "Push Notifications", "iCloud"])
    assert crash.startswith("Example asks for App Groups and iCloud, which the profile doesn't grant; "
                            "it will probably crash at launch.")
    assert feature == ("Example asks for Push Notifications, which the profile doesn't grant; "
                       "those features won't work.")


def test_many_missing_groups_are_counted_and_none_carries_a_control_character() -> None:
    gap = capabilities.Gap("App Groups", tuple(f"group.g{n}\x1b[2J" for n in range(6)))
    (line,) = _warnings([gap])
    assert "\x1b" not in line
    assert "App Groups (group.g0[2J, group.g1[2J, group.g2[2J, group.g3[2J and 2 more)" in line


def test_the_warning_never_suggests_auto_profile() -> None:
    """App Group assignment can't be done through the App Store Connect API
    (`apple_portal`), so auto_profile could never make this profile."""
    for missing in (["App Groups"], ["iCloud"], ["App Groups", "HealthKit", "iCloud"]):
        assert all("auto_profile" not in line for line in _warnings(missing))


def test_three_or_more_are_listed_with_commas() -> None:
    (line,) = _warnings(["Associated Domains", "HealthKit", "Sign in with Apple"])
    assert "asks for Associated Domains, HealthKit and Sign in with Apple, which" in line


def test_every_capability_is_in_the_one_constant() -> None:
    assert list(capabilities.CAPABILITIES) == [
        "App Groups", "Push Notifications", "iCloud", "Associated Domains", "HealthKit",
        "Sign in with Apple",
    ]
    patterns = [p for ps in capabilities.CAPABILITIES.values() for p in ps]
    assert "keychain-access-groups" not in patterns
    assert "application-identifier" not in patterns



# --- cli-11: the Watch app cyan removes (-w), and a Watch gap is no crash ---------------

WATCH_GROUP = "group.com.example.watch"


def test_a_watch_extension_cyan_removes_is_not_checked(tmp_path: Path) -> None:
    """With `-w` (cyan's for a renamed bundle id, `keep_bundle_id: false`,
    or given in `extra_args`) the Watch app is gone from the device: its
    extension's App Group gave a false crash warning."""
    ipa = make_ipa(tmp_path / "x.ipa", REWRITTEN,
                   watch_appexes={"WatchExt": {"com.apple.security.application-groups": [WATCH_GROUP]}})
    assert capabilities.missing_for(ipa, wildcard_profile(tmp_path), watch=False) == []


def test_a_watch_only_gap_is_a_feature_on_the_watch_not_a_crash(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", REWRITTEN,
                   watch_appexes={"WatchExt": {"com.apple.security.application-groups": [WATCH_GROUP]}})
    (gap,) = capabilities.missing_for(ipa, wildcard_profile(tmp_path))
    assert (gap.name, gap.groups, gap.likely_crash) == ("App Groups", (WATCH_GROUP,), False)
    (line,) = capabilities.warnings("Example", "example", [gap])
    assert line == (f"Example asks for App Groups ({WATCH_GROUP}) for its Watch app, which the profile "
                    "doesn't grant; those features won't work.")


def test_a_gap_the_iphone_app_has_too_stays_a_likely_crash(tmp_path: Path) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, **APP_GROUPS},
                   watch_appexes={"WatchExt": {"com.apple.security.application-groups": [GROUP, WATCH_GROUP]}})
    gaps = capabilities.missing_for(ipa, wildcard_profile(tmp_path))
    assert [(g.groups, g.likely_crash) for g in gaps] == [((GROUP,), True), ((WATCH_GROUP,), False)]


def test_the_watch_flag_follows_cyan() -> None:
    from graft_ipa import cyan_runner
    from graft_ipa.config import CyanOptions

    assert cyan_runner.removes_watch(CyanOptions(keep_bundle_id=False)) is True
    assert cyan_runner.removes_watch(CyanOptions(extra_args=["--no-watch"])) is True
    assert cyan_runner.removes_watch(CyanOptions(extra_args=["-w"])) is True
    assert cyan_runner.removes_watch(CyanOptions()) is False


# --- cli-12: key-value storage alone is a feature, not a crash -------------------------

KVSTORE = {"com.apple.developer.ubiquity-kvstore-identifier": "VENDOR1234.com.example"}


def test_icloud_key_value_storage_alone_is_a_feature_that_wont_work(tmp_path: Path) -> None:
    """An app that only syncs a few values through iCloud reads an empty
    store without it; it looks no container up at launch."""
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, **KVSTORE})
    (gap,) = capabilities.missing_for(ipa, wildcard_profile(tmp_path))
    assert (gap.name, gap.likely_crash) == ("iCloud", False)
    (line,) = capabilities.warnings("Example", "example", [gap])
    assert line == ("Example asks for iCloud (key-value storage only), which the profile doesn't grant; "
                    "those features won't work.")


@pytest.mark.parametrize("key", ["com.apple.developer.icloud-container-identifiers",
                                 "com.apple.developer.ubiquity-container-identifiers"])
def test_an_icloud_container_is_still_a_likely_crash(tmp_path: Path, key: str) -> None:
    ipa = make_ipa(tmp_path / "x.ipa", {**REWRITTEN, **KVSTORE, key: ["iCloud.com.example"]})
    (gap,) = capabilities.missing_for(ipa, wildcard_profile(tmp_path))
    assert (gap.name, gap.likely_crash) == ("iCloud", True)
    assert capabilities.warnings("Example", "example", [gap])[0].endswith("it will probably crash at launch.")
