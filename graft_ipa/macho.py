"""Read Mach-O load commands in pure Python: what Graft used `otool` for.

Graft asks three things of a Mach-O, and all three are in its load commands:

* `cryptids`: the `cryptid` of every `LC_ENCRYPTION_INFO(_64)`, one per command
  per slice, like `otool -l`. Is this binary still FairPlay-encrypted?
* `dylib_names`: what it links (`LC_LOAD_DYLIB` and its weak, re-export, lazy
  and upward variants), never its own `LC_ID_DYLIB`, which `otool -L` prints on
  line two and once made every tweak look Substrate-linked.
* `rpaths`: its `LC_RPATH` entries.

And one of its code signature, which `LC_CODE_SIGNATURE` points at:

* `entitlements`: what it was signed to ask for, its XML entitlements blob,
  as `codesign -d --entitlements` prints them. Bounded the same way: the
  SuperBlob's index and that one blob are read, never the whole signature.

This replaces `otool` so the CLI no longer needs the Xcode Command Line
Tools. Only the header and the load-command region are read, never segments,
so a 400 MB binary costs a few kilobytes of I/O.

Anything malformed raises `MachOError`, never an empty answer: an empty
`cryptids` means "no encryption command", which the decryption check reads as
decrypted, so a parse failure must never be able to look like that.

`NotMachO`, a `MachOError`, is the one narrower case: the file isn't a Mach-O
at all, only its first bytes resemble one. A Java `.class` file starts
`cafebabe`, exactly like a fat binary; so does a universal static library
(`lipo -create` of two `.a` archives), whose slices are `!<arch>` archives,
not images. The decryption check skips those; it still fails on every other
`MachOError`, because that is a real Mach-O whose load commands can't be read.

In a universal file, a slice that is not a Mach-O at all is left out, and the
others are read; one whose header is a Mach-O's but is broken still fails.
What the reader holds is bounded across all of a file's slices, not per
slice (`MAX_SIZEOFCMDS`, `MAX_LOAD_COMMANDS`): sixteen fat entries naming one
16 MB region of load commands used to cost gigabytes.
"""

from __future__ import annotations

import os
import plistlib
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
#: A fat magic read in the wrong byte order. Fat headers are always big-endian
#: on disk, so a file starting with one of these is not a fat binary.
FAT_CIGAM = 0xBEBAFECA
FAT_CIGAM_64 = 0xBFBAFECA
MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
MH_CIGAM = 0xCEFAEDFE
MH_CIGAM_64 = 0xCFFAEDFE

LC_REQ_DYLD = 0x80000000
LC_LOAD_DYLIB = 0xC
LC_ID_DYLIB = 0xD
LC_LOAD_WEAK_DYLIB = 0x18 | LC_REQ_DYLD
LC_RPATH = 0x1C | LC_REQ_DYLD
LC_REEXPORT_DYLIB = 0x1F | LC_REQ_DYLD
LC_LAZY_LOAD_DYLIB = 0x20
LC_ENCRYPTION_INFO = 0x21
LC_LOAD_UPWARD_DYLIB = 0x23 | LC_REQ_DYLD
LC_ENCRYPTION_INFO_64 = 0x2C
LC_CODE_SIGNATURE = 0x1D

# <kern/cs_blobs.h>. Code-signing structures are big-endian on disk, whatever
# the byte order of the Mach-O they sit in.
CSMAGIC_EMBEDDED_SIGNATURE = 0xFADE0CC0
CSMAGIC_EMBEDDED_ENTITLEMENTS = 0xFADE7171
CSSLOT_ENTITLEMENTS = 5

DYLIB_COMMANDS = frozenset({
    LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, LC_REEXPORT_DYLIB, LC_LAZY_LOAD_DYLIB, LC_LOAD_UPWARD_DYLIB,
})
ENCRYPTION_COMMANDS = frozenset({LC_ENCRYPTION_INFO, LC_ENCRYPTION_INFO_64})

#: Far above any real file: universal binaries carry two to four slices.
MAX_FAT_ARCHS = 16
#: Far above any real binary's load commands (a few hundred KB at most), for
#: all of a file's slices together.
MAX_SIZEOFCMDS = 16 * 1024 * 1024
#: Far above any real binary's count (a few hundred), likewise for the whole
#: file. Each one read is an object: at 8 bytes a command, 16 MB of them was
#: some 340 MB of memory per slice.
MAX_LOAD_COMMANDS = 1 << 16
#: Far above any real signature's slot count: a CodeDirectory or two, the
#: requirements, both forms of the entitlements and the CMS wrapper.
MAX_SIGNATURE_SLOTS = 64
#: Far above any real entitlements plist: LiveContainer's, with its 128
#: keychain groups, is about 10 KB. Per slice.
MAX_ENTITLEMENTS = 1024 * 1024


class MachOError(Exception):
    """The file is not a Mach-O this module can read. The message says why."""


class NotMachO(MachOError):
    """The file is not a Mach-O at all; its first bytes only look like one.

    Raised for a fat header claiming an implausible number of architectures
    (a Java `.class` file: `cafebabe`, then its version), a byte-swapped fat
    magic, a thin file with no Mach-O header at offset 0, and a universal
    file none of whose slices is a Mach-O (a universal static library). Never
    for a truncated fat table, a truncated header or bad load commands: those
    are real Mach-Os that can't be read, and stay a plain `MachOError`.
    """


@dataclass(frozen=True)
class LoadCommand:
    cmd: int
    #: The whole command, its 8-byte header included, in the file's byte order.
    data: bytes
    little: bool

    def u32(self, offset: int) -> int:
        if offset + 4 > len(self.data):
            raise MachOError(f"load command 0x{self.cmd:x} is too short ({len(self.data)} bytes)")
        return struct.unpack_from("<I" if self.little else ">I", self.data, offset)[0]

    def lc_str(self, offset_field: int) -> str:
        """The NUL-terminated string an `lc_str` at `offset_field` points to."""
        start = self.u32(offset_field)
        if start < 8 or start >= len(self.data):
            raise MachOError(f"load command 0x{self.cmd:x} points its string outside itself")
        return self.data[start:].split(b"\0", 1)[0].decode("utf-8", "replace")


def load_commands(path: Path) -> list[list[LoadCommand]]:
    """Every slice's load commands: one list per slice; a thin file has one."""
    try:
        with path.open("rb") as f:
            head = f.read(8)
            if len(head) < 4:
                raise NotMachO(f"{path.name} is too short to be a Mach-O")
            magic = struct.unpack(">I", head[:4])[0]
            if magic in (FAT_CIGAM, FAT_CIGAM_64):
                raise NotMachO(f"{path.name} has a byte-swapped fat magic, which no fat binary has")
            budget = _Budget(path)
            if magic in (FAT_MAGIC, FAT_MAGIC_64):
                size = os.fstat(f.fileno()).st_size
                slices = [
                    cmds for off in _fat_offsets(f, head, path, size)
                    if (cmds := _thin(f, off, path, budget, in_fat=True)) is not None
                ]
                if not slices:
                    raise NotMachO(
                        f"{path.name} is a universal file with no Mach-O slice in it "
                        "(a static library?)"
                    )
                return slices
            thin = _thin(f, 0, path, budget, in_fat=False)
            assert thin is not None
            return [thin]
    except OSError as e:
        raise MachOError(f"couldn't read {path.name}: {e}") from e


def cryptids(path: Path) -> list[int]:
    return [
        lc.u32(16)
        for cmds in load_commands(path)
        for lc in cmds
        if lc.cmd in ENCRYPTION_COMMANDS
    ]


def dylib_names(path: Path) -> list[str]:
    return _unique(
        lc.lc_str(8) for cmds in load_commands(path) for lc in cmds if lc.cmd in DYLIB_COMMANDS
    )


LC_VERSION_MIN_MACOSX = 0x24
LC_VERSION_MIN_IPHONEOS = 0x25
LC_VERSION_MIN_TVOS = 0x2F
LC_VERSION_MIN_WATCHOS = 0x30
LC_BUILD_VERSION = 0x32
#: <mach-o/loader.h>'s PLATFORM_* values this module names.
PLATFORM_MACOS = 1
PLATFORM_IOS = 2
PLATFORM_TVOS = 3
PLATFORM_WATCHOS = 4
PLATFORM_MACCATALYST = 6
#: Platforms whose code runs on a device, never on the Mac: iOS, tvOS,
#: watchOS, their simulators (7, 8, 9), visionOS and its simulator (11, 12).
DEVICE_PLATFORMS = frozenset({2, 3, 4, 7, 8, 9, 11, 12})
_VERSION_MIN = {LC_VERSION_MIN_MACOSX: PLATFORM_MACOS, LC_VERSION_MIN_IPHONEOS: PLATFORM_IOS,
                LC_VERSION_MIN_TVOS: PLATFORM_TVOS, LC_VERSION_MIN_WATCHOS: PLATFORM_WATCHOS}


def _unpack_version(v: int) -> tuple[int, int, int]:
    return v >> 16, (v >> 8) & 0xFF, v & 0xFF


def build_versions(path: Path) -> list[tuple[int | None, tuple[int, int, int] | None]]:
    """(platform, minimum OS as (major, minor, patch)) of each slice, from its
    LC_BUILD_VERSION or LC_VERSION_MIN_*; (None, None) for a slice with neither."""
    out = []
    for cmds in load_commands(path):
        found: tuple[int | None, tuple[int, int, int] | None] = (None, None)
        for lc in cmds:
            if lc.cmd == LC_BUILD_VERSION:
                found = (lc.u32(8), _unpack_version(lc.u32(12)))
                break
            if lc.cmd in _VERSION_MIN:
                found = (_VERSION_MIN[lc.cmd], _unpack_version(lc.u32(8)))
                break
        out.append(found)
    return out


def is_ios(path: Path) -> bool:
    """Whether any slice is built for a device platform (DEVICE_PLATFORMS)."""
    return any(platform in DEVICE_PLATFORMS for platform, _ in build_versions(path))


def rpaths(path: Path) -> list[str]:
    return _unique(
        lc.lc_str(8) for cmds in load_commands(path) for lc in cmds if lc.cmd == LC_RPATH
    )


def entitlements(path: Path) -> dict[str, Any]:
    """The entitlements `path`'s code signature embeds, every slice's merged.

    `{}` for a binary with no LC_CODE_SIGNATURE, or a signature with no XML
    entitlements in it (an ad-hoc linker signature has only a
    CodeDirectory): it asks for nothing. A signature that is there and
    broken raises `MachOError`.
    """
    try:
        with path.open("rb") as f:
            return entitlements_from(f, path.name, os.fstat(f.fileno()).st_size)
    except OSError as e:
        raise MachOError(f"couldn't read {path.name}: {e}") from e


def entitlements_from(f: BinaryIO, name: str, size: int) -> dict[str, Any]:
    """`entitlements` of an open file `size` bytes long; `name` names it in
    errors.

    Read front to back, once, and never past `size` (`_Forward`): a member of
    an IPA opened with `ZipFile.open` decompresses again from its top on
    every backward seek, so a crafted fat binary whose slices' signatures
    pointed past the next slice turned one read into sixteen of a 300 MB
    executable. The slices are taken in file order, each one's load commands
    and then its signature, and a signature must lie inside its own slice,
    after its load commands and before the next slice; in a real binary it
    always does. One that doesn't is a `MachOError`, never a seek back.
    """
    label = Path(name)
    reader = _Forward(f, name, size)
    head = reader.read(8)
    if len(head) < 4:
        raise NotMachO(f"{name} is too short to be a Mach-O")
    magic = struct.unpack(">I", head[:4])[0]
    if magic in (FAT_CIGAM, FAT_CIGAM_64):
        raise NotMachO(f"{name} has a byte-swapped fat magic, which no fat binary has")
    budget = _Budget(label)
    in_fat = magic in (FAT_MAGIC, FAT_MAGIC_64)
    merged: dict[str, Any] = {}
    machos = 0
    for offset, end in _slice_bounds(reader, head, label, size) if in_fat else [(0, size)]:
        cmds = _thin(reader, offset, label, budget, in_fat=in_fat)
        if cmds is None:
            continue
        machos += 1
        signature = next((lc for lc in cmds if lc.cmd == LC_CODE_SIGNATURE), None)
        if signature is not None:
            merged.update(_signed_entitlements(
                reader, offset, signature, name, after=reader.tell(), end=end,
            ))
    if not machos:
        raise NotMachO(f"{name} is a universal file with no Mach-O slice in it")
    return merged


def _slice_bounds(f: BinaryIO, head: bytes, path: Path, size: int) -> list[tuple[int, int]]:
    """Each fat slice's `(start, end)`, in file order: its end is where the fat
    table says it ends, or where the next slice starts, or the end of the
    file, whichever comes first."""
    slices = sorted(set(_fat_slices(f, head, path, size)))
    bounds = []
    for i, (offset, length) in enumerate(slices):
        end = min(offset + length, size)
        if i + 1 < len(slices):
            end = min(end, slices[i + 1][0])
        bounds.append((offset, end))
    return bounds


class _Forward:
    """`f` read front to back only, and never past `size` bytes.

    Its first 8 bytes are read once and replayed, so a thin file's header
    can be read from offset 0 after its magic was; any other seek backwards,
    or past `size`, is a `MachOError`. Everything read or skipped through
    therefore adds up to `size` at most, whatever the file claims.
    """

    def __init__(self, f: BinaryIO, name: str, size: int) -> None:
        self._f = f
        self._name = name
        self._size = size
        self._head = f.read(min(8, size))
        self._pos = 0  # where the reader is
        self._at = len(self._head)  # where `f` is

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        if whence != 0:
            raise ValueError("only absolute seeks")
        replay = pos < len(self._head) and self._at == len(self._head)
        if pos < self._pos and not replay:
            raise MachOError(f"{self._name}: reading it would mean going back to offset {pos}")
        if pos > self._size:
            raise MachOError(f"{self._name}: offset {pos} is past its end ({self._size} bytes)")
        self._pos = pos
        return pos

    def read(self, n: int = -1) -> bytes:
        out = b""
        if self._pos < len(self._head):
            out = self._head[self._pos:self._pos + (n if n >= 0 else len(self._head))]
            self._pos += len(out)
            if n >= 0:
                n -= len(out)
        want = self._size - self._pos if n < 0 else min(n, self._size - self._pos)
        if want <= 0:
            return out
        if self._pos != self._at:
            self._f.seek(self._pos)  # forward: `seek` refused anything else
        data = self._f.read(want)
        self._pos += len(data)
        self._at = self._pos
        return out + data


def _signed_entitlements(
    f: BinaryIO, offset: int, signature: LoadCommand, name: str, *, after: int, end: int,
) -> dict[str, Any]:
    """One slice's XML entitlements, from the signature `signature` names.

    `after` is where the slice's load commands end and `end` where the slice
    does: the signature has to lie between the two."""
    dataoff, datasize = signature.u32(8), signature.u32(12)
    start = offset + dataoff
    if start < after:
        raise MachOError(f"{name}: its code signature starts inside its load commands")
    if datasize < 12 or start + datasize > end:
        raise MachOError(f"{name}: its code signature runs past the end of its slice")
    f.seek(start)
    header = f.read(12)
    if len(header) < 12:
        raise MachOError(f"{name}: its code signature is truncated")
    magic, length, count = struct.unpack(">III", header)
    if magic != CSMAGIC_EMBEDDED_SIGNATURE:
        raise MachOError(f"{name}: its code signature is not an embedded signature (0x{magic:x})")
    index_end = 12 + 8 * count
    if count > MAX_SIGNATURE_SLOTS or length > datasize or index_end > length:
        raise MachOError(f"{name}: its code signature's index ({count} slots) is not plausible")
    index = f.read(8 * count)
    if len(index) < 8 * count:
        raise MachOError(f"{name}: its code signature is truncated")
    slots = [struct.unpack_from(">II", index, 8 * i) for i in range(count)]
    at = next((where for kind, where in slots if kind == CSSLOT_ENTITLEMENTS), None)
    if at is None:
        return {}
    # Past the index, so the seek is forward; and room for a blob header.
    if at < index_end or at + 8 > length:
        raise MachOError(f"{name}: its entitlements slot points outside the signature")
    f.seek(start + at)
    blob_magic, blob_length = struct.unpack(">II", f.read(8).ljust(8, b"\0"))
    if blob_magic != CSMAGIC_EMBEDDED_ENTITLEMENTS:
        raise MachOError(f"{name}: its entitlements slot holds no entitlements (0x{blob_magic:x})")
    if blob_length < 8 or at + blob_length > length:
        raise MachOError(f"{name}: its entitlements run past the end of its signature")
    if blob_length - 8 > MAX_ENTITLEMENTS:
        raise MachOError(f"{name}: {blob_length - 8} bytes of entitlements is not plausible")
    body = f.read(blob_length - 8)
    if len(body) < blob_length - 8:
        raise MachOError(f"{name}: its entitlements are truncated")
    return _entitlements_plist(body, name)


def _entitlements_plist(body: bytes, name: str) -> dict[str, Any]:
    if not body.strip(b"\0 \t\r\n"):
        return {}
    try:
        # XML only, which is what the blob is: a binary plist's parser
        # recurses, and nothing signs one into this slot.
        parsed = plistlib.loads(body, fmt=plistlib.FMT_XML)
    except Exception as e:
        # plistlib's failures are not a closed set: ExpatError, ValueError
        # (an entity declaration, a 5000-digit integer), AttributeError (a
        # `<date>` that isn't one). Each is a blob that can't be read.
        raise MachOError(
            f"{name}: its entitlements are not a readable plist ({type(e).__name__})"
        ) from e
    if not isinstance(parsed, dict):
        raise MachOError(f"{name}: its entitlements are a {type(parsed).__name__}, not a dictionary")
    return parsed


def _unique(items) -> list[str]:
    """`items` once each, in order: a dict, so N names cost N hash lookups,
    not N^2 comparisons against a list."""
    return list(dict.fromkeys(items))


class _Budget:
    """What one file's slices may make the reader hold, all together."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._bytes = MAX_SIZEOFCMDS
        self._cmds = MAX_LOAD_COMMANDS

    def spend(self, ncmds: int, sizeofcmds: int) -> None:
        if sizeofcmds > self._bytes:
            raise MachOError(
                f"{self._path.name}: {sizeofcmds} more bytes of load commands is not plausible"
            )
        if ncmds > self._cmds:
            raise MachOError(f"{self._path.name}: {ncmds} more load commands is not plausible")
        self._bytes -= sizeofcmds
        self._cmds -= ncmds


def _fat_offsets(f: BinaryIO, head: bytes, path: Path, size: int) -> list[int]:
    return [offset for offset, _ in _fat_slices(f, head, path, size)]


def _fat_slices(f: BinaryIO, head: bytes, path: Path, size: int) -> list[tuple[int, int]]:
    """Each fat table entry's `(offset, size)`, in table order."""
    if len(head) < 8:
        raise MachOError(f"{path.name}'s fat header is truncated")
    magic, nfat = struct.unpack(">II", head)
    if not 0 < nfat <= MAX_FAT_ARCHS:
        raise NotMachO(f"{path.name} claims {nfat} architectures")
    is64 = magic == FAT_MAGIC_64
    entry = 32 if is64 else 20
    table = f.read(nfat * entry)
    if len(table) < nfat * entry:
        raise MachOError(f"{path.name}'s architecture table is truncated")
    fmt = ">iiQQII" if is64 else ">iiIII"
    slices = [struct.unpack_from(fmt, table, i * entry)[2:4] for i in range(nfat)]
    for offset, _ in slices:
        # Before any seek: a fat64 offset of 2^63 or more is no file position
        # at all, and `seek` raised ValueError past every MachOError handler.
        if offset >= size:
            raise MachOError(
                f"{path.name}: no Mach-O header at offset {offset} (past the end of the file)"
            )
    return slices


def _thin(
    f: BinaryIO, offset: int, path: Path, budget: _Budget, *, in_fat: bool,
) -> list[LoadCommand] | None:
    """One slice's load commands.

    `in_fat`: the slice is named by a fat header. One that is not a Mach-O at
    all (no Mach-O magic: an `ar` archive in a universal static library) is
    None, left out; a thin file with no Mach-O magic is `NotMachO`. A slice
    with a Mach-O magic is a Mach-O either way, and one that is broken
    raises a plain `MachOError`.
    """
    f.seek(offset)
    header = f.read(28)
    if len(header) < 4:
        raise MachOError(f"{path.name}: no Mach-O header at offset {offset} (too short)")
    magic = struct.unpack("<I", header[:4])[0]
    if magic in (MH_MAGIC, MH_MAGIC_64):
        little = True
    elif magic in (MH_CIGAM, MH_CIGAM_64):
        little = False
    elif in_fat:
        return None
    else:
        raise NotMachO(f"{path.name}: no Mach-O header at offset {offset}")
    if len(header) < 28:
        raise MachOError(f"{path.name}: the Mach-O header at offset {offset} is truncated (too short)")
    is64 = magic in (MH_MAGIC_64, MH_CIGAM_64)
    e = "<" if little else ">"
    ncmds, sizeofcmds = struct.unpack_from(e + "II", header, 16)
    budget.spend(ncmds, sizeofcmds)
    f.seek(offset + (32 if is64 else 28))
    blob = f.read(sizeofcmds)
    if len(blob) < sizeofcmds:
        raise MachOError(f"{path.name}: load commands truncated")
    cmds: list[LoadCommand] = []
    pos = 0
    for index in range(ncmds):
        if pos + 8 > len(blob):
            raise MachOError(f"{path.name}: load command {index} runs past sizeofcmds")
        cmd, cmdsize = struct.unpack_from(e + "II", blob, pos)
        if cmdsize < 8 or pos + cmdsize > len(blob):
            raise MachOError(f"{path.name}: load command {index} has a bad size ({cmdsize})")
        cmds.append(LoadCommand(cmd=cmd, data=blob[pos:pos + cmdsize], little=little))
        pos += cmdsize
    return cmds
