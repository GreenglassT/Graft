"""GLOBAL ASSERTION 4 — the `stage` event's `name` is a vocabulary too.

`EVENT_TYPES` and the partition around it prove the GUI recognizes a `stage`
*line*. They say nothing about the string inside it, and one of those strings is
read for meaning rather than for display.

`CLIContract.Stage.decryptionCheck` is `"verify"`.
`ConfigStore.note(_:forBuildOf:)` treats that stage's `status: "end"` as the
only signal the cryptid-0 check reached a verdict — the CLI emits
`encrypted-binaries` when it finds encrypted binaries and nothing at all when it
doesn't, so "the check ran and the IPA is fine" has no event of its own. It is
this stage ending without one.

Which made it the last instance of the defect class the two previous rounds kept
finding: a value one side depends on that the other side can change without
anything failing. Rename the CLI's `verify` stage and the `stage` event still
decodes, still renders in the transcript, still looks entirely normal — while
the `where name == …` in `note` silently stops matching and a card reading
"N binaries are still encrypted — will crash on launch" stands forever over an
IPA that has been clean for weeks.

So `STAGE_NAMES` registers them, `JsonEmitter._line` raises on an unregistered
one, `--describe` publishes the set, and this module holds it to an equality:

    STAGE_NAMES == {every name emitted by graft_ipa/}

The equality is the load-bearing part, and a permit-list would not do. The
Swift assertion asks "is the name I depend on one the CLI emits?" — if the
registry were merely a superset, a rename that added the new name and left the
old one behind would answer yes about a string that has not been on the wire
since, which is the original bug with an extra step.

WHAT THE SWIFT SIDE OWES: source, not binary. `CLIContractTests` runs the same
subset check against the `graft` actually on PATH, which need not be this
checkout.
"""

from __future__ import annotations

import functools

import pytest

from graft_ipa.json_output import STAGE_NAMES
from tests.cli_ast import (
    CONTRACT_SWIFT,
    SWIFT_STRING,
    contract_swift_source,
    emit_calls,
)


@functools.cache
def _emitted_stage_names() -> dict[str, str]:
    """Every `name=` on an `emit("stage", …)` call in the package, to its site.

    Filtered out of the one emit scan in `tests/cli_ast.py`, which parses each
    module once and asserts the event type is a literal. The literalness
    assertion below is not decoration either — it is the premise that makes
    raising (rather than warning) on an unregistered name safe, since a
    computed name could be one a user's input produced mid-build.
    """
    found: dict[str, str] = {}
    for call in emit_calls():
        if call.event_type != "stage":
            continue
        assert call.keyword("name") is not None, f"{call.where}: emit('stage', …) with no `name=`"
        name = call.literal_keyword("name")
        assert name is not None, (
            f"{call.where}: stage name is computed, not a literal — the registry can "
            "no longer be checked statically, and `_line` raising on an "
            "unregistered name stops being a development-time error"
        )
        found[name] = call.where
    return found


def _swift_stage_constants() -> set[str]:
    """The string literals of `static let` inside Swift's `enum Stage { … }`.

    Brace-counted rather than pattern-matched over the whole file: stage names
    have no `--` prefix to key on, so the scan narrows by location instead, and
    a wrong extent would quietly read fewer constants than exist.

    Comments are stripped by the shared stripper rather than by a
    `startswith("//")` filter of this scan's own. The filter was the better of
    the two sibling scans and still had the hole: a trailing `// "verify"` on a
    line the brace count keeps is not the start of a comment line, and the
    braces inside a comment were counted as structure.
    """
    if not CONTRACT_SWIFT.exists():
        pytest.skip(f"no Swift checkout at {CONTRACT_SWIFT}")
    lines = contract_swift_source().splitlines()
    start = next((i for i, line in enumerate(lines) if "enum Stage {" in line), None)
    assert start is not None, (
        f"could not find `enum Stage {{` in {CONTRACT_SWIFT}. If it was renamed or "
        "restructured, this assertion is reading nothing and proving nothing."
    )

    depth = 0
    names: set[str] = set()
    for line in lines[start:]:
        # Checked before `depth` is updated, so the `enum Stage {` header line
        # itself is outside the body.
        if depth > 0 and "static let" in line:
            names.update(SWIFT_STRING.findall(line))
        depth += line.count("{") - line.count("}")
        if depth <= 0:
            break

    assert names, "parsed an empty `enum Stage`; the brace scan is matching the wrong thing"
    return names


# ---------------------------------------------------------------------------
# The equality
# ---------------------------------------------------------------------------


def test_every_stage_name_the_cli_emits_is_registered() -> None:
    """Static counterpart to `_line`'s runtime check.

    `_line` only rejects an unregistered name when something actually runs that
    emit; the build path's five stages are exercised by the suite, but a new
    stage added on a branch nothing covers would reach a release unregistered.
    """
    emitted = _emitted_stage_names()
    assert emitted, "found no `emit('stage', …)` calls at all — this scan is broken"

    unregistered = {n: where for n, where in emitted.items() if n not in STAGE_NAMES}
    assert not unregistered, (
        f"emitted but not in STAGE_NAMES: {unregistered}. `--describe` publishes "
        "STAGE_NAMES as the CLI's statement of what it emits, and the GUI checks "
        "the names it depends on against exactly that."
    )


def test_the_registry_names_no_stage_the_cli_stopped_emitting() -> None:
    """The half that makes the registry an equality, and the one with teeth.

    Without it `STAGE_NAMES` is a permit list, and a rename done the natural
    way — add the new spelling, leave the old — publishes `verify` from
    `--describe` long after the CLI stopped emitting it. Swift's subset check
    passes, the GUI waits for a stage that will never arrive, and the
    encrypted-binaries card never clears again: the exact bug, one step later.
    """
    emitted = set(_emitted_stage_names())
    dead = sorted(STAGE_NAMES - emitted)
    assert not dead, (
        f"STAGE_NAMES registers {len(dead)} stage name(s) nothing emits: "
        f"{', '.join(dead)}. Either the stage was renamed and the old entry left "
        "behind — in which case `--describe` is now advertising a name the GUI "
        "may be waiting on — or the stage is gone and the entry with it."
    )


def test_the_gui_depends_on_nothing_the_cli_cannot_emit() -> None:
    """The cross-boundary check, run from the side that can do it cheaply.

    `CLIContractTests` asserts this against the *installed* CLI, which is the
    binary the GUI will actually spawn. This asserts it against this checkout,
    where a rename lands in `pytest` before anyone opens Xcode.
    """
    depended_on = _swift_stage_constants()
    stale = sorted(depended_on - STAGE_NAMES)
    assert not stale, (
        f"CLIContract.Stage names {len(stale)} stage(s) the CLI never emits: "
        f"{', '.join(stale)}. A `stage` event with an unexpected name still "
        "decodes and still renders, so nothing fails — ConfigStore's "
        "`where name == …` just stops matching and the state it gates is never "
        "reached."
    )


def test_the_gui_is_not_asked_to_track_every_stage() -> None:
    """Deliberately *not* the partition `EVENT_TYPES` gets, and why.

    An event nothing decodes is dropped by `try?` and its feature silently
    ceases to exist, so every event must be handled or excused. A stage name
    nothing branches on is rendered verbatim by `BuildLogView`, `AppDetailView`
    and `ReinstallSheet` — correct with no Swift change at all. Requiring Swift
    to enumerate all seven would prevent no defect and would cost the one thing
    that makes the subset check worth running: that membership in
    `CLIContract.Stage` means "something depends on this", not "this exists".

    Asserted rather than left as a comment because the pressure to "just add
    the missing ones" is what would erase the distinction.

    Phrased as "the GUI does not name them all" rather than as a proper-subset
    check, which would also fire when the GUI names a stage the CLI *cannot*
    emit — a different bug, already caught above with a message that says so.
    Two tests failing where one is wrong about why is how a real diagnosis gets
    read past.
    """
    depended_on = _swift_stage_constants()
    assert not STAGE_NAMES <= depended_on, (
        "the GUI now names every stage the CLI emits. If that is because it "
        "genuinely branches on all of them, delete this test and make the Swift "
        "assertion an equality. If it is because the list was completed for "
        "tidiness, remove the names nothing depends on — each one is a rename "
        "this contract will now block for no reason."
    )


# ---------------------------------------------------------------------------
# …and over the wire, which is what Swift can read
# ---------------------------------------------------------------------------


def test_describe_publishes_the_stage_registry(contract: dict) -> None:
    """The Swift side cannot import Python. `--describe` is the whole channel."""
    assert set(contract["stages"]) == set(STAGE_NAMES)
    # The one the GUI reads for meaning, named here so a rename that somehow
    # kept both sets self-consistent still has to face it.
    assert "verify" in contract["stages"]
