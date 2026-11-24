"""The silence limit counts any forward progress, not one report per stage
(combined fix check #31).

`SILENCE_SECONDS` is 60 s for real. It bounded each whole 4 MiB AFC write,
so a link under about 70 KB/s was taken for a phone gone quiet, and it was
reset only by an install answer carrying a PercentComplete, so a stage
reported by Status alone ran against it. Now every acknowledged AFC write
(`AFC_WRITE_BYTES`) and every answer installd sends count, and the budget is
unchanged: slow but steady progress (here, one step every 0.7 of the limit,
three steps in a row) goes through, and real silence is still cut off.

The install side runs pymobiledevice3's own `InstallationProxyService`
(`send_package` and its completion loop) over a scripted connection, so the
hook is checked against the library's real reading of installd's answers.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from graft_ipa import device_install
from graft_ipa.device_session import Teardown

SILENCE = 0.5
#: One step of slow but steady progress: inside the limit, three of them past it.
STEP = 0.7 * SILENCE


@pytest.fixture(autouse=True)
def _short_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_install, "SILENCE_SECONDS", SILENCE)


# --- the upload --------------------------------------------------------------------


class SlowLink:
    """An AFC service on a link that carries one `AFC_WRITE_BYTES` write per
    `STEP`: each write is acknowledged when its bytes are across. `stall_at`
    is the write after which it never answers again."""

    def __init__(self, *, stall_at: int | None = None) -> None:
        self.writes: list[int] = []
        self.stall_at = stall_at

    async def fopen(self, remote: str, mode: str) -> int:
        return 7

    async def fwrite(self, handle: int, data: bytes) -> None:
        if self.stall_at is not None and len(self.writes) >= self.stall_at:
            await asyncio.Event().wait()
        await asyncio.sleep(STEP * len(data) / device_install.AFC_WRITE_BYTES)
        self.writes.append(len(data))

    async def fclose(self, handle: int) -> None:
        pass


def _upload(afc: SlowLink, tmp_path: Path, size: int) -> float:
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"x" * size)
    started = time.monotonic()
    asyncio.run(asyncio.wait_for(device_install.upload(afc, ipa, "/PublicStaging/x.ipa", Teardown()), 15))
    return time.monotonic() - started


@pytest.fixture
def three_writes_a_chunk(monkeypatch: pytest.MonkeyPatch) -> int:
    """A chunk read from the file is three AFC writes: on this link, one
    chunk takes 2.1 times the limit."""
    monkeypatch.setattr(device_install, "AFC_WRITE_BYTES", 16 * 1024)
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 48 * 1024)
    return 48 * 1024


def test_a_slow_but_steady_upload_is_not_cut_off(tmp_path, three_writes_a_chunk) -> None:
    afc = SlowLink()
    took = _upload(afc, tmp_path, three_writes_a_chunk)
    assert afc.writes == [16 * 1024] * 3
    assert took > 2 * SILENCE, "premise: the chunk took longer than the limit"


def test_an_upload_that_stops_being_acknowledged_is_still_cut_off(tmp_path, three_writes_a_chunk) -> None:
    afc = SlowLink(stall_at=2)
    started = time.monotonic()
    with pytest.raises(device_install.PhoneWentSilentError, match="during the upload"):
        _upload(afc, tmp_path, three_writes_a_chunk)
    assert afc.writes == [16 * 1024] * 2
    assert time.monotonic() - started < 2 * STEP + SILENCE + 1


def test_the_real_write_size_keeps_a_slow_link_alive() -> None:
    """60 s for one write is under 9 KB/s; a whole 4 MiB chunk was 70 KB/s."""
    assert device_install.AFC_WRITE_BYTES <= 512 * 1024
    assert device_install.UPLOAD_CHUNK_BYTES % device_install.AFC_WRITE_BYTES == 0


# --- the install ---------------------------------------------------------------------


class Installd:
    """The installation_proxy connection: answers scripted as (delay,
    answer); after the last, nothing more (the connection stays open)."""

    def __init__(self, answers: list[tuple[float, dict]]) -> None:
        self.answers = list(answers)
        self.sent: list[dict] = []

    async def send_plist(self, d, *a, **k) -> None:
        self.sent.append(dict(d))

    async def recv_plist(self, *a, **k) -> dict:
        if not self.answers:
            await asyncio.Event().wait()
        delay, answer = self.answers.pop(0)
        await asyncio.sleep(delay)
        return answer

    async def close(self) -> None:
        pass


def _proxy(installd: Installd):
    """pymobiledevice3's own InstallationProxyService, connected to `installd`."""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    class Lockdown:
        async def start_lockdown_service(self, name, include_escrow_bag=False):
            return installd

        async def start_lockdown_developer_service(self, name, include_escrow_bag=False):
            return installd

    return InstallationProxyService(lockdown=Lockdown())


def _install(installd: Installd, waiting_on_user: threading.Event | None = None) -> list:
    percents: list = []

    async def run():
        with device_install._quiet_progress_bars():
            # Bounded here too, so a limit that never fires fails the test
            # rather than hanging it.
            await asyncio.wait_for(device_install._install_until_silent(
                _proxy(installd), "/PublicStaging/x.ipa", lambda p, *_: percents.append(p),
                waiting_on_user=waiting_on_user), 15)

    asyncio.run(run())
    return percents


#: Stages reported by Status alone, each inside the limit, three past it.
STATUS_ONLY = [(STEP, {"Status": "TakingInstallLock"}), (STEP, {"Status": "PreflightingApplication"}),
               (STEP, {"Status": "VerifyingApplication"})]


def test_stages_reported_by_status_alone_keep_the_install_alive() -> None:
    installd = Installd([*STATUS_ONLY, (0, {"Status": "Complete"})])
    started = time.monotonic()
    _install(installd)
    assert time.monotonic() - started > 2 * SILENCE, "premise: longer than the limit in all"
    assert installd.sent[0]["Command"] == "Install"


def test_progress_still_reaches_the_handler() -> None:
    installd = Installd([(STEP, {"Status": "CopyingApplication", "PercentComplete": 15}),
                         (STEP, {"Status": "ExtractingPackage"}),
                         (STEP, {"Status": "InspectingPackage", "PercentComplete": 40}),
                         (0, {"Status": "Complete"})])
    assert _install(installd) == [15, 40]


def test_an_install_that_goes_quiet_after_its_statuses_is_still_cut_off() -> None:
    installd = Installd(STATUS_ONLY[:2])
    started = time.monotonic()
    with pytest.raises(device_install.PhoneWentSilentError, match="while it installed"):
        _install(installd)
    took = time.monotonic() - started
    assert 2 * STEP + SILENCE <= took < 2 * STEP + SILENCE + 5 + 1, took


def test_a_proxy_with_no_connection_to_hook_is_run_as_before() -> None:
    """A test's stand-in (no `connect`, no `service`) is only timed by its
    progress reports, as it always was."""
    class Proxy:
        async def send_package(self, cmd, options, handler, path, *args):
            for p in (30, 60, 90):
                await asyncio.sleep(STEP)
                handler(p)

    seen: list = []
    asyncio.run(device_install._install_until_silent(Proxy(), "/x", lambda p, *_: seen.append(p)))
    assert seen == [30, 60, 90]



# --- cli-1: an --lc-install waits on the user, not on a phone gone quiet -----------
#
# iOS holds an install over a running LiveContainer until the user closes it,
# and installd says nothing meanwhile. `lc_manage._install_one` says "close
# LiveContainer on the device to finish" after 20 s and sets
# `waiting_on_user`; from then on installd's silence is the user's time, up
# to `PATIENT_SECONDS`. Timers scaled: the real 60 s limit is `SILENCE`.

#: `PATIENT_SECONDS` (15 minutes), scaled.
PATIENT = 3.0


def _real(seconds: float) -> float:
    """`seconds` of real time, on these tests' scale (the real limit is 60 s)."""
    return seconds * SILENCE / 60.0


@pytest.fixture
def patient(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_install, "PATIENT_SECONDS", PATIENT)


def _held_until_closed(gap_seconds: float) -> Installd:
    """installd's last stage, then nothing for `gap_seconds` (real time),
    then Complete: the user closing LiveContainer that long after."""
    return Installd([(STEP, {"Status": "InstallingApplication", "PercentComplete": 90}),
                     (_real(gap_seconds), {"Status": "Complete"})])


@pytest.mark.parametrize("gap", [65, 120])
def test_an_install_waiting_on_the_user_outlasts_the_silence_limit(patient, gap: int) -> None:
    """The verifier's experiment: 65 s and 120 s gaps failed with
    PhoneWentSilentError. With the notice out, they finish."""
    waiting = threading.Event()
    waiting.set()
    started = time.monotonic()
    assert _install(_held_until_closed(gap), waiting) == [90]
    assert time.monotonic() - started > SILENCE, "premise: past the old limit"


def test_the_notice_coming_out_mid_silence_turns_the_wait_patient(patient) -> None:
    waiting = threading.Event()
    threading.Timer(STEP + 0.1, waiting.set).start()
    assert _install(_held_until_closed(120), waiting) == [90]


@pytest.mark.parametrize("given", [False, True], ids=["sideload", "notice-not-out"])
def test_without_the_notice_the_silence_limit_still_holds(patient, given: bool) -> None:
    """An ordinary install (no switch), and an --lc-install before its
    notice: 60 s of silence is still a phone gone."""
    started = time.monotonic()
    with pytest.raises(device_install.PhoneWentSilentError, match="while it installed"):
        _install(_held_until_closed(120), threading.Event() if given else None)
    assert time.monotonic() - started < STEP + SILENCE + 1


def test_a_wait_on_the_user_past_the_longer_bound_says_so(patient) -> None:
    waiting = threading.Event()
    waiting.set()
    started = time.monotonic()
    with pytest.raises(device_install.StillWaitingError, match="still waiting for the app it replaces to be closed"):
        _install(Installd([(STEP, {"Status": "InstallingApplication"})]), waiting)
    took = time.monotonic() - started
    assert STEP + PATIENT <= took < STEP + PATIENT + 1.5, took


def test_the_longer_bound_is_minutes() -> None:
    assert device_install.PATIENT_SECONDS == 15 * 60
