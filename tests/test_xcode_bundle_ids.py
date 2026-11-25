"""Graft.app's bundle id, per Xcode configuration.

Release, which scripts/graft-release builds from the committed project, is
the id every shipped Graft has had: the one the updater's codesign
requirement names (`app_update.BUNDLE_ID`) and whose preferences
`--remove-data` lists. Debug, which the Swift test suite runs as its host, has
its own: under the release id, AppKit and SwiftUI saved the test host's window
frames and split-view sizes into the owner's real Graft preferences, keys no
Graft code writes. The Swift side checks the running host
(`NoRealDefaultsTests`); this checks both configurations where they're set.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from graft_ipa import app_update

ROOT = Path(__file__).resolve().parent.parent
RELEASE_ID = app_update.BUNDLE_ID


def test_the_release_id_is_the_one_every_graft_has_shipped_with() -> None:
    assert RELEASE_ID == "com.greenglasst.graft"


def test_project_yml_gives_release_the_real_id_and_debug_its_own() -> None:
    project = yaml.safe_load((ROOT / "Graft" / "project.yml").read_text(encoding="utf-8"))
    settings = project["targets"]["Graft"]["settings"]
    assert settings["base"]["PRODUCT_BUNDLE_IDENTIFIER"] == RELEASE_ID
    configs = settings.get("configs") or {}
    assert "PRODUCT_BUNDLE_IDENTIFIER" not in (configs.get("Release") or {})
    debug = (configs.get("Debug") or {}).get("PRODUCT_BUNDLE_IDENTIFIER")
    assert debug and debug != RELEASE_ID


def _app_bundle_ids(pbxproj: str) -> dict[str, str]:
    """The Graft app target's PRODUCT_BUNDLE_IDENTIFIER per configuration
    name: the build configurations whose INFOPLIST_FILE is the app's."""
    found: dict[str, str] = {}
    for body, name in re.findall(
        r"isa = XCBuildConfiguration;\s*buildSettings = \{(.*?)\n\t\t\t\};\s*name = (\w+);", pbxproj, re.S
    ):
        if re.search(r"^\s*INFOPLIST_FILE = Graft/Info\.plist;$", body, re.M):
            ids = re.findall(r"^\s*PRODUCT_BUNDLE_IDENTIFIER = \"?([^\";]+)\"?;$", body, re.M)
            assert len(ids) == 1, (name, ids)
            assert name not in found, f"two {name} configurations for the app"
            found[name] = ids[0]
    return found


def test_the_committed_project_builds_release_under_the_real_id_and_debug_under_its_own() -> None:
    pbxproj = (ROOT / "Graft" / "Graft.xcodeproj" / "project.pbxproj").read_text(encoding="utf-8")
    ids = _app_bundle_ids(pbxproj)
    assert set(ids) == {"Debug", "Release"}, ids
    assert ids["Release"] == RELEASE_ID
    assert ids["Debug"] != RELEASE_ID
