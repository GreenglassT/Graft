"""The plan, per app, for each case in spec 3.1."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from graft_ipa import devices_seen, host, update_plan, update_refusals
from graft_ipa.config import add_app, set_app_options
from graft_ipa.errors import ConfigError
from graft_ipa.update_plan import StoreAnswer
from graft_ipa.update_readiness import FullProbe

from .update_fakes import DEVELOPMENT, OTHER_PHONE, PHONE, FakePhone, deb, plan_of

CONTRACT = Path(__file__).resolve().parent.parent / "Graft" / "Graft" / "Models" / "CLIContract.swift"


def _step(world, app: str, answer: StoreAnswer | None):
    cfg = world.cfg
    return update_plan.store_step(cfg, cfg.get_app(app), answer)


def test_a_newer_version_is_a_get(world) -> None:
    step = _step(world, "example", StoreAnswer("2.0"))

    assert (step.from_version, step.to_version, step.skipped_reason) == ("1.0", "2.0", None)
    assert step.planned


@pytest.mark.parametrize("store", ["1.0", "0.9"])
def test_the_same_or_an_older_version_is_nothing(world, store: str) -> None:
    step = _step(world, "example", StoreAnswer(store))

    assert (step.to_version, step.skipped_reason) == (None, None)
    assert not step.planned


def test_already_staged_is_nothing_the_step_is_done(world) -> None:
    world.stage("example", "2.0")

    step = _step(world, "example", StoreAnswer("2.0"))

    assert (step.from_version, step.to_version, step.skipped_reason) == ("2.0", None, None)


def test_a_held_app_is_not_got(world) -> None:
    set_app_options(world.cfg_path, "example", preferred_version="1.0")

    step = _step(world, "example", StoreAnswer("2.0"))

    assert step.skipped_reason == "held at 1.0" and not step.planned


def test_without_apple_silicon_the_newer_version_shows_and_is_not_got(world, monkeypatch) -> None:
    monkeypatch.setattr(host, "apple_silicon_blocker", lambda: host.INTEL_MAC)

    step = _step(world, "example", StoreAnswer("2.0"))

    assert (step.to_version, step.skipped_reason) == ("2.0", update_plan.NEEDS_APPLE_SILICON)


def test_a_failed_lookup_is_couldnt_check(world) -> None:
    step = _step(world, "example", StoreAnswer(None, "offline"))

    assert step.skipped_reason == update_plan.LOOKUP_FAILED and not step.planned


def test_a_refused_version_is_said_instead_of_offered(world) -> None:
    cfg = world.cfg
    update_refusals.remember(cfg.state_dir, cfg.get_app("example"), "2.0")

    step = _step(world, "example", StoreAnswer("2.0"))

    assert step.skipped_reason == "Your Apple ID doesn't have 2.0; get it in the App Store"
    assert step.to_version == "2.0"


def test_a_newer_version_than_the_refused_one_clears_the_memory(world) -> None:
    cfg = world.cfg
    update_refusals.remember(cfg.state_dir, cfg.get_app("example"), "2.0")

    step = _step(world, "example", StoreAnswer("2.1"))

    assert step.planned
    assert update_refusals.refused_version(cfg.state_dir, cfg.get_app("example")) is None


def test_apples_own_app_is_never_asked_about(world) -> None:
    step = _step(world, "example", None)

    assert (step.to_version, step.skipped_reason) == (None, None)


# --- the whole plan (spec 3.1) -------------------------------------------------


def test_a_newer_app_store_version_plans_a_get_and_an_install(world) -> None:
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"])["example"]

    assert plan["store"] == {"from": "1.0", "to": "2.0", "skipped_reason": None}
    assert plan["install"] is True and plan["nothing_to_do"] is False
    assert plan["actionable"] is True
    assert plan["readiness"] == "checked"
    assert "target" not in plan
    assert plan["targets"] == [{
        "udid": PHONE, "device_name": "Example iPhone", "delivery": "livecontainer",
        "lc_bundle_id": "com.kdt.livecontainer", "install": True, "install_skipped_reason": None,
        "manual_install": False, "waiting": None, "blocked_reason": None, "readiness": "checked",
        "readiness_note": None,
    }]
    assert (plan["manual_install"], plan["readiness_note"]) == (False, None)


def test_a_newer_tweak_is_listed_with_its_versions(world) -> None:
    world.published["com.example.btweak"] = "2.1"

    plan = plan_of(world, ["bravo"])["bravo"]

    assert plan["tweaks"] == [{"filename": "b-tweak.deb", "name": "B Tweak", "from": "2.0", "to": "2.1",
                               "unchecked": False, "also_affects": []}]
    assert plan["install"] is True


def test_a_tweak_that_couldnt_be_checked_is_listed_and_left_alone(world) -> None:
    world.unreachable.add("com.example.btweak")

    plan = plan_of(world, ["bravo"])["bravo"]

    assert plan["tweaks"] == [{"filename": "b-tweak.deb", "name": "B Tweak", "from": "2.0", "to": None,
                               "unchecked": True, "also_affects": []}]
    assert plan["install"] is False and plan["nothing_to_do"] is True


def test_a_library_file_no_app_uses_is_not_part_of_update(world) -> None:
    deb(world.cfg.tweaks_dir / "orphan.deb", "com.example.orphan", "1.0")
    world.published["com.example.orphan"] = "9.0"

    plans = plan_of(world, ["example", "bravo"])

    assert all(t["filename"] != "orphan.deb" for p in plans.values() for t in p["tweaks"])
    assert "com.example.orphan" not in world.latest_asked, "nor looked up online"


def test_nothing_newer_and_current_is_nothing_to_do(world) -> None:
    plan = plan_of(world, ["example"])["example"]

    assert plan["nothing_to_do"] is True
    assert plan["install"] is False
    assert world.sessions == [], "no phone is asked about an app with nothing to do"


def test_a_change_staged_since_the_last_install_is_an_install(world) -> None:
    world.stage("example", "1.1")  # staged, never installed
    world.store["com.example.app"] = "1.1"

    plan = plan_of(world, ["example"])["example"]

    assert plan["store"]["to"] is None
    assert plan["tweaks"] == []
    assert plan["install"] is True and plan["nothing_to_do"] is False


def test_an_app_never_delivered_is_left_to_build_and_install(world) -> None:
    add_app(world.cfg_path, "delta", {"bundle_id": "com.delta.app", "delivery": "livecontainer", "tweaks": []})
    world.store["com.delta.app"] = "3.0"

    plan = plan_of(world, ["delta"])["delta"]

    assert plan["install"] is False
    assert plan["install_skipped_reason"] == update_plan.NOT_INSTALLED_YET
    assert plan["nothing_to_do"] is False


# --- what Install Update covers (spec §11.1) ------------------------------------


def test_a_reinstalled_older_build_is_offered_not_installed(world) -> None:
    """A deliberate Re-install of an archived build: nothing to compare, and
    the person chose it, so it waits for them to press Install Update."""
    world.deliver("example", origin="reinstall")

    plan = plan_of(world, ["example"])["example"]

    assert plan["install"] is False
    assert plan["install_skipped_reason"] == update_plan.REINSTALLED
    assert plan["manual_install"] is True
    assert plan["nothing_to_do"] is False
    assert world.sessions == [], "no badge: not an update to check readiness for"


def test_a_last_install_the_device_no_longer_has_is_offered_not_installed(world) -> None:
    """`--reconcile` didn't find LiveContainer's copy, which an import and a
    clean-up remove too: not evidence of a change to bring over."""
    world.reconciled_missing(world.deliver("example"))

    plan = plan_of(world, ["example"])["example"]

    assert plan["install"] is False
    assert plan["install_skipped_reason"] == update_plan.NOT_ON_DEVICE
    assert plan["manual_install"] is True


def test_a_real_change_installs_a_reinstalled_app_too(world) -> None:
    world.deliver("example", origin="reinstall")
    world.published["com.example.atweak"] = "1.1"

    plan = plan_of(world, ["example"])["example"]

    assert plan["install"] is True
    assert (plan["install_skipped_reason"], plan["manual_install"]) == (None, False)


def test_the_delivery_facts_carry_the_reasons_origin_and_device(world) -> None:
    from graft_ipa import cli as cli_mod

    world.deliver("example", origin="reinstall")

    (facts,) = cli_mod._delivery_targets(world.cfg)("example").targets

    assert (facts.kind, facts.origin, facts.reconcile_missing, facts.udid) == (
        "stale", "reinstall", False, PHONE)
    assert facts.reasons


# --- the rest of the plan --------------------------------------------------------


def test_a_failed_lookup_never_blocks_the_tweak_and_install_steps(world) -> None:
    world.lookup_error = "iTunes lookup request failed: offline"
    world.published["com.example.atweak"] = "1.1"

    plan = plan_of(world, ["example"])["example"]

    assert plan["store"]["skipped_reason"] == update_plan.LOOKUP_FAILED
    assert [t["to"] for t in plan["tweaks"]] == ["1.1"]
    assert plan["install"] is True


def test_a_held_app_still_gets_its_tweaks_and_install(world) -> None:
    set_app_options(world.cfg_path, "example", preferred_version="1.0")
    world.store["com.example.app"] = "2.0"
    world.published["com.example.atweak"] = "1.1"

    plan = plan_of(world, ["example"])["example"]

    assert plan["store"]["skipped_reason"] == "held at 1.0"
    assert plan["install"] is True


def test_a_shared_tweak_names_the_other_app_it_affects(world) -> None:
    world.published["com.example.atweak"] = "1.1"

    plan = plan_of(world, ["example"])["example"]

    assert plan["tweaks"][0]["also_affects"] == ["bravo"]


def test_with_affected_adds_the_other_app_to_the_plan(world) -> None:
    world.published["com.example.atweak"] = "1.1"

    plans = plan_of(world, ["example"], with_affected=True)

    assert list(plans) == ["example", "bravo"]
    assert plans["example"]["tweaks"][0]["also_affects"] == []
    assert plans["bravo"]["install"] is True


def test_a_blocked_app_says_why_and_still_shows_its_update(world) -> None:
    world.store["com.example.app"] = "2.0"
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone")  # a listing, once
    world.phones.clear()

    plan = plan_of(world, ["example"])["example"]

    assert plan["blocked_reason"] == "Example iPhone isn't connected: it'll update when it is"
    assert plan["store"]["to"] == "2.0"
    assert (plan["install"], plan["actionable"], plan["nothing_to_do"]) == (False, False, False)


def test_every_app_is_looked_up_in_one_request_per_store(world, monkeypatch) -> None:
    monkeypatch.setattr(host, "mac_region", lambda: None)

    plan_of(world, ["example", "bravo"])

    assert world.lookups == [(["com.example.app", "com.bravo.app"], "US")]


def test_the_quick_plan_is_likely_and_names_the_phone_from_memory(world) -> None:
    world.store["com.example.app"] = "2.0"
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone",
                          livecontainer_bundle_id="com.kdt.livecontainer")

    plan = plan_of(world, ["example"], quick=True)["example"]

    assert plan["readiness"] == "likely"
    assert plan["targets"][0]["device_name"] == "Example iPhone"
    assert plan["targets"][0]["readiness"] == "likely"
    assert world.sessions == [], "no session to the phone"


def test_the_quick_plan_shows_a_remembered_developer_mode_off(world) -> None:
    set_app_options(world.cfg_path, "example", delivery="sideload")
    world.profile(DEVELOPMENT)
    world.store["com.example.app"] = "2.0"
    devices_seen.remember(world.cfg.state_dir, PHONE, name="Example iPhone", developer_mode=False)

    plan = plan_of(world, ["example"], quick=True)["example"]

    assert plan["blocked_reason"] is None
    assert plan["readiness_note"] == "Developer Mode was off on Example iPhone when Graft last checked"


def test_an_unknown_app_is_refused_before_anything_is_asked(world) -> None:
    with pytest.raises(ConfigError):
        plan_of(world, ["nope"])
    assert world.lookups == []


# --- in Terminal -------------------------------------------------------------------


def _plans(world, names):
    from graft_ipa import cli as cli_mod

    cfg = world.cfg
    return update_plan.plan(cfg, names, quick=False, with_affected=False,
                            probe=FullProbe(cfg.state_dir), tweak_records=cli_mod._collect_tweak_records,
                            delivery=cli_mod._delivery_targets(cfg))


def test_the_human_line_says_what_would_change(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.published["com.example.atweak"] = "1.1"

    assert update_plan.describe(_plans(world, ["example"])[0]) == (
        "Example+: App Store 2.0 · A Tweak 1.0 → 1.1 · also used by bravo: it will need an install "
        "afterward · rebuild & install on Example iPhone"
    )


@pytest.mark.parametrize("why", ["held", "intel", "refused"])
def test_a_newer_version_that_wont_be_got_says_why_not_up_to_date(world, monkeypatch, why) -> None:
    world.store["com.example.app"] = "2.0"
    if why == "held":
        set_app_options(world.cfg_path, "example", preferred_version="1.0")
        reason = "held at 1.0"
    elif why == "intel":
        monkeypatch.setattr(host, "apple_silicon_blocker", lambda: host.INTEL_MAC)
        reason = update_plan.NEEDS_APPLE_SILICON
    else:
        update_refusals.remember(world.cfg.state_dir, world.cfg.get_app("example"), "2.0")
        reason = "Your Apple ID doesn't have 2.0; get it in the App Store"

    assert update_plan.describe(_plans(world, ["example"])[0]) == (
        f"Example+: App Store has 2.0 ({reason})")


def test_up_to_date_is_said_only_when_it_is(world) -> None:
    assert update_plan.describe(_plans(world, ["example"])[0]) == "Example+: up to date"


def test_the_apps_vocabulary_matches_the_cli() -> None:
    """`CLIContract.Update`'s literals are the CLI's own words."""
    from graft_ipa import update_readiness, update_run

    text = CONTRACT.read_text()
    pairs = {
        "stepTweaks": update_plan.STEP_TWEAKS, "stepGet": update_plan.STEP_GET,
        "stepBuild": update_plan.STEP_BUILD, "statusStart": update_run.START,
        "statusEnd": update_run.END, "statusSkipped": update_run.SKIPPED,
        "readinessChecked": update_readiness.CHECKED, "readinessLikely": update_readiness.LIKELY,
        "needsAppleSilicon": update_plan.NEEDS_APPLE_SILICON,
        "notInstalledYet": update_plan.NOT_INSTALLED_YET,
        "notStarted": update_run.NOT_STARTED,
        "refusedPrefix": update_plan.REFUSED.split("{", 1)[0],
    }
    for name, value in pairs.items():
        assert re.search(rf'static let {name} = "{re.escape(value)}"', text), name


# --- every device the app is on (spec §11.3, option B) ------------------------------


def _on_both(world, *, connected: bool = True) -> None:
    """`example` delivered to the iPad too, with the same build."""
    world.deliver("example", udid=OTHER_PHONE)
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")
    if connected:
        world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")


def _targets(plan: dict) -> dict[str, dict]:
    return {t["udid"]: t for t in plan["targets"]}


def test_every_device_is_a_target_default_first_then_by_name(world) -> None:
    from graft_ipa.config import set_settings

    _on_both(world)
    world.store["com.example.app"] = "2.0"

    assert [t["udid"] for t in plan_of(world, ["example"])["example"]["targets"]] == [PHONE, OTHER_PHONE]
    set_settings(world.cfg_path, default_device_udid=OTHER_PHONE)
    assert [t["udid"] for t in plan_of(world, ["example"])["example"]["targets"]] == [OTHER_PHONE, PHONE]
    set_settings(world.cfg_path, default_device_udid="")
    assert [t["device_name"] for t in plan_of(world, ["example"])["example"]["targets"]] == [
        "Example iPad", "Example iPhone"], "by name"


def test_a_store_step_installs_on_every_target(world) -> None:
    _on_both(world)
    world.store["com.example.app"] = "2.0"

    targets = _targets(plan_of(world, ["example"])["example"])

    assert targets[PHONE]["install"] is True and targets[OTHER_PHONE]["install"] is True


def test_only_the_device_that_isnt_current_installs(world) -> None:
    """The iPhone has the newest build; the iPad doesn't."""
    _on_both(world)
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.deliver("example")

    plan = plan_of(world, ["example"])["example"]

    targets = _targets(plan)
    assert (targets[PHONE]["install"], targets[OTHER_PHONE]["install"]) == (False, True)
    assert (plan["install"], plan["actionable"], plan["nothing_to_do"]) == (True, True, False)
    assert targets[PHONE]["waiting"] is None and targets[PHONE]["blocked_reason"] is None


def test_a_target_that_isnt_connected_waits_and_the_other_installs(world) -> None:
    _on_both(world, connected=False)
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"])["example"]

    targets = _targets(plan)
    assert targets[PHONE]["install"] is True
    assert targets[OTHER_PHONE]["install"] is False
    assert targets[OTHER_PHONE]["waiting"] == "Example iPad isn't connected: it'll update when it is"
    assert targets[OTHER_PHONE]["blocked_reason"] is None
    assert (plan["blocked_reason"], plan["actionable"], plan["install"]) == (None, True, True)


def test_waiting_only_work_is_not_actionable(world) -> None:
    """The iPhone is current; only the unplugged iPad has anything to do."""
    _on_both(world, connected=False)
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.deliver("example")

    plan = plan_of(world, ["example"])["example"]

    assert plan["actionable"] is False
    assert plan["install"] is False
    assert plan["nothing_to_do"] is False, "waiting work is still work"
    assert plan["blocked_reason"] == "Example iPad isn't connected: it'll update when it is"


def test_a_current_target_that_isnt_connected_isnt_waiting(world) -> None:
    _on_both(world, connected=False)
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.deliver("example", udid=OTHER_PHONE)

    targets = _targets(plan_of(world, ["example"])["example"])

    assert targets[OTHER_PHONE]["waiting"] is None, "nothing to wait for"
    assert targets[PHONE]["install"] is True


def test_a_blocked_target_doesnt_install_and_the_other_runs(world) -> None:
    _on_both(world)
    world.phones[OTHER_PHONE].installed = ["com.apple.mobilesafari"]
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"])["example"]

    targets = _targets(plan)
    assert "LiveContainer" in targets[OTHER_PHONE]["blocked_reason"]
    assert targets[OTHER_PHONE]["install"] is False
    assert targets[PHONE]["install"] is True
    assert (plan["blocked_reason"], plan["actionable"]) == (None, True)


def test_an_app_with_no_target_that_can_run_is_blocked_naming_each(world) -> None:
    _on_both(world)
    world.phones[OTHER_PHONE].installed = ["com.apple.mobilesafari"]
    del world.phones[PHONE]
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"])["example"]

    targets = _targets(plan)
    assert plan["blocked_reason"] == "; ".join(
        [targets[PHONE]["waiting"], targets[OTHER_PHONE]["blocked_reason"]])
    assert (plan["install"], plan["actionable"], plan["nothing_to_do"]) == (False, False, False)


def test_a_reinstall_on_one_device_is_offered_there_alone(world) -> None:
    _on_both(world)
    world.deliver("example", udid=OTHER_PHONE, origin="reinstall")

    plan = plan_of(world, ["example"])["example"]

    targets = _targets(plan)
    assert (targets[OTHER_PHONE]["manual_install"], targets[OTHER_PHONE]["install_skipped_reason"]) == (
        True, update_plan.REINSTALLED)
    assert (targets[PHONE]["manual_install"], targets[PHONE]["install"]) == (False, False)
    assert (plan["manual_install"], plan["install_skipped_reason"]) == (True, update_plan.REINSTALLED)
    assert plan["actionable"] is False, "spec §11.1: no badge"
    assert world.sessions == [], "not an update to check readiness for"


def test_a_reinstall_on_one_device_and_a_change_on_the_other(world) -> None:
    _on_both(world)
    world.deliver("example", udid=OTHER_PHONE, origin="reinstall")
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"

    plan = plan_of(world, ["example"])["example"]

    targets = _targets(plan)
    assert targets[PHONE]["install"] is True and targets[OTHER_PHONE]["install"] is False
    assert targets[OTHER_PHONE]["manual_install"] is True
    assert (plan["install"], plan["manual_install"], plan["install_skipped_reason"]) == (True, True, None)
    assert plan["actionable"] is True


def test_a_dropped_device_is_not_a_target(world) -> None:
    from graft_ipa import cli as cli_mod

    _on_both(world)
    cli_mod._drop_update_target(world.cfg, "example", OTHER_PHONE)
    world.store["com.example.app"] = "2.0"

    assert [t["udid"] for t in plan_of(world, ["example"])["example"]["targets"]] == [PHONE]


def test_an_app_dropped_from_every_device_is_out_of_update(world) -> None:
    from graft_ipa import cli as cli_mod

    cli_mod._drop_update_target(world.cfg, "example", PHONE)
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"])["example"]

    assert plan["targets"] == []
    assert plan["install_skipped_reason"] == update_plan.DROPPED_EVERYWHERE
    assert (plan["install"], plan["actionable"]) == (False, False)


def test_an_app_delivered_only_without_a_device_says_build_once(world) -> None:
    from graft_ipa.config import set_settings

    add_app(world.cfg_path, "delta", {"bundle_id": "com.delta.app", "delivery": "livecontainer", "tweaks": []})
    world.stage("delta", "1.0")
    world.deliver("delta", udid=None)
    set_settings(world.cfg_path, default_device_udid="")
    world.store["com.delta.app"] = "2.0"

    plan = plan_of(world, ["delta"])["delta"]

    assert plan["install_skipped_reason"] == (
        "Build & Install delta once, so Graft knows which device it's on")
    assert plan["targets"] == [] and plan["actionable"] is False


def test_a_quick_plan_waits_per_device_from_memory(world) -> None:
    _on_both(world, connected=False)
    world.store["com.example.app"] = "2.0"

    plan = plan_of(world, ["example"], quick=True)["example"]

    targets = _targets(plan)
    assert targets[OTHER_PHONE]["waiting"] == "Example iPad isn't connected: it'll update when it is"
    assert targets[PHONE]["readiness"] == "likely"
    assert world.sessions == []


def test_the_human_line_names_each_device(world) -> None:
    _on_both(world, connected=False)
    world.store["com.example.app"] = "2.0"

    assert update_plan.describe(_plans(world, ["example"])[0]) == (
        "Example+: App Store 2.0 · rebuild & install on Example iPhone · "
        "Example iPad isn't connected: it'll update when it is"
    )


def test_the_human_line_names_a_blocked_device(world) -> None:
    _on_both(world)
    world.phones[OTHER_PHONE].installed = []
    world.store["com.example.app"] = "2.0"

    assert update_plan.describe(_plans(world, ["example"])[0]) == (
        "Example+: App Store 2.0 · rebuild & install on Example iPhone · blocked on Example iPad: "
        "Example iPad listed no apps: unlock it and keep it on the home screen"
    )
