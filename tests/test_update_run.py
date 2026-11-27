"""`graft --update`'s run (spec 3.2): tweaks once before any build, then one
app at a time, its get through the download-and-stage path, then its build,
on the device it was last delivered to."""

from __future__ import annotations

import json

import pytest
import yaml
from typer.testing import CliRunner

from graft_ipa import appstore, ipatool, update_plan, update_readiness, update_refusals, update_run
from graft_ipa import cli as cli_mod
from graft_ipa import config as config_mod
from graft_ipa.canister import CanisterError
from graft_ipa.config import add_app, set_app_options
from graft_ipa.errors import GraftError
from graft_ipa.ipa_cache import staged_name

from .appstore_fakes import make_ipa
from .update_fakes import DEVELOPMENT, PHONE, confirm_keys, emitter, events, new_name, pipeline, plan_of


def _run(world, names, *, all_apps=False, with_affected=False, trusted=None):
    em, out = emitter()
    cfg = world.cfg
    code = update_run.run(cfg, names, all_apps=all_apps, with_affected=with_affected,
                          probe=update_readiness.FullProbe(cfg.state_dir),
                          pipeline=pipeline(world, em), emitter=em, trusted=trusted or {})
    return code, events(out.getvalue())


def _of(evs, kind):
    return [e for e in evs if e["type"] == kind]


def _index(evs, **match):
    return next(i for i, e in enumerate(evs) if all(e.get(k) == v for k, v in match.items()))


def test_a_get_then_a_build_and_one_result_each(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")

    code, evs = _run(world, ["example"])

    assert code == 0
    assert [(e["app"], e["step"], e["status"]) for e in _of(evs, "update-step")] == [
        (None, "tweaks", "skipped"),
        ("example", "get", "start"), ("example", "get", "end"),
        ("example", "build", "start"),
        ("example", "install", "start"), ("example", "install", "end"),
        ("example", "build", "end"),
    ]
    assert _of(evs, "ipa-provided")[0]["version"] == "2.0", "the get's own events pass through"
    assert world.build.calls == [("example", PHONE, ["a-tweak.deb"], "2.0")], "to its own device"
    (end,) = _of(evs, "update-app-end")
    assert (end["ok"], end["done"], end["left"], end["error"]) == (True, ["get", "build"], [], None)
    (summary,) = _of(evs, "update-end")
    assert summary == {"type": "update-end", "ok": True, "stopped": False, "updated": ["example"],
                       "failed": [], "blocked": [], "not_started": [], "waiting": 0}
    assert end["targets"] == [{"udid": PHONE, "device_name": "Example iPhone", "ok": True,
                               "error": None, "left": [], "waiting": False}]
    assert evs[0]["type"] == "update-plan", "the plan comes first"


def test_tweaks_are_updated_once_before_any_build(world) -> None:
    world.published["com.example.atweak"] = "1.1"

    code, evs = _run(world, ["example", "bravo"])

    assert code == 0
    assert len(world.fetches) == 1, "a shared tweak is downloaded once"
    tweaks_end = _index(evs, type="update-step", step="tweaks", status="end")
    first_build = _index(evs, type="update-step", step="build", status="start")
    assert tweaks_end < first_build
    assert [c[0] for c in world.build.calls] == ["example", "bravo"], "one app at a time, in order"
    assert [c[2][0] for c in world.build.calls] == [new_name("com.example.atweak", "1.1")] * 2, "built re-pointed"


def test_one_app_ends_before_the_next_starts(world) -> None:
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.stage("bravo", "5.1")
    world.store["com.bravo.app"] = "5.1"

    _code, evs = _run(world, ["bravo", "example"])

    assert _index(evs, type="update-app-end", app="bravo") < _index(
        evs, type="update-step", app="example", step="build", status="start")


def test_an_app_the_apple_id_doesnt_have_stops_at_the_get_and_the_next_runs(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.tools.set(download_error="license is required")
    world.stage("bravo", "5.1")
    world.store["com.bravo.app"] = "5.1"

    code, evs = _run(world, ["example", "bravo"])

    assert code == 1
    example = next(e for e in _of(evs, "update-app-end") if e["app"] == "example")
    assert example["error"] == update_run.DOESNT_HAVE
    assert example["left"] == ["get", "build"]
    assert [r["reason"] for r in _of(evs, "appstore-refused")] == ["needs-license"]
    assert [c[0] for c in world.build.calls] == ["bravo"]
    cfg = world.cfg
    assert update_refusals.refused_version(cfg.state_dir, cfg.get_app("example")) == "2.0"
    assert plan_of(world, ["example"])["example"]["store"]["skipped_reason"] == (
        "Your Apple ID doesn't have 2.0; get it in the App Store"), "shown by the next plan"


def test_a_blocked_app_is_reported_and_the_others_run(world) -> None:
    set_app_options(world.cfg_path, "example", delivery="sideload")
    world.profile(DEVELOPMENT)
    world.phones[PHONE].developer_mode = False
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.stage("bravo", "5.1")
    world.store["com.bravo.app"] = "5.1"

    code, evs = _run(world, ["example", "bravo"])

    assert code == 1
    assert _of(evs, "update-end")[0]["blocked"] == ["example"]
    assert [c[0] for c in world.build.calls] == ["bravo"]


def test_all_covers_only_the_apps_with_something_to_update(world) -> None:
    add_app(world.cfg_path, "delta", {"bundle_id": "com.delta.app", "delivery": "livecontainer", "tweaks": []})
    world.store["com.delta.app"] = "3.0"
    world.stage("bravo", "5.1")
    world.store["com.bravo.app"] = "5.1"

    code, evs = _run(world, ["bravo", "delta", "example"], all_apps=True)

    assert code == 0, "an app never delivered isn't counted in Update All"
    assert [e["app"] for e in _of(evs, "update-app-end")] == ["bravo"]


def test_all_leaves_an_install_that_is_only_offered_to_the_person(world) -> None:
    """Spec §11.1: a Re-install isn't installed over, nor counted."""
    world.deliver("example", origin="reinstall")

    code, evs = _run(world, ["bravo", "example"], all_apps=True)

    assert code == 0
    assert _of(evs, "update-app-end") == []
    assert world.build.calls == []


def test_a_named_app_never_delivered_is_blocked_with_not_installed_yet(world) -> None:
    add_app(world.cfg_path, "delta", {"bundle_id": "com.delta.app", "delivery": "livecontainer", "tweaks": []})
    world.store["com.delta.app"] = "3.0"

    code, evs = _run(world, ["delta"])

    assert code == 1
    (end,) = _of(evs, "update-app-end")
    assert end["error"] == update_plan.NOT_INSTALLED_YET
    assert world.build.calls == []


def test_a_shared_tweak_leaves_the_other_app_stale(world) -> None:
    world.published["com.example.atweak"] = "1.1"

    _run(world, ["example"])

    assert world.kind("bravo") == "stale"


def test_with_affected_installs_the_other_app_too(world) -> None:
    world.published["com.example.atweak"] = "1.1"

    code, _evs = _run(world, ["example"], with_affected=True)

    assert code == 0
    assert [c[0] for c in world.build.calls] == ["example", "bravo"]
    assert world.kind("bravo") == "current"


def test_a_tweak_that_fails_leaves_its_app_to_try_again(world) -> None:
    world.published["com.example.atweak"] = "1.1"
    world.fetch_error = CanisterError("download failed: 503")

    code, evs = _run(world, ["example"])

    assert code == 1
    (end,) = _of(evs, "update-app-end")
    assert end["left"] == ["tweaks", "build"]
    assert "download failed: 503" in end["error"]
    assert world.build.calls == [], "nothing else changed, so nothing to build"
    assert any(e.get("source") == "warning" for e in _of(evs, "log"))


def test_a_failed_build_says_what_is_downloaded_and_what_is_left(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")
    world.build.fail["example"] = GraftError("cyan exited with code 1")

    code, evs = _run(world, ["example"])

    assert code == 1
    (end,) = _of(evs, "update-app-end")
    assert end["done"] == ["get"] and end["left"] == ["build"]
    assert end["error"] == "2.0 is downloaded; the build failed: cyan exited with code 1"


def test_update_through_the_cli_exits_by_its_result(world, monkeypatch) -> None:
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    monkeypatch.setattr(cli_mod, "_build_one", lambda **kw: world.build(
        cfg=kw["cfg"], app_name=kw["app_name"], device=kw["device"], emitter=kw["emitter"],
        built=kw["outcome"]))

    result = CliRunner().invoke(cli_mod.app, ["--json", "--update", "example"])

    assert result.exit_code == 0, result.output
    kinds = [json.loads(line)["type"] for line in result.stdout.splitlines() if line.startswith("{")]
    assert kinds[-1] == "update-end"


def test_a_failed_tweak_never_leaves_a_reinstall_to_be_installed_over(world) -> None:
    """The tweak was the only real change (spec §11.1): with it failed, a
    Re-installed build stays, though its delivery state is stale."""
    world.deliver("example", origin="reinstall")
    world.published["com.example.atweak"] = "1.1"
    world.fetch_error = CanisterError("download failed: 503")

    code, evs = _run(world, ["example"])

    assert code == 1
    assert world.build.calls == []
    (end,) = _of(evs, "update-app-end")
    assert end["left"] == ["tweaks", "build"]


def test_a_failed_tweak_still_installs_a_change_staged_since_the_last_install(world) -> None:
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.published["com.example.atweak"] = "1.1"
    world.fetch_error = CanisterError("download failed: 503")

    code, evs = _run(world, ["example"])

    assert code == 1, "the tweak is still to do"
    assert [c[0] for c in world.build.calls] == ["example"]
    (end,) = _of(evs, "update-app-end")
    assert (end["done"], end["left"]) == (["build"], ["tweaks"])



# --- resume, holds and claims: what an update must never do (spec 3.3, 3.6) ---


def _forbid(reached: list[str], what: str):
    """A stand-in that records `what` and fails the test from anywhere:
    `pytest.fail` is a BaseException, so the run's `except Exception` around
    each step can't swallow it (review #2)."""

    def stand_in(*_a, **_k):
        reached.append(what)
        pytest.fail(f"an update reached {what}")

    return stand_in


def test_a_failed_tweak_update_resumes_at_the_tweak(world) -> None:
    world.published["com.example.atweak"] = "1.1"
    world.fetch_error = CanisterError("download failed: 503")
    _run(world, ["example"])
    world.fetch_error = None

    code, evs = _run(world, ["example"])

    assert code == 0
    assert _of(evs, "update-app-end")[0]["done"] == ["tweaks", "build"]
    assert [c[2][0] for c in world.build.calls] == [new_name("com.example.atweak", "1.1")]


def test_a_failed_get_resumes_at_the_get(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")
    world.tools.set(download_error="received error: something went wrong")
    _run(world, ["example"])
    world.tools.set(download_error=None)

    code, evs = _run(world, ["example"])

    assert code == 0
    assert _of(evs, "update-app-end")[0]["done"] == ["get", "build"]


def test_a_failed_build_resumes_at_the_build_and_downloads_nothing_again(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")
    world.build.fail["example"] = GraftError("cyan exited with code 1")
    _run(world, ["example"])
    assert world.downloads() == 1

    code, evs = _run(world, ["example"])

    assert code == 0
    plan = _of(evs, "update-plan")[0]["apps"][0]
    assert plan["store"]["to"] is None and plan["install"] is True, "the IPA stayed staged"
    assert world.downloads() == 1
    assert _of(evs, "update-app-end")[0]["done"] == ["build"]


def test_a_hold_is_never_released(world, monkeypatch) -> None:
    """Held by build (Plan C): the hold, its build and its file stay."""
    reached: list[str] = []
    for target, name in ((appstore, "hold"), (appstore, "release_hold"),
                         (appstore, "swap_preferred_version"), (config_mod, "swap_preferred_version")):
        monkeypatch.setattr(target, name, _forbid(reached, f"{target.__name__}.{name}"))
    raw = yaml.safe_load(world.cfg_path.read_text())
    raw["apps"]["example"].update(preferred_version="1.0",
                                  preferred_build={"version": "1.0", "version_id": "123456789"})
    world.cfg_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    make_ipa(world.cfg.cache_dir / staged_name("com.example.app", "1.0", "123456789"),
             bundle_id="com.example.app", version="1.0", encrypted=False)
    world.store["com.example.app"] = "2.0"
    world.published["com.example.atweak"] = "1.1"

    code, evs = _run(world, ["example"])

    assert reached == []
    assert code == 0, "a held app still gets its tweak updates and install"
    held = world.cfg.apps["example"]
    assert (held.preferred_version, held.preferred_version_id) == ("1.0", "123456789")
    assert world.downloads() == 0
    assert _of(evs, "update-app-end")[0]["done"] == ["tweaks", "build"]


@pytest.mark.parametrize("refused", [False, True])
def test_nothing_is_claimed_or_bought(world, monkeypatch, refused: bool) -> None:
    reached: list[str] = []
    monkeypatch.setattr(appstore, "claim", _forbid(reached, "appstore.claim"))
    monkeypatch.setattr(ipatool, "purchase", _forbid(reached, "ipatool.purchase"))
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")
    if refused:
        world.tools.set(download_error="license is required")

    _run(world, ["example"])

    assert reached == []
    argvs = [c["argv"] for c in world.tools.calls("ipatool")]
    assert argvs, "the get did run ipatool"
    assert not [a for a in argvs if a[:1] == ["purchase"] or "--purchase" in a]


# --- 2.3.1 A1: an unverified tweak update needs the person's OK ------------------


@pytest.mark.parametrize("all_apps", [False, True])
def test_an_unverified_tweak_is_never_updated_without_the_person_s_ok(world, all_apps) -> None:
    world.published["com.example.atweak"] = "1.1"
    world.serve_insecure("com.example.atweak")
    before = world.cfg_path.read_text()

    code, evs = _run(world, [] if all_apps else ["example", "bravo"], all_apps=all_apps)

    assert code == 0
    assert world.fetches == [], "nothing downloaded"
    assert world.cfg_path.read_text() == before, "no app re-pointed"
    assert _of(evs, "update-app-end") == [], "nothing else to do, so neither app is run"
    assert world.build.calls == []


def test_with_the_person_s_ok_it_is_updated(world) -> None:
    from graft_ipa import tweak_sources

    world.published["com.example.atweak"] = "1.1"
    world.serve_insecure("com.example.atweak")

    code, evs = _run(world, ["example"], trusted=confirm_keys(world, ["example"]))

    assert code == 0
    new = new_name("com.example.atweak", "1.1")
    assert [c[2][0] for c in world.build.calls] == [new]
    cfg = world.cfg
    recorded = tweak_sources.recorded_for(cfg.state_dir, new, tweak_sources.file_sha256(cfg.tweaks_dir / new))
    assert recorded is not None and recorded.url.startswith("http://")


def test_trust_tweak_reaches_the_run_through_the_cli(world) -> None:
    world.published["com.example.atweak"] = "1.1"
    world.serve_insecure("com.example.atweak")

    key = confirm_keys(world, ["example"])["a-tweak.deb"]
    result = CliRunner().invoke(cli_mod.app, ["--json", "--update", "example", "--plan",
                                              "--trust-tweak", key])

    assert result.exit_code == 0, result.output
    (plan,) = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    (tweak,) = plan["apps"][0]["tweaks"]
    assert tweak["confirmed"] is True and plan["apps"][0]["actionable"] is True


def test_trust_tweak_takes_the_plan_s_confirm_key_not_a_filename(world) -> None:
    """A bare filename trusted whatever that file's update turned into by
    the time the run planned it (re-verify #2)."""
    result = CliRunner().invoke(cli_mod.app, ["--update", "example", "--plan", "--trust-tweak", "a-tweak.deb"])

    assert result.exit_code == 2
    assert "confirm_key" in result.output


def test_trust_tweak_must_name_a_library_tweak(world) -> None:
    """A typo would otherwise trust nothing, without a word."""
    result = CliRunner().invoke(cli_mod.app, ["--update", "example", "--plan",
                                              "--trust-tweak", "nope.deb@" + "0" * 64])

    assert result.exit_code == 15
    assert "nope.deb" in result.output


def test_an_update_that_changed_since_its_ok_is_refused_and_nothing_runs(world) -> None:
    """The re-verify's probe: the confirmation showed 1.1; by the run, the
    same tweak's update was 9.9. The person's yes was for 1.1, so the app
    fails, and nothing is downloaded, re-pointed or built for it."""
    world.published["com.example.atweak"] = "1.1"
    world.serve_insecure("com.example.atweak")
    keys = confirm_keys(world, ["example"])
    world.published["com.example.atweak"] = "9.9"
    before = world.cfg_path.read_text()

    code, evs = _run(world, ["example"], trusted=keys)

    assert code == 1
    assert world.fetches == [] and world.build.calls == []
    assert world.cfg_path.read_text() == before
    (end,) = _of(evs, "update-app-end")
    assert end["ok"] is False and end["app"] == "example"
    assert "changed since you confirmed it" in end["error"] and "9.9" in end["error"]
    assert _of(evs, "update-end")[0]["failed"] == ["example"]


def test_a_changed_ok_holds_back_only_the_apps_that_use_that_tweak(world) -> None:
    """b-tweak.deb is bravo's alone: example's App Store update still runs."""
    world.published["com.example.btweak"] = "2.1"
    world.serve_insecure("com.example.btweak")
    keys = confirm_keys(world, ["bravo"])
    world.published["com.example.btweak"] = "9.9"
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")

    code, evs = _run(world, ["example", "bravo"], trusted=keys)

    assert code == 1
    assert {e["app"]: e["ok"] for e in _of(evs, "update-app-end")} == {"example": True, "bravo": False}
    assert [c[0] for c in world.build.calls] == ["example"]


def test_trust_tweak_is_refused_beside_a_verb_that_ignores_it(world) -> None:
    result = CliRunner().invoke(cli_mod.app, ["--list", "--trust-tweak", "a-tweak.deb"])

    assert result.exit_code == 2
    assert "--trust-tweak" in result.output


def test_a_changed_ok_holds_back_the_app_s_other_work_too(world) -> None:
    """The person confirmed example's whole update: with one part of it
    changed, none of it runs (no get, no build), and it ends once."""
    world.published["com.example.atweak"] = "1.1"
    world.serve_insecure("com.example.atweak")
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")
    keys = confirm_keys(world, ["example"])
    world.published["com.example.atweak"] = "9.9"

    code, evs = _run(world, ["example"], trusted=keys)

    assert code == 1
    assert world.downloads() == 0 and world.build.calls == []
    (end,) = _of(evs, "update-app-end")
    assert end["ok"] is False and "changed since you confirmed it" in end["error"]
