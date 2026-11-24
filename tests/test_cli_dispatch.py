"""`main` dispatches from `_VERBS`, and the order in that tuple is behavior.

The body used to be forty-nine `if`s. Three things about their *sequence* were
load-bearing and none of them was written down:

  * some verbs run without a loaded config — four need none at all, because
    the GUI's contract test introspects `--describe` on a machine that has
    never run `--init`, and two need the path unvalidated, because a config too
    broken to load is the one the settings editor exists to repair;
  * the verb-only flag guard sits in the middle, so the seven verbs above it
    ignore flags the verbs below it reject;
  * the guard picks the verb to judge against from its own ordering, which is
    not always the verb that ends up running.

Converting the chain to a table makes all three visible, and moves the risk: a
table can be reordered, mistyped, or given a handler that asks for an option the
CLI does not have — none of which the compiler or ruff would notice, and the
first two of which are silent at runtime until somebody passes that exact flag.
These are the assertions that make the table as safe as the chain was obvious.
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import typer.main
import yaml
from typer.testing import CliRunner

from graft_ipa import cli as cli_mod
from graft_ipa.cli import _VERB_ONLY_FLAGS, _VERBS, _Needs, _StrayFlagGate, _Verb
from tests.cli_ast import cli_tree


def _verbs() -> list[_Verb]:
    return [v for v in _VERBS if isinstance(v, _Verb)]


def _main_parameters() -> set[str]:
    return set(inspect.signature(cli_mod.main).parameters)


def _cli_flags() -> set[str]:
    """Every option spelling Click will accept, including `--no-` halves."""
    command = typer.main.get_command(cli_mod.app)
    return {opt for p in command.params for opt in (*p.opts, *p.secondary_opts)}


# Options on `main` that are deliberately not verbs, with the reason each one
# is not. This is the other half of the partition below, and it is a dict rather
# than a set for the reason `CLI_ONLY_EVENTS` is: the whole failure this guards
# against is nobody having to write anything down. A new option lands here or in
# `_VERBS`, and either way somebody has decided which.
_NON_VERB_PARAMS: dict[str, str] = {
    "ctx": "Typer's context object, not an option at all.",
    "apps": (
        "The positional argument. It is what the walk falls through *to*, so a "
        "row in `_VERBS` selecting on it would make every build take the verb "
        "path and `_verb_build_apps` unreachable. Beside a verb, which never "
        "reads it, `_ModifierGate` refuses it."
    ),
    "config_path": (
        "`--config` says where the config is, not which verb runs. `main` "
        "resolves it to a real path once and hands it to `load_config` for the "
        "verbs that need a loaded one and straight to the handler for the "
        "`_Needs.PATH` verbs that read the YAML themselves."
    ),
    "json_mode": (
        "`--json` changes how every verb reports, not which one runs. It is the "
        "one flag in the signature that all 31 handlers read."
    ),
    "init_account": (
        "`--account` qualifies `--init`: free or paid, and no questions. Not in "
        "`_VERB_ONLY_FLAGS`: `--init` sits above the stray-flag gate, which needs a "
        "config `--init` is there to create, and no verb above the gate may consume a "
        "verb-only flag. `_ModifierGate` refuses it beside anything else, before any "
        "config load."
    ),
    "init_p12": "`--p12` qualifies `--init --account paid`; refused elsewhere like `--account`.",
    "init_profile": "`--profile` qualifies `--init --account paid`; refused elsewhere like `--account`.",
    "init_replace": (
        "`--replace` qualifies `--init --account paid` on a paid config: a new certificate "
        "and profile. Refused elsewhere like `--account`."
    ),
    "current_version": (
        "`--current-version` qualifies `--check-update`: the version to compare against. "
        "Not in `_VERB_ONLY_FLAGS` for `--account`'s reason: `--check-update` needs no "
        "config and sits above the gate. `_ModifierGate` refuses it beside anything else."
    ),
    "app_path": (
        "`--app-path` qualifies `--download-update` and `--install-update`: the Graft.app "
        "to replace. Both sit above the gate for `--check-update`'s reason, so "
        "`_ModifierGate` refuses it beside anything else."
    ),
    "wait_pid": "`--wait-pid` qualifies `--install-update`; refused elsewhere like `--app-path`.",
    "relaunch": "`--relaunch` qualifies `--install-update`; refused elsewhere like `--app-path`.",
    "dry_run": (
        "A build modifier, read by `_verb_build_apps`, `_verb_prune_builds` and "
        "`_verb_init` (with `--account`). "
        "Not in `_VERB_ONLY_FLAGS`: it has a default of False, and the verbs "
        "that may take it are read off their handlers (`_Verb.honours_dry_run`) "
        "by `_DryRunGate`, which refuses it everywhere else."
    ),
    "skip_install": (
        "A build modifier: stop after signing rather than pushing to a device. "
        "`_ModifierGate` refuses it beside a verb, none of which reads it."
    ),
    "for_playcover": (
        "A build modifier: also make the artifact loadable on this Mac. Not a "
        "verb-only flag — those *require* one of their verbs, and putting it "
        "there made every other verb refuse to run."
    ),
    "skip_decrypt_check": "A build modifier: skip the stage 2.5 cryptid check.",
    "verbose": "A build modifier: more output, and the temp directory is kept.",
    "device": (
        "Names the target device for a build or a `--reconcile`. Global rather "
        "than verb-only — several verbs read it and none is selected by it, "
        "which is why it is deliberately absent from `_Verb.accepts` too. "
        "`_ModifierGate` refuses it beside a verb whose handler doesn't take it."
    ),
    "fetch_sha": (
        "`--fetch-sha` qualifies `--fetch`'s download and is verified after it. "
        "Not in `_VERB_ONLY_FLAGS`, whose flags need a verb: a build is "
        "refused it too. `_ModifierGate` refuses it anywhere `--fetch` isn't "
        "what runs, since a checksum nobody checks reads as one that passed."
    ),
    "refresh": "`--refresh` qualifies `--repo-packages`: re-fetch the index before listing.",
    "refresh_artwork": (
        "`--refresh-artwork` qualifies two verbs — `--artwork`, and "
        "`--device-apps --with-icons` — with the same idea: ignore the artwork "
        "cache and resolve again. Not in `_VERB_ONLY_FLAGS` for the same reason "
        "`refresh` isn't — passing it without either is inert, not a lie about "
        "what happened, so there is nothing for the stray-flag gate to reject. "
        "That it qualifies more than one verb is a second reason: `accepts` is "
        "declared per verb, so a flag two verbs share would have to be listed "
        "twice to stop being rejected under one of them."
    ),
    "builds_app": "`--builds-app` narrows `--builds` output to one app.",
    "builds_limit": "`--builds-limit` caps `--builds` output; it has a default, never None.",
}


# ---------------------------------------------------------------------------
# The table describes the CLI that actually exists
# ---------------------------------------------------------------------------


def test_every_verb_selects_on_a_parameter_main_really_has() -> None:
    """`param` is looked up in a dict, so a typo is a KeyError — and only when
    somebody passes that verb's flag, which may be months later."""
    missing = {v.flag: v.param for v in _verbs() if v.param not in _main_parameters()}
    assert not missing, f"these verbs select on a parameter `main` does not have: {missing}"


def test_every_option_main_has_is_a_verb_or_declared_not_to_be() -> None:
    """A partition, not a subset — the direction the same diff already learned.

    The check above says `_VERBS ⊆ main`'s parameters, and the one below says
    `_VERBS ⊆ Click's flags`. Neither says anything about the opposite and more
    common direction, which is `test_cli_event_contract`'s whole argument: a
    subset check is silent about extras. Add a verb flag to `main` and forget
    the `_VERBS` row and Click accepts it, no test fails, the walk falls through
    — and `graft --new-verb` either exits 64 or, with app names also
    present (which is how the GUI builds argv), runs a plain build and discards
    the verb without a word.

    So every option is accounted for exactly once:

        main's parameters == _VERBS | _VERB_ONLY_FLAGS | _NON_VERB_PARAMS
    """
    verbs = {v.param for v in _verbs()}
    verb_only = set(_VERB_ONLY_FLAGS.values())
    declared = verbs | verb_only | set(_NON_VERB_PARAMS)

    undeclared = sorted(_main_parameters() - declared)
    assert not undeclared, (
        f"`main` has {len(undeclared)} option(s) nothing accounts for: {undeclared}. "
        "If one selects a verb it needs a row in `_VERBS` — without it the walk "
        "falls through and the flag is silently discarded. If it qualifies a verb "
        "it belongs in `_VERB_ONLY_FLAGS`. If it is neither, say so in "
        "`_NON_VERB_PARAMS` with the reason."
    )

    stale = sorted(set(_NON_VERB_PARAMS) - _main_parameters())
    assert not stale, (
        f"_NON_VERB_PARAMS excuses {stale}, which `main` no longer has — an "
        "argument about an option that is gone is one nobody will read"
    )

    overlap = sorted(set(_NON_VERB_PARAMS) & (verbs | verb_only))
    assert not overlap, (
        f"{overlap} are declared 'not a verb' and are also selecting or "
        "qualifying one. Whichever is wrong, the partition above would still "
        "hold: the overlap makes the union too big, never too small."
    )

    for param, reason in _NON_VERB_PARAMS.items():
        assert reason.strip(), f"_NON_VERB_PARAMS[{param!r}] needs an argument, not a key"


def test_no_two_verbs_select_on_the_same_parameter() -> None:
    """First match wins, so a duplicate is a verb that can never run."""
    seen: dict[str, str] = {}
    clashes = []
    for v in _verbs():
        if v.param in seen:
            clashes.append(f"{v.flag} and {seen[v.param]} both select on {v.param}")
        seen[v.param] = v.flag
    assert not clashes, "; ".join(clashes)


def test_every_verb_names_a_flag_the_cli_accepts() -> None:
    """`flag` is what a diagnostic calls the verb. The parsed value cannot tell
    you which spelling the user typed, so it is written down here — and a
    written-down spelling drifts."""
    unknown = sorted(v.flag for v in _verbs() if v.flag not in _cli_flags())
    assert not unknown, f"these verbs name a flag Click does not accept: {unknown}"


def test_every_verb_only_flag_maps_to_a_real_flag_and_a_real_parameter() -> None:
    flags, params = _cli_flags(), _main_parameters()
    bad = {
        flag: param
        for flag, param in _VERB_ONLY_FLAGS.items()
        if flag not in flags or param not in params
    }
    assert not bad, f"_VERB_ONLY_FLAGS entries that no longer exist: {bad}"


def test_no_verb_accepts_a_flag_that_is_not_verb_only() -> None:
    """`accepts` is checked against `_VERB_ONLY_FLAGS` and nothing else, so a
    flag listed there but not guarded is a line that reads like a permission and
    grants nothing."""
    stray = {
        v.flag: sorted(v.accepts - set(_VERB_ONLY_FLAGS))
        for v in _verbs()
        if v.accepts - set(_VERB_ONLY_FLAGS)
    }
    assert not stray, f"accepted flags that are not in _VERB_ONLY_FLAGS: {stray}"


def _implies_json_mode(test: ast.expr, *, when_true: bool) -> bool:
    """Does `test` evaluating to `when_true` prove `json_mode` was set?

    The question the previous version asked was "does `json_mode` appear
    anywhere in this test", and the answer was handed to the `body` *and* the
    `orelse`. So both halves of a conditional counted as guarded, and

        emitter = None if json_mode else JsonEmitter()

    — verb 31 copied into verb 32 with the ternary inverted, which is an easy
    slip since the true-branch is the emitter at all 22 existing sites —
    scored as guarded while doing the exact opposite: newline-delimited JSON
    on a human's stdout and `None` under `--json`. The docstring below claims a
    `JsonEmitter` is only constructed *under* a `json_mode` test; what was
    actually held was "somewhere inside a statement that mentions `json_mode`".

    So the branch is what carries the guard, and the sign is tracked. Sound
    rather than complete: anything this cannot prove is left unguarded, which
    fails loudly and is fixable by spelling the test the plain way.
    """
    if isinstance(test, ast.Name):
        return test.id == "json_mode" and when_true
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        return _implies_json_mode(test.operand, when_true=not when_true)
    if isinstance(test, ast.BoolOp):
        # `a and b` true ⇒ every operand true; `a or b` false ⇒ every operand
        # false. The other direction of each proves nothing about any operand.
        wanted = isinstance(test.op, ast.And)
        if when_true is not wanted:
            return False
        return any(_implies_json_mode(v, when_true=when_true) for v in test.values)
    return False


def unguarded_json_emitters(fn: ast.FunctionDef) -> list[int]:
    """Line numbers of `JsonEmitter()` calls in `fn` not proven to be under --json."""
    out: list[int] = []

    def walk(node: ast.AST, guarded: bool) -> None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "JsonEmitter"
            and not guarded
        ):
            out.append(node.lineno)
        if isinstance(node, ast.IfExp | ast.If):
            walk(node.test, guarded)
            body = [node.body] if isinstance(node, ast.IfExp) else node.body
            orelse = [node.orelse] if isinstance(node, ast.IfExp) else node.orelse
            for child in body:
                walk(child, guarded or _implies_json_mode(node.test, when_true=True))
            for child in orelse:
                walk(child, guarded or _implies_json_mode(node.test, when_true=False))
            return
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    for stmt in fn.body:
        walk(stmt, False)
    return out


def test_no_handler_can_emit_json_at_a_human() -> None:
    """`emitter=JsonEmitter() if json_mode else None`, copy-pasted 22 times.

    The duplication is the smaller half. The hazard is that verb 32 is written
    by copying verb 31 and dropping the `if json_mode`, which puts a JSON event
    stream on a terminal user's stdout — and nothing would fail, because the
    suite exercises most verbs in exactly one of the two modes.

    Hoisting the ternary into `main` would remove the shape entirely, and it is
    the better fix; it also moves *when* the object is constructed across 25
    call sites in one pass. This asserts the property that refactor would give
    for free, and it holds whether or not the refactor ever happens: inside a
    `_verb_*` handler, a `JsonEmitter` may only be constructed on the branch
    that runs when `json_mode` is set.
    """
    offenders: list[str] = []
    for fn in ast.walk(cli_tree()):
        if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("_verb_"):
            continue
        offenders += [f"{fn.name} (cli.py:{line})" for line in unguarded_json_emitters(fn)]

    assert not offenders, (
        "these construct a JsonEmitter without checking --json, so the verb "
        "writes newline-delimited JSON onto a human's stdout: " + "; ".join(offenders)
    )


def _guard_check(body: str) -> list[int]:
    fn = ast.parse(f"def _verb_x(*, json_mode, cfg):\n{body}").body[0]
    assert isinstance(fn, ast.FunctionDef)
    return unguarded_json_emitters(fn)


def test_the_guard_is_not_satisfied_by_merely_mentioning_json_mode() -> None:
    """The evasion the assertion above was written to stop and did not.

    Both halves of a `json_mode` conditional used to count as guarded, so the
    inverted ternary — the single most likely way to get this wrong — passed
    while doing precisely the thing the message describes.
    """
    assert _guard_check("    e = None if json_mode else JsonEmitter()\n") == [2]
    assert _guard_check(
        "    if json_mode:\n        e = None\n    else:\n        e = JsonEmitter()\n"
    ) == [5]


def test_the_guard_still_accepts_every_way_of_spelling_it_correctly() -> None:
    """A guard nobody can satisfy gets deleted, so the true-branch forms —
    including the negated test, where the emitter legitimately sits in the
    `else` — have to keep passing."""
    assert _guard_check("    e = JsonEmitter() if json_mode else None\n") == []
    assert _guard_check("    if json_mode:\n        e = JsonEmitter()\n") == []
    assert _guard_check(
        "    if not json_mode:\n        e = None\n    else:\n        e = JsonEmitter()\n"
    ) == []
    assert _guard_check("    e = None if not json_mode else JsonEmitter()\n") == []
    assert _guard_check("    if json_mode and cfg:\n        e = JsonEmitter()\n") == []


def test_the_option_bundle_is_a_copy_of_the_frame() -> None:
    """`opts = locals()` is a live view of `main`'s frame on 3.11 and 3.12.

    Before PEP 667 (3.13) `locals()` inside a function returns the frame's
    `f_locals` snapshot dict, and the interpreter *refreshes that same dict in
    place* every time the frame is materialised again — a nested
    `locals()`/`vars()`, `inspect.currentframe().f_locals`, a pdb session, a
    tracing profiler. Anything doing that between the assignment and the
    dispatch adds `cfg` to `opts`, and `verb.run(cfg=cfg, **opts)` raises
    `TypeError: got multiple values for keyword argument 'cfg'` — not one verb
    failing, every CLI invocation dying with a traceback, and with it the GUI,
    which spawns this CLI for everything it does.

    `pyproject.toml` declares `requires-python = ">=3.11"`, so those are
    supported interpreters. This suite runs on 3.13, where `locals()` is
    already a copy — which is exactly why the bug was invisible and why this
    test is over the source rather than over the behavior. There is no
    runtime assertion available here that would fail on 3.13; the spelling is
    the only thing that distinguishes the two, so the spelling is what gets
    pinned.
    """
    main_fn = next(
        n for n in ast.walk(cli_tree())
        if isinstance(n, ast.FunctionDef) and n.name == "main"
    )
    assignments = [
        node
        for node in ast.walk(main_fn)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "opts" for t in node.targets)
    ]
    assert len(assignments) == 1, "`opts` is assigned somewhere other than the one place"
    value = assignments[0].value

    assert not (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                and value.func.id == "locals"), (
        "`opts = locals()` aliases main's frame on every interpreter before "
        "3.13. Copy it — `dict(locals())` — so nothing that materialises the "
        "frame later can add `cfg` to the bundle it is passed alongside."
    )
    assert any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "locals"
        for n in ast.walk(value)
    ), (
        "`opts` no longer comes from `locals()` at all. If it is now built by "
        "hand, every option has to be listed — and the partition above only "
        "proves each option is *accounted for*, not that it is passed on."
    )


def test_every_handler_can_be_called_with_the_whole_option_bundle() -> None:
    """`main` calls `verb.run(cfg=cfg, **opts)`; a handler that declares an
    option by a name `main` does not use raises TypeError — but only on the one
    invocation that selects it, which is exactly the coverage a rarely-used verb
    does not have. Bound rather than called, so nothing runs.
    """
    opts = dict.fromkeys(_main_parameters())
    problems = []
    for run in [v.run for v in _verbs()] + [cli_mod._verb_build_apps]:
        try:
            inspect.signature(run).bind(cfg=None, **opts)
        except TypeError as e:
            problems.append(f"{run.__name__}: {e}")
    assert not problems, (
        "these handlers cannot be called the way `main` calls them: " + "; ".join(problems)
    )


# ---------------------------------------------------------------------------
# The order
# ---------------------------------------------------------------------------


def test_the_verbs_that_need_nothing_are_the_ones_that_have_to() -> None:
    """--describe is introspected by the GUI's contract test on a machine that
    has no config.yaml, the app runs --check-update before setup is done, and
    --init is what creates one. Anything else reaching
    `_Needs.NOTHING` would be a verb silently running against no config at all
    rather than reporting that it needs one.

    An exact set, restored: while this was a boolean the settings verbs had to
    be listed here too, under a docstring claiming they ran "against no config
    at all" — which they never did. They need the file to exist and say so.

    `--remove-data` has to as well: it lists and removes what Graft left on a
    machine whose setup never finished, or whose config.yaml won't load, and
    reads that file itself, leniently, only for the folders it must leave.
    """
    assert {v.flag for v in _verbs() if v.needs is _Needs.NOTHING} == {
        "--describe", "--version", "--check-update", "--init", "--setup-portal",
        "--remove-data",
        # The self-update: the app downloads and installs a newer Graft
        # whether or not setup is done, as it checks for one.
        "--download-update", "--install-update",
    }


def test_the_verbs_that_take_the_path_are_the_ones_that_repair_it() -> None:
    """The middle state, and the reason it exists.

    These two take the config *path* and read or write the YAML directly,
    precisely so they still work on a document `load_config` refuses — an
    incomplete `signing:` block is the thing the settings editor exists to
    repair, and routing the read through a validated `Config` made the pane
    inert on exactly those configs. That is a weaker requirement than a loaded
    `Config` and a stronger one than nothing, which is why it is neither.

    The two App Store Connect writers joined them: `load_config` refuses an
    `app_store_connect:` section that is missing a key, and replacing or
    removing that section is what `--set-portal` and `--clear-portal` do.
    """
    assert {v.flag for v in _verbs() if v.needs is _Needs.PATH} == {
        "--settings", "--set-settings", "--set-portal", "--clear-portal",
    }


@pytest.mark.parametrize("verb", [["--settings"], ["--set-settings", "--archive-keep", "4"]])
def test_a_path_verb_below_the_gate_runs_on_an_unloadable_config(
    temp_config_env: Path, tmp_path: Path, verb: list[str]
) -> None:
    """What `_Needs.PATH` is *for*, asserted through `main` rather than around it.

    A config whose `signing:` block is missing a required key is exactly what
    the settings editor exists to fix, so these have to answer for it rather
    than refusing to load it. `--set-settings` is the case that matters most
    and the one that was missing: it is the only verb below the gate that
    *consumes* verb-only flags, which is the combination — runs unvalidated,
    still has its flags checked — that the old position-based ordering rule
    could not express at all.
    """
    raw = yaml.safe_load(temp_config_env.read_text())
    del raw["signing"]["team_id"]
    broken = tmp_path / "broken.yaml"
    broken.write_text(yaml.safe_dump(raw))

    # Premises, so a table edit that makes the case vacuous fails here rather
    # than leaving a green test that no longer covers anything.
    gate = next(i for i, s in enumerate(_VERBS) if isinstance(s, _StrayFlagGate))
    below = {s.flag for s in _VERBS[gate:] if isinstance(s, _Verb) and s.needs is _Needs.PATH}
    assert {"--settings", "--set-settings"} <= below, "premise: both sit below the gate"

    result = _invoke(["--config", str(broken), "--json", *verb])
    assert result.exit_code == 0, result.output

    events = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    by_type = {event["type"]: event for event in events}
    # Both verbs report the settings — `--set-settings` emits the state its
    # write produced, so the GUI does not spawn a second process to read it.
    assert by_type["settings"]["signing_team_id"] is None, "the missing field reports as unset"
    if verb[0] == "--set-settings":
        assert by_type["settings-updated"]["changed"] == ["archive_keep"]
        assert yaml.safe_load(broken.read_text())["archive_keep"] == 4

    # And a verb that does need a config still refuses the same file.
    refused = _invoke(["--config", str(broken), "--json", "--list"])
    assert refused.exit_code != 0


def test_the_guard_sits_between_the_verbs_that_ignore_these_flags_and_the_ones_that_do_not() -> None:
    gate = [i for i, step in enumerate(_VERBS) if isinstance(step, _StrayFlagGate)]
    assert len(gate) == 1, "there must be exactly one stray-flag gate"
    first_consumer = next(i for i, s in enumerate(_VERBS) if isinstance(s, _Verb) and s.accepts)
    assert gate[0] < first_consumer
    assert not any(
        s.accepts for s in _VERBS[: gate[0]] if isinstance(s, _Verb)
    ), "a verb above the gate consumes a verb-only flag it will never be checked for"


def test_reconcile_is_considered_before_delivery_state() -> None:
    """Given both, the device check plus its report is strictly more than the
    report alone — and the guard has to agree with the dispatch about which of
    the two is active, since they accept the same flag."""
    order = [v.flag for v in _verbs()]
    assert order.index("--reconcile") < order.index("--delivery-state")


# ---------------------------------------------------------------------------
# …and the behavior those positions produce
# ---------------------------------------------------------------------------


def _invoke(args: list[str]):
    return CliRunner().invoke(cli_mod.app, args)


def test_a_verb_above_the_gate_still_ignores_a_verb_only_flag(temp_config_env: Path) -> None:
    """`graft --list --app example` lists every app and ignores the --app.

    Undocumented and untested until now, and the reason the gate cannot simply
    move to the top of the order: the GUI has always been free to pass --app
    alongside --list, and turning that into exit 2 would break it.
    """
    result = _invoke(["--json", "--list", "--app", "example"])

    assert result.exit_code == 0, result.output
    assert "is not valid with" not in result.output
    assert "requires --add-app" not in result.output


def test_the_gate_judges_the_first_flag_consuming_verb_not_the_one_that_runs(
    temp_config_env: Path,
) -> None:
    """--cached-ipa consumes no verb-only flag and is dispatched before
    --prune-builds, so `--cached-ipa X --prune-builds --keep 3` runs the
    cached-ipa report while --keep is judged against --prune-builds and allowed.

    A quirk, not a design — but a quirk with a user-visible exit code, so it is
    pinned rather than tidied. `_active_verb_only_flags` reads the order out of
    `_VERBS` precisely so this cannot drift silently.
    """
    result = _invoke(["--json", "--cached-ipa", "example", "--prune-builds", "--keep", "3"])

    assert result.exit_code == 0, result.output
    assert '"type":"cached-ipa"' in result.stdout, result.stdout
    assert "--keep" not in result.output


def test_the_same_flag_without_that_second_verb_is_rejected(temp_config_env: Path) -> None:
    """The other half: --keep is not something --cached-ipa can consume."""
    result = _invoke(["--json", "--cached-ipa", "example", "--keep", "3"])

    assert result.exit_code == 2, result.output
    assert "--keep requires --add-app" in result.output


def test_a_config_that_will_not_load_is_reported_before_a_stray_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both are exit 2, so only the message distinguishes them — and "--keep
    requires --add-app" is a wrong diagnosis on a machine whose config.yaml is
    the actual problem. `load_config` ran ahead of the guard when both were
    open-coded; the gate declares `_Needs.LOADED` to keep it that way.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    broken = tmp_path / "cfg" / "graft" / "config.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("this: is: not: valid: yaml:\n")

    result = _invoke(["--keep", "3"])

    assert result.exit_code != 0
    assert "requires --add-app" not in result.output, (
        "a broken config was diagnosed as a bad flag"
    )
    assert "failed to parse" in result.output, result.output


def test_a_build_still_loads_the_config_nothing_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fall-through path is where the config load is easiest to lose.

    No entry in `_VERBS` matches a bare app name, so `graft example` walks
    the whole table without selecting anything and then builds. `main` now
    resolves the verb *first* and loads a config only for the selected one —
    which is behavior-preserving for every verb and would hand the build
    `None`, turning a broken config.yaml from a config error into a failed
    build with an AttributeError in it. `selected is None` is the clause that
    stops that, and this is what holds it in place.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    broken = tmp_path / "cfg" / "graft" / "config.yaml"
    broken.parent.mkdir(parents=True)
    broken.write_text("this: is: not: valid: yaml:\n")

    result = _invoke(["example"])

    assert "failed to parse" in result.output, result.output


def test_the_stray_flag_message_names_every_verb_that_consumes_one(
    temp_config_env: Path,
) -> None:
    """The "requires ..." sentence is prose, and prose does not know when a verb
    grows or loses a flag. Held to the table rather than reworded from it, so the
    wording the GUI's tests match on is still a literal."""
    result = _invoke(["--keep", "3"])

    assert result.exit_code == 2, result.output
    # Rich wraps the sentence across lines; the flags themselves survive intact.
    sentence = " ".join(result.output.split("requires", 1)[1].split())
    named = set(re.findall(r"--[a-z0-9-]+", sentence))
    assert named == {v.flag for v in _verbs() if v.accepts}


def test_two_verbs_at_once_still_run_the_earlier_one(temp_config_env: Path) -> None:
    """Not an error, deliberately: the GUI builds argv programmatically and a
    new way for it to fail is worse than a documented precedence."""
    result = _invoke(["--json", "--version", "--list"])

    assert result.exit_code == 0
    assert result.stdout.startswith("graft ")
    assert '"type": "app"' not in result.stdout


def test_a_build_id_of_zero_is_still_a_build_id(temp_config_env: Path) -> None:
    """`--reveal-build 0` selects the verb. Selecting on truthiness would read
    it as absent and fall through to the usage error — silently, since a build
    id nobody has is a plausible thing to be told nothing about.

    Asserted on the call, because the verb's own failure ("no build log yet")
    is a message this test would otherwise accept as proof it ran.
    """
    with patch.object(cli_mod, "_do_reveal_build") as reveal:
        result = _invoke(["--reveal-build", "0"])

    assert reveal.call_args is not None, "the verb was not dispatched at all"
    assert reveal.call_args.args[1] == 0
    assert "usage: graft APP" not in result.output


def test_a_stray_negated_flag_is_named_as_the_user_typed_it(
    temp_config_env: Path,
) -> None:
    """A negated flag sets the same parameter as its positive form, so the gate
    listed *both* dict keys — accusing the user of passing a flag they never
    typed, with `sorted` putting the one they didn't type first."""
    result = _invoke(["example", "--keep-encrypted-extensions"])

    assert result.exit_code != 0
    assert "--keep-encrypted-extensions" in result.output
    assert "--strip-encrypted-extensions" not in result.output, (
        "the error names a flag the user never typed"
    )


def test_a_stray_positive_flag_is_still_named_correctly(
    temp_config_env: Path,
) -> None:
    result = _invoke(["example", "--strip-encrypted-extensions"])

    assert result.exit_code != 0
    assert "--strip-encrypted-extensions" in result.output
    assert "--keep-encrypted-extensions" not in result.output


@pytest.mark.parametrize(
    ("positive", "negative"),
    [
        ("--auto-profile", "--no-auto-profile"),
        ("--keep-bundle-id", "--no-keep-bundle-id"),
        ("--strip-extensions", "--no-strip-extensions"),
        ("--strip-supported-devices", "--no-strip-supported-devices"),
    ],
)
def test_every_negated_verb_only_flag_is_named_as_typed(
    temp_config_env: Path, positive: str, negative: str
) -> None:
    """Review #11: only one `--x/--no-x` pair was in the table, so
    `--builds --no-auto-profile` was refused as "--auto-profile requires ..."."""
    result = _invoke(["--builds", negative])

    assert result.exit_code == 2, result.output
    said = " ".join(result.output.split())
    assert f"{negative} requires" in said
    assert f"{positive} requires" not in said


def test_the_negated_table_covers_every_pair_main_declares() -> None:
    """Derived from the declarations, so a new pair cannot be left out."""
    declared = {
        decl.split("/", 1)[0]
        for p in inspect.signature(cli_mod.main).parameters.values()
        for decl in getattr(p.default, "param_decls", ()) or ()
        if "/" in decl
    }
    assert declared and declared == set(cli_mod._NEGATED_FLAGS)
