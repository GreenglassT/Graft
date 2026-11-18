"""`--describe` is the machine-readable half of the CLI↔GUI contract.

The GUI duplicates flag names, event names, and app-option field names as
Swift string literals. `GraftTests/CLIContractTests.swift` runs this very
flag and asserts its own vocabulary is a subset of what comes back. These
tests guard the CLI's end of that handshake.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from graft_ipa.config import SETTINGS
from graft_ipa.json_output import APP_OPTION_FIELDS, EVENT_TYPES
from tests.cli_ast import PACKAGE_DIR, emit_calls, emit_calls_in, parse


def test_describe_works_without_a_config(
    describe_proc: subprocess.CompletedProcess[str], contract: dict
) -> None:
    """--describe must not load config.yaml.

    The GUI's contract test runs on any machine that has the CLI installed,
    including one that has never run `--init`. The `describe_proc` fixture runs
    it with XDG_CONFIG_HOME pointed at an empty directory, so a config lookup
    fails loudly rather than silently falling through to the developer's real
    ~/.config/graft/ — and the "exactly one JSON line" shape is asserted
    where the process is decoded, in `contract`.
    """
    assert describe_proc.returncode == 0, describe_proc.stderr
    assert contract["type"] == "cli-contract"


def test_describe_lists_known_flags(contract: dict) -> None:
    flags = set(contract["flags"])
    # A spread of the flags the GUI actually sends: a build toggle, a flag
    # taking a value, a listing verb, and the JSON switch itself.
    assert {"--dry-run", "--device", "--json", "--list", "--set-app-options"} <= flags


def test_describe_includes_negated_boolean_flags(contract: dict) -> None:
    """`--x/--no-x` pairs must report both halves.

    ConfigStore.updateAppOptions sends the `--no-` spelling whenever a toggle
    is turned off; if only the positive half were declared, the contract test
    couldn't catch a rename of the negative one.
    """
    flags = set(contract["flags"])
    assert {
        "--no-keep-bundle-id",
        "--no-strip-extensions",
        "--no-strip-supported-devices",
    } <= flags


def test_describe_lists_known_events(contract: dict) -> None:
    events = set(contract["events"])
    assert {"app", "build-start", "build-end", "stage", "log", "app-updated"} <= events
    assert events == set(EVENT_TYPES)


def test_describe_lists_app_option_fields(contract: dict) -> None:
    assert set(contract["app_option_fields"]) == set(APP_OPTION_FIELDS)
    assert "delivery" in contract["app_option_fields"]


def test_describe_lists_setting_fields(contract: dict) -> None:
    """Equality, not a subset — the direction the Swift side cannot check.

    `CLIContractTests` asserts what it sends is a subset of what is declared,
    which stays green when `--describe` advertises a setting no flag can write.
    Register a sixth key in `SETTINGS` and this is what notices; the equality
    guard in `_verb_set_settings` is what makes it impossible to fix by
    declaring it and walking away.
    """
    assert set(contract["setting_fields"]) == set(SETTINGS)
    assert "signing_team_id" in contract["setting_fields"]


def test_describe_flags_match_the_commands_real_parameters(contract: dict) -> None:
    """The flag list is derived from Click, not hand-maintained.

    Cross-check against a fresh parse of the callback's own signature: every
    declared flag must correspond to a `typer.Option` spelling in cli.py.
    """
    source = parse(PACKAGE_DIR / "cli.py")
    literal_flags: set[str] = set()
    for node in ast.walk(source):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "Option":
            continue
        for arg in node.args[1:]:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                # A `--x/--no-x` declaration is one string holding both halves.
                literal_flags.update(arg.value.split("/"))

    undeclared = set(contract["flags"]) - literal_flags
    assert not undeclared, f"--describe reported flags not found in cli.py: {sorted(undeclared)}"


def test_every_emitted_event_type_is_registered() -> None:
    """Static counterpart to `JsonEmitter.emit`'s runtime check.

    `emit()` only rejects an unregistered type when a test actually exercises
    that code path; parsing the package catches a new emit call whose path
    nothing covers yet.

    Scans `emit` AND `emit_to_tee`, via the one scanner in `tests/cli_ast.py`.
    It used to match `"emit"` alone while the two newer scans over the same
    calls matched both — so `emit_to_tee("brand-new-type", …)` was caught by the
    stage registry and by the warning channel and slipped past the *event*
    registry, which is the only one that decides whether the GUI has a case for
    it. An unhandled event is dropped by Swift's `try?` with no trace: the
    `device-error` failure exactly.
    """
    calls = emit_calls()
    assert calls, "found no emit calls at all — this scan is broken"

    unregistered = {c.event_type: c.where for c in calls if c.event_type not in EVENT_TYPES}
    assert not unregistered, f"emitted but not in EVENT_TYPES: {unregistered}"


def test_the_registry_check_sees_both_ways_an_event_reaches_the_wire() -> None:
    """The positive control for the scan above, on the method it used to miss.

    A hole that has been closed once tends to reopen as an "unused" branch: if
    `emit_to_tee` ever stops being scanned — or stops existing — this says so,
    rather than leaving the assertion above quietly narrower than it reads.
    """
    methods = {c.node.func.attr for c in emit_calls()}
    assert methods == {"emit", "emit_to_tee"}, (
        f"the emit scan found only {sorted(methods)}. Both are real emitters "
        "(json_output.py) and both put a line on the wire; a scan that knows "
        "one of them is a registry check with a door in it."
    )


# ---------------------------------------------------------------------------
# …and the *spelling* the scan used to be blind to
# ---------------------------------------------------------------------------
#
# Run over synthetic source rather than over the package, because the property
# is about what the scanner can see, and the package is by construction free of
# the shapes worth testing — that is what every assertion above is for.


def _scan(source: str) -> list:
    return emit_calls_in(ast.parse(source), Path("synthetic.py"))


def test_the_scan_sees_an_event_type_passed_by_keyword() -> None:
    """`emit(event_type=…)` is a call `JsonEmitter.emit` accepts, and the scan
    dropped it before the literalness check every assertion here rests on.

    One spelling, four blind assertions: `test_every_emitted_event_type_is_
    registered` above, the stage registry, and both legs of the warning
    channel. A hand-rolled warning skipping `_warn`'s stderr half, or an event
    type in neither `EVENT_TYPES` nor `CLIContract.eventsHandled`, shipped
    green — and the GUI drops what it cannot decode without a trace, which is
    the `device-error` failure these scans exist to make impossible.
    """
    calls = _scan(
        "def f(e):\n"
        "    e.emit(event_type='brand-new-type', n=1)\n"
        "    e.emit_to_tee(event_type='stage', name='ghost')\n"
    )

    assert [c.event_type for c in calls] == ["brand-new-type", "stage"]
    # The keyword accessors have to keep working on this shape too: the stage
    # registry reads `name=` off exactly these calls.
    assert calls[1].literal_keyword("name") == "ghost"


def test_an_emit_the_scan_cannot_read_fails_instead_of_being_skipped() -> None:
    """Silence about a call is not evidence about it.

    Skipping a call the scanner cannot resolve makes every assertion downstream
    quietly narrower than it reads — which is how the keyword form got in. A
    splatted payload is the remaining way to hide an event type, so it stops
    the scan rather than slipping through it.
    """
    with pytest.raises(AssertionError, match="no positional argument"):
        _scan("def f(e, payload):\n    e.emit(**payload)\n")

    with pytest.raises(AssertionError, match="is computed, not"):
        _scan("def f(e, t):\n    e.emit(event_type=t, n=1)\n")

    # The positional form's own literalness check is unchanged.
    with pytest.raises(AssertionError, match="non-literal"):
        _scan("def f(e, t):\n    e.emit(t, n=1)\n")
