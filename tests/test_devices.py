"""A device that fails to describe must be reported, not dropped.

`asyncio.gather(return_exceptions=True)` followed by an `isinstance(r, DeviceInfo)`
filter discarded any paired phone whose lockdown call raised. `list_devices()`
returned fewer devices than were attached, the Devices tab rendered "No devices
connected" and the build-options picker came up empty — while the `idevice_id`
paths in pre-flight and `select_device` still saw the phone. With no diagnostic
anywhere, the visible conclusion was a bad cable or a lost trust.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graft_ipa import devices as devices_mod
from graft_ipa.devices import BriefDevice, DeviceInfo


class _FakeMuxDevice:
    def __init__(self, serial: str, connection_type: str = "USB") -> None:
        self.serial = serial
        self.connection_type = connection_type


def _info(udid: str) -> DeviceInfo:
    return DeviceInfo(
        udid=udid,
        name="iPhone",
        model="iPhone14,2",
        ios_version="26.0.1",
        connection_type="USB",
        livecontainer_bundle_id=None,
    )


def _run(mux_devices, describe):
    async def _fake_mux_list():
        return mux_devices

    async def _fake_describe(_mux_dev, udid):
        return describe(udid)

    with patch("pymobiledevice3.usbmux.list_devices", _fake_mux_list), \
         patch.object(devices_mod, "_describe_device", _fake_describe):
        return asyncio.run(devices_mod._list_devices_async())


def test_a_device_that_raises_while_being_described_is_reported() -> None:
    def describe(udid: str) -> DeviceInfo:
        if udid == "00008130-BAD":
            raise ConnectionRefusedError("lockdown refused")
        return _info(udid)

    listing = _run(
        [_FakeMuxDevice("00008130-OK"), _FakeMuxDevice("00008130-BAD")], describe
    )

    assert [d.udid for d in listing.devices] == ["00008130-OK"]
    assert len(listing.failures) == 1
    assert listing.failures[0].udid == "00008130-BAD"
    assert "lockdown refused" in listing.failures[0].error
    assert "ConnectionRefusedError" in listing.failures[0].error


def test_the_only_connected_device_failing_is_not_the_same_as_no_devices() -> None:
    """The case that reads as an unplugged cable. It must not be silent."""

    def describe(_udid: str) -> DeviceInfo:
        raise OSError("device not paired")

    listing = _run([_FakeMuxDevice("00008130-BAD")], describe)

    assert listing.devices == []
    assert [f.udid for f in listing.failures] == ["00008130-BAD"]


def test_failures_are_attributed_to_the_right_udid() -> None:
    """gather() results are positional; a mis-zip would blame the wrong phone."""

    def describe(udid: str) -> DeviceInfo:
        if udid in ("00008130-B", "00008130-D"):
            raise RuntimeError(f"boom {udid}")
        return _info(udid)

    listing = _run(
        [_FakeMuxDevice(u) for u in ("00008130-A", "00008130-B", "00008130-C", "00008130-D")],
        describe,
    )

    assert [d.udid for d in listing.devices] == ["00008130-A", "00008130-C"]
    assert [f.udid for f in listing.failures] == ["00008130-B", "00008130-D"]
    assert "boom 00008130-B" in listing.failures[0].error
    assert "boom 00008130-D" in listing.failures[1].error


def test_all_healthy_devices_report_no_failures() -> None:
    listing = _run([_FakeMuxDevice("00008130-A"), _FakeMuxDevice("00008130-B")], _info)

    assert [d.udid for d in listing.devices] == ["00008130-A", "00008130-B"]
    assert listing.failures == []


def test_duplicate_udids_are_still_collapsed() -> None:
    """Dedupe happens before description; both halves must stay paired."""
    listing = _run(
        [_FakeMuxDevice("00008130-A"), _FakeMuxDevice("00008130-A")], _info
    )

    assert [d.udid for d in listing.devices] == ["00008130-A"]
    assert listing.failures == []


def test_duplicate_udids_are_still_collapsed_in_the_brief_listing() -> None:
    """The same coalescing `_list_devices_async` needs, and for the same
    reason: usbmuxd can enumerate one physical phone as two distinct entries —
    a USB entry and a Network entry — carrying the same serial. `--brief`
    never describes a device, so its dedupe cannot lean on `_describe_device`
    catching this the way `list_devices()` incidentally would if it didn't
    dedupe first; the loop in `_list_devices_brief_async` is the only place
    left to collapse them. Without it, the GUI's poll would report the wrong
    *count* even though the UDID *set* it compares against the previous poll
    would still, by luck, be right — a count-only regression a change-detector
    would never trip over.

    Patches `pymobiledevice3.usbmux.list_devices` at its source module, not
    `graft_ipa.devices.list_devices`: the brief path's import is
    function-local (`from pymobiledevice3.usbmux import list_devices as
    mux_list` inside `_list_devices_brief_async`), so there is no such name at
    `graft_ipa.devices` module scope for `patch.object` to find.
    """

    async def _fake_mux_list():
        return [
            _FakeMuxDevice("00008130-A", connection_type="USB"),
            _FakeMuxDevice("00008130-A", connection_type="Network"),
        ]

    with patch("pymobiledevice3.usbmux.list_devices", _fake_mux_list):
        devices = asyncio.run(devices_mod._list_devices_brief_async())

    # The whole record, not `[d.udid for d in devices]`. Asserting the udids
    # alone threw away the very field these two entries differ in, and left the
    # `"Network" if str(conn).lower().startswith("network") else "USB"` mapping
    # with no coverage anywhere — every `--brief` CLI test hands `_do_devices_
    # brief` pre-built `BriefDevice`s, so nothing else ever runs it. Swift's
    # `ConnectionType` is a non-optional enum accepting exactly `"USB"` and
    # `"Network"`, so a typo there is not a wrong label: the whole event fails
    # to decode and the phone vanishes from the list.
    assert devices == [BriefDevice(udid="00008130-A", connection_type="USB")]


def test_the_brief_listing_normalises_the_connection_type_to_the_two_swift_accepts() -> None:
    """Both halves of the mapping, since the dedupe test above can only reach one.

    `connection_type` comes off a usbmux entry as whatever pymobiledevice3 puts
    there, and lands in a `device-brief` event that Swift decodes into a
    non-optional `ConnectionType` enum with exactly two cases. Anything but
    `"USB"` or `"Network"` fails the whole record's decode, so this is the one
    place the vocabulary is fixed — and it was previously exercised by nothing
    at all.
    """

    async def _fake_mux_list():
        return [
            _FakeMuxDevice("00008130-A", connection_type="USB"),
            _FakeMuxDevice("00008130-B", connection_type="Network"),
            # Differently cased, which is why the mapping goes through
            # `str(conn).lower().startswith(...)` rather than comparing to a
            # literal: this field is whatever pymobiledevice3 put there, and the
            # `str()` is there because it is not always a string to begin with.
            _FakeMuxDevice("00008130-C", connection_type="NETWORK"),
        ]

    with patch("pymobiledevice3.usbmux.list_devices", _fake_mux_list):
        devices = asyncio.run(devices_mod._list_devices_brief_async())

    assert devices == [
        BriefDevice(udid="00008130-A", connection_type="USB"),
        BriefDevice(udid="00008130-B", connection_type="Network"),
        BriefDevice(udid="00008130-C", connection_type="Network"),
    ]


def test_a_lockdown_failure_inside_describe_propagates_rather_than_returning_none() -> None:
    """The real `_describe_device`, not a stub.

    It used to swallow a `create_using_usbmux` failure and return None, which
    the caller could not tell apart from "not a device". If that guard came
    back, this device would be dropped with no failure recorded.
    """

    async def _fake_mux_list():
        return [_FakeMuxDevice("00008130-BAD")]

    async def _boom(**_kwargs):
        raise ConnectionRefusedError("lockdown refused")

    with patch("pymobiledevice3.usbmux.list_devices", _fake_mux_list), \
         patch("pymobiledevice3.lockdown.create_using_usbmux", _boom):
        listing = asyncio.run(devices_mod._list_devices_async())

    assert listing.devices == []
    assert [f.udid for f in listing.failures] == ["00008130-BAD"]
    assert "lockdown refused" in listing.failures[0].error


@contextmanager
def _a_phone_that_describes(proxy: MagicMock) -> Iterator[MagicMock]:
    """One device, a fake lockdown session, and `proxy` as its InstallationProxy.

    Yields the lockdown so a test can assert what was closed. Patched at the
    source modules because `_describe_device` imports `create_using_usbmux` —
    and `_detect_livecontainer_async` imports `InstallationProxyService` —
    inside the function body, so neither name exists at `graft_ipa.devices`
    module scope for `patch.object` to find.
    """
    lock = MagicMock()
    lock.close = AsyncMock()

    async def _fake_mux_list():
        return [_FakeMuxDevice("00008130-A")]

    with patch("pymobiledevice3.usbmux.list_devices", _fake_mux_list), \
         patch(
             "pymobiledevice3.lockdown.create_using_usbmux",
             new=AsyncMock(return_value=lock),
         ), \
         patch(
             "pymobiledevice3.services.installation_proxy.InstallationProxyService",
             return_value=proxy,
         ):
        yield lock


def test_describing_a_device_closes_both_connections_it_opened() -> None:
    """The leak, pinned on both halves.

    Measured under `-X dev` against the test phone, one `list_devices()` call:
    five unclosed resources with neither `finally`, three with the lockdown's
    alone, none with both. `asyncio.run` tears the loop down with whatever is
    still live, so each call left a session on the *phone* — and the GUI is
    about to run `--devices` on every plug and unplug.

    The proxy is asserted separately from the lockdown because closing the
    lockdown does not close it: `InstallationProxyService` opens its own
    connection, which is why removing either `finally` alone still leaks.
    """
    proxy = MagicMock()
    proxy.lookup = AsyncMock(return_value={"com.kdt.livecontainer.X": {"CFBundleIdentifier": "com.kdt.livecontainer.X"}})
    proxy.close = AsyncMock()

    with _a_phone_that_describes(proxy) as lock:
        listing = asyncio.run(devices_mod._list_devices_async())

    assert [d.livecontainer_bundle_id for d in listing.devices] == ["com.kdt.livecontainer.X"]
    lock.close.assert_awaited_once()
    proxy.close.assert_awaited_once()


def test_both_connections_are_closed_when_the_describe_fails() -> None:
    """A `finally`, not a trailing call — which is the whole point of the shape.

    A failure mid-describe is exactly when a leaked session is most likely and
    least visible: the device is reported as a `DeviceLookupFailure` either way,
    so nothing downstream would ever hint that a connection was left open.
    """
    proxy = MagicMock()
    proxy.lookup = AsyncMock(side_effect=ConnectionResetError("device went away"))
    proxy.close = AsyncMock()

    with _a_phone_that_describes(proxy) as lock:
        with patch.object(
            devices_mod,
            "_detect_livecontainer_async",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ):
            listing = asyncio.run(devices_mod._list_devices_async())

    assert [f.udid for f in listing.failures] == ["00008130-A"]
    assert "boom" in listing.failures[0].error, (
        "the close must not replace the error the failure exists to report"
    )
    lock.close.assert_awaited_once()


def test_a_proxy_that_fails_costs_the_bundle_id_and_nothing_else() -> None:
    """`livecontainer_bundle_id` is an optional field on a device that is
    otherwise perfectly describable, so every failure inside
    `_detect_livecontainer_async` — the close included — has to read as "not
    installed" rather than take the device off the listing.

    Stronger than the sibling test below, and worth keeping separate: there it
    is only the *close* that must not cost the device, here it is every failure
    in the function, the listing included.
    """
    proxy = MagicMock()
    proxy.lookup = AsyncMock(side_effect=ConnectionResetError("device went away"))
    proxy.close = AsyncMock(side_effect=OSError("and the close failed too"))

    with _a_phone_that_describes(proxy) as lock:
        listing = asyncio.run(devices_mod._list_devices_async())

    assert listing.failures == []
    assert [d.livecontainer_bundle_id for d in listing.devices] == [None]
    lock.close.assert_awaited_once()


def test_a_phone_that_drops_off_at_teardown_keeps_the_answer_it_gave() -> None:
    """The `DeviceInfo` is complete before the close runs, so the close cannot
    be allowed to take it away.

    Left bare, `finally: await lockdown.close()` ran *after* `_describe_device`
    had computed and staged its return value, and an exception there replaces a
    pending return: a Wi-Fi phone that fell off the network between the last
    read and `wait_closed()` came back as a `DeviceLookupFailure`, and the
    sidebar drew "connected, but couldn't be read" for a phone it had read
    perfectly. That is the disappearing-phone defect this module's header
    comment exists to end, arriving from the cleanup rather than the read.

    `ConnectionResetError` specifically because that is the shape observed —
    the suppression is broad for the reason every catch in this module is, but
    the case that motivated it is a real one.
    """
    proxy = MagicMock()
    proxy.lookup = AsyncMock(return_value={"com.kdt.livecontainer.X": {"CFBundleIdentifier": "com.kdt.livecontainer.X"}})
    proxy.close = AsyncMock()

    with _a_phone_that_describes(proxy) as lock:
        lock.close = AsyncMock(side_effect=ConnectionResetError("phone went away at teardown"))
        listing = asyncio.run(devices_mod._list_devices_async())

    assert listing.failures == [], "a failed teardown is not a device that couldn't be read"
    assert [d.udid for d in listing.devices] == ["00008130-A"]
    assert [d.livecontainer_bundle_id for d in listing.devices] == ["com.kdt.livecontainer.X"]
    lock.close.assert_awaited_once()


# ---- which LiveContainer the sidebar names: the push's answer -------------------
#
# `_detect_livecontainer_async` had a discovery of its own: a case-sensitive
# `startswith("com.kdt.livecontainer")` over User apps, first match wins. So a
# phone with only `com.kdt.LiveContainer2.<TEAM>` (an extra instance), only a
# capitalized primary, or a TrollStore install (a System app) showed "no LC" in
# the sidebar while the push found it fine; and with two installed, the sidebar
# named whichever the phone listed first, not the one the push goes into.

TEAM = "ABCDE12345"


def _detect(monkeypatch, installed: list[str], *, system: tuple[str, ...] = ()) -> str | None:
    from .test_lc_discovery import fake_device

    lookups: list[dict] = []
    fake_device(monkeypatch, installed, system=list(system), lookups=lookups)
    found = asyncio.run(devices_mod._detect_livecontainer_async(object()))
    # Asked the way the push asks: every application type, bundle ids only.
    assert lookups == [{"ApplicationType": "Any", "ReturnAttributes": ["CFBundleIdentifier"]}]
    return found


@pytest.mark.parametrize("only", [
    f"com.kdt.LiveContainer2.{TEAM}",
    f"com.kdt.LiveContainer.{TEAM}",
    f"COM.KDT.LIVECONTAINER3.{TEAM}",
])
def test_a_capitalised_or_secondary_livecontainer_is_found(monkeypatch, only: str) -> None:
    assert _detect(monkeypatch, ["com.example.app", only]) == only


def test_a_livecontainer_installed_as_a_system_app_is_found(monkeypatch) -> None:
    assert _detect(monkeypatch, ["com.example.app"], system=("com.kdt.livecontainer",)) == (
        "com.kdt.livecontainer"
    )


def test_the_sidebar_names_the_livecontainer_the_push_chooses(monkeypatch) -> None:
    from graft_ipa import lc_discovery

    ids = [f"com.kdt.livecontainer3.{TEAM}", f"com.kdt.livecontainer.{TEAM}"]

    found = _detect(monkeypatch, ids)

    assert found == lc_discovery.choose(ids)[0] == f"com.kdt.livecontainer.{TEAM}"


def test_two_primaries_still_report_a_livecontainer(monkeypatch) -> None:
    """The push needs a pin there and says so; the sidebar's question is only
    "is there one", and the answer is the first the push's refusal names."""
    other = "com.kdt.livecontainer.FGHIJ67890"

    assert _detect(monkeypatch, [other, f"com.kdt.livecontainer.{TEAM}"]) == (
        f"com.kdt.livecontainer.{TEAM}"
    )


def test_no_livecontainer_is_none(monkeypatch) -> None:
    assert _detect(monkeypatch, ["com.example.app", "com.example.livecontainer.X"]) is None
