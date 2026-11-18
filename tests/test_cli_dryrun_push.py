"""The push's target on the `lc-push` stage's end (contract C-DRYRUN).

The `stage` event `{name: lc-push, status: end}` carried `bundle_id` and
`size_bytes`; it now also carries `documents_path` (where in that
LiveContainer's sandbox the IPA goes, `Documents/<app>.ipa`) and `dry_run`.
On a dry run the bundle id is the container discovery chose on the phone, or
the pin when the phone does not answer, so the app's dry run can say where a
real push would land.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa.build_log import BuildLog
from graft_ipa.cyan_runner import CyanResult
from graft_ipa.json_output import JsonEmitter
from graft_ipa.lc_runner import LCDeliveryResult

from .test_cli_build_json import _stub_config
from .test_lc_discovery import fake_device

TEAM = "ABCDE12345"
PRIMARY = f"com.kdt.livecontainer.{TEAM}"
LC2 = f"com.kdt.LiveContainer2.{TEAM}"


def _push_end(buf: StringIO) -> dict:
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    (end,) = [e for e in events if e["type"] == "stage" and e["name"] == "lc-push"
              and e["status"] == "end"]
    return end


def _build(tmp_path: Path, *, dry_run: bool, pin: str | None = None) -> dict:
    cfg = _stub_config(tmp_path)
    cfg.apps["example"].lc_bundle_id = pin

    def cyan(cfg, app, src, out, *, dry_run=False):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"tweaked")
        return CyanResult(output_ipa=out, stdout="", stderr="")

    buf = StringIO()
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan), \
         patch.object(cli_mod, "select_device", return_value=("UDID1", "USB")):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                           dry_run=dry_run, skip_install=False, skip_decrypt_check=True,
                           verbose=False, device=None, emitter=JsonEmitter(stream=buf))
    return _push_end(buf)


def test_a_dry_run_names_the_container_discovery_chose(tmp_path, monkeypatch) -> None:
    fake_device(monkeypatch, [LC2, PRIMARY, "com.example.app"])

    end = _build(tmp_path, dry_run=True)

    assert end["dry_run"] is True
    assert end["bundle_id"] == PRIMARY
    assert end["documents_path"] == "Documents/example.ipa"


def test_a_dry_run_honours_the_pin(tmp_path, monkeypatch) -> None:
    fake_device(monkeypatch, [LC2, PRIMARY])

    end = _build(tmp_path, dry_run=True, pin=LC2.lower())

    assert (end["dry_run"], end["bundle_id"]) == (True, LC2)


def test_a_dry_run_with_a_silent_phone_names_the_pin(tmp_path, monkeypatch) -> None:
    import pymobiledevice3.lockdown as lockdown_mod

    async def unreachable(*, serial):
        raise ConnectionError("no phone")

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", unreachable)

    end = _build(tmp_path, dry_run=True, pin=LC2)

    assert (end["dry_run"], end["bundle_id"]) == (True, LC2)
    assert end["documents_path"] == "Documents/example.ipa"


def test_a_real_push_says_it_was_not_a_dry_run(tmp_path, monkeypatch) -> None:
    written = fake_device(monkeypatch, [PRIMARY])

    end = _build(tmp_path, dry_run=False)

    assert end["dry_run"] is False
    assert end["bundle_id"] == PRIMARY
    assert end["documents_path"] == "Documents/example.ipa"
    assert (PRIMARY, "Documents/example.ipa") in written


def test_a_reinstall_push_carries_both_fields(tmp_path) -> None:
    cfg = _stub_config(tmp_path)
    ipa = tmp_path / "built" / "example.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_bytes(b"unsigned")
    build_id = BuildLog(cfg.state_dir / "log.sqlite").record(
        app="example", bundle_id="com.example.app", version="1.0", tweaks=[], ipa_path=ipa,
        signing_team=None, udid="UDID1", success=True, delivery="livecontainer",
    )
    result = LCDeliveryResult(udid="UDID1", bundle_id=PRIMARY,
                              documents_path="Documents/example.ipa", size_bytes=8)
    buf = StringIO()
    with patch.object(cli_mod, "select_device", return_value=("UDID1", "USB")), \
         patch.object(cli_mod.lc_runner, "push_ipa_to_livecontainer", return_value=result):
        cli_mod._do_reinstall_build(cfg, build_id, emitter=JsonEmitter(stream=buf))

    end = _push_end(buf)
    assert (end["dry_run"], end["documents_path"], end["bundle_id"]) == (
        False, "Documents/example.ipa", PRIMARY,
    )


@pytest.fixture(autouse=True)
def _no_archive_retention(monkeypatch):
    """Retention is not under test here, and a real build archives."""
    monkeypatch.setattr(cli_mod, "_prune_archives", lambda *a, **k: ([], []))


def test_a_dry_run_whose_ipa_was_never_made_sends_no_size(tmp_path, monkeypatch, capsys) -> None:
    """A real dry run does not run cyan, so there is no tweaked IPA to measure.

    The size was 0 then, and the app said "would push Zero KB"; null lets it
    say "would push the IPA" instead, the wording it already has for a size it
    was not told.
    """
    fake_device(monkeypatch, [PRIMARY])
    cfg = _stub_config(tmp_path)

    def cyan_dry(cfg, app, src, out, *, dry_run=False):
        return CyanResult(output_ipa=out, stdout="", stderr="")

    buf = StringIO()
    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan_dry), \
         patch.object(cli_mod, "select_device", return_value=("UDID1", "USB")):
        acquire.return_value.path = tmp_path / "in.ipa"
        acquire.return_value.version = "1.0"
        cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                           dry_run=True, skip_install=False, skip_decrypt_check=True,
                           verbose=False, device=None, emitter=JsonEmitter(stream=buf))

    end = _push_end(buf)
    said = " ".join(capsys.readouterr().out.split())
    assert end["dry_run"] is True
    assert end["size_bytes"] is None
    assert "would push the IPA to Documents/example.ipa" in said
    assert "0 bytes" not in said


def test_the_decryption_check_unpacks_nothing_that_outlives_it(tmp_path, monkeypatch) -> None:
    """Stage 2.5 unpacks the whole IPA (417 MB, 22,000 files for a real app)
    only to read its Mach-O headers, and the tree sat in the work dir until the
    build ended. A Stop then had two unpacked apps to delete and took 3.1 s,
    past the 2 s the Mac app gives it before SIGKILL: no build-end, a leftover
    work dir. It is removed as soon as the check is done, before cyan runs."""
    import zipfile

    fake_device(monkeypatch, [PRIMARY])
    cfg = _stub_config(tmp_path)
    src = tmp_path / "in.ipa"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("Payload/Example.app/Info.plist", b"<plist/>")
        z.writestr("Payload/Example.app/Assets.car", b"x" * 64)
    seen = {}

    def cyan(cfg, app, source, out, *, dry_run=False):
        seen["verify_left"] = (out.parent / "verify").exists()
        out.write_bytes(b"tweaked")
        return CyanResult(output_ipa=out, stdout="", stderr="")

    with patch.object(cli_mod, "run_all_checks", return_value=[]), \
         patch.object(cli_mod, "acquire_ipa") as acquire, \
         patch.object(cli_mod, "run_cyan", side_effect=cyan):
        acquire.return_value.path = src
        acquire.return_value.version = "1.0"
        cli_mod._build_one(cfg=cfg, log=BuildLog(tmp_path / "log.sqlite"), app_name="example",
                           dry_run=False, skip_install=True, skip_decrypt_check=False,
                           verbose=False, device=None, emitter=JsonEmitter(stream=StringIO()))

    assert seen == {"verify_left": False}
