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
Bytes: if the file still exceeds ``max_db_mb`` (a fast rate with a long raw
retention on a small card), the oldest raw samples go first, then the oldest
minutes, until it is back under 90 % of the cap. Freed pages are returned to
the filesystem with incremental vacuum, and the WAL is truncated, so the cap
is a cap on disk use and not just on row counts.

Written for an SD card. Samples are buffered and written in one transaction
per flush (every few seconds), in WAL mode with synchronous=NORMAL: a power
cut can lose the last flush, never the database.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from pathlib import Path

from retina_magnetometer import series as shape

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


class Storage:
    """One database file. Safe to share between threads: every call opens its
    own short-lived connection except the writer's, which is serialised."""

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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            # auto_vacuum only takes effect before the first table exists.
            db.execute("PRAGMA auto_vacuum = INCREMENTAL")
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(_SCHEMA)
            db.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        db.execute("PRAGMA synchronous = NORMAL")
        db.execute("PRAGMA busy_timeout = 10000")
        db.create_function("magnitude", 3, shape.magnitude, deterministic=True)
        return db

    # ── Writes ───────────────────────────────────────────────────────────────

    def write_samples(self, rows: list[tuple[int, float, float, float]]) -> None:
        """Insert (t_ms, x, y, z) rows in one transaction."""
        if not rows:
            return
        with self._write_lock:
            db = self._connect()
            try:
                db.execute("BEGIN")
                db.executemany("INSERT OR REPLACE INTO samples (t_ms, x, y, z) VALUES (?, ?, ?, ?)", rows)
                db.execute("COMMIT")
            finally:
                db.close()

    def start_session(
        self, *, cycle_count: int, gain: float, rate_hz: float, mode: str, bus: str, address: int
    ) -> None:
        with self._write_lock:
            db = self._connect()
            try:
                db.execute(
                    "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(self._clock() * 1000), cycle_count, gain, rate_hz, mode, bus, address),
                )
            finally:
                db.close()

    def rollup(self, now_ms: int | None = None, *, grace_ms: int = 15_000) -> int:
        """Summarise every complete minute not yet summarised.

        A minute is complete once ``grace_ms`` has passed after it ends, which
        covers the writer's flush interval. Re-summarising is harmless (the
        minute's row is replaced), which is what makes this safe after a
        crash: the watermark only moves after the rows are written.
        """
        now_ms = int(self._clock() * 1000) if now_ms is None else now_ms
        end = ((now_ms - grace_ms) // MINUTE_MS) * MINUTE_MS
        with self._write_lock:
            db = self._connect()
            try:
                row = db.execute("SELECT value FROM meta WHERE key = 'rollup_watermark'").fetchone()
                if row is None:
                    first = db.execute("SELECT MIN(t_ms) FROM samples").fetchone()[0]
                    start = (first // MINUTE_MS) * MINUTE_MS if first is not None else end
                else:
                    start = int(row[0])
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
        with self._write_lock:
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
        with self._write_lock:
            db = self._connect()
            try:
                removed["samples"] += db.execute(
                    "DELETE FROM samples WHERE t_ms < ?", (now_ms - self.raw_retention_ms,)
                ).rowcount
                removed["minutes"] += db.execute(
                    "DELETE FROM minutes WHERE t_ms < ?", (now_ms - self.rollup_retention_ms,)
                ).rowcount
                self._reclaim(db)
                if self._file_bytes() > self.max_bytes:
                    removed["size_capped"] = True
                    target = int(self.max_bytes * 0.9)
                    for table in ("samples", "minutes"):
                        while self._file_bytes() > target:
                            oldest = db.execute(f"SELECT MIN(t_ms) FROM {table}").fetchone()[0]  # noqa: S608 - fixed table names
                            if oldest is None:
                                break
                            # An hour at a time: bounded transactions, and a
                            # cap reached by a fast rate clears in a few steps.
                            cut = oldest + 3_600_000
                            removed[table] += db.execute(f"DELETE FROM {table} WHERE t_ms < ?", (cut,)).rowcount  # noqa: S608
                            self._reclaim(db)
            finally:
                db.close()
        return removed

    def _reclaim(self, db: sqlite3.Connection) -> None:
        # Both pragmas do their work as the statement is stepped, so the rows
        # must be fetched: execute() alone steps once, and incremental_vacuum
        # frees one page per step — without fetchall it frees almost nothing.
        db.execute("PRAGMA incremental_vacuum").fetchall()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()

    def _file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    # ── Reads ────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        db = self._connect()
        try:
            samples = db.execute("SELECT COUNT(*), MIN(t_ms), MAX(t_ms) FROM samples").fetchone()
            minutes = db.execute("SELECT COUNT(*), MIN(t_ms), MAX(t_ms) FROM minutes").fetchone()
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
        }

    def sessions(self, limit: int = 20) -> list[dict]:
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
        """
        max_points = shape.clamp_points(max_points)
        span = max(1, end_ms - start_ms)
        bucket = max(1, math.ceil(span / max_points))
        db = self._connect()
        try:
            if bucket >= MINUTE_MS:
                bucket = math.ceil(bucket / MINUTE_MS) * MINUTE_MS
                rows = db.execute(
                    """
                    SELECT (t_ms - ?) / ? AS k, MIN(t_ms), SUM(n),
                           MIN(x_min), SUM(x_mean * n) / SUM(n), MAX(x_max),
                           MIN(y_min), SUM(y_mean * n) / SUM(n), MAX(y_max),
                           MIN(z_min), SUM(z_mean * n) / SUM(n), MAX(z_max),
                           MIN(b_min), SUM(b_mean * n) / SUM(n), MAX(b_max)
                    FROM minutes WHERE t_ms >= ? AND t_ms < ?
                    GROUP BY k ORDER BY k
                    """,
                    (start_ms, bucket, start_ms, end_ms),
                ).fetchall()
                return shape.bucketed_points(rows, start_ms, bucket, "minutes")
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
                SELECT (t_ms - ?) / ? AS k, MIN(t_ms), COUNT(*),
                       MIN(x), AVG(x), MAX(x),
                       MIN(y), AVG(y), MAX(y),
                       MIN(z), AVG(z), MAX(z),
                       MIN(magnitude(x, y, z)), AVG(magnitude(x, y, z)), MAX(magnitude(x, y, z))
                FROM samples WHERE t_ms >= ? AND t_ms < ?
                GROUP BY k ORDER BY k
                """,
                (start_ms, bucket, start_ms, end_ms),
            ).fetchall()
            return shape.bucketed_points(rows, start_ms, bucket, "samples")
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
