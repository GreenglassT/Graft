"""Turning the upstream LiveContainer.app into instance 1, 2 or 3.

Instances 2 and 3 mirror LiveContainer's own
`+[LCUtils archiveIPAWithBundleName:includingExtraInfoDict:]`. The URL scheme is
the part that matters most and is easiest to miss: `multiLCStatus` decides that
an instance is a *secondary* LiveContainer by its scheme, so a LiveContainer2
that kept `livecontainer` would claim the primary's scheme and believe it was
the primary.

The app here is synthetic — the real Info.plist keys these edits touch, and a
thin arm64 Mach-O built byte by byte with one `LC_UUID` command — so every
assertion is about an edit, not about upstream's current layout.
"""

from __future__ import annotations

import plistlib
import struct
import uuid
import zipfile
from pathlib import Path

import pytest

from graft_ipa import lc_instance
from graft_ipa.lc_runner import LiveContainerError

TEAM = "ABCDE12345"
EXTENSIONS = ("LaunchAppExtension", "LiveProcess", "ShareExtension")
OLD_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")

MH_MAGIC_64 = 0xFEEDFACF
LC_UUID = 0x1B
LC_SEGMENT_64 = 0x19


def _macho(*, with_uuid: bool = True, magic: int = MH_MAGIC_64) -> bytes:
    """A thin arm64 Mach-O header, one filler load command, then LC_UUID.

    The filler sits first so the walk has to step over a command to find the
    UUID — a parser that only looks at the first command would pass a fixture
    where LC_UUID came first.
    """
    filler = struct.pack("<II", LC_SEGMENT_64, 72) + b"\0" * 64
    cmds = [filler]
    if with_uuid:
        cmds.append(struct.pack("<II", LC_UUID, 24) + OLD_UUID.bytes)
    body = b"".join(cmds)
    header = struct.pack("<IiiIIIII", magic, 0x0100000C, 0, 2, len(cmds), len(body), 0, 0)
    return header + body + b"\0" * 64


def _read_uuid(path: Path) -> uuid.UUID:
    data = path.read_bytes()
    off = 32
    for _ in range(struct.unpack_from("<I", data, 16)[0]):
        cmd, size = struct.unpack_from("<II", data, off)
        if cmd == LC_UUID:
            return uuid.UUID(bytes=data[off + 8 : off + 24])
        off += size
    raise AssertionError("no LC_UUID")


def _icons(files: list[str]) -> dict:
    return {
        "CFBundleAlternateIcons": {"AppIconGrey": {"CFBundleIconName": "AppIconGrey"}},
        "CFBundlePrimaryIcon": {"CFBundleIconFiles": files, "CFBundleIconName": "AppIcon"},
    }


def _make_app(root: Path, *, binary: bytes | None = None) -> Path:
    app = root / "Payload" / "LiveContainer.app"
    app.mkdir(parents=True)
    info = {
        "CFBundleIdentifier": "com.kdt.livecontainer",
        "CFBundleExecutable": "LiveContainer",
        "CFBundleName": "LiveContainer",
        "CFBundleDisplayName": "LiveContainer",
        "CFBundleShortVersionString": "3.8.14",
        "CFBundleIconName": "AppIcon",
        "CFBundleIcons": _icons(["AppIcon60x60"]),
        "CFBundleIcons~ipad": _icons(["AppIcon60x60", "AppIcon76x76"]),
        "CFBundleURLTypes": [
            {"CFBundleURLName": "com.kdt.livecontainer.urlscheme",
             "CFBundleURLSchemes": ["livecontainer"]},
            {"CFBundleURLName": "sidestore", "CFBundleURLSchemes": ["sidestore"]},
        ],
        "UTExportedTypeDeclarations": [{"UTTypeIdentifier": "com.kdt.livecontainer.lcapp"}],
    }
    (app / "Info.plist").write_bytes(plistlib.dumps(info))
    (app / "LiveContainer").write_bytes(binary if binary is not None else _macho())
    for ext in EXTENSIONS:
        appex = app / "PlugIns" / f"{ext}.appex"
        appex.mkdir(parents=True)
        # Binary plist on purpose: the format must be preserved on write-back.
        (appex / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleIdentifier": f"com.kdt.livecontainer.{ext}", "CFBundleExecutable": ext},
            fmt=plistlib.FMT_BINARY,
        ))
        (appex / ext).write_bytes(_macho())
    return app


def _info(app: Path) -> dict:
    return plistlib.loads((app / "Info.plist").read_bytes())


# ---- identities ----


def test_instance_bundle_ids_carry_the_team_suffix() -> None:
    """`com.kdt.livecontainer` itself belongs to another team (ASC 409), and
    the `.TEAM` ids are the ones AltStore registered with app groups."""
    assert lc_instance.instance_bundle_id(1, TEAM) == f"com.kdt.livecontainer.{TEAM}"
    assert lc_instance.instance_bundle_id(2, TEAM) == f"com.kdt.LiveContainer2.{TEAM}"
    assert lc_instance.instance_bundle_id(3, TEAM) == f"com.kdt.LiveContainer3.{TEAM}"


@pytest.mark.parametrize("bad", [0, 4])
def test_there_are_three_instances(bad: int) -> None:
    with pytest.raises(ValueError):
        lc_instance.instance_bundle_id(bad, TEAM)


# ---- primary ----


def test_the_primary_renames_the_app_and_every_extension(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    prepared = lc_instance.prepare_app(app, team=TEAM, instance=1)

    main = f"com.kdt.livecontainer.{TEAM}"
    assert prepared.bundle_id == main
    assert _info(app)["CFBundleIdentifier"] == main
    for ext in EXTENSIONS:
        plist_path = app / "PlugIns" / f"{ext}.appex" / "Info.plist"
        assert plistlib.loads(plist_path.read_bytes())["CFBundleIdentifier"] == f"{main}.{ext}"
        assert plist_path.read_bytes().startswith(b"bplist")
    assert prepared.appex_ids == {f"{ext}.appex": f"{main}.{ext}" for ext in EXTENSIONS}


def test_the_primary_keeps_its_scheme_executable_and_extensions(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    lc_instance.prepare_app(app, team=TEAM, instance=1)

    info = _info(app)
    assert info["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["livecontainer"]
    assert info["CFBundleExecutable"] == "LiveContainer"
    assert _read_uuid(app / "LiveContainer") == OLD_UUID
    assert (app / "PlugIns").is_dir()


def test_an_extension_outside_the_family_refuses(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    stray = app / "PlugIns" / "LiveProcess.appex" / "Info.plist"
    stray.write_bytes(plistlib.dumps({"CFBundleIdentifier": "org.example.other"}))

    with pytest.raises(LiveContainerError):
        lc_instance.prepare_app(app, team=TEAM, instance=1)


# ---- secondary ----


def test_a_secondary_has_its_own_identity_and_scheme(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    prepared = lc_instance.prepare_app(app, team=TEAM, instance=2)

    info = _info(app)
    assert prepared.bundle_id == f"com.kdt.LiveContainer2.{TEAM}"
    assert prepared.appex_ids == {}
    assert info["CFBundleIdentifier"] == f"com.kdt.LiveContainer2.{TEAM}"
    assert info["CFBundleDisplayName"] == "LiveContainer2"
    assert info["CFBundleName"] == "LiveContainer2"
    # What makes it secondary — `multiLCStatus` keys off the scheme.
    assert info["CFBundleURLTypes"] == [
        {"CFBundleURLName": "com.kdt.livecontainer.urlscheme",
         "CFBundleURLSchemes": ["livecontainer2"]},
    ]
    assert "UTExportedTypeDeclarations" not in info
    assert info["PrimaryLiveContainerTeamId"] == TEAM


def test_a_secondary_has_grey_icons(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    lc_instance.prepare_app(app, team=TEAM, instance=3)

    info = _info(app)
    assert info["CFBundleIconName"] == "AppIconGrey"
    phone = info["CFBundleIcons"]["CFBundlePrimaryIcon"]
    pad = info["CFBundleIcons~ipad"]["CFBundlePrimaryIcon"]
    assert phone == {"CFBundleIconFiles": ["AppIconGrey60x60"], "CFBundleIconName": "AppIconGrey"}
    assert pad == {
        "CFBundleIconFiles": ["AppIconGrey60x60", "AppIconGrey76x76"],
        "CFBundleIconName": "AppIconGrey",
    }


def test_a_secondary_renames_its_executable_with_a_fresh_uuid(tmp_path: Path) -> None:
    """iOS must not see two installed apps whose executables share a UUID."""
    app = _make_app(tmp_path)

    lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert _info(app)["CFBundleExecutable"] == "LiveContainer2"
    assert not (app / "LiveContainer").exists()
    new = _read_uuid(app / "LiveContainer2")
    assert new != OLD_UUID
    # Only the 16 UUID bytes changed.
    before, after = _macho(), (app / "LiveContainer2").read_bytes()
    assert len(before) == len(after)
    assert sum(a != b for a, b in zip(before, after, strict=True)) <= 16


def test_a_secondary_drops_its_extensions(tmp_path: Path) -> None:
    app = _make_app(tmp_path)

    lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert not (app / "PlugIns").exists()


def test_a_fat_binary_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path, binary=_macho(magic=0xBEBAFECA))

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert exc.value.message.startswith("LiveContainer: expected a thin 64-bit Mach-O")


def test_a_binary_without_lc_uuid_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path, binary=_macho(with_uuid=False))

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert exc.value.message == "LiveContainer: no LC_UUID load command"


# ---- plist values are untrusted ----
#
# The IPA is downloaded. Its Info.plist names the executable this module reads,
# patches and renames, so a tampered `CFBundleExecutable` of `../../x` or an
# absolute path would make us edit files outside the extraction directory.


def _with_executable(app: Path, value: str, *, appex: str | None = None) -> None:
    path = app / "PlugIns" / f"{appex}.appex" / "Info.plist" if appex else app / "Info.plist"
    info = plistlib.loads(path.read_bytes())
    info["CFBundleExecutable"] = value
    path.write_bytes(plistlib.dumps(info))


@pytest.mark.parametrize("instance", [1, 2])
@pytest.mark.parametrize(
    "bad",
    # The last one resolves inside the bundle to a real file: only the
    # plain-name rule refuses it, so it is what pins that rule on its own.
    ["../evil", "../../evil", "/tmp/evil", ".", "..", "a/b", "", "PlugIns/../LiveContainer"],
)
def test_an_executable_name_that_is_not_a_plain_file_name_is_refused(
    tmp_path: Path, bad: str, instance: int
) -> None:
    app = _make_app(tmp_path / "x")
    sentinel = tmp_path / "evil"
    sentinel.write_bytes(_macho())
    before = sentinel.read_bytes()
    _with_executable(app, bad if not bad.startswith("/") else str(sentinel))

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=instance)

    assert "CFBundleExecutable" in exc.value.message
    assert sentinel.read_bytes() == before
    assert not (app / "LiveContainer2").exists()


def test_an_extension_executable_outside_its_bundle_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path / "x")
    _with_executable(app, "../../LiveContainer", appex="LiveProcess")

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=1)

    assert exc.value.message == (
        "LiveProcess.appex: CFBundleExecutable '../../LiveContainer' is not a plain "
        "file name inside the bundle"
    )


def test_an_executable_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path / "x")
    outside = tmp_path / "outside"
    outside.write_bytes(_macho())
    before = outside.read_bytes()
    (app / "LiveContainer").unlink()
    (app / "LiveContainer").symlink_to(outside)

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert exc.value.message == "LiveContainer.app: CFBundleExecutable 'LiveContainer' is a symlink"
    assert outside.read_bytes() == before


def test_a_missing_executable_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path / "x")
    (app / "LiveContainer").unlink()

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert exc.value.message == "LiveContainer.app: CFBundleExecutable 'LiveContainer' is not a file"


def test_a_symlinked_extension_bundle_is_refused(tmp_path: Path) -> None:
    app = _make_app(tmp_path / "x")
    elsewhere = tmp_path / "Elsewhere.appex"
    (app / "PlugIns" / "ShareExtension.appex").rename(elsewhere)
    (app / "PlugIns" / "ShareExtension.appex").symlink_to(elsewhere)
    before = (elsewhere / "Info.plist").read_bytes()

    with pytest.raises(LiveContainerError):
        lc_instance.prepare_app(app, team=TEAM, instance=1)

    assert (elsewhere / "Info.plist").read_bytes() == before


# ---- the IPA round trip ----


def test_extract_and_archive_round_trip(tmp_path: Path) -> None:
    src = tmp_path / "src"
    _make_app(src)
    ipa = tmp_path / "in.ipa"
    with zipfile.ZipFile(ipa, "w") as z:
        for f in sorted((src / "Payload").rglob("*")):
            z.write(f, f.relative_to(src))

    app = lc_instance.extract_app(ipa, tmp_path / "work")
    assert app.name == "LiveContainer.app"
    out = tmp_path / "out" / "x.ipa"
    lc_instance.archive_payload(tmp_path / "work", out)

    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert "Payload/LiveContainer.app/Info.plist" in names
    assert "Payload/LiveContainer.app/PlugIns/LiveProcess.appex/Info.plist" in names


# ---- an Info.plist or binary of the wrong shape is a clean refusal ----


@pytest.mark.parametrize("url_types", ["nope", [], [{"CFBundleURLName": "x"}],
                                       [{"CFBundleURLSchemes": []}]])
def test_malformed_url_types_are_refused_cleanly(tmp_path: Path, url_types) -> None:
    app = _make_app(tmp_path)
    info = _info(app)
    info["CFBundleURLTypes"] = url_types
    (app / "Info.plist").write_bytes(plistlib.dumps(info))

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert "CFBundleURLTypes" in exc.value.message


@pytest.mark.parametrize("cut", [8, 20, 40, 60])
def test_a_truncated_binary_is_refused_cleanly(tmp_path: Path, cut: int) -> None:
    app = _make_app(tmp_path, binary=_macho()[:cut])

    with pytest.raises(LiveContainerError) as exc:
        lc_instance.prepare_app(app, team=TEAM, instance=2)

    assert "truncated" in exc.value.message or "no LC_UUID" in exc.value.message
