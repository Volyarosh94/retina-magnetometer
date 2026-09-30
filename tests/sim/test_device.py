"""The app's driver against the register model: the chip's behaviour end to end.

Time is a FakeClock shared by the driver (its sleeps advance it) and the chip
(its conversions complete by it), so the datasheet timing runs exactly and
instantly.
"""

import statistics

import pytest
from fakes import FakeClock, ModelBus

from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.driver import RM3100, DataReadyTimeout, find_rm3100
from rm3100_sim import physics
from rm3100_sim import scenario as sc
from rm3100_sim.device import EREMOTEIO, RM3100Model, SimClock

START = 1_790_726_400.0  # 2026-09-30T00:00:00Z
SITE = {"site": {"latitude": 34.85, "longitude": -82.39, "altitude_m": 300}}


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
        scenario, _, driver, clock = rig({"field": {"diurnal": {"enabled": False}}})
        samples = measure_many(driver, clock, 1500)
        expected = scenario.sensor_field(START + 75)
        sigma = reg.noise_nt(200)
        for axis, name in enumerate(("x_nt", "y_nt", "z_nt")):
            values = [getattr(s, name) for s in samples]
            assert statistics.fmean(values) == pytest.approx(expected[axis], abs=4 * sigma / len(values) ** 0.5 + 1)
            # Quantisation (13.3 nT steps) is part of the datasheet's 15 nT.
            assert statistics.pstdev(values) == pytest.approx((sigma**2 + reg.lsb_nt(200) ** 2 / 12) ** 0.5, rel=0.08)

    def test_higher_cycle_count_is_quieter(self):
        _, _, driver, clock = rig({"field": {"diurnal": {"enabled": False}}})
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
        driver.single_measurement()
        assert not driver.data_ready()
        driver.read_result()  # DRDY is low: stale data, NACK2 set
        assert model.transfer(0x20, b"\x35", 1)[0] & 0x40

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
        scenario, _, driver, clock = rig(
            {"sensor": {"mounting": {"roll_deg": 180}}, "field": {"diurnal": {"enabled": False}}}
        )
        values = [s.z_nt for s in measure_many(driver, clock, 200)]
        # +Z points up, so the sensor reads the (downward) vertical field negated.
        assert statistics.fmean(values) == pytest.approx(-scenario.field_model(START)[2], abs=10)


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

    def test_poll_during_continuous_is_ignored_with_nack1(self):
        _, model, driver, _ = rig()
        driver.start_continuous(0x96)
        model.transfer(0x20, b"\x00\x70")
        assert model.transfer(0x20, b"\x35", 1)[0] & 0x20

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
    def test_undefined_register_write_sets_nack0(self):
        _, model, _, _ = rig()
        model.transfer(0x20, b"\x50\x01")
        assert model.transfer(0x20, b"\x35", 1)[0] & 0x10

    def test_register_write_clears_drdy(self):
        _, model, driver, clock = rig()
        model.transfer(0x20, b"\x00\x70")
        clock.advance(0.05)
        assert driver.data_ready()
        model.transfer(0x20, b"\x0b\x96")  # any write, DRC0
        assert not driver.data_ready()

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
