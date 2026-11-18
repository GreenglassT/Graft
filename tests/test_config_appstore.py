"""The optional `appstore:` section: where the user's ipatool and UnfairPlay live.

Both tools are installed by hand, outside this project, at paths chosen on the
day (2026-11-17) — so the defaults are those paths and the section exists only
for a machine where they differ. Missing section, missing key: defaults.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graft_ipa.config import add_app, load_config, set_settings
from graft_ipa.errors import ConfigError

from .config_fixtures import write_test_config


def _config_with(tmp_path: Path, appstore: object = None) -> Path:
    path = tmp_path / "config.yaml"
    write_test_config(path, team_id="TESTTEAM01")
    if appstore is not None:
        raw = yaml.safe_load(path.read_text())
        raw["appstore"] = appstore
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
    return path


@pytest.fixture(autouse=True)
def _no_model_dir_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The developer's own shell may export it, and then the default under test
    # would be whatever their shell says.
    monkeypatch.delenv("UNFAIRPLAY_MODEL_DIR", raising=False)


@pytest.fixture(autouse=True)
def _home_based_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The defaults below are spelled from `~`, so the data directory has to
    # come from `~` too — and from a scratch `~`, because under the real one
    # an unmigrated `~/.local/share/tweak-build` would rightly be preferred
    # and these assertions would depend on the developer's machine.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_a_config_without_the_section_gets_the_default_paths(tmp_path: Path) -> None:
    cfg = load_config(_config_with(tmp_path))

    home = Path.home()
    assert cfg.appstore.ipatool == home / ".local/bin/ipatool"
    assert cfg.appstore.unfairplay == home / ".local/share/graft/unfairplay/unfairplay"
    assert cfg.appstore.unfairplay_models == home / "Library/Application Support/unfairplay/models"


def test_an_empty_section_is_the_same_as_none(tmp_path: Path) -> None:
    """`appstore:` with nothing under it parses as None in YAML."""
    path = _config_with(tmp_path)
    path.write_text(path.read_text() + "appstore:\n")

    assert load_config(path).appstore.ipatool == Path.home() / ".local/bin/ipatool"


def test_configured_paths_are_tilde_expanded(tmp_path: Path) -> None:
    cfg = load_config(_config_with(tmp_path, {
        "ipatool": "~/bin/ipatool-2.8.0",
        "unfairplay": "~/src/unfairplay/unfairplay",
        "unfairplay_models": "~/models",
    }))

    home = Path.home()
    assert cfg.appstore.ipatool == home / "bin/ipatool-2.8.0"
    assert cfg.appstore.unfairplay == home / "src/unfairplay/unfairplay"
    assert cfg.appstore.unfairplay_models == home / "models"


def test_one_key_set_leaves_the_others_at_their_defaults(tmp_path: Path) -> None:
    cfg = load_config(_config_with(tmp_path, {"ipatool": "/opt/ipatool"}))

    assert cfg.appstore.ipatool == Path("/opt/ipatool")
    assert cfg.appstore.unfairplay == Path.home() / ".local/share/graft/unfairplay/unfairplay"


def test_the_model_dir_env_var_is_honoured_when_the_config_names_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UnfairPlay's own override, so a user who set it for the tool by hand
    gets the same directory here without saying it twice."""
    monkeypatch.setenv("UNFAIRPLAY_MODEL_DIR", str(tmp_path / "from-env"))

    cfg = load_config(_config_with(tmp_path))

    assert cfg.appstore.unfairplay_models == tmp_path / "from-env"


def test_the_config_beats_the_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UNFAIRPLAY_MODEL_DIR", str(tmp_path / "from-env"))

    cfg = load_config(_config_with(tmp_path, {"unfairplay_models": str(tmp_path / "from-cfg")}))

    assert cfg.appstore.unfairplay_models == tmp_path / "from-cfg"


@pytest.mark.parametrize("bad", [3, ["a"], {"x": 1}, True])
def test_a_non_string_path_is_a_config_error(tmp_path: Path, bad: object) -> None:
    with pytest.raises(ConfigError) as e:
        load_config(_config_with(tmp_path, {"ipatool": bad}))

    assert e.value.message.startswith("appstore.ipatool must be a path")


def test_a_section_that_is_not_a_mapping_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as e:
        load_config(_config_with(tmp_path, ["ipatool"]))

    assert e.value.message == "'appstore' must be a mapping of tool name -> path"


def test_writers_keep_the_section(tmp_path: Path) -> None:
    """The spec's round-trip clause. Every writer here reloads the raw YAML
    and dumps it back, so an unknown section survives by construction — this
    is what notices if one of them ever starts rebuilding the document from
    `Config` instead."""
    section = {"ipatool": "/opt/ipatool", "unfairplay_models": "~/models"}
    path = _config_with(tmp_path, section)

    set_settings(path, archive_keep=4)
    add_app(path, "bravo", {"bundle_id": "com.bravo.app", "tweaks": []})

    assert yaml.safe_load(path.read_text())["appstore"] == section
