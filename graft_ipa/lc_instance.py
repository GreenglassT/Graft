"""Turn the upstream LiveContainer.app into instance 1, 2 or 3.

Three LiveContainers can live on one device side by side, and that is how
AltStore set this account up: `com.kdt.livecontainer.<TEAM>` (the primary, with
its extensions), `com.kdt.LiveContainer2.<TEAM>` and
`com.kdt.LiveContainer3.<TEAM>`. Those explicit App IDs exist in App Store
Connect with the shared app group assigned, which is why these are the ids to
build — `com.kdt.livecontainer` itself belongs to another team (ASC answers 409).

The secondaries mirror LiveContainer's own
`+[LCUtils archiveIPAWithBundleName:includingExtraInfoDict:]`
(LiveContainerSwiftUI/Utilities/LCUtils.m), i.e. exactly what its in-app
"Install Another LiveContainer" would produce if this device had AltStore to
install it with:

* display/bundle name `LiveContainerN`;
* URL scheme `livecontainerN`, and only one URL type. **This is what makes an
  instance secondary**: `multiLCStatus` decides by the scheme, so an instance
  that kept `livecontainer` would claim the primary's scheme and believe it was
  the primary;
* no `UTExportedTypeDeclarations` (the primary owns the document type);
* grey icons, `PrimaryLiveContainerTeamId = <TEAM>`;
* the executable renamed to the instance name with a fresh `LC_UUID`, because
  iOS must not see two installed apps whose executables share a UUID;
* `PlugIns/` removed — extensions belong to the primary.

The output is unsigned; `lc_sign` signs it. Ported from
`scripts/lc_instance.py`, verified by hand on a real iPad before this existed.
"""

from __future__ import annotations

import plistlib
import shutil
import struct
import subprocess
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .child_env import child_env
from .lc_runner import LiveContainerError

#: Upstream's own bundle id, before any team suffix.
LC_ORIGINAL_ID = "com.kdt.livecontainer"

LC_UUID = 0x1B
MH_MAGIC_64 = 0xFEEDFACF
INSTANCES = (1, 2, 3)


def instance_name(instance: int) -> str:
    """`LiveContainer`, `LiveContainer2`, `LiveContainer3`."""
    _check(instance)
    return "LiveContainer" if instance == 1 else f"LiveContainer{instance}"


def instance_bundle_id(instance: int, team: str) -> str:
    """The App ID each instance is installed under — see the module doc."""
    _check(instance)
    if instance == 1:
        return f"{LC_ORIGINAL_ID}.{team}"
    return f"com.kdt.{instance_name(instance)}.{team}"


def _check(instance: int) -> None:
    if instance not in INSTANCES:
        raise ValueError(f"LiveContainer instance must be 1, 2 or 3, not {instance}")


@dataclass(frozen=True)
class PreparedInstance:
    instance: int
    bundle_id: str
    #: `<Name>.appex` → its new bundle id. Empty for a secondary, which has none.
    appex_ids: dict[str, str] = field(default_factory=dict)


def change_macho_uuid(path: Path) -> str:
    """Give a thin 64-bit Mach-O a new `LC_UUID`, in place. Returns it.

    LiveContainer's `LCChangeMachOUUID` does the same for its secondaries. The
    upstream binary is thin arm64, so a fat one is refused rather than handled:
    if upstream ever ships one, this should fail loudly, not half-work.
    """
    data = bytearray(path.read_bytes())
    if len(data) < 32:
        raise LiveContainerError(f"{path.name}: truncated Mach-O ({len(data)} bytes)")
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != MH_MAGIC_64:
        raise LiveContainerError(
            f"{path.name}: expected a thin 64-bit Mach-O, magic={magic:#x}",
            hint="upstream's LiveContainer.ipa layout changed; this needs updating",
        )
    ncmds = struct.unpack_from("<I", data, 16)[0]
    off = 32
    for _ in range(ncmds):
        # Every read is bounds-checked: a load command that runs past the end
        # of the file is a truncated binary, said as such rather than as
        # struct.error from deep inside the walk.
        if off + 8 > len(data):
            raise LiveContainerError(f"{path.name}: truncated Mach-O (load commands cut off)")
        cmd, size = struct.unpack_from("<II", data, off)
        if size < 8 or off + size > len(data):
            raise LiveContainerError(f"{path.name}: truncated Mach-O (load command overruns)")
        if cmd == LC_UUID:
            if size < 24:
                raise LiveContainerError(f"{path.name}: truncated Mach-O (short LC_UUID)")
            new = uuid.uuid4()
            data[off + 8 : off + 24] = new.bytes
            path.write_bytes(bytes(data))
            return str(new).upper()
        off += size
    raise LiveContainerError(f"{path.name}: no LC_UUID load command")


def bundle_file(bundle: Path, info: dict, key: str = "CFBundleExecutable") -> Path:
    """The file an Info.plist `key` names inside `bundle`, checked to be one.

    The IPA is downloaded, so its plist is untrusted input, and this module
    reads, patches and renames the file it names. A tampered
    `CFBundleExecutable` of `../../x` or `/Users/…/x` would otherwise point all
    three at a file outside the extraction directory. So the value must be a
    single plain file name, the joined path must resolve inside the bundle, and
    it must be a regular file that is not a symlink. Every caller that turns a
    plist value into a path — here and in `lc_sign` — goes through this.
    """
    value = info.get(key)
    where = f"{bundle.name}: {key} {value!r}"
    if (
        not isinstance(value, str)
        or value in ("", ".", "..")
        or "/" in value
        or "\\" in value
        or "\0" in value
        or Path(value).is_absolute()
        or Path(value).name != value
    ):
        raise LiveContainerError(f"{where} is not a plain file name inside the bundle")
    target = bundle / value
    if target.is_symlink():
        raise LiveContainerError(f"{where} is a symlink")
    # Belt and braces: given the plain-name rule above and the symlink refusal,
    # nothing can reach this today (mutation-checked: deleting it fails no
    # test). It stays so the guarantee does not rest on one string test.
    if not target.resolve().is_relative_to(bundle.resolve()):
        raise LiveContainerError(f"{where} is not a plain file name inside the bundle")
    if not target.is_file():
        raise LiveContainerError(f"{where} is not a file")
    return target


def check_bundle_dir(path: Path) -> Path:
    """`path`, refused if it is a symlink — a nested bundle must be a real
    directory inside the app, or edits through it land wherever it points."""
    if path.is_symlink():
        raise LiveContainerError(f"{path.name} is a symlink, not a bundle inside the app")
    return path


def _load_plist(path: Path) -> tuple[dict, plistlib.PlistFormat]:
    raw = path.read_bytes()
    fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
    return plistlib.loads(raw), fmt


def prepare_app(app: Path, *, team: str, instance: int) -> PreparedInstance:
    """Edit an extracted `LiveContainer.app` in place into `instance`."""
    _check(instance)
    info_path = app / "Info.plist"
    info, fmt = _load_plist(info_path)
    # Validated before anything is written, for every instance: the signer
    # reads these too, and a bad one should stop the run while nothing has
    # been touched yet.
    exe_path = bundle_file(app, info)
    appexes = [check_bundle_dir(a) for a in sorted(app.glob("PlugIns/*.appex"))]
    for appex in appexes:
        bundle_file(appex, _load_plist(appex / "Info.plist")[0])
    orig_id = info["CFBundleIdentifier"]
    new_id = instance_bundle_id(instance, team)
    info["CFBundleIdentifier"] = new_id
    appex_ids: dict[str, str] = {}

    if instance == 1:
        # AltStore's naming: every extension keeps its suffix under the new id.
        for appex in appexes:
            path = appex / "Info.plist"
            ai, afmt = _load_plist(path)
            old = ai["CFBundleIdentifier"]
            if not old.startswith(orig_id + "."):
                raise LiveContainerError(
                    f"{appex.name}: unexpected bundle id {old} (not under {orig_id})"
                )
            ai["CFBundleIdentifier"] = new_id + old[len(orig_id) :]
            path.write_bytes(plistlib.dumps(ai, fmt=afmt))
            appex_ids[appex.name] = ai["CFBundleIdentifier"]
    else:
        name = instance_name(instance)
        info["CFBundleDisplayName"] = name
        info["CFBundleName"] = name
        url_types = info.get("CFBundleURLTypes")
        if not (
            isinstance(url_types, list) and url_types and isinstance(url_types[0], dict)
            and isinstance(url_types[0].get("CFBundleURLSchemes"), list)
            and url_types[0]["CFBundleURLSchemes"]
        ):
            raise LiveContainerError(
                f"{app.name}: CFBundleURLTypes is not the shape LiveContainer ships "
                "(a list whose first entry has CFBundleURLSchemes)",
                hint="upstream's Info.plist changed; this needs updating",
            )
        url_types[0]["CFBundleURLSchemes"][0] = name.lower()
        del url_types[1:]
        info.pop("UTExportedTypeDeclarations", None)
        info["CFBundleIconName"] = "AppIconGrey"
        for key, files in (
            ("CFBundleIcons", ["AppIconGrey60x60"]),
            ("CFBundleIcons~ipad", ["AppIconGrey60x60", "AppIconGrey76x76"]),
        ):
            primary = info.setdefault(key, {}).setdefault("CFBundlePrimaryIcon", {})
            if "CFBundleIconName" in primary:
                primary["CFBundleIconName"] = "AppIconGrey"
            icon_files = primary.setdefault("CFBundleIconFiles", [])
            for i, f in enumerate(files):
                if i < len(icon_files):
                    icon_files[i] = f
                else:
                    icon_files.append(f)
        info["PrimaryLiveContainerTeamId"] = team
        change_macho_uuid(exe_path)
        exe_path.rename(app / name)
        info["CFBundleExecutable"] = name
        shutil.rmtree(app / "PlugIns", ignore_errors=True)

    info_path.write_bytes(plistlib.dumps(info, fmt=fmt))
    return PreparedInstance(instance=instance, bundle_id=new_id, appex_ids=appex_ids)


def extract_app(ipa: Path, dest: Path) -> Path:
    """Unzip `ipa` into `dest` and return its one `Payload/*.app`."""
    with zipfile.ZipFile(ipa) as z:
        z.extractall(dest)
    apps = sorted((dest / "Payload").glob("*.app"))
    if len(apps) != 1:
        raise LiveContainerError(f"{ipa.name}: expected one .app in Payload, found {len(apps)}")
    return apps[0]


def archive_payload(root: Path, ipa_out: Path) -> None:
    """Zip `root/Payload` into `ipa_out`, replacing it.

    `zip -qry` as the verified script did: `-y` keeps any symlink a symlink
    rather than inlining its target, which would break a sealed signature.
    """
    ipa_out.parent.mkdir(parents=True, exist_ok=True)
    ipa_out.unlink(missing_ok=True)
    subprocess.run(
        ["zip", "-qry", str(ipa_out.resolve()), "Payload"],
        cwd=root, check=True, capture_output=True, env=child_env(),
    )
