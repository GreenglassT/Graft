"""The source scans the contract assertions share, in one place.

Four modules now assert something over the source of `graft_ipa/`, and three
of them are looking at the same calls. They had drifted apart in the way that
matters most: `test_cli_stage_contract` and `test_cli_warning_channel` matched
`{"emit", "emit_to_tee"}` while the registry check in `test_cli_describe`
matched `"emit"` alone — so `emit_to_tee("brand-new-type", …)` was caught by two
scanners and slipped past the one that decides whether the GUI can decode it.
That is the `device-error` class of hole: an event on the wire that nothing on
the other side has a case for.

So the scan is written once and each assertion filters it. The literalness
check lives here too, because it is the premise every one of them rests on: a
computed event type cannot be checked statically at all.

Parsing is cached. `cli.py` is 3k lines and the assertions re-read it eight
times between them; at ~36 ms a parse that was a measurable slice of the suite.

The Swift half is here for the same reason and after the same drift. Two
modules read `CLIContract.swift` for string literals; the stage scanner dropped
comment lines before looking and the event scanner did not, so deleting
`"device-error"` from `eventsHandled` and leaving `// "device-error" moved to a
store` inside the literal left both partition tests green over an event the GUI
had stopped decoding — the very event whose disappearance those tests were
written after. One stripper, both callers.
"""

from __future__ import annotations

import ast
import functools
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PACKAGE_DIR = REPO_ROOT / "graft_ipa"
CLI_SOURCE = PACKAGE_DIR / "cli.py"

CONTRACT_SWIFT = REPO_ROOT / "Graft" / "Graft" / "Models" / "CLIContract.swift"

# One Swift string literal. Both Swift scans read names with it.
SWIFT_STRING = re.compile(r'"([^"\\]*)"')


def strip_swift_comments(text: str) -> str:
    """`text` with `//` and `/* … */` comments blanked out, string literals kept.

    A quoted name inside a comment is not a declaration, and a scan that reads
    one is reading the argument *against* the thing it is asserting. Quote state
    is tracked rather than assumed, so a `//` inside a string literal is not
    mistaken for a comment; newlines survive so the line- and brace-oriented
    scans downstream keep their shape.

    Wider than the whole-comment-line filter it replaces, which a trailing
    `"app", // "device-error" moved to a store` walks straight past.
    """
    out: list[str] = []
    i, n = 0, len(text)
    in_string = in_block = False
    while i < n:
        ch = text[i]
        if in_block:
            out.append("\n" if ch == "\n" else " ")
            if text.startswith("*/", i):
                in_block = False
                out.append(" ")
                i += 2
                continue
            i += 1
        elif in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
        elif ch == '"':
            in_string = True
            out.append(ch)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            in_block = True
            out.append("  ")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def contract_swift_source() -> str:
    """`CLIContract.swift` with its commentary removed. The one reader."""
    return strip_swift_comments(CONTRACT_SWIFT.read_text())

# Both ways an event reaches a consumer. `emit_to_tee` is not a niche cousin:
# it replays `build-start` into a log file that is opened after the build has
# already begun (json_output.py), and it has a live call site on the build path.
EMIT_METHODS = frozenset({"emit", "emit_to_tee"})


@functools.cache
def parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def cli_tree() -> ast.Module:
    return parse(CLI_SOURCE)


@functools.cache
def enclosing_functions(path: Path) -> dict[ast.AST, str]:
    """Map every node in `path` to the name of the innermost function holding it."""
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                else current
            )
            owner[child] = name
            walk(child, name)

    walk(parse(path), "<module>")
    return owner


@dataclass(frozen=True)
class EmitCall:
    """One `emit`/`emit_to_tee` call, with its event type already read off."""

    path: Path
    node: ast.Call
    event_type: str

    @property
    def where(self) -> str:
        return f"{self.path.name}:{self.node.lineno}"

    @property
    def function(self) -> str:
        """The innermost function this call sits in."""
        return enclosing_functions(self.path).get(self.node, "<module>")

    def keyword(self, name: str) -> ast.expr | None:
        return next((kw.value for kw in self.node.keywords if kw.arg == name), None)

    def literal_keyword(self, name: str) -> str | None:
        """The value of `name=` if it is a string literal, else None."""
        value = self.keyword(name)
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return value.value
        return None


def _event_type_of(node: ast.Call, path: Path) -> str:
    """The literal event type of one emit call, or a loud failure.

    `emit(self, event_type: str, **fields)` accepts its first argument by
    keyword, and the scan used to drop any call with no positional arguments
    before it ever reached the literalness check every downstream assertion
    declares as its premise. So

        emitter.emit(event_type="log", source="warning", line=msg)

    was invisible to all four of them at once: a hand-rolled warning skipping
    `_warn`'s stderr half, an event type in neither `EVENT_TYPES` nor
    `CLIContract.eventsHandled`, and an unregistered `stage` name all shipped
    green — and the GUI's `try?` decoder then drops the line without a trace.
    That is the `device-error` failure these scans exist to make impossible,
    reachable through a spelling `JsonEmitter.emit` accepts.

    Every shape that is not a resolvable string literal fails here rather than
    being skipped, because "the scanner could not read this call" and "there is
    nothing to check" are the same silence, and only one of them is true.
    """
    if node.args:
        first = node.args[0]
        assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
            f"{path.name}:{node.lineno}: {node.func.attr}() called with a non-literal "
            "event type — the registry can no longer be checked statically"
        )
        return first.value

    keyword = next((kw for kw in node.keywords if kw.arg == "event_type"), None)
    assert keyword is not None, (
        f"{path.name}:{node.lineno}: {node.func.attr}() with no positional argument "
        "and no `event_type=` — this scan cannot tell what goes on the wire. If "
        "the event type is being splatted in from a dict, name it literally: "
        "every assertion over these calls rests on being able to read it."
    )
    assert isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str), (
        f"{path.name}:{node.lineno}: {node.func.attr}(event_type=…) is computed, not "
        "a literal — the registry can no longer be checked statically"
    )
    return keyword.value.value


def emit_calls_in(tree: ast.Module, path: Path) -> list[EmitCall]:
    calls: list[EmitCall] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in EMIT_METHODS:
            continue
        calls.append(
            EmitCall(path=path, node=node, event_type=_event_type_of(node, path))
        )
    return calls


@functools.cache
def emit_calls(package_dir: Path = PACKAGE_DIR) -> tuple[EmitCall, ...]:
    """Every `emit`/`emit_to_tee` call in the package, in file order."""
    found: list[EmitCall] = []
    for path in sorted(package_dir.glob("*.py")):
        found.extend(emit_calls_in(parse(path), path))
    return tuple(found)
