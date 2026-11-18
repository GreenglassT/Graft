"""The first run starts empty.

`--init` writes the signing/paths skeleton and no apps, plus one commented-out
app entry built from placeholders, so the file shows what an app looks like
without configuring one. And `--init` moves nothing into the cache on its own:
the step that imported a hand-made build directory is gone.
"""

from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from graft_ipa import init_flow
from graft_ipa.config import AppConfig, load_config, write_default_config


def _starter(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    write_default_config(path, team_id="ABCDE12345")
    return path


def test_the_starter_config_configures_no_apps(tmp_path: Path) -> None:
    path = _starter(tmp_path)

    cfg = load_config(path)

    assert cfg.apps == {}
    assert cfg.signing.team_id == "ABCDE12345"
    assert yaml.safe_load(path.read_text())["apps"] == {}


def test_the_example_is_commented_out_and_uses_only_placeholders(tmp_path: Path) -> None:
    text = _starter(tmp_path).read_text()

    commented = [ln for ln in text.splitlines() if ln.startswith("#")]
    assert any("bundle_id: com.example.app" in ln for ln in commented)
    assert any("- some-tweak.deb" in ln for ln in commented)
    # Every bundle id and every tweak file the starter mentions anywhere is a
    # placeholder — the only way a real app or tweak name could be in it.
    assert set(re.findall(r"\b(?:com|org|net|io)\.[\w.-]+", text)) == {"com.example.app"}
    assert set(re.findall(r"[\w.-]+\.(?:deb|dylib)\b", text)) == {"some-tweak.deb"}


def test_the_example_is_a_real_app_entry_once_uncommented(tmp_path: Path) -> None:
    """A commented example that no longer parses is worse than none."""
    text = _starter(tmp_path).read_text()
    block = [ln[2:] for ln in text.splitlines() if ln.startswith("#   ") or ln == "# apps:"]

    example = yaml.safe_load("\n".join(block))["apps"]

    [(name, entry)] = example.items()
    app = AppConfig.from_dict(name, entry)
    assert app.bundle_id == "com.example.app"
    assert app.tweaks == ["some-tweak.deb"]


def test_an_app_added_to_the_starter_lands_in_its_apps(tmp_path: Path) -> None:
    from graft_ipa.config import add_app

    path = _starter(tmp_path)
    add_app(path, "example", {"bundle_id": "com.example.app", "tweaks": []})

    assert list(load_config(path).apps) == ["example"]


def test_init_writes_the_empty_starter_and_imports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Driven end to end with every prompt answered by its default. The home
    directory holds an old project folder with an IPA and a tweak in it, so a
    first run that went looking for files to import would find some."""
    home = tmp_path / "home"
    legacy = home / "old-build"
    legacy.mkdir(parents=True)
    (legacy / "App.ipa").write_bytes(b"ipa")
    (legacy / "some-tweak.deb").write_bytes(b"deb")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(lambda *a, default=None, **k: default))
    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(lambda *a, default=None, **k: True))
    monkeypatch.setattr(init_flow, "list_connected_devices", lambda: [])
    out = StringIO()

    cfg_path = init_flow.run_init(Console(file=out, width=200))

    assert load_config(cfg_path).apps == {}
    assert list((tmp_path / "cache" / "graft" / "ipas").iterdir()) == []
    assert list((tmp_path / "cfg" / "graft" / "tweaks").iterdir()) == []
    assert "old-build" not in out.getvalue()
    assert "--add-app" in out.getvalue()


def _init_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("GRAFT_P12_PASS", raising=False)
    monkeypatch.setattr(init_flow.Prompt, "ask", staticmethod(lambda *a, default=None, **k: default))
    monkeypatch.setattr(init_flow.Confirm, "ask", staticmethod(lambda *a, default=None, **k: True))
    monkeypatch.setattr(init_flow, "list_connected_devices", lambda: [])
    out = StringIO()
    init_flow.run_init(Console(file=out, width=200))
    return out.getvalue()


def test_init_points_the_p12_password_at_the_keychain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Mac app is started by launchd, not by a shell, so a variable exported
    in ~/.zshrc never reaches the CLI it runs: a password kept only there fails
    the app's `.p12 password` check on every sideload build. The Keychain is the
    one place both can read, so that is what setup has to recommend."""
    text = _init_output(tmp_path, monkeypatch)
    step = text.split("Signing password", 1)[1].split("Provisioning profile", 1)[0]

    assert "graft --set-p12-password" in step
    # The shell variable may still be offered, but never as the advice on its own.
    assert "~/.zshrc" not in step or "Mac app" in step
