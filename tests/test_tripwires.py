"""The tripwire catches every shape a spawn can take (re-check #35).

Each shape runs in a child interpreter: if one got past the tripwire it
would really fork or exec, and doing that inside pytest would take the run
with it. The names are bound *before* the tripwire is armed, as a module's
top-level `from subprocess import Popen` would be, which is the shape that
escaped.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

_PROBE = '''
import sys
sys.path.insert(0, {repo!r})
from subprocess import Popen, run
from os import system, posix_spawn, posix_spawnp, execv, execve, execvp, spawnv, fork, popen
import os
import pytest
from tests import tripwires

tripwires.forbid_child_processes(pytest.MonkeyPatch(), "the probe")
try:
    {shape}
except AssertionError as e:
    print("CAUGHT", e)
else:
    print("ESCAPED")
'''

ECHO = "/bin/echo"

SHAPES = {
    "module-level Popen": f"Popen([{ECHO!r}, 'RAN']).wait()",
    "module-level run": f"run([{ECHO!r}, 'RAN'])",
    "module-level system": "system('echo RAN')",
    "module-level posix_spawn": f"os.waitpid(posix_spawn({ECHO!r}, ['echo', 'RAN'], {{}}), 0)",
    "module-level posix_spawnp": "os.waitpid(posix_spawnp('echo', ['echo', 'RAN'], {}), 0)",
    "module-level execv": f"execv({ECHO!r}, ['echo', 'RAN'])",
    "module-level execve": f"execve({ECHO!r}, ['echo', 'RAN'], {{}})",
    "module-level execvp": "execvp('echo', ['echo', 'RAN'])",
    "module-level spawnv": f"spawnv(os.P_WAIT, {ECHO!r}, ['echo', 'RAN'])",
    "module-level fork": "pid = fork()\n    if pid == 0: os._exit(0)\n    os.waitpid(pid, 0)",
    "module-level popen": "popen('echo RAN').read()",
    "subprocess, through the module": f"import subprocess; subprocess.run([{ECHO!r}, 'RAN'])",
}


@pytest.mark.parametrize("shape", list(SHAPES))
def test_every_spawn_shape_is_caught(shape: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE.format(repo=str(REPO), shape=SHAPES[shape])],
        capture_output=True, text=True, timeout=60,
    )
    lines = proc.stdout.splitlines()
    # "RAN" is what the spawned echo prints; "ESCAPED", the probe when
    # nothing stopped it. Whole lines: the caught message quotes the argv.
    assert "RAN" not in lines and "ESCAPED" not in lines, (
        f"{shape} started a process:\n{proc.stdout}{proc.stderr}"
    )
    assert lines and lines[0].startswith("CAUGHT"), proc.stdout + proc.stderr
    assert "the probe started a process" in proc.stdout


def test_the_tripwire_is_disarmed_after_the_test() -> None:
    from tests import tripwires

    assert tripwires.ARMED is None
    assert subprocess.run(["/usr/bin/true"]).returncode == 0
