from __future__ import annotations

import sqlite3
from pathlib import Path

from graft_ipa.build_log import BuildLog, sha256_of


def test_record_and_recent(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    log.record(
        app="example",
        bundle_id="com.example.app",
        version="21.16.2",
        tweaks=["a.deb", "b.deb"],
        ipa_path=None,
        signing_team="ABC1234567",
        udid="00008130xxx",
        success=True,
    )
    rows = log.recent()
    assert len(rows) == 1
    assert rows[0]["app"] == "example"
    assert rows[0]["tweaks"] == "a.deb,b.deb"
    assert rows[0]["success"] == 1


def test_clear_ipa_path_nulls_only_the_matching_rows(tmp_path: Path) -> None:
    """Retention clears the pointer before it trashes the file.

    The GUI offers "Re-install" for exactly the rows with a non-null ipa_path,
    so a leftover pointer to a trashed archive is a button that can only fail.
    """
    log = BuildLog(tmp_path / "log.sqlite")
    doomed = tmp_path / "bravo_1.0_20260601T000000.ipa"
    kept = tmp_path / "bravo_1.0_20260601T010000.ipa"
    for path in (doomed, kept):
        path.write_bytes(b"\x00")

    ids = {
        path: log.record(
            app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
            ipa_path=path, signing_team=None, udid=None, success=True,
        )
        for path in (doomed, kept)
    }
    # A second build that shipped the same archive — both rows must be cleared.
    ids["duplicate"] = log.record(
        app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
        ipa_path=doomed, signing_team=None, udid=None, success=True,
    )

    cleared = log.clear_ipa_path(doomed)

    assert sorted(cleared) == sorted([ids[doomed], ids["duplicate"]])
    by_id = {row["id"]: row["ipa_path"] for row in log.recent(limit=10)}
    assert by_id[ids[doomed]] is None
    assert by_id[ids["duplicate"]] is None
    assert by_id[ids[kept]] == str(kept)


def test_clear_ipa_path_on_an_unknown_path_changes_nothing(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    log.record(
        app="bravo", bundle_id="com.bravo.app", version="1.0", tweaks=[],
        ipa_path=tmp_path / "a.ipa", signing_team=None, udid=None, success=True,
    )
    assert log.clear_ipa_path(tmp_path / "elsewhere.ipa") == []
    assert log.recent()[0]["ipa_path"] == str(tmp_path / "a.ipa")


def test_a_deliveries_table_missing_a_column_is_migrated(tmp_path: Path) -> None:
    """CREATE TABLE IF NOT EXISTS is a no-op against a table that exists.

    So a column added to SCHEMA never reaches a database created before it, and
    every reader here indexes rows by name — which makes a missing column an
    IndexError on `--list`, the GUI's entire app list. `builds` has carried
    ALTER TABLE guards for this since ipa_path; `deliveries` is now read on the
    same hot path and needs the same protection.

    Stands in for a future column by creating the table as an earlier version
    of this file would have: the six NOT NULL columns and nothing else.
    """
    db = tmp_path / "log.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                app TEXT NOT NULL,
                bundle_id TEXT NOT NULL,
                tweaks TEXT NOT NULL,
                source_version TEXT NOT NULL,
                delivery TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO deliveries (timestamp, app, bundle_id, tweaks, source_version,"
            " delivery) VALUES ('2026-08-03', 'example', 'com.example.app', '[]',"
            " '21.16.2', 'livecontainer')"
        )

    row = BuildLog(db).latest_delivery("example")

    assert row is not None
    assert row["app"] == "example", "the pre-existing row must survive the migration"
    # NULL in a column that didn't exist is exactly the legacy signal every
    # reader already handles.
    assert row["inputs_digest"] is None
    assert row["reconcile_ok"] is None
    # `origin` is read on the same hot path (a NULL there means "a build",
    # which is what this row is), so a migration that missed it would be an
    # IndexError on --list rather than a wrong badge.
    assert row["origin"] is None


def test_the_migration_sees_every_column_the_schema_declares(tmp_path: Path) -> None:
    """The list of columns to add is parsed out of SCHEMA, so a parse that
    silently matched nothing would leave the migration above dead code that
    still passes. Checked against sqlite's own view of the table."""
    from graft_ipa.build_log import _declared_columns

    log = BuildLog(tmp_path / "log.sqlite")
    with log._connect() as conn:
        live = [r[1] for r in conn.execute("PRAGMA table_info(deliveries)").fetchall()]

    assert [name for name, _ in _declared_columns("deliveries")] == live


def test_latest_delivery_can_be_scoped_to_one_device(tmp_path) -> None:
    """The same app delivered to two phones has two answers, not one.

    This is the truth the single-state view hides: an app on phone A and never
    on phone B genuinely reads "not delivered" on B.
    """
    log = BuildLog(tmp_path / "log.db")
    common = dict(
        app="example",
        bundle_id="com.example.app",
        inputs_digest="d",
        digest_version=1,
        cyan=None,
        tweaks=[],
        source_version="1.0",
        ipa_sha256="x",
        delivery="livecontainer",
        lc_bundle_id=None,
        remote_name=None,
        origin="build",
    )
    log.record_delivery(**common, udid="PHONE-A")
    log.record_delivery(**common, udid="PHONE-B")

    assert log.latest_delivery("example") is not None, "unscoped still means 'anywhere'"

    a = log.latest_delivery("example", udid="PHONE-A")
    assert a is not None
    assert a["udid"] == "PHONE-A"

    assert log.latest_delivery("example", udid="PHONE-C") is None


def test_scoping_picks_that_device_s_latest_not_the_global_latest(tmp_path) -> None:
    """Ordering is by id *within* the filter, not filtered after ordering.

    "the latest row for this device" and "the latest row, if it happens to be
    this device" are different questions and only the first is useful.
    """
    log = BuildLog(tmp_path / "log.db")
    common = dict(
        app="example",
        bundle_id="com.example.app",
        inputs_digest="d",
        digest_version=1,
        cyan=None,
        tweaks=[],
        ipa_sha256="x",
        delivery="livecontainer",
        lc_bundle_id=None,
        remote_name=None,
        origin="build",
    )
    log.record_delivery(**common, source_version="1.0", udid="PHONE-A")
    log.record_delivery(**common, source_version="2.0", udid="PHONE-B")
    row = log.latest_delivery("example", udid="PHONE-A")
    assert row is not None
    assert row["source_version"] == "1.0"


def test_a_null_udid_row_is_not_matched_by_a_scoped_query(tmp_path) -> None:
    """`udid` is nullable — a push whose device wasn't recorded exists.

    SQL `= NULL` never matches, so such a row is invisible to a scoped query.
    That is the right behaviour (we cannot claim it reached this phone), but it
    must be a decision rather than an accident, so it is pinned.
    """
    log = BuildLog(tmp_path / "log.db")
    log.record_delivery(
        app="example",
        bundle_id="com.example.app",
        inputs_digest="d",
        digest_version=1,
        cyan=None,
        tweaks=[],
        source_version="1.0",
        ipa_sha256="x",
        delivery="livecontainer",
        lc_bundle_id=None,
        remote_name=None,
        udid=None,
        origin="build",
    )
    assert log.latest_delivery("example") is not None
    assert log.latest_delivery("example", udid="PHONE-A") is None


def test_sha256(tmp_path: Path) -> None:
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello")
    h = sha256_of(p)
    assert h == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


def test_a_database_written_before_target_existed_still_opens(tmp_path: Path) -> None:
    """The read-side default, against the shape it has to survive.

    A database created before the ALTER TABLE ran has rows that carry NULL in
    the new column, and NULL must read as `device`. That is all this covers.

    It is emphatically *not* a test of the migration, despite the name it was
    given: delete the ALTER TABLE and this still passes, measured. `recent()`
    injects `target` into every row it returns, so a build list against an
    unmigrated table renders fine and lies about nothing — the read side is
    total by construction. `test_a_migrated_database_can_record_a_mac_build`
    is the one that holds the migration, and it does it from the write side,
    where a missing column is an OperationalError that cannot be papered over.
    """
    db = tmp_path / "log.sqlite"
    conn = sqlite3.connect(db)
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
    conn.commit()
    conn.close()

    log = BuildLog(db)
    row = log.recent(app=None, limit=1)[0]
    assert row["target"] == "device", "a row written before the column is a phone build"


def test_a_mac_build_records_its_target(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    log.record(
        app="example", bundle_id="com.example.app", version="1.0",
        tweaks=[], ipa_path=None, log_path=None, signing_team=None,
        udid=None, success=True, error=None, target="mac",
    )
    assert log.recent(app=None, limit=1)[0]["target"] == "mac"


def test_a_build_that_does_not_say_is_a_device_build(tmp_path: Path) -> None:
    """The default is the safe one. A Mac artifact points a tweak at a stub
    framework iOS has for real, so a row that cannot say what it is must not
    read as Mac — the GUI would offer to install it here and, worse, withhold
    the phone action that was correct all along."""
    log = BuildLog(tmp_path / "log.sqlite")
    log.record(
        app="example", bundle_id="com.example.app", version="1.0",
        tweaks=[], ipa_path=None, log_path=None, signing_team=None,
        udid=None, success=True, error=None,
    )
    assert log.recent(app=None, limit=1)[0]["target"] == "device"


def test_a_target_nobody_recognises_reads_as_a_device_build(tmp_path: Path) -> None:
    """'mac' or device, with no third answer available.

    `target` is a closed set, and the fallback is the survivable direction.
    A row that says `'Mac'` is a writer that meant Mac and spelled it wrong;
    reading it back as itself would let a later `target == "mac"` branch fall
    through to the *device* path, which offers a phone the one artifact that
    must never reach one — its tweak dylib points at a stub Preferences
    framework that iOS has for real. Reading it as `device` costs an action
    the user can get by rebuilding. Reading it as `mac` costs a tweak that
    breaks silently, at runtime, in a pane nobody opens for a week.

    Nothing writes these strings today; the point is that nothing can start.
    """
    log = BuildLog(tmp_path / "log.sqlite")
    for spelling in ("Mac", "macos", "playcover", ""):
        log.record(
            app="example", bundle_id="com.example.app", version="1.0",
            tweaks=[], ipa_path=None, log_path=None, signing_team=None,
            udid=None, success=True, error=None, target=spelling,
        )
        assert log.recent(app=None, limit=1)[0]["target"] == "device", spelling


def test_a_migrated_database_can_record_a_mac_build(tmp_path: Path) -> None:
    """The read side hides a missing migration; the write side does not.

    `_target_of` answers "device" for a row whose table has no such column, so
    the ALTER TABLE can be deleted outright and the migration test above still
    passes — measured, not assumed. What cannot survive a missing column is an
    INSERT that names it, which is every build recorded from here on against a
    database that predates it.
    """
    db = tmp_path / "log.sqlite"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE builds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL, app TEXT NOT NULL, bundle_id TEXT NOT NULL,
            version TEXT NOT NULL, tweaks TEXT NOT NULL, ipa_sha256 TEXT,
            ipa_path TEXT, log_path TEXT, signing_team TEXT, udid TEXT,
            success INTEGER NOT NULL, error TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    log = BuildLog(db)
    log.record(
        app="example", bundle_id="com.example.app", version="1.0",
        tweaks=[], ipa_path=None, log_path=None, signing_team=None,
        udid=None, success=True, error=None, target="mac",
    )
    assert log.recent(app=None, limit=1)[0]["target"] == "mac"


# ---- LiveContainer installs ----
#
# `--lc-status` reports `managed_by: graft` only for a LiveContainer this
# tool installed *on that device*, so the record is per (udid, bundle id).


def test_an_lc_install_is_remembered_for_its_device_only(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    log.record_lc_install(
        udid="ipad", bundle_id="com.kdt.LiveContainer2.T", version="3.8.14",
        channel="nightly", ipa_sha256="ab",
    )

    assert log.lc_installed_bundle_ids("ipad") == {"com.kdt.LiveContainer2.T"}
    assert log.lc_installed_bundle_ids("iphone") == set()


def test_pushed_by_instance_is_per_device_and_per_livecontainer(tmp_path: Path) -> None:
    """`--lc-clean` deletes on this, and a push overwrites on it: only rows for
    this device count, each under the container it went into."""
    log = BuildLog(tmp_path / "log.sqlite")
    common = dict(
        bundle_id="b", inputs_digest=None, digest_version=None, cyan=None, tweaks=[],
        source_version="1", ipa_sha256=None, delivery="livecontainer",
    )
    log.record_delivery(app="example", lc_bundle_id="LC1", remote_name="example.ipa",
                        udid="ipad", **common)
    log.record_delivery(app="bravo", lc_bundle_id="LC2", remote_name="bravo.ipa",
                        udid="ipad", **common)
    log.record_delivery(app="delta", lc_bundle_id="LC1", remote_name=None, udid="ipad", **common)
    log.record_delivery(app="echo", lc_bundle_id="LC1", remote_name="echo.ipa",
                        udid="iphone", **common)
    # No device recorded: proves nothing about any one device.
    log.record_delivery(app="fox", lc_bundle_id="LC1", remote_name="fox.ipa", udid=None,
                        **common)

    assert log.pushed_by_instance("ipad") == {"LC1": {"example.ipa"}, "LC2": {"bravo.ipa"}}
    assert log.pushed_by_instance("iphone") == {"LC1": {"echo.ipa"}}
    assert log.pushed_by_instance("ipod") == {}


def test_a_cert_push_is_recorded_per_device_and_forgotten_once_cleaned(tmp_path: Path) -> None:
    log = BuildLog(tmp_path / "log.sqlite")
    log.record_cert_push(udid="ipad", bundle_id="LC1", name="AppleDevelopment.p12")
    log.record_cert_push(udid="iphone", bundle_id="LC1", name="AppleDevelopment.p12")

    assert log.cert_pushes("ipad") == {"LC1": {"AppleDevelopment.p12"}}

    log.forget_cert_push(udid="ipad", bundle_id="LC1")

    assert log.cert_pushes("ipad") == {}
    assert log.cert_pushes("iphone") == {"LC1": {"AppleDevelopment.p12"}}


def test_a_v1_database_gains_the_cert_pushes_table_and_keeps_its_deliveries(
    tmp_path: Path,
) -> None:
    """A log.sqlite from 1.0 has deliveries with lc_bundle_id, remote_name and
    udid (all `--lc-clean` and the push need) and no cert_pushes table."""
    db = tmp_path / "log.sqlite"
    log = BuildLog(db)
    log.record_delivery(app="example", bundle_id="b", inputs_digest=None, digest_version=None,
                        cyan=None, tweaks=[], source_version="1", ipa_sha256=None,
                        delivery="livecontainer", lc_bundle_id="LC1",
                        remote_name="example.ipa", udid="ipad")
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE cert_pushes")

    reopened = BuildLog(db)

    assert reopened.cert_pushes("ipad") == {}
    assert reopened.pushed_by_instance("ipad") == {"LC1": {"example.ipa"}}


def test_a_database_from_before_lc_installs_gains_the_table(tmp_path: Path) -> None:
    db = tmp_path / "log.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE builds (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL,"
            " app TEXT NOT NULL, bundle_id TEXT NOT NULL, version TEXT NOT NULL,"
            " tweaks TEXT NOT NULL, ipa_sha256 TEXT, ipa_path TEXT, log_path TEXT,"
            " signing_team TEXT, udid TEXT, target TEXT, success INTEGER NOT NULL,"
            " error TEXT)"
        )

    log = BuildLog(db)
    log.record_lc_install(udid="ipad", bundle_id="b", version="1", channel=None, ipa_sha256=None)

    assert log.lc_installed_bundle_ids("ipad") == {"b"}
