from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa.config import load_config
from graft_ipa.devices import BriefDevice
from graft_ipa.json_output import JsonEmitter


def test_brief_lists_udids_without_describing_them() -> None:
    """The point of `--brief` is what it does NOT do.

    A full listing opens a lockdown session per phone (~0.3s each) to read the
    name, model and iOS version. `--brief` answers only "which UDIDs are on the
    bus", which is all the GUI's poll needs to decide whether to escalate.

    No `cfg`: `_do_devices_brief` takes none, because nothing in it ever
    consulted configuration.
    """
    from graft_ipa import cli as cli_mod

    fake = [
        BriefDevice(udid="00008030-0000000000000001", connection_type="Network"),
        BriefDevice(udid="00008030-0000000000000003", connection_type="USB"),
    ]
    buf = StringIO()
    with patch.object(cli_mod, "list_devices_brief", return_value=fake) as brief:
        with patch.object(cli_mod, "list_devices") as full:
            cli_mod._do_devices_brief(JsonEmitter(stream=buf))

    brief.assert_called_once()
    full.assert_not_called()

    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    devices = [e for e in events if e["type"] == "device-brief"]
    assert len(devices) == 2
    assert devices[0] == {
        "type": "device-brief",
        "udid": "00008030-0000000000000001",
        "connection_type": "Network",
    }
    end = [e for e in events if e["type"] == "devices-end"]
    assert end == [{"type": "devices-end", "count": 2, "failed": 0}]


def test_the_brief_event_is_not_the_full_one_under_a_short_payload() -> None:
    """`device-brief`, never `device`. This is the whole of fix 1.

    Swift's `DeviceRecord` makes `name`, `model`, `ios_version` and
    `connection_type` non-optional, so a two-field payload sent as `device`
    decodes to nil and `CLIRunner`'s `try?` drops it — the phone simply is not
    there, with nothing on any channel to say why.

    Asserted as "no `device` event at all", not just "a `device-brief` event
    exists": emitting both would satisfy the test above and still put a line on
    the wire that the GUI's decoder rejects.
    """
    from graft_ipa import cli as cli_mod

    buf = StringIO()
    with patch.object(
        cli_mod,
        "list_devices_brief",
        return_value=[BriefDevice(udid="00008130-A", connection_type="USB")],
    ):
        cli_mod._do_devices_brief(JsonEmitter(stream=buf))

    types = [json.loads(line)["type"] for line in buf.getvalue().splitlines() if line.strip()]
    assert types == ["device-brief", "devices-end"]


def test_brief_emits_no_failures_because_it_cannot_have_any() -> None:
    """`DeviceLookupFailure` describes a phone that answered usbmux and then
    failed to describe. `--brief` never describes, so the category is empty by
    construction — `failed: 0` is a fact about the verb, not a lucky run."""
    from graft_ipa import cli as cli_mod

    buf = StringIO()
    with patch.object(cli_mod, "list_devices_brief", return_value=[]):
        cli_mod._do_devices_brief(JsonEmitter(stream=buf))
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    assert events == [{"type": "devices-end", "count": 0, "failed": 0}]


def test_the_console_arm_says_so_when_nothing_is_connected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The arm a person reads printed nothing at all for the commonest case.

    `_print_devices` says "no devices connected" for the full listing; this one
    exited silently, which reads as a command that did nothing rather than one
    that found nothing.
    """
    from graft_ipa import cli as cli_mod

    with patch.object(cli_mod, "list_devices_brief", return_value=[]):
        cli_mod._do_devices_brief(None)

    assert "no devices connected" in capsys.readouterr().out


def test_the_full_path_is_untouched(temp_config_env: Path) -> None:
    """`--brief` is additive. Without it, `_do_devices` still describes."""
    from graft_ipa import cli as cli_mod
    from graft_ipa.devices import DeviceListing

    cfg = load_config(temp_config_env)
    buf = StringIO()
    with patch.object(cli_mod, "list_devices", return_value=DeviceListing([], [])) as full:
        with patch.object(cli_mod, "list_devices_brief") as brief:
            cli_mod._do_devices(cfg, emitter=JsonEmitter(stream=buf))
    full.assert_called_once()
    brief.assert_not_called()


def test_the_verb_dispatches_the_two_arms_apart(temp_config_env: Path) -> None:
    """`_verb_devices` is the only thing that still knows about `--brief`.

    Pinned because the flag's routing moved out of `_do_devices` and into the
    verb: with the dispatch inverted, `--devices --brief` would open a lockdown
    session per phone and `--devices` would answer with two fields, and every
    test above would still pass.
    """
    from graft_ipa import cli as cli_mod

    cfg = load_config(temp_config_env)
    with patch.object(cli_mod, "_do_devices_brief") as brief:
        with patch.object(cli_mod, "_do_devices") as full:
            cli_mod._verb_devices(cfg=cfg, json_mode=False, brief=True)
            brief.assert_called_once()
            full.assert_not_called()

            brief.reset_mock()
            cli_mod._verb_devices(cfg=cfg, json_mode=False, brief=False)
            full.assert_called_once()
            brief.assert_not_called()


def test_brief_is_rejected_as_a_stray_flag_with_a_different_verb(
    temp_config_env: Path,
) -> None:
    """`--brief` is registered in `_VERB_ONLY_FLAGS` and only `--devices` lists
    it in `accepts`. Passed alongside a verb below the stray-flag gate that
    does not accept it, it must be rejected (exit 2) rather than silently
    discarded — the failure mode `--compression-level example` used to have
    before these five were added to the guard (see
    test_cli_app_options.test_set_app_options_only_flags_are_rejected_without_a_verb)."""
    from typer.testing import CliRunner

    from graft_ipa.cli import app as cli_app

    result = CliRunner().invoke(cli_app, ["--brief", "--prune-builds"])

    assert result.exit_code == 2, result.output
    assert "--brief" in result.output
    assert "is not valid with --prune-builds" in result.output
