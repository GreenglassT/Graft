"""Loading an Apple-exported .p12 prints nothing to stderr.

Keychain Access exports a BER- rather than DER-encoded .p12, and cryptography
warns "PKCS#12 bundle could not be parsed as DER, falling back to parsing as
BER" on every load of one: a UserWarning on stderr in every paid build, where
the Mac app reads graft's error lines. Graft silences that one warning, by its
message, at every place it loads a .p12, and lets every other warning through.

pytest records warnings itself, so in-process they never reach `capsys`; the
in-process tests record them instead, and one test runs the load in a child
Python to see its stderr.
"""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from collections.abc import Callable
from pathlib import Path

import pytest

from graft_ipa import apple_portal, lc_sign, p12

from .test_lc_sign import _make_p12

PASSWORD = "p12-password"
BER_WARNING = "could not be parsed as DER"


def _to_ber(der: bytes) -> bytes:
    """`der` with its outer SEQUENCE re-encoded in BER's indefinite-length form.

    The MAC covers the auth-safe's contents only, so the bundle still opens
    with the same password; cryptography's DER parser rejects it and falls
    back to BER, as it does for a Keychain Access export.
    """
    assert der[0] == 0x30
    if der[1] < 0x80:
        start, length = 2, der[1]
    else:
        n = der[1] & 0x7F
        start, length = 2 + n, int.from_bytes(der[2:2 + n], "big")
    assert start + length == len(der)
    return b"\x30\x80" + der[start:] + b"\x00\x00"


def _ber_p12(tmp_path: Path) -> tuple[Path, str]:
    path, sha1 = _make_p12(tmp_path, PASSWORD)
    path.write_bytes(_to_ber(path.read_bytes()))
    return path, sha1


def _reencrypt(path: Path, tmp_path: Path, sha1: str) -> None:
    dest = tmp_path / "copy.p12"
    p12.write_reencrypted(path, PASSWORD, dest, "one-time")
    assert lc_sign.p12_identity(dest, "one-time") == sha1


def _identity(path: Path, tmp_path: Path, sha1: str) -> None:
    assert lc_sign.p12_identity(path, PASSWORD) == sha1


def _serial(path: Path, tmp_path: Path, sha1: str) -> None:
    assert apple_portal.read_p12_cert_serial(path, PASSWORD) == "1"


LOADERS: dict[str, Callable[[Path, Path, str], None]] = {
    "p12.write_reencrypted": _reencrypt,
    "lc_sign.p12_identity": _identity,
    "apple_portal.read_p12_cert_serial": _serial,
}


def _ber_warnings(seen: list[warnings.WarningMessage]) -> list[str]:
    return [str(w.message) for w in seen if BER_WARNING in str(w.message)]


def test_the_ber_fixture_does_make_cryptography_warn(tmp_path: Path) -> None:
    # Guards the tests below: they would pass against a fixture that no
    # longer provokes the warning.
    from cryptography.hazmat.primitives.serialization import pkcs12

    path, _ = _ber_p12(tmp_path)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        _, cert, _ = pkcs12.load_key_and_certificates(path.read_bytes(), PASSWORD.encode())
    assert cert is not None
    assert _ber_warnings(seen)


@pytest.mark.parametrize("loader", LOADERS.values(), ids=LOADERS.keys())
def test_loading_a_ber_p12_raises_no_ber_warning(
    tmp_path: Path, loader: Callable[[Path, Path, str], None]
) -> None:
    path, sha1 = _ber_p12(tmp_path)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        loader(path, tmp_path, sha1)
    assert _ber_warnings(seen) == []


@pytest.mark.parametrize("loader", LOADERS.values(), ids=LOADERS.keys())
def test_other_warnings_from_a_p12_load_still_show(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    loader: Callable[[Path, Path, str], None],
) -> None:
    from cryptography.hazmat.primitives.serialization import pkcs12

    real = pkcs12.load_key_and_certificates

    def load(data: bytes, password: bytes | None, *args, **kwargs):
        warnings.warn("some other PKCS#12 notice", UserWarning, stacklevel=2)
        return real(data, password, *args, **kwargs)

    monkeypatch.setattr(pkcs12, "load_key_and_certificates", load)
    path, sha1 = _ber_p12(tmp_path)
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        loader(path, tmp_path, sha1)
    assert {str(w.message) for w in seen} == {"some other PKCS#12 notice"}


def test_a_paid_builds_p12_load_prints_nothing_to_stderr(tmp_path: Path) -> None:
    # The path every paid build takes (zsign's private copy), in a child
    # Python with the default warning filters, which print a UserWarning.
    path, _ = _ber_p12(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHONWARNINGS")}
    env.update(HOME=str(home), GRAFT_NO_KEYCHAIN="1")
    code = (
        "import sys; from pathlib import Path; from graft_ipa import p12; "
        "p12.write_reencrypted(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3]), 'x'); "
        "print('ok')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(path), PASSWORD, str(tmp_path / "copy.p12")],
        capture_output=True, text=True, env=env, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "ok\n"
    assert proc.stderr == ""
