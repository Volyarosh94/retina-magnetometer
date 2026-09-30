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


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Health:
    def __init__(self, config: Config, *, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self.config = config
        self.started_at = clock()
        self.state = "config_error" if config.errors else "starting"
        self.detail = "; ".join(config.errors) if config.errors else "Looking for the sensor"
        self.bus: str | None = None
        self.sensor: dict | None = None
        self.self_test: dict | None = None
        self.effective_rate_hz: float | None = None
        self.last_sample_at: float | None = None
        self.last_sample: dict | None = None
        self.samples_total = 0
        self.errors_total = 0
        self.consecutive_errors = 0
        self.reinitialisations = 0
        self.recent_errors: deque = deque(maxlen=_RECENT_ERRORS)
        self._sample_times: deque = deque()
        self.storage: dict | None = None
        self.storage_error: str | None = None

    # ── Updates ──────────────────────────────────────────────────────────────

    def _record_error(self, kind: str, message: str) -> None:
        self.errors_total += 1
        self.consecutive_errors += 1
        self.recent_errors.append({"at": _iso(self._clock()), "kind": kind, "message": message})

    def no_bus(self, message: str) -> None:
        with self._lock:
            self.state, self.detail = "no_bus", message
            self.bus = None
            self.sensor = None

    def no_sensor(self, bus: str, message: str) -> None:
        with self._lock:
            self.state, self.detail = "no_sensor", message
            self.bus = bus
            self.sensor = None

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
            while self._sample_times and self._sample_times[0] < t - 60.0:
                self._sample_times.popleft()
            self.state = "ok"
            self.detail = "Sampling normally"
            if self.self_test is not None and not self.self_test["passed"]:
                self.state = "degraded"
                failed = [a for a in ("x", "y", "z") if not self.self_test[f"{a}_ok"]]
                self.detail = f"Self test failed on {', '.join(failed).upper() or 'the sensor'}; readings are suspect"

    def error(self, kind: str, message: str) -> None:
        with self._lock:
            self._record_error(kind, message)
            if self.state in ("ok", "starting", "degraded", "stalled"):
                self.state = "degraded"
                self.detail = f"{kind}: {message}"

    def storage_update(self, stats: dict | None, error: str | None = None) -> None:
        with self._lock:
            if stats is not None:
                self.storage = stats
            self.storage_error = error

    # ── Reads ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        now = self._clock()
        with self._lock:
            state, detail = self.state, self.detail
            period = 1.0 / self.config.sample_rate_hz
            if state in ("ok", "degraded") and self.last_sample_at is not None:
                if now - self.last_sample_at > max(5 * period, 10.0):
                    state = "stalled"
                    detail = f"No sample for {now - self.last_sample_at:.0f} s"
            measured_rate = None
            if len(self._sample_times) >= 2:
                span = self._sample_times[-1] - self._sample_times[0]
                if span > 0:
                    measured_rate = (len(self._sample_times) - 1) / span
            return {
                "schema": STATUS_SCHEMA,
                "written_at": _iso(now),
                "state": state,
                "state_text": STATES.get(state, state),
                "detail": detail,
                "errors": [e["message"] for e in list(self.recent_errors)[-5:]],
                "config_errors": list(self.config.errors),
                "started_at": _iso(self.started_at),
                "uptime_s": round(now - self.started_at, 1),
                "bus": self.bus,
                "sensor": self.sensor,
                "self_test": self.self_test,
                "mode": self.config.mode,
                "configured_rate_hz": self.config.sample_rate_hz,
                "effective_rate_hz": self.effective_rate_hz,
                "measured_rate_hz": measured_rate,
                "last_sample_at": _iso(self.last_sample_at),
                "last_sample_age_s": None if self.last_sample_at is None else round(now - self.last_sample_at, 3),
                "last_sample": self.last_sample,
                "samples_total": self.samples_total,
                "read_errors_total": self.errors_total,
                "consecutive_errors": self.consecutive_errors,
                "reinitialisations": self.reinitialisations,
                "recent_errors": list(self.recent_errors),
                "storage": self.storage,
                "storage_error": self.storage_error,
            }


def write_status_file(path: Path, snapshot: dict) -> None:
    """Atomically replace the status file (write, fsync, rename; mode 0644)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as handle:
        json.dump(snapshot, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
