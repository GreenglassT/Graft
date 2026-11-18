from __future__ import annotations

import plistlib
import zipfile
from pathlib import Path

import pytest

from graft_ipa import ipa_inspect, provide_ipa
from graft_ipa.provide_ipa import IPAValidationError


def _make_ipa(
    path: Path,
    *,
    bundle_id: str = "com.example.x",
    version: str = "1.0",
    app_name: str = "X",
    skip_bundle_id: bool = False,
    skip_version: bool = False,
    payload_only: bool = False,
) -> Path:
    """Build a minimal valid IPA fixture in `path`."""
    plist: dict = {"CFBundleName": app_name}
    if not skip_bundle_id:
        plist["CFBundleIdentifier"] = bundle_id
    if not skip_version:
        plist["CFBundleShortVersionString"] = version
        plist["CFBundleVersion"] = version
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"Payload/{app_name}.app/Info.plist",
            plistlib.dumps(plist),
        )
        if not payload_only:
            zf.writestr(f"Payload/{app_name}.app/{app_name}", b"\x00\x00")
    return path


def test_validate_ipa_reads_bundle_id_and_version(tmp_path: Path) -> None:
    ipa = _make_ipa(tmp_path / "x.ipa", bundle_id="com.bravo.app", version="2025.20.0")
    info = provide_ipa.validate_ipa(ipa, expected_bundle_id="com.bravo.app")
    assert info.bundle_id == "com.bravo.app"
    assert info.version == "2025.20.0"
    assert info.source_path == ipa


def test_validate_ipa_rejects_missing_file(tmp_path: Path) -> None:
    missing = tmp_path / "nope.ipa"
    with pytest.raises(IPAValidationError):
        provide_ipa.validate_ipa(missing, expected_bundle_id="com.x")


def test_validate_ipa_rejects_wrong_extension(tmp_path: Path) -> None:
    not_ipa = tmp_path / "x.zip"
    not_ipa.write_bytes(b"PK\x03\x04")
    with pytest.raises(IPAValidationError):
        provide_ipa.validate_ipa(not_ipa, expected_bundle_id="com.x")


def test_validate_ipa_rejects_bundle_id_mismatch(tmp_path: Path) -> None:
    ipa = _make_ipa(tmp_path / "x.ipa", bundle_id="com.bravo.app")
    with pytest.raises(IPAValidationError) as exc:
        provide_ipa.validate_ipa(ipa, expected_bundle_id="com.delta.app")
    msg = str(exc.value)
    assert "com.bravo.app" in msg
    assert "com.delta.app" in msg


def test_validate_ipa_rejects_missing_bundle_id_in_plist(tmp_path: Path) -> None:
    ipa = _make_ipa(tmp_path / "x.ipa", skip_bundle_id=True)
    with pytest.raises(IPAValidationError):
        provide_ipa.validate_ipa(ipa, expected_bundle_id="com.example.x")


def test_provide_copies_to_canonical_filename(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _make_ipa(tmp_path / "src.ipa", bundle_id="com.bravo.app", version="2025.20.0")
    dest, _ = provide_ipa.provide(
        cache_dir=cache,
        expected_bundle_id="com.bravo.app",
        source_path=src,
    )
    assert dest == cache / "com.bravo.app_2025.20.0.ipa"
    assert dest.exists()
    # Validates against the cached file's own info too.
    assert dest.read_bytes() == src.read_bytes()
    # No leftover partial files in the cache.
    assert not any(p.name.startswith(".") for p in cache.iterdir())


def test_provide_clears_a_stale_stripped_sidecar(tmp_path: Path) -> None:
    """`_do_decrypt` clears the sidecar beside a fresh dump for exactly this
    reason (`cli.py`'s own comment: "`shutil.move` replaces the IPA but not
    the note beside it") — `provide()`'s `os.replace` has the identical gap.
    Stage a dump with `--strip-encrypted-extensions` (a `.stripped.json`
    sidecar beside the canonical `{bundle_id}_{version}.ipa`), then
    `--provide-ipa` a *complete* IPA of the same version over it: without
    this fix the sidecar survives the replace, and the next add or build
    reads a complete file while `Prefill.stagedLine` still warns, in
    `Palette.warn`, that it is missing app extensions no tool could decrypt —
    a false warning about a file that is not, in fact, partial."""
    cache = tmp_path / "cache"
    cache.mkdir()
    stale = cache / "com.bravo.app_2025.20.0.ipa"
    stale.write_bytes(b"a stub standing in for a previously-staged dump")
    ipa_inspect.mark_stripped(stale, ["Payload/Bravo.app/PlugIns/Widget.appex"])
    assert ipa_inspect.stripped_extensions(stale) != [], (
        "premise: the sidecar must actually be there before provide() runs, "
        "or clearing it proves nothing"
    )

    src = _make_ipa(
        tmp_path / "src.ipa", bundle_id="com.bravo.app", version="2025.20.0"
    )
    dest, _ = provide_ipa.provide(
        cache_dir=cache, expected_bundle_id="com.bravo.app", source_path=src
    )

    assert dest == stale, "premise: the replace really did land on the same path"
    assert ipa_inspect.stripped_extensions(dest) == []


def test_provide_overwrites_existing_same_version(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    first = _make_ipa(tmp_path / "a.ipa", bundle_id="com.bravo.app", version="1.0")
    second = _make_ipa(tmp_path / "b.ipa", bundle_id="com.bravo.app", version="1.0")
    # Make `second` distinguishable on bytes.
    with open(second, "ab") as f:
        f.write(b"distinct")
    provide_ipa.provide(
        cache_dir=cache, expected_bundle_id="com.bravo.app", source_path=first
    )
    dest, _ = provide_ipa.provide(
        cache_dir=cache, expected_bundle_id="com.bravo.app", source_path=second
    )
    assert dest.read_bytes().endswith(b"distinct")


def test_provide_creates_cache_dir_if_missing(tmp_path: Path) -> None:
    cache = tmp_path / "deep" / "nested" / "cache"
    src = _make_ipa(tmp_path / "x.ipa", bundle_id="com.x.y", version="1.0")
    dest, _ = provide_ipa.provide(
        cache_dir=cache, expected_bundle_id="com.x.y", source_path=src
    )
    assert dest.parent == cache
    assert cache.is_dir()


def test_provide_raises_on_mismatch_without_writing(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    src = _make_ipa(tmp_path / "x.ipa", bundle_id="com.bravo.app", version="1.0")
    with pytest.raises(IPAValidationError):
        provide_ipa.provide(
            cache_dir=cache,
            expected_bundle_id="com.different.bundle",
            source_path=src,
        )
    assert not any(cache.iterdir())  # nothing copied on failure
