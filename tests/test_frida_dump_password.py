"""frida-ios-dump never gets the device's root password on its command line.

`dump.py -P <password>` put the SSH root password in argv, where `ps -axww`
shows it to every user on the Mac for as long as the dump runs — a minute or
more. dump.py is now started through a short `python -c` launcher that reads
the password from an environment variable, takes it out of the environment,
and sets `sys.argv` in-process before running dump.py. `ps` shows the exec
argv, which never holds it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from graft_ipa import decrypt_runner

SECRET = "the-devices-root-password"


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    return tmp_path


class _Tunnel:
    def __init__(self, *_a, **_k): pass
    def poll(self): return None
    def terminate(self): pass
    def wait(self, timeout=None): return 0
    def kill(self): pass


def _dump(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """A real `_dump_frida_ios_dump` up to the point it starts dump.py."""
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")
    monkeypatch.setattr(decrypt_runner, "ensure_frontmost",
                        lambda *_a, **_k: decrypt_runner.LAUNCH_OK)
    monkeypatch.setattr(decrypt_runner.subprocess, "Popen", _Tunnel)
    monkeypatch.setattr(decrypt_runner, "_wait_for_port", lambda _p, **_k: True)
    seen: dict = {}

    def streaming(cmd, *, cwd=None, env=None, on_line=None):
        seen["cmd"], seen["env"] = list(cmd), env
        (tmp_path / "out").mkdir(exist_ok=True)
        (tmp_path / "out" / "com.example.app.ipa").write_bytes(b"PK")
        return 0, ""

    monkeypatch.setattr(decrypt_runner, "_run_streaming", streaming)
    decrypt_runner.decrypt_app("com.example.app", tmp_path / "out", udid="UDID-EXAMPLE",
                               backend=decrypt_runner.FRIDA)
    return seen


def test_the_password_is_not_on_dump_pys_command_line(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_SSH_PASS", SECRET)
    seen = _dump(monkeypatch, tmp_path)
    assert not [arg for arg in seen["cmd"] if SECRET in arg], seen["cmd"]
    assert "-P" not in seen["cmd"]


def test_it_reaches_dump_py_through_its_own_variable_only(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_SSH_PASS", SECRET)
    seen = _dump(monkeypatch, tmp_path)
    assert seen["env"][decrypt_runner.FRIDA_DUMP_PASSWORD_ENV] == SECRET
    assert "GRAFT_SSH_PASS" not in seen["env"]


def test_a_key_sends_no_password_at_all(
    clone: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GRAFT_SSH_PASS", SECRET)
    monkeypatch.setenv("GRAFT_SSH_KEY", "/home/someone/.ssh/id_ed25519")
    seen = _dump(monkeypatch, tmp_path)
    assert "-K" in seen["cmd"]
    assert decrypt_runner.FRIDA_DUMP_PASSWORD_ENV not in seen["env"]
    assert SECRET not in seen["env"].values()


def test_a_dry_run_does_not_print_the_password(
    clone: Path, monkeypatch: pytest.MonkeyPatch, fake_iproxy: Path,
) -> None:
    monkeypatch.setenv("GRAFT_SSH_PASS", SECRET)
    out = decrypt_runner.decrypt_app("com.example.app", clone / "out", udid="UDID-EXAMPLE",
                                     backend=decrypt_runner.FRIDA, dry_run=True).stdout
    assert SECRET not in out
    assert decrypt_runner.FRIDA_DUMP_PASSWORD_ENV in out


_FAKE_DUMP = """
import json, os, sys
print(json.dumps({
    "argv": sys.argv,
    "env_has_password": %r in os.environ,
    "name": __name__,
    "file": __file__,
}))
"""


def test_the_launcher_hands_dump_py_the_password_in_process(tmp_path: Path) -> None:
    """Against a real interpreter: what dump.py sees, against what `ps` sees."""
    fake = tmp_path / "dump.py"
    fake.write_text(_FAKE_DUMP % decrypt_runner.FRIDA_DUMP_PASSWORD_ENV)
    argv = [sys.executable, "-c", decrypt_runner.FRIDA_DUMP_LAUNCHER, str(fake),
            "-o", "out.ipa", "-u", "root", "com.example.app"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", decrypt_runner.FRIDA_DUMP_PASSWORD_ENV: SECRET},
    )
    assert proc.returncode == 0, proc.stderr
    seen = json.loads(proc.stdout)
    # dump.py parses -P from its own sys.argv, run as a script...
    assert seen["argv"][0] == str(fake)
    assert seen["argv"][seen["argv"].index("-P") + 1] == SECRET
    assert seen["argv"][-1] == "com.example.app"
    assert seen["name"] == "__main__" and seen["file"] == str(fake)
    # ...and nothing it starts inherits the variable.
    assert seen["env_has_password"] is False
    # The exec argv, which is what `ps` reports, never held it.
    assert not [arg for arg in argv if SECRET in arg]
