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
from dataclasses import dataclass
from pathlib import Path

from .errors import IPAInspectionError, IPAValidationError
from .ipa_inspect import clear_stripped, read_info_plist


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


def provide(
    *,
    cache_dir: Path,
    expected_bundle_id: str,
    source_path: Path,
) -> tuple[Path, ValidatedIPA]:
    """Validate `source_path` against `expected_bundle_id`, then copy it into
    `cache_dir` under the canonical `<bundle_id>_<version>.ipa` name the build
    pipeline expects. Returns (destination path, validated IPA info).

    The copy is atomic-ish: writes to a uniquely named
    `.<bundle_id>_<version>.<random>.partial.ipa` in the same directory, then
    `os.replace`s into place — so a crash mid-copy doesn't leave a
    half-written file at the canonical name. Replaces any existing file with
    the same version.

    Unique rather than fixed since the App Store work: a drop and a get of the
    same version can stage at once, and a shared temp name let the second copy
    truncate the first's file under it. The temp file goes on *any* exception,
    not only `OSError` — an interrupt (or the get's SIGTERM-as-exception)
    otherwise left a partial IPA in the cache.
    """
    info = validate_ipa(source_path, expected_bundle_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    final = cache_dir / f"{info.bundle_id}_{info.version}.ipa"
    fd, tmp_name = tempfile.mkstemp(
        dir=cache_dir, prefix=f".{info.bundle_id}_{info.version}.", suffix=".partial.ipa"
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(source_path, tmp)
        os.replace(tmp, final)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise IPAValidationError(
            f"failed to copy IPA into {cache_dir}: {e}"
        ) from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    # `os.replace` swaps the IPA at `final` but, same as `shutil.move` in
    # `_do_decrypt`, leaves any `.stripped.json` sitting beside the old one
    # untouched. A version staged earlier with `--strip-encrypted-extensions`
    # left exactly that note; providing a *complete* IPA of the same version
    # over it must clear it, or every later read of `final` — the add sheet's
    # `Prefill`, a build's own cache hit — keeps warning about extensions a
    # tool never had the chance to fail to decrypt this time. `_do_decrypt`
    # earned this the same way: see its own comment beside `clear_stripped`.
    clear_stripped(final)
    return final, info
