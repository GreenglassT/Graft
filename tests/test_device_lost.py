"""The phone lost mid-transfer: a cable pulled, a Wi-Fi-synced phone asleep
(review #15 and the critic's gap 3).

What pymobiledevice3 raises on those paths, read from its source: a socket
that closes or resets under the AFC reader or InstallationProxy's wait is
`ConnectionTerminatedError` (`ServiceConnection.recvall`/`sendall`); a usbmux
connect the phone refuses is `ConnectionFailedError`; no usbmuxd at all is
`ConnectionFailedToUsbmuxdError`; a phone gone from usbmux is
`DeviceNotFoundError`; a service the phone won't start is
`StartServiceError`; an install stream that ends without "Complete" is a bare
`AppInstallError`. On 10.x each has an empty message, so the hint is all the
user gets.

And what reaches stderr, which the app shows as a failed run's log: the real
`AfcService` over a fake phone (`tests/afc_device.py`), run in a child.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pymobiledevice3.lockdown as pmd_lockdown
import pytest
from pymobiledevice3.exceptions import (
    AppInstallError,
    ConnectionFailedError,
    ConnectionFailedToUsbmuxdError,
    ConnectionTerminatedError,
    DeviceNotFoundError,
    StartServiceError,
)

from graft_ipa import installer, lc_runner
from graft_ipa.errors import InstallError

from .device_fakes import phone

REPO = Path(__file__).parent.parent
DROPPED = "the connection to the phone dropped"


def _ipa(tmp_path):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK..")
    return ipa


@pytest.mark.parametrize("where, error, phrase", [
    ("write", ConnectionTerminatedError(), DROPPED),           # cable pulled mid-upload
    ("write", ConnectionResetError(54, "Connection reset by peer"), DROPPED),
    ("install", ConnectionTerminatedError(), DROPPED),         # gone while installing
    ("connect", ConnectionFailedError("Connection refused"), "gone to sleep"),
    ("connect", ConnectionFailedToUsbmuxdError(), "usbmuxd"),
    ("connect", DeviceNotFoundError("no device u1"), "not connected"),
    ("connect", StartServiceError("com.apple.afc", ""), "wouldn't start the service"),
    ("install", AppInstallError(), "without the phone saying why"),
], ids=lambda v: v if isinstance(v, str) else type(v).__name__)
def test_a_phone_lost_mid_transfer_gets_a_plain_hint(tmp_path, monkeypatch, capsys, where, error, phrase):
    phone(monkeypatch, **{where: error})
    with pytest.raises(InstallError) as e:
        installer.install(_ipa(tmp_path), udid="u1")
    assert phrase in e.value.hint
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("output", [
    # 10.x: no default message, so the name is all there is.
    "ConnectionTerminatedError: ",
    # 11.x: the class docstring fills it.
    "ConnectionTerminatedError: Connection was terminated abruptly.",
])
def test_the_hint_does_not_depend_on_the_message(output):
    assert DROPPED in installer._install_hint(output)


def test_an_install_refusal_wrapped_in_appinstallerror_keeps_its_own_hint():
    """The bare-AppInstallError hint is for no reason at all, not for every
    AppInstallError: the device's codes arrive wrapped in one."""
    hint = installer._install_hint("AppInstallError: ApplicationVerificationFailed: no")
    assert "couldn't verify the signature" in hint


def test_a_livecontainer_push_lost_mid_transfer_says_so(tmp_path, monkeypatch):
    async def create_using_usbmux(serial=None, **_):
        raise ConnectionTerminatedError()

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create_using_usbmux)
    with pytest.raises(lc_runner.LiveContainerError) as e:
        lc_runner.push_ipa_to_livecontainer(_ipa(tmp_path), "u1")
    assert DROPPED in e.value.hint


# --- over the real AfcService -----------------------------------------------------


def _over_real_afc(mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", f"from tests.afc_device import main; main({mode!r})"],
        cwd=REPO, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.parametrize("mode", ["drop-write", "reset-write", "drop-install"])
def test_over_the_real_afc_a_lost_phone_is_one_plain_error_and_a_quiet_stderr(mode):
    """Unquieted, a drop mid-upload put asyncio's 'Future exception was never
    retrieved' and pymobiledevice3's traceback on stderr: the AFC reader fails
    every pending request, one whose sender had already given up on it too."""
    proc = _over_real_afc(mode)
    assert proc.returncode == 0, proc.stderr
    assert f"RESULT InstallError | {DROPPED}" in proc.stdout, proc.stdout
    assert proc.stderr == ""


# --- a phone that goes silent, the connection still open (re-check #13) ----------


SILENT = "the phone stopped answering"


@pytest.mark.parametrize("mode", ["silent-write", "silent-install"])
def test_a_phone_that_goes_silent_ends_in_a_plain_error_not_a_hang(mode):
    """A peer that stops answering but keeps the connection open (half-open
    TCP, a Wi-Fi-synced iPad asleep) left the upload's write, or the wait for
    "Complete", waiting for ever with nothing said. The fake answers nothing
    after it goes quiet, and the limit here is half a second."""
    from .afc_device import SILENCE

    proc = _over_real_afc(mode)
    assert proc.returncode == 0, proc.stderr
    assert f"RESULT InstallError | {SILENT}" in proc.stdout, proc.stdout
    elapsed = float(proc.stdout.split("ELAPSED ", 1)[1].split()[0])
    # The limit, plus the clean-up's own budget at most (`Teardown`).
    assert SILENCE <= elapsed < SILENCE + 5 + 2, elapsed
    assert proc.stderr == ""


def test_a_slow_install_that_keeps_reporting_is_not_cut_off():
    """An inactivity limit, not a total one: progress every third of the
    limit, for three times the limit, finishes."""
    proc = _over_real_afc("slow-install")
    assert "RESULT ok |" in proc.stdout, proc.stdout + proc.stderr


def test_the_silence_limit_is_the_wait_graft_already_grants_a_phone():
    from graft_ipa import device_install, device_session

    assert device_install.SILENCE_SECONDS == device_session.PAIR_TIMEOUT_SECONDS == 60


def test_a_livecontainer_push_to_a_silent_phone_gives_up_within_its_bounds(tmp_path, monkeypatch):
    """The LC push streams through the same `upload`, so the same limit
    applies. Its clean-up gets `Teardown`'s one budget, which a phone that
    answers nothing uses up; the hidden partial it leaves is overwritten by
    the next push (`_write_into_place`), and is never renamed into place."""
    import asyncio
    import time

    from graft_ipa import device_install, device_session, lc_runner

    monkeypatch.setattr(device_install, "SILENCE_SECONDS", 0.2)
    monkeypatch.setattr(device_session, "TEARDOWN_SECONDS", 0.3)

    class SilentSandbox:
        async def fopen(self, path, mode):
            return 1

        async def fwrite(self, handle, chunk):
            await asyncio.Event().wait()

        async def fclose(self, handle):
            await asyncio.Event().wait()

        async def rm_single(self, path, force=False):
            await asyncio.Event().wait()

        async def rename(self, a, b):
            raise AssertionError("a partial upload was renamed into place")

    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"x" * 1024)
    started = time.monotonic()

    with pytest.raises(device_install.PhoneWentSilentError, match="during the upload"):
        asyncio.run(lc_runner._write_into_place(SilentSandbox(), ipa, "Documents/example.ipa"))

    assert time.monotonic() - started < 0.2 + 0.3 + 1
