"""Every run of ipatool goes through one helper (combined fix check,
medium #1).

`version`, `auth info` and `download` were given one environment and
`purchase` (--appstore-claim), merged separately, another: it went without
the bundled-Unicorn variable Graft.app then set. Graft.app no longer bundles
Unicorn (medium #2, option (a)), so the helper sets nothing of its own, but
the rule stays: one route, so whatever ipatool must be given reaches every
subcommand.

Two halves, so neither a new subcommand nor a new call site can slip by:
  - a source scan: nothing in graft_ipa starts ipatool except
    `ipatool.run_ipatool`, and every subcommand handed to it is listed here;
  - a fake ipatool inside a fake Graft.app that records what of the
    environment reached each run, for each of those subcommands.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from graft_ipa import ipatool
from tests.appstore_fakes import FakeTools

PACKAGE = Path(ipatool.__file__).resolve().parent

#: Calls that start a program, by dotted name.
_STARTERS = {
    *(f"subprocess.{n}" for n in ("run", "Popen", "call", "check_call", "check_output",
                                  "getoutput", "getstatusoutput")),
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
    *(f"os.{n}" for n in ("system", "popen", "posix_spawn", "posix_spawnp", "execv", "execve",
                          "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
                          "spawnv", "spawnve", "spawnvp", "spawnvpe", "spawnl", "spawnle",
                          "forkpty")),
    "pty.spawn", "pty.fork",
}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _functions(tree: ast.AST):
    """(function, call) for every call, with the innermost def around it."""
    def walk(node, owner):
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else owner
            if isinstance(child, ast.Call):
                yield inner, child
            yield from walk(child, inner)
    yield from walk(tree, None)


def _starts_a_program(call: ast.Call, module: str) -> bool:
    name = _dotted(call.func)
    # ipatool.py's functions take the runner as `run` (subprocess.run unless
    # a test injects one).
    return name in _STARTERS or (module == "ipatool" and name == "run")


def ipatool_starts() -> list[str]:
    """Every place in graft_ipa that starts a program and is about ipatool,
    outside `ipatool.run_ipatool`, as `module.function:line`."""
    found = []
    for py in sorted(PACKAGE.rglob("*.py")):
        source = py.read_text()
        tree = ast.parse(source)
        module = py.stem
        for owner, call in _functions(tree):
            if not _starts_a_program(call, module):
                continue
            if module == "ipatool" and owner is not None and owner.name == "run_ipatool":
                continue
            context = ast.get_source_segment(source, owner or call) or ""
            if module == "ipatool" or "ipatool" in context.lower():
                found.append(f"{module}.{owner.name if owner else '<module>'}:{call.lineno}")
    return found


def subcommands_in_source() -> set[str]:
    """The subcommand of every `run_ipatool(path, [...])` call in graft_ipa:
    its leading words ("auth info", "purchase", "--version")."""
    found = set()
    for py in sorted(PACKAGE.rglob("*.py")):
        for _, call in _functions(ast.parse(py.read_text())):
            if not _dotted(call.func).endswith("run_ipatool"):
                continue
            argv = call.args[1] if len(call.args) > 1 else None
            assert isinstance(argv, ast.List), f"{py.name}:{call.lineno}: pass run_ipatool a list literal"
            words = []
            for item in argv.elts:
                if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                    break
                if item.value.startswith("-") and words:
                    break
                words.append(item.value)
                if item.value.startswith("-"):
                    break
            found.add(" ".join(words))
    return found


def test_nothing_in_graft_ipa_starts_ipatool_but_run_ipatool():
    assert ipatool_starts() == [], (
        "these start ipatool without ipatool.run_ipatool, the one place its environment is built"
    )


def test_the_scan_sees_a_start_that_skips_the_helper(tmp_path, monkeypatch):
    """The scan's own control: a module that runs ipatool through subprocess
    directly is found."""
    pkg = tmp_path / "graft_ipa"
    pkg.mkdir()
    (pkg / "ipatool.py").write_text("def run_ipatool(path, args, *, run): return run([path, *args])\n")
    (pkg / "elsewhere.py").write_text(
        "import subprocess\n"
        "def revoke(ipatool_path):\n"
        "    subprocess.run([str(ipatool_path), 'auth', 'revoke'])\n"
    )
    monkeypatch.setattr(__import__(__name__, fromlist=["PACKAGE"]), "PACKAGE", pkg)
    assert ipatool_starts() == ["elsewhere.revoke:3"]


def _bundled(tmp_path: Path) -> FakeTools:
    return FakeTools(tmp_path / "Graft.app" / "Contents" / "Helpers" / "bin")


def _download(fakes: FakeTools, tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    ipatool.download(fakes.ipatool, "com.example.app", out)


def _sign_in(fakes: FakeTools, _: Path) -> None:
    """`auth login` the way `--appstore-login` runs it: under a pty."""
    from graft_ipa import appstore_login
    from graft_ipa.secret_input import SecretLine

    def line(value: bytes) -> SecretLine:
        held = SecretLine()
        held._buf[: len(value)] = value
        held._len = len(value)
        return held

    appstore_login.sign_in(
        fakes.ipatool, line(b"someone@example.invalid"), line(b"Example-Passw0rd"),
        lambda timeout: None, emitter=None, say=lambda _: None,
    )


#: How to make each subcommand run: the real caller of each, against the fake.
DRIVERS: dict[str, Callable[[FakeTools, Path], object]] = {
    "--version": lambda f, _: ipatool.version(f.ipatool),
    "auth info": lambda f, _: ipatool.signed_in(f.ipatool),
    "purchase": lambda f, _: ipatool.purchase(f.ipatool, "com.example.app"),
    "download": _download,
    "auth login": _sign_in,
    "auth revoke": lambda f, _: ipatool.revoke(f.ipatool),
}


def test_every_subcommand_in_the_source_has_a_driver_here():
    missing = subcommands_in_source() - set(DRIVERS)
    assert not missing, f"add a driver for {sorted(missing)} so its environment is checked"


@pytest.mark.parametrize("sub", sorted(DRIVERS))
def test_every_subcommand_gets_child_envs_environment_and_no_unicorn_variable(sub, tmp_path, monkeypatch):
    """The .p12 password variable is kept out (child_env), and Graft names no
    Unicorn library: the bundled ipatool fetches its own."""
    monkeypatch.delenv("IPATOOL_UNICORN_LIBRARY", raising=False)
    monkeypatch.setenv("GRAFT_P12_PASS", "a secret no tool needs")
    fakes = _bundled(tmp_path)
    fakes.set(login_two_factor=False)
    DRIVERS[sub](fakes, tmp_path)
    runs = [c for c in fakes.calls("ipatool") if not c.get("started")]
    ran = {" ".join(c["argv"][:2] if c["argv"][0] == "auth" else c["argv"][:1]) for c in runs}
    assert sub in ran, f"{sub} never ran: {runs}"
    assert {(c["unicorn"], c["p12_secret"]) for c in runs} == {(None, None)}, runs


def test_run_ipatool_refuses_an_environment_from_its_caller(tmp_path):
    with pytest.raises(TypeError, match="builds ipatool's environment itself"):
        ipatool.run_ipatool(_bundled(tmp_path).ipatool, ["--version"], env={})


def test_appstore_claim_runs_purchase_through_the_helper(temp_config_env, tmp_path, monkeypatch):
    """`graft --appstore-claim`, end to end with Graft.app's ipatool: the
    verb the review found running purchase its own way."""
    import yaml
    from typer.testing import CliRunner

    from graft_ipa import cli as cli_mod
    from graft_ipa import itunes

    monkeypatch.setenv("GRAFT_P12_PASS", "a secret no tool needs")
    fakes = _bundled(tmp_path)
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = fakes.appstore_section()
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    monkeypatch.setattr(itunes, "lookup_by_bundle_id", lambda bundle_id, *, country=None: itunes.ITunesApp(
        track_id=100000001, bundle_id=bundle_id, name="Example", version="2.0", price=0.0,
        formatted_price="Free"))
    seen = []
    real = ipatool.run_ipatool
    monkeypatch.setattr(ipatool, "run_ipatool", lambda path, args, **kw: seen.append(args[0]) or real(path, args, **kw))
    result = CliRunner().invoke(cli_mod.app, ["--json", "--appstore-claim", "--app", "example"])
    assert result.exit_code == 0, result.output
    assert "purchase" in seen, seen
    purchases = [c for c in fakes.calls("ipatool") if c["argv"][:1] == ["purchase"]]
    assert len(purchases) == 1 and purchases[0]["p12_secret"] is None, fakes.calls("ipatool")


# --- signin-2: no child but `auth login` inherits the CLI's stdin ------------
#
# `--appstore-login` reads the Apple ID and the password from stdin, and runs
# `ipatool --version` (the known-bad check) before it reads them: a
# `--version` that inherited fd 0 could read both. ipatool's other runs and
# `security`'s have no use for it either.

import contextlib  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402

#: What stands in for the unread lines on the CLI's stdin.
UNREAD = b"someone@example.invalid\nExample-Passw0rd\n"

_READS_STDIN = r'''
import json, sys
from pathlib import Path
got = sys.stdin.buffer.read()
with open(Path(__file__).with_name("stdin.log"), "ab") as f:
    f.write(repr(got).encode() + b"\n")
argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("version 2.8.0")
elif argv[:2] == ["auth", "info"]:
    print(json.dumps({"success": True, "email": "someone@example.invalid"}))
else:
    print(json.dumps({"success": True}))
'''


@contextlib.contextmanager
def _stdin_holding(data: bytes):
    """fd 0 is a pipe holding `data`, as the CLI's is while the app's lines
    wait to be read; put back after."""
    read_end, write_end = os.pipe()
    os.write(write_end, data)
    os.close(write_end)
    saved = os.dup(0)
    os.dup2(read_end, 0)
    os.close(read_end)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def _reader(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    (folder / "tool.py").write_text(_READS_STDIN)
    tool = folder / name
    tool.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{folder / "tool.py"}" "$@"\n')
    tool.chmod(0o755)
    return tool


def _logged(tool: Path) -> list[str]:
    return tool.with_name("stdin.log").read_text().splitlines()


@pytest.mark.parametrize("sub", ["--version", "auth info", "auth revoke", "purchase", "download"])
def test_no_ipatool_run_but_the_login_reads_the_cli_s_stdin(sub, tmp_path):
    tool = _reader(tmp_path, "ipatool")
    drive = {
        "--version": lambda: ipatool.version(tool),
        "auth info": lambda: ipatool.account(tool),
        "auth revoke": lambda: ipatool.revoke(tool),
        "purchase": lambda: ipatool.purchase(tool, "com.example.app"),
        "download": lambda: ipatool.download(tool, "com.example.app", tmp_path),
    }[sub]
    with _stdin_holding(UNREAD):
        with contextlib.suppress(ipatool.AppStoreError):  # download: "named no file"
            drive()
    assert _logged(tool) == ["b''"], "it read nothing of what waits on the CLI's stdin"


def test_the_premise_a_child_given_fd_0_reads_what_waits_there(tmp_path):
    tool = _reader(tmp_path, "ipatool")
    with _stdin_holding(UNREAD):
        subprocess.run([str(tool), "--version"], capture_output=True, check=True, timeout=30)
    assert _logged(tool) == [repr(UNREAD)]


def test_security_never_reads_the_cli_s_stdin(tmp_path, monkeypatch):
    """`keychain._run_security` itself, with `security` swapped for a reader
    by full path in the one `subprocess.run` it makes (never the real one),
    and the suite's switch off for this call only."""
    from graft_ipa import keychain

    tool = _reader(tmp_path, "stdin-reader")  # never named `security`: program_guard refuses that
    real = subprocess.run
    monkeypatch.delenv("GRAFT_NO_KEYCHAIN")
    monkeypatch.setattr(keychain.subprocess, "run", lambda argv, **kw: real([str(tool), *argv[1:]], **kw))
    with _stdin_holding(UNREAD):
        keychain._run_security(["security", "find-generic-password", "-s", "x", "-a", "y"],
                               capture_output=True, timeout=30)
        keychain._run_security(["security", "-i"], input=b"add-generic-password\n", capture_output=True,
                               timeout=30)
    assert _logged(tool) == ["b''", repr(b"add-generic-password\n")], "only what Graft gave it"
