"""Reading the App Store Connect `.p8`: a regular file, capped, and read once.

Three gaps, all in how the key file is opened rather than what is in it:

  * the `--setup-portal` wizard gated on `exists()`, which is true for a FIFO,
    and then `read_bytes()` blocked forever on one;
  * nothing capped the read, so any file named as the key went into memory
    whole — the key is a few hundred bytes, and 64 KiB is generous;
  * `--set-portal` checked the key against App Store Connect from the chosen
    path and then read that path *again* to install it, so the key installed
    need not be the key that was checked.

App Store Connect is a fake here, as in `test_cli_set_portal.py`; nothing
touches the network.
"""

from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import apple_portal, setup_portal
from graft_ipa import cli as cli_mod
from graft_ipa.apple_portal import MAX_KEY_BYTES, PortalCredentials, PortalError
from graft_ipa.config import add_signing_section

KEY_ID = "KEY1234567"
ISSUER = "00000000-0000-0000-0000-000000000000"
PEM = b"-----BEGIN PRIVATE KEY-----\nnot a real key\n-----END PRIVATE KEY-----\n"


@contextlib.contextmanager
def _must_not_block(seconds: int = 5):
    """Fail the test, rather than hang the suite, if the body blocks."""

    def _hung(signum, frame):
        raise TimeoutError("blocked reading the key file")

    previous = signal.signal(signal.SIGALRM, _hung)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _paid_config(path: Path, tmp_path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"apps": {}}))
    add_signing_section(path, cert=tmp_path / "c.p12", profile=tmp_path / "p.mp",
                        team_id="ABCDE12345")
    return path


def _never_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(creds):
        raise AssertionError("App Store Connect must not be asked")

    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", refuse)


def _set_portal(key_file: Path):
    return CliRunner().invoke(cli_mod.app, [
        "--json", "--set-portal", "--asc-key-id", KEY_ID, "--asc-issuer-id", ISSUER,
        "--asc-key-file", str(key_file),
    ])


# --- (a) a FIFO is not a key file -----------------------------------------------


def test_the_wizard_refuses_a_fifo_instead_of_blocking_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _paid_config(tmp_path / "cfg" / "config.yaml", tmp_path)
    fifo = tmp_path / f"AuthKey_{KEY_ID}.p8"
    os.mkfifo(fifo)
    answers = iter([KEY_ID, ISSUER, str(fifo)])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(setup_portal.Confirm, "ask", staticmethod(lambda *a, **k: True))
    _never_asked(monkeypatch)
    before = config.read_text()

    with _must_not_block():
        result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert not isinstance(result.exception, TimeoutError), "the wizard blocked on a FIFO"
    assert result.exit_code != 0, result.output
    assert ".p8 file not found" in " ".join(result.output.split()), result.output
    assert config.read_text() == before
    assert not (config.parent / f"AuthKey_{KEY_ID}.p8").exists()


def test_the_reader_refuses_a_fifo_on_the_descriptor_it_reads(tmp_path: Path) -> None:
    """The gate a caller runs first can be raced; the reader's own check, on
    the descriptor it then reads from, cannot."""
    fifo = tmp_path / "key.p8"
    os.mkfifo(fifo)

    with _must_not_block(), pytest.raises(PortalError, match="is not a file"):
        apple_portal.read_key_file(fifo)


def test_set_portal_refuses_a_fifo_without_blocking(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fifo = tmp_path / "key.p8"
    os.mkfifo(fifo)
    _never_asked(monkeypatch)

    with _must_not_block():
        result = _set_portal(fifo)

    assert result.exit_code != 0, result.output
    assert "app_store_connect" not in yaml.safe_load(temp_config_env.read_text())


# --- (b) capped at 64 KiB ---------------------------------------------------------


def test_a_key_file_over_64_kib_is_refused_before_anything_is_asked(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    big = tmp_path / "AuthKey_big.p8"
    big.write_bytes(b"x" * (MAX_KEY_BYTES + 1))
    _never_asked(monkeypatch)

    result = _set_portal(big)

    assert result.exit_code != 0, result.output
    said = " ".join(result.output.split())
    assert "larger than 64 KiB" in said, said
    assert "isn't an App Store Connect key" in said, said
    assert "app_store_connect" not in yaml.safe_load(temp_config_env.read_text())
    assert not (temp_config_env.parent / f"AuthKey_{KEY_ID}.p8").exists()


def test_the_cap_holds_when_reads_come_back_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`read` may return less than asked. A file one byte over the cap must
    still be refused when it arrives in pieces that land exactly on 64 KiB,
    not returned truncated as a key."""
    big = tmp_path / "AuthKey_big.p8"
    big.write_bytes(b"x" * (MAX_KEY_BYTES + 1))
    real_read = os.read
    monkeypatch.setattr(apple_portal.os, "read", lambda fd, n: real_read(fd, min(n, 4096)))

    with pytest.raises(PortalError, match="larger than 64 KiB"):
        apple_portal.read_key_file(big)


def test_a_key_file_of_exactly_64_kib_is_read(tmp_path: Path) -> None:
    edge = tmp_path / "edge.p8"
    edge.write_bytes(b"x" * MAX_KEY_BYTES)
    assert apple_portal.read_key_file(edge) == b"x" * MAX_KEY_BYTES


def test_the_key_a_build_signs_with_is_capped_too(tmp_path: Path) -> None:
    """`PortalCredentials.load_key` is how every later use reads the
    configured key (auto-provisioning, `--lc-install`), not only the setup."""
    big = tmp_path / "AuthKey_big.p8"
    big.write_bytes(b"x" * (MAX_KEY_BYTES + 1))
    creds = PortalCredentials(key_id=KEY_ID, issuer_id=ISSUER, key_path=big)

    with pytest.raises(PortalError, match="larger than 64 KiB"):
        creds.load_key()


def test_a_missing_configured_key_still_says_so(tmp_path: Path) -> None:
    creds = PortalCredentials(key_id=KEY_ID, issuer_id=ISSUER, key_path=tmp_path / "gone.p8")

    with pytest.raises(PortalError, match="ASC API key not found"):
        creds.load_key()


# --- (c) the key installed is the key that was checked ---------------------------


def test_set_portal_installs_exactly_the_bytes_it_checked(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file is replaced while App Store Connect is being asked — after the
    check has its bytes, before the copy. What is installed must be what was
    checked, not whatever the path holds by then."""
    source = tmp_path / "Downloads" / f"AuthKey_{KEY_ID}.p8"
    source.parent.mkdir()
    source.write_bytes(PEM)
    checked: list[bytes] = []

    def client(creds):
        class Client:
            def list_devices(self, **_):
                assert creds.key_bytes is not None, "the check re-reads the key file"
                checked.append(creds.load_key())
                source.write_bytes(b"-----BEGIN PRIVATE KEY-----\nswapped\n-----END PRIVATE KEY-----\n")
                return []

        return Client()

    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", client)

    result = _set_portal(source)

    assert result.exit_code == 0, result.output
    assert checked == [PEM]
    assert (temp_config_env.parent / f"AuthKey_{KEY_ID}.p8").read_bytes() == PEM


def test_the_wizard_checks_the_bytes_it_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _paid_config(tmp_path / "cfg" / "config.yaml", tmp_path)
    source = tmp_path / f"AuthKey_{KEY_ID}.p8"
    source.write_bytes(PEM)
    answers = iter([KEY_ID, ISSUER, str(source)])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(setup_portal.Confirm, "ask", staticmethod(lambda *a, **k: True))
    seen: list[PortalCredentials] = []

    def client(creds):
        seen.append(creds)

        class Client:
            def list_devices(self, **_):
                return []

        return Client()

    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", client)

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == 0, result.output
    (creds,) = seen
    assert creds.key_bytes == PEM, "the check read the key file a second time"
    assert (config.parent / f"AuthKey_{KEY_ID}.p8").read_bytes() == PEM


# --- (d) the key never shows in a repr --------------------------------------------


def test_the_key_bytes_are_not_in_the_credentials_repr(tmp_path: Path) -> None:
    """A dataclass repr names every field: a traceback's locals, a log line or
    an f-string of the credentials printed the private key whole."""
    creds = PortalCredentials(
        key_id=KEY_ID, issuer_id=ISSUER, key_path=tmp_path / "k.p8", key_bytes=PEM
    )

    for shown in (repr(creds), str(creds), f"{creds}"):
        assert "PRIVATE KEY" not in shown, shown
        assert "not a real key" not in shown, shown
        assert KEY_ID in shown, "the rest of the repr is still there"
    assert creds.load_key() == PEM


# --- app-2: the .p8 the user picked, refused by macOS ---------------------------

import errno  # noqa: E402


@pytest.mark.parametrize("code", [errno.EPERM, errno.EACCES])
def test_a_key_macos_wont_let_graft_read_names_the_setting(tmp_path, monkeypatch, code) -> None:
    key = tmp_path / "Downloads" / "AuthKey_ABCDE12345.p8"
    key.parent.mkdir()
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\n")
    real = os.open

    def fake(path, *args, **kwargs):
        if os.path.realpath(path) == os.path.realpath(key):
            raise PermissionError(code, os.strerror(code), str(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fake)
    with pytest.raises(apple_portal.PortalError) as e:
        apple_portal.read_key_file(key)
    assert e.value.hint == (
        f"macOS didn't let Graft read {key}: allow Graft in System Settings › Privacy & Security › "
        "Files and Folders, or move the file out of Downloads/Desktop/Documents and choose it again"
    )
