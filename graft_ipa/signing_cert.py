"""What the signing certificate says about itself: its name, its team and
when it runs out.

The paid build's signing line, `✓ team=… subject=… expiry=…`, read these
by regex out of zsign's output, and zsign prints none of them in a form the
regexes matched: it says `>>> SubjectCN:` where they wanted `Subject:`, and
prints no expiry at all when it signs. Every paid build read `subject=?
expiry=?`. The certificate in the .p12 has all three, and the run has just
opened it with the same password to give zsign its private copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import p12 as p12_mod


@dataclass(frozen=True)
class CertFacts:
    #: The subject's common name: "Apple Development: Name (ID)".
    subject: str | None
    #: The subject's organizational unit, which Apple sets to the team ID.
    team_id: str | None
    #: The day the certificate stops being valid, YYYY-MM-DD in UTC: how the
    #: setup's summary gives a date ("expires 2027-11-17").
    expiry: str | None


NO_FACTS = CertFacts(subject=None, team_id=None, expiry=None)


def read(p12_path: Path, password: str) -> CertFacts:
    """The facts of the certificate in `p12_path`, opened with `password`.

    Never raises: they only label a line of output, and a signing run whose
    identity zsign has just used is not failed over a label. A .p12 that is
    missing or won't open gives `NO_FACTS`.
    """
    try:
        _, cert, _ = p12_mod.load_key_and_certificates(p12_path.read_bytes(), password.encode())
    except (OSError, ValueError):
        return NO_FACTS
    if cert is None:
        return NO_FACTS
    from cryptography.x509.oid import NameOID

    def first(oid) -> str | None:
        values = cert.subject.get_attributes_for_oid(oid)
        if not values:
            return None
        return str(values[0].value).strip() or None

    return CertFacts(
        subject=first(NameOID.COMMON_NAME),
        team_id=first(NameOID.ORGANIZATIONAL_UNIT_NAME),
        expiry=cert.not_valid_after_utc.date().isoformat(),
    )
