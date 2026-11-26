"""Where `graft --update` installs (spec §11.3, option B): every device in the
app's delivery history, each with its own delivery state; a delivery
recorded without a device counts as the default's only when Graft has seen
the default; and a device dropped with `--drop-update-target` is left out
until that app is delivered there again."""

from __future__ import annotations

from graft_ipa import cli as cli_mod
from graft_ipa import devices_seen, update_drops
from graft_ipa.config import add_app, set_settings

from .update_fakes import OTHER_PHONE, PHONE


def _deliveries(world, app: str = "example"):
    return cli_mod._delivery_targets(world.cfg)(app)


def _kinds(world, app: str = "example") -> dict[str, str]:
    return {t.udid: t.kind for t in _deliveries(world, app).targets}


def test_every_device_the_app_was_delivered_to_is_a_target(world) -> None:
    world.deliver("example", udid=OTHER_PHONE)

    found = _deliveries(world)

    assert found.delivered is True
    assert sorted(t.udid for t in found.targets) == sorted([PHONE, OTHER_PHONE])


def test_a_device_delivered_to_twice_is_one_target(world) -> None:
    world.deliver("example")

    assert [t.udid for t in _deliveries(world).targets] == [PHONE]


def test_each_target_has_its_own_delivery_state(world) -> None:
    """The iPhone has the newest build; the other device still has the last one."""
    world.deliver("example", udid=OTHER_PHONE)
    world.stage("example", "1.1")
    world.deliver("example")

    assert _kinds(world) == {PHONE: "current", OTHER_PHONE: "stale"}


def test_each_target_carries_its_own_rows_origin(world) -> None:
    world.deliver("example", udid=OTHER_PHONE, origin="reinstall")

    by_udid = {t.udid: t for t in _deliveries(world).targets}

    assert (by_udid[PHONE].origin, by_udid[PHONE].kind) == ("build", "current")
    assert (by_udid[OTHER_PHONE].origin, by_udid[OTHER_PHONE].kind) == ("reinstall", "stale")


def test_an_app_never_delivered_has_no_target(world) -> None:
    add_app(world.cfg_path, "delta", {"bundle_id": "com.delta.app", "delivery": "livecontainer", "tweaks": []})

    found = _deliveries(world, "delta")

    assert (found.delivered, found.targets, found.unplaced) == (False, [], False)


# --- a delivery recorded without a device ---------------------------------------


def _only_unrecorded(world, app: str = "delta") -> None:
    add_app(world.cfg_path, app, {"bundle_id": "com.delta.app", "delivery": "livecontainer", "tweaks": []})
    world.stage("delta", "1.0")
    world.deliver(app, udid=None)


def test_a_delivery_without_a_device_is_the_defaults_when_graft_has_seen_it(world) -> None:
    _only_unrecorded(world)
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone")

    found = _deliveries(world, "delta")

    assert [(t.udid, t.kind) for t in found.targets] == [(PHONE, "current")]
    assert found.unplaced is False


def test_a_delivery_without_a_device_is_left_out_when_graft_never_saw_the_default(world) -> None:
    _only_unrecorded(world)

    found = _deliveries(world, "delta")

    assert (found.delivered, found.targets, found.unplaced) == (True, [], True)


def test_a_delivery_without_a_device_is_left_out_with_no_default(world) -> None:
    _only_unrecorded(world)
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone")
    set_settings(world.cfg_path, default_device_udid="")

    found = _deliveries(world, "delta")

    assert (found.targets, found.unplaced) == ([], True)


def test_the_default_is_one_target_for_its_own_rows_and_the_unrecorded_ones(world) -> None:
    """The newest of the two decides its state: here the unrecorded one,
    made after the staged version changed."""
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone")
    world.stage("example", "1.1")
    world.deliver("example", udid=None)

    found = _deliveries(world)

    assert [(t.udid, t.kind) for t in found.targets] == [(PHONE, "current")]


# --- dropped targets ----------------------------------------------------------------


def _drop(world, app: str, udid: str) -> None:
    cli_mod._drop_update_target(world.cfg, app, udid)


def test_a_dropped_device_is_left_out(world) -> None:
    world.deliver("example", udid=OTHER_PHONE)

    _drop(world, "example", OTHER_PHONE)

    found = _deliveries(world)
    assert [t.udid for t in found.targets] == [PHONE]
    assert found.dropped == [OTHER_PHONE]
    assert [t.udid for t in _deliveries(world, "bravo").targets] == [PHONE], "another app's own"


def test_a_new_delivery_of_that_app_there_brings_it_back(world) -> None:
    world.deliver("example", udid=OTHER_PHONE)
    _drop(world, "example", OTHER_PHONE)

    world.deliver("example", udid=OTHER_PHONE)

    found = _deliveries(world)
    assert sorted(t.udid for t in found.targets) == sorted([PHONE, OTHER_PHONE])
    assert found.dropped == []


def test_another_apps_delivery_there_doesnt_bring_it_back(world) -> None:
    world.deliver("example", udid=OTHER_PHONE)
    _drop(world, "example", OTHER_PHONE)

    world.deliver("bravo", udid=OTHER_PHONE)

    assert [t.udid for t in _deliveries(world).targets] == [PHONE]


def test_the_drop_is_kept_in_the_state_folder(world) -> None:
    world.deliver("example", udid=OTHER_PHONE)

    _drop(world, "example", OTHER_PHONE)

    cfg = world.cfg
    assert (cfg.state_dir / update_drops.FILENAME).is_file()
    assert set(update_drops.dropped(cfg.state_dir, "example")) == {OTHER_PHONE}


def test_the_drop_file_read_leniently(world) -> None:
    cfg = world.cfg
    (cfg.state_dir / update_drops.FILENAME).write_text("not json")

    assert update_drops.dropped(cfg.state_dir, "example") == {}
    assert len(_deliveries(world).targets) == 1


def test_an_unreadable_delivery_log_has_no_target_and_says_so(world, monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_open_delivery_log", lambda _cfg: cli_mod._UnreadableLog())

    found = _deliveries(world)

    assert found.targets == [] and found.delivered is True
    assert found.unreadable is True
