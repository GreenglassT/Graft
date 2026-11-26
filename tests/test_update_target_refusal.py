"""`graft --update`: a refusal about the first device alone fails that device,
and the next ready one builds (final review 3 #3, spec §11.3: one target's
failure doesn't stop the others).

Two refusals a build makes about its own device before it builds anything: a
stranger's `<app>.ipa` already in that device's LiveContainer Documents
(`cli._check_lc_target_early`), and Developer Mode found off there at build
time (`cli._require_developer_mode`). Either used to fail every device the
app is on.

`_build_one` runs for real here: the early LiveContainer check, the
Developer Mode gate's place in the build, the archive, and the delivery
record. Stood in for: the pre-flight, the decryption check, cyan, zsign, and
the device itself (its Documents, its Developer Mode answer, the push and
the install).
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import developer_mode, devices_seen, lc_runner, update_readiness, update_run
from graft_ipa.build_log import BuildLog
from graft_ipa.config import set_app_options
from graft_ipa.errors import CyanError, DeveloperModeOffError, IPANotFoundError
from graft_ipa.installer import InstallResult

from .update_fakes import DEVELOPMENT, OTHER_PHONE, PHONE, FakePhone, emitter, events

pytestmark = pytest.mark.usefixtures("stop_safety_net")


def _of(evs, kind):
    return [e for e in evs if e["type"] == kind]


def _targets(evs, app="example"):
    (end,) = [e for e in _of(evs, "update-app-end") if e["app"] == app]
    return {t["udid"]: t for t in end["targets"]}


def _on_both(world) -> None:
    world.deliver("example", udid=OTHER_PHONE)
    devices_seen.remember(world.cfg.state_dir, OTHER_PHONE, name="Example iPad")
    world.phones[OTHER_PHONE] = FakePhone(name="Example iPad")
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")


def _cyan(tmp_path):
    cyan = MagicMock()
    cyan.output_ipa = tmp_path / "tweaked.ipa"
    cyan.output_ipa.write_bytes(b"tweaked")
    cyan.stdout = cyan.stderr = ""
    return cyan


def _pushed(udid: str) -> lc_runner.LCDeliveryResult:
    return lc_runner.LCDeliveryResult(udid=udid, bundle_id="com.kdt.livecontainer",
                                      documents_path="Documents/example.ipa", size_bytes=1024)


class Device:
    """The devices' side of a build: what each early check and push saw."""

    def __init__(self) -> None:
        self.checked: list[str] = []
        self.pushed: list[str] = []
        self.installed: list[str] = []
        self.stranger_on: set[str] = set()
        self.developer_mode_off_on: set[str] = set()

    def check(self, udid, *, inner_filename, **_k):
        self.checked.append(udid)
        if udid in self.stranger_on:
            raise lc_runner._foreign_file("com.kdt.livecontainer", udid, inner_filename)
        return "com.kdt.livecontainer"

    def push(self, ipa, *, udid, **_k):
        self.pushed.append(udid)
        return _pushed(udid)

    def require_on(self, state_dir, udid, **_k):
        if udid in self.developer_mode_off_on:
            raise DeveloperModeOffError(f"Developer Mode is off on {devices_seen.label(state_dir, udid)}",
                                        hint=developer_mode.TURN_ON)

    def install(self, ipa, *, udid, **_k):
        self.installed.append(udid)
        return InstallResult(udid=udid, stdout="Install: Complete", stderr="")


def _zsign(_cfg, _src, out, **_k):
    out.write_bytes(b"signed")
    result = MagicMock()
    result.stdout = result.stderr = ""
    result.team_id, result.cert_subject, result.expiry = "ABCDE12345", "Example", "2027-01-01"
    result.profile_notes = []
    result.unregistered_devices = []
    return result


def _run(world, tmp_path, device: Device, *, cyan=None, zsign=_zsign):
    em, out = emitter()
    cfg = world.cfg
    with contextlib.ExitStack() as stack:
        for target, name, kw in (
            (cli_mod, "run_all_checks", {"return_value": []}),
            (cli_mod, "extract_ipa", {}),
            (cli_mod, "verify_decrypted", {"return_value": []}),
            (cli_mod, "run_cyan", {"side_effect": cyan} if cyan else {"return_value": _cyan(tmp_path)}),
            (cli_mod, "run_zsign", {"side_effect": zsign}),
            (cli_mod, "install", {"side_effect": device.install}),
            (developer_mode, "require_on", {"side_effect": device.require_on}),
            (lc_runner, "check_push_target", {"side_effect": device.check}),
            (lc_runner, "push_ipa_to_livecontainer", {"side_effect": device.push}),
        ):
            stack.enter_context(patch.object(target, name, **kw))
        code = update_run.run(cfg, ["example"], all_apps=False, with_affected=False,
                              probe=update_readiness.FullProbe(cfg.state_dir),
                              pipeline=cli_mod._update_pipeline(em), emitter=em)
    return code, events(out.getvalue())


def _install_steps(evs):
    return [(e["udid"], e["status"]) for e in _of(evs, "update-step") if e["step"] == "install"]


def _delivery(world, udid):
    return BuildLog(world.cfg.state_dir / "log.sqlite").latest_delivery("example", udid=udid)


def test_a_strangers_ipa_on_the_first_device_fails_it_alone_and_the_next_one_builds(
        world, tmp_path) -> None:
    _on_both(world)
    device = Device()
    device.stranger_on = {PHONE}
    before = _delivery(world, PHONE)["id"]

    code, evs = _run(world, tmp_path, device)

    assert code == 1, "the iPhone's refusal is a failure"
    assert device.checked == [PHONE, OTHER_PHONE]
    assert device.pushed == [OTHER_PHONE], "the iPad's install was the build's own"
    targets = _targets(evs)
    assert (targets[PHONE]["ok"], targets[PHONE]["waiting"], targets[PHONE]["left"]) == (
        False, False, ["install"])
    assert "did not push" in targets[PHONE]["error"]
    assert targets[OTHER_PHONE]["ok"] is True
    assert _install_steps(evs) == [(PHONE, "skipped"), (OTHER_PHONE, "start"), (OTHER_PHONE, "end")]
    (end,) = _of(evs, "update-app-end")
    assert (end["ok"], end["done"], end["left"]) == (False, ["get", "build"], [])
    assert end["error"].startswith("Example iPhone: ")
    assert [e["ok"] for e in _of(evs, "build-end")] == [False, True], "one refused, one built"
    # The iPad's delivery is the build's own: origin `build`, with the digest
    # of what was built, so it reads current; the iPhone's is untouched.
    ipad = _delivery(world, OTHER_PHONE)
    assert ipad["origin"] == "build"
    assert ipad["source_version"] == "2.0"
    assert ipad["inputs_digest"] is not None
    assert world.kind("example", OTHER_PHONE) == "current"
    assert _delivery(world, PHONE)["id"] == before
    assert world.kind("example", PHONE) == "stale"


def test_developer_mode_off_on_the_first_device_at_build_time_fails_it_alone(world, tmp_path) -> None:
    """On when the plan read it, off by the build: the iPad still installs."""
    set_app_options(world.cfg_path, "example", delivery="sideload")
    world.profile(DEVELOPMENT, [PHONE, OTHER_PHONE])
    _on_both(world)
    device = Device()
    device.developer_mode_off_on = {PHONE}

    code, evs = _run(world, tmp_path, device)

    assert code == 1
    assert device.installed == [OTHER_PHONE]
    targets = _targets(evs)
    assert targets[PHONE]["error"].startswith("Developer Mode is off on Example iPhone")
    assert targets[OTHER_PHONE]["ok"] is True
    assert _install_steps(evs) == [(PHONE, "skipped"), (OTHER_PHONE, "start"), (OTHER_PHONE, "end")]
    ipad = _delivery(world, OTHER_PHONE)
    assert (ipad["origin"], ipad["source_version"]) == ("build", "2.0")
    assert world.kind("example", OTHER_PHONE) == "current"
    assert world.kind("example", PHONE) == "stale"


def test_the_only_device_refused_fails_the_app_saying_why(world, tmp_path) -> None:
    world.store["com.example.app"] = "2.0"
    world.offer_get("2.0")
    device = Device()
    device.stranger_on = {PHONE}

    code, evs = _run(world, tmp_path, device)

    assert code == 1
    assert device.pushed == []
    (end,) = _of(evs, "update-app-end")
    assert (end["ok"], end["done"], end["left"]) == (False, ["get"], ["build"])
    assert "did not push" in end["error"]
    build_end = next(e for e in _of(evs, "update-step") if e["step"] == "build" and e["status"] == "end")
    assert "did not push" in build_end["detail"]
    assert _install_steps(evs) == [(PHONE, "skipped")]


def test_a_build_failure_about_no_device_still_fails_every_device(world, tmp_path) -> None:
    """cyan failing is the build's, not the first device's: nothing builds
    for the next one either."""
    _on_both(world)
    device = Device()

    code, evs = _run(world, tmp_path, device, cyan=CyanError("cyan exited with code 1"))

    assert code == 1
    assert device.checked == [PHONE], "one build, which failed"
    assert device.pushed == []
    assert {u: t["ok"] for u, t in _targets(evs).items()} == {PHONE: False, OTHER_PHONE: False}
    assert len(_of(evs, "build-start")) == 1


def test_the_next_build_signs_for_the_devices_after_it_alone(world, tmp_path) -> None:
    """The refused device isn't one the next build's profile is made for."""
    set_app_options(world.cfg_path, "example", delivery="sideload")
    world.profile(DEVELOPMENT, [PHONE, OTHER_PHONE])
    _on_both(world)
    device = Device()
    device.developer_mode_off_on = {PHONE}
    signed: list[tuple[str | None, list[str]]] = []

    def zsign(cfg, src, out, **k):
        signed.append((k.get("target"), list(k.get("also") or ())))
        return _zsign(cfg, src, out, **k)

    _run(world, tmp_path, device, zsign=zsign)

    assert signed == [(OTHER_PHONE, [])]


# --- `_build_one` says when its refusal was about its device alone -----------------


def test_the_build_marks_a_refusal_about_its_device_alone(world, tmp_path) -> None:
    device = Device()
    device.stranger_on = {PHONE}
    built = update_run.Built()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(lc_runner, "check_push_target", side_effect=device.check), \
         patch.object(cli_mod, "run_cyan", side_effect=AssertionError("cyan ran")), \
         pytest.raises(lc_runner.LiveContainerError):
        cli_mod._build_one(cfg=world.cfg, app_name="example", dry_run=False, skip_install=False,
                           skip_decrypt_check=True, verbose=False, device=PHONE, outcome=built)

    assert built.target_refused is True
    assert built.archive is None


@pytest.mark.parametrize("where", ["before", "after"])
def test_a_failure_that_isnt_about_the_device_is_not_marked(world, tmp_path, where) -> None:
    """Before the device is asked (no IPA), or after it said yes (cyan)."""
    device = Device()
    built = update_run.Built()
    failure = IPANotFoundError("no IPA") if where == "before" else CyanError("cyan exited with code 1")

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa", side_effect=failure if where == "before" else None,
                      wraps=None if where == "before" else cli_mod.acquire_ipa), \
         patch.object(lc_runner, "check_push_target", side_effect=device.check), \
         patch.object(cli_mod, "run_cyan", side_effect=failure), \
         pytest.raises(type(failure)):
        cli_mod._build_one(cfg=world.cfg, app_name="example", dry_run=False, skip_install=False,
                           skip_decrypt_check=True, verbose=False, device=PHONE, outcome=built)

    assert device.checked == ([] if where == "before" else [PHONE])
    assert built.target_refused is False


def test_a_refused_device_then_an_unplugged_one_ends_the_build_saying_why(world, tmp_path, monkeypatch) -> None:
    """Not "none of its devices is connected": one was, and was refused."""
    _on_both(world)
    device = Device()
    device.stranger_on = {PHONE}
    checked = device.check

    def check_then_unplug_the_ipad(udid, **kw):
        world.phones.pop(OTHER_PHONE, None)
        return checked(udid, **kw)

    device.check = check_then_unplug_the_ipad

    code, evs = _run(world, tmp_path, device)

    assert code == 1
    targets = _targets(evs)
    assert (targets[PHONE]["waiting"], targets[OTHER_PHONE]["waiting"]) == (False, True)
    build_end = next(e for e in _of(evs, "update-step") if e["step"] == "build" and e["status"] == "end")
    assert "did not push" in build_end["detail"]


def test_an_error_that_isnt_a_refusal_is_not_marked(world) -> None:
    """Only a refusal is about the device: something that broke while asking
    (here, a bug) fails the build as before, for every device."""
    built = update_run.Built()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(lc_runner, "check_push_target", side_effect=RuntimeError("a bug")), \
         patch.object(cli_mod, "run_cyan", side_effect=AssertionError("cyan ran")), \
         pytest.raises(RuntimeError):
        cli_mod._build_one(cfg=world.cfg, app_name="example", dry_run=False, skip_install=False,
                           skip_decrypt_check=True, verbose=False, device=PHONE, outcome=built)

    assert built.target_refused is False
