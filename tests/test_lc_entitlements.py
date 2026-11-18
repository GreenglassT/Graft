"""The entitlements LiveContainer is signed with — the heart of the keychain fix.

zsign copies a wildcard profile's entitlements into the signature verbatim, so
the signature says `keychain-access-groups = ["TEAM.*"]`. In a profile that is
a pattern; in a signature it is the literal string `TEAM.*`. LiveContainer's
keychain hook (`LiveContainer/Tweaks/SecItem.m`) probes
`TEAM.com.kdt.livecontainer.shared`, gets errSecMissingEntitlement, and turns
itself off — after which a guest app's own keychain writes fail and
sign-in is "not remembered".

`build_entitlements` starts instead from what LiveContainer's binaries ask for
(the fixtures below are the real 3.8.14 binaries' embedded requests, extracted
once with `codesign -d --entitlements - --xml`), swaps the `AAAAA11111`
placeholder for the real team, and keeps only what the profile grants. The
profiles are the real decoded ones from this account, copied as dicts.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from graft_ipa.errors import SigningError
from graft_ipa.lc_sign import build_entitlements

FIXTURES = Path(__file__).parent / "fixtures" / "lc"
TEAM = "ABCDE12345"
MAIN_ID = f"com.kdt.livecontainer.{TEAM}"
SIDESTORE_GROUP = f"group.com.SideStore.SideStore.{TEAM}"

# `security cms -D` of com.kdt.livecontainer.ABCDE12345.mobileprovision.
EXACT_PROFILE = {
    "application-identifier": f"{TEAM}.{MAIN_ID}",
    "com.apple.developer.kernel.increased-memory-limit": True,
    "com.apple.developer.team-identifier": TEAM,
    "com.apple.security.application-groups": [SIDESTORE_GROUP],
    "get-task-allow": True,
    "keychain-access-groups": [f"{TEAM}.*", "com.apple.token"],
}

# `security cms -D` of com_kdt_x.mobileprovision — the `com.kdt.*` wildcard
# profile ShareExtension has to ride, since it has no App ID of its own.
WILDCARD_PROFILE = {
    "application-identifier": f"{TEAM}.com.kdt.*",
    "com.apple.developer.team-identifier": TEAM,
    "get-task-allow": True,
    "keychain-access-groups": [f"{TEAM}.*", "com.apple.token"],
}


def _requested(binary: str) -> dict:
    return plistlib.loads((FIXTURES / f"{binary}.entitlements.xml").read_bytes())


def _main(**kwargs) -> tuple[dict, list[str]]:
    return build_entitlements(
        _requested("LiveContainer"), EXACT_PROFILE, team=TEAM, bundle_id=MAIN_ID, **kwargs
    )


def test_the_fixture_really_is_the_placeholder_request() -> None:
    """Guards every test below: if the fixture already carried a real team,
    the placeholder test would pass against a builder that never replaces it."""
    raw = (FIXTURES / "LiveContainer.entitlements.xml").read_text()
    assert "AAAAA11111" in raw
    assert TEAM not in raw


def test_the_placeholder_team_is_replaced_everywhere() -> None:
    ents, _ = _main()

    assert "AAAAA11111" not in plistlib.dumps(ents).decode()
    assert ents["com.apple.developer.team-identifier"] == TEAM


def test_the_application_identifier_is_exact() -> None:
    ents, _ = _main()

    assert ents["application-identifier"] == f"{TEAM}.{MAIN_ID}"


def test_an_extension_on_a_wildcard_profile_still_gets_an_exact_application_identifier() -> None:
    """The second half of the zsign defect: `TEAM.com.kdt.*` is what zsign
    wrote, and it is a pattern, not an identifier."""
    share_id = f"{MAIN_ID}.ShareExtension"
    ents, _ = build_entitlements(
        _requested("ShareExtension"), WILDCARD_PROFILE, team=TEAM, bundle_id=share_id
    )

    assert ents["application-identifier"] == f"{TEAM}.{share_id}"


def test_keychain_groups_are_the_128_explicit_ones_the_hook_probes() -> None:
    ents, _ = _main()
    groups = ents["keychain-access-groups"]

    assert len(groups) == 128
    assert groups[0] == f"{TEAM}.com.kdt.livecontainer.shared"
    assert groups[127] == f"{TEAM}.com.kdt.livecontainer.shared.127"
    # The defect itself, asserted directly: no pattern ever reaches a signature.
    assert not [g for g in groups if "*" in g]


def test_a_keychain_group_the_profile_does_not_grant_is_dropped_and_counted() -> None:
    narrow = {**EXACT_PROFILE, "keychain-access-groups": [f"{TEAM}.com.kdt.livecontainer.shared"]}
    ents, notes = build_entitlements(
        _requested("LiveContainer"), narrow, team=TEAM, bundle_id=MAIN_ID
    )

    assert ents["keychain-access-groups"] == [f"{TEAM}.com.kdt.livecontainer.shared"]
    assert "keychain-access-groups: kept 1/128" in notes


def test_app_groups_are_mapped_to_their_team_suffixed_form() -> None:
    """AltStore/SideStore register the groups with the team appended; the
    binary asks for the bare names. The SideStore group is the first one
    LiveContainer's `appGroupID` tries, which is why all three instances share it."""
    ents, _ = _main()

    assert ents["com.apple.security.application-groups"] == [SIDESTORE_GROUP]


def test_capabilities_the_profile_does_not_grant_are_dropped_and_named() -> None:
    ents, notes = _main()

    assert "com.apple.developer.healthkit" not in ents
    assert "dropped com.apple.developer.healthkit (profile does not grant it)" in notes
    # Granted ones survive.
    assert ents["com.apple.developer.kernel.increased-memory-limit"] is True
    assert ents["get-task-allow"] is True


def test_a_profile_with_no_app_groups_drops_the_key_entirely() -> None:
    ents, notes = build_entitlements(
        _requested("ShareExtension"), WILDCARD_PROFILE, team=TEAM,
        bundle_id=f"{MAIN_ID}.ShareExtension",
    )

    assert "com.apple.security.application-groups" not in ents
    assert "dropped com.apple.security.application-groups (profile does not grant it)" in notes


def test_verbatim_app_id_keeps_the_profiles_literal() -> None:
    """Only for upgrading a zsign-signed install in place: iOS refuses an
    upgrade whose application-identifier changed."""
    share_id = f"{MAIN_ID}.ShareExtension"
    ents, _ = build_entitlements(
        _requested("ShareExtension"), WILDCARD_PROFILE, team=TEAM, bundle_id=share_id,
        verbatim_app_id=True,
    )

    assert ents["application-identifier"] == f"{TEAM}.com.kdt.*"


def test_a_profile_that_does_not_cover_the_bundle_refuses() -> None:
    with pytest.raises(SigningError) as exc:
        build_entitlements(
            _requested("LiveContainer"), EXACT_PROFILE, team=TEAM,
            bundle_id=f"com.kdt.LiveContainer2.{TEAM}",
        )

    assert exc.value.message == (
        f"profile application-identifier '{TEAM}.{MAIN_ID}' does not cover "
        f"'{TEAM}.com.kdt.LiveContainer2.{TEAM}'"
    )


def test_an_unsigned_binary_requests_nothing_but_identity() -> None:
    ents, notes = build_entitlements({}, EXACT_PROFILE, team=TEAM, bundle_id=MAIN_ID)

    assert ents == {
        "application-identifier": f"{TEAM}.{MAIN_ID}",
        "com.apple.developer.team-identifier": TEAM,
        "get-task-allow": True,
    }
    assert notes == []


# ---- the App ID prefix is not always the team id ----
#
# Accounts from before 2011 can have an App ID prefix that differs from their
# team id. Their profiles say `<prefix>.<bundle id>`, so an identifier built as
# `<team>.<bundle id>` is not covered and every signature was refused.

PREFIX = "PREFIX9876"


def _prefixed(profile: dict) -> dict:
    """`profile` as an account whose App ID prefix is not its team id has it.

    Both prefix-scoped values move to the prefix — the application-identifier
    and the keychain-access-groups it grants (`PREFIX.*`) — while the team
    identifier and the team-suffixed app groups stay the team's, as they are
    in such an account's real profiles.
    """
    return {
        **profile,
        "application-identifier": profile["application-identifier"].replace(
            f"{TEAM}.", f"{PREFIX}.", 1
        ),
        "keychain-access-groups": [
            g.replace(f"{TEAM}.", f"{PREFIX}.", 1) for g in profile["keychain-access-groups"]
        ],
    }


def test_the_application_identifier_takes_the_profiles_prefix() -> None:
    ents, _ = build_entitlements(
        _requested("LiveContainer"), _prefixed(EXACT_PROFILE), team=TEAM, bundle_id=MAIN_ID,
        app_id_prefix=PREFIX,
    )

    assert ents["application-identifier"] == f"{PREFIX}.{MAIN_ID}"
    # The team id is still the team id: only the App ID uses the prefix.
    assert ents["com.apple.developer.team-identifier"] == TEAM


def test_keychain_groups_take_the_prefix_and_all_128_survive() -> None:
    """Keychain access groups are scoped by App ID prefix, not team. Filled
    with the team, all 128 fall outside the profile's `PREFIX.*` and are
    dropped — the very login failure this signer exists to fix."""
    ents, notes = build_entitlements(
        _requested("LiveContainer"), _prefixed(EXACT_PROFILE), team=TEAM, bundle_id=MAIN_ID,
        app_id_prefix=PREFIX,
    )

    groups = ents["keychain-access-groups"]
    assert len(groups) == 128
    assert groups[0] == f"{PREFIX}.com.kdt.livecontainer.shared"
    assert not [g for g in groups if g.startswith(f"{TEAM}.")]
    assert not [n for n in notes if n.startswith("keychain-access-groups")]


def test_only_keychain_groups_move_to_the_prefix() -> None:
    """Every other placeholder stays the team's. LiveContainer's own binaries
    put it in nothing else that survives to the signature, so a synthetic
    request is the only way to tell "keychain groups take the prefix" from
    "everything does"."""
    requested = {
        "keychain-access-groups": ["AAAAA11111.com.kdt.livecontainer.shared"],
        "com.example.team-scoped": "AAAAA11111",
    }
    profile = {
        **_prefixed(EXACT_PROFILE),
        "com.example.team-scoped": TEAM,
    }

    ents, notes = build_entitlements(
        requested, profile, team=TEAM, bundle_id=MAIN_ID, app_id_prefix=PREFIX,
    )

    assert ents["com.example.team-scoped"] == TEAM, notes
    assert ents["keychain-access-groups"] == [f"{PREFIX}.com.kdt.livecontainer.shared"]


def test_app_groups_stay_team_scoped_under_a_different_prefix() -> None:
    ents, _ = build_entitlements(
        _requested("LiveContainer"), _prefixed(EXACT_PROFILE), team=TEAM, bundle_id=MAIN_ID,
        app_id_prefix=PREFIX,
    )

    assert ents["com.apple.security.application-groups"] == [SIDESTORE_GROUP]


def test_a_wildcard_profile_with_a_different_prefix_still_covers_its_extension() -> None:
    share_id = f"{MAIN_ID}.ShareExtension"
    ents, _ = build_entitlements(
        _requested("ShareExtension"), _prefixed(WILDCARD_PROFILE), team=TEAM,
        bundle_id=share_id, app_id_prefix=PREFIX,
    )

    assert ents["application-identifier"] == f"{PREFIX}.{share_id}"


def test_without_a_prefix_the_team_id_is_the_prefix() -> None:
    ents, _ = build_entitlements(
        _requested("LiveContainer"), EXACT_PROFILE, team=TEAM, bundle_id=MAIN_ID,
        app_id_prefix=None,
    )

    assert ents["application-identifier"] == f"{TEAM}.{MAIN_ID}"


@pytest.mark.parametrize(
    ("plist", "expected"),
    [
        ({"ApplicationIdentifierPrefix": [PREFIX], "TeamIdentifier": [TEAM]}, PREFIX),
        ({"TeamIdentifier": [TEAM]}, TEAM),
        ({}, "FALLBACK01"),
        ({"ApplicationIdentifierPrefix": [], "TeamIdentifier": []}, "FALLBACK01"),
    ],
    ids=["prefix-wins", "team-when-no-prefix", "fallback-when-neither", "empty-lists"],
)
def test_the_prefix_rule_shared_with_the_profile_cache(plist: dict, expected: str) -> None:
    from graft_ipa.mobileprovision import app_id_prefix

    assert app_id_prefix(plist, fallback="FALLBACK01") == expected


def test_a_profile_for_another_prefix_is_still_refused() -> None:
    """Taking the prefix from the profile must not turn the coverage check off."""
    with pytest.raises(SigningError) as exc:
        build_entitlements(
            _requested("LiveContainer"), _prefixed(EXACT_PROFILE), team=TEAM,
            bundle_id=MAIN_ID, app_id_prefix="OTHERPREF1",
        )

    assert exc.value.message == (
        f"profile application-identifier '{PREFIX}.{MAIN_ID}' does not cover "
        f"'OTHERPREF1.{MAIN_ID}'"
    )
