"""`devices_seen` keeps what each device last called itself, its model and its
iOS version (spec §5b), beside Plan B's Developer Mode reading: how Graft
names a device that isn't connected."""

from __future__ import annotations

from pathlib import Path

from graft_ipa import devices_seen

PHONE = "00008030-0000000000000001"


def test_names_are_kept_beside_developer_mode(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, PHONE, developer_mode=True)
    devices_seen.remember(tmp_path, PHONE, name="iPhone 15 Pro", model="iPhone16,1",
                          ios_version="26.1")

    assert devices_seen.seen(tmp_path, PHONE) == {
        "developer_mode": True, "name": "iPhone 15 Pro", "model": "iPhone16,1",
        "ios_version": "26.1",
    }


def test_a_name_that_is_not_text_is_not_kept(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, PHONE, name=7)  # type: ignore[arg-type]

    assert devices_seen.seen(tmp_path, PHONE) == {}


def test_name_of_is_the_last_known_name_or_none(tmp_path: Path) -> None:
    assert devices_seen.name_of(tmp_path, PHONE) is None

    devices_seen.remember(tmp_path, PHONE, name="iPhone 15 Pro")

    assert devices_seen.name_of(tmp_path, PHONE) == "iPhone 15 Pro"


def test_a_blank_name_is_no_name(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, PHONE, name="   ")

    assert devices_seen.name_of(tmp_path, PHONE) is None
    assert devices_seen.label(tmp_path, PHONE) == PHONE


def test_label_is_the_name_else_the_udid(tmp_path: Path) -> None:
    assert devices_seen.label(tmp_path, PHONE) == PHONE

    devices_seen.remember(tmp_path, PHONE, name="iPhone 15 Pro")

    assert devices_seen.label(tmp_path, PHONE) == "iPhone 15 Pro"


def test_forget_others_keeps_only_the_named_devices(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, PHONE, name="iPhone 15 Pro")
    devices_seen.remember(tmp_path, "00008030-0000000000000002", name="iPad Air")

    devices_seen.forget_others(tmp_path, {PHONE})

    assert set(devices_seen.load(tmp_path)) == {PHONE}
