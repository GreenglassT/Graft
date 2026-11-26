"""What `graft --update` remembers between runs: a version the Apple ID was
refused (`update_refusals`, spec 3.1) and, for the quick plan, each phone's
LiveContainer (`devices_seen`). Both live in the state folder, owner-only,
and fail soft: a file that can't be read is no memory."""

from __future__ import annotations

import stat
from pathlib import Path

from graft_ipa import devices_seen, update_refusals
from graft_ipa.config import AppConfig, CyanOptions

PHONE = "00008130-FAKE00000000PHONE1"


def _app(name: str = "example", bundle_id: str = "com.example.app") -> AppConfig:
    return AppConfig(name=name, bundle_id=bundle_id, preferred_version="latest",
                     tweaks=[], cyan=CyanOptions())


def test_a_refusal_is_remembered_by_app_and_version(tmp_path: Path) -> None:
    update_refusals.remember(tmp_path, _app(), "2.0")

    assert update_refusals.refused_version(tmp_path, _app()) == "2.0"
    assert update_refusals.refused_version(tmp_path, _app("bravo", "com.bravo.app")) is None


def test_a_refusal_for_another_bundle_id_under_the_same_name_does_not_apply(tmp_path: Path) -> None:
    """An app removed and added again under its old name is another app."""
    update_refusals.remember(tmp_path, _app(), "2.0")

    assert update_refusals.refused_version(tmp_path, _app(bundle_id="com.other.app")) is None


def test_forgetting_clears_only_that_app(tmp_path: Path) -> None:
    update_refusals.remember(tmp_path, _app(), "2.0")
    update_refusals.remember(tmp_path, _app("bravo", "com.bravo.app"), "5.1")

    update_refusals.forget(tmp_path, "example")

    assert update_refusals.refused_version(tmp_path, _app()) is None
    assert update_refusals.refused_version(tmp_path, _app("bravo", "com.bravo.app")) == "5.1"


def test_the_file_is_owner_only(tmp_path: Path) -> None:
    update_refusals.remember(tmp_path, _app(), "2.0")

    mode = stat.S_IMODE((tmp_path / update_refusals.FILENAME).stat().st_mode)
    assert mode == 0o600


def test_an_unreadable_or_foreign_file_is_no_memory(tmp_path: Path) -> None:
    path = tmp_path / update_refusals.FILENAME
    path.write_text("not json")
    assert update_refusals.refused_version(tmp_path, _app()) is None

    path.write_text('{"format": 99, "apps": {"example": {"bundle_id": "com.example.app", "version": "2.0"}}}')
    assert update_refusals.refused_version(tmp_path, _app()) is None


def test_a_state_folder_that_cannot_be_written_is_not_an_error(tmp_path: Path) -> None:
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("x")

    update_refusals.remember(blocked, _app(), "2.0")  # no exception

    assert update_refusals.refused_version(blocked, _app()) is None


def test_a_phones_livecontainer_is_remembered(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, PHONE, livecontainer_bundle_id="com.kdt.livecontainer")

    assert devices_seen.seen(tmp_path, PHONE)["livecontainer_bundle_id"] == "com.kdt.livecontainer"


def test_the_plans_refused_reason_points_at_get_in_the_app_store() -> None:
    """Spec 3.1's sentence: the person gets it in the App Store (or Graft's
    Get, which a claim of a free app leads to), and Update offers it again.
    No colon of its own: the confirmation shows it as "App Store 1.2.0:
    <reason>" (final review 1)."""
    from graft_ipa import update_plan

    assert update_plan.REFUSED.format(version="1.2.0") == (
        "Your Apple ID doesn't have 1.2.0; get it in the App Store"
    )
