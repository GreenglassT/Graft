"""The suite-wide network block (`tests/network_guard.py`).

A "remote" here is 0.0.0.0: no loopback address, so the guard must refuse
it, yet the kernel delivers a connection to it on this Mac. So even with the
guard broken, these tests send nothing off the machine; they only fail.
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading

import pytest

from .network_guard import NetworkBlocked, is_local_host

NOT_LOOPBACK = "0.0.0.0"


@pytest.fixture
def server():
    """A TCP listener on 127.0.0.1, accepting in the background."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(5)

    def accept():
        try:
            while True:
                conn, _ = srv.accept()
                conn.close()
        except OSError:
            pass

    threading.Thread(target=accept, daemon=True).start()
    yield srv.getsockname()[1]
    srv.close()


@pytest.mark.parametrize("host", ["93.184.216.34", "2001:db8::1", "example.com", "itunes.apple.com", "0.0.0.0"])
def test_a_remote_host_is_not_local(host):
    assert not is_local_host(host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "localhost", None, ""])
def test_loopback_is_local(host):
    assert is_local_host(host)


def test_a_remote_connect_lookup_and_create_connection_are_refused(server, no_network):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(2)
    try:
        with pytest.raises(NetworkBlocked):
            sock.connect((NOT_LOOPBACK, server))
        with pytest.raises(NetworkBlocked):
            sock.connect_ex((NOT_LOOPBACK, server))
    finally:
        sock.close()
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo(NOT_LOOPBACK, server)
    with pytest.raises(NetworkBlocked):
        socket.create_connection((NOT_LOOPBACK, server), timeout=2)
    assert len(no_network) == 4, no_network
    no_network.clear()  # refused on purpose; the teardown check is for accidents


def test_loopback_still_connects(server, no_network):
    with socket.create_connection(("127.0.0.1", server), timeout=2):
        pass
    with socket.create_connection(("localhost", server), timeout=2):
        pass
    assert socket.getaddrinfo("localhost", server)
    assert no_network == []


def test_af_unix_still_connects(no_network):
    where = tempfile.mkdtemp(prefix="gng-", dir="/tmp")
    path = os.path.join(where, "s.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
        srv.listen(1)
        cli = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        cli.connect(path)
        cli.close()
        a, b = socket.socketpair()
        a.close(), b.close()
    finally:
        srv.close()
        os.unlink(path) if os.path.exists(path) else None
        os.rmdir(where)
    assert no_network == []


def test_a_swallowed_refusal_still_fails_the_test(server, no_network):
    """Code that treats any OSError as "offline" must not make the attempt
    invisible: the fixture's teardown fails the test on what it recorded."""
    try:
        socket.create_connection((NOT_LOOPBACK, server), timeout=2)
    except OSError:
        pass
    assert no_network, "the refusal left no record"
    no_network.clear()


@pytest.mark.allow_network
def test_the_opt_out_marker_leaves_sockets_alone():
    """Checked without connecting to anything: the real functions are in place."""
    assert "connect" not in vars(socket.socket)
    assert socket.getaddrinfo.__module__ == "socket"
    assert socket.create_connection.__module__ == "socket"


def test_no_test_in_the_suite_opts_out():
    from pathlib import Path

    here = Path(__file__)
    users = [
        p.name for p in here.parent.glob("test_*.py")
        if p != here and "mark.allow_network" in p.read_text()
    ]
    assert users == []


def test_a_test_that_swallows_the_refusal_is_reported(tmp_path):
    """The teardown check, seen from outside: a test whose code catches the
    refusal and carries on still ends in an error naming the attempt."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import no_network, pytest_configure  # noqa: F401\n"
    )
    (tmp_path / "test_quiet_client.py").write_text(
        "import socket\n"
        "def test_offline_is_fine():\n"
        "    try:\n"
        f"        socket.create_connection(({NOT_LOOPBACK!r}, 9), timeout=1)\n"
        "    except OSError:\n"
        "        pass\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    assert proc.returncode != 0, proc.stdout
    assert "tried to reach the network" in proc.stdout
    assert "1 error" in proc.stdout


# ---- the processes a test starts (the combined fix check's critic) ----------------

_CHILD = """
import socket, sys
port = int(sys.argv[1])
for label, host in (("remote", sys.argv[2]), ("loopback", "127.0.0.1")):
    try:
        socket.create_connection((host, port), timeout=2).close()
        print(label, "ok")
    except OSError as e:
        print(label, type(e).__name__)
"""


@pytest.mark.parametrize("env", ["inherited", "explicit"])
def test_a_child_cannot_reach_the_network_and_can_reach_loopback(server, children_offline, env):
    import subprocess
    import sys

    given = None if env == "inherited" else {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"]}
    proc = subprocess.run([sys.executable, "-c", _CHILD, str(server), NOT_LOOPBACK],
                          capture_output=True, text=True, timeout=60, env=given)
    assert proc.stdout.split("\n")[:2] == ["remote NetworkBlocked", "loopback ok"], proc.stdout + proc.stderr
    assert f"create_connection to ('{NOT_LOOPBACK}', {server})" in children_offline.read_text()
    children_offline.write_text("")  # refused on purpose


def test_the_cli_run_as_a_child_with_no_fake_is_caught(children_offline, temp_config_env):
    """The critic's scenario: `--appstore-search` in a child, with nothing
    faking iTunes. Through a proxy at 0.0.0.0, so that even with the guard
    broken the request goes nowhere: requests asks the proxy, which the
    kernel answers locally, and never looks up itunes.apple.com itself."""
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    env.update(HTTPS_PROXY=f"http://{NOT_LOOPBACK}:9", HTTP_PROXY=f"http://{NOT_LOOPBACK}:9")
    proc = subprocess.run([sys.executable, "-m", "graft_ipa", "--json", "--appstore-search", "notes",
                           "--country", "US"], cwd=repo, capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode != 0, proc.stdout
    assert f"'{NOT_LOOPBACK}'" in children_offline.read_text(), children_offline.read_text()
    children_offline.write_text("")


def test_a_test_whose_child_swallows_the_refusal_is_reported(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "conftest.py").write_text(
        "from tests.conftest import children_offline, no_network, pytest_configure  # noqa: F401\n"
    )
    (tmp_path / "test_quiet_child.py").write_text(
        "import subprocess, sys\n"
        "def test_offline_is_fine():\n"
        "    subprocess.run([sys.executable, '-c', 'import socket\\n"
        f"try:\\n    socket.create_connection((\"{NOT_LOOPBACK}\", 9), timeout=1)\\nexcept OSError:\\n"
        "    pass'], check=True)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-o", "addopts=", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": str(repo)},
    )
    assert proc.returncode != 0, proc.stdout
    assert "a process it started tried to reach the network" in proc.stdout
    assert f"create_connection to ('{NOT_LOOPBACK}', 9)" in proc.stdout


def test_the_child_guard_chains_to_the_next_sitecustomize(tmp_path, children_offline):
    """It shadows any other sitecustomize on sys.path, so it must run the
    next one, as the bundle acceptance test's spy does."""
    import subprocess
    import sys

    other = tmp_path / "other"
    other.mkdir()
    (other / "sitecustomize.py").write_text("import builtins; builtins.CHAINED = True\n")
    proc = subprocess.run([sys.executable, "-c", "import builtins; print(getattr(builtins, 'CHAINED', False))"],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": f"{other}"})
    # The guard goes at the end of PYTHONPATH, so `other` is found first and
    # nothing chains past it here; with the guard first, it must chain on.
    assert proc.stdout.strip() == "True"
    from .conftest import CHILD_SITE

    proc = subprocess.run([sys.executable, "-c", "import builtins, socket; print(getattr(builtins, 'CHAINED', False),"
                           " socket.getaddrinfo.__module__)"],
                          capture_output=True, text=True, timeout=60,
                          env={**os.environ, "PYTHONPATH": f"{CHILD_SITE}{os.pathsep}{other}"})
    assert proc.stdout.split() == ["True", "_graft_test_network_guard"], proc.stdout + proc.stderr
