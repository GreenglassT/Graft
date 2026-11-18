from __future__ import annotations

from pathlib import Path

from graft_ipa.config import Config, SigningConfig
from graft_ipa.preflight import run_all_checks


def _cfg(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=tmp_path / "profile.mobileprovision",
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={},
    )


def _names(results) -> set[str]:
    return {r.name for r in results}


def test_livecontainer_skips_signing_and_device_when_not_installing(tmp_path: Path) -> None:
    names = _names(
        run_all_checks(_cfg(tmp_path), delivery="livecontainer", will_install=False)
    )
    # injection + decrypt verification still needed
    assert "cyan" in names
    assert "otool" in names
    # signing identity is irrelevant to a livecontainer build
    assert "zsign" not in names
    assert "ideviceinstaller" not in names
    assert "signing.cert (.p12)" not in names
    assert "signing profile (.mobileprovision)" not in names
    assert ".p12 password" not in names
    # not pushing to a device → no device required
    assert "connected ios device(s)" not in names
    assert "idevice_id" not in names


def test_livecontainer_requires_device_when_installing(tmp_path: Path) -> None:
    names = _names(
        run_all_checks(_cfg(tmp_path), delivery="livecontainer", will_install=True)
    )
    assert "connected ios device(s)" in names
    # Found through pymobiledevice3, so no libimobiledevice tool is needed.
    assert "idevice_id" not in names
    assert "ideviceinstaller" not in names
    # still no signing requirements
    assert "zsign" not in names
    assert ".p12 password" not in names


def test_sideload_default_requires_full_signing_identity(tmp_path: Path) -> None:
    # Default args must preserve the original mode-agnostic behaviour so the
    # global `--check` command and existing callers are unaffected.
    names = _names(run_all_checks(_cfg(tmp_path)))
    for required in (
        "cyan",
        "zsign",
        "ideviceinstaller",
        "otool",
        "signing.cert (.p12)",
        "signing profile (.mobileprovision)",
        ".p12 password",
        "connected ios device(s)",
    ):
        assert required in names, required
