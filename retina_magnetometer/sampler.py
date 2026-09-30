"""The acquisition thread: find the sensor, configure it, sample it, recover.

It never exits on a hardware problem. A missing bus, a missing sensor, a run
of NACKs or a sensor that was power-cycled behind its back are all states it
reports through ``Health`` and retries out of with a capped backoff; stopping
is only ever the app asking it to.

Poll mode (the default) samples on a grid aligned to the wall clock — at
1 Hz, on the second — so timestamps from different nodes line up. Each sample
is stamped at the middle of its conversion. Continuous mode uses the nearest
TMRC rate and stamps each sample when it is read.

The sensor's registers are checked once a minute. A brief power dip resets
the chip to 200 cycles without a single failed transfer, and every value read
after it would carry the wrong gain without anything else noticing.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from retina_magnetometer.config import Config
from retina_magnetometer.health import Health
from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.bus import I2CBus, open_bus
from retina_magnetometer.rm3100.driver import RM3100, Measurement, RM3100Error, find_rm3100

log = logging.getLogger(__name__)

# Consecutive failed samples before the sensor is dropped and found again.
REINIT_AFTER = 3
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
VERIFY_EVERY_S = 60.0


class Sampler:
    def __init__(
        self,
        config: Config,
        health: Health,
        on_sample: Callable[[int, float, float, float], None],
        *,
        on_session: Callable[..., None] | None = None,
        bus_opener: Callable[..., I2CBus] = open_bus,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] | None = None,
        sensor_sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.health = health
        self.on_sample = on_sample
        self.on_session = on_session
        self._open_bus = bus_opener
        self._clock = clock
        self._monotonic = monotonic
        self._stop = threading.Event()
        # Waits between samples and between retries go through the stop event,
        # so stopping interrupts them. The driver's DRDY polling sleeps are a
        # millisecond long and use plain sleep. Tests substitute both with a
        # fake clock.
        self._wait = wait or (lambda seconds: self._stop.wait(max(0.0, seconds)))
        self._sensor_sleep = sensor_sleep
        self._bus: I2CBus | None = None
        self._sensor: RM3100 | None = None
        self._backoff = BACKOFF_MIN_S
        self._consecutive = 0
        self._last_verify = 0.0
        self._thread: threading.Thread | None = None
        self.period = 1.0 / config.sample_rate_hz
        self.tmrc = reg.tmrc_for_rate(config.sample_rate_hz)

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(target=self.run, name="sampler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._drop_sensor()

    def run(self) -> None:
        if self.config.errors:
            log.error("not sampling: %s", "; ".join(self.config.errors))
            return
        while not self._stop.is_set():
            self.step()

    def step(self) -> None:
        """One unit of work: find the sensor, or take (or wait for) a sample."""
        if self._sensor is None and not self._acquire():
            self._wait(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
            return
        try:
            if self._monotonic() - self._last_verify >= VERIFY_EVERY_S:
                self._verify()
                if self._sensor is None:
                    return
            if self.config.mode == "poll":
                self._poll_once()
            else:
                self._continuous_once()
        except (OSError, RM3100Error) as exc:
            self._failed(exc)

    # ── Finding and configuring the sensor ───────────────────────────────────

    def _acquire(self) -> bool:
        try:
            bus = self._open_bus(self.config.bus, repeated_start=self.config.repeated_start)
        except FileNotFoundError:
            self.health.no_bus(
                f"{self.config.bus} does not exist. On a Pi, enable I2C (dtparam=i2c_arm=on in config.txt, "
                "and load i2c-dev) and pass the device into the container."
            )
            return False
        except PermissionError:
            self.health.no_bus(f"{self.config.bus} exists but this container may not open it (device permissions).")
            return False
        except (OSError, ValueError) as exc:
            self.health.no_bus(f"{self.config.bus}: {exc}")
            return False
        try:
            if self.config.i2c_address is None:
                sensor = find_rm3100(bus, clock=self._monotonic, sleep=self._sensor_sleep)
            else:
                sensor = RM3100(bus, self.config.i2c_address, clock=self._monotonic, sleep=self._sensor_sleep)
                sensor.probe()
            revid = sensor.read_revid()
            if self.config.self_test:
                result = sensor.self_test()
                self.health.self_test_result(
                    passed=result.passed,
                    ran=result.ran,
                    x_ok=result.x_ok,
                    y_ok=result.y_ok,
                    z_ok=result.z_ok,
                    raw=result.raw,
                )
                if not result.passed:
                    log.warning("self test failed: BIST=0x%02X", result.raw)
            sensor.set_cycle_count(self.config.cycle_count)
            if self.config.mode == "continuous":
                sensor.start_continuous(self.tmrc)
                effective = reg.effective_continuous_rate_hz(self.tmrc, self.config.cycle_count)
            else:
                # A previous run (or another program) may have left continuous
                # mode on, and a POLL is ignored while it runs.
                sensor.stop_continuous()
                effective = self.config.sample_rate_hz
        except (OSError, RM3100Error) as exc:
            where = "at any of 0x20-0x23" if self.config.i2c_address is None else f"at 0x{self.config.i2c_address:02X}"
            self.health.no_sensor(bus.description, f"No RM3100 answered {where} on {bus.description}: {exc}")
            try:
                bus.close()
            except OSError:
                pass
            return False
        self._bus, self._sensor = bus, sensor
        self._backoff = BACKOFF_MIN_S
        self._consecutive = 0
        self._last_verify = self._monotonic()
        gain = reg.gain_lsb_per_ut(self.config.cycle_count)
        self.health.sensor_ready(
            bus=bus.description,
            address=sensor.address,
            revid=revid,
            cycle_count=self.config.cycle_count,
            gain=gain,
            effective_rate_hz=effective,
        )
        log.info(
            "RM3100 at 0x%02X on %s: %d cycles, %s mode at %.3g Hz",
            sensor.address,
            bus.description,
            self.config.cycle_count,
            self.config.mode,
            effective,
        )
        if self.on_session is not None:
            self.on_session(
                cycle_count=self.config.cycle_count,
                gain=gain,
                rate_hz=effective,
                mode=self.config.mode,
                bus=bus.description,
                address=sensor.address,
            )
        return True

    def _drop_sensor(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except OSError:
                pass
        self._bus = None
        self._sensor = None

    def _verify(self) -> None:
        self._last_verify = self._monotonic()
        counts = self._sensor.read_cycle_counts()
        if counts != (self.config.cycle_count,) * 3:
            self.health.error(
                "sensor reset", f"cycle counts read {counts}, expected {self.config.cycle_count}; reconfiguring"
            )
            log.warning("sensor registers changed under us (%s); reconfiguring", counts)
            self._drop_sensor()

    # ── Sampling ─────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        now = self._clock()
        next_tick = (int(now / self.period) + 1) * self.period
        self._wait(next_tick - now)
        if self._stop.is_set():
            return
        started = self._clock()
        measurement = self._sensor.single_measurement()
        conversion = reg.xyz_conversion_s(measurement.cycle_count)
        self._deliver(started + conversion / 2.0, measurement)

    def _continuous_once(self) -> None:
        measurement = self._sensor.read_if_ready()
        if measurement is None:
            self._wait(min(self.period / 4.0, 0.05))
            return
        self._deliver(self._clock(), measurement)

    def _deliver(self, t: float, m: Measurement) -> None:
        x, y, z = m.x_nt, m.y_nt, m.z_nt
        self._consecutive = 0
        self.health.sample(t, x, y, z)
        self.on_sample(int(round(t * 1000)), x, y, z)

    def _failed(self, exc: Exception) -> None:
        kind = "timeout" if "DRDY" in str(exc) else ("I2C error" if isinstance(exc, OSError) else "sensor error")
        self.health.error(kind, str(exc))
        self._consecutive += 1
        log.warning("sample failed (%d in a row): %s", self._consecutive, exc)
        if self._consecutive >= REINIT_AFTER:
            log.warning("dropping the sensor after %d failures; will look for it again", self._consecutive)
            self._drop_sensor()
            self._wait(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
