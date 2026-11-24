"""No test reaches the network: any connection off this Mac fails loudly.

A code path with no fake sent real (if public and credential-free) iTunes
lookups from the suite, and only a per-file fake stopped it. This stops the
next one wherever it is: while a test runs, a connect, a name lookup or
`socket.create_connection` to anything but this Mac raises `NetworkBlocked`,
and the attempt is recorded so the test fails at teardown even if the code
under test swallows the error (a client that treats any OSError as "offline"
would otherwise pass quietly).

Allowed: AF_UNIX (the no-usbmuxd guard, socketpairs) and loopback, 127/8 and
::1 by address and `localhost` by name, since some tests run local servers.

A process the suite starts is guarded the same way (the critic's gap in the
combined fix check): every child gets `tests/child_site` on its PYTHONPATH,
whose sitecustomize calls `install_in_child`, and a refused attempt there is
written to the test's own log, which fails it at teardown just the same. A
child started with `-I` or `-E` ignores PYTHONPATH and is not covered; the
suite starts none that way but `p12_probe`'s, which has no network code.
A test that genuinely needs the network says so with
`@pytest.mark.allow_network`; none does.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # a child's sitecustomize loads this file, and needs no pytest
    import pytest

MARKER = "allow_network"
#: The file a child process records its refused attempts in (`install_in_child`).
CHILD_LOG_ENV = "GRAFT_TEST_NETWORK_LOG"

_LOCAL_NAMES = frozenset({"localhost", "localhost.", "ip6-localhost", "ip6-loopback"})


class NetworkBlocked(ConnectionRefusedError):
    """A test tried to reach the network. An OSError, so the code under test
    takes its offline path rather than hanging; the teardown check is what
    makes it fail."""


def is_local_host(host: Any) -> bool:
    """Whether `host` names this Mac: a loopback address or `localhost`.

    `None` and "" are the passive and any-address forms, which bind rather
    than reach anything.
    """
    if host is None:
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    host = str(host).strip("[]")
    if host == "" or host.lower() in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_local_address(family: int, address: Any) -> bool:
    if family == getattr(socket, "AF_UNIX", None):
        return True
    if isinstance(address, tuple) and address:
        return is_local_host(address[0])
    return False


def install(monkeypatch: pytest.MonkeyPatch, attempts: list[str]) -> None:
    """Guard this process's sockets for one test; `attempts` collects what
    was refused."""
    _guard(monkeypatch.setattr, attempts.append)


def install_in_child(log: str) -> None:
    """Guard a child process's sockets for good: what `tests/child_site`'s
    sitecustomize runs in every process the suite starts. A refused attempt
    is appended to `log`, which the test's teardown reads."""
    def record(what: str) -> None:
        with open(log, "a") as fh:
            fh.write(f"pid {os.getpid()}: {what}\n")

    _guard(setattr, record)


def _guard(setattr_: Callable[..., Any], record: Callable[[str], None]) -> None:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def refuse(what: str) -> NetworkBlocked:
        record(what)
        return NetworkBlocked(f"the test suite blocks the network: {what}")

    def connect(self, address):
        if not _is_local_address(self.family, address):
            raise refuse(f"connect to {address!r}")
        return real_connect(self, address)

    def connect_ex(self, address):
        if not _is_local_address(self.family, address):
            raise refuse(f"connect_ex to {address!r}")
        return real_connect_ex(self, address)

    def getaddrinfo(host, *args, **kwargs):
        if not is_local_host(host):
            raise refuse(f"name lookup of {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    def create_connection(address, *args, **kwargs):
        if not is_local_host(address[0]):
            raise refuse(f"create_connection to {address!r}")
        return real_create_connection(address, *args, **kwargs)

    setattr_(socket.socket, "connect", connect)
    setattr_(socket.socket, "connect_ex", connect_ex)
    setattr_(socket, "getaddrinfo", getaddrinfo)
    setattr_(socket, "create_connection", create_connection)
