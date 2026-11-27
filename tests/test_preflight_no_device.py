"""`--check` with no iPhone or iPad connected: information, not a failure.

Settings › Preflight shows `--check`'s rows, and a red Fail for "no phone
plugged in right now" reads as a broken setup to someone who has just
installed Graft. The device row is then an `info` row ("not connected"),
which the app draws as information and counts as neither OK nor Fail, and
which a CLI from before it already sent for a free account's `account` row.

A real failure stays one: a build that installs needs a device, so its
preflight still fails without one, and a `--check` that couldn't ask macOS
which devices are connected (`preflight.usbmux_problem`) fails too.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa import cli as cli_mod
from graft_ipa import preflight, tools
from graft_ipa.config import load_config
from graft_ipa.json_output import JsonEmitter
from tests.tool_fakes import fake_path

NAME = preflight.DEVICES_CHECK_NAME


@pytest.fixture
def usbmuxd_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS answered: there is no device, rather than no answer. (Every
    test's usbmuxd is one that isn't there, `conftest._no_real_usbmuxd`.)"""
    monkeypatch.setattr(preflight, "usbmux_problem", lambda: None)


@pytest.fixture
def no_device(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [])


def _not_asked() -> str | None:
    raise AssertionError("usbmuxd was asked again, though nothing turns on its answer")


def _free(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"apps": {}}))
    return path


def _events(cfg) -> tuple[bool, list[dict]]:
    out = StringIO()
    ok = cli_mod._do_check(cfg, emitter=JsonEmitter(stream=out))
    return ok, [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


# --- The row ------------------------------------------------------------------------


def test_no_device_where_none_is_needed_is_information(
    temp_config_env: Path, usbmuxd_answers: None
) -> None:
    (row,) = preflight.check_devices(load_config(temp_config_env), udids=[], required=False)

    assert (row.name, row.ok, row.info, row.status) == (NAME, True, True, "INFO")
    assert row.detail == preflight.NOT_CONNECTED_DETAIL
    assert row.detail.startswith("not connected")
    assert row.hint == ""
    assert row.install is None


def test_no_device_where_one_is_needed_still_fails(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that installs: as before, and usbmuxd isn't asked twice."""
    monkeypatch.setattr(preflight, "usbmux_problem", _not_asked)

    (row,) = preflight.check_devices(load_config(temp_config_env), udids=[])

    assert (row.name, row.ok, row.info, row.status) == (NAME, False, False, "FAIL")
    assert row.detail == "none"
    assert row.hint == "plug an iPhone or iPad in via USB and trust this Mac"


def test_a_device_listing_that_failed_is_still_a_failure(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No answer from macOS is not "not connected": Graft couldn't look."""
    monkeypatch.setattr(preflight, "usbmux_problem",
                        lambda: "ConnectionFailedToUsbmuxdError: no usbmuxd")

    (row,) = preflight.check_devices(load_config(temp_config_env), udids=[], required=False)

    assert (row.name, row.ok, row.info, row.status) == (NAME, False, False, "FAIL")
    assert row.detail == ("couldn't ask macOS which devices are connected: "
                          "ConnectionFailedToUsbmuxdError: no usbmuxd")
    assert "restart this Mac" in row.hint


def test_a_connected_device_passes_either_way(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preflight, "usbmux_problem", _not_asked)
    cfg = load_config(temp_config_env)
    for required in (True, False):
        row = preflight.check_devices(cfg, udids=["UDID1"], check_profile=False, required=required)[0]
        assert (row.name, row.ok, row.info, row.detail) == (NAME, True, False, "UDID1")


# --- usbmuxd's answer ---------------------------------------------------------------


def test_usbmuxd_that_answers_is_no_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing_connected():
        return []

    monkeypatch.setattr("pymobiledevice3.usbmux.list_devices", nothing_connected)
    assert preflight.usbmux_problem() is None


def test_usbmuxd_that_doesnt_answer_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    async def unreachable():
        raise ConnectionRefusedError("usbmuxd")

    monkeypatch.setattr("pymobiledevice3.usbmux.list_devices", unreachable)
    assert preflight.usbmux_problem() == "ConnectionRefusedError: usbmuxd"


# --- --check, and a build -------------------------------------------------------------


def test_check_on_a_free_account_with_no_device_passes_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_device: None, usbmuxd_answers: None
) -> None:
    """Everything else in order: `--check` passes (exit 0), and the device row
    is information on the wire, which the app already draws as such."""
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))

    ok, events = _events(load_config(_free(tmp_path)))

    assert ok is True
    row = next(e for e in events if e.get("name") == NAME)
    assert row == {
        "type": "preflight-check", "name": NAME, "ok": True,
        "detail": preflight.NOT_CONNECTED_DETAIL, "install": None, "info": True,
    }
    # No device, no LiveContainer row.
    assert preflight.LIVECONTAINER_CHECK_NAME not in {e.get("name") for e in events}
    assert events[-1] == {
        "type": "preflight-end", "ok_count": len(events) - 1, "fail_count": 0, "account": "free",
    }


def test_check_on_a_paid_account_with_no_device_shows_it_as_information(
    temp_config_env: Path, no_device: None, usbmuxd_answers: None
) -> None:
    _, events = _events(load_config(temp_config_env))

    row = next(e for e in events if e.get("name") == NAME)
    assert (row["ok"], row.get("info"), row["detail"]) == (True, True, preflight.NOT_CONNECTED_DETAIL)
    assert "hint" not in row
    end = events[-1]
    assert end["fail_count"] == sum(1 for e in events[:-1] if not e["ok"])


def test_check_with_no_answer_from_usbmuxd_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_device: None
) -> None:
    """The test suite's own usbmuxd, which isn't there."""
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))

    ok, events = _events(load_config(_free(tmp_path)))

    assert ok is False
    row = next(e for e in events if e.get("name") == NAME)
    assert row["ok"] is False and "info" not in row
    assert row["detail"].startswith("couldn't ask macOS which devices are connected: ")
    assert events[-1]["fail_count"] == 1


@pytest.mark.parametrize("delivery", ["livecontainer", "sideload"])
def test_a_build_that_installs_still_needs_a_device(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch, no_device: None, delivery: str
) -> None:
    monkeypatch.setattr(preflight, "usbmux_problem", _not_asked)

    rows = preflight.run_all_checks(load_config(temp_config_env), delivery=delivery,
                                    will_install=True)

    row = next(r for r in rows if r.name == NAME)
    assert (row.ok, row.info) == (False, False)
