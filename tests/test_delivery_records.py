from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from graft_ipa.build_log import BuildLog


def _log(tmp_path: Path) -> BuildLog:
    return BuildLog(tmp_path / "log.sqlite")


def _tables(db: Path) -> set[str]:
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def test_record_and_read_back_the_latest(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record_delivery(
        app="example", bundle_id="com.example.app",
        inputs_digest="aaa", digest_version=1, cyan=None,
        tweaks=[["a.deb", "1"]], source_version="21.16.2", ipa_sha256="deadbeef",
        delivery="livecontainer", lc_bundle_id="com.kdt.livecontainer.T",
        remote_name="example.ipa", udid="UDID1",
    )
    log.record_delivery(
        app="example", bundle_id="com.example.app",
        inputs_digest="bbb", digest_version=1, cyan=None,
        tweaks=[["a.deb", "2"]], source_version="21.16.2", ipa_sha256="cafe",
        delivery="livecontainer", lc_bundle_id="com.kdt.livecontainer.T",
        remote_name="example.ipa", udid="UDID1",
    )

    latest = log.latest_delivery("example")

    assert latest is not None
    assert latest["inputs_digest"] == "bbb"


def test_apps_do_not_share_deliveries(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.record_delivery(
        app="bravo", bundle_id="com.bravo.app",
        inputs_digest="rrr", digest_version=1, cyan=None,
        tweaks=[], source_version="2026.18.0", ipa_sha256=None,
        delivery="livecontainer", lc_bundle_id=None, remote_name="bravo.ipa", udid=None,
    )

    assert log.latest_delivery("bravo")["inputs_digest"] == "rrr"
    assert log.latest_delivery("example") is None


def test_mark_reconciled_records_both_outcomes(tmp_path: Path) -> None:
    log = _log(tmp_path)
    row_id = log.record_delivery(
        app="example", bundle_id="com.example.app",
        inputs_digest="aaa", digest_version=1, cyan=None,
        tweaks=[], source_version="21.16.2", ipa_sha256=None,
        delivery="livecontainer", lc_bundle_id=None, remote_name="example.ipa", udid="U",
    )

    log.mark_reconciled(row_id, seen=False)
    assert log.latest_delivery("example")["reconcile_ok"] == 0

    log.mark_reconciled(row_id, seen=True)
    row = log.latest_delivery("example")
    assert row["reconcile_ok"] == 1
    assert row["reconciled_at"]


def test_an_existing_database_gains_the_table(tmp_path: Path) -> None:
    """BuildLog is constructed against databases created by older versions.

    The old database is built here with raw sqlite3 rather than by
    constructing a BuildLog twice: today's `SCHEMA` already creates
    `deliveries`, so two BuildLogs over one file never exercise the migration
    this test exists to pin.

    The DDL below is `builds` before `ipa_path`/`log_path`, which is a shape
    older than any real database — the state directory holds a `builds` that
    has been through those ALTERs and still has no `deliveries` — so this
    covers the live case and the column migration on the way to it.

    Both halves of that are asserted. The `builds` ALTERs used to be claimed
    here and checked nowhere: deleting them left this test, and the other 441,
    green, because nothing read `ipa_path` off a migrated database. `recent()`
    is `SELECT *`, so the missing column only surfaces when something indexes
    it by name — which is what `--list` and the retention sweep do, and what
    the last two assertions do here.
    """
    db = tmp_path / "log.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE builds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            app TEXT NOT NULL,
            bundle_id TEXT NOT NULL,
            version TEXT NOT NULL,
            tweaks TEXT NOT NULL,
            ipa_sha256 TEXT,
            signing_team TEXT,
            udid TEXT,
            success INTEGER NOT NULL,
            error TEXT
        );
        INSERT INTO builds (timestamp, app, bundle_id, version, tweaks, success)
        VALUES ('2026-01-01T00:00:00+00:00', 'example', 'com.example.app',
                '21.16.2', '', 1);
        """
    )
    conn.commit()
    conn.close()
    assert not _tables(db) & {"deliveries"}

    log = BuildLog(db)  # opened by a later run of the tool

    assert "deliveries" in _tables(db)
    assert log.latest_delivery("example") is None  # table exists, no rows

    # `builds` survived, and gained the two columns the old DDL above lacks.
    # Indexed by name rather than counted, because that is how every reader
    # gets them and the only way a missed ALTER shows up (as an IndexError,
    # on `--list`).
    builds = log.recent(app="example")
    assert len(builds) == 1
    assert builds[0]["ipa_path"] is None
    assert builds[0]["log_path"] is None

    # And the new table is usable on the migrated database, not merely present.
    log.record_delivery(
        app="example", bundle_id="com.example.app",
        inputs_digest="aaa", digest_version=1, cyan=None,
        tweaks=[], source_version="21.16.2", ipa_sha256=None,
        delivery="livecontainer", lc_bundle_id=None, remote_name=None, udid=None,
    )
    assert log.latest_delivery("example")["inputs_digest"] == "aaa"


def test_cyan_round_trips_as_json_and_none_stays_null(tmp_path: Path) -> None:
    """`cyan` is stored verbatim as JSON; only `None` becomes NULL.

    NULL is the legacy-row signal `diff_delivery` keys the generic "build
    options changed" reason off, so a row that *did* carry options must never
    land there.

    The empty-dict row is the point of this test, not padding: `dict`
    truthiness is emptiness, so `if cyan else None` and `if cyan is not None
    else None` — the two spellings of the write — agree on every *populated*
    dict no matter how falsy its values are. `{}` is the only input that tells
    them apart, and under the truth test it would be filed as a row from
    before the column existed.
    """
    log = _log(tmp_path)
    cyan = {"display_name": "", "keep_bundle_id": False, "extra_args": ["--x"]}
    log.record_delivery(
        app="example", bundle_id="com.example.app",
        inputs_digest="aaa", digest_version=1, cyan=cyan,
        tweaks=[["a.deb", None]], source_version="21.16.2", ipa_sha256=None,
        delivery="livecontainer", lc_bundle_id=None, remote_name="example.ipa", udid=None,
    )
    log.record_delivery(
        app="bravo", bundle_id="com.bravo.app",
        inputs_digest="rrr", digest_version=1, cyan=None,
        tweaks=[], source_version="2026.18.0", ipa_sha256=None,
        delivery="sideload", lc_bundle_id=None, remote_name=None, udid=None,
    )
    log.record_delivery(
        app="charlie", bundle_id="com.charlie.app",
        inputs_digest="sss", digest_version=1, cyan={},
        tweaks=[], source_version="9.0.0", ipa_sha256=None,
        delivery="sideload", lc_bundle_id=None, remote_name=None, udid=None,
    )

    assert json.loads(log.latest_delivery("example")["cyan"]) == cyan
    assert json.loads(log.latest_delivery("example")["tweaks"]) == [["a.deb", None]]
    assert log.latest_delivery("bravo")["cyan"] is None
    # "recorded no options" — distinct from "couldn't record any".
    charlie = log.latest_delivery("charlie")
    assert charlie["cyan"] is not None, "an empty dict is not a pre-column legacy row"
    assert json.loads(charlie["cyan"]) == {}


def test_a_legacy_shaped_row_round_trips_as_null(tmp_path: Path) -> None:
    """The three nullable columns are what make "I can't tell" expressible.

    diff_delivery's unknown state exists so the tool can decline to guess.
    Tightening any of these to NOT NULL would make that guard unreachable,
    and this is the test that says so out loud.
    """
    log = _log(tmp_path)
    log.record_delivery(
        app="example", bundle_id="com.example.app",
        inputs_digest=None, digest_version=None, cyan=None,
        tweaks=[], source_version="21.16.2", ipa_sha256=None,
        delivery="livecontainer", lc_bundle_id=None, remote_name=None, udid=None,
    )

    row = log.latest_delivery("example")

    assert row is not None
    assert row["inputs_digest"] is None
    assert row["digest_version"] is None
    assert row["cyan"] is None
