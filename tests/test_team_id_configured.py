"""One definition of "the team id is configured": `Config.team_id`.

It is None for a free Apple ID, a blank or whitespace team id, and the
`REPLACEME` placeholder `--init` writes when it cannot read a profile. Every
reader asks it: the `signing.team_id` row of `--check`, the `.p12 password`
row's Keychain lookup, the signer's, and `--set-p12-password`, which used to
store the password at `p12-REPLACEME` where nothing would ever look again
once the real team id was set. A YAML-null `team_id:` used to load as the
string "None" and pass the row.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import keychain, preflight, signer
from graft_ipa.config import TEAM_ID_PLACEHOLDER, load_config

TEAM = "ABCDE12345"


def _paid(path: Path, team) -> Path:
    raw = yaml.safe_load(path.read_text())
    raw["signing"]["team_id"] = team
    raw["signing"]["cert_password_env"] = "GRAFT_TEST_UNSET_P12_PASS"
    path.write_text(yaml.safe_dump(raw))
    return path


def _row(cfg, name: str) -> preflight.CheckResult:
    return next(r for r in preflight.check_signing_files(cfg) if r.name == name)


@pytest.mark.parametrize("team", ["", " ", "\t", TEAM_ID_PLACEHOLDER, f" {TEAM_ID_PLACEHOLDER} "])
def test_an_unset_team_fails_the_team_row(temp_config_env: Path, team: str) -> None:
    row = _row(load_config(_paid(temp_config_env, team)), preflight.TEAM_ID_CHECK_NAME)

    assert row.ok is False


def test_a_null_team_id_is_missing_not_the_string_none(temp_config_env: Path) -> None:
    path = _paid(temp_config_env, TEAM)
    path.write_text(path.read_text().replace(f"team_id: {TEAM}", "team_id:"))
    cfg = load_config(path)

    assert cfg.signing.team_id == ""
    assert cfg.team_id is None
    row = _row(cfg, preflight.TEAM_ID_CHECK_NAME)
    assert (row.ok, row.detail) == (False, "(missing)")


def test_a_real_team_passes_the_team_row(temp_config_env: Path) -> None:
    row = _row(load_config(_paid(temp_config_env, TEAM)), preflight.TEAM_ID_CHECK_NAME)

    assert (row.ok, row.detail) == (True, TEAM)


def _set_p12_password(password: str = "hunter2"):
    return CliRunner().invoke(cli_mod.app, ["--set-p12-password"], input=f"{password}\n")


@pytest.mark.parametrize("team", [TEAM_ID_PLACEHOLDER, "", "  "])
def test_set_p12_password_refuses_until_the_team_is_set(
    temp_config_env: Path, team: str
) -> None:
    _paid(temp_config_env, team)

    result = _set_p12_password()
    said = " ".join(result.output.split())

    assert result.exit_code != 0, result.output
    assert "set signing.team_id first (`graft --set-settings --signing-team-id <TEAM>`)" in said
    assert keychain.read_p12_password(TEAM_ID_PLACEHOLDER) is None
    assert keychain.read_p12_password(team) is None


def test_set_p12_password_stores_under_the_team_and_the_check_reads_it_there(
    temp_config_env: Path,
) -> None:
    _paid(temp_config_env, TEAM)

    result = _set_p12_password()

    assert result.exit_code == 0, result.output
    assert keychain.read_p12_password(TEAM) == "hunter2"
    cfg = load_config(temp_config_env)
    row = _row(cfg, ".p12 password")
    assert (row.ok, row.detail) == (True, "keychain entry present")
    assert signer.resolve_p12_password(cfg) == "hunter2"


def test_a_padded_team_is_one_key_for_the_writer_and_the_readers(temp_config_env: Path) -> None:
    _paid(temp_config_env, f" {TEAM} ")

    assert _set_p12_password().exit_code == 0
    cfg = load_config(temp_config_env)

    assert keychain.read_p12_password(TEAM) == "hunter2"
    assert _row(cfg, ".p12 password").ok is True
    assert signer.resolve_p12_password(cfg) == "hunter2"


@pytest.mark.parametrize(("team", "ok"), [(f" {TEAM} ", True), (TEAM_ID_PLACEHOLDER, False)])
def test_the_profile_team_is_compared_with_the_configured_one(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, team: str, ok: bool
) -> None:
    cfg = load_config(_paid(temp_config_env, team))
    cfg.signing.profile.parent.mkdir(parents=True, exist_ok=True)
    cfg.signing.profile.write_bytes(b"x")
    monkeypatch.setattr(preflight, "parse_mobileprovision",
                        lambda p: {"TeamIdentifier": [TEAM, TEAM_ID_PLACEHOLDER]})

    [row] = [r for r in preflight.check_profile_contents(cfg)
             if r.name == "profile team id matches config"]

    assert row.ok is ok


def test_a_password_stored_under_the_placeholder_is_not_read_as_present(
    temp_config_env: Path,
) -> None:
    """What the old `--set-p12-password` left behind: `--check` said the
    password was there, and it was, under a team id no build would use."""
    keychain.write_p12_password(TEAM_ID_PLACEHOLDER, "hunter2")
    cfg = load_config(_paid(temp_config_env, TEAM_ID_PLACEHOLDER))

    assert _row(cfg, ".p12 password").ok is False
    assert signer.resolve_p12_password(cfg) is None
