"""RC audit #20: a configured `appstore.ipatool` or `appstore.unfairplay`
that isn't there any more.

Under 1.x a user could point either at a copy of their own (a Homebrew
ipatool, an UnfairPlay built elsewhere). 2.0's upgrade guide says those
tools aren't needed any more, so they remove them, and the configured path
then named nothing. It still won over Graft.app's own copy, so Settings ›
App Store showed "not found" with a Terminal block of curl and Go build
steps, hid Sign In… and Set Up Decryption…, and Get failed.

Now a missing configured path gives way to the bundled copy, in Graft.app
only, and `--appstore-status` says so. A configured path that is there
still wins (the documented override), and a CLI that isn't Graft.app's
still names the configured place.
"""

from __future__ import annotations

import json
from io import StringIO
from types import SimpleNamespace

import pytest

from graft_ipa import appstore, tools
from graft_ipa.config import AppStoreToolsConfig
from graft_ipa.json_output import JsonEmitter
from tests.appstore_fakes import FakeTools


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def no_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Nothing on this Mac's PATH: a real ipatool there would be run.
    monkeypatch.setattr(tools, "which", lambda _name: None)


@pytest.fixture
def bundled(tmp_path, monkeypatch, no_path):
    fake = FakeTools(tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin")
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: fake.root)
    return fake


def _gone_config(tmp_path, models):
    gone = tmp_path / "removed-since"
    return AppStoreToolsConfig.from_dict({
        "ipatool": str(gone / "ipatool"),
        "unfairplay": str(gone / "unfairplay"),
        "unfairplay_models": str(models),
    }), gone


# --- the resolver -------------------------------------------------------------


def test_a_configured_path_that_is_gone_gives_way_to_the_bundled_copy(tmp_path, monkeypatch, no_path):
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    _exe(helpers / "unfairplay")
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)

    r = tools.resolve("unfairplay", configured=tmp_path / "gone" / "unfairplay")

    assert (r.path, r.source, r.found) == (helpers / "unfairplay", tools.SOURCE_BUNDLED, True)


def test_a_configured_path_that_is_there_still_wins_over_the_bundled_copy(tmp_path, monkeypatch, no_path):
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    _exe(helpers / "unfairplay")
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    mine = tmp_path / "mine" / "unfairplay"
    mine.parent.mkdir()
    mine.write_text("not even executable: still the user's choice, and the error names it")

    r = tools.resolve("unfairplay", configured=mine)

    assert (r.path, r.source) == (mine, tools.SOURCE_CONFIG)


def test_a_dangling_symlink_counts_as_gone(tmp_path, monkeypatch, no_path):
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    _exe(helpers / "ipatool")
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    link = tmp_path / "brew" / "ipatool"
    link.parent.mkdir()
    link.symlink_to(tmp_path / "Cellar" / "ipatool" / "2.1.4" / "bin" / "ipatool")

    assert tools.resolve("ipatool", configured=link).source == tools.SOURCE_BUNDLED


def test_outside_graft_app_a_missing_configured_path_is_still_the_one_named(tmp_path, monkeypatch, no_path):
    """A pipx CLI has nothing to fall back to: "not found at" must name the
    place the user chose, as before."""
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)
    gone = tmp_path / "gone" / "ipatool"

    r = tools.resolve("ipatool", configured=gone, default=_exe(tmp_path / "default" / "ipatool"))

    assert (r.path, r.source, r.found) == (gone, tools.SOURCE_CONFIG, False)


def test_a_damaged_bundle_without_the_tool_names_the_configured_path(tmp_path, monkeypatch, no_path):
    helpers = tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin"
    helpers.mkdir(parents=True)
    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: helpers)
    gone = tmp_path / "gone" / "unfairplay"

    assert (tools.resolve("unfairplay", configured=gone).path) == gone


# --- what the app and the CLI see ---------------------------------------------


def test_status_runs_the_bundled_tools_and_offers_no_terminal_steps(tmp_path, bundled):
    cfg, gone = _gone_config(tmp_path, bundled.models)

    payload = appstore.status(cfg)

    ipa, up = payload["ipatool"], payload["unfairplay"]
    assert (ipa["path"], ipa["source"], ipa["present"]) == (str(bundled.ipatool), tools.SOURCE_BUNDLED, True)
    assert (up["path"], up["source"], up["present"]) == (str(bundled.unfairplay), tools.SOURCE_BUNDLED, True)
    # Present is what the app's Sign In… and Set Up Decryption… wait for.
    assert ipa["install"] is None and up["install"] is None
    steps = "\n".join(payload["setup_commands"])
    assert str(gone) not in steps
    for terminal in ("brew install go", "go -C unfairplay build", "git clone", "curl -fL"):
        assert terminal not in steps
    # A working bundled copy is ready: the app reads any warning as "not".
    assert ipa["warning"] is None


def test_get_runs_the_bundled_tools_instead_of_failing_on_the_gone_path(tmp_path, bundled):
    cfg, _ = _gone_config(tmp_path, bundled.models)

    assert appstore._ipatool_path(cfg) == bundled.ipatool
    appstore._require_unfairplay(cfg)  # no "UnfairPlay not found at …"


def test_status_says_the_setting_was_passed_over_and_how_to_stop_it(tmp_path, bundled):
    cfg, gone = _gone_config(tmp_path, bundled.models)

    said: list[str] = []
    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=None, say=said.append)

    for key, name in (("ipatool", "ipatool"), ("unfairplay", "UnfairPlay")):
        note = next(line for line in said if f"appstore.{key} " in line)
        assert str(gone / key) in note and "isn't there" in note
        assert f"Graft.app's own {name}" in note
        assert f"remove appstore.{key}" in note


def test_under_json_each_note_is_a_warning_event_before_the_status(tmp_path, bundled):
    cfg, gone = _gone_config(tmp_path, bundled.models)
    buf = StringIO()

    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=JsonEmitter(stream=buf), say=print)

    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    warnings = [e["line"] for e in events if e["type"] == "log" and e["source"] == "warning"]
    assert len(warnings) == 2
    assert str(gone / "ipatool") in warnings[0] and str(gone / "unfairplay") in warnings[1]
    assert events[-1]["type"] == "appstore-status"


def test_a_configured_path_that_is_there_runs_and_says_nothing(tmp_path, bundled):
    mine = FakeTools(tmp_path / "mine")
    cfg = AppStoreToolsConfig.from_dict(mine.appstore_section())

    payload = appstore.status(cfg)
    said: list[str] = []
    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=None, say=said.append)

    assert payload["ipatool"]["source"] == payload["unfairplay"]["source"] == tools.SOURCE_CONFIG
    assert payload["ipatool"]["path"] == str(mine.ipatool)
    assert not any("isn't there" in line for line in said)


def test_a_bundled_copy_with_no_setting_says_nothing_about_one(tmp_path, bundled):
    said: list[str] = []
    cfg = AppStoreToolsConfig.from_dict({"unfairplay_models": str(bundled.models)})

    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=None, say=said.append)

    assert not any("isn't there" in line for line in said)


# --- Delta audit #7: the note reaches the app on the status itself -------------


def _status_event(cfg) -> dict:
    buf = StringIO()
    appstore.report_status(SimpleNamespace(appstore=cfg), emitter=JsonEmitter(stream=buf), say=print)
    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert events[-1]["type"] == "appstore-status"
    return events[-1]


def test_the_status_event_itself_carries_each_note_for_the_app(tmp_path, bundled):
    """Graft.app keeps only the appstore-status event, so a note sent as a
    `log` warning never showed there, and the stale key stayed until a
    reinstall at that path quietly won again. Each tool's entry has it now,
    as well as the warning event and the Terminal text."""
    cfg, gone = _gone_config(tmp_path, bundled.models)

    status = _status_event(cfg)

    for key in ("ipatool", "unfairplay"):
        note = status[key]["note"]
        assert str(gone / key) in note and "isn't there" in note and f"remove appstore.{key}" in note
        assert note == appstore.missing_path_note(cfg, key, tools.SOURCE_BUNDLED)
    assert appstore.status(cfg)["ipatool"]["note"] == status["ipatool"]["note"]


def test_with_nothing_passed_over_each_note_is_null(tmp_path, bundled):
    mine = FakeTools(tmp_path / "mine")
    for cfg in (AppStoreToolsConfig.from_dict({"unfairplay_models": str(bundled.models)}),
                AppStoreToolsConfig.from_dict(mine.appstore_section())):
        status = _status_event(cfg)
        assert status["ipatool"]["note"] is None and status["unfairplay"]["note"] is None


def test_the_1x_default_spelled_out_is_the_ipatool_note_too(tmp_path, bundled):
    """`appstore.ipatool: ~/.local/bin/ipatool` (re-check #12) is passed over
    the same way, and was as invisible in the app; folded into the warning
    instead when there is one, as before."""
    cfg = AppStoreToolsConfig.from_dict({"ipatool": "~/.local/bin/ipatool",
                                         "unfairplay_models": str(bundled.models)})
    status = _status_event(cfg)
    assert status["ipatool"]["note"] == appstore.ipatool_setting_note(cfg, tools.SOURCE_BUNDLED)
    assert "no choice" in status["ipatool"]["note"] and status["unfairplay"]["note"] is None
    bundled.set(ipatool_version="2.8.1")
    ipa = appstore.status(cfg)["ipatool"]
    assert "no choice" in ipa["warning"] and ipa["note"] is None


def test_the_note_is_documented_on_the_status_event():
    from pathlib import Path

    import graft_ipa.json_output as json_output

    text = " ".join(Path(json_output.__file__).read_text().split()).replace("# ", "")
    assert "`ipatool.note` and `unfairplay.note`" in text
