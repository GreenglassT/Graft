from __future__ import annotations

import io
import json

import pytest

from graft_ipa.json_output import EVENT_TYPES, JsonEmitter


def test_emit_writes_one_json_object_per_line() -> None:
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    em.emit("stage", name="cyan", status="start")
    em.emit("log", line="injecting tweaks")
    em.emit("stage", name="cyan", status="end", ok=True)

    lines = buf.getvalue().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"type": "stage", "name": "cyan", "status": "start"}
    assert json.loads(lines[1]) == {"type": "log", "line": "injecting tweaks"}
    assert json.loads(lines[2]) == {"type": "stage", "name": "cyan", "status": "end", "ok": True}


def test_emit_flushes_each_line() -> None:
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    em.emit("log", line="first")
    flushed = [0]
    real_flush = buf.flush
    def counting_flush() -> None:
        flushed[0] += 1
        real_flush()
    buf.flush = counting_flush
    em.emit("log", line="second")
    assert flushed[0] >= 1


def test_emit_rejects_an_unregistered_event_type() -> None:
    """A renamed or brand-new event must not reach the wire unregistered.

    Swift's `String.buildEvents()` decodes with `try?`, so an event the GUI
    doesn't know about is silently dropped and the UI simply shows nothing.
    Failing here makes that a loud CLI-side error instead.
    """
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    with pytest.raises(ValueError, match="unregistered event type"):
        em.emit("build-startt", app="example")
    assert buf.getvalue() == "", "a rejected event must not be written"


def test_emit_rejects_an_unregistered_stage_name() -> None:
    """The `stage` event's `name` is a second vocabulary inside the first.

    Registering the event type proves the GUI recognizes the line; it says
    nothing about the string inside it, and `ConfigStore.note` branches on one
    of those strings — `"verify"` — for the decryption check's only all-clear.
    A renamed stage keeps decoding and keeps rendering, so the CLI is the only
    place the mistake can still be made loud.
    """
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    with pytest.raises(ValueError, match="unregistered stage name"):
        em.emit("stage", name="verifyy", status="start")
    assert buf.getvalue() == "", "a rejected stage must not be written"


def test_emit_rejects_a_stage_with_no_name_at_all() -> None:
    """Swift decodes a missing `name` as `""`, which matches nothing.

    Same silent outcome as a misspelling — a stage row with no label in the
    transcript and a `where name == …` that never fires — so it is rejected by
    the same guard rather than treated as a different kind of mistake.
    """
    buf = io.StringIO()
    em = JsonEmitter(stream=buf)
    with pytest.raises(ValueError, match="unregistered stage name"):
        em.emit("stage", status="start")
    assert buf.getvalue() == ""


def test_registry_covers_the_documented_contract_events() -> None:
    assert "cli-contract" in EVENT_TYPES
    assert {"app", "app-updated", "build-start", "build-end", "stage"} <= EVENT_TYPES
