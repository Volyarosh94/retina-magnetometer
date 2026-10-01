"""The recorder's schedule: flushing, rolling up, pruning, status, failures."""

import json
import logging
import math
import os
import sqlite3

import pytest

from retina_magnetometer import recorder as recorder_module
from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health
from retina_magnetometer.recorder import PRUNE_EVERY_S, ROLLUP_EVERY_S, STATS_EVERY_S, STATUS_EVERY_S, Recorder
from retina_magnetometer.storage import Storage

T0 = 1_790_726_400.0
SESSION = {"cycle_count": 200, "gain": 74.92, "rate_hz": 1.0, "mode": "poll", "bus": "test bus", "address": 0x20}


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def rig(tmp_path, *, monotonic=None, **env):
    values = {"MAGNETOMETER_DATA_DIR": str(tmp_path), "MAGNETOMETER_FLUSH_INTERVAL_S": "5"}
    values.update({f"MAGNETOMETER_{k}": str(v) for k, v in env.items()})
    config = from_env(values)
    clock = Clock()
    health = Health(config, clock=clock, monotonic=clock)
    storage = Storage(config.db_path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=clock)
    recorder = Recorder(storage, health, config, clock=clock, monotonic=monotonic or clock)
    return recorder, storage, health, clock


def run_sampler(recorder, clock, seconds, *, tick=0.5, monotonic=None, x0=1.0):
    """A 1 Hz poll-mode sampler (each sample 3 ms past its second), with the
    recorder ticking every ``tick`` seconds, for ``seconds`` of both clocks."""
    next_sample = math.ceil(clock.now)
    end = clock.now + seconds
    while clock.now < end:
        while next_sample <= clock.now:
            recorder.add(int(next_sample * 1000) + 3, x0 + next_sample % 7, 2.0, 3.0)
            next_sample += 1
        recorder.tick()
        clock.now += tick
        if monotonic is not None:
            monotonic.now += tick


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


@pytest.mark.parametrize("phase_s", [0.0, 10.0, 20.0, 30.0, 45.0, 52.5, 57.5])
def test_minutes_are_complete_with_a_long_flush_interval(tmp_path, phase_s):
    # With a minute between flushes, a minute that ended well before the
    # roll-up can still be partly in memory. Summarising it then would lose
    # those samples from the long views for good (the watermark moves on).
    recorder, storage, _, clock = rig(tmp_path, FLUSH_INTERVAL_S=60)
    clock.now = T0 + phase_s
    first_full = math.ceil((T0 + phase_s) / 60) * 60
    run_sampler(recorder, clock, 20 * 60)
    db = sqlite3.connect(storage.path)
    minutes = db.execute("SELECT t_ms, n FROM minutes WHERE t_ms >= ?", (first_full * 1000,)).fetchall()
    assert len(minutes) >= 17
    assert all(n == 60 for _, n in minutes), minutes


def test_a_wall_clock_step_back_does_not_stop_the_housekeeping(tmp_path):
    # The schedule runs on the monotonic clock: after the wall clock steps
    # back a quarter of an hour, flushes, roll-ups and the status file carry
    # on, where they used to wait for the clock to catch up.
    monotonic = Clock(5000.0)
    recorder, storage, health, clock = rig(tmp_path, monotonic=monotonic)
    run_sampler(recorder, clock, 20 * 60, monotonic=monotonic)
    clock.now -= 15 * 60
    written = []
    for _ in range(10):
        run_sampler(recorder, clock, 60, monotonic=monotonic, x0=100.0)
        written.append(json.loads((tmp_path / "status.json").read_text())["written_at"])
    assert len(set(written)) == 10  # rewritten throughout, not frozen
    assert recorder.pending_from_ms() is None or recorder.pending_from_ms() > int(clock.now * 1000) - 10_000
    # The live buffer stayed in time order, up to the newest sample.
    recent = [r[0] for r in recorder.recent()]
    assert recent == sorted(recent) and recent[-1] > int(clock.now * 1000) - 2000
    # The replayed span overwrote the samples it landed on (same millisecond
    # each second), and its minutes, summarised before the step, were
    # summarised again from what is on disk now.
    db = sqlite3.connect(storage.path)
    replayed = db.execute("SELECT COUNT(*) FROM samples WHERE x >= 100").fetchone()[0]
    assert replayed >= 590 and storage.stats()["samples"] == 20 * 60
    mismatched = db.execute(
        """
        SELECT m.t_ms FROM minutes m
        JOIN (SELECT (t_ms / 60000) * 60000 AS t, COUNT(*) AS n, AVG(x) AS x FROM samples GROUP BY t) s ON s.t = m.t_ms
        WHERE m.n != s.n OR abs(m.x_mean - s.x) > 1e-9
        """
    ).fetchall()
    assert mismatched == []


def test_sessions_are_written_with_the_next_flush(tmp_path):
    # The sampler records a session when it (re)finds the sensor; it must not
    # wait on the disk for that, or fail with it.
    recorder, storage, _, clock = rig(tmp_path)
    recorder.start_session(**SESSION)
    assert storage.sessions() == []
    clock.now += 1
    recorder.flush()
    (row,) = storage.sessions()
    assert row["started_ms"] == int(T0 * 1000) and row["cycle_count"] == 200 and row["bus"] == "test bus"


def test_a_failed_write_keeps_the_samples_and_sessions_and_says_so(tmp_path, monkeypatch):
    recorder, storage, health, clock = rig(tmp_path)
    recorder.add(int(T0 * 1000), 1.0, 1.0, 1.0)
    recorder.start_session(**SESSION)

    def broken(rows, sessions=()):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(storage, "write_samples", broken)
    recorder.flush()
    assert "disk I/O error" in health.snapshot()["storage_error"]
    assert recorder.pending_from_ms() == int(T0 * 1000)
    monkeypatch.undo()
    recorder.flush()
    assert storage.stats()["samples"] == 1 and len(storage.sessions()) == 1
    assert health.snapshot()["storage_error"] is None
    assert recorder.pending_from_ms() is None


def test_a_lasting_failure_is_logged_once(tmp_path, monkeypatch, caplog):
    # Five failed flushes are one problem, in the log as on the page. The
    # samples waiting are bounded (three, here), and losing the oldest of them
    # is said once too.
    recorder, storage, health, clock = rig(tmp_path)
    monkeypatch.setattr(recorder_module, "RECENT_MAX_SAMPLES", 3)

    def broken(rows, sessions=()):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(storage, "write_samples", broken)
    with caplog.at_level(logging.INFO, logger="retina_magnetometer.recorder"):
        for i in range(5):
            recorder.add(int(T0 * 1000) + i, 1.0, 1.0, 1.0)
            recorder.flush()
        monkeypatch.undo()
        recorder.flush()
    messages = [r.getMessage() for r in caplog.records]
    assert messages == [
        "write failed: database or disk is full",
        "3 samples wait to be written, the most kept: the oldest are being dropped",
        "storage: write works again",
    ]
    assert [r[0] for r in recorder.recent()] == [int(T0 * 1000) + i for i in range(5)]
    assert storage.stats()["oldest_sample_ms"] == int(T0 * 1000) + 2 and storage.stats()["samples"] == 3


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_database_that_cannot_be_opened_is_reported_and_picked_up(tmp_path):
    # The data directory cannot be written (a bind mount with the wrong
    # owner). The app runs on: the problem is on the page, the samples wait,
    # and the database opens once the directory is fixed, with no restart.
    locked = tmp_path / "data"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        recorder, storage, health, clock = rig(locked)
        recorder.add(int(T0 * 1000), 1.0, 2.0, 3.0)
        recorder.tick()
        problem = health.snapshot()["storage_error"]
        assert problem.startswith(f"cannot open {locked / 'magnetometer.sqlite'}")
        assert recorder.pending_from_ms() == int(T0 * 1000)
        locked.chmod(0o700)
        clock.now += STATUS_EVERY_S
        recorder.tick()
        assert storage.stats()["samples"] == 1
        assert health.snapshot()["storage_error"] is None
        assert json.loads((locked / "status.json").read_text())["storage_error"] is None
    finally:
        locked.chmod(0o700)


def test_storage_figures_follow_another_writer(tmp_path):
    # The simulator's backfill writes into the same database while the app
    # runs; the page must show what is there within a minute, not at the next
    # prune.
    recorder, storage, health, clock = rig(tmp_path)
    recorder.tick()
    assert health.storage["samples"] == 0
    other = Storage(storage.path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=clock)
    other.write_samples([(int((T0 - 3600 + i) * 1000), 1.0, 2.0, 3.0) for i in range(3600)])
    clock.now += STATS_EVERY_S - 1
    recorder.tick()
    assert health.storage["samples"] == 0
    clock.now += 2
    recorder.tick()
    assert health.storage["samples"] == 3600
    assert health.storage["oldest_sample_ms"] == int((T0 - 3600) * 1000)


def test_status_file_follows_health(tmp_path):
    recorder, _, health, clock = rig(tmp_path)
    health.sample(T0, 1.0, 2.0, 2.0)
    recorder.tick()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "ok" and status["last_sample"]["b"] == 3.0


def test_stop_flushes_everything(tmp_path):
    recorder, storage, _, _ = rig(tmp_path)
    recorder.start()
    recorder.add(int(T0 * 1000), 1.0, 1.0, 1.0)
    recorder.start_session(**SESSION)
    recorder.stop()
    assert storage.stats()["samples"] == 1 and len(storage.sessions()) == 1
    assert (tmp_path / "status.json").exists()


def test_a_prune_that_reaches_the_size_cap_says_so(tmp_path, caplog):
    recorder, storage, health, clock = rig(tmp_path)
    storage.max_bytes = 64 * 1024
    for i in range(20_000):
        recorder.add(int((T0 - 20_000 + i) * 1000), 16012.37 + i * 0.013, -2001.11, 43005.13)
    with caplog.at_level(logging.WARNING, logger="retina_magnetometer.recorder"):
        recorder.tick()
    assert "database reached its size cap" in caplog.text
    assert health.storage["size_capped_ms"] == int(T0 * 1000)
    assert health.storage["samples"] < 20_000 and health.storage["newest_sample_ms"] == int((T0 - 1) * 1000)


def test_a_session_with_the_wrong_arguments_fails_where_it_is_made(tmp_path):
    # Not in a flush later, where it would take every sample with it.
    recorder, storage, _, _ = rig(tmp_path)
    with pytest.raises(TypeError):
        recorder.start_session(cycle_count=200, gain_lsb_per_ut=74.92, rate_hz=1.0, mode="poll", bus="b", address=0x20)
    recorder.add(int(T0 * 1000), 1.0, 2.0, 3.0)
    recorder.flush()
    assert storage.stats()["samples"] == 1 and storage.sessions() == []


def test_a_flush_that_fails_unexpectedly_keeps_its_samples(tmp_path, monkeypatch, caplog):
    recorder, storage, health, clock = rig(tmp_path)
    recorder.add(int(T0 * 1000), 1.0, 2.0, 3.0)
    recorder.start_session(**SESSION)

    def bug(rows, sessions=()):
        raise KeyError("gain")

    monkeypatch.setattr(storage, "write_samples", bug)
    with caplog.at_level(logging.ERROR, logger="retina_magnetometer.recorder"):
        recorder.flush()
    assert health.snapshot()["storage_error"] == "write failed: KeyError: 'gain'"
    assert caplog.records[0].exc_info is not None  # a bug: logged with where it happened
    monkeypatch.undo()
    recorder.flush()
    assert storage.stats()["samples"] == 1 and len(storage.sessions()) == 1


class StopAfter:
    """The recorder thread's stop event, for ``rounds`` waits; each wait moves
    the clock on as the real half-second would, only further."""

    def __init__(self, rounds, clock, step):
        self.rounds, self.clock, self.step = rounds, clock, step

    def wait(self, timeout):
        self.clock.now += self.step
        self.rounds -= 1
        return self.rounds < 0

    def set(self):
        self.rounds = -1


def test_a_task_that_fails_unexpectedly_holds_up_none_of_the_others(tmp_path, monkeypatch, caplog):
    # A bug in the roll-up, at every tick: flushes and status.json carry on,
    # and the page and the log say what is wrong, the log once.
    recorder, storage, health, clock = rig(tmp_path)

    def bug(**kwargs):
        raise RuntimeError("a bug in the roll-up")

    monkeypatch.setattr(storage, "rollup", bug)
    with caplog.at_level(logging.ERROR, logger="retina_magnetometer.recorder"):
        for i in range(4):
            recorder.add(int((T0 + i) * 1000), 1.0, 2.0, 3.0)
            recorder.tick()
            clock.now += ROLLUP_EVERY_S + 1
    assert storage.stats()["samples"] == 4
    assert json.loads((tmp_path / "status.json").read_text())["storage_error"] == (
        "rollup failed: RuntimeError: a bug in the roll-up"
    )
    assert [r.getMessage() for r in caplog.records] == ["rollup failed: RuntimeError: a bug in the roll-up"]


def test_the_housekeeping_thread_outlives_an_unexpected_error(tmp_path, monkeypatch, caplog):
    # The last line of defence: whatever escapes a tick is logged, once, and
    # the thread goes on to the next.
    recorder, storage, health, clock = rig(tmp_path)
    ticks = []

    def bug():
        ticks.append(clock.now)
        raise RuntimeError("a bug")

    monkeypatch.setattr(recorder, "tick", bug)
    recorder._stop = StopAfter(3, clock, 0.5)
    with caplog.at_level(logging.ERROR, logger="retina_magnetometer.recorder"):
        recorder._run()
    assert len(ticks) == 3
    assert [r.getMessage() for r in caplog.records] == ["housekeeping failed: RuntimeError: a bug"]
    assert health.snapshot()["internal_errors_total"] == 1  # once, as it is logged


def test_a_lasting_roll_up_failure_stays_reported_while_writes_get_through(tmp_path, monkeypatch):
    # Every flush that got through used to clear the storage error, so with a
    # roll-up failing for good the state flipped between ok and degraded all
    # day. It is reported until the roll-up itself works again, and listed
    # once.
    recorder, storage, health, clock = rig(tmp_path)

    def broken(**kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(storage, "rollup", broken)
    health.sensor_ready(bus="b", address=0x20, revid=0x22, cycle_count=200, gain=74.92, effective_rate_hz=1.0)
    seen = []
    for second in range(600):
        clock.now += 1
        health.sample(clock.now, 1.0, 2.0, 3.0)
        recorder.add(int(clock.now * 1000), 1.0, 2.0, 3.0)
        recorder.tick()
        snapshot = health.snapshot()
        seen.append((snapshot["state"], snapshot["storage_error"]))
    assert set(seen) == {("degraded", "rollup failed: database disk image is malformed")}
    recorder.flush()
    assert storage.stats()["samples"] == 600  # the writes did get through
    assert [e["kind"] for e in health.snapshot()["recent_errors"]] == ["storage"]
    monkeypatch.undo()
    clock.now += ROLLUP_EVERY_S
    health.sample(clock.now, 1.0, 2.0, 3.0)
    recorder.tick()
    assert health.snapshot()["storage_error"] is None and health.snapshot()["state"] == "ok"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_storage_problem_that_is_over_clears_without_a_sample_to_write(tmp_path):
    # A node with no sensor, the norm today, has nothing to flush, and only a
    # flush with rows used to clear the error: fixed in a minute, the data
    # directory stayed reported as unwritable on the page and in status.json.
    locked = tmp_path / "data"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        recorder, storage, health, clock = rig(locked)
        health.no_bus("/dev/i2c-1 does not exist")
        recorder.tick()
        assert health.snapshot()["storage_error"].startswith("cannot open")
        locked.chmod(0o700)
        clock.now += ROLLUP_EVERY_S
        recorder.tick()
        assert health.snapshot()["storage_error"] is None
        assert json.loads((locked / "status.json").read_text())["storage_error"] is None
    finally:
        locked.chmod(0o700)


def test_every_failing_operation_is_reported_until_each_works(tmp_path, monkeypatch):
    recorder, storage, health, clock = rig(tmp_path)

    def broken(what):
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError(f"{what} is broken")

        return fail

    monkeypatch.setattr(storage, "rollup", broken("roll-up"))
    monkeypatch.setattr(storage, "stats", broken("counting"))
    recorder.tick()
    assert health.snapshot()["storage_error"] == "rollup failed: roll-up is broken; stats failed: counting is broken"
    monkeypatch.setattr(storage, "rollup", lambda **kwargs: 0)
    clock.now += ROLLUP_EVERY_S
    recorder.tick()
    assert health.snapshot()["storage_error"] == "stats failed: counting is broken"
    monkeypatch.undo()
    clock.now += STATS_EVERY_S
    recorder.tick()
    assert health.snapshot()["storage_error"] is None
