"""Acceptance: on a bare PATH, with only the tools a Graft.app bundle ships,
`graft --check` is satisfied and never runs the Xcode tools or libimobiledevice.

The real CLI runs as a subprocess. Its interpreter path sits inside a fake
`Graft.app`, so `tools.bundled_tools_dir()` finds `Contents/Helpers/bin` there
for real, not through a monkeypatch. PATH is the system's own directories,
plus one whose Xcode and libimobiledevice tools record that they were run and
then fail; `DEVELOPER_DIR` names nothing, as on a Mac without the Command Line
Tools. A `sitecustomize` the child loads also logs every program it starts,
through an audit hook, so a tool run by absolute path (`/usr/bin/otool`),
which no PATH tripwire can see, is caught too, however it is started:
`subprocess`, `os.system`, `os.posix_spawn`, the `os.exec*` and `os.spawn*`
families, or a `/bin/sh -c` wrapper (every word of the command is logged).
It used to wrap `Popen.__init__` only, and an `os.system` probe of
`/usr/bin/xcode-select` passed both acceptance tests (re-check #36).

Both account modes, as the spec's acceptance asks: a free config, and a paid
one whose app sideloads, which is what brings zsign into `--check`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

from .conftest import NO_USBMUXD

REPO = Path(__file__).resolve().parents[1]

#: Programs a 2.0 `--check` must never start: the Xcode tools (and their
#: /usr/bin shims, which pop the install dialog on a Mac without them) and
#: libimobiledevice.
FORBIDDEN = (
    "otool", "xcrun", "xcode-select", "install_name_tool", "clang", "lipo",
    "ideviceinstaller", "idevice_id", "ideviceinfo", "iproxy",
)

#: Logs every process the child starts, one per line, every word of its
#: command, to $GRAFT_TEST_SPAWN_LOG. An audit hook sees the interpreter's own
#: spawn events, so no way of starting a program gets past it.
#:
#: It is a `sitecustomize`, so it shadows the interpreter's own one, and it
#: runs that one too: Homebrew's (in its stdlib directory) is what sets
#: sys.executable to its opt/bin path, which `_env`'s PYTHONEXECUTABLE is
#: there to stop. Shadowing it made that line dead code that a refactor of
#: the spy would have brought back to life as a failure on every Homebrew
#: Python (re-check #39).
_SPY = '''
import importlib.util, os, sys
_log = os.environ.get("GRAFT_TEST_SPAWN_LOG")

def _words(value):
    if isinstance(value, (str, bytes, os.PathLike)):
        return os.fsdecode(value).split()
    return [os.fsdecode(v) for v in value]

def _spy(event, args):
    if not _log:
        return
    if event == "subprocess.Popen":  # executable, args, cwd, env
        words = ([os.fsdecode(args[0])] if args[0] else []) + _words(args[1])
    elif event == "os.system":  # command
        words = _words(args[0])
    elif event in ("os.posix_spawn", "os.exec"):  # path, argv, env
        words = [os.fsdecode(args[0])] + _words(args[1])
    elif event == "os.spawn":  # mode, path, args, env
        words = [os.fsdecode(args[1])] + _words(args[2])
    else:
        return
    with open(_log, "a") as fh:
        fh.write(" ".join(words) + "\\n")

sys.addaudithook(_spy)

_here = os.path.dirname(os.path.abspath(__file__))
for _entry in sys.path:
    _shadowed = os.path.join(os.path.abspath(_entry or "."), "sitecustomize.py")
    if os.path.dirname(_shadowed) != _here and os.path.isfile(_shadowed):
        _spec = importlib.util.spec_from_file_location("_shadowed_sitecustomize", _shadowed)
        _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
        break
'''


def _exe(path: Path, body: str = "exit 0") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def fake_app(tmp_path: Path) -> dict[str, Path]:
    app = tmp_path / "Graft.app" / "Contents"
    py = app / "Frameworks" / "GraftPython" / "bin" / "python3"
    py.parent.mkdir(parents=True)
    py.symlink_to(Path(sys.executable).resolve())
    helpers = app / "Helpers" / "bin"
    for name in ("cyan", "zsign", "ipatool", "unfairplay"):
        _exe(helpers / name)
    trip = tmp_path / "tripwire"
    log = tmp_path / "tripped.log"
    for name in FORBIDDEN:
        _exe(trip / name, f'echo {name} >> "{log}"; exit 99')
    spy = tmp_path / "spy"
    spy.mkdir()
    (spy / "sitecustomize.py").write_text(_SPY)
    home = tmp_path / "home"
    home.mkdir()
    return {
        "python": py, "helpers": helpers, "trip": trip, "log": log, "home": home,
        "spy": spy, "spawned": tmp_path / "spawned.log",
    }


def _env(fake_app) -> dict[str, str]:
    home = fake_app["home"]
    env = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"), "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local/state"), "XDG_DATA_HOME": str(home / ".local/share"),
        "GRAFT_NO_KEYCHAIN": "1", "USBMUXD_SOCKET_ADDRESS": NO_USBMUXD,
        "PATH": f"{fake_app['trip']}:/usr/bin:/bin:/usr/sbin:/sbin",
        # As on a Mac without the Command Line Tools.
        "DEVELOPER_DIR": "/nonexistent-dev",
        "GRAFT_TEST_SPAWN_LOG": str(fake_app["spawned"]),
        # The symlinked interpreter has no venv of its own: hand it the repo
        # and this venv's site-packages, after the spy.
        "PYTHONPATH": os.pathsep.join(
            [str(fake_app["spy"]), str(REPO), sysconfig.get_paths()["purelib"]]
        ),
    }
    # Homebrew's own sitecustomize (which the spy runs) sets sys.executable
    # to /opt/homebrew/opt/python@3.x/bin/python3.x, outside the fake bundle,
    # unless this is set or the interpreter is in a venv, which the bundle's
    # is not; then no bundled tool is ever found (review #38). Not a matter of
    # framework builds: Xcode's framework Python reports the in-bundle path
    # unaided (re-check #39). Set whatever the interpreter: on macOS it is the
    # path the interpreter was started as, which is the true answer anyway.
    env["PYTHONEXECUTABLE"] = str(fake_app["python"])
    return env


def _run(fake_app, *args: str) -> subprocess.CompletedProcess:
    # `cwd`: under `-m`, the working directory comes ahead of PYTHONPATH, so
    # without it the child imported whatever graft_ipa sat in pytest's
    # directory, such as the main checkout's when a worktree nested inside
    # it was tested from there (review #27).
    return subprocess.run([str(fake_app["python"]), "-B", "-m", "graft_ipa", *args],
                          capture_output=True, text=True, env=_env(fake_app), timeout=120,
                          cwd=fake_app["home"])


def _rows(proc: subprocess.CompletedProcess) -> list[dict]:
    return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]


def _forbidden_runs(fake_app) -> list[str]:
    tripped = fake_app["log"].read_text().split() if fake_app["log"].exists() else []
    spawned = fake_app["spawned"].read_text().split() if fake_app["spawned"].exists() else []
    return tripped + [p for p in spawned if Path(p).name in FORBIDDEN]


def _write_config(fake_app, text: str) -> None:
    cfg = fake_app["home"] / ".config" / "graft" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text)


def test_the_child_runs_from_inside_the_fake_bundle(fake_app):
    """The premise every other test here rests on, in the environment they
    run the CLI in, checked so a failure says which part broke: on a
    framework Python this was a bare `assert False` (review #38)."""
    probe = subprocess.run(
        [str(fake_app["python"]), "-c", "import sys; print(sys.executable)"],
        capture_output=True, text=True, cwd=fake_app["home"], env=_env(fake_app),
    )
    assert probe.stdout.strip() == str(fake_app["python"]), (
        f"the bundle's interpreter reports sys.executable={probe.stdout.strip()!r}, "
        "so bundled_tools_dir() finds no bundle"
    )


def test_check_finds_every_tool_in_the_bundle_and_never_runs_otool(fake_app):
    _write_config(fake_app, "apps: {}\n")  # a free account
    proc = _run(fake_app, "--check", "--json")
    rows = _rows(proc)
    tool_rows = {r["name"]: r for r in rows if r.get("type") == "preflight-check" and r["name"] in ("cyan", "zsign")}
    assert "cyan" in tool_rows, f"no cyan row; stderr: {proc.stderr!r}; rows: {rows}"
    assert tool_rows["cyan"]["ok"] and tool_rows["cyan"]["source"] == "bundled", tool_rows["cyan"]
    names = {r.get("name") for r in rows}
    assert "otool" not in names and "ideviceinstaller" not in names
    assert _forbidden_runs(fake_app) == []


def test_a_paid_check_finds_zsign_in_the_bundle_and_runs_no_xcode_tool(fake_app):
    """Review #32: only free mode was covered, so zsign resolved past the
    bundle, or a re-added Xcode probe, would have stayed green."""
    home = fake_app["home"]
    _write_config(fake_app, (
        "signing:\n"
        f"  cert: {home}/cert.p12\n"
        "  cert_password_env: GRAFT_P12_PASS\n"
        f"  profile: {home}/profile.mobileprovision\n"
        "  team_id: ABCDE12345\n"
        "apps:\n"
        "  example:\n"
        "    bundle_id: com.example.app\n"
        "    delivery: sideload\n"
    ))
    proc = _run(fake_app, "--check", "--json")
    rows = _rows(proc)
    tools = {r["name"]: r for r in rows if r.get("type") == "preflight-check" and r["name"] in ("cyan", "zsign")}
    assert set(tools) == {"cyan", "zsign"}, f"stderr: {proc.stderr!r}; rows: {rows}"
    for name in ("cyan", "zsign"):
        assert tools[name]["ok"] and tools[name]["source"] == "bundled", tools[name]
        assert tools[name]["detail"].startswith(str(fake_app["helpers"])), tools[name]
    assert _forbidden_runs(fake_app) == []


def test_the_child_imports_this_tree_whatever_pytest_s_directory_holds(
    fake_app, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Review #27: a graft_ipa in pytest's working directory was the one the
    child ran."""
    decoy = tmp_path / "elsewhere"
    (decoy / "graft_ipa").mkdir(parents=True)
    (decoy / "graft_ipa" / "__init__.py").write_text("")
    (decoy / "graft_ipa" / "__main__.py").write_text("print('DECOY')\n")
    monkeypatch.chdir(decoy)

    proc = _run(fake_app, "--version")

    assert "DECOY" not in proc.stdout
    assert proc.returncode == 0, proc.stderr
