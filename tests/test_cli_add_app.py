from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from graft_ipa.config import load_config
from graft_ipa.itunes import ITunesApp
from graft_ipa.json_output import JsonEmitter


def test_add_app_url_writes_entry_and_emits_event(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake = ITunesApp(
        track_id=100000001,
        bundle_id="com.bravo.app",
        name="Bravo",
        version="2025.20.0",
    )
    buf = StringIO()
    with patch.object(cli_mod.itunes, "lookup", return_value=fake):
        cli_mod._do_add_app_url(
            cfg,
            "https://apps.apple.com/us/app/bravo/id100000001",
            emitter=JsonEmitter(stream=buf),
        )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    added = next(e for e in events if e["type"] == "app-added")
    assert added["name"] == "bravo"
    assert added["bundle_id"] == "com.bravo.app"
    assert added["display_name"] == "Bravo"
    assert added["delivery"] == "sideload"
    # The event's whole field set, so a field for a removed download source
    # cannot come back unnoticed.
    assert set(added) == {"type", "name", "bundle_id", "display_name", "delivery"}

    reloaded = load_config(temp_config_env)
    bravo = reloaded.get_app("bravo")
    assert bravo.bundle_id == "com.bravo.app"
    assert bravo.cyan.display_name == "Bravo"
    # An App Store URL identifies the app; it is not a download source, so
    # nothing but the bundle id, the store the URL names (tests/
    # test_store_country.py) and the defaults reaches config.yaml.
    raw = yaml.safe_load(temp_config_env.read_text())["apps"]["bravo"]
    assert set(raw) == {
        "bundle_id", "store_country", "preferred_version", "delivery", "tweaks", "cyan",
    }
    assert raw["store_country"] == "US"


def test_add_app_url_dedupes_colliding_slug(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake = ITunesApp(
        track_id=100000002,
        bundle_id="com.alt.example",
        name="Example",  # collides with the default 'example' app
        version="1.0",
    )
    buf = StringIO()
    with patch.object(cli_mod.itunes, "lookup", return_value=fake):
        cli_mod._do_add_app_url(
            cfg,
            "https://apps.apple.com/us/app/example/id100000002",
            emitter=JsonEmitter(stream=buf),
        )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    added = next(e for e in events if e["type"] == "app-added")
    assert added["name"] == "example_2"
    reloaded = load_config(temp_config_env)
    assert reloaded.get_app("example_2").bundle_id == "com.alt.example"
    # The original `example` entry is untouched.
    assert reloaded.get_app("example").bundle_id == "com.example.app"


def test_add_app_url_propagates_itunes_error(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.itunes import ITunesError

    cfg = load_config(temp_config_env)
    with patch.object(cli_mod.itunes, "lookup", side_effect=ITunesError("no app found")):
        with pytest.raises(ITunesError):
            cli_mod._do_add_app_url(
                cfg,
                "https://apps.apple.com/us/app/x/id999999999",
                emitter=JsonEmitter(stream=StringIO()),
            )
    # No app persisted on failure.
    assert "x" not in load_config(temp_config_env).apps


def test_add_app_manual_minimal_fields(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_add_app_manual(
        cfg,
        name="customapp",
        bundle_id="com.example.customapp",
        display_name=None,
        delivery="sideload",
        emitter=JsonEmitter(stream=buf),
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    added = next(e for e in events if e["type"] == "app-added")
    assert added["name"] == "customapp"
    assert added["bundle_id"] == "com.example.customapp"
    assert added["display_name"] == "customapp"  # falls back to slug
    assert added["delivery"] == "sideload"
    assert set(added) == {"type", "name", "bundle_id", "display_name", "delivery"}

    reloaded = load_config(temp_config_env)
    app = reloaded.get_app("customapp")
    assert app.bundle_id == "com.example.customapp"


def test_add_app_manual_with_all_fields(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_add_app_manual(
        cfg,
        name="MyApp",  # not yet slugified — handler slugifies
        bundle_id="com.example.myapp",
        display_name="My Cool App",
        delivery="livecontainer",
        emitter=JsonEmitter(stream=buf),
    )

    added = next(
        json.loads(line)
        for line in buf.getvalue().splitlines()
        if json.loads(line)["type"] == "app-added"
    )
    assert added["name"] == "myapp"
    assert added["display_name"] == "My Cool App"
    assert added["delivery"] == "livecontainer"

    app = load_config(temp_config_env).get_app("myapp")
    assert app.delivery == "livecontainer"
    assert app.cyan.display_name == "My Cool App"


def test_add_app_manual_rejects_bad_delivery(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import ConfigError

    cfg = load_config(temp_config_env)
    with pytest.raises(ConfigError):
        cli_mod._do_add_app_manual(
            cfg,
            name="x",
            bundle_id="com.example.x",
            display_name=None,
            delivery="weirdmode",
            emitter=JsonEmitter(stream=StringIO()),
        )


def test_add_app_url_delivery_flag_flows_through(temp_config_env: Path) -> None:
    """`--delivery livecontainer` on the URL path must reach app_cfg, not get
    silently overridden to sideload."""
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    fake = ITunesApp(
        track_id=100000001,
        bundle_id="com.bravo.app",
        name="Bravo",
        version="1.0",
    )
    buf = StringIO()
    with patch.object(cli_mod.itunes, "lookup", return_value=fake):
        cli_mod._do_add_app_url(
            cfg,
            "https://apps.apple.com/us/app/bravo/id100000001",
            delivery="livecontainer",
            emitter=JsonEmitter(stream=buf),
        )
    added = next(
        json.loads(line)
        for line in buf.getvalue().splitlines()
        if json.loads(line)["type"] == "app-added"
    )
    assert added["delivery"] == "livecontainer"
    assert load_config(temp_config_env).get_app("bravo").delivery == "livecontainer"


def test_add_app_url_rejects_bad_delivery(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod
    from graft_ipa.errors import ConfigError

    cfg = load_config(temp_config_env)
    with pytest.raises(ConfigError):
        cli_mod._do_add_app_url(
            cfg,
            "https://apps.apple.com/us/app/x/id1",
            delivery="weirdmode",
            emitter=JsonEmitter(stream=StringIO()),
        )


@pytest.mark.parametrize(
    ("argv", "flag"),
    [
        (["--add-app-manual", "--name", "x", "--bundle-id", "com.example.x",
          "--app-store-id", "1"], "--app-store-id"),
        (["--force-redownload", "example"], "--force-redownload"),
    ],
)
def test_the_flags_that_only_reached_a_download_source_are_gone(
    temp_config_env: Path, argv: list[str], flag: str
) -> None:
    """`--app-store-id` only ever wrote a download URL into the app entry and
    `--force-redownload` only ever bypassed the cache to download again. Both
    are unknown options now, refused by the parser before anything runs."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    before = temp_config_env.read_bytes()
    result = CliRunner().invoke(cli_app, argv)
    assert result.exit_code == 2, result.output
    assert f"No such option: {flag}" in result.output
    assert temp_config_env.read_bytes() == before
