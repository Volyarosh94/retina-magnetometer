"""What the app knows about its own health, and the status file it publishes.

One ``Health`` object is written by the sampler and the recorder and read by
the web threads. Its snapshot is served at ``/api/health``, drawn on the
status card, and written every few seconds to ``status.json`` in the data
directory, the contract retina-telemetry established for node services: an
integer ``schema``, ``written_at`` in UTC, a ``state``, a human ``detail``,
and ``errors``. A file, not a Docker HEALTHCHECK, because a container health
check that is red (no sensor fitted, I2C not yet enabled) holds up the Mender
install of the whole node, and a missing sensor is a fact to report, not a
reason to fail the deployment.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from retina_magnetometer.config import Config

STATUS_SCHEMA = 1

STATES = {
    "starting": "Starting up",
    "ok": "Sampling",
    "degraded": "Sampling with errors",
    "stalled": "No recent samples",
    "no_bus": "I2C bus not available",
    "no_sensor": "No RM3100 found on the bus",
    "config_error": "Configuration error",
}

# Errors kept for the status page. Enough to see a pattern, bounded so a
# flapping bus cannot grow memory.
_RECENT_ERRORS = 20

# The span the measured rate, and the ticks poll mode missed, are counted over.
_RATE_WINDOW_S = 60.0

# Poll mode keeping fewer of its ticks than this over that span is degraded,
# provided it missed at least _MISSED_AT_LEAST of them: one late tick at a slow
# rate is a busy moment of the host, not a rate it cannot keep.
_KEEP_UP = 0.9
_MISSED_AT_LEAST = 3

# A storage operation that fails again within this long of its last report,
# the same way, is the same problem and is not listed again: one that keeps
# failing and working in turn (a prune during a backfill) is listed once.
_STORAGE_RELIST_S = 900.0


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Health:
    def __init__(self, config: Config, *, clock=time.time, monotonic=time.monotonic):
        # Wall time for what is shown (timestamps, uptime), monotonic time for
        # how long things have taken: a clock stepped by NTP at boot must not
        # read as a sampler that has been silent for hours.
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self.config = config
        self.started_at = clock()
        self._started = monotonic()
        self.state = "config_error" if config.errors else "starting"
        self.detail = "; ".join(config.errors) if config.errors else "Looking for the sensor"
        self.config_warnings: list[str] = list(config.warnings)
        self.bus: str | None = None
        self.sensor: dict | None = None
        self.self_test: dict | None = None
        self.effective_rate_hz: float | None = None
        self.last_sample_at: float | None = None
        self.last_sample: dict | None = None
        self.samples_total = 0
        self.errors_total = 0
        self.consecutive_errors = 0
        self.internal_errors_total = 0
        self.reinitialisations = 0
        self.recent_errors: deque = deque(maxlen=_RECENT_ERRORS)
        self._sample_times: deque = deque()
        # Poll mode: (time, ticks, how long the measurement took) for each run
        # of ticks a measurement overran.
        self._missed_ticks: deque = deque()
        # Since when a sample has been due and none has come (monotonic), and
        # how to say so; None while none is due: no sensor, or still looking.
        # Finding the sensor again does not restart it, so a sensor that is
        # found over and over but never delivers still shows up as stalled.
        self._quiet_since: float | None = None
        self._quiet_says = ""
        self.storage: dict | None = None
        self.storage_error: str | None = None
        # (operation, message) -> when last reported (monotonic)
        self._storage_reported: dict[tuple[str, str], float] = {}

    # ── Updates ──────────────────────────────────────────────────────────────

    def _record_error(self, kind: str, message: str) -> None:
        self.errors_total += 1
        self.consecutive_errors += 1
        self.recent_errors.append({"at": _iso(self._clock()), "kind": kind, "message": message})

    def _trim(self, t: float) -> None:
        while self._sample_times and self._sample_times[0] < t - _RATE_WINDOW_S:
            self._sample_times.popleft()
        while self._missed_ticks and self._missed_ticks[0][0] < t - _RATE_WINDOW_S:
            self._missed_ticks.popleft()

    def no_bus(self, message: str) -> None:
        with self._lock:
            self.state, self.detail = "no_bus", message
            self.bus = None
            self.sensor = None
            self._quiet_since = None

    def no_sensor(self, bus: str, message: str) -> None:
        with self._lock:
            self.state, self.detail = "no_sensor", message
            self.bus = bus
            self.sensor = None
            self._quiet_since = None

    def sensor_ready(
        self, *, bus: str, address: int, revid: int, cycle_count: int, gain: float, effective_rate_hz: float
    ) -> None:
        with self._lock:
            if self.sensor is not None or self.samples_total:
                self.reinitialisations += 1
            self.bus = bus
            self.sensor = {
                "address": f"0x{address:02X}",
                "revid": f"0x{revid:02X}",
                "cycle_count": cycle_count,
                "gain_lsb_per_ut": round(gain, 3),
            }
            self.effective_rate_hz = effective_rate_hz
            self.consecutive_errors = 0
            self.state, self.detail = "starting", "Sensor found; waiting for the first sample"
            if self._quiet_since is None:
                self._quiet_since = self._monotonic()
                self._quiet_says = "Sensor found {:.0f} s ago; no sample since"

    def self_test_result(self, *, passed: bool, ran: bool, x_ok: bool, y_ok: bool, z_ok: bool, raw: int) -> None:
        with self._lock:
            self.self_test = {
                "at": _iso(self._clock()),
                "passed": passed,
                "ran": ran,
                "x_ok": x_ok,
                "y_ok": y_ok,
                "z_ok": z_ok,
                "raw": f"0x{raw:02X}",
            }

    def sample(self, t: float, x: float, y: float, z: float) -> None:
        with self._lock:
            self.samples_total += 1
            self.consecutive_errors = 0
            self.last_sample_at = t
            self.last_sample = {"x": x, "y": y, "z": z, "b": (x * x + y * y + z * z) ** 0.5}
            self._sample_times.append(t)
            self._trim(t)
            self._quiet_since = self._monotonic()
            self._quiet_says = "No sample for {:.0f} s"
            self.state = "ok"
            self.detail = "Sampling normally"
            if self.self_test is not None and not self.self_test["passed"]:
                self.state = "degraded"
                failed = [a for a in ("x", "y", "z") if not self.self_test[f"{a}_ok"]]
                self.detail = f"Self test failed on {', '.join(failed).upper() or 'the sensor'}; readings are suspect"

    def error(self, kind: str, message: str) -> None:
        """A failed read or a sensor that answered wrongly."""
        with self._lock:
            self._record_error(kind, message)
            # "stalled" is never stored: snapshot() works it out from the time.
            if self.state in ("ok", "starting", "degraded"):
                self.state = "degraded"
                self.detail = f"{kind}: {message}"

    def internal_error(self, message: str) -> None:
        """Something failed that is not the sensor or the bus: a bug, most
        likely. Listed and counted, but not as a read error."""
        with self._lock:
            self.internal_errors_total += 1
            self.recent_errors.append({"at": _iso(self._clock()), "kind": "internal error", "message": message})
            if self.state in ("ok", "starting", "degraded"):
                self.state = "degraded"
                self.detail = f"internal error: {message}"

    def config_warning(self, message: str) -> None:
        """A setting that could not be used as given and has been replaced by a
        safe one, found after start-up (the listen address, when binding)."""
        with self._lock:
            if message not in self.config_warnings:
                self.config_warnings.append(message)

    def ticks_missed(self, t: float, ticks: int, measurement_s: float) -> None:
        """Poll mode: ``ticks`` went by while the sampler was still busy with
        the last sample, whose measurement took ``measurement_s``."""
        with self._lock:
            self._missed_ticks.append((t, ticks, measurement_s))
            self._trim(t)

    def storage_status(self, stats: dict | None, failing: Mapping[str, str]) -> None:
        """The storage figures, if there are new ones, and the storage
        operations failing now, by name, with how each fails.

        ``storage_error`` and the detail are all of them together. The recent
        errors list each operation when it starts failing, or fails another
        way, and not again while it goes on failing or comes back the same way
        within _STORAGE_RELIST_S of its last report: operations failing and
        recovering in turn are not each a new problem, and listing them as
        such would push the read errors out of the list. A storage error is
        not a read error, so the read counts stay put.
        """
        with self._lock:
            if stats is not None:
                self.storage = stats
            now = self._monotonic()
            for listed, reported in list(self._storage_reported.items()):
                if now - reported > _STORAGE_RELIST_S:
                    del self._storage_reported[listed]
            for operation, message in failing.items():
                if (operation, message) not in self._storage_reported:
                    self.recent_errors.append({"at": _iso(self._clock()), "kind": "storage", "message": message})
                self._storage_reported[(operation, message)] = now
            self.storage_error = "; ".join(failing.values()) or None

    # ── Reads ────────────────────────────────────────────────────────────────

    def _effective_rate(self) -> float | None:
        """What the configuration delivers: the chip's rate in continuous mode,
        and in poll mode the rate it runs at less the ticks it missed."""
        missed = sum(ticks for _, ticks, _ in self._missed_ticks)
        if self.config.mode != "poll" or not missed or self.effective_rate_hz is None:
            return self.effective_rate_hz
        kept = len(self._sample_times)
        return self.effective_rate_hz * kept / (kept + missed)

    def _falling_behind(self, effective_rate: float | None) -> str | None:
        """Poll mode missing too many of its ticks: what to say about it."""
        if self.config.mode != "poll" or effective_rate is None:
            return None
        rate = self.config.sample_rate_hz
        missed = sum(ticks for _, ticks, _ in self._missed_ticks)
        if missed < _MISSED_AT_LEAST or effective_rate >= _KEEP_UP * rate:
            return None
        said = f"Sampling at {effective_rate:.3g} Hz instead of {rate:.3g} Hz"
        period = 1.0 / rate
        measurement = self._missed_ticks[-1][2]
        # The measurement is to blame only when it is longer than the period
        # by itself; otherwise the sampler was held up by something else.
        if measurement >= period:
            return (
                f"{said}: a measurement takes {measurement * 1000:.1f} ms of the {period * 1000:.1f} ms period; "
                "lower the rate or the cycle count, or use continuous mode"
            )
        return f"{said}: {missed} ticks missed in the last minute while the sampler was held up"

    def _state(self, now: float, effective_rate: float | None) -> tuple[str, str]:
        """The state and detail as of ``now`` (monotonic), which the last
        update may not be."""
        state, detail = self.state, self.detail
        quiet = max(5.0 / self.config.sample_rate_hz, 10.0)
        if state in ("ok", "degraded", "starting"):
            if self._quiet_since is not None:
                if now - self._quiet_since > quiet:
                    return "stalled", self._quiet_says.format(now - self._quiet_since)
            elif state == "starting" and now - self._started > quiet:
                # Every acquisition attempt ends in a state of its own within
                # seconds, so a sampler still looking has stopped reporting.
                return "stalled", f"Still looking for the sensor after {now - self._started:.0f} s"
        if state not in ("ok", "degraded"):
            return state, detail
        problems = [] if state == "ok" else [detail]
        problems.extend(self.config_warnings)
        behind = self._falling_behind(effective_rate)
        if behind is not None:
            problems.append(behind)
        if self.storage_error is not None:
            problems.append(f"Storage: {self.storage_error}")
        if not problems:
            return state, detail
        return "degraded", ". ".join(problems)

    def snapshot(self) -> dict:
        now = self._clock()
        now_monotonic = self._monotonic()
        with self._lock:
            measured_rate = None
            if len(self._sample_times) >= 2:
                span = self._sample_times[-1] - self._sample_times[0]
                if span > 0:
                    measured_rate = (len(self._sample_times) - 1) / span
            effective_rate = self._effective_rate()
            state, detail = self._state(now_monotonic, effective_rate)
            return {
                "schema": STATUS_SCHEMA,
                "written_at": _iso(now),
                "state": state,
                "state_text": STATES.get(state, state),
                "detail": detail,
                "errors": [e["message"] for e in list(self.recent_errors)[-5:]],
                "config_errors": list(self.config.errors),
                "config_warnings": list(self.config_warnings),
                # Consequences of valid settings (the size cap holding less
                # history than the retentions ask for): shown, never a fault.
                "config_notes": list(self.config.notes),
                "started_at": _iso(self.started_at),
                "uptime_s": round(now - self.started_at, 1),
                "bus": self.bus,
                "sensor": self.sensor,
                "self_test": self.self_test,
                "mode": self.config.mode,
                "configured_rate_hz": self.config.requested_rate_hz or self.config.sample_rate_hz,
                "effective_rate_hz": effective_rate,
                "measured_rate_hz": measured_rate,
                "last_sample_at": _iso(self.last_sample_at),
                "last_sample_age_s": None if self.last_sample_at is None else round(now - self.last_sample_at, 3),
                "last_sample": self.last_sample,
                "samples_total": self.samples_total,
                "read_errors_total": self.errors_total,
                "consecutive_errors": self.consecutive_errors,
                "internal_errors_total": self.internal_errors_total,
                "reinitialisations": self.reinitialisations,
                "recent_errors": list(self.recent_errors),
                "storage": self.storage,
                "storage_error": self.storage_error,
            }


def write_status_file(path: Path, snapshot: dict) -> None:
    """Atomically replace the status file (write, rename; mode 0644).

    The rename is what readers need: they see the old document or the new
    one, never half of one. There is no fsync. The file is rewritten every
    few seconds for as long as the node runs, so a sync each time would be
    the card's most frequent one, for a document whose next version is
    seconds away; retina-telemetry writes its status the same way.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as handle:
        json.dump(snapshot, handle, indent=2)
        handle.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
