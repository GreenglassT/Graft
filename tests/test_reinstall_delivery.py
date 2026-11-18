"""A re-install ships an archive the way it was built, or not at all (F3).

`--reinstall-build` chose how to ship by the app's *current* delivery, and the
builds table did not say how the archive had been built. After a mode flip
that hands an unsigned LiveContainer archive to ideviceinstaller (free, then
paid with `--delivery sideload`, as `--init` suggests), or pushes a
zsign-signed archive into LiveContainer (paid, then `signing:` removed). The
row now records its delivery, and a re-install whose archive was built for the
other mode is refused with the way out: rebuild.

Rows written before the column existed carry no delivery. Their archive says
how it was built: a zsign build recorded its team, and its app carries the
`embedded.mobileprovision` zsign put there; an unsigned LiveContainer build
has neither. Such a row re-installs when that reading matches the app's mode,
and is refused when it does not or when the archive cannot be read.
"""

from __future__ import annotations

import json
import sqlite3
import zipfile
from io import StringIO
from pathlib import Path

import pytest
import yaml

from graft_ipa import cli as cli_mod
from graft_ipa.build_log import BuildLog
from graft_ipa.config import add_signing_section, load_config
from graft_ipa.errors import GraftError
from graft_ipa.json_output import JsonEmitter
from graft_ipa.lc_runner import LCDeliveryResult

TEAM = "ABCDE12345"


def _free_cfg(tmp_path: Path) -> Path:
    path = tmp_path / "xdg-config" / "graft" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({
        "cache_dir": str(tmp_path / "ipas"),
        "built_dir": str(tmp_path / "built"),
        "state_dir": str(tmp_path / "state"),
        "default_device_udid": "UDID1",
        "apps": {"example": {"bundle_id": "com.example.app"}},
    }))
    return path


def _archive(tmp_path: Path, name: str, *, signed: bool) -> Path:
    """An IPA as each mode archives it: zsign's has an embedded profile."""
    ipa = tmp_path / "built" / name
    ipa.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ipa, "w") as zf:
        zf.writestr("Payload/Example.app/Info.plist", b"plist")
        zf.writestr("Payload/Example.app/Example", b"binary")
        if signed:
            zf.writestr("Payload/Example.app/embedded.mobileprovision", b"profile")
    return ipa


def _row(cfg, ipa: Path, *, delivery: str | None, signing_team: str | None) -> int:
    log = BuildLog(cfg.state_dir / "log.sqlite")
    return log.record(app="example", bundle_id="com.example.app", version="1.0", tweaks=[],
                      ipa_path=ipa, signing_team=signing_team, udid="UDID1", success=True,
                      delivery=delivery)


@pytest.fixture
def shipped(monkeypatch):
    """What reached each door: ideviceinstaller and the LiveContainer push."""
    got: dict[str, bytes] = {}
    monkeypatch.setattr(cli_mod, "select_device", lambda u: (u or "UDID1", "usb"))

    class Installed:
        stdout = stderr = ""

    def install(p, **kw):
        got["install"] = Path(p).read_bytes()
        return Installed()

    def push(p, **kw):
        got["push"] = Path(p).read_bytes()
        return LCDeliveryResult(udid="UDID1", bundle_id=f"com.kdt.livecontainer.{TEAM}",
                                documents_path="Documents/example.ipa", size_bytes=1)

    monkeypatch.setattr(cli_mod, "install", install)
    monkeypatch.setattr(cli_mod.lc_runner, "push_ipa_to_livecontainer", push)
    return got


def _end(buf: StringIO) -> dict:
    events = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    return next(e for e in events if e["type"] == "build-end")


# --- the column -------------------------------------------------------------------


def test_a_build_row_records_its_delivery(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    log.record(app="example", bundle_id="com.example.app", version="1.0", tweaks=[],
               ipa_path=None, signing_team=None, udid=None, success=True,
               delivery="livecontainer")

    assert log.recent()[0]["delivery"] == "livecontainer"


def test_the_build_envelope_writes_the_apps_delivery(tmp_path: Path) -> None:
    """`_BuildReport` is the one place a build's row is written; the delivery
    it was opened with is the one the row must carry."""
    log = BuildLog(tmp_path / "log.sqlite")
    with cli_mod._BuildReport(None, app_name="example", bundle_id="com.example.app",
                              delivery="livecontainer") as report:
        report.log = log
        report.version = "1.0"

    assert log.recent()[0]["delivery"] == "livecontainer"


def test_a_builds_table_from_before_the_column_is_migrated(tmp_path: Path) -> None:
    db = tmp_path / "log.sqlite"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE builds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL, app TEXT NOT NULL, bundle_id TEXT NOT NULL,
                version TEXT NOT NULL, tweaks TEXT NOT NULL, ipa_sha256 TEXT,
                ipa_path TEXT, log_path TEXT, signing_team TEXT, udid TEXT,
                success INTEGER NOT NULL, error TEXT
            );
            INSERT INTO builds (timestamp, app, bundle_id, version, tweaks, success)
            VALUES ('2026-01-01T00:00:00', 'example', 'com.example.app', '1.0', '', 1);
            """
        )

    log = BuildLog(db)
    log.record(app="example", bundle_id="com.example.app", version="1.1", tweaks=[],
               ipa_path=None, signing_team=None, udid=None, success=True, delivery="sideload")

    assert [r["delivery"] for r in log.recent()] == ["sideload", None]


def test_builds_json_carries_the_delivery(tmp_path: Path) -> None:
    cfg = load_config(_free_cfg(tmp_path))
    _row(cfg, _archive(tmp_path, "a.ipa", signed=False), delivery="livecontainer",
         signing_team=None)
    buf = StringIO()

    cli_mod._do_builds(cfg, None, 10, emitter=JsonEmitter(stream=buf))

    (record,) = [json.loads(line) for line in buf.getvalue().splitlines()]
    assert record["delivery"] == "livecontainer"


# --- a mode flip between the build and the re-install -----------------------------


def test_free_then_paid_sideload_refuses_the_unsigned_livecontainer_archive(
    tmp_path: Path, shipped
) -> None:
    path = _free_cfg(tmp_path)
    build_id = _row(load_config(path), _archive(tmp_path, "example_lc.ipa", signed=False),
                    delivery="livecontainer", signing_team=None)
    add_signing_section(path, cert=tmp_path / "c.p12", profile=tmp_path / "p.mp", team_id=TEAM)
    cli_mod._do_set_app_options(load_config(path), app_name="example", delivery="sideload")
    paid = load_config(path)
    assert paid.apps["example"].delivery == "sideload"

    with pytest.raises(GraftError) as exc:
        cli_mod._do_reinstall_build(paid, build_id)

    assert shipped == {}, "nothing may reach ideviceinstaller"
    assert "livecontainer" in exc.value.message and "sideload" in exc.value.message
    assert "rebuild" in exc.value.hint and "graft example" in exc.value.hint


def test_paid_then_free_refuses_to_push_the_zsign_archive_into_livecontainer(
    tmp_path: Path, shipped
) -> None:
    free = load_config(_free_cfg(tmp_path))
    assert free.apps["example"].delivery == "livecontainer"
    build_id = _row(free, _archive(tmp_path, "example_signed.ipa", signed=True),
                    delivery="sideload", signing_team=TEAM)
    buf = StringIO()

    with pytest.raises(GraftError):
        cli_mod._do_reinstall_build(free, build_id, emitter=JsonEmitter(stream=buf))

    assert shipped == {}
    end = _end(buf)
    assert end["ok"] is False and "sideload" in end["error"]


def test_an_archive_in_the_apps_own_mode_still_reinstalls(tmp_path: Path, shipped) -> None:
    free = load_config(_free_cfg(tmp_path))
    build_id = _row(free, _archive(tmp_path, "example_lc.ipa", signed=False),
                    delivery="livecontainer", signing_team=None)

    cli_mod._do_reinstall_build(free, build_id)

    assert "push" in shipped and "install" not in shipped


# --- rows from before the column ------------------------------------------------


def test_an_old_unsigned_row_reinstalls_into_livecontainer(tmp_path: Path, shipped) -> None:
    free = load_config(_free_cfg(tmp_path))
    build_id = _row(free, _archive(tmp_path, "old_lc.ipa", signed=False),
                    delivery=None, signing_team=None)

    cli_mod._do_reinstall_build(free, build_id)

    assert "push" in shipped


@pytest.mark.parametrize("signing_team", [TEAM, None])
def test_an_old_signed_row_is_refused_for_livecontainer(
    tmp_path: Path, shipped, signing_team: str | None
) -> None:
    """Signed shows either way: a team on the row, or zsign's embedded profile."""
    free = load_config(_free_cfg(tmp_path))
    build_id = _row(free, _archive(tmp_path, "old_signed.ipa", signed=True),
                    delivery=None, signing_team=signing_team)

    with pytest.raises(GraftError) as exc:
        cli_mod._do_reinstall_build(free, build_id)

    assert shipped == {}
    assert "sideload" in exc.value.message


@pytest.mark.parametrize("archive", ["zip", "unreadable"])
def test_an_old_signed_row_reinstalls_through_ideviceinstaller(
    tmp_path: Path, shipped, archive: str
) -> None:
    """The recorded team is enough on its own: only zsign builds have one."""
    path = _free_cfg(tmp_path)
    add_signing_section(path, cert=tmp_path / "c.p12", profile=tmp_path / "p.mp", team_id=TEAM)
    cli_mod._do_set_app_options(load_config(path), app_name="example", delivery="sideload")
    paid = load_config(path)
    if archive == "zip":
        ipa = _archive(tmp_path, "old_signed.ipa", signed=True)
    else:
        ipa = tmp_path / "built" / "old_signed.ipa"
        ipa.parent.mkdir(parents=True)
        ipa.write_bytes(b"signed, and not readable as a zip here")
    build_id = _row(paid, ipa, delivery=None, signing_team=TEAM)

    cli_mod._do_reinstall_build(paid, build_id)

    assert "install" in shipped


def test_an_old_row_whose_archive_cannot_be_read_is_refused(tmp_path: Path, shipped) -> None:
    free = load_config(_free_cfg(tmp_path))
    ipa = tmp_path / "built" / "old.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_bytes(b"not a zip")
    build_id = _row(free, ipa, delivery=None, signing_team=None)

    with pytest.raises(GraftError) as exc:
        cli_mod._do_reinstall_build(free, build_id)

    assert shipped == {}
    assert "rebuild" in exc.value.hint
