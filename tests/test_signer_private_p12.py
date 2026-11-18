"""zsign never sees the user's real .p12 password, and `--init` keeps the
signing files private.

`zsign -p <password>` put the .p12 password on a command line, where `ps`
shows it to every user on the Mac for as long as signing runs. zsign is now
given a 0600 copy of the identity, re-encrypted in-process under a one-time
password, inside a 0700 temporary directory that is removed on every way out
of the run — success, failure, a missing zsign, an interrupt, a GUI Stop.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
from rich.console import Console

from graft_ipa import init_flow, signer
from graft_ipa.errors import SigningError

from .test_lc_sign import _make_p12
from .test_signer import make_cfg

REAL = "the-users-real-p12-password"


def _cfg(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    p12, _ = _make_p12(tmp_path, REAL)
    cfg.signing.cert = p12
    return cfg


class Zsign:
    """Stands in for `subprocess.run`, looking at the identity zsign is handed
    while zsign would still be running."""

    def __init__(self, *, returncode: int = 0, raises: BaseException | None = None,
                 during=None) -> None:
        self.returncode = returncode
        self.raises = raises
        self.during = during
        self.cmds: list[list[str]] = []
        self.identity: Path | None = None
        self.seen: dict = {}

    def __call__(self, cmd, **kwargs):
        self.cmds.append(list(cmd))
        if "-k" in cmd:
            self.identity = Path(cmd[cmd.index("-k") + 1])
            self.seen = {
                "exists": self.identity.is_file(),
                "mode": stat.S_IMODE(self.identity.stat().st_mode),
                "dir_mode": stat.S_IMODE(self.identity.parent.stat().st_mode),
                "bytes": self.identity.read_bytes(),
                "password": cmd[cmd.index("-p") + 1],
            }
        if self.during is not None:
            self.during()
        if self.raises is not None:
            raise self.raises
        if self.returncode == 0:
            Path(cmd[cmd.index("-o") + 1]).write_bytes(b"signed")
        return subprocess.CompletedProcess(cmd, self.returncode, stdout="", stderr="boom")


def _sign(cfg, tmp_path: Path, zsign: Zsign, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TEST_P12_PASS", REAL)
    with patch.object(signer.subprocess, "run", side_effect=zsign):
        return signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")


def test_the_real_password_never_reaches_zsigns_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    zsign = Zsign()
    _sign(cfg, tmp_path, zsign, monkeypatch)

    assert zsign.cmds, "zsign never ran"
    for cmd in zsign.cmds:
        assert not [arg for arg in cmd if REAL in arg], cmd
    # Not the user's file, and not their password: a copy under a one-time one.
    assert zsign.identity != cfg.signing.cert
    assert zsign.seen["password"] != REAL


def test_zsign_gets_a_private_copy_that_holds_the_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import pkcs12

    cfg = _cfg(tmp_path)
    zsign = Zsign()
    _sign(cfg, tmp_path, zsign, monkeypatch)

    assert zsign.seen["exists"]
    assert zsign.seen["mode"] == 0o600
    assert zsign.seen["dir_mode"] == 0o700
    key, cert, _ = pkcs12.load_key_and_certificates(
        zsign.seen["bytes"], zsign.seen["password"].encode()
    )
    _, original, _ = pkcs12.load_key_and_certificates(
        cfg.signing.cert.read_bytes(), REAL.encode()
    )
    assert key is not None
    assert cert.fingerprint(hashes.SHA256()) == original.fingerprint(hashes.SHA256())


@pytest.mark.parametrize(
    "zsign",
    [
        pytest.param(lambda: Zsign(), id="success"),
        pytest.param(lambda: Zsign(returncode=1), id="zsign-failed"),
        pytest.param(lambda: Zsign(raises=FileNotFoundError("zsign")), id="no-zsign"),
        pytest.param(lambda: Zsign(raises=KeyboardInterrupt()), id="interrupted"),
    ],
)
def test_the_copy_is_gone_however_the_run_ends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, zsign
) -> None:
    cfg = _cfg(tmp_path)
    fake = zsign()
    try:
        _sign(cfg, tmp_path, fake, monkeypatch)
    except (SigningError, KeyboardInterrupt):
        pass
    assert fake.identity is not None, "zsign never ran"
    assert not fake.identity.exists()
    assert not fake.identity.parent.exists()


_STOPPED_CHILD = """
import os, signal, sys, time
from pathlib import Path
from graft_ipa import signer
from graft_ipa.config import Config, SigningConfig

cert, profile, out = map(Path, sys.argv[1:4])
cfg = Config(
    path=out / "config.yaml",
    signing=SigningConfig(cert=cert, cert_password_env="TEST_P12_PASS",
                          profile=profile, team_id="ABC1234567"),
    cache_dir=out, built_dir=out, tweaks_dir=out, state_dir=out, apps={},
)

def zsign(cmd, **kwargs):
    print(cmd[cmd.index("-k") + 1], flush=True)
    os.kill(os.getpid(), signal.SIGTERM)  # the GUI's Stop, mid-signing
    time.sleep(10)

signer.subprocess.run = zsign
signer.run_zsign(cfg, out / "in.ipa", out / "out.ipa")
"""


def test_a_gui_stop_while_zsign_runs_still_removes_the_copy(tmp_path: Path) -> None:
    """SIGTERM's default disposition kills the process where it stands and
    skips every `finally`. A real child interpreter, because the signal is
    real: sent to the test process, it would take the test run with it."""
    import sys

    p12, _ = _make_p12(tmp_path, REAL)
    profile = tmp_path / "profile.mobileprovision"
    profile.write_bytes(b"x")
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    script = tmp_path / "child.py"
    script.write_text(_STOPPED_CHILD)
    env = {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(scratch),
        "TEST_P12_PASS": REAL, "GRAFT_NO_KEYCHAIN": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(script), str(p12), str(profile), str(tmp_path)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    identity = Path(proc.stdout.strip().splitlines()[0])
    assert identity.is_relative_to(scratch)
    # Exited through SystemExit(128 + SIGTERM), not killed by the signal.
    assert proc.returncode == 128 + signal.SIGTERM, proc.stderr
    assert not identity.parent.exists()


def test_a_wrong_password_is_said_before_zsign_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", "not-it")
    with patch.object(signer.subprocess, "run",
                      side_effect=AssertionError("zsign ran")), \
         pytest.raises(SigningError) as exc:
        signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa")
    assert "password" in str(exc.value).lower()
    assert "$TEST_P12_PASS" in (exc.value.hint or "")
    assert "--set-p12-password" in (exc.value.hint or "")


def test_a_dry_run_shows_no_password_and_makes_no_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path)
    monkeypatch.setenv("TEST_P12_PASS", REAL)
    made: list = []
    real_mkdtemp = signer.tempfile.mkdtemp
    monkeypatch.setattr(signer.tempfile, "mkdtemp",
                        lambda *a, **k: made.append(1) or real_mkdtemp(*a, **k))
    res = signer.run_zsign(cfg, tmp_path / "in.ipa", tmp_path / "out.ipa", dry_run=True)
    assert REAL not in res.stdout
    assert str(cfg.signing.cert) in res.stdout
    assert made == []


# ---- --init ----


def _answer_init(monkeypatch: pytest.MonkeyPatch, answers: dict[str, str]) -> None:
    def prompt(question, *a, default=None, **k):
        for key, value in answers.items():
            if key in question:
                return value
        return default

    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(prompt))
    monkeypatch.setattr(init_flow, "list_connected_devices", lambda: [])


def test_init_copies_the_signing_files_owner_only_into_a_private_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    p12 = downloads / "AppleDevelopment.p12"
    p12.write_bytes(b"p12 bytes")
    profile = downloads / "wildcard.mobileprovision"
    profile.write_bytes(b"profile bytes")
    # What a browser download or a Finder copy leaves: readable by everyone.
    p12.chmod(0o644)
    profile.chmod(0o644)
    _answer_init(monkeypatch, {".p12": str(p12), ".mobileprovision": str(profile)})
    monkeypatch.setattr(init_flow, "parse_mobileprovision", lambda p: {})

    cfg_path = init_flow.run_init(Console(file=StringIO(), width=200))

    cfg_dir = cfg_path.parent
    assert stat.S_IMODE(cfg_dir.stat().st_mode) == 0o700
    for name, original in (("AppleDevelopment.p12", p12), ("wildcard.mobileprovision", profile)):
        copy = cfg_dir / name
        assert copy.read_bytes() == original.read_bytes()
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600, name


def test_init_replacing_an_existing_copy_keeps_it_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg_dir = tmp_path / "cfg" / "graft"
    cfg_dir.mkdir(parents=True)
    old = cfg_dir / "AppleDevelopment.p12"
    old.write_bytes(b"old")
    old.chmod(0o644)
    new = tmp_path / "new.p12"
    new.write_bytes(b"new")
    _answer_init(monkeypatch, {".p12": str(new), ".mobileprovision": str(tmp_path / "none")})

    init_flow.run_init(Console(file=StringIO(), width=200))

    assert old.read_bytes() == b"new"
    assert stat.S_IMODE(old.stat().st_mode) == 0o600
