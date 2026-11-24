"""Graft's frida questions go to frida-ios-dump's virtualenv (review #3).

Graft.app has no frida. `decrypt_runner` asks frida through
`graft_ipa/frida_probe.py`, run by frida-ios-dump's own `.venv/bin/python`.
These tests stand in for that interpreter with a script that records its
argv and answers each question with canned JSON, so they check both ends of
the contract: the argv Graft sends, and what it makes of the answers. Then
the same calls with no frida-ios-dump at all, which must fail plainly (and
leave the readiness card listing what's missing), and bagbak, which never
needed Graft's frida and must still dump.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from graft_ipa import decrypt_ready, decrypt_runner

UDID = "00008160-0000000000C0FFEE"
OTHER = "00008030-001234567890802E"

ANSWERS = {
    "version": {"ok": True, "result": "16.7.19", "frida": "16.7.19"},
    "devices": {"ok": True, "frida": "16.7.19", "result": [
        {"id": OTHER, "name": "iPad", "type": "usb"}, {"id": UDID, "name": "iPhone", "type": "usb"},
        {"id": "local", "name": "Local System", "type": "local"}]},
    "processes": {"ok": True, "frida": "16.7.19", "result": [{"pid": 1, "name": "launchd"},
                                                             {"pid": 42, "name": "Front"}]},
    "applications": {"ok": True, "frida": "16.7.19", "result": [
        {"identifier": "com.example.front", "name": "Front", "pid": 42},
        {"identifier": "com.example.idle", "name": "Idle", "pid": 0}]},
    "frontmost": {"ok": True, "frida": "16.7.19",
                  "result": {"identifier": "com.example.front", "name": "Front", "pid": 42}},
    "launch": {"ok": True, "frida": "16.7.19", "result": {"pid": 77}},
}


def _dump_venv(tmp_path: Path, monkeypatch, answers: dict) -> Path:
    """A frida-ios-dump clone whose venv `python` records its argv (one JSON
    list per line in the returned log) and prints answers[question]."""
    home = tmp_path / "frida-ios-dump"
    (home / ".venv" / "bin").mkdir(parents=True)
    (home / "dump.py").write_text("# frida-ios-dump's dump.py\n")
    log = tmp_path / "probe-argv.jsonl"
    python = home / ".venv" / "bin" / "python"
    python.with_suffix(".py").write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        f"open({str(log)!r}, 'a').write(json.dumps(argv) + '\\n')\n"
        f"answers = json.loads({json.dumps(answers)!r})\n"
        "reply = answers.get(argv[2] if len(argv) > 2 else '', {'ok': False, 'error': 'no answer'})\n"
        "print('a line frida printed first')\n"
        "print(json.dumps(reply))\n"
        "sys.exit(0 if reply.get('ok') else 1)\n")
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$0.py" "$@"\n')
    python.chmod(0o755)
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", home)
    return log


def _asked(log: Path) -> list[list[str]]:
    probe = str(decrypt_runner.FRIDA_PROBE)
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(r[:2] == ["-I", probe] for r in rows), rows
    return [r[2:] for r in rows]


# -------------------------------------------------- (a) through the dump venv

def test_frida_questions_run_the_probe_with_the_dump_venvs_python(tmp_path, monkeypatch):
    log = _dump_venv(tmp_path, monkeypatch, ANSWERS)
    assert decrypt_runner.frontmost_app(UDID) == "com.example.front"
    assert decrypt_runner.app_is_running("com.example.front", None) is True
    assert decrypt_runner.app_is_running("com.example.idle", UDID) is False, "pid 0 is not running"
    assert decrypt_runner._app_pid("com.example.front", UDID) == 42
    assert decrypt_runner._pid_alive(42, UDID) is True and decrypt_runner._pid_alive(9, UDID) is False
    device = decrypt_runner._frida_device(decrypt_runner._import_frida("launch the app to dump it"), UDID)
    pid = device.spawn(["com.example.app"])
    device.resume(pid)
    assert pid == 77
    assert _asked(log) == [
        ["frontmost", "--device", UDID], ["applications"], ["applications", "--device", UDID],
        ["applications", "--device", UDID], ["processes", "--device", UDID], ["processes", "--device", UDID],
        ["launch", "--device", UDID, "com.example.app"]], "launch spawns and resumes: one probe, no resume"


def test_the_wrong_device_guard_reads_the_dump_venvs_device_list(tmp_path, monkeypatch):
    log = _dump_venv(tmp_path, monkeypatch, ANSWERS)
    with pytest.raises(decrypt_runner.DecryptError, match="iPad"):
        decrypt_runner._refuse_if_frida_dump_would_target_another_device(UDID)
    decrypt_runner._refuse_if_frida_dump_would_target_another_device(OTHER)
    assert _asked(log) == [["devices"], ["devices"]]


def test_the_readiness_probe_counts_processes_through_the_dump_venv(tmp_path, monkeypatch):
    _dump_venv(tmp_path, monkeypatch, ANSWERS)
    monkeypatch.setattr(decrypt_ready, "_server_version", lambda udid: "16.7.19")
    assert decrypt_ready._check_frida(UDID) == (True, "reachable, 2 processes", "16.7.19")
    assert decrypt_ready._frontmost(UDID) == ("com.example.front", "Front")


def test_a_frida_major_mismatch_names_the_dump_venvs_frida_and_its_pip(tmp_path, monkeypatch):
    answers = {**ANSWERS, "processes": {"ok": False, "frida": "16.7.19", "error":
                                        "unable to communicate with remote frida-server; please ensure "
                                        "that major versions match"}}
    _dump_venv(tmp_path, monkeypatch, answers)
    ok, detail, fix = decrypt_ready._check_frida(UDID)
    assert not ok and "we have 16.7.19" in detail
    assert str(tmp_path / "frida-ios-dump" / ".venv" / "bin" / "pip") in fix and "'frida==16.*'" in fix


def test_an_unreachable_device_is_frida_servers_problem_not_the_probes(tmp_path, monkeypatch):
    answers = {**ANSWERS, "frontmost": {"ok": False, "frida": "16.7.19", "error": "unable to connect"}}
    _dump_venv(tmp_path, monkeypatch, answers)
    with pytest.raises(decrypt_runner.DecryptError, match="couldn't reach frida-server"):
        decrypt_runner.frontmost_app(UDID)


def test_a_probe_that_prints_no_answer_is_an_error_with_its_output(tmp_path, monkeypatch):
    _dump_venv(tmp_path, monkeypatch, {})
    (tmp_path / "frida-ios-dump" / ".venv" / "bin" / "python.py").write_text(
        "import sys\nprint('Traceback: frida exploded', file=sys.stderr)\nsys.exit(1)\n")
    frida = decrypt_runner._import_frida("read the foreground app")
    with pytest.raises(decrypt_runner.FridaProbeError, match="frida exploded"):
        frida.enumerate_devices()


# ---------------------------------------------------- (b) no frida-ios-dump

def _no_dump_venv(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path / "no-frida-ios-dump")


def test_without_frida_ios_dump_a_frida_question_says_what_to_set_up(tmp_path, monkeypatch):
    _no_dump_venv(tmp_path, monkeypatch)
    with pytest.raises(decrypt_runner.DecryptError) as e:
        decrypt_runner.frontmost_app(UDID)
    assert e.value.message == "the frida module is needed to read the foreground app"
    assert decrypt_runner.frida_ios_dump_setup_command() in e.value.hint
    assert decrypt_runner.app_is_running.__name__  # the others fail the same way:
    for call in (lambda: decrypt_runner.app_is_running("com.example.app", UDID),
                 lambda: decrypt_runner.ensure_frontmost("com.example.app", UDID)):
        with pytest.raises(decrypt_runner.DecryptError, match="the frida module is needed"):
            call()
    assert decrypt_runner._app_pid("com.example.app", UDID) is None
    assert decrypt_runner._pid_alive(42, UDID) is True, "can't tell, so not reported dead"
    decrypt_runner._refuse_if_frida_dump_would_target_another_device(UDID)  # no guard, no error


def test_without_frida_ios_dump_the_readiness_card_lists_what_is_missing(tmp_path, monkeypatch):
    _no_dump_venv(tmp_path, monkeypatch)
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: None)
    monkeypatch.setattr(decrypt_ready, "_check_ssh", lambda udid: (True, "SSH-2.0-OpenSSH", None, None))
    report = decrypt_ready.check(UDID)  # never raises
    frida_row = next(c for c in report.checks if c.key == "frida")
    assert not frida_row.ok and frida_row.detail == "the frida module is needed to check the jailbreak"
    assert decrypt_runner.frida_ios_dump_setup_command() in frida_row.fix
    rows = {b.name: b for b in report.backends}
    assert not rows[decrypt_runner.FRIDA].usable
    assert rows[decrypt_runner.FRIDA].note == decrypt_runner.frida_ios_dump_setup_command()
    assert not rows[decrypt_runner.BAGBAK].usable and rows[decrypt_runner.BAGBAK].reason == "bagbak is not installed"
    assert report.recommended_backend is None and not report.can_dump_from_list


# ------------------------------------------------------- (c) bagbak, no venv

def _fake_bagbak(tmp_path, monkeypatch) -> Path:
    """A bagbak that writes an IPA, and the only one on PATH: the dump runs
    `bagbak` by name, and this Mac may have the real one (which would reach
    for a device)."""
    bagbak = tmp_path / "bin" / "bagbak"
    bagbak.parent.mkdir()
    bagbak.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; shift; done\n'
                      'printf "PK" > "$out/com.example.app.ipa"\n')
    bagbak.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bagbak.parent}:/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: str(bagbak))
    monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 4)
    monkeypatch.setenv("GRAFT_SSH_PASS", "not-a-real-password")
    return bagbak


def test_bagbak_still_dumps_without_frida_ios_dump(tmp_path, monkeypatch):
    """bagbak brings its own frida (Node's); it never used Graft's."""
    _no_dump_venv(tmp_path, monkeypatch)
    bagbak = _fake_bagbak(tmp_path, monkeypatch)
    out = tmp_path / "out"
    result = decrypt_runner.decrypt_app("com.example.app", out, udid=UDID, backend=decrypt_runner.BAGBAK)
    assert result.ipa_path == out / "com.example.app.ipa" and bagbak.is_file()


def test_without_frida_ios_dump_bagbak_is_usable_but_unverified(tmp_path, monkeypatch):
    """bagbak brings its own frida: a check Graft can no longer run without
    frida-ios-dump must not block it. If frida-server is down, the dump says so."""
    _no_dump_venv(tmp_path, monkeypatch)
    _fake_bagbak(tmp_path, monkeypatch)
    monkeypatch.setattr(decrypt_ready, "_check_ssh", lambda udid: (True, "SSH-2.0-OpenSSH", None, None))
    monkeypatch.setattr(decrypt_ready, "_bagbak_frida_version", lambda: "16.7.19")
    report = decrypt_ready.check(UDID)
    rows = {b.name: b for b in report.backends}
    bag = rows[decrypt_runner.BAGBAK]
    assert bag.usable and bag.note == decrypt_ready.FRIDA_SERVER_UNCHECKED
    assert "frida-server not checked" in bag.note and "frida-ios-dump" in bag.note
    assert report.recommended_backend == decrypt_runner.BAGBAK and report.can_dump_from_list
    assert not rows[decrypt_runner.FRIDA].usable


def test_bagbak_unverified_still_needs_what_it_can_be_checked_for(tmp_path, monkeypatch):
    """Unverified is about frida-server only: a 4.x bagbak still needs SSH."""
    _no_dump_venv(tmp_path, monkeypatch)
    _fake_bagbak(tmp_path, monkeypatch)
    monkeypatch.setattr(decrypt_ready, "_check_ssh", lambda udid: (False, "no SSH server answered", "fix", "x"))
    monkeypatch.setattr(decrypt_runner, "mode_is_supported", lambda major=None: False)
    bag = {b.name: b for b in decrypt_ready.check(UDID).backends}[decrypt_runner.BAGBAK]
    assert not bag.usable and "SSH" in bag.reason


def test_in_graft_app_bagbak_runs_with_its_own_folder_on_path(tmp_path, monkeypatch):
    """Graft.app gives children its Helpers/bin and the system's folders only,
    but bagbak is `#!/usr/bin/env node`, with node beside it in npm's folder."""
    from graft_ipa import tools
    _no_dump_venv(tmp_path, monkeypatch)
    bindir = tmp_path / "npm" / "bin"
    bindir.mkdir(parents=True)
    (bindir / "fakenode").write_text('#!/bin/sh\nexec /bin/sh "$@"\n')
    (bindir / "bagbak").write_text('#!/usr/bin/env fakenode\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out=$2; '
                                   'shift; done\nprintf "PK" > "$out/com.example.app.ipa"\n')
    for f in ("fakenode", "bagbak"):
        (bindir / f).chmod(0o755)
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
    monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 4)
    monkeypatch.setenv("GRAFT_SSH_PASS", "not-a-real-password")
    out = tmp_path / "out"
    result = decrypt_runner.decrypt_app("com.example.app", out, udid=UDID, backend=decrypt_runner.BAGBAK)
    assert result.ipa_path == out / "com.example.app.ipa"


# --------------------------- cli-10: a frida that won't import in the dump venv

#: What `frida_probe.py` answers when its interpreter has no working frida.
UNIMPORTABLE = {"ok": False, "error": "frida can't be imported here: No module named 'frida._frida'",
                "frida": None}


def _broken_frida(tmp_path, monkeypatch) -> None:
    _dump_venv(tmp_path, monkeypatch, {q: UNIMPORTABLE for q in ANSWERS})


def _pip(tmp_path) -> str:
    return str(tmp_path / "frida-ios-dump" / ".venv" / "bin" / "pip")


def test_a_broken_frida_in_the_dump_venv_is_said_as_that_not_as_the_phone(tmp_path, monkeypatch):
    """It was "couldn't reach frida-server", with "open the Dopamine app":
    advice for a healthy phone, and the one thing to fix was this Mac's
    virtualenv (the whole-diff review's cli-10). 1.1 said so at the import."""
    _broken_frida(tmp_path, monkeypatch)
    with pytest.raises(decrypt_runner.DecryptError) as e:
        decrypt_runner.frontmost_app(UDID)
    assert "can't import frida" in e.value.message and "couldn't reach" not in e.value.message
    assert _pip(tmp_path) in (e.value.hint or "") and "'frida==16.*'" in e.value.hint
    assert "Dopamine" not in e.value.hint


def test_a_broken_frida_isnt_an_app_that_isnt_running(tmp_path, monkeypatch):
    _broken_frida(tmp_path, monkeypatch)
    with pytest.raises(decrypt_runner.DecryptError, match="can't import frida"):
        decrypt_runner.app_is_running("com.example.front", UDID)


def test_the_readiness_card_says_a_broken_frida_is_this_mac_s(tmp_path, monkeypatch):
    _broken_frida(tmp_path, monkeypatch)
    ok, detail, fix = decrypt_ready._check_frida(UDID)
    assert not ok and "can't import frida" in detail
    assert fix is not None and _pip(tmp_path) in fix and "Dopamine" not in fix


def test_a_probe_failing_on_the_device_still_points_at_the_phone(tmp_path, monkeypatch):
    """The other side of it: frida imported (its version is in the reply)."""
    answers = {**ANSWERS, "processes": {"ok": False, "frida": "16.7.19", "error": "unable to connect"}}
    _dump_venv(tmp_path, monkeypatch, answers)
    ok, detail, fix = decrypt_ready._check_frida(UDID)
    assert not ok and fix is None and "not reachable" in detail
