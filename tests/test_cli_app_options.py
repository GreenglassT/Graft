from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest

from graft_ipa.config import load_config
from graft_ipa.errors import ConfigError
from graft_ipa.json_output import JsonEmitter


def test_remove_app_drops_entry_and_emits_event(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    bundle_id = cfg.get_app("example").bundle_id

    buf = StringIO()
    cli_mod._do_remove_app(cfg, app_name="example", emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "app-removed")
    assert ev["name"] == "example"
    assert ev["bundle_id"] == bundle_id
    assert "example" not in load_config(temp_config_env).apps


def test_remove_app_unknown_name_raises(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with pytest.raises(ConfigError):
        cli_mod._do_remove_app(
            cfg, app_name="nosuchapp", emitter=JsonEmitter(stream=StringIO())
        )


def test_set_app_options_emits_changed_keys(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    buf = StringIO()
    cli_mod._do_set_app_options(
        cfg,
        app_name="example",
        preferred_version="21.16.2",
        display_name="Example+",
        emitter=JsonEmitter(stream=buf),
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "app-updated")
    assert ev["name"] == "example"
    assert sorted(ev["changed"]) == ["display_name", "preferred_version"]

    updated = load_config(temp_config_env).get_app("example")
    assert updated.preferred_version == "21.16.2"
    assert updated.cyan.display_name == "Example+"


def test_set_app_options_empty_display_name_reports_changed(
    temp_config_env: Path,
) -> None:
    """The bug this guards against: clearing the Display Name field in the
    GUI's EditAppSheet sends `--display-name ""`. set_app_options() responds
    by deleting the key rather than writing `""` — but the CLI still DID
    change the app, so `display_name` must appear in `changed`. If it didn't,
    Swift's ConfigStore.updateAppOptions reconciliation (which throws when a
    requested field isn't echoed back) would surface a spurious failure for
    a request that actually succeeded.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    original = cfg.get_app("example")
    assert original.cyan.display_name, "fixture must start with a real display name"

    buf = StringIO()
    cli_mod._do_set_app_options(
        cfg,
        app_name="example",
        display_name="",
        emitter=JsonEmitter(stream=buf),
    )

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    ev = next(e for e in events if e["type"] == "app-updated")
    assert ev["changed"] == ["display_name"]

    updated = load_config(temp_config_env).get_app("example")
    assert updated.cyan.display_name is None


def test_set_app_options_booleans_reach_the_cyan_block(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    cli_mod._do_set_app_options(
        cfg,
        app_name="example",
        keep_bundle_id=False,
        strip_extensions=True,
        strip_supported_devices=False,
        compression_level=9,
        emitter=JsonEmitter(stream=StringIO()),
    )

    cyan = load_config(temp_config_env).get_app("example").cyan
    assert cyan.keep_bundle_id is False
    assert cyan.strip_extensions is True
    assert cyan.strip_supported_devices is False
    assert cyan.compression_level == 9


def test_set_app_options_with_no_fields_raises(temp_config_env: Path) -> None:
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with pytest.raises(ConfigError):
        cli_mod._do_set_app_options(
            cfg,
            app_name="example",
            emitter=JsonEmitter(stream=StringIO()),
        )


def test_app_event_carries_every_cyan_field(temp_config_env: Path) -> None:
    """The GUI's edit sheet prefills from this event, so it must be complete."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--json", "--list"])
    assert result.exit_code == 0, result.output
    events = [
        json.loads(line) for line in result.stdout.splitlines()
        if line.strip().startswith("{")
    ]
    app_events = [e for e in events if e["type"] == "app"]
    assert app_events, "expected at least one app event"
    for ev in app_events:
        for key in (
            "keep_bundle_id",
            "strip_extensions",
            "strip_supported_devices",
            "compression_level",
        ):
            assert key in ev, f"app event missing {key}"


def _cli_events(result_output: str) -> list[dict]:
    return [
        json.loads(line) for line in result_output.splitlines()
        if line.strip().startswith("{")
    ]


def test_set_app_options_delivery_livecontainer_to_sideload_via_typer(
    temp_config_env: Path,
) -> None:
    """Regression test for the silent-data-loss bug: `--delivery` used to
    default to the string 'sideload' (not None), so the CLI could never tell
    "user explicitly asked for sideload" apart from "user passed nothing" —
    an explicit `--delivery sideload` on a livecontainer app was silently
    discarded. This goes through the real typer layer (unlike the
    `_do_set_app_options` calls above), which is the layer that had the bug.
    """
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    runner = CliRunner()

    # First flip example to livecontainer.
    setup = runner.invoke(
        cli_app,
        ["--json", "--set-app-options", "--app", "example", "--delivery", "livecontainer"],
    )
    assert setup.exit_code == 0, setup.output
    assert load_config(temp_config_env).get_app("example").delivery == "livecontainer"

    # Now flip it back to sideload — this is the exact repro from the bug
    # report (combined with an unrelated field change so the silent-drop
    # would otherwise hide behind a reported success).
    result = runner.invoke(
        cli_app,
        [
            "--json",
            "--set-app-options",
            "--app",
            "example",
            "--delivery",
            "sideload",
            "--compression-level",
            "9",
        ],
    )
    assert result.exit_code == 0, result.output
    events = _cli_events(result.stdout)
    updated = next(e for e in events if e["type"] == "app-updated")
    assert sorted(updated["changed"]) == ["compression_level", "delivery"]

    reloaded = load_config(temp_config_env).get_app("example")
    assert reloaded.delivery == "sideload"
    assert reloaded.cyan.compression_level == 9


def test_set_app_options_delivery_sideload_to_livecontainer_via_typer(
    temp_config_env: Path,
) -> None:
    """The other direction: the default config's example app starts as
    sideload (delivery key absent -> defaults to sideload); flipping it to
    livecontainer through typer must actually take effect."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    assert load_config(temp_config_env).get_app("example").delivery == "sideload"

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        ["--json", "--set-app-options", "--app", "example", "--delivery", "livecontainer"],
    )
    assert result.exit_code == 0, result.output
    events = _cli_events(result.stdout)
    updated = next(e for e in events if e["type"] == "app-updated")
    assert updated["changed"] == ["delivery"]
    assert load_config(temp_config_env).get_app("example").delivery == "livecontainer"


def test_set_app_options_delivery_alone_does_not_require_another_field(
    temp_config_env: Path,
) -> None:
    """`--delivery sideload` by itself used to be indistinguishable from
    passing no field flags at all (because the default was the string
    'sideload'), so it hit the "--set-app-options requires at least one
    field to change" error. It must now succeed on its own."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    runner = CliRunner()
    # Start from livecontainer so the change is real, not a same-value no-op.
    setup = runner.invoke(
        cli_app,
        ["--json", "--set-app-options", "--app", "example", "--delivery", "livecontainer"],
    )
    assert setup.exit_code == 0, setup.output

    result = runner.invoke(
        cli_app,
        ["--json", "--set-app-options", "--app", "example", "--delivery", "sideload"],
    )
    assert result.exit_code == 0, result.output
    assert "requires at least one field" not in result.output
    events = _cli_events(result.stdout)
    updated = next(e for e in events if e["type"] == "app-updated")
    assert updated["changed"] == ["delivery"]
    assert load_config(temp_config_env).get_app("example").delivery == "sideload"


def test_add_app_manual_via_typer_with_no_delivery_flag_defaults_sideload(
    temp_config_env: Path,
) -> None:
    """`--add-app-manual` with no `--delivery` at all must still default to
    sideload now that the typer option's default is None instead of the
    string "sideload"."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    runner = CliRunner()
    result = runner.invoke(
        cli_app,
        [
            "--json",
            "--add-app-manual",
            "--name",
            "typercli",
            "--bundle-id",
            "com.example.typercli",
        ],
    )
    assert result.exit_code == 0, result.output
    events = _cli_events(result.stdout)
    added = next(e for e in events if e["type"] == "app-added")
    assert added["delivery"] == "sideload"
    assert load_config(temp_config_env).get_app("typercli").delivery == "sideload"


# --- stray-flag guard -------------------------------------------------------
#
# Every flag below is consumed by exactly one verb. Typer silently drops it
# elsewhere, so without the guard the CLI reports success for a change it
# never made.

@pytest.mark.parametrize(
    "flag_args,reported_as",
    [
        (["--preferred-version", "1.2.3"], "--preferred-version"),
        (["--compression-level", "9"], "--compression-level"),
        (["--keep-bundle-id"], "--keep-bundle-id"),
        # A `--no-` negation is the same option, so it's reported under the
        # canonical positive name.
        (["--no-keep-bundle-id"], "--keep-bundle-id"),
        (["--strip-extensions"], "--strip-extensions"),
        (["--no-strip-extensions"], "--strip-extensions"),
        (["--strip-supported-devices"], "--strip-supported-devices"),
    ],
)
def test_set_app_options_only_flags_are_rejected_without_a_verb(
    temp_config_env: Path, flag_args: list[str], reported_as: str
) -> None:
    """These five were never added to the stray-flag guard, so
    `graft --compression-level 9 example` exited 0 and ran a full build
    with the flag silently discarded. The booleans default to None, so
    `is not None` correctly distinguishes "passed" from "omitted" — including
    the `--no-` forms, which must not read as "omitted"."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(cli_app, [*flag_args, "example"])
    assert result.exit_code == 2, result.output
    assert reported_as in result.output
    assert "requires --add-app" in result.output


@pytest.mark.parametrize(
    "flag_args",
    [
        ["--bundle-id", "com.new.id"],
        ["--name", "bogus"],
    ],
)
def test_set_app_options_rejects_flags_it_cannot_consume(
    temp_config_env: Path, flag_args: list[str]
) -> None:
    """`--set-app-options` used to switch the whole guard off rather than just
    the flags it consumes, so `--set-app-options --app example --bundle-id
    com.new.id --display-name Foo` exited 0 reporting only display_name
    changed — the bundle-id change was silently dropped. _do_set_app_options
    has no way to apply any of these."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app,
        ["--json", "--set-app-options", "--app", "example", *flag_args,
         "--display-name", "Foo"],
    )
    assert result.exit_code == 2, result.output
    assert f"{flag_args[0]} is not valid with --set-app-options" in result.output
    # And the change it *would* have applied must not have landed either.
    assert load_config(temp_config_env).get_app("example").cyan.display_name != "Foo"


def test_set_app_options_still_accepts_every_flag_it_consumes(
    temp_config_env: Path,
) -> None:
    """The guard is per-verb, not all-or-nothing, so the full set the GUI's
    ConfigStore.updateAppOptions sends must still go through in one call."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app,
        [
            "--json", "--set-app-options", "--app", "example",
            "--preferred-version", "21.16.2",
            "--display-name", "YT+",
            "--delivery", "livecontainer",
            "--no-keep-bundle-id",
            "--strip-extensions",
            "--no-strip-supported-devices",
            "--compression-level", "3",
        ],
    )
    assert result.exit_code == 0, result.output
    updated = next(e for e in _cli_events(result.stdout) if e["type"] == "app-updated")
    assert sorted(updated["changed"]) == [
        "compression_level", "delivery", "display_name", "keep_bundle_id",
        "preferred_version", "strip_extensions", "strip_supported_devices",
    ]

    app_cfg = load_config(temp_config_env).get_app("example")
    assert app_cfg.preferred_version == "21.16.2"
    assert app_cfg.delivery == "livecontainer"
    assert app_cfg.cyan.display_name == "YT+"
    assert app_cfg.cyan.keep_bundle_id is False
    assert app_cfg.cyan.strip_extensions is True
    assert app_cfg.cyan.strip_supported_devices is False
    assert app_cfg.cyan.compression_level == 3


def test_add_app_manual_rejects_set_app_options_only_flags(
    temp_config_env: Path,
) -> None:
    """The guard cuts both ways: --add-app-manual can't apply
    --compression-level either, and used to accept it silently."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app,
        [
            "--json", "--add-app-manual",
            "--name", "guarded",
            "--bundle-id", "com.example.guarded",
            "--compression-level", "9",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "--compression-level is not valid with --add-app-manual" in result.output
    assert "guarded" not in load_config(temp_config_env).apps


def test_add_app_manual_still_accepts_its_own_flags(temp_config_env: Path) -> None:
    """Guard against over-tightening: the exact argv the GUI's
    ConfigStore.addAppManual builds must still be accepted."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app,
        [
            "--json", "--add-app-manual",
            "--name", "okapp",
            "--bundle-id", "com.example.okapp",
            "--delivery", "livecontainer",
            "--display-name", "OK App",
        ],
    )
    assert result.exit_code == 0, result.output
    added = load_config(temp_config_env).get_app("okapp")
    assert added.delivery == "livecontainer"
    assert added.cyan.display_name == "OK App"


def test_attach_tweak_rejects_flags_it_cannot_consume(temp_config_env: Path) -> None:
    """--attach-tweak consumes only --app."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(
        cli_app,
        ["--json", "--attach-tweak", "some-tweak.deb", "--app", "example",
         "--display-name", "Nope"],
    )
    assert result.exit_code == 2, result.output
    assert "--display-name is not valid with --attach-tweak" in result.output
