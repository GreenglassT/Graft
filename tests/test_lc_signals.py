"""A SIGTERM during signing must not leave our keychain on the search list.

The GUI's Stop, and CLIRunner's cancellation, end a CLI run with SIGTERM.
Python's default action for SIGTERM kills the process without running a
single `finally`, so `temp_keychain` never took its keychain back off the
user's search list — unlocked, for up to the hour its auto-lock allows.

This runs a real child interpreter through the same `exit_on_termination`
the `--lc-*` verbs wrap themselves in, with a file-backed fake `security`
(the search list lives in a JSON file, so nothing touches a real keychain),
signals it mid-signing, and reads what it left behind.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from .test_lc_sign import LOGIN, REAL_PASSWORD, SYSTEM, _make_p12

ROOT = Path(__file__).resolve().parent.parent

CHILD = r'''
import json, subprocess, sys, time
from pathlib import Path
from graft_ipa import lc_manage, lc_sign

state, p12, work, guarded = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4] == "1"

class FileRunner(lc_sign.Runner):
    def run(self, cmd, *, cwd=None, check=True):
        st = json.loads(state.read_text())
        st["calls"].append(cmd)
        out = ""
        if cmd == ["security", "list-keychains", "-d", "user"]:
            out = "".join(f'    "{p}"\n' for p in st["search"])
        elif cmd[:5] == ["security", "list-keychains", "-d", "user", "-s"]:
            st["search"] = cmd[5:]
        elif cmd[:2] == ["security", "delete-keychain"]:
            st["search"] = [p for p in st["search"] if p != cmd[2]]
        state.write_text(json.dumps(st))
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

def sign():
    with lc_sign.temp_keychain(FileRunner(), p12, "PASSWORD", work) as kc:
        print("SIGNING", kc, flush=True)
        time.sleep(30)

if guarded:
    with lc_manage.exit_on_termination():
        sign()
else:
    sign()
'''


def _run_and_terminate(tmp_path: Path, *, guarded: bool) -> tuple[int, dict, str]:
    p12, _ = _make_p12(tmp_path)
    state = tmp_path / "security.json"
    state.write_text(json.dumps({"search": [LOGIN, SYSTEM], "calls": []}))
    script = tmp_path / "child.py"
    script.write_text(CHILD.replace('"PASSWORD"', repr(REAL_PASSWORD)))
    proc = subprocess.Popen(
        [sys.executable, str(script), str(state), str(p12), str(tmp_path), "1" if guarded else "0"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    try:
        line = proc.stdout.readline()
        assert line.startswith("SIGNING"), (line, proc.stderr.read())
        keychain = line.split(" ", 1)[1].strip()
        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    return code, json.loads(state.read_text()), keychain


def test_sigterm_mid_signing_takes_the_keychain_off_the_list(tmp_path: Path) -> None:
    code, state, keychain = _run_and_terminate(tmp_path, guarded=True)

    assert code == 128 + signal.SIGTERM
    assert state["search"] == [LOGIN, SYSTEM]
    assert ["security", "delete-keychain", keychain] in state["calls"]


def test_the_control_without_the_handler_leaks_it(tmp_path: Path) -> None:
    """The premise, measured: an unguarded SIGTERM runs no `finally`. If this
    stops leaking, the test above no longer proves the handler matters."""
    code, state, keychain = _run_and_terminate(tmp_path, guarded=False)

    assert code == -signal.SIGTERM
    assert keychain in state["search"]


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGHUP])
def test_the_previous_handlers_come_back(sig: signal.Signals) -> None:
    from graft_ipa import lc_manage

    before = signal.getsignal(sig)
    with lc_manage.exit_on_termination():
        assert signal.getsignal(sig) is not before
    assert signal.getsignal(sig) is before


def test_off_the_main_thread_it_is_a_no_op_not_a_crash() -> None:
    """`signal.signal` raises ValueError outside the main thread. The verbs
    always run on it, but a caller that does not (a test harness, an
    embedding) must still get its body run rather than an exception."""
    import threading

    from graft_ipa import lc_manage

    outcome: dict[str, object] = {}

    def worker() -> None:
        try:
            with lc_manage.exit_on_termination():
                outcome["ran"] = True
        except Exception as e:  # the assertion is that nothing is raised
            outcome["error"] = e

    before = signal.getsignal(signal.SIGTERM)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert outcome == {"ran": True}
    assert signal.getsignal(signal.SIGTERM) is before
