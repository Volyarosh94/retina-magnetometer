"""The sampler against the chip model: sampling, health, and every recovery path.

The node app must never import the simulator (it ships without it in spirit,
and "no code changes" means the app cannot know it is simulated). The tests
may: they are where the two meet.
"""

import ast
import pathlib

import pytest
from fakes import BrokenBus, FakeClock, ModelBus

from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health
from retina_magnetometer.sampler import REINIT_AFTER, Sampler
from rm3100_sim import scenario as sc
from rm3100_sim.device import RM3100Model, SimClock

START = 1_790_726_400.0


class Rig:
    """Sampler + health + a chip model, all on one fake clock."""

    def __init__(self, scenario_extra=None, **config):
        self.clock = FakeClock(START)
        values = {"MAGNETOMETER_SAMPLE_RATE_HZ": "1"}
        values.update({f"MAGNETOMETER_{k}": str(v) for k, v in config.items()})
        self.config = from_env(values)
        self.scenario = sc.from_dict(
            {"site": {"latitude": 34.85, "longitude": -82.39}, **(scenario_extra or {})}, start=START, seed=1
        )
        self.model = RM3100Model(self.scenario, SimClock(START, monotonic=self.clock.monotonic))
        self.samples = []
        self.sessions = []
        self.health = Health(self.config, clock=self.clock.monotonic)
        self.bus_opens = 0
        self.sampler = Sampler(
            self.config,
            self.health,
            lambda *s: self.samples.append(s),
            on_session=lambda **kw: self.sessions.append(kw),
            bus_opener=self.open_bus,
            clock=self.clock.monotonic,
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
    assert len(rig.samples) == pytest.approx(4 * 37.5, abs=3)


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


def test_no_sensor_is_a_state_not_a_crash():
    rig = Rig()
    rig.sampler._open_bus = lambda spec, repeated_start=True: BrokenBus(121)
    rig.run(5)
    snap = rig.health.snapshot()
    assert snap["state"] == "no_sensor"
    assert "0x20-0x23" in snap["detail"]


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


def test_nack_burst_is_counted_and_recovered_from():
    rig = Rig({"faults": [{"type": "nack", "at": "+5s", "duration": "3s", "probability": 1.0}]})
    rig.run(20)
    snap = rig.health.snapshot()
    assert snap["read_errors_total"] >= REINIT_AFTER
    assert snap["state"] == "ok"  # recovered
    assert snap["recent_errors"][0]["kind"] == "I2C error"
    assert snap["reinitialisations"] >= 1


def test_power_cycled_sensor_is_reconfigured():
    rig = Rig({"faults": [{"type": "disconnect", "at": "+5s", "duration": "3s"}]}, CYCLE_COUNT=400)
    rig.run(30)
    assert rig.model.stats.power_cycles == 1
    assert rig.model.cycle_counts == [400, 400, 400]  # set again after the reset


def test_silent_register_reset_is_caught_by_verification():
    rig = Rig(CYCLE_COUNT=400)
    rig.run(5)
    rig.model._power_on_reset()  # a brown-out that no transfer noticed
    rig.run(70)
    assert rig.model.cycle_counts == [400, 400, 400]
    assert any(e["kind"] == "sensor reset" for e in rig.health.snapshot()["recent_errors"])


def test_stuck_drdy_is_a_timeout_error():
    rig = Rig({"faults": [{"type": "stuck_drdy", "at": "+3s", "duration": "2s"}]})
    rig.run(10)
    kinds = {e["kind"] for e in rig.health.snapshot()["recent_errors"]}
    assert "timeout" in kinds


def test_failed_self_test_degrades_but_keeps_sampling():
    rig = Rig({"sensor": {"dead_axis": "y"}})
    rig.run(5)
    snap = rig.health.snapshot()
    assert snap["state"] == "degraded"
    assert "Self test failed on Y" in snap["detail"]
    assert rig.samples


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
