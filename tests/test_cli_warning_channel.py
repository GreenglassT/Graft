"""GLOBAL ASSERTION 1 — no warning may reach the user except through `_warn`.

`_warn` exists because `console` writes to stdout, which under `--json` *is* the
event stream: the GUI decodes it line by line with `try?` and drops anything
that isn't a registered event, so a warning printed there is a warning the GUI
structurally cannot show. `_warn` sends every warning out twice instead — as a
`log` event, which the build panel renders, and on stderr for a consumer that
has never heard of the event stream.

Round one converted the warning sites it could find. Round two found more, in
sites the conversion had missed — including `_do_devices`, whose entire purpose
is to warn, and which carried a comment claiming its hand-rolled
`err_console.print` "reaches a GUI that has never heard of `device-error` too".
It does not: `--devices` exits 0, and the GUI reads stderr only off a non-zero
exit. A green suite could not tell anyone that, because the tests had been
written against the sites that *were* converted.

So the invariant is asserted globally, over the source, in both directions:

  * nothing in cli.py may write warning-shaped text to a channel a human or the
    event stream reads, unless it is inside `_warn`;
  * no `log` event with `source="warning"` may be emitted except from `_warn`.

Neither leg alone is enough. The first would let somebody emit the structured
half by hand and skip stderr; the second would let somebody print to a console
and skip the event. Together they say: one function, both channels, always.

THE FIRST LEG KEYS ON THE DESTINATION, not on the method name. It used to key
on `attr == "print"`, which is blind to the two shapes most likely to reach the
JSON stream:

    MISSED   print("warning: the disk is full")             # ast.Name
    MISSED   sys.stdout.write("warning: the disk is full")  # attr is "write"
    CAUGHT   console.print("warning: …")

Both misses write to stdout, which under `--json` *is* the event stream — the
exact failure this file exists for — and `sys.stdout.write` is already the
idiom `_do_build_log` uses. So the rule is "does this land somewhere a reader
is": bare `print`, a `write` on `sys.stdout`/`sys.stderr` or on a Console, and
any `.print` at all. The last is deliberately wider than the destination set:
`_report_app_failure` prints through a local alias (`out = err_console if
emitter else console`), aliases are resolved here but an unresolved one is
still caught by the method name.

SCOPE: `cli.py`, because that is where `_warn` lives and where every JSON-mode
verb is dispatched. The only other modules that print are `init_flow` and
`setup_portal`, the two interactive wizards — they take a `Console` and prompt
for input, have no `--json` form at all, and (checked) emit no warning-shaped
line. Widening this to the package would assert something true of code that
cannot reach the failure mode.

DRIFT: the first leg keys on two literal prefixes, `warning:` and `note:`. It
cannot be derived from `_warn` (which renders only the first) and it does not
catch a warning that invents a third register — "careful:", say. What makes that
acceptable is the second leg: a warning has to reach a machine consumer somehow,
and the only way to do that is the `log` event, which is pinned structurally.
A print-only warning with a novel prefix would slip past this file, and would
also be a warning that no front-end renders at all — which is the bug, not the
detection of it.
"""

from __future__ import annotations

import ast
import functools
import json
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa.json_output import GUI_EVENTS, JsonEmitter
from tests.cli_ast import CLI_SOURCE, cli_tree, emit_calls_in, enclosing_functions

# How this module spells a warning to a human. `_warn` renders the first; the
# second is the register every "we carried on anyway" line used before the
# conversion, and is kept in the list precisely so a reversion is caught.
WARNING_MARKERS = ("warning:", "note:")

# The one function allowed to produce either. Not a list that grows: a second
# entry here would mean two answers to "where do warnings come from".
THE_WARNING_FUNCTION = "_warn"

# The two module-level Consoles. `console` is stdout — which under `--json` *is*
# the event stream — and `err_console` is stderr.
CONSOLE_NAMES = frozenset({"console", "err_console"})

# The raw streams underneath them, spelled the way cli.py spells them.
RAW_STREAMS = frozenset({"sys.stdout", "sys.stderr"})

# Methods that put bytes on a stream. `print` is the Console API; `write` is
# what you reach for when you have the stream itself.
WRITE_METHODS = frozenset({"write", "writelines"})


def _strings_in(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


@functools.cache
def _console_aliases() -> frozenset[str]:
    """Local names bound to one of the module Consoles.

    `_report_app_failure` does `out = err_console if emitter else console` and
    then prints through `out`. Resolved rather than assumed, so the `.write`
    leg below knows that destination too.
    """
    aliases: set[str] = set()
    for node in ast.walk(cli_tree()):
        if not isinstance(node, ast.Assign):
            continue
        mentioned = {n.id for n in ast.walk(node.value) if isinstance(n, ast.Name)}
        if mentioned & CONSOLE_NAMES:
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return frozenset(aliases - CONSOLE_NAMES)


def _destination(node: ast.Call) -> str | None:
    """Where this call writes, if it writes anywhere a reader is."""
    if isinstance(node.func, ast.Name):
        # The builtin. Goes to stdout, i.e. to the event stream under --json,
        # and is the single easiest way to reintroduce this bug.
        return "print() → stdout" if node.func.id == "print" else None
    if not isinstance(node.func, ast.Attribute):
        return None
    receiver = ast.unparse(node.func.value)
    if node.func.attr == "print":
        return f"{receiver}.print()"
    if node.func.attr in WRITE_METHODS and (
        receiver in RAW_STREAMS or receiver in CONSOLE_NAMES or receiver in _console_aliases()
    ):
        return f"{receiver}.{node.func.attr}()"
    return None


@dataclass(frozen=True)
class _Write:
    function: str
    lineno: int
    destination: str
    marker: str

    def __str__(self) -> str:
        return f"{self.function} (cli.py:{self.lineno}) writes {self.marker!r} to {self.destination}"


@functools.cache
def _warning_writes() -> tuple[_Write, ...]:
    """Every warning-shaped write to a human channel anywhere in cli.py."""
    owner = enclosing_functions(CLI_SOURCE)
    found: list[_Write] = []
    for node in ast.walk(cli_tree()):
        if not isinstance(node, ast.Call):
            continue
        destination = _destination(node)
        if destination is None:
            continue
        # Every string literal anywhere in the arguments, so an f-string's
        # leading fragment counts — that is where the prefix always lives.
        text = " ".join(s for arg in node.args for s in _strings_in(arg))
        marker = next((m for m in WARNING_MARKERS if m in text), None)
        if marker is None:
            continue
        found.append(
            _Write(owner.get(node, "<module>"), node.lineno, destination, marker)
        )
    return tuple(found)


@functools.cache
def _warning_log_emits() -> tuple[tuple[str, int], ...]:
    """(function, line) for every `log` event carrying `source="warning"`."""
    owner = enclosing_functions(CLI_SOURCE)
    found: list[tuple[str, int]] = []
    for call in emit_calls_in(cli_tree(), CLI_SOURCE):
        if call.event_type != "log":
            continue
        if call.literal_keyword("source") != "warning":
            continue
        found.append((owner.get(call.node, "<module>"), call.node.lineno))
    return tuple(found)


def test_both_scans_can_see_the_warning_that_is_already_there() -> None:
    """The positive control the other three global assertions all have.

    Assertion 2 asserts `entries`, the stage scan asserts `emitted`, `_swift_set`
    asserts `values`; this file had nothing. Move `_warn` or its callers to
    another module, rename `err_console`, change how `_warn` spells its prefix,
    and both scans below would find nothing and pass — a green assertion over an
    empty set, which is the shape of every bug the last two rounds turned up.
    """
    writes = _warning_writes()
    assert writes, (
        "the scan found no warning-shaped write anywhere in cli.py. `_warn` "
        "renders `warning:` on `err_console`, so this is not a state cli.py can "
        "be in — the scan is looking at the wrong file, the wrong destinations, "
        "or the wrong markers, and both assertions below are now vacuous."
    )
    assert any(w.function == THE_WARNING_FUNCTION for w in writes), (
        f"warning-shaped writes exist ({[str(w) for w in writes]}) but none is "
        f"inside `{THE_WARNING_FUNCTION}` — either it has moved out of cli.py, "
        "or it no longer writes the human half at all"
    )

    emits = _warning_log_emits()
    assert emits, (
        "the scan found no `log` event with `source=\"warning\"`. That is "
        "`_warn`'s own emit, so finding none means the machine half of the "
        "warning has moved, been renamed, or been dropped — and the assertion "
        "below is passing over nothing."
    )
    assert any(fn == THE_WARNING_FUNCTION for fn, _ in emits), (
        f"warning `log` events exist ({emits}) but none is inside "
        f"`{THE_WARNING_FUNCTION}`"
    )


def test_no_write_to_a_human_channel_carries_a_warning_outside_warn() -> None:
    """The human half, keyed on where the text lands.

    `print(...)` and `sys.stdout.write(...)` both reach the same stdout
    `console.print` does, which under `--json` is the event stream the GUI
    decodes and drops from. A rule that keyed on the method name saw neither.
    """
    offenders = [w for w in _warning_writes() if w.function != THE_WARNING_FUNCTION]
    assert not offenders, (
        "these write a warning without going through `_warn`, so under --json "
        "it lands on the JSON event stream's own stdout (dropped by the GUI's "
        "decoder) or on a stderr nobody reads off an exit-0 verb: "
        + "; ".join(str(w) for w in offenders)
    )


def test_no_warning_log_event_is_emitted_outside_warn() -> None:
    """The machine half. `log`/`source="warning"` is `_warn`'s signature.

    Without this, the assertion above could be satisfied by emitting the event
    by hand and skipping stderr — half a warning, in the half of the pair that
    happens to be under test. Scans `emit_to_tee` as well as `emit`, through the
    one emit scanner in `tests/cli_ast.py`.
    """
    offenders = [
        f"{fn} (cli.py:{lineno})"
        for fn, lineno in _warning_log_emits()
        if fn != THE_WARNING_FUNCTION
    ]
    assert not offenders, (
        "these emit a warning `log` event directly, bypassing `_warn` and "
        "therefore the stderr half of the warning: " + "; ".join(offenders)
    )


def test_warn_is_reachable_and_writes_both_channels(capsys: pytest.CaptureFixture[str]) -> None:
    """The assertions above are only worth anything if `_warn` does both."""
    from graft_ipa.cli import _warn

    _warn(JsonEmitter(), "the disk is nearly full")
    captured = capsys.readouterr()

    events = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert events == [{"type": "log", "source": "warning", "line": "the disk is nearly full"}]
    assert "the disk is nearly full" in captured.err


# ---------------------------------------------------------------------------
# `carried_by` — the suppression, and the claim it rests on
# ---------------------------------------------------------------------------


def test_a_warning_an_event_already_carries_is_not_said_twice(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The whole purpose of the parameter: one sentence, one panel, once."""
    from graft_ipa.cli import _warn

    _warn(JsonEmitter(), "3 binaries are encrypted", carried_by="encrypted-binaries")
    captured = capsys.readouterr()

    assert captured.out == "", "the structured event already said this"
    # stderr regardless: no event type reaches a terminal user at all.
    assert "3 binaries are encrypted" in captured.err


def test_a_warning_cannot_claim_to_be_carried_by_an_event_nobody_decodes() -> None:
    """The claim is checked, which is the difference from the boolean it replaces.

    `structured=True` asserted "a registered event already carries this to the
    GUI" and nothing anywhere tested it. Rename `livecontainer-choice`'s `emit`
    and the flag five lines below went on suppressing the `log` event for an
    event that no longer existed: the warning silently became stderr-only, on a
    path whose stderr the GUI reads only off a non-zero exit. Invisible
    everywhere, and no test failed.
    """
    from graft_ipa.cli import _warn

    with pytest.raises(ValueError, match="not an event the GUI decodes"):
        _warn(JsonEmitter(), "x", carried_by="livecontainer-choise")

    # A registered event is not enough either: CLI-only means nothing renders it.
    with pytest.raises(ValueError, match="not an event the GUI decodes"):
        _warn(JsonEmitter(), "x", carried_by="cli-contract")


def test_every_carried_by_names_an_event_the_gui_decodes() -> None:
    """The runtime check above only fires on a path something exercises.

    Statically, over the source, for the same reason the event registry is
    checked that way: a warning site nothing covers yet would otherwise ship
    with a name that suppresses its own only channel.
    """
    offenders: list[str] = []
    for node in ast.walk(cli_tree()):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "carried_by":
                continue
            assert isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str), (
                f"cli.py:{node.lineno}: carried_by is computed, not a literal — "
                "the claim can no longer be checked without running the line"
            )
            if kw.value.value not in GUI_EVENTS:
                offenders.append(f"cli.py:{node.lineno} claims {kw.value.value!r}")

    assert not offenders, (
        "these suppress the `log` half of a warning on the strength of an event "
        "that is unregistered or declared CLI-only, leaving the warning on a "
        "stderr the GUI reads only off a non-zero exit: " + "; ".join(offenders)
    )


# ---------------------------------------------------------------------------
# F5 — the site the assertion caught
# ---------------------------------------------------------------------------


def _events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]


def _listing(*, devices: list[MagicMock]) -> MagicMock:
    listing = MagicMock()
    listing.devices = devices
    failure = MagicMock()
    failure.udid, failure.error = "00008130-DEAD", "lockdownd said no"
    listing.failures = [failure]
    return listing


def test_a_device_that_could_not_be_queried_reaches_both_front_ends(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A phone usbmux saw and we failed to describe is not "no device connected".

    `device-error` is the structured half and it is no longer dropped: it has a
    `BuildEvent` case, `DevicesStore` keeps it beside the real devices, and
    `DevicesView` renders a sentence naming the UDID. So this site now answers
    `carried_by` the same way `_report_lc_choice` does three functions away —
    they used to disagree, on identical reasoning, with a test each pinning the
    opposite conclusion.
    """
    from graft_ipa import cli as cli_mod

    # A config with no default device, as `Config` would hold it: a bare
    # `MagicMock`'s truthy attribute reads as an unplugged default, which
    # `--devices` now names in a `device-seen` event (spec §5b).
    with patch.object(cli_mod, "list_devices", return_value=_listing(devices=[])):
        cli_mod._do_devices(MagicMock(default_device_udid=None), emitter=JsonEmitter())

    captured = capsys.readouterr()
    events = _events(captured.out)
    errors = [e for e in events if e["type"] == "device-error"]
    assert errors, "the GUI's only channel for a phone it cannot describe got nothing"
    assert errors[0]["udid"] == "00008130-DEAD"
    assert errors[0]["error"] == "lockdownd said no"
    assert not [e for e in events if e["type"] == "log"], (
        "the GUI decodes device-error and renders it, so a log event duplicates it"
    )
    # Still on stderr: the terminal has no event stream, and `--devices` is the
    # verb whose stderr the GUI reads least, which is why this half is not a swap.
    assert "00008130-DEAD" in captured.err
    assert "lockdownd said no" in captured.err


def test_the_warning_precedes_the_devices_it_is_about() -> None:
    """The sentence says "missing from the list below", so it must be above it.

    Asserted on the call order rather than on captured text: the two halves go
    to different streams, so nothing that reads `capsys` can see the sequence a
    terminal shows. Under `--json` the order carries no meaning anyway —
    `DevicesStore.load` collects the whole scan and partitions it, and
    `DevicesView` renders the problems above the list however they arrived.
    """
    from graft_ipa import cli as cli_mod

    good = MagicMock()
    good.udid, good.name, good.model = "00008130-OK", "iPhone", "iPhone16,1"
    good.ios_version, good.connection_type = "18.0", "usb"
    good.livecontainer_bundle_id = None
    order: list[str] = []

    with patch.object(cli_mod, "list_devices", return_value=_listing(devices=[good])), \
         patch.object(cli_mod, "_warn", side_effect=lambda *a, **k: order.append("warn")), \
         patch.object(cli_mod, "_print_devices", side_effect=lambda *a: order.append("table")):
        cli_mod._do_devices(MagicMock(), emitter=None)

    assert order == ["warn", "table"]


# ---------------------------------------------------------------------------
# F4 — the multi-LiveContainer note, on both push paths
# ---------------------------------------------------------------------------


def _lc_result(others: list[str]) -> MagicMock:
    result = MagicMock()
    result.bundle_id = "com.kdt.livecontainer.TEAM1"
    result.documents_path = "Documents/example.ipa"
    result.size_bytes = 1024
    result.others = others
    return result


def test_multiple_livecontainers_are_reported_once_on_each_channel(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """It used to be a `console.print` on stdout — i.e. on the event stream.

    The count rode in the `stage` event's extra fields, which `BuildProgress`
    drops. So under --json there was no channel at all: the user opened the
    LiveContainer they happened to have open, found nothing new in its
    Documents tab, and concluded the tweak had not built.

    An earlier revision asserted a `log` event alongside the structured one,
    because the GUI had no case for `livecontainer-choice` yet and the `log`
    line was the only thing it would render. It has one now, so the `log`
    event is not a fallback any more — it is the same sentence a second time
    in the same panel. stderr still carries it, because no event type reaches
    a terminal user at all.
    """
    from graft_ipa import cli as cli_mod

    cli_mod._report_lc_choice(
        JsonEmitter(), app_name="example", result=_lc_result(["com.kdt.LiveContainer2.TEAM1"])
    )

    captured = capsys.readouterr()
    events = _events(captured.out)
    choice = [e for e in events if e["type"] == "livecontainer-choice"]
    assert choice, "no structured event for the multi-LiveContainer case"
    assert choice[0]["bundle_id"] == "com.kdt.livecontainer.TEAM1"
    assert choice[0]["others"] == ["com.kdt.LiveContainer2.TEAM1"]
    assert not [e for e in events if e["type"] == "log"], (
        "the GUI decodes livecontainer-choice, so a log event duplicates it"
    )
    assert "--lc-bundle-id" in captured.err


def test_a_single_livecontainer_is_not_reported_at_all(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """There is no choice to report, and a line on every push trains people to
    skip lines."""
    from graft_ipa import cli as cli_mod

    cli_mod._report_lc_choice(JsonEmitter(), app_name="example", result=_lc_result([]))

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize("pinned", [False, True])
def test_the_choice_warning_names_the_pin_command_or_says_it_is_pinned(
    pinned: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """It told the user to edit `lc_bundle_id:` in config.yaml, and suggested a
    pin even to an app that already had one — what a real paid build with three
    LiveContainers printed. Unpinned, it names the command that sets a pin;
    pinned, it says the IPA went where the pin points."""
    from graft_ipa import cli as cli_mod

    cli_mod._report_lc_choice(
        None, app_name="example", result=_lc_result(["com.kdt.LiveContainer2.TEAM1"]),
        pinned=pinned,
    )

    said = " ".join(capsys.readouterr().err.split())
    assert "config.yaml" not in said
    if pinned:
        assert "the one it is pinned to" in said
        assert "--lc-bundle-id" not in said
    else:
        assert "graft --set-app-options --app example --lc-bundle-id <id>" in said
