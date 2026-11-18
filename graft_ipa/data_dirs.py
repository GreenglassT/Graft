"""Where Graft keeps its files, and moving them from where it kept them as TweakBuild.

Four XDG roots, each holding one `graft` directory:

    config  $XDG_CONFIG_HOME  (~/.config)       config.yaml, tweaks, profiles, keys
    cache   $XDG_CACHE_HOME   (~/.cache)        source IPAs, built IPAs, artwork
    state   $XDG_STATE_HOME   (~/.local/state)  the build log, repos, rollback
    data    $XDG_DATA_HOME    (~/.local/share)  tools installed for it (UnfairPlay,
                                                frida-ios-dump)

Before the rename each of these was `…/tweak-build`. `migrate_legacy_dirs`
moves an old one to the new name and leaves a symlink where it was. The link
is not a courtesy: a config written before the rename names its cert,
profiles, key and cache by absolute `…/tweak-build/...` paths, and the link is
what keeps every one of them valid without rewriting the user's file.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

APP_DIRNAME = "graft"

#: The name before the rename. Never created; only migrated away from, or
#: used in place while a migration has not happened or could not.
LEGACY_DIRNAME = "tweak-build"


def xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")


def xdg_cache_home() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def xdg_state_home() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")


def _roots() -> tuple[Path, ...]:
    return (xdg_config_home(), xdg_cache_home(), xdg_state_home(), xdg_data_home())


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def app_dir(root: Path) -> Path:
    """`root/graft`, or `root/tweak-build` while the data is still there.

    The old path is used in place when the new path does not exist and the
    old one is a directory — the migration has not run (`--describe` never
    runs it), it failed, or the old path is a link the user made to wherever
    they keep the files (followed, not moved: it is theirs). Answering the new
    path then would point every default at an empty directory while the
    user's files sit elsewhere. Our own `tweak-build -> graft` link only ever
    exists beside `graft`, so it never reaches this branch.
    """
    new = root / APP_DIRNAME
    old = root / LEGACY_DIRNAME
    if not os.path.lexists(new) and old.is_dir():
        return old
    return new


def config_dir() -> Path:
    return app_dir(xdg_config_home())


def cache_dir() -> Path:
    return app_dir(xdg_cache_home())


def state_dir() -> Path:
    return app_dir(xdg_state_home())


def data_dir() -> Path:
    return app_dir(xdg_data_home())


def _reason(e: OSError) -> str:
    return e.strerror or str(e) or type(e).__name__


def _same_place(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


def _both_exist(old: Path, new: Path) -> str:
    return (
        f"both {old} and {new} exist; using {new}, and {old} is left untouched "
        f"(move anything you still need from it into {new}, then remove it)"
    )


def migrate_legacy_dir(root: Path) -> str | None:
    """Move `root/tweak-build` to `root/graft`, leaving a symlink behind.

    Returns the one line to tell the user, or None when there was nothing to
    do. Never raises: a migration that cannot happen leaves the old directory
    exactly as it was, says so, and the CLI carries on using it (`app_dir`).

    What happens depends on what is at each path:

    - old missing, a file, or a dangling link: nothing to migrate.
    - old is our own link to new (every run after a migration): silent.
    - old is a real directory, new absent: moved, see below.
    - old is a real directory, new an *empty* real directory: the move goes
      ahead, `rename` replacing the empty one. Something made it before the
      first `graft` run — a build script's `mkdir -p`, a user's own
      `mkdir` — and keeping it would orphan the user's real config beside
      it for good.
    - both hold something (or new is a link, or old is a user's link): new
      is the one in use and nothing moves — two directories is the user's
      arrangement to sort out, not ours to merge. But it is said on every
      run, because the old one may hold the config the user believes is in
      use.
    - old is a link the user made, new absent: nothing moves; `app_dir`
      follows the link.

    The move is a rename between siblings, so it is atomic and never copies.
    The symlink is relative (`graft`) so it keeps working if the home
    directory itself is moved or restored somewhere else. If the link cannot
    be made the directory is moved back: moved-but-unlinked would leave every
    absolute old path in config.yaml dangling, which is worse than not
    migrating at all.

    One window is left open on purpose. Between the rename and the symlink a
    process still using the old path (a pre-rename CLI, a build script with
    the old path baked in) can recreate `tweak-build` as a fresh directory.
    The symlink then fails and the move back lands on — or is refused by —
    that directory; either way the outcome is reported, the data stays whole
    in one of the two places, and nothing is deleted. Closing it would take an
    atomic exchange (`renamex_np` with `RENAME_SWAP`) for a race that needs
    the old tool running at the instant of the first run of the new one.
    """
    new = root / APP_DIRNAME
    old = root / LEGACY_DIRNAME
    if not old.is_dir():
        return None
    if os.path.lexists(new):
        if not (_is_real_dir(old) and _is_real_dir(new)):
            # Looked at again before calling it two directories, because the
            # GUI starts several CLIs at once and another may have renamed one
            # onto the other since the first check: `tweak-build` gone, or
            # already our link, is a winner mid-flight or done.
            if not os.path.lexists(old) or _same_place(old, new):
                return None
            return _both_exist(old, new)
        # Both real directories: `rename` decides. It replaces an *empty*
        # `graft` atomically and refuses one with anything in it (ENOTEMPTY
        # or EEXIST), in one step no other process can get between — where
        # removing the empty one first could remove a directory another
        # process had just moved there.
    elif old.is_symlink():
        return None

    try:
        os.rename(old, new)
    except OSError as e:
        # Lost the rename to another CLI (ENOENT, or EISDIR once the winner's
        # link is in place): finding the work done is not a failure.
        if not _is_real_dir(old):
            return None
        if e.errno in (errno.ENOTEMPTY, errno.EEXIST):
            return _both_exist(old, new)
        return f"could not move {old} to {new} ({_reason(e)}); still using {old}"

    try:
        os.symlink(APP_DIRNAME, old)
    except OSError as link_error:
        try:
            os.rename(new, old)
        except OSError as back_error:
            return (
                f"moved {old} to {new} but could not leave a symlink at the old path "
                f"({_reason(link_error)}) or move it back ({_reason(back_error)}); "
                f"paths in config.yaml that start with {old} will not resolve"
            )
        return (
            f"could not move {old} to {new} (no symlink could be left at the old "
            f"path: {_reason(link_error)}); still using {old}"
        )

    return f"moved {old} to {new}; a symlink at the old path keeps existing paths working"


def migrate_legacy_dirs() -> list[str]:
    """`migrate_legacy_dir` for all four roots; one notice per directory that
    moved or could not. Each root is independent: one failure stops nothing."""
    notices: list[str] = []
    for root in _roots():
        notice = migrate_legacy_dir(root)
        if notice is not None:
            notices.append(notice)
    return notices
