"""`graft --update`'s Stop (spec 3.4, §11.2): immediate during a get and
cyan; deferred through `lc-push`, `install` and a tweak's move-and-re-point
(`update-stopping`); the apps not started are reported so. Real signals to
this process, with the run's own gate installed."""

from __future__ import annotations

import os
import signal
import time
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import (
    ipatool,
    stop_deferral,
    update_plan,
    update_readiness,
    update_run,
    update_tweaks,
)
from graft_ipa.config import set_app_options
from graft_ipa.errors import GraftError, Stopped
from graft_ipa.installer import InstallResult, Target

from .update_fakes import PHONE, emitter, events, pipeline

pytestmark = pytest.mark.usefixtures("stop_safety_net")


def _term() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _stopped_run(world, names, *, real_build=False, signum=signal.SIGTERM):
    em, out = emitter()
    cfg = world.cfg
    pipe = cli_mod._update_pipeline(em) if real_build else pipeline(world, em)
    with pytest.raises(Stopped) as raised:
        update_run.run(cfg, names, all_apps=False, with_affected=False,
                       probe=update_readiness.FullProbe(cfg.state_dir), pipeline=pipe, emitter=em)
    assert raised.value.code == 128 + signum
    return events(out.getvalue())


def _of(evs, kind):
    return [e for e in evs if e["type"] == kind]


def _both_due(world) -> None:
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"
    world.stage("bravo", "5.1")
    world.store["com.bravo.app"] = "5.1"


def test_a_stop_during_a_get_is_at_once(world, monkeypatch) -> None:
    world.store["com.example.app"] = "2.0"
    world.stage("bravo", "5.1")
    world.store["com.bravo.app"] = "5.1"

    def slow_download(*_a, **_k):
        _term()
        time.sleep(5)

    monkeypatch.setattr(ipatool, "download", slow_download)

    evs = _stopped_run(world, ["example", "bravo"])

    assert _of(evs, "update-stopping") == []
    assert _of(evs, "ipa-provided") == []
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert ends["example"]["stopped"] is True and ends["example"]["left"] == ["get", "build"]
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    (summary,) = _of(evs, "update-end")
    assert (summary["stopped"], summary["failed"], summary["not_started"]) == (True, ["example"], ["bravo"])
    assert world.build.calls == []


def test_a_stop_during_the_build_before_the_install_is_at_once(world) -> None:
    _both_due(world)
    world.build.before = lambda _app: _term()

    evs = _stopped_run(world, ["example", "bravo"])

    assert _of(evs, "update-stopping") == []
    assert [c[0] for c in world.build.calls] == ["example"]
    assert world.kind("example") == "stale", "nothing was delivered"


def test_an_interrupt_stops_it_too_and_ends_with_update_end(world) -> None:
    """Ctrl-C in Terminal: a Stop, exit 130, and the summary still comes."""
    _both_due(world)
    world.build.before = lambda _app: os.kill(os.getpid(), signal.SIGINT)

    evs = _stopped_run(world, ["example", "bravo"], signum=signal.SIGINT)

    (summary,) = _of(evs, "update-end")
    assert (summary["stopped"], summary["not_started"]) == (True, ["bravo"])


def test_a_stop_held_through_a_push_whose_build_failed_still_starts_no_other_app(world) -> None:
    """Review #6: the held Stop takes effect when the failed build is done
    with, even were the runner's own release after the failure lost: the
    next app checks for it before it starts."""
    _both_due(world)

    def push_then_fail(app: str) -> None:
        stop_deferral.finish_before_stopping(app, "lc-push")
        _term()

    world.build.before = push_then_fail
    world.build.fail["example"] = GraftError("the push failed")

    evs = _stopped_run(world, ["example", "bravo"])

    assert [c[0] for c in world.build.calls] == ["example"], "bravo never started"
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    assert _of(evs, "update-end")[0]["stopped"] is True


def test_a_stop_held_through_the_last_apps_install_still_stops(world) -> None:
    """No app follows to check for it: the run checks once more at its end."""
    world.stage("example", "1.1")
    world.store["com.example.app"] = "1.1"

    def pushing(app: str) -> None:
        stop_deferral.finish_before_stopping(app, "lc-push")
        _term()

    world.build.before = pushing

    evs = _stopped_run(world, ["example"])

    assert [e["app"] for e in _of(evs, "update-app-end") if e["ok"]] == ["example"], "it finished"
    assert _of(evs, "update-end")[0]["stopped"] is True


def _pushed() -> MagicMock:
    result = MagicMock()
    result.size_bytes = 1024
    result.bundle_id = "com.kdt.livecontainer"
    result.documents_path = "Documents/example.ipa"
    result.others = []
    return result


def _cyan(tmp_path):
    cyan = MagicMock()
    cyan.output_ipa = tmp_path / "tweaked.ipa"
    cyan.output_ipa.write_bytes(b"x")
    cyan.stdout = cyan.stderr = ""
    return cyan


def test_a_stop_during_the_push_waits_for_the_install(world, tmp_path) -> None:
    _both_due(world)

    def push(*_a, **_k):
        _term()
        return _pushed()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan", return_value=_cyan(tmp_path)), \
         patch.object(cli_mod, "resolve_target", return_value=Target(PHONE, None, "usb")), \
         patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = push
        evs = _stopped_run(world, ["example", "bravo"], real_build=True)

    assert _of(evs, "update-stopping") == [{"type": "update-stopping", "app": "example", "step": "lc-push"}]
    build_end = _of(evs, "build-end")[-1]
    assert (build_end["ok"], build_end["stopped"]) == (True, False), "the install finished"
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert ends["example"]["ok"] is True
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    assert _of(evs, "update-end")[0]["updated"] == ["example"]
    assert world.kind("example") == "current", "the delivery was recorded"


def test_a_second_stop_during_the_push_cancels_it(world, tmp_path) -> None:
    """Spec §11.2: the first Stop waits; a second ends the push and the run."""
    _both_due(world)

    def push(*_a, **_k):
        _term()
        _term()
        return _pushed()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan", return_value=_cyan(tmp_path)), \
         patch.object(cli_mod, "resolve_target", return_value=Target(PHONE, None, "usb")), \
         patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = push
        evs = _stopped_run(world, ["example", "bravo"], real_build=True)

    assert _of(evs, "build-end")[-1]["stopped"] is True
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert ends["example"]["stopped"] is True
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    assert world.kind("example") == "stale", "no delivery recorded"


def test_a_stop_during_the_install_waits_for_it(world, tmp_path) -> None:
    set_app_options(world.cfg_path, "example", delivery="sideload")
    _both_due(world)

    def zsign(_cfg, _src, out, **_k):
        out.write_bytes(b"signed")
        result = MagicMock()
        result.stdout = result.stderr = ""
        result.team_id, result.cert_subject, result.expiry = "ABCDE12345", "Example", "2027-01-01"
        result.profile_notes = []
        return result

    def install(*_a, **_k):
        _term()
        return InstallResult(udid=PHONE, stdout="Install: Complete", stderr="")

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan", return_value=_cyan(tmp_path)), \
         patch.object(cli_mod, "run_zsign", side_effect=zsign), \
         patch.object(cli_mod, "resolve_target", return_value=Target(PHONE, None, "usb")), \
         patch.object(cli_mod, "install", side_effect=install):
        evs = _stopped_run(world, ["example", "bravo"], real_build=True)

    assert _of(evs, "update-stopping") == [{"type": "update-stopping", "app": "example", "step": "install"}]
    assert _of(evs, "build-end")[-1]["ok"] is True
    assert {e["app"]: e["ok"] for e in _of(evs, "update-app-end")} == {"example": True, "bravo": False}


def test_a_stop_during_a_tweak_write_waits_and_no_build_starts(world, monkeypatch) -> None:
    world.published["com.example.atweak"] = "1.1"
    real = update_tweaks.repoint_tweak

    def stopped_midway(*a, **k):
        _term()
        return real(*a, **k)

    monkeypatch.setattr(update_tweaks, "repoint_tweak", stopped_midway)

    evs = _stopped_run(world, ["example", "bravo"])

    assert _of(evs, "update-stopping") == [{"type": "update-stopping", "app": None, "step": "tweaks"}]
    assert {a.tweaks[0] for a in world.cfg.apps.values()} == {"atweak-tweak_1.1.deb"}, "every app re-pointed"
    assert world.build.calls == []
    ends = _of(evs, "update-app-end")
    assert [e["error"] for e in ends] == [update_run.NOT_STARTED] * 2
    assert all(e["done"] == ["tweaks"] for e in ends)


def test_a_stop_while_planning_still_ends_the_run_saying_so(world, monkeypatch) -> None:
    def stopped(*_a, **_k):
        _term()
        return {}

    monkeypatch.setattr(update_plan, "store_versions", stopped)

    evs = _stopped_run(world, ["example"])

    assert [e["type"] for e in evs] == ["update-end"]
    assert evs[0]["stopped"] is True


def test_a_stop_as_the_plan_goes_out_still_ends_the_run_saying_so(world, monkeypatch) -> None:
    """Final review 3 #4: between the plan and the work, a Stop still ends
    with each app's result and `update-end`."""
    _both_due(world)
    emit = update_run.emit_plan

    def emit_then_stop(*a, **k):
        emit(*a, **k)
        _term()

    monkeypatch.setattr(update_run, "emit_plan", emit_then_stop)

    evs = _stopped_run(world, ["example", "bravo"])

    assert evs[0]["type"] == "update-plan"
    assert {e["app"]: e["error"] for e in _of(evs, "update-app-end")} == {
        "example": update_run.NOT_STARTED, "bravo": update_run.NOT_STARTED}
    (summary,) = _of(evs, "update-end")
    assert (summary["stopped"], summary["not_started"]) == (True, ["example", "bravo"])
    assert evs[-1]["type"] == "update-end"
    assert world.build.calls == []


# --- per target (spec §11.3): each install is held, and the targets after a Stop wait ---


def _on_both(world) -> None:
    from graft_ipa import devices_seen

    from .update_fakes import OTHER_PHONE, FakePhone

    world.deliver("example", udid=OTHER_PHONE)
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")


def _targets(evs, app="example"):
    (end,) = [e for e in _of(evs, "update-app-end") if e["app"] == app]
    return {t["udid"]: t for t in end["targets"]}


def test_a_stop_during_the_first_devices_push_waits_and_the_next_device_is_not_started(world) -> None:
    from .update_fakes import OTHER_PHONE

    _on_both(world)
    _both_due(world)

    def pushing(app: str) -> None:
        stop_deferral.finish_before_stopping(app, "lc-push")
        _term()

    world.build.installing = pushing

    evs = _stopped_run(world, ["example", "bravo"])

    assert _of(evs, "update-stopping") == [{"type": "update-stopping", "app": "example", "step": "lc-push"}]
    assert world.install.calls == [], "the iPad's install never started"
    targets = _targets(evs)
    assert targets[PHONE]["ok"] is True, "the iPhone's install finished"
    assert {k: targets[OTHER_PHONE][k] for k in ("ok", "error", "left")} == {
        "ok": False, "error": None, "left": ["install"]}
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert ends["example"]["stopped"] is True
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    assert world.kind("example", PHONE) == "current"
    assert world.kind("example", OTHER_PHONE) == "stale"


def test_a_stop_during_a_later_devices_install_waits_for_it(world) -> None:
    from .update_fakes import OTHER_PHONE

    _on_both(world)
    _both_due(world)

    def installing(app: str, _device: str) -> None:
        stop_deferral.finish_before_stopping(app, "lc-push")
        _term()

    world.install.before = installing

    evs = _stopped_run(world, ["example", "bravo"])

    assert _of(evs, "update-stopping") == [{"type": "update-stopping", "app": "example", "step": "lc-push"}]
    assert world.install.calls == [("example", OTHER_PHONE)]
    assert {u: t["ok"] for u, t in _targets(evs).items()} == {PHONE: True, OTHER_PHONE: True}
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert (ends["example"]["ok"], ends["example"]["stopped"]) == (True, False)
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    assert world.kind("example", OTHER_PHONE) == "current"


def test_a_second_stop_during_a_later_devices_push_cancels_it(world) -> None:
    from .update_fakes import OTHER_PHONE

    _on_both(world)
    _both_due(world)

    def push(*_a, **_k):
        _term()
        _term()
        return _pushed()

    with patch.object(cli_mod, "lc_runner") as lc:
        lc.push_ipa_to_livecontainer.side_effect = push
        em, out = emitter()
        cfg = world.cfg
        pipe = pipeline(world, em, real_install=True)
        with pytest.raises(Stopped):
            update_run.run(cfg, ["example", "bravo"], all_apps=False, with_affected=False,
                           probe=update_readiness.FullProbe(cfg.state_dir), pipeline=pipe, emitter=em)
        evs = events(out.getvalue())

    assert _of(evs, "build-end")[-1]["stopped"] is True, "the iPad's install envelope says so"
    targets = _targets(evs)
    assert targets[PHONE]["ok"] is True
    assert {k: targets[OTHER_PHONE][k] for k in ("ok", "error", "left")} == {
        "ok": False, "error": None, "left": ["install"]}
    ends = {e["app"]: e for e in _of(evs, "update-app-end")}
    assert ends["example"]["stopped"] is True
    assert ends["bravo"]["error"] == update_run.NOT_STARTED
    assert world.kind("example", OTHER_PHONE) == "stale", "no delivery recorded"
