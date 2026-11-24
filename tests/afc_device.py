"""A fake phone that speaks AFC, for running graft's install over the REAL
pymobiledevice3 `AfcService` (its reader task, its framing, its exceptions).

`main(mode)` runs `installer.install` against it in this process and prints
`RESULT <ExceptionName> | <hint>` on stdout; tests run it in a child so that
whatever pymobiledevice3 or asyncio would print reaches a real stderr, not
pytest's capture. No phone and no usbmuxd: the AFC connection is one end of a
socketpair, the device a thread on the other. Only the lockdown session and
InstallationProxy are faked, at their Python boundary.

Modes:
  drop-write   the phone goes away (closes its socket) on the 3rd WRITE: a
               cable pulled mid-upload
  reset-write  the same, with a TCP reset
  drop-install the upload completes, then the connection dies while the
               install is waited for
  silent-write the phone stops answering at the 3rd WRITE but keeps the
               connection open: a Wi-Fi-synced iPad gone to sleep (re-check #13)
  silent-install the upload completes, the install reports once, then
               nothing more, the connection still open
  slow-install the install reports progress slowly but steadily, for longer
               than the silence allowed: an inactivity limit, not a total one

The silent modes run with `device_install.SILENCE_SECONDS` cut to a fraction
of a second, and print `ELAPSED <seconds>` before the result.
"""

from __future__ import annotations

import socket
import struct
import sys
import tempfile
import threading
from pathlib import Path

MAGIC = b"CFA6LPAA"
#: `device_install.SILENCE_SECONDS` here: 60 s for real, a moment for a test.
SILENCE = 0.5
HEADER = 40


def _device(sock: socket.socket, mode: str) -> None:
    from pymobiledevice3.services.afc import AfcOpcode

    writes = 0

    def recv(n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            part = sock.recv(n - len(buf))
            if not part:
                raise EOFError
            buf += part
        return buf

    def reply(packet: int, op: int, payload: bytes = b"") -> None:
        size = HEADER + len(payload)
        sock.sendall(MAGIC + struct.pack("<QQQQ", size, size, packet, op) + payload)

    try:
        while True:
            entire, _this, packet, op = struct.unpack("<QQQQ", recv(HEADER)[8:])
            recv(entire - HEADER)
            if op == AfcOpcode.WRITE:
                writes += 1
                if writes == 3 and mode == "silent-write":
                    # Still reading, never answering again.
                    while recv(HEADER):
                        pass
                if writes == 3 and mode in ("drop-write", "reset-write"):
                    if mode == "reset-write":
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    sock.close()
                    return
            if op == AfcOpcode.READ_DIR:
                reply(packet, AfcOpcode.DATA, b".\0..\0")
            elif op == AfcOpcode.FILE_OPEN:
                reply(packet, AfcOpcode.DATA, struct.pack("<Q", 7))
            elif op == AfcOpcode.GET_FILE_INFO:
                reply(packet, AfcOpcode.DATA, b"st_ifmt\0S_IFDIR\0st_size\x000\0")
            else:
                reply(packet, AfcOpcode.STATUS, struct.pack("<Q", 0))
    except (EOFError, OSError):
        return


def main(mode: str) -> None:
    import pymobiledevice3.lockdown as pmd_lockdown
    import pymobiledevice3.services.installation_proxy as pmd_ip
    from pymobiledevice3.exceptions import ConnectionTerminatedError
    from pymobiledevice3.service_connection import ServiceConnection

    from graft_ipa import device_install, installer

    class Lockdown:
        paired = True

        def __init__(self) -> None:
            self._mine, theirs = socket.socketpair()
            threading.Thread(target=_device, args=(theirs, mode), daemon=True).start()

        async def start_lockdown_service(self, name, include_escrow_bag=False):
            # How a connected socket becomes a service connection differs by
            # release: 11.x binds it with `from_socket`; 10.x takes it in
            # the constructor and opens its streams on first use.
            if hasattr(ServiceConnection, "from_socket"):
                return await ServiceConnection.from_socket(self._mine)
            return ServiceConnection(self._mine)

        async def close(self) -> None:
            pass

    async def create_using_usbmux(serial=None, **_):
        return Lockdown()

    class Proxy:
        def __init__(self, lockdown) -> None:
            pass

        async def send_package(self, cmd, options, handler, path, *args):
            import asyncio

            if mode == "drop-install":
                # What `_watch_completion`'s read raises when the phone goes.
                raise ConnectionTerminatedError()
            if mode == "silent-install":
                handler(10)
                await asyncio.Event().wait()  # never answers again
            if mode == "slow-install":
                for percent in range(10, 100, 10):
                    await asyncio.sleep(SILENCE / 3)
                    handler(percent)
            handler(100)

        async def close(self) -> None:
            pass

    pmd_lockdown.create_using_usbmux = create_using_usbmux
    pmd_ip.InstallationProxyService = Proxy
    device_install.UPLOAD_CHUNK_BYTES = 64 * 1024
    device_install.SILENCE_SECONDS = SILENCE

    with tempfile.TemporaryDirectory() as scratch:
        ipa = Path(scratch) / "app.ipa"
        ipa.write_bytes(b"x" * (1024 * 1024))
        import time

        started = time.monotonic()
        try:
            installer.install(ipa, udid="u1")
            print(f"ELAPSED {time.monotonic() - started:.2f}")
            print("RESULT ok |")
        except Exception as e:  # the result is what it raised
            print(f"ELAPSED {time.monotonic() - started:.2f}")
            print(f"RESULT {type(e).__name__} | {getattr(e, 'hint', '')}")


if __name__ == "__main__":
    main(sys.argv[1])
