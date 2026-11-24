"""Synthetic Mach-O files for tests: just a header and load commands, which
is all `graft_ipa.macho` reads. Big- and little-endian, 32- and 64-bit, thin
and fat.

The numbers are written out here from Apple's <mach-o/loader.h> and
<mach-o/fat.h>, never imported from `graft_ipa.macho`: a builder that packs
the reader's own constants agrees with the reader whatever they are, so a
wrong one (LC_RPATH, say) passed every test built here (review #29).
"""

from __future__ import annotations

import struct

# <mach-o/loader.h>
MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_LOAD_WEAK_DYLIB = 0x80000018
LC_RPATH = 0x8000001C
LC_REEXPORT_DYLIB = 0x8000001F
LC_LAZY_LOAD_DYLIB = 0x20
LC_ENCRYPTION_INFO = 0x21
LC_LOAD_UPWARD_DYLIB = 0x80000023
LC_ENCRYPTION_INFO_64 = 0x2C
LC_VERSION_MIN_MACOSX = 0x24
LC_VERSION_MIN_IPHONEOS = 0x25
LC_BUILD_VERSION = 0x32
LC_CODE_SIGNATURE = 0x1D
PLATFORM_MACOS = 1
PLATFORM_IOS = 2
PLATFORM_MACCATALYST = 6
# <kern/cs_blobs.h>: code-signing structures, big-endian whatever the Mach-O's
# byte order.
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_CODEDIRECTORY = 0xFADE0C02
CSMAGIC_EMBEDDED_ENTITLEMENTS = 0xFADE7171
CSMAGIC_EMBEDDED_DER_ENTITLEMENTS = 0xFADE7172
CSSLOT_CODEDIRECTORY = 0
CSSLOT_ENTITLEMENTS = 5
CSSLOT_DER_ENTITLEMENTS = 7
# <mach-o/fat.h>
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF

CPU_ARM64 = 0x0100000C
CPU_X86_64 = 0x01000007
MH_EXECUTE = 2
MH_DYLIB = 6


def _pad(data: bytes, to: int = 8) -> bytes:
    return data + b"\0" * (-len(data) % to)


def encryption_cmd(cryptid: int, *, is64: bool = True, little: bool = True) -> bytes:
    e = "<" if little else ">"
    if is64:
        return struct.pack(e + "IIIIII", LC_ENCRYPTION_INFO_64, 24, 0x4000, 0x8000, cryptid, 0)
    return struct.pack(e + "IIIII", LC_ENCRYPTION_INFO, 20, 0x4000, 0x8000, cryptid)


def dylib_cmd(name: str, *, cmd: int = LC_LOAD_DYLIB, little: bool = True) -> bytes:
    e = "<" if little else ">"
    body = _pad(name.encode() + b"\0")
    size = 24 + len(body)
    return struct.pack(e + "IIIIII", cmd, size, 24, 2, 0x10000, 0x10000) + body


def _version(text: str) -> int:
    """X.Y.Z as loader.h packs it: xxxx.yy.zz in nibbles."""
    major, minor, patch = [*map(int, text.split(".")), 0, 0][:3]
    return (major << 16) | (minor << 8) | patch


def build_version_cmd(platform: int, minos: str, sdk: str = "15.0", *, little: bool = True) -> bytes:
    """LC_BUILD_VERSION with no tools."""
    e = "<" if little else ">"
    return struct.pack(e + "IIIIII", LC_BUILD_VERSION, 24, platform, _version(minos), _version(sdk), 0)


def version_min_cmd(cmd: int, version: str, sdk: str = "15.0", *, little: bool = True) -> bytes:
    e = "<" if little else ">"
    return struct.pack(e + "IIII", cmd, 16, _version(version), _version(sdk))


def rpath_cmd(path: str, *, little: bool = True) -> bytes:
    e = "<" if little else ">"
    body = _pad(path.encode() + b"\0")
    return struct.pack(e + "III", LC_RPATH, 12 + len(body), 12) + body


def thin(
    cmds: list[bytes], *, is64: bool = True, little: bool = True,
    cputype: int = CPU_ARM64, filetype: int = MH_EXECUTE,
) -> bytes:
    e = "<" if little else ">"
    magic = MH_MAGIC_64 if is64 else MH_MAGIC
    blob = b"".join(cmds)
    header = struct.pack(e + "IiiIIII", magic, cputype, 0, filetype, len(cmds), len(blob), 0)
    if is64:
        header += struct.pack(e + "I", 0)  # reserved
    return header + blob


def fat(slices: list[tuple[int, bytes]], *, is64: bool = False, align: int = 0x4000) -> bytes:
    """`slices` is (cputype, thin bytes). Fat headers are always big-endian."""
    magic = FAT_MAGIC_64 if is64 else FAT_MAGIC
    entry = 32 if is64 else 20
    header_len = 8 + entry * len(slices)
    offset = -(-header_len // align) * align
    table, body = b"", b""
    for cputype, data in slices:
        if is64:
            table += struct.pack(">iiQQII", cputype, 0, offset, len(data), 14, 0)
        else:
            table += struct.pack(">iiIII", cputype, 0, offset, len(data), 14)
        body += data + b"\0" * (-len(data) % align)
        offset += len(data) + (-len(data) % align)
    head = struct.pack(">II", magic, len(slices)) + table
    return head + b"\0" * (-(len(head)) % align) + body


def blob(magic: int, payload: bytes) -> bytes:
    """One code-signing blob: magic, length (header included), payload."""
    return struct.pack(">II", magic, 8 + len(payload)) + payload


def superblob(slots: list[tuple[int, bytes]]) -> bytes:
    """An embedded signature: `(slot type, whole blob)` pairs, laid out in
    that order after the index."""
    first = 12 + 8 * len(slots)
    index, body = b"", b""
    for slot, data in slots:
        index += struct.pack(">II", slot, first + len(body))
        body += data
    return struct.pack(">III", CSMAGIC_EMBEDDED_SIGNATURE, first + len(body), len(slots)) + index + body


def entitlements_signature(xml: bytes) -> bytes:
    """A signature as codesign lays it out: a CodeDirectory first (slot 0,
    padding here), then the XML entitlements in slot 5."""
    return superblob([
        (CSSLOT_CODEDIRECTORY, blob(CSMAGIC_CODEDIRECTORY, b"\0" * 40)),
        (CSSLOT_ENTITLEMENTS, blob(CSMAGIC_EMBEDDED_ENTITLEMENTS, xml)),
    ])


def signed(
    cmds: list[bytes], signature: bytes, *, is64: bool = True, little: bool = True,
    cputype: int = CPU_ARM64, filetype: int = MH_EXECUTE,
    dataoff: int | None = None, datasize: int | None = None,
) -> bytes:
    """`thin` plus an LC_CODE_SIGNATURE naming `signature`, which follows the
    load commands at a 16-byte boundary. `dataoff` and `datasize` override
    what the command says, for a malformed one."""
    e = "<" if little else ">"
    at = (32 if is64 else 28) + sum(len(c) for c in cmds) + 16
    at += -at % 16
    command = struct.pack(
        e + "IIII", LC_CODE_SIGNATURE, 16,
        at if dataoff is None else dataoff, len(signature) if datasize is None else datasize,
    )
    image = thin([*cmds, command], is64=is64, little=little, cputype=cputype, filetype=filetype)
    return image + b"\0" * (at - len(image)) + signature
