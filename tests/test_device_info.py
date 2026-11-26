from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from graft_ipa import device_info


def _lockdown(values: dict) -> AsyncMock:
    lock = AsyncMock()
    lock.get_value = AsyncMock(side_effect=lambda domain=None, key=None: values.get(domain))
    lock.close = AsyncMock()
    return lock


def test_reads_storage_and_battery() -> None:
    lock = _lockdown(
        {
            "com.apple.disk_usage": {
                "TotalDiskCapacity": 1024000000000,
                "AmountDataAvailable": 910693560320,
            },
            "com.apple.mobile.battery": {
                "BatteryCurrentCapacity": 74,
                "BatteryIsCharging": True,
            },
        }
    )
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info.total_bytes == 1024000000000
    assert info.free_bytes == 910693560320
    assert info.battery_percent == 74
    assert info.battery_charging is True
    lock.close.assert_awaited_once()


def test_each_half_is_independently_optional() -> None:
    """A locked phone answers some domains and not others.

    One refusal must not blank the whole strip — the pane renders "—" per cell,
    so partial data is more useful than none.
    """
    lock = _lockdown({"com.apple.disk_usage": {"TotalDiskCapacity": 1, "AmountDataAvailable": 1}})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info.total_bytes == 1
    assert info.battery_percent is None
    assert info.battery_charging is None


def test_an_unreachable_phone_yields_nothing_rather_than_raising() -> None:
    with patch.object(
        device_info, "create_using_usbmux", new=AsyncMock(side_effect=OSError("no device"))
    ):
        assert device_info.read_device_info("UDID") is None


def test_a_non_dict_domain_answer_is_ignored() -> None:
    """lockdown returns whatever the phone sends. A string where a dict was
    expected must read as "no data", not raise — same posture as artwork.py's
    guards, and for the same reason: this is a wire we did not write."""
    lock = _lockdown({"com.apple.disk_usage": "surprise", "com.apple.mobile.battery": None})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.total_bytes is None and info.battery_percent is None


def test_a_bool_is_not_an_int() -> None:
    """`isinstance(True, int)` is True in Python, which is what `_as_int`'s
    `and not isinstance(value, bool)` is for — and nothing tested it, so
    deleting that clause survived the whole suite.

    A disk capacity of `True` would be reported as the integer 1: a phone with
    one byte of storage, drawn by the pane as a full bar.
    """
    lock = _lockdown(
        {
            "com.apple.disk_usage": {"TotalDiskCapacity": True, "AmountDataAvailable": False},
            "com.apple.mobile.battery": {"BatteryCurrentCapacity": True},
        }
    )
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.total_bytes is None
    assert info.free_bytes is None
    assert info.battery_percent is None


def test_charging_is_a_bool_or_nothing_and_is_never_coerced() -> None:
    """`bool(battery["BatteryIsCharging"])` turned anything into a verdict, and
    `bool("NO")` is True.

    The `in` check it sat behind guarded a missing key but not a null value, so
    a phone that answered the domain and said nothing about charging was
    reported as "not charging" — which the pane draws exactly like a phone
    genuinely on battery. `_as_bool` mirrors `_as_int`'s posture instead: a real
    bool or None, and None is the honest third state.
    """
    for answer in ("NO", "YES", 1, 0, None, [], {}):
        lock = _lockdown({"com.apple.mobile.battery": {"BatteryIsCharging": answer}})
        with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
            info = device_info.read_device_info("UDID")
        assert info is not None
        assert info.battery_charging is None, f"{answer!r} is not a charging verdict"

    for answer in (True, False):
        lock = _lockdown({"com.apple.mobile.battery": {"BatteryIsCharging": answer}})
        with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
            info = device_info.read_device_info("UDID")
        assert info is not None
        assert info.battery_charging is answer


def test_a_missing_charging_key_is_still_none() -> None:
    """The case the old `"BatteryIsCharging" in battery` guard did cover, kept
    covered by its replacement."""
    lock = _lockdown({"com.apple.mobile.battery": {"BatteryCurrentCapacity": 50}})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.battery_percent == 50
    assert info.battery_charging is None


def test_a_domain_that_raises_does_not_take_the_other_down() -> None:
    """`get_value` can raise per-domain on a busy or locked phone."""
    lock = AsyncMock()

    async def flaky(domain=None, key=None):
        if domain == "com.apple.disk_usage":
            raise RuntimeError("busy")
        return {"BatteryCurrentCapacity": 50, "BatteryIsCharging": False}

    lock.get_value = AsyncMock(side_effect=flaky)
    lock.close = AsyncMock()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.total_bytes is None
    assert info.battery_percent == 50
    lock.close.assert_awaited_once()


def test_a_close_that_fails_does_not_discard_the_reading() -> None:
    """The record is complete before the close runs; the close must not undo it.

    Left bare, `finally: await lockdown.close()` replaced the pending return
    with its own exception, `read_device_info`'s broad `except` mapped that to
    None, and the CLI emitted `reachable: false` for a phone whose storage and
    battery it had just read — the stat strip blanked on a phone that had
    answered every domain. Only an unreachable *device* is allowed to produce
    None here; that is the distinction `read_device_info`'s docstring draws.
    """
    lock = _lockdown(
        {
            "com.apple.disk_usage": {"TotalDiskCapacity": 512, "AmountDataAvailable": 256},
            "com.apple.mobile.battery": {"BatteryCurrentCapacity": 81, "BatteryIsCharging": False},
        }
    )
    lock.close = AsyncMock(side_effect=ConnectionResetError("phone went away at teardown"))
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None, "a failed teardown is not an unreachable phone"
    assert info.total_bytes == 512
    assert info.battery_percent == 81
    lock.close.assert_awaited_once()


# --- Developer Mode (spec §5) ---------------------------------------------------

AMFI = ("com.apple.security.mac.amfi", "DeveloperModeStatus")


def _phone(product_version: object, developer_mode: object = True) -> AsyncMock:
    """A lockdown session that answers the amfi key by key, as the phone does,
    and a battery reading so a test can see the rest survive."""
    lock = AsyncMock()
    lock.product_version = product_version

    async def get_value(domain=None, key=None):
        if (domain, key) == AMFI:
            return developer_mode
        if domain == "com.apple.mobile.battery":
            return {"BatteryCurrentCapacity": 64, "BatteryIsCharging": False}
        return {}

    lock.get_value = AsyncMock(side_effect=get_value)
    lock.close = AsyncMock()
    return lock


def _asked_amfi(lock: AsyncMock) -> bool:
    return any((c.kwargs.get("domain"), c.kwargs.get("key")) == AMFI for c in lock.get_value.await_args_list)


@pytest.mark.parametrize("answer", [True, False])
def test_developer_mode_is_read_on_ios_16_and_later(answer: bool) -> None:
    lock = _phone("17.5.1", answer)
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.developer_mode is answer
    assert info.battery_percent == 64


@pytest.mark.parametrize("version", ["15.7.9", "1.0"])
def test_before_ios_16_there_is_nothing_to_read(version: str) -> None:
    """`1.0` is what pymobiledevice3's `product_version` says when the phone
    gave none."""
    lock = _phone(version, True)
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None and info.developer_mode is None
    assert not _asked_amfi(lock), "an iOS 15 phone has no Developer Mode to ask about"


@pytest.mark.parametrize("version", ["", "unknown", "١٧.0", None, 17])
def test_a_version_graft_cannot_read_has_no_developer_mode(version: object) -> None:
    lock = _phone(version, True)
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None and info.developer_mode is None


@pytest.mark.parametrize("answer", ["YES", 1, 0, None, {}, []])
def test_an_answer_that_is_not_a_bool_is_no_answer(answer: object) -> None:
    """`bool("NO")` is True, which is why pymobiledevice3's own
    `get_developer_mode_status` isn't used: a verdict made from anything
    could block a build over a value nobody understood."""
    lock = _phone("26.1", answer)
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None and info.developer_mode is None


def test_a_refused_developer_mode_read_keeps_the_rest() -> None:
    lock = _phone("26.1")

    async def get_value(domain=None, key=None):
        if domain == "com.apple.security.mac.amfi":
            raise RuntimeError("locked")
        return {"BatteryCurrentCapacity": 64} if domain == "com.apple.mobile.battery" else {}

    lock.get_value = AsyncMock(side_effect=get_value)
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.developer_mode is None
    assert info.battery_percent == 64


def test_a_developer_mode_read_that_never_answers_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_info, "DEVELOPER_MODE_SECONDS", 0.05)
    lock = _phone("26.1")

    async def get_value(domain=None, key=None):
        if domain == "com.apple.security.mac.amfi":
            await asyncio.Event().wait()
        return {"BatteryCurrentCapacity": 64} if domain == "com.apple.mobile.battery" else {}

    lock.get_value = AsyncMock(side_effect=get_value)
    started = time.monotonic()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")
    assert info is not None
    assert info.developer_mode is None
    assert info.battery_percent == 64
    assert time.monotonic() - started < 5


def test_the_live_read_opens_its_own_session_without_pairing() -> None:
    lock = _phone("26.1", False)
    create = AsyncMock(return_value=lock)
    with patch.object(device_info, "create_using_usbmux", new=create):
        assert device_info.read_developer_mode("UDID") is False
    create.assert_awaited_once_with(serial="UDID", autopair=False)
    lock.close.assert_awaited_once()


def test_the_live_read_of_an_unreachable_phone_is_no_answer() -> None:
    with patch.object(
        device_info, "create_using_usbmux", new=AsyncMock(side_effect=OSError("no device"))
    ):
        assert device_info.read_developer_mode("UDID") is None


def test_the_live_read_is_bounded_as_a_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Wi-Fi phone asleep with its connection half-open holds the
    handshake itself, before any key is asked."""
    monkeypatch.setattr(device_info, "DEVELOPER_MODE_SECONDS", 0.05)

    async def never(**_):
        await asyncio.Event().wait()

    started = time.monotonic()
    with patch.object(device_info, "create_using_usbmux", new=never):
        assert device_info.read_developer_mode("UDID") is None
    assert time.monotonic() - started < 5


def test_a_close_that_fails_keeps_the_live_answer() -> None:
    lock = _phone("26.1", True)
    lock.close = AsyncMock(side_effect=ConnectionResetError("gone at teardown"))
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        assert device_info.read_developer_mode("UDID") is True


# --- Revealing the switch (spec §5) ----------------------------------------------
#
# iOS hides the Developer Mode switch until a development app is installed or
# Xcode connects. `check_developer_mode(reveal=True)`, a real build's, asks
# the phone to show it when the answer is False, in the same session. Nothing
# else does: not `read_developer_mode` (readiness), not `read_device_info`.


def _amfi(fails: BaseException | None = None, hangs: bool = False):
    """A stand-in for pymobiledevice3's `AmfiService`: records the session
    each reveal was sent on."""
    revealed: list[object] = []

    class Amfi:
        def __init__(self, lockdown: object) -> None:
            self._lockdown = lockdown

        async def reveal_developer_mode_option_in_ui(self) -> None:
            revealed.append(self._lockdown)
            if hangs:
                await asyncio.Event().wait()
            if fails is not None:
                raise fails

    return Amfi, revealed


def test_off_with_reveal_asks_the_phone_to_show_the_switch_in_the_same_session() -> None:
    lock = _phone("26.1", False)
    amfi, revealed = _amfi()
    create = AsyncMock(return_value=lock)
    with patch.object(device_info, "create_using_usbmux", new=create), \
         patch.object(device_info, "AmfiService", new=amfi):
        check = device_info.check_developer_mode("UDID", reveal=True)

    assert check.on is False
    assert check.revealed is True
    assert check.reveal_error is None
    assert revealed == [lock], "one reveal, on the session that read it"
    create.assert_awaited_once_with(serial="UDID", autopair=False)
    lock.close.assert_awaited_once()


@pytest.mark.parametrize(("version", "answer"), [
    ("26.1", True),
    ("26.1", "NO"),  # an answer nobody understood: None
    ("15.7.9", False),  # before iOS 16: None, and there's no switch to show
])
def test_on_or_unknown_reveals_nothing(version: str, answer: object) -> None:
    amfi, revealed = _amfi()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=_phone(version, answer))), \
         patch.object(device_info, "AmfiService", new=amfi):
        check = device_info.check_developer_mode("UDID", reveal=True)

    assert check.on is not False
    assert check.revealed is False
    assert revealed == []


def test_off_without_reveal_reveals_nothing() -> None:
    amfi, revealed = _amfi()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=_phone("26.1", False))), \
         patch.object(device_info, "AmfiService", new=amfi):
        check = device_info.check_developer_mode("UDID", reveal=False)

    assert check.on is False and check.revealed is False
    assert revealed == []


def test_neither_readiness_nor_the_device_check_ever_reveals() -> None:
    """`read_developer_mode` is `--update`'s readiness (and its quick plan's
    live read); `read_device_info` is `--device-info`. Off, both just say so."""
    amfi, revealed = _amfi()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=_phone("26.1", False))), \
         patch.object(device_info, "AmfiService", new=amfi):
        assert device_info.read_developer_mode("UDID") is False
        info = device_info.read_device_info("UDID")

    assert info is not None and info.developer_mode is False
    assert revealed == []


@pytest.mark.parametrize("trouble", ["fails", "hangs"])
def test_a_reveal_that_fails_keeps_the_reading(
    trouble: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(device_info, "DEVELOPER_MODE_SECONDS", 0.05)
    amfi, revealed = _amfi(
        fails=RuntimeError("The device did not reveal the Developer Mode setting")
        if trouble == "fails" else None,
        hangs=trouble == "hangs",
    )
    started = time.monotonic()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=_phone("26.1", False))), \
         patch.object(device_info, "AmfiService", new=amfi):
        check = device_info.check_developer_mode("UDID", reveal=True)

    assert check.on is False, "a reveal never costs the reading"
    assert check.revealed is False
    assert check.reveal_error
    assert revealed, "premise: it was tried"
    assert time.monotonic() - started < 5


def test_the_check_names_the_device_as_the_listing_does() -> None:
    """`devices.name_from_lockdown` without the UDID: the model's name ("iPad
    Air"), else the name the device gives itself, else its family ("iPad"),
    else none. And for the hint, the model's name or the family alone."""
    def named(display_name: object, product_type: object, values: object) -> tuple:
        lock = _phone("26.1", True)
        lock.display_name = display_name
        lock.product_type = product_type
        lock.all_values = values
        with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
            check = device_info.check_developer_mode("UDID", reveal=False)
        return check.name, check.model_name

    own = {"DeviceClass": "iPad", "DeviceName": "Sample's iPad"}
    assert named("iPad Air", "iPad13,16", own) == ("iPad Air", "iPad Air")
    assert named(None, "iPad19,1", own) == ("Sample's iPad", "iPad"), "the hint's is never its own"
    assert named(None, "iPad19,1", {"DeviceClass": "iPad"}) == ("iPad", "iPad")
    assert named(None, "Watch7,1", {"DeviceClass": "Watch"}) == (None, None)
    assert named(None, None, None) == (None, None)


def test_an_unreachable_check_is_no_answer_and_reveals_nothing() -> None:
    amfi, revealed = _amfi()
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(side_effect=OSError("no device"))), \
         patch.object(device_info, "AmfiService", new=amfi):
        check = device_info.check_developer_mode("UDID", reveal=True)

    assert check.on is None and check.revealed is False
    assert revealed == []


def test_the_session_says_what_the_device_is_called() -> None:
    """The same name `--devices` shows (pymobiledevice3's `display_name`,
    from the product type), for `devices-seen.json` (spec §5b)."""
    lock = _lockdown({})
    lock.display_name = "iPad Air"
    lock.product_type = "iPad13,16"
    lock.all_values = {"ProductVersion": "26.1"}
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")

    assert (info.name, info.model, info.ios_version) == ("iPad Air", "iPad13,16", "26.1")


def test_a_session_that_says_nothing_names_nothing() -> None:
    lock = _lockdown({})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lock)):
        info = device_info.read_device_info("UDID")

    assert (info.name, info.model, info.ios_version) == (None, None, None)
