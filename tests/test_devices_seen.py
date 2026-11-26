"""`devices_seen`: what Graft last saw of each device, in the state folder.

A cache, so every way it can be wrong reads as "not seen": a file Graft
didn't write, a fact of the wrong type, a link planted at its path, a state
folder that can't be written.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from graft_ipa import devices_seen

UDID = "00008030-0000000000000001"
OTHER = "00008030-0000000000000002"


def test_a_fact_round_trips(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=False)

    assert devices_seen.seen(tmp_path, UDID) == {"developer_mode": False}
    assert devices_seen.load(tmp_path) == {UDID: {"developer_mode": False}}


def test_nothing_remembered_is_an_empty_answer(tmp_path: Path) -> None:
    assert devices_seen.load(tmp_path) == {}
    assert devices_seen.seen(tmp_path, UDID) == {}
    assert not devices_seen.path(tmp_path).exists()


def test_no_answer_leaves_the_last_one(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=True)
    devices_seen.remember(tmp_path, UDID, developer_mode=None)

    assert devices_seen.seen(tmp_path, UDID) == {"developer_mode": True}


def test_devices_are_kept_apart(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=True)
    devices_seen.remember(tmp_path, OTHER, developer_mode=False)

    assert devices_seen.load(tmp_path) == {
        UDID: {"developer_mode": True},
        OTHER: {"developer_mode": False},
    }


def test_an_unchanged_fact_writes_nothing(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=True)
    file = devices_seen.path(tmp_path)
    old = file.stat().st_mtime_ns - 10**9
    os.utime(file, ns=(old, old))

    devices_seen.remember(tmp_path, UDID, developer_mode=True)

    assert file.stat().st_mtime_ns == old


def test_the_file_is_the_users_alone(tmp_path: Path) -> None:
    devices_seen.remember(tmp_path, UDID, developer_mode=True)

    assert devices_seen.path(tmp_path).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("body", [
    "not json",
    "[]",
    '{"format": 2, "devices": {}}',
    '{"format": 1, "devices": []}',
    '{"format": 1}',
])
def test_a_file_graft_did_not_write_reads_as_nothing(tmp_path: Path, body: str) -> None:
    devices_seen.path(tmp_path).write_text(body)

    assert devices_seen.load(tmp_path) == {}


def test_a_fact_of_the_wrong_type_or_name_is_dropped(tmp_path: Path) -> None:
    devices_seen.path(tmp_path).write_text(json.dumps({"format": 1, "devices": {
        UDID: {"developer_mode": "yes"},
        OTHER: {"developer_mode": False, "unknown": 1},
        "": {"developer_mode": True},
    }}))

    assert devices_seen.load(tmp_path) == {OTHER: {"developer_mode": False}}


def test_a_link_at_the_path_is_neither_read_nor_written_through(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"format": 1, "devices": {UDID: {"developer_mode": True}}}))
    devices_seen.path(tmp_path).symlink_to(target)

    assert devices_seen.load(tmp_path) == {}
    devices_seen.remember(tmp_path, OTHER, developer_mode=False)

    assert json.loads(target.read_text())["devices"] == {UDID: {"developer_mode": True}}
    assert not devices_seen.path(tmp_path).is_symlink()
    assert devices_seen.load(tmp_path) == {OTHER: {"developer_mode": False}}


def test_a_file_too_big_to_be_graft_s_is_nothing(tmp_path: Path) -> None:
    devices_seen.path(tmp_path).write_text(" " * (300 * 1024) + "{}")

    assert devices_seen.load(tmp_path) == {}


def test_an_unknown_fact_is_a_programming_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="color"):
        devices_seen.remember(tmp_path, UDID, color="blue")


def test_a_state_folder_that_cannot_be_written_is_not_an_error(tmp_path: Path) -> None:
    not_a_folder = tmp_path / "file"
    not_a_folder.write_text("x")

    devices_seen.remember(not_a_folder, UDID, developer_mode=True)

    assert devices_seen.load(not_a_folder) == {}
