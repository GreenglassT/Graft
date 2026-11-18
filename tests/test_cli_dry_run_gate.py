"""`--dry-run` is refused by every verb that would not honour it.

Typer accepts `--dry-run` alongside any verb, and only a build and
`--prune-builds` ever read it. Everywhere else it was dropped without a word —
`graft --lc-clean --dry-run` deleted LiveContainer's files for real, on a real
device, under a flag that promised it would not. A preview that is silently a
real run is worse than no preview, so the verbs that cannot preview now say so
before anything runs.

Which verbs preview is read off their handlers (a handler that takes
`dry_run` honours it), so the list the gate refuses against and the list its
message names are the same list.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa import lc_manage
from graft_ipa.cli import _VERBS, _DryRunGate, _Verb


def _invoke(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def _verbs() -> list[_Verb]:
    return [v for v in _VERBS if isinstance(v, _Verb)]


def _said(result) -> str:
    return " ".join(result.output.split())


def test_lc_clean_with_dry_run_is_refused_and_cleans_nothing(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that was found on a device: a "preview" that deleted files."""
    cleaned: list[object] = []
    monkeypatch.setattr(
        lc_manage, "run_clean", lambda *a, **k: cleaned.append((a, k))
    )

    result = _invoke(["--json", "--lc-clean", "--device", "U1", "--dry-run"])

    assert result.exit_code == 2, result.output
    assert cleaned == [], "--lc-clean ran under --dry-run"
    said = _said(result)
    assert "--dry-run is not valid with --lc-clean" in said, said
    assert "previews only a build and --prune-builds" in said, said


def test_the_refusal_comes_before_the_config_is_even_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Before anything runs" includes the load: `load_config` sweeps stale
    partials out of the cache, and the migration moves directories. A config
    that does not parse proves the load never happened."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    broken = tmp_path / "cfg" / "graft" / "config.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("this: is: not: valid: yaml:\n")

    result = _invoke(["--lc-clean", "--dry-run"])

    assert result.exit_code == 2, result.output
    assert "failed to parse" not in result.output
    assert "--dry-run is not valid with --lc-clean" in _said(result)


def test_the_verbs_that_preview_are_the_ones_whose_handlers_take_dry_run() -> None:
    """Pinned, so a verb gaining or losing a preview is a decision somebody
    makes here rather than something the gate notices for them."""
    assert {v.flag for v in _verbs() if v.honours_dry_run} == {"--prune-builds"}
    for v in _verbs():
        assert v.honours_dry_run == ("dry_run" in inspect.signature(v.run).parameters)


def _opts_selecting(verb: _Verb, *, dry_run: bool) -> dict:
    # Everything unset (None reads as "not passed" to every step's `selected`)
    # except the one verb.
    opts = dict.fromkeys(inspect.signature(cli_mod.main).parameters)
    opts[verb.param] = True
    opts["dry_run"] = dry_run
    return opts


@pytest.mark.parametrize("verb", _verbs(), ids=lambda v: v.flag)
def test_every_verb_that_cannot_preview_is_refused_first(verb: _Verb) -> None:
    """Over the whole table, so a destructive verb added later is covered the
    day it lands. The gate has to be what the walk selects — first, ahead of
    the verb itself and of anything that touches disk."""
    opts = _opts_selecting(verb, dry_run=True)
    selected = next(step for step in _VERBS if step.selected(opts))

    if verb.honours_dry_run:
        assert selected is verb
    else:
        assert isinstance(selected, _DryRunGate), f"{verb.flag} ran under --dry-run"

    # And without --dry-run the gate stays out of the way.
    opts = _opts_selecting(verb, dry_run=False)
    assert next(step for step in _VERBS if step.selected(opts)) is verb


def test_the_gate_neither_loads_a_config_nor_migrates() -> None:
    gate = next(step for step in _VERBS if isinstance(step, _DryRunGate))
    assert _VERBS.index(gate) == 0, "a verb above the gate runs before it is checked"
    assert gate.needs is cli_mod._Needs.NOTHING
    assert gate.migrates is False


def test_prune_builds_still_previews(temp_config_env: Path, tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # nothing may reach the real ~/.Trash
    result = _invoke(["--json", "--prune-builds", "--dry-run"])

    assert result.exit_code == 0, result.output
    ends = [json.loads(line) for line in result.stdout.splitlines()
            if line.startswith("{") and '"prune-end"' in line]
    assert ends and ends[0]["dry_run"] is True


def test_a_build_still_previews(temp_config_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No verb is a build, and a build honours --dry-run: the gate must not
    read "no verb selected" as "a verb that cannot preview"."""
    seen: list[bool] = []
    monkeypatch.setattr(
        cli_mod, "_build_one", lambda **kw: seen.append(kw["dry_run"])
    )

    result = _invoke(["--json", "--dry-run", "example"])

    assert result.exit_code == 0, result.output
    assert seen == [True]
    assert "is not valid with" not in result.output
