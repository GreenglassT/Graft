"""A phone that has never trusted this Mac (review, critic's gap 1).

For a first-time user this is the most common state. pymobiledevice3's
`create_using_usbmux` pairs by itself and, by default, waits for the Trust
dialog with no limit and no word: its 'waiting user pairing dialog...' is an
INFO record nobody shows. In 1.1, `ideviceinstaller` failed at once. So graft
opens the session without that (`device_session.open_lockdown`), says what to
do, waits `PAIR_TIMEOUT_SECONDS`, and then fails with the Trust hint.

The fakes model pymobiledevice3's own pairing: `pair(timeout=None)` waits for
ever (here: until the test's patience runs out), `pair(timeout=t)` gives up
after `t` with `PairingDialogResponsePendingError`.
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar

import pymobiledevice3.lockdown as pmd_lockdown
import pymobiledevice3.services.afc as pmd_afc
import pymobiledevice3.services.installation_proxy as pmd_ip
import pytest
from pymobiledevice3.exceptions import (
    PairingDialogResponsePendingError,
    PasswordRequiredError,
    UserDeniedPairingError,
)

from graft_ipa import (
    artwork,
    device_apps,
    device_info,
    device_session,
    devices,
    installer,
    lc_manage,
    lc_runner,
)
from graft_ipa.errors import InstallError
from graft_ipa.lc_runner import LiveContainerError

#: How long a fake "for ever" lasts: far past the bounded wait under test.
FOREVER = 5.0
#: The bounded wait under test, cut down.
PAIR_TIMEOUT = 0.2
#: What graft really waits, read before any test patches it.
REAL_PAIR_TIMEOUT = device_session.PAIR_TIMEOUT_SECONDS


class NeverTrusted:
    """A lockdown session with a phone that has no pairing with this Mac.

    `answer` is what the user does with the Trust dialog: None ignores it,
    "trust" taps Trust, "deny" taps Don't Trust, "locked" is a locked phone.
    """

    answer: ClassVar[str | None] = None
    made: ClassVar[list[NeverTrusted]] = []

    def __init__(self, serial, autopair):
        self.serial, self.autopair = serial, autopair
        self.paired = False
        self.pair_timeouts: list[float | None] = []
        self.closed = False
        NeverTrusted.made.append(self)

    async def pair(self, timeout=None, private_key=None):
        self.pair_timeouts.append(timeout)
        if NeverTrusted.answer == "locked":
            raise PasswordRequiredError()
        if NeverTrusted.answer == "deny":
            raise UserDeniedPairingError()
        if NeverTrusted.answer == "trust":
            self.paired = True
            return
        start = time.monotonic()
        while time.monotonic() - start <= (FOREVER if timeout is None else timeout):
            await asyncio.sleep(0.01)
        raise PairingDialogResponsePendingError()

    async def validate_pairing(self):
        return self.paired

    async def close(self):
        self.closed = True


@pytest.fixture
def untrusted(monkeypatch):
    NeverTrusted.answer = None
    NeverTrusted.made.clear()
    calls: list[dict] = []

    async def create_using_usbmux(*, serial=None, autopair=True, **kwargs):
        calls.append({"serial": serial, "autopair": autopair, **kwargs})
        if autopair:
            # pymobiledevice3's own pairing, in silence: with no
            # `pair_timeout` it waits for ever; with one, it gives up.
            if kwargs.get("pair_timeout") is None:
                await asyncio.sleep(FOREVER)
            raise PairingDialogResponsePendingError()
        return NeverTrusted(serial, autopair)

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(device_info, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(device_session, "PAIR_TIMEOUT_SECONDS", PAIR_TIMEOUT)
    return calls


@pytest.fixture
def prompts(capsys):
    """How many times the user was told to tap Trust, on stderr (the default
    `on_trust_prompt`), so far."""
    return lambda: capsys.readouterr().err.count(device_session.TRUST_PROMPT)


def _ipa(tmp_path):
    ipa = tmp_path / "app.ipa"
    ipa.write_bytes(b"PK")
    return ipa


# --- the sideload install --------------------------------------------------------


def test_an_unanswered_trust_prompt_fails_in_bounded_time_with_the_trust_hint(tmp_path, untrusted):
    start = time.monotonic()
    with pytest.raises(InstallError) as e:
        installer.install(_ipa(tmp_path), udid="u1")
    assert time.monotonic() - start < FOREVER / 2
    assert "Trust This Computer" in e.value.hint
    [lock] = NeverTrusted.made
    assert lock.pair_timeouts == [PAIR_TIMEOUT]
    assert lock.closed


def test_the_user_is_told_to_tap_trust_before_graft_waits(tmp_path, untrusted, capsys):
    with pytest.raises(InstallError):
        installer.install(_ipa(tmp_path), udid="u1")
    err = capsys.readouterr().err
    assert device_session.TRUST_PROMPT in err


def test_the_prompt_comes_before_the_wait_and_only_once(tmp_path, untrusted, monkeypatch):
    order: list[str] = []
    real_pair = NeverTrusted.pair

    async def pair(self, timeout=None, private_key=None):
        order.append("pair")
        return await real_pair(self, timeout, private_key)

    monkeypatch.setattr(NeverTrusted, "pair", pair)
    from graft_ipa import device_install

    with pytest.raises(device_install.DeviceInstallError):
        device_install.install_ipa(
            _ipa(tmp_path), udid="u1", on_trust_prompt=lambda: order.append("told"),
        )
    assert order == ["told", "pair"]


def test_a_tap_on_trust_goes_on_to_install(tmp_path, untrusted, monkeypatch):
    NeverTrusted.answer = "trust"
    sent: list[str] = []

    class Proxy:
        def __init__(self, lockdown):
            pass

        async def send_package(self, cmd, options, handler, path, *args):
            sent.append(cmd)

        async def close(self):
            pass

    class Afc:
        def __init__(self, lockdown, service_name=None):
            pass

        async def makedirs(self, path): pass
        async def listdir(self, path): return []
        async def fopen(self, path, mode="r"): return 1
        async def fwrite(self, handle, data, chunk_size=0): pass
        async def fclose(self, handle): pass
        async def rm_single(self, path, force=False): pass
        async def aclose(self): pass

    monkeypatch.setattr(pmd_ip, "InstallationProxyService", Proxy)
    monkeypatch.setattr(pmd_afc, "AfcService", Afc)
    installer.install(_ipa(tmp_path), udid="u1")
    assert sent == ["Install"]


def test_a_phone_that_already_trusts_this_mac_is_not_asked(tmp_path, untrusted, prompts, monkeypatch):
    monkeypatch.setattr(NeverTrusted, "__init__", _paired_init)
    with pytest.raises(InstallError):  # this test's fakes go no further
        installer.install(_ipa(tmp_path), udid="u1")
    assert prompts() == 0
    assert NeverTrusted.made[0].pair_timeouts == []


def _paired_init(self, serial, autopair):
    self.serial, self.autopair, self.paired = serial, autopair, True
    self.pair_timeouts, self.closed = [], False
    NeverTrusted.made.append(self)


@pytest.mark.parametrize("answer, phrase", [
    ("deny", "unplug it and plug it back in"),
    ("locked", "unlock it"),
])
def test_a_declined_or_locked_prompt_says_what_to_do(tmp_path, untrusted, answer, phrase):
    NeverTrusted.answer = answer
    with pytest.raises(InstallError) as e:
        installer.install(_ipa(tmp_path), udid="u1")
    assert phrase in e.value.hint


@pytest.mark.parametrize("output, phrase", [
    # Not a substring match on "PairingError": that is not in this name.
    ("PairingDialogResponsePendingError: ", "Trust This Computer"),
    ("UserDeniedPairingError: The user refused to trust this computer.", "plug it back in"),
    ("FatalPairingError: ", "tap Trust"),
])
def test_each_pairing_failure_has_its_own_hint(output, phrase):
    assert phrase in installer._install_hint(output)


# --- LiveContainer: the push, and --lc-install's sessions ----------------------------


def test_a_livecontainer_push_waits_bounded_and_says_tap_trust(tmp_path, untrusted, prompts):
    with pytest.raises(LiveContainerError) as e:
        lc_runner.push_ipa_to_livecontainer(_ipa(tmp_path), "u1")
    assert "Trust This Computer" in e.value.hint
    assert prompts() == 1
    assert NeverTrusted.made[0].pair_timeouts == [PAIR_TIMEOUT]


def test_lc_installs_first_session_waits_bounded_and_says_tap_trust(untrusted, prompts):
    with pytest.raises(LiveContainerError) as e:
        lc_manage.UsbmuxDevice().installed_apps("u1")
    assert "Trust This Computer" in e.value.hint
    assert prompts() == 1


@pytest.mark.parametrize("call", [
    lambda: lc_runner.list_livecontainer_documents(udid="u1", lc_bundle_id="com.example.lc"),
    lambda: lc_manage.UsbmuxDevice().list_documents("u1", "com.example.lc"),
    lambda: lc_runner.check_push_target(
        "u1", inner_filename="x.ipa", lc_bundle_id=None, team=None, free_account=True, ours={},
    ),
], ids=["lc-documents", "lc-manage-sandbox", "early-check"])
def test_every_livecontainer_session_pairs_bounded(untrusted, prompts, call):
    try:
        call()
    except Exception:  # how each caller words it is tested elsewhere
        pass
    assert [c["autopair"] for c in untrusted] == [False]
    assert NeverTrusted.made[0].pair_timeouts == [PAIR_TIMEOUT]


# --- read-only sessions: no prompt to give, but no wait for ever -------------------


@pytest.mark.parametrize("call", [
    lambda: devices.list_devices(),
    lambda: device_info.read_device_info("u1"),
    lambda: device_apps.list_installed_apps("u1"),
    lambda: artwork.icon_from_device("com.example.app", udid="u1"),
], ids=["devices", "device-info", "device-apps", "artwork"])
def test_read_only_sessions_give_up_on_an_unanswered_prompt(untrusted, call, monkeypatch):
    class Mux:
        serial = "u1"
        connection_type = "USB"

    async def mux_list(*a, **k):
        return [Mux()]

    import pymobiledevice3.usbmux as pmd_usbmux

    monkeypatch.setattr(pmd_usbmux, "list_devices", mux_list)
    start = time.monotonic()
    try:
        call()
    except Exception:  # whether each raises is its own contract
        pass
    assert time.monotonic() - start < FOREVER / 2
    assert [c.get("pair_timeout") for c in untrusted] == [REAL_PAIR_TIMEOUT]
