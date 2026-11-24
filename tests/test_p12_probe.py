"""The backstop under `p12.check_kdf_cost`: a chosen .p12 is first opened in
a child process with a wall-clock limit (`p12.probe`), because a key bag
inside an encrypted SafeContents hides its key-derivation count until it is
decrypted, and nothing in-process can stop OpenSSL spending it."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from graft_ipa import account_setup
from graft_ipa import p12 as p12_mod
from graft_ipa.errors import ConfigError

from .signing_fixtures import PASSWORD, make_identity, nested_key_bag_p12


def test_a_key_bag_hiding_its_cost_inside_the_encrypted_part_is_refused_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """60M rounds behind a 2048-round outer layer: the DER read passes it,
    an in-process open would spend seconds on it (minutes at 2^31), and
    the child is stopped at the limit (1 s here, 10 s for real)."""
    ident = make_identity(tmp_path)
    hostile = nested_key_bag_p12(PASSWORD, 60_000_000)
    ident.p12.write_bytes(hostile)
    assert p12_mod.kdf_iterations(hostile) == 2048, "the pre-check can't see the inner count"
    monkeypatch.setattr(p12_mod, "PROBE_SECONDS", 1.0)
    loaded = []
    monkeypatch.setattr(p12_mod, "load_key_and_certificates", lambda *a: loaded.append(a))
    started = time.monotonic()

    with pytest.raises(ConfigError, match=(
        r"Certificates\.p12: this \.p12 asks for an unreasonable amount of work to open; "
        r"export it again"
    )):
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)

    assert time.monotonic() - started < 8, "a 1 s limit and a child's start, not the 60M rounds"
    assert loaded == [], "fell back to opening it in-process"


def test_the_timeout_never_leaves_the_child_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "child.pid"
    sleeper = (
        "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
        "sys.stdin.buffer.read(); time.sleep(60)"
    )
    monkeypatch.setattr(p12_mod, "_probe_argv",
                        lambda: [sys.executable, "-I", "-c", sleeper, str(pid_file)])

    with pytest.raises(p12_mod.SlowP12Error):
        p12_mod.probe(b"data", b"pw", seconds=0.5)

    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # killed and reaped: not even a zombie is left


def test_the_password_goes_to_the_child_on_stdin_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = make_identity(tmp_path)
    seen: list[dict] = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        seen.append({"argv": list(argv), "env": dict(kwargs.get("env") or {}),
                     "input": kwargs.get("input")})
        return real_run(argv, **kwargs)

    monkeypatch.setattr(p12_mod.subprocess, "run", run)
    monkeypatch.setenv("GRAFT_P12_PASS", "not the password")

    account_setup.read_identity(ident.p12, ident.profile, PASSWORD)

    (call,) = seen
    assert call["argv"][-2:] == ["-m", "graft_ipa.p12_probe"] and "-I" in call["argv"]
    assert not any(PASSWORD in arg for arg in call["argv"])
    assert not any(PASSWORD in value for value in call["env"].values())
    assert PASSWORD.encode() in call["input"]


@pytest.mark.parametrize(("password", "answer"), [(PASSWORD, "ok"), ("not it", "wrong")])
def test_the_child_answers_in_one_word(tmp_path: Path, password: str, answer: str) -> None:
    data = make_identity(tmp_path).p12.read_bytes()
    assert p12_mod.probe(data, password.encode()) == answer
    assert p12_mod.probe(b"not a p12", b"x") == "wrong"


def test_a_child_that_cannot_run_is_a_refusal_not_an_in_process_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ident = make_identity(tmp_path)
    monkeypatch.setattr(p12_mod, "_probe_argv", lambda: [str(tmp_path / "no-such-python")])
    loaded = []
    monkeypatch.setattr(p12_mod, "load_key_and_certificates", lambda *a: loaded.append(a))

    with pytest.raises(ConfigError, match=r"couldn't check Certificates\.p12"):
        account_setup.read_identity(ident.p12, ident.profile, PASSWORD)
    assert loaded == []
