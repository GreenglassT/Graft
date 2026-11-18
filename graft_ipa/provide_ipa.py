"""Bring-your-own IPA: validate a user-supplied .ipa and stage it in cache_dir
under the canonical `<bundle_id>_<version>.ipa` filename the build pipeline
expects.

Validation reads the IPA's own Info.plist (via `ipa_inspect.read_info_plist`)
and rejects any IPA whose CFBundleIdentifier doesn't match the app's
configured `bundle_id` — so dropping the wrong IPA on the wrong app fails
loudly instead of silently producing a bad build.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

from .errors import IPAInspectionError, IPAValidationError
from .ipa_inspect import account_data_in, clear_stripped, read_info_plist, strip_account_data


@dataclass
class ValidatedIPA:
    bundle_id: str
    version: str
    source_path: Path


def _unsafe_in_filename(value: str) -> bool:
    return any(bad in value for bad in ("/", "\\", "\0", ".."))


def validate_ipa(path: Path, expected_bundle_id: str) -> ValidatedIPA:
    """Validate a candidate IPA. Returns a `ValidatedIPA` or raises IPAValidationError.

    Checks (in order): file exists, .ipa extension, readable zip with an
    Info.plist that carries CFBundleIdentifier + CFBundleShortVersionString,
    and that the bundle id equals `expected_bundle_id`.
    """
    if not path.exists() or not path.is_file():
        raise IPAValidationError(f"file not found: {path}")
    if path.suffix.lower() != ".ipa":
        raise IPAValidationError(
            f"not an IPA file: {path.name}",
            hint="drop a .ipa file (the suffix must be .ipa)",
        )
    try:
        info = read_info_plist(path)
    except IPAInspectionError as e:
        raise IPAValidationError(f"couldn't read IPA: {e}") from e
    # Both become part of the canonical cache filename, and both come from a
    # plist this project did not write. A `CFBundleShortVersionString` of
    # `../../x` would otherwise have staged outside cache_dir.
    if _unsafe_in_filename(info.bundle_id):
        raise IPAValidationError(f"IPA bundle id {info.bundle_id!r} can't be used in a filename")
    if _unsafe_in_filename(info.version):
        raise IPAValidationError(
            f"IPA version {info.version!r} can't be used in a filename",
            hint="this IPA's Info.plist is malformed or was made to escape the cache",
        )
    if info.bundle_id != expected_bundle_id:
        raise IPAValidationError(
            f"IPA bundle id mismatch: file is {info.bundle_id}, "
            f"expected {expected_bundle_id}",
            hint=(
                "drop the IPA that matches this app, or update the app's "
                "bundle_id in config.yaml"
            ),
        )
    return ValidatedIPA(
        bundle_id=info.bundle_id,
        version=info.version,
        source_path=path,
    )


def stage(source: Path, final: Path, *, keep: Collection[str] = ()) -> list[str]:
    """Put `source` at `final`, owner-only and identifying nobody.

    The one way an IPA enters the cache, whatever it came from — a drop, an
    App Store get, a `--decrypt` dump — and the reason all three can be said
    to stage nothing that names the buyer. An App Store IPA carries
    `iTunesMetadata.plist` (the Apple ID's email and account ids) and an
    `SC_Info/` licence in the app and in every extension
    (`ipa_inspect.account_data_in`); a copy without them is written, or a plain
    copy when there were none. The result is then *checked*, not assumed: no
    tool's word — UnfairPlay's `--strip-account-data` included — decides what
    reaches the cache.

    Written to a uniquely named `.<name>.<random>.partial.ipa` beside `final`
    (`mkstemp`, so 0600) and `os.replace`d into place, so a crash leaves no
    half-written file at the canonical name. Unique rather than fixed since the
    App Store work: a drop and a get of the same version can stage at once,
    and a shared temp name let the second truncate the first's file under it.
    The temp file goes on *any* exception, an interrupt included.

    `keep` names account-data entries to leave in (`ipa_inspect.
    sealed_account_data`: what a signed archive's signature seals); the rest
    must be gone.

    Returns the entries removed.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=final.parent, prefix=f".{final.stem}.", suffix=".partial.ipa"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if account_data_in(source):
            removed = strip_account_data(source, tmp, keep=keep)
        else:
            # Into the 0600 file mkstemp made: `copyfile` copies data, not mode.
            shutil.copyfile(source, tmp)
            removed = []
        left = sorted(set(account_data_in(tmp)) - set(keep))
        if left:
            raise IPAValidationError(
                f"{source.name} still holds account data after staging: {', '.join(left)}",
                hint="nothing was staged; please report this",
            )
        os.replace(tmp, final)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise IPAValidationError(f"failed to copy IPA into {final.parent}: {e}") from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return removed


def needs_scrub(ipa: Path, *, keep: Collection[str] = ()) -> bool:
    """Whether `ipa` holds account data beyond `keep`. One zip listing.

    An IPA that cannot be read as a zip is left for the build's own checks to
    report (stage 2.5, then cyan), which say what is wrong with it; this one
    would only say that it could not look.
    """
    try:
        return bool(set(account_data_in(ipa)) - set(keep))
    except (zipfile.BadZipFile, OSError):
        return False


def scrub_staged(ipa: Path, *, keep: Collection[str] = ()) -> list[str]:
    """Take the account data out of an IPA already in the cache, in place.

    For a file staged before `stage` did it, or an archive built from one. A
    no-op — one zip listing — for every IPA staged since. Returns the entries
    removed.
    """
    if not needs_scrub(ipa, keep=keep):
        return []
    return stage(ipa, ipa, keep=keep)


def scrubbed_copy(ipa: Path, dest: Path, *, keep: Collection[str] = ()) -> list[str]:
    """`ipa` without its account data, written to `dest` (0600); the original
    is not touched. For when the original's directory will not take a write —
    a full or read-only cache — and the build goes on from the copy.
    Checked like `stage`. Returns the entries removed."""
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        removed = strip_account_data(ipa, dest, keep=keep)
        left = sorted(set(account_data_in(dest)) - set(keep))
        if left:
            raise IPAValidationError(
                f"{ipa.name} still holds account data after scrubbing: {', '.join(left)}",
                hint="nothing was shipped with it; please report this",
            )
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return removed


def provide(
    *,
    cache_dir: Path,
    expected_bundle_id: str,
    source_path: Path,
) -> tuple[Path, ValidatedIPA]:
    """Validate `source_path` against `expected_bundle_id`, then copy it into
    `cache_dir` under the canonical `<bundle_id>_<version>.ipa` name the build
    pipeline expects. Returns (destination path, validated IPA info).

    Through `stage`: owner-only, atomic, and without the buyer's metadata and
    licences. Replaces any existing file with the same version.
    """
    info = validate_ipa(source_path, expected_bundle_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    final = cache_dir / f"{info.bundle_id}_{info.version}.ipa"
    stage(source_path, final)
    # `stage` swaps the IPA at `final` but, as it does for
    # `_do_decrypt`, leaves any `.stripped.json` sitting beside the old one
    # untouched. A version staged earlier with `--strip-encrypted-extensions`
    # left exactly that note; providing a *complete* IPA of the same version
    # over it must clear it, or every later read of `final` — the add sheet's
    # `Prefill`, a build's own cache hit — keeps warning about extensions a
    # tool never had the chance to fail to decrypt this time. `_do_decrypt`
    # earned this the same way: see its own comment beside `clear_stripped`.
    clear_stripped(final)
    return final, info
