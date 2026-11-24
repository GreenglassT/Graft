"""`--init`, answered "no paid membership" on a paid config, keeps everything (F6).

The answer used to reach the fresh-start path: "overwrite?" and, on yes, an
empty free starter in place of the config — every app, repo and App Store
Connect key gone. It is now the mirror of the upgrade: a question whether to
switch this config to a free Apple ID, and on yes the `signing:` section is
removed and nothing else.

The design call for apps: an app with no `delivery:` key is written nothing.
It was a sideload app because the config was paid, and on a free Apple ID
the default — the only delivery there is — is LiveContainer, which is what
the user is switching to build with. An app with an explicit `delivery:
sideload` is kept as it is, so `--check` names it with its one-line fix and
each build of it is refused with the paid-only sentence, instead of the
switch quietly changing what the user wrote; `--init` lists them.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from graft_ipa import init_flow, preflight
from graft_ipa.config import (
    add_app,
    add_signing_section,
    load_config,
    remove_signing_section,
    write_default_config,
)
from graft_ipa.errors import ConfigError

PAID_QUESTION = "Do you have a paid Apple Developer Program membership?"

APPS = {
    "example": {"bundle_id": "com.example.app", "tweaks": ["a.deb"],
                "cyan": {"display_name": "Example+"}},
    "bravo": {"bundle_id": "com.example.bravo", "delivery": "livecontainer",
              "lc_bundle_id": "com.kdt.LiveContainer2.ABCDE12345"},
    "side": {"bundle_id": "com.example.side", "delivery": "sideload"},
}
ASC = {"key_id": "KEY123", "issuer_id": "ISSUER", "key_file": "/keys/AuthKey.p8",
       "device_udids": ["UDID1"]}


@pytest.fixture
def paid_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(init_flow, "list_connected_devices",
                        lambda: pytest.fail("the switch asked for a device"))
    path = tmp_path / "xdg-config" / "graft" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump({
        "default_device_udid": "UDID1", "archive_keep": 4,
        "apps": APPS, "repos": [{"url": "https://repo.example.com", "name": "Example"}],
        "app_store_connect": ASC,
    }, sort_keys=False))
    add_signing_section(path, cert=tmp_path / "c.p12", profile=tmp_path / "p.mobileprovision",
                        team_id="ABCDE12345")
    # The upgrade pinned every no-key app; take that back for one, so the
    # config has the paid-side shape too: a bare app that sideloads.
    raw = yaml.safe_load(path.read_text())
    del raw["apps"]["example"]["delivery"]
    path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = load_config(path)
    assert not cfg.free_account and cfg.apps["example"].delivery == "sideload"
    return path


def _init(monkeypatch, path: Path, *, switch: bool) -> tuple[list[str], str]:
    asked: list[str] = []

    def confirm(question, *a, **k):
        asked.append(question)
        return False if question == PAID_QUESTION else switch

    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(confirm))
    monkeypatch.setattr(init_flow.Prompt, "ask",
                        staticmethod(lambda *a, **k: pytest.fail("prompted for a path")))
    out = StringIO()
    init_flow.run_init(Console(file=out, width=300), cfg_path=path)
    return asked, out.getvalue()


def test_no_on_a_paid_config_switches_it_and_keeps_everything(
    paid_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = load_config(paid_config)

    asked, text = _init(monkeypatch, paid_config, switch=True)

    after = load_config(paid_config)
    assert after.free_account
    assert set(after.apps) == set(APPS)
    assert after.apps["example"].tweaks == ["a.deb"]
    assert after.apps["example"].cyan.display_name == "Example+"
    assert after.apps["bravo"].lc_bundle_id == "com.kdt.LiveContainer2.ABCDE12345"
    assert after.repos == before.repos
    assert after.app_store_connect == before.app_store_connect
    assert (after.archive_keep, after.default_device_udid) == (4, "UDID1")
    assert not any("overwrite" in q for q in asked), "never the fresh-start question"
    assert "side" in text, "the apps left on sideload are named"


def test_the_design_call_for_each_kind_of_app(paid_config: Path, monkeypatch) -> None:
    _init(monkeypatch, paid_config, switch=True)

    raw = yaml.safe_load(paid_config.read_text())["apps"]
    after = load_config(paid_config)
    # No key: nothing written, and LiveContainer is the free default.
    assert "delivery" not in raw["example"]
    assert after.apps["example"].delivery == "livecontainer"
    # Explicit sideload: kept, and --check says so with its fix.
    assert raw["side"]["delivery"] == "sideload"
    rows = preflight.check_app_deliveries(after)
    assert [r.name for r in rows] == ["app 'side'"]
    assert "--delivery livecontainer" in rows[0].hint


def test_declining_the_switch_keeps_the_paid_config_untouched(
    paid_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text_before = paid_config.read_text()

    _init(monkeypatch, paid_config, switch=False)

    assert paid_config.read_text() == text_before
    assert not load_config(paid_config).free_account


def test_remove_signing_section_returns_the_sideload_apps(paid_config: Path) -> None:
    assert remove_signing_section(paid_config) == ["side"]
    assert "signing" not in yaml.safe_load(paid_config.read_text())


def test_a_free_config_has_no_section_to_remove(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"apps": {}}))

    with pytest.raises(ConfigError):
        remove_signing_section(path)


def test_a_preview_of_the_switch_names_the_sideload_apps_and_writes_nothing(tmp_path: Path) -> None:
    """`--init --account free --dry-run`: the same answer as the switch, from
    the same code, with config.yaml untouched."""
    path = tmp_path / "config.yaml"
    write_default_config(path, team_id="ABCDE12345")
    add_app(path, "side", {"bundle_id": "com.example.side", "delivery": "sideload"})
    add_app(path, "plain", {"bundle_id": "com.example.plain"})
    before = path.read_text()

    assert remove_signing_section(path, write=False) == ["side"]
    assert path.read_text() == before
