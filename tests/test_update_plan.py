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

from .update_fakes import DEVELOPMENT, OTHER_PHONE, PHONE, REPO_URL, FakePhone, confirm_keys, deb, plan_of

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


def _hold_by_build(world, version: str, version_id: str) -> None:
    import yaml

    raw = yaml.safe_load(world.cfg_path.read_text())
    raw["apps"]["example"].update(preferred_version=version,
                                  preferred_build={"version": version, "version_id": version_id})
    world.cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))


def test_a_held_build_whose_file_is_gone_says_it_isnt_downloaded(world, monkeypatch) -> None:
    """T2-B8: "held at 1.2" alone, with nothing to build from, read as if
    the hold were the only thing in the way."""
    from graft_ipa import appstore

    _hold_by_build(world, "1.2", "800200")
    monkeypatch.setattr(appstore, "held_build",
                        lambda cfg, app: appstore.HeldBuild(number=1, count=2))

    step = _step(world, "example", StoreAnswer("1.2"))

    assert step.from_version is None, "premise: the held build's file isn't staged"
    assert step.skipped_reason == (
        "held at 1.2 (build 1 of 2), which isn't downloaded: "
        "get it again from Get an older version…"
    )
    assert not step.planned


def test_a_held_version_whose_file_is_gone_says_it_isnt_downloaded(world) -> None:
    set_app_options(world.cfg_path, "example", preferred_version="1.5")

    step = _step(world, "example", StoreAnswer("2.0"))

    assert step.skipped_reason == (
        "held at 1.5, which isn't downloaded: get it again from Get an older version…")


def test_without_apple_silicon_the_newer_version_shows_and_is_not_got(world, monkeypatch) -> None:
    monkeypatch.setattr(host, "apple_silicon_blocker", lambda: host.INTEL_MAC)

    step = _step(world, "example", StoreAnswer("2.0"))

    assert (step.to_version, step.skipped_reason) == ("2.0", update_plan.NEEDS_APPLE_SILICON)


def test_without_decryption_set_up_the_newer_version_shows_and_is_not_got(world) -> None:
    """B5: Update & Install before the App Store's setup failed at the get,
    with the enrollment's Terminal steps. The plan says it can't, and why."""
    (world.tools.models / "enrollment.bin").unlink()

    step = _step(world, "example", StoreAnswer("2.0"))

    assert (step.to_version, step.skipped_reason) == ("2.0", update_plan.DECRYPTION_NOT_SET_UP)
    assert not step.planned


def test_without_unfairplay_the_newer_version_is_not_got(world) -> None:
    world.tools.unfairplay.unlink()

    step = _step(world, "example", StoreAnswer("2.0"))

    assert step.skipped_reason == update_plan.DECRYPTION_NOT_SET_UP


def _bundled_ipatool(monkeypatch) -> None:
    """Graft.app's own ipatool, as status would find it."""
    from graft_ipa import ipatool, tools

    monkeypatch.setattr(ipatool, "source_of", lambda configured, *, explicit: tools.SOURCE_BUNDLED)


def test_graft_s_ipatool_never_signed_in_is_not_got(world, monkeypatch) -> None:
    """Read from Graft's own record of its sign-in, not the keychain: the
    plan reads no keychain, and with Graft's ipatool a get needs the
    sign-in that record vouches for."""
    _bundled_ipatool(monkeypatch)

    step = _step(world, "example", StoreAnswer("2.0"))

    assert (step.to_version, step.skipped_reason) == ("2.0", update_plan.NOT_SIGNED_IN)


def test_graft_s_ipatool_signed_in_is_got(world, monkeypatch) -> None:
    from graft_ipa import appstore_account

    _bundled_ipatool(monkeypatch)
    appstore_account._write_marker({
        "format": appstore_account.MARKER_FORMAT, "ipatool_source": "bundled",
        "bundled_items": {"account": "2026-11-26 10:00:00 +0000"},
    })

    assert _step(world, "example", StoreAnswer("2.0")).planned


def test_signed_out_with_graft_s_ipatool_is_not_got(world, monkeypatch) -> None:
    """A Sign Out takes `account` off the record and leaves the device item."""
    from graft_ipa import appstore_account

    _bundled_ipatool(monkeypatch)
    appstore_account._write_marker({
        "format": appstore_account.MARKER_FORMAT, "ipatool_source": "bundled",
        "bundled_items": {"anisette-v4": "2026-11-26 10:00:00 +0000"},
    })

    assert _step(world, "example", StoreAnswer("2.0")).skipped_reason == update_plan.NOT_SIGNED_IN


def test_another_ipatool_s_sign_in_is_its_own_business(world) -> None:
    """One set in config.yaml may be signed in in Terminal, which Graft has
    no record of: the get finds out, as before."""
    assert _step(world, "example", StoreAnswer("2.0")).planned


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
        "manual_install": False, "kind": "current", "connected": True, "waiting": None,
        "blocked_reason": None, "readiness": "checked", "readiness_note": None,
    }]
    assert (plan["manual_install"], plan["readiness_note"]) == (False, None)


def test_a_newer_tweak_is_listed_with_its_versions(world) -> None:
    world.published["com.example.btweak"] = "2.1"

    plan = plan_of(world, ["bravo"])["bravo"]

    assert plan["tweaks"] == [{"filename": "b-tweak.deb", "name": "B Tweak", "from": "2.0", "to": "2.1",
                               "unchecked": False, "also_affects": [], "source": REPO_URL,
                               "verified": True, "unverified_reason": None, "confirmed": True,
                               "confirm_key": confirm_keys(world, ["bravo"])["b-tweak.deb"],
                               "trust_changed": False}]
    assert plan["install"] is True
    assert plan["needs_confirmation"] is False


def test_a_tweak_that_couldnt_be_checked_is_listed_and_left_alone(world) -> None:
    world.unreachable.add("com.example.btweak")

    plan = plan_of(world, ["bravo"])["bravo"]

    assert plan["tweaks"] == [{"filename": "b-tweak.deb", "name": "B Tweak", "from": "2.0", "to": None,
                               "unchecked": True, "also_affects": [], "source": None,
                               "verified": False, "unverified_reason": None, "confirmed": False,
                               "confirm_key": None, "trust_changed": False}]
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


def _payload(world, names: list[str]) -> dict:
    from graft_ipa import cli as cli_mod

    cfg = world.cfg
    plans = update_plan.plan(cfg, names, quick=True, with_affected=False,
                             probe=FullProbe(cfg.state_dir),
                             tweak_records=cli_mod._collect_tweak_records,
                             delivery=cli_mod._delivery_targets(cfg))
    return update_plan.payload(plans, quick=True)


def _offline(world) -> None:
    world.lookup_error = "iTunes lookup request failed: offline"
    world.unreachable |= {"com.example.atweak", "com.example.btweak"}


def test_a_plan_whose_every_lookup_failed_says_the_checks_failed(world) -> None:
    """T2-A1: offline, the plan still says every app has nothing to do; the
    app mustn't take that for an answer."""
    _offline(world)

    payload = _payload(world, ["example", "bravo"])

    assert payload["checks_failed"] is True
    assert all(a["nothing_to_do"] for a in payload["apps"]), "premise: it looks like up to date"


def test_a_plan_that_got_its_answers_hasnt_failed(world) -> None:
    assert _payload(world, ["example", "bravo"])["checks_failed"] is False


@pytest.mark.parametrize("answered", ["store", "canister"])
def test_one_service_answering_is_an_answer(world, answered: str) -> None:
    _offline(world)
    if answered == "store":
        world.lookup_error = None
    else:
        world.unreachable.clear()

    assert _payload(world, ["example", "bravo"])["checks_failed"] is False


def test_not_on_the_store_is_an_answer_too(world) -> None:
    """A store that answered "no such app" was reached."""
    _offline(world)
    world.lookup_error = None
    world.store.clear()

    assert _payload(world, ["example"])["checks_failed"] is False


REPO = "https://repo.example.com"


def _from_the_repo(world, monkeypatch, *, fresh: bool) -> None:
    """Both library tweaks recorded as fetched from `REPO`, whose cached index
    (`updates.repo_offers`) carries them, `fresh` or past `CACHE_MAX_AGE`.
    `updates.statuses` then reads that index alone, and asks canister nothing."""
    from graft_ipa import tweak_sources, updates
    from graft_ipa.tweak_sources import REPO as KIND
    from graft_ipa.tweak_sources import Source, file_sha256
    from graft_ipa.updates import Download, Offer

    cfg = world.cfg
    source = Source(KIND, REPO, REPO)
    offers: dict[str, list[Offer]] = {}
    for filename, package_id, version in (("a-tweak.deb", "com.example.atweak", "1.0"),
                                          ("b-tweak.deb", "com.example.btweak", "2.0")):
        sha = file_sha256(cfg.tweaks_dir / filename)
        tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, filename, source, sha)
        build = Download(f"{REPO}/debs/{filename}", "0" * 64)
        offers[package_id] = [Offer(source, version, (build,), frozenset({sha}), fresh=fresh)]
    monkeypatch.setattr(updates, "repo_offers", lambda _cfg, max_age=updates.CACHE_MAX_AGE: offers)


def test_a_cached_repo_index_is_no_sign_of_being_online(world, monkeypatch) -> None:
    """A tweak its own repo's recently fetched index answered was never asked
    online, so it says nothing about the network."""
    world.lookup_error = "iTunes lookup request failed: offline"
    _from_the_repo(world, monkeypatch, fresh=True)

    payload = _payload(world, ["example", "bravo"])

    assert world.latest_asked == [], "premise: canister wasn't asked"
    assert payload["checks_failed"] is True


def test_a_stale_repo_index_is_no_sign_of_being_offline(world, monkeypatch) -> None:
    """Past `CACHE_MAX_AGE` a repo-sourced tweak is unchecked, and that's the
    index's age, not the network: nothing was asked online. Here nothing else
    is asked either (an app of Apple's own isn't looked up), so nothing
    failed."""
    _from_the_repo(world, monkeypatch, fresh=False)
    add_app(world.cfg_path, "apple", {"bundle_id": "com.apple.example", "delivery": "livecontainer",
                                      "tweaks": ["a-tweak.deb"]})

    payload = _payload(world, ["apple"])

    assert payload["apps"][0]["tweaks"][0]["unchecked"] is True, "premise: the stale index"
    assert world.latest_asked == [] and world.lookups == [], "premise: nothing was asked online"
    assert payload["checks_failed"] is False


def test_a_plan_that_asks_nothing_online_hasnt_failed(world) -> None:
    """Apple's own apps aren't looked up, and a tweak with no package id
    isn't either: nothing asked is nothing failed."""
    _offline(world)
    add_app(world.cfg_path, "apple", {"bundle_id": "com.apple.example", "delivery": "livecontainer",
                                      "tweaks": []})

    assert _payload(world, ["apple"])["checks_failed"] is False


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
        "decryptionNotSetUp": update_plan.DECRYPTION_NOT_SET_UP,
        "notSignedIn": update_plan.NOT_SIGNED_IN,
        "lookupFailed": update_plan.LOOKUP_FAILED,
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


# --- 2.3.1 A1: a tweak update whose source isn't verified ------------------------

INSECURE = REPO_URL.replace("https://", "http://")


def test_an_unverified_tweak_is_shown_and_waits_for_the_person(world) -> None:
    """Shown, with where it comes from and why it isn't verified, and not
    counted: alone it's nothing to do, so Update All and `--all` leave it."""
    world.published["com.example.btweak"] = "2.1"
    world.serve_insecure("com.example.btweak")

    plan = plan_of(world, ["bravo"])["bravo"]

    (tweak,) = plan["tweaks"]
    assert (tweak["to"], tweak["source"], tweak["verified"], tweak["confirmed"]) == \
        ("2.1", INSECURE, False, False)
    assert tweak["unverified_reason"] == f"{INSECURE} doesn't use HTTPS"
    assert plan["needs_confirmation"] is True
    assert (plan["nothing_to_do"], plan["actionable"], plan["install"]) == (True, False, False)
    assert [t["install"] for t in plan["targets"]] == [False]


def test_the_person_s_ok_plans_it(world) -> None:
    world.published["com.example.btweak"] = "2.1"
    world.serve_insecure("com.example.btweak")

    keys = confirm_keys(world, ["bravo"])

    plan = plan_of(world, ["bravo"], trusted=keys)["bravo"]

    (tweak,) = plan["tweaks"]
    assert (tweak["verified"], tweak["confirmed"], tweak["trust_changed"]) == (False, True, False)
    assert tweak["confirm_key"] == keys["b-tweak.deb"]
    assert tweak["confirm_key"].startswith("b-tweak.deb@") and len(tweak["confirm_key"]) == len("b-tweak.deb@") + 64
    assert (plan["needs_confirmation"], plan["actionable"], plan["install"]) == (False, True, True)


def test_an_ok_counts_only_for_what_was_confirmed(world) -> None:
    """Re-verify #2: the person said yes to 2.1 from this source. A plan made
    after it moved on (another version, another build, another source) isn't
    confirmed by that yes, and says so (`trust_changed`)."""
    world.published["com.example.btweak"] = "2.1"
    world.serve_insecure("com.example.btweak")
    keys = confirm_keys(world, ["bravo"])
    world.published["com.example.btweak"] = "9.9"

    plan = plan_of(world, ["bravo"], trusted=keys)["bravo"]

    (tweak,) = plan["tweaks"]
    assert (tweak["to"], tweak["confirmed"], tweak["trust_changed"]) == ("9.9", False, True)
    assert tweak["confirm_key"] != keys["b-tweak.deb"]
    assert (plan["needs_confirmation"], plan["actionable"]) == (True, False)


def test_another_tweak_s_ok_doesn_t_count(world) -> None:
    world.published["com.example.btweak"] = "2.1"
    world.serve_insecure("com.example.btweak")

    plan = plan_of(world, ["bravo"], trusted={"a-tweak.deb": "a-tweak.deb@" + "0" * 64})["bravo"]

    assert plan["needs_confirmation"] is True and plan["actionable"] is False


def test_with_affected_doesn_t_reach_through_an_unconfirmed_tweak(world) -> None:
    world.published["com.example.atweak"] = "1.1"
    world.serve_insecure("com.example.atweak")

    plans = plan_of(world, ["example"], with_affected=True)

    assert list(plans) == ["example"]


def test_the_terminal_line_says_what_needs_the_person_s_ok(world) -> None:
    from graft_ipa import cli as cli_mod

    world.published["com.example.btweak"] = "2.1"
    world.serve_insecure("com.example.btweak")
    cfg = world.cfg
    (plan,) = update_plan.plan(cfg, ["bravo"], quick=False, with_affected=False,
                               probe=FullProbe(cfg.state_dir),
                               tweak_records=cli_mod._collect_tweak_records,
                               delivery=cli_mod._delivery_targets(cfg))

    (change,) = plan.tweaks
    assert update_plan.describe(plan) == (
        f"bravo: B Tweak 2.0 → 2.1 needs your OK ({INSECURE} doesn't use HTTPS): "
        f"--trust-tweak {change.confirm_key}"
    )


# --- every target's local facts, for the app's offline merge ----------------------


def test_every_target_says_its_delivery_state_and_whether_its_device_is_connected(world) -> None:
    """Readiness (`install`, `waiting`) is worked out only for a target with
    work, and an App Store update gives every target work, so those two say
    as much about the network as about the device. `kind` and `connected`
    are the device's alone, for every target: what the app compares to tell
    an offline plan's local change (`AppUpdatesStore.merged`)."""
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")
    world.deliver("example", udid=OTHER_PHONE)  # the iPad isn't plugged in

    targets = {t["udid"]: t for t in plan_of(world, ["example"])["example"]["targets"]}

    assert (targets[PHONE]["kind"], targets[PHONE]["connected"]) == ("current", True)
    assert (targets[OTHER_PHONE]["kind"], targets[OTHER_PHONE]["connected"]) == ("current", False)
    assert targets[OTHER_PHONE]["waiting"] is None, "premise: no work, so no readiness was asked"


def test_a_target_whose_build_changed_since_says_stale(world) -> None:
    world.stage("example", "2.0")

    (target,) = plan_of(world, ["example"])["example"]["targets"]

    assert (target["kind"], target["connected"]) == ("stale", True)
