"""Review #12: in Graft.app a missing bundled tool means the app is damaged.

Its tools come inside it, so the pipx and Homebrew commands a pipx install
is told are the wrong advice: the fix is a fresh download.
"""

from __future__ import annotations

from pathlib import Path

from graft_ipa import preflight, tools


def _app_without(tmp_path: Path, monkeypatch, *present: str) -> Path:
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    for name in present:
        (helpers / name).write_text("#!/bin/sh\n")
        (helpers / name).chmod(0o755)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    monkeypatch.setattr(tools, "which", lambda name: None)
    return helpers


def test_in_graft_app_a_missing_bundled_tool_says_download_graft_again(tmp_path, monkeypatch):
    helpers = _app_without(tmp_path, monkeypatch, "zsign")
    cyan, zsign = preflight.check_tools(["cyan", "zsign"])
    assert zsign.ok and not cyan.ok
    assert cyan.hint == f"Graft.app is damaged: {helpers / 'cyan'} is missing. Download Graft again."
    assert cyan.install is None, "no command to copy: the fix is a download"
    assert "pipx" not in cyan.hint and "brew" not in cyan.hint


def test_outside_graft_app_the_install_command_stays(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    monkeypatch.setattr(tools, "which", lambda name: None)
    (cyan,) = preflight.check_tools(["cyan"])
    assert not cyan.ok and "damaged" not in cyan.hint and cyan.hint.startswith("install with: ")


def test_a_system_tool_graft_app_does_not_carry_keeps_its_hint(tmp_path, monkeypatch):
    _app_without(tmp_path, monkeypatch)
    (security,) = preflight.check_tools(["security"])
    assert "damaged" not in (security.hint or "")
