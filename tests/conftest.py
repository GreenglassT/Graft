"""Shared pytest fixtures for the graft CLI test suite."""

from __future__ import annotations

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from .config_fixtures import write_test_config

REPO_ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _isolated_xdg_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own XDG roots, whether it asked for them or not.

    Not optional since the rename: the CLI's first act is to move a legacy
    `…/tweak-build` directory to `…/graft` and leave a symlink behind. A test
    that invoked `main` with only `--config` pointed somewhere safe, and
    inherited the developer's real XDG roots, would have migrated the
    developer's real config, cache, state and data directories. Tests that set
    their own roots (`temp_config_env`, the subprocess fixtures) override
    these; everything else lands under `tmp_path`.
    """
    for var, name in (
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_DATA_HOME", "xdg-data"),
    ):
        monkeypatch.setenv(var, str(tmp_path / name))


#: What every test gets for PATH: only what ships with macOS.
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


@pytest.fixture(autouse=True)
def _no_real_home_or_tools(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test gets a scratch HOME and a PATH of only the system folders.

    Graft looks for its tools at an explicit path, in the bundle, at a default
    under HOME (`~/.local/bin/ipatool`) and then on PATH, and it writes under
    HOME too (`~/.Trash`, PlayCover's settings). A test that forgot to fake
    one of those used to reach the developer's own: one ran a real
    `ipatool auth info`, which reads that ipatool's Keychain item, and
    retention tests moved archives into the real Trash. Tests that need a fake
    tool put its folder in front of this PATH, as they always have.
    """
    # Beside tmp_path, not in it: many tests assert exactly what tmp_path holds.
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", SYSTEM_PATH)


@pytest.fixture(autouse=True)
def _an_apple_silicon_mac(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test runs as if on Apple silicon, whatever Mac runs the suite.

    PlayCover and UnfairPlay word their failures differently on an Intel Mac
    (`graft_ipa.host`). Without this, the same test would assert one message
    on the developer's Mac and get the other on an Intel one. Tests of the
    Intel path replace the reader themselves.
    """
    from graft_ipa import host

    monkeypatch.setattr(host, "_sysctl_arm64", lambda: 1)


@pytest.fixture(autouse=True)
def _no_real_mac_region(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test reads this Mac's own region (`defaults read -g AppleLocale`).

    `--appstore-search` asks the store of the Mac's region when no `--country`
    is given, so without this the same test would search the GB store on one
    developer's Mac and the US store on another's. Tests of the reader, and of
    a region reaching the search, set their own value over this one.
    """
    from graft_ipa import host

    monkeypatch.setattr(host, "_read_apple_locale", lambda: None)


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test talks to an empty fake `security`, never the login keychain.

    Reads used to be harmless here, so a test that reached the signer or the
    pre-flight with no password mocked simply missed. Since the rename a miss
    under `graft` reads the old `tweak-build` item and *writes* a copy — so an
    unmocked read with a team id that exists on the developer's machine would
    add items to their real keychain. `tests/test_keychain.py` installs its
    own seeded fake over this one.
    """
    from graft_ipa import keychain

    from .keychain_fakes import FakeSecurity

    monkeypatch.setattr(keychain, "_run", FakeSecurity())
    # Unlocked, and never the Security framework's own answer about the real
    # login keychain (`keychain.login_keychain_locked`).
    monkeypatch.setattr(keychain, "_lock_probe", lambda: False)
    # The fake covers this process only. A CLI the suite spawns as a
    # subprocess imports keychain afresh, and this switch is what keeps it
    # off the real `security` too.
    monkeypatch.setenv("GRAFT_NO_KEYCHAIN", "1")


#: macOS's `sun_path`: a socket path of this many bytes or more can't be used.
SUN_PATH_MAX = 104


def private_socket_path(base: str | None = None) -> str:
    """A socket path nothing listens on, in a fresh owner-only directory.

    Owner-only and freshly made (`mkdtemp`: mode 0700, a random name, created
    exclusively) is what keeps another account from planting a listener
    there — wherever it is, /tmp included. It was a fixed name in
    world-writable /tmp, which held only while no other account had put a
    socket there first: a planted listener, or a proxy to the real usbmuxd,
    would have answered every test that reached pymobiledevice3 unfaked.

    Under `base` (the per-user temp folder by default) unless that would put
    the path at or past AF_UNIX's limit: a long TMPDIR made `connect` fail
    with "AF_UNIX path too long" instead of "nothing there", which the tests
    asserting the latter read as a failure. Then /tmp, whose short name keeps
    it well inside the limit.
    """
    for root in (tempfile.gettempdir() if base is None else base, "/tmp"):
        made = tempfile.mkdtemp(prefix="gnu-", dir=root)
        path = os.path.join(made, "s.sock")
        if len(path.encode()) < SUN_PATH_MAX:
            atexit.register(shutil.rmtree, made, True)
            return path
        os.rmdir(made)
    raise RuntimeError("no short enough private directory for the no-usbmuxd socket")


#: No usbmuxd listens here, so pymobiledevice3 fails to connect at once.
NO_USBMUXD = private_socket_path()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "allow_network: this test may reach the network (none should)"
    )
    config.addinivalue_line(
        "markers",
        "real_keychain: an opt-in test that runs the real `security` on a throwaway keychain; "
        "with GRAFT_REAL_KEYCHAIN_TESTS=1 alone, the program guard lets it start `security`",
    )


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """No test reaches past this Mac; see `tests/network_guard.py`.

    Yields the refused attempts. A test of the guard itself, which makes
    some on purpose, clears them.
    """
    from . import network_guard

    attempts: list[str] = []
    if request.node.get_closest_marker(network_guard.MARKER) is None:
        network_guard.install(monkeypatch, attempts)
    yield attempts
    assert not attempts, f"{request.node.nodeid} tried to reach the network: {attempts}"


#: What every child of the suite gets on its PYTHONPATH (see `children_offline`).
CHILD_SITE = Path(__file__).parent / "child_site"


# --- No test starts security, osascript, sudo, su, authopen or login ----------
# (`tests/program_guard.py`.) In this process for the whole session, from the
# moment conftest is imported, so collection and every fixture are covered
# too; nothing in graft_ipa is relied on.

from . import program_guard  # noqa: E402

#: What the guard refused in this process since the current test began.
PROGRAM_ATTEMPTS: list[str] = []
program_guard.install(PROGRAM_ATTEMPTS.append)


class ProgramRefusals:
    """`forbidden_programs`'s yield: what was refused in this process
    (`attempts`) and in its children (`log`), for the guard's own tests to
    clear after refusals they made on purpose."""

    def __init__(self, log: Path) -> None:
        self.attempts = PROGRAM_ATTEMPTS
        self.log = log

    def children(self) -> str:
        return self.log.read_text().strip() if self.log.exists() else ""

    def clear(self) -> None:
        self.attempts.clear()
        self.log.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def forbidden_programs(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
                       tmp_path_factory: pytest.TempPathFactory):
    """Fails any test during which a forbidden program was asked for, in
    this process or in one it started, even if the code under test swallowed
    the refusal. No marker opts out."""
    PROGRAM_ATTEMPTS.clear()
    log = tmp_path_factory.mktemp("child-programs") / "refused.log"
    monkeypatch.setenv(program_guard.CHILD_LOG_ENV, str(log))
    # The one exception: an opt-in real-keychain test (its marker, and the
    # owner's GRAFT_REAL_KEYCHAIN_TESTS=1, both) may start `security`, and
    # nothing else on the list; its children too. Never any other test.
    real_keychain = (
        request.node.get_closest_marker(program_guard.REAL_KEYCHAIN_MARKER) is not None
        and os.environ.get(program_guard.REAL_KEYCHAIN_ENV) == "1"
    )
    program_guard.allow_security(real_keychain)
    if real_keychain:
        monkeypatch.setenv(program_guard.ALLOW_SECURITY_ENV, "1")
    else:
        monkeypatch.delenv(program_guard.ALLOW_SECURITY_ENV, raising=False)
    refusals = ProgramRefusals(log)
    try:
        yield refusals
    finally:
        program_guard.allow_security(False)
    found = [*PROGRAM_ATTEMPTS, *([refusals.children()] if refusals.children() else [])]
    PROGRAM_ATTEMPTS.clear()
    assert not found, (
        f"{request.node.nodeid} tried to start a program the suite never runs:\n" + "\n".join(found)
    )


@pytest.fixture(autouse=True)
def children_offline(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
                     tmp_path_factory: pytest.TempPathFactory):
    """Every process a test starts is kept off the network too: the
    critic's gap in the combined fix check. About a dozen files run
    `sys.executable -m graft_ipa ...`, and `no_network` patches sockets in
    this process only, so a child with no fake reached the network and the
    test passed with nothing recorded.

    Every `subprocess.Popen` (so `run`, `check_output` and asyncio's
    subprocesses too) is given `tests/child_site` at the end of PYTHONPATH,
    whose sitecustomize blocks the network as `no_network` does, and
    GRAFT_TEST_NETWORK_LOG, the file it records refusals in. The environment
    a test passes is kept otherwise as it is. Yields the log's path; a
    test of the guard itself, which makes attempts on purpose, empties it.
    """
    import inspect
    import subprocess

    from . import network_guard

    log = tmp_path_factory.mktemp("child-network") / "refused.log"
    # The child site goes to every child, marker or not: it also carries the
    # program guard, which no test opts out of (`forbidden_programs`).
    offline = request.node.get_closest_marker(network_guard.MARKER) is None
    real_init = subprocess.Popen.__init__
    signature = inspect.signature(real_init)

    def guarded_init(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        env = bound.arguments.get("env")
        env = dict(os.environ if env is None else env)
        if all(isinstance(k, str) for k in env):
            path = env.get("PYTHONPATH", "")
            entries = [e for e in path.split(os.pathsep) if e]
            if str(CHILD_SITE) not in entries:
                env["PYTHONPATH"] = os.pathsep.join([*entries, str(CHILD_SITE)])
            if offline:
                env[network_guard.CHILD_LOG_ENV] = str(log)
            if program_guard.CHILD_LOG_ENV in os.environ:
                env[program_guard.CHILD_LOG_ENV] = os.environ[program_guard.CHILD_LOG_ENV]
            bound.arguments["env"] = env
        real_init(*bound.args, **bound.kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    yield log
    refused = log.read_text().strip() if log.exists() else ""
    assert not refused, f"{request.node.nodeid}: a process it started tried to reach the network:\n{refused}"


@pytest.fixture(autouse=True)
def _no_real_usbmuxd(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test, and every CLI it spawns, sees no usbmuxd and so no phone.

    Installs used to shell out to `ideviceinstaller`, which a test could fake
    or find missing. They now run in-process over pymobiledevice3
    (`device_install`), so a test that reaches one unfaked would install on
    whatever phone is plugged into the developer's Mac. pymobiledevice3 honours
    `USBMUXD_SOCKET_ADDRESS` at the socket layer, for every call site; tests
    that fake `create_using_usbmux` never get as far as the socket.
    """
    monkeypatch.setenv("USBMUXD_SOCKET_ADDRESS", NO_USBMUXD)


@pytest.fixture(autouse=True)
def _no_bundled_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test picks up a real Graft.app's bundled tools.

    `tools.bundled_tools_dir` finds `Contents/Helpers/bin` from the running
    interpreter's own path, so a suite run by the app's embedded Python would
    otherwise resolve cyan, zsign, ipatool and UnfairPlay from that bundle.
    Tests of the bundled rung set their own directory over this one: a later
    setattr wins.
    """
    from graft_ipa import tools

    monkeypatch.setattr(tools, "bundled_tools_dir", lambda: None)


@pytest.fixture
def temp_config_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG dirs + a config.yaml with one app (`example`). Returns the config path.

    Use this whenever a test needs to invoke the CLI as a subprocess and shouldn't
    pollute the real `~/.config/graft/`.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg = tmp_path / "cfg" / "graft" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    write_test_config(cfg, team_id="TESTTEAM01")
    return cfg


@pytest.fixture(scope="session")
def describe_proc(
    tmp_path_factory: pytest.TempPathFactory,
) -> subprocess.CompletedProcess[str]:
    """`graft --describe`, run once for the whole session.

    `--describe` prints one deterministic line and exits; three modules assert
    against it and between them were spawning a fresh interpreter eight times
    per run — 1.24s of a 4.7s suite for the same string.

    Session-scoped, so `tmp_path_factory` rather than `tmp_path`. The hermetic
    environment is the point of the fixture and is preserved exactly: a `PATH`
    with nothing of the developer's on it, and XDG roots under a directory that
    has never seen an `--init`. Pointing them at empty paths is what makes a
    config lookup fail loudly instead of silently finding the real
    `~/.config/graft/` — the GUI's contract test runs `--describe` on
    machines that have never run `--init`, and this is how that stays true.
    """
    home = tmp_path_factory.mktemp("describe-home")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "cfg"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
    }
    return subprocess.run(
        [sys.executable, "-m", "graft_ipa.cli", "--describe"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO_ROOT),
    )


@pytest.fixture(scope="session")
def contract(describe_proc: subprocess.CompletedProcess[str]) -> dict:
    """The decoded `cli-contract` payload. Read-only: it is shared session-wide."""
    assert describe_proc.returncode == 0, describe_proc.stderr
    lines = describe_proc.stdout.splitlines()
    assert len(lines) == 1, f"expected exactly one JSON line, got {lines!r}"
    return json.loads(lines[0])


@pytest.fixture
def fake_iproxy(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """An `iproxy` on PATH, for code that only checks one is installed (a
    frida dry run refuses without one, then never starts it).

    Without this, those tests passed only where Homebrew's libimobiledevice
    is installed, and failed on a clean Mac (review #33). It is first on
    PATH, and if anything runs it, it records the run and the test fails at
    teardown. Its exit status alone (99) proved nothing: the tunnel is
    started with its output discarded and its status read only by the code
    that waits for the port, which a dry run that started it and returned
    never reaches (re-check #37).
    """
    bin_dir = tmp_path_factory.mktemp("fake-iproxy")
    ran = bin_dir / "ran.log"
    iproxy = bin_dir / "iproxy"
    iproxy.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{ran}"\n'
        "echo 'fake iproxy: a dry run must not start it' >&2\n"
        "exit 99\n"
    )
    iproxy.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    yield iproxy
    # A tunnel started in the background may not have run its first line yet.
    deadline = time.monotonic() + 2
    while not ran.exists() and time.monotonic() < deadline and _started_iproxy(bin_dir):
        time.sleep(0.05)
    assert not ran.exists(), f"iproxy was started: {ran.read_text().strip()!r}"


def _started_iproxy(bin_dir: Path) -> bool:
    """Whether a process running this fake iproxy is still alive."""
    import subprocess as _sp

    out = _sp.run(["/bin/ps", "-axo", "command="], capture_output=True, text=True).stdout
    return str(bin_dir / "iproxy") in out
