"""A profile with hours left is not an expired profile.

`days_left = (expiry - now).days` truncated a sub-24h timedelta to 0, and the
check was `days_left > 0`. A profile expiring at 18:00 when it is 09:00 came
back FAIL, `_build_one` raised PreflightError before stage 2 and `--check`
exited 3 — so every sideload build was refused for the profile's entire last
day, on a profile zsign would have signed with.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import preflight
from graft_ipa.config import Config, SigningConfig
from graft_ipa.errors import PreflightError


def _cfg(tmp_path: Path) -> Config:
    profile = tmp_path / "wildcard.mobileprovision"
    profile.write_bytes(b"x")
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=profile,
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={},
    )


# Padded so the microseconds that elapse between building the expiry here and
# `datetime.now(UTC)` inside the check cannot tip a boundary (9h becomes 8h59m).
PAD = timedelta(seconds=30)


def _expiry_row(cfg: Config, expiry: datetime):
    plist = {"TeamIdentifier": ["TEAM"], "ExpirationDate": expiry}
    with patch.object(preflight, "parse_mobileprovision", return_value=plist):
        results = preflight.check_profile_contents(cfg)
    return next(r for r in results if r.name == "profile expiry")


def test_a_profile_with_nine_hours_left_passes(tmp_path: Path) -> None:
    """The reported case: expires 18:00, it is 09:00."""
    row = _expiry_row(_cfg(tmp_path), datetime.now(UTC) + timedelta(hours=9) + PAD)

    assert row.ok is True
    assert "expires in 9 hours" in row.detail


def test_a_profile_with_nine_hours_left_does_not_abort_the_build(tmp_path: Path) -> None:
    """The consequence that actually bit: assert_all_passed is what raises."""
    cfg = _cfg(tmp_path)
    plist = {"TeamIdentifier": ["TEAM"], "ExpirationDate": datetime.now(UTC) + timedelta(hours=9) + PAD}
    with patch.object(preflight, "parse_mobileprovision", return_value=plist):
        results = preflight.check_profile_contents(cfg)

    preflight.assert_all_passed(results)  # must not raise


def test_a_profile_with_forty_five_minutes_left_still_passes(tmp_path: Path) -> None:
    row = _expiry_row(_cfg(tmp_path), datetime.now(UTC) + timedelta(minutes=45) + PAD)

    assert row.ok is True
    assert "expires in 45 minutes" in row.detail


def test_an_expired_profile_still_fails(tmp_path: Path) -> None:
    row = _expiry_row(_cfg(tmp_path), datetime.now(UTC) - timedelta(hours=2) - PAD)

    assert row.ok is False
    assert "expired 2 hours ago" in row.detail
    assert "regenerate" in row.hint


def test_an_expired_profile_aborts_the_build(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    plist = {"TeamIdentifier": ["TEAM"], "ExpirationDate": datetime.now(UTC) - timedelta(hours=2) - PAD}
    with patch.object(preflight, "parse_mobileprovision", return_value=plist):
        results = preflight.check_profile_contents(cfg)

    with pytest.raises(PreflightError):
        preflight.assert_all_passed(results)


def test_a_healthy_profile_reads_in_days(tmp_path: Path) -> None:
    row = _expiry_row(_cfg(tmp_path), datetime.now(UTC) + timedelta(days=30, hours=5))

    assert row.ok is True
    assert "expires in 30 days" in row.detail


def test_a_naive_expiry_is_still_treated_as_utc(tmp_path: Path) -> None:
    """plistlib hands back naive datetimes; the old code coped and so must this."""
    naive = (datetime.now(UTC) + timedelta(hours=9) + PAD).replace(tzinfo=None)
    row = _expiry_row(_cfg(tmp_path), naive)

    assert row.ok is True
    assert "expires in 9 hours" in row.detail
