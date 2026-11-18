"""`--setup-portal` works on the config `--config` names (F5).

It read `default_config_path()` whatever `--config` said: with a free config
named by `--config` and none at the default it said "config not found", and
with a paid default it ran the wizard for that one and wrote the App Store
Connect key into a file the user had not named.
"""

from __future__ import annotations

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
    p8 = tmp_path / "AuthKey_KEY123.p8"
    p8.write_text("key")
    answers = iter(["KEY123", "ISSUER", str(p8)])
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
    assert asc["key_id"] == "KEY123"
    assert asc["key_file"] == str(elsewhere.parent / "AuthKey_KEY123.p8")
    assert not (tmp_path / "xdg-config" / "graft" / "config.yaml").exists()
