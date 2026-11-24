"""A stand-in for `graft_ipa.tools.which`, the resolver's PATH lookup.

`tools.resolve` checks that what it found is an executable file, so a fake
has to hand back a real one: a made-up `/opt/fake/bin/<tool>` reads as
missing. Each tool here is a do-nothing script under `root`, made on first
lookup. Tests that use this find tools; they never run them.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path


def fake_path(
    root: Path, *, missing: Iterable[str] = (), seen: list[str] | None = None
) -> Callable[[str], str | None]:
    """Every tool is on PATH, under `root`, except those in `missing`.
    Each name looked up is appended to `seen` when given."""
    absent = frozenset(missing)

    def which(name: str) -> str | None:
        if seen is not None:
            seen.append(name)
        if name in absent:
            return None
        exe = root / name
        if not exe.exists():
            root.mkdir(parents=True, exist_ok=True)
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o755)
        return str(exe)

    return which
