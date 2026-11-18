"""What Graft may overwrite in a LiveContainer's Documents, and when it says so (F11, R8).

A same-named file Graft has no record of pushing is the user's, and a push
refuses it. Three ways that rule answered wrongly:

- F11: `--lc-clean` removed the pushed IPAs but kept their delivery records,
  so a file the user later put there under the same name was "ours" and was
  overwritten without a word. A clean now retires the records of what it
  removed.
- R8: a record is not the only evidence. After a state reset (no log.sqlite),
  Graft's own `<app>.ipa` on the phone has no record, and the push refused it;
  a file whose SHA-256 is one of this app's archived builds is ours whatever
  the records say. And a 1.0-era `AppleDevelopment.p12` (pushed before cert
  pushes were recorded) refused `--lc-push-cert` for good; one whose bytes are
  the configured .p12's is ours.
- R8: the push target's conflict was found at stage 5, after a full cyan run;
  it is now checked before cyan.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import lc_manage, lc_runner
from graft_ipa.build_log import BuildLog
from graft_ipa.lc_runner import LiveContainerError

from .test_cli_build_json import _stub_config
from .test_lc_discovery import fake_device
from .test_lc_install import FakeDevice, Recorder, _make_p12

TEAM = "ABCDE12345"
PRIMARY = f"com.kdt.livecontainer.{TEAM}"
OTHER_LC = f"com.kdt.LiveContainer2.{TEAM}"


def _deliver(log: BuildLog, *, lc: str = PRIMARY, name: str = "example.ipa", udid: str = "u1"):
    log.record_delivery(
        app="example", bundle_id="com.example.app", inputs_digest=None, digest_version=None,
        cyan=None, tweaks=[], source_version="1", ipa_sha256=None, delivery="livecontainer",
        lc_bundle_id=lc, remote_name=name, udid=udid,
    )


# --- F11: a clean retires the records of what it removed ------------------------


def test_forgetting_a_container_s_pushes_leaves_the_others(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    _deliver(log)
    _deliver(log, lc=OTHER_LC, name="bravo.ipa")
    _deliver(log, udid="another-phone")

    log.forget_pushes(udid="u1", bundle_id=PRIMARY)

    assert log.pushed_by_instance("u1") == {OTHER_LC: {"bravo.ipa"}}
    assert log.pushed_by_instance("another-phone") == {PRIMARY: {"example.ipa"}}


def test_a_push_after_the_clean_is_recorded_again(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    _deliver(log)
    log.forget_pushes(udid="u1", bundle_id=PRIMARY)
    _deliver(log)

    assert log.pushed_by_instance("u1") == {PRIMARY: {"example.ipa"}}


def _free_cfg(tmp_path: Path):
    import yaml

    from graft_ipa.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "state_dir": str(tmp_path / "state"), "built_dir": str(tmp_path / "built"),
        "default_device_udid": "u1", "apps": {"example": {"bundle_id": "com.example.app"}},
    }))
    return load_config(path)


def test_after_a_clean_the_users_same_named_file_is_refused(tmp_path, monkeypatch) -> None:
    cfg = _free_cfg(tmp_path)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(log)
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa"]})
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events)

    assert device.removed == [(PRIMARY, "example.ipa")]
    # Their own example.ipa, put there afterwards, is not the one Graft pushed.
    assert log.pushed_by_instance("u1") == {}


def test_a_cert_only_clean_keeps_the_ipa_records(tmp_path, monkeypatch) -> None:
    cfg = _free_cfg(tmp_path)
    log = BuildLog(cfg.state_dir / "log.sqlite")
    _deliver(log)
    log.record_cert_push(udid="u1", bundle_id=PRIMARY, name="AppleDevelopment.p12")
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa", "AppleDevelopment.p12"]})
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events, cert_only=True)

    assert log.pushed_by_instance("u1") == {PRIMARY: {"example.ipa"}}


# --- R8: an archived build's bytes are evidence too ------------------------------


def _ipa(tmp_path: Path, data: bytes, name: str = "example_1.0_20260101T000000.ipa") -> Path:
    path = tmp_path / "built" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_graft_s_own_ipa_without_a_record_is_overwritten(tmp_path, monkeypatch) -> None:
    """A state reset: no records at all, and on the phone exactly the bytes of
    a build Graft archived."""
    old = _ipa(tmp_path, b"the build from last week")
    new = _ipa(tmp_path, b"the build from today", "example_1.1_20260102T000000.ipa")
    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]},
                          contents={(PRIMARY, "example.ipa"): old.read_bytes()})

    lc_runner.push_ipa_to_livecontainer(new, "u1", inner_filename="example.ipa",
                                        archives=[old, new])

    assert written[(PRIMARY, "Documents/example.ipa")] == b"the build from today"


def test_a_same_sized_stranger_is_still_refused(tmp_path, monkeypatch) -> None:
    old = _ipa(tmp_path, b"AAAA")
    new = _ipa(tmp_path, b"BBBB", "example_1.1_x.ipa")
    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]},
                          contents={(PRIMARY, "example.ipa"): b"CCCC"})

    with pytest.raises(LiveContainerError) as exc:
        lc_runner.push_ipa_to_livecontainer(new, "u1", inner_filename="example.ipa",
                                            archives=[old, new])

    assert "did not push" in exc.value.message
    assert written == {}


def test_a_stranger_of_another_size_is_not_even_read(tmp_path, monkeypatch) -> None:
    """Hashing a phone's file means reading all of it over USB or Wi-Fi: only
    when an archive of the same size could match."""
    from .test_lc_discovery import _HouseArrest

    old = _ipa(tmp_path, b"AAAA")
    fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]},
                contents={(PRIMARY, "example.ipa"): b"a much longer file of somebody's"})

    read: list[str] = []

    async def recorded(self, path):
        # Recorded, not raised: the push turns any exception into its own.
        read.append(path)
        return b"a much longer file of somebody's"

    monkeypatch.setattr(_HouseArrest, "get_file_contents", recorded)

    with pytest.raises(LiveContainerError) as exc:
        lc_runner.push_ipa_to_livecontainer(old, "u1", inner_filename="example.ipa",
                                            archives=[old])

    assert "did not push" in exc.value.message
    assert read == [], "read a file no archive could match"


def test_the_build_passes_this_app_s_archives_to_the_push(tmp_path: Path) -> None:
    cfg = _stub_config(tmp_path)
    cfg.built_dir.mkdir(parents=True)
    mine = cfg.built_dir / "example_1.0_a.ipa"
    mine.write_bytes(b"x")
    (cfg.built_dir / "bravo_1.0_a.ipa").write_bytes(b"y")

    target = cli_mod._lc_target(cfg, cfg.apps["example"], log=BuildLog(tmp_path / "l.sqlite"),
                                udid="u1")

    assert target["archives"] == [mine]


# --- R8: the configured .p12 is evidence for the certificate ---------------------


def test_the_configured_p12_already_there_is_ours(tmp_path: Path) -> None:
    """Pushed by 1.0, before cert pushes were recorded: the same bytes."""
    p12, _ = _make_p12(tmp_path)
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["AppleDevelopment.p12"]})
    device.contents = {(PRIMARY, "AppleDevelopment.p12"): p12.read_bytes()}
    log = BuildLog(tmp_path / "log.sqlite")

    lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=device,
                        events=Recorder().events, log=log)

    assert device.pushed == [(PRIMARY, "AppleDevelopment.p12", p12.read_bytes())]
    assert log.cert_pushes("u1") == {PRIMARY: {"AppleDevelopment.p12"}}


def test_another_certificate_there_is_still_refused(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["AppleDevelopment.p12"]})
    device.contents = {(PRIMARY, "AppleDevelopment.p12"): b"someone else's identity"}

    with pytest.raises(LiveContainerError):
        lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=device,
                            events=Recorder().events, log=BuildLog(tmp_path / "log.sqlite"))

    assert device.pushed == []


# --- R8: the conflict is found before cyan runs ----------------------------------


def test_a_foreign_file_stops_the_build_before_cyan(tmp_path, monkeypatch) -> None:
    cfg = _stub_config(tmp_path)
    fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]})

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "select_device", return_value=("u1", "USB")), \
         patch.object(cli_mod, "run_cyan", side_effect=AssertionError("cyan ran")):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        with pytest.raises(LiveContainerError) as exc:
            cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "l.sqlite"), app_name="example",
                               dry_run=False, skip_install=False, skip_decrypt_check=True,
                               verbose=False, device=None)

    assert "did not push" in exc.value.message


def test_an_unreachable_phone_does_not_stop_the_build_early(tmp_path, monkeypatch) -> None:
    """The early check is a convenience: a phone that is not answering yet may
    be by the end of cyan, and the push checks again."""
    import pymobiledevice3.lockdown as lockdown_mod

    async def unreachable(*, serial):
        raise ConnectionError("not yet")

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", unreachable)

    assert lc_runner.check_push_target(
        "u1", inner_filename="example.ipa", lc_bundle_id=None, team=None,
        free_account=True, ours={}, archives=[],
    ) is None


def test_a_locked_phone_does_not_stop_the_build_early(monkeypatch) -> None:
    """P4. A locked phone answers the app listing with no apps at all
    (`lc_manage.install`), and "no apps" holds no LiveContainer: the check
    said "no LiveContainer found" and stopped a build the push, after cyan,
    could have finished once the phone was unlocked. No apps is no answer."""
    fake_device(monkeypatch, [])

    assert lc_runner.check_push_target(
        "u1", inner_filename="example.ipa", lc_bundle_id=None, team=None,
        free_account=True, ours={}, archives=[],
    ) is None
    assert lc_runner.check_push_target(
        "u1", inner_filename="example.ipa", lc_bundle_id=PRIMARY, team=TEAM,
        free_account=False, ours={}, archives=[],
    ) is None


def test_a_phone_with_apps_and_no_livecontainer_still_stops_it_early(monkeypatch) -> None:
    """The other side of P4: a phone that lists its apps has answered."""
    fake_device(monkeypatch, ["com.example.other"])

    with pytest.raises(LiveContainerError) as exc:
        lc_runner.check_push_target(
            "u1", inner_filename="example.ipa", lc_bundle_id=None, team=None,
            free_account=True, ours={}, archives=[],
        )

    assert exc.value.message == "no LiveContainer found on the device"
    assert "SideStore" in exc.value.hint


@pytest.mark.parametrize(("pin", "target"), [
    (None, lc_runner.DRY_RUN_TARGET), (PRIMARY, PRIMARY),
])
def test_a_dry_run_on_a_locked_phone_leaves_the_target_unknown(
    tmp_path, monkeypatch, pin, target
) -> None:
    """As for a phone that does not answer: the pin if there is one, never
    "no LiveContainer found" for a container the phone did not list."""
    fake_device(monkeypatch, [])
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x")

    result = lc_runner.push_ipa_to_livecontainer(
        ipa, "u1", lc_bundle_id=pin, free_account=True, dry_run=True,
    )

    assert result.bundle_id == target


def test_the_early_check_passes_what_the_push_would_overwrite(tmp_path, monkeypatch) -> None:
    fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]})

    assert lc_runner.check_push_target(
        "u1", inner_filename="example.ipa", lc_bundle_id=None, team=None,
        free_account=True, ours={PRIMARY: {"example.ipa"}}, archives=[],
    ) == PRIMARY

