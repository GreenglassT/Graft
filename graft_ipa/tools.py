"""Which external tool to run: one rule for cyan, zsign, ipatool and UnfairPlay.

In order:

1. **config**: a path the user set explicitly in config.yaml. Used when it is
   there, even over Graft.app's own copy (the documented override), and,
   outside Graft.app, even when it is missing, so the error names the place
   they chose. Inside Graft.app a configured path that isn't there any more
   gives way to the bundled copy (RC audit #20: a 1.x user removed the
   Homebrew ipatool or the UnfairPlay build the setting named, as the upgrade
   guide says they may), and `--appstore-status` says so.
2. **bundled**: the copy inside Graft.app, when this CLI is the app's own:
   `<Graft.app>/Contents/Helpers/bin/<name>`, found from the interpreter's own
   location. No environment variable can point it elsewhere.
3. **default**: where the tool's install instructions put it.
4. **PATH**: absolute entries only (`child_env.which`).

A result says which rung answered (`source`), so `--check` and
`--appstore-status` can say "bundled" or "PATH" instead of leaving the user to
guess which copy ran.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from . import child_env

#: What Graft.app carries in Contents/Helpers/bin.
BUNDLED_TOOLS = frozenset({"cyan", "zsign", "ipatool", "unfairplay"})

SOURCE_CONFIG = "config"
SOURCE_BUNDLED = "bundled"
SOURCE_DEFAULT = "default"
SOURCE_PATH = "PATH"
SOURCE_MISSING = "missing"


@dataclass(frozen=True)
class Resolved:
    name: str
    #: The binary to run, or, when `source` is "missing", where it was expected.
    path: Path | None
    source: str

    @property
    def found(self) -> bool:
        return self.path is not None and executable(self.path)


def executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def which(name: str) -> str | None:
    """PATH lookup, absolute entries only. A seam tests replace."""
    return child_env.which(name)


def _bundled_tools_dir_for(executable_path: str) -> Path | None:
    for parent in Path(executable_path).parents:
        if parent.suffix == ".app":
            helpers = parent / "Contents" / "Helpers" / "bin"
            return helpers if helpers.is_dir() else None
    return None


def bundled_tools_dir() -> Path | None:
    """`<Graft.app>/Contents/Helpers/bin` when this interpreter runs from inside
    an app bundle, else None. `sys.executable` is not resolved: the bundle's
    launcher execs the embedded interpreter by its in-bundle path."""
    return _bundled_tools_dir_for(sys.executable)


def resolve(name: str, *, configured: Path | None = None, default: Path | None = None) -> Resolved:
    bundled = bundled_tools_dir()
    has_bundled = bundled is not None and executable(bundled / name)
    # Missing means nothing there at all (a dangling symlink too); a file
    # that is there but won't run is still the user's choice, named in the
    # error.
    if configured is not None and (configured.exists() or not has_bundled):
        return Resolved(name, configured, SOURCE_CONFIG)
    if has_bundled:
        return Resolved(name, bundled / name, SOURCE_BUNDLED)
    if default is not None and executable(default):
        return Resolved(name, default, SOURCE_DEFAULT)
    found = which(name)
    if found:
        return Resolved(name, Path(found), SOURCE_PATH)
    return Resolved(name, default, SOURCE_MISSING)
