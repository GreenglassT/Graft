from pathlib import Path
from types import SimpleNamespace

import pytest

from graft_ipa import data_dirs, ipatool, tools
from graft_ipa.config import AppStoreToolsConfig
from graft_ipa.errors import ConfigError


def test_no_section_means_nothing_is_explicit():
    assert AppStoreToolsConfig.from_dict(None).explicit == frozenset()


def test_a_path_the_user_chose_is_explicit(tmp_path):
    cfg = AppStoreToolsConfig.from_dict({"ipatool": str(tmp_path / "ipatool")})
    assert cfg.explicit == frozenset({"ipatool"})
    assert cfg.ipatool == tmp_path / "ipatool"


def test_a_1x_config_spelling_out_the_old_defaults_counts_as_unset():
    cfg = AppStoreToolsConfig.from_dict({
        "ipatool": "~/.local/bin/ipatool",
        "unfairplay": str(data_dirs.data_dir() / "unfairplay" / "unfairplay"),
    })
    assert cfg.explicit == frozenset()


def test_the_models_dir_is_never_a_tool():
    cfg = AppStoreToolsConfig.from_dict({"unfairplay_models": "/tmp/models"})
    assert cfg.explicit == frozenset()


@pytest.mark.parametrize("value", ["ipatool", "2.8.0", "bin/ipatool", "./ipatool", "$NO_SUCH_VAR/ipatool"])
def test_a_relative_tool_path_is_ignored_with_a_notice(value, monkeypatch):
    """Review #36: it became an explicit relative Path that beat the bundled
    copy, never fell back, and resolved against the CLI's cwd (`/` for the
    app). Refusing it then failed every verb on a config 1.1 had loaded (the
    whole-diff review's upgrade-2 = cli-4), so it is read as unset, with a
    notice, as a `store_country` that isn't a store is. So is a `$VAR` that
    isn't set, which leaves the path relative."""
    monkeypatch.delenv("NO_SUCH_VAR", raising=False)
    notices: list[str] = []
    cfg = AppStoreToolsConfig.from_dict({"ipatool": value}, notices=notices)
    assert cfg.ipatool == AppStoreToolsConfig().ipatool, "the default, as if unset"
    assert cfg.explicit == frozenset(), "so Graft.app's own copy still wins"
    assert notices == [
        f"appstore.ipatool {value!r} in config.yaml isn't a full path; ignoring it "
        "(start it with / or ~/, or remove it to use the default)"
    ]


def test_a_relative_unfairplay_is_ignored_too():
    notices: list[str] = []
    cfg = AppStoreToolsConfig.from_dict({"unfairplay": "unfairplay"}, notices=notices)
    assert (cfg.unfairplay, cfg.explicit) == (AppStoreToolsConfig().unfairplay, frozenset())
    assert [n.split(" in config.yaml")[0] for n in notices] == ["appstore.unfairplay 'unfairplay'"]


@pytest.mark.parametrize("value", ["models", "./unfairplay/models"])
def test_a_relative_models_dir_is_ignored_as_well(value):
    """Re-check #38: it resolves against the CLI's cwd (`/` for the app)
    just as a tool path does. The README names all three keys; this pins
    the third."""
    notices: list[str] = []
    cfg = AppStoreToolsConfig.from_dict({"unfairplay_models": value}, notices=notices)
    assert cfg.unfairplay_models == AppStoreToolsConfig().unfairplay_models
    assert len(notices) == 1 and notices[0].startswith(f"appstore.unfairplay_models {value!r}")


def test_a_path_that_isnt_text_is_still_refused():
    """1.1 refused these too: not a path at all."""
    with pytest.raises(ConfigError, match=r"appstore\.ipatool must be a path"):
        AppStoreToolsConfig.from_dict({"ipatool": 2.8})


def test_a_1_1_config_with_relative_tool_paths_loads_and_every_verb_warns(tmp_path):
    """upgrade-2 with a config.yaml shaped as 1.1 wrote it, and keys 1.1
    accepted: it loads, the good key is kept, and `--list` warns once per
    bad key and carries on."""
    import json

    import yaml
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config
    from tests.config_fixtures import write_test_config

    config = tmp_path / "config.yaml"
    write_test_config(config)
    raw = yaml.safe_load(config.read_text())
    raw["appstore"] = {"ipatool": "bin/ipatool", "unfairplay": "/opt/tools/unfairplay",
                       "unfairplay_models": "models"}
    config.write_text(yaml.safe_dump(raw, sort_keys=False))

    cfg = load_config(config)
    assert cfg.appstore.ipatool == AppStoreToolsConfig().ipatool
    assert cfg.appstore.unfairplay == Path("/opt/tools/unfairplay")
    assert cfg.appstore.explicit == frozenset({"unfairplay"})
    assert [n.split(" ")[0] for n in cfg.notices] == ["appstore.ipatool", "appstore.unfairplay_models"]

    result = CliRunner().invoke(cli_mod.app, ["--json", "--config", str(config), "--list"])
    assert result.exit_code == 0, result.output
    logs = [e for e in map(json.loads, result.stdout.splitlines()) if e["type"] == "log"]
    assert [(e["source"], e["line"]) for e in logs] == [("startup", n) for n in cfg.notices]


@pytest.mark.parametrize("value", ["~/tools/ipatool", "/opt/ipatool/bin/ipatool"])
def test_a_full_or_home_relative_path_still_loads(value):
    assert AppStoreToolsConfig.from_dict({"ipatool": value}).ipatool.is_absolute()


def test_the_bundled_bad_version_advice_can_take_effect():
    """Review #37: the advice was to point appstore.ipatool at a good
    binary, and the natural place, ~/.local/bin/ipatool, is the old default:
    not explicit, so the bundled bad copy still won and the warning came
    back. The path the advice names must be one a config makes explicit."""
    hint = ipatool.bad_version_hint("2.8.1", source=tools.SOURCE_BUNDLED)

    assert ipatool.OVERRIDE_PATH in hint
    assert "~/.local/bin" not in hint, "the one path a config cannot make win"
    chosen = AppStoreToolsConfig.from_dict({"ipatool": ipatool.OVERRIDE_PATH})
    assert chosen.explicit == frozenset({"ipatool"})


# --- re-check #12: the old default is read as unset, and now says so --------


def _bundled_ipatool(tmp_path, monkeypatch, version):
    from tests.appstore_fakes import FakeTools

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Nothing on this Mac's PATH: a real ipatool there would be run.
    monkeypatch.setattr(tools, "which", lambda _name: None)
    bundled = FakeTools(tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin")
    bundled.set(ipatool_version=version)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: bundled.root)
    return bundled


def test_a_config_naming_the_old_default_is_marked_as_read_as_unset():
    cfg = AppStoreToolsConfig.from_dict({"ipatool": "~/.local/bin/ipatool"})
    assert cfg.spelled_default == frozenset({"ipatool"})
    assert AppStoreToolsConfig.from_dict({"ipatool": "/opt/x/ipatool"}).spelled_default == frozenset()
    assert AppStoreToolsConfig.from_dict(None).spelled_default == frozenset()


def test_status_says_why_the_configured_ipatool_was_passed_over(tmp_path, monkeypatch, capsys):
    """Re-check #12: `appstore: {ipatool: ~/.local/bin/ipatool}` is the 1.x
    default spelled out, so Graft.app's copy runs, and nothing said that the
    setting was read as no choice at all."""
    from graft_ipa import appstore

    _bundled_ipatool(tmp_path, monkeypatch, "2.8.0")
    cfg = AppStoreToolsConfig.from_dict({"ipatool": "~/.local/bin/ipatool"})

    payload = appstore.status(cfg)
    assert payload["ipatool"]["source"] == tools.SOURCE_BUNDLED
    # A working bundled copy is still ready: the app reads any warning as "not".
    assert payload["ipatool"]["warning"] is None

    said: list[str] = []
    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=None, say=said.append)
    note = next(line for line in said if "appstore.ipatool" in line)
    assert "~/.local/bin/ipatool" in note and "no choice" in note
    assert ipatool.OVERRIDE_PATH in note


def test_under_json_the_note_is_a_warning_event(tmp_path, monkeypatch):
    import json
    from io import StringIO

    from graft_ipa import appstore
    from graft_ipa.json_output import JsonEmitter

    _bundled_ipatool(tmp_path, monkeypatch, "2.8.0")
    cfg = AppStoreToolsConfig.from_dict({"ipatool": "~/.local/bin/ipatool"})
    buf = StringIO()

    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=JsonEmitter(stream=buf), say=print)

    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    warnings = [e["line"] for e in events if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 1 and "no choice" in warnings[0]
    assert events[-1]["type"] == "appstore-status"


def test_with_a_bad_bundled_copy_the_warning_carries_the_reason(tmp_path, monkeypatch):
    """The reviewer's case: Graft.app's ipatool is the bad 2.8.1, the user
    has a good one at ~/.local/bin and named it. The warning the app shows
    must say why that setting didn't count."""
    from graft_ipa import appstore

    _bundled_ipatool(tmp_path, monkeypatch, "2.8.1")
    cfg = AppStoreToolsConfig.from_dict({"ipatool": "~/.local/bin/ipatool"})

    warning = appstore.status(cfg)["ipatool"]["warning"]

    assert "2.8.1" in warning and "no choice" in warning


def test_a_bundled_copy_with_no_such_setting_says_nothing_more(tmp_path, monkeypatch):
    from graft_ipa import appstore

    _bundled_ipatool(tmp_path, monkeypatch, "2.8.0")
    said: list[str] = []
    appstore.report_status(
        SimpleNamespace(appstore=AppStoreToolsConfig.from_dict(None)), emitter=None, say=said.append
    )
    assert not any("no choice" in line for line in said)


def test_without_graft_app_the_old_default_is_used_and_nothing_is_said(tmp_path, monkeypatch):
    """A pipx CLI has no bundled copy: ~/.local/bin/ipatool is what runs."""
    from graft_ipa import appstore

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Nothing on this Mac's PATH: a real ipatool there would be run.
    monkeypatch.setattr(tools, "which", lambda _name: None)
    calls: list[str] = []
    monkeypatch.setattr(ipatool, "version", lambda path, **_: calls.append(str(path)))
    monkeypatch.setattr(ipatool, "signed_in", lambda path, **_: calls.append(str(path)))
    cfg = AppStoreToolsConfig.from_dict({"ipatool": "~/.local/bin/ipatool"})

    said: list[str] = []
    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=None, say=said.append)

    assert not any("no choice" in line for line in said)
    assert calls == [], "an ipatool ran, and none is there to run"


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("doc", ["CHANGELOG.md", "README.md", "docs/upgrading.md"])
def test_the_docs_say_a_relative_path_is_ignored_not_refused(doc):
    """The try14 delta audit's F1: the release notes, the README and the
    upgrade guide still said a relative appstore.* path was refused and the
    config wouldn't load, after upgrade-2 made it a notice. Nothing pinned
    the docs to the behavior above, which is how they drifted."""
    text = " ".join((REPO / doc).read_text().split())
    assert "relative path there is now refused" not in text
    assert "A relative one is refused" not in text
    assert "config doesn't load until you fix" not in text
    assert "a relative path is refused" not in text
    assert "ignored" in text and "relative" in text
