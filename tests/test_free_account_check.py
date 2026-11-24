"""`--check` on a free Apple ID, and the mode on the wire.

A free config has no signing identity, and that is information, not a failure:
no zsign, .p12, profile or password rows. What a LiveContainer build does need
is still checked — cyan and a device — plus one thing only this mode asks:
that a LiveContainer is on the device, found the way the push finds it.

The account mode reaches the Mac app on two events: every `app` from `--list`
(what it reads at launch) and `preflight-end` from `--check` (which exists
even with no apps configured).
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
from graft_ipa.lc_runner import LiveContainerError
from tests.tool_fakes import fake_path

PRIMARY = "com.kdt.livecontainer.ABCDE12345"
LC2 = "com.kdt.LiveContainer2.ABCDE12345"
OTHER_PRIMARY = "com.kdt.livecontainer.FGHIJ67890"


def _free(tmp_path: Path, apps: dict | None = None, **extra) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"apps": apps or {}, **extra}))
    return path


@pytest.fixture
def tools_and_device(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every tool present, one phone connected. Returns the tools looked up."""
    looked_up: list[str] = []
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin", seen=looked_up))
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: ["UDID1"])
    return looked_up


def _installed(*ids: str):
    return lambda udid: [*ids, "com.example.app"]


def _rows(cfg, installed) -> dict[str, preflight.CheckResult]:
    return {r.name: r for r in preflight.run_free_account_checks(cfg, installed_apps=installed)}


def test_no_signing_reads_as_information(tmp_path: Path, tools_and_device) -> None:
    rows = _rows(load_config(_free(tmp_path)), _installed(PRIMARY))

    account = rows["account"]
    assert account.ok is True
    assert account.info is True
    assert account.status == "INFO"
    assert account.detail == "free Apple ID: LiveContainer delivery only"


def test_signing_files_and_zsign_are_not_checked(tmp_path: Path, tools_and_device) -> None:
    rows = _rows(load_config(_free(tmp_path)), _installed(PRIMARY))

    assert set(tools_and_device) == {"cyan"}
    for absent in ("zsign", "security", "ideviceinstaller", "signing.cert (.p12)",
                   "signing.team_id", ".p12 password", preflight.PROFILE_CHECK_NAME):
        assert absent not in rows
    assert all(r.ok for r in rows.values())


def test_cyan_and_a_device_are_still_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tools, "which", lambda tool: None)
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: [])

    rows = _rows(load_config(_free(tmp_path)), _installed(PRIMARY))

    assert rows["cyan"].ok is False
    assert rows["connected ios device(s)"].ok is False
    # No device, no LiveContainer row: the device row already says why.
    assert preflight.LIVECONTAINER_CHECK_NAME not in rows


def test_a_livecontainer_on_the_device_passes_and_names_it(tmp_path: Path, tools_and_device) -> None:
    row = _rows(load_config(_free(tmp_path)), _installed(PRIMARY, LC2))[
        preflight.LIVECONTAINER_CHECK_NAME
    ]

    assert row.ok is True
    assert row.detail == f"UDID1: {PRIMARY} (also installed: {LC2})"


def test_no_livecontainer_fails_and_points_at_the_free_setup(tmp_path: Path, tools_and_device) -> None:
    row = _rows(load_config(_free(tmp_path)), _installed())[preflight.LIVECONTAINER_CHECK_NAME]

    assert row.ok is False
    assert row.detail == "UDID1: no LiveContainer found on the device"
    assert "SideStore" in row.hint and "iloader" in row.hint


def test_two_primaries_fail_until_the_apps_are_pinned(tmp_path: Path, tools_and_device) -> None:
    unpinned = _rows(
        load_config(_free(tmp_path, {"example": {"bundle_id": "com.example.app"}})),
        _installed(PRIMARY, OTHER_PRIMARY),
    )[preflight.LIVECONTAINER_CHECK_NAME]
    pinned = _rows(
        load_config(_free(tmp_path, {
            "example": {"bundle_id": "com.example.app", "lc_bundle_id": OTHER_PRIMARY},
        })),
        _installed(PRIMARY, OTHER_PRIMARY),
    )[preflight.LIVECONTAINER_CHECK_NAME]

    assert unpinned.ok is False
    assert PRIMARY in unpinned.detail and OTHER_PRIMARY in unpinned.detail
    assert "--lc-bundle-id" in unpinned.hint
    assert pinned.ok is True
    assert pinned.detail == f"UDID1: {OTHER_PRIMARY} (also installed: {PRIMARY})"


def test_an_explicit_sideload_app_is_a_failed_row_with_the_fix(
    tmp_path: Path, tools_and_device
) -> None:
    """A free config may carry `delivery: sideload` apps over from a paid one.
    Every build of them is refused, so a green `--check` would be a lie."""
    cfg = load_config(_free(tmp_path, {
        "example": {"bundle_id": "com.example.app"},                       # livecontainer
        "side": {"bundle_id": "com.example.side", "delivery": "sideload"},
        "other": {"bundle_id": "com.example.other", "delivery": "livecontainer"},
        "bravo": {"bundle_id": "com.example.bravo", "delivery": "sideload"},
    }))

    rows = preflight.run_free_account_checks(cfg, installed_apps=_installed(PRIMARY))
    failed = [r for r in rows if not r.ok]

    assert [r.name for r in failed] == ["app 'bravo'", "app 'side'"]
    side = failed[1]
    assert side.detail.startswith(
        "delivery: sideload (app 'side') needs a paid Apple Developer Program membership"
    )
    assert side.hint.startswith("graft --set-app-options --app side --delivery livecontainer")


def test_a_free_check_with_a_sideload_app_fails(
    tmp_path: Path, tools_and_device, device_seam: list[str]
) -> None:
    cfg = load_config(_free(tmp_path, {
        "side": {"bundle_id": "com.example.side", "delivery": "sideload"},
    }))

    assert cli_mod._verb_check(cfg=cfg, json_mode=True) != 0


def test_a_phone_that_lists_no_apps_at_all_is_asked_to_unlock(
    tmp_path: Path, tools_and_device
) -> None:
    """A locked phone answers the listing with nothing, not even system apps.
    That is no answer, the same evidence the build path now reads as "locked",
    so --check must not tell a user with LiveContainer installed to set it up."""
    row = _rows(load_config(_free(tmp_path)), lambda udid: [])[preflight.LIVECONTAINER_CHECK_NAME]

    assert row.ok is False
    assert row.detail == "UDID1: the device listed no apps at all, which is how a locked phone answers"
    assert "unlock" in row.hint and "graft --check" in row.hint
    assert "SideStore" not in row.hint


def test_a_device_that_will_not_list_its_apps_is_a_failed_row(
    tmp_path: Path, tools_and_device
) -> None:
    def unreachable(udid: str):
        raise LiveContainerError(f"could not talk to {udid}: locked", hint="unlock it")

    row = _rows(load_config(_free(tmp_path)), unreachable)[preflight.LIVECONTAINER_CHECK_NAME]

    assert row.ok is False
    assert "could not talk to UDID1" in row.detail
    assert row.hint == "unlock it"


def test_the_default_device_is_the_one_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tools_and_device
) -> None:
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: ["UDID1", "UDID2"])
    asked: list[str] = []

    def installed(udid: str):
        asked.append(udid)
        return [PRIMARY]

    preflight.run_free_account_checks(
        load_config(_free(tmp_path, default_device_udid="UDID2")), installed_apps=installed
    )

    assert asked == ["UDID2"]


# --- the JSON the app reads -------------------------------------------------------


@pytest.fixture
def device_seam(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`preflight._installed_bundle_ids`, the one seam between `--check` and
    the phone, answering with a primary. Returns the udids it was asked about.

    The real device class is replaced with one that fails the test, so a
    seam that is not looked up (a default argument bound at import, which is
    what this once was) shows up as a failure here, not as a usbmux call."""
    from graft_ipa import lc_manage

    asked: list[str] = []

    def installed(udid: str) -> list[str]:
        asked.append(udid)
        return [PRIMARY]

    class NoRealDevice:
        def __getattr__(self, name: str):
            pytest.fail(f"--check reached the real device ({name}) past the seam")

    monkeypatch.setattr(preflight, "_installed_bundle_ids", installed)
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", NoRealDevice)
    return asked


def _check_events(cfg) -> list[dict]:
    out = StringIO()
    cli_mod._do_check(cfg, emitter=JsonEmitter(stream=out))
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_check_json_carries_the_free_mode(
    tmp_path: Path, tools_and_device, device_seam: list[str]
) -> None:
    events = _check_events(load_config(_free(tmp_path)))

    assert device_seam == ["UDID1"], "the module seam is what --check asked"
    lc_row = next(e for e in events if e.get("name") == preflight.LIVECONTAINER_CHECK_NAME)
    assert lc_row["ok"] is True and lc_row["detail"] == f"UDID1: {PRIMARY}"
    account = next(e for e in events if e.get("name") == "account")
    assert account == {
        "type": "preflight-check", "name": "account", "ok": True,
        "detail": "free Apple ID: LiveContainer delivery only", "info": True,
        "install": None,
    }
    assert events[-1] == {
        "type": "preflight-end", "ok_count": len(events) - 1, "fail_count": 0,
        "account": "free",
    }


def test_check_livecontainer_looks_the_seam_up_when_it_runs(
    tmp_path: Path, device_seam: list[str]
) -> None:
    [row] = preflight.check_livecontainer(load_config(_free(tmp_path)), ["UDID1"])

    assert (row.ok, row.detail) == (True, f"UDID1: {PRIMARY}")
    assert device_seam == ["UDID1"]


def test_check_json_carries_the_paid_mode(
    temp_config_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_mod, "run_all_checks", lambda cfg, **_: [])
    out = StringIO()

    cli_mod._do_check(load_config(temp_config_env), emitter=JsonEmitter(stream=out))

    [end] = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert end["account"] == "paid"


def test_a_free_check_passes_with_exit_zero(
    tmp_path: Path, tools_and_device, device_seam: list[str]
) -> None:
    """A free Apple ID with everything in place is a passing `--check`, not a
    failing one with the signing rows explained away."""
    cfg = load_config(_free(tmp_path))

    assert cli_mod._verb_check(cfg=cfg, json_mode=True) == 0
    assert device_seam == ["UDID1"]


def test_the_human_table_says_info(
    tmp_path: Path, tools_and_device, device_seam: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from rich.console import Console

    out = StringIO()
    monkeypatch.setattr(cli_mod, "console", Console(file=out, width=200))

    cli_mod._do_check(load_config(_free(tmp_path)))

    line = next(ln for ln in out.getvalue().splitlines() if "free Apple ID" in ln)
    assert "INFO" in line and "FAIL" not in line


@pytest.mark.parametrize(("free", "account"), [(True, "free"), (False, "paid")])
def test_every_app_event_carries_the_account(
    free: bool, account: str, temp_config_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--list --json` is what `ConfigStore` runs at launch."""
    if free:
        raw = yaml.safe_load(temp_config_env.read_text())
        del raw["signing"]
        temp_config_env.write_text(yaml.safe_dump(raw))
    cfg = load_config(temp_config_env)

    cli_mod._verb_list_apps(cfg=cfg, json_mode=True)

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    apps = [e for e in events if e["type"] == "app"]
    assert apps and all(e["account"] == account for e in apps)
