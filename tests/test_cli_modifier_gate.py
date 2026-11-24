"""Review #7 and #8: a global modifier a verb ignores is refused, not dropped.

Typer accepts positional app names, `--device`, `--skip-install`,
`--for-playcover`, `--skip-decrypt-check` and `--fetch-sha` beside any verb,
and only the handlers that take the parameter read it. So `graft example
--prune-builds --keep 0` trashed every app's archives, `--reinstall-build 5
--device B` installed on the phone the build was recorded for, `--lc-clean
--skip-install` still wrote to the device, and `--update-tweak X --fetch-sha H`
never checked H. `_ModifierGate` refuses each, before any config load.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa.config import load_config


def _run(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _opts(**given) -> dict:
    """Every `main` parameter at its default, with `given` on top."""
    opts = {
        name: getattr(p.default, "default", p.default)
        for name, p in inspect.signature(cli_mod.main).parameters.items()
    }
    opts.update(given)
    return opts


def test_app_names_beside_prune_builds_are_refused_and_nothing_moves(
    temp_config_env: Path,
) -> None:
    cfg = load_config(temp_config_env)
    cfg.built_dir.mkdir(parents=True, exist_ok=True)
    for app in ("example", "other"):
        (cfg.built_dir / f"{app}_1.0_20261119T100000.ipa").write_bytes(b"x")

    result = _run(["example", "--prune-builds", "--keep", "0"])

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert "app names (example) is not valid with --prune-builds" in said
    assert "every app's archives" in said
    assert sorted(p.name for p in cfg.built_dir.iterdir()) == [
        "example_1.0_20261119T100000.ipa", "other_1.0_20261119T100000.ipa",
    ]


def test_device_beside_reinstall_build_is_refused(temp_config_env: Path) -> None:
    result = _run(["--json", "--reinstall-build", "1", "--device", "00008130-PHONE-B"])

    assert result.exit_code == 2, result.output
    said = " ".join(result.stderr.split())
    assert "--device is not valid with --reinstall-build" in said
    assert "recorded for" in said
    assert result.stdout == "", "a refusal under --json prints no events"


@pytest.mark.parametrize(
    ("args", "named"),
    [
        (["--lc-clean", "--skip-install", "--device", "U1"], "--skip-install"),
        (["--reinstall-build", "1", "--skip-install"], "--skip-install"),
        (["--update-tweak", "x.deb", "--fetch-sha", "ab" * 32], "--fetch-sha"),
        (["--check", "--skip-decrypt-check"], "--skip-decrypt-check"),
        (["example", "--fetch-sha", "ab" * 32], "--fetch-sha"),
    ],
)
def test_each_ignored_modifier_is_named(temp_config_env: Path, args: list[str], named: str) -> None:
    result = _run(args)

    assert result.exit_code == 2, result.output
    assert f"{named} is not valid with" in " ".join(result.stderr.split())


def test_two_ignored_modifiers_are_named_together(temp_config_env: Path) -> None:
    result = _run(["--reinstall-build", "1", "--device", "U", "--skip-install"])

    assert "--device, --skip-install are not valid with --reinstall-build" in " ".join(
        result.stderr.split()
    )


@pytest.mark.parametrize(
    "given",
    [
        {"apps": ["example"], "device": "U", "skip_install": True, "for_playcover": True,
         "skip_decrypt_check": True},
        {"fetch": "https://example.invalid/x.deb", "fetch_sha": "ab" * 32},
        {"lc_clean": True, "device": "U"},
        {"reconcile_flag": True, "device": "U"},
        {"decrypt": "com.example.app", "device": "U"},
    ],
)
def test_a_verb_that_reads_the_modifier_is_let_through(given: dict) -> None:
    assert not cli_mod._ModifierGate().selected(_opts(**given))


def test_every_verb_that_reads_device_may_be_given_it() -> None:
    """Read off the handler: a verb that learns to take `device` is let
    through without a second list to update."""
    for verb in cli_mod._VERBS:
        if isinstance(verb, cli_mod._Verb) and "device" in inspect.signature(verb.run).parameters:
            opts = _opts(device="U")
            opts[verb.param] = "x" if opts[verb.param] is None else True
            assert not cli_mod._ModifierGate().selected(opts), verb.flag


def test_the_refusal_comes_before_a_config_load(tmp_path: Path) -> None:
    """Like `_DryRunGate`: no config is needed to be told no."""
    missing = tmp_path / "nowhere" / "config.yaml"

    result = _run(["--config", str(missing), "--reinstall-build", "1", "--device", "U"])

    assert result.exit_code == 2
    said = " ".join(result.stderr.split())
    assert "not valid with --reinstall-build" in said
    assert "not found" not in said

