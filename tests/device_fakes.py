"""A phone at pymobiledevice3's Python boundary, for tests that run graft's
real install (`device_install`) end to end without a device: the lockdown
session, AFC and InstallationProxy, each faked where graft calls them.

`phone(...)` installs the fakes and returns a `Phone` recording what reached
it. Each keyword is an exception raised at that step instead of working:
`connect` from `create_using_usbmux`, `write` from the second AFC write (and
the phone gone for the clean-up after it), `install` from the install itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pymobiledevice3.lockdown as pmd_lockdown
import pymobiledevice3.services.afc as pmd_afc
import pymobiledevice3.services.installation_proxy as pmd_ip
import pytest
from pymobiledevice3.exceptions import ConnectionTerminatedError


@dataclass
class Phone:
    #: The UDID each lockdown session was opened for.
    serials: list[str | None] = field(default_factory=list)
    #: (command, the bytes staged at the path the install was asked for).
    sent: list[tuple[str, bytes]] = field(default_factory=list)


def phone(
    monkeypatch: pytest.MonkeyPatch, *,
    connect: BaseException | None = None,
    write: BaseException | None = None,
    install: BaseException | None = None,
) -> Phone:
    seen = Phone()
    files: dict[str, bytes] = {}

    class Lockdown:
        paired = True

        async def close(self):
            pass

    async def create_using_usbmux(serial=None, **_):
        seen.serials.append(serial)
        if connect is not None:
            raise connect
        return Lockdown()

    class Afc:
        def __init__(self, lockdown, service_name=None):
            self.writes = 0

        async def makedirs(self, path): pass
        async def listdir(self, path): return []

        async def fopen(self, path, mode="r"):
            files[path] = b""
            self.path = path
            return 1

        async def fwrite(self, handle, data, chunk_size=0):
            self.writes += 1
            if write is not None and self.writes == 2:
                raise write
            files[self.path] += bytes(data)

        async def fclose(self, handle):
            if write is not None:
                raise ConnectionTerminatedError()

        async def rm_single(self, path, force=False):
            if write is not None or install is not None:
                raise ConnectionTerminatedError()
            files.pop(path, None)

        async def aclose(self): pass

    class Proxy:
        def __init__(self, lockdown):
            pass

        async def send_package(self, cmd, options, handler, path, *args):
            seen.sent.append((cmd, files[path]))
            if install is not None:
                raise install
            handler(100)

        async def close(self):
            pass

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(pmd_afc, "AfcService", Afc)
    monkeypatch.setattr(pmd_ip, "InstallationProxyService", Proxy)
    from graft_ipa import device_install

    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 1)
    return seen
