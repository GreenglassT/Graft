"""The small JSON files Graft keeps in its state folder, read the way
`devices_seen` reads its own (final review 3 #5): a regular file only, not
through a link, never waiting on a FIFO, at most a size no file Graft writes
comes near, and nothing it holds (nesting too deep for the parser included)
raises. Each is a memory, and a memory that can't be read is none.

- `update-refusals.json` (`update_refusals`), which every daily
  `--update --plan --quick` reads;
- `update-dropped-targets.json` (`update_drops`), which every plan reads;
- `playcover-settings.json` (`playcover_handoff`).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from graft_ipa import playcover_handoff, update_drops, update_refusals
from graft_ipa.config import AppConfig, CyanOptions

APP = AppConfig(name="example", bundle_id="com.example.app", preferred_version="latest",
                tweaks=[], cyan=CyanOptions())
PHONE = "00008130-FAKE00000000PHONE1"


class Memory:
    """One state file: its name, a body Graft would have written, and the
    public read that says what it remembers (None or empty: nothing)."""

    def __init__(self, filename: str, body: dict, read: Callable[[Path], object]) -> None:
        self.filename = filename
        self.body = body
        self.read = read


MEMORIES = {
    "refusals": Memory(update_refusals.FILENAME,
                       {"format": 1, "apps": {"example": {"bundle_id": "com.example.app", "version": "2.0"}}},
                       lambda state: update_refusals.refused_version(state, APP)),
    "drops": Memory(update_drops.FILENAME, {"format": 1, "apps": {"example": {PHONE: 3}}},
                    lambda state: update_drops.dropped(state, "example") or None),
    "playcover": Memory(playcover_handoff.MEMORY_FILE,
                        {"format": 1, "apps": {"com.example.app": {"keymapping": True}}},
                        lambda state: playcover_handoff.remembered(state, "com.example.app")),
}


@pytest.fixture(params=sorted(MEMORIES))
def memory(request) -> Memory:
    return MEMORIES[request.param]


def test_a_file_graft_wrote_is_read(tmp_path: Path, memory: Memory) -> None:
    """The control: the same body, as a plain file, is remembered."""
    (tmp_path / memory.filename).write_text(json.dumps(memory.body))

    assert memory.read(tmp_path) is not None


def test_nesting_too_deep_to_parse_is_no_memory(tmp_path: Path, memory: Memory) -> None:
    (tmp_path / memory.filename).write_text("[" * 10_000 + "]" * 10_000)

    assert memory.read(tmp_path) is None


def test_a_link_at_the_path_is_not_read_through(tmp_path: Path, memory: Memory) -> None:
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(memory.body))
    (tmp_path / memory.filename).symlink_to(elsewhere)

    assert memory.read(tmp_path) is None


def test_a_file_too_big_to_be_graft_s_is_no_memory(tmp_path: Path, memory: Memory) -> None:
    body = json.dumps(memory.body)
    (tmp_path / memory.filename).write_text(body[:-1] + " " * (300 * 1024) + body[-1])

    assert memory.read(tmp_path) is None


def test_a_fifo_at_the_path_is_no_memory(tmp_path: Path, memory: Memory) -> None:
    """A plain read of a FIFO waits for a writer that never comes."""
    path = tmp_path / memory.filename
    os.mkfifo(path)
    read: list[object] = []
    reader = threading.Thread(target=lambda: read.append(memory.read(tmp_path)), daemon=True)

    reader.start()
    reader.join(5)

    if reader.is_alive():
        try:  # free the stuck reader
            os.close(os.open(path, os.O_WRONLY | os.O_NONBLOCK))
        except OSError:
            pass
        pytest.fail(f"reading {memory.filename} waited on a FIFO")
    assert read == [None]


def test_a_fifo_holding_what_graft_writes_is_still_not_read(tmp_path: Path, memory: Memory) -> None:
    """Only a regular file is a memory, whatever a FIFO there carries."""
    path = tmp_path / memory.filename
    os.mkfifo(path)
    keep_open = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    writer = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(writer, json.dumps(memory.body).encode())

        assert memory.read(tmp_path) is None
    finally:
        os.close(writer)
        os.close(keep_open)


def test_a_corrupt_refusal_memory_doesnt_stop_the_daily_plan(world) -> None:
    """The plan every day reads it; deep nesting used to raise out of it."""
    from .update_fakes import plan_of

    (world.cfg.state_dir / update_refusals.FILENAME).write_text("[" * 10_000 + "]" * 10_000)
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"], quick=True)["example"]

    assert plan["store"] == {"from": "1.0", "to": "2.0", "skipped_reason": None}


def test_each_memory_still_round_trips(tmp_path: Path) -> None:
    update_refusals.remember(tmp_path, APP, "2.0")
    update_drops.set_dropped(tmp_path, "example", {PHONE: 3})
    playcover_handoff.remember(tmp_path, "com.example.app", {"keymapping": True})

    assert update_refusals.refused_version(tmp_path, APP) == "2.0"
    assert update_drops.dropped(tmp_path, "example") == {PHONE: 3}
    assert playcover_handoff.remembered(tmp_path, "com.example.app") == {"keymapping": True}
