from __future__ import annotations

from pathlib import Path

import pytest

from graft_ipa import tools


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    return helpers


@pytest.fixture
def path_has(monkeypatch):
    found: dict[str, Path] = {}
    monkeypatch.setattr(tools, "which", lambda name: str(found[name]) if name in found else None)
    return found


def test_an_explicit_config_path_wins_even_over_a_bundled_copy(tmp_path, bundle, path_has):
    _exe(bundle / "zsign")
    mine = _exe(tmp_path / "mine" / "zsign")
    r = tools.resolve("zsign", configured=mine)
    assert (r.path, r.source) == (mine, tools.SOURCE_CONFIG)


def test_a_bundled_copy_wins_over_the_default_and_path(tmp_path, bundle, path_has):
    _exe(bundle / "zsign")
    default = _exe(tmp_path / "default" / "zsign")
    path_has["zsign"] = _exe(tmp_path / "onpath" / "zsign")
    r = tools.resolve("zsign", default=default)
    assert (r.path, r.source) == (bundle / "zsign", tools.SOURCE_BUNDLED)


def test_without_a_bundle_the_default_then_path_are_used(tmp_path, path_has, monkeypatch):
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    default = tmp_path / "default" / "ipatool"
    path_has["ipatool"] = _exe(tmp_path / "onpath" / "ipatool")
    assert tools.resolve("ipatool", default=default).source == tools.SOURCE_PATH
    _exe(default)
    assert tools.resolve("ipatool", default=default).source == tools.SOURCE_DEFAULT


def test_missing_everywhere_names_the_default_location(tmp_path, path_has, monkeypatch):
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    default = tmp_path / "default" / "ipatool"
    r = tools.resolve("ipatool", default=default)
    assert (r.path, r.source, r.found) == (default, tools.SOURCE_MISSING, False)


def test_a_non_executable_bundled_file_is_skipped(tmp_path, bundle, path_has):
    (bundle / "zsign").write_text("not executable")
    path_has["zsign"] = _exe(tmp_path / "onpath" / "zsign")
    assert tools.resolve("zsign").source == tools.SOURCE_PATH


@pytest.mark.parametrize("exe, expected", [
    ("/Applications/Graft.app/Contents/Frameworks/GraftPython/bin/python3", "/Applications/Graft.app/Contents/Helpers/bin"),
    ("/Users/x/venv/bin/python3", None),
])
def test_bundled_tools_dir_is_found_from_the_interpreters_location(tmp_path, exe, expected):
    real = tmp_path / exe.lstrip("/")
    if expected:
        (tmp_path / expected.lstrip("/")).mkdir(parents=True)
    got = tools._bundled_tools_dir_for(str(real))
    assert got == (tmp_path / expected.lstrip("/") if expected else None)


def test_an_app_bundle_without_helpers_has_no_bundled_tools(tmp_path):
    exe = tmp_path / "Graft.app" / "Contents" / "Frameworks" / "GraftPython" / "bin" / "python3"
    assert tools._bundled_tools_dir_for(str(exe)) is None
