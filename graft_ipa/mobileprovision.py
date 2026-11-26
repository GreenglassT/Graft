"""Reading a .mobileprovision, and deciding whether a cached one is still usable.

A provisioning profile is a CMS-signed plist, so `plistlib` alone cannot open
it — the XML plist is cut out of the CMS bytes (see `decode`). That one fact used to live
only in `preflight.parse_mobileprovision`, which put it above `signer` and
`apple_portal` in the import graph and therefore out of reach of the code that
decides whether to re-provision. This module sits below all three so both
answers come from one decoder.

`cache_problem` is the other half: the auto-provisioning cache under
`app_store_connect.profile_cache_dir` is derived data with a knowable validity
condition, and until this existed nothing checked it. `ensure_profile_for_bundle`
returned the cached file whenever it existed — its comment said "signer.py
validates expiry upstream", which signer.py did not do — while app-aware
pre-flight *did* check expiry and device membership and hard-failed the build
before stage 4 could refresh anything. The result was an app whose every build
failed pre-flight with no in-product way out: the only remedy was deleting a
file under ~/.config by hand.
"""

from __future__ import annotations

import plistlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from xml.parsers.expat import ExpatError

from .errors import PreflightError


def decode(profile: Path) -> dict:
    """Decode a .mobileprovision and return the embedded plist as a dict.

    The profile is DER CMS whose signed content is an XML plist, stored
    verbatim, so the plist is cut out of the bytes. `security cms -D` is
    deliberately not used: besides needing a default keychain, it imports the
    profile's embedded certificates into the user's login keychain.
    """
    if not profile.exists():
        raise PreflightError(f"provisioning profile not found: {profile}")
    try:
        data = profile.read_bytes()
    except OSError as e:
        raise PreflightError(f"failed to decode {profile}: {e}") from e
    return decode_bytes(data, source=str(profile))


#: The largest profile `decode_bytes` reads. A development profile is tens
#: of KiB, a hundred devices included; anything near this is not a profile.
MAX_PROFILE_BYTES = 2 << 20

#: `<!DOCTYPE …>` up to its `>`, and whether an internal subset (`[`) opens
#: before it. Apple's line is `<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST
#: 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">`.
_DOCTYPE = re.compile(rb"<!DOCTYPE\b([^>\[]*)(\[)?", re.IGNORECASE)


def _refuse_declarations(xml: bytes, source: str) -> None:
    """Refuse XML that declares an entity, or has an internal DTD subset
    beyond Apple's one DOCTYPE line, before any parser sees it: the billion
    laughs, and its cousins. plistlib refuses entity declarations too, but
    only once expat is reading them; this is a scan, linear in the bytes."""
    if re.search(rb"<!ENTITY", xml, re.IGNORECASE):
        raise PreflightError(f"failed to decode {source}: it declares an XML entity, "
                             "which no provisioning profile does")
    doctypes = list(_DOCTYPE.finditer(xml))
    if len(doctypes) > 1 or any(m.group(2) for m in doctypes):
        raise PreflightError(f"failed to decode {source}: it has a DTD of its own, which no "
                             "provisioning profile does")


#: What plistlib raises for an XML plist it can't read: a missing marker or
#: a bad number (ValueError), XML that doesn't parse (ExpatError), a <date>
#: it can't match (AttributeError), and a structure it can't build
#: (TypeError, OverflowError).
_UNPARSEABLE = (ValueError, ExpatError, plistlib.InvalidFileException, AttributeError, TypeError,
                OverflowError)


def decode_bytes(data: bytes, *, source: str = "the profile") -> dict:
    """`decode` for a profile already read: `data` is its bytes, `source`
    names it in the error. For a caller that must decode exactly the bytes it
    read (`account_setup`, which installs those bytes), and for one App Store
    Connect has just sent, read before anything decides to keep it
    (`apple_portal`).

    Refused before any parsing: more than `MAX_PROFILE_BYTES`, an XML entity
    or a DTD of its own (`_refuse_declarations`). And only an XML plist is
    read. Apple's profiles, from developer.apple.com and from App Store
    Connect alike, always carry one; a binary plist can make one object
    stand for many (a DAG), and walking it as a tree costs twice as much per
    level of a file of a few hundred bytes (the 2.0 review's SEC-2).
    """
    if len(data) > MAX_PROFILE_BYTES:
        raise PreflightError(f"failed to decode {source}: it is larger than 2 MiB, so it "
                             "isn't a provisioning profile")
    try:
        start = data.index(b"<?xml")
        end = data.rindex(b"</plist>", start) + len(b"</plist>")
        _refuse_declarations(data[start:end], source)
        plist = plistlib.loads(data[start:end], fmt=plistlib.FMT_XML)
    except _UNPARSEABLE as e:
        # ValueError: a missing marker; ExpatError: a plist that does not parse;
        # AttributeError: a <date> plistlib's pattern doesn't match, which it
        # reads as None.groupdict() (combined fix check #30).
        # The slice fails on BER content split into octet-string chunks, whose
        # headers sit between the pieces (an 0x04 tag is never valid XML, so
        # the slice can never parse into a wrong plist instead): read the
        # signed content out of the CMS structure and try that.
        content = _signed_content(data)
        if content:
            _refuse_declarations(content, source)
        try:
            plist = plistlib.loads(content, fmt=plistlib.FMT_XML) if content else None
        except _UNPARSEABLE:
            plist = None
        if plist is None:
            raise PreflightError(f"failed to decode {source}: {e}") from e
    if not isinstance(plist, dict):
        raise PreflightError(f"failed to decode {source}: embedded plist is not a dictionary")
    return plist


def _ber_header(data: bytes, i: int) -> tuple[int, int, int | None]:
    """`(tag, content_start, content_end)` of the BER element at `i`; the end
    is None for an indefinite length. Raises IndexError/ValueError on garbage."""
    tag = data[i]
    i += 1
    if tag & 0x1F == 0x1F:  # a multi-byte tag: none in CMS, but skip it whole
        while data[i] & 0x80:
            i += 1
        i += 1
    first = data[i]
    i += 1
    if first == 0x80:
        if not tag & 0x20:
            raise ValueError("indefinite length on a primitive")
        return tag, i, None
    if first & 0x80:
        count = first & 0x7F
        if not 0 < count <= 4:
            raise ValueError("length out of range")
        length = int.from_bytes(data[i:i + count], "big")
        i += count
    else:
        length = first
    if i + length > len(data):
        raise ValueError("element runs past the end")
    return tag, i, i + length


def _ber_children(data: bytes, start: int, end: int | None) -> tuple[list[tuple[int, int, int]], int]:
    """The elements inside a constructed one, as `(tag, at, after)`, and where
    it ends — after its end-of-contents marker when the length is indefinite."""
    children: list[tuple[int, int, int]] = []
    i = start
    while True:
        if end is None and data[i:i + 2] == b"\x00\x00":
            return children, i + 2
        if end is not None and i >= end:
            return children, end
        tag, content, content_end = _ber_header(data, i)
        after = (
            _ber_children(data, content, None)[1] if content_end is None else content_end
        )
        children.append((tag, i, after))
        i = after


def _ber_octets(data: bytes, at: int) -> bytes:
    """An OCTET STRING's bytes, its chunks joined if it is constructed."""
    tag, content, content_end = _ber_header(data, at)
    if tag == 0x04:
        return data[content:content_end]
    if tag != 0x24:
        raise ValueError(f"not an octet string: tag {tag:#x}")
    kids, _ = _ber_children(data, content, content_end)
    return b"".join(_ber_octets(data, child_at) for _, child_at, _ in kids)


def _ber_inside(data: bytes, at: int) -> list[tuple[int, int, int]]:
    _, content, content_end = _ber_header(data, at)
    return _ber_children(data, content, content_end)[0]


def _signed_content(data: bytes) -> bytes | None:
    """The content of a CMS SignedData — the profile's plist — or None.

    ContentInfo { contentType, [0] SignedData { version, digestAlgorithms,
    encapContentInfo { eContentType, [0] eContent }, … } }, walked just far
    enough to reach eContent, in DER or BER, definite or indefinite length.
    """
    def child(at: int, index: int, tag: int) -> int:
        """Where the `index`th element inside the one at `at` starts; it must
        carry `tag`."""
        found_tag, found_at, _ = _ber_inside(data, at)[index]
        if found_tag != tag:
            raise ValueError(f"expected tag {tag:#x}, found {found_tag:#x}")
        return found_at

    try:
        if data[0] != 0x30:
            return None
        signed = child(child(0, 1, 0xA0), 0, 0x30)
        encap = child(signed, 2, 0x30)
        econtent = _ber_inside(data, child(encap, 1, 0xA0))[0][1]
        return _ber_octets(data, econtent)
    except (IndexError, ValueError, RecursionError):
        return None


def expiration(plist: dict) -> datetime | None:
    """The profile's expiry as an aware UTC datetime, or None if it declares none.

    plistlib hands back naive datetimes for some profiles; Apple writes them in
    UTC, and every caller compares against `datetime.now(UTC)`.
    """
    value = plist.get("ExpirationDate")
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


#: An Apple team ID: ten capital letters and digits, nothing else.
_TEAM_ID = re.compile(r"[A-Z0-9]{10}")


def is_team_id(value: str) -> bool:
    """Whether `value` is shaped like an Apple team ID.

    A profile is a downloaded file, not trusted input, and the team it names
    goes into the Keychain account `p12-<team>` (written through `security
    -i`, one command per line), into config.yaml and into file and bundle
    names. A `TeamIdentifier` holding a line break or a quote could otherwise
    end that command and start another, so every team read off a profile to
    be stored is checked against this first.
    """
    return _TEAM_ID.fullmatch(value) is not None


def is_development(plist: dict) -> bool:
    """Whether the profile is a development one: its entitlements grant
    `get-task-allow`, which lets a debugger attach. Ad hoc, enterprise and
    App Store profiles don't. On iOS 16 and later an app signed with a
    development profile opens only with Developer Mode on
    (`developer_mode.needed`).

    Only a real `True`: a profile is a downloaded file, and `"true"` or `1`
    there is a value nobody understood.
    """
    entitlements = plist.get("Entitlements")
    return isinstance(entitlements, dict) and entitlements.get("get-task-allow") is True


def provisioned_devices(plist: dict) -> list[str]:
    """The UDIDs the profile lists. Empty for a distribution profile."""
    return list(plist.get("ProvisionedDevices") or [])


def app_id_prefix(plist: dict, fallback: str = "") -> str:
    """The App ID prefix a decoded profile's application-identifier starts with.

    Not the team id: accounts from before 2011 can have a prefix that differs
    from their team id, and an identifier built from the team is covered by
    none of their profiles, fresh ones included. `ApplicationIdentifierPrefix`
    first, then `TeamIdentifier` (they are the same on every newer account),
    then `fallback`. One rule for the two places that need it: judging a cached
    profile (`cache_problem`) and signing with one (`lc_sign`).
    """
    return (plist.get("ApplicationIdentifierPrefix") or plist.get("TeamIdentifier") or [fallback])[0]


def serial_value(serial: str) -> int | None:
    """A certificate serial as the number it is, or None if it isn't hex.

    App Store Connect, Keychain Access and `openssl` print serials with or
    without colons and spaces, in either case, and padded to whole bytes or
    not (`0F3A…` against `F3A…`). Compared as numbers, every spelling of one
    serial is equal and no two serials are.
    """
    try:
        return int(serial.replace(":", "").replace(" ", ""), 16)
    except ValueError:
        return None


def developer_certificate_serials(plist: dict) -> set[int] | None:
    """The serial of each certificate the profile lets sign, or None if it
    lists none (`DeveloperCertificates` absent: not judged on it).

    An entry that won't parse as DER X.509 is left out, so a profile whose
    every entry is unreadable lists no usable certificate.
    """
    entries = plist.get("DeveloperCertificates")
    if entries is None:
        return None
    from cryptography import x509

    serials: set[int] = set()
    for der in entries:
        try:
            serials.add(x509.load_der_x509_certificate(bytes(der)).serial_number)
        except (TypeError, ValueError):
            continue
    return serials


def cache_problem(
    profile: Path,
    *,
    device_udids: Iterable[str] = (),
    application_identifier: str | None = None,
    cert_serial: str | None = None,
    team_id: str | None = None,
) -> str | None:
    """Why this cached profile can't be reused as-is, or None if it can.

    The two conditions are the two the user cannot fix from inside the product:

    * expired — a profile is good for a year and this cache is refreshed by
      nothing else, so left alone it eventually fails every build of the app;
    * missing a device — connect a new iPhone and the cached profile, generated
      before it existed, does not list it.

    A file that won't decode counts as a problem too. That direction is
    deliberately the safe one: re-provisioning costs an App Store Connect round
    trip, while trusting a profile we cannot read costs a signed IPA the phone
    refuses to install.

    Returns the reason as a sentence rather than a bool because it is worth
    saying out loud — the refresh is otherwise invisible, and "why did this
    build talk to Apple?" is a fair question.

    `application_identifier` (the bundle id or wildcard pattern, without the
    prefix) adds a third condition: the profile must be *for* that App ID —
    `<the profile's ApplicationIdentifierPrefix>.<it>`, falling back to the
    team id, case-insensitively. A profile built against the wrong App ID
    (as `matches[0]` once did, picking `….LiveProcess` for the main app) is
    valid in every other respect and would otherwise be reused forever. A
    profile that declares no application-identifier is not judged on it.

    `team_id` and `cert_serial` (the signing .p12's certificate) add two more:
    the profile must belong to that team and list that certificate among its
    `DeveloperCertificates`. Without them a profile made for a revoked or
    replaced certificate, or by the team the user just moved away from, was
    reused forever: zsign signs with the .p12's own certificate whatever the
    profile says, and the phone refuses the result. A profile that declares
    no team, or lists no certificates, is not judged on that.
    """
    try:
        plist = decode(profile)
    except PreflightError as exc:
        return f"the cached profile could not be read ({exc.message})"

    if application_identifier is not None:
        declared = str((plist.get("Entitlements") or {}).get("application-identifier", ""))
        wanted = f"{app_id_prefix(plist)}.{application_identifier}"
        if declared and declared.lower() != wanted.lower():
            return f"the cached profile is for {declared}, not {wanted}"

    if team_id is not None:
        teams = [str(t) for t in plist.get("TeamIdentifier") or []]
        if teams and team_id.upper() not in {t.upper() for t in teams}:
            return f"the cached profile belongs to team {', '.join(teams)}, not {team_id}"

    wanted_serial = serial_value(cert_serial) if cert_serial is not None else None
    if wanted_serial is not None:
        listed_serials = developer_certificate_serials(plist)
        if listed_serials is not None and wanted_serial not in listed_serials:
            return (
                f"the cached profile does not list the signing certificate "
                f"(serial {wanted_serial:X})"
            )

    expiry = expiration(plist)
    if expiry is not None and expiry <= datetime.now(UTC):
        return f"the cached profile expired on {expiry.isoformat()}"

    # An empty ProvisionedDevices list is a distribution profile, which covers
    # every device by not naming any. Absence of the key is not absence of the
    # device, and treating it as such would re-provision on every single build.
    listed = {u.lower() for u in provisioned_devices(plist)}
    if listed:
        missing = sorted(u for u in device_udids if u.lower() not in listed)
        if missing:
            return (
                "the cached profile does not list "
                + ", ".join(missing)
                + " among its provisioned devices"
            )
    return None
