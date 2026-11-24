"""`mobileprovision.decode` reads a profile without running `security`.

`security cms -D` imports the profile's embedded certificates into the user's
login keychain as a side effect, and fails outright when there is no default
keychain. A profile is DER CMS whose content is an XML plist, so the plist can
be cut out of the bytes directly.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import mobileprovision
from graft_ipa.errors import PreflightError

PLIST = {
    "Name": "Example Profile",
    "TeamIdentifier": ["TEAM123456"],
    "ProvisionedDevices": ["00008130-001A2B3C0123456D"],
    "ExpirationDate": datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    "Entitlements": {"application-identifier": "TEAM123456.com.example.app"},
}


def _hand_built(plist: dict) -> bytes:
    """DER-ish CMS framing: binary header, the XML plist, binary signature."""
    xml = plistlib.dumps(plist, fmt=plistlib.FMT_XML)
    header = b"\x30\x82\x0a\x1b\x06\x09\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02\xa0\x82"
    trailer = b"\x31\x82\x01\x00\x30\x82\x00\xfe\x00\xff</plist-not>\x00"
    return header + xml + trailer


def _no_security():
    return patch.object(
        subprocess, "run", side_effect=AssertionError("decode must not run a subprocess")
    )


def test_decodes_a_hand_built_profile_without_running_security(tmp_path: Path) -> None:
    f = tmp_path / "p.mobileprovision"
    f.write_bytes(_hand_built(PLIST))
    with _no_security():
        assert mobileprovision.decode(f) == PLIST


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_decodes_a_real_der_cms_made_by_openssl(tmp_path: Path) -> None:
    """A genuine CMS SignedData (nodetach, DER), built under a throwaway dir."""
    (tmp_path / "in.plist").write_bytes(plistlib.dumps(PLIST, fmt=plistlib.FMT_XML))
    env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(tmp_path),
        "GRAFT_NO_KEYCHAIN": "1",
    }

    def run(*args: str) -> None:
        subprocess.run(args, check=True, capture_output=True, env=env, cwd=tmp_path)

    run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", "k.pem", "-out", "c.pem", "-subj", "/CN=Graft Test", "-days", "2",
    )
    run(
        "openssl", "cms", "-sign", "-binary", "-in", "in.plist", "-signer", "c.pem",
        "-inkey", "k.pem", "-outform", "DER", "-nodetach", "-out", "p.mobileprovision",
    )
    with _no_security():
        assert mobileprovision.decode(tmp_path / "p.mobileprovision") == PLIST


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_decodes_a_ber_streamed_cms_whose_content_is_chunked(tmp_path: Path) -> None:
    """`openssl cms -sign -stream` writes indefinite-length BER with the
    content split into octet-string chunks, a header between each — so the
    bytes from `<?xml` to `</plist>` are not a plist. Apple's profiles have
    been single-chunk DER so far; this one has 400 devices (18 KB of plist)."""
    plist = {**PLIST, "ProvisionedDevices": [f"00008130-{i:016X}" for i in range(400)]}
    (tmp_path / "in.plist").write_bytes(plistlib.dumps(plist, fmt=plistlib.FMT_XML))
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path),
           "GRAFT_NO_KEYCHAIN": "1"}

    def run(*args: str) -> None:
        subprocess.run(args, check=True, capture_output=True, env=env, cwd=tmp_path)

    run(
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", "k.pem", "-out", "c.pem", "-subj", "/CN=Graft Test", "-days", "2",
    )
    run(
        "openssl", "cms", "-sign", "-binary", "-stream", "-in", "in.plist",
        "-signer", "c.pem", "-inkey", "k.pem", "-outform", "DER", "-nodetach",
        "-out", "p.mobileprovision",
    )
    data = (tmp_path / "p.mobileprovision").read_bytes()
    assert b"\x24\x80" in data, "openssl did not stream: the test proves nothing"
    with _no_security():
        assert mobileprovision.decode(tmp_path / "p.mobileprovision") == plist


def _ber(tag: int, *parts: bytes, indefinite: bool = False) -> bytes:
    body = b"".join(parts)
    if indefinite:
        return bytes([tag, 0x80]) + body + b"\x00\x00"
    n = len(body)
    length = bytes([n]) if n < 0x80 else bytes([0x82]) + n.to_bytes(2, "big")
    return bytes([tag]) + length + body


def test_decodes_chunks_nested_in_a_definite_length_frame(tmp_path: Path) -> None:
    """The same content, split into chunks inside definite-length framing."""
    xml = plistlib.dumps(PLIST, fmt=plistlib.FMT_XML)
    chunks = [xml[i:i + 100] for i in range(0, len(xml), 100)]
    oid_data = _ber(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x01")
    oid_signed = _ber(0x06, b"\x2a\x86\x48\x86\xf7\x0d\x01\x07\x02")
    econtent = _ber(0x24, *(_ber(0x04, c) for c in chunks))
    encap = _ber(0x30, oid_data, _ber(0xA0, econtent))
    signed = _ber(0x30, _ber(0x02, b"\x01"), _ber(0x31), encap, _ber(0x31))
    blob = _ber(0x30, oid_signed, _ber(0xA0, signed))
    f = tmp_path / "p.mobileprovision"
    f.write_bytes(blob)
    with _no_security():
        assert mobileprovision.decode(f) == PLIST


def test_a_file_with_no_plist_is_a_preflight_error(tmp_path: Path) -> None:
    f = tmp_path / "p.mobileprovision"
    f.write_bytes(b"\x30\x82\x00\x10 not a profile")
    with _no_security(), pytest.raises(PreflightError, match="failed to decode"):
        mobileprovision.decode(f)


def test_a_truncated_plist_is_a_preflight_error(tmp_path: Path) -> None:
    f = tmp_path / "p.mobileprovision"
    f.write_bytes(b"\x30\x82<?xml version='1.0'?><plist><dict><key>a</key></plist>")
    with _no_security(), pytest.raises(PreflightError, match="failed to decode"):
        mobileprovision.decode(f)


@pytest.mark.parametrize("value", [b"<date>soon</date>", b"<date>2030-13-45T00:00:00Z</date>",
                                   b"<real>lots</real>", b"<integer>many</integer>"])
def test_a_malformed_value_is_a_preflight_error_not_a_raw_one(value: bytes) -> None:
    """Combined fix check #30: plistlib reads a <date> its pattern doesn't
    match as None.groupdict(), an AttributeError that reached setup, --check
    and builds as 'a failure graft knows nothing of'."""
    xml = plistlib.dumps(PLIST, fmt=plistlib.FMT_XML)
    start = xml.index(b"<key>ExpirationDate</key>") + len(b"<key>ExpirationDate</key>")
    end = xml.index(b"</date>", start) + len(b"</date>")
    data = _hand_built(PLIST).replace(xml, xml[:start] + value + xml[end:])
    with pytest.raises(PreflightError, match="failed to decode"):
        mobileprovision.decode_bytes(data, source="bad.mobileprovision")


def test_a_missing_file_is_still_a_preflight_error(tmp_path: Path) -> None:
    with pytest.raises(PreflightError, match="not found"):
        mobileprovision.decode(tmp_path / "nope.mobileprovision")


def test_decode_bytes_reads_only_the_bytes_it_is_given() -> None:
    """`account_setup` reads a chosen profile once and decodes those bytes,
    so the profile it checks is the profile it installs. `source` names it in
    the error, as the path does for `decode`."""
    with _no_security():
        assert mobileprovision.decode_bytes(_hand_built(PLIST), source="x.mobileprovision") == PLIST
    with pytest.raises(PreflightError, match=r"failed to decode chosen\.mobileprovision"):
        mobileprovision.decode_bytes(b"not a profile", source="chosen.mobileprovision")


# --- hostile profiles (2.0 review SEC-2) ---------------------------------------------


def _cms(content: bytes) -> bytes:
    """A minimal ContentInfo / SignedData whose eContent is `content`: just
    enough structure for `_signed_content` to walk, no signature."""

    def der(tag: int, body: bytes) -> bytes:
        n = len(body)
        size = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes(
            (n.bit_length() + 7) // 8, "big")
        return bytes([tag]) + size + body

    data_oid = der(0x06, bytes.fromhex("2a864886f70d010701"))
    signed_oid = der(0x06, bytes.fromhex("2a864886f70d010702"))
    encap = der(0x30, data_oid + der(0xA0, der(0x04, content)))
    signed = der(0x30, der(0x02, b"\x01") + der(0x31, b"") + encap)
    return der(0x30, signed_oid + der(0xA0, signed))


def _billion_laughs(levels: int = 10) -> bytes:
    entities = ['<!ENTITY lol0 "lol">'] + [
        f'<!ENTITY lol{n} "{("&lol" + str(n - 1) + ";") * 10}">' for n in range(1, levels)
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist [\n' + "\n".join(entities)
        + f'\n]>\n<plist version="1.0"><dict><key>Name</key><string>&lol{levels - 1};</string>'
        "</dict></plist>"
    ).encode()


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare", "in CMS"])
def test_a_billion_laughs_profile_is_refused_at_once(wrapped: bool) -> None:
    import time

    data = _billion_laughs()
    if wrapped:
        data = _cms(data)
    started = time.monotonic()
    with pytest.raises(PreflightError, match="declares an XML entity"):
        mobileprovision.decode_bytes(data)
    assert time.monotonic() - started < 2, "a parser expanding it would never finish"


def test_an_internal_dtd_subset_without_entities_is_refused() -> None:
    xml = plistlib.dumps({"Name": "x"}).replace(
        b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd" [ <!ATTLIST plist a CDATA "b"> ]>',
    )
    with pytest.raises(PreflightError, match="has a DTD of its own"):
        mobileprovision.decode_bytes(xml)


def test_apples_own_doctype_line_is_read() -> None:
    xml = plistlib.dumps({"Name": "x"})
    assert b"<!DOCTYPE plist PUBLIC" in xml
    assert mobileprovision.decode_bytes(xml) == {"Name": "x"}


def test_a_binary_plist_dag_is_refused_without_being_walked() -> None:
    """The review's 210-byte profile: a list made of itself, doubled 24 times,
    as a binary plist inside CMS. Apple never ships a binary plist in a
    profile, so it isn't read at all, where walking it once took 7 s."""
    import time

    a: list = ["x"]
    for _ in range(24):
        a = [a, a]
    data = _cms(plistlib.dumps({"TeamIdentifier": a}, fmt=plistlib.FMT_BINARY))
    assert len(data) < 400
    started = time.monotonic()
    with pytest.raises(PreflightError, match="failed to decode"):
        mobileprovision.decode_bytes(data)
    assert time.monotonic() - started < 2, "walking it took 7 s"


def test_a_profile_over_2_mib_is_refused_before_it_is_read() -> None:
    big = plistlib.dumps({"Name": "x" * (mobileprovision.MAX_PROFILE_BYTES + 1)})
    with pytest.raises(PreflightError, match="larger than 2 MiB"):
        mobileprovision.decode_bytes(big)


def test_a_cms_wrapped_xml_profile_still_reads() -> None:
    assert mobileprovision.decode_bytes(_cms(plistlib.dumps(PLIST)))["Name"] == PLIST["Name"]


@pytest.mark.parametrize("value", ["ABCDE12345\n", "ABCDE12345 ", " ABCDE12345", "ABCDE1234\n5"])
def test_is_team_id_refuses_a_team_with_anything_around_it(value: str) -> None:
    """T4 of the 2.0 review: `match` with `$` accepts a trailing newline;
    `fullmatch` doesn't."""
    assert mobileprovision.is_team_id(value) is False
    assert mobileprovision.is_team_id("ABCDE12345") is True
