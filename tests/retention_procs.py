"""Other processes on the retention lock, for the tests that need a real one.

`flock` is per open file, so a second descriptor in the test's own process
would conflict too — but what the lock exists for is a build in one process
and a sweep in another (the app's "Clean up old builds…" beside a build, two
Terminals). These helpers make that second process real:

* `held_by_another_process` holds `retention.lock` shared (`sh`, a build
  delivering) or exclusive (`ex`, a sweep) until the `with` block ends;
* `sweep_from_another_process` runs `_do_prune_builds` at `keep: 0` in a
  child, with a short wait, and returns how it went.

Every path the child touches is handed to it; HOME and the XDG roots point
under the test's own directory, so nothing reaches the real ~/.Trash or
~/.cache/graft.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

from .conftest import NO_USBMUXD

REPO_ROOT = Path(__file__).parent.parent

_HOLDER = r"""
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_SH if sys.argv[2] == "sh" else fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""

_SWEEPER = r"""
import sys, types
from pathlib import Path
from graft_ipa import cli
cli._RETENTION_LOCK_TIMEOUT = float(sys.argv[5])
cfg = types.SimpleNamespace(
    cache_dir=Path(sys.argv[1]), built_dir=Path(sys.argv[2]),
    state_dir=Path(sys.argv[3]), archive_keep=0,
)
try:
    cli._do_prune_builds(cfg, keep=0, trash_dir=Path(sys.argv[4]))
except cli.GraftError as e:
    print("refused:", e.message, flush=True)
    sys.exit(e.exit_code)
print("swept", flush=True)
"""


def lock_path(cfg) -> Path:
    return cfg.cache_dir / ".locks" / "retention.lock"


def _child_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "xdg-config"),
        "XDG_CACHE_HOME": str(home / "xdg-cache"),
        "XDG_STATE_HOME": str(home / "xdg-state"),
        "XDG_DATA_HOME": str(home / "xdg-data"),
        "GRAFT_NO_KEYCHAIN": "1",
        "USBMUXD_SOCKET_ADDRESS": NO_USBMUXD,
    })
    return env


@contextlib.contextmanager
def held_by_another_process(cfg, mode: str, *, home: Path) -> Iterator[None]:
    """`retention.lock` held `mode` ("sh" or "ex") by a child until the block ends."""
    lock = lock_path(cfg)
    lock.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock), mode],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        cwd=REPO_ROOT, env=_child_env(home),
    )
    try:
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        yield
    finally:
        assert proc.stdin is not None
        proc.stdin.close()
        proc.wait(timeout=10)


def sweep_from_another_process(
    cfg, *, trash: Path, home: Path, wait: float = 0.3
) -> subprocess.CompletedProcess[str]:
    """`--prune-builds --keep 0`'s sweep, run by a child process."""
    return subprocess.run(
        [sys.executable, "-c", _SWEEPER, str(cfg.cache_dir), str(cfg.built_dir),
         str(cfg.state_dir), str(trash), str(wait)],
        capture_output=True, text=True, timeout=60, cwd=REPO_ROOT, env=_child_env(home),
    )
