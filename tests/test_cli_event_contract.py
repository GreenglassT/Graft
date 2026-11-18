"""GLOBAL ASSERTION 3 — every emitted event is consumed, or declared not to be.

`EVENT_TYPES` says what the CLI may put on the wire. `CLIContract.eventsHandled`
says what the GUI decodes. Until now the only check between them ran on the
Swift side and asserted a *subset*: `eventsHandled ⊆ events`. That catches a
renamed event the GUI still looks for. It cannot catch the opposite and more
common direction — an event the CLI emits that nothing on earth reads — because
a subset check is silent about extras.

`device-error` is what that silence cost. Registered, emitted on every
`--devices` call, absent from `eventsHandled`; `String.buildEvents()` decodes
with `try?`, so each one was dropped without a trace, filtered out of
`DevicesStore.load`, and the Devices tab said "No devices connected" about a
phone that was plugged in. `--devices` exits 0, so the stderr line that
accompanied it never reached the GUI either. Both sides' tests were green
throughout.

So the leftovers are named, and the partition is asserted:

    EVENT_TYPES == eventsHandled | CLI_ONLY_EVENTS

There is deliberately no third bucket for "the GUI will get to it". A backlog
entry is indistinguishable from a bug at runtime — the user sees nothing either
way — and an event's only two honest states are "something renders it" and
"nothing should".

WHY THIS READS SWIFT SOURCE: `eventsHandled` is parsed out of CLIContract.swift.
That file is the declaration itself — hand-maintained by design, because Swift
cannot enumerate a switch's patterns — so parsing it is reading the contract
rather than guessing at it. A missing Swift checkout skips; a Swift file this
cannot parse fails, because finding zero events would make every assertion here
pass by proving nothing.

WHAT THIS CANNOT SEE, AND WHAT THE SWIFT SIDE OWES: source, not binary. A GUI
running against a *different* installed `graft` than the one in this
checkout is outside its reach. `--describe` now publishes `cli_only_events`
alongside `events` so `CLIContractTests` can assert the same partition against
the CLI actually on PATH — `Set(contract.events).subtracting(contract.cliOnly)
⊆ eventsHandled` — which is the half only Swift can run.
"""

from __future__ import annotations

import re

import pytest

from graft_ipa.json_output import CLI_ONLY_EVENTS, EVENT_TYPES
from tests.cli_ast import (
    CONTRACT_SWIFT,
    SWIFT_STRING,
    contract_swift_source,
    strip_swift_comments,
)


def _swift_set(name: str) -> set[str]:
    """A `static let <name>: Set<String> = [...]` literal, as a Python set.

    Read off the source with its commentary stripped. It used to regex string
    literals out of the raw text between the brackets, comments included, so
    deleting `"device-error"` from `eventsHandled` and leaving
    `// "device-error" moved to a store` behind kept every partition test below
    green about an event the GUI had stopped decoding — a quoted name in a
    comment counting as a declaration, in the one file that is nothing but
    declarations. The sibling scan in `test_cli_stage_contract` dropped comment
    lines and this one did not; now neither has its own opinion.
    """
    if not CONTRACT_SWIFT.exists():
        pytest.skip(f"no Swift checkout at {CONTRACT_SWIFT}")
    pattern = re.compile(
        rf"static\s+let\s+{re.escape(name)}\s*:\s*Set<String>\s*=\s*\[(.*?)^\s*\]",
        re.DOTALL | re.MULTILINE,
    )
    match = pattern.search(contract_swift_source())
    assert match, (
        f"could not find `static let {name}: Set<String> = [...]` in "
        f"{CONTRACT_SWIFT}. If it was renamed or restructured, this assertion is "
        "reading nothing and proving nothing — update the pattern."
    )
    values = set(SWIFT_STRING.findall(match.group(1)))
    assert values, f"parsed an empty {name}; the pattern is matching the wrong thing"
    return values


def _events_handled() -> set[str]:
    return _swift_set("eventsHandled")


# ---------------------------------------------------------------------------
# The scan itself
# ---------------------------------------------------------------------------


def test_a_quoted_name_in_a_comment_is_not_a_declaration() -> None:
    """The evasion every partition test below used to accept.

    Deleting an event from `eventsHandled` and leaving the name quoted in a
    comment beside it — the way anybody would record where it went — kept the
    partition green while the GUI stopped decoding the event. That is
    `device-error` again: registered, emitted, dropped by `try?`, and no test
    anywhere with anything to say about it.

    Every shape that hides a literal is covered, because the one that got
    through was the one the scan happened not to think of.
    """
    stripped = strip_swift_comments(
        'static let eventsHandled: Set<String> = [\n'
        '    "app",  // "device-error" moved to a store\n'
        '    // "build-end" too\n'
        '    /* "stage" as well */\n'
        '    "log",\n'
        ']\n'
    )

    assert set(SWIFT_STRING.findall(stripped)) == {"app", "log"}
    # The brace scan in the stage contract counts these, so commentary must not
    # contribute structure either.
    assert "[" in stripped and stripped.count("\n") == 6


def test_the_stripper_does_not_eat_a_slash_inside_a_string() -> None:
    """Quote state is tracked, not assumed. A scan that truncated a literal at
    an embedded `//` would under-read the declaration — the same silence as
    over-reading it, pointed the other way."""
    assert SWIFT_STRING.findall(strip_swift_comments('let x = "a//b"')) == ["a//b"]


# ---------------------------------------------------------------------------
# The partition
# ---------------------------------------------------------------------------


def test_every_emitted_event_is_handled_or_declared_cli_only() -> None:
    handled = _events_handled()
    orphans = sorted(EVENT_TYPES - handled - set(CLI_ONLY_EVENTS))

    assert not orphans, (
        f"{len(orphans)} event type(s) are emitted by the CLI and read by nobody: "
        f"{', '.join(orphans)}. The GUI decodes with `try?`, so each one is "
        "dropped silently and the feature it belongs to shows nothing at all. "
        "Add a `case` to BuildEvent.decode and the name to "
        "CLIContract.eventsHandled, or say why the GUI needs no surface for it "
        "in json_output.py's CLI_ONLY_EVENTS."
    )


def test_the_gui_decodes_nothing_the_cli_cannot_emit() -> None:
    """The direction the Swift side already checked, checked from here too.

    Cheap to assert and it fails earlier: a rename lands in this repo's Python
    suite before anyone opens Xcode.
    """
    stale = sorted(_events_handled() - EVENT_TYPES)
    assert not stale, (
        f"BuildEvent.decode handles {len(stale)} event type(s) the CLI never "
        f"emits: {', '.join(stale)}. Either the CLI dropped them or one side "
        "was renamed; the GUI is waiting for a line that will not come."
    )


def test_no_event_is_both_handled_and_declared_cli_only() -> None:
    """A contradiction, and the way this dict would rot if left unchecked.

    `CLI_ONLY_EVENTS` claims no user-visible state depends on the event. A GUI
    case for it says the opposite. Whichever is wrong, the partition above
    would still hold — the overlap makes the union too big, never too small —
    so it needs saying separately.
    """
    both = sorted(_events_handled() & set(CLI_ONLY_EVENTS))
    assert not both, (
        f"{', '.join(both)} are declared CLI-only in json_output.py but the GUI "
        "decodes them. One of the two declarations is wrong."
    )


def test_both_sides_declare_the_same_cli_only_events() -> None:
    """Swift keeps its own `cliOnlyEvents`; the two must be the same list.

    Neither side can be the sole author. The CLI knows what it emits and the
    GUI knows what it renders, so "this needs no surface" is a joint claim —
    and a divergence means one of them is excusing an event the other is
    quietly relying on.
    """
    swift = _swift_set("cliOnlyEvents")
    assert swift == set(CLI_ONLY_EVENTS), (
        "the two CLI-only declarations disagree: "
        f"only in Swift={sorted(swift - set(CLI_ONLY_EVENTS))}, "
        f"only in Python={sorted(set(CLI_ONLY_EVENTS) - swift)}"
    )


# ---------------------------------------------------------------------------
# The half that needs no Swift checkout
# ---------------------------------------------------------------------------


def test_cli_only_events_are_real_and_argued() -> None:
    """Everything the Python half can assert alone.

    The reason strings are the mechanism, not decoration: the bug this
    assertion exists for was nobody having to write anything down.
    """
    unknown = sorted(set(CLI_ONLY_EVENTS) - EVENT_TYPES)
    assert not unknown, f"CLI_ONLY_EVENTS names events the CLI cannot emit: {unknown}"
    for event, reason in CLI_ONLY_EVENTS.items():
        assert len(reason) > 80, (
            f"CLI_ONLY_EVENTS[{event!r}] needs an argument somebody can check, "
            "not a label"
        )


# ---------------------------------------------------------------------------
# …and the same partition over `--describe`, which is what Swift can read
# ---------------------------------------------------------------------------


def test_describe_publishes_the_cli_only_declaration(contract: dict) -> None:
    """The Swift side cannot import Python. `--describe` is the whole channel.

    Publishing only `events` is what made a subset check the strongest thing
    CLIContractTests could assert against a *running* CLI. With the declaration
    on the wire it can assert the complement instead.
    """
    payload = contract
    assert set(payload["cli_only_events"]) == set(CLI_ONLY_EVENTS)
    # The set a GUI must be able to decode, derivable from the wire alone.
    must_handle = set(payload["events"]) - set(payload["cli_only_events"])
    assert "build-end" in must_handle
    assert "device-error" in must_handle
    assert "cli-contract" not in must_handle
