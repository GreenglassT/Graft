"""The signing line's subject, expiry and team come from the certificate.

From a device test of the release build: every paid build printed
`✓ team=<team> subject=? expiry=?`. `run_zsign` read those by regex out of
zsign's output, and zsign prints none of them in a form the regexes match:
it says `>>> SubjectCN:` (not `Subject:`) and, when it signs, no expiry at
all. They are read from the certificate in the .p12 now, which the run has
just opened with the same password.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.serialization import pkcs12

from graft_ipa import mobileprovision, signer, signing_cert, tools
from graft_ipa.config import Config, SigningConfig

from .signing_fixtures import COMMON_NAME, PASSWORD, make_identity
from .tool_fakes import fake_path

CERT_TEAM = "QRSTU67890"
#: What zsign 1.1.x prints when it signs (its `>>> ` lines), minus the paths:
#: a team id, a `SubjectCN:` the old `Subject[:\s]+` never matched, no expiry.
ZSIGN_SAYS = (
    ">>> Signing: \tPayload/Example.app ...\n"
    ">>> AppName: \tExample\n"
    ">>> BundleId: \tcom.example.app\n"
    ">>> Version: \t1\n"
    ">>> TeamId: \tABCDE12345\n"
    ">>> SubjectCN: \tsomething zsign made up\n"
    ">>> ReadCache: \tNO\n"
    ">>> Signed OK!\n"
)


@pytest.fixture(autouse=True)
def _zsign_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    monkeypatch.setenv("TEST_P12_PASS", PASSWORD)


def _cfg(tmp_path: Path, *, unit: str | None = CERT_TEAM) -> Config:
    ident = make_identity(tmp_path, unit=unit)
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=ident.p12, cert_password_env="TEST_P12_PASS", profile=ident.profile,
            team_id="ABCDE12345",
        ),
        cache_dir=tmp_path, built_dir=tmp_path, tweaks_dir=tmp_path, state_dir=tmp_path,
        apps={},
    )


def _sign(cfg: Config, tmp_path: Path, *, stdout: str = "", stderr: str = ""):
    def zsign(cmd, **_):
        Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=stderr)

    with patch.object(signer.subprocess, "run", side_effect=zsign):
        return signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")


def _cert_expiry(cfg: Config) -> datetime:
    _, cert, _ = pkcs12.load_key_and_certificates(
        cfg.signing.cert.read_bytes(), PASSWORD.encode()
    )
    return cert.not_valid_after_utc


def test_a_zsign_that_says_nothing_still_gets_the_certificates_name_and_expiry(
    tmp_path: Path,
) -> None:
    cfg = _cfg(tmp_path)
    result = _sign(cfg, tmp_path)

    assert result.cert_subject == COMMON_NAME
    assert result.expiry == _cert_expiry(cfg).date().isoformat()


def test_zsigns_own_lines_are_not_what_the_summary_reports(tmp_path: Path) -> None:
    """What zsign 1.1.x really prints: neither its `SubjectCN:` nor a missing
    expiry reaches the summary, and the certificate's date is not the
    profile's (the fixture's profile expires 100 days before its cert)."""
    cfg = _cfg(tmp_path)
    result = _sign(cfg, tmp_path, stdout=ZSIGN_SAYS)

    assert result.cert_subject == COMMON_NAME
    assert result.expiry == _cert_expiry(cfg).date().isoformat()
    profile_expiry = mobileprovision.expiration(mobileprovision.decode(cfg.signing.profile))
    assert result.expiry != profile_expiry.astimezone(UTC).date().isoformat()


def test_the_team_is_the_certificates_ou(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    result = _sign(cfg, tmp_path, stdout=ZSIGN_SAYS)

    assert result.team_id == CERT_TEAM


def test_the_team_is_the_certificates_ou_when_zsign_says_nothing(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert _sign(cfg, tmp_path).team_id == CERT_TEAM


def test_a_certificate_with_no_ou_falls_back_to_zsigns_team_line(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, unit=None)
    result = _sign(cfg, tmp_path, stdout=ZSIGN_SAYS)

    assert result.team_id == "ABCDE12345"
    assert result.cert_subject == COMMON_NAME


def test_a_dry_run_still_reports_nothing_about_the_certificate(tmp_path: Path) -> None:
    """A dry run renders the command and opens no .p12; it stays that way."""
    cfg = _cfg(tmp_path)
    with patch.object(signing_cert, "read", side_effect=AssertionError("opened the .p12")):
        result = signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", dry_run=True)

    assert (result.team_id, result.cert_subject, result.expiry) == (None, None, None)


def test_reading_the_facts_never_raises_on_a_p12_that_will_not_open(tmp_path: Path) -> None:
    ident = make_identity(tmp_path)
    assert signing_cert.read(ident.p12, "not the password") == signing_cert.CertFacts(
        subject=None, team_id=None, expiry=None
    )
    assert signing_cert.read(tmp_path / "missing.p12", PASSWORD) == signing_cert.CertFacts(
        subject=None, team_id=None, expiry=None
    )
