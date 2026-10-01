"""Health on its own: how what the sampler and the recorder report becomes the
state, detail and errors a reader of /api/health or status.json sees."""

import json
import os

import pytest

from retina_magnetometer import health as health_module
from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health, write_status_file

T0 = 1_790_726_400.0


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


def make(**settings):
    """Health on one fake clock, used as both its wall and monotonic time."""
    values = {f"MAGNETOMETER_{k}": str(v) for k, v in settings.items()}
    clock = Clock()
    return Health(from_env(values), clock=clock, monotonic=clock), clock


def found(h, rate_hz=1.0):
    h.sensor_ready(bus="test bus", address=0x20, revid=0x22, cycle_count=200, gain=74.92, effective_rate_hz=rate_hz)


def sampling(h, clock, rate_hz=1.0, seconds=5):
    found(h, rate_hz)
    for _ in range(int(seconds * rate_hz)):
        clock.now += 1 / rate_hz
        h.sample(clock.now, 20_000.0, 5_000.0, 43_000.0)


# ── Storage ──────────────────────────────────────────────────────────────────


def test_a_storage_failure_is_not_ok():
    # A node that samples but stores nothing is not healthy, and a reader of
    # state/detail/errors (retina-telemetry's contract) must be able to tell.
    h, clock = make()
    sampling(h, clock)
    h.storage_update(None, "write failed: database or disk is full")
    snap = h.snapshot()
    assert snap["state"] == "degraded" and snap["state_text"] == "Sampling with errors"
    assert snap["detail"] == "Storage: write failed: database or disk is full"
    assert snap["errors"][-1] == "write failed: database or disk is full"
    assert snap["storage_error"] == "write failed: database or disk is full"
    # Storage is not the sensor: the read-error figures are untouched.
    assert snap["read_errors_total"] == 0 and snap["consecutive_errors"] == 0


def test_storage_recovering_clears_the_state():
    h, clock = make()
    sampling(h, clock)
    h.storage_update(None, "write failed: disk I/O error")
    h.storage_update(None, None)
    snap = h.snapshot()
    assert snap["state"] == "ok" and snap["detail"] == "Sampling normally" and snap["storage_error"] is None


def test_a_storage_problem_is_listed_once_while_it_keeps_coming_back():
    # The recorder clears the storage error whenever a write gets through, so
    # a roll-up that keeps failing comes back every half minute: one problem,
    # listed once, not twenty "storage" entries pushing the read errors out.
    h, clock = make()
    sampling(h, clock)
    for _ in range(5):  # every flush while the card is full
        h.storage_update(None, "write failed: database or disk is full")
    h.storage_update({"bytes": 1}, h.storage_error)  # the stats refresh keeps it
    for _ in range(40):  # 20 minutes of a failing roll-up between good writes
        h.storage_update(None, "rollup failed: database disk image is malformed")
        clock.now += 5
        h.storage_update(None, None)
        clock.now += 25
    h.storage_update(None, "write failed: database or disk is full")  # twenty minutes after its last report
    storage = [e["message"] for e in h.snapshot()["recent_errors"] if e["kind"] == "storage"]
    assert storage == [
        "write failed: database or disk is full",
        "rollup failed: database disk image is malformed",
        # Last reported 20 minutes before: a new occurrence, listed again.
        "write failed: database or disk is full",
    ]


def test_a_storage_problem_back_soon_after_it_cleared_is_not_listed_again():
    h, clock = make()
    sampling(h, clock)
    h.storage_update(None, "prune failed: database is locked")
    clock.now += 600  # the next prune, ten minutes on, fails the same way
    h.storage_update(None, None)
    h.storage_update(None, "prune failed: database is locked")
    storage = [e for e in h.snapshot()["recent_errors"] if e["kind"] == "storage"]
    assert len(storage) == 1


def test_a_storage_problem_adds_to_a_sensor_problem():
    h, clock = make()
    h.self_test_result(passed=False, ran=True, x_ok=True, y_ok=False, z_ok=True, raw=0xEF)
    sampling(h, clock)
    h.storage_update(None, "write failed: disk I/O error")
    snap = h.snapshot()
    assert snap["state"] == "degraded"
    assert snap["detail"] == "Self test failed on Y; readings are suspect. Storage: write failed: disk I/O error"


@pytest.mark.parametrize("sensor_state", ["no_bus", "no_sensor", "stalled"])
def test_a_storage_problem_does_not_hide_a_sensor_state(sensor_state):
    h, clock = make()
    sampling(h, clock)
    if sensor_state == "no_bus":
        h.no_bus("/dev/i2c-1 does not exist")
    elif sensor_state == "no_sensor":
        h.no_sensor("test bus", "No RM3100 answered")
    else:
        clock.now += 60
    h.storage_update(None, "write failed: disk I/O error")
    snap = h.snapshot()
    assert snap["state"] == sensor_state
    assert snap["errors"][-1] == "write failed: disk I/O error"


# ── Errors that are not the sensor's, and settings that were replaced ───────


def test_an_internal_error_is_counted_apart_from_read_errors():
    h, clock = make()
    sampling(h, clock)
    h.internal_error("RuntimeError: recorder broke")
    snap = h.snapshot()
    assert snap["state"] == "degraded" and snap["detail"] == "internal error: RuntimeError: recorder broke"
    assert snap["internal_errors_total"] == 1
    assert snap["read_errors_total"] == 0 and snap["consecutive_errors"] == 0
    assert snap["recent_errors"][-1]["kind"] == "internal error"


def test_a_configuration_warning_is_degraded_while_sampling():
    h, clock = make(LATITUDE="34.85")
    (warning,) = h.snapshot()["config_warnings"]
    assert h.snapshot()["state"] == "starting"  # not hidden behind it, not hiding it
    sampling(h, clock)
    snap = h.snapshot()
    assert snap["state"] == "degraded" and snap["detail"] == warning
    assert snap["config_errors"] == []


def test_a_setting_replaced_after_start_up_is_a_warning_too():
    # The listen address only fails when the server binds, in __main__.
    h, clock = make()
    sampling(h, clock)
    message = "MAGNETOMETER_HOST='192.0.2.1' cannot be listened on: the page is served on 127.0.0.1 only"
    h.config_warning(message)
    h.config_warning(message)
    snap = h.snapshot()
    assert snap["config_warnings"] == [message]
    assert snap["state"] == "degraded" and snap["detail"] == message


def test_a_capacity_note_is_shown_but_is_not_a_fault():
    # An operator may choose a small cap on purpose: the note says what it
    # holds, and the state stays what sampling makes it.
    h, clock = make(MAX_DB_MB="16")
    sampling(h, clock)
    snap = h.snapshot()
    assert snap["state"] == "ok" and snap["detail"] == "Sampling normally"
    assert snap["config_notes"] == list(h.config.notes) and len(snap["config_notes"]) == 1
    assert snap["config_warnings"] == [] and snap["config_errors"] == []


# ── A sampler that has gone quiet ────────────────────────────────────────────


def test_still_starting_long_after_start_reads_as_stalled():
    # Before the first acquisition attempt has reported anything, or after
    # the sensor was found and no sample followed: either way, not "starting".
    h, clock = make()
    clock.now += 9
    assert h.snapshot()["state"] == "starting"
    clock.now += 2
    snap = h.snapshot()
    assert snap["state"] == "stalled" and "Still looking for the sensor after 11 s" in snap["detail"]


def test_a_found_sensor_that_never_samples_reads_as_stalled():
    h, clock = make()
    clock.now += 30
    found(h)
    clock.now += 9
    assert h.snapshot()["state"] == "starting"
    clock.now += 3
    snap = h.snapshot()
    assert snap["state"] == "stalled" and snap["detail"] == "Sensor found 12 s ago; no sample since"
    h.sample(clock.now, 1.0, 2.0, 2.0)
    assert h.snapshot()["state"] == "ok"


def test_a_sensor_found_again_and_again_without_a_sample_is_still_stalled():
    # Each re-acquire says "found" again; the time without a sample runs on
    # from the first, and read errors in between do not hide it either.
    h, clock = make()
    found(h)
    for _ in range(6):
        clock.now += 1
        h.error("timeout", "DRDY did not rise within 26.8 ms")
        clock.now += 2
        found(h)
    snap = h.snapshot()
    assert snap["state"] == "stalled" and snap["detail"] == "Sensor found 18 s ago; no sample since"
    assert snap["reinitialisations"] == 6


def test_a_lost_sensor_found_again_gets_a_fresh_start():
    h, clock = make()
    sampling(h, clock)
    h.no_sensor("test bus", "No RM3100 answered")
    clock.now += 300
    found(h)
    clock.now += 5
    assert h.snapshot()["state"] == "starting"


def test_samples_stopping_after_an_error_read_as_stalled():
    h, clock = make()
    sampling(h, clock)
    clock.now += 1
    h.error("I2C error", "[Errno 121] Remote I/O error")
    clock.now += 11
    snap = h.snapshot()
    assert snap["state"] == "stalled" and snap["detail"] == "No sample for 12 s"


def test_a_slow_rate_waits_longer_before_calling_it_stalled():
    h, clock = make(SAMPLE_RATE_HZ="0.05")  # a sample every 20 s
    found(h, 0.05)
    clock.now += 99
    assert h.snapshot()["state"] == "starting"
    clock.now += 2
    assert h.snapshot()["state"] == "stalled"


def test_a_wall_clock_step_is_not_silence():
    # An RTC-less board sets its clock from NTP after boot: hours forward.
    wall, monotonic = Clock(T0 - 86_400), Clock(5_000.0)
    h = Health(from_env({}), clock=wall, monotonic=monotonic)
    monotonic.now += 1
    wall.now += 1 + 86_400
    assert h.snapshot()["state"] == "starting"
    found(h)
    monotonic.now += 1
    wall.now += 1
    h.sample(wall.now, 1.0, 2.0, 2.0)
    wall.now += 3600  # and once more, while sampling
    monotonic.now += 0.5
    assert h.snapshot()["state"] == "ok"


# ── Rates ────────────────────────────────────────────────────────────────────


def test_poll_mode_effective_rate_leaves_out_the_ticks_it_missed():
    h, clock = make(SAMPLE_RATE_HZ="10")
    found(h, 10.0)
    for _ in range(50):  # every other tick lost to an overrunning measurement
        clock.now += 0.1
        h.ticks_missed(clock.now, 1, 0.13)
        clock.now += 0.1
        h.sample(clock.now, 1.0, 2.0, 2.0)
    snap = h.snapshot()
    assert snap["effective_rate_hz"] == pytest.approx(5.0)
    assert snap["measured_rate_hz"] == pytest.approx(5.0)
    assert snap["state"] == "degraded"
    assert snap["detail"] == (
        "Sampling at 5 Hz instead of 10 Hz: a measurement takes 130.0 ms of the 100.0 ms period; "
        "lower the rate or the cycle count, or use continuous mode"
    )


def test_ticks_lost_to_a_held_up_sampler_are_not_blamed_on_the_measurement():
    h, clock = make(SAMPLE_RATE_HZ="10")
    found(h, 10.0)
    for _ in range(20):
        clock.now += 0.1
        h.sample(clock.now, 1.0, 2.0, 2.0)
        clock.now += 0.2
        h.ticks_missed(clock.now, 2, 0.007)  # a 7 ms measurement, then a busy host
    snap = h.snapshot()
    assert snap["state"] == "degraded"
    assert snap["detail"].endswith("40 ticks missed in the last minute while the sampler was held up")


def test_one_lost_tick_at_a_slow_rate_is_not_a_problem():
    # At 0.1 Hz a single tick is a sixth of a minute's; one stall of the host
    # costs it, and the rate is not wrong for that.
    h, clock = make(SAMPLE_RATE_HZ="0.1")
    found(h, 0.1)
    for _ in range(6):
        clock.now += 10
        h.sample(clock.now, 1.0, 2.0, 3.0)
    clock.now += 0.5
    h.ticks_missed(clock.now, 1, 0.007)
    snap = h.snapshot()
    assert snap["state"] == "ok" and snap["detail"] == "Sampling normally"
    assert snap["effective_rate_hz"] == pytest.approx(0.1 * 6 / 7)


def test_a_rare_missed_tick_is_not_a_problem():
    h, clock = make(SAMPLE_RATE_HZ="10")
    sampling(h, clock, rate_hz=10, seconds=30)
    h.ticks_missed(clock.now, 1, 0.007)
    snap = h.snapshot()
    assert snap["effective_rate_hz"] == pytest.approx(10 * 300 / 301)
    assert snap["state"] == "ok"


def test_missed_ticks_age_out_with_the_minute():
    h, clock = make(SAMPLE_RATE_HZ="10")
    found(h, 10.0)
    h.ticks_missed(clock.now, 300, 0.2)
    sampling(h, clock, rate_hz=10, seconds=61)
    snap = h.snapshot()
    assert snap["effective_rate_hz"] == 10.0 and snap["state"] == "ok"


def test_continuous_mode_reports_the_chips_rate():
    h, clock = make(MODE="continuous", SAMPLE_RATE_HZ="37")
    sampling(h, clock, rate_hz=37.5, seconds=5)
    assert h.snapshot()["effective_rate_hz"] == 37.5


def test_a_clamped_poll_rate_shows_what_was_configured():
    h, clock = make(SAMPLE_RATE_HZ="120")
    assert h.config.sample_rate_hz < 120
    sampling(h, clock, rate_hz=h.config.sample_rate_hz, seconds=5)
    snap = h.snapshot()
    assert snap["configured_rate_hz"] == 120.0
    assert snap["effective_rate_hz"] == h.config.sample_rate_hz


# ── The status file ──────────────────────────────────────────────────────────


def test_status_file_is_replaced_whole_and_not_synced(tmp_path, monkeypatch):
    # Every few seconds, for as long as the node runs: a sync each time would
    # be the card's most frequent one. The rename is what readers rely on.
    def no_sync(fd):
        raise AssertionError("status.json must not be fsync'd")

    monkeypatch.setattr(health_module.os, "fsync", no_sync)
    path = tmp_path / "data" / "status.json"
    write_status_file(path, {"schema": 1, "state": "ok"})
    write_status_file(path, {"schema": 1, "state": "degraded"})
    assert json.loads(path.read_text()) == {"schema": 1, "state": "degraded"}
    assert os.stat(path).st_mode & 0o777 == 0o644
    assert sorted(p.name for p in path.parent.iterdir()) == ["status.json"]
