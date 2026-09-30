"""The recorder's schedule: flushing, rolling up, pruning, status, failures."""

import json
import sqlite3

from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health
from retina_magnetometer.recorder import PRUNE_EVERY_S, ROLLUP_EVERY_S, Recorder
from retina_magnetometer.storage import Storage

T0 = 1_790_726_400.0


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now


def rig(tmp_path, **env):
    values = {"MAGNETOMETER_DATA_DIR": str(tmp_path), "MAGNETOMETER_FLUSH_INTERVAL_S": "5"}
    values.update({f"MAGNETOMETER_{k}": str(v) for k, v in env.items()})
    config = from_env(values)
    clock = Clock()
    health = Health(config, clock=clock)
    storage = Storage(config.db_path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=clock)
    return Recorder(storage, health, config, clock=clock), storage, health, clock


def test_flushes_on_its_interval_not_per_sample(tmp_path):
    recorder, storage, _, clock = rig(tmp_path)
    recorder.tick()  # first tick flushes (nothing) and starts the clocks
    recorder.add(int(T0 * 1000), 1.0, 2.0, 3.0)
    clock.now += 2
    recorder.tick()
    assert storage.stats()["samples"] == 0  # still buffered
    clock.now += 4
    recorder.tick()
    assert storage.stats()["samples"] == 1


def test_recent_buffer_serves_the_live_edge(tmp_path):
    recorder, *_ = rig(tmp_path)
    for i in range(10):
        recorder.add(int(T0 * 1000) + i * 1000, float(i), 0.0, 0.0)
    assert recorder.recent_coverage_ms() == int(T0 * 1000)
    assert [r[1] for r in recorder.recent(int(T0 * 1000) + 7000)] == [7.0, 8.0, 9.0]


def test_rollup_and_prune_run_on_their_own_cadence(tmp_path):
    recorder, storage, health, clock = rig(tmp_path)
    for i in range(180):
        recorder.add(int((T0 - 300 + i) * 1000), 1.0, 1.0, 1.0)
    recorder.tick()
    assert storage.stats()["minutes"] >= 2  # rolled up on the first tick
    clock.now += ROLLUP_EVERY_S + 1
    recorder.tick()
    clock.now += PRUNE_EVERY_S + 1
    recorder.tick()
    assert health.storage["samples"] == 180  # stats refreshed after the prune


def test_status_file_follows_health(tmp_path):
    recorder, _, health, clock = rig(tmp_path)
    health.sample(T0, 1.0, 2.0, 2.0)
    recorder.tick()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "ok" and status["last_sample"]["b"] == 3.0


def test_a_failed_write_keeps_the_samples_and_says_so(tmp_path, monkeypatch):
    recorder, storage, health, clock = rig(tmp_path)
    recorder.add(int(T0 * 1000), 1.0, 1.0, 1.0)

    def broken(rows):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(storage, "write_samples", broken)
    recorder.flush()
    assert "disk I/O error" in health.snapshot()["storage_error"]
    monkeypatch.undo()
    recorder.flush()
    assert storage.stats()["samples"] == 1
    assert health.snapshot()["storage_error"] is None


def test_stop_flushes_everything(tmp_path):
    recorder, storage, _, _ = rig(tmp_path)
    recorder.start()
    recorder.add(int(T0 * 1000), 1.0, 1.0, 1.0)
    recorder.stop()
    assert storage.stats()["samples"] == 1
    assert (tmp_path / "status.json").exists()
