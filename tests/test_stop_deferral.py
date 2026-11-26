"""`graft --update`'s Stop (spec 3.4): at once, except while a push, an
install or a tweak's move and re-point is running, which then finishes first.
Real signals, sent to this process with the gate installed: the gate, not a
mock, is what's under test."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import device_session, lc_manage, stop_deferral
from graft_ipa.build_log import BuildLog
from graft_ipa.device_info import DeveloperModeCheck
from graft_ipa.errors import Stopped
from graft_ipa.installer import InstallResult
from graft_ipa.json_output import JsonEmitter
from graft_ipa.stop_deferral import StopGate

from .test_cli_build_json import _stub_config

PHONE = "00008130-FAKE00000000PHONE1"


class _StraySignal(Exception):
    """A SIGTERM or SIGINT that reached this module's safety net: nothing
    under test was handling it, which under Python's default would have ended
    pytest (SIGTERM) or interrupted the whole run (SIGINT)."""


@pytest.fixture(autouse=True)
def _signals_restored() -> Iterator[None]:
    """Every test here sends real SIGTERMs (and some SIGINTs) to this
    process. For each test a handler that raises takes the default's place,
    so a gate that failed to install fails that test instead of killing the
    run; the handlers the test found come back whatever happens, and no gate
    outlives the test."""
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}

    def stray(signum: int, _frame: object) -> None:
        raise _StraySignal(f"signal {signum} reached the test's safety net")

    signal.signal(signal.SIGTERM, stray)
    signal.signal(signal.SIGINT, stray)
    try:
        yield
    finally:
        for sig, handler in before.items():
            signal.signal(sig, handler)
        stop_deferral._gate = None


def _term() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


def _interrupt() -> None:
    os.kill(os.getpid(), signal.SIGINT)


def test_a_stop_outside_a_held_step_is_at_once() -> None:
    with pytest.raises(Stopped) as raised, StopGate(lambda *_: None).installed():
        _term()

    assert raised.value.code == 128 + signal.SIGTERM


def test_a_stop_during_a_deferred_step_waits_for_it_and_is_announced_once() -> None:
    said: list[tuple[str | None, str]] = []
    finished: list[bool] = []
    with StopGate(lambda app, step: said.append((app, step))).installed() as gate:
        with pytest.raises(Stopped):
            with gate.deferring(None, "tweaks"):
                _term()
                _term()
                finished.append(True)

    assert finished == [True], "the step ran to its end"
    assert said == [(None, "tweaks")], "announced once, for the first Stop"


def test_a_held_stop_takes_effect_when_released() -> None:
    with StopGate(lambda *_: None).installed() as gate:
        stop_deferral.finish_before_stopping("example", "lc-push")
        assert stop_deferral.active()
        _term()
        assert gate.stop_pending
        with pytest.raises(Stopped):
            gate.release()

    assert not stop_deferral.active()


def test_without_a_gate_nothing_is_held() -> None:
    """A plain build: `finish_before_stopping` does nothing, and the build's
    own handler stops at once as it always has."""
    stop_deferral.finish_before_stopping("example", "install")
    assert not stop_deferral.active()

    with pytest.raises(Stopped), lc_manage.exit_on_termination():
        _term()


def test_a_failure_with_a_stop_pending_ends_as_the_stop() -> None:
    with StopGate(lambda *_: None).installed() as gate:
        with pytest.raises(Stopped) as raised:
            with gate.deferring(None, "tweaks"):
                _term()
                raise OSError("disk full")

    assert isinstance(raised.value.__cause__, OSError)


def test_the_gate_puts_back_the_handlers_it_replaced() -> None:
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP))
    with StopGate(lambda *_: None).installed():
        assert signal.getsignal(signal.SIGTERM) is not before[0]

    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGHUP)) == before


def test_a_hangup_that_arrived_ignored_stays_ignored() -> None:
    """`nohup graft --update …` asks for the run to outlive its terminal."""
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        with StopGate(lambda *_: None).installed():
            assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGHUP, previous)


def test_a_device_session_under_a_held_stop_runs_to_its_end() -> None:
    async def install() -> str:
        _term()
        await asyncio.sleep(0.05)
        return "installed"

    with StopGate(lambda *_: None).installed() as gate:
        gate.hold("example", "install")
        assert device_session.run(install()) == "installed"
        assert gate.stop_pending
        with pytest.raises(Stopped):
            gate.release()


def test_without_a_hold_a_device_session_is_cancelled_and_stops() -> None:
    async def install() -> str:
        _term()
        await asyncio.sleep(5)
        return "installed"

    with StopGate(lambda *_: None).installed():
        with pytest.raises(Stopped):
            device_session.run(install())


def _pushed() -> MagicMock:
    result = MagicMock()
    result.size_bytes = 1024
    result.bundle_id = "com.kdt.livecontainer"
    result.documents_path = "Documents/example.ipa"
    result.others = []
    return result


def _build(tmp_path: Path, cfg, capsys, said: list) -> tuple[list[dict], StopGate]:
    with StopGate(lambda app, step: said.append((app, step))).installed() as gate:
        cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                           dry_run=False, skip_install=False, skip_decrypt_check=False,
                           verbose=False, device=None, emitter=JsonEmitter())
        pending = gate.stop_pending
        with pytest.raises(Stopped):
            gate.release()
    assert pending, "the Stop waited for the build"
    out = capsys.readouterr().out
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")], gate


def test_a_stop_during_the_push_waits_for_the_whole_build(tmp_path: Path, capsys) -> None:
    cfg = _stub_config(tmp_path)
    said: list = []

    def push(*_a, **_k):
        _term()  # the app's Stop, mid-push
        return _pushed()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "resolve_target", return_value=cli_mod.Target(PHONE, None, "usb")), \
         patch.object(cli_mod, "lc_runner") as lc:
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.output_ipa.write_bytes(b"x")
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        lc.push_ipa_to_livecontainer.side_effect = push
        events, _ = _build(tmp_path, cfg, capsys, said)

    end = [e for e in events if e["type"] == "build-end"][-1]
    assert (end["ok"], end["stopped"]) == (True, False), "the push and the build finished"
    assert said == [("example", "lc-push")]


def test_a_stop_during_the_install_waits_for_the_whole_build(tmp_path: Path, capsys) -> None:
    cfg = _stub_config(tmp_path)
    cfg.apps["example"].delivery = "sideload"
    said: list = []

    def zsign(_cfg, _src, out, **_k):
        out.write_bytes(b"signed")
        result = MagicMock()
        result.stdout = result.stderr = ""
        result.team_id, result.cert_subject, result.expiry = "ABCDE12345", "Example", "2027-01-01"
        result.profile_notes = []
        return result

    def install(*_a, **_k):
        _term()  # the app's Stop, mid-install
        return InstallResult(udid=PHONE, stdout="Install: Complete", stderr="")

    # Through the sideload build's Developer Mode gate (Plan B), on: stage 5
    # installs on the phone that check read.
    with patch("graft_ipa.developer_mode.needed", return_value=True), \
         patch("graft_ipa.device_info.check_developer_mode",
               return_value=DeveloperModeCheck(on=True)) as checked, \
         patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "run_zsign", side_effect=zsign), \
         patch.object(cli_mod, "resolve_target", return_value=cli_mod.Target(PHONE, None, "usb")), \
         patch.object(cli_mod, "install", side_effect=install):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.output_ipa.write_bytes(b"x")
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        events, _ = _build(tmp_path, cfg, capsys, said)

    end = [e for e in events if e["type"] == "build-end"][-1]
    assert (end["ok"], end["stopped"]) == (True, False)
    assert said == [("example", "install")]
    assert [c.args[0] for c in checked.call_args_list] == [PHONE]


def test_a_dry_run_holds_no_stop(tmp_path: Path, capsys) -> None:
    """A dry run writes nothing to the phone, so there is nothing to finish:
    its Stop is at once, even under an update's gate."""
    cfg = _stub_config(tmp_path)

    def push(*_a, **_k):
        _term()
        return _pushed()

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "extract_ipa"), \
         patch.object(cli_mod, "verify_decrypted", return_value=[]), \
         patch.object(cli_mod, "run_cyan") as cyan, \
         patch.object(cli_mod, "resolve_target", return_value=cli_mod.Target(PHONE, None, "usb")), \
         patch.object(cli_mod, "lc_runner") as lc:
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cyan.return_value.output_ipa = tmp_path / "tweaked.ipa"
        cyan.return_value.stdout = cyan.return_value.stderr = ""
        lc.push_ipa_to_livecontainer.side_effect = push
        with StopGate(lambda *_: None).installed() as gate, pytest.raises(Stopped):
            cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                               dry_run=True, skip_install=False, skip_decrypt_check=False,
                               verbose=False, device=None, emitter=JsonEmitter())
        assert not gate.stop_pending, "stopped at once, not held"

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert [e["stopped"] for e in events if e["type"] == "build-end"] == [True]


# --- a second Stop (spec §11.2) ---------------------------------------------


def test_a_second_stop_during_a_held_step_stops_at_once() -> None:
    """The first Stop during a push or an install waits for it; a second
    cancels it."""
    said: list = []
    with StopGate(lambda app, step: said.append((app, step))).installed() as gate:
        gate.hold("example", "lc-push")
        _term()
        assert gate.stop_pending
        with pytest.raises(Stopped):
            _term()

    assert said == [("example", "lc-push")]


def test_a_second_stop_cancels_a_held_device_session() -> None:
    """As `device_session` cancels on a first Stop outside an update: at the
    await it is in, so its clean-up still runs."""
    reached: list[str] = []

    async def install() -> str:
        _term()
        await asyncio.sleep(0.05)
        reached.append("after the first Stop")
        _term()
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            reached.append("cancelled at the await")
            raise
        return "installed"

    with StopGate(lambda *_: None).installed() as gate:
        gate.hold("example", "install")
        with pytest.raises(Stopped) as raised:
            device_session.run(install())

    assert raised.value.code == 128 + signal.SIGTERM
    assert reached == ["after the first Stop", "cancelled at the await"]


def test_the_tweak_write_is_never_cut_short_however_many_stops() -> None:
    """Its deferral is absolute: it lasts milliseconds, and a half-written
    re-point is what it exists to prevent. A device session inside it isn't
    cancelled either."""
    async def session() -> str:
        for _ in range(3):
            _term()
            await asyncio.sleep(0.02)
        return "finished"

    finished: list[str] = []
    with StopGate(lambda *_: None).installed() as gate:
        with pytest.raises(Stopped):
            with gate.deferring(None, "tweaks"):
                _term()
                _term()
                finished.append(device_session.run(session()))
                _term()
                finished.append("wrote")

    assert finished == ["finished", "wrote"]


def test_a_notice_that_failed_to_go_out_is_tried_again() -> None:
    """`update-stopping` is what lets the app hold off its kill: a Stop whose
    notice didn't go out isn't counted as announced."""
    tries: list[str] = []

    def announce(_app: str | None, step: str) -> None:
        tries.append(step)
        if len(tries) == 1:
            raise BrokenPipeError("stdout closed for a moment")

    with StopGate(announce).installed() as gate:
        with pytest.raises(Stopped):
            with gate.deferring(None, "tweaks"):
                _term()
                _term()
                _term()

    assert tries == ["tweaks", "tweaks"], "tried again once, then announced"


def test_an_interrupt_is_a_stop_too() -> None:
    """Ctrl-C in Terminal: `Stopped`, so the run still ends with `update-end`."""
    with pytest.raises(Stopped) as raised, StopGate(lambda *_: None).installed():
        _interrupt()

    assert raised.value.code == 128 + signal.SIGINT


def test_an_interrupt_during_a_deferred_step_waits_for_it() -> None:
    finished: list[bool] = []
    with StopGate(lambda *_: None).installed() as gate:
        with pytest.raises(Stopped) as raised:
            with gate.deferring(None, "tweaks"):
                _interrupt()
                finished.append(True)

    assert finished == [True]
    assert raised.value.code == 128 + signal.SIGINT


def test_an_interrupt_that_arrived_ignored_stays_ignored() -> None:
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with StopGate(lambda *_: None).installed():
            assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN
    finally:
        signal.signal(signal.SIGINT, previous)


def test_under_an_update_a_device_session_takes_an_interrupt_as_a_stop() -> None:
    """So a second Ctrl-C during a held install cancels it at an await, as a
    second SIGTERM does, instead of tearing through the event loop."""
    handlers: dict[str, object] = {}

    async def look() -> None:
        handlers["int"] = signal.getsignal(signal.SIGINT)
        handlers["term"] = signal.getsignal(signal.SIGTERM)

    with StopGate(lambda *_: None).installed() as gate:
        gate.hold("example", "install")
        device_session.run(look())

    assert handlers["int"] is handlers["term"], "the session's own handler, for both"


def test_without_an_update_a_device_session_leaves_an_interrupt_alone() -> None:
    handlers: dict[str, object] = {}

    async def look() -> None:
        handlers["int"] = signal.getsignal(signal.SIGINT)

    before = signal.getsignal(signal.SIGINT)
    device_session.run(look())

    assert handlers["int"] is before
