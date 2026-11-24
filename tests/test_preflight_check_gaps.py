"""`--check` gaps: the REPLACEME team id."""

from __future__ import annotations

from pathlib import Path

from graft_ipa import preflight
from graft_ipa.config import Config, SigningConfig


def _cfg(tmp_path: Path, team_id: str) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="GRAFT_P12_PASS_TEST_UNSET",
            profile=tmp_path / "profile.mobileprovision",
            team_id=team_id,
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={},
    )


def _row(results, name):
    return next(r for r in results if r.name == name)


def test_replaceme_team_id_is_a_failure_with_a_fix_hint(tmp_path: Path) -> None:
    results = preflight.check_signing_files(_cfg(tmp_path, "REPLACEME"))
    row = _row(results, preflight.TEAM_ID_CHECK_NAME)
    assert row.ok is False
    assert "graft --set-settings --signing-team-id <TEAM>" in row.hint


def test_a_real_team_id_passes_the_team_id_check(tmp_path: Path) -> None:
    results = preflight.check_signing_files(_cfg(tmp_path, "TEAM123456"))
    assert _row(results, preflight.TEAM_ID_CHECK_NAME).ok is True
