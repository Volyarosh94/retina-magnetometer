"""Between the sampler and the disk: a recent-samples buffer and housekeeping.

Every sample lands in two places. An in-memory ring buffer holds the last
hour (bounded in count), which the live chart and the orientation estimate
read without touching the card. A pending list is flushed to SQLite in one
transaction every ``flush_interval_s``. The same background thread also rolls
up complete minutes, applies retention, refreshes storage statistics and
rewrites the status file.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections import deque

from retina_magnetometer.config import Config
from retina_magnetometer.health import Health, write_status_file
from retina_magnetometer.storage import Storage

log = logging.getLogger(__name__)

RECENT_SECONDS = 3600
RECENT_MAX_SAMPLES = 200_000
ROLLUP_EVERY_S = 30.0
PRUNE_EVERY_S = 600.0
STATUS_EVERY_S = 5.0
# The storage figures on the page. Counted from the database rather than kept
# in step with this process's writes, so a second writer shows up too (the
# simulator's backfill, run beside the app). A count scans the table, a few
# tens of milliseconds at a week of 1 Hz, so once a minute, not every status.
STATS_EVERY_S = 60.0


class Recorder:
    def __init__(self, storage: Storage, health: Health, config: Config, *, clock=time.time):
        self.storage = storage
        self.health = health
        self.config = config
        self._clock = clock
        self._lock = threading.Lock()
        size = min(int(config.sample_rate_hz * RECENT_SECONDS) + 16, RECENT_MAX_SAMPLES)
        self._recent: deque = deque(maxlen=size)
        self._pending: list[tuple[int, float, float, float]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = {"flush": 0.0, "rollup": 0.0, "prune": 0.0, "stats": 0.0, "status": 0.0}

    # ── Intake (sampler thread) ──────────────────────────────────────────────

    def add(self, t_ms: int, x: float, y: float, z: float) -> None:
        row = (t_ms, x, y, z)
        with self._lock:
            self._recent.append(row)
            self._pending.append(row)

    def recent(self, since_ms: int | None = None) -> list[tuple[int, float, float, float]]:
        with self._lock:
            rows = list(self._recent)
        if since_ms is None:
            return rows
        return [r for r in rows if r[0] >= since_ms]

    def recent_coverage_ms(self) -> int | None:
        """How far back the ring buffer reaches, or None when empty."""
        with self._lock:
            return self._recent[0][0] if self._recent else None

    # ── Housekeeping (its own thread) ────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.flush()
        self._write_status()

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            self.tick()

    def tick(self) -> None:
        now = self._clock()
        if now - self._last["flush"] >= self.config.flush_interval_s:
            self._last["flush"] = now
            self.flush()
        if now - self._last["rollup"] >= ROLLUP_EVERY_S:
            self._last["rollup"] = now
            self._guarded("rollup", self.storage.rollup)
        if now - self._last["prune"] >= PRUNE_EVERY_S:
            self._last["prune"] = now
            removed = self._guarded("prune", self.storage.prune)
            if removed and removed.get("size_capped"):
                log.warning("database reached its size cap; oldest data removed: %s", removed)
            self._last["stats"] = 0.0  # what the prune removed shows at once
        if now - self._last["stats"] >= STATS_EVERY_S:
            self._last["stats"] = now
            self._refresh_stats()
        if now - self._last["status"] >= STATUS_EVERY_S:
            self._last["status"] = now
            self._write_status()

    def flush(self) -> None:
        with self._lock:
            rows, self._pending = self._pending, []
        if not rows:
            return
        try:
            self.storage.write_samples(rows)
        except (sqlite3.Error, OSError) as exc:
            # Keep them for the next attempt rather than drop them; the buffer
            # is bounded so a dead disk cannot grow memory without limit.
            with self._lock:
                self._pending = (rows + self._pending)[-RECENT_MAX_SAMPLES:]
            self.health.storage_update(None, f"write failed: {exc}")
            log.error("could not write %d samples: %s", len(rows), exc)
        else:
            if self.health.storage_error:
                self.health.storage_update(None, None)

    def _guarded(self, what: str, fn):
        try:
            return fn()
        except (sqlite3.Error, OSError) as exc:
            self.health.storage_update(None, f"{what} failed: {exc}")
            log.error("%s failed: %s", what, exc)
            return None

    def _refresh_stats(self) -> None:
        stats = self._guarded("stats", self.storage.stats)
        if stats is not None:
            self.health.storage_update(stats, self.health.storage_error)

    def _write_status(self) -> None:
        try:
            write_status_file(self.config.status_path, self.health.snapshot())
        except OSError as exc:
            log.error("could not write %s: %s", self.config.status_path, exc)
