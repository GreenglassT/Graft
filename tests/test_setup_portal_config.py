"""`--setup-portal` works on the config `--config` names (F5).

It read `default_config_path()` whatever `--config` said: with a free config
named by `--config` and none at the default it said "config not found", and
with a paid default it ran the wizard for that one and wrote the App Store
Connect key into a file the user had not named.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import setup_portal
from graft_ipa.config import add_signing_section


def _config(path: Path, *, paid: bool, tmp_path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"apps": {}}))
    if paid:
        add_signing_section(path, cert=tmp_path / "c.p12", profile=tmp_path / "p.mp",
                            team_id="ABCDE12345")
    return path


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    asked: list[str] = []

    def ask(prompt, *a, **k):
        asked.append(prompt)
        raise KeyboardInterrupt

    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(ask))
    return asked


def test_a_free_config_named_by_config_is_refused_as_free(tmp_path: Path, prompts) -> None:
    elsewhere = _config(tmp_path / "elsewhere" / "config.yaml", paid=False, tmp_path=tmp_path)

    result = CliRunner().invoke(cli_mod.app, ["--config", str(elsewhere), "--setup-portal"])

    assert result.exit_code == 2, result.output
    assert "config not found" not in result.output
    assert "needs a paid Apple Developer Program membership" in result.output
    assert prompts == []


def test_a_paid_default_does_not_stand_in_for_a_free_config(tmp_path: Path, prompts) -> None:
    _config(tmp_path / "xdg-config" / "graft" / "config.yaml", paid=True, tmp_path=tmp_path)
    elsewhere = _config(tmp_path / "elsewhere" / "config.yaml", paid=False, tmp_path=tmp_path)

    CliRunner().invoke(cli_mod.app, ["--config", str(elsewhere), "--setup-portal"])

    assert prompts == [], "the wizard ran for the default config, not the one named"


def test_a_paid_config_named_by_config_gets_the_wizard(tmp_path: Path, prompts) -> None:
    elsewhere = _config(tmp_path / "elsewhere" / "config.yaml", paid=True, tmp_path=tmp_path)

    CliRunner().invoke(cli_mod.app, ["--config", str(elsewhere), "--setup-portal"])

    assert prompts == ["Key ID"]


def test_the_key_lands_beside_the_named_config(tmp_path: Path, monkeypatch) -> None:
    elsewhere = _config(tmp_path / "elsewhere" / "config.yaml", paid=True, tmp_path=tmp_path)
    p8 = tmp_path / "AuthKey_KEY1234567.p8"
    p8.write_text("key")
    p8.chmod(0o644)
    answers = iter(["KEY1234567", "00000000-0000-0000-0000-000000000000", str(p8)])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(setup_portal.Confirm, "ask", staticmethod(lambda *a, **k: True))

    class Client:
        def __init__(self, creds):
            pass

        def list_devices(self):
            return []

    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", Client)

    result = CliRunner().invoke(cli_mod.app, ["--config", str(elsewhere), "--setup-portal"])

    assert result.exit_code == 0, result.output
    asc = yaml.safe_load(elsewhere.read_text())["app_store_connect"]
    assert asc["key_id"] == "KEY1234567"
    dest = elsewhere.parent / "AuthKey_KEY1234567.p8"
    assert asc["key_file"] == str(dest)
    # The wizard copies through the same owner-only path `--set-portal` does.
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert not (tmp_path / "xdg-config" / "graft" / "config.yaml").exists()


def test_the_wizard_refuses_a_key_id_that_names_a_path(tmp_path: Path, monkeypatch) -> None:
    elsewhere = _config(tmp_path / "elsewhere" / "config.yaml", paid=True, tmp_path=tmp_path)
    answers = iter(["../../x", "00000000-0000-0000-0000-000000000000"])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    before = elsewhere.read_text()

    result = CliRunner().invoke(cli_mod.app, ["--config", str(elsewhere), "--setup-portal"])

    assert result.exit_code == 2, result.output
    assert "10-character id" in " ".join(result.output.split())
    assert elsewhere.read_text() == before


# --- the wizard checks the key before it installs it ---------------------------
#
# It used to copy the chosen .p8 over `AuthKey_<id>.p8` first and ask App Store
# Connect after, outside `_check_key`: a file that is not an EC private key
# escaped as a raw ValueError with the bad bytes already installed, and a key
# App Store Connect refused printed "auth check failed" and exited 0. It now
# takes `--set-portal`'s path: read once, check those bytes, install those.

KEY_ID = "KEY1234567"
ISSUER = "00000000-0000-0000-0000-000000000000"


def _wizard(tmp_path: Path, monkeypatch, *, source_bytes: bytes, client) -> tuple:
    config = _config(tmp_path / "elsewhere" / "config.yaml", paid=True, tmp_path=tmp_path)
    source = tmp_path / "Downloads" / f"AuthKey_{KEY_ID}.p8"
    source.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    answers = iter([KEY_ID, ISSUER, str(source)])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(setup_portal.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", client)
    return config, config.parent / f"AuthKey_{KEY_ID}.p8"


def _failing_client(error: Exception):
    def client(creds):
        class Client:
            def list_devices(self, **_):
                raise error

        return Client()

    return client


def test_a_key_that_cannot_sign_is_reported_and_not_installed(tmp_path: Path, monkeypatch) -> None:
    """Not an EC private key: the JWT library's error, as a PortalError with
    a hint, and the key already in place is still the one there."""
    from graft_ipa import apple_portal

    config, dest = _wizard(
        tmp_path, monkeypatch, source_bytes=b"not a key",
        client=_failing_client(ValueError("Could not deserialize key data")),
    )
    dest.write_bytes(b"the working key")
    before = config.read_text()

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == apple_portal.PortalError.exit_code, result.output
    said = " ".join(result.output.split())
    assert "couldn't sign in to App Store Connect with" in said, said
    assert "Could not deserialize key data" in said, said
    assert "isn't a failure graft knows how to explain" not in said, said
    assert not isinstance(result.exception, ValueError)
    assert dest.read_bytes() == b"the working key", "the bad key was installed over the good one"
    assert config.read_text() == before


def test_a_key_app_store_connect_refuses_fails_the_wizard(tmp_path: Path, monkeypatch) -> None:
    from graft_ipa import apple_portal

    config, dest = _wizard(
        tmp_path, monkeypatch, source_bytes=b"-----BEGIN PRIVATE KEY-----\n",
        client=_failing_client(apple_portal.PortalError(
            "ASC API 401 on GET /devices: NOT_AUTHORIZED", hint="check the key id and issuer id",
        )),
    )
    before = config.read_text()

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == apple_portal.PortalError.exit_code, result.output
    said = " ".join(result.output.split())
    assert "NOT_AUTHORIZED" in said and "check the key id and issuer id" in said, said
    assert config.read_text() == before
    assert not dest.exists(), "a key App Store Connect refused was installed"


# --- a relative --config still records an absolute key_file ---------------------


def _relative_config(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    """A paid config at `<cwd>/rel/config.yaml`, named relatively."""
    cwd = tmp_path / "cwd"
    _config(cwd / "rel" / "config.yaml", paid=True, tmp_path=tmp_path)
    monkeypatch.chdir(cwd)
    return Path("rel") / "config.yaml", cwd / "rel" / f"AuthKey_{KEY_ID}.p8"


def _no_devices(creds):
    class Client:
        def list_devices(self, **_):
            return []

    return Client()


def test_set_portal_with_a_relative_config_records_an_absolute_key_file(
    tmp_path: Path, monkeypatch
) -> None:
    """Stored relative, the key path meant something else from any other
    directory: a build run from elsewhere, or the app, found no key."""
    import json

    config, dest = _relative_config(tmp_path, monkeypatch)
    source = tmp_path / f"AuthKey_{KEY_ID}.p8"
    source.write_bytes(b"key")
    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", _no_devices)

    result = CliRunner().invoke(cli_mod.app, [
        "--config", str(config), "--json", "--set-portal", "--asc-key-id", KEY_ID,
        "--asc-issuer-id", ISSUER, "--asc-key-file", str(source),
    ])

    assert result.exit_code == 0, result.output
    recorded = yaml.safe_load(config.read_text())["app_store_connect"]["key_file"]
    assert Path(recorded).is_absolute(), recorded
    assert Path(recorded) == dest and dest.read_bytes() == b"key"
    (event,) = [json.loads(line) for line in result.stdout.splitlines()
                if line.startswith('{"type":"portal-set"')]
    assert event["key_file"] == recorded


def test_the_wizard_with_a_relative_config_records_an_absolute_key_file(
    tmp_path: Path, monkeypatch
) -> None:
    config, dest = _relative_config(tmp_path, monkeypatch)
    source = tmp_path / f"AuthKey_{KEY_ID}.p8"
    source.write_bytes(b"key")
    answers = iter([KEY_ID, ISSUER, str(source)])
    monkeypatch.setattr(setup_portal.Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
    monkeypatch.setattr(setup_portal.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(setup_portal.apple_portal, "ASCClient", _no_devices)

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == 0, result.output
    recorded = yaml.safe_load(config.read_text())["app_store_connect"]["key_file"]
    assert Path(recorded).is_absolute(), recorded
    assert Path(recorded) == dest and dest.read_bytes() == b"key"


# --- the wizard's halves of #12 and #23 (re-check #29, #31) ---------------------


def _working_client(creds):
    from graft_ipa.apple_portal import Device

    class Client:
        def list_devices(self, **_):
            return [Device(id="d1", udid="00008130-001A2B3C0123456D", name="iPhone",
                           platform="IOS", status="ENABLED", device_class="IPHONE")]

    return Client()


def test_the_wizard_explains_a_key_in_a_folder_it_cannot_search(tmp_path: Path, monkeypatch) -> None:
    """Re-check #29: `set_portal`'s call site was tested, the wizard's was
    not. Python 3.11-3.13's `is_file` raises PermissionError there; forced
    here so the check holds on any interpreter."""
    config, _dest = _wizard(tmp_path, monkeypatch, source_bytes=b"key", client=_working_client)
    source = tmp_path / "Downloads" / f"AuthKey_{KEY_ID}.p8"
    real_is_file = Path.is_file

    def is_file(self):
        if self == source:
            raise PermissionError(13, "Permission denied", str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", is_file)
    before = config.read_text()

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == 2, result.output
    said = " ".join(result.output.split())
    assert "can't look at" in said, said
    assert "isn't a failure graft knows how to explain" not in said, said
    assert config.read_text() == before


def test_a_wizard_whose_config_write_fails_takes_its_key_copy_back_out(
    tmp_path: Path, monkeypatch
) -> None:
    """Re-check #31: the config lock held past its wait. --set-portal took its
    fresh key copy back out (#23a); the wizard's same fix had no test."""
    from graft_ipa.errors import ConfigError

    config, dest = _wizard(tmp_path, monkeypatch, source_bytes=b"key", client=_working_client)

    def locked(*a, **k):
        raise ConfigError("another graft process is editing the config")

    monkeypatch.setattr(setup_portal, "set_app_store_connect", locked)
    before = config.read_text()

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == 2, result.output
    assert "another graft process is editing the config" in " ".join(result.output.split())
    assert not dest.exists(), "the key copy stayed, with config.yaml naming nothing"
    assert config.read_text() == before


def test_a_wizard_write_that_fails_keeps_a_copy_that_was_already_there(
    tmp_path: Path, monkeypatch
) -> None:
    from graft_ipa.errors import ConfigError

    config, dest = _wizard(tmp_path, monkeypatch, source_bytes=b"key", client=_working_client)
    dest.write_bytes(b"key")

    monkeypatch.setattr(
        setup_portal, "set_app_store_connect",
        lambda *a, **k: (_ for _ in ()).throw(ConfigError("another graft process is editing the config")),
    )

    CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert dest.read_bytes() == b"key"


def test_the_wizard_names_an_earlier_keys_copy_left_beside_the_config(tmp_path: Path, monkeypatch) -> None:
    """Combined fix check #6: rotating keys with the wizard left
    AuthKey_<old>.p8, a revocable private key no setting uses, beside
    config.yaml, and the wizard said nothing; --set-portal names it."""
    config, dest = _wizard(tmp_path, monkeypatch, source_bytes=b"key", client=_working_client)
    old = config.parent / "AuthKey_OLDOLDOLD1.p8"
    old.write_bytes(b"old key")

    result = CliRunner().invoke(cli_mod.app, ["--config", str(config), "--setup-portal"])

    assert result.exit_code == 0, result.output
    said = "".join(result.output.split())  # the console wraps long paths anywhere
    assert f"AuthKey_OLDOLDOLD1.p8isstillin{config.parent}:aprivatekeynosettinguses" in said, said
    assert f"{dest.name}isstillin" not in said, "the key in use is not named"
    assert old.exists(), "named, never deleted"
