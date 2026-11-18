"""`graft --init` asks about the account first, and each answer is a different setup.

Driven through the real prompts with scripted answers, the way
test_starter_config.py drives the paid path. "No" must never reach a .p12 or
profile prompt — a free Apple ID has neither, and asking for one would read as
a requirement — and it writes the config that makes Graft a free-account tool.
"""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from graft_ipa import init_flow
from graft_ipa.config import load_config

PAID_QUESTION = "Do you have a paid Apple Developer Program membership?"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    monkeypatch.setattr(init_flow, "list_connected_devices", lambda: ["UDID-PLACEHOLDER"])
    return tmp_path / "cfg" / "graft" / "config.yaml"


def _drive(monkeypatch: pytest.MonkeyPatch, *, paid: bool) -> tuple[list[str], list[str], str]:
    """Run `--init` answering the account question with `paid`, every other
    confirmation yes and every path prompt with its default. Returns the
    confirmations asked, the prompts asked, and what was printed."""
    confirms: list[str] = []
    prompts: list[str] = []

    def confirm(question, *a, default=..., **k):
        confirms.append(question)
        if question == PAID_QUESTION:
            # Asked with no default: Enter must not choose an account for the user.
            assert default is ...
            return paid
        return True

    def prompt(question, *a, default=None, **k):
        prompts.append(question)
        return default

    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(confirm))
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(prompt))
    out = StringIO()
    init_flow.run_init(Console(file=out, width=300))
    return confirms, prompts, out.getvalue()


def test_no_writes_a_free_config_without_asking_for_signing_files(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    confirms, prompts, text = _drive(monkeypatch, paid=False)

    assert confirms[0] == PAID_QUESTION
    assert prompts == [], "no .p12, profile or team questions"
    raw = yaml.safe_load(home.read_text())
    assert "signing" not in raw
    cfg = load_config(home)
    assert cfg.free_account is True
    assert cfg.default_device_udid == "UDID-PLACEHOLDER"
    assert "--set-p12-password" not in text


def test_no_prints_the_free_account_setup_in_order(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, text = _drive(monkeypatch, paid=False)

    steps = [
        "https://sidestore.io",
        "https://github.com/nab138/iloader",
        "https://livecontainer.github.io/docs/installation/lc_sidestore",
        "import the certificate from SideStore",
        "graft --add-app",
    ]
    positions = [text.find(s) for s in steps]
    assert all(p >= 0 for p in positions), dict(zip(steps, positions, strict=True))
    assert positions == sorted(positions)


def test_yes_is_todays_flow(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    confirms, prompts, text = _drive(monkeypatch, paid=True)

    assert confirms[0] == PAID_QUESTION
    assert any(".p12" in p for p in prompts)
    assert any(".mobileprovision" in p for p in prompts)
    cfg = load_config(home)
    assert cfg.free_account is False
    assert cfg.signing.team_id == "REPLACEME"
    assert "graft --set-p12-password" in text
    assert "sidestore.io" not in text


def test_no_still_asks_before_overwriting_a_config(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home.parent.mkdir(parents=True)
    home.write_text("archive_keep: 5\n")
    monkeypatch.setattr(init_flow, "list_connected_devices", lambda: [])

    def confirm(question, *a, default=..., **k):
        return False  # no paid membership, and keep the existing config

    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(confirm))
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(lambda *a, **k: pytest.fail("asked")))

    init_flow.run_init(Console(file=StringIO()))

    assert home.read_text() == "archive_keep: 5\n"
