"""What Graft calls a connected device (`devices.name_from_lockdown`).

pymobiledevice3's `display_name` is the model's marketing name, from a table
of models built into it ("iPhone 15 Pro" for iPhone16,1). A model newer than
that table has none, and Graft named such a device by its UDID everywhere:
the sidebar, the build bar, the update confirmation, the waiting sentence.
The device itself says the name its owner gave it (lockdown's `DeviceName`).

The ladder: the model's name when pymobiledevice3 knows the model, else the
device's own name, else its family from the product type ("iPad"), else the
UDID.
"""

from __future__ import annotations

import asyncio
import json
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymobiledevice3.lockdown import UsbmuxLockdownClient

from graft_ipa import developer_mode, device_info, devices, devices_seen
from graft_ipa.devices import model_or_family_from_lockdown, name_from_lockdown
from graft_ipa.errors import DeveloperModeOffError

UDID = "00008140-FAKE0000000000001"
#: A model newer than pymobiledevice3's table, as the hands-on's iPad was.
NEWER_IPAD = "iPad19,1"


def _session(values: dict) -> UsbmuxLockdownClient:
    """pymobiledevice3's own lockdown client, with the values a device gave:
    the class `create_using_usbmux` returns, its `display_name` looked up in
    its real table, and no `device_name` at all. Set the way its
    `_initialize` sets them, with no connection behind it."""
    lockdown = UsbmuxLockdownClient(None, "HOST")
    lockdown.all_values = values
    lockdown.product_type = values.get("ProductType")
    return lockdown


def _stand_in(*, display_name: object = None, product_type: object = None,
              values: object = None) -> MagicMock:
    lockdown = MagicMock()
    lockdown.display_name = display_name
    lockdown.product_type = product_type
    lockdown.all_values = values
    return lockdown


# --- each rung -----------------------------------------------------------------


def test_a_known_model_keeps_its_model_name() -> None:
    lockdown = _stand_in(display_name="iPhone 15 Pro", product_type="iPhone16,1",
                         values={"ProductType": "iPhone16,1", "DeviceName": "Sample's iPhone"})

    assert name_from_lockdown(lockdown, UDID) == "iPhone 15 Pro"


def test_a_model_newer_than_the_table_is_called_what_it_calls_itself() -> None:
    lockdown = _stand_in(product_type=NEWER_IPAD,
                         values={"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad"})

    assert name_from_lockdown(lockdown, UDID) == "Sample's iPad"


@pytest.mark.parametrize(("product_type", "family"), [
    (NEWER_IPAD, "iPad"),
    ("iPhone19,1", "iPhone"),
    ("iPod10,1", "iPod touch"),
])
@pytest.mark.parametrize("device_name", [None, "", "   ", 7])
def test_with_no_name_of_its_own_it_is_its_family(
    product_type: str, family: str, device_name: object,
) -> None:
    values: dict = {"ProductType": product_type}
    if device_name is not None:
        values["DeviceName"] = device_name
    lockdown = _stand_in(product_type=product_type, values=values)

    assert name_from_lockdown(lockdown, UDID) == family


def test_a_family_graft_does_not_deliver_to_is_no_family() -> None:
    lockdown = _stand_in(product_type="Watch7,1", values={"ProductType": "Watch7,1"})

    assert name_from_lockdown(lockdown, UDID) == UDID


def test_with_nothing_said_it_is_its_udid() -> None:
    assert name_from_lockdown(_stand_in(values={}), UDID) == UDID


def test_with_nothing_said_and_no_udid_it_is_nothing() -> None:
    """For a caller with a sentence of its own for "don't know" (the
    Developer Mode hint's "your iPhone or iPad")."""
    assert name_from_lockdown(_stand_in(values={})) is None


def test_a_name_is_trimmed() -> None:
    lockdown = _stand_in(product_type=NEWER_IPAD,
                         values={"ProductType": NEWER_IPAD, "DeviceName": "  Sample's iPad \n"})

    assert name_from_lockdown(lockdown, UDID) == "Sample's iPad"


# --- the shapes the code really sees ------------------------------------------


def test_pymobiledevice3s_own_client_for_a_newer_model() -> None:
    """The real class: `display_name` None for a model not in its table, and
    no `device_name` attribute to ask."""
    lockdown = _session({"ProductType": NEWER_IPAD, "DeviceClass": "iPad",
                         "DeviceName": "Sample's iPad"})
    assert lockdown.display_name is None, "premise: the table doesn't know it"
    assert not hasattr(lockdown, "device_name"), "premise: nothing by that name"

    assert name_from_lockdown(lockdown, UDID) == "Sample's iPad"


def test_pymobiledevice3s_own_client_for_a_known_model() -> None:
    lockdown = _session({"ProductType": "iPhone16,1", "DeviceClass": "iPhone",
                         "DeviceName": "Sample's iPhone"})

    assert name_from_lockdown(lockdown, UDID) == "iPhone 15 Pro"


def test_pymobiledevice3s_own_client_with_no_name() -> None:
    lockdown = _session({"ProductType": NEWER_IPAD, "DeviceClass": "iPad"})

    assert name_from_lockdown(lockdown, UDID) == "iPad"


def test_model_or_family_is_never_the_devices_own_name() -> None:
    """For the Developer Mode hint ("your iPad", "your iPhone 15 Pro"),
    which would read oddly as "your Sample's iPad"."""
    own_name = {"DeviceName": "Sample's iPad"}

    assert model_or_family_from_lockdown(_session({"ProductType": NEWER_IPAD, **own_name})) == "iPad"
    assert model_or_family_from_lockdown(
        _session({"ProductType": "iPhone16,1", **own_name})) == "iPhone 15 Pro"
    assert model_or_family_from_lockdown(_session(own_name)) is None
    assert model_or_family_from_lockdown(AsyncMock()) is None


def test_an_async_session_whose_attributes_are_coroutine_functions() -> None:
    """The async stand-in every device test builds (`AsyncMock()`): whatever
    isn't set is an async function, not text, and is never a name, nor
    called (no coroutine left un-awaited)."""
    lockdown = AsyncMock()
    lockdown.all_values = {"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad"}
    silent = AsyncMock()

    assert name_from_lockdown(lockdown, UDID) == "Sample's iPad"
    assert name_from_lockdown(silent, UDID) == UDID
    assert lockdown.mock_calls == [] and silent.mock_calls == [], "nothing was called"


def test_a_property_that_raises_is_no_answer() -> None:
    class Session:
        def __init__(self) -> None:
            self.all_values = {"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad"}
            self.product_type = NEWER_IPAD

        @property
        def display_name(self) -> str:
            raise RuntimeError("table unreadable")

    assert name_from_lockdown(Session(), UDID) == "Sample's iPad"


# --- where it's used -----------------------------------------------------------


def _listed(lockdown) -> devices.DeviceListing:
    class Mux:
        serial = UDID
        connection_type = "USB"

    async def mux_list():
        return [Mux()]

    lockdown.close = AsyncMock()
    with patch("pymobiledevice3.usbmux.list_devices", mux_list), \
         patch("pymobiledevice3.lockdown.create_using_usbmux", new=AsyncMock(return_value=lockdown)), \
         patch.object(devices, "_livecontainer_on_device_async",
                      new=AsyncMock(return_value=(None, False))):
        return asyncio.run(devices._list_devices_async())


def test_the_listing_names_a_newer_model_by_its_own_name() -> None:
    listing = _listed(_session({"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad",
                                "ProductVersion": "27.0"}))

    assert [(d.name, d.model) for d in listing.devices] == [("Sample's iPad", NEWER_IPAD)]


def test_the_listing_keeps_a_known_models_name() -> None:
    listing = _listed(_session({"ProductType": "iPhone16,1", "DeviceName": "Sample's iPhone"}))

    assert [d.name for d in listing.devices] == ["iPhone 15 Pro"]


def test_the_listing_remembers_that_name_for_a_plan_that_doesnt_look(temp_config_env: Path) -> None:
    """`devices-seen.json` keeps the listing's name, so `--update --plan
    --quick`, and a target not looked up this run, name the device."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.config import load_config
    from graft_ipa.json_output import JsonEmitter

    listing = _listed(_session({"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad"}))
    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=listing):
        cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert [e["name"] for e in events if e["type"] == "device"] == ["Sample's iPad"]
    assert devices_seen.name_of(cfg.state_dir, UDID) == "Sample's iPad"


def _device_info_session(values: dict) -> UsbmuxLockdownClient:
    lockdown = _session(values)
    lockdown.get_value = AsyncMock(return_value=None)
    lockdown.close = AsyncMock()
    return lockdown


def test_device_info_names_a_newer_model_by_its_own_name() -> None:
    lockdown = _device_info_session({"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad",
                                     "ProductVersion": "27.0"})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lockdown)):
        info = device_info.read_device_info(UDID)

    assert (info.name, info.model) == ("Sample's iPad", NEWER_IPAD)


def test_device_info_with_nothing_said_names_nothing() -> None:
    """None, not the UDID: `devices-seen.json` keeps no name rather than one
    that isn't."""
    lockdown = _device_info_session({})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lockdown)):
        info = device_info.read_device_info(UDID)

    assert info.name is None


def test_the_developer_mode_check_names_a_newer_model_by_its_own_name() -> None:
    lockdown = _device_info_session({"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad",
                                     "ProductVersion": "27.0"})
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lockdown)):
        check = device_info.check_developer_mode(UDID, reveal=False)

    assert check.name == "Sample's iPad"


def test_the_developer_mode_hint_calls_a_newer_model_by_its_family(tmp_path: Path) -> None:
    """The refusal names the device as everything else does, and the hint
    says "your iPad", never "your Sample's iPad"."""
    lockdown = _device_info_session({"ProductType": NEWER_IPAD, "DeviceName": "Sample's iPad",
                                     "ProductVersion": "27.0"})
    lockdown.get_value = AsyncMock(
        side_effect=lambda domain=None, key=None: False if key == "DeveloperModeStatus" else None)
    with patch.object(device_info, "create_using_usbmux", new=AsyncMock(return_value=lockdown)), \
         pytest.raises(DeveloperModeOffError) as caught:
        developer_mode.require_on(tmp_path, UDID)

    assert caught.value.hint == developer_mode.turn_on("iPad")
    assert caught.value.hint.startswith("Turn on Developer Mode on your iPad in Settings")
    assert caught.value.message == "Developer Mode is off on Sample's iPad"
    assert devices_seen.name_of(tmp_path, UDID) == "Sample's iPad"
