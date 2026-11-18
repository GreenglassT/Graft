"""`--json --settings` reports where this config puts everything.

The Mac app's Settings → Paths used to hardcode `~/.config/graft`,
`~/.cache/graft/built` and friends, which is wrong for anyone with
`XDG_*_HOME` set or a `cache_dir` / `tweaks_dir` of their own. The CLI is the
one that resolves those directories, so it reports them, on the one event the
pane already reads, as `paths`.

The resolution has to be `load_config`'s exactly — a pane that names a
different directory than the build uses is worse than no pane — so these
tests compare against `load_config` rather than restating its rules.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa import cli as cli_mod
from graft_ipa.config import SETTINGS, load_config
from graft_ipa.json_output import JsonEmitter

PATH_KEYS = {"config_dir", "cache_dir", "built_dir", "state_dir", "tweaks_dir"}


def _settings_event(config_path: Path) -> dict:
    out = StringIO()
    cli_mod._do_settings(config_path, emitter=JsonEmitter(stream=out))
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert [e["type"] for e in events] == ["settings"]
    return events[0]


def _expected(config_path: Path) -> dict[str, str]:
    cfg = load_config(config_path)
    return {
        "config_dir": str(config_path.parent),
        "cache_dir": str(cfg.cache_dir),
        "built_dir": str(cfg.built_dir),
        "state_dir": str(cfg.state_dir),
        "tweaks_dir": str(cfg.tweaks_dir),
    }


def test_the_settings_event_reports_the_resolved_directories(temp_config_env: Path) -> None:
    event = _settings_event(temp_config_env)
    assert set(event["paths"]) == PATH_KEYS
    assert event["paths"] == _expected(temp_config_env)


def test_an_xdg_home_moves_every_default(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no directory keys in the config, each follows its XDG root —
    the case the hardcoded `~/.config/graft` got wrong."""
    raw = yaml.safe_load(temp_config_env.read_text())
    for key in ("cache_dir", "built_dir", "state_dir", "tweaks_dir"):
        raw.pop(key, None)
    temp_config_env.write_text(yaml.safe_dump(raw))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "elsewhere-cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere-state"))

    paths = _settings_event(temp_config_env)["paths"]
    assert paths == _expected(temp_config_env)
    assert paths["built_dir"] == str(tmp_path / "elsewhere-cache" / "graft" / "built")
    assert paths["state_dir"] == str(tmp_path / "elsewhere-state" / "graft")
    assert paths["tweaks_dir"] == str(temp_config_env.parent / "tweaks")


def test_the_configs_own_directories_win(
    temp_config_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `cache_dir` / `tweaks_dir` in the file — `~` and `$VARS` expanded, as
    every build expands them — is what is reported."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("EXAMPLE_ROOT", str(tmp_path / "root"))
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["cache_dir"] = "~/ipas"
    raw["tweaks_dir"] = "$EXAMPLE_ROOT/debs"
    raw["built_dir"] = str(tmp_path / "built-here")
    temp_config_env.write_text(yaml.safe_dump(raw))

    paths = _settings_event(temp_config_env)["paths"]
    assert paths == _expected(temp_config_env)
    assert paths["cache_dir"] == str(tmp_path / "home" / "ipas")
    assert paths["tweaks_dir"] == str(tmp_path / "root" / "debs")
    assert paths["built_dir"] == str(tmp_path / "built-here")


def test_the_settings_themselves_are_unchanged(temp_config_env: Path) -> None:
    """Additive: the five settings are still top-level, and beside them only
    `paths` and the two fields that say whether the file parsed."""
    event = _settings_event(temp_config_env)
    assert set(event) == {"type", "paths", "parse_error", "paths_are_defaults", *SETTINGS}


def test_paths_are_reported_for_a_config_too_broken_to_load(temp_config_env: Path) -> None:
    """`--settings` is the verb that has to work on a config the strict loader
    refuses — and the pane's Paths section is how a user finds that file."""
    raw = yaml.safe_load(temp_config_env.read_text())
    del raw["signing"]["team_id"]
    temp_config_env.write_text(yaml.safe_dump(raw))

    paths = _settings_event(temp_config_env)["paths"]
    assert paths["config_dir"] == str(temp_config_env.parent)
    assert set(paths) == PATH_KEYS


def test_paths_are_reported_for_a_config_that_does_not_parse(
    temp_config_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A YAML syntax error is the config most in need of finding: the pane
    still names its directory, the default places, and every setting as unset.

    The parse error rides on the event itself (C-SETTINGS): `parse_error`, one
    line, and `paths_are_defaults: true`, because the pane keeps only the
    `settings` event — the `log` warning it used to go out as was dropped, the
    paths read as the config's own, and the pane said "saving replaces it" to
    a config `--set-settings` then refused. stderr still gets it."""
    from graft_ipa.config import default_built_dir, default_cache_dir, default_state_dir

    temp_config_env.write_text("signing: [unclosed\n  cert: x\n")
    out = StringIO()
    cli_mod._do_settings(temp_config_env, emitter=JsonEmitter(stream=out))
    events = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]

    assert [e["type"] for e in events] == ["settings"], "carried by the event, not a log line"
    (event,) = events
    assert set(event) == {"type", "paths", "parse_error", "paths_are_defaults", *SETTINGS}
    assert all(event[key] is None for key in SETTINGS)
    assert "failed to parse" in event["parse_error"]
    assert "\n" not in event["parse_error"], "one line"
    assert event["paths_are_defaults"] is True
    paths = event["paths"]
    assert set(paths) == PATH_KEYS
    assert paths["config_dir"] == str(temp_config_env.parent)
    assert paths["cache_dir"] == str(default_cache_dir())
    assert paths["built_dir"] == str(default_built_dir())
    assert paths["state_dir"] == str(default_state_dir())
    assert paths["tweaks_dir"] == str(temp_config_env.parent / "tweaks")
    assert "failed to parse" in capsys.readouterr().err


def test_a_config_that_parses_says_so(temp_config_env: Path) -> None:
    event = _settings_event(temp_config_env)

    assert event["parse_error"] is None
    assert event["paths_are_defaults"] is False


def test_a_top_level_that_is_not_a_mapping_is_a_parse_error_too(temp_config_env: Path) -> None:
    temp_config_env.write_text("- just\n- a list\n")

    event = _settings_event(temp_config_env)

    assert "mapping" in event["parse_error"]
    assert event["paths_are_defaults"] is True


def test_the_cli_exits_zero_with_them_so_the_app_reads_them(temp_config_env: Path) -> None:
    """The Mac app discards a non-zero run's events, so exit 0 is what lets
    the Paths section show anything at all."""
    from typer.testing import CliRunner

    temp_config_env.write_text("signing: [unclosed\n")
    result = CliRunner().invoke(cli_mod.app, ["--json", "--settings"])
    assert result.exit_code == 0, result.output
    assert any(json.loads(line)["type"] == "settings"
               for line in result.stdout.splitlines() if line.startswith("{"))


def test_a_person_is_still_told_the_config_does_not_parse(temp_config_env: Path) -> None:
    from typer.testing import CliRunner

    temp_config_env.write_text("signing: [unclosed\n")
    result = CliRunner().invoke(cli_mod.app, ["--settings"])
    assert result.exit_code != 0
    assert "failed to parse" in result.output


def test_a_missing_config_is_still_an_error_under_json(temp_config_env: Path) -> None:
    """Not this fix's business: "no config yet" is how the app knows to offer
    setup, and it stays an error."""
    from typer.testing import CliRunner

    temp_config_env.unlink()
    result = CliRunner().invoke(cli_mod.app, ["--json", "--settings"])
    assert result.exit_code != 0
