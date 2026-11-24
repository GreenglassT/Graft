"""`--init`, answered yes on a free config, upgrades it and loses nothing.

Every "needs a paid membership" hint says to run `graft --init` and answer
yes. That used to offer to overwrite config.yaml with an empty starter (every
app, repo and App Store Connect key gone) or keep the free config unchanged.
Now it asks only the signing questions and adds a `signing:` section to the
config that is there. An app with no `delivery:` key, which is a LiveContainer
app on a free account and would become a sideload app on a paid one, is given
an explicit `delivery: livecontainer`, so no app changes how it is delivered.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from graft_ipa import init_flow
from graft_ipa.config import (
    TEAM_ID_PLACEHOLDER,
    add_app,
    add_repo,
    add_signing_section,
    load_config,
    set_app_store_connect,
    write_default_config,
)
from graft_ipa.errors import ConfigError

PAID_QUESTION = "Do you have a paid Apple Developer Program membership?"

APPS = {
    # No delivery key: a LiveContainer app while the config is free.
    "example": {"bundle_id": "com.example.app", "tweaks": ["some-tweak.deb"],
                "cyan": {"display_name": "Example+"}},
    "bravo": {"bundle_id": "com.example.bravo", "delivery": "livecontainer",
              "lc_bundle_id": "com.kdt.LiveContainer2.ABCDE12345"},
    # Carried over from a paid config once: it stays a sideload app.
    "side": {"bundle_id": "com.example.side", "delivery": "sideload"},
}
ASC = {"key_id": "KEY123", "issuer_id": "ISSUER-PLACEHOLDER", "key_file": "/keys/AuthKey.p8",
       "device_udids": ["UDID-PLACEHOLDER"]}


@pytest.fixture
def free_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A free config at the default path, built by the same writers `--init`,
    `--add-app`, `--add-repo` and `--setup-portal` use."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    monkeypatch.setattr(init_flow, "list_connected_devices",
                        lambda: pytest.fail("the upgrade asked for a device"))
    path = tmp_path / "cfg" / "graft" / "config.yaml"
    write_default_config(path, free_account=True, default_device_udid="UDID-PLACEHOLDER")
    for name, entry in APPS.items():
        add_app(path, name, dict(entry))
    add_repo(path, "https://repo.example.com", "Example repo")
    add_repo(path, "https://other.example.com", "Other repo")
    set_app_store_connect(path, dict(ASC))
    raw = yaml.safe_load(path.read_text())
    raw["archive_keep"] = 4
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    assert load_config(path).free_account
    return path


def _init_answering_yes(monkeypatch: pytest.MonkeyPatch, cfg_path: Path | None = None):
    """`--init` with yes to the account question and to every other
    confirmation, and each path prompt at its default. Returns the questions
    asked and what was printed."""
    confirms: list[str] = []
    prompts: list[str] = []

    def confirm(question, *a, **k):
        confirms.append(question)
        return True

    def prompt(question, *a, default=None, **k):
        prompts.append(question)
        return default

    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(confirm))
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(prompt))
    out = StringIO()
    init_flow.run_init(Console(file=out, width=300), cfg_path=cfg_path)
    return confirms, prompts, out.getvalue()


def test_init_upgrades_a_free_config_keeping_everything(
    free_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = yaml.safe_load(free_config.read_text())
    deliveries_before = {n: a.delivery for n, a in load_config(free_config).apps.items()}

    confirms, prompts, text = _init_answering_yes(monkeypatch)

    after = yaml.safe_load(free_config.read_text())
    cfg = load_config(free_config)
    # Paid now, with the section --init's paid path writes.
    assert cfg.free_account is False
    assert set(after["signing"]) == {"cert", "cert_password_env", "profile", "team_id"}
    assert after["signing"]["team_id"] == TEAM_ID_PLACEHOLDER  # no profile to read here
    # Every app, with every key it had, and delivered exactly as before.
    assert set(after["apps"]) == set(APPS)
    for name, entry in before["apps"].items():
        assert after["apps"][name].items() >= entry.items(), name
    assert {n: a.delivery for n, a in cfg.apps.items()} == deliveries_before == {
        "example": "livecontainer", "bravo": "livecontainer", "side": "sideload",
    }
    assert after["apps"]["example"]["delivery"] == "livecontainer", "written, not implied"
    # And everything that is not an app.
    for key in ("repos", "app_store_connect", "archive_keep", "default_device_udid",
                "cache_dir", "tweaks_dir"):
        assert after[key] == before[key], key
    # Only the signing questions: no overwrite question, no device question.
    assert confirms[0] == PAID_QUESTION
    assert not any("overwrite" in q for q in confirms)
    assert any(".p12" in p for p in prompts) and any(".mobileprovision" in p for p in prompts)
    flat = " ".join(text.split())
    assert "delivery: livecontainer written into example" in flat
    assert "set it before `graft --set-p12-password`" in flat


def test_init_honours_config_for_the_upgrade(
    free_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal for a config elsewhere says `graft --config <it> --init`."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    elsewhere = tmp_path / "elsewhere" / "config.yaml"
    elsewhere.parent.mkdir()
    elsewhere.write_text(free_config.read_text())
    default_before = free_config.read_text()
    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(lambda *a, **k: True))
    monkeypatch.setattr(init_flow.Prompt, "ask",
                        staticmethod(lambda *a, default=None, **k: default))

    result = CliRunner().invoke(cli_mod.app, ["--config", str(elsewhere), "--init"])

    assert result.exit_code == 0, result.output
    assert load_config(elsewhere).free_account is False
    assert free_config.read_text() == default_before


def test_a_paid_config_is_not_upgraded_twice(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    write_default_config(path, team_id="ABCDE12345")
    before = path.read_text()

    with pytest.raises(ConfigError, match="already has a signing: section"):
        add_signing_section(path, team_id="FGHIJ67890")

    assert path.read_text() == before


def test_the_upgrade_writes_the_section_it_is_given(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"apps": {"example": {"bundle_id": "com.example.app"}}}))

    pinned = add_signing_section(path, cert=tmp_path / "c.p12", profile=tmp_path / "p",
                                 team_id="ABCDE12345")

    assert pinned == ["example"]
    cfg = load_config(path)
    assert cfg.team_id == "ABCDE12345"
    assert cfg.signing.cert == tmp_path / "c.p12"
    assert next(iter(yaml.safe_load(path.read_text()))) == "signing"


def test_a_preview_of_the_upgrade_names_the_apps_it_would_pin_and_writes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "config.yaml"
    write_default_config(path, free_account=True)
    add_app(path, "plain", {"bundle_id": "com.example.plain"})
    add_app(path, "pinned", {"bundle_id": "com.example.pinned", "delivery": "livecontainer"})
    before = path.read_text()

    assert add_signing_section(path, write=False, team_id="ABCDE12345") == ["plain"]
    assert path.read_text() == before
