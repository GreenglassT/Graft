"""`preferred_version: latest` must mean newest, not first-in-glob-order."""

from __future__ import annotations

import plistlib
import zipfile
from pathlib import Path

from graft_ipa.config import AppConfig, CyanOptions
from graft_ipa.ipa_cache import find_in_cache

BUNDLE = "com.example.app"


def _fake_ipa(cache: Path, version: str, bundle: str = BUNDLE) -> Path:
    """An IPA just real enough for read_info_plist: a zip with an Info.plist."""
    path = cache / f"{bundle}_{version}.ipa"
    info = {"CFBundleIdentifier": bundle, "CFBundleShortVersionString": version}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Payload/Example.app/Info.plist", plistlib.dumps(info))
    return path


def _cfg(tmp_path: Path):
    class _Cfg:
        cache_dir = tmp_path
    return _Cfg()


def _app(pinned: str) -> AppConfig:
    return AppConfig(
        name="example", bundle_id=BUNDLE,
        preferred_version=pinned, tweaks=[], cyan=CyanOptions(),
    )


def test_latest_picks_the_newest_not_the_first_filename(tmp_path: Path) -> None:
    # Lexicographically "…_21.16.2" sorts before "…_22.1.0", so the old
    # first-match-wins behaviour returned the OLDER build.
    _fake_ipa(tmp_path, "21.16.2")
    _fake_ipa(tmp_path, "22.1.0")

    found = find_in_cache(_cfg(tmp_path), _app("latest"))

    assert found is not None
    assert found.version == "22.1.0"


def test_latest_compares_numerically_not_lexically(tmp_path: Path) -> None:
    # "21.9.1" sorts AFTER "21.16.2" as text but is the older release.
    _fake_ipa(tmp_path, "21.16.2")
    _fake_ipa(tmp_path, "21.9.1")

    found = find_in_cache(_cfg(tmp_path), _app("latest"))

    assert found is not None
    assert found.version == "21.16.2"


def test_an_explicit_pin_still_wins_over_a_newer_cached_build(tmp_path: Path) -> None:
    _fake_ipa(tmp_path, "21.16.2")
    _fake_ipa(tmp_path, "22.1.0")

    found = find_in_cache(_cfg(tmp_path), _app("21.16.2"))

    assert found is not None
    assert found.version == "21.16.2"


def test_a_pin_with_no_matching_cache_entry_is_a_miss(tmp_path: Path) -> None:
    _fake_ipa(tmp_path, "22.1.0")

    assert find_in_cache(_cfg(tmp_path), _app("21.16.2")) is None


def test_other_apps_in_the_cache_are_ignored(tmp_path: Path) -> None:
    _fake_ipa(tmp_path, "99.0.0", bundle="com.bravo.app")
    _fake_ipa(tmp_path, "21.16.2")

    found = find_in_cache(_cfg(tmp_path), _app("latest"))

    assert found is not None
    assert found.bundle_id == BUNDLE
    assert found.version == "21.16.2"


def test_single_cached_build_is_returned(tmp_path: Path) -> None:
    _fake_ipa(tmp_path, "21.16.2")

    found = find_in_cache(_cfg(tmp_path), _app("latest"))

    assert found is not None
    assert found.version == "21.16.2"
