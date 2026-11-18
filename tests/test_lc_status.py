"""What `--lc-status` says about one device.

`apps` is the shape InstallationProxy returns: bundle id → metadata. The
metadata used here are the keys the real iPad answered with on 2026-11-17.
"""

from __future__ import annotations

import pytest

from graft_ipa import lc_discovery, lc_manage

TEAM = "ABCDE12345"
PRIMARY = f"com.kdt.livecontainer.{TEAM}"
LC2 = f"com.kdt.LiveContainer2.{TEAM}"
LC3 = f"com.kdt.LiveContainer3.{TEAM}"
NIGHTLY = lc_manage.Release("nightly", "3.8.14", "https://example/LiveContainer.ipa")


def _app(version: str, app_id: str | None = None) -> dict:
    meta = {"CFBundleShortVersionString": version, "CFBundleVersion": version}
    if app_id is not None:
        meta["Entitlements"] = {"application-identifier": app_id}
    return meta


def _status(apps: dict, *, ours: set[str] = frozenset(), latest=NIGHTLY) -> dict:
    return lc_manage.status_payload(
        udid="u1", apps=apps, team=TEAM, ours=set(ours), latest=latest
    )


@pytest.mark.parametrize(
    ("bundle_id", "role"),
    [
        (PRIMARY, "primary"),
        (LC2, "2"),
        (LC3, "3"),
        ("com.kdt.livecontainer", "legacy"),
        ("com.kdt.LiveContainer2.OTHERTEAM", "legacy"),
        ("com.example.app", None),
        ("com.kdt.livecontainerfan", "legacy"),
    ],
)
def test_roles(bundle_id: str, role: str | None) -> None:
    assert lc_discovery.role_of(bundle_id, TEAM) == role


def test_the_ipad_today() -> None:
    """Three instances we installed by hand (scripts, not this verb), so
    nothing recorded them: `unknown`, and up to date."""
    status = _status({
        PRIMARY: _app("3.8.14", f"{TEAM}.{PRIMARY}"),
        LC2: _app("3.8.14", f"{TEAM}.{LC2}"),
        LC3: _app("3.8.14", f"{TEAM}.{LC3}"),
        "com.example.app": _app("21.46.3"),
    })

    assert status["instances"] == [
        {"bundle_id": PRIMARY, "version": "3.8.14", "role": "primary", "wildcard_signed": False},
        {"bundle_id": LC2, "version": "3.8.14", "role": "2", "wildcard_signed": False},
        {"bundle_id": LC3, "version": "3.8.14", "role": "3", "wildcard_signed": False},
    ]
    assert status["managed_by"] == "unknown"
    assert status["latest"] == {"version": "3.8.14", "channel": "nightly"}
    assert status["update_available"] is False
    assert status["reachable"] is True
    assert status["udid"] == "u1"


def test_the_iphone_today() -> None:
    status = _status({
        PRIMARY: _app("3.7.2"), LC2: _app("3.7.2"), LC3: _app("3.7.2"),
        f"com.rileytestut.AltStore.{TEAM}": _app("2.2.1"),
    })

    assert status["managed_by"] == "altstore"
    assert status["update_available"] is True
    assert status["sources"] == {
        "stable": lc_manage.STABLE_SOURCE_URL, "nightly": lc_manage.NIGHTLY_SOURCE_URL,
    }


def test_the_iphones_real_altstore_id_is_recognised() -> None:
    """`ideviceinstaller -n -u 00008130-… list` on the iPhone, 2026-11-17:
    `com.ABCDE12345.com.rileytestut.AltStore, "2.2.1", "AltStore"`. The team
    id is a *prefix*, so a `startswith` check could never match it, and the
    real `--lc-status` called this AltStore-managed phone `unknown`."""
    status = _status({
        PRIMARY: _app("3.7.2"), LC2: _app("3.7.2"), LC3: _app("3.7.2"),
        f"com.{TEAM}.com.rileytestut.AltStore": _app("2.2.1"),
    })

    assert status["managed_by"] == "altstore"


@pytest.mark.parametrize("bundle_id", [
    "com.rileytestut.AltStore",
    f"com.rileytestut.AltStore.{TEAM}",
    f"com.{TEAM}.com.rileytestut.AltStore",
    f"com.{TEAM}.com.SideStore.SideStore",
    "COM.SIDESTORE.SIDESTORE",
])
def test_store_ids_match_on_dot_boundaries_anywhere(bundle_id: str) -> None:
    assert lc_manage.altstore_present({bundle_id: {}}) is True


@pytest.mark.parametrize("bundle_id", [
    "com.rileytestut.AltStoreClone",
    "xcom.rileytestut.AltStore",
    "com.example.notcom.sidestore.sidestore",
    "com.rileytestut.Delta",
    "com.sidestore.sidestorex.helper",
])
def test_ids_that_merely_contain_the_text_do_not_match(bundle_id: str) -> None:
    assert lc_manage.altstore_present({bundle_id: {}}) is False


def test_sidestore_counts_as_altstore() -> None:
    assert _status({PRIMARY: _app("3.8.14"), f"com.SideStore.SideStore.{TEAM}": _app("0.6")})[
        "managed_by"
    ] == "altstore"


def test_an_instance_altstore_installed_counts_even_without_altstore_listed() -> None:
    """AltStore writes `ALTCertificateID` / `ALTBundleIdentifier` into every
    app it installs (LiveContainer reads the first to find its certificate).
    The real iPhone answered `unknown` on 2026-11-17 with no AltStore among
    its User apps — so the app's presence alone is not enough to protect
    AltStore's copies from being replaced."""
    status = _status({PRIMARY: {**_app("3.7.2"), "ALTCertificateID": "X"}, LC2: _app("3.7.2")})

    assert status["managed_by"] == "altstore"


def test_another_app_altstore_installed_says_nothing_about_livecontainer() -> None:
    """The question is who manages *these* LiveContainers. An app AltStore
    sideloaded once, with AltStore since deleted, is not that."""
    status = _status({
        PRIMARY: _app("3.8.14"),
        "com.example.sideloaded": {**_app("1.0"), "ALTBundleIdentifier": "com.example"},
    })

    assert status["managed_by"] == "unknown"


def test_altstore_present_beats_our_own_record() -> None:
    """AltStore would refresh its own copy back over ours."""
    status = _status(
        {PRIMARY: _app("3.8.14"), f"com.rileytestut.AltStore.{TEAM}": _app("2.2.1")},
        ours={PRIMARY},
    )

    assert status["managed_by"] == "altstore"


def test_a_recorded_install_is_ours() -> None:
    assert _status({PRIMARY: _app("3.8.14")}, ours={PRIMARY})["managed_by"] == "graft"


def test_a_record_for_an_instance_no_longer_installed_does_not_count() -> None:
    assert _status({PRIMARY: _app("3.8.14")}, ours={LC2})["managed_by"] == "unknown"


def test_versions_compare_numerically() -> None:
    """As strings '3.8.9' > '3.8.14' and '3.10.0' < '3.9.0' — both wrong."""
    assert _status({PRIMARY: _app("3.8.9")})["update_available"] is True
    newer_than_latest = lc_manage.Release("stable", "3.9.0", "u")
    assert _status({PRIMARY: _app("3.10.0")}, latest=newer_than_latest)[
        "update_available"
    ] is False
    assert _status({PRIMARY: _app("3.7.2")})["update_available"] is True


def test_nothing_installed_is_not_an_update() -> None:
    status = _status({})

    assert status["instances"] == []
    assert status["update_available"] is False


def test_an_unknown_latest_is_not_an_update() -> None:
    status = _status({PRIMARY: _app("1.0")}, latest=None)

    assert status["latest"] is None
    assert status["update_available"] is False


def test_a_legacy_copy_does_not_make_an_update_available() -> None:
    assert _status({"com.kdt.livecontainer": _app("3.7.2")})["update_available"] is False


def test_a_wildcard_signed_copy_is_flagged() -> None:
    """The zsign-signed copy: its keychain hook is off, and upgrading it in
    place fails with MismatchedApplicationIdentifierEntitlement."""
    status = _status({PRIMARY: _app("3.8.14", f"{TEAM}.com.kdt.*")})

    assert status["instances"][0]["wildcard_signed"] is True


def test_an_unreachable_device() -> None:
    status = lc_manage.unreachable_payload("u9", "Device not found")

    assert status["reachable"] is False
    assert status["error"] == "Device not found"
    assert status["instances"] == []
    assert status["managed_by"] == "unknown"
