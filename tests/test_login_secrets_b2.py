"""Release gate B.2: the Apple ID password and the 2FA code reach ipatool's
prompt and nowhere else, and the Apple ID is never an argument.

The real CLI as a subprocess, the way Graft.app runs it: `--json
--appstore-login`, the Apple ID and the password as stdin's first two lines,
the code after `needs-code`. ipatool is the fake, which writes down its argv,
its whole environment and hashes of what it was typed. Then every place a
secret could leak is searched:

- the NDJSON stream and stderr, for the password and the code (the stream
  names the signed-in email on purpose: Settings shows it);
- every file under the temp HOME, which holds the config, cache, state and
  data dirs, so any log or `log.sqlite` Graft writes is in there; under the
  TMPDIR the CLI and ipatool are given (its own scratch folder, so a
  `tempfile` write is seen); and beside the fake ipatool, for all three;
- ipatool's argv and environment, for all three;
- `ps`: every process's arguments, and every one of this user's processes'
  environments, read while the CLI waits for the code, which is when both
  processes are alive, for all three (controller decision 3: the Apple ID
  must not show in `ps` either).

Positive controls show the search can find something: ipatool really was
typed the right Apple ID, password and code (checked by hash), `ps` shows
ipatool's own argv, and the leak scan finds a planted copy.

Crash reports: Python prints no local values in a traceback, and every
`except` on this path either re-raises a `GraftError` whose message is
redacted (`appstore_login.sign_in`) or reports a fixed sentence. The app side
(pasteboard, undo, Settings) is in GraftTests (SecretFieldTests,
AppStoreAccountStoreTests).
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from tests.appstore_fakes import FAKE_EMAIL, FakeTools
from tests.config_fixtures import write_test_config
from tests.conftest import NO_USBMUXD, REPO_ROOT

PASSWORD = "Gate-B2-Passw0rd-q9Z"
CODE = "731846"


@pytest.fixture
def world(tmp_path: Path) -> tuple[dict[str, str], Path, FakeTools]:
    home = tmp_path / "home"
    tmp = tmp_path / "tmpdir"
    tmp.mkdir()
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        # Its own, so whatever the CLI or ipatool puts in a temp file is
        # where the scan looks (the review's #27).
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "GRAFT_NO_KEYCHAIN": "1",
        "USBMUXD_SOCKET_ADDRESS": NO_USBMUXD,
        "LANG": "en_US.UTF-8",
    }
    # The suite's network guard for child processes, kept.
    for key in ("PYTHONPATH",):
        if key in os.environ:
            env[key] = os.environ[key]
    config = home / ".config" / "graft" / "config.yaml"
    config.parent.mkdir(parents=True)
    write_test_config(config, team_id="TESTTEAM01")
    # Outside HOME, so its records (which hold argv and env by design) don't
    # count as "a file Graft wrote".
    fakes = FakeTools(tmp_path / "tools")
    raw = yaml.safe_load(config.read_text())
    raw["appstore"] = fakes.appstore_section()
    config.write_text(yaml.safe_dump(raw, sort_keys=False))
    return env, home, fakes


def _login(env: dict[str, str], *, stop_at_code: bool = False) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-m", "graft_ipa", "--json", "--appstore-login"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, cwd=REPO_ROOT,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    stdout, events, ps_args, ps_env = [], [], "", ""
    try:
        proc.stdin.write(FAKE_EMAIL.encode() + b"\n" + PASSWORD.encode() + b"\n")
        proc.stdin.flush()
        for raw in proc.stdout:
            stdout.append(raw.decode())
            event = json.loads(raw)
            events.append(event)
            if event.get("state") == "needs-code":
                ps_args = subprocess.run(
                    ["ps", "-axww", "-o", "command="], capture_output=True, text=True
                ).stdout
                ps_env = subprocess.run(
                    ["ps", "-E", "-ww", "-U", str(os.getuid()), "-o", "command="],
                    capture_output=True, text=True,
                ).stdout
                if stop_at_code:
                    proc.send_signal(signal.SIGTERM)
                else:
                    proc.stdin.write(CODE.encode() + b"\n")
                    proc.stdin.flush()
    finally:
        proc.stdin.close()
    return {
        "events": events, "stdout": "".join(stdout), "stderr": proc.stderr.read().decode(),
        "code": proc.wait(timeout=60), "ps_args": ps_args, "ps_env": ps_env,
    }


SECRETS = (("password", PASSWORD), ("code", CODE))
EVERYTHING = (*SECRETS, ("apple id", FAKE_EMAIL))


def _leaks(blob: bytes, of=SECRETS) -> list[str]:
    return [name for name, s in of if s.encode() in blob]


def _gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


def test_a_sign_in_with_2fa_works_end_to_end(world) -> None:
    env, _, fakes = world
    run = _login(env)
    assert run["code"] == 0, run["stderr"]
    assert run["events"][-1] == {"type": "appstore-login", "state": "signed-in", "email": FAKE_EMAIL}
    _, (call,) = fakes.login_runs()
    assert call["email_sha256"] == hashlib.sha256(FAKE_EMAIL.encode()).hexdigest()
    assert call["password_sha256"] == hashlib.sha256(PASSWORD.encode()).hexdigest()
    assert call["code_sha256"] == hashlib.sha256(CODE.encode()).hexdigest()


def test_neither_secret_is_in_the_event_stream_or_stderr(world) -> None:
    run = _login(world[0])
    assert _leaks(run["stdout"].encode()) == []
    assert _leaks(run["stderr"].encode(), EVERYTHING) == []


def _files(*roots: Path) -> list[Path]:
    return [p for root in roots for p in root.rglob("*") if p.is_file() and not p.is_symlink()]


def test_nothing_of_the_account_is_in_any_file_it_could_write(world) -> None:
    """HOME, the TMPDIR it was given, and beside the fake ipatool (whose
    records hold hashes of what it was typed, never the values)."""
    env, home, fakes = world
    _login(env)
    tmp = Path(env["TMPDIR"])
    # The fakes' own scripts name the fake Apple ID: they are what answers
    # for it, not something written.
    own = {fakes.ipatool, fakes.ipatool.with_suffix(".py"), fakes.unfairplay,
           fakes.unfairplay.with_suffix(".py")}
    files = [p for p in _files(home, tmp, fakes.root) if p not in own]
    assert "appstore-signin.json" in {p.name for p in files}, "premise: the CLI wrote under HOME"
    assert fakes.calls_file in files, "premise: the folder beside ipatool is scanned"
    assert tmp.is_dir(), "premise: the TMPDIR it was given exists"
    leaked = {str(p): _leaks(p.read_bytes(), EVERYTHING) for p in files}
    assert {path: names for path, names in leaked.items() if names} == {}


def test_the_tmpdir_scan_finds_a_temp_file_the_cli_would_write(world) -> None:
    """Positive control for the TMPDIR part: the child's `tempfile` lands
    where the scan looks."""
    env, _, _ = world
    proc = subprocess.run(
        [sys.executable, "-c", "import tempfile; f = tempfile.NamedTemporaryFile(delete=False); "
         f"f.write({PASSWORD.encode()!r}); f.close()"],
        env=env, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert [_leaks(p.read_bytes()) for p in _files(Path(env["TMPDIR"]))] == [["password"]]


def test_nothing_of_the_account_is_on_ipatools_argv_or_in_its_environment(world) -> None:
    env, _, fakes = world
    _login(env)
    _, (call,) = fakes.login_runs()
    assert _leaks(json.dumps(call["argv"]).encode(), EVERYTHING) == []
    assert _leaks(json.dumps(call["env"]).encode(), EVERYTHING) == []


def test_nothing_of_the_account_is_in_any_process_arguments_or_environment(world) -> None:
    _, _, fakes = world
    run = _login(world[0])
    assert run["ps_args"] and run["ps_env"], "premise: ps ran while both processes were alive"
    assert f"{fakes.ipatool}.py auth login --format json" in run["ps_args"], (
        "positive control: ps shows ipatool's own argv"
    )
    assert "graft_ipa --json --appstore-login" in run["ps_args"], "positive control: and Graft's"
    assert _leaks(run["ps_args"].encode(), EVERYTHING) == []
    assert _leaks(run["ps_env"].encode(), EVERYTHING) == []


def test_the_leak_scan_finds_a_planted_copy(tmp_path: Path) -> None:
    planted = tmp_path / "log.txt"
    planted.write_text(f"oops {PASSWORD} and {CODE} for {FAKE_EMAIL}")
    assert _leaks(planted.read_bytes(), EVERYTHING) == ["password", "code", "apple id"]


def test_a_stop_at_the_code_prompt_stops_ipatool_and_says_so(world) -> None:
    env, _, fakes = world
    run = _login(env, stop_at_code=True)
    assert run["code"] == 143
    assert run["events"][-1]["state"] == "failed"
    assert run["events"][-1]["reason"] == "stopped"
    (started,), finished = fakes.login_runs()
    assert finished == []
    assert _gone(started["pid"])


def test_a_refused_password_or_code_fails_cleanly(world) -> None:
    env, home, fakes = world
    fakes.set(login_code_error="apple did not complete verification; try a fresh 2FA code")
    run = _login(env)
    assert run["code"] == 20
    assert run["events"][-1]["reason"] == "not-accepted"
    assert not list(home.rglob("appstore-signin.json"))


def test_a_wrong_password_fails_cleanly(world) -> None:
    env, home, fakes = world
    fakes.set(login_error="Your Apple ID or password was entered incorrectly.", login_two_factor=False)
    run = _login(env)
    assert run["code"] == 20
    assert run["events"][-1]["reason"] == "wrong-password"
    assert not list(home.rglob("appstore-signin.json"))
