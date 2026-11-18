from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
