"""The sampler against the chip model: sampling, health, and every recovery path.

The recovery paths run in both modes. Continuous mode has its own ways to go
wrong: the chip keeps measuring through a NACK burst and across a restart of
the app, and a reset ends it without any transfer failing.

The node app must never import the simulator (it ships without it in spirit,
and "no code changes" means the app cannot know it is simulated). The tests
may: they are where the two meet.
"""

import ast
import pathlib
import sqlite3
import time

import pytest
from fakes import BrokenBus, FakeClock, ModelBus, ScriptedBus, nack

from retina_magnetometer.config import Config, from_env
from retina_magnetometer.health import Health
from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.driver import RM3100
from retina_magnetometer.sampler import BACKOFF_MAX_S, REINIT_AFTER, Sampler
from rm3100_sim import scenario as sc
from rm3100_sim.device import RM3100Model, SimClock

START = 1_790_726_400.0
MODES = ["poll", "continuous"]


class Rig:
    """Sampler + health + a chip model, all on one fake clock.

    The sampler's wall clock is that clock plus ``wall_offset``, so a test can
    step the wall clock (an NTP correction) while monotonic time runs on.
    ``restart`` starts a second app on the same chip, the way a container
    restart does: the chip keeps whatever the last run left in its registers.
    """

    def __init__(self, scenario_extra=None, *, chip=None, config=None, **settings):
        if chip is None:
            self.clock = FakeClock(START)
            self.scenario = sc.from_dict(
                {"site": {"latitude": 34.85, "longitude": -82.39}, **(scenario_extra or {})}, start=START, seed=1
            )
            self.model = RM3100Model(self.scenario, SimClock(START, monotonic=self.clock.monotonic))
        else:
            self.clock, self.scenario, self.model = chip
        if config is None:
            values = {"MAGNETOMETER_SAMPLE_RATE_HZ": "1"}
            values.update({f"MAGNETOMETER_{k}": str(v) for k, v in settings.items()})
            config = from_env(values)
        self.config = config
        self.samples = []
        self.sessions = []
        self.health = Health(self.config, clock=self.clock.monotonic, monotonic=self.clock.monotonic)
        self.bus_opens = 0
        self.wall_offset = 0.0
        self.sampler = Sampler(
            self.config,
            self.health,
            lambda *s: self.samples.append(s),
            on_session=lambda **kw: self.sessions.append(kw),
            bus_opener=self.open_bus,
            clock=lambda: self.clock.now + self.wall_offset,
            monotonic=self.clock.monotonic,
            wait=self.clock.sleep,
            sensor_sleep=self.clock.sleep,
        )

    def open_bus(self, spec, repeated_start=True):
        self.bus_opens += 1
        return ModelBus(self.model)

    def run(self, seconds):
        until = self.clock.now + seconds
        while self.clock.now < until:
            self.sampler.step()

    def run_thread_loop(self, seconds):
        """``Sampler.run``, the thread's own loop, for ``seconds`` of fake time."""
        until = self.clock.now + seconds

        def wait(s):
            self.clock.sleep(s)
            if self.clock.now >= until:
                self.sampler._stop.set()

        self.sampler._wait = wait
        self.sampler.run()

    def restart(self, **settings):
        """A new app on this chip. The old one is abandoned, not stopped: a
        killed or crashed container leaves the chip in whatever mode it was."""
        return Rig(chip=(self.clock, self.scenario, self.model), **settings)

    def samples_after(self, t):
        return [s for s in self.samples if s[0] > t * 1000]


class TrackedBus(ModelBus):
    """The chip model on a bus that counts how often it is closed."""

    def __init__(self, model):
        super().__init__(model)
        self.closed = 0

    def close(self):
        self.closed += 1


class SlowModelBus(ModelBus):
    """The chip model behind a bus on which every transfer takes ``seconds``."""

    def __init__(self, model, clock, seconds):
        super().__init__(model)
        self.clock = clock
        self.seconds = seconds

    def transfer(self, address, write, read_length=0):
        self.clock.advance(self.seconds)
        return super().transfer(address, write, read_length)


class HundredKilohertzBus(ModelBus):
    """The chip model behind a 100 kHz I2C bus (nine bit times a byte, the
    address and a repeated start included), as a chip whose conversion takes
    ``slower`` times Table 3-1's: DRDY reads low until then."""

    def __init__(self, model, clock, cycle_count, slower=1.0, host_s=0.0):
        super().__init__(model)
        self.clock = clock
        self.conversion = reg.xyz_conversion_s(cycle_count) * slower
        self.host_s = host_s  # the host's own share of each measurement
        self.polled_at = None

    def transfer(self, address, write, read_length=0):
        size = 1 + len(write) + (1 + read_length if read_length else 0)
        self.clock.advance(size * 9 / 100_000)
        if write[:1] == b"\x00" and write[1:2] not in (b"", b"\x00"):
            self.clock.advance(self.host_s)
            self.polled_at = self.clock.now
        reply = super().transfer(address, write, read_length)
        if write == b"\x34" and self.polled_at is not None and self.clock.now - self.polled_at < self.conversion:
            return b"\x00"
        return reply


class ClosingBus(ModelBus):
    """The chip model on a bus that refuses transfers once closed, as a real
    one does, and keeps the order of what was done on it."""

    def __init__(self, model):
        super().__init__(model)
        self.events = []
        self.closed = False

    def transfer(self, address, write, read_length=0):
        if self.closed:
            raise OSError(9, "Bad file descriptor")
        self.events.append(bytes(write))
        return super().transfer(address, write, read_length)

    def close(self):
        self.events.append("close")
        self.closed = True


# ── Sampling ─────────────────────────────────────────────────────────────────


def test_samples_on_the_second_with_mid_conversion_timestamps():
    rig = Rig()
    rig.run(10)
    assert len(rig.samples) == 10
    stamps = [s[0] for s in rig.samples]
    assert all(abs((t % 1000) - 3.4) < 1.0 for t in stamps)  # half of a 6.8 ms conversion
    assert rig.health.snapshot()["state"] == "ok"
    assert rig.sessions[0]["cycle_count"] == 200 and rig.sessions[0]["mode"] == "poll"


def test_health_reports_the_sensor_and_the_self_test():
    rig = Rig()
    rig.run(3)
    snap = rig.health.snapshot()
    assert snap["sensor"]["address"] == "0x20" and snap["sensor"]["revid"] == "0x22"
    assert snap["self_test"]["passed"] is True
    assert snap["samples_total"] == 3
    assert snap["last_sample"]["b"] == pytest.approx(48_600, abs=400)


def test_configured_cycle_count_reaches_the_chip():
    rig = Rig(CYCLE_COUNT=400)
    rig.run(2)
    assert rig.model.cycle_counts == [400, 400, 400]


def test_continuous_mode_delivers_at_the_tmrc_rate():
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    rig.run(4)
    assert rig.model.continuous
    rig.sampler.stop()  # delivers what still waits for a read-back
    assert len(rig.samples) == pytest.approx(4 * 37.5, abs=3)


def test_poll_mode_that_keeps_up_reports_the_configured_rate():
    rig = Rig(SAMPLE_RATE_HZ=80)
    rig.run(3)
    rig.sampler.stop()
    snap = rig.health.snapshot()
    assert len(rig.samples) == pytest.approx(240, abs=2)
    assert snap["effective_rate_hz"] == 80.0 and snap["state"] == "ok"


@pytest.mark.parametrize("cycle_count", [200, 1000])
def test_poll_mode_keeps_its_limit_with_a_slower_chip_on_a_100_khz_bus(cycle_count):
    # Just under the limit, with a chip 5 % slower than Table 3-1, every
    # transfer at 100 kHz and a millisecond of the host's own per sample:
    # every tick is kept. (At 1000 cycles the limit before this budget lost
    # every other one.)
    rate = RM3100.max_poll_rate_hz(cycle_count) * 0.995
    rig = Rig(SAMPLE_RATE_HZ=f"{rate!r}", CYCLE_COUNT=cycle_count)
    assert rig.config.warnings == ()
    rig.sampler._open_bus = lambda spec, repeated_start=True: HundredKilohertzBus(
        rig.model, rig.clock, cycle_count, slower=1.05, host_s=0.001
    )
    rig.run(12)
    rig.sampler.stop()
    window = [t for t, *_ in rig.samples if (START + 2) * 1000 <= t < (START + 12) * 1000]
    delivered = len(window) / 10
    snap = rig.health.snapshot()
    assert delivered == pytest.approx(rate, rel=0.01)
    assert snap["state"] == "ok" and snap["effective_rate_hz"] == pytest.approx(rate)


def test_a_poll_rate_above_the_limit_runs_at_the_limit_and_says_so():
    # A node configured before poll mode had a limit of its own keeps
    # collecting data after an upgrade, at the most poll mode keeps.
    limit = RM3100.max_poll_rate_hz(200)
    rig = Rig(SAMPLE_RATE_HZ=120)
    assert rig.config.errors == () and rig.config.sample_rate_hz == limit
    rig.run(3)
    rig.sampler.stop()
    snap = rig.health.snapshot()
    assert len(rig.samples) == pytest.approx(3 * limit, abs=2)
    assert snap["state"] == "degraded"
    assert f"sampling at {limit:.3g} Hz of the 120 Hz configured" in snap["detail"]
    assert "continuous mode" in snap["detail"]
    assert snap["configured_rate_hz"] == 120 and snap["effective_rate_hz"] == pytest.approx(limit)
    assert snap["config_warnings"] == list(rig.config.warnings)


def test_poll_mode_that_cannot_keep_up_says_so():
    # Validation keeps a rate like this out (each sample here takes 9.8 ms of
    # a 7.1 ms period); a bus slower than it allows for gets here anyway, and
    # every other tick is lost. The health document must not call that 140 Hz.
    rig = Rig(config=Config(sample_rate_hz=140.0))
    rig.sampler._open_bus = lambda spec, repeated_start=True: SlowModelBus(rig.model, rig.clock, 0.001)
    rig.run(3)
    rig.sampler.stop()
    snap = rig.health.snapshot()
    assert len(rig.samples) == pytest.approx(3 * 70, rel=0.05)
    assert snap["effective_rate_hz"] == pytest.approx(70, rel=0.05)
    assert snap["measured_rate_hz"] == pytest.approx(70, rel=0.05)
    assert snap["state"] == "degraded"
    assert snap["detail"].startswith("Sampling at 70.2 Hz instead of 140 Hz: a measurement takes 9.8 ms of the 7.1 ms")


# ── Finding and configuring the sensor ───────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_acquisition_stops_continuous_mode_before_anything_needs_a_poll(mode):
    # Byte for byte: whatever the chip was left doing, continuous mode is
    # stopped before the self test's POLL (UM16 p.28, p.31: a POLL written
    # while it runs is NACKed and ignored), and the cycle count is in place
    # before the self test, whose wait depends on it.
    rig = Rig(I2C_ADDRESS="0x20", CYCLE_COUNT=400, MODE=mode)
    a = 0x20
    bus = (
        ScriptedBus()
        .expect(a, b"\x36", 1, b"\x22")  # probe
        .expect(a, b"\x36", 1, b"\x22")  # REVID for the health page
        .expect(a, b"\x01\x00")  # stop whatever the last run left going
        .expect(a, b"\x04" + b"\x01\x90" * 3)  # 400 cycles on all three axes
        .expect(a, b"\x04", 6, b"\x01\x90" * 3)
        .expect(a, b"\x01\x00")  # the self test makes sure of it too
        .expect(a, b"\x33\x8f")
        .expect(a, b"\x00\x70")
        .expect_status(a, 0x80)
        .expect(a, b"\x33", 1, b"\xff")
        .expect(a, b"\x00\x00")
        .expect(a, b"\x33\x00")
    )
    if mode == "continuous":
        bus.expect(a, b"\x01\x00").expect(a, b"\x0b\x9b").expect(a, b"\x01\x79")
        bus.expect(a, b"\x0b", 1, b"\x9b")  # TMRC as set, for the read-backs (reading it ends nothing)
    else:
        bus.expect(a, b"\x0b\x9f")  # TMRC, unused in poll mode, set to what no reset leaves
        bus.expect(a, b"\x0b", 1, b"\x9f")
    rig.sampler._open_bus = lambda spec, repeated_start=True: bus
    assert rig.sampler._acquire()
    bus.assert_done()
    assert rig.health.snapshot()["self_test"]["passed"]


@pytest.mark.parametrize("mode", MODES)
def test_a_restart_finds_the_chip_still_in_continuous_mode(mode):
    # The last run was continuous and never stopped the chip (killed, or a
    # config change applied with --force-recreate). The new run's self test
    # must still run, and pass, in either mode.
    first = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    first.run(3)
    assert first.model.continuous
    second = first.restart(MODE=mode, SAMPLE_RATE_HZ=37 if mode == "continuous" else 1)
    second.run(5)
    snap = second.health.snapshot()
    assert snap["self_test"]["passed"] and snap["self_test"]["raw"] == "0xFF"
    assert snap["state"] == "ok" and snap["reinitialisations"] == 0
    assert len(second.samples) >= 4
    assert second.model.continuous is (mode == "continuous")


def test_without_the_self_test_continuous_mode_is_still_stopped_first():
    # The self test stops continuous mode itself, but it can be turned off;
    # poll mode's POLLs need the chip stopped all the same.
    first = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    first.run(3)
    second = first.restart(SELF_TEST="false")
    second.run(5)
    snap = second.health.snapshot()
    assert snap["self_test"] is None
    assert snap["state"] == "ok" and snap["read_errors_total"] == 0 and len(second.samples) >= 4
    assert not second.model.continuous


def test_sampling_needs_no_session_recorder():
    rig = Rig()
    rig.sampler.on_session = None
    rig.run(3)
    assert len(rig.samples) == 3 and rig.sessions == []


def test_stop_leaves_the_chip_idle():
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    rig.run(2)
    assert rig.model.continuous
    rig.sampler.stop()
    assert not rig.model.continuous
    assert rig.sampler._sensor is None and rig.sampler._bus is None


def test_stop_in_poll_mode_leaves_the_chip_alone():
    rig = Rig()
    rig.run(2)
    transfers = rig.model.stats.transfers
    rig.sampler.stop()
    assert rig.model.stats.transfers == transfers
    assert rig.sampler._sensor is None


def test_stop_survives_a_chip_that_does_not_answer():
    rig = Rig({"faults": [{"type": "nack", "at": "+3s", "duration": "1h", "probability": 1.0}]}, MODE="continuous")
    rig.run(2)
    rig.clock.advance(2)
    rig.sampler.stop()  # the NACK is logged, not raised
    assert rig.sampler._sensor is None


def test_stop_stops_continuous_mode_before_closing_the_bus():
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    buses = []

    def open_bus(spec, repeated_start=True):
        buses.append(ClosingBus(rig.model))
        return buses[-1]

    rig.sampler._open_bus = open_bus
    rig.run(1)
    rig.sampler.stop()
    assert buses[-1].events[-2:] == [b"\x01\x00", "close"]
    assert not rig.model.continuous


def test_stop_leaves_the_chip_to_a_sampler_that_did_not_finish():
    # The join timed out: the sampler thread may be mid-transfer, or about to
    # drop the sensor, so the chip is not touched from here. The bus is still
    # closed, as it always was.
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    rig.run(1)

    class Stuck:
        def join(self, timeout):
            pass

        def is_alive(self):
            return True

    rig.sampler._thread = Stuck()
    transfers = rig.model.stats.transfers
    rig.sampler.stop()
    assert rig.model.stats.transfers == transfers and rig.model.continuous
    assert rig.sampler._bus is None and rig.sampler._sensor is None


def test_the_thread_starts_samples_and_stops():
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    rig.sampler.start()
    deadline = time.monotonic() + 10
    while len(rig.samples) < 10 and time.monotonic() < deadline:
        time.sleep(0.005)
    rig.sampler.stop()
    assert not rig.sampler._thread.is_alive()
    assert len(rig.samples) >= 10
    assert not rig.model.continuous


def test_no_bus_is_a_state_not_a_crash():
    rig = Rig()

    def missing(spec, repeated_start=True):
        raise FileNotFoundError(2, "No such file", spec)

    rig.sampler._open_bus = missing
    rig.run(5)
    snap = rig.health.snapshot()
    assert snap["state"] == "no_bus"
    assert "dtparam=i2c_arm=on" in snap["detail"]
    assert rig.samples == []


@pytest.mark.parametrize(
    "error,fragment",
    [
        (PermissionError(13, "Permission denied"), "may not open it (device permissions)"),
        (OSError(16, "Device or resource busy"), "Device or resource busy"),
        (ValueError("a tcp bus needs a host and a port"), "a tcp bus needs a host and a port"),
    ],
)
def test_a_bus_that_cannot_be_opened_says_why(error, fragment):
    rig = Rig()

    def refuse(spec, repeated_start=True):
        raise error

    rig.sampler._open_bus = refuse
    rig.run(3)
    snap = rig.health.snapshot()
    assert snap["state"] == "no_bus" and fragment in snap["detail"]


def test_no_sensor_is_a_state_not_a_crash():
    rig = Rig()
    rig.sampler._open_bus = lambda spec, repeated_start=True: BrokenBus(121)
    rig.run(5)
    snap = rig.health.snapshot()
    assert snap["state"] == "no_sensor"
    assert "0x20-0x23" in snap["detail"]


def test_a_configured_address_is_the_only_one_probed():
    rig = Rig(I2C_ADDRESS="0x20")
    rig.run(2)
    assert rig.health.snapshot()["state"] == "ok" and rig.samples
    elsewhere = Rig(I2C_ADDRESS="0x21")
    elsewhere.run(2)
    snap = elsewhere.health.snapshot()
    assert snap["state"] == "no_sensor" and "at 0x21" in snap["detail"]


def test_a_wrong_revid_reaches_the_health_page():
    # docs/hardware-verification.md item 2 is read off this detail.
    class Impostor(ScriptedBus):
        def transfer(self, address, write, read_length=0):
            if address == 0x20 and write == b"\x36":
                return b"\x23"
            raise nack()

    rig = Rig()
    rig.sampler._open_bus = lambda spec, repeated_start=True: Impostor()
    rig.run(2)
    snap = rig.health.snapshot()
    assert snap["state"] == "no_sensor"
    assert "device at 0x20 reports REVID 0x23, expected 0x22" in snap["detail"]


def test_retries_back_off_but_find_a_late_sensor():
    rig = Rig()
    real_open = rig.open_bus
    attempts = []

    def flaky(spec, repeated_start=True):
        attempts.append(rig.clock.now)
        if len(attempts) < 4:
            return BrokenBus(121)
        return real_open(spec, repeated_start)

    rig.sampler._open_bus = flaky
    rig.run(20)
    gaps = [b - a for a, b in zip(attempts, attempts[1:])]
    assert gaps[1] > gaps[0]  # exponential backoff
    assert rig.health.snapshot()["state"] == "ok"
    assert rig.samples


# ── Recovery, in both modes ──────────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_nack_burst_is_counted_and_recovered_from(mode):
    rig = Rig({"faults": [{"type": "nack", "at": "+5s", "duration": "3s", "probability": 1.0}]}, MODE=mode)
    rig.run(20)
    snap = rig.health.snapshot()
    assert snap["read_errors_total"] >= REINIT_AFTER
    assert snap["state"] == "ok"  # recovered
    assert snap["recent_errors"][0]["kind"] == "I2C error"
    assert snap["reinitialisations"] >= 1
    # The chip measured on through the burst in continuous mode; the self
    # test after it still ran on a stopped chip.
    assert snap["self_test"]["passed"] and snap["self_test"]["raw"] == "0xFF"
    assert rig.model.continuous is (mode == "continuous")
    assert len(rig.samples_after(START + 8)) >= 10


@pytest.mark.parametrize("mode", MODES)
def test_power_cycled_sensor_is_reconfigured(mode):
    rig = Rig({"faults": [{"type": "disconnect", "at": "+5s", "duration": "3s"}]}, CYCLE_COUNT=400, MODE=mode)
    rig.run(30)
    assert rig.model.stats.power_cycles == 1
    assert rig.model.cycle_counts == [400, 400, 400]  # set again after the reset
    assert rig.model.continuous is (mode == "continuous")
    assert rig.health.snapshot()["state"] == "ok"


@pytest.mark.parametrize(
    "mode,cycle_count,noticed_as",
    [
        ("poll", 400, "sensor reset"),  # the once-a-minute cycle-count check
        ("continuous", 400, "timeout"),  # the reset also ends continuous mode, and DRDY stops first
        ("continuous", 200, "timeout"),  # the cycle counts still match: nothing but DRDY can tell
    ],
)
def test_a_silent_reset_is_noticed_and_recovered_from(mode, cycle_count, noticed_as):
    rig = Rig(CYCLE_COUNT=cycle_count, MODE=mode)
    rig.run(5)
    rig.model._power_on_reset()  # a brown-out that no transfer noticed
    reset_at = rig.clock.now
    rig.run(70)
    assert rig.model.cycle_counts == [cycle_count] * 3
    assert rig.model.continuous is (mode == "continuous")
    snap = rig.health.snapshot()
    assert any(e["kind"] == noticed_as for e in snap["recent_errors"])
    assert snap["reinitialisations"] >= 1 and snap["state"] == "ok"
    assert len(rig.samples_after(reset_at + 15)) >= 50


def test_a_silent_reset_at_the_defaults_is_noticed_too():
    # At 200 cycles the counts read the same after a reset, but TMRC, which
    # poll mode sets to a value no reset leaves, does not: the sample that
    # may have been read from reset registers is dropped, and the chip set up
    # again.
    rig = Rig()
    rig.run(5)
    rig.model._power_on_reset()
    reset_at = rig.clock.now
    rig.run(30)
    snap = rig.health.snapshot()
    (reset,) = [e for e in snap["recent_errors"] if e["kind"] == "sensor reset"]
    assert "TMRC read 0x96, expected 0x9F" in reset["message"]
    assert "1 sample measured since the last good read-back dropped" in reset["message"]
    assert snap["reinitialisations"] == 1 and snap["state"] == "ok"
    assert len(rig.samples_after(reset_at)) >= 28
    assert rig.model.tmrc == 0x9F


def test_a_failed_sample_is_not_counted_as_missed_ticks():
    # Two DRDY timeouts in a row at 50 Hz each take longer than a period.
    # The ticks they cost are failures, already counted as such; counting
    # them again as missed ticks would blame the rate.
    rig = Rig({"faults": [{"type": "stuck_drdy", "at": "+2s", "duration": "0.05s"}]}, SAMPLE_RATE_HZ=50)
    rig.run(4)
    snap = rig.health.snapshot()
    assert snap["read_errors_total"] == 2 and snap["reinitialisations"] == 0
    assert snap["effective_rate_hz"] == 50.0 and snap["state"] == "ok"


@pytest.mark.parametrize("step_s", [3600.0, -3600.0])
def test_a_wall_clock_step_is_not_taken_for_missed_ticks(step_s):
    # An NTP correction moves the grid, not the time a measurement took:
    # missed ticks are counted on the monotonic clock.
    rig = Rig(SAMPLE_RATE_HZ=10)
    rig.run(2)
    rig.wall_offset += step_s
    rig.run(3)
    rig.sampler.stop()
    snap = rig.health.snapshot()
    assert snap["effective_rate_hz"] == 10.0 and snap["state"] == "ok"
    assert len(rig.samples) >= 49


def test_continuous_mode_ended_by_another_program_is_started_again():
    # i2cdump during bring-up reads CMM, which ends continuous mode (UM16
    # p.31). The cycle counts are untouched, so only DRDY going quiet shows it.
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37, CYCLE_COUNT=400)
    rig.run(3)
    rig.model.transfer(0x20, b"\x01", 1)
    assert not rig.model.continuous
    ended_at = rig.clock.now
    rig.run(5)
    assert rig.model.continuous
    assert len(rig.samples_after(ended_at)) >= 100
    snap = rig.health.snapshot()
    assert snap["reinitialisations"] == 1 and snap["state"] == "ok"
    assert any(e["kind"] == "timeout" and "continuous mode" in e["message"] for e in snap["recent_errors"])


def magnitudes(rig):
    return [(x * x + y * y + z * z) ** 0.5 for _, x, y, z in rig.samples]


@pytest.mark.parametrize("cycle_count", [400, 1000])
@pytest.mark.parametrize("mode,rate", [("poll", 1), ("poll", 10), ("continuous", 10)])
def test_a_brown_out_never_reaches_storage_at_the_wrong_gain(mode, rate, cycle_count):
    # A brown-out puts the chip back to 200 cycles without a single transfer
    # failing. Poll mode goes on measuring, at half the gain at 400 cycles and
    # a fifth at 1000, and the end-to-end review stored 27 s of |B| at 24,556
    # nT that way. Nothing measured after a reset may be delivered: the field
    # here is ~48,600 nT throughout.
    rig = Rig(
        {"faults": [{"type": "brownout", "at": "+5.55s"}]}, MODE=mode, SAMPLE_RATE_HZ=rate, CYCLE_COUNT=cycle_count
    )
    rig.run(20)
    rig.sampler.stop()
    assert rig.model.stats.power_cycles == 1
    b = magnitudes(rig)
    assert min(b) > 47_000 and max(b) < 50_500
    snap = rig.health.snapshot()
    if mode == "poll":
        (reset,) = [e for e in snap["recent_errors"] if e["kind"] == "sensor reset"]
        assert "dropped" in reset["message"]
    # Sampling went on, at the configured gain again.
    assert rig.model.cycle_counts == [cycle_count] * 3
    assert len(rig.samples_after(START + 10)) >= 9 * rate


@pytest.mark.parametrize(
    "cycle_count,rate",
    [
        (200, 37),  # every register the app sets is at its reset value: only a later DRDY tells
        (200, 10),
        (400, 10),
        (1000, 10),
    ],
)
def test_a_reset_between_drdy_and_the_results_is_not_delivered(cycle_count, rate):
    # In continuous mode a brown-out stops the chip measuring, which the
    # watchdog notices; but one that lands after the STATUS read that saw
    # DRDY and before the results are read hands over the reset registers:
    # a sample of zeros (UM16 Table 5-1). At 200 cycles and TMRC 0x96 (37.5 Hz)
    # nothing the app can read back differs from a reset chip, and CMM cannot
    # be read without ending continuous mode.
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=rate, CYCLE_COUNT=cycle_count)
    armed = []

    class ResetAfterDrdy(ModelBus):
        def transfer(self, address, write, read_length=0):
            reply = super().transfer(address, write, read_length)
            if armed and write == b"\x34" and reply[0] & 0x80:
                armed.clear()
                rig.model._power_on_reset()
            return reply

    rig.sampler._open_bus = lambda spec, repeated_start=True: ResetAfterDrdy(rig.model)
    rig.run(3)
    armed.append(True)
    rig.run(10)
    rig.sampler.stop()
    assert not armed  # it happened
    b = magnitudes(rig)
    assert min(b) > 47_000 and max(b) < 50_500
    assert rig.model.continuous is False and len(rig.samples_after(START + 6)) >= 5 * rate


@pytest.mark.parametrize("cycle_count", [200, 400])
def test_a_reset_between_drdy_and_the_results_in_poll_mode_is_not_delivered(cycle_count):
    # The same window in poll mode: the measurement is done, STATUS says so,
    # and the chip resets before its results are read. At the default 200
    # cycles the counts read back the same either way.
    rig = Rig(SAMPLE_RATE_HZ=1, CYCLE_COUNT=cycle_count)
    armed = []

    class ResetAfterDrdy(ModelBus):
        def transfer(self, address, write, read_length=0):
            reply = super().transfer(address, write, read_length)
            if armed and write == b"\x34" and reply[0] & 0x80:
                armed.clear()
                rig.model._power_on_reset()
            return reply

    rig.sampler._open_bus = lambda spec, repeated_start=True: ResetAfterDrdy(rig.model)
    rig.run(3)
    armed.append(True)
    rig.run(10)
    rig.sampler.stop()
    assert not armed
    b = magnitudes(rig)
    assert min(b) > 47_000 and max(b) < 50_500
    (reset,) = [e for e in rig.health.snapshot()["recent_errors"] if e["kind"] == "sensor reset"]
    assert "1 sample measured since the last good read-back dropped" in reset["message"]
    assert len(rig.samples) >= 11


def test_a_read_back_that_fails_keeps_its_samples_for_the_next():
    rig = Rig(SAMPLE_RATE_HZ=10, CYCLE_COUNT=400)
    fail = []

    class FailingReadBack(ModelBus):
        def transfer(self, address, write, read_length=0):
            if fail and write == b"\x04" and read_length == 6:
                fail.clear()
                raise nack()
            return super().transfer(address, write, read_length)

    rig.sampler._open_bus = lambda spec, repeated_start=True: FailingReadBack(rig.model)
    rig.run(2)
    fail.append(True)
    rig.run(3)
    rig.sampler.stop()
    assert not fail
    stamps = [s[0] for s in rig.samples]
    # Every tick from the first sample to the last, once: those held across
    # the failed read-back went out with the next one.
    assert stamps == sorted(set(stamps))
    assert len(stamps) == round((stamps[-1] - stamps[0]) / 100) + 1 >= 49
    assert rig.health.snapshot()["read_errors_total"] == 1


def test_a_clean_stop_delivers_what_is_held():
    # Nothing measured is lost on a clean stop: it is read back and delivered.
    rig = Rig(SAMPLE_RATE_HZ=10, CYCLE_COUNT=400)
    rig.run(1.55)
    rig.sampler.stop()
    stamps = [s[0] for s in rig.samples]
    assert stamps[-1] > (START + 1.5) * 1000
    assert len(stamps) == round((stamps[-1] - stamps[0]) / 100) + 1


@pytest.mark.parametrize("rate", [10, 37])
def test_a_clean_stop_in_continuous_mode_delivers_every_sample_read(rate):
    # The newest sample waits for a DRDY after it; at a clean stop the app
    # waits for that one DRDY rather than lose the sample.
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=rate)
    reads = []

    class CountingResults(ModelBus):
        def transfer(self, address, write, read_length=0):
            if write == b"\x24" and read_length == 9:
                reads.append(rig.clock.now)
            return super().transfer(address, write, read_length)

    rig.sampler._open_bus = lambda spec, repeated_start=True: CountingResults(rig.model)
    rig.run(2.03)
    rig.sampler.stop()
    assert len(rig.samples) == len(reads) >= 2 * rate * 0.9


def test_a_reset_just_before_a_clean_stop_is_not_delivered():
    # The zero sample is the newest one held when the app stops: the DRDY
    # that would confirm it never comes, so it is dropped, not delivered.
    rig = Rig(MODE="continuous", SAMPLE_RATE_HZ=37)
    armed = []

    class ResetAfterDrdy(ModelBus):
        def transfer(self, address, write, read_length=0):
            reply = super().transfer(address, write, read_length)
            if armed and write == b"\x34" and reply[0] & 0x80:
                armed.clear()
                rig.model._power_on_reset()
            return reply

    rig.sampler._open_bus = lambda spec, repeated_start=True: ResetAfterDrdy(rig.model)
    rig.run(2)
    armed.append(True)
    while armed:
        rig.sampler.step()
    rig.sampler.stop()
    b = magnitudes(rig)
    assert len(b) >= 60 and min(b) > 47_000


def test_samples_held_when_the_sensor_is_dropped_are_read_back_first():
    # DRDY sticks halfway through a batch: the samples measured before it are
    # still good, and a read-back as the sensor is dropped delivers them.
    rig = Rig(
        {"faults": [{"type": "stuck_drdy", "at": "+2.05s", "duration": "10s"}]}, SAMPLE_RATE_HZ=10, CYCLE_COUNT=400
    )
    rig.run(4)
    stamps = [s[0] for s in rig.samples]
    # The last tick before DRDY stuck, stamped mid-conversion.
    assert stamps[-1] == pytest.approx((START + 2.0 + reg.xyz_conversion_s(400) / 2) * 1000, abs=1)
    assert len(stamps) == round((stamps[-1] - stamps[0]) / 100) + 1


@pytest.mark.parametrize("mode", MODES)
def test_stuck_drdy_is_a_timeout_error(mode):
    rig = Rig({"faults": [{"type": "stuck_drdy", "at": "+3s", "duration": "10s"}]}, MODE=mode)
    rig.run(30)
    snap = rig.health.snapshot()
    assert "timeout" in {e["kind"] for e in snap["recent_errors"]}
    assert snap["state"] == "ok"  # and sampling again once DRDY works
    assert rig.samples_after(START + 14)


@pytest.mark.parametrize("mode", MODES)
def test_a_sensor_that_never_delivers_reads_as_stalled_and_is_retried_less_often(mode):
    # Found, self test passed, and then DRDY never rises: every re-acquire
    # finds it again, which must not hide that nothing has been sampled.
    rig = Rig({"faults": [{"type": "stuck_drdy", "at": "+0s", "duration": "1h"}]}, MODE=mode)
    rig.run(9)
    assert rig.health.snapshot()["state"] in ("starting", "degraded")
    rig.run(600)
    snap = rig.health.snapshot()
    assert rig.samples == []
    assert snap["state"] == "stalled" and snap["detail"].startswith("Sensor found ")
    assert snap["detail"].endswith(" s ago; no sample since")
    assert snap["reinitialisations"] <= 25  # backing off to 30 s, not every 2 s


@pytest.mark.parametrize("mode", MODES)
def test_failed_self_test_degrades_but_keeps_sampling(mode):
    rig = Rig({"sensor": {"dead_axis": "y"}}, MODE=mode)
    rig.run(5)
    snap = rig.health.snapshot()
    assert snap["state"] == "degraded"
    assert "Self test failed on Y" in snap["detail"]
    assert rig.samples


# ── Failures that are not the sensor's ───────────────────────────────────────


def test_a_failed_session_record_does_not_stop_sampling(caplog):
    # The recorder queues sessions and writes them with its own storage
    # operations, so a session recorder that raises is a bug in the app: the
    # row is lost, the samples are not, and it is an internal error, not a
    # storage one (which would hide a real write failure, and be cleared by
    # the recorder's next report).
    rig = Rig({"faults": [{"type": "nack", "at": "+5s", "duration": "3s", "probability": 1.0}]})

    def broken(**session):
        raise RuntimeError("session queue broke")

    rig.sampler.on_session = broken
    with caplog.at_level("ERROR", logger="retina_magnetometer.sampler"):
        rig.run(20)
    assert len(rig.samples_after(START + 9)) >= 10
    snap = rig.health.snapshot()
    assert snap["storage_error"] is None
    assert snap["internal_errors_total"] == 2  # the first acquisition and the one after the burst
    assert [e["message"] for e in snap["recent_errors"] if e["kind"] == "internal error"] == [
        "session record failed: RuntimeError: session queue broke"
    ] * 2
    # The traceback once; the second time, a line.
    assert len([r for r in caplog.records if r.exc_info]) == 1


def test_a_session_record_that_fails_on_a_reacquire_does_not_stop_sampling():
    rig = Rig({"faults": [{"type": "nack", "at": "+5s", "duration": "3s", "probability": 1.0}]})
    calls = []

    def second_one_fails(**session):
        calls.append(session)
        if len(calls) == 2:
            raise sqlite3.OperationalError("database or disk is full")

    rig.sampler.on_session = second_one_fails
    rig.run(30)
    assert len(calls) == 2
    assert len(rig.samples_after(START + 10)) >= 15


def test_every_bus_opened_is_closed_once():
    # Each re-acquire opens the bus again: a dropped sensor, the attempts that
    # fail while the burst lasts and the final stop must each give their file
    # descriptor back.
    rig = Rig({"faults": [{"type": "nack", "at": "+5s", "duration": "6s", "probability": 1.0}]})
    opened = []

    def open_bus(spec, repeated_start=True):
        opened.append(TrackedBus(rig.model))
        return opened[-1]

    rig.sampler._open_bus = open_bus
    rig.run(30)
    rig.sampler.stop()
    assert len(opened) >= 4  # the first, two failed attempts, the one after
    assert rig.health.snapshot()["reinitialisations"] == 1
    assert [bus.closed for bus in opened] == [1] * len(opened)


def test_a_bus_that_breaks_unexpectedly_while_acquiring_is_closed():
    rig = Rig()
    opened = []

    class Broken(TrackedBus):
        def transfer(self, address, write, read_length=0):
            raise TypeError("a bug in the transport")

    def open_bus(spec, repeated_start=True):
        opened.append((Broken if not opened else TrackedBus)(rig.model))
        return opened[-1]

    rig.sampler._open_bus = open_bus
    rig.run_thread_loop(5)
    assert opened[0].closed == 1
    assert any(e["kind"] == "internal error" for e in rig.health.snapshot()["recent_errors"])
    assert rig.samples


def test_the_thread_loop_outlives_an_unexpected_error():
    # Anything step() does not expect (a bug, a callback that raised) is
    # reported and retried; it must not end the thread while the page serves.
    rig = Rig()
    failures = []

    def on_sample(*sample):
        if not failures:
            failures.append(sample)
            raise RuntimeError("recorder broke")
        rig.samples.append(sample)

    rig.sampler.on_sample = on_sample
    rig.run_thread_loop(10)
    snap = rig.health.snapshot()
    assert any(e["kind"] == "internal error" and "recorder broke" in e["message"] for e in snap["recent_errors"])
    assert len(rig.samples) >= 6 and snap["state"] == "ok"
    assert snap["reinitialisations"] == 1
    # Not a read error: the sensor and the bus were fine.
    assert snap["read_errors_total"] == 0 and snap["internal_errors_total"] == 1


def test_an_unexpected_error_that_keeps_coming_back_backs_off(caplog):
    # A callback that fails every time: finding the sensor again succeeds
    # each time and proves nothing, so the retries back off to the cap, and
    # the traceback is logged once, not at every retry.
    rig = Rig()
    failed_at = []

    def broken(*sample):
        failed_at.append(rig.clock.now)
        raise RuntimeError("recorder broke")

    rig.sampler.on_sample = broken
    with caplog.at_level("ERROR", logger="retina_magnetometer.sampler"):
        rig.run_thread_loop(300)
    gaps = [b - a for a, b in zip(failed_at, failed_at[1:])]
    assert gaps == sorted(gaps) and gaps[-1] >= BACKOFF_MAX_S
    assert len(failed_at) <= 15
    tracebacks = [r for r in caplog.records if r.exc_info]
    assert len(tracebacks) == 1
    assert sum(", again;" in r.getMessage() for r in caplog.records) == len(failed_at) - 1
    snap = rig.health.snapshot()
    assert snap["read_errors_total"] == 0 and snap["internal_errors_total"] == len(failed_at)


def test_a_sample_that_gets_through_resets_the_backoff():
    rig = Rig()
    calls = []

    def fails_twice(*sample):
        calls.append(rig.clock.now)
        if len(calls) <= 2:
            raise RuntimeError("recorder broke")
        rig.samples.append(sample)

    rig.sampler.on_sample = fails_twice
    rig.run_thread_loop(20)
    assert rig.sampler._backoff == 1.0 and rig.sampler._unexpected is None
    assert len(rig.samples) >= 12


def test_half_a_location_does_not_stop_sampling():
    rig = Rig(LATITUDE="34.85")
    rig.run(3)
    snap = rig.health.snapshot()
    assert len(rig.samples) == 3
    assert snap["state"] == "degraded" and "MAGNETOMETER_LATITUDE is set without" in snap["detail"]


def test_config_errors_stop_sampling_and_say_why():
    rig = Rig(CYCLE_COUNT="nonsense")
    rig.sampler.run()  # returns at once
    snap = rig.health.snapshot()
    assert snap["state"] == "config_error"
    assert "MAGNETOMETER_CYCLE_COUNT" in snap["detail"]
    assert rig.bus_opens == 0


def test_stale_samples_read_as_stalled():
    rig = Rig()
    rig.run(3)
    rig.clock.advance(60)
    assert rig.health.snapshot()["state"] == "stalled"


def test_node_app_never_imports_the_simulator():
    root = pathlib.Path(__file__).parent.parent / "retina_magnetometer"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            assert not any(n.split(".")[0] == "rm3100_sim" for n in names), f"{path} imports the simulator"
