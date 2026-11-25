from __future__ import annotations

import shutil
import struct
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from graft_ipa import macho

from . import macho_builder as mb


def _write(tmp_path: Path, data: bytes, name: str = "bin") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


@pytest.mark.parametrize("is64", [True, False])
@pytest.mark.parametrize("little", [True, False])
def test_a_thin_file_yields_its_load_commands_in_order(tmp_path, is64, little):
    data = mb.thin(
        [mb.encryption_cmd(1, is64=is64, little=little), mb.rpath_cmd("@loader_path", little=little)],
        is64=is64, little=little,
    )
    slices = macho.load_commands(_write(tmp_path, data))
    assert len(slices) == 1
    expected_enc = mb.LC_ENCRYPTION_INFO_64 if is64 else mb.LC_ENCRYPTION_INFO
    assert [lc.cmd for lc in slices[0]] == [expected_enc, mb.LC_RPATH]


@pytest.mark.parametrize("is64", [False, True])
def test_a_fat_file_yields_one_list_per_slice(tmp_path, is64):
    a = mb.thin([mb.encryption_cmd(0)])
    b = mb.thin([mb.encryption_cmd(1), mb.rpath_cmd("@executable_path/Frameworks")],
                cputype=mb.CPU_X86_64)
    slices = macho.load_commands(_write(tmp_path, mb.fat([(mb.CPU_ARM64, a), (mb.CPU_X86_64, b)], is64=is64)))
    assert [len(s) for s in slices] == [1, 2]


@pytest.mark.parametrize("data, why", [
    (b"", "too short"),
    (b"\x00\x01\x02\x03", "no Mach-O header"),
    (b"\xca\xfe\xba\xbe\x00\x00\x00\x34", "claims 52 architectures"),  # a Java class file
    (b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\0" * 10, "architecture table is truncated"),
])
def test_files_that_are_not_readable_mach_o_raise_machoerror(tmp_path, data, why):
    with pytest.raises(macho.MachOError, match=why):
        macho.load_commands(_write(tmp_path, data))


@pytest.mark.parametrize("data", [
    pytest.param(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64, id="a Java class file"),
    pytest.param(b"\xca\xfe\xba\xbe\x00\x00\x00\x00" + b"\0" * 64, id="a fat header with no architectures"),
    pytest.param(b"\xbe\xba\xfe\xca\x02\x00\x00\x00" + b"\0" * 64, id="a byte-swapped fat magic"),
    pytest.param(b"\xbf\xba\xfe\xca\x02\x00\x00\x00" + b"\0" * 64, id="a byte-swapped 64-bit fat magic"),
    pytest.param(b"\x00\x01\x02\x03" + b"\0" * 64, id="no Mach-O header at offset 0"),
    # A universal file whose only slice is no Mach-O at all (review #18).
    pytest.param(mb.fat([(mb.CPU_ARM64, b"\0" * 64)]), id="a fat file whose slice is not a Mach-O"),
])
def test_a_file_that_only_looks_like_a_mach_o_is_not_a_mach_o(tmp_path, data):
    """`NotMachO`: what the decryption check may skip rather than fail."""
    with pytest.raises(macho.NotMachO):
        macho.load_commands(_write(tmp_path, data))


@pytest.mark.parametrize("data", [
    pytest.param(b"\xca\xfe\xba\xbe", id="a truncated fat header"),
    pytest.param(b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\0" * 10, id="a truncated architecture table"),
    pytest.param(mb.fat([(mb.CPU_ARM64, b"\xcf\xfa\xed\xfe" + b"\0" * 8)], align=8)[:-4],
                 id="a fat slice whose Mach-O header is truncated"),
    pytest.param(mb.fat([(mb.CPU_ARM64, mb.thin([mb.rpath_cmd("@loader_path")])[:-4])], align=4),
                 id="a fat slice whose load commands are truncated"),
    pytest.param(mb.fat([(mb.CPU_ARM64, mb.thin([mb.encryption_cmd(0)])),
                         (mb.CPU_X86_64, b"\xcf\xfa\xed\xfe" + b"\0" * 8)], align=8)[:-4],
                 id="a broken Mach-O slice beside a good one"),
    pytest.param(b"\xcf\xfa\xed\xfe" + b"\0" * 8, id="a truncated header"),
    pytest.param(mb.thin([mb.encryption_cmd(1)])[:-4], id="truncated load commands"),
])
def test_a_broken_mach_o_is_a_machoerror_but_never_notmacho(tmp_path, data):
    """A real Mach-O the reader can't parse must fail, never be skipped as
    something else: skipping it would read as decrypted."""
    with pytest.raises(macho.MachOError) as e:
        macho.load_commands(_write(tmp_path, data))
    assert not isinstance(e.value, macho.NotMachO), e.value


def test_a_load_command_running_past_sizeofcmds_is_refused(tmp_path):
    good = mb.thin([mb.rpath_cmd("@loader_path")])
    # Claim 2 commands while only 1 is present.
    broken = good[:16] + (2).to_bytes(4, "little") + good[20:]
    with pytest.raises(macho.MachOError, match="runs past sizeofcmds"):
        macho.load_commands(_write(tmp_path, broken))


def test_a_load_command_with_an_impossible_size_is_refused(tmp_path):
    data = bytearray(mb.thin([mb.rpath_cmd("@loader_path")]))
    data[32 + 4:32 + 8] = (4).to_bytes(4, "little")  # cmdsize 4 < 8
    with pytest.raises(macho.MachOError, match="bad size"):
        macho.load_commands(_write(tmp_path, bytes(data)))


def test_truncated_load_commands_are_refused(tmp_path):
    data = mb.thin([mb.rpath_cmd("@loader_path")])
    with pytest.raises(macho.MachOError, match="truncated"):
        macho.load_commands(_write(tmp_path, data[:-4]))


def test_cryptids_lists_every_encryption_command_of_every_slice(tmp_path):
    a = mb.thin([mb.encryption_cmd(0)])
    b = mb.thin([mb.encryption_cmd(1)], cputype=mb.CPU_X86_64)
    p = _write(tmp_path, mb.fat([(mb.CPU_ARM64, a), (mb.CPU_X86_64, b)]))
    assert macho.cryptids(p) == [0, 1]


def test_a_binary_with_no_encryption_command_has_no_cryptids(tmp_path):
    assert macho.cryptids(_write(tmp_path, mb.thin([mb.rpath_cmd("@loader_path")]))) == []


def test_a_truncated_encryption_command_raises_rather_than_reading_as_decrypted(tmp_path):
    short = mb.encryption_cmd(1)[:16]
    short = short[:4] + (16).to_bytes(4, "little") + short[8:]
    with pytest.raises(macho.MachOError, match="too short"):
        macho.cryptids(_write(tmp_path, mb.thin([short])))


def test_dylib_names_are_dependencies_only_never_the_files_own_id(tmp_path):
    data = mb.thin([
        mb.dylib_cmd("/Library/MobileSubstrate/DynamicLibraries/libexample.dylib", cmd=mb.LC_ID_DYLIB),
        mb.dylib_cmd("/usr/lib/libobjc.A.dylib"),
        mb.dylib_cmd("/System/Library/PrivateFrameworks/Preferences.framework/Preferences",
                     cmd=mb.LC_LOAD_WEAK_DYLIB),
        mb.dylib_cmd("/usr/lib/libobjc.A.dylib"),  # a duplicate, as a fat file's 2nd slice would give
    ], filetype=mb.MH_DYLIB)
    assert macho.dylib_names(_write(tmp_path, data)) == [
        "/usr/lib/libobjc.A.dylib",
        "/System/Library/PrivateFrameworks/Preferences.framework/Preferences",
    ]


@pytest.mark.parametrize("cmd", [
    mb.LC_LOAD_DYLIB, mb.LC_LOAD_WEAK_DYLIB, mb.LC_REEXPORT_DYLIB, mb.LC_LAZY_LOAD_DYLIB,
    mb.LC_LOAD_UPWARD_DYLIB,
], ids=["load", "weak", "reexport", "lazy", "upward"])
def test_every_kind_of_dylib_dependency_is_a_dependency(tmp_path, cmd):
    """A tweak that re-exports or upward-links Preferences still needs its
    stand-in on a Mac: missing one of these read as "nothing to stub"."""
    data = mb.thin([mb.dylib_cmd("/System/Library/PrivateFrameworks/Preferences.framework/Preferences", cmd=cmd)],
                   filetype=mb.MH_DYLIB)
    assert macho.dylib_names(_write(tmp_path, data)) == [
        "/System/Library/PrivateFrameworks/Preferences.framework/Preferences",
    ]


@pytest.mark.parametrize("little", [True, False])
def test_a_32_bit_encryption_command_is_read(tmp_path, little):
    """An encrypted armv7 slice: LC_ENCRYPTION_INFO, not the _64 one."""
    data = mb.thin([mb.encryption_cmd(1, is64=False, little=little)], is64=False, little=little)
    assert macho.cryptids(_write(tmp_path, data)) == [1]


def test_rpaths_are_read_from_every_slice_without_duplicates(tmp_path):
    a = mb.thin([mb.rpath_cmd("@loader_path"), mb.rpath_cmd("@executable_path/Frameworks")])
    b = mb.thin([mb.rpath_cmd("@loader_path")], cputype=mb.CPU_X86_64)
    p = _write(tmp_path, mb.fat([(mb.CPU_ARM64, a), (mb.CPU_X86_64, b)]))
    assert macho.rpaths(p) == ["@loader_path", "@executable_path/Frameworks"]


def test_a_string_offset_outside_its_command_raises(tmp_path):
    cmd = bytearray(mb.rpath_cmd("@loader_path"))
    cmd[8:12] = (999).to_bytes(4, "little")
    with pytest.raises(macho.MachOError, match="outside itself"):
        macho.rpaths(_write(tmp_path, mb.thin([bytes(cmd)])))


# ---- cross-checks against the real otool, where there is one ----

def _working_otool() -> bool:
    path = shutil.which("otool")
    if not path:
        return False
    probe = subprocess.run([path, "--version"], capture_output=True, text=True)
    return probe.returncode == 0


needs_otool = pytest.mark.skipif(not _working_otool(), reason="needs a working otool to compare against")


def _otool_cryptids(p: Path) -> list[int]:
    out = subprocess.run(["otool", "-l", str(p)], capture_output=True, text=True, check=True).stdout
    return [int(line.split()[1]) for line in out.splitlines() if line.strip().startswith("cryptid ")]


def _otool_deps(p: Path) -> list[str]:
    out = subprocess.run(["otool", "-L", str(p)], capture_output=True, text=True, check=True).stdout
    deps = [line.split()[0] for line in out.splitlines() if line.startswith(("\t", " ")) and line.strip()]
    own = subprocess.run(["otool", "-D", str(p)], capture_output=True, text=True).stdout.splitlines()[1:]
    out_deps: list[str] = []
    for d in deps:
        if d not in own and d not in out_deps:
            out_deps.append(d)
    return out_deps


@needs_otool
@pytest.mark.parametrize("cryptid", [0, 1])
@pytest.mark.parametrize("is64", [True, False])
def test_cryptids_match_otool_on_synthetic_binaries(tmp_path, cryptid, is64):
    p = _write(tmp_path, mb.thin([mb.encryption_cmd(cryptid, is64=is64)], is64=is64))
    assert macho.cryptids(p) == _otool_cryptids(p)


def _real_binaries() -> list[Path]:
    found = [Path(sys.executable).resolve(), Path("/bin/ls")]
    libdir = sysconfig.get_config_var("LIBDIR")
    if libdir:
        found += sorted(Path(libdir).glob("libpython3*.dylib"))[:1]
    return [p for p in found if p.is_file()]


@needs_otool
@pytest.mark.parametrize("binary", _real_binaries(), ids=lambda p: p.name)
def test_dylib_names_and_cryptids_match_otool_on_real_binaries(binary):
    assert macho.dylib_names(binary) == _otool_deps(binary)
    assert macho.cryptids(binary) == _otool_cryptids(binary)


# --- universal static libraries (review #18) ---------------------------------------
#
# `lipo -create` of two `.a` archives is a well-formed universal file whose
# slices are `ar` archives, not images. 1.1's `otool -l` read it with exit 0
# and no cryptid; the reader refused it, and with it the whole IPA.

#: The start of an `ar` archive: its magic, then a member header.
AR = b"!<arch>\n" + b"/               0           0     0     644     4         `\n" + b"\0" * 4


@pytest.mark.parametrize("is64", [False, True])
def test_a_universal_static_library_is_not_a_mach_o(tmp_path, is64):
    p = _write(tmp_path, mb.fat([(mb.CPU_ARM64, AR), (mb.CPU_X86_64, AR)], is64=is64), "libsdk.a")
    with pytest.raises(macho.NotMachO, match="no Mach-O slice"):
        macho.load_commands(p)


def test_a_slice_that_is_not_a_mach_o_is_left_out_and_the_rest_still_read(tmp_path):
    """Never a way to hide an encrypted slice: the Mach-O beside it is read."""
    enc = mb.thin([mb.encryption_cmd(1)])
    p = _write(tmp_path, mb.fat([(mb.CPU_ARM64, enc), (mb.CPU_X86_64, AR)]))
    assert macho.cryptids(p) == [1]


# --- offsets that are no position in the file (review #19, #24) --------------------


def _fat64_at(offset: int) -> bytes:
    return (struct.pack(">II", mb.FAT_MAGIC_64, 1)
            + struct.pack(">iiQQII", mb.CPU_ARM64, 0, offset, 64, 14, 0) + b"\0" * 64)


@pytest.mark.parametrize("offset", [2**63, 2**64 - 1, 2**63 - 1, 4096])
@pytest.mark.parametrize("read", [macho.load_commands, macho.cryptids, macho.rpaths, macho.dylib_names],
                         ids=lambda f: f.__name__)
def test_a_slice_offset_past_the_end_of_the_file_is_a_machoerror(tmp_path, offset, read):
    """2^63 and up used to reach `seek` and raise ValueError, past every
    MachOError handler, and so out of the "couldn't read X" message."""
    with pytest.raises(macho.MachOError, match="past the end of the file") as e:
        read(_write(tmp_path, _fat64_at(offset)))
    assert not isinstance(e.value, macho.NotMachO)


def test_a_fat32_slice_offset_past_the_end_is_a_machoerror_too(tmp_path):
    data = struct.pack(">II", mb.FAT_MAGIC, 1) + struct.pack(">iiIII", mb.CPU_ARM64, 0, 1 << 20, 64, 14)
    with pytest.raises(macho.MachOError, match="past the end of the file"):
        macho.load_commands(_write(tmp_path, data))


def test_check_cryptid_explains_a_huge_offset_by_name(tmp_path):
    from graft_ipa import ipa_inspect

    with pytest.raises(ipa_inspect.IPAInspectionError, match="couldn't read Example's load commands"):
        ipa_inspect.check_cryptid(_write(tmp_path, _fat64_at(2**63), "Example"))


# --- what the reader may hold, for the whole file (review #20) -------------------


def _same_slice_n_times(thin: bytes, n: int) -> bytes:
    """A fat header with `n` entries that all name one slice: what a crafted
    file does to make each read of one region count again."""
    offset = 8 + 20 * n
    table = b"".join(struct.pack(">iiIII", mb.CPU_ARM64, 0, offset, len(thin), 0) for _ in range(n))
    return struct.pack(">II", mb.FAT_MAGIC, n) + table + thin


def test_the_load_command_bytes_are_bounded_across_slices_not_per_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(macho, "MAX_SIZEOFCMDS", 1024)
    thin = mb.thin([mb.rpath_cmd("x" * 600)])  # one slice: well under the cap
    assert len(macho.load_commands(_write(tmp_path, _same_slice_n_times(thin, 1)))) == 1
    with pytest.raises(macho.MachOError, match="not plausible"):
        macho.load_commands(_write(tmp_path, _same_slice_n_times(thin, 2)))


def test_the_load_command_count_is_bounded_across_slices_too(tmp_path, monkeypatch):
    monkeypatch.setattr(macho, "MAX_LOAD_COMMANDS", 4)
    thin = mb.thin([mb.rpath_cmd(f"/r{i}") for i in range(3)])
    assert len(macho.load_commands(_write(tmp_path, thin))) == 1
    with pytest.raises(macho.MachOError, match="not plausible"):
        macho.load_commands(_write(tmp_path, _same_slice_n_times(thin, 2)))
    with pytest.raises(macho.MachOError, match="not plausible"):
        macho.load_commands(_write(tmp_path, mb.thin([mb.rpath_cmd(f"/r{i}") for i in range(5)])))


def test_a_real_universal_binary_is_well_within_the_budget(tmp_path):
    slices = [(cpu, mb.thin([mb.encryption_cmd(0)] + [mb.dylib_cmd(f"/usr/lib/l{i}.dylib") for i in range(300)],
                            cputype=cpu)) for cpu in (mb.CPU_ARM64, mb.CPU_X86_64)]
    assert [len(s) for s in macho.load_commands(_write(tmp_path, mb.fat(slices)))] == [301, 301]


# --- dedupe (review #21) ---------------------------------------------------------


def test_names_are_deduplicated_without_comparing_each_to_all_the_others():
    """A list membership test was N^2 comparisons: 400K distinct
    LC_LOAD_DYLIB names in one crafted dylib was a quarter of an hour."""
    compared = 0

    class Counting(str):
        __hash__ = str.__hash__

        def __eq__(self, other):
            nonlocal compared
            compared += 1
            return str.__eq__(self, other)

    names = [Counting(f"/usr/lib/lib{i}.dylib") for i in range(2000)]
    again = [Counting(str(n)) for n in names[:10]]
    assert macho._unique(names + again) == names
    assert compared < 100


# --- the shipped bounds themselves (re-check #33) ------------------------------
#
# The two tests above patch the constants down to prove the budget is shared
# across slices. These run the shipped values: raising MAX_LOAD_COMMANDS to
# 1 << 22 left every test green while one crafted thin file then cost some
# 400 MB and two seconds.


def _thin_with_commands(n: int) -> bytes:
    """`n` minimal (8-byte) load commands of a type the reader doesn't know."""
    return mb.thin([struct.pack("<II", 0x7FFF_0000, 8)] * n)


def test_the_shipped_command_count_is_the_bound(tmp_path):
    at_cap = tmp_path / "at-cap"
    at_cap.write_bytes(_thin_with_commands(65_536))
    assert len(macho.load_commands(at_cap)[0]) == 65_536

    over = tmp_path / "over"
    over.write_bytes(_thin_with_commands(65_537))
    with pytest.raises(macho.MachOError, match="65537 more load commands is not plausible"):
        macho.load_commands(over)


def test_the_shipped_byte_bound_holds_for_one_thin_file(tmp_path):
    # One command whose size claims a little over 16 MB, in a file that has it.
    size = 16 * 1024 * 1024 + 8
    path = tmp_path / "huge"
    path.write_bytes(mb.thin([struct.pack("<II", 0x7FFF_0000, size) + b"\0" * (size - 8)]))
    with pytest.raises(macho.MachOError, match="more bytes of load commands is not plausible"):
        macho.load_commands(path)


# ---- platform and minimum OS (review #6/#34: iOS code isn't re-signed; #14/#36: macOS 15) ----

def test_each_slices_platform_and_minimum_os_come_from_its_load_commands(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(mb.thin([mb.build_version_cmd(mb.PLATFORM_MACOS, "15.2.1")]))
    assert macho.build_versions(p) == [(macho.PLATFORM_MACOS, (15, 2, 1))]
    p.write_bytes(mb.thin([mb.version_min_cmd(mb.LC_VERSION_MIN_IPHONEOS, "11.0", little=False)], little=False))
    assert macho.build_versions(p) == [(macho.PLATFORM_IOS, (11, 0, 0))]
    p.write_bytes(mb.fat([(mb.CPU_ARM64, mb.thin([mb.version_min_cmd(mb.LC_VERSION_MIN_MACOSX, "10.15")])),
                          (mb.CPU_X86_64, mb.thin([mb.build_version_cmd(mb.PLATFORM_MACCATALYST, "14.0")]))]))
    assert macho.build_versions(p) == [(macho.PLATFORM_MACOS, (10, 15, 0)), (macho.PLATFORM_MACCATALYST, (14, 0, 0))]
    p.write_bytes(mb.thin([]))
    assert macho.build_versions(p) == [(None, None)], "a slice that says nothing says so"


def test_ios_code_is_any_slice_built_for_a_device_platform(tmp_path):
    p = tmp_path / "x"
    for cmds, ios in (([mb.build_version_cmd(mb.PLATFORM_IOS, "14.0")], True),
                      ([mb.version_min_cmd(mb.LC_VERSION_MIN_IPHONEOS, "11.0")], True),
                      ([mb.build_version_cmd(mb.PLATFORM_MACOS, "15.0")], False),
                      ([mb.build_version_cmd(mb.PLATFORM_MACCATALYST, "15.0")], False),
                      ([], False)):
        p.write_bytes(mb.thin(cmds))
        assert macho.is_ios(p) is ios, cmds


# ---- architectures: which processors a file has code for (the Intel build's updater) ----

@pytest.mark.parametrize("little", [True, False])
def test_a_thin_file_is_its_own_headers_architecture(tmp_path, little):
    p = tmp_path / "x"
    p.write_bytes(mb.thin([], cputype=mb.CPU_X86_64, cpusubtype=mb.CPU_SUBTYPE_X86_64_ALL, little=little))
    assert macho.architectures(p) == ["x86_64"]
    p.write_bytes(mb.thin([], little=little))
    assert macho.architectures(p) == ["arm64"]
    p.write_bytes(mb.thin([], cputype=mb.CPU_I386, is64=False, little=little))
    assert macho.architectures(p) == ["i386"]


@pytest.mark.parametrize("is64", [False, True])
def test_a_universal_file_has_each_slices_architecture_in_file_order(tmp_path, is64):
    p = tmp_path / "x"
    p.write_bytes(mb.fat([(mb.CPU_X86_64, mb.thin([], cputype=mb.CPU_X86_64)),
                          (mb.CPU_ARM64, mb.thin([mb.rpath_cmd("@loader_path")]))], is64=is64))
    assert macho.architectures(p) == ["x86_64", "arm64"]


def test_arm64e_and_capability_bits_are_told_apart_from_plain_arm64(tmp_path):
    """arm64e is its own architecture, which a plain arm64 process can't load;
    a subtype's top byte (here LIB64) is capability bits, not a subtype."""
    p = tmp_path / "x"
    p.write_bytes(mb.thin([], cpusubtype=mb.CPU_SUBTYPE_ARM64E))
    assert macho.architectures(p) == ["arm64e"]
    p.write_bytes(mb.thin([], cpusubtype=mb.CPU_SUBTYPE_ARM64E | mb.CPU_SUBTYPE_LIB64))
    assert macho.architectures(p) == ["arm64e"]
    p.write_bytes(mb.thin([], cputype=mb.CPU_X86_64, cpusubtype=mb.CPU_SUBTYPE_X86_64_ALL | mb.CPU_SUBTYPE_LIB64))
    assert macho.architectures(p) == ["x86_64"]
    p.write_bytes(mb.fat([(mb.CPU_ARM64, mb.CPU_SUBTYPE_ARM64E, mb.thin([], cpusubtype=mb.CPU_SUBTYPE_ARM64E)),
                          (mb.CPU_ARM64, mb.thin([]))]))
    assert macho.architectures(p) == ["arm64e", "arm64"]


def test_an_unknown_processor_is_named_by_its_numbers(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(mb.thin([], cputype=0x0100_0099, cpusubtype=5))
    assert macho.architectures(p) == ["cputype 0x1000099 subtype 5"]


def test_a_slice_whose_header_disagrees_with_the_fat_table_is_refused(tmp_path):
    """The table is what the kernel picks a slice by; the header is what the
    slice is. A file where they differ has no answer to "which processors"."""
    p = tmp_path / "x"
    p.write_bytes(mb.fat([(mb.CPU_X86_64, mb.thin([])), (mb.CPU_ARM64, mb.thin([]))]))
    with pytest.raises(macho.MachOError, match="its fat table says"):
        macho.architectures(p)
    p.write_bytes(mb.fat([(mb.CPU_ARM64, mb.thin([], cpusubtype=mb.CPU_SUBTYPE_ARM64E))]))
    with pytest.raises(macho.MachOError, match="its fat table says"):
        macho.architectures(p)


@pytest.mark.parametrize("data", [
    pytest.param(b"#!/bin/sh\nexit 0\n", id="a script"),
    pytest.param(b"", id="empty"),
    pytest.param(b"\xca\xfe\xba\xbe\x00\x00\x00\x34" + b"\0" * 64, id="a Java class file"),
    pytest.param(mb.fat([(mb.CPU_ARM64, AR), (mb.CPU_X86_64, AR)]), id="a universal static library"),
])
def test_something_that_is_not_a_mach_o_has_no_architectures(tmp_path, data):
    p = tmp_path / "x"
    p.write_bytes(data)
    with pytest.raises(macho.NotMachO):
        macho.architectures(p)


@pytest.mark.parametrize("data", [
    pytest.param(struct.pack("<I", mb.MH_MAGIC_64) + b"\0\0", id="a truncated header"),
    pytest.param(b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\0" * 10, id="a truncated fat table"),
    pytest.param(_fat64_at(4096), id="a slice past the end"),
])
def test_a_broken_mach_o_has_no_architectures_either(tmp_path, data):
    p = tmp_path / "x"
    p.write_bytes(data)
    with pytest.raises(macho.MachOError):
        macho.architectures(p)


def test_a_missing_file_is_a_machoerror(tmp_path):
    with pytest.raises(macho.MachOError, match="couldn't read"):
        macho.architectures(tmp_path / "nothing")


def test_the_interpreter_running_this_test_has_code_for_this_process(tmp_path):
    """A real file, as the kernel loaded it: whatever this Python is (thin or
    universal), it has a slice for the architecture it is running as."""
    import platform

    assert platform.machine() in macho.architectures(Path(sys.executable).resolve())


def test_a_fat_slice_whose_header_is_cut_off_has_no_architectures(tmp_path):
    """A table entry pointing at the file's last bytes: too few for a magic
    is no answer, nor is a Mach-O magic with its cputype cut off. Never a
    guess at what the slice was for."""
    p = tmp_path / "x"
    whole = mb.fat([(mb.CPU_X86_64, mb.thin([], cputype=mb.CPU_X86_64))], align=0x1000)
    header = struct.pack(">II", mb.FAT_MAGIC, 1) + struct.pack(">iIIII", mb.CPU_X86_64, 0, 0x1000, 32, 12)
    p.write_bytes(header + b"\0" * (0x1000 - len(header)) + whole[0x1000:0x1000 + 2])
    with pytest.raises(macho.MachOError, match="too short"):
        macho.architectures(p)
    p.write_bytes(header + b"\0" * (0x1000 - len(header)) + whole[0x1000:0x1000 + 8])
    with pytest.raises(macho.MachOError, match="truncated"):
        macho.architectures(p)
