"""A throwaway signing identity for tests: a self-signed certificate in a
.p12, and a profile that lists it (or another one, or none).

Made in-process with cryptography, under the test's own directory. Nothing
here runs `security`: `security cms -D` on a profile imports the profile's
certificates into the login keychain of whatever HOME it runs under. A plain
plist is enough for a profile, because `mobileprovision.decode` cuts the XML
out of the CMS bytes."""

from __future__ import annotations

import os
import plistlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

TEAM = "ABCDE12345"
PASSWORD = "correct horse battery"
UDIDS = ("00008130-001A2B3C0123456D", "00008110-000E1C2A3B4C5D6E")
COMMON_NAME = "Apple Development: Test Person (XYZ9876543)"
SERIAL = 0x5EED


def _certificate(
    serial: int, *, common_name: str = COMMON_NAME, rsa_key: bool = False,
    valid: tuple[timedelta, timedelta] = (timedelta(days=-1), timedelta(days=300)),
    unit: str | None = None,
):
    """A self-signed certificate valid from now+valid[0] to now+valid[1].
    EC P-256 unless `rsa_key` (RSA 2048, what Apple issues). `unit` is the
    subject's OU, where Apple's certificates carry the team; None, none."""
    key: Any = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048) if rsa_key
        else ec.generate_private_key(ec.SECP256R1())
    )
    attributes = [x509.NameAttribute(NameOID.COMMON_NAME, common_name)]
    if unit is not None:
        attributes.append(x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, unit))
    name = x509.Name(attributes)
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(now + valid[0]).not_valid_after(now + valid[1])
        .sign(key, hashes.SHA256())
    )
    return key, cert


#: `(key, cert, password) -> .p12 bytes`: how a test's .p12 is exported.
P12Maker = Callable[[Any, Any, str], bytes]


def cryptography_p12(key: Any, cert: Any, password: str) -> bytes:
    """cryptography's best encryption: AES-256-CBC, PBKDF2."""
    return pkcs12.serialize_key_and_certificates(
        b"test", key, cert, None, serialization.BestAvailableEncryption(password.encode())
    )


def openssl_p12(binary: str, *flags: str) -> P12Maker:
    """A maker that runs `binary pkcs12 -export <flags>` on PEM files in a
    temp dir: how most real .p12 files are made. `/usr/bin/openssl` is
    LibreSSL, whose default is the legacy RC2-40 / 3DES / SHA-1 MAC."""

    def make(key: Any, cert: Any, password: str) -> bytes:
        import tempfile

        with tempfile.TemporaryDirectory() as work:
            key_pem, cert_pem, out = (Path(work) / n for n in ("k.pem", "c.pem", "o.p12"))
            key_pem.write_bytes(key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ))
            cert_pem.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            subprocess.run(
                [binary, "pkcs12", "-export", "-inkey", str(key_pem), "-in", str(cert_pem),
                 "-out", str(out), "-passout", f"pass:{password}", *flags],
                check=True, capture_output=True, timeout=120,
            )
            return out.read_bytes()

    return make


#: An OpenSSL 3, which has `-legacy` and `-iter`; macOS's own is LibreSSL.
OPENSSL3 = next(
    (p for p in ("/opt/homebrew/bin/openssl", "/usr/local/bin/openssl")
     if os.access(p, os.X_OK)
     and b"OpenSSL 3" in subprocess.run([p, "version"], capture_output=True).stdout),
    None,
)
LIBRESSL = "/usr/bin/openssl" if os.access("/usr/bin/openssl", os.X_OK) else None


@dataclass(frozen=True)
class Identity:
    p12: Path
    profile: Path


def make_identity(
    root: Path,
    *,
    listed: bool | None = True,
    team: str | None = TEAM,
    expires_in: timedelta = timedelta(days=200),
    devices: tuple[Any, ...] = UDIDS,
    password: str = PASSWORD,
    common_name: str = COMMON_NAME,
    rsa_key: bool = False,
    valid: tuple[timedelta, timedelta] = (timedelta(days=-1), timedelta(days=300)),
    p12_maker: P12Maker = cryptography_p12,
    extra: dict[str, Any] | None = None,
    unit: str | None = None,
) -> Identity:
    """`root/Downloads/Certificates.p12` and `root/Downloads/Test.mobileprovision`.

    `listed`: True, the profile lists the .p12's certificate; False, it lists
    a different one; None, it has no `DeveloperCertificates` at all.
    `team=None` leaves `TeamIdentifier` out. `extra` sets (or, with None,
    removes) profile keys after the rest, for fields of the wrong type.
    `p12_maker` exports the .p12 (`cryptography_p12`, `openssl_p12`). `unit`
    is the certificate subject's OU, which Apple's certificates set to the
    team; None, there is none.
    """
    downloads = root / "Downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    key, cert = _certificate(
        SERIAL, common_name=common_name, rsa_key=rsa_key, valid=valid, unit=unit
    )
    p12 = downloads / "Certificates.p12"
    p12.write_bytes(p12_maker(key, cert, password))
    prefix = team or "PREFIX0000"
    plist: dict = {
        "Name": "Test Wildcard",
        "ApplicationIdentifierPrefix": [prefix],
        "Entitlements": {"application-identifier": f"{prefix}.*"},
        "ExpirationDate": datetime.now(UTC) + expires_in,
        "ProvisionedDevices": list(devices),
    }
    if team is not None:
        plist["TeamIdentifier"] = [team]
    if listed is not None:
        shown = cert if listed else _certificate(SERIAL + 1)[1]
        plist["DeveloperCertificates"] = [shown.public_bytes(serialization.Encoding.DER)]
    for k, v in (extra or {}).items():
        if v is None:
            plist.pop(k, None)
        else:
            plist[k] = v
    profile = downloads / "Test.mobileprovision"
    profile.write_bytes(plistlib.dumps(plist))
    return Identity(p12=p12, profile=profile)


# --- a hostile .p12: a key bag whose cost hides inside the encrypted part ----------


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    if n < 0x80:
        return bytes([tag, n]) + body
    width = (n.bit_length() + 7) // 8
    return bytes([tag, 0x80 | width]) + n.to_bytes(width, "big") + body


def _oid(dotted: str) -> bytes:
    arcs = [int(a) for a in dotted.split(".")]
    out = bytes([40 * arcs[0] + arcs[1]])
    for arc in arcs[2:]:
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc:
            chunk.append(0x80 | (arc & 0x7F))
            arc >>= 7
        out += bytes(reversed(chunk))
    return _der(0x06, out)


def _int(n: int) -> bytes:
    return _der(0x02, n.to_bytes((n.bit_length() + 8) // 8, "big"))


def _seq(*parts: bytes) -> bytes:
    return _der(0x30, b"".join(parts))


def _pbes2(salt: bytes, iterations: int, iv: bytes) -> bytes:
    """PBES2 / PBKDF2-HMAC-SHA256 / AES-256-CBC, as OpenSSL 3 writes it."""
    return _seq(_oid("1.2.840.113549.1.5.13"), _seq(
        _seq(_oid("1.2.840.113549.1.5.12"), _seq(
            _der(0x04, salt), _int(iterations), _seq(_oid("1.2.840.113549.2.9"), b"\x05\x00"),
        )),
        _seq(_oid("2.16.840.1.101.3.4.1.42"), _der(0x04, iv)),
    ))


def nested_key_bag_p12(password: str, inner_iterations: int) -> bytes:
    """A PKCS#12 file whose one encrypted SafeContents (2048 rounds, properly
    encrypted under `password`) holds a shrouded key bag asking for
    `inner_iterations`. Only the outer count can be read before decrypting
    (`p12.kdf_iterations` sees 2048); OpenSSL decrypts it, then spends the
    inner count before finding the key bag is garbage. No MAC, which OpenSSL
    doesn't require. Built by hand: no exporter lays a file out this way."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    bag = _seq(
        _oid("1.2.840.113549.1.12.10.1.2"),
        _der(0xA0, _seq(_pbes2(os.urandom(16), inner_iterations, os.urandom(16)),
                        _der(0x04, os.urandom(1232)))),
    )
    salt, iv = os.urandom(16), os.urandom(16)
    key = PBKDF2HMAC(hashes.SHA256(), 32, salt, 2048).derive(password.encode())
    padder = padding.PKCS7(128).padder()
    plain = padder.update(_seq(bag)) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    sealed = encryptor.update(plain) + encryptor.finalize()
    encrypted = _seq(_int(0), _seq(_oid("1.2.840.113549.1.7.1"), _pbes2(salt, 2048, iv),
                                   _der(0x80, sealed)))
    auth_safe = _seq(_seq(_oid("1.2.840.113549.1.7.6"), _der(0xA0, encrypted)))
    return _seq(_int(3), _seq(_oid("1.2.840.113549.1.7.1"), _der(0xA0, _der(0x04, auth_safe))))
