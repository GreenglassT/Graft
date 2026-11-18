"""Options must parse the same whether they precede or follow the app name.

This is a safety test, not a convenience one. `invoke_without_command=True`
makes Typer build a Click *Group*, and Click sets `allow_interspersed_args`
False on Groups so a group cannot steal its subcommands' options. This app
registers no subcommands, so that protection guarded nothing and cost real
safety: `graft example --dry-run` parsed `--dry-run` as a second app
name, leaving `dry_run` False. It ran a real build and a real signed install of
example, then failed on an app called "--dry-run". The one flag whose entire
purpose is not touching the device was the flag the device got.

These assert on the parsed parameters rather than on behaviour, because the
behaviour under test is "does not install", and a test that has to reach the
install path to prove an install didn't happen is a test one bad refactor away
from installing.
"""

from __future__ import annotations

import typer.main

from graft_ipa.cli import app as typer_app


def _params(argv: list[str]) -> dict:
    """Click's parsed parameters, with `apps` as a list whatever Click made it.

    Click 8.4 hands a variadic argument back as a tuple and 8.5 as a list.
    What these tests pin is which tokens became app names and in what order —
    not the container — so it is normalised here once rather than in every
    assertion. `main` itself reads `locals()`, where Typer has already made it
    a list on both versions.
    """
    cmd = typer.main.get_command(typer_app)
    params = cmd.make_context("graft", list(argv), resilient_parsing=True).params
    params["apps"] = list(params["apps"])
    return params


def test_a_trailing_dry_run_is_a_flag_not_an_app_name() -> None:
    """The exact invocation that used to perform a real signed install."""
    params = _params(["example", "--dry-run"])

    assert params["dry_run"] is True
    assert params["apps"] == ["example"]


def test_a_leading_dry_run_still_works() -> None:
    """The ordering the GUI uses. It was always correct; keep it that way."""
    params = _params(["--dry-run", "example"])

    assert params["dry_run"] is True
    assert params["apps"] == ["example"]


def test_a_trailing_option_does_not_swallow_its_own_value() -> None:
    """`--delivery sideload` after the app name consumed BOTH tokens as apps."""
    params = _params(["example", "--delivery", "sideload"])

    assert params["apps"] == ["example"]


def test_several_app_names_are_still_several_app_names() -> None:
    """The variadic argument has to keep working — that is what made this subtle.

    Turning interspersed args on must not make the parser start treating the
    second positional as something other than an app.
    """
    params = _params(["--dry-run", "example", "bravo"])

    assert params["dry_run"] is True
    assert params["apps"] == ["example", "bravo"]


def test_an_option_between_two_app_names_binds_as_an_option() -> None:
    params = _params(["example", "--dry-run", "bravo"])

    assert params["dry_run"] is True
    assert params["apps"] == ["example", "bravo"]
