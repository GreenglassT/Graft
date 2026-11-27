"""`graft --update` on every device the app is on (spec §11.3, option B): one
build per app, its own install on the first target, then the same archive on
each other target through the re-install path, recorded with origin `update`
and the build's inputs digest. A waiting or failing target never stops the
others; results come per target; `--update` again installs only where the
app isn't current."""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import devices_seen, update_plan, update_readiness, update_run
from graft_ipa.build_log import BuildLog
from graft_ipa.errors import GraftError, InstallError

from .update_fakes import OTHER_PHONE, PHONE, FakePhone, emitter, events, pipeline, plan_of


def _run(world, names, *, all_apps=False, real_install=False):
    em, out = emitter()
    cfg = world.cfg
    code = update_run.run(cfg, names, all_apps=all_apps, with_affected=False,
                          probe=update_readiness.FullProbe(cfg.state_dir),
                          pipeline=pipeline(world, em, real_install=real_install), emitter=em)
    return code, events(out.getvalue())


def _of(evs, kind):
    return [e for e in evs if e["type"] == kind]


def _steps(evs):
    return [(e["step"], e["status"], e["udid"]) for e in _of(evs, "update-step") if e["app"] == "example"]


def _on_both(world, *, connected: bool = True) -> None:
    world.deliver("example", udid=OTHER_PHONE)
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")
    if connected:
        world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")


def _due(world) -> None:
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")


def _targets(evs, app="example"):
    (end,) = [e for e in _of(evs, "update-app-end") if e["app"] == app]
    return {t["udid"]: t for t in end["targets"]}


def test_one_build_then_each_other_device(world) -> None:
    _on_both(world)
    _due(world)

    code, evs = _run(world, ["example"])

    assert code == 0
    assert [c[:2] for c in world.build.calls] == [("example", PHONE)], "one build, on the default"
    assert world.install.calls == [("example", OTHER_PHONE)], "the same build on the iPad"
    assert _steps(evs) == [
        ("get", "start", None), ("get", "end", None),
        ("build", "start", None),
        ("install", "start", PHONE), ("install", "end", PHONE),
        ("build", "end", None),
        ("install", "start", OTHER_PHONE), ("install", "end", OTHER_PHONE),
    ]
    step = next(e for e in _of(evs, "update-step") if e["step"] == "install" and e["udid"] == OTHER_PHONE)
    assert step["device_name"] == "Example iPad"
    targets = _targets(evs)
    assert list(targets) == [PHONE, OTHER_PHONE], "in plan order"
    assert targets[OTHER_PHONE] == {"udid": OTHER_PHONE, "device_name": "Example iPad", "ok": True,
                                    "error": None, "left": [], "waiting": False}
    (end,) = _of(evs, "update-app-end")
    assert (end["ok"], end["done"], end["left"]) == (True, ["get", "build"], [])
    assert _of(evs, "update-end")[0]["waiting"] == 0
    assert world.kind("example", PHONE) == world.kind("example", OTHER_PHONE) == "current"


def test_only_the_device_that_isnt_current_installs(world) -> None:
    _on_both(world)
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.deliver("example")  # the iPhone has it; the iPad doesn't

    code, evs = _run(world, ["example"])

    assert code == 0
    assert [c[:2] for c in world.build.calls] == [("example", OTHER_PHONE)], "built for the iPad"
    assert world.install.calls == []
    assert ("install", "skipped", PHONE) in _steps(evs)
    skipped = next(e for e in _of(evs, "update-step") if e["status"] == "skipped" and e["udid"] == PHONE)
    assert skipped["detail"] == update_run.UP_TO_DATE
    assert _targets(evs)[PHONE]["ok"] is True


def test_skips_come_first_before_the_get(world) -> None:
    _on_both(world, connected=False)
    _due(world)

    _code, evs = _run(world, ["example"])

    assert _steps(evs)[:2] == [("install", "skipped", OTHER_PHONE), ("get", "start", None)]


def test_a_waiting_device_never_stops_the_other(world) -> None:
    _on_both(world, connected=False)
    _due(world)

    code, evs = _run(world, ["example"])

    assert code == 0, "waiting isn't failing"
    assert [c[:2] for c in world.build.calls] == [("example", PHONE)]
    assert world.install.calls == []
    waiting = "Example iPad isn't connected: it'll update when it is"
    skipped = next(e for e in _of(evs, "update-step") if e["udid"] == OTHER_PHONE)
    assert (skipped["status"], skipped["detail"], skipped["waiting"]) == ("skipped", waiting, True)
    targets = _targets(evs)
    assert targets[OTHER_PHONE] == {"udid": OTHER_PHONE, "device_name": "Example iPad", "ok": False,
                                    "error": waiting, "left": ["install"], "waiting": True}
    assert _of(evs, "update-app-end")[0]["ok"] is True, "every target that isn't waiting is ok"
    summary = _of(evs, "update-end")[0]
    assert (summary["waiting"], summary["updated"]) == (1, ["example"])
    assert world.kind("example", OTHER_PHONE) == "stale", "so the next check offers it"


def test_a_failed_push_to_the_first_device_leaves_the_other_installed(world) -> None:
    _on_both(world)
    _due(world)
    world.build.fail_install["example"] = InstallError("installing on the device failed")

    code, evs = _run(world, ["example"])

    assert code == 1
    assert world.install.calls == [("example", OTHER_PHONE)], "the archive still went to the iPad"
    targets = _targets(evs)
    assert (targets[PHONE]["ok"], targets[PHONE]["left"]) == (False, ["install"])
    assert "installing on the device failed" in targets[PHONE]["error"]
    assert targets[OTHER_PHONE]["ok"] is True
    (end,) = _of(evs, "update-app-end")
    assert (end["ok"], end["done"], end["left"]) == (False, ["get", "build"], [])
    assert world.kind("example", OTHER_PHONE) == "current"
    assert world.kind("example", PHONE) == "stale"


def test_a_failed_install_on_another_device_leaves_the_first_installed(world) -> None:
    _on_both(world)
    _due(world)
    world.install.fail[OTHER_PHONE] = InstallError("installing on the device failed")

    code, evs = _run(world, ["example"])

    assert code == 1
    targets = _targets(evs)
    assert targets[PHONE]["ok"] is True
    assert (targets[OTHER_PHONE]["ok"], targets[OTHER_PHONE]["left"]) == (False, ["install"])
    end = next(e for e in _of(evs, "update-step") if e["udid"] == OTHER_PHONE and e["status"] == "end")
    assert end["detail"] == "installing on the device failed"
    assert world.kind("example", PHONE) == "current"


def test_a_build_that_fails_before_its_archive_fails_every_device(world) -> None:
    _on_both(world)
    _due(world)
    world.build.fail["example"] = GraftError("cyan exited with code 1")

    code, evs = _run(world, ["example"])

    assert code == 1
    assert world.install.calls == []
    targets = _targets(evs)
    assert {u: (t["ok"], t["left"]) for u, t in targets.items()} == {
        PHONE: (False, ["install"]), OTHER_PHONE: (False, ["install"])}
    (end,) = _of(evs, "update-app-end")
    assert (end["done"], end["left"]) == (["get"], ["build"])
    assert end["error"] == "2.0 is downloaded; the build failed: cyan exited with code 1"


def test_try_again_installs_only_where_the_app_isnt_current(world) -> None:
    _on_both(world)
    _due(world)
    world.build.fail_install["example"] = InstallError("installing on the device failed")
    _run(world, ["example"])
    world.build.calls.clear()
    world.install.calls.clear()

    code, evs = _run(world, ["example"])

    assert code == 0
    assert [c[:2] for c in world.build.calls] == [("example", PHONE)]
    assert world.install.calls == []
    assert _targets(evs)[OTHER_PHONE]["ok"] is True
    assert ("install", "skipped", OTHER_PHONE) in _steps(evs)


def test_an_app_no_target_of_which_can_run_is_blocked_with_each_target(world) -> None:
    _on_both(world, connected=False)
    del world.phones[PHONE]
    _due(world)

    code, evs = _run(world, ["example"])

    assert code == 1
    assert world.build.calls == []
    (end,) = _of(evs, "update-app-end")
    assert end["error"] == plan_of(world, ["example"])["example"]["blocked_reason"]
    assert [t["left"] for t in end["targets"]] == [["install"], ["install"]]
    assert _of(evs, "update-end")[0]["blocked"] == ["example"]


def test_update_all_leaves_out_an_app_whose_only_work_is_waiting(world) -> None:
    _on_both(world, connected=False)
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.deliver("example")  # current on the iPhone; the iPad is unplugged

    code, evs = _run(world, ["bravo", "example"], all_apps=True)

    assert code == 0
    assert _of(evs, "update-app-end") == []
    assert _of(evs, "update-end")[0]["waiting"] == 1


# --- the installs after the first, through the re-install path ------------------------


def _pushed(udid: str) -> MagicMock:
    result = MagicMock()
    result.size_bytes = 1024
    result.bundle_id = "com.kdt.livecontainer"
    result.documents_path = "Documents/example.ipa"
    result.others = []
    return result


def test_the_second_device_is_recorded_as_an_update_with_the_builds_digest(world) -> None:
    _on_both(world)
    _due(world)
    pushes: list[str] = []

    def push(ipa, *, udid, **_k):
        pushes.append(udid)
        return _pushed(udid)

    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = push
        code, evs = _run(world, ["example"], real_install=True)

    assert code == 0, _of(evs, "update-app-end")
    assert pushes == [OTHER_PHONE]
    log = BuildLog(world.cfg.state_dir / "log.sqlite")
    first = log.latest_delivery("example", udid=PHONE)
    second = log.latest_delivery("example", udid=OTHER_PHONE)
    assert second["origin"] == "update"
    assert second["inputs_digest"] == first["inputs_digest"] is not None
    assert second["source_version"] == "2.0"
    assert world.kind("example", OTHER_PHONE) == "current"
    plan = plan_of(world, ["example"])["example"]
    assert plan["nothing_to_do"] is True, "§11.1 doesn't take it for a deliberate older install"
    assert [t["install_skipped_reason"] for t in plan["targets"]] == [None, None]
    starts = [e for e in _of(evs, "build-start")]
    assert len(starts) == 1, "the re-install path's own envelope (the stand-in build emits none)"
    install_end = next(e for e in _of(evs, "update-step") if e["udid"] == OTHER_PHONE and e["status"] == "end")
    assert install_end["detail"] is None


def test_the_second_devices_push_is_held_against_a_stop(world) -> None:
    from graft_ipa import stop_deferral

    _on_both(world)
    _due(world)
    held: list[bool] = []

    def push(ipa, *, udid, **_k):
        held.append(stop_deferral.active())
        return _pushed(udid)

    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = push
        _run(world, ["example"], real_install=True)

    assert held == [True]


WAITING_IPAD = "Example iPad isn't connected: it'll update when it is"


def test_a_device_unplugged_during_the_run_waits(world) -> None:
    """Review G-4: not connected by the time its turn comes is waiting, as
    the plan would have said, not a failure. Final review 3 #1: it says so
    (`waiting`), and it's asked before the install's own envelope, so the
    stream has no failed `build-end` for a device that only waits."""
    _on_both(world)
    _due(world)
    world.build.installing = lambda _app: world.phones.pop(OTHER_PHONE)

    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = AssertionError("nothing to push to")
        code, evs = _run(world, ["example"], real_install=True)

    assert code == 0, "waiting isn't failing"
    targets = _targets(evs)
    assert targets[PHONE]["ok"] is True
    assert {k: targets[OTHER_PHONE][k] for k in ("ok", "error", "left", "waiting")} == {
        "ok": False, "error": WAITING_IPAD, "left": ["install"], "waiting": True}
    (step,) = [e for e in _of(evs, "update-step") if e["udid"] == OTHER_PHONE]
    assert (step["status"], step["detail"], step["waiting"]) == ("skipped", WAITING_IPAD, True)
    assert _of(evs, "build-start") == [] and _of(evs, "build-end") == [], (
        "the stand-in build emits none, and the iPad's install never opened its own")
    (app_end,) = _of(evs, "update-app-end")
    assert (app_end["ok"], app_end["error"]) == (True, None)
    summary = _of(evs, "update-end")[0]
    assert (summary["waiting"], summary["updated"]) == (1, ["example"])


def test_the_first_device_unplugged_before_the_build_waits_and_the_next_one_builds(world) -> None:
    _on_both(world)
    _due(world)
    world.build.before = lambda _app: world.phones.pop(PHONE, None)

    code, evs = _run(world, ["example"])

    assert code == 0
    assert [c[:2] for c in world.build.calls] == [("example", PHONE), ("example", OTHER_PHONE)]
    assert world.install.calls == [], "the iPad's install was the build's own"
    targets = _targets(evs)
    waiting = "Example iPhone isn't connected: it'll update when it is"
    assert {k: targets[PHONE][k] for k in ("ok", "error", "left", "waiting")} == {
        "ok": False, "error": waiting, "left": ["install"], "waiting": True}
    (step,) = [e for e in _of(evs, "update-step") if e["udid"] == PHONE]
    assert (step["status"], step["waiting"]) == ("skipped", True)
    assert targets[OTHER_PHONE]["ok"] is True
    assert _of(evs, "update-end")[0]["waiting"] == 1
    assert world.kind("example", OTHER_PHONE) == "current"


def test_an_install_that_ran_or_failed_is_not_waiting(world) -> None:
    """`waiting` is on every install step and every target: false unless its
    device wasn't connected (final review 3 #1, the app's `.waiting`)."""
    _on_both(world)
    _due(world)
    world.install.fail[OTHER_PHONE] = InstallError("installing on the device failed")

    _code, evs = _run(world, ["example"])

    installs = [e for e in _of(evs, "update-step") if e["step"] == "install"]
    assert installs and all(e["waiting"] is False for e in installs)
    assert {u: t["waiting"] for u, t in _targets(evs).items()} == {PHONE: False, OTHER_PHONE: False}
    others = [e for e in _of(evs, "update-step") if e["step"] != "install"]
    assert others and all("waiting" not in e for e in others), "an install step's field alone"


def test_a_device_unplugged_since_the_plan_is_skipped_before_its_build(world, monkeypatch) -> None:
    """The first device gone by the time the build starts: asked first, so
    no build of its own runs (and fails, with a history row) for it. The
    next one builds."""
    _on_both(world)
    _due(world)
    _unplug_after_the_plan(world, monkeypatch, PHONE)

    code, evs = _run(world, ["example"])

    assert code == 0
    assert [c[:2] for c in world.build.calls] == [("example", OTHER_PHONE)], "none for the iPhone"
    (step,) = [e for e in _of(evs, "update-step") if e["udid"] == PHONE]
    assert (step["status"], step["waiting"]) == ("skipped", True)
    assert _targets(evs)[OTHER_PHONE]["ok"] is True


def _unplug_after_the_plan(world, monkeypatch, *udids: str) -> None:
    emit = update_run.emit_plan

    def emit_then_unplug(*a, **k):
        emit(*a, **k)
        for udid in udids:
            world.phones.pop(udid)

    monkeypatch.setattr(update_run, "emit_plan", emit_then_unplug)


def test_every_device_unplugged_since_the_plan_waits_and_nothing_failed(world, monkeypatch) -> None:
    """Final review 3 #2a: each waits, as one would alone; the app isn't a
    failure and the run exits 0."""
    _on_both(world)
    _due(world)
    _unplug_after_the_plan(world, monkeypatch, PHONE, OTHER_PHONE)

    code, evs = _run(world, ["example"])

    assert code == 0, "waiting isn't failing"
    assert world.build.calls == []
    assert {u: (t["waiting"], t["left"]) for u, t in _targets(evs).items()} == {
        PHONE: (True, ["install"]), OTHER_PHONE: (True, ["install"])}
    (end,) = _of(evs, "update-app-end")
    assert (end["ok"], end["error"], end["done"], end["left"]) == (True, None, ["get"], ["build"])
    build_end = next(e for e in _of(evs, "update-step") if e["step"] == "build" and e["status"] == "end")
    assert build_end["detail"] == update_run.NONE_CONNECTED
    summary = _of(evs, "update-end")[0]
    assert (summary["ok"], summary["waiting"], summary["failed"]) == (True, 2, [])


def test_every_device_unplugged_still_fails_an_app_whose_tweak_failed(world, monkeypatch) -> None:
    """Through `_outcome`: waiting doesn't hide the tweak that couldn't be
    updated."""
    from graft_ipa.canister import CanisterError

    _on_both(world)
    _due(world)
    world.published["com.example.atweak"] = "1.1"
    world.fetch_error = CanisterError("download failed: 503")
    _unplug_after_the_plan(world, monkeypatch, PHONE, OTHER_PHONE)

    code, evs = _run(world, ["example"])

    assert code == 1
    (end,) = [e for e in _of(evs, "update-app-end") if e["app"] == "example"]
    assert (end["ok"], end["error"]) == (False, "couldn't update a-tweak.deb: download failed: 503")


def _push_failed(udid: str) -> Exception:
    from graft_ipa.lc_runner import LiveContainerError

    return LiveContainerError(f"failed to push example.ipa into LiveContainer: device {udid} gone")


@pytest.mark.parametrize("failure", ["install", "push"])
def test_the_first_device_unplugged_during_the_build_waits_and_the_next_still_installs(
        world, failure) -> None:
    """Final review 3 #2b: gone after the build resolved it (during the
    pre-flight, cyan or zsign), so stage 5 fails. It waits, as it would
    have before the build."""
    _on_both(world)
    _due(world)
    world.build.installing = lambda _app: world.phones.pop(PHONE)
    world.build.fail_install["example"] = (
        InstallError("installing on the device failed") if failure == "install" else _push_failed(PHONE))

    code, evs = _run(world, ["example"])

    assert code == 0, "waiting isn't failing"
    targets = _targets(evs)
    waiting = "Example iPhone isn't connected: it'll update when it is"
    assert {k: targets[PHONE][k] for k in ("ok", "error", "left", "waiting")} == {
        "ok": False, "error": waiting, "left": ["install"], "waiting": True}
    end = next(e for e in _of(evs, "update-step") if e["udid"] == PHONE and e["status"] == "end")
    assert (end["detail"], end["waiting"]) == (waiting, True)
    assert world.install.calls == [("example", OTHER_PHONE)], "the archive still went to the iPad"
    assert targets[OTHER_PHONE]["ok"] is True
    (app_end,) = _of(evs, "update-app-end")
    assert (app_end["ok"], app_end["error"], app_end["done"]) == (True, None, ["get", "build"])
    assert _of(evs, "update-end")[0]["waiting"] == 1


@pytest.mark.parametrize("failure", ["install", "push"])
def test_a_later_device_unplugged_during_its_install_waits(world, failure) -> None:
    _on_both(world)
    _due(world)
    world.install.before = lambda _app, device: world.phones.pop(device)
    world.install.fail[OTHER_PHONE] = (
        InstallError("installing on the device failed") if failure == "install"
        else _push_failed(OTHER_PHONE))

    code, evs = _run(world, ["example"])

    assert code == 0
    other = _targets(evs)[OTHER_PHONE]
    assert {k: other[k] for k in ("ok", "error", "waiting")} == {
        "ok": False, "error": WAITING_IPAD, "waiting": True}
    end = next(e for e in _of(evs, "update-step") if e["udid"] == OTHER_PHONE and e["status"] == "end")
    assert (end["detail"], end["waiting"]) == (WAITING_IPAD, True)
    assert _of(evs, "update-end")[0]["waiting"] == 1


def test_a_failure_stands_when_graft_cant_tell_whether_the_device_is_there(world, monkeypatch) -> None:
    """Asking usbmux failed: nothing says the device is gone, so the install's
    own failure is what's reported (the safe way to be wrong)."""
    _on_both(world)
    _due(world)
    world.install.fail[OTHER_PHONE] = InstallError("installing on the device failed")
    emit = update_run.emit_plan

    def unanswerable(*_a, **_k):
        raise RuntimeError("usbmuxd didn't answer")

    def emit_then_break_usbmux(*a, **k):
        emit(*a, **k)
        monkeypatch.setattr(update_readiness, "connection", unanswerable)

    monkeypatch.setattr(update_run, "emit_plan", emit_then_break_usbmux)

    code, evs = _run(world, ["example"])

    assert code == 1
    assert world.install.calls == [("example", OTHER_PHONE)], "asked nothing it couldn't, and went on"
    other = _targets(evs)[OTHER_PHONE]
    assert (other["error"], other["waiting"]) == ("installing on the device failed", False)


def _install_steps(evs) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in _of(evs, "update-step"):
        if e["step"] == "install":
            out.setdefault(e["udid"], []).append(e["status"])
    return out


@pytest.mark.parametrize("case", ["ok", "get", "build", "first-push", "later-install", "waiting"])
def test_every_device_gets_exactly_one_install_step(world, case) -> None:
    """Review G-3: `start` and `end`, or `skipped`, once per target, however
    the app's run went."""
    _on_both(world, connected=case != "waiting")
    _due(world)
    if case == "get":
        world.tools.set(download_error="received error: something went wrong")
    elif case == "build":
        world.build.fail["example"] = GraftError("cyan exited with code 1")
    elif case == "first-push":
        world.build.fail_install["example"] = InstallError("installing on the device failed")
    elif case == "later-install":
        world.install.fail[OTHER_PHONE] = InstallError("installing on the device failed")

    _code, evs = _run(world, ["example"])

    steps = _install_steps(evs)
    assert set(steps) == {PHONE, OTHER_PHONE}
    assert all(s in (["start", "end"], ["skipped"]) for s in steps.values()), steps


@pytest.mark.parametrize("case", ["get", "build"])
def test_a_failed_get_or_build_skips_each_install_saying_why(world, case) -> None:
    _on_both(world)
    _due(world)
    if case == "get":
        world.tools.set(download_error="received error: something went wrong")
    else:
        world.build.fail["example"] = GraftError("cyan exited with code 1")

    _code, evs = _run(world, ["example"])

    (end,) = _of(evs, "update-app-end")
    skips = [e for e in _of(evs, "update-step") if e["step"] == "install"]
    assert [(e["udid"], e["status"], e["detail"]) for e in skips] == [
        (PHONE, "skipped", end["error"]), (OTHER_PHONE, "skipped", end["error"])]
    after = ("get", "end") if case == "get" else ("build", "end")
    assert _index(evs, step="install", udid=PHONE) > _index(evs, step=after[0], status=after[1])


def _index(evs, **match):
    return next(i for i, e in enumerate(evs) if all(e.get(k) == v for k, v in match.items()))


# --- one profile for every device (review G-1) -------------------------------------


def test_the_build_is_told_every_other_device_it_installs_on(world) -> None:
    _on_both(world)
    _due(world)

    _run(world, ["example"])

    assert world.build.also_for == [[OTHER_PHONE]]


def test_a_device_the_builds_profile_left_out_is_refused_before_the_push(world) -> None:
    _on_both(world)
    _due(world)
    real_build = world.build

    def build(**kw):
        """App Store Connect didn't have the iPad, so the profile the build
        signed with doesn't cover it (`SignResult.unregistered_devices`)."""
        real_build(**kw)
        kw["built"].not_in_profile = (OTHER_PHONE.lower(),)

    em, out = emitter()
    cfg = world.cfg
    pipe = dataclasses.replace(pipeline(world, em, real_install=True), build=build)
    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = AssertionError("never pushed")
        code = update_run.run(cfg, ["example"], all_apps=False, with_affected=False,
                              probe=update_readiness.FullProbe(cfg.state_dir), pipeline=pipe, emitter=em)
    evs = events(out.getvalue())

    assert code == 1
    error = _targets(evs)[OTHER_PHONE]["error"]
    assert error.startswith("Example iPad isn't registered in your developer account: add it at "
                            "developer.apple.com, then try again")
    assert _targets(evs)[PHONE]["ok"] is True


# --- no digest, no record (review G-2) ----------------------------------------------


def test_a_later_device_is_not_recorded_as_a_reinstall_when_the_build_had_no_digest(world) -> None:
    _on_both(world)
    _due(world)
    world.build.no_digest = True

    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = lambda ipa, *, udid, **_k: _pushed(udid)
        code, _evs = _run(world, ["example"], real_install=True)

    assert code == 0
    row = BuildLog(world.cfg.state_dir / "log.sqlite").latest_delivery("example", udid=OTHER_PHONE)
    assert row["origin"] == "build", "nothing recorded, as the build's own install records nothing"
    plan = plan_of(world, ["example"])["example"]
    other = next(t for t in plan["targets"] if t["udid"] == OTHER_PHONE)
    assert (other["manual_install"], other["install_skipped_reason"]) == (False, None)


@pytest.mark.parametrize("origin", ["build", "update"])
def test_an_update_delivery_reads_like_a_build(world, origin) -> None:
    """Spec §11.1: only `reinstall` is a deliberate older install."""
    world.deliver("example", origin=origin)

    (facts,) = cli_mod._delivery_targets(world.cfg)("example").targets

    assert (facts.kind, facts.offered_only) == ("current", None)
    assert update_plan.ORIGIN_UPDATE == "update"


def test_the_pipeline_installs_through_the_reinstall_path(world) -> None:
    """`cli._update_pipeline`'s install is `_do_update_install`."""
    pipe = cli_mod._update_pipeline(None)
    called: list[dict] = []
    with patch.object(cli_mod, "_do_update_install", side_effect=lambda **kw: called.append(kw)):
        pipe.install(cfg=world.cfg, app_name="example", device=OTHER_PHONE,
                     built=update_run.Built(), emitter=None)
    assert called and called[0]["device"] == OTHER_PHONE
    assert dataclasses.is_dataclass(update_run.Built)


def test_in_terminal_each_device_is_said(world) -> None:
    _on_both(world, connected=False)
    _due(world)
    lines: list[str] = []
    cfg = world.cfg
    pipe = dataclasses.replace(pipeline(world, None), say=lines.append)

    code = update_run.run(cfg, ["example"], all_apps=False, with_affected=False,
                          probe=update_readiness.FullProbe(cfg.state_dir), pipeline=pipe, emitter=None)

    assert code == 0
    assert "  example: not installing on Example iPad: Example iPad isn't connected: it'll update when it is" in lines
    assert "→ example: install on Example iPhone" in lines
    assert lines[-1] == "updated: example; failed: none; blocked: none; waiting for 1 device"


def test_in_terminal_the_tweak_step_reads_as_the_app_says_it(world) -> None:
    """2.3.1 §3.2: the shared tweak step has no app. It was "→ every app:
    tweaks 1 tweak"; the progress sheet's line is "→ tweaks: 1 tweak"
    (`AppUpdateText.logLines`), and an app's own step still names it."""
    _due(world)
    world.published["com.example.atweak"] = "1.1"
    lines: list[str] = []
    cfg = world.cfg
    pipe = dataclasses.replace(pipeline(world, None), say=lines.append)

    code = update_run.run(cfg, ["example"], all_apps=False, with_affected=False,
                          probe=update_readiness.FullProbe(cfg.state_dir), pipeline=pipe, emitter=None)

    assert code == 0
    arrows = [line for line in lines if line.startswith("→")]
    assert arrows[0] == "→ tweaks: 1 tweak", arrows
    assert "→ example: get 2.0" in arrows, arrows
    assert not any("every app" in line for line in lines), lines


def test_a_failed_tweak_is_said_once_when_another_device_still_installs(world) -> None:
    """The tweak was the iPhone's only change; the iPad's own delivery state
    still calls for an install, which goes ahead."""
    from graft_ipa.canister import CanisterError

    _on_both(world)
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.deliver("example")  # the iPhone has 1.1; the iPad still has 1.0
    world.published["com.example.atweak"] = "1.1"
    world.fetch_error = CanisterError("download failed: 503")

    code, evs = _run(world, ["example"])

    assert code == 1
    assert [c[:2] for c in world.build.calls] == [("example", OTHER_PHONE)]
    targets = _targets(evs)
    assert targets[OTHER_PHONE]["ok"] is True
    assert targets[PHONE]["left"] == ["install"]
    (end,) = _of(evs, "update-app-end")
    assert end["error"] == "couldn't update a-tweak.deb: download failed: 503"


def test_a_later_device_unplugged_mid_push_ends_its_envelope_waiting(world) -> None:
    """T2-B7, on the devices after the first: their install has its own
    `build-start` and `build-end`, and the end said the push failed while
    the target said it waits."""
    _on_both(world)
    _due(world)

    def push(ipa, *, udid, **_k):
        world.phones.pop(udid, None)
        raise _push_failed(udid)

    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = push
        code, evs = _run(world, ["example"], real_install=True)

    waiting = "Example iPad isn't connected: it'll update when it is"
    assert _targets(evs)[OTHER_PHONE]["waiting"] is True, "premise"
    (end,) = _of(evs, "build-end")  # the iPad's install; the build here is a stand-in
    assert (end["ok"], end.get("waiting"), end["error"]) == (False, True, waiting)
    assert code == 0
