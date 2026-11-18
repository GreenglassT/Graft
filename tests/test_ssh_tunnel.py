"""The SSH tunnel every device probe and dump runs over.

It lives in `decrypt_runner` because the dump needs it too — the readiness
probe was not the only thing that had to reach the phone, and two copies of
"pick a port, start iproxy" is how one of them keeps port 2222.
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest

from graft_ipa import decrypt_runner

#: Captured before any test can monkeypatch the module attribute — the fake
#: iproxy has to keep listening for real while the code under test is handed a
#: stand-in that returns a chosen port.
_real_socket = socket.socket


@pytest.fixture
def fake_iproxy(monkeypatch, tmp_path):
    """An iproxy that listens on the requested port, recording how it was called.

    Real iproxy is not available in CI and would need a phone anyway. What is
    being tested is this module's port arithmetic, so a listener that behaves
    like iproxy on the local end is the whole fixture.
    """
    calls: list[list[str]] = []
    # Every tunnel is kept alive deliberately. Letting them be collected made
    # the teardown untestable: CPython closes the socket when the last reference
    # goes, so a deleted `finally` looked exactly like a working one.
    tunnels: list[object] = []
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")

    class FakeTunnel:
        def __init__(self, args, **_):
            calls.append(args)
            tunnels.append(self)
            self.args = args
            self._rc = None
            port = int(args[1].split(":")[0])
            self.sock = _real_socket()
            try:
                self.sock.bind(("127.0.0.1", port))
                self.sock.listen(1)
            except OSError:
                # Exactly what iproxy does when the port is taken: exit,
                # quietly, because stderr goes to DEVNULL.
                self.sock.close()
                self.sock = None
                self._rc = 1

        def poll(self):
            return self._rc

        def terminate(self):
            self._rc = self._rc if self._rc is not None else 0
            if self.sock:
                self.sock.close()
                self.sock = None

        def wait(self, timeout=None):
            return self._rc

        def kill(self):
            self.terminate()

    monkeypatch.setattr(decrypt_runner.subprocess, "Popen", FakeTunnel)
    return SimpleNamespace(calls=calls, tunnels=tunnels)


def test_nobody_takes_a_fixed_port_any_more(fake_iproxy):
    """Everything that reaches the phone now picks its own port.

    Sharing 2222 meant a readiness check could tear down a running dump's tunnel
    in its `finally`, and two dumps at once meant the second iproxy exited on
    EADDRINUSE — silently, stderr being DEVNULL — while its dump pulled the
    bundle down the first one's tunnel.
    """
    seen: list[int] = []
    decrypt_runner._with_tunnel("UDID", lambda port: seen.append(port) or "ok")
    assert seen and seen[0] != 2222


def test_a_second_probe_does_not_reuse_the_first_ones_port(fake_iproxy):
    ports: list[int] = []

    def hold(port):
        # Nested: the outer tunnel is still bound when the inner one picks.
        decrypt_runner._with_tunnel("UDID", lambda inner: ports.append(inner))
        ports.append(port)
        return "ok"

    decrypt_runner._with_tunnel("UDID", hold)
    assert len(ports) == 2 and ports[0] != ports[1]


def test_a_tunnel_that_could_not_bind_reports_nothing(fake_iproxy, monkeypatch):
    """The finding this file exists for.

    If something else already holds the port, iproxy exits and the connection
    that follows lands on *that* listener — another phone, or another device's
    tunnel. Answering from it is worse than answering not-at-all: the card would
    say "authenticated as root" about a handset that is not jailbroken.
    """
    squatter = socket.socket()
    squatter.bind(("127.0.0.1", 0))
    squatter.listen(1)
    stolen = squatter.getsockname()[1]
    # Standing in for losing the bind race: `_free_port` hands back a port
    # that something else already holds.
    monkeypatch.setattr(decrypt_runner, "_free_port", lambda: stolen)
    try:
        assert decrypt_runner._with_tunnel("UDID", lambda port: "root") is None
    finally:
        squatter.close()


def test_the_device_is_pinned_on_the_tunnel(fake_iproxy):
    """Two phones on the wire; an unpinned tunnel answers for either."""
    decrypt_runner._with_tunnel("00008030-0000000000000003", lambda port: "ok")
    assert fake_iproxy.calls[0][-2:] == ["-u", "00008030-0000000000000003"]


def test_no_iproxy_is_not_an_answer(monkeypatch):
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: None)
    assert decrypt_runner._with_tunnel("UDID", lambda port: "root") is None


def test_the_tunnel_is_torn_down_even_when_the_work_raises(fake_iproxy):
    """A leaked iproxy is a background process nobody owns — and it holds the
    port, which is what the collision above is made of.

    Asserted against the tunnel that actually ran. Re-binding a *fresh*
    ephemeral port proved nothing, since that succeeds whether or not the
    `finally` ran, so the test passed with the teardown deleted.
    """
    def boom(_port):
        raise RuntimeError("probe blew up")

    with pytest.raises(RuntimeError):
        decrypt_runner._with_tunnel("UDID", boom)

    assert fake_iproxy.tunnels, "no tunnel was ever started"
    tunnel = fake_iproxy.tunnels[-1]
    assert tunnel.poll() is not None, "the tunnel outlived the probe that started it"
    assert tunnel.sock is None, "its port is still held"
