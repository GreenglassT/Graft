"""Tripwires for "this starts no child process".

Patching one module's `subprocess` attribute proves nothing about a spawn
written any other way (`from subprocess import run`, a helper in another
module, `os.posix_spawn`). So every name Python starts a process through is
replaced, for a clear message at the call; and, because a name bound before
the patch (a module-level `from subprocess import Popen`, `from os import
system`) still holds the real function, an audit hook is the backstop. It
sees the interpreter's own spawn events — `subprocess.Popen`, `os.system`,
`os.posix_spawn`, `os.exec`, `os.spawn`, `os.fork` — however the function was
reached, and raises before the process starts. So any spawn from anywhere,
in this process, raises (re-check #35).

The `_posixsubprocess.fork_exec` patch this used to rest on was inert:
`subprocess` binds its own `_fork_exec` at import, and never reads the
module attribute again.
"""

from __future__ import annotations

import _posixsubprocess
import os
import subprocess
import sys

import pytest

_SPAWNERS = [
    (subprocess, ["Popen", "run", "call", "check_call", "check_output", "getoutput",
                  "getstatusoutput", "_fork_exec"]),
    (os, ["system", "popen", "posix_spawn", "posix_spawnp", "fork", "forkpty", "execv",
          "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe", "spawnv",
          "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle"]),
    (_posixsubprocess, ["fork_exec"]),
]

#: The audit events that come before a new process, whichever name ran it.
SPAWN_EVENTS = frozenset({
    "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec", "os.spawn",
    "os.fork", "os.forkpty",
})

#: What the armed tripwire is guarding, or None. Set through `monkeypatch`, so
#: it is disarmed again when the test ends.
ARMED: str | None = None


def _audit(event: str, args: tuple) -> None:
    if ARMED is not None and event in SPAWN_EVENTS:
        raise AssertionError(f"{ARMED} started a process ({event}: {args!r:.200})")


# Once per process: an audit hook can't be removed, so it is armed by a flag.
sys.addaudithook(_audit)


def forbid_child_processes(monkeypatch: pytest.MonkeyPatch, what: str = "the code under test") -> None:
    def no_child(*a, **k):
        raise AssertionError(f"{what} started a process: {a[0] if a else k}")

    for module, names in _SPAWNERS:
        for name in names:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, no_child)
    monkeypatch.setattr(sys.modules[__name__], "ARMED", what)
