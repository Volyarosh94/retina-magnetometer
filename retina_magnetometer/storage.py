"""Local storage: SQLite, bounded in time and in bytes.

Three tables:

- ``samples``: every measurement, (time, x, y, z) in nT, kept for
  ``raw_retention_days``. The time in milliseconds is the rowid, so range
  scans are index walks.
- ``minutes``: min/mean/max of each axis and of |B| per UTC minute, kept for
  ``rollup_retention_days``. Long views read these, so a year of history
  costs 525,600 rows however fast the sensor samples.
- ``sessions``: one row each time sampling (re)starts, with the cycle count,
  rate, mode, bus and address in force, so every stored value can be traced
  to the gain that produced it.

Bounded two ways. Time: rows past their retention are deleted every prune.
Bytes: if the data outgrow ``max_db_mb`` (a fast rate with a long raw
retention on a small card), the oldest go until the data are back under 90 %
of the cap. Raw samples that the minute summaries already cover go first,
then the oldest minutes; raw samples not yet summarised go only if nothing
else is left. A cap too small for the minute summaries' own retention would
otherwise cost every raw sample at each prune, so while both tables have to
give way, the raw samples keep half of the room. The size measured is the
data's (the pages in use), not the file's: a reader holding an old snapshot
keeps the WAL from being checkpointed, and deleting more rows could not
shrink it. Freed pages are returned to the filesystem with incremental vacuum
and the WAL is truncated, so the cap is a cap on disk use and not just on row
counts.

Written for an SD card. Samples are buffered and written in one transaction
per flush (every few seconds), in WAL mode with synchronous=NORMAL: a power
cut can lose the last flush, never the database.

Opened lazily, and opened again whenever opening failed or the file went
missing (deleted, or replaced, while the app runs). A data directory that
cannot be written or a file that cannot be opened makes every call raise
``StorageUnavailable`` saying why, and the app keeps running and reporting it
until the problem is fixed, without a restart. A file that is not an SQLite
database at all, or whose schema is damaged (an SD card fault), is moved aside
under a name stamped with the time, never deleted, and a new database started.
One written by a newer version of the app (its ``user_version`` is past
``SCHEMA_VERSION``, as after a rollback) is left exactly as it is, and
reported: the newer version will want it back.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from retina_magnetometer import series as shape

log = logging.getLogger(__name__)

# Kept in the file's header as its user_version. A version that changes the
# schema raises it, so that an older one, after a rollback, knows to leave the
# database alone.
SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    t_ms INTEGER PRIMARY KEY,
    x REAL NOT NULL,
    y REAL NOT NULL,
    z REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS minutes (
    t_ms INTEGER PRIMARY KEY,
    n INTEGER NOT NULL,
    x_min REAL, x_mean REAL, x_max REAL,
    y_min REAL, y_mean REAL, y_max REAL,
    z_min REAL, z_mean REAL, z_max REAL,
    b_min REAL, b_mean REAL, b_max REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    started_ms INTEGER PRIMARY KEY,
    cycle_count INTEGER NOT NULL,
    gain_lsb_per_ut REAL NOT NULL,
    rate_hz REAL NOT NULL,
    mode TEXT NOT NULL,
    bus TEXT NOT NULL,
    address INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

MINUTE_MS = 60_000

# What a row takes on disk with real readings (no column holds a whole number,
# which SQLite would store as a small integer); tests/test_storage.py measures
# both. The size cap sizes its steps and splits the room between the tables
# with them. What it deletes is measured, so an error here costs steps, not
# data.
SAMPLE_ROW_BYTES = 36
MINUTE_ROW_BYTES = 126
# The rows one step of the size cap deletes, at least and at most: enough to
# free whole pages, and a bounded transaction (and WAL) per step.
CAP_STEP_MIN_ROWS = 256
CAP_STEP_MAX_ROWS = 100_000
# How long the WAL truncation at each prune waits for readers. A reader holding
# an old snapshot (a long query, an open sqlite3 shell) pins the WAL until it
# finishes; the next prune tries again, rather than hold up the writer, and with
# it every flush, for the full busy timeout.
CHECKPOINT_WAIT_MS = 1000

# Errors that mean the file is not a usable database at all, as opposed to one
# that cannot be reached (permissions, a full or missing disk, a lock).
_UNREADABLE = (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB)
# What an unreadable database is moved aside as: its name, the time and, if
# that second is taken, a number. Nothing else in the directory matches, so
# nothing else is ever counted as one.
_SET_ASIDE = r"\.unreadable-\d{8}T\d{6}Z(?:-\d+)?(?:-wal|-shm)?"

_SAMPLE_STATS = ("SELECT COUNT(*) FROM samples", "SELECT MIN(t_ms) FROM samples", "SELECT MAX(t_ms) FROM samples")
_MINUTE_STATS = ("SELECT COUNT(*) FROM minutes", "SELECT MIN(t_ms) FROM minutes", "SELECT MAX(t_ms) FROM minutes")


class StorageUnavailable(sqlite3.OperationalError):
    """The database cannot be opened; the message says why."""


class Storage:
    """One database file. Safe to share between threads: every call opens its
    own short-lived connection except the writer's, which is serialised.

    Constructing one never fails. If the file cannot be opened, ``unavailable``
    says why and every call tries again before raising ``StorageUnavailable``,
    so a permission fixed or a card replaced is picked up as it happens. A
    call that finds the database gone has the next one open it afresh."""

    def __init__(
        self,
        path: Path,
        *,
        raw_retention_days: float,
        rollup_retention_days: float,
        max_db_mb: float,
        clock=time.time,
    ):
        self.path = Path(path)
        self.raw_retention_ms = int(raw_retention_days * 86_400_000)
        self.rollup_retention_ms = int(rollup_retention_days * 86_400_000)
        self.max_bytes = int(max_db_mb * 1024 * 1024)
        self._clock = clock
        self._write_lock = threading.Lock()
        self._ready = False
        self.unavailable: str | None = None
        # Set when this run moved an unreadable file aside: when, why, and
        # where to (also kept in the new database's meta table).
        self.moved_aside: dict | None = None
        try:
            self._ensure_ready()
        except StorageUnavailable as exc:
            log.error("%s; will keep trying", exc)

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 10000")
        db.create_function("magnitude", 3, shape.magnitude, deterministic=True)
        return db

    # ── Opening ──────────────────────────────────────────────────────────────

    def _ensure_ready(self) -> None:
        if self._ready:
            return
        with self._write_lock:
            if self._ready:
                return
            try:
                self._open()
            except (sqlite3.Error, OSError) as exc:
                self.unavailable = f"cannot open {self.path}: {exc}"
                raise StorageUnavailable(self.unavailable) from exc
            self.unavailable = None
            self._ready = True

    @contextmanager
    def _opened(self) -> Iterator[None]:
        """Around every call: the database opened first, and opened afresh by
        the next call if this one finds it gone (deleted or replaced while the
        app runs, which leaves an empty or unreadable file in its place)."""
        self._ensure_ready()
        try:
            yield
        except sqlite3.Error as exc:
            if _gone(exc):
                self._ready = False
            raise

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create()
        except sqlite3.DatabaseError as exc:
            if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) not in _UNREADABLE:
                raise
            # A schema this SQLite cannot read may be a newer app's, after a
            # rollback; the header says, even when SQLite will not.
            version = _header_version(self.path)
            if version is not None and version > SCHEMA_VERSION:
                raise StorageUnavailable(_newer(version)) from exc
            self._move_aside(exc)
            self._create()
            # Kept in the new database, so the page explains the missing
            # history for as long as this database lasts, restarts included.
            db = self._connect()
            try:
                db.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('moved_aside', ?)",
                    (json.dumps(self.moved_aside),),
                )
            finally:
                db.close()

    def _create(self) -> None:
        db = self._connect()
        try:
            # First, before anything is written: a newer app's database is
            # left as it is. (The header is readable even when the schema is
            # not.)
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StorageUnavailable(_newer(version))
            # auto_vacuum only takes effect before the first table exists.
            db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(_SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            if version < SCHEMA_VERSION:
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            db.close()

    def _move_aside(self, exc: sqlite3.Error) -> None:
        now = self._clock()
        stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        kept = self.path.with_name(f"{self.path.name}.unreadable-{stamp}")
        number = 1
        while any(Path(f"{kept}{suffix}").exists() for suffix in ("", "-wal", "-shm")):
            number += 1
            kept = self.path.with_name(f"{self.path.name}.unreadable-{stamp}-{number}")
        # The WAL and its index go first: a stale WAL beside a new database
        # would be replayed into it. Nothing is deleted, earlier copies
        # included; the page shows how many there are and what they take.
        for suffix in ("-wal", "-shm", ""):
            source = Path(f"{self.path}{suffix}")
            if source.exists():
                os.replace(source, f"{kept}{suffix}")
        self.moved_aside = {"at_ms": int(now * 1000), "reason": str(exc), "kept_as": kept.name}
        log.error(
            "%s is not a readable database (%s); moved it aside as %s and started a new one", self.path, exc, kept
        )

    def _set_aside(self) -> dict | None:
        """The unreadable databases moved aside so far, and the bytes they
        take with their WAL files, or None if there are none."""
        pattern = re.compile(re.escape(self.path.name) + _SET_ASIDE)
        names = [p for p in self.path.parent.iterdir() if pattern.fullmatch(p.name)]
        if not names:
            return None
        databases = [p for p in names if not p.name.endswith(("-wal", "-shm"))]
        return {"count": len(databases), "bytes": sum(p.stat().st_size for p in names)}

    # ── Writes ───────────────────────────────────────────────────────────────

    def write_samples(self, rows: list[tuple[int, float, float, float]], *, sessions: Sequence[dict] = ()) -> None:
        """Insert (t_ms, x, y, z) rows, and session records, in one transaction.

        A session is a dict of ``start_session``'s arguments, ``started_ms``
        included. Rows behind the rollup watermark (a backfill, or samples
        stamped after the clock stepped back) have their minutes summarised
        again, so the summaries keep agreeing with the samples.
        """
        if not rows and not sessions:
            return
        with self._opened(), self._write_lock:
            db = self._connect()
            try:
                db.execute("BEGIN")
                if sessions:
                    db.executemany(
                        "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [_session_row(s) for s in sessions],
                    )
                if rows:
                    db.executemany("INSERT OR REPLACE INTO samples (t_ms, x, y, z) VALUES (?, ?, ?, ?)", rows)
                    watermark = _watermark(db)
                    first = min(row[0] for row in rows)
                    if watermark is not None and first < watermark:
                        last = max(row[0] for row in rows)
                        end = min(watermark, (last // MINUTE_MS + 1) * MINUTE_MS)
                        _summarise(db, (first // MINUTE_MS) * MINUTE_MS, end)
                db.execute("COMMIT")
            finally:
                db.close()

    def start_session(
        self,
        *,
        cycle_count: int,
        gain: float,
        rate_hz: float,
        mode: str,
        bus: str,
        address: int,
        started_ms: int | None = None,
    ) -> None:
        session = {
            "started_ms": int(self._clock() * 1000) if started_ms is None else started_ms,
            "cycle_count": cycle_count,
            "gain": gain,
            "rate_hz": rate_hz,
            "mode": mode,
            "bus": bus,
            "address": address,
        }
        self.write_samples([], sessions=[session])

    def rollup(self, now_ms: int | None = None, *, pending_from_ms: int | None = None, grace_ms: int = 15_000) -> int:
        """Summarise every complete minute not yet summarised.

        A minute is complete once nothing before its end is still to be
        written: ``pending_from_ms`` is the oldest sample the writer still
        holds (None if it holds none), and ``grace_ms`` covers a sample on its
        way from the sensor to the writer. Re-summarising is harmless (the
        minute's row is replaced), which is what makes this safe after a
        crash: the watermark only moves after the rows are written.
        """
        now_ms = int(self._clock() * 1000) if now_ms is None else now_ms
        complete = now_ms - grace_ms if pending_from_ms is None else min(now_ms - grace_ms, pending_from_ms)
        end = (complete // MINUTE_MS) * MINUTE_MS
        with self._opened(), self._write_lock:
            db = self._connect()
            try:
                start = _watermark(db)
                if start is None:
                    first = db.execute("SELECT MIN(t_ms) FROM samples").fetchone()[0]
                    start = (first // MINUTE_MS) * MINUTE_MS if first is not None else end
                if end <= start:
                    return 0
                db.execute("BEGIN")
                written = _summarise(db, start, end)
                db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('rollup_watermark', ?)", (str(end),))
                db.execute("COMMIT")
                return written
            finally:
                db.close()

    def summarise_range(self, start_ms: int, end_ms: int) -> int:
        """(Re)build the minute summaries for a range, whatever the watermark.

        For history written behind the live edge, such as a backfill.
        """
        start = (start_ms // MINUTE_MS) * MINUTE_MS
        with self._opened(), self._write_lock:
            db = self._connect()
            try:
                db.execute("BEGIN")
                written = _summarise(db, start, end_ms)
                db.execute("COMMIT")
                return written
            finally:
                db.close()

    def prune(self, now_ms: int | None = None) -> dict:
        """Apply retention, then the size cap. Returns what was removed."""
        now_ms = int(self._clock() * 1000) if now_ms is None else now_ms
        removed = {"samples": 0, "minutes": 0, "size_capped": False}
        with self._opened(), self._write_lock:
            db = self._connect()
            try:
                removed["samples"] += db.execute(
                    "DELETE FROM samples WHERE t_ms < ?", (now_ms - self.raw_retention_ms,)
                ).rowcount
                removed["minutes"] += db.execute(
                    "DELETE FROM minutes WHERE t_ms < ?", (now_ms - self.rollup_retention_ms,)
                ).rowcount
                if _data_bytes(db) > self.max_bytes:
                    removed["size_capped"] = True
                    self._cap(db, removed)
                    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('size_capped_ms', ?)", (str(now_ms),))
                self._reclaim(db)
            finally:
                db.close()
        return removed

    def _cap(self, db: sqlite3.Connection, removed: dict) -> None:
        """Delete the oldest data until they fit in 90 % of the cap, in the
        order the module docstring gives. Each step deletes about what the
        excess needs and the next measures again, so the cap takes what it
        must and at most a step more."""
        target = int(self.max_bytes * 0.9)
        summarised = _watermark(db)
        minutes = db.execute("SELECT COUNT(*) FROM minutes").fetchone()[0]
        while (used := _data_bytes(db)) > target:
            excess = used - target
            minute_bytes = minutes * MINUTE_ROW_BYTES
            # The raw samples' share: all the room the minutes leave, and never
            # less than half of it while the minutes have to give way too.
            raw_excess = min(excess, used - minute_bytes - max(target - minute_bytes, target // 2))
            if (
                raw_excess > 0
                and summarised is not None
                and (deleted := _delete_oldest(db, "samples", _step(raw_excess, SAMPLE_ROW_BYTES), summarised))
            ):
                removed["samples"] += deleted
            elif minutes and (deleted := _delete_oldest(db, "minutes", _step(excess, MINUTE_ROW_BYTES))):
                removed["minutes"] += deleted
                minutes -= deleted
            elif deleted := _delete_oldest(db, "samples", _step(excess, SAMPLE_ROW_BYTES)):
                # Nothing left but samples the minutes do not cover yet (the
                # rollup is failing, or far behind): the cap holds regardless.
                removed["samples"] += deleted
            else:
                break

    def _reclaim(self, db: sqlite3.Connection) -> None:
        # Both pragmas do their work as the statement is stepped, so the rows
        # must be fetched: execute() alone steps once, and incremental_vacuum
        # frees one page per step — without fetchall it frees almost nothing.
        db.execute("PRAGMA incremental_vacuum").fetchall()
        db.execute(f"PRAGMA busy_timeout = {CHECKPOINT_WAIT_MS}")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        db.execute("PRAGMA busy_timeout = 10000")

    def _file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    # ── Reads ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._opened():
            db = self._connect()
            try:
                # One aggregate per query: SQLite answers a lone MIN or MAX of
                # the key from the index, but MIN and MAX together scan the table.
                samples = [db.execute(query).fetchone()[0] for query in _SAMPLE_STATS]
                minutes = [db.execute(query).fetchone()[0] for query in _MINUTE_STATS]
                notes = dict(db.execute("SELECT key, value FROM meta WHERE key IN ('size_capped_ms', 'moved_aside')"))
            finally:
                db.close()
        return {
            "bytes": self._file_bytes(),
            "max_bytes": self.max_bytes,
            "samples": samples[0],
            "oldest_sample_ms": samples[1],
            "newest_sample_ms": samples[2],
            "minutes": minutes[0],
            "oldest_minute_ms": minutes[1],
            "newest_minute_ms": minutes[2],
            "raw_retention_days": self.raw_retention_ms / 86_400_000,
            "rollup_retention_days": self.rollup_retention_ms / 86_400_000,
            # When the size cap last removed data, and the unreadable database
            # this one replaced, if it did: both explain missing history.
            "size_capped_ms": int(notes["size_capped_ms"]) if "size_capped_ms" in notes else None,
            "moved_aside": json.loads(notes["moved_aside"]) if "moved_aside" in notes else None,
            # Unreadable databases are kept, never deleted: what they take is
            # the operator's to reclaim.
            "set_aside": self._set_aside(),
        }

    def sessions(self, limit: int = 20) -> list[dict]:
        with self._opened():
            db = self._connect()
            try:
                rows = db.execute(
                    "SELECT started_ms, cycle_count, gain_lsb_per_ut, rate_hz, mode, bus, address FROM sessions ORDER BY started_ms DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                db.close()
        keys = ("started_ms", "cycle_count", "gain_lsb_per_ut", "rate_hz", "mode", "bus", "address")
        return [dict(zip(keys, row)) for row in rows]

    def raw_rows(self, start_ms: int, end_ms: int, limit: int) -> list[tuple[int, float, float, float]] | None:
        """Raw (t, x, y, z) rows in a range, or None if there are more than
        ``limit`` of them (the caller should use ``series`` instead)."""
        with self._opened():
            db = self._connect()
            try:
                count = db.execute(
                    "SELECT COUNT(*) FROM samples WHERE t_ms >= ? AND t_ms < ?", (start_ms, end_ms)
                ).fetchone()[0]
                if count > limit:
                    return None
                return db.execute(
                    "SELECT t_ms, x, y, z FROM samples WHERE t_ms >= ? AND t_ms < ? ORDER BY t_ms", (start_ms, end_ms)
                ).fetchall()
            finally:
                db.close()

    def series(self, start_ms: int, end_ms: int, max_points: int = 1500) -> dict:
        """What a chart of [start, end] needs, in at most ~max_points points.

        Raw samples when they fit; otherwise buckets of equal width with the
        min, mean and max of each axis and of |B|, computed from the raw
        samples when buckets are shorter than a minute and from the minute
        summaries when they are longer. Min and max travel with every bucket
        because a spike averaged into a long bucket would otherwise vanish.
        Buckets sit on a grid of their own width (see ``series.py``).
        """
        max_points = shape.clamp_points(max_points)
        bucket = shape.bucket_width(start_ms, end_ms, max_points)
        with self._opened():
            return self._series(start_ms, end_ms, max_points, bucket)

    def _series(self, start_ms: int, end_ms: int, max_points: int, bucket: int) -> dict:
        db = self._connect()
        try:
            if bucket >= MINUTE_MS:
                bucket = math.ceil(bucket / MINUTE_MS) * MINUTE_MS
                rows = db.execute(
                    """
                    SELECT t_ms / ? AS k, MIN(t_ms), SUM(n),
                           MIN(x_min), SUM(x_mean * n) / SUM(n), MAX(x_max),
                           MIN(y_min), SUM(y_mean * n) / SUM(n), MAX(y_max),
                           MIN(z_min), SUM(z_mean * n) / SUM(n), MAX(z_max),
                           MIN(b_min), SUM(b_mean * n) / SUM(n), MAX(b_max)
                    FROM minutes WHERE t_ms >= ? AND t_ms < ?
                    GROUP BY k ORDER BY k
                    """,
                    (bucket, start_ms, end_ms),
                ).fetchall()
                return shape.bucketed_points(rows, bucket, "minutes")
            count = db.execute(
                "SELECT COUNT(*) FROM samples WHERE t_ms >= ? AND t_ms < ?", (start_ms, end_ms)
            ).fetchone()[0]
            if count <= max_points:
                raw = db.execute(
                    "SELECT t_ms, x, y, z FROM samples WHERE t_ms >= ? AND t_ms < ? ORDER BY t_ms", (start_ms, end_ms)
                ).fetchall()
                return shape.raw_points(raw, "samples")
            rows = db.execute(
                """
                SELECT t_ms / ? AS k, MIN(t_ms), COUNT(*),
                       MIN(x), AVG(x), MAX(x),
                       MIN(y), AVG(y), MAX(y),
                       MIN(z), AVG(z), MAX(z),
                       MIN(magnitude(x, y, z)), AVG(magnitude(x, y, z)), MAX(magnitude(x, y, z))
                FROM samples WHERE t_ms >= ? AND t_ms < ?
                GROUP BY k ORDER BY k
                """,
                (bucket, start_ms, end_ms),
            ).fetchall()
            return shape.bucketed_points(rows, bucket, "samples")
        finally:
            db.close()


def _summarise(db: sqlite3.Connection, start: int, end: int) -> int:
    cursor = db.execute(
        """
        INSERT OR REPLACE INTO minutes
        SELECT (t_ms / 60000) * 60000, COUNT(*),
               MIN(x), AVG(x), MAX(x),
               MIN(y), AVG(y), MAX(y),
               MIN(z), AVG(z), MAX(z),
               MIN(b), AVG(b), MAX(b)
        FROM (SELECT t_ms, x, y, z, magnitude(x, y, z) AS b FROM samples WHERE t_ms >= ? AND t_ms < ?)
        GROUP BY t_ms / 60000
        """,
        (start, end),
    )
    return cursor.rowcount


def _watermark(db: sqlite3.Connection) -> int | None:
    """Where the minute summaries end: the samples before it are summarised."""
    row = db.execute("SELECT value FROM meta WHERE key = 'rollup_watermark'").fetchone()
    return None if row is None else int(row[0])


def _gone(exc: sqlite3.Error) -> bool:
    """Whether an error means the database file is not the one opened: it was
    deleted (SQLite then makes an empty one), replaced, or cannot be opened."""
    code = getattr(exc, "sqlite_errorcode", 0)
    return (
        (code & 0xFF) in (sqlite3.SQLITE_CANTOPEN, *_UNREADABLE)
        or code == sqlite3.SQLITE_READONLY_DBMOVED
        or str(exc).startswith("no such table")
    )


def _header_version(path: Path) -> int | None:
    """The user_version in a database file's header, or None if the file does
    not start with an SQLite header (or cannot be read)."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(100)
    except OSError:
        return None
    if len(header) < 100 or not header.startswith(b"SQLite format 3\x00"):
        return None
    return int.from_bytes(header[60:64], "big", signed=True)


def _newer(version: int) -> str:
    return (
        f"it was written by a newer version of this app (schema {version}; this one knows {SCHEMA_VERSION}), "
        "so it is left as it is: run the newer version, or move the file aside to start a new database"
    )


def _session_row(session: dict) -> tuple:
    s = session
    return (s["started_ms"], s["cycle_count"], s["gain"], s["rate_hz"], s["mode"], s["bus"], s["address"])


def _data_bytes(db: sqlite3.Connection) -> int:
    """The bytes the data occupy: the database's pages less its free ones.
    Unlike the file's size, this falls as soon as rows are deleted, whether or
    not the WAL can be checkpointed yet."""
    pages, free, size = (
        db.execute(f"PRAGMA {name}").fetchone()[0] for name in ("page_count", "freelist_count", "page_size")
    )
    return (pages - free) * size


def _step(excess_bytes: int, row_bytes: int) -> int:
    return max(CAP_STEP_MIN_ROWS, min(CAP_STEP_MAX_ROWS, math.ceil(excess_bytes / row_bytes)))


def _delete_oldest(db: sqlite3.Connection, table: str, rows: int, before: int | None = None) -> int:
    """Delete a table's oldest ``rows`` rows (of those before ``before``, if
    given), or all of them if there are fewer. Returns how many went."""
    where, args = ("", ()) if before is None else ("WHERE t_ms < ?", (before,))
    cut = db.execute(f"SELECT t_ms FROM {table} {where} ORDER BY t_ms LIMIT 1 OFFSET ?", (*args, rows)).fetchone()  # noqa: S608 - fixed table names
    if cut is not None:
        where, args = "WHERE t_ms < ?", (cut[0],)
    return db.execute(f"DELETE FROM {table} {where}", args).rowcount  # noqa: S608
