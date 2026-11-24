"""`graft_ipa/frida_probe.py`: Graft's questions to frida, asked in
frida-ios-dump's own virtualenv (review #3: Graft.app bundles no frida).

The probe runs under another interpreter, so it is tested on its own here:
its argv (validated, never a shell), its one-line JSON answer, and which
frida calls each question makes, against a stand-in `frida` module.
"""

from __future__ import annotations

import ast
import io
import json
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from graft_ipa import frida_probe

PROBE = Path(frida_probe.__file__)


class _App:
    def __init__(self, identifier, name, pid):
        self.identifier, self.name, self.pid = identifier, name, pid


class _Proc:
    def __init__(self, pid, name):
        self.pid, self.name = pid, name


def _fake_frida(monkeypatch, *, boom: str | None = None, front=("com.example.front", "Front", 42)):
    calls = []

    class Device:
        def enumerate_processes(self):
            calls.append(("enumerate_processes",))
            if boom:
                raise RuntimeError(boom)
            return [_Proc(1, "launchd"), _Proc(42, "Front")]

        def enumerate_applications(self):
            calls.append(("enumerate_applications",))
            return [_App("com.example.front", "Front", 42), _App("com.example.idle", "Idle", 0)]

        def get_frontmost_application(self):
            calls.append(("get_frontmost_application",))
            return _App(*front) if front else None

        def spawn(self, argv):
            calls.append(("spawn", list(argv)))
            return 77

        def resume(self, pid):
            calls.append(("resume", pid))

    class Dev:
        def __init__(self, id, name, type):
            self.id, self.name, self.type = id, name, type

    fake = types.ModuleType("frida")
    fake.__version__ = "16.7.19"
    fake.enumerate_devices = lambda: [Dev("local", "Local System", "local"), Dev("00008160-0001", "iPhone", "usb")]

    def get_device(udid, timeout):
        calls.append(("get_device", udid, timeout))
        return Device()

    def get_usb_device(timeout):
        calls.append(("get_usb_device", timeout))
        return Device()

    fake.get_device, fake.get_usb_device = get_device, get_usb_device
    monkeypatch.setitem(sys.modules, "frida", fake)
    return calls


def _ask(*argv: str) -> tuple[int, dict]:
    out = io.StringIO()
    with redirect_stdout(out):
        code = frida_probe.main(list(argv))
    lines = out.getvalue().splitlines()
    assert len(lines) == 1, lines
    return code, json.loads(lines[0])


def test_each_question_is_one_frida_call_and_one_json_line(monkeypatch):
    calls = _fake_frida(monkeypatch)
    assert _ask("version") == (0, {"ok": True, "result": "16.7.19", "frida": "16.7.19"})
    assert _ask("devices")[1]["result"] == [{"id": "local", "name": "Local System", "type": "local"},
                                            {"id": "00008160-0001", "name": "iPhone", "type": "usb"}]
    assert _ask("processes", "--device", "00008160-0001")[1]["result"] == [
        {"pid": 1, "name": "launchd"}, {"pid": 42, "name": "Front"}]
    assert _ask("applications")[1]["result"] == [
        {"identifier": "com.example.front", "name": "Front", "pid": 42},
        {"identifier": "com.example.idle", "name": "Idle", "pid": 0}]
    assert _ask("frontmost")[1]["result"] == {"identifier": "com.example.front", "name": "Front", "pid": 42}
    assert _ask("launch", "--device", "00008160-0001", "com.example.app")[1]["result"] == {"pid": 77}
    assert calls == [
        ("get_device", "00008160-0001", 10), ("enumerate_processes",),
        ("get_usb_device", 10), ("enumerate_applications",),
        ("get_usb_device", 10), ("get_frontmost_application",),
        ("get_device", "00008160-0001", 10), ("spawn", ["com.example.app"]), ("resume", 77)]


def test_the_home_screen_is_null_not_an_error(monkeypatch):
    _fake_frida(monkeypatch, front=None)
    assert _ask("frontmost") == (0, {"ok": True, "result": None, "frida": "16.7.19"})


def test_a_frida_failure_is_an_answer_with_its_text(monkeypatch):
    _fake_frida(monkeypatch, boom="unable to connect to remote frida-server")
    code, reply = _ask("processes")
    assert code == 1 and reply == {"ok": False, "error": "unable to connect to remote frida-server",
                                   "frida": "16.7.19"}


def test_no_frida_in_the_interpreter_is_an_answer_too(monkeypatch):
    monkeypatch.setitem(sys.modules, "frida", None)  # `import frida` raises ImportError
    code, reply = _ask("version")
    assert code == 1 and reply["ok"] is False and "frida" in reply["error"] and reply["frida"] is None


@pytest.mark.parametrize("argv", [
    [], ["spawn", "com.example.app"], ["launch"], ["launch", "com.example.app", "extra"],
    ["launch", "com.example.app;touch x"], ["launch", "-com.example.app"], ["frontmost", "com.example.app"],
    ["processes", "--device"], ["processes", "--device", "-x"], ["processes", "--device", "a b"],
    ["processes", "--device", "U1", "--device", "U2"], ["devices", "--device", "U1"],
])
def test_anything_but_a_known_question_with_plain_arguments_is_refused(monkeypatch, argv):
    calls = _fake_frida(monkeypatch)
    code, reply = _ask(*argv)
    assert code == 2 and reply["ok"] is False and reply["error"].startswith("bad arguments")
    assert calls == [], "nothing reached frida"


def test_the_probe_imports_only_the_standard_library_and_frida():
    """It runs in frida-ios-dump's virtualenv, which has no graft_ipa."""
    tree = ast.parse(PROBE.read_text())
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert names <= {"__future__", "json", "re", "sys", "frida"}, names
