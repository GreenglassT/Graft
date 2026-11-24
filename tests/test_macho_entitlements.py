"""`macho.entitlements`: what a binary's code signature says it asks for.

Read in pure Python from the signature itself: LC_CODE_SIGNATURE, then the
embedded-signature SuperBlob, then its XML entitlements blob (0xfade7171).
Every structure here is built at test time (`macho_builder`) from Apple's
numbers, never the reader's own.
"""

from __future__ import annotations

import io
import plistlib
import struct
import zipfile
from pathlib import Path

import pytest

from graft_ipa import macho

from . import macho_builder as mb

GROUPS = {"com.apple.security.application-groups": ["group.com.example.shared"]}


def _xml(ents: dict) -> bytes:
    return plistlib.dumps(ents, fmt=plistlib.FMT_XML)


def _write(tmp_path: Path, data: bytes, name: str = "bin") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


class Counting(io.BytesIO):
    """A file that counts the bytes read out of it."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.taken = 0

    def read(self, n: int = -1) -> bytes:
        out = super().read(n)
        self.taken += len(out)
        return out


class ForwardOnly(io.BytesIO):
    """A file that fails a seek backwards: what a member of a zip costs a
    full re-decompression for."""

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence == 0 and pos < self.tell():
            raise AssertionError(f"seeked back from {self.tell()} to {pos}")
        return super().seek(pos, whence)


@pytest.mark.parametrize("is64", [True, False])
@pytest.mark.parametrize("little", [True, False])
def test_a_signed_binary_gives_its_entitlements(tmp_path: Path, is64: bool, little: bool) -> None:
    data = mb.signed(
        [mb.encryption_cmd(0, is64=is64, little=little)],
        mb.entitlements_signature(_xml(GROUPS)), is64=is64, little=little,
    )
    assert macho.entitlements(_write(tmp_path, data)) == GROUPS


def test_an_unsigned_binary_asks_for_nothing(tmp_path: Path) -> None:
    assert macho.entitlements(_write(tmp_path, mb.thin([mb.encryption_cmd(0)]))) == {}


def test_a_signature_with_no_entitlements_slot_asks_for_nothing(tmp_path: Path) -> None:
    """An ad-hoc linker signature: a CodeDirectory and nothing else."""
    sig = mb.superblob([(mb.CSSLOT_CODEDIRECTORY, mb.blob(mb.CSMAGIC_CODEDIRECTORY, b"\0" * 40))])
    assert macho.entitlements(_write(tmp_path, mb.signed([], sig))) == {}


def test_only_the_xml_slot_is_read_not_the_der_one(tmp_path: Path) -> None:
    sig = mb.superblob([
        (mb.CSSLOT_CODEDIRECTORY, mb.blob(mb.CSMAGIC_CODEDIRECTORY, b"\0" * 40)),
        (mb.CSSLOT_DER_ENTITLEMENTS, mb.blob(mb.CSMAGIC_EMBEDDED_DER_ENTITLEMENTS, b"\x70\x00")),
        (mb.CSSLOT_ENTITLEMENTS, mb.blob(mb.CSMAGIC_EMBEDDED_ENTITLEMENTS, _xml(GROUPS))),
    ])
    assert macho.entitlements(_write(tmp_path, mb.signed([], sig))) == GROUPS


def test_a_fat_binary_gives_every_slices_entitlements(tmp_path: Path) -> None:
    a = mb.signed([], mb.entitlements_signature(_xml(GROUPS)))
    b = mb.signed([], mb.entitlements_signature(_xml({"aps-environment": "production"})),
                  cputype=mb.CPU_X86_64)
    data = mb.fat([(mb.CPU_ARM64, a), (mb.CPU_X86_64, b)])
    assert macho.entitlements(_write(tmp_path, data)) == {**GROUPS, "aps-environment": "production"}


def test_every_read_of_a_fat_binary_moves_forward() -> None:
    """Slices in file order, each one's load commands then its signature,
    whatever order the fat table lists them in."""
    a = mb.signed([], mb.entitlements_signature(_xml(GROUPS)))
    b = mb.signed([], mb.entitlements_signature(_xml({"aps-environment": "production"})),
                  cputype=mb.CPU_X86_64)
    data = bytearray(mb.fat([(mb.CPU_ARM64, a), (mb.CPU_X86_64, b)]))
    # Swap the two table entries: the second slice is listed first.
    data[8:48] = data[28:48] + data[8:28]
    assert macho.entitlements_from(ForwardOnly(bytes(data)), "bin", len(data)) == {
        **GROUPS, "aps-environment": "production",
    }


def test_a_slot_pointing_back_into_the_index_is_refused_before_seeking_back() -> None:
    sig = (struct.pack(">III", mb.CSMAGIC_EMBEDDED_SIGNATURE, 28, 1)
           + struct.pack(">II", mb.CSSLOT_ENTITLEMENTS, 12) + b"\0" * 8)
    data = mb.fat([(mb.CPU_ARM64, mb.signed([], sig))])
    with pytest.raises(macho.MachOError, match="outside the signature"):
        macho.entitlements_from(ForwardOnly(data), "bin", len(data))


def test_a_zip_member_is_read_in_place(tmp_path: Path) -> None:
    """How the capability check reads an IPA: `ZipFile.open`, never extracted."""
    data = mb.signed([], mb.entitlements_signature(_xml(GROUPS)))
    ipa = tmp_path / "x.ipa"
    with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Payload/Example.app/Example", data)
    with zipfile.ZipFile(ipa) as z:
        info = z.getinfo("Payload/Example.app/Example")
        with z.open(info) as f:
            assert macho.entitlements_from(f, "Example", info.file_size) == GROUPS


# ---- malformed, and bounded ----


def _signed_with(signature: bytes, **kw) -> bytes:
    return mb.signed([], signature, **kw)


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_signed_with(b"", dataoff=1 << 30, datasize=64), id="signature-past-eof"),
        pytest.param(_signed_with(mb.entitlements_signature(b"<plist/>"), datasize=1 << 30),
                     id="signature-size-past-eof"),
        pytest.param(_signed_with(struct.pack(">III", 0xDEADBEEF, 12, 0)), id="not-a-superblob"),
        pytest.param(_signed_with(struct.pack(">III", mb.CSMAGIC_EMBEDDED_SIGNATURE, 12, 1 << 20)),
                     id="too-many-slots"),
        pytest.param(_signed_with(struct.pack(">III", mb.CSMAGIC_EMBEDDED_SIGNATURE, 20, 1)
                                  + struct.pack(">II", mb.CSSLOT_ENTITLEMENTS, 4)),
                     id="slot-inside-the-index"),
        pytest.param(_signed_with(struct.pack(">III", mb.CSMAGIC_EMBEDDED_SIGNATURE, 28, 1)
                                  + struct.pack(">II", mb.CSSLOT_ENTITLEMENTS, 20)
                                  + struct.pack(">II", 0xFADE0000, 8)),
                     id="slot-is-not-entitlements"),
        pytest.param(_signed_with(struct.pack(">III", mb.CSMAGIC_EMBEDDED_SIGNATURE, 28, 1)
                                  + struct.pack(">II", mb.CSSLOT_ENTITLEMENTS, 20)
                                  + struct.pack(">II", mb.CSMAGIC_EMBEDDED_ENTITLEMENTS, 4000)),
                     id="blob-runs-past-the-signature"),
        pytest.param(_signed_with(mb.entitlements_signature(b"not a plist")), id="not-a-plist"),
        pytest.param(_signed_with(mb.entitlements_signature(plistlib.dumps(GROUPS, fmt=plistlib.FMT_BINARY))),
                     id="binary-plist"),
        pytest.param(_signed_with(mb.entitlements_signature(_xml([1, 2]))), id="not-a-dict"),
        pytest.param(_signed_with(mb.entitlements_signature(
            b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]><plist><dict>'
            b"<key>k</key><string>&a;</string></dict></plist>")), id="entity-declaration"),
        pytest.param(_signed_with(mb.entitlements_signature(
            b"<plist><dict><key>k</key><date>not a date</date></dict></plist>")), id="bad-date"),
        pytest.param(_signed_with(mb.entitlements_signature(
            b"<plist><dict><key>k</key><integer>" + b"9" * 5000 + b"</integer></dict></plist>")),
            id="huge-integer"),
    ],
)
def test_a_malformed_signature_is_a_machoerror_not_a_crash(tmp_path: Path, data: bytes) -> None:
    with pytest.raises(macho.MachOError):
        macho.entitlements(_write(tmp_path, data))


def test_an_oversized_entitlements_blob_is_refused_without_being_read(tmp_path: Path) -> None:
    """Four times the cap, really there in the file: refused from its
    header, having read a few hundred bytes of it, not megabytes."""
    huge = _xml({"k": "x" * (4 * macho.MAX_ENTITLEMENTS)})
    data = _signed_with(mb.entitlements_signature(huge))
    f = Counting(data)
    with pytest.raises(macho.MachOError, match="entitlements"):
        macho.entitlements_from(f, "bin", len(data))
    assert f.taken < 4096


def test_a_signature_claiming_many_slots_reads_only_what_the_cap_allows(tmp_path: Path) -> None:
    """An index that really is in the file, and consistent with the
    signature's own length: 131072 slots, a megabyte. Refused from the count
    alone; the slots of a 400 MB binary's worth would be gigabytes of tuples."""
    count = 1 << 17
    data = _signed_with(
        struct.pack(">III", mb.CSMAGIC_EMBEDDED_SIGNATURE, 12 + 8 * count, count)
        + b"\0" * (8 * count)
    )
    f = Counting(data)
    with pytest.raises(macho.MachOError):
        macho.entitlements_from(f, "bin", len(data))
    assert f.taken < 4096


def test_not_a_macho_is_notmacho(tmp_path: Path) -> None:
    with pytest.raises(macho.NotMachO):
        macho.entitlements(_write(tmp_path, b"#!/bin/sh\necho hi\n"))


# ---- forward-only, against a crafted file in a compressed member ----
#
# A `ZipFile.open` member re-decompresses from its top on every backward
# seek. A fat binary whose slices' signatures point past the next slice
# would make the reader seek back for every slice: sixteen slices, sixteen
# decompressions of a 300 MB executable (the security review).


class ZipCost:
    """Stands between the reader and a `ZipExtFile`, adding up what each call
    costs it: a read or a forward seek decompresses through to where it
    lands; a backward seek decompresses again from the top."""

    def __init__(self, f) -> None:
        self._f = f
        self.cost = 0
        self.backward = 0

    def tell(self) -> int:
        return self._f.tell()

    def read(self, n: int = -1) -> bytes:
        out = self._f.read(n)
        self.cost += len(out)
        return out

    def seek(self, pos: int, whence: int = 0) -> int:
        here = self._f.tell()
        if pos < here:
            self.backward += 1
            self.cost += pos
        else:
            self.cost += pos - here
        return self._f.seek(pos, whence)


def _backwards_fat(slices: int = 16, spacing: int = 1 << 16) -> bytes:
    """`slices` slices `spacing` apart, each one's signature pointing at the
    last slice's region, past every slice header after its own."""
    span = slices * spacing
    parts = []
    for i in range(slices):
        pad = span - i * spacing - 4096  # every signature lands near the end
        parts.append(mb.signed([], b"", dataoff=pad, datasize=1024))
    data = bytearray(8 + 20 * slices)
    data[:8] = struct.pack(">II", mb.FAT_MAGIC, slices)
    first = spacing
    for i, part in enumerate(parts):
        offset = first + i * spacing
        struct.pack_into(">iiIII", data, 8 + 20 * i, mb.CPU_ARM64, 0, offset, len(part), 14)
    body = bytearray(first + span)
    body[: len(data)] = data
    for i, part in enumerate(parts):
        offset = first + i * spacing
        body[offset: offset + len(part)] = part
    # A real signature at each target, so a reader that follows them parses one.
    sig = mb.entitlements_signature(_xml(GROUPS))
    for i in range(slices):
        target = first + i * spacing + span - i * spacing - 4096
        body[target: target + len(sig)] = sig
    return bytes(body)


def _deflated(tmp_path: Path, data: bytes) -> Path:
    ipa = tmp_path / "x.ipa"
    with zipfile.ZipFile(ipa, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Payload/Example.app/Example", data)
    return ipa


def test_a_fat_binary_whose_signatures_point_backwards_is_refused_in_one_pass(
    tmp_path: Path,
) -> None:
    data = _backwards_fat()
    ipa = _deflated(tmp_path, data)
    with zipfile.ZipFile(ipa) as z:
        info = z.getinfo("Payload/Example.app/Example")
        with z.open(info) as member:
            f = ZipCost(member)
            with pytest.raises(macho.MachOError, match="past the end of its slice"):
                macho.entitlements_from(f, "Example", info.file_size)
    assert f.backward == 0
    assert f.cost <= info.file_size


def test_a_signature_inside_its_own_load_commands_is_refused() -> None:
    data = mb.fat([(mb.CPU_ARM64, mb.signed([], mb.entitlements_signature(_xml(GROUPS)),
                                            dataoff=8))])
    f = ForwardOnly(data)
    with pytest.raises(macho.MachOError, match="inside its load commands"):
        macho.entitlements_from(f, "bin", len(data))


def test_a_signature_running_into_the_next_slice_is_refused() -> None:
    sig = mb.entitlements_signature(_xml(GROUPS))
    a = mb.signed([], sig)
    b = mb.signed([], sig, cputype=mb.CPU_X86_64)
    data = bytearray(mb.fat([(mb.CPU_ARM64, a), (mb.CPU_X86_64, b)]))
    # The first slice claims to run into the second: its signature is told
    # it is 1 MB long, which the file has room for only across slice two.
    data += b"\0" * (1 << 20)
    first = struct.unpack_from(">I", data, 16)[0]
    struct.pack_into(">I", data, 8 + 12, len(data) - first)  # slice 1's size: to EOF
    lc_at = first + 32  # LC_CODE_SIGNATURE, its only command
    struct.pack_into("<I", data, lc_at + 12, 1 << 20)
    with pytest.raises(macho.MachOError, match="past the end of its slice"):
        macho.entitlements_from(ForwardOnly(bytes(data)), "bin", len(data))


def test_the_reader_never_reads_past_the_size_it_was_given() -> None:
    """The member's declared size is the budget: nothing past it is read,
    however much more the file holds."""
    data = mb.signed([], mb.entitlements_signature(_xml(GROUPS)))
    f = Counting(data + b"\0" * (1 << 20))
    assert macho.entitlements_from(f, "bin", len(data)) == GROUPS
    assert f.taken <= len(data)
    with pytest.raises(macho.MachOError):
        macho.entitlements_from(Counting(data), "bin", len(data) - 16)


def test_the_forward_reader_refuses_to_go_back() -> None:
    reader = macho._Forward(io.BytesIO(b"\0" * 64), "bin", 64)
    assert reader.read(8) == b"\0" * 8
    reader.seek(0)  # the magic, read once, is replayed for the header
    assert len(reader.read(16)) == 16
    with pytest.raises(macho.MachOError, match="back"):
        reader.seek(4)
    with pytest.raises(macho.MachOError, match="past"):
        reader.seek(65)


def test_the_forward_reader_stops_at_the_size_it_was_given() -> None:
    reader = macho._Forward(io.BytesIO(b"\1" * 64), "bin", 16)
    assert reader.read(100) == b"\1" * 16
    assert reader.read(1) == b""
