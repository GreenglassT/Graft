"""Moving files to the macOS Trash.

We move the file into ``~/.Trash`` directly rather than driving Finder via
``osascript``. Finder automation would give us a working "Put Back", but it
triggers a TCC automation prompt and fails in headless CLI use. The tradeoff:
the file is recoverable from the Trash, but restoring it means dragging it back.
"""

from __future__ import annotations

import shutil
from pathlib import Path


def default_trash_dir() -> Path:
    return Path.home() / ".Trash"


def move_to_trash(path: Path, *, trash_dir: Path | None = None) -> Path:
    """Move ``path`` into the Trash and return where it landed.

    On a name collision the item is renamed Finder-style — ``some-tweak.deb`` becomes
    ``some-tweak 2.deb``, then ``some-tweak 3.deb``, and so on — so an existing item in the
    Trash isn't clobbered. The check-then-move isn't atomic, which is
    acceptable for a single-user CLI.

    Raises FileNotFoundError if ``path`` doesn't exist. OSError propagates from
    the move itself (permissions, ENOSPC, cross-device copy failure) — callers
    that need a clean CLI error should translate it.
    """
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    destination_dir = trash_dir if trash_dir is not None else default_trash_dir()
    destination_dir.mkdir(parents=True, exist_ok=True)

    dest = destination_dir / path.name
    counter = 2
    # Split on the last dot only, like Finder: "a.tar.gz" -> "a.tar 2.gz".
    while dest.exists():
        dest = destination_dir / f"{path.stem} {counter}{path.suffix}"
        counter += 1

    shutil.move(path, dest)
    return dest
