"""The app's driver against the register model: the chip's behaviour end to end.

Time is a FakeClock shared by the driver (its sleeps advance it) and the chip
(its conversions complete by it), so the datasheet timing runs exactly and
instantly.
"""

import logging
import statistics
from datetime import datetime, timezone

import pytest
from fakes import FakeClock, ModelBus

from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.driver import RM3100, DataReadyTimeout, find_rm3100
from rm3100_sim import physics
from rm3100_sim import scenario as sc
from rm3100_sim.device import EREMOTEIO, RM3100Model, SimClock, input_noise_nt, measure_counts

START = 1_790_726_400.0  # 2026-09-30T00:00:00Z
SITE = {"site": {"latitude": 34.85, "longitude": -82.39, "altitude_m": 300}}
QUIET = {"field": {"diurnal": {"enabled": False}}}

HSHAKE = b"\x35"
NACK0, NACK1, NACK2 = 0x10, 0x20, 0x40


def rig(extra=None, *, seed=5):
    """A chip, a driver on it, and the clock they share."""
    scenario = sc.from_dict({**SITE, **(extra or {})}, start=START, seed=seed)
    clock = FakeClock()
    model = RM3100Model(scenario, SimClock(START, monotonic=clock.monotonic))
    driver = RM3100(ModelBus(model), scenario.sensor.address, clock=clock.monotonic, sleep=clock.sleep)
    return scenario, model, driver, clock


def measure_many(driver, clock, n, period=0.1):
    samples = []
    for _ in range(n):
        samples.append(driver.single_measurement())
        clock.advance(period)
    return samples


def hshake(model) -> int:
    return model.transfer(0x20, HSHAKE, 1)[0]


def refused(model, write) -> OSError:
    """The write fails on the wire as i2c-dev reports a NACK."""
    with pytest.raises(OSError) as info:
        model.transfer(0x20, write)
    assert info.value.errno == EREMOTEIO
    return info.value


class TestIdentityAndConfiguration:
    def test_found_at_its_strapped_address(self):
        scenario, model, _, clock = rig({"sensor": {"address": 0x22}})
        found = find_rm3100(ModelBus(model), clock=clock.monotonic, sleep=clock.sleep)
        assert found.address == 0x22
        assert model.stats.transfers == 3  # 0x20 and 0x21 NACKed first

    def test_other_addresses_nack(self):
        _, model, _, _ = rig()
        with pytest.raises(OSError) as info:
            model.transfer(0x23, b"\x36", 1)
        assert info.value.errno == EREMOTEIO

    def test_power_on_cycle_count_is_200_and_writes_stick(self):
        _, model, driver, _ = rig()
        assert driver.read_cycle_counts() == (200, 200, 200)
        driver.set_cycle_count(400)
        assert model.cycle_counts == [400, 400, 400]


class TestMeasurements:
    def test_mean_is_the_scenario_field_and_noise_is_the_datasheet(self):
        scenario, _, driver, clock = rig(QUIET)
        samples = measure_many(driver, clock, 1500)
        expected = scenario.sensor_field(START + 75)
        sigma = reg.noise_nt(200)
        for axis, name in enumerate(("x_nt", "y_nt", "z_nt")):
            values = [getattr(s, name) for s in samples]
            assert statistics.fmean(values) == pytest.approx(expected[axis], abs=4 * sigma / len(values) ** 0.5 + 1)
            # Table 3-1's 15 nT is the output's noise, rounding to 13.3 nT counts included.
            assert statistics.pstdev(values) == pytest.approx(sigma, rel=0.08)

    @pytest.mark.parametrize("cycle_count", [30, 50, 200, 800])
    def test_output_noise_is_table_3_1_rounding_included(self, cycle_count):
        # Adding the table's noise and then rounding to counts would land 3 %
        # high at 200 cycles and 15 % at 30. Where the field sits within a
        # count changes the spread at low cycle counts, so the field is moved
        # across one count and each position's spread is taken about its own
        # mean: 24,000 values, enough to tell 3 % apart.
        lsb = reg.lsb_nt(cycle_count)
        variances = []
        for step in range(8):
            offset = lsb * step / 8
            sensor = {"sensor": {"hard_iron_nt": [offset, offset, offset]}}
            scenario = sc.from_dict({**SITE, **QUIET, **sensor}, start=START, seed=2)
            times = [START + 0.25 * (k + 1000 * step) for k in range(1000)]
            counts = [measure_counts(scenario, t, (cycle_count,) * 3) for t in times]
            for axis in range(3):
                variances.append(statistics.pvariance(reg.counts_to_nt(c[axis], cycle_count) for c in counts))
        assert statistics.fmean(variances) ** 0.5 == pytest.approx(reg.noise_nt(cycle_count), rel=0.015)

    def test_input_noise_leaves_room_for_the_rounding(self):
        for cycle_count in (30, 50, 200, 1000):
            total = (input_noise_nt(cycle_count) ** 2 + reg.lsb_nt(cycle_count) ** 2 / 12) ** 0.5
            assert total == pytest.approx(reg.noise_nt(cycle_count), rel=1e-12)
        assert input_noise_nt(200, noise_scale=0.0) == 0.0  # a noiseless sensor still rounds

    def test_higher_cycle_count_is_quieter(self):
        _, _, driver, clock = rig(QUIET)
        driver.set_cycle_count(800)
        values = [s.x_nt for s in measure_many(driver, clock, 1200)]
        assert statistics.pstdev(values) == pytest.approx(reg.noise_nt(800), rel=0.1)

    def test_drdy_waits_for_the_conversion_time(self):
        _, model, driver, clock = rig()
        driver.set_cycle_count(200)
        model.transfer(0x20, b"\x00\x70")
        assert not driver.data_ready()
        clock.advance(reg.xyz_conversion_s(200) * 0.99)
        assert not driver.data_ready()
        clock.advance(reg.xyz_conversion_s(200) * 0.02)
        assert driver.data_ready()

    def test_reading_results_clears_drdy_and_early_reads_flag_nack2(self):
        _, model, driver, clock = rig()
        assert not hshake(model) & NACK2
        driver.single_measurement()
        assert not driver.data_ready()
        assert not hshake(model) & NACK2
        driver.read_result()  # DRDY is low: stale data, NACK2 set
        assert hshake(model) & NACK2

    def test_single_poll_can_measure_one_axis(self):
        _, model, driver, clock = rig()
        before = bytes(model.results)
        model.transfer(0x20, b"\x00\x40")  # Z only
        clock.advance(reg.axis_conversion_s(200) * 1.01)
        assert driver.data_ready()
        after = bytes(model.results)
        assert after[:6] == before[:6] and after[6:] != before[6:]

    def test_saturates_at_the_sensor_range(self):
        _, _, driver, clock = rig({"sensor": {"hard_iron_nt": [5_000_000, 0, 0]}})
        m = driver.single_measurement()
        assert m.x_nt == pytest.approx(physics.SENSOR_RANGE_NT, rel=0.001)

    def test_mounting_reaches_the_registers(self):
        scenario, _, driver, clock = rig({"sensor": {"mounting": {"roll_deg": 180}}, **QUIET})
        values = [s.z_nt for s in measure_many(driver, clock, 200)]
        # +Z points up, so the sensor reads the (downward) vertical field negated.
        assert statistics.fmean(values) == pytest.approx(-scenario.field_model(START)[2], abs=10)

    def test_a_zero_cycle_count_reads_zero_instead_of_failing(self):
        # UM16 p.30 allows 0; the model must survive whatever a client writes.
        _, model, driver, clock = rig()
        model.transfer(0x20, b"\x04" + b"\x00\x00" * 3)
        model.transfer(0x20, b"\x00\x70")
        clock.advance(0.01)
        assert driver.data_ready()
        assert model.transfer(0x20, b"\x24", 9) == bytes(9)


class TestContinuousMode:
    def test_runs_at_the_tmrc_rate(self):
        _, _, driver, clock = rig()
        driver.start_continuous(0x96)  # ~37.5 Hz
        got = 0
        for _ in range(4000):  # one second in 0.25 ms steps
            clock.advance(0.00025)
            if driver.read_if_ready() is not None:
                got += 1
        assert got == pytest.approx(37, abs=1)

    def test_rate_is_capped_by_conversion_time(self):
        _, _, driver, clock = rig()
        driver.start_continuous(0x92)  # asks 600 Hz; 200 cycles allow ~147
        got = 0
        for _ in range(8000):
            clock.advance(0.000125)
            if driver.read_if_ready() is not None:
                got += 1
        assert got == pytest.approx(reg.max_xyz_rate_hz(200), abs=2)

    def test_reading_cmm_ends_continuous_mode(self):
        _, model, driver, clock = rig()
        driver.start_continuous(0x96)
        assert model.continuous
        model.transfer(0x20, b"\x01", 1)
        assert not model.continuous

    def test_writing_tmrc_ends_continuous_mode(self):
        _, model, driver, _ = rig()
        driver.start_continuous(0x96)
        model.transfer(0x20, b"\x0b\x98")
        assert not model.continuous

    def test_poll_during_continuous_is_nacked_and_ignored(self):
        # UM16 §4.5.1: the chip NACKs a write it cannot carry out, POLL during CMM among them.
        _, model, driver, clock = rig()
        driver.start_continuous(0x96)
        assert not hshake(model) & NACK1
        refused(model, b"\x00\x70")
        assert hshake(model) & NACK1
        assert model.continuous and model.poll == 0x00 and model._pending is None

    def test_self_test_poll_during_continuous_is_nacked_and_never_runs(self):
        # What a driver that forgets to stop continuous mode first runs into.
        _, model, driver, clock = rig()
        driver.start_continuous(0x96)
        model.transfer(0x20, b"\x33\x8f")  # BIST armed, which is allowed
        refused(model, b"\x00\x70")
        clock.advance(0.05)
        assert model.transfer(0x20, b"\x33", 1) == b"\x8f"  # no pass bits: the test never ran
        assert model.stats.writes_refused == 1

    def test_cmm_write_during_a_poll_is_nacked_and_the_poll_completes(self):
        _, model, driver, clock = rig()
        model.transfer(0x20, b"\x00\x70")
        refused(model, b"\x01\x79")
        assert hshake(model) & NACK1
        assert not model.continuous and model.cmm == 0x00
        clock.advance(reg.xyz_conversion_s(200) * 1.01)
        assert driver.data_ready()
        driver.stop_continuous()  # the POLL is done, so CMM may be written again

    def test_stop(self):
        _, model, driver, _ = rig()
        driver.start_continuous(0x96)
        driver.stop_continuous()
        assert not model.continuous


class TestSelfTest:
    def test_healthy_sensor_passes(self):
        _, model, driver, _ = rig()
        assert driver.self_test().passed
        assert model.bist == 0x00  # left off

    def test_dead_coil_fails_that_axis_and_reads_zero(self):
        _, _, driver, _ = rig({"sensor": {"dead_axis": "z"}})
        result = driver.self_test()
        assert (result.x_ok, result.y_ok, result.z_ok) == (True, True, False)
        assert driver.single_measurement().z_counts == 0


class TestFaults:
    def test_nack_burst(self):
        _, model, driver, clock = rig({"faults": [{"type": "nack", "at": "0s", "duration": "10s", "probability": 1.0}]})
        with pytest.raises(OSError) as info:
            driver.single_measurement()
        assert info.value.errno == EREMOTEIO
        assert model.stats.nacks_injected == 1

    def test_partial_nack_rate(self):
        _, model, _, _ = rig({"faults": [{"type": "nack", "at": "0s", "duration": "1h", "probability": 0.3}]})
        failures = 0
        for _ in range(2000):
            try:
                model.transfer(0x20, b"\x36", 1)
            except OSError:
                failures += 1
        assert failures / 2000 == pytest.approx(0.3, abs=0.04)

    def test_disconnect_then_power_cycled(self):
        _, model, driver, clock = rig({"faults": [{"type": "disconnect", "at": "+5s", "duration": "5s"}]})
        driver.set_cycle_count(400)
        clock.advance(6)
        with pytest.raises(OSError):
            driver.probe()
        clock.advance(5)
        driver.probe()
        assert driver.read_cycle_counts() == (200, 200, 200)  # our 400 is gone
        assert model.stats.power_cycles == 1

    def test_a_disconnect_no_transfer_saw_still_power_cycles(self):
        # Half a second off the bus between two samples a second apart.
        _, model, driver, clock = rig({"faults": [{"type": "disconnect", "at": "+5.2s", "duration": "0.5s"}]})
        driver.set_cycle_count(400)
        clock.advance(5.0)
        driver.probe()
        assert driver.read_cycle_counts() == (400, 400, 400)
        clock.advance(1.0)
        assert driver.read_cycle_counts() == (200, 200, 200)
        assert model.stats.power_cycles == 1 and model.stats.disconnected_transfers == 0

    def test_every_disconnect_is_a_power_cycle(self):
        faults = [{"type": "disconnect", "at": "+1s", "every": "2s", "count": 3, "duration": "0.5s"}]
        _, model, driver, clock = rig({"faults": faults})
        clock.advance(10)
        driver.probe()
        assert model.stats.power_cycles == 3
        driver.probe()
        assert model.stats.power_cycles == 3  # each one counted once

    def test_brownout_resets_continuous_mode_without_a_failed_transfer(self):
        _, model, driver, clock = rig({"faults": [{"type": "brownout", "at": "+2s"}]})
        driver.set_cycle_count(400)
        driver.start_continuous(0x96)
        got_before = got_after = 0
        for _ in range(16_000):  # four seconds in 0.25 ms steps, never an OSError
            clock.advance(0.00025)
            if driver.data_ready():
                driver.read_result()
                if model.clock.epoch(clock.now) < START + 2:
                    got_before += 1
                else:
                    got_after += 1
        assert got_before > 50 and got_after == 0
        assert not model.continuous and model.cycle_counts == [200, 200, 200]
        assert model.stats.power_cycles == 1

    def test_brownout_during_a_conversion_loses_it(self):
        _, model, driver, clock = rig({"faults": [{"type": "brownout", "at": "+10s"}]})
        clock.advance(10 - 0.002)
        model.transfer(0x20, b"\x00\x70")  # POLL two milliseconds before the dip
        clock.advance(0.05)
        assert not driver.data_ready()
        assert model.stats.power_cycles == 1 and model.stats.measurements == 0
        model.transfer(0x20, b"\x00\x70")  # and the chip measures again afterwards
        clock.advance(0.05)
        assert driver.data_ready()

    def test_stuck_drdy_times_out(self):
        _, _, driver, _ = rig({"faults": [{"type": "stuck_drdy", "at": "0s", "duration": "1h"}]})
        with pytest.raises(DataReadyTimeout):
            driver.single_measurement()


class TestReproducibility:
    def run(self, seed):
        _, _, driver, clock = rig(seed=seed)
        return [(s.x_counts, s.y_counts, s.z_counts) for s in measure_many(driver, clock, 50, period=1.0)]

    def test_same_seed_same_samples(self):
        assert self.run(1) == self.run(1)

    def test_different_seed_different_noise(self):
        assert self.run(1) != self.run(2)


class TestRegisterEdges:
    def test_hshake_resets_to_the_manuals_0x1b(self):
        # NACK0 is already set in the manual's reset value (Table 5-1), so
        # only the wire can show whether a write was refused for it.
        _, model, _, _ = rig()
        assert hshake(model) == 0x1B

    def test_undefined_register_write_is_nacked(self):
        _, model, _, _ = rig()
        refused(model, b"\x50\x01")
        assert hshake(model) & NACK0
        assert model.stats.writes_refused == 1

    @pytest.mark.parametrize("register", [0x02, 0x03, 0x0C, 0x23, 0x24, 0x2D, 0x34, 0x36, 0x37, 0x7F])
    def test_every_register_without_a_write_is_nacked(self, register):
        _, model, _, _ = rig()
        refused(model, bytes([register, 0x00]))

    def test_bytes_before_a_refused_one_take_effect(self):
        _, model, _, _ = rig()
        refused(model, b"\x0b\x98\x01")  # TMRC, then undefined 0x0C
        assert model.tmrc == 0x98

    def test_nack_bits_stay_set_until_a_power_cycle(self):
        _, model, driver, clock = rig({"faults": [{"type": "brownout", "at": "+1s"}]})
        driver.start_continuous(0x96)
        refused(model, b"\x00\x70")
        model.transfer(0x20, b"\x35\x03")  # writing HSHAKE leaves its NACK bits alone
        assert hshake(model) & NACK1
        clock.advance(2)
        assert hshake(model) == 0x1B

    def test_register_write_clears_drdy(self):
        _, model, driver, clock = rig()
        model.transfer(0x20, b"\x00\x70")
        clock.advance(0.05)
        assert driver.data_ready()
        model.transfer(0x20, b"\x0b\x96")  # any write, DRC0
        assert not driver.data_ready()

    def test_drdy_clears_with_the_first_data_byte_even_if_it_is_refused(self):
        # DRC0: a write clears DRDY when its first data byte arrives, before
        # that byte is taken or refused.
        _, model, driver, clock = rig()
        model.transfer(0x20, b"\x00\x70")
        clock.advance(0.05)
        refused(model, b"\x50\x01")
        assert not driver.data_ready()

    def test_a_pointer_only_write_leaves_drdy_alone(self):
        # The first half of every register read, STATUS included: if it
        # cleared DRDY, polling STATUS could never see it rise.
        _, model, _, clock = rig()
        model.transfer(0x20, b"\x00\x70")
        clock.advance(0.05)
        model.transfer(0x20, b"\x34")
        assert model.transfer(0x20, b"", 1)[0] & reg.STATUS_DRDY

    def test_read_continues_from_the_pointer(self):
        _, model, _, _ = rig()
        model.transfer(0x20, b"\x36")  # STOP-separated framing: pointer first
        assert model.transfer(0x20, b"", 1) == b"\x22"

    def test_invalid_tmrc_write_is_ignored(self):
        _, model, _, _ = rig()
        model.transfer(0x20, b"\x0b\x05")
        assert model.tmrc == 0x96

    def test_revid_and_unknown_reads(self):
        _, model, _, _ = rig()
        assert model.transfer(0x20, b"\x36", 1) == b"\x22"
        assert model.transfer(0x20, b"\x60", 2) == b"\x00\x00"


class TestModelYears:
    def test_a_warning_once_simulated_time_leaves_wmm2025(self, caplog):
        # serve runs as long as it is left to; past 2030.0 the main field is
        # extrapolated, and the log should say so, once.
        start = datetime(2029, 12, 31, 23, 59, tzinfo=timezone.utc).timestamp()
        clock = FakeClock()
        model = RM3100Model(sc.from_dict(SITE, start=start, seed=5), SimClock(start, monotonic=clock.monotonic))
        with caplog.at_level(logging.WARNING, logger="rm3100_sim.device"):
            model.transfer(0x20, b"\x36", 1)
            clock.advance(59.9)
            model.transfer(0x20, b"\x36", 1)
            assert not caplog.records
            for _ in range(3):
                clock.advance(30)
                model.transfer(0x20, b"\x36", 1)
        assert [r.levelname for r in caplog.records if "WMM2025" in r.getMessage()] == ["WARNING"]


class TestClock:
    @pytest.mark.parametrize("speed", [0.0, -1.0, float("nan"), float("inf")])
    def test_speed_must_be_a_positive_number(self, speed):
        with pytest.raises(ValueError, match="speed"):
            SimClock(START, speed=speed)
