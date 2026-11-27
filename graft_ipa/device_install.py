"""Install an IPA on a device over pymobiledevice3, without libimobiledevice.

What `ideviceinstaller install` did: upload the IPA into the device's
PublicStaging over AFC, then ask InstallationProxy to install it and wait for
"Complete". One function for sideload builds, re-installs and LiveContainer
installs, so all three fail the same way and get the same hints.

The upload is graft's own rather than pymobiledevice3's `install_from_local`,
which does `package_path.read_bytes()`: an IPA can be gigabytes, and that is
gigabytes of graft's memory. `AfcService.fopen` + `fwrite`, a chunk at a time,
streams it: what `AfcService.push` does above 4 MB, without push's round trips
to work out the destination or its progress bar.

What graft stages is named `graft-<mac>-<pid>-<uuid>.ipa`, `<mac>` standing
for this Mac (`this_mac`), and each install first removes such files that
this Mac's graft left and whose process is gone (`_remove_leftovers`): a Stop
or Ctrl-C mid-install ends graft with the upload's worker thread still in it,
so that upload's own clean-up never runs.

On the main thread, a Stop (SIGTERM, SIGHUP) cancels the install at an await
(`device_session.run`), and its clean-up closes and removes the staged file
within `device_session.TEARDOWN_SECONDS`, then raises `Stopped` as before.

A phone that stops answering but keeps the connection open (half-open TCP:
a Wi-Fi-synced iPad gone to sleep) is given `SILENCE_SECONDS` of silence,
then the install fails with `PhoneWentSilentError` and the same clean-up.
Unless the caller has said the install now waits on the user
(`waiting_on_user`): iOS holds an install over a running LiveContainer until
it is closed, with installd silent meanwhile, so from then on installd's
silence may last `PATIENT_SECONDS` (`StillWaitingError` after that). The
upload's limit is never lengthened.

USB and Wi-Fi alike: usbmux reaches a Wi-Fi-paired device by UDID, so there is
no `--network` switch to pass any more.
"""

from __future__ import annotations

import asyncio
import ctypes
import functools
import hashlib
import os
import re
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import device_session
from .device_session import Teardown, closing_best_effort

Progress = Callable[[int], None]

#: How much of the IPA is read at a time: AFC's own read size
#: (`afc.MAXIMUM_READ_SIZE`), well under its largest write.
UPLOAD_CHUNK_BYTES = 4 * 1024 * 1024
#: How much one AFC write carries. Each write the phone acknowledges is word
#: from it, so this is what has to cross the link inside `SILENCE_SECONDS`:
#: a whole 4 MiB chunk took more than that below about 70 KB/s, where a slow
#: but live link was taken for a phone gone quiet (combined fix check #31).
#: 512 KiB in 60 s is under 9 KB/s.
AFC_WRITE_BYTES = 512 * 1024

#: What graft names a staged IPA: the Mac, the process, a nonce. All
#: `_remove_leftovers` may remove, and only when the Mac is this one.
_STAGED_NAME = re.compile(r"graft-([0-9a-f]{12})-(\d+)-[0-9a-f]{32}\.ipa")


#: How long the phone may make no forward progress, mid-upload (no AFC write
#: acknowledged) or mid-install (no answer from installd of any kind), before
#: graft gives up on it (re-check #13). Before this a phone that went quiet
#: with the connection open held the build at stage 5, printing nothing,
#: until the kernel gave up on the TCP connection or the user pressed Stop.
#:
#: `device_session.PAIR_TIMEOUT_SECONDS` (60 s), the longest graft already
#: waits on a phone: a person reading the Trust prompt has that long, and a
#: phone that has acknowledged no write (`AFC_WRITE_BYTES`), and sent
#: installd's every status, progress or not, in that long has gone.
#: `TEARDOWN_SECONDS` (5 s) is a clean-up allowance and too short. It bounds
#: silence, not the whole install, which for a large app on an old phone
#: takes minutes.
SILENCE_SECONDS = device_session.PAIR_TIMEOUT_SECONDS


#: How long installd may say nothing once the caller has said the install
#: waits on the user (`waiting_on_user`): `--lc-install` over a running
#: LiveContainer, which iOS holds until the user closes it. 1.1's
#: ideviceinstaller waited with no limit; the 60 s limit failed it as a
#: phone gone quiet, with the locked-phone hint, while the CLI was saying it
#: would wait (the whole-diff review's cli-1). A Stop ends it at any time.
PATIENT_SECONDS = 15 * 60.0


class PhoneWentSilentError(Exception):
    """The connection stayed open and the phone stopped answering; named
    for `installer.connection_hint`."""


class StillWaitingError(Exception):
    """An install that waited on the user (`waiting_on_user`) heard nothing
    from installd for `PATIENT_SECONDS`. Named for the caller's hint: not a
    phone gone quiet, an app nobody closed."""


async def _answered(step: Any, what: str) -> Any:
    """`step`'s result, or `PhoneWentSilentError` once the phone has said
    nothing for `SILENCE_SECONDS`."""
    try:
        return await asyncio.wait_for(step, SILENCE_SECONDS)
    except TimeoutError:
        raise PhoneWentSilentError(
            f"the device stopped answering: nothing for {SILENCE_SECONDS:.0f} s {what}"
        ) from None


async def _hear_every_answer(proxy: Any, alive: Callable[[], None]) -> Callable[[], None]:
    """Make each answer installd sends, whatever it says, call `alive`.
    Returns what undoes it.

    pymobiledevice3's `send_package` calls its handler only for an answer
    with a PercentComplete, so a stage reported by Status alone was silence
    to the limit (combined fix check #31). Its own loop reads every answer
    through the service connection's `recv_plist`, in 10.4.0 and 11.x alike;
    connected first, so `proxy.service` is that connection and not the lazy
    stand-in that becomes it. A proxy without them (a test's) is left as it is.
    """
    connect = getattr(proxy, "connect", None)
    if connect is None:
        return lambda: None
    await _answered(connect(), "when the install began")
    conn = getattr(proxy, "service", None)
    recv = getattr(conn, "recv_plist", None)
    if recv is None:
        return lambda: None

    async def recv_plist(*args: Any, **kwargs: Any) -> Any:
        answer = await recv(*args, **kwargs)
        alive()
        return answer

    try:
        conn.recv_plist = recv_plist
    except (AttributeError, TypeError):  # a connection that takes no attributes
        return lambda: None
    return lambda: vars(conn).pop("recv_plist", None)


async def _install_until_silent(
    proxy: Any, remote: str, handler: Callable[..., None], *,
    waiting_on_user: threading.Event | None = None,
) -> None:
    """`proxy.send_package("Install", …)`, given up on once the phone has
    said nothing for `SILENCE_SECONDS`.

    A limit on each wait, not on the whole: the install's own pace is the
    phone's business, and every answer from installd, a progress report or
    a bare status, starts the clock again. Once `waiting_on_user` is set
    (the caller has told the user the install waits on them) the limit is
    `PATIENT_SECONDS`, looked at again whenever a wait ends, so it applies
    from the moment it is set.
    """
    loop = asyncio.get_running_loop()
    heard = loop.time()

    def alive() -> None:
        nonlocal heard
        heard = loop.time()

    def progress(*args: Any) -> None:
        alive()
        handler(*args)

    undo = await _hear_every_answer(proxy, alive)
    alive()
    task = asyncio.ensure_future(proxy.send_package("Install", {}, progress, remote))
    try:
        while True:
            patient = waiting_on_user is not None and waiting_on_user.is_set()
            limit = PATIENT_SECONDS if patient else SILENCE_SECONDS
            quiet = loop.time() - heard
            if quiet >= limit and patient:
                raise StillWaitingError(
                    "the install was still waiting for the app it replaces to be closed on the "
                    f"device, {PATIENT_SECONDS / 60:.0f} minutes on, so Graft stopped waiting"
                )
            if quiet >= limit:
                raise PhoneWentSilentError(
                    f"the device stopped answering: no word for {SILENCE_SECONDS:.0f} s "
                    "while it installed the app"
                )
            done, _ = await asyncio.wait({task}, timeout=limit - quiet)
            if done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()
            # A canceled read ends at once; bounded all the same.
            await asyncio.wait({task}, timeout=device_session.TEARDOWN_SECONDS)
        undo()


class DeviceInstallError(Exception):
    """The install failed. `output` is the device's (or pymobiledevice3's) own
    words, `<ErrorName>: <description>`, for `installer._install_hint`."""

    def __init__(self, output: str) -> None:
        super().__init__(output)
        self.output = output


def install_ipa(
    ipa: Path, *, udid: str, on_progress: Progress | None = None,
    on_trust_prompt: Callable[[], None] | None = device_session.say_trust_prompt,
    waiting_on_user: threading.Event | None = None,
) -> None:
    """Install `ipa` on `udid`, blocking until the device says it is done.

    `on_progress` gets each percentage the device reports. `on_trust_prompt`
    is called when the phone has never trusted this Mac and graft starts
    waiting (`device_session.PAIR_TIMEOUT_SECONDS`) for the user to tap
    Trust; by default it says so on stderr. Every failure is a
    `DeviceInstallError`: a refusal from the device keeps its error name
    (`ApplicationVerificationFailed: …`), and a connection problem is named
    by its pymobiledevice3 exception (`PasswordRequiredError: …`).
    `waiting_on_user`, once set, lets installd be silent for
    `PATIENT_SECONDS` rather than `SILENCE_SECONDS` (`_install_until_silent`).
    """
    try:
        device_session.run(_install(ipa, udid, on_progress, on_trust_prompt, waiting_on_user))
    except DeviceInstallError:
        raise
    except Exception as e:  # every failure becomes one error type
        raise DeviceInstallError(f"{type(e).__name__}: {e}") from e


async def _install(
    ipa: Path, udid: str, on_progress: Progress | None,
    on_trust_prompt: Callable[[], None] | None,
    waiting_on_user: threading.Event | None = None,
) -> None:
    from pymobiledevice3.services.afc import AfcService
    from pymobiledevice3.services.installation_proxy import (
        TEMP_REMOTE_BASEDIR,
        InstallationProxyService,
    )

    def handler(percent, *_):
        if on_progress is not None:
            on_progress(int(percent))

    # Every clean-up wait below, on one budget: a phone that has stopped
    # answering must not hold a Stop past the app's grace.
    teardown = Teardown()
    with _quiet_progress_bars():
        lock = await device_session.open_lockdown(udid, on_trust_prompt=on_trust_prompt)
        async with closing_best_effort(lock, within=teardown):
            proxy = InstallationProxyService(lockdown=lock)
            async with closing_best_effort(proxy, within=teardown):
                afc = AfcService(lockdown=lock)
                try:
                    await _answered(afc.makedirs(TEMP_REMOTE_BASEDIR), "when the install began")
                    await _remove_leftovers(afc, TEMP_REMOTE_BASEDIR)
                    remote = f"{TEMP_REMOTE_BASEDIR}/{staged_name()}"
                    try:
                        await upload(afc, ipa, remote, teardown)
                        await _install_until_silent(proxy, remote, handler,
                                                    waiting_on_user=waiting_on_user)
                    finally:
                        # Never in place of the install's own outcome: a phone
                        # gone by now has failed the install, or finished it.
                        await teardown(afc.rm_single(remote, force=True))
                finally:
                    # `aclose`, not `close`: it also stops AFC's reader task.
                    # Best effort, for `device_session`'s reason: a teardown
                    # failure must not replace a finished install.
                    await teardown(afc.aclose())


def staged_name() -> str:
    """The name this process stages an IPA under, one per install."""
    return f"graft-{this_mac()}-{os.getpid()}-{uuid.uuid4().hex}.ipa"


@functools.cache
def this_mac() -> str:
    """Twelve hex digits that stand for this Mac, the same on every run.

    The staging folder is on the phone, and a phone can be paired with, and
    reached by, several Macs: a pid alone said nothing about whose a file
    was, so one Mac removed another's upload in flight. Hashed from the host
    UUID (`gethostuuid`, the hardware UUID), so the identifier a phone shows
    every Mac it meets is not one that tracks this Mac anywhere else; from
    the host name if that cannot be read.
    """
    host = _host_uuid() or socket.gethostname().encode()
    return hashlib.sha256(b"graft staging\0" + host).hexdigest()[:12]


def _host_uuid() -> bytes | None:
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        found = ctypes.create_string_buffer(16)
        wait = (ctypes.c_long * 2)(1, 0)  # struct timespec: up to a second
        if libc.gethostuuid(found, wait) != 0 or not any(found.raw):
            return None
        return found.raw
    except (OSError, AttributeError):
        return None


async def _remove_leftovers(afc: Any, staging: str) -> None:
    """Remove the IPAs a graft on this Mac staged in `staging` and left.

    Only names graft gives (`_STAGED_NAME`), only this Mac's (`this_mac`),
    and only when that graft's pid is not a live process here: another graft
    may be installing on this phone right now, from a second run or the app.
    Another Mac's file is never removed, whatever its pid: nothing here can
    tell whether its upload is still running, and that Mac's next install
    clears it. Best effort: a leftover still there is clutter, and must not
    stop this install. A phone gone quiet does (`_answered`, the whole-diff
    review's cli-7): that is no clutter, and the install would only wait on
    it again.
    """
    sweeping = "while Graft cleared what earlier installs left"
    try:
        for name in await _answered(afc.listdir(staging), sweeping):
            if left_behind(name, _STAGED_NAME):
                await _answered(afc.rm_single(f"{staging}/{name}", force=True), sweeping)
    except PhoneWentSilentError:
        raise
    except Exception:
        pass


def left_behind(name: str, pattern: re.Pattern[str]) -> bool:
    """Whether `name` is one a graft on this Mac put on a phone and left
    there: `pattern` matches it whole, its group 1 is this Mac
    (`this_mac`), and its group 2 a pid with no live process here.

    The rule for every file graft leaves on a phone, the IPAs it stages
    (`_remove_leftovers`) and the LiveContainer push's partials
    (`lc_runner.is_abandoned_partial`). Whether a process on this Mac is
    running is a fact this Mac can check; nothing here can tell that of
    another Mac's.
    """
    match = pattern.fullmatch(name)
    return bool(match) and match.group(1) == this_mac() and not _running(int(match.group(2)))


def _running(pid: int) -> bool:
    """Whether `pid` is a live process; yes when it can't be told."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, OverflowError):  # PermissionError: alive, someone else's
        return True
    return True


async def upload(afc: Any, local: Path, remote: str, teardown: Teardown) -> None:
    """Copy `local` to `remote` over `afc`, read UPLOAD_CHUNK_BYTES at a
    time and written AFC_WRITE_BYTES at a time.

    A failed or stopped upload still closes its handle, within `teardown`'s
    budget; the write's own error (or the cancellation) is the one raised.
    Each write may wait `SILENCE_SECONDS` for the phone's answer, so the
    limit is on the phone acknowledging nothing, not on a chunk's whole time.
    """
    handle = await _answered(afc.fopen(remote, "w"), "when the upload began")
    try:
        with local.open("rb") as f:
            while chunk := f.read(UPLOAD_CHUNK_BYTES):
                view = memoryview(chunk)
                for at in range(0, len(view), AFC_WRITE_BYTES):
                    piece = bytes(view[at:at + AFC_WRITE_BYTES])
                    await _answered(afc.fwrite(handle, piece), "during the upload")
    except BaseException:
        await teardown(afc.fclose(handle))
        raise
    await _answered(afc.fclose(handle), "as the upload finished")


_quiet_lock = threading.Lock()
_quiet_users = 0
_real_bar: Any = None


@contextmanager
def _quiet_progress_bars() -> Iterator[None]:
    """Keep pymobiledevice3's own progress bars off graft's stderr.

    The upload draws none (`upload` is graft's own loop); the install's wait
    for "Complete" draws one, and so would any upload pymobiledevice3 did.

    pymobiledevice3 11.x draws them with `disable=False` hard-coded and offers
    no switch. When Graft.app runs graft, stderr is not a TTY and the app reads
    it for error lines, so each bar lands there as a line of text. While any
    install runs, `installation_proxy.ProgressBar` makes disabled bars; the
    last install to finish puts the real one back. A count under a lock,
    because LiveContainer installs run on a worker thread: with a bare swap,
    two overlapping installs would restore each other's stand-in.
    """
    from pymobiledevice3.services import installation_proxy

    global _quiet_users, _real_bar
    with _quiet_lock:
        if _quiet_users == 0:
            # Absent in releases that drew with tqdm instead: nothing to quiet.
            _real_bar = getattr(installation_proxy, "ProgressBar", None)
            if _real_bar is not None:
                installation_proxy.ProgressBar = _disabled(_real_bar)
        _quiet_users += 1
    try:
        yield
    finally:
        with _quiet_lock:
            _quiet_users -= 1
            if _quiet_users == 0 and _real_bar is not None:
                installation_proxy.ProgressBar = _real_bar
                _real_bar = None


def _disabled(bar: Any) -> Callable[..., Any]:
    def quiet(*args: Any, **kwargs: Any) -> Any:
        return bar(*args, **{**kwargs, "disable": True})

    return quiet
