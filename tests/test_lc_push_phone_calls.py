"""The LiveContainer push and the sideload install, call by call, against a
phone that fails or goes quiet at one of them (the whole-diff review's
cli-5, cli-6 and cli-7).

- cli-5: `_write_into_place` removed the old IPA and then renamed the new one
  in; a rename that failed left neither. Now the old one is moved aside and
  put back if the new one can't go in, and the whole upload is kept, hidden.
- cli-6: `_is_an_archive` read a failed `stat` as "not ours", so Graft's own
  earlier push was refused as a stranger's file. Only not-found is.
- cli-7: makedirs, the sweeps, the LiveContainer lookup, the sandbox's
  creation, `_is_foreign`'s reads and the post-upload rename had no limit,
  so a half-open phone held the push until Stop. Each is now
  `device_install._answered`'s: `PhoneWentSilentError`, with its hint.

The fakes answer as pymobiledevice3's AFC does, and `hang` names the calls
that never answer. `SILENCE_SECONDS` is cut to `SILENCE`.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from pymobiledevice3.exceptions import AfcException, AfcFileNotFoundError, ConnectionTerminatedError

from graft_ipa import device_install, device_session, lc_runner

SILENCE = 0.3
#: How long past the limit a test allows for the rest of the run.
SLACK = 1.5


@pytest.fixture(autouse=True)
def _short_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_install, "SILENCE_SECONDS", SILENCE)
    monkeypatch.setattr(device_session, "TEARDOWN_SECONDS", 0.3)


class Sandbox:
    """A LiveContainer's Documents over AFC. `files` by path; `hang` the
    calls that never answer; `fail` a call -> the exception it raises.
    `rename` refuses an existing target, which AFC may: the push must not
    depend on it replacing one."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.hang: set[str] = set()
        self.fail: dict[str, BaseException] = {}
        self.calls: list[tuple] = []
        self._open: dict[int, str] = {}
        self._reading: dict[int, list] = {}

    async def _call(self, name: str, *args) -> None:
        self.calls.append((name, *args))
        if name in self.hang:
            await asyncio.Event().wait()
        if name in self.fail:
            raise self.fail[name]

    async def listdir(self, folder: str) -> list[str]:
        await self._call("listdir", folder)
        return sorted(p.removeprefix(f"{folder}/") for p in self.files if p.startswith(f"{folder}/"))

    async def exists(self, path: str) -> bool:
        await self._call("exists", path)
        return path in self.files

    async def stat(self, path: str) -> dict:
        await self._call("stat", path)
        if path not in self.files:
            raise AfcFileNotFoundError(f"No such file on the device: {path}", 4, path)
        return {"st_size": len(self.files[path])}

    async def fopen(self, path: str, mode: str = "r") -> int:
        await self._call("fopen", path, mode)
        handle = len(self._open) + len(self._reading) + 1
        if mode == "w":
            self.files[path] = b""
            self._open[handle] = path
        else:
            self._reading[handle] = [path, 0]
        return handle

    async def fseek(self, handle: int, offset: int, whence: int = 0) -> None:
        await self._call("fseek", offset)
        self._reading[handle][1] = offset

    async def fread(self, handle: int, size: int) -> bytes:
        await self._call("fread", size)
        path, at = self._reading[handle]
        data = self.files[path][at:at + size]
        self._reading[handle][1] = at + len(data)
        return data

    async def fwrite(self, handle: int, data: bytes) -> None:
        await self._call("fwrite")
        self.files[self._open[handle]] += bytes(data)

    async def fclose(self, handle: int) -> None:
        await self._call("fclose")
        self._open.pop(handle, None)
        self._reading.pop(handle, None)

    async def rm_single(self, path: str, force: bool = False) -> bool:
        await self._call("rm_single", path)
        return self.files.pop(path, None) is not None

    async def rename(self, source: str, target: str) -> None:
        await self._call("rename", source, target)
        if target in self.files:
            raise AfcException(f"{target} exists", 16, target)
        self.files[target] = self.files.pop(source)


TARGET = "Documents/example.ipa"


def _ipa(tmp_path: Path, body: bytes = b"the new build") -> Path:
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(body)
    return ipa


def _write(ha: Sandbox, ipa: Path) -> None:
    asyncio.run(asyncio.wait_for(lc_runner._write_into_place(ha, ipa, TARGET), 15))


# --- cli-5: whole or not at all, the old file included --------------------------------


def test_a_push_over_an_earlier_one_replaces_it_and_leaves_nothing_beside(tmp_path) -> None:
    ha = Sandbox({TARGET: b"the old build"})
    _write(ha, _ipa(tmp_path))
    assert ha.files == {TARGET: b"the new build"}


def test_a_push_with_nothing_there_puts_it_in_place(tmp_path) -> None:
    ha = Sandbox()
    _write(ha, _ipa(tmp_path))
    assert ha.files == {TARGET: b"the new build"}


def test_a_rename_that_fails_puts_the_old_file_back_and_keeps_the_new_one_hidden(tmp_path) -> None:
    """It removed the old file first, then the failed rename's clean-up
    removed the partial: neither was left."""
    ha = Sandbox({TARGET: b"the old build"})
    real = ha.rename

    async def rename(source, target):
        if target == TARGET and source == lc_runner.partial_path(TARGET):
            ha.calls.append(("rename", source, target))
            raise AfcException("Permission denied", 10, target)
        await real(source, target)

    ha.rename = rename
    with pytest.raises(AfcException):
        _write(ha, _ipa(tmp_path))
    assert ha.files[TARGET] == b"the old build"
    partial = lc_runner.partial_path(TARGET)
    assert ha.files[partial] == b"the new build", "the whole upload kept, hidden, for the next push to sweep"
    assert set(ha.files) == {TARGET, partial}
    assert lc_runner.is_abandoned_partial(partial.rsplit("/", 1)[1]) is False, "this process is alive"


def test_the_old_file_moved_aside_is_one_a_later_push_sweeps(tmp_path) -> None:
    """If putting it back fails too, what is left aside is hidden and named
    as a partial is, so the next push or --lc-clean removes it once this
    process is gone."""
    ha = Sandbox({TARGET: b"the old build"})
    moves: list[tuple[str, str]] = []
    real = ha.rename

    async def rename(source, target):
        moves.append((source, target))
        if target == TARGET:
            raise AfcException("Permission denied", 10, target)
        await real(source, target)

    ha.rename = rename
    with pytest.raises(AfcException):
        _write(ha, _ipa(tmp_path))
    aside = moves[0][1]
    assert moves[0][0] == TARGET and aside.startswith("Documents/.")
    assert lc_runner._PARTIAL_NAME.fullmatch(aside.rsplit("/", 1)[1])
    assert ha.files[aside] == b"the old build", "never deleted: only moved"


def test_a_failed_upload_still_removes_its_partial_and_leaves_the_old_file(tmp_path) -> None:
    ha = Sandbox({TARGET: b"the old build"})
    ha.fail["fwrite"] = ConnectionTerminatedError()
    with pytest.raises(ConnectionTerminatedError):
        _write(ha, _ipa(tmp_path))
    assert ha.files == {TARGET: b"the old build"}


# --- cli-6: only "not found" is "not an archive" ----------------------------------------


@pytest.mark.parametrize("error", [ConnectionTerminatedError(), AfcException("Permission denied", 10, TARGET)],
                         ids=["dropped", "refused"])
def test_a_stat_that_fails_is_raised_not_read_as_a_stranger_s_file(tmp_path, error) -> None:
    archive = _ipa(tmp_path, b"PK...an earlier build")
    ha = Sandbox({TARGET: archive.read_bytes()})
    ha.fail["stat"] = error
    with pytest.raises(type(error)):
        asyncio.run(lc_runner._is_foreign(ha, "example.ipa", "lc", {}, [archive]))


def test_a_file_gone_before_its_stat_is_not_an_archive(tmp_path) -> None:
    archive = _ipa(tmp_path, b"PK...an earlier build")
    ha = Sandbox()
    assert asyncio.run(lc_runner._is_an_archive(ha, TARGET, [archive])) is False


def test_the_premise_graft_s_own_earlier_push_is_recognised(tmp_path) -> None:
    archive = _ipa(tmp_path, b"PK...an earlier build")
    ha = Sandbox({TARGET: archive.read_bytes()})
    assert asyncio.run(lc_runner._is_foreign(ha, "example.ipa", "lc", {}, [archive])) is False


# --- cli-7: every phone call has the silence limit ------------------------------------


def _silent(coro) -> float:
    started = time.monotonic()
    with pytest.raises(device_install.PhoneWentSilentError):
        asyncio.run(asyncio.wait_for(coro, 15))
    took = time.monotonic() - started
    assert took < SILENCE + SLACK, took
    return took


@pytest.mark.parametrize("call", ["exists", "stat", "fopen", "fread"])
def test_is_foreign_s_reads_give_up_on_a_silent_phone(tmp_path, call) -> None:
    archive = _ipa(tmp_path, b"PK...an earlier build")
    ha = Sandbox({TARGET: archive.read_bytes()})
    ha.hang.add(call)
    _silent(lc_runner._is_foreign(ha, "example.ipa", "lc", {}, [archive]))


def test_the_sweep_gives_up_on_a_silent_phone_rather_than_swallowing_it() -> None:
    ha = Sandbox()
    ha.hang.add("listdir")
    _silent(lc_runner._sweep_partials(ha))


@pytest.mark.parametrize("call", ["rename", "exists"])
def test_the_rename_into_place_gives_up_on_a_silent_phone(tmp_path, call) -> None:
    ha = Sandbox({TARGET: b"the old build"})
    ha.hang.add(call)
    _silent(lc_runner._write_into_place(ha, _ipa(tmp_path), TARGET))


def test_the_livecontainer_lookup_gives_up_on_a_silent_phone(monkeypatch) -> None:
    from graft_ipa import lc_discovery

    async def never(lock):
        await asyncio.Event().wait()

    monkeypatch.setattr(lc_discovery, "installed_bundle_ids", never)
    _silent(lc_runner._resolve_lc_bundle_id(object(), pinned=None, team=None, free_account=True))


def test_a_push_whose_sandbox_never_opens_says_the_phone_went_quiet(monkeypatch, tmp_path) -> None:
    import pymobiledevice3.services.house_arrest as house_arrest_mod

    from tests.test_lc_discovery import OTHER, fake_device

    fake_device(monkeypatch, [f"com.kdt.livecontainer.{OTHER}"])

    async def never(*, lockdown, bundle_id):
        await asyncio.Event().wait()

    monkeypatch.setattr(house_arrest_mod.HouseArrestService, "create", never)
    started = time.monotonic()
    with pytest.raises(lc_runner.LiveContainerError) as e:
        asyncio.run(asyncio.wait_for(lc_runner._push_async(
            _ipa(tmp_path), "example.ipa", "UDID1", pinned=None, team=None, free_account=True,
            ours={}, archives=[]), 15))
    assert time.monotonic() - started < SILENCE + SLACK
    assert "stopped answering" in (e.value.hint or "")


class StagingAfc(Sandbox):
    """The sideload install's PublicStaging over AFC."""

    async def makedirs(self, path: str) -> None:
        await self._call("makedirs", path)

    async def aclose(self) -> None:
        pass


@pytest.mark.parametrize("call", ["makedirs", "listdir"])
def test_the_sideload_install_s_first_calls_give_up_on_a_silent_phone(monkeypatch, tmp_path, call) -> None:
    import pymobiledevice3.lockdown as pmd_lockdown
    import pymobiledevice3.services.afc as pmd_afc
    import pymobiledevice3.services.installation_proxy as pmd_ip

    afc = StagingAfc()
    afc.hang.add(call)

    class Lockdown:
        paired = True

        async def close(self):
            pass

    class Proxy:
        def __init__(self, lockdown):
            pass

        async def send_package(self, *a, **k):
            raise AssertionError("never reached")

        async def close(self):
            pass

    async def create_using_usbmux(serial=None, **_):
        return Lockdown()

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(pmd_ip, "InstallationProxyService", Proxy)
    monkeypatch.setattr(pmd_afc, "AfcService", lambda lockdown, **_: afc)
    _silent(device_install._install(_ipa(tmp_path), "u1", None, None))


def test_the_rename_of_the_new_file_into_place_gives_up_on_a_silent_phone(tmp_path) -> None:
    """The second rename alone: the old file moved aside, then silence."""
    ha = Sandbox({TARGET: b"the old build"})
    real = ha.rename

    async def rename(source, target):
        if source == lc_runner.partial_path(TARGET):
            await asyncio.Event().wait()
        await real(source, target)

    ha.rename = rename
    _silent(lc_runner._write_into_place(ha, _ipa(tmp_path), TARGET))
    assert ha.files[TARGET] == b"the old build", "put back"


@pytest.mark.parametrize("status", [None, 4, 8])
def test_not_found_is_said_by_its_class_or_its_status(status) -> None:
    assert lc_runner._not_found(AfcFileNotFoundError("gone", status, TARGET)) is True
    if status is not None:
        assert lc_runner._not_found(AfcException("gone", status, TARGET)) is True
    assert lc_runner._not_found(AfcException("refused", 10, TARGET)) is False
    assert lc_runner._not_found(ConnectionTerminatedError()) is False
