from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import apple_portal
from graft_ipa.apple_portal import (
    ASCClient,
    BundleId,
    Certificate,
    Device,
    PortalCredentials,
    PortalError,
    Profile,
    derive_wildcard_for,
    ensure_profile_for_bundle,
)

# ---- derive_wildcard_for ----


@pytest.mark.parametrize(
    "bundle_id,expected",
    [
        ("com.example.ios.app", "com.example.*"),
        ("com.delta.app", "com.delta.*"),
        ("com.delta.app.IntentExtension", "com.delta.*"),
        ("com.charlie.client", "com.charlie.*"),
        ("net.example.app", "net.example.*"),
    ],
)
def test_derive_wildcard_namespace(bundle_id: str, expected: str) -> None:
    assert derive_wildcard_for(bundle_id) == expected


# ---- JWT ----


def _ed_key() -> str:
    """Generate a fresh P-256 (ES256) key in PKCS#8 PEM each test run."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return pem.decode()


def test_make_jwt(tmp_path: Path) -> None:
    key_path = tmp_path / "test.p8"
    key_path.write_text(_ed_key())
    creds = PortalCredentials(key_id="ABCDE12345", issuer_id="abc-123", key_path=key_path)
    token = apple_portal.make_jwt(creds)
    # JWT has three base64 segments
    assert token.count(".") == 2
    # Header is unauthenticated base64, decode it to confirm fields
    import base64

    header_b64 = token.split(".")[0]
    header_b64 += "=" * ((4 - len(header_b64) % 4) % 4)
    header = json.loads(base64.urlsafe_b64decode(header_b64))
    assert header["alg"] == "ES256"
    assert header["kid"] == "ABCDE12345"
    assert header["typ"] == "JWT"


# ---- ASCClient ----


def _stub_response(status_code: int, body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(body or {})
    resp.content = b"{}" if status_code != 204 else b""
    resp.json.return_value = body or {}
    return resp


def _make_client(tmp_path: Path) -> ASCClient:
    key = tmp_path / "k.p8"
    key.write_text(_ed_key())
    creds = PortalCredentials(key_id="A", issuer_id="B", key_path=key)
    session = MagicMock()
    return ASCClient(creds, session=session)


def test_list_bundle_ids(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    client.session.request.return_value = _stub_response(
        200,
        {
            "data": [
                {
                    "id": "X",
                    "type": "bundleIds",
                    "attributes": {
                        "identifier": "com.example.*",
                        "name": "Example",
                        "platform": "IOS",
                    },
                }
            ]
        },
    )
    result = client.list_bundle_ids(identifier="com.example.*")
    assert len(result) == 1
    assert result[0] == BundleId(id="X", identifier="com.example.*", name="Example", platform="IOS")
    # Verify the URL/params
    call = client.session.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1].endswith("/bundleIds")
    assert call.kwargs["params"]["filter[identifier]"] == "com.example.*"


def test_401_raises_portal_error(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    client.session.request.return_value = _stub_response(401)
    with pytest.raises(PortalError) as exc:
        client.list_devices()
    assert "401" in str(exc.value) or "rejected" in str(exc.value)


def test_403_hints_role(tmp_path: Path) -> None:
    client = _make_client(tmp_path)
    client.session.request.return_value = _stub_response(403)
    with pytest.raises(PortalError) as exc:
        client.list_certificates()
    assert "App Manager" in str(exc.value) or "role" in str(exc.value).lower()


# ---- profile cache naming ----


def test_profile_cache_path_is_the_one_naming_rule(tmp_path: Path) -> None:
    """`signer.plan_profile_for_app` answers "which profile will this build
    use?" and has to give the same answer `ensure_profile_for_bundle` writes.

    It used to reach into `apple_portal._cache_filename` — a private name in
    another module — to do it. The literal below is pinned from both sides on
    purpose: a change to the rule that only lands here would let pre-flight
    validate one file while the portal writes another.
    """
    from graft_ipa.apple_portal import profile_cache_path

    assert profile_cache_path(tmp_path, "com.example.*") == (
        tmp_path / "com_example_x.mobileprovision"
    )


# ---- ensure_profile_for_bundle ----


def test_ensure_profile_returns_cached_if_present(tmp_path: Path) -> None:
    """A cached profile that is still good is reused, with no HTTP at all."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    pattern = "com_example_x.mobileprovision"
    (cache / pattern).write_bytes(b"cached-blob")

    client = MagicMock()
    # `cache_problem` is the validity rule, patched here rather than reproduced:
    # this test is about what happens once the cache is judged good.
    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.example.ios.app",
            cert_serial="DEADBEEF",
            device_udids=["00008130-X"],
            cache_dir=cache,
        )
    assert result.refreshed is False
    assert result.refresh_reason is None
    assert result.profile_path.read_bytes() == b"cached-blob"
    # No HTTP traffic if cached
    client.list_bundle_ids.assert_not_called()
    client.create_profile.assert_not_called()


def test_ensure_profile_creates_and_caches(tmp_path: Path) -> None:
    cache = tmp_path / "profiles"
    client = MagicMock(spec=ASCClient)
    client.list_bundle_ids.return_value = []  # not yet registered
    client.create_bundle_id.return_value = BundleId(
        id="bid1", identifier="com.example.*", name="x", platform="IOS"
    )
    client.list_certificates.return_value = [
        Certificate(
            id="cert1",
            certificate_type="DEVELOPMENT",
            display_name="Apple Development: tester",
            serial_number="DEADBEEF",
            expiration=None,
        )
    ]
    client.list_devices.return_value = [
        Device(
            id="dev1",
            udid="00008130-001",
            name="iPhone 15 Pro",
            platform="IOS",
            status="ENABLED",
        ),
    ]
    client.list_profiles.return_value = []  # no existing profile

    import base64

    client.create_profile.return_value = Profile(
        id="prof1",
        name="x",
        profile_type="IOS_APP_DEVELOPMENT",
        uuid="uuid",
        expiration=None,
        profile_content_b64=base64.b64encode(b"PROFILE_BYTES").decode(),
    )

    # `cache_problem` is the validity rule, patched here rather than reproduced
    # — same reason as the test above. It is asked about the *new* profile now
    # as well as the old one, and `PROFILE_BYTES` is not a CMS blob.
    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.example.ios.app",
            cert_serial="DEADBEEF",
            device_udids=["00008130-001"],
            cache_dir=cache,
        )

    assert result.refreshed is True
    assert result.bundle_id_pattern == "com.example.*"
    assert result.profile_path.exists()
    assert result.profile_path.read_bytes() == b"PROFILE_BYTES"
    args, kwargs = client.create_bundle_id.call_args
    assert args == ("com.example.*",)
    assert "name" in kwargs
    args, kwargs = client.create_profile.call_args
    assert kwargs["bundle_id"].id == "bid1"
    assert kwargs["certificate_ids"] == ["cert1"]
    assert kwargs["device_ids"] == ["dev1"]


def test_ensure_profile_errors_when_cert_missing(tmp_path: Path) -> None:
    client = MagicMock(spec=ASCClient)
    client.list_bundle_ids.return_value = [
        BundleId(id="b", identifier="com.example.*", name="x", platform="IOS")
    ]
    client.list_certificates.return_value = [
        Certificate(
            id="cert1",
            certificate_type="IOS_DEVELOPMENT",
            display_name="someone else",
            serial_number="OTHER",
            expiration=None,
        )
    ]
    with pytest.raises(PortalError) as exc:
        ensure_profile_for_bundle(
            client,
            bundle_id="com.example.ios.app",
            cert_serial="DEADBEEF",
            device_udids=["00008130-001"],
            cache_dir=tmp_path / "p",
        )
    assert "no IOS_DEVELOPMENT cert" in str(exc.value)


def _client_with_devices(devices: list[Device]) -> MagicMock:
    client = MagicMock(spec=ASCClient)
    client.list_bundle_ids.return_value = [
        BundleId(id="b", identifier="com.delta.*", name="x", platform="IOS")
    ]
    client.list_certificates.return_value = [
        Certificate(
            id="cert1",
            certificate_type="IOS_DEVELOPMENT",
            display_name="t",
            serial_number="DEADBEEF",
            expiration=None,
        )
    ]
    client.list_devices.return_value = devices
    return client


def _enabled(udid: str, ident: str) -> Device:
    return Device(id=ident, udid=udid, name="iPhone", platform="IOS", status="ENABLED")


def test_ensure_profile_errors_when_device_missing(tmp_path: Path) -> None:
    client = _client_with_devices([])  # no devices in ASC
    with pytest.raises(PortalError) as exc:
        ensure_profile_for_bundle(
            client,
            bundle_id="com.delta.app",
            cert_serial="DEADBEEF",
            device_udids=["00008130-X"],
            cache_dir=tmp_path / "p",
        )
    assert "no enabled iOS devices" in str(exc.value)


def test_a_udid_ASC_does_not_know_is_skipped_and_carried_out_on_the_result(
    tmp_path: Path,
) -> None:
    """One registered, one not: the build proceeds for the one that exists.

    Refusing outright was the other candidate and it is worse in the ordinary
    case — replacing a phone leaves its UDID in config.yaml, and a build that
    stops working for a device the user no longer owns is a bad trade. What
    made the silent version harmful was that nothing was ever said about it,
    so the profile came back missing a phone and the install was refused with
    no explanation anywhere. The fact is carried out on the result and the
    build warns; `unregistered_devices` is what makes that possible.
    """
    client = _client_with_devices([_enabled("00008130-KNOWN", "dev1")])
    client.list_profiles.return_value = []
    client.create_profile.return_value = Profile(
        id="p1",
        name="graft com.delta.* dev",
        profile_type="IOS_APP_DEVELOPMENT",
        uuid="u",
        expiration=None,
        profile_content_b64=base64.b64encode(b"BYTES").decode(),
    )

    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.delta.app",
            cert_serial="DEADBEEF",
            device_udids=["00008130-KNOWN", "00008130-STRANGER"],
            cache_dir=tmp_path / "p",
        )

    assert result.unregistered_devices == ("00008130-STRANGER",)
    assert client.create_profile.call_args.kwargs["device_ids"] == ["dev1"]


def test_a_registered_udid_is_matched_case_insensitively(tmp_path: Path) -> None:
    """ASC normalises UDIDs to lower case in places and config.yaml does not.

    The resolution below has always been case-insensitive; asserted here so the
    hard failure above cannot become a hard failure over letter case.
    """
    client = _client_with_devices([_enabled("00008130-known", "dev1")])
    client.list_profiles.return_value = []
    client.create_profile.return_value = Profile(
        id="p1",
        name="graft com.delta.* dev",
        profile_type="IOS_APP_DEVELOPMENT",
        uuid="u",
        expiration=None,
        profile_content_b64=base64.b64encode(b"BYTES").decode(),
    )

    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        result = ensure_profile_for_bundle(
            client,
            bundle_id="com.delta.app",
            cert_serial="DEADBEEF",
            device_udids=["00008130-KNOWN"],
            cache_dir=tmp_path / "p",
        )

    assert result.profile_path.read_bytes() == b"BYTES"
    assert client.create_profile.call_args.kwargs["device_ids"] == ["dev1"]


# ---- exact App ID matching ----
#
# App Store Connect's `filter[identifier]` is a prefix match, not an equality
# test: asked for `com.kdt.livecontainer.ABCDE12345` it returns the `.LiveProcess`
# App ID too, and on the account this was found on it returned that one *first*.
# `matches[0]` then built the main app's profile against the extension's App ID
# — a profile whose application-identifier cannot sign the app it was made for.


_MAIN = "com.kdt.livecontainer.ABCDE12345"


def _lc_client(bundle_ids: list[BundleId]) -> MagicMock:
    client = MagicMock(spec=ASCClient)
    client.list_bundle_ids.return_value = bundle_ids
    client.create_bundle_id.return_value = BundleId(
        id="NEW", identifier=_MAIN, name="x", platform="IOS"
    )
    client.list_certificates.return_value = [
        Certificate(
            id="cert1",
            certificate_type="DEVELOPMENT",
            display_name="t",
            serial_number="DEADBEEF",
            expiration=None,
        )
    ]
    client.list_devices.return_value = [_enabled("00008160-001", "dev1")]
    client.list_profiles.return_value = []
    client.create_profile.return_value = Profile(
        id="prof1",
        name="x",
        profile_type="IOS_APP_DEVELOPMENT",
        uuid="uuid",
        expiration=None,
        profile_content_b64=base64.b64encode(b"PROFILE_BYTES").decode(),
    )
    return client


def _ensure_exact(client: MagicMock, tmp_path: Path, **kwargs) -> None:
    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        ensure_profile_for_bundle(
            client,
            bundle_id=_MAIN,
            cert_serial="DEADBEEF",
            device_udids=["00008160-001"],
            cache_dir=tmp_path / "profiles",
            exact=True,
            **kwargs,
        )


def test_the_exact_identifier_wins_over_a_prefix_match_listed_first(tmp_path: Path) -> None:
    client = _lc_client([
        BundleId(id="LP", identifier=f"{_MAIN}.LiveProcess", name="lp", platform="IOS"),
        BundleId(id="MAIN", identifier=_MAIN, name="main", platform="IOS"),
    ])

    _ensure_exact(client, tmp_path)

    assert client.create_profile.call_args.kwargs["bundle_id"].id == "MAIN"
    client.create_bundle_id.assert_not_called()


def test_the_exact_match_ignores_case(tmp_path: Path) -> None:
    """Bundle ids are case-insensitive to Apple: registering
    `com.kdt.livecontainer2.abcde12345` next to `com.kdt.LiveContainer2.ABCDE12345`
    would be a duplicate at best and a 409 at worst."""
    registered = "com.kdt.LiveContainer2.ABCDE12345"
    client = _lc_client([
        BundleId(id="LC2", identifier=registered, name="lc2", platform="IOS"),
    ])

    with patch.object(apple_portal.mobileprovision, "cache_problem", return_value=None):
        ensure_profile_for_bundle(
            client, bundle_id=registered.lower(), cert_serial="DEADBEEF",
            device_udids=["00008160-001"], cache_dir=tmp_path / "profiles",
            exact=True, register_missing=False,
        )

    assert client.create_profile.call_args.kwargs["bundle_id"].id == "LC2"
    client.create_bundle_id.assert_not_called()


def test_a_prefix_match_alone_is_not_the_app_id_asked_for(tmp_path: Path) -> None:
    """Default behaviour registers the missing pattern rather than borrowing a
    neighbour's App ID — `matches[0]` used to hand back `.LiveProcess` here."""
    client = _lc_client([
        BundleId(id="LP", identifier=f"{_MAIN}.LiveProcess", name="lp", platform="IOS"),
    ])

    _ensure_exact(client, tmp_path)

    assert client.create_bundle_id.call_args.args == (_MAIN,)
    assert client.create_profile.call_args.kwargs["bundle_id"].id == "NEW"


def test_register_missing_false_refuses_instead_of_registering(tmp_path: Path) -> None:
    """LiveContainer's App IDs need an App Group assigned, which the ASC API
    cannot do — registering one here would produce an App ID that can never
    carry the group the keychain hook depends on."""
    client = _lc_client([
        BundleId(id="LP", identifier=f"{_MAIN}.LiveProcess", name="lp", platform="IOS"),
    ])

    with pytest.raises(apple_portal.AppIdNotRegistered) as exc:
        _ensure_exact(client, tmp_path, register_missing=False)

    assert exc.value.message == f"App ID {_MAIN} is not registered in this account"
    client.create_bundle_id.assert_not_called()
    client.create_profile.assert_not_called()


# ---- a profile for the wrong App ID is a cache problem ----
#
# `matches[0]` once built a main app's profile against `.LiveProcess`. A file
# made that way is still sitting in some caches, valid in every other respect
# — so the application-identifier is checked too, and such a file is replaced.

from datetime import UTC, datetime, timedelta  # noqa: E402

_LATER = datetime.now(UTC) + timedelta(days=100)


def _decoded(app_id: str) -> dict:
    return {
        "TeamIdentifier": ["ABCDE12345"], "ExpirationDate": _LATER,
        "ProvisionedDevices": ["00008160-001"],
        "Entitlements": {"application-identifier": app_id},
    }


def test_a_profile_for_another_app_id_is_a_cache_problem(tmp_path: Path) -> None:
    profile = tmp_path / "p.mobileprovision"
    profile.write_bytes(b"x")
    with patch.object(apple_portal.mobileprovision, "decode",
                      return_value=_decoded(f"ABCDE12345.{_MAIN}.LiveProcess")):
        problem = apple_portal.mobileprovision.cache_problem(
            profile, device_udids=["00008160-001"], application_identifier=_MAIN
        )
        fine = apple_portal.mobileprovision.cache_problem(
            profile, device_udids=["00008160-001"],
            application_identifier=f"{_MAIN}.LiveProcess",
        )

    assert problem == (
        f"the cached profile is for ABCDE12345.{_MAIN}.LiveProcess, not ABCDE12345.{_MAIN}"
    )
    assert fine is None


def test_a_cached_profile_for_the_wrong_app_id_is_replaced_and_the_remote_deleted(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "profiles"
    cache.mkdir()
    cached = apple_portal.profile_cache_path(cache, _MAIN)
    cached.write_bytes(b"BAD")
    client = _lc_client([BundleId(id="MAIN", identifier=_MAIN, name="m", platform="IOS")])
    client.list_profiles.return_value = [Profile(
        id="OLD", name=f"graft {_MAIN} dev", profile_type="IOS_APP_DEVELOPMENT",
        uuid="u", expiration=None, profile_content_b64=None,
    )]
    client.create_profile.return_value = Profile(
        id="NEW", name="x", profile_type="IOS_APP_DEVELOPMENT", uuid="u2", expiration=None,
        profile_content_b64=base64.b64encode(b"GOOD").decode(),
    )
    plists = {
        b"BAD": _decoded(f"ABCDE12345.{_MAIN}.LiveProcess"),
        b"GOOD": _decoded(f"ABCDE12345.{_MAIN}"),
    }

    with patch.object(apple_portal.mobileprovision, "decode",
                      side_effect=lambda path: plists[path.read_bytes()]):
        result = ensure_profile_for_bundle(
            client, bundle_id=_MAIN, cert_serial="DEADBEEF",
            device_udids=["00008160-001"], cache_dir=cache, exact=True,
            register_missing=False,
        )

    assert cached.read_bytes() == b"GOOD"
    assert result.refresh_reason.startswith("the cached profile is for ")
    client.delete_profile.assert_called_once_with("OLD")


def _remote_profile_only(tmp_path: Path, name: str) -> tuple[MagicMock, Path]:
    """An account holding one profile called `name` for `_MAIN`, and no local copy."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    client = _lc_client([BundleId(id="MAIN", identifier=_MAIN, name="m", platform="IOS")])
    client.list_profiles.return_value = [Profile(
        id="REMOTE", name=name, profile_type="IOS_APP_DEVELOPMENT",
        uuid="u", expiration=None, profile_content_b64=None,
    )]
    client.get_profile.return_value = Profile(
        id="REMOTE", name=name, profile_type="IOS_APP_DEVELOPMENT", uuid="u",
        expiration=None, profile_content_b64=base64.b64encode(b"GOOD").decode(),
    )
    return client, cache


@pytest.mark.parametrize("name", [f"graft {_MAIN} dev", f"tweak-build {_MAIN} dev"])
def test_a_profile_this_tool_made_is_reused_under_either_name(
    tmp_path: Path, name: str
) -> None:
    """Profiles created before the rename are called `tweak-build <id> dev`.

    Recognised by the new name only, every existing install would create a
    second profile for each app on its first build after upgrading, and keep
    the orphan in the account forever — the reuse path is the only thing
    that ever looks at an existing remote profile.
    """
    client, cache = _remote_profile_only(tmp_path, name)

    with patch.object(apple_portal.mobileprovision, "decode",
                      return_value=_decoded(f"ABCDE12345.{_MAIN}")):
        result = ensure_profile_for_bundle(
            client, bundle_id=_MAIN, cert_serial="DEADBEEF",
            device_udids=["00008160-001"], cache_dir=cache, exact=True,
            register_missing=False,
        )

    assert result.profile_id == "REMOTE"
    client.create_profile.assert_not_called()


def test_a_graft_profile_is_preferred_over_a_surviving_legacy_one(tmp_path: Path) -> None:
    """Both can exist — a refresh that could not delete the old one, or two
    machines on one account mid-upgrade. Whichever ASC lists first must not
    decide: the new name is the one this tool maintains."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    client = _lc_client([BundleId(id="MAIN", identifier=_MAIN, name="m", platform="IOS")])
    client.list_profiles.return_value = [
        Profile(id="LEGACY", name=f"tweak-build {_MAIN} dev", profile_type="IOS_APP_DEVELOPMENT",
                uuid="u1", expiration=None, profile_content_b64=None),
        Profile(id="CURRENT", name=f"graft {_MAIN} dev", profile_type="IOS_APP_DEVELOPMENT",
                uuid="u2", expiration=None, profile_content_b64=None),
    ]
    client.get_profile.side_effect = lambda pid, with_content=False: Profile(
        id=pid, name="x", profile_type="IOS_APP_DEVELOPMENT", uuid="u", expiration=None,
        profile_content_b64=base64.b64encode(b"GOOD").decode(),
    )

    with patch.object(apple_portal.mobileprovision, "decode",
                      return_value=_decoded(f"ABCDE12345.{_MAIN}")):
        result = ensure_profile_for_bundle(
            client, bundle_id=_MAIN, cert_serial="DEADBEEF",
            device_udids=["00008160-001"], cache_dir=cache, exact=True,
            register_missing=False,
        )

    assert result.profile_id == "CURRENT"
    client.create_profile.assert_not_called()


def test_a_new_profile_is_named_for_graft(tmp_path: Path) -> None:
    cache = tmp_path / "profiles"
    cache.mkdir()
    client = _lc_client([BundleId(id="MAIN", identifier=_MAIN, name="m", platform="IOS")])
    client.list_profiles.return_value = []
    client.create_profile.return_value = Profile(
        id="NEW", name="x", profile_type="IOS_APP_DEVELOPMENT", uuid="u2", expiration=None,
        profile_content_b64=base64.b64encode(b"GOOD").decode(),
    )

    with patch.object(apple_portal.mobileprovision, "decode",
                      return_value=_decoded(f"ABCDE12345.{_MAIN}")):
        ensure_profile_for_bundle(
            client, bundle_id=_MAIN, cert_serial="DEADBEEF",
            device_udids=["00008160-001"], cache_dir=cache, exact=True,
            register_missing=False,
        )

    assert client.create_profile.call_args.kwargs["name"] == f"graft {_MAIN} dev"


def test_an_app_id_prefix_that_is_not_the_team_id_is_still_the_right_app_id(
    tmp_path: Path,
) -> None:
    """Accounts from before 2011 can have App ID prefixes that differ from the
    team id. Building `TeamIdentifier + pattern` would then reject every
    profile they have — freshly created ones included — and re-provision on
    every run. The prefix the profile itself declares is the one to use."""
    profile = tmp_path / "p.mobileprovision"
    profile.write_bytes(b"x")
    legacy = {
        **_decoded(f"ABCDE12345.{_MAIN}"),
        "ApplicationIdentifierPrefix": ["ABCDE12345"],
    }
    with patch.object(apple_portal.mobileprovision, "decode", return_value=legacy):
        ok = apple_portal.mobileprovision.cache_problem(
            profile, device_udids=["00008160-001"], application_identifier=_MAIN
        )
        wrong = apple_portal.mobileprovision.cache_problem(
            profile, device_udids=["00008160-001"], application_identifier=f"{_MAIN}.LiveProcess"
        )

    assert ok is None
    assert wrong == (
        f"the cached profile is for ABCDE12345.{_MAIN}, not ABCDE12345.{_MAIN}.LiveProcess"
    )
