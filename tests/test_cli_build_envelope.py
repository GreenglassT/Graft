"""GLOBAL ASSERTION 2 — nothing that can fail may run before `build-start`.

`_BuildReport`'s docstring promises that `build-start` goes out in `__enter__`
"so nothing can run ahead of it". That promise was true of the re-install path
and false of the build path for as long as `_build_one` opened with
`app_cfg = cfg.get_app(app_name)`: an app removed from config.yaml while the GUI
was open raised `ConfigError` before the `with`, and the GUI got zero JSON
events, an empty transcript and no history row. The same class, one door over,
put `BuildLog(...)` in `main` ahead of the loop — a `mkdir` plus an
`executescript` in the very state directory `open_tee` had been hardened
against, failing before any app was attempted.

Both are invisible to a test written against the envelope, because from inside
the envelope everything looks fine. So the shape is asserted over the source:

  For every function a build starts in, everything evaluated before the first
  build begins must be lookup-only.

THE ENTRY POINTS ARE DECLARED, NOT DISCOVERED. This assertion used to find them
by asking which functions *contain* a `with _BuildReport(...)` — a set of two,
`_build_one` and `_do_reinstall_build`. The `BuildLog(...)` defect its own
docstring cites lived in neither: it was in the dispatcher, `main`'s fall-through
verb, now `_verb_build_apps`. Discovering entry points by "contains a `with`" is
precisely the rule that excludes the caller, so `_BUILD_ENTRY_POINTS` names all
three and a separate assertion checks that nothing has opened an envelope
without being added to it.

TWO RULES, BECAUSE THE TWO LAYERS OWE DIFFERENT THINGS.

Inside a build function the envelope must come first, so the prologue is held to
lookup-only: attribute reads, names, constants, conditional expressions, and the
handful of calls declared total by exact spelling in `TOTAL_CALLS`. Anything
else — a call, a subscript, a branch, a `raise` — belongs inside the block,
where `build-end` can report it. "Before `__enter__`" includes the `with` header
itself, since Python evaluates the context expression first; writing the lookup
as `cfg.get_app(app_name)` *inside* the header would move the guard by one line
and change nothing at all about when it raises.

In the dispatcher there is no envelope to be first — the boundary is the loop
that calls `_build_one` — and a guard that returns before any build is attempted
is correct there, not a violation. What must not happen is *work*: the rule is
that nothing may be called ahead of the loop but a callable declared inert in
`PROLOGUE_INERT_CALLS`, with the reason it cannot fail. `BuildLog(...)` is not
on that list, which is the whole point.

A function that genuinely cannot comply declares itself in
`ENVELOPE_PROLOGUE_EXEMPT` with the reason. There is exactly one, and it is
there because it has no app name to put in `build-start` until it has read the
database. An exemption is a paragraph somebody has to write and somebody has to
read, which is the difference between this and silence.
"""

from __future__ import annotations

import ast
import functools
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa.build_log import BuildLog
from graft_ipa.config import (
    AppConfig,
    Config,
    CyanOptions,
    SigningConfig,
)
from graft_ipa.errors import ConfigError
from graft_ipa.json_output import JsonEmitter
from tests.cli_ast import PACKAGE_DIR, cli_tree, parse

ENVELOPE_CLASS = "_BuildReport"

# Every function a build starts in. Declared rather than discovered: the defect
# this assertion cites (`BuildLog(...)` ahead of the loop) lived in the third
# one, which contains no `with _BuildReport(...)` and so was invisible to a scan
# that looked for one. `test_no_envelope_is_opened_outside_a_declared_entry_point`
# keeps the declaration honest in the other direction.
_BUILD_ENTRY_POINTS: frozenset[str] = frozenset(
    {"_build_one", "_do_reinstall_build", "_verb_build_apps"}
)

# Calls that cannot raise for any argument, by their exact spelling. Matching on
# the attribute name alone (`{"get"}`) read `session.get(url)` as total — a
# network round trip scored as a dictionary lookup. `cfg.apps.get` is how a
# build names an app it may not have, which is the one lookup the envelope's own
# arguments need.
TOTAL_CALLS: frozenset[str] = frozenset({"cfg.apps.get"})

# Callables the dispatcher may run before the first build, with the reason each
# cannot fail. This is the list `BuildLog` is not on.
PROLOGUE_INERT_CALLS: dict[str, str] = {
    "JsonEmitter": (
        "`__init__` binds `sys.stdout` and a tee of None and does nothing else "
        "— no file, no directory, no database. It is the one object the "
        "dispatcher must hold before the loop, because every app in the loop "
        "writes to the same stream."
    ),
    "console.print": (
        "The usage error, which is the one thing that can be said before a "
        "build starts because it is the case where no build was asked for. "
        "Arguments are held to the same lookup-only rule as the envelope "
        "prologue, so it cannot interpolate something that raises on the way in."
    ),
    "err_console.print": (
        "The same usage error under --json, where stdout is the event stream: "
        "the same console class pointed at stderr, and the same arguments."
    ),
}

# Expression nodes allowed before `__enter__`.
LOOKUP_ONLY_NODES = (
    ast.Name,
    ast.Attribute,  # narrowed by _package_properties() below
    ast.Constant,
    ast.IfExp,
    ast.keyword,
    ast.Load,
    ast.Store,
    ast.Call,  # narrowed by TOTAL_CALLS above
)

ENVELOPE_PROLOGUE_EXEMPT: dict[str, str] = {
    "_do_reinstall_build": (
        "It is handed a build id, not an app name, and `build-start` carries "
        "the app. Reading the row is what tells it which app this is, so the "
        "read cannot move inside the envelope without emitting a `build-start` "
        "for nothing — which is worse than none, because the GUI opens a build "
        "panel on it. The row lookup, and only the row lookup, stays above; "
        "every guard that follows it (missing app, unsuccessful build, missing "
        "archive) is inside, and that placement is itself under test in "
        "tests/test_cli_reinstall.py."
    ),
}

# An attribute read is only a lookup if it reads a field. A `@property` is a
# method call wearing a field's clothes and can raise like one, so a prologue
# attribute whose name is a property anywhere in `graft_ipa/` is flagged —
# name-based, since nothing here resolves types. A collision with a plain field
# is excused here, with the argument for why it is not a property.
PROLOGUE_PROPERTY_EXEMPT: dict[str, str] = {
    "apps": (
        "`retention.PrunePlan.apps` is a property, but the prologue read is "
        "`cfg.apps` — a `Config` dataclass field, and a plain dict. The two "
        "share a name and nothing else. If `Config.apps` ever becomes a "
        "property, delete this entry rather than widening it."
    ),
}


def _cli_tree() -> ast.Module:
    return cli_tree()


@functools.cache
def _package_properties() -> frozenset[str]:
    """Every name defined with `@property` anywhere in `graft_ipa/`."""
    names: set[str] = set()
    for path in sorted(PACKAGE_DIR.glob("*.py")):
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.FunctionDef):
                continue
            if any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list):
                names.add(node.name)
    return frozenset(names)


def _is_envelope_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == ENVELOPE_CLASS
    )


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}


def _opens_an_envelope(node: ast.AST) -> bool:
    return isinstance(node, ast.With) and any(
        any(_is_envelope_call(sub) for sub in ast.walk(item.context_expr))
        for item in node.items
    )


def _body_after_docstring(fn: ast.FunctionDef) -> list[ast.stmt]:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            return body[1:]
    return body


def _boundary(fn: ast.FunctionDef) -> tuple[ast.stmt | None, str]:
    """The statement the prologue ends at, and which rule this function is under.

    `envelope` for a function that enters a `_BuildReport` itself; `dispatch` for
    one that reaches a build by calling another entry point — for which the
    boundary is the statement that makes the call.
    """
    body = _body_after_docstring(fn)
    for stmt in body:
        if _opens_an_envelope(stmt):
            return stmt, "envelope"
    others = _BUILD_ENTRY_POINTS - {fn.name}
    for stmt in body:
        if any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id in others
            for sub in ast.walk(stmt)
        ):
            return stmt, "dispatch"
    return None, "none"


def _effectful_nodes(node: ast.AST) -> list[str]:
    """Everything in `node` that can run code: calls, and property reads."""
    bad: list[str] = []
    properties = _package_properties()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if _is_envelope_call(sub):
                continue
            name = ast.unparse(sub.func)
            if name in TOTAL_CALLS or name in PROLOGUE_INERT_CALLS:
                continue
            bad.append(f"call to {name}() on line {sub.lineno}")
        elif isinstance(sub, ast.Attribute):
            if sub.attr in properties and sub.attr not in PROLOGUE_PROPERTY_EXEMPT:
                bad.append(
                    f"the @property `.{sub.attr}` on line {sub.lineno} (a call wearing "
                    "a field's clothes)"
                )
    return bad


def _non_lookup_nodes(node: ast.AST) -> list[str]:
    """`_effectful_nodes`, plus the node kinds that have no business in a prologue."""
    bad = _effectful_nodes(node)
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call | ast.Attribute):
            continue
        if not isinstance(sub, LOOKUP_ONLY_NODES):
            bad.append(f"{type(sub).__name__} on line {sub.lineno}")
    return bad


def _inert_call_arguments(node: ast.AST) -> list[str]:
    """A declared-inert call is only inert for arguments that cannot run code."""
    bad: list[str] = []
    for sub in ast.walk(node):
        if not (isinstance(sub, ast.Call) and ast.unparse(sub.func) in PROLOGUE_INERT_CALLS):
            continue
        name = ast.unparse(sub.func)
        for arg in [*sub.args, *(kw.value for kw in sub.keywords)]:
            bad += [f"{d}, as an argument to {name}()" for d in _non_lookup_nodes(arg)]
    return bad


def test_every_build_envelope_is_entered_before_anything_that_can_fail() -> None:
    tree = _cli_tree()
    functions = _functions(tree)
    problems: list[str] = []

    for name in sorted(_BUILD_ENTRY_POINTS):
        if name in ENVELOPE_PROLOGUE_EXEMPT:
            continue
        fn = functions[name]
        boundary, rule = _boundary(fn)
        body = _body_after_docstring(fn)
        if boundary is None:
            problems.append(
                f"{name}: declared a build entry point but neither enters a "
                f"{ENVELOPE_CLASS} nor calls one that does — either the build moved "
                "or the declaration is stale"
            )
            continue
        if boundary not in body:
            problems.append(
                f"{name}: the build is started from inside another statement "
                f"(line {boundary.lineno}); whatever it is nested in runs outside "
                "the envelope"
            )
            continue
        prologue = body[: body.index(boundary)]

        if rule == "dispatch":
            # No envelope of its own to be first: the rule is that nothing may
            # *work* before the loop. `BuildLog(...)` here did a mkdir and an
            # executescript in the state directory and failed before the first
            # app was attempted — no build event for any of them.
            for stmt in prologue:
                problems += [
                    f"{name}: {d} runs before the first build starts"
                    for d in _effectful_nodes(stmt) + _inert_call_arguments(stmt)
                ]
            continue

        for stmt in prologue:
            if not isinstance(stmt, ast.Assign | ast.AnnAssign):
                problems.append(
                    f"{name}: {type(stmt).__name__} on line {stmt.lineno} runs before "
                    "build-start"
                )
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if not all(isinstance(t, ast.Name) for t in targets):
                problems.append(
                    f"{name}: the assignment on line {stmt.lineno} writes somewhere "
                    "other than a plain local, which can run arbitrary code"
                )
            if stmt.value is not None:
                problems += [
                    f"{name}: {d} runs before build-start"
                    for d in _non_lookup_nodes(stmt.value)
                ]
        for item in boundary.items:
            problems += [
                f"{name}: {d} is in the `with` header, which Python evaluates "
                "before __enter__"
                for d in _non_lookup_nodes(item.context_expr)
            ]

    assert not problems, (
        "these run ahead of `build-start`, so when they raise the GUI gets no "
        "build event at all — no transcript, no history row, and a failure it "
        "cannot attribute to the app it asked for:\n  " + "\n  ".join(problems)
    )


def test_the_declared_entry_points_are_the_real_ones() -> None:
    """Both directions, because the declaration is what the scan above trusts.

    A name that no longer exists makes the scan quietly skip a layer; a function
    that opens an envelope without being declared is a whole build path nothing
    checks — which is exactly the state `_verb_build_apps` was in.
    """
    functions = _functions(_cli_tree())
    missing = sorted(_BUILD_ENTRY_POINTS - set(functions))
    assert not missing, f"_BUILD_ENTRY_POINTS names functions cli.py does not have: {missing}"

    opens_envelope = {
        name
        for name, fn in functions.items()
        if any(_opens_an_envelope(node) for node in ast.walk(fn))
    }
    undeclared = sorted(opens_envelope - _BUILD_ENTRY_POINTS)
    assert not undeclared, (
        f"these enter a {ENVELOPE_CLASS} but are not in _BUILD_ENTRY_POINTS: "
        f"{undeclared}. Everything above their `with` is unchecked."
    )
    assert opens_envelope, (
        f"found no `with {ENVELOPE_CLASS}(...)` anywhere — has the envelope been "
        "renamed? The scan above would then be checking prologues against nothing."
    )


def test_every_declaration_that_excuses_something_gives_a_reason() -> None:
    """An exemption that has outlived its subject is an exemption nobody reads."""
    for name, reason in ENVELOPE_PROLOGUE_EXEMPT.items():
        assert name in _BUILD_ENTRY_POINTS, (
            f"{name} is exempted from the envelope rule but is not a build entry "
            "point — delete the exemption"
        )
        assert reason.strip(), f"{name}'s exemption needs an argument"
    for name, reason in PROLOGUE_INERT_CALLS.items():
        assert reason.strip(), f"{name} is declared inert with no argument for why"
    for name, reason in PROLOGUE_PROPERTY_EXEMPT.items():
        assert name in _package_properties(), (
            f"{name} is excused from the @property rule but is no longer a property "
            "anywhere in the package — delete the entry"
        )
        assert reason.strip(), f"{name}'s property exemption needs an argument"


# ---------------------------------------------------------------------------
# F2 — the site the assertion caught
# ---------------------------------------------------------------------------


def _cfg(tmp_path: Path) -> Config:
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12",
            cert_password_env="X",
            profile=tmp_path / "profile",
            team_id="TEAM",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={
            "bravo": AppConfig(
                name="bravo",
                bundle_id="com.bravo.app",
                preferred_version="latest",
                tweaks=[],
                cyan=CyanOptions(),
                delivery="livecontainer",
            )
        },
    )


def _events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]


def test_an_app_missing_from_the_config_still_starts_and_ends(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Delete an app from config.yaml with the GUI open, then press Build.

    `cfg.get_app` raised on the first line of `_build_one`, ahead of the
    envelope: no build-start, no build-end, an empty transcript and no history
    row. The re-install path had already been fixed for this exact shape.
    """
    from graft_ipa import cli as cli_mod

    with pytest.raises(ConfigError):
        cli_mod._build_one(
            cfg=_cfg(tmp_path), app_name="ghost", dry_run=False,
            skip_install=True, skip_decrypt_check=True,
            verbose=False, device=None, emitter=JsonEmitter(),
        )

    events = _events(capsys.readouterr().out)
    assert [e["type"] for e in events] == ["build-start", "build-end"]
    assert events[0]["app"] == "ghost"
    assert events[1]["ok"] is False
    assert "not configured" in events[1]["error"]


def test_the_error_for_a_missing_app_keeps_its_hint(tmp_path: Path) -> None:
    """Moving the guard inside the envelope must not cost the message.

    The temptation is to re-raise something generic from inside the block. The
    hint names the apps that *are* configured, which is the whole diagnosis
    when the failure is a typo.
    """
    from graft_ipa import cli as cli_mod

    with pytest.raises(ConfigError) as excinfo:
        cli_mod._build_one(
            cfg=_cfg(tmp_path), app_name="ghost", dry_run=False,
            skip_install=True, skip_decrypt_check=True,
            verbose=False, device=None, emitter=None,
        )

    assert "bravo" in (excinfo.value.hint or "")


# ---------------------------------------------------------------------------
# F3 — the same class, reached through `main`
# ---------------------------------------------------------------------------


def test_a_state_dir_that_will_not_open_is_a_failed_build_not_a_traceback(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`BuildLog(...)` used to be constructed in `main`, ahead of the loop.

    Its `__init__` does a `mkdir` and an `executescript` in the same state
    directory the tee lives in, so a read-only or full volume made it raise
    before the first app was attempted — no build event for ANY named app,
    which is precisely the failure `open_tee` was hardened against, reached
    through a different door.

    Driven through `main` on purpose: the defect was in the caller, and a test
    that called `_build_one` directly would have been green the whole time.
    """
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod

    monkeypatch.setenv("HOME", str(tmp_path))
    # A plain file where the state directory belongs. `mkdir(exist_ok=True)`
    # only tolerates an existing *directory*, so this raises the same OSError
    # family a read-only or full volume would, without needing either.
    state = Path(tmp_path / "state" / "graft")
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("not a directory")

    result = CliRunner().invoke(
        cli_mod.app, ["--json", "--skip-install", "--skip-decrypt-check", "example"]
    )

    assert result.exit_code == 1
    types = [e["type"] for e in _events(result.stdout)]
    assert types[:1] == ["build-start"], f"no build event at all: {result.stdout!r}"
    assert types[-1] == "build-end"
    end = _events(result.stdout)[-1]
    assert end["ok"] is False
    assert end["app"] == "example"


# ---------------------------------------------------------------------------
# F1 — the tee outlives the build it belongs to
# ---------------------------------------------------------------------------


class _FullVolume:
    """A tee whose writes fail the way a filling volume's do: after the fact.

    `JsonEmitter.emit` writes stdout first and flushes, then writes the tee. So
    the GUI has already seen `build-end{ok:true}` by the time this raises, which
    is what makes the leak silent.
    """

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, line: str) -> int:
        self.lines.append(line)
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def test_a_tee_that_throws_on_the_terminal_event_is_still_detached(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from graft_ipa import cli as cli_mod

    emitter = JsonEmitter()
    volume = _FullVolume()
    with pytest.raises(OSError):
        with cli_mod._BuildReport(
            emitter, app_name="app1", bundle_id="com.one", delivery="livecontainer"
        ) as report:
            report.open_tee(tmp_path / "logs")
            # Substituted for the real handle: reaching into the private
            # attribute is how a volume that fills mid-build is simulated
            # without filling one.
            report._tee = volume
            emitter.tee = volume

    assert emitter.tee is None, (
        "the emitter still points at the finished build's log file; the next "
        "build's events would be appended to it"
    )
    assert volume.closed, "the handle leaked"


class _UncloseableTee(_FullVolume):
    """Writes fine, refuses to close. The other half of a failing volume.

    `close()` flushes, and a flush is a write. Closing before unhooking meant
    this raised with `emitter.tee` still set — the same leak, reached from the
    one line that was supposed to prevent it.
    """

    def write(self, line: str) -> int:
        self.lines.append(line)
        return len(line)

    def close(self) -> None:
        self.closed = True
        raise OSError(28, "No space left on device")


def test_a_tee_whose_close_fails_still_leaves_the_emitter_clean(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from graft_ipa import cli as cli_mod

    emitter = JsonEmitter()
    tee = _UncloseableTee()
    # No `pytest.raises`: a build that shipped must not be failed by the
    # closing of its own log file, and there is nothing buffered to lose —
    # every write to the tee is flushed at the moment it is made.
    with cli_mod._BuildReport(
        emitter, app_name="app1", bundle_id="com.one", delivery="livecontainer"
    ) as report:
        report.open_tee(tmp_path / "logs")
        report._tee = tee
        emitter.tee = tee

    assert tee.closed, "close was never attempted"
    assert emitter.tee is None, (
        "close() threw and took the unhook with it; the next build's events "
        "would be written to a closed handle belonging to this one"
    )
    ends = [e for e in _events(capsys.readouterr().out) if e["type"] == "build-end"]
    assert len(ends) == 1 and ends[0]["ok"] is True


def test_a_second_build_cannot_land_in_the_first_builds_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user-visible shape of it, in a multi-app `--json` run.

    The state volume fills during app1. `build-end{ok:true}` reaches the GUI,
    then `tee.write` raises — and with the close skipped, app2's `build-start`
    was appended to app1's `.jsonl`. Replaying app1's saved log showed a
    transcript that changed apps halfway through.
    """
    from graft_ipa import cli as cli_mod

    emitter = JsonEmitter()
    volume = _FullVolume()

    with pytest.raises(OSError):
        with cli_mod._BuildReport(
            emitter, app_name="app1", bundle_id="com.one", delivery="livecontainer"
        ) as report:
            report.open_tee(tmp_path / "logs")
            report._tee = volume
            emitter.tee = volume

    before = len(volume.lines)
    with cli_mod._BuildReport(
        emitter, app_name="app2", bundle_id="com.two", delivery="livecontainer"
    ):
        pass

    assert len(volume.lines) == before, (
        "app2 wrote into app1's log: " + repr(volume.lines[before:])
    )
    assert '"app2"' in capsys.readouterr().out, "app2 still reported on the live stream"


def test_the_terminal_event_survives_a_history_write_that_throws(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_record_history` swallows its own failures — but not a failure to warn.

    Rich raises MarkupError on a closing-tag-shaped token, and the warning
    `_record_history` uses to report a locked database interpolates the
    exception. Raised there it aborted `__exit__` before `build-end`: the one
    invariant this class exists to guarantee, lost to the reporting of a
    bookkeeping failure. The `finally` is what makes that unreachable whatever
    the warning does.
    """
    from graft_ipa import cli as cli_mod

    emitter = JsonEmitter()
    log = MagicMock()
    log.record.side_effect = sqlite3.OperationalError("database is locked")

    with patch.object(cli_mod, "_warn", side_effect=RuntimeError("the warning blew up")):
        with pytest.raises(RuntimeError):
            with cli_mod._BuildReport(
                emitter, app_name="app1", bundle_id="com.one", delivery="sideload"
            ) as report:
                report.log = log

    ends = [e for e in _events(capsys.readouterr().out) if e["type"] == "build-end"]
    assert len(ends) == 1 and ends[0]["ok"] is True


def test_a_normal_build_still_closes_its_tee_exactly_once(tmp_path: Path) -> None:
    """The `finally` must not leave the ordinary path double-closing or open."""
    from graft_ipa import cli as cli_mod

    emitter = JsonEmitter()
    with cli_mod._BuildReport(
        emitter, app_name="app1", bundle_id="com.one", delivery="livecontainer"
    ) as report:
        report.open_tee(tmp_path / "logs")
        path = report.log_path

    assert emitter.tee is None
    assert path is not None
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [e["type"] for e in lines] == ["build-start", "build-end"]


def test_the_history_row_is_still_written_from_inside_the_envelope(tmp_path: Path) -> None:
    """`log` and `tweaks` moved from constructor arguments to attributes.

    They are set from inside the block because opening log.sqlite and resolving
    the app both raise. This pins that the move did not quietly drop the row.
    """
    from graft_ipa import cli as cli_mod

    log = BuildLog(tmp_path / "log.sqlite")
    with cli_mod._BuildReport(
        None, app_name="bravo", bundle_id="com.bravo.app", delivery="sideload"
    ) as report:
        report.log = log
        report.tweaks = ["x.deb"]
        report.version = "1.2.3"

    row = log.recent(limit=1)[0]
    assert row["app"] == "bravo"
    assert row["version"] == "1.2.3"
    assert row["tweaks"] == "x.deb"
    assert row["success"] == 1
