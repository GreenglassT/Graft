"""IPA inspection helpers: plist parsing, cryptid verification, version extraction."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import zipfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .child_env import child_env
from .errors import IPAInspectionError

IPA_ZIP_MAGIC = b"PK\x03\x04"


@dataclass
class IPAInfo:
    bundle_id: str
    version: str
    display_name: str
    executable: str
    info_plist_path_in_zip: str  # e.g. "Payload/Example.app/Info.plist"


def verify_zip(ipa: Path) -> None:
    """Verify the file is at least a zip archive."""
    if not ipa.exists():
        raise IPAInspectionError(f"IPA not found: {ipa}")
    with ipa.open("rb") as f:
        magic = f.read(4)
    if magic != IPA_ZIP_MAGIC:
        raise IPAInspectionError(
            f"{ipa} does not look like a valid IPA (bad zip magic)",
            hint="the file may be an HTML error page, a partial download, or corrupted",
        )


def read_info_plist(ipa: Path) -> IPAInfo:
    """Open the IPA without extracting and parse Payload/<App>.app/Info.plist."""
    verify_zip(ipa)
    try:
        with zipfile.ZipFile(ipa) as zf:
            info_path = None
            for name in zf.namelist():
                if (
                    name.startswith("Payload/")
                    and name.endswith(".app/Info.plist")
                    and name.count("/") == 2
                ):
                    info_path = name
                    break
            if info_path is None:
                raise IPAInspectionError(
                    f"could not find Payload/<App>.app/Info.plist inside {ipa}"
                )
            with zf.open(info_path) as f:
                plist = plistlib.load(f)
    except zipfile.BadZipFile as e:
        raise IPAInspectionError(f"{ipa} is not a valid zip archive: {e}") from e

    raw_bundle_id = plist.get("CFBundleIdentifier")
    # Checked for truthiness before coercion, deliberately: `str(0)` and
    # `str([])` are both truthy strings, and coercing first would make this
    # guard blind to the falsy values it exists to reject. `raw_bundle_id`
    # itself is exactly as free to be a non-string legal plist value as the
    # other three fields below — see the coercion paragraph there for why.
    if not raw_bundle_id:
        raise IPAInspectionError(f"Info.plist in {ipa} has no CFBundleIdentifier")

    fields = {
        "bundle_id": raw_bundle_id,
        "version": (
            plist.get("CFBundleShortVersionString")
            or plist.get("CFBundleVersion")
            or "unknown"
        ),
        "display_name": (
            plist.get("CFBundleDisplayName") or plist.get("CFBundleName") or "Unknown"
        ),
        "executable": plist.get("CFBundleExecutable") or "Unknown",
    }
    # None of these four is guaranteed to be a string — each is a bare
    # `plist.get(...)`, and every one is exactly as free to be a plist
    # integer, array or dict as any other. A hand-copied file is under no
    # obligation to have a well-typed Info.plist at all, and `find_in_cache`'s
    # third match strategy exists precisely to accept one. Coerced once, here,
    # rather than essayed field by field — each field's own consumer assumes a
    # string, and where an uncoerced value's failure mode is worth naming:
    #   - `bundle_id`: `find_in_cache` compares it directly against a
    #     configured bundle id, so an uncoerced non-string is silently unequal
    #     to every one and the file is skipped there too.
    #   - `display_name`: reaches `--staged-ipas`'s wire format untouched;
    #     `StagedIPARecord`'s `String`-typed decode throws, `compactMap { try?
    #     }` drops the record, and `StagedIPAStore`'s count check then
    #     refuses to publish anything at all — one bad plist going
    #     permanently dark for the whole feature.
    #   - `executable`: never reaches that wire format, but does reach
    #     `IPAInfoMemo`'s entries via `getattr(info, field)`, and
    #     `IPAInfoMemo.save()`'s `json.dumps` cannot serialize a raw
    #     `datetime` (a legal `<date>` `CFBundleExecutable`) — which used to
    #     crash `_do_staged_ipas` from `finally: memo.save()` mid-stream,
    #     before the `staged-ipas-end` sentinel. See `ipa_memo.save`'s own
    #     doc for the rest of that fix.
    return IPAInfo(
        **{field: str(value) for field, value in fields.items()},
        info_plist_path_in_zip=info_path,
    )


CRYPTID_RE = re.compile(r"cryptid\s+(\d+)")


def list_macho_in_extract(extract_dir: Path) -> list[Path]:
    """Find Mach-O binaries inside an extracted IPA directory.

    Targets the main app binary plus those under Frameworks/, PlugIns/, and Extensions/.
    Identification is via the Mach-O magic bytes — fast and reliable.
    """
    macho_magics = {
        b"\xca\xfe\xba\xbe",  # fat
        b"\xbe\xba\xfe\xca",  # fat (swapped)
        b"\xcf\xfa\xed\xfe",  # 64-bit
        b"\xfe\xed\xfa\xcf",  # 64-bit (swapped)
        b"\xce\xfa\xed\xfe",  # 32-bit
        b"\xfe\xed\xfa\xce",  # 32-bit (swapped)
    }
    results: list[Path] = []
    payload = extract_dir / "Payload"
    if not payload.exists():
        return results
    for p in payload.rglob("*"):
        # `is_file()` follows symlinks, and `extract_ipa` now materialises real
        # ones — so a framework's `Y -> Versions/A/Y` was counted as a second
        # Mach-O, inflating the "(N binaries, cryptid 0)" line, running `otool`
        # twice on one file, and handing `normalize_exec_bits` a link to chmod
        # *through*. The link's target is inside the bundle and gets walked on
        # its own account.
        if p.is_symlink() or not p.is_file():
            continue
        # Heuristic: only inspect files in app bundles / frameworks / plugins / extensions.
        s = str(p)
        if not any(
            seg in s
            for seg in (".app/", ".framework/", ".appex/", "/PlugIns/", "/Extensions/")
        ):
            continue
        # Skip clearly-not-binary files by extension.
        if p.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".plist",
            ".strings",
            ".car",
            ".nib",
            ".storyboardc",
            ".json",
            ".html",
            ".css",
            ".js",
            ".ttf",
            ".otf",
            ".mp3",
            ".mp4",
            ".webp",
            ".lproj",
            ".xib",
            ".pdf",
        }:
            continue
        try:
            with p.open("rb") as f:
                head = f.read(4)
        except OSError:
            continue
        if head in macho_magics:
            results.append(p)
    return results


@dataclass
class CryptidResult:
    binary: Path
    cryptids: list[int]  # one per architecture / LC_ENCRYPTION_INFO command

    @property
    def is_decrypted(self) -> bool:
        # Some binaries have no encryption load command at all (e.g. some bundled
        # frameworks) — in that case cryptids will be empty, which we treat as fine.
        return all(c == 0 for c in self.cryptids)


def check_cryptid(binary: Path) -> CryptidResult:
    """Run otool -l on a Mach-O and parse all cryptid values from LC_ENCRYPTION_INFO."""
    try:
        out = subprocess.run(
            ["otool", "-l", str(binary)],
            check=True,
            capture_output=True,
            text=True,
            env=child_env(),
        ).stdout
    except FileNotFoundError as e:
        raise IPAInspectionError(
            "otool not found",
            hint="otool ships with Xcode Command Line Tools — run `xcode-select --install`",
        ) from e
    except subprocess.CalledProcessError as e:
        raise IPAInspectionError(
            f"otool failed on {binary}: {e.stderr.strip() or e}"
        ) from e
    return CryptidResult(binary=binary, cryptids=[int(m) for m in CRYPTID_RE.findall(out)])


def verify_decrypted(extract_dir: Path) -> list[CryptidResult]:
    """Inspect every Mach-O binary in the extracted IPA. Returns the per-binary results.

    Caller decides what to do with cryptid != 0 (warn vs. abort).
    """
    binaries = list_macho_in_extract(extract_dir)
    return [check_cryptid(b) for b in binaries]


def extract_ipa(ipa: Path, target_dir: Path) -> Path:
    """Extract an IPA into target_dir, preserving file modes. Returns target_dir.

    `extractall` alone is not enough, and the difference is not cosmetic.
    Python's zipfile deliberately does not restore permissions: a 0755 entry
    comes back out as 0644. That was harmless while every consumer only *read*
    the tree — but `repack_ipa` writes it back into an installable archive, and
    `ZipFile.write` faithfully stores whatever mode it finds. The result was a
    stripped IPA whose main binary was not executable: it verified cryptid 0,
    signed cleanly, installed, and then would not launch, with nothing between
    here and the phone able to explain why.

    Symlinks get the same treatment for the same reason. `extractall` writes
    one as a regular file containing its target path, so a round trip through
    here silently turns a link into a text file. iOS bundles are flat and
    rarely contain any, which is exactly why this would have gone unnoticed.
    """
    verify_zip(ipa)
    target_dir.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(ipa) as zf:
            zf.extractall(target_dir)
            root = target_dir.resolve()
            for info in zf.infolist():
                mode = info.external_attr >> 16
                if not mode:
                    # Written by a tool that stored no mode (or on Windows).
                    # Nothing to restore, and forcing a default would be a
                    # guess about a bundle we did not create.
                    continue
                # Rebuilt from the *raw* archive name, which `extractall` has
                # already refused to honour if it escapes the directory — so a
                # `../../x` entry names a file this loop would `unlink` outside
                # the tree it was handed, and an IPA is not a trusted archive.
                # Anything that does not land under `target_dir` is something
                # `extractall` put elsewhere, and is not ours to touch.
                target = target_dir / info.filename
                try:
                    resolved = target.resolve()
                    resolved.relative_to(root)
                except (OSError, ValueError):
                    continue
                if stat.S_ISLNK(mode):
                    # `is_symlink` guards a re-extraction over an existing tree:
                    # the link is already a link, and `read_text` would follow it
                    # to its target's contents.
                    if target.is_symlink() or not target.is_file():
                        continue
                    link_to = target.read_text()
                    target.unlink()
                    target.symlink_to(link_to)
                elif target.is_file() and not target.is_symlink():
                    os.chmod(target, stat.S_IMODE(mode))
    except zipfile.BadZipFile as e:
        raise IPAInspectionError(f"{ipa} is corrupt: {e}") from e
    return target_dir


def normalize_exec_bits(extract_dir: Path) -> list[Path]:
    """Give every Mach-O the execute bit iOS installs it with. Returns what changed.

    The device is the authority, and it is unambiguous — an installed app binary
    is `-rwxr-xr-x`, owned by `_installd`. Dumps do not always match it:
    frida-ios-dump packages the main binary `0o655`, which has execute for group
    and other but *not* for the owner, and Python's `extractall` produces `0o644`,
    which has it for nobody.

    Whether a given install path cares is not something this can know — a
    LiveContainer guest is `dlopen`ed rather than executed, so the bit is
    inert there, while an ordinary sideload runs it. Which is the argument for
    normalizing rather than reasoning: it costs nothing, it removes a variable
    that differs by backend, and it makes the staged bundle match the one the
    phone already has.

    Bits are only ever added. A dump is not the place to decide a file should
    lose a permission it arrived with.
    """
    fixed: list[Path] = []
    for binary in list_macho_in_extract(extract_dir):
        mode = stat.S_IMODE(binary.stat().st_mode)
        if mode & 0o111 == 0o111:
            continue
        os.chmod(binary, mode | 0o111)
        fixed.append(binary)
    return fixed


def appex_of(binary: Path, extract_dir: Path) -> Path | None:
    """The `.appex` bundle a binary lives in, or None if it isn't in one.

    Walks up from the binary rather than pattern-matching the path, so a
    framework nested *inside* an extension
    (`X.appex/Frameworks/Y.framework/Y`) is correctly attributed to the
    extension that carries it — the case a `PlugIns/*.appex/<name>` glob
    misses, and the one where getting it wrong means stripping a bundle whose
    main binary was fine.
    """
    for parent in binary.parents:
        if parent == extract_dir:
            return None
        if parent.suffix == ".appex":
            return parent
    return None


#: Extension points where the extension *is* the product.
#:
#: Dropping an app extension normally costs a widget or a share-sheet entry —
#: annoying, not fatal, and worth it for a build that exists. For these it is
#: not a trade: the host app is a settings screen, and everything the user
#: installed it for lives in the extension. Measured on a real sweep of 26 apps,
#: stripping left a content blocker at 0.1 MB and a VPN app at 0.7 MB, both reported as
#: successful dumps. Neither backend can rescue them — a Safari content blocker
#: and a packet-tunnel extension are not processes anything can launch on
#: demand, so bagbak hangs on them too (verified: 27s and 58s to a timeout).
#:
#: Judged by `NSExtensionPointIdentifier` — Apple's own declaration of what an
#: extension plugs into — rather than by name or size, either of which would be
#: a guess about every app that ships one. The values below were read off the
#: device rather than remembered.
ESSENTIAL_EXTENSION_POINTS = {
    "com.apple.Safari.content-blocker",           # a content blocker is its extension
    "com.apple.networkextension.packet-tunnel",   # a VPN app's tunnel is its extension
    "com.apple.networkextension.app-proxy",
    "com.apple.networkextension.filter-data",
    "com.apple.networkextension.filter-control",
    "com.apple.keyboard-service",                 # a keyboard is its extension
}

#: Extension points that carry a headline feature without being the whole app.
#:
#: A password manager without `com.apple.authentication-services-credential-provider-ui`
#: still shows you your vault; it just cannot fill anything in. That is a real
#: loss and worth naming loudly, but refusing the dump over it would cost the
#: one-click path for an app that remains useful.
#: Extension points that carry a headline feature without being the whole app,
#: mapped to what the user loses in words they will recognise.
#:
#: A password manager without its credential provider still shows you your vault; it just
#: cannot fill anything in. Refusing the dump over that would cost the one-click
#: path for an app that remains useful — but "AutoFillExtension.appex
#: was removed" is a filename, where "password autofill will not work" is the
#: thing they notice next Tuesday and blame on whatever tweak was injected.
EXTENSION_CAPABILITY = {
    "com.apple.authentication-services-credential-provider-ui": "password autofill",
    "com.apple.Safari.extension": "the Safari extension",
    "com.apple.Safari.web-extension": "the Safari extension",
    "com.apple.fileprovider-nonui": "the Files app integration",
    "com.apple.fileprovider-actionsui": "the Files app integration",
    "com.apple.identitylookup.message-filter": "SMS filtering",
    "com.apple.callkit.call-directory": "call blocking",
    "com.apple.photo-editing": "editing from the Photos app",
    "com.apple.broadcast-services-upload": "screen broadcasting",
    "com.apple.intents-service": "Siri and Shortcuts",
    # An app whose product *is* its iMessage app loses everything here; one that
    # merely ships a sticker pack loses a feature. Named rather than refused,
    # because the identifier cannot tell those two apart.
    "com.apple.message-payload-provider": "the iMessage app",
    "com.apple.PassKit.issuer-provisioning": "adding cards to Apple Wallet",
    "com.apple.PassKit.issuer-provisioning.authorization": "adding cards to Apple Wallet",
}

MAJOR_EXTENSION_POINTS = set(EXTENSION_CAPABILITY)


def extension_point(appex: Path) -> str | None:
    """What an `.appex` plugs into, from its own Info.plist.

    Two keys, because Apple changed where this lives. The classic dictionary is
    `NSExtension.NSExtensionPointIdentifier`; an ExtensionKit extension (iOS 16
    and later — App Intents, controls) declares
    `EXAppExtensionAttributes.EXExtensionPointIdentifier` instead and has no
    `NSExtension` at all. Reading only the old key made every modern extension
    look like one we knew nothing about — which, with unknown treated as
    essential, refused to dump an app over its App Intents bundle.

    None when neither key is present or the plist cannot be read.
    """
    try:
        with (appex / "Info.plist").open("rb") as fh:
            plist = plistlib.load(fh)
    except (OSError, ValueError):
        return None
    point = (
        plist.get("NSExtension", {}).get("NSExtensionPointIdentifier")
        or plist.get("EXAppExtensionAttributes", {}).get("EXExtensionPointIdentifier")
    )
    return str(point) if point else None


def lost_capabilities(appexes) -> list[str]:
    """Plain-language names for what removing these extensions costs.

    Only the ones with a name worth saying: a widget that goes missing is
    visibly missing, while autofill silently stops being offered.
    """
    out = []
    for appex in appexes:
        name = EXTENSION_CAPABILITY.get(extension_point(appex) or "")
        if name and name not in out:
            out.append(name)
    return out


def extension_importance(appex: Path) -> str:
    """`"essential"`, `"major"` or `"cosmetic"` — how much removing it costs."""
    point = extension_point(appex)
    if point is None or point in ESSENTIAL_EXTENSION_POINTS:
        return "essential"
    if point in MAJOR_EXTENSION_POINTS:
        return "major"
    return "cosmetic"


def repack_ipa(extract_dir: Path, output_ipa: Path) -> Path:
    """Zip an extracted tree back into an IPA.

    `ZIP_DEFLATED` at the default level, matching what the App Store ships and
    what cyan produces; stored-uncompressed would double the size of a dump for
    no gain.

    Modes and symlinks are carried through, because this archive gets signed and
    installed. `ZipFile.write` preserves the mode it finds on disk, so this is
    only correct in company with `extract_ipa` restoring them on the way in —
    the pair is the contract, and either half alone loses the executable bit on
    every Mach-O in the bundle.
    """
    output_ipa.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_ipa, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(extract_dir.rglob("*")):
            if path.is_symlink():
                info = zipfile.ZipInfo(str(path.relative_to(extract_dir)))
                info.create_system = 3  # Unix, so the mode below is honoured
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zf.writestr(info, os.readlink(path))
            elif path.is_file():
                zf.write(path, path.relative_to(extract_dir))
    return output_ipa


#: The two things in an App Store IPA that identify the Apple ID that bought
#: it: `iTunesMetadata.plist` at the root (the account's email and ids), and an
#: `SC_Info/` directory in the app and in each extension (the FairPlay licence
#: issued to that account). Nothing a build or a device needs.
ACCOUNT_METADATA = "itunesmetadata.plist"
LICENSE_DIR = "sc_info"


def _is_account_data(name: str) -> bool:
    """Whether the zip entry `name` is one of them. Case-insensitively, as
    the volumes these unpack onto are."""
    parts = [p.casefold() for p in name.removeprefix("./").split("/")]
    if parts == [ACCOUNT_METADATA]:
        return True
    # Directory entries end in "/", so their last part is empty.
    return LICENSE_DIR in parts[:-1] or parts[-1] == LICENSE_DIR


def account_data_in(ipa: Path) -> list[str]:
    """The entries of `ipa` that identify the account it was bought with."""
    with zipfile.ZipFile(ipa) as zf:
        return [n for n in zf.namelist() if _is_account_data(n)]


def sealed_account_data(ipa: Path) -> set[str]:
    """The account-data entries of `ipa` that a code signature in it seals.

    A zsign-signed archive was signed with the app's `SC_Info/` present, and
    zsign lists every file in the bundle in `_CodeSignature/CodeResources`, so
    removing one of those breaks the seal and iOS refuses the install.
    Re-signing is a rebuild, so they stay. Read per bundle — the app and each
    extension seal their own `SC_Info/` — and only for a bundle that has a
    CodeResources to read: a decrypted App Store IPA's licences were added
    after Apple signed it and are sealed by nothing. `iTunesMetadata.plist`
    sits outside every bundle and is never sealed.
    """
    sealed: set[str] = set()
    seals: dict[str, set[str]] = {}
    with zipfile.ZipFile(ipa) as zf:
        for name in zf.namelist():
            if not _is_account_data(name) or name.endswith("/"):
                continue
            parts = name.removeprefix("./").split("/")
            at = next((i for i, p in enumerate(parts) if p.casefold() == LICENSE_DIR), None)
            if not at:
                continue
            bundle, inside = "/".join(parts[:at]), "/".join(parts[at:])
            if bundle not in seals:
                seals[bundle] = _sealed_paths(zf, f"{bundle}/_CodeSignature/CodeResources")
            if inside in seals[bundle]:
                sealed.add(name)
    return sealed


def _sealed_paths(zf: zipfile.ZipFile, name: str) -> set[str]:
    try:
        resources = plistlib.loads(zf.read(name))
    except Exception:
        return set()
    found: set[str] = set()
    for key in ("files", "files2"):
        section = resources.get(key) if isinstance(resources, dict) else None
        if isinstance(section, dict):
            found.update(section)
    return found


def strip_account_data(src: Path, dest: Path, *, keep: Collection[str] = ()) -> list[str]:
    """Write `src` to `dest` without its account data; return what was left out.

    Entry by entry, streamed, with each entry's compression, timestamp and
    mode (and so its symlink-ness) carried over. `dest` is opened for writing
    as it is, so a caller that created it 0600 gets a 0600 IPA. Entries named
    in `keep` (`sealed_account_data`) are carried over like any other.
    """
    keep = set(keep)
    removed: list[str] = []
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dest, "w") as zout:
        for info in zin.infolist():
            if _is_account_data(info.filename) and info.filename not in keep:
                removed.append(info.filename)
                continue
            # A fresh ZipInfo: writing through the source's own would rewrite
            # fields `zin` is still reading from.
            out = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            out.compress_type = info.compress_type
            out.external_attr = info.external_attr
            out.create_system = info.create_system
            out.file_size = info.file_size  # decides zip64 up front
            if info.is_dir():
                zout.writestr(out, b"")
                continue
            with zin.open(info) as source, zout.open(out, "w") as target:
                shutil.copyfileobj(source, target, 1 << 20)
    return removed


#: Sidecar recording that a staged IPA is not a complete dump.
#:
#: Named after the IPA it describes and living beside it, so deleting the cache
#: deletes the record with it — the same discipline `IPAInfoMemo` follows.
STRIPPED_SUFFIX = ".stripped.json"


def _sidecar_path(ipa: Path) -> Path:
    """Where `ipa`'s stripped-note sidecar lives — named after the IPA and
    beside it, so deleting the cache deletes the record with it."""
    return ipa.parent / (ipa.name + STRIPPED_SUFFIX)


def mark_stripped(
    ipa: Path,
    removed: list[str],
    *,
    lost: list[str] | None = None,
    importance: dict[str, str] | None = None,
) -> None:
    """Record that `ipa` had bundles removed to make it verify.

    A stripped IPA is staged under the *same* canonical
    `{bundle_id}_{version}.ipa` name a complete dump uses, and nothing inside
    it differs in a way any consumer looks at: `find_in_cache` matches on the
    name and validates the Info.plist, which stripping leaves untouched. So
    without this, a build days later reuses an app with no widgets and no share
    sheet while reporting an ordinary cache hit, and the person who notices the
    missing widget has no way to tell the two files apart.

    Renaming the file instead would have hidden it from `find_in_cache`
    entirely, which defeats the point — the whole reason to strip is to get a
    buildable IPA.

    `lost` and `importance` are written *because this is the only moment they
    can be*. `lost_capabilities` and `extension_importance` both read each
    `.appex`'s own Info.plist, and the caller deletes those bundles seconds
    later — so every later reader can name files and nothing else, which
    `_do_decrypt`'s own comment calls the wrong unit for a capability that
    stops being offered silently.

    `lost=None` and `lost=[]` are different claims and are written
    differently: the key is absent when nothing computed it, and present-empty
    when something did and found no capability worth naming (a widget that
    goes missing is visibly missing). See `stripped_note`.

    `importance` gets no matching treatment — it is written only when
    non-empty (`if importance:`, not `if importance is not None:`), so an
    absent key and an empty map end up meaning the same thing on disk. The two
    fields were never describing the same set: `importance` is keyed by
    bundle path and records every tier, `"essential"` included, while `lost`
    only ever names the `"major"` ones — a packet-tunnel or keyboard `.appex`
    shows up in `importance` and nowhere in `lost` at all. The collapse is
    safe only because nothing may read `importance == {}` as a positive claim
    about what stayed; see `StrippedNote` for what that obliges a reader to
    do with it.
    """
    payload: dict[str, Any] = {"removed": removed}
    if lost is not None:
        payload["lost"] = lost
    if importance:
        payload["importance"] = importance
    _sidecar_path(ipa).write_text(json.dumps(payload, indent=2))


def clear_stripped(ipa: Path) -> None:
    """Forget that `ipa` was ever a partial dump.

    The counterpart to `mark_stripped`, and needed because staging *replaces*
    the IPA without touching the note beside it: a complete re-dump of the same
    version inherited the previous run's sidecar, and every later build warned
    about extensions that were present. Silence is the claim being made here, so
    it has to be written as deliberately as the warning is.
    """
    _sidecar_path(ipa).unlink(missing_ok=True)


@dataclass(frozen=True)
class StrippedNote:
    """Everything the sidecar beside one IPA says.

    `lost is None` means **unrecorded** — a sidecar written before that field
    existed — and is not the same as `lost == []`, which means something looked
    and found no capability with a name worth saying. Only the second may ever
    carry a reassurance to the user; flattening them would let a partial app
    claim it lost nothing important.

    `importance` has no equivalent unrecorded state: an absent key and an
    empty map both read back as `{}`. That is safe under the same reassurance
    rule that governs `lost` above — an empty `importance` may only mean
    "nobody recorded one", never "nothing essential was lost" — because
    nothing here can tell an unrecorded sidecar apart from one that genuinely
    stripped only `"major"` extensions. A surface that later joins
    `importance` against `removed` hunting an `"essential"` match must fall
    through to the same neutral line for both, not treat the second as
    cleared.
    """

    removed: list[str]
    lost: list[str] | None
    importance: dict[str, str]


def stripped_note(ipa: Path) -> StrippedNote | None:
    """The sidecar beside `ipa`, or None when there is nothing to read.

    None covers both "no sidecar" (which `clear_stripped` writes deliberately,
    so absence is a claim of completeness) and "a sidecar that will not parse".
    Collapsing those is the same reading `stripped_extensions` derives from
    this function, and two readers disagreeing about one file would be worse
    than either answer.
    """
    sidecar = _sidecar_path(ipa)
    try:
        data = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        # `null`, `[]`, `3`, `"x"` all parse without raising, so the except
        # above never sees them — without this, `data.get` below is an
        # AttributeError on anything but a dict.
        return None
    removed = data.get("removed")
    if not isinstance(removed, list):
        return None
    lost = data.get("lost")
    importance = data.get("importance")
    return StrippedNote(
        removed=[str(x) for x in removed],
        lost=[str(x) for x in lost] if isinstance(lost, list) else None,
        importance=(
            {str(k): str(v) for k, v in importance.items()}
            if isinstance(importance, dict)
            else {}
        ),
    )


def stripped_extensions(ipa: Path) -> list[str]:
    """What was removed from `ipa`, or [] if it is a complete dump.

    A two-line derivation of `stripped_note`, which parses the identical
    sidecar and degrades to `None` on the identical unreadable/malformed
    shapes `stripped_note` degrades to `None` on — `OSError`/`ValueError`, a
    non-dict payload, or a `removed` that isn't a list. `[]` on every one of
    those, same as `stripped_note(ipa).removed` when there *is* a note: this
    annotates a build, and a corrupt sidecar must not be able to stop one.
    """
    note = stripped_note(ipa)
    return note.removed if note else []
