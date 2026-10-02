"""Between the sampler and the disk: a recent-samples buffer and housekeeping.

Every sample lands in two places. An in-memory ring buffer holds the last
hour (bounded in count), which the live chart and the orientation estimate
read without touching the card. A pending list is flushed to SQLite in one
transaction every ``flush_interval_s``, together with any session records, so
the sampler never waits on the disk. The same background thread also rolls
up complete minutes, applies retention, refreshes storage statistics and
rewrites the status file.

The schedule runs on the monotonic clock. Samples carry wall-clock time, and
a wall clock that steps back (an NTP correction, an RTC-less board setting
its time) would otherwise hold every flush, roll-up and status write until it
caught up again.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections import deque

from retina_magnetometer.config import Config
from retina_magnetometer.health import Health, write_status_file
from retina_magnetometer.storage import Storage, StorageUnavailable

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
# The status file's name among the storage operations that can fail.
STATUS = "status.json"
# A task that failed is tried again sooner than its cadence: after 5 s, then
# 10, 20, 40, and every minute after that (never later than its cadence would
# run it). A prune that meets a busy database is not reported for the ten
# minutes until the next one.
RETRY_FIRST_S = 5.0
RETRY_MAX_S = 60.0


class Recorder:
    def __init__(self, storage: Storage, health: Health, config: Config, *, clock=time.time, monotonic=time.monotonic):
        self.storage = storage
        self.health = health
        self.config = config
        self._clock = clock  # wall time, for the session records
        self._monotonic = monotonic  # the schedule
        self._lock = threading.Lock()
        size = min(int(config.sample_rate_hz * RECENT_SECONDS) + 16, RECENT_MAX_SAMPLES)
        self._recent: deque = deque(maxlen=size)
        self._pending: list[tuple[int, float, float, float]] = []
        self._pending_from: int | None = None  # the oldest pending sample's time
        self._sessions: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Never run, so the first tick runs everything.
        self._last = dict.fromkeys(("flush", "rollup", "prune", "stats", "status"), -math.inf)
        # Tasks whose last run failed: when to try again, and the wait after
        # that one.
        self._retry: dict[str, tuple[float, float]] = {}
        # Each storage operation failing now, and how. The storage problem on
        # the page is all of them together, and it clears only when the last
        # one works again: a roll-up that keeps failing is not cleared by every
        # flush that gets through, and a problem that is over does not wait
        # for a flush with rows in it (a node with no sensor has none). Each
        # is logged when it starts or changes, not every few seconds.
        self._failing: dict[str, str] = {}
        # Logged once each, but not storage problems of their own: samples
        # being dropped (the failing write says why), and housekeeping bugs.
        self._noted: dict[str, str] = {}

    # ── Intake (sampler thread) ──────────────────────────────────────────────

    def add(self, t_ms: int, x: float, y: float, z: float) -> None:
        row = (t_ms, x, y, z)
        with self._lock:
            newest = self._recent[-1][0] if self._recent else None
            # A time at or before the newest one means the wall clock stepped
            # back (or two reads fell in one millisecond). The buffer drops its
            # tail from that time on, so it stays in time order; those samples
            # are still pending, and nothing unwritten is lost.
            while self._recent and self._recent[-1][0] >= t_ms:
                self._recent.pop()
            self._recent.append(row)
            self._pending.append(row)
            if self._pending_from is None or t_ms < self._pending_from:
                self._pending_from = t_ms
        if newest is not None and t_ms < newest:
            log.warning(
                "the clock went back %.3f s; the live buffer dropped its samples from after the new time",
                (newest - t_ms) / 1000,
            )

    def start_session(
        self, *, cycle_count: int, gain: float, rate_hz: float, mode: str, bus: str, address: int
    ) -> None:
        """Record that sampling (re)started, with what is in force. Written
        with the next flush, stamped now. The arguments are spelt out so that
        a caller passing the wrong ones fails at once, in its own thread,
        rather than poisoning a flush here."""
        session = {
            "started_ms": int(self._clock() * 1000),
            "cycle_count": cycle_count,
            "gain": gain,
            "rate_hz": rate_hz,
            "mode": mode,
            "bus": bus,
            "address": address,
        }
        with self._lock:
            self._sessions.append(session)

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

    def pending_from_ms(self) -> int | None:
        """The oldest sample not yet on disk, or None: minutes from it on are
        not complete in the database yet."""
        with self._lock:
            return self._pending_from

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
            try:
                self.tick()
            except Exception as exc:
                # Each task already fails on its own; this is the last line.
                # Nothing may end this thread: without it nothing is written
                # and status.json goes stale while the page looks alive.
                problem = f"{type(exc).__name__}: {exc}"
                if self._noted.get("unexpected") != problem:
                    self._noted["unexpected"] = problem
                    log.exception("housekeeping failed: %s", problem)
                    self.health.internal_error(f"housekeeping: {problem}")

    def tick(self) -> None:
        now = self._monotonic()
        self._task("flush", self.config.flush_interval_s, now, self.flush)
        # Only as far as what is on disk: with a long flush interval the
        # samples of a minute that ended a while ago may still be pending.
        self._task("rollup", ROLLUP_EVERY_S, now, self._rollup)
        if self._task("prune", PRUNE_EVERY_S, now, self._prune):
            self._last["stats"] = -math.inf  # what the prune removed shows at once
        self._task("stats", STATS_EVERY_S, now, self._refresh_stats)
        self._task("status", STATUS_EVERY_S, now, self._write_status)

    def _task(self, name: str, cadence: float, now: float, run) -> bool:
        """Run a housekeeping task if it is due: on its cadence, or sooner
        after a failure, on a backoff of its own. Whether it ran."""
        retry = self._retry.get(name)
        if now - self._last[name] < cadence and (retry is None or now < retry[0]):
            return False
        self._last[name] = now
        if run():
            self._retry.pop(name, None)
        else:
            wait = min(cadence, RETRY_MAX_S, retry[1] * 2 if retry else RETRY_FIRST_S)
            self._retry[name] = (now + wait, wait)
        return True

    def _rollup(self) -> bool:
        return self._guarded("rollup", self.storage.rollup, pending_from_ms=self.pending_from_ms())[0]

    def _prune(self) -> bool:
        ok, removed = self._guarded("prune", self.storage.prune)
        if removed and removed.get("size_capped"):
            log.warning("database reached its size cap; oldest data removed: %s", removed)
        # A cap that a reader made wait is tried again soon, as a failure is.
        return ok and not removed.get("deferred")

    def flush(self) -> bool:
        """Write what is pending: the session records, then the samples, each
        an operation of its own, so that neither holds the other up. Whatever
        fails is kept for the next attempt. Whether it all got through."""
        with self._lock:
            rows, self._pending = self._pending, []
            sessions, self._sessions = self._sessions, []
            self._pending_from = None
        ok = True
        if sessions:
            written, _ = self._guarded("session", self.storage.write_samples, [], sessions=sessions)
            if not written:
                with self._lock:
                    self._sessions = sessions + self._sessions
                ok = False
        if rows:
            written, _ = self._guarded("write", self.storage.write_samples, rows)
            if written:
                self._noted.pop("dropped", None)
            else:
                # The buffer is bounded so a dead disk cannot grow memory
                # without limit.
                with self._lock:
                    kept = rows + self._pending
                    dropped = max(0, len(kept) - RECENT_MAX_SAMPLES)
                    self._pending = kept[dropped:]
                    self._pending_from = min((r[0] for r in self._pending), default=None)
                if dropped and "dropped" not in self._noted:
                    self._noted["dropped"] = "dropping"
                    log.error(
                        "%d samples wait to be written, the most kept: the oldest are being dropped",
                        RECENT_MAX_SAMPLES,
                    )
                ok = False
        return ok

    def _guarded(self, what: str, fn, *args, **kwargs) -> tuple[bool, object]:
        """(Whether it worked, what it returned.) Each operation fails on its
        own: one that keeps failing must not hold up the ones after it in a
        tick, the status file above all."""
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            self._failed(what, exc)
            return False, None
        self._succeeded(what)
        return True, result

    def _failed(self, what: str, exc: Exception) -> None:
        # A database that cannot be opened is one problem, whichever call
        # met it, and its message already says what it is. Anything but a
        # storage error is a bug, logged with where it happened.
        expected = isinstance(exc, (sqlite3.Error, OSError))
        detail = str(exc) if expected else f"{type(exc).__name__}: {exc}"
        key, problem = ("open", detail) if isinstance(exc, StorageUnavailable) else (what, f"{what} failed: {detail}")
        if self._failing.get(key) != problem:
            log.error("%s", problem, exc_info=not expected)
        self._failing[key] = problem
        self._report()

    def _succeeded(self, what: str) -> None:
        # Any operation on the database shows it opens; writing status.json
        # does not.
        keys = (what,) if what == STATUS else (what, "open")
        cleared = [key for key in keys if self._failing.pop(key, None) is not None]
        for key in cleared:
            log.info("storage: %s works again", "the database" if key == "open" else key)
        if cleared:
            self._report()

    def _report(self, stats: dict | None = None) -> None:
        self.health.storage_status(stats, dict(self._failing))

    def _refresh_stats(self) -> bool:
        ok, stats = self._guarded("stats", self.storage.stats)
        if ok:
            self._report(stats)
        return ok

    def _write_status(self) -> bool:
        recovering = STATUS in self._failing
        try:
            write_status_file(self.config.status_path, self.health.snapshot())
        except Exception as exc:
            self._failed(STATUS, exc)
            return False
        self._succeeded(STATUS)
        if recovering:
            # That one was written while its own failure still stood; this one
            # says it is over.
            return self._write_status()
        return True
