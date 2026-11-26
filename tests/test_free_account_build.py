"""A free Apple ID's build, end to end, into a LiveContainer it was never told about.

The config is what `--init` writes for a free account plus an app added with
no `delivery:` key. Everything between that and the phone is real except the
three things a test cannot have: the external tools' presence (cyan is looked
up, not run), cyan itself (it copies the IPA through),
and the device, which is the fake pymobiledevice3 services from
test_lc_discovery.py — an InstallationProxy listing apps and a HouseArrest that
records what was written. No team id is configured anywhere; the push has to
find `com.kdt.livecontainer.<TEAMID>` on the device by itself.
"""

from __future__ import annotations

import json
import plistlib
import shutil
import zipfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from graft_ipa import cli as cli_mod
from graft_ipa import preflight, tools
from graft_ipa.build_log import BuildLog
from graft_ipa.config import add_app, default_config_path, load_config, write_default_config
from graft_ipa.json_output import JsonEmitter
from tests.appstore_fakes import macho
from tests.tool_fakes import fake_path

from .test_lc_discovery import fake_device

LC = "com.kdt.livecontainer.FGHIJ67890"
LC2 = "com.kdt.LiveContainer2.FGHIJ67890"


def _stage_ipa(cache: Path, bundle_id: str = "com.example.app", version: str = "1.0") -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{bundle_id}_{version}.ipa"
    info = plistlib.dumps({
        "CFBundleIdentifier": bundle_id,
        "CFBundleShortVersionString": version,
        "CFBundleExecutable": "Example",
    })
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Payload/Example.app/Info.plist", info)
        # The executable Info.plist names, decrypted: stage 2.5 refuses an
        # app whose executable isn't there to check (re-check #10).
        zf.writestr("Payload/Example.app/Example", macho(0))
    return path


@pytest.fixture
def free_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = default_config_path()
    write_default_config(path, free_account=True, default_device_udid="UDID1")
    add_app(path, "example", {"bundle_id": "com.example.app", "tweaks": []})
    cfg = load_config(path)
    assert cfg.free_account and cfg.team_id is None
    assert cfg.apps["example"].delivery == "livecontainer"
    _stage_ipa(cfg.cache_dir)

    # The tools are looked up, never run; the phone is one connected device.
    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: ["UDID1"])
    monkeypatch.setattr(cli_mod, "resolve_target",
                        lambda cfg, app, explicit_udid=None: cli_mod.Target("UDID1", None, "usb"))

    def cyan(_cfg, _app, src, dst, *, dry_run=False):
        shutil.copyfile(src, dst)
        Path(dst).write_bytes(Path(dst).read_bytes() + b"<tweaked>")
        return SimpleNamespace(output_ipa=Path(dst), stdout="", stderr="")

    monkeypatch.setattr(cli_mod, "run_cyan", cyan)
    # Signing must not be reached at all on this path.
    monkeypatch.setattr(cli_mod, "run_zsign", lambda *a, **k: pytest.fail("zsign ran"))
    monkeypatch.setattr(cli_mod, "install", lambda *a, **k: pytest.fail("a sideload install ran"))
    return cfg


def _build(cfg, tmp_path: Path) -> list[dict]:
    out = StringIO()
    cli_mod._build_one(
        cfg=cfg, log=BuildLog(cfg.state_dir / "log.sqlite"), app_name="example",
        dry_run=False, skip_install=False, skip_decrypt_check=False, verbose=False,
        device=None, emitter=JsonEmitter(stream=out), trash_dir=tmp_path / "Trash",
    )
    return [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]


def test_a_free_account_build_pushes_into_the_discovered_livecontainer(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = fake_device(monkeypatch, [LC, LC2, "com.example.other"])

    events = _build(free_config, tmp_path)

    [(sandbox, path)] = written
    assert (sandbox, path) == (LC, "Documents/example.ipa")
    assert written[(sandbox, path)].endswith(b"<tweaked>")

    stages = [(e["name"], e["status"]) for e in events if e["type"] == "stage"]
    assert ("sign", "start") not in stages
    assert ("lc-push", "end") in stages
    push_end = next(e for e in events if e["type"] == "stage" and e["name"] == "lc-push"
                    and e["status"] == "end")
    assert push_end["bundle_id"] == LC
    assert push_end["others"] == [LC2]
    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True

    # The delivery is recorded against the container it went into, so
    # `--reconcile` and `--lc-clean` look in the right place later.
    delivered = BuildLog(free_config.state_dir / "log.sqlite").latest_delivery("example")
    assert delivered["lc_bundle_id"] == LC
    assert delivered["remote_name"] == "example.ipa"


def test_a_free_account_build_with_no_livecontainer_fails_with_the_setup_pointer(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa.lc_runner import LiveContainerError

    written = fake_device(monkeypatch, ["com.example.other"])

    with pytest.raises(LiveContainerError) as exc:
        _build(free_config, tmp_path)

    assert written == {}
    assert exc.value.message == "no LiveContainer found on the device"
    assert "SideStore" in exc.value.hint and "iloader" in exc.value.hint
    assert BuildLog(free_config.state_dir / "log.sqlite").latest_delivery("example") is None


# --- a locked phone (P4) ---------------------------------------------------------
#
# A locked phone lists no apps at all. The early check read that as "no
# LiveContainer here" and failed the build before cyan with the setup steps,
# on a phone that had LiveContainer and only needed unlocking. No apps is no
# answer now, as for a phone that is not answering: the push asks again.


def _locked_for(monkeypatch: pytest.MonkeyPatch, listings: int) -> list:
    """The fake device, answering its first `listings` app listings with no
    apps, the way a locked phone does. Returns the listings made."""
    import pymobiledevice3.services.installation_proxy as ipx_mod

    unlocked = ipx_mod.InstallationProxyService
    made: list = []

    class Locked(unlocked):
        async def lookup(self, options=None):
            made.append(options)
            return {} if len(made) <= listings else await super().lookup(options)

    monkeypatch.setattr(ipx_mod, "InstallationProxyService", Locked)
    return made


def test_a_phone_locked_before_cyan_is_asked_again_by_the_push(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    written = fake_device(monkeypatch, [LC])
    listings = _locked_for(monkeypatch, 1)

    events = _build(free_config, tmp_path)

    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True
    assert list(written) == [(LC, "Documents/example.ipa")]
    assert len(listings) == 2  # the early check, then the push


def test_a_phone_still_locked_at_the_push_is_told_to_unlock_not_to_install(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from graft_ipa.lc_runner import LiveContainerError

    written = fake_device(monkeypatch, [LC])
    _locked_for(monkeypatch, 99)
    injected = []
    cyan = cli_mod.run_cyan
    monkeypatch.setattr(cli_mod, "run_cyan", lambda *a, **k: injected.append(1) or cyan(*a, **k))

    with pytest.raises(LiveContainerError) as exc:
        _build(free_config, tmp_path)

    assert injected, "the build stopped before cyan"
    assert written == {}
    assert "no LiveContainer found" not in exc.value.message
    assert "unlocked" in (exc.value.hint or "")
    assert "SideStore" not in (exc.value.hint or "")


def test_a_rebuild_overwrites_its_own_push(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivery the first build recorded is what lets the second replace
    it: the file is there now, and it is ours."""
    written = fake_device(monkeypatch, [LC])

    _build(free_config, tmp_path)
    written[(LC, "Documents/example.ipa")] = b"stale"
    events = _build(free_config, tmp_path)

    assert events[-1]["type"] == "build-end" and events[-1]["ok"] is True
    assert written[(LC, "Documents/example.ipa")].endswith(b"<tweaked>")


def test_a_build_does_not_overwrite_the_users_own_file(
    free_config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SideStore's LiveContainer, holding the user's own `example.ipa`."""
    from graft_ipa.lc_runner import LiveContainerError

    written = fake_device(monkeypatch, [LC], docs={LC: ["example.ipa"]})

    with pytest.raises(LiveContainerError, match="Graft did not push"):
        _build(free_config, tmp_path)

    assert written == {}
    assert BuildLog(free_config.state_dir / "log.sqlite").latest_delivery("example") is None
