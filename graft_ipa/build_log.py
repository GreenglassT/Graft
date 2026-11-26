"""SQLite-backed build history."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    app TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    version TEXT NOT NULL,
    tweaks TEXT NOT NULL,        -- comma-separated list
    ipa_sha256 TEXT,             -- hash of the signed IPA
    ipa_path TEXT,               -- absolute path to the archived IPA
    log_path TEXT,               -- absolute path to the per-build JSON-Lines log
    signing_team TEXT,
    udid TEXT,
    target TEXT,                 -- 'device' or 'mac'; NULL means a row written
                                 -- before this column existed, which is a
                                 -- device build. Read through `_target_of`.
    delivery TEXT,               -- 'sideload' or 'livecontainer': how the
                                 -- archive was built (zsign-signed, or
                                 -- unsigned for LiveContainer). NULL is a row
                                 -- from before the column; `--reinstall-build`
                                 -- reads the archive itself then.
    success INTEGER NOT NULL,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_builds_app ON builds(app);
CREATE INDEX IF NOT EXISTS idx_builds_timestamp ON builds(timestamp);

-- What actually reached a device, as opposed to what was built. Only the push
-- path writes here; see `record_delivery`.
--
-- `inputs_digest`, `digest_version` and `cyan` are all nullable by design: a
-- row written before this feature existed carries none of them, and
-- `delivery.diff_delivery` reports "can't tell" for such a row rather than
-- guessing. A NOT NULL constraint would make that guard dead code.
CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    app TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    inputs_digest TEXT,
    digest_version INTEGER,      -- which hashing format produced it; see spec 4.2
    cyan TEXT,                   -- JSON of the hashed cyan options; see spec 5.1
    tweaks TEXT NOT NULL,        -- JSON [[name, sha256], ...]
    source_version TEXT NOT NULL,
    ipa_sha256 TEXT,
    delivery TEXT NOT NULL,
    lc_bundle_id TEXT,
    remote_name TEXT,
    udid TEXT,
    -- How the artifact got there: 'build' for the push at the end of a build,
    -- 'reinstall' for `--reinstall-build` re-shipping an archived IPA. The
    -- distinction is not cosmetic: an archive's build inputs were never
    -- recorded and cannot be reconstructed (the tweak files on disk now are
    -- not necessarily the ones inside it), so such a row carries no digest and
    -- must never be read as `current`. 'reinstall-recorded' is the exception
    -- that proves it: a re-ship of bytes (same ipa_sha256) that an earlier
    -- push recorded the inputs of, so it carries that push's digest and is
    -- compared like a build. NULL is a row written before this column
    -- existed, i.e. a build.
    origin TEXT,
    reconciled_at TEXT,
    reconcile_ok INTEGER,
    -- When `--lc-clean` removed what this row pushed. A cleaned row no longer
    -- vouches for a file of that name (`pushed_by_instance`): one the user puts
    -- there afterward is theirs. NULL: not cleaned, or from before the column.
    cleaned_at TEXT,
    -- `InputsDigest.held_version_id`: the App Store version id of the build
    -- the app was held at, which the digest hashed. NULL: no hold by build,
    -- or a row from before the column (whose digest hashed none).
    held_version_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_deliveries_app ON deliveries(app);

-- LiveContainers this tool installed, per device. `--lc-status` reads it to
-- tell a LiveContainer we manage from one AltStore (or a hand-run script) put
-- there; see `lc_manage.status_payload`.
CREATE TABLE IF NOT EXISTS lc_installs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    udid TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    version TEXT NOT NULL,
    channel TEXT,
    ipa_sha256 TEXT
);

-- Certificates `--lc-push-cert` put into a LiveContainer's Documents, per
-- device, until `--lc-clean` removes them. The only evidence `--lc-clean` takes
-- that an `AppleDevelopment.p12` there is ours: the name is generic, and one
-- pushed before this table existed is reported, not deleted.
CREATE TABLE IF NOT EXISTS cert_pushes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    udid TEXT NOT NULL,
    bundle_id TEXT NOT NULL,
    name TEXT NOT NULL
);
"""


def _declared_columns(table: str) -> list[tuple[str, str]]:
    """(name, type) for every column SCHEMA declares on `table`.

    Read out of SCHEMA rather than restated as a second list, because a second
    list is one more thing to forget: the failure it guards against is somebody
    adding a column above and nowhere else.
    """
    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS {table} \((.*?)\n\);", SCHEMA, re.DOTALL
    )
    if body is None:  # pragma: no cover - only reachable by renaming a table
        raise KeyError(f"SCHEMA declares no table named {table!r}")
    columns = []
    for line in body.group(1).splitlines():
        # Strip the trailing comment before matching, so a `-- ...` note that
        # happens to mention a type can't be read as a column.
        match = re.match(r"([A-Za-z_]\w*)\s+(TEXT|INTEGER|REAL|BLOB)\b", line.split("--")[0].strip())
        if match:
            columns.append((match.group(1), match.group(2)))
    return columns


def _target_of(row: sqlite3.Row) -> str:
    """`'device'` or `'mac'`, never None.

    A closed set of two, not whatever the column happens to hold. A row
    predating the column reads NULL, and NULL here means a build made before
    Mac builds existed — which is a device build. So does anything else
    unrecognized: `'Mac'`, `'macos'`, a typo. Only the exact string a writer
    means by "this artifact was prepared for this Mac" reads as `mac`.

    That asymmetry is deliberate, because the two errors are not equal. A Mac
    build offered to a phone points a tweak dylib at a stub Preferences
    framework that iOS has for real, and breaks it silently at runtime; a Mac
    build read as a device build merely withholds an action. Falling back to
    `device` is the survivable direction, and making it the default for
    everything except one spelling means no future writer can reach the other
    one by accident.

    Normalized here rather than in each consumer, so that "unrecognized means
    phone" is written down once: `--builds` and the GUI reading its JSON are
    both about to branch on this, and each would get it wrong differently.

    The `except` is belt and braces, not the live path — with the migration in
    place `recent()` always hands over a row that has the column, and the NULL
    it carries is handled below. It fires only if the column is missing
    outright, which a `sqlite3.Row` reports as IndexError, not KeyError (both
    for an unknown name and an out-of-range position — measured). A dict would
    raise KeyError, but this is only ever handed a Row, so catching that too
    would be an arm nothing can reach and a claim the code cannot back.
    """
    try:
        value = row["target"]
    except IndexError:
        return "device"
    return value if value == "mac" else "device"


class BuildLog:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            # CREATE TABLE IF NOT EXISTS is a no-op against a database that
            # already has the table, so a column added to SCHEMA never reaches
            # one — and every reader indexes rows by name, which makes a
            # missing column an IndexError on `--list`, the GUI's whole app
            # list. Added bare (no NOT NULL, no default) because that is all
            # ALTER TABLE ADD COLUMN can do; every nullable column here already
            # reads NULL as "written before this existed". `builds` had three
            # hand-written ALTERs (ipa_path, log_path, target) before it got
            # this general form too, with `delivery`.
            for table in ("builds", "deliveries"):
                live = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                for name, decl in _declared_columns(table):
                    if name not in live:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def record(
        self,
        *,
        app: str,
        bundle_id: str,
        version: str,
        tweaks: list[str],
        ipa_path: Path | None,
        signing_team: str | None,
        udid: str | None,
        success: bool,
        error: str | None = None,
        log_path: Path | None = None,
        target: str = "device",
        delivery: str | None = None,
    ) -> int:
        """One build's row. `delivery` is the app's mode the build ran in,
        which decides what the archive is: zsign-signed for `sideload`,
        unsigned for `livecontainer`."""
        ipa_hash = sha256_of(ipa_path) if ipa_path and ipa_path.exists() else None
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO builds (
                    timestamp, app, bundle_id, version, tweaks,
                    ipa_sha256, ipa_path, log_path, signing_team, udid, target,
                    delivery, success, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    app,
                    bundle_id,
                    version,
                    ",".join(tweaks),
                    ipa_hash,
                    str(ipa_path) if ipa_path else None,
                    str(log_path) if log_path else None,
                    signing_team,
                    udid,
                    target,
                    delivery,
                    1 if success else 0,
                    error,
                ),
            )
            return int(cur.lastrowid or 0)

    def clear_ipa_path(self, ipa_path: Path) -> list[int]:
        """NULL out `ipa_path` for every row pointing at `ipa_path`.

        Returns the ids of the rows changed. Call this *before* removing the
        file: the GUI offers "Re-install" for exactly the rows with a non-null
        ipa_path, so a row that still points at a deleted archive renders a
        button that can only fail. Clearing first and then failing to remove
        the file is the survivable half of that race — the archive is still on
        disk, only the shortcut to it is gone.

        Matching is on the same string `record()` writes (`str(ipa_path)`), and
        both sides are built from `cfg.built_dir`, so the spellings agree.
        """
        target = str(ipa_path)
        with self._connect() as conn:
            ids = [
                int(row[0])
                for row in conn.execute(
                    "SELECT id FROM builds WHERE ipa_path = ?", (target,)
                ).fetchall()
            ]
            if ids:
                conn.execute(
                    "UPDATE builds SET ipa_path = NULL WHERE ipa_path = ?", (target,)
                )
        return ids

    def restore_ipa_path(self, build_ids: list[int], ipa_path: Path) -> None:
        """Point `build_ids` back at `ipa_path`: undoes `clear_ipa_path` for a
        prune whose move to the Trash then failed, so the archive still on
        disk keeps its Re-install button."""
        if not build_ids:
            return
        with self._connect() as conn:
            conn.executemany(
                "UPDATE builds SET ipa_path = ? WHERE id = ? AND ipa_path IS NULL",
                [(str(ipa_path), i) for i in build_ids],
            )

    def reinstallable_paths(self) -> list[Path]:
        """Every archive a successful row still points at: what has a
        Re-install button. Retention gives keep slots to these alone
        (`retention.plan_prune`)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT ipa_path FROM builds"
                " WHERE success = 1 AND ipa_path IS NOT NULL"
            ).fetchall()
        return [Path(r[0]) for r in rows]

    def tweaks_of(self, ipa_path: Path) -> list[str] | None:
        """The tweaks the newest successful build archived at `ipa_path` was
        built with, or None when no row names that archive. Matched on the
        string `record()` writes, as `clear_ipa_path` is."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tweaks FROM builds WHERE ipa_path = ? AND success = 1"
                " ORDER BY id DESC LIMIT 1",
                (str(ipa_path),),
            ).fetchone()
        if row is None:
            return None
        return [t for t in (row[0] or "").split(",") if t]

    def recent(self, app: str | None = None, limit: int = 10) -> list[dict]:
        """Recent build rows, as dicts whose `target` is always populated.

        Dicts rather than `sqlite3.Row` because `ALTER TABLE ADD COLUMN` cannot
        supply a default for rows that already exist, so `target` has to be
        normalized on read — and a Row is immutable. Every caller indexes by
        name, so the swap is invisible to them. What would not survive it is
        positional access (`r[0]`), and subscripting or comparing the key view
        as a list (`r.keys()[0]`, `r.keys() == [...]`) — `dict.keys()` itself
        works and yields the identical names. No caller uses any of it.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            if app:
                cur = conn.execute(
                    "SELECT * FROM builds WHERE app = ? ORDER BY id DESC LIMIT ?",
                    (app, limit),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM builds ORDER BY id DESC LIMIT ?", (limit,)
                )
            return [dict(r) | {"target": _target_of(r)} for r in cur.fetchall()]

    def record_delivery(
        self,
        *,
        app: str,
        bundle_id: str,
        inputs_digest: str | None,
        digest_version: int | None,
        cyan: dict[str, object] | None,
        tweaks: list[list[str | None]],
        source_version: str,
        ipa_sha256: str | None,
        delivery: str,
        lc_bundle_id: str | None,
        remote_name: str | None,
        udid: str | None,
        origin: str = "build",
        held_version_id: str | None = None,
    ) -> int:
        """Record that an artifact reached a device.

        Written on the PUSH path, never the build path: "built but never
        delivered" is one of the states this exists to surface, so a build that
        is not pushed must leave no row behind.

        `digest_version` is `InputsDigest.version`, not a constant read at
        write time: it is what lets a later comparison say "a different version
        of this tool delivered that" instead of inventing reasons out of a
        `cyan` payload whose key set has since changed.

        `origin` defaults to the ordinary case. Only `--reinstall-build` passes
        anything else, and what it means is on the column in SCHEMA.
        """
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO deliveries (
                    timestamp, app, bundle_id, inputs_digest, digest_version, cyan,
                    tweaks, source_version, ipa_sha256, delivery, lc_bundle_id,
                    remote_name, udid, origin, held_version_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now(UTC).isoformat(),
                    app,
                    bundle_id,
                    inputs_digest,
                    digest_version,
                    # `is not None`, not a truth test: NULL here is the signal
                    # for "row from before the column existed", the one path
                    # that still yields the generic "build options changed"
                    # reason. An empty dict is a row that recorded its options
                    # and must not be mistaken for one that couldn't.
                    json.dumps(cyan, sort_keys=True, separators=(",", ":"))
                    if cyan is not None
                    else None,
                    json.dumps(tweaks, separators=(",", ":")),
                    source_version,
                    ipa_sha256,
                    delivery,
                    lc_bundle_id,
                    remote_name,
                    udid,
                    origin,
                    held_version_id,
                ),
            )
            return int(cur.lastrowid or 0)

    def recorded_delivery_of(self, app: str, ipa_sha256: str) -> sqlite3.Row | None:
        """The newest delivery of `app` that shipped exactly these bytes and
        recorded what went into them, or None.

        What lets a re-install of an archive vouch for its inputs: the archive
        is byte-for-byte what that push shipped. A row with no digest (a plain
        'reinstall') is no evidence, so it is never returned.
        """
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT * FROM deliveries WHERE app = ? AND ipa_sha256 = ?"
                " AND inputs_digest IS NOT NULL ORDER BY id DESC LIMIT 1",
                (app, ipa_sha256),
            ).fetchone()

    def delivery_udids(self, app: str) -> dict[str | None, int]:
        """Every device `app` was delivered to, with the id of its newest
        delivery there: `graft --update`'s targets (spec §11.3). Rows recorded
        without a device are under None."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT udid, MAX(id) FROM deliveries WHERE app = ? GROUP BY udid", (app,)
            ).fetchall()
        out: dict[str | None, int] = {}
        for udid, last in rows:
            key = str(udid) if udid else None
            out[key] = max(out.get(key, 0), int(last))
        return out

    def latest_delivery(
        self, app: str, *, udid: str | None = None, or_unrecorded: bool = False
    ) -> sqlite3.Row | None:
        """The most recent delivery recorded for `app`, or None.

        Ordered by `id`, not `timestamp`: the id is monotonic by construction,
        so it stays correct across a system clock that steps backwards (NTP
        correction, a timezone-confused VM), where ordering by the recorded
        wall time would hand back a superseded row as the latest one.

        `udid` scopes it to one phone. The filter is inside the query, not
        applied to its result — "the latest row for this device" and "the
        latest row, if it happens to be this device" are different questions
        and only the first is useful. Unscoped remains "the latest anywhere",
        which is what every existing caller means.

        A row whose `udid` is NULL is invisible to a scoped query, because SQL
        `= NULL` never matches. That is correct: we cannot claim an artifact
        reached this phone when nothing recorded where it went. The one
        exception is `or_unrecorded`, for a caller that has decided those
        rows are this phone's (`graft --update`'s default device, spec §11.3).
        """
        sql = "SELECT * FROM deliveries WHERE app = ?"
        params: list[object] = [app]
        if udid is not None:
            sql += " AND (udid = ? OR udid IS NULL)" if or_unrecorded else " AND udid = ?"
            params.append(udid)
        sql += " ORDER BY id DESC LIMIT 1"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchone()

    def mark_reconciled(self, delivery_id: int, *, seen: bool) -> None:
        """Note that the device was checked, and whether the artifact was there.

        Both outcomes are recorded. `reconcile_ok` stays NULL only while a row
        has never been checked, which is a different claim from "checked and
        absent" — the reader distinguishes the three.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE deliveries SET reconciled_at = ?, reconcile_ok = ? WHERE id = ?",
                (datetime.now(UTC).isoformat(), 1 if seen else 0, delivery_id),
            )


    def record_lc_install(
        self, *, udid: str, bundle_id: str, version: str, channel: str | None,
        ipa_sha256: str | None,
    ) -> None:
        """Note that `--lc-install` put `bundle_id` on `udid`."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO lc_installs (timestamp, udid, bundle_id, version, channel,"
                " ipa_sha256) VALUES (?, ?, ?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), udid, bundle_id, version, channel, ipa_sha256),
            )

    def pushed_by_instance(self, udid: str) -> dict[str, set[str]]:
        """Every LiveContainer a delivery to `udid` went into, with the file names pushed.

        The evidence `--lc-clean` deletes on, and the push overwrites on, so it
        is exactly that: rows for this device and this container. A row with
        no device recorded proves nothing about this one; both push sites have
        stored the udid since the table existed (v1.0 included), so only a
        hand-made row lacks it.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT lc_bundle_id, remote_name FROM deliveries"
                " WHERE lc_bundle_id IS NOT NULL AND remote_name IS NOT NULL"
                " AND udid = ? AND cleaned_at IS NULL",
                (udid,),
            ).fetchall()
        out: dict[str, set[str]] = {}
        for bundle_id, name in rows:
            out.setdefault(bundle_id, set()).add(name)
        return out

    def forget_pushes(self, *, udid: str, bundle_id: str) -> None:
        """`--lc-clean` emptied `bundle_id` of what Graft pushed to `udid`: those
        records stop vouching for the names they pushed, so a file the user
        later puts there under one of them is theirs, and the push refuses it.

        Marked, not deleted: the rows are delivery history too.
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE deliveries SET cleaned_at = ? WHERE udid = ? AND lc_bundle_id = ?"
                " AND cleaned_at IS NULL",
                (datetime.now(UTC).isoformat(), udid, bundle_id),
            )

    def record_cert_push(self, *, udid: str, bundle_id: str, name: str) -> None:
        """Note that `--lc-push-cert` put `name` into `bundle_id`'s Documents."""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO cert_pushes (timestamp, udid, bundle_id, name) VALUES (?, ?, ?, ?)",
                (datetime.now(UTC).isoformat(), udid, bundle_id, name),
            )

    def cert_pushes(self, udid: str) -> dict[str, set[str]]:
        """Where on `udid` a certificate push is recorded and not yet cleaned:
        LiveContainer bundle id → the file names pushed."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT bundle_id, name FROM cert_pushes WHERE udid = ?", (udid,)
            ).fetchall()
        out: dict[str, set[str]] = {}
        for bundle_id, name in rows:
            out.setdefault(bundle_id, set()).add(name)
        return out

    def forget_cert_push(self, *, udid: str, bundle_id: str) -> None:
        """`--lc-clean` took the certificate out of `bundle_id`: the record goes
        with it, so a certificate the user puts there later is not ours."""
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM cert_pushes WHERE udid = ? AND bundle_id = ?", (udid, bundle_id)
            )

    def lc_installed_bundle_ids(self, udid: str) -> set[str]:
        """Every LiveContainer bundle id this tool has installed on `udid`."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT bundle_id FROM lc_installs WHERE udid = ?", (udid,)
            ).fetchall()
        return {r[0] for r in rows}


def delivered_udids(db_path: Path) -> set[str] | None:
    """Every device the delivery log records a delivery to, read read-only:
    the polled `--devices` asks, and must not migrate or write-lock the file
    a build is writing. `set()` when there is no log yet; None when there is
    one that can't be read, which callers treat as "don't know"."""
    if not db_path.exists():
        return set()
    try:
        with contextlib.closing(
            sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        ) as conn:
            rows = conn.execute(
                "SELECT DISTINCT udid FROM deliveries WHERE udid IS NOT NULL"
            ).fetchall()
    except sqlite3.Error:
        return None
    return {str(udid) for (udid,) in rows if udid}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
