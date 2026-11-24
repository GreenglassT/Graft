"""A Stop (SIGTERM, SIGHUP) in the middle of an install (review #2).

`lc_manage.exit_on_termination` raises `Stopped` at whatever bytecode the main
thread is on. Mid-upload that is `select()`, so the `Stopped` left the event
loop and `asyncio.run`'s teardown canceled every task together,
pymobiledevice3's AFC reader with them. The upload's clean-up then waited for
an AFC reply nothing was left to deliver: graft hung until the app SIGKILLed
it, with no `build-end`, the build's temp folder left behind and the partial
IPA left on the phone.

The fakes here answer the way pymobiledevice3's AFC does, through a reader
task: cancel that task and nothing answers. The Stop is sent from another
thread, as the app sends it from another process, so it lands while the main
thread waits in `select()`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from typing import ClassVar

import pymobiledevice3.lockdown as pmd_lockdown
import pymobiledevice3.services.afc as pmd_afc
import pymobiledevice3.services.installation_proxy as pmd_ip
import pytest

from graft_ipa import device_install, device_session, installer, lc_manage, lc_runner
from graft_ipa.errors import Stopped

STAGING = pmd_ip.TEMP_REMOTE_BASEDIR

#: How long a fake phone takes over anything that "hangs". Long against the
#: test's patience, short enough that an unfixed install fails the test
#: rather than hanging the suite.
HANG = 4.0
#: The clean-up budget under test, cut down so these run quickly.
BUDGET = 0.5
#: A stopped install must be over well inside this.
PROMPT = 2.5


class FakeLockdown:
    paired = True

    def __init__(self, serial):
        self.serial = serial

    async def close(self):
        pass


#: The LiveContainer a push goes into.
LC = "com.kdt.livecontainer.ABCDE12345"


class FakeProxy:
    def __init__(self, lockdown):
        pass

    async def send_package(self, *a, **k):
        raise AssertionError("a stopped upload must never be installed")

    async def lookup(self, options=None):
        # The apps a push chooses its LiveContainer from.
        return {LC: {"CFBundleIdentifier": LC, "ApplicationType": "User"}}

    async def close(self):
        pass


class ReaderAfc:
    """AFC whose replies come back through a reader task, like
    pymobiledevice3's `afc-reader`. The first chunk goes through; the second
    is a slow phone, and that is where the Stop arrives."""

    files: ClassVar[dict[str, bytes]] = {}
    in_upload: ClassVar[threading.Event] = threading.Event()
    #: The phone stops answering the clean-up at all (closing the file,
    #: removing it, closing the service): the budget is all that ends it.
    cleanup_hangs: ClassVar[bool] = False
    #: How long the slow second write takes.
    slow_write: ClassVar[float] = HANG

    def __init__(self, lockdown, service_name=None):
        self._requests: asyncio.Queue | None = None
        self._reader: asyncio.Task | None = None
        self._writes = 0

    async def _answer(self, result=None):
        loop = asyncio.get_running_loop()
        if self._reader is None:
            self._requests = asyncio.Queue()
            self._reader = loop.create_task(self._read(), name="afc-reader")
        if ReaderAfc.cleanup_hangs and self._writes > 1:
            await asyncio.sleep(HANG)
        fut = loop.create_future()
        await self._requests.put((fut, result))
        return await asyncio.wait_for(fut, HANG)

    async def _read(self):
        while True:
            fut, result = await self._requests.get()
            if not fut.done():
                fut.set_result(result)

    async def makedirs(self, path):
        return await self._answer()

    async def listdir(self, path):
        return await self._answer([])

    async def fopen(self, path, mode="r"):
        ReaderAfc.files[path] = b""
        self._path = path
        return await self._answer(1)

    async def fwrite(self, handle, data, chunk_size=1 << 30):
        self._writes += 1
        if self._writes > 1:
            ReaderAfc.in_upload.set()
            await asyncio.sleep(ReaderAfc.slow_write)  # the Stop lands here
        await self._answer()
        ReaderAfc.files[self._path] += bytes(data)

    async def fclose(self, handle):
        return await self._answer()

    async def rm_single(self, path, force=False):
        await self._answer()
        ReaderAfc.files.pop(path, None)

    async def aclose(self):
        if ReaderAfc.cleanup_hangs:
            await asyncio.sleep(HANG)
        if self._reader is not None:
            self._reader.cancel()


class ReaderSandbox(ReaderAfc):
    """A LiveContainer's Documents over the same AFC: what a push writes to
    (`HouseArrestService` is an `AfcService`)."""

    async def exists(self, path):
        return await self._answer(path in ReaderAfc.files)

    async def rename(self, source, target):
        await self._answer()
        ReaderAfc.files[target] = ReaderAfc.files.pop(source)

    async def close(self):
        await self.aclose()


@pytest.fixture
def lc_phone(phone, monkeypatch):
    """`phone`, with a LiveContainer installed to push into."""
    from pymobiledevice3.services.house_arrest import HouseArrestService

    async def create(*, lockdown, bundle_id):
        assert bundle_id == LC
        return ReaderSandbox(lockdown)

    monkeypatch.setattr(HouseArrestService, "create", create)
    return phone


def _push(ipa) -> None:
    lc_runner.push_ipa_to_livecontainer(ipa, "u1", inner_filename="example.ipa",
                                        free_account=True)


@pytest.fixture
def phone(monkeypatch, tmp_path):
    ReaderAfc.files.clear()
    ReaderAfc.in_upload = threading.Event()
    ReaderAfc.cleanup_hangs = False
    ReaderAfc.slow_write = HANG

    async def create_using_usbmux(serial=None, **_):
        return FakeLockdown(serial)

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(pmd_ip, "InstallationProxyService", FakeProxy)
    monkeypatch.setattr(pmd_afc, "AfcService", ReaderAfc)
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 2)
    monkeypatch.setattr(device_session, "TEARDOWN_SECONDS", BUDGET)
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"0123456789")
    return ipa


class _Signaller:
    """Sends `sig` to this process once the upload is under way, from a
    thread of its own, and again `again_after` seconds later if that is
    given; joined before the handlers it relies on are gone."""

    def __init__(self, sig: int = signal.SIGTERM, *, again_after: float | None = None) -> None:
        self.sent = False
        #: When the last signal went (`time.monotonic`).
        self.last_at: float | None = None
        self._again_after = again_after
        self._thread = threading.Thread(target=self._go, args=(sig,), daemon=True)
        self._thread.start()

    def _go(self, sig: int) -> None:
        if ReaderAfc.in_upload.wait(10):
            time.sleep(0.05)  # in select(), not in the fake's own frame
            self.sent = True
            self.last_at = time.monotonic()
            os.kill(os.getpid(), sig)
            if self._again_after is not None:
                time.sleep(self._again_after)
                self.last_at = time.monotonic()
                os.kill(os.getpid(), sig)

    def join(self) -> None:
        self._thread.join(15)
        assert not self._thread.is_alive()


def _stopped(
    work, sig: int = signal.SIGTERM, *, again_after: float | None = None,
) -> tuple[float, float, BaseException | None]:
    """Run `work` as the build does (inside `exit_on_termination`), with a
    Stop (`sig`) mid-upload, and a second one `again_after` seconds after it
    if that is given. Returns how long it took in all, how long it went on
    after the last signal, and what it raised."""
    raised: BaseException | None = None
    with lc_manage.exit_on_termination():
        signaller = _Signaller(sig, again_after=again_after)
        start = time.monotonic()
        try:
            work()
        except BaseException as e:  # what it raised is the result
            raised = e
        end = time.monotonic()
        signaller.join()
    assert signaller.sent and signaller.last_at is not None
    return end - start, end - signaller.last_at, raised


def _stopped_install(ipa, sig: int = signal.SIGTERM) -> tuple[float, BaseException | None]:
    """A sideload install (`installer.install`) with a Stop mid-upload.
    Returns how long it took and what it raised."""
    elapsed, _, raised = _stopped(lambda: installer.install(ipa, udid="u1"), sig)
    return elapsed, raised


def test_a_stop_mid_upload_ends_promptly_as_stopped_and_removes_the_staged_ipa(phone):
    elapsed, raised = _stopped_install(phone)
    # `Stopped`, not an install failure: the build's envelope reads it as a
    # stop, writes build-end {stopped} and removes its temp folder.
    assert isinstance(raised, Stopped) and raised.code == 128 + signal.SIGTERM
    assert elapsed < PROMPT
    assert ReaderAfc.files == {}, "the partial IPA was left in PublicStaging"


def test_a_terminal_hangup_mid_upload_is_a_stop_and_removes_the_staged_ipa(phone):
    """SIGHUP, the Terminal window closed under `graft`, is a Stop as SIGTERM
    is (re-check D1). Left to `exit_on_termination` alone, it raised `Stopped`
    in `select()` and tore the AFC reader down with the rest, so the
    clean-up's removal of the staged IPA never got an answer."""
    elapsed, raised = _stopped_install(phone, signal.SIGHUP)
    assert isinstance(raised, Stopped) and raised.code == 128 + signal.SIGHUP
    assert elapsed < PROMPT
    assert ReaderAfc.files == {}, "the partial IPA was left in PublicStaging"


def test_a_stop_ends_within_the_budget_when_the_phone_never_answers_the_cleanup(phone):
    ReaderAfc.cleanup_hangs = True
    elapsed, raised = _stopped_install(phone)
    assert isinstance(raised, Stopped)
    assert elapsed < PROMPT


def test_a_stop_mid_push_removes_the_partial_and_leaves_nothing_at_the_real_name(lc_phone):
    """Re-check D5: only `except BaseException` in `_write_into_place` sees
    the Stop (a `CancelledError` out of the upload) and removes the hidden
    partial, which would otherwise sit in LiveContainer's Documents."""
    elapsed, _, raised = _stopped(lambda: _push(lc_phone))
    assert isinstance(raised, Stopped) and raised.code == 128 + signal.SIGTERM
    assert elapsed < PROMPT
    assert ReaderAfc.in_upload.is_set()
    assert ReaderAfc.files == {}, "the stopped push left its partial in Documents"


#: How long after the first Stop the second comes: the clean-up is stuck by then.
SECOND_STOP_AFTER = 0.3
#: How long graft may go on after the second.
SECOND_STOP_PROMPT = 1.0


@pytest.mark.parametrize("how", ["sideload", "push"])
def test_a_second_stop_ends_a_clean_up_the_phone_never_answers(lc_phone, monkeypatch, how):
    """Re-check D2. The phone stops answering as the user stops the install,
    and the clean-up waits for it. A second Stop ends the clean-up, as the
    `Teardown` docstring said it did: it used to cancel only the wait in
    progress, and the next wait used up the rest of the budget."""
    ReaderAfc.cleanup_hangs = True
    # A budget long enough to tell "the rest of the budget" from "at once".
    monkeypatch.setattr(device_session, "TEARDOWN_SECONDS", HANG)
    if how == "push":
        def work():
            _push(lc_phone)
    else:
        def work():
            installer.install(lc_phone, udid="u1")

    elapsed, after_second, raised = _stopped(work, again_after=SECOND_STOP_AFTER)

    assert isinstance(raised, Stopped) and raised.code == 128 + signal.SIGTERM
    assert after_second < SECOND_STOP_PROMPT, f"went on {after_second:.2f} s after the second Stop"
    assert elapsed < SECOND_STOP_AFTER + SECOND_STOP_PROMPT + 0.5


def test_a_stop_puts_back_the_handlers_it_found(phone):
    with lc_manage.exit_on_termination():
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP)}
        signaller = _Signaller()
        with pytest.raises(Stopped):
            device_install.install_ipa(phone, udid="u1")
        signaller.join()
        assert {sig: signal.getsignal(sig) for sig in handlers} == handlers


def test_an_ignored_hangup_stays_ignored(phone, monkeypatch):
    """`nohup graft …` asks for the run to outlive its terminal: a SIGHUP is
    not a Stop, and the upload carries on."""
    ReaderAfc.slow_write = 0.3
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with lc_manage.exit_on_termination():
            signaller = _Signaller(signal.SIGHUP)
            with pytest.raises(device_install.DeviceInstallError) as e:
                device_install.install_ipa(phone, udid="u1")
            signaller.join()
        # It got as far as the install, which this fake refuses.
        assert "a stopped upload must never be installed" in e.value.output
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_off_the_main_thread_it_sets_no_handler(phone, monkeypatch):
    """`--lc-install` installs on a worker thread, where handlers cannot be
    set: there it is a plain `asyncio.run`."""
    seen: list[BaseException] = []

    async def done():
        return "ok"

    def work():
        try:
            assert device_session.run(done()) == "ok"
        except BaseException as e:
            seen.append(e)

    worker = threading.Thread(target=work)
    worker.start()
    worker.join(5)
    assert seen == []


# --- what a Stop leaves on stderr ------------------------------------------------
#
# A canceled AFC request's reply arrives with nobody waiting for it, and
# pymobiledevice3 logs `AFC: no waiter for packet_num=N` at WARNING. graft
# configures no logging, so `logging.lastResort` printed it on stderr, which
# the app shows as a failed run's log. Run in a child: pytest's own log
# capture would swallow the record here and prove nothing.

_LOG_A_WARNING = (
    "import logging{extra}; "
    "logging.getLogger('pymobiledevice3.services.afc').warning('AFC: no waiter for packet_num=7')"
)


def _stderr_of(code: str) -> str:
    import subprocess
    import sys
    from pathlib import Path

    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=Path(__file__).parent.parent,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stderr


def test_pymobiledevice3s_warnings_stay_off_stderr():
    # The control: with nothing of graft's imported, the line does reach stderr.
    assert "no waiter" in _stderr_of(_LOG_A_WARNING.format(extra=""))
    assert _stderr_of(_LOG_A_WARNING.format(extra=", graft_ipa.device_install")) == ""


def test_a_configured_root_logger_still_gets_them():
    """Quiet by default, not discarded: anyone who configures logging (a
    debugging session, a future --verbose) still sees pymobiledevice3's."""
    code = _LOG_A_WARNING.format(extra=", graft_ipa.device_install; logging.basicConfig()")
    assert "no waiter" in _stderr_of(code)
