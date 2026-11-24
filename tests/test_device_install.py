from __future__ import annotations

import os
import re
import threading
import uuid
from collections.abc import Callable
from typing import ClassVar

import pymobiledevice3.lockdown as pmd_lockdown
import pymobiledevice3.services.afc as pmd_afc
import pymobiledevice3.services.installation_proxy as pmd_ip
import pytest
from pymobiledevice3.exceptions import AfcException, AppInstallError, PasswordRequiredError

from graft_ipa import device_install

STAGING = pmd_ip.TEMP_REMOTE_BASEDIR

#: The bar class pymobiledevice3 draws installs with, before any install swaps
#: it out. 11.x only: 10.x draws no bar there, and has no `progress` module.
REAL_BAR = getattr(pmd_ip, "ProgressBar", None)
draws_bars = pytest.mark.skipif(
    REAL_BAR is None, reason="this pymobiledevice3 draws no install progress bar"
)


class FakeLockdown:
    #: A phone that already trusts this Mac: `open_lockdown` asks nothing.
    paired = True

    def __init__(self, serial):
        self.serial = serial
        self.closed = False

    async def close(self):
        self.closed = True


class FakeAfc:
    """The device's AFC share, as far as an install touches it: one shared
    file table (the phone), and a record of every write each session made."""

    instances: ClassVar[list[FakeAfc]] = []
    files: ClassVar[dict[str, bytearray]] = {}
    dirs: ClassVar[set[str]] = set()
    #: "fwrite" makes the second write fail, as a phone unplugged mid-upload,
    #: and the close after it.
    behaviour: ClassVar[str] = "ok"
    #: The phone gone by teardown: removing the staged file and closing fail.
    teardown_fails: ClassVar[bool] = False
    #: Listing the staging folder fails.
    listdir_fails: ClassVar[bool] = False

    def __init__(self, lockdown, service_name=None):
        self.lockdown = lockdown
        self.writes: list[bytes] = []
        self.removed: list[str] = []
        #: Every call, in order: ("listdir", path), ("rm", path), ("fopen", path).
        self.ops: list[tuple[str, str]] = []
        self.closed = False
        self._open: dict[int, str] = {}
        FakeAfc.instances.append(self)

    async def makedirs(self, path):
        FakeAfc.dirs.add(path)

    async def listdir(self, path):
        self.ops.append(("listdir", path))
        if FakeAfc.listdir_fails:
            raise AfcException("Failed to list the folder.", 1)
        return [f.rsplit("/", 1)[1] for f in FakeAfc.files if f.rsplit("/", 1)[0] == path]

    async def fopen(self, path, mode="r"):
        self.ops.append(("fopen", path))
        assert mode == "w"
        assert path.rsplit("/", 1)[0] in FakeAfc.dirs, f"no folder for {path}"
        FakeAfc.files[path] = bytearray()
        handle = len(self._open) + 1
        self._open[handle] = path
        return handle

    async def fwrite(self, handle, data, chunk_size=1 << 30):
        if FakeAfc.behaviour == "fwrite" and self.writes:
            raise AfcException("Failed to write to the file.", 1)
        self.writes.append(bytes(data))
        FakeAfc.files[self._open[handle]] += data

    async def fclose(self, handle):
        del self._open[handle]
        if FakeAfc.behaviour == "fwrite":
            raise ConnectionAbortedError("the phone went away")

    async def rm_single(self, path, force=False):
        self.ops.append(("rm", path))
        self.removed.append(path)
        if FakeAfc.teardown_fails:
            raise ConnectionAbortedError("the phone went away")
        return FakeAfc.files.pop(path, None) is not None

    async def aclose(self):
        self.closed = True
        if FakeAfc.teardown_fails:
            raise ConnectionAbortedError("the phone went away")


class FakeProxy:
    instances: ClassVar[list[FakeProxy]] = []
    behaviour = "ok"
    #: Run inside the install with the staged bytes, as the real one draws
    #: its bar there.
    during: ClassVar[Callable[[bytes], None] | None] = None

    def __init__(self, lockdown):
        self.lockdown = lockdown
        self.closed = False
        #: (command, options, remote path, the bytes staged there).
        self.sent: list[tuple[str, dict, str, bytes]] = []
        FakeProxy.instances.append(self)

    # The real signature (pymobiledevice3 10.4 and 11.x). No `install_from_local`:
    # that reads the whole IPA into memory, and graft must not call it.
    async def send_package(self, cmd, options, handler, package_path, *args):
        staged = bytes(FakeAfc.files[package_path])
        self.sent.append((cmd, options, package_path, staged))
        if FakeProxy.during is not None:
            FakeProxy.during(staged)
        if FakeProxy.behaviour == "verify-failed":
            raise AppInstallError("ApplicationVerificationFailed: Failed to verify code signature")
        for pct in (10, 55, 100):
            if handler:
                handler(pct, *args)

    async def close(self):
        self.closed = True


@pytest.fixture
def fakes(monkeypatch):
    FakeProxy.instances.clear()
    FakeProxy.behaviour = "ok"
    FakeProxy.during = None
    FakeAfc.instances.clear()
    FakeAfc.files.clear()
    FakeAfc.dirs.clear()
    FakeAfc.behaviour = "ok"
    FakeAfc.teardown_fails = False
    FakeAfc.listdir_fails = False
    made: list[FakeLockdown] = []

    async def create_using_usbmux(serial=None, **_):
        if serial == "locked":
            raise PasswordRequiredError()
        made.append(FakeLockdown(serial))
        return made[-1]

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(pmd_ip, "InstallationProxyService", FakeProxy)
    monkeypatch.setattr(pmd_afc, "AfcService", FakeAfc)
    return made


def test_it_installs_the_ipa_on_the_named_device_and_reports_progress(tmp_path, fakes):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    seen: list[int] = []
    device_install.install_ipa(ipa, udid="u1", on_progress=seen.append)
    assert fakes[0].serial == "u1"
    assert FakeAfc.instances[0].lockdown is fakes[0]
    [(cmd, options, _, staged)] = FakeProxy.instances[0].sent
    assert (cmd, options, staged) == ("Install", {}, b"PK")
    assert seen == [10, 55, 100]


def test_it_closes_the_services_and_the_session(tmp_path, fakes):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    assert FakeProxy.instances[0].closed and FakeAfc.instances[0].closed and fakes[0].closed


# --- the upload streams --------------------------------------------------------
#
# pymobiledevice3's `install_from_local` does `package_path.read_bytes()`: a
# 2 GB IPA is 2 GB of graft's memory. The upload is graft's own, a chunk at a
# time.


def test_the_ipa_is_uploaded_a_chunk_at_a_time(tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 4)
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"0123456789")  # three chunks: 4 + 4 + 2
    device_install.install_ipa(ipa, udid="u1")
    assert FakeAfc.instances[0].writes == [b"0123", b"4567", b"89"]
    [(_, _, _, staged)] = FakeProxy.instances[0].sent
    assert staged == b"0123456789"


def test_the_install_is_asked_for_the_path_it_was_uploaded_to(tmp_path, fakes):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    [(_, _, remote, _)] = FakeProxy.instances[0].sent
    assert remote.startswith(f"{STAGING}/") and remote.endswith(".ipa")
    assert STAGING in FakeAfc.dirs


@pytest.mark.parametrize("failure", [None, "verify-failed", "fwrite"])
def test_the_staged_ipa_is_removed_whatever_the_outcome(tmp_path, fakes, monkeypatch, failure):
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 1)
    FakeProxy.behaviour = failure or "ok"
    FakeAfc.behaviour = failure or "ok"
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    if failure is None:
        device_install.install_ipa(ipa, udid="u1")
    else:
        with pytest.raises(device_install.DeviceInstallError):
            device_install.install_ipa(ipa, udid="u1")
    [afc] = FakeAfc.instances
    assert [p for p in afc.removed if p.endswith(".ipa")], "nothing was removed"
    assert FakeAfc.files == {}
    assert afc.closed


def test_a_failed_upload_keeps_its_own_error_and_installs_nothing(tmp_path, fakes, monkeypatch):
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 1)
    FakeAfc.behaviour = "fwrite"
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    with pytest.raises(device_install.DeviceInstallError) as e:
        device_install.install_ipa(ipa, udid="u1")
    assert e.value.output.startswith("AfcException")
    assert FakeProxy.instances[0].sent == []


def test_a_teardown_failure_does_not_undo_a_finished_install(tmp_path, fakes):
    """`device_session`'s rule: the phone dropping after "Complete" fails the
    clean-up, not the install the user is told about."""
    FakeAfc.teardown_fails = True
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    seen: list[int] = []
    device_install.install_ipa(ipa, udid="u1", on_progress=seen.append)
    assert seen[-1] == 100


def test_a_teardown_failure_does_not_hide_the_devices_refusal(tmp_path, fakes):
    FakeAfc.teardown_fails = True
    FakeProxy.behaviour = "verify-failed"
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    with pytest.raises(device_install.DeviceInstallError) as e:
        device_install.install_ipa(ipa, udid="u1")
    assert e.value.output.startswith("AppInstallError: ApplicationVerificationFailed")


def test_a_device_refusal_keeps_the_devices_own_words(tmp_path, fakes):
    FakeProxy.behaviour = "verify-failed"
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    with pytest.raises(device_install.DeviceInstallError) as e:
        device_install.install_ipa(ipa, udid="u1")
    assert "ApplicationVerificationFailed" in e.value.output


def test_a_locked_device_says_so_by_name(tmp_path, fakes):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    with pytest.raises(device_install.DeviceInstallError) as e:
        device_install.install_ipa(ipa, udid="locked")
    assert "PasswordRequiredError" in e.value.output


# --- leftovers ------------------------------------------------------------------
#
# A Stop or Ctrl-C mid-install kills graft with the upload's daemon worker
# thread still in it (lc_manage), so its `finally` never removes the staged
# IPA. Each install clears what such a graft left behind, before uploading.

#: Above macOS's largest pid (99998): no process here ever has it.
GONE_PID = 999_999


def _staged(name: str, data: bytes = b"old") -> str:
    path = f"{STAGING}/{name}"
    FakeAfc.dirs.add(STAGING)
    FakeAfc.files[path] = bytearray(data)
    return path


#: This Mac, as graft names it in what it stages.
HERE = device_install.this_mac()
#: Another Mac paired with the same phone.
ELSEWHERE = "0123456789ab" if HERE != "0123456789ab" else "ba9876543210"


def test_graft_names_what_it_stages_after_this_mac_and_its_process(tmp_path, fakes):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    [(_, _, remote, _)] = FakeProxy.instances[0].sent
    assert re.fullmatch(rf"{STAGING}/graft-{HERE}-{os.getpid()}-[0-9a-f]{{32}}\.ipa", remote)


def test_this_mac_is_stable_and_is_not_the_host_uuid_itself():
    device_install.this_mac.cache_clear()
    first = device_install.this_mac()
    device_install.this_mac.cache_clear()
    assert device_install.this_mac() == first == HERE
    assert re.fullmatch(r"[0-9a-f]{12}", first)
    host = device_install._host_uuid()
    assert host is not None and len(host) == 16
    assert first not in host.hex() and first not in str(uuid.UUID(bytes=host)).replace("-", "")


def test_a_gone_grafts_leftover_is_removed_before_the_upload(tmp_path, fakes):
    leftover = _staged(f"graft-{HERE}-{GONE_PID}-{uuid.uuid4().hex}.ipa")
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    assert leftover not in FakeAfc.files
    ops = FakeAfc.instances[0].ops
    assert ops.index(("rm", leftover)) < next(i for i, op in enumerate(ops) if op[0] == "fopen")


def test_only_leftovers_graft_named_and_no_running_graft_owns_are_removed(tmp_path, fakes):
    kept = [
        _staged(f"{uuid.uuid4()}.ipa"),  # pymobiledevice3's own naming
        _staged("graft-notes.txt"),
        _staged(f"graft-{HERE}-{GONE_PID}-{uuid.uuid4().hex}.ipa.part"),
        # A graft still running may be mid-install with it: this one, or one
        # of another user's, which can't be signaled (pid 1 stands in).
        _staged(f"graft-{HERE}-{os.getpid()}-{uuid.uuid4().hex}.ipa"),
        _staged(f"graft-{HERE}-1-{uuid.uuid4().hex}.ipa"),
        # Another Mac's, mid-upload from there: its pid means nothing here
        # (review #16). Nor does the name graft gave before it named the Mac.
        _staged(f"graft-{ELSEWHERE}-{GONE_PID}-{uuid.uuid4().hex}.ipa"),
        _staged(f"graft-{GONE_PID}-{uuid.uuid4().hex}.ipa"),
    ]
    elsewhere = f"/PublicStaging/graft-{HERE}-{GONE_PID}-{uuid.uuid4().hex}.ipa"
    FakeAfc.files[elsewhere] = bytearray(b"old")
    # Listed last, so a clean-up that gave up on any of the above keeps it.
    _staged(f"graft-{HERE}-{GONE_PID}-{uuid.uuid4().hex}.ipa")
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    assert sorted(FakeAfc.files) == sorted([*kept, elsewhere])


def test_a_cleanup_that_fails_does_not_stop_the_install(tmp_path, fakes):
    FakeAfc.listdir_fails = True
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    assert [staged for *_, staged in FakeProxy.instances[0].sent] == [b"PK"]


def test_with_no_usbmuxd_to_reach_it_fails_by_name_and_touches_no_phone(tmp_path):
    """No fakes: the real pymobiledevice3, pointed by the suite's autouse
    guard at a socket nothing listens on. This is also the proof that guard
    holds, so a test that forgets its fakes can't reach a real phone."""
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    with pytest.raises(device_install.DeviceInstallError) as e:
        device_install.install_ipa(ipa, udid="u1")
    assert e.value.output.startswith("ConnectionFailedToUsbmuxdError")


def _draw_a_bar(_staged: bytes) -> None:
    """What pymobiledevice3 11.x does around an upload and an install:
    `installation_proxy.ProgressBar(...)`, with `disable` left False."""
    with pmd_ip.ProgressBar(total=1) as bar:
        bar.update(1)


@draws_bars
def test_pymobiledevice3s_progress_bars_stay_out_of_graft_stderr(tmp_path, fakes, capsys):
    """A graft run by Graft.app has a non-TTY stderr that the app reads for
    error lines. An enabled bar writes `━━━ 100% 1/1 0:00:00` there."""
    FakeProxy.during = _draw_a_bar
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    device_install.install_ipa(ipa, udid="u1")
    assert capsys.readouterr().err == ""


@draws_bars
@pytest.mark.parametrize("behaviour", ["ok", "verify-failed"])
def test_the_real_progress_bar_is_put_back_after_an_install(tmp_path, fakes, behaviour):
    FakeProxy.behaviour = behaviour
    FakeProxy.during = _draw_a_bar
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    try:
        device_install.install_ipa(ipa, udid="u1")
    except device_install.DeviceInstallError:
        assert behaviour == "verify-failed"
    assert pmd_ip.ProgressBar is REAL_BAR


@draws_bars
def test_overlapping_installs_keep_bars_off_until_the_last_one_ends(tmp_path, fakes, capsys):
    """LiveContainer installs run on a worker thread. If two overlap, the one
    that ends first must not switch bars back on under the other, nor leave
    the other's stand-in in place for good."""
    first_in, second_in, first_done = threading.Event(), threading.Event(), threading.Event()

    def during(staged: bytes) -> None:
        if staged == b"first":
            first_in.set()
            assert second_in.wait(5)
        else:
            second_in.set()
            assert first_done.wait(5)
            _draw_a_bar(staged)

    FakeProxy.during = during
    errors: list[BaseException] = []

    def install(name: str, then: threading.Event | None = None) -> None:
        ipa = tmp_path / name
        ipa.write_bytes(name.removesuffix(".ipa").encode())
        try:
            device_install.install_ipa(ipa, udid="u1")
        except BaseException as e:
            errors.append(e)
        finally:
            if then is not None:
                then.set()

    first = threading.Thread(target=install, args=("first.ipa", first_done))
    second = threading.Thread(target=install, args=("second.ipa",))
    first.start()
    assert first_in.wait(5)
    second.start()
    first.join(10)
    second.join(10)
    assert errors == []
    assert capsys.readouterr().err == ""
    assert pmd_ip.ProgressBar is REAL_BAR


# --- the real LiveContainer install (review #26) ------------------------------------


def test_lc_installs_device_hands_the_ipa_to_device_install(tmp_path, fakes):
    """`UsbmuxDevice.install_ipa`, the one line that puts a prepared
    LiveContainer on the phone. Every --lc-install test swaps the whole device
    for a fake, so a no-op here, or its arguments swapped, left the suite
    green while nothing reached the phone."""
    from graft_ipa import lc_manage

    ipa = tmp_path / "LiveContainer.ipa"
    ipa.write_bytes(b"LC-IPA")
    seen: list[int] = []
    lc_manage.UsbmuxDevice().install_ipa("u1", ipa, on_progress=seen.append)
    assert [lock.serial for lock in fakes] == ["u1"]
    [(cmd, _, _, staged)] = FakeProxy.instances[0].sent
    assert (cmd, staged) == ("Install", b"LC-IPA")
    assert seen == [10, 55, 100]


def test_the_wait_on_the_user_reaches_the_install_wait(tmp_path, fakes, monkeypatch):
    """cli-1: `--lc-install`'s switch goes all the way to installd's wait."""
    import threading

    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    seen: list = []
    real = device_install._install_until_silent

    async def recording(proxy, remote, handler, **kw):
        seen.append(kw.get("waiting_on_user"))
        return await real(proxy, remote, handler, **kw)

    monkeypatch.setattr(device_install, "_install_until_silent", recording)
    switch = threading.Event()
    device_install.install_ipa(ipa, udid="u1", waiting_on_user=switch)
    device_install.install_ipa(ipa, udid="u1")
    assert seen == [switch, None]
