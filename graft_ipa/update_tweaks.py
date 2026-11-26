"""One tweak update, atomically: the step `graft --update` runs before any build.

`--update-tweak` downloads into the library and re-points each app with a
config write of its own (`cli._do_update_tweak`), so a write that fails
partway leaves some apps on the new build and some on the old. An update
builds every app it re-points straight afterward, so it can't have that. Here
the new .deb is downloaded first, into a folder of its own inside the library
(`workdir`: dotted, so `--tweaks` never lists it, and on the library's
filesystem, so the move is a rename); then it is moved in and every app that
uses the tweak is re-pointed in **one** config write under `config.yaml.lock`
(`config.repoint_tweak`). A Stop during the move and the write waits for both,
however many arrive (`StopGate.deferring`); a Stop during the download just
discards it. If the write fails, the moved file is taken back out, so no app
is re-pointed. The old .deb stays, and a copy is kept for rollback, as
`--update-tweak` keeps one.

A folder a killed update left behind (a SIGKILL runs no `finally`) is swept
by the next update once it is a day old, as `appstore.private_workdir` sweeps
a get's.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from . import canister
from .config import Config, repoint_tweak
from .json_output import JsonEmitter
from .stop_deferral import StopGate
from .updates import TweakStatus

TMP_DIRNAME = ".update-tmp"
STEP = "tweaks"
#: The prefix of each download's folder: the only folders `_sweep_stale` removes.
_PREFIX = "tweak-"
#: Older than this, a folder belongs to no running update (`appstore`'s rule).
STALE_AFTER_SECONDS = 24 * 60 * 60


def _sweep_stale(root: Path) -> None:
    cutoff = time.time() - STALE_AFTER_SECONDS
    for child in root.iterdir():
        try:
            if (
                child.name.startswith(_PREFIX) and child.is_dir() and not child.is_symlink()
                and child.stat().st_mtime < cutoff
            ):
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


@contextlib.contextmanager
def workdir(tweaks_dir: Path) -> Iterator[Path]:
    """A folder of its own for one download, under the library's
    `.update-tmp`, gone when the block exits, however it exits."""
    root = tweaks_dir / TMP_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    _sweep_stale(root)
    work = Path(tempfile.mkdtemp(prefix=_PREFIX, dir=root))
    try:
        yield work
    finally:
        shutil.rmtree(work, ignore_errors=True)


@dataclass(frozen=True)
class Updated:
    filename: str
    new_filename: str
    apps: list[str]
    replaced_in_place: bool


def update(
    cfg: Config, status: TweakStatus, users: list[str], *, gate: StopGate,
    emitter: JsonEmitter | None, on_done: Callable[[Updated], None] | None = None,
) -> Updated:
    """Download `status`'s newer build and re-point `users` (the apps that use
    it, as `--tweaks` lists them) to it. Emits `tweak-updated`, with the same
    fields `--update-tweak` gives, once the write is done, and calls
    `on_done` then: inside the deferred step, so a caller learns the tweak is
    in even when a Stop is waiting to be raised as the step ends."""
    assert status.available is not None
    with workdir(cfg.tweaks_dir) as work:
        downloaded = canister.fetch_deb(
            status.available.download, work, status.available.sha256,
            version=status.available.version,
        )
        rollback_dir = cfg.state_dir / "tweak-rollback"
        rollback_dir.mkdir(parents=True, exist_ok=True)
        rollback = rollback_dir / status.filename
        shutil.copy2(cfg.tweaks_dir / status.filename, rollback)
        dest = cfg.tweaks_dir / downloaded.name
        in_place = dest.name == status.filename
        existed = dest.exists()
        with gate.deferring(None, STEP):
            old_key = cfg.tweak_key(status.filename)
            os.replace(downloaded, dest)
            if in_place:
                apps = list(users)
            else:
                try:
                    apps = repoint_tweak(cfg.path, key=cfg.tweak_key, old_key=old_key,
                                         new_filename=dest.name)
                except BaseException:
                    if not existed:
                        dest.unlink(missing_ok=True)
                    raise
            done = Updated(status.filename, dest.name, apps, in_place)
            if emitter:
                emitter.emit(
                    "tweak-updated",
                    filename=status.filename,
                    new_filename=dest.name,
                    package_id=status.package_id,
                    installed=status.installed,
                    latest=status.available.version,
                    source=status.available.source,
                    apps=apps,
                    replaced_in_place=in_place,
                    rollback=str(rollback),
                    updated=True,
                    variant=canister.variant_of(dest.name),
                )
            if on_done is not None:
                on_done(done)
    return done
