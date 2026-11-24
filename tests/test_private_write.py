"""`data_dirs.private_write`: bytes into an owner-only file, never through a link."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from graft_ipa import data_dirs


def test_the_file_is_owner_only_and_holds_exactly_the_bytes(tmp_path: Path) -> None:
    dest = tmp_path / "AppleDevelopment.p12"
    old = os.umask(0)  # a umask can only narrow the mode; this one narrows nothing
    try:
        data_dirs.private_write(b"p12 bytes", dest)
    finally:
        os.umask(old)

    assert dest.read_bytes() == b"p12 bytes"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600


def test_a_link_planted_at_the_destination_is_replaced_not_written_through(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_bytes(b"someone else's file")
    dest = tmp_path / "wildcard.mobileprovision"
    dest.symlink_to(target)

    data_dirs.private_write(b"profile bytes", dest)

    assert target.read_bytes() == b"someone else's file"
    assert not dest.is_symlink()
    assert dest.read_bytes() == b"profile bytes"


def test_a_failed_write_leaves_no_temporary_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(data_dirs.os, "fsync", full)

    with pytest.raises(OSError, match="No space left"):
        data_dirs.private_write(b"x", tmp_path / "x.p12")
    assert list(tmp_path.iterdir()) == []
