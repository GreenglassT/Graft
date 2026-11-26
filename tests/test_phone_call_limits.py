"""The calls outside the push and the install that waited on a silent phone
with no limit (the try14 delta audit's d14-cli-3).

pymobiledevice3 bounds the dial, the TLS handshake and the closes, but not
the lockdown exchange, an app lookup or an AFC call. A Wi-Fi-synced phone
asleep with the connection half-open then held `--lc-install` (its first
step lists the apps), `--lc-clean`, `--lc-status` and the certificate push,
saying nothing, until Stop. Each of those calls now has
`device_install._answered`'s silence limit, cut to `SILENCE` here.
"""

from __future__ import annotations

import asyncio
import time

import pymobiledevice3.lockdown as pmd_lockdown
import pymobiledevice3.services.house_arrest as pmd_house_arrest
import pymobiledevice3.services.installation_proxy as pmd_ip
import pytest

from graft_ipa import device_install, device_session, lc_discovery, lc_manage
from graft_ipa.installer import connection_hint
from graft_ipa.lc_runner import LiveContainerError

SILENCE = 0.2
#: How long past the limit a test allows for the rest of the run.
SLACK = 2.0
UDID = "00000000-0000000000000000"


@pytest.fixture(autouse=True)
def _short_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_install, "SILENCE_SECONDS", SILENCE)


async def _never(*_args, **_kwargs):
    await asyncio.Event().wait()


class Lockdown:
    """A lockdown session that answers, paired or not."""

    def __init__(self, *, paired: bool = True) -> None:
        self.paired = paired
        self.closed = False

    async def pair(self, timeout=None, private_key=None) -> None:
        self.paired = True

    async def validate_pairing(self) -> bool:
        return True

    async def close(self) -> None:
        self.closed = True


def _bounded(call) -> BaseException:
    """Run `call`, which must fail, and say what it raised; never past the
    silence limit by much."""
    started = time.monotonic()
    with pytest.raises(BaseException) as raised:
        call()
    assert time.monotonic() - started < SILENCE + SLACK
    return raised.value


def test_a_silent_lockdown_handshake_is_given_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", _never)
    raised = _bounded(lambda: device_session.run(device_session.open_lockdown(UDID)))
    assert isinstance(raised, device_install.PhoneWentSilentError)
    assert "when Graft connected to it" in str(raised)


def test_a_silent_pairing_check_is_given_up_on_and_the_session_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """After the Trust tap (`pair`, bounded on its own), the check that the
    pairing took."""
    lock = Lockdown(paired=False)
    lock.validate_pairing = _never

    async def create(serial=None, **_):
        return lock

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create)
    raised = _bounded(lambda: device_session.run(device_session.open_lockdown(UDID, on_trust_prompt=None)))
    assert isinstance(raised, device_install.PhoneWentSilentError)
    assert lock.closed


def test_the_premise_a_phone_that_answers_opens(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = Lockdown(paired=False)

    async def create(serial=None, **_):
        return lock

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create)
    assert device_session.run(device_session.open_lockdown(UDID, on_trust_prompt=None)) is lock


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> Lockdown:
    """`open_lockdown` answers at once, so what is timed is the call after it."""
    lock = Lockdown()

    async def open_lockdown(udid, **_):
        return lock

    monkeypatch.setattr(device_session, "open_lockdown", open_lockdown)
    return lock


def _silent_device_error(call) -> LiveContainerError:
    raised = _bounded(call)
    assert isinstance(raised, LiveContainerError), repr(raised)
    assert raised.hint == connection_hint("PhoneWentSilentError"), raised.hint
    return raised


@pytest.mark.parametrize("silent", ["first", "second"])
def test_the_app_listing_gives_up_on_a_silent_phone(
    monkeypatch: pytest.MonkeyPatch, opened: Lockdown, silent: str
) -> None:
    """`--lc-install`'s first step: two lookups, the second only when a
    LiveContainer is installed."""
    class Proxy:
        def __init__(self, lockdown):
            self.asked = 0

        async def lookup(self, options):
            self.asked += 1
            if silent == "first" or self.asked == 2:
                await asyncio.Event().wait()
            return {"com.kdt.livecontainer": {"ApplicationType": "User"}}

        async def close(self):
            pass

    monkeypatch.setattr(pmd_ip, "InstallationProxyService", Proxy)
    monkeypatch.setattr(lc_discovery, "livecontainers", lambda apps: ["com.kdt.livecontainer"])
    error = _silent_device_error(lambda: lc_manage.UsbmuxDevice().installed_apps(UDID))
    assert "when Graft listed the apps on it" in str(error)
    assert opened.closed


def test_the_bundle_id_listing_gives_up_on_a_silent_phone(
    monkeypatch: pytest.MonkeyPatch, opened: Lockdown
) -> None:
    monkeypatch.setattr(lc_discovery, "installed_bundle_ids", _never)
    _silent_device_error(lambda: lc_manage.UsbmuxDevice().installed_bundle_ids(UDID))


class HouseArrest:
    """LiveContainer's sandbox over AFC, silent at `hang`."""

    hang = ""

    @classmethod
    async def create(cls, lockdown, bundle_id):
        if cls.hang == "create":
            await asyncio.Event().wait()
        return cls()

    async def _maybe_hang(self, name):
        if HouseArrest.hang == name:
            await asyncio.Event().wait()

    async def listdir(self, folder):
        await self._maybe_hang("listdir")
        return []

    async def get_file_contents(self, path):
        await self._maybe_hang("get_file_contents")
        return b""

    # The certificate push writes as an IPA push does
    # (`lc_runner._write_into_place`): a partial file, then a rename.
    async def fopen(self, path, mode="r"):
        await self._maybe_hang("fopen")
        return 1

    async def fwrite(self, handle, data):
        await self._maybe_hang("fwrite")

    async def fclose(self, handle):
        pass

    async def exists(self, path):
        return False

    async def rename(self, source, target):
        await self._maybe_hang("rename")

    async def rm_single(self, path, force=False):
        return True

    async def rm(self, path):
        await self._maybe_hang("rm")

    async def close(self):
        pass


@pytest.mark.parametrize(("hang", "call"), [
    ("create", lambda d: d.list_documents(UDID, "com.kdt.livecontainer")),
    ("listdir", lambda d: d.list_documents(UDID, "com.kdt.livecontainer")),
    ("get_file_contents", lambda d: d.read_document(UDID, "com.kdt.livecontainer", "cert.p12")),
    ("fopen", lambda d: d.push_document(UDID, "com.kdt.livecontainer", "cert.p12", b"x")),
    ("rename", lambda d: d.push_document(UDID, "com.kdt.livecontainer", "cert.p12", b"x")),
    ("rm", lambda d: d.remove_document(UDID, "com.kdt.livecontainer", "cert.p12")),
])
def test_livecontainer_s_documents_give_up_on_a_silent_phone(
    monkeypatch: pytest.MonkeyPatch, opened: Lockdown, hang: str, call
) -> None:
    """`--lc-clean`, `--lc-status` and the certificate push and check."""
    monkeypatch.setattr(HouseArrest, "hang", hang)
    monkeypatch.setattr(pmd_house_arrest, "HouseArrestService", HouseArrest)
    _silent_device_error(lambda: call(lc_manage.UsbmuxDevice()))
    assert opened.closed


def test_the_premise_documents_that_answer_are_read(monkeypatch: pytest.MonkeyPatch, opened: Lockdown) -> None:
    monkeypatch.setattr(HouseArrest, "hang", "")
    monkeypatch.setattr(pmd_house_arrest, "HouseArrestService", HouseArrest)
    assert lc_manage.UsbmuxDevice().list_documents(UDID, "com.kdt.livecontainer") == []
