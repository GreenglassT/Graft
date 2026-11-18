"""Pre-flight has to validate the profile that will actually sign the app.

`run_all_checks` was made delivery-aware but not app-aware: it always read
`cfg.signing.profile`, even for an app configured with a per-app `profile:`
override or with `auto_profile`. That failed in both directions.

  False fail — an app with `profile: ~/profiles/bravo.mobileprovision` and no
  global wildcard on disk (SigningConfig.from_dict requires the *key*, not the
  file). `check_signing_files` failed on `signing.profile` and `_build_one`
  raised before stage 2, so a supported config could never be built.

  False pass — for an `auto_profile` app, `check_devices` read
  ProvisionedDevices out of the global wildcard, so "device in provisioning
  profile" went green off a file the build never uses, while the ASC-generated
  profile that does sign may not list the connected phone.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import preflight, signer
from graft_ipa.build_log import BuildLog
from graft_ipa.config import (
    AppConfig,
    AppStoreConnectConfig,
    Config,
    CyanOptions,
    SigningConfig,
)
from graft_ipa.errors import PreflightError
from graft_ipa.preflight import CheckResult

PROFILE_ROW = "signing profile (.mobileprovision)"
DEVICE_ROW = "device in provisioning profile"
UDID = "00008130-001A2B3C0123456D"


def _cfg(tmp_path: Path) -> Config:
    """A config whose global wildcard path is configured but NOT on disk."""
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=tmp_path / "wildcard.mobileprovision",
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={},
    )


def _app(**kwargs) -> AppConfig:
    base = {
        "name": "bravo",
        "bundle_id": "com.bravo.app",
        "preferred_version": "latest",
        "tweaks": [],
        "cyan": CyanOptions(),
    }
    return AppConfig(**{**base, **kwargs})


def _asc(tmp_path: Path) -> AppStoreConnectConfig:
    return AppStoreConnectConfig(
        key_id="KEYID12345",
        issuer_id="1234-issuer",
        key_file=tmp_path / "AuthKey.p8",
        device_udids=[UDID],
        profile_cache_dir=tmp_path / "profiles",
    )


def _row(results, name):
    return next((r for r in results if r.name == name), None)


# ---------------------------------------------------------------------------
# False fail
# ---------------------------------------------------------------------------


def test_a_per_app_override_is_the_profile_that_gets_checked(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)  # global wildcard missing from disk
    override = tmp_path / "bravo.mobileprovision"
    override.write_bytes(b"x")

    row = _row(preflight.check_signing_files(cfg, app=_app(profile_override=override)), PROFILE_ROW)

    assert row.ok is True
    assert str(override) in row.detail


def test_an_override_app_survives_a_full_check_run(tmp_path: Path) -> None:
    """End to end: this config used to be unbuildable, not merely mis-reported."""
    cfg = _cfg(tmp_path)
    override = tmp_path / "bravo.mobileprovision"
    override.write_bytes(b"x")

    results = preflight.run_all_checks(
        cfg, will_install=False, app=_app(profile_override=override)
    )

    assert _row(results, PROFILE_ROW).ok is True


def test_a_missing_override_still_fails(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    app = _app(profile_override=tmp_path / "nope.mobileprovision")

    row = _row(preflight.check_signing_files(cfg, app=app), PROFILE_ROW)

    assert row.ok is False
    assert "nope.mobileprovision" in row.detail


def test_without_an_app_the_global_wildcard_is_still_what_is_checked(tmp_path: Path) -> None:
    """`--check` has no app in hand; its behaviour must not change."""
    cfg = _cfg(tmp_path)

    row = _row(preflight.check_signing_files(cfg), PROFILE_ROW)

    assert row.ok is False
    assert str(cfg.signing.profile) in row.detail


def test_profile_contents_are_parsed_from_the_override(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")  # wildcard exists but belongs to another team
    override = tmp_path / "bravo.mobileprovision"
    override.write_bytes(b"x")
    plists = {
        cfg.signing.profile: {"TeamIdentifier": ["WRONGTEAM"]},
        override: {"TeamIdentifier": ["TEAM"]},
    }

    with patch.object(preflight, "parse_mobileprovision", side_effect=lambda p: plists[p]):
        results = preflight.check_profile_contents(cfg, app=_app(profile_override=override))

    assert _row(results, "profile team id matches config").ok is True


# ---------------------------------------------------------------------------
# False pass
# ---------------------------------------------------------------------------


def test_an_auto_profile_app_gets_no_device_row_off_the_wildcard(tmp_path: Path) -> None:
    """The wildcard lists the phone. The profile that will sign does not exist yet.

    Reporting "device in provisioning profile: OK" here is a claim about a file
    the build never opens.
    """
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")
    cfg.app_store_connect = _asc(tmp_path)

    with patch.object(preflight, "list_connected_devices", return_value=[UDID]), \
         patch.object(
             preflight, "parse_mobileprovision", return_value={"ProvisionedDevices": [UDID]}
         ):
        results = preflight.check_devices(cfg, app=_app(auto_profile=True))

    assert _row(results, DEVICE_ROW) is None
    assert _row(results, "connected ios device(s)").ok is True


def test_a_cached_auto_profile_is_the_file_the_device_check_reads(tmp_path: Path) -> None:
    """Once the ASC profile is on disk it is checkable — and it disagrees.

    The disagreement is real and the row is about the cached file, not the
    wildcard. What it is *not* is a build-stopping failure: an auto-provisioned
    profile is a cache, the connected phone is one of the UDIDs stage 4
    re-provisions for, and `ensure_profile_for_bundle` replaces the file then.
    Failing here aborted the build before the only code that could fix it — a
    dead end with no exit that wasn't `rm` under ~/.config.
    """
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")
    cfg.app_store_connect = _asc(tmp_path)
    cached = tmp_path / "profiles" / "com_bravo_x.mobileprovision"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    plists = {
        cfg.signing.profile: {"ProvisionedDevices": [UDID]},
        cached: {"ProvisionedDevices": ["00008130-SOMEOTHERPHONE"]},
    }

    with patch.object(preflight, "list_connected_devices", return_value=[UDID]), \
         patch.object(preflight, "parse_mobileprovision", side_effect=lambda p: plists[p]):
        results = preflight.check_devices(cfg, app=_app(auto_profile=True))

    row = _row(results, DEVICE_ROW)
    assert row is not None
    # The cached file is what was read: the wildcard lists UDID, so a check
    # against it would have had nothing to say.
    assert row.ok is True
    assert "re-provisions it at sign time" in row.detail
    assert UDID in row.detail


def test_a_phone_the_config_never_names_still_fails_the_device_check(
    tmp_path: Path,
) -> None:
    """The limit of the self-heal, and why it is conditional.

    Stage 4 re-provisions for the UDIDs `app_store_connect.device_udids` (or
    `default_device_udid`) names. A phone in none of them is not about to be
    added, so passing it here would only move the failure to zsign — further
    from the setting that explains it.
    """
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")
    cfg.app_store_connect = _asc(tmp_path)
    cached = tmp_path / "profiles" / "com_bravo_x.mobileprovision"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    stranger = "00008130-NOTINCONFIG"

    with patch.object(preflight, "list_connected_devices", return_value=[stranger]), \
         patch.object(
             preflight,
             "parse_mobileprovision",
             return_value={"ProvisionedDevices": ["00008130-SOMEOTHERPHONE"]},
         ):
        results = preflight.check_devices(cfg, app=_app(auto_profile=True))

    row = _row(results, DEVICE_ROW)
    assert row is not None and row.ok is False
    assert "Apple Developer portal" in row.hint


def test_a_profile_that_lists_the_phone_in_another_case_still_matches(
    tmp_path: Path,
) -> None:
    """ASC normalises UDIDs to lower case in places; config.yaml does not.

    The membership test here was exact while the `configured`/`renewing` pair
    three lines below it, and `cache_problem` on both sides, are
    case-insensitive. So a case mismatch fell through to the self-heal and
    promised "App Store Connect re-provisions it at sign time" about a refresh
    that never happens — `cache_problem` reads the same file, matches the phone,
    and tells stage 4 the cache is fine. On an app whose phone is not in
    `device_udids` it went the other way and hard-failed a build whose profile
    does list the device.
    """
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")
    override = tmp_path / "bravo.mobileprovision"
    override.write_bytes(b"x")

    with patch.object(preflight, "list_connected_devices", return_value=[UDID.upper()]), \
         patch.object(
             preflight,
             "parse_mobileprovision",
             return_value={"ProvisionedDevices": [UDID.lower()]},
         ):
        results = preflight.check_devices(cfg, app=_app(profile_override=override))

    row = _row(results, DEVICE_ROW)
    assert row.ok is True
    assert "re-provisions" not in row.detail, (
        "reported a refresh for a phone the profile already lists"
    )


def test_the_device_check_reads_an_override_not_the_wildcard(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")
    override = tmp_path / "bravo.mobileprovision"
    override.write_bytes(b"x")
    plists = {
        cfg.signing.profile: {"ProvisionedDevices": [UDID]},
        override: {"ProvisionedDevices": ["00008130-SOMEOTHERPHONE"]},
    }

    with patch.object(preflight, "list_connected_devices", return_value=[UDID]), \
         patch.object(preflight, "parse_mobileprovision", side_effect=lambda p: plists[p]):
        results = preflight.check_devices(cfg, app=_app(profile_override=override))

    assert _row(results, DEVICE_ROW).ok is False


def test_an_auto_profile_app_with_no_portal_config_fails_the_profile_row(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    cfg.signing.profile.write_bytes(b"x")  # a wildcard exists — it is not the answer

    row = _row(preflight.check_signing_files(cfg, app=_app(auto_profile=True)), PROFILE_ROW)

    assert row.ok is False
    assert "app_store_connect" in row.detail


def test_an_uncached_auto_profile_is_not_a_failure(tmp_path: Path) -> None:
    """Stage 4 generates it; a red row here would refuse a build that works."""
    cfg = _cfg(tmp_path)
    cfg.app_store_connect = _asc(tmp_path)

    row = _row(preflight.check_signing_files(cfg, app=_app(auto_profile=True)), PROFILE_ROW)

    assert row.ok is True
    assert "com.bravo.*" in row.detail


def test_preflight_never_provisions_while_resolving(tmp_path: Path) -> None:
    """`--check` must not register anything in the user's developer account."""
    cfg = _cfg(tmp_path)
    cfg.app_store_connect = _asc(tmp_path)

    def boom(*_args, **_kwargs):
        raise AssertionError("pre-flight reached the Apple Developer Portal")

    with patch.object(signer.apple_portal, "ASCClient", side_effect=boom), \
         patch.object(signer.apple_portal, "ensure_profile_for_bundle", side_effect=boom), \
         patch.object(signer.apple_portal, "read_p12_cert_serial", side_effect=boom), \
         patch.object(preflight, "list_connected_devices", return_value=[UDID]):
        preflight.run_all_checks(cfg, app=_app(auto_profile=True))

    assert not (tmp_path / "profiles").exists()


def test_the_build_hands_its_app_to_preflight(tmp_path: Path) -> None:
    """The cli.py half of the fix.

    App-awareness in `run_all_checks` is inert unless `_build_one` passes the
    app it is building. Asserts on the call rather than on a check result so it
    stays true through any restructuring of the build's error handling.
    """
    from graft_ipa import cli as cli_mod

    app = _app(profile_override=tmp_path / "bravo.mobileprovision")
    cfg = _cfg(tmp_path)
    cfg.apps = {"bravo": app}
    calls: list[dict] = []

    def record(_cfg, **kwargs):
        calls.append(kwargs)
        return [CheckResult(name="cyan", ok=False, hint="stop here")]

    with patch.object(cli_mod, "run_all_checks", side_effect=record):
        with pytest.raises(PreflightError):
            cli_mod._build_one(
                cfg=cfg,
                log=BuildLog(tmp_path / "log.sqlite"),
                app_name="bravo",
                dry_run=False,
                skip_install=True,
                skip_decrypt_check=True,
                verbose=False,
                device=None,
            )

    assert calls and calls[0]["app"] is app
