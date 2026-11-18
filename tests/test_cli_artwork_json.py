"""The `--artwork` verb: `_do_artwork` / `_verb_artwork`, and the `artwork` event.

`artwork.resolve` itself is Task 5's territory (tests/test_artwork_resolve.py);
everything here patches it and checks only what the CLI layer does with the
answer — emit an event on a hit, emit nothing under `--json` on a miss, and say
something a human can read either way.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import typer

from graft_ipa import artwork as artwork_mod
from graft_ipa.artwork import Artwork
from graft_ipa.config import add_app, load_config
from graft_ipa.json_output import EVENT_TYPES, JsonEmitter


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _add_bravo(config_path: Path) -> None:
    add_app(
        config_path,
        "bravo",
        {"bundle_id": "com.bravo.app", "preferred_version": "latest", "tweaks": []},
    )


#: The app the fixture config ships, and so the default subject of these tests.
_EXAMPLE = "com.example.app"


def _artwork(
    bundle_id: str = _EXAMPLE, *, source: str = "app-store", cached: bool = False
) -> Artwork:
    """What `artwork.resolve` hands back on a hit, for `_do_artwork` to report.

    `path` is not a parameter: it is always the path `artwork_path` would
    produce for this id, because that is precisely what the `artwork` event has
    to carry and a made-up one would let a test pass while the CLI reported a
    file the GUI could never open. The one test that does want an arbitrary
    path builds its own `Artwork` — it is checking the loop survives a raising
    `resolve`, and the path never leaves the mock.
    """
    return Artwork(
        bundle_id=bundle_id,
        path=artwork_mod.artwork_path(bundle_id),
        source=source,
        cached=cached,
    )


def test_artwork_is_a_registered_event_type() -> None:
    """`JsonEmitter.emit` refuses anything unregistered, so this is load-bearing."""
    assert "artwork" in EVENT_TYPES


def test_a_hit_emits_one_artwork_event_with_exactly_four_keys(
    temp_config_env: Path,
) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    found = _artwork()
    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=found) as resolve:
        cli_mod._do_artwork(cfg, emitter=JsonEmitter(stream=buf))

    events = [e for e in _events(buf.getvalue()) if e["type"] == "artwork"]
    assert len(events) == 1
    ev = events[0]
    assert set(ev) == {"type", "bundle_id", "path", "source", "cached"}
    assert ev["bundle_id"] == "com.example.app"
    assert ev["path"] == str(found.path)
    assert isinstance(ev["path"], str)
    assert ev["source"] == "app-store"
    assert ev["cached"] is False
    assert resolve.called


def test_a_miss_emits_no_event(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None):
        cli_mod._do_artwork(cfg, emitter=JsonEmitter(stream=buf))

    assert _events(buf.getvalue()) == []


def test_a_mixed_result_emits_only_for_the_hit(temp_config_env: Path) -> None:
    """A miss must not simply be silent by accident — a real hit alongside it
    still has to make it onto the wire."""
    from graft_ipa import cli as cli_mod

    _add_bravo(temp_config_env)
    cfg = load_config(temp_config_env)

    def _resolve(bundle_id: str, *, ipa, refresh=False):
        if bundle_id == "com.bravo.app":
            return _artwork(bundle_id, source="ipa", cached=True)
        return None

    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", side_effect=_resolve):
        cli_mod._do_artwork(cfg, emitter=JsonEmitter(stream=buf))

    events = [e for e in _events(buf.getvalue()) if e["type"] == "artwork"]
    assert len(events) == 1
    assert events[0]["bundle_id"] == "com.bravo.app"


def test_app_narrows_to_the_named_app(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    _add_bravo(temp_config_env)
    cfg = load_config(temp_config_env)
    found = _artwork("com.bravo.app")
    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=found) as resolve:
        cli_mod._do_artwork(cfg, app_name="bravo", emitter=JsonEmitter(stream=buf))

    assert resolve.call_count == 1
    assert resolve.call_args.args[0] == "com.bravo.app"
    events = [e for e in _events(buf.getvalue()) if e["type"] == "artwork"]
    assert [e["bundle_id"] for e in events] == ["com.bravo.app"]


def test_an_unknown_app_name_raises_typer_exit(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with patch.object(cli_mod.artwork, "resolve") as resolve:
        with pytest.raises(typer.Exit):
            cli_mod._do_artwork(
                cfg, app_name="not-a-real-app", emitter=JsonEmitter(stream=StringIO())
            )
    assert not resolve.called, "an unknown name must fail before resolving anything"


def test_refresh_is_passed_through_to_every_resolve_call(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    _add_bravo(temp_config_env)
    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None) as resolve:
        cli_mod._do_artwork(cfg, refresh=True, emitter=JsonEmitter(stream=buf))

    assert resolve.call_count == 2
    assert all(call.kwargs.get("refresh") is True for call in resolve.call_args_list)


def test_refresh_defaults_to_false(temp_config_env: Path) -> None:
    """The mirror of the test above — a call with no explicit refresh must not
    silently mean "always refresh"."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None) as resolve:
        cli_mod._do_artwork(cfg, emitter=JsonEmitter(stream=buf))

    assert resolve.call_args.kwargs.get("refresh") is False


def test_the_non_json_path_reports_a_hit(temp_config_env: Path, capsys) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    found = _artwork(source="ipa")
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=found):
        cli_mod._do_artwork(cfg, emitter=None)

    out = capsys.readouterr().out
    assert "example" in out


def test_the_non_json_path_reports_a_miss(temp_config_env: Path, capsys) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None):
        cli_mod._do_artwork(cfg, emitter=None)

    out = capsys.readouterr().out
    assert "example" in out


def test_a_refresh_miss_with_a_cached_icon_says_so_and_keeps_it(
    temp_config_env: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`resolve` never deletes on a miss, so a stale icon survives a refresh
    that finds nothing — and the whole point of `--refresh-artwork` was to
    change something. Silence here would look identical to success."""
    from graft_ipa import cli as cli_mod

    # Wide enough that Rich cannot wrap the path across lines, same as
    # test_the_human_list_shows_a_delivery_column's COLUMNS trick — this call
    # goes straight through _do_artwork rather than CliRunner, so the width
    # has to be set on the environment Console() itself will read.
    monkeypatch.setenv("COLUMNS", "300")
    cfg = load_config(temp_config_env)
    stale = cli_mod.artwork.artwork_path("com.example.app")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale-png-bytes")

    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None):
        cli_mod._do_artwork(cfg, refresh=True, emitter=None)

    out = capsys.readouterr().out
    assert "example" in out
    assert "refresh found nothing" in out
    assert str(stale) in out
    assert "delete" in out


def test_a_refresh_miss_with_no_cached_icon_reads_as_an_ordinary_miss(
    temp_config_env: Path, capsys
) -> None:
    """No stale file to warn about — the plain "tried: ..." line is correct,
    and must not claim a cached icon that does not exist."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None):
        cli_mod._do_artwork(cfg, refresh=True, emitter=None)

    out = capsys.readouterr().out
    assert "no artwork found for" in out
    assert "refresh found nothing" not in out


def test_a_non_refresh_miss_never_mentions_a_stale_icon(
    temp_config_env: Path, capsys
) -> None:
    """The keep-cached wording is specific to an explicit refresh — an
    ordinary (non-refresh) run must not claim credit for a file it never
    looked at twice."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    stale = cli_mod.artwork.artwork_path("com.example.app")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale-png-bytes")

    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None):
        cli_mod._do_artwork(cfg, refresh=False, emitter=None)

    out = capsys.readouterr().out
    assert "refresh found nothing" not in out
    assert "no artwork found for" in out


def test_a_refresh_miss_under_json_stays_silent_even_with_a_stale_icon(
    temp_config_env: Path,
) -> None:
    """The --json contract does not change for this case: still nothing on
    the wire, matching the plain "silence is the signal" rule for any miss."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    stale = cli_mod.artwork.artwork_path("com.example.app")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"stale-png-bytes")

    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=None):
        cli_mod._do_artwork(cfg, refresh=True, emitter=JsonEmitter(stream=buf))

    assert _events(buf.getvalue()) == []


def test_an_unrecognized_source_string_is_passed_through_verbatim(
    temp_config_env: Path,
) -> None:
    """`artwork.SOURCE_UNKNOWN` is a legal value of `source` for a sidecar this
    tool did not stamp itself; the CLI has to forward it as-is rather than
    assume the three named rungs are exhaustive."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    found = _artwork(source=cli_mod.artwork.SOURCE_UNKNOWN, cached=True)
    buf = StringIO()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=found):
        cli_mod._do_artwork(cfg, emitter=JsonEmitter(stream=buf))

    ev = next(e for e in _events(buf.getvalue()) if e["type"] == "artwork")
    assert ev["source"] == "unknown"


def test_the_verb_is_wired_through_the_command_line(temp_config_env: Path) -> None:
    """`--artwork` end to end, through `main`'s dispatch — catches a misplaced
    `_VERBS` row or a `_verb_artwork` that forgot to pass `--app` through."""
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    found = _artwork()
    with patch.object(cli_mod, "find_in_cache", return_value=None), \
         patch.object(cli_mod.artwork, "resolve", return_value=found):
        result = CliRunner().invoke(cli_mod.app, ["--json", "--artwork"])

    assert result.exit_code == 0, result.output
    events = [e for e in _events(result.stdout) if e["type"] == "artwork"]
    assert events == [
        {
            "type": "artwork",
            "bundle_id": "com.example.app",
            "path": str(found.path),
            "source": "app-store",
            "cached": False,
        }
    ]


def test_the_verb_rejects_an_unknown_app_over_the_real_flag_path(
    temp_config_env: Path,
) -> None:
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    result = CliRunner().invoke(cli_mod.app, ["--artwork", "--app", "nope"])

    assert result.exit_code != 0
    assert "is not configured" in result.output


def test_one_app_failing_to_cache_does_not_abandon_the_rest(temp_config_env: Path) -> None:
    """`resolve`'s write path is the only part of that module that can raise.

    A full disk or a root-owned cache directory raises on the first app; before
    this guard the remaining apps were never looked at and the CLI exited
    non-zero, which the GUI's `try?` swallows into a screen of letter tiles
    with no explanation.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    # The fixture config ships one app; the point of this test is that the
    # loop survives an app, so it needs a second one behind the failure.
    first = next(iter(cfg.apps))
    cfg.apps["second"] = cfg.apps[first]
    names = list(cfg.apps)

    calls: list[str] = []

    def flaky(bundle_id, *, ipa, udid=None, refresh=False):
        calls.append(bundle_id)
        if len(calls) == 1:
            raise OSError(28, "No space left on device")
        return Artwork(bundle_id=bundle_id, path=Path("/c/x.png"), source="ipa", cached=False)

    buf = StringIO()
    with patch.object(cli_mod.artwork, "resolve", side_effect=flaky):
        cli_mod._do_artwork(cfg, app_name=None, refresh=False, emitter=JsonEmitter(stream=buf))

    assert len(calls) == len(names), "every app should still have been attempted"
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    artwork_events = [e for e in events if e["type"] == "artwork"]
    assert len(artwork_events) == len(names) - 1, "the survivors still emit"
