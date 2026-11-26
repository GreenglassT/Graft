"""Whether each of an app's update targets is ready (spec 3.1's delivery
readiness, per device since §11.3's option B): connected, through
`installer.resolve_target` with that device named (§11.6), else waiting;
then a LiveContainer on it, Developer Mode, and the signing profile, in the
quick form that opens no session and the full form that checks. One device's
answer never decides another's."""

from __future__ import annotations

import pymobiledevice3.lockdown as pmd_lockdown
import pytest

from graft_ipa import (
    developer_mode,
    device_info,
    devices,
    devices_seen,
    lc_manage,
    update_readiness,
)
from graft_ipa.config import set_app_options, set_settings
from graft_ipa.update_readiness import FullProbe, QuickProbe

from .update_fakes import AD_HOC, DEVELOPMENT, OTHER_PHONE, PHONE, FakePhone


def _ready(world, app: str, probe, udid: str = PHONE) -> update_readiness.Readiness:
    cfg = world.cfg
    return update_readiness.readiness_for(cfg, [(cfg.get_app(app), udid)], probe)[(app, udid)]


def _probes(world):
    return (QuickProbe(world.cfg.state_dir), FullProbe(world.cfg.state_dir))


# --- connected, else waiting (§11.3, §11.6) -------------------------------------


def test_a_connected_target_is_ready_and_named(world) -> None:
    ready = _ready(world, "example", FullProbe(world.cfg.state_dir))

    assert (ready.waiting, ready.blocked_reason) == (None, None)
    assert (ready.target.udid, ready.target.device_name, ready.target.lc_bundle_id) == (
        PHONE, "Example iPhone", "com.kdt.livecontainer",
    )


def test_a_target_that_isnt_connected_is_waiting_not_blocked(world) -> None:
    """Another device connected, and the default at that, changes nothing:
    the target waits for its own device."""
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")

    for probe in _probes(world):
        ready = _ready(world, "example", probe, OTHER_PHONE)
        assert ready.waiting == "Example iPad isn't connected: it'll update when it is"
        assert ready.blocked_reason is None
        assert (ready.target.udid, ready.target.device_name) == (OTHER_PHONE, "Example iPad")


def test_one_target_waiting_leaves_the_other_ready(world) -> None:
    cfg = world.cfg
    app = cfg.get_app("example")

    ready = update_readiness.readiness_for(cfg, [(app, OTHER_PHONE), (app, PHONE)],
                                           FullProbe(cfg.state_dir))

    assert ready[("example", PHONE)].waiting is None
    assert ready[("example", PHONE)].blocked_reason is None
    assert ready[("example", OTHER_PHONE)].waiting is not None


def test_several_devices_connected_never_asks_for_a_pick(world) -> None:
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")
    set_settings(world.cfg_path, default_device_udid="")

    for probe in _probes(world):
        for udid, name in ((PHONE, "Example iPhone"), (OTHER_PHONE, "Example iPad")):
            ready = _ready(world, "example", probe, udid)
            assert (ready.waiting, ready.blocked_reason) == (None, None)
            if probe.blocks:
                assert ready.target.device_name == name


def test_no_device_at_all_is_waiting_naming_the_one_to_connect(world) -> None:
    world.phones.clear()

    assert _ready(world, "example", FullProbe(world.cfg.state_dir)).waiting == (
        f"{PHONE} isn't connected: it'll update when it is"), "its UDID until Graft knows its name"

    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone")
    assert _ready(world, "example", QuickProbe(world.cfg.state_dir)).waiting == (
        "Example iPhone isn't connected: it'll update when it is")


def test_the_sentence_names_the_device_as_the_not_connected_error_does(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "no target" contract between Plans E and A (spec §11.6): a named
    device that isn't connected is `DeviceNotConnectedError`, and its
    `.label` is what the sentence says."""
    from graft_ipa import installer
    from graft_ipa.errors import DeviceNotConnectedError

    asked: list[str | None] = []

    def gone(cfg, app, explicit_udid=None):
        asked.append(explicit_udid)
        raise DeviceNotConnectedError(explicit_udid, label="Spare iPad")

    monkeypatch.setattr(installer, "resolve_target", gone)

    ready = _ready(world, "example", FullProbe(world.cfg.state_dir), OTHER_PHONE)

    assert asked == [OTHER_PHONE], "asked about that device by name"
    assert ready.waiting == "Spare iPad isn't connected: it'll update when it is"
    assert ready.target.udid == OTHER_PHONE


# --- LiveContainer, per device ------------------------------------------------------


def test_no_livecontainer_blocks_that_target_alone(world) -> None:
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad", installed=["com.apple.mobilesafari"])
    cfg = world.cfg
    app = cfg.get_app("example")

    ready = update_readiness.readiness_for(cfg, [(app, PHONE), (app, OTHER_PHONE)],
                                           FullProbe(cfg.state_dir))

    assert ready[("example", PHONE)].blocked_reason is None
    blocked = ready[("example", OTHER_PHONE)].blocked_reason
    assert blocked and "LiveContainer" in blocked


def test_a_locked_phone_that_lists_nothing_blocks_a_full_plan(world) -> None:
    world.phones[PHONE].installed = []

    blocked = _ready(world, "example", FullProbe(world.cfg.state_dir)).blocked_reason

    assert blocked == "Example iPhone listed no apps: unlock it and keep it on the home screen"


def test_a_phone_lockdown_cannot_describe_blocks_saying_so(world) -> None:
    world.phones[PHONE].unreadable = "PasswordRequiredError"

    blocked = _ready(world, "example", FullProbe(world.cfg.state_dir)).blocked_reason

    assert blocked == f"{PHONE} is connected but couldn't be read: PasswordRequiredError"


# --- Developer Mode: development-signed installs only (spec §5) ---------------


def _sideload(world, entitlements=DEVELOPMENT, devices_in_profile=None) -> None:
    set_app_options(world.cfg_path, "example", delivery="sideload")
    world.profile(entitlements, devices_in_profile)


#: An update never has the phone show the switch, as a real build's refusal
#: does (final review 1): so the hint says how to make it appear.
DEVELOPER_MODE_OFF = (
    "Turn on Developer Mode on your iPhone or iPad in Settings › Privacy & Security › "
    "Developer Mode, then restart it. If the switch isn't there, Build & Install the app once: "
    "Graft makes it appear."
)


def test_developer_mode_off_blocks_a_development_signed_install(world) -> None:
    _sideload(world)
    world.phones[PHONE].developer_mode = False

    assert _ready(world, "example", FullProbe(world.cfg.state_dir)).blocked_reason == DEVELOPER_MODE_OFF
    assert DEVELOPER_MODE_OFF.startswith(developer_mode.TURN_ON), "the build's own hint, then how"


def test_developer_mode_off_on_one_device_blocks_that_one(world) -> None:
    _sideload(world)
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad", developer_mode=False)

    assert _ready(world, "example", FullProbe(world.cfg.state_dir)).blocked_reason is None
    assert _ready(world, "example", FullProbe(world.cfg.state_dir), OTHER_PHONE).blocked_reason == (
        DEVELOPER_MODE_OFF)


def test_developer_mode_off_never_blocks_an_ad_hoc_install(world) -> None:
    """An ad hoc or enterprise profile runs without it (`developer_mode.needed`)."""
    _sideload(world, AD_HOC)
    world.phones[PHONE].developer_mode = False

    ready = _ready(world, "example", FullProbe(world.cfg.state_dir))

    assert ready.blocked_reason is None
    assert world.sessions == [PHONE], "the listing's session alone: Developer Mode wasn't asked"


def test_developer_mode_unknown_never_blocks(world) -> None:
    _sideload(world)
    world.phones[PHONE].developer_mode = None

    assert _ready(world, "example", FullProbe(world.cfg.state_dir)).blocked_reason is None


@pytest.mark.parametrize("cached", [False, None])
def test_a_quick_plan_never_blocks_on_a_cached_reading_and_shows_an_off_one(world, cached) -> None:
    """A remembered "Developer Mode off" only shows (spec 3.1's table): the
    full plan the confirmation runs checks it."""
    _sideload(world)
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone", developer_mode=cached)

    ready = _ready(world, "example", QuickProbe(world.cfg.state_dir))

    assert ready.blocked_reason is None
    if cached is False:
        assert ready.note == "Developer Mode was off on Example iPhone when Graft last checked"
    else:
        assert ready.note is None


# --- the signing profile (§11.3: registered) ------------------------------------------


def test_a_device_the_signing_profile_doesnt_list_is_blocked(world) -> None:
    _sideload(world, devices_in_profile=[PHONE])
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")

    assert _ready(world, "example", FullProbe(world.cfg.state_dir)).blocked_reason is None
    assert _ready(world, "example", FullProbe(world.cfg.state_dir), OTHER_PHONE).blocked_reason == (
        "Example iPad isn't in the provisioning profile that signs Example+")


def test_a_quick_plan_never_reads_the_signing_profile_to_block(world) -> None:
    _sideload(world, devices_in_profile=[PHONE])
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")

    assert _ready(world, "example", QuickProbe(world.cfg.state_dir), OTHER_PHONE).blocked_reason is None


def test_a_livecontainer_app_has_no_signing_profile_to_check(world) -> None:
    world.profile(DEVELOPMENT, [PHONE])
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")

    assert _ready(world, "example", FullProbe(world.cfg.state_dir), OTHER_PHONE).blocked_reason is None


# --- the two forms ---------------------------------------------------------------


def test_a_quick_plan_asks_usbmux_and_opens_no_session(world, monkeypatch) -> None:
    """Spec §8: a stand-in usbmux, and a lockdown that raises."""
    def no_session(*_a, **_k):
        raise AssertionError("a quick plan opened a session to the phone")

    monkeypatch.setattr(pmd_lockdown, "create_using_usbmux", no_session)
    monkeypatch.setattr(devices, "list_devices", no_session)
    monkeypatch.setattr(lc_manage.UsbmuxDevice, "installed_bundle_ids", no_session)
    monkeypatch.setattr(device_info, "read_developer_mode", no_session)
    cfg = world.cfg
    devices_seen.remember(cfg.state_dir, PHONE, name="Example iPhone",
                          livecontainer_bundle_id="com.kdt.livecontainer")

    ready = _ready(world, "example", QuickProbe(cfg.state_dir))

    assert (ready.waiting, ready.blocked_reason) == (None, None)
    assert (ready.target.udid, ready.target.device_name, ready.target.lc_bundle_id) == (
        PHONE, "Example iPhone", "com.kdt.livecontainer",
    )
    assert QuickProbe(cfg.state_dir).readiness == update_readiness.LIKELY


def test_a_full_look_is_remembered_for_the_next_quick_one(world) -> None:
    cfg = world.cfg

    _ready(world, "example", FullProbe(cfg.state_dir))

    seen = devices_seen.seen(cfg.state_dir, PHONE)
    assert (seen.get("name"), seen.get("livecontainer_bundle_id")) == ("Example iPhone", "com.kdt.livecontainer")


def test_each_phone_is_looked_at_once_for_every_app_on_it(world) -> None:
    cfg = world.cfg
    update_readiness.readiness_for(cfg, [(cfg.get_app("example"), PHONE), (cfg.get_app("bravo"), PHONE)],
                                   FullProbe(cfg.state_dir))

    assert world.sessions == [PHONE, PHONE], "one listing, one app list"


def test_nothing_is_asked_when_no_target_needs_it(world) -> None:
    assert update_readiness.readiness_for(world.cfg, [], FullProbe(world.cfg.state_dir)) == {}
    assert world.sessions == []
