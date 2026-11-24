"""Review #4: a cached or downloaded auto-provisioned profile must list the
signing certificate and belong to the configured team.

`cache_problem` judged a profile on its App ID, expiry and devices only, and
`ensure_profile_for_bundle` reused a cached or name-matched remote profile on
that verdict. So after the development certificate was revoked or replaced, the
next build reused a profile listing only the old certificate; zsign signed with
the new one and the phone refused the install. Deleting the cached file only
downloaded the same remote profile again. After a move to another team the old
team's profile was reused too, and pre-flight then failed with a hint pointing
back at the old team.

Real profile plists and real DER certificates throughout, so `cache_problem`
itself runs; only App Store Connect is a fake.
"""

from __future__ import annotations

import base64
import plistlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from graft_ipa import mobileprovision, preflight
from graft_ipa.apple_portal import (
    BundleId,
    Certificate,
    Device,
    Profile,
    _find_cert_by_serial,
    ensure_profile_for_bundle,
)
from graft_ipa.config import (
    AppConfig,
    AppStoreConnectConfig,
    Config,
    CyanOptions,
    SigningConfig,
)

UDID = "00008130-001A2B3C0123456D"
TEAM = "TEAMAAAAAA"
OLD_SERIAL = 0x0A11CE
NEW_SERIAL = 0x0B0B


def _cert_der(serial: int, *, team: str = TEAM) -> bytes:
    """An Apple Development certificate as App Store Connect issues one: the
    team that issued it is the subject's OU."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Apple Development: tester"),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, team),
    ])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=300))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.DER)


def _profile_bytes(*, serial: int, team: str = TEAM, pattern: str = "com.bravo.*") -> bytes:
    """A profile as decode() reads one: the XML plist is cut out of the CMS
    bytes, so the plist on its own is enough."""
    return plistlib.dumps({
        "TeamIdentifier": [team],
        "ApplicationIdentifierPrefix": [team],
        "Entitlements": {"application-identifier": f"{team}.{pattern}"},
        "ExpirationDate": datetime.now(UTC) + timedelta(days=200),
        "ProvisionedDevices": [UDID],
        "DeveloperCertificates": [_cert_der(serial, team=team)],
    })


def _profile(id_: str, content: bytes) -> Profile:
    return Profile(
        id=id_, name="graft com.bravo.* dev", profile_type="IOS_APP_DEVELOPMENT",
        uuid=id_, expiration=None, profile_content_b64=base64.b64encode(content).decode(),
    )


def _client(*, remote: bytes, created: bytes) -> MagicMock:
    client = MagicMock()
    client.list_bundle_ids.return_value = [
        BundleId(id="bid1", identifier="com.bravo.*", name="x", platform="IOS")
    ]
    client.list_certificates.return_value = [
        Certificate(id="newcert", certificate_type="DEVELOPMENT", display_name="dev",
                    serial_number=f"{NEW_SERIAL:X}", expiration=None)
    ]
    client.list_devices.return_value = [
        Device(id="dev1", udid=UDID, name="iPhone", platform="IOS", status="ENABLED")
    ]
    client.list_profiles.return_value = [_profile("old1", b"")]
    client.get_profile.return_value = _profile("old1", remote)
    client.create_profile.return_value = _profile("new1", created)
    return client


def _ensure(client: MagicMock, cache: Path, **kwargs):
    return ensure_profile_for_bundle(
        client, bundle_id="com.bravo.app", cert_serial=f"{NEW_SERIAL:X}",
        device_udids=[UDID], cache_dir=cache, **kwargs,
    )


def _serials(path: Path) -> set[int] | None:
    return mobileprovision.developer_certificate_serials(mobileprovision.decode(path))


# --- cache_problem ------------------------------------------------------------


def test_a_profile_without_the_signing_certificate_is_not_reusable(tmp_path: Path) -> None:
    path = tmp_path / "p.mobileprovision"
    path.write_bytes(_profile_bytes(serial=OLD_SERIAL))

    problem = mobileprovision.cache_problem(path, device_udids=[UDID], cert_serial=f"{NEW_SERIAL:X}")

    assert problem is not None and "signing certificate" in problem


def test_a_profile_with_it_is(tmp_path: Path) -> None:
    path = tmp_path / "p.mobileprovision"
    path.write_bytes(_profile_bytes(serial=NEW_SERIAL))

    assert mobileprovision.cache_problem(
        path, device_udids=[UDID], cert_serial=f"{NEW_SERIAL:X}", team_id=TEAM
    ) is None


def test_a_byte_padded_serial_is_the_same_certificate(tmp_path: Path) -> None:
    """`0B0B` and `B0B` are one serial (finding #42's rule, shared here)."""
    path = tmp_path / "p.mobileprovision"
    path.write_bytes(_profile_bytes(serial=NEW_SERIAL))

    assert mobileprovision.cache_problem(path, cert_serial="0b:0b") is None


def test_a_profile_from_another_team_is_not_reusable(tmp_path: Path) -> None:
    path = tmp_path / "p.mobileprovision"
    path.write_bytes(_profile_bytes(serial=NEW_SERIAL, team="OLDTEAM123"))

    problem = mobileprovision.cache_problem(path, team_id=TEAM)

    assert problem is not None and "OLDTEAM123" in problem and TEAM in problem


# --- ensure_profile_for_bundle ------------------------------------------------


def test_a_cached_profile_for_a_replaced_certificate_is_regenerated(tmp_path: Path) -> None:
    """Scenario (a): it was returned '(cached)' with no network call at all."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(_profile_bytes(serial=OLD_SERIAL))
    client = _client(remote=_profile_bytes(serial=OLD_SERIAL), created=_profile_bytes(serial=NEW_SERIAL))

    result = _ensure(client, cache)

    assert result.refreshed is True
    assert "signing certificate" in (result.refresh_reason or "")
    client.create_profile.assert_called_once()
    assert client.create_profile.call_args.kwargs["certificate_ids"] == ["newcert"]
    assert _serials(result.profile_path) == {NEW_SERIAL}
    client.delete_profile.assert_called_once_with("old1")


def test_deleting_the_cache_no_longer_fetches_the_old_certificate_back(tmp_path: Path) -> None:
    """The remote profile of the same name lists only the old certificate;
    it was downloaded and cached again with no create_profile."""
    cache = tmp_path / "profiles"
    client = _client(remote=_profile_bytes(serial=OLD_SERIAL), created=_profile_bytes(serial=NEW_SERIAL))

    result = _ensure(client, cache)

    client.create_profile.assert_called_once()
    assert _serials(result.profile_path) == {NEW_SERIAL}


def test_a_remote_profile_with_the_right_certificate_is_still_reused(tmp_path: Path) -> None:
    cache = tmp_path / "profiles"
    client = _client(remote=_profile_bytes(serial=NEW_SERIAL), created=b"unused")

    result = _ensure(client, cache, team_id=TEAM)

    client.create_profile.assert_not_called()
    assert _serials(result.profile_path) == {NEW_SERIAL}


def test_the_old_team_s_cached_profile_is_regenerated_after_a_team_switch(tmp_path: Path) -> None:
    """Scenario (b): a new key and signing.team_id for another team kept
    profile_cache_dir, and the old team's profile was reused."""
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(
        _profile_bytes(serial=NEW_SERIAL, team="OLDTEAM123")
    )
    # The new team's own graft profile, made for another certificate: a key
    # lists only its own team's profiles, so the old team's is not among them.
    client = _client(
        remote=_profile_bytes(serial=OLD_SERIAL),
        created=_profile_bytes(serial=NEW_SERIAL),
    )

    result = _ensure(client, cache, team_id=TEAM)

    assert "OLDTEAM123" in (result.refresh_reason or "")
    assert mobileprovision.decode(result.profile_path)["TeamIdentifier"] == [TEAM]


def test_a_created_profile_without_the_certificate_stops_the_build(tmp_path: Path) -> None:
    """Judged like every other profile: caching one zsign can't use would
    hand the next build the same refresh, and the phone a refusal."""
    from graft_ipa.apple_portal import PortalError

    cache = tmp_path / "profiles"
    client = _client(remote=_profile_bytes(serial=OLD_SERIAL), created=_profile_bytes(serial=OLD_SERIAL))

    with pytest.raises(PortalError, match="signing certificate"):
        _ensure(client, cache)
    assert not (cache / "com_bravo_x.mobileprovision").exists()


def test_the_signer_passes_the_configured_team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from graft_ipa import apple_portal, signer

    cfg = _auto_cfg(tmp_path)
    seen: dict = {}
    monkeypatch.setattr(apple_portal, "read_p12_cert_serial", lambda *a: f"{NEW_SERIAL:X}")
    monkeypatch.setattr(apple_portal, "ASCClient", lambda creds: object())

    def ensure(client, **kwargs):
        seen.update(kwargs)
        return apple_portal.ProvisionResult(
            profile_path=tmp_path / "p", profile_id="x", bundle_id_pattern="com.bravo.*",
            refreshed=False,
        )

    monkeypatch.setattr(apple_portal, "ensure_profile_for_bundle", ensure)
    signer.resolve_profile_for_app(cfg, _auto_app(auto_profile=True), password="pw")

    assert seen["team_id"] == TEAM
    assert seen["cert_serial"] == f"{NEW_SERIAL:X}"


# --- review #1/#2/#16: a signing.team_id the key can never satisfy ------------

KEY_TEAM = "REALTEAM01"
STALE_TEAM = "OLDTEAM001"


class FakePortal:
    """App Store Connect as one team's API key sees it, with state.

    Profiles it lists, creates and deletes really change the account, and
    every profile it makes is the key's team's, listing the certificate it was
    asked for: a key cannot issue a profile for any other team. Each write is
    counted, because the defect being pinned is writes to the user's developer
    account that nothing undoes.
    """

    def __init__(self, *, team: str = KEY_TEAM, serial: int = NEW_SERIAL,
                 with_content: bool = True) -> None:
        self.team = team
        self.serial = serial
        self.cert_der = _cert_der(serial, team=team)
        self.with_content = with_content
        self.profiles: dict[str, tuple[str, bytes]] = {}
        self.created: list[str] = []
        self.deleted: list[str] = []
        self.read: list[str] = []
        self.bundle_ids = [BundleId(id="bid1", identifier="com.bravo.*", name="x", platform="IOS")]
        self.bundle_ids_created: list[str] = []
        self._next = 0

    def seed(self, name: str, content: bytes) -> str:
        self._next += 1
        pid = f"P{self._next}"
        self.profiles[pid] = (name, content)
        return pid

    def list_bundle_ids(self, *, identifier=None, limit=200):
        return list(self.bundle_ids)

    def create_bundle_id(self, identifier, name, *, platform="IOS"):
        self.bundle_ids_created.append(identifier)
        made = BundleId(id="bid2", identifier=identifier, name=name, platform=platform)
        self.bundle_ids.append(made)
        return made

    def list_certificates(self, *, certificate_type=None):
        content = base64.b64encode(self.cert_der).decode() if self.with_content else None
        return [Certificate(id="cert1", certificate_type="DEVELOPMENT", display_name="dev",
                            serial_number=f"{self.serial:X}", expiration=None,
                            content_b64=content)]

    def list_devices(self, *, platform="IOS"):
        return [Device(id="dev1", udid=UDID, name="iPhone", platform="IOS", status="ENABLED")]

    def _profile(self, pid: str, *, content: bool) -> Profile:
        name, data = self.profiles[pid]
        return Profile(id=pid, name=name, profile_type="IOS_APP_DEVELOPMENT", uuid=pid,
                       expiration=None,
                       profile_content_b64=base64.b64encode(data).decode() if content else None)

    def list_profiles(self, *, profile_type="IOS_APP_DEVELOPMENT"):
        return [self._profile(pid, content=False) for pid in self.profiles]

    def get_profile(self, profile_id, *, with_content=True):
        self.read.append(profile_id)
        return self._profile(profile_id, content=with_content)

    def create_profile(self, *, name, bundle_id, certificate_ids, device_ids,
                       profile_type="IOS_APP_DEVELOPMENT"):
        from graft_ipa.apple_portal import PortalError

        if any(n == name for n, _ in self.profiles.values()):
            raise PortalError("409: a profile with this name already exists")
        pid = self.seed(name, _profile_bytes(serial=self.serial, team=self.team))
        self.created.append(pid)
        return self._profile(pid, content=True)

    def delete_profile(self, profile_id):
        self.deleted.append(profile_id)
        del self.profiles[profile_id]


def _build(portal: FakePortal, cache: Path, *, team_id: str):
    return ensure_profile_for_bundle(
        portal, bundle_id="com.bravo.app", cert_serial=f"{portal.serial:X}",
        device_udids=[UDID], cache_dir=cache, team_id=team_id,
    )


def _old_team_cache(cache: Path) -> None:
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "com_bravo_x.mobileprovision").write_bytes(
        _profile_bytes(serial=OLD_SERIAL, team=STALE_TEAM)
    )


@pytest.mark.parametrize(
    ("cached", "remote"),
    [(False, False), (False, True), (True, True)],
    ids=["empty-account", "fresh-mac", "old-team-cache"],
)
def test_a_team_id_the_key_is_not_from_is_refused_before_anything_is_made(
    tmp_path: Path, cached: bool, remote: bool
) -> None:
    """Review #1/#2: a typo in Team ID, or a team switch with Team ID not yet
    updated. Each build created a profile in the key's team, refused it on the
    team rule, and left it there: ten builds, ten profiles, nothing built.

    The empty account is the case only the certificate can answer: no App ID
    and no profile yet, so a check any later than the certificate lookup
    would already have registered one or made the other."""
    from graft_ipa.apple_portal import PortalError

    portal = FakePortal()
    seeded = []
    if remote:
        seeded.append(portal.seed(
            "graft com.bravo.* dev", _profile_bytes(serial=NEW_SERIAL, team=KEY_TEAM)
        ))
    else:
        portal.bundle_ids = []
    cache = tmp_path / "profiles"
    if cached:
        _old_team_cache(cache)

    for _build_attempt in range(3):
        with pytest.raises(PortalError) as exc:
            _build(portal, cache, team_id=STALE_TEAM)
        assert exc.value.message == (
            f"signing.team_id {STALE_TEAM} doesn't match the App Store Connect "
            f"key's team {KEY_TEAM}"
        )
        assert f"--signing-team-id {KEY_TEAM}" in exc.value.hint
        assert "Nothing was created or deleted" in exc.value.hint

    assert portal.created == []
    assert portal.deleted == []
    assert portal.bundle_ids_created == []
    assert list(portal.profiles) == seeded


def test_with_no_certificate_content_a_listed_profile_still_names_the_key_s_team(
    tmp_path: Path,
) -> None:
    """The key's team read from the profile it lists, when App Store Connect
    sent the certificate without its content: still nothing written."""
    from graft_ipa.apple_portal import PortalError

    portal = FakePortal(with_content=False)
    portal.seed("graft com.bravo.* dev", _profile_bytes(serial=NEW_SERIAL, team=KEY_TEAM))

    with pytest.raises(PortalError, match="doesn't match the App Store Connect key's team"):
        _build(portal, tmp_path / "profiles", team_id=STALE_TEAM)

    assert (portal.created, portal.deleted) == ([], [])


def test_with_nothing_to_read_the_team_from_the_profile_made_is_removed_again(
    tmp_path: Path,
) -> None:
    """The last resort: only the profile just made says which team the key
    is. It is refused, and it does not stay in the account."""
    from graft_ipa.apple_portal import PortalError

    portal = FakePortal(with_content=False)

    for _build_attempt in range(2):
        with pytest.raises(PortalError, match="doesn't match the App Store Connect key's team"):
            _build(portal, tmp_path / "profiles", team_id=STALE_TEAM)

    assert portal.profiles == {}
    assert portal.deleted == portal.created


def test_a_matching_team_id_still_creates_once_and_then_reuses(tmp_path: Path) -> None:
    portal = FakePortal()
    cache = tmp_path / "profiles"

    first = _build(portal, cache, team_id=KEY_TEAM)
    second = _build(portal, cache, team_id=KEY_TEAM)

    assert first.refreshed is True and second.profile_id == "(cached)"
    assert len(portal.created) == 1 and portal.deleted == []


def test_after_a_team_switch_the_new_team_s_valid_profile_is_reused_not_deleted(
    tmp_path: Path,
) -> None:
    """Review #16: the local file is the old team's, so it says nothing about
    the new team's remote profile, which a teammate's Mac is using. It was
    deleted unread and replaced; a fresh Mac in the same state reused it."""
    portal = FakePortal()
    teammate = portal.seed("graft com.bravo.* dev", _profile_bytes(serial=NEW_SERIAL, team=KEY_TEAM))
    cache = tmp_path / "profiles"
    _old_team_cache(cache)

    result = _build(portal, cache, team_id=KEY_TEAM)

    assert result.profile_id == teammate
    assert portal.read == [teammate]
    assert (portal.created, portal.deleted) == ([], [])
    assert mobileprovision.decode(result.profile_path)["TeamIdentifier"] == [KEY_TEAM]


def test_a_remote_profile_is_deleted_only_once_it_has_been_read_and_refused(
    tmp_path: Path,
) -> None:
    portal = FakePortal()
    stale = portal.seed("graft com.bravo.* dev", _profile_bytes(serial=OLD_SERIAL, team=KEY_TEAM))
    cache = tmp_path / "profiles"
    _old_team_cache(cache)

    result = _build(portal, cache, team_id=KEY_TEAM)

    assert portal.read == [stale]
    assert portal.deleted == [stale]
    assert portal.created == [result.profile_id]
    assert _serials(result.profile_path) == {NEW_SERIAL}


# --- pre-flight's team row ----------------------------------------------------


def _auto_cfg(tmp_path: Path) -> Config:
    cfg = Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12", cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=tmp_path / "mine.mobileprovision", team_id=TEAM,
        ),
        cache_dir=tmp_path / "cache", built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks", state_dir=tmp_path / "state", apps={},
    )
    cfg.app_store_connect = AppStoreConnectConfig(
        key_id="KEYID12345", issuer_id="1234-issuer", key_file=tmp_path / "AuthKey.p8",
        device_udids=[UDID], profile_cache_dir=tmp_path / "profiles",
    )
    return cfg


def _auto_app(**kwargs) -> AppConfig:
    return AppConfig(
        name="bravo", bundle_id="com.bravo.app", preferred_version="latest",
        tweaks=[], cyan=CyanOptions(), **kwargs,
    )


def _team_row(results) -> preflight.CheckResult:
    return next(r for r in results if r.name == "profile team id matches config")


def test_preflight_lets_stage_four_replace_the_old_team_s_cached_profile(tmp_path: Path) -> None:
    cfg = _auto_cfg(tmp_path)
    cached = tmp_path / "profiles" / "com_bravo_x.mobileprovision"
    cached.parent.mkdir()
    cached.write_bytes(_profile_bytes(serial=NEW_SERIAL, team="OLDTEAM123"))

    row = _team_row(preflight.check_profile_contents(cfg, app=_auto_app(auto_profile=True)))

    assert row.ok is True, row.hint
    assert "re-provisions it at sign time" in row.detail


def test_a_profile_the_user_chose_still_fails_and_the_hint_names_both_teams(tmp_path: Path) -> None:
    cfg = _auto_cfg(tmp_path)
    cfg.signing.profile.write_bytes(_profile_bytes(serial=NEW_SERIAL, team="OLDTEAM123"))

    row = _team_row(preflight.check_profile_contents(cfg, app=_auto_app()))

    assert row.ok is False
    assert "OLDTEAM123" in row.hint and TEAM in row.hint
    assert row.hint != "update signing.team_id in config.yaml to match the profile"


# --- finding #42: serials compared as numbers ---------------------------------


@pytest.mark.parametrize("listed", ["0F3A11", "0f:3a:11", "F3A11", "00 0F 3A 11"])
def test_a_certificate_is_found_however_asc_pads_its_serial(listed: str) -> None:
    """`read_p12_cert_serial` gives `format(n, 'X')`, which drops leading zero
    nibbles; a byte-padded `0F3A…` from App Store Connect never matched."""
    cert = Certificate(id="c", certificate_type="DEVELOPMENT", display_name="d",
                       serial_number=listed, expiration=None)

    assert _find_cert_by_serial([cert], format(0x0F3A11, "X")) is cert


def test_a_different_serial_is_still_a_different_certificate() -> None:
    cert = Certificate(id="c", certificate_type="DEVELOPMENT", display_name="d",
                       serial_number="0F3A12", expiration=None)

    assert _find_cert_by_serial([cert], "F3A11") is None


# --- re-check #30: the narrowed recheck asks the certificate and team too -------

RETIRED = "00008030-000000000RETIRED"


@pytest.mark.parametrize(
    "cached",
    [
        _profile_bytes(serial=OLD_SERIAL, team=KEY_TEAM),
        _profile_bytes(serial=NEW_SERIAL, team=STALE_TEAM),
    ],
    ids=["replaced-certificate", "other-team"],
)
def test_a_retired_phone_does_not_let_a_stale_profile_through(tmp_path: Path, cached: bytes) -> None:
    """A configured phone App Store Connect no longer has sends the cache
    through a second check, against the phones it can provide. If that
    check forgot the certificate or the team, the old profile came back
    "(cached)": zsign signed with the new certificate and the phone refused
    the install, #4 again."""
    portal = FakePortal()
    cache = tmp_path / "profiles"
    cache.mkdir()
    (cache / "com_bravo_x.mobileprovision").write_bytes(cached)

    result = ensure_profile_for_bundle(
        portal, bundle_id="com.bravo.app", cert_serial=f"{NEW_SERIAL:X}",
        device_udids=[UDID, RETIRED], cache_dir=cache, team_id=KEY_TEAM,
    )

    assert result.profile_id != "(cached)"
    assert result.unregistered_devices == (RETIRED,)
    assert _serials(result.profile_path) == {NEW_SERIAL}
    assert mobileprovision.decode(result.profile_path)["TeamIdentifier"] == [KEY_TEAM]
