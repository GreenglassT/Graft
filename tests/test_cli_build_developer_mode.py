"""A development-signed build stops before stage 2 when the phone it installs
on has Developer Mode off (spec §5). Null never stops it, and nothing but an
install signed with a development profile reads it (`developer_mode.needed`).
A real build refused for it first has the phone show the switch (AMFI's
reveal); a dry run doesn't.

`_build_one` runs for real here, and so do the gate, `require_on` and the
read. Stubbed: the preflight, the device listing, the acquire, and the phone
itself, a stand-in lockdown session and a stand-in `AmfiService`. The
acquire raising is the proof a build got past the gate, so every build in
`_build` fails, and `_build_one` raises its failure after `build-end`, as it
does for any failed build. The profile is real, a development one unless a
test says otherwise: the gate reads it.
"""

from __future__ import annotations

import json
import plistlib
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import developer_mode as developer_mode_mod
from graft_ipa import device_info as device_info_mod
from graft_ipa import devices_seen
from graft_ipa.build_log import BuildLog
from graft_ipa.config import AppConfig, AppStoreConnectConfig, Config, CyanOptions, SigningConfig
from graft_ipa.errors import DeveloperModeOffError, GraftError, InstallError, IPANotFoundError
from graft_ipa.json_output import JsonEmitter

UDID = "00008030-0000000000000001"
CHECKING = f"checking Developer Mode on {UDID}"
REVEALED = "Graft has made the option visible"


def _config(
    tmp_path: Path, delivery: str, *, profile: str = "development", auto_profile: bool = False,
) -> Config:
    """`profile` is what the global profile file holds: `development`
    (get-task-allow true), `ad-hoc` (false, as enterprise is too), or
    `unreadable`. `auto_profile` signs with App Store Connect's instead,
    which isn't on disk."""
    path = tmp_path / "profile"
    if profile == "unreadable":
        path.write_bytes(b"not a profile")
    else:
        path.write_bytes(plistlib.dumps({
            "Name": "Example",
            "TeamIdentifier": ["ABCDE12345"],
            "Entitlements": {"get-task-allow": profile == "development"},
        }, fmt=plistlib.FMT_XML))
    return Config(
        path=tmp_path / "config.yaml",
        signing=SigningConfig(
            cert=tmp_path / "cert.p12", cert_password_env="X",
            profile=path, team_id="ABCDE12345",
        ),
        cache_dir=tmp_path / "cache",
        built_dir=tmp_path / "built",
        tweaks_dir=tmp_path / "tweaks",
        state_dir=tmp_path / "state",
        apps={"example": AppConfig(
            name="example", bundle_id="com.example.app", preferred_version="latest",
            tweaks=[], cyan=CyanOptions(), delivery=delivery, auto_profile=auto_profile,
        )},
        app_store_connect=AppStoreConnectConfig(
            key_id="KEY", issuer_id="ISSUER", key_file=tmp_path / "key.p8",
            device_udids=[UDID], profile_cache_dir=tmp_path / "profiles",
        ),
    )


def _phone(answer: object) -> AsyncMock:
    """A stand-in lockdown session on iOS 26 that says `answer` for
    Developer Mode (None, or anything not a bool, is no answer), with no
    name to give."""
    lock = AsyncMock()
    lock.product_version = "26.1"
    lock.display_name = None
    lock.all_values = None

    async def get_value(domain=None, key=None):
        if (domain, key) == ("com.apple.security.mac.amfi", "DeveloperModeStatus"):
            return answer
        return {}

    lock.get_value = AsyncMock(side_effect=get_value)
    lock.close = AsyncMock()
    return lock


@dataclass
class Phone:
    """The stand-ins `_on_a_phone` installs, and what they saw."""

    lock: AsyncMock
    create: AsyncMock
    #: The session each reveal was sent on.
    revealed: list[object] = field(default_factory=list)


def _on_a_phone(answer: object, *, reveal_fails: bool = False):
    phone = Phone(lock=_phone(answer), create=AsyncMock())
    phone.create.return_value = phone.lock

    class Amfi:
        def __init__(self, lockdown: object) -> None:
            self._lockdown = lockdown

        async def reveal_developer_mode_option_in_ui(self) -> None:
            phone.revealed.append(self._lockdown)
            if reveal_fails:
                raise RuntimeError("The device did not reveal the Developer Mode setting")

    patches = (
        patch.object(device_info_mod, "create_using_usbmux", new=phone.create),
        patch.object(device_info_mod, "AmfiService", new=Amfi),
    )
    return phone, patches


@dataclass
class Run:
    cfg: Config
    end: dict
    events: list[dict]
    #: Which failure `_build_one` raised: the gate's, or stage 2's.
    raised: type
    phone: Phone
    selecting: MagicMock
    acquire: MagicMock

    def said_checking(self) -> bool:
        return any(e["type"] == "log" and e["line"] == CHECKING for e in self.events)

    def warnings(self) -> list[str]:
        return [e["line"] for e in self.events if e["type"] == "log" and e["source"] == "warning"]


def _build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], *, delivery: str = "sideload",
    answer: object = False, skip_install: bool = False, dry_run: bool = False,
    device: str | None = None, select: object = cli_mod.Target(UDID, None, "usb"), profile: str = "development",
    auto_profile: bool = False, reveal_fails: bool = False,
) -> Run:
    cfg = _config(tmp_path, delivery, profile=profile, auto_profile=auto_profile)
    phone, on_the_phone = _on_a_phone(answer, reveal_fails=reveal_fails)
    if isinstance(select, BaseException):
        selecting = MagicMock(side_effect=select)
    else:
        selecting = MagicMock(return_value=select)
    acquire = MagicMock(side_effect=IPANotFoundError("no IPA found for example"))
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "resolve_target", selecting), \
         patch.object(cli_mod, "acquire_ipa", acquire), \
         on_the_phone[0], on_the_phone[1], \
         pytest.raises(GraftError) as failed:
        cli_mod._build_one(
            cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
            dry_run=dry_run, skip_install=skip_install, skip_decrypt_check=False,
            verbose=False, device=device, emitter=JsonEmitter(),
        )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]
    (end,) = [e for e in events if e["type"] == "build-end"]
    return Run(cfg, end, events, type(failed.value), phone, selecting, acquire)


def test_off_stops_a_development_signed_build_before_it_starts(tmp_path: Path, capsys) -> None:
    run = _build(tmp_path, capsys, answer=False)

    assert run.end["ok"] is False
    assert f"Developer Mode is off on {UDID}" in run.end["error"]
    assert run.raised is DeveloperModeOffError
    run.phone.create.assert_awaited_once_with(serial=UDID, autopair=False)
    assert devices_seen.seen(run.cfg.state_dir, UDID) == {"developer_mode": False}
    run.acquire.assert_not_called()


def test_a_real_refused_build_first_reveals_the_switch(tmp_path: Path, capsys) -> None:
    """In the session that read it, and the refusal says the switch is there."""
    run = _build(tmp_path, capsys, answer=False)

    assert run.phone.revealed == [run.phone.lock]
    assert developer_mode_mod.turn_on(revealed=True) in run.end["error"]
    assert run.raised is DeveloperModeOffError


def test_a_dry_run_checks_too_and_reveals_nothing(tmp_path: Path, capsys) -> None:
    """It changes nothing on the phone: refused with the plain words."""
    run = _build(tmp_path, capsys, answer=False, dry_run=True)

    assert run.raised is DeveloperModeOffError
    assert developer_mode_mod.TURN_ON in run.end["error"]
    assert REVEALED not in run.end["error"]
    assert run.phone.revealed == []
    run.acquire.assert_not_called()


def test_a_reveal_that_fails_still_refuses_with_the_plain_words(tmp_path: Path, capsys) -> None:
    run = _build(tmp_path, capsys, answer=False, reveal_fails=True)

    assert run.phone.revealed, "premise: it was tried"
    assert run.raised is DeveloperModeOffError
    assert developer_mode_mod.TURN_ON in run.end["error"]
    assert REVEALED not in run.end["error"]
    (warning,) = run.warnings()
    assert "couldn't show the Developer Mode switch" in warning
    run.acquire.assert_not_called()


def test_the_read_is_said_before_it_is_made(tmp_path: Path, capsys) -> None:
    """A Wi-Fi phone asleep can hold the read for seconds: the build's log
    says what it's waiting on."""
    run = _build(tmp_path, capsys, answer=True)

    assert run.said_checking()
    (said,) = [i for i, e in enumerate(run.events) if e.get("line") == CHECKING]
    (ended,) = [i for i, e in enumerate(run.events) if e["type"] == "build-end"]
    assert said < ended


@pytest.mark.parametrize("answer", [True, None, "NO"])
def test_on_or_unknown_builds_on_and_reveals_nothing(tmp_path: Path, capsys, answer: object) -> None:
    run = _build(tmp_path, capsys, answer=answer)

    run.phone.create.assert_awaited_once()
    assert run.phone.revealed == []
    run.acquire.assert_called_once()
    assert "no IPA found" in run.end["error"], "stage 2 ran: the gate let it through"
    assert run.raised is IPANotFoundError


def test_an_auto_provisioned_profile_is_checked_before_it_exists(tmp_path: Path, capsys) -> None:
    """App Store Connect makes it at stage 4, and only ever a development
    one: the gate doesn't wait for the file."""
    run = _build(tmp_path, capsys, answer=False, profile="ad-hoc", auto_profile=True)

    assert run.raised is DeveloperModeOffError
    run.acquire.assert_not_called()


def test_an_ad_hoc_or_enterprise_profile_builds_with_it_off(tmp_path: Path, capsys) -> None:
    """Their apps open with Developer Mode off: only a development profile
    (`get-task-allow: true`) needs it. Nor is the phone read: the build's
    target is still resolved, from usbmux alone (`installer.resolve_target`),
    but no session is opened."""
    run = _build(tmp_path, capsys, answer=False, profile="ad-hoc")

    run.phone.create.assert_not_awaited()
    assert not run.said_checking()
    assert run.phone.revealed == []
    run.acquire.assert_called_once()


def test_a_profile_graft_cannot_read_is_never_refused(tmp_path: Path, capsys) -> None:
    """`needed` says None: pre-flight's to report (it's stubbed here), never
    a Developer Mode refusal over a file nobody read."""
    run = _build(tmp_path, capsys, answer=False, profile="unreadable")

    run.phone.create.assert_not_awaited()
    run.acquire.assert_called_once()


def test_livecontainer_delivery_never_reads_it(tmp_path: Path, capsys) -> None:
    run = _build(tmp_path, capsys, delivery="livecontainer", answer=False)

    run.phone.create.assert_not_awaited()
    run.acquire.assert_called_once()


def test_a_skipped_install_never_reads_it(tmp_path: Path, capsys) -> None:
    run = _build(tmp_path, capsys, answer=False, skip_install=True)

    run.phone.create.assert_not_awaited()
    run.acquire.assert_called_once()


def test_it_reads_the_phone_the_build_installs_on(tmp_path: Path, capsys) -> None:
    chosen = "00008030-0000000000000009"
    run = _build(tmp_path, capsys, answer=True, device=chosen,
                 select=cli_mod.Target(chosen, None, "network"))

    run.selecting.assert_called_once_with(run.cfg, run.cfg.apps["example"], explicit_udid=chosen)
    run.phone.create.assert_awaited_once_with(serial=chosen, autopair=False)


def test_no_phone_reads_nothing_and_stops_the_build(tmp_path: Path, capsys) -> None:
    """No target is the build's failure before the gate (`_planned_target`):
    nothing is read, and nothing is built for a phone that isn't there."""
    run = _build(tmp_path, capsys, select=InstallError("no iOS devices connected"))

    run.phone.create.assert_not_awaited()
    assert not run.said_checking()
    assert run.raised is InstallError
    run.acquire.assert_not_called()


def test_the_install_goes_to_the_phone_that_was_read(tmp_path: Path, capsys) -> None:
    """One `resolve_target`, before the pre-flight. Stage 5, minutes later,
    installs on what it gave rather than resolving again, which could pick another phone
    than the one whose Developer Mode was read. A dry run, so the stages
    between run as far as their stubs."""
    cfg = _config(tmp_path, "sideload")
    chosen = cli_mod.Target("00008030-0000000000000009", None, "network")
    selecting = MagicMock(return_value=chosen)
    cached = MagicMock(path=tmp_path / "in.ipa", version="1.0")
    cyan = MagicMock()
    cyan.return_value.configure_mock(output_ipa=tmp_path / "tweaked.ipa", stdout="", stderr="")
    zsign = MagicMock()
    zsign.return_value.configure_mock(profile_notes=(), stdout="", stderr="")
    install = MagicMock()
    install.return_value.configure_mock(stdout="", stderr="")
    phone, on_the_phone = _on_a_phone(True)
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "resolve_target", selecting), \
         patch.object(cli_mod, "acquire_ipa", return_value=cached), \
         patch.object(cli_mod, "run_cyan", cyan), \
         patch.object(cli_mod, "run_zsign", zsign), \
         patch.object(cli_mod, "install", install), \
         on_the_phone[0], on_the_phone[1]:
        cli_mod._build_one(
            cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
            dry_run=True, skip_install=False, skip_decrypt_check=True,
            verbose=False, device=None, emitter=JsonEmitter(),
        )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()
              if line.startswith("{")]

    assert [e["ok"] for e in events if e["type"] == "build-end"] == [True]
    phone.create.assert_awaited_once_with(serial=chosen.udid, autopair=False)
    selecting.assert_called_once()
    install.assert_called_once()
    assert install.call_args.kwargs["udid"] == chosen.udid


def test_the_device_check_never_reveals(temp_config_env: Path) -> None:
    """`--device-info` reads Developer Mode and says Off, and asks the phone
    for nothing."""
    from graft_ipa.config import load_config

    phone, on_the_phone = _on_a_phone(False)
    buf = StringIO()
    with on_the_phone[0], on_the_phone[1]:
        cli_mod._do_device_info(
            load_config(temp_config_env), udid=UDID, emitter=JsonEmitter(stream=buf),
        )

    (event,) = [e for e in map(json.loads, buf.getvalue().splitlines()) if e["type"] == "device-info"]
    assert event["developer_mode"] is False
    phone.create.assert_awaited_once()
    assert phone.revealed == []
