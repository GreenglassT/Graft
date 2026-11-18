from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import mobileprovision, signer
from graft_ipa.apple_portal import ProvisionResult
from graft_ipa.config import (
    AppConfig,
    AppStoreConnectConfig,
    Config,
    CyanOptions,
    SigningConfig,
)
from graft_ipa.errors import SigningError
from graft_ipa.signer import run_zsign


def make_cfg(tmp_path: Path) -> Config:
    cert = tmp_path / "cert.p12"
    cert.write_bytes(b"x")
    profile = tmp_path / "profile.mobileprovision"
    profile.write_bytes(b"x")
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=cert,
            cert_password_env="TEST_P12_PASS",
            profile=profile,
            team_id="ABC1234567",
        ),
        cache_dir=tmp_path,
        built_dir=tmp_path,
        tweaks_dir=tmp_path,
        state_dir=tmp_path,
        apps={},
    )


def test_missing_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(tmp_path)
    monkeypatch.delenv("TEST_P12_PASS", raising=False)
    with pytest.raises(SigningError) as exc:
        run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "TEST_P12_PASS" in str(exc.value)


def test_dry_run_redacts_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    res = run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", dry_run=True)
    assert "supersecret" not in res.stdout
    assert "<TEST_P12_PASS>" in res.stdout


def test_wrong_password_hint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "wrong")
    fake = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="mac verify failure"
    )
    with patch("subprocess.run", return_value=fake):
        with pytest.raises(SigningError) as exc:
            run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "password" in str(exc.value).lower()
    # The variable the config names, not a hard-coded default: a config that
    # says `cert_password_env: TEST_P12_PASS` must be told to check that one.
    assert "$TEST_P12_PASS" in exc.value.hint


# ---------------------------------------------------------------------------
# --dry-run must not write to the Apple Developer Portal.
#
# `resolve_profile_for_app` ran above the dry_run guard, so an `auto_profile`
# app built with --dry-run reached `apple_portal.ensure_profile_for_bundle`:
# it registered a permanent Bundle ID in the user's Apple developer account
# when none matched, consumed a provisioning-profile slot, and left a cached
# .mobileprovision on disk. Nothing in the output said a portal write happened.
# ---------------------------------------------------------------------------


def _asc_config(tmp_path: Path) -> AppStoreConnectConfig:
    return AppStoreConnectConfig(
        key_id="KEYID12345",
        issuer_id="1234-issuer",
        key_file=tmp_path / "AuthKey.p8",
        device_udids=["00008130-001A2B3C0123456D"],
        profile_cache_dir=tmp_path / "profiles",
    )


def _auto_profile_app() -> AppConfig:
    return AppConfig(
        name="example",
        bundle_id="com.example.app",
        preferred_version="latest",
        tweaks=[],
        cyan=CyanOptions(),
        auto_profile=True,
    )


def _portal_tripwires():
    """Patch every door into the portal so touching one fails the test."""

    def boom(*_args, **_kwargs):
        raise AssertionError("--dry-run reached the Apple Developer Portal")

    return (
        patch.object(signer.apple_portal, "read_p12_cert_serial", side_effect=boom),
        patch.object(signer.apple_portal, "ASCClient", side_effect=boom),
        patch.object(signer.apple_portal, "ensure_profile_for_bundle", side_effect=boom),
    )


def test_dry_run_does_not_touch_the_portal_for_an_auto_profile_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path)
    cfg.app_store_connect = _asc_config(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")

    serial, client, ensure = _portal_tripwires()
    with serial, client, ensure:
        res = run_zsign(
            cfg,
            tmp_path / "in.ipa",
            tmp_path / "out.ipa",
            app=_auto_profile_app(),
            dry_run=True,
        )

    # ...and it left nothing behind on disk either.
    assert not (tmp_path / "profiles").exists()
    assert "supersecret" not in res.stdout


def test_dry_run_still_reports_the_profile_it_would_have_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silently skipping resolution would hide a real misconfiguration.

    The filename is spelled out rather than derived: it is the exact file
    `ensure_profile_for_bundle` caches `com.example.*` to, so this pins the two
    to the same name instead of restating whatever the implementation does.
    """
    cfg = make_cfg(tmp_path)
    cfg.app_store_connect = _asc_config(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")

    serial, client, ensure = _portal_tripwires()
    with serial, client, ensure:
        res = run_zsign(
            cfg,
            tmp_path / "in.ipa",
            tmp_path / "out.ipa",
            app=_auto_profile_app(),
            dry_run=True,
        )

    expected = tmp_path / "profiles" / "com_example_x.mobileprovision"
    assert str(expected) in res.stdout
    assert "com.example.*" in res.stdout
    assert "not cached yet" in res.stdout


def test_dry_run_still_fails_on_an_auto_profile_app_with_no_portal_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A misconfiguration a real run would hit still has to surface."""
    cfg = make_cfg(tmp_path)  # no app_store_connect section
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")

    with pytest.raises(SigningError) as exc:
        run_zsign(
            cfg,
            tmp_path / "in.ipa",
            tmp_path / "out.ipa",
            app=_auto_profile_app(),
            dry_run=True,
        )
    assert "app_store_connect" in str(exc.value)


def test_dry_run_still_fails_on_a_missing_per_app_profile_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    app = AppConfig(
        name="bravo",
        bundle_id="com.bravo.app",
        preferred_version="latest",
        tweaks=[],
        cyan=CyanOptions(),
        profile_override=tmp_path / "nope.mobileprovision",
    )

    with pytest.raises(SigningError) as exc:
        run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", app=app, dry_run=True)
    assert "nope.mobileprovision" in str(exc.value)


def test_dry_run_names_a_cached_auto_profile_it_would_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path)
    cfg.app_store_connect = _asc_config(tmp_path)
    cached = tmp_path / "profiles" / "com_example_x.mobileprovision"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")

    serial, client, ensure = _portal_tripwires()
    with serial, client, ensure, _decoded_profile(devices=["00008130-001A2B3C0123456D"]):
        res = run_zsign(
            cfg,
            tmp_path / "in.ipa",
            tmp_path / "out.ipa",
            app=_auto_profile_app(),
            dry_run=True,
        )

    assert "already cached" in res.stdout
    assert str(cached) in res.stdout


def _decoded_profile(*, devices: list[str], expires_in_days: int = 300):
    """Stand in for `security cms -D`, so no test shells out to macOS."""
    from datetime import UTC, datetime, timedelta

    plist = {
        "ProvisionedDevices": devices,
        "ExpirationDate": datetime.now(UTC) + timedelta(days=expires_in_days),
    }
    return patch.object(mobileprovision, "decode", return_value=plist)


def test_dry_run_says_the_real_run_would_reprovision_when_the_cache_lacks_the_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real repro (2026-11-17): a new iPad, a cached profile that did not list
    it, and a dry run that said "a real run reuses this file" — while the real
    run correctly re-provisioned. Same offline question, same answer."""
    cfg = make_cfg(tmp_path)
    cfg.app_store_connect = _asc_config(tmp_path)  # device 00008130-001A2B3C0123456D
    cached = tmp_path / "profiles" / "com_example_x.mobileprovision"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")

    serial, client, ensure = _portal_tripwires()
    with serial, client, ensure, _decoded_profile(devices=["00008030-OLDPHONE"]):
        res = run_zsign(
            cfg,
            tmp_path / "in.ipa",
            tmp_path / "out.ipa",
            app=_auto_profile_app(),
            dry_run=True,
        )

    assert "a real run reuses this file" not in res.stdout
    assert "a real run would try to re-provision" in res.stdout
    # The reason is `cache_problem`'s own sentence, naming the missing device.
    assert "does not list 00008130-001A2B3C0123456D" in res.stdout


def test_dry_run_says_the_real_run_would_reprovision_an_expired_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_cfg(tmp_path)
    cfg.app_store_connect = _asc_config(tmp_path)
    cached = tmp_path / "profiles" / "com_example_x.mobileprovision"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"x")
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")

    serial, client, ensure = _portal_tripwires()
    with serial, client, ensure, _decoded_profile(
        devices=["00008130-001A2B3C0123456D"], expires_in_days=-2
    ):
        res = run_zsign(
            cfg,
            tmp_path / "in.ipa",
            tmp_path / "out.ipa",
            app=_auto_profile_app(),
            dry_run=True,
        )

    assert "a real run would try to re-provision" in res.stdout
    assert "expired" in res.stdout


def test_a_real_run_still_auto_provisions_through_the_portal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction: fixing --dry-run must not disable auto-provisioning."""
    cfg = make_cfg(tmp_path)
    cfg.app_store_connect = _asc_config(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "supersecret")
    provisioned = tmp_path / "profiles" / "com_example_x.mobileprovision"
    provisioned.parent.mkdir(parents=True)
    provisioned.write_bytes(b"x")
    result = ProvisionResult(
        profile_path=provisioned,
        profile_id="PROFILE1",
        bundle_id_pattern="com.example.*",
        refreshed=True,
    )
    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch.object(signer.apple_portal, "read_p12_cert_serial", return_value="AABB"), \
         patch.object(signer.apple_portal, "ASCClient"), \
         patch.object(
             signer.apple_portal, "ensure_profile_for_bundle", return_value=result
         ) as ensure, \
         patch("subprocess.run", side_effect=fake_run):
        run_zsign(
            cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", app=_auto_profile_app()
        )

    assert ensure.call_count == 1
    assert captured["cmd"][captured["cmd"].index("-m") + 1] == str(provisioned)
