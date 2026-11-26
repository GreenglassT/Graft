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
import json
import os
import re
import secrets
import shutil
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

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


#: What Graft's own directories are kept at: the .p12, profiles, staged IPAs,
#: archived builds and build logs are nobody else's business.
PRIVATE_DIR_MODE = 0o700


def secure_app_dirs() -> None:
    """Create Graft's four directories owner-only, and tighten them if not.

    Run at startup, after `migrate_legacy_dirs`, so the directories every
    default resolves to exist 0700 before anything is written into them: a
    `mkdir(parents=True)` of a subdirectory would otherwise create the parent
    at the default 0755 on a first run.

    Only the four `…/graft` directories (or a pre-rename one still in use) and
    only when they are real directories. Never the XDG roots, which are shared
    with every other program; never what is inside, which is private by being
    inside; and never a link the user made, which points at something of
    theirs. A legacy `tweak-build` link the user made is followed by `app_dir`
    while `graft` is absent, so nothing is created beside it either. Failures
    are ignored: this must not be the reason a run cannot start.
    """
    for root in _roots():
        path = app_dir(root)
        try:
            if not os.path.lexists(path):
                # Only ever `graft`. `app_dir` answers `tweak-build` while the
                # data is still there, and another CLI can rename it away in
                # between: creating a fresh one then broke that CLI's symlink
                # and sent its move back onto an empty directory.
                if path.name != APP_DIRNAME:
                    continue
                path.mkdir(mode=PRIVATE_DIR_MODE, parents=True)
                os.chmod(path, PRIVATE_DIR_MODE)  # exactly, whatever the umask
            elif _is_real_dir(path) and (path.stat().st_mode & 0o777) != PRIVATE_DIR_MODE:
                os.chmod(path, PRIVATE_DIR_MODE)
        except OSError:
            continue


#: How old a hidden partial file must be before a startup sweep removes it:
#: long past any staging or archiving still running in another Graft.
STALE_PARTIAL_SECONDS = 24 * 60 * 60


#: `tempfile.mkstemp`'s random part: eight of `[a-z0-9_]`
#: (`tempfile._RandomNameSequence`). Should that ever change, the sweep
#: stops matching rather than starts matching too much.
_MKSTEMP_RANDOM = r"[a-z0-9_]{8}"

#: The hidden temporaries Graft writes into `cache_dir` and `built_dir`, and
#: no other name. Both directories are settings and can be a folder other
#: things write to; "any hidden `.part` or `.partial`" swept a browser's or a
#: sync tool's unfinished download along with Graft's.
_PARTIAL_NAME = re.compile(
    rf"\..+\.ipa\.{_MKSTEMP_RANDOM}\.partial"         # private_copy: archiving a build
    rf"|\..+\.{_MKSTEMP_RANDOM}\.partial\.ipa"        # provide_ipa.stage: staging, scrubbing
    r"|\.com\.kdt\.livecontainer-[0-9a-f]{8}\.part"   # lc_manage.ensure_ipa
)


def _is_partial(name: str) -> bool:
    """One of the hidden temporaries Graft writes beside a file it is
    replacing: `private_copy`'s `.<name>.ipa.<random>.partial`,
    `provide_ipa.stage`'s `.<stem>.<random>.partial.ipa`, and
    `lc_manage.ensure_ipa`'s `.com.kdt.livecontainer-<hex>.part`."""
    return _PARTIAL_NAME.fullmatch(name) is not None


def sweep_stale_partials(*dirs: Path, older_than: float = STALE_PARTIAL_SECONDS) -> list[Path]:
    """Remove hidden partial files left in `dirs` by a run that never finished.

    Every writer here removes its own temporary on any exception, a Stop
    included — but not on SIGKILL (the GUI escalates to one two seconds after
    a Stop), a crash, or a power cut, and retention skips dotfiles, so those
    stayed for good: whole IPAs, some still holding the buyer's Apple ID data.
    Only files older than `older_than`, so another run's write in progress is
    never touched, and only the names Graft's own writers use (`_is_partial`),
    so another program's files in a shared folder are never touched either.
    Never raises; returns what it removed.
    """
    cutoff = time.time() - older_than
    removed: list[Path] = []
    for directory in dirs:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if (
                    _is_partial(entry.name)
                    and entry.is_file(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                ):
                    os.unlink(entry.path)
                    removed.append(Path(entry.path))
            except OSError:
                continue
    return removed


def private_copy(src: Path, dest: Path) -> None:
    """Copy `src` to `dest` owner-only (0600), whatever `src`'s mode.

    Not `shutil.copy2`, which carries the source's mode across. Written to a
    0600 temp file beside `dest` and renamed over it, so `dest` is never
    briefly another mode, nor half-written.
    """
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".partial")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as source:
            shutil.copyfileobj(source, out, 1 << 20)
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def private_stage(data: bytes, dest: Path) -> Path:
    """`data` in a fresh owner-only file beside `dest`, synced, not yet in its
    place; returns that file. `private_write` renames it over `dest`;
    `account_setup` renames it there itself, once the old entry is kept aside.

    Created 0600 with `O_EXCL | O_NOFOLLOW`, so nothing already at its random
    name, a planted link included, is written through. Removed again if the
    write fails.
    """
    tmp = dest.parent / f".{dest.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            # The umask can only narrow the mode above; this makes it exact.
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def private_write(data: bytes, dest: Path) -> None:
    """Put `data` at `dest`, owner-only (0600) from the first byte.

    Bytes, not a source path: what lands at `dest` is exactly what the caller
    read and checked, whatever the source path holds by now.

    A `copy2` followed by a `chmod` leaves the file at `dest` with the
    umask's mode until the chmod lands, and opening `dest` for writing follows
    a symlink planted there. So the bytes go into a fresh file beside it
    (`private_stage`) and are renamed over `dest`: a rename replaces a
    symlink at `dest` rather than writing through it.

    The App Store Connect key (`setup_portal`) is written this way.
    """
    tmp = private_stage(data, dest)
    try:
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


#: The most a small state file (`read_small_json`) may be: far above any
#: Graft writes, so anything larger isn't one of its own.
SMALL_STATE_FILE_BYTES = 256 * 1024


def read_small_json(path: Path, *, limit: int = SMALL_STATE_FILE_BYTES) -> Any:
    """What one of Graft's small JSON state files holds, or None when there's
    nothing Graft could have written there: read only as a regular file, not
    through a link, of at most `limit` bytes, and opened without blocking, so
    a FIFO at the path can't hang the read. JSON nested too deep to parse is
    None too. As `devices_seen` reads its own file; for the memories a reader
    takes leniently (`update_refusals`, `update_drops`, `playcover_handoff`).
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        data = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > limit:
        return None
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        return None
