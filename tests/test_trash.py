from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa.trash import move_to_trash


def test_moves_the_file_into_the_trash_dir(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"
    trash.mkdir()
    victim = tmp_path / "some-tweak.deb"
    victim.write_bytes(b"payload")

    dest = move_to_trash(victim, trash_dir=trash)

    assert dest == trash / "some-tweak.deb"
    assert dest.read_bytes() == b"payload"
    assert not victim.exists()


def test_uniquifies_on_collision_finder_style(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"
    trash.mkdir()
    (trash / "some-tweak.deb").write_bytes(b"older")
    victim = tmp_path / "some-tweak.deb"
    victim.write_bytes(b"newer")

    dest = move_to_trash(victim, trash_dir=trash)

    assert dest == trash / "some-tweak 2.deb"
    assert dest.read_bytes() == b"newer"
    assert (trash / "some-tweak.deb").read_bytes() == b"older", "must not clobber the existing file"


def test_uniquifier_keeps_counting_past_two(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"
    trash.mkdir()
    (trash / "some-tweak.deb").write_bytes(b"a")
    (trash / "some-tweak 2.deb").write_bytes(b"b")
    (trash / "some-tweak 3.deb").write_bytes(b"c")
    victim = tmp_path / "some-tweak.deb"
    victim.write_bytes(b"d")

    assert move_to_trash(victim, trash_dir=trash) == trash / "some-tweak 4.deb"


def test_handles_a_name_with_no_extension(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"
    trash.mkdir()
    (trash / "README").write_bytes(b"old")
    victim = tmp_path / "README"
    victim.write_bytes(b"new")

    assert move_to_trash(victim, trash_dir=trash) == trash / "README 2"


def test_creates_the_trash_dir_when_absent(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"  # deliberately not created
    victim = tmp_path / "some-tweak.deb"
    victim.write_bytes(b"payload")

    dest = move_to_trash(victim, trash_dir=trash)

    assert dest.exists()
    assert trash.is_dir()


def test_missing_source_raises(tmp_path: Path) -> None:
    trash = tmp_path / "Trash"
    trash.mkdir()
    with pytest.raises(FileNotFoundError):
        move_to_trash(tmp_path / "nope.deb", trash_dir=trash)


def test_defaults_to_the_users_trash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    victim = tmp_path / "some-tweak.deb"
    victim.write_bytes(b"payload")
    assert move_to_trash(victim) == tmp_path / ".Trash" / "some-tweak.deb"
