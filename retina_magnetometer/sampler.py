"""The acquisition thread: find the sensor, configure it, sample it, recover.

With a valid configuration it runs until the app stops it (with a
configuration error it does not sample at all). A missing bus, a missing
sensor, a run of NACKs or a sensor that was power-cycled behind its back are
all states it reports through ``Health`` and retries out of with a capped
backoff. Anything it did not expect (a bug, a callback that raised) is
reported and retried the same way, because a dead thread would stop sampling
for good while the page went on serving. The backoff starts again from its
shortest only once a sample has got all the way through.

Poll mode (the default) samples on a grid aligned to the wall clock — at
1 Hz, on the second — so timestamps from different nodes line up. Each sample
is stamped at the middle of its conversion. Continuous mode uses the nearest
TMRC rate and stamps each sample when it is read.

A brief power dip resets the chip to 200 cycles without a single failed
transfer, and in poll mode it goes on measuring, so every value read after it
would carry the wrong gain without anything else noticing. No sample is
therefore delivered until what the app set (the cycle counts, and TMRC), read
back after it was measured, still reads as set, and in continuous mode until a
later DRDY shows continuous mode was still running when it was read; a reset
found that way drops every sample since the last good read-back and finds the
sensor again. The same dip ends continuous mode; there, DRDY staying low for a
few sample periods counts as a failed read (``RM3100.read_if_ready``), and
three of those find the sensor again.
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
from retina_magnetometer.rm3100.driver import RM3100, DataReadyTimeout, Measurement, RM3100Error, find_rm3100

log = logging.getLogger(__name__)

# Consecutive failed samples before the sensor is dropped and found again.
REINIT_AFTER = 3
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 30.0
# The longest a sample waits for the read-back of the registers that lets it
# be delivered. Up to 1 Hz that is a read-back after every sample; faster, the
# samples of up to a second share one. The read-back (the cycle counts and
# TMRC) takes 1.2 ms of a 100 kHz bus: after every sample it would add two
# thirds to a poll-mode measurement's own transfers and take up to 10 % off
# poll mode's top rate, and in continuous mode at 147 Hz nearly a fifth of the
# bus; once a second it costs neither.
READ_BACK_EVERY_S = 1.0
# Poll mode has no use for TMRC, the continuous-mode rate, so it sets it to a
# value the chip never powers up with (0x96, UM16 Table 5-1): a read-back then
# tells a chip that was reset from one still configured even at the default
# cycle count, where the counts read the same either way.
POLL_MODE_TMRC = reg.TMRC_MAX


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
        # Samples measured and not yet delivered, (time, measurement), waiting
        # for the next read-back of the registers; and since when (monotonic).
        self._held: list[tuple[float, Measurement]] = []
        self._held_since = 0.0
        # TMRC as it read back once the chip was configured.
        self._tmrc_set = reg.TMRC_DEFAULT
        # Poll mode: the monotonic time of the tick the last measurement was
        # taken on, so the next one can tell whether it ran past any ticks,
        # and how long that measurement took.
        self._tick_monotonic: float | None = None
        self._measurement_s = 0.0
        # The last unexpected error, and the last failure to record a session,
        # so that each traceback is logged once rather than at every retry.
        self._unexpected: str | None = None
        self._session_failure: str | None = None
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
        # Not while a sampler that did not finish in time may still be using
        # the chip and the samples it holds.
        if self._thread is None or not self._thread.is_alive():
            # Nothing measured is lost on a clean stop: what is held is read
            # back and delivered first, in continuous mode once the next
            # DRDY has confirmed the newest sample.
            confirmed = self.config.mode == "continuous" and self._sensor is not None and self._next_drdy()
            self._settle_held(newest_confirmed=confirmed)
            if self._sensor is not None and self.config.mode == "continuous":
                # Leave the chip idle rather than measuring for nobody. The
                # next start copes either way, but another program on the bus
                # during bring-up should find it out of continuous mode, as
                # poll mode leaves it (the cycle count and TMRC stay as set).
                try:
                    self._sensor.stop_continuous()
                except (OSError, RM3100Error) as exc:
                    log.warning("could not stop continuous mode: %s", exc)
        self._drop_sensor()

    def run(self) -> None:
        if self.config.errors:
            log.error("not sampling: %s", "; ".join(self.config.errors))
            return
        while not self._stop.is_set():
            try:
                self.step()
            except Exception as exc:
                # step() handles every failure of the bus and the sensor; this
                # is for anything else. Start again from finding the sensor,
                # whose state is unknown after it, say so on the page, and back
                # off as for any failure: finding the sensor again is no sign
                # that whatever failed after it will not fail again.
                what = f"{type(exc).__name__}: {exc}"
                if what != self._unexpected:
                    self._unexpected = what
                    log.exception("sampler: unexpected error; finding the sensor again")
                else:
                    log.error("sampler: %s, again; finding the sensor again in %.0f s", what, self._backoff)
                self.health.internal_error(what)
                if self._held:
                    # Whatever failed may be what delivers them.
                    log.warning("%d held samples dropped after the unexpected error", len(self._held))
                    self._held = []
                self._drop_sensor()
                self._wait(self._backoff)
                self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)

    def step(self) -> None:
        """One unit of work: find the sensor, or take (or wait for) a sample."""
        if self._sensor is None and not self._acquire():
            self._wait(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
            return
        try:
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
        # Held from here on, so that however acquiring fails, dropping the
        # sensor closes the bus.
        self._bus = bus
        try:
            if self.config.i2c_address is None:
                sensor = find_rm3100(bus, clock=self._monotonic, sleep=self._sensor_sleep)
            else:
                sensor = RM3100(bus, self.config.i2c_address, clock=self._monotonic, sleep=self._sensor_sleep)
                sensor.probe()
            revid = sensor.read_revid()
            # Stop whatever the chip was doing before configuring it. A
            # previous run (or another program) may have left continuous mode
            # on, and while it runs a POLL is NACKed and ignored (UM16 p.28,
            # p.31): the self test needs one, and so does every poll-mode
            # sample. The self test and start_continuous make sure again.
            sensor.stop_continuous()
            # The cycle count goes in before the self test, whose wait is
            # sized for the count the chip is at.
            sensor.set_cycle_count(self.config.cycle_count)
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
            if self.config.mode == "continuous":
                sensor.start_continuous(self.tmrc)
                effective = reg.effective_continuous_rate_hz(self.tmrc, self.config.cycle_count)
            else:
                sensor.set_tmrc(POLL_MODE_TMRC)
                effective = self.config.sample_rate_hz
            # What the read-backs compare with: TMRC as the chip holds it now,
            # so that one which does not keep the value written is no reset.
            tmrc_set = sensor.read_tmrc()
        except (OSError, RM3100Error) as exc:
            where = "at any of 0x20-0x23" if self.config.i2c_address is None else f"at 0x{self.config.i2c_address:02X}"
            self.health.no_sensor(bus.description, f"No RM3100 answered {where} on {bus.description}: {exc}")
            self._drop_sensor()
            return False
        self._sensor = sensor
        self._consecutive = 0
        self._tick_monotonic = None
        self._held = []
        self._tmrc_set = tmrc_set
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
        self._record_session(
            cycle_count=self.config.cycle_count,
            gain=gain,
            rate_hz=effective,
            mode=self.config.mode,
            bus=bus.description,
            address=sensor.address,
        )
        return True

    def _record_session(self, **session) -> None:
        if self.on_session is None:
            return
        try:
            self.on_session(**session)
        except Exception as exc:
            # The recorder queues the session and writes it with its own
            # storage operations, whose failures it reports itself, so an
            # exception here is a bug in the app rather than a storage
            # problem. It must not stop the sampling the session describes.
            what = f"{type(exc).__name__}: {exc}"
            if what != self._session_failure:
                self._session_failure = what
                log.exception("could not record the session")
            else:
                log.error("could not record the session: %s, again", what)
            self.health.internal_error(f"session record failed: {what}")

    def _drop_sensor(self) -> None:
        if self._bus is not None:
            try:
                self._bus.close()
            except OSError:
                pass
        self._bus = None
        self._sensor = None

    # ── Reading the registers back ───────────────────────────────────────────

    def _read_back(self, *, newest_confirmed: bool = False) -> None:
        """Read back what configuring the chip set, and settle the samples
        held until now.

        If the cycle counts and TMRC still read as set, the samples are
        delivered. In continuous mode the newest one waits for the next, or
        for ``newest_confirmed``: a reset between the STATUS read that saw
        DRDY and the read of the results hands over the reset registers, all
        zeros (UM16 Table 5-1), and with every register the app sets at its
        reset value (200 cycles, TMRC 0x96) only a DRDY after it shows that
        continuous mode was still running. CMM would say, but reading it ends
        continuous mode (UM16 p.31).

        If anything reads otherwise, the chip was reset since the last
        read-back, when within that span is not known: all of them are
        dropped and the sensor found again. A failing read raises, and the
        samples wait for the next read-back.
        """
        counts = self._sensor.read_cycle_counts()
        tmrc = self._sensor.read_tmrc()
        if counts == (self.config.cycle_count,) * 3 and tmrc == self._tmrc_set:
            waiting = 1 if self.config.mode == "continuous" and not newest_confirmed else 0
            ready, self._held = self._held[: len(self._held) - waiting], self._held[len(self._held) - waiting :]
            for t, m in ready:
                self._deliver(t, m)
            return
        held, self._held = self._held, []
        changed = []
        if counts != (self.config.cycle_count,) * 3:
            changed.append(f"cycle counts read {counts}, expected {self.config.cycle_count}")
        if tmrc != self._tmrc_set:
            changed.append(f"TMRC read 0x{tmrc:02X}, expected 0x{self._tmrc_set:02X}")
        dropped = f"{len(held)} sample{'' if len(held) == 1 else 's'}"
        self.health.error(
            "sensor reset",
            f"{'; '.join(changed)}: the chip was reset. {dropped} measured since the last good read-back "
            "dropped, as the reset may have come before any of them; reconfiguring",
        )
        log.warning("sensor registers changed under us (%s): %s dropped; reconfiguring", "; ".join(changed), dropped)
        self._drop_sensor()

    def _settle_held(self, *, newest_confirmed: bool = False) -> None:
        """Before letting go of the sensor: a last read-back for the samples
        it still holds, or, if the chip does not answer, dropping them. What
        the read-back leaves unconfirmed is dropped too: nothing will confirm
        it now."""
        if not self._held:
            return
        if self._sensor is None:
            log.warning("%d held samples dropped: no sensor to read the registers back from", len(self._held))
        else:
            try:
                self._read_back(newest_confirmed=newest_confirmed)
            except (OSError, RM3100Error) as exc:
                log.warning("%d held samples dropped: the registers could not be read back (%s)", len(self._held), exc)
            else:
                if self._held:
                    log.info("the newest sample dropped: no DRDY after it showed continuous mode still running")
        self._held = []

    def _next_drdy(self) -> bool:
        """Continuous mode, at a clean stop: whether DRDY rises within one
        sample period (at most a second), confirming the newest sample."""
        rate = reg.effective_continuous_rate_hz(self.tmrc, self.config.cycle_count)
        deadline = self._monotonic() + min(1.0 / rate + RM3100.DRDY_MARGIN_S, 1.0)
        try:
            while not self._sensor.data_ready():
                if self._monotonic() >= deadline:
                    return False
                self._sensor_sleep(RM3100.DRDY_POLL_S)
        except (OSError, RM3100Error):
            return False
        return True

    # ── Sampling ─────────────────────────────────────────────────────────────

    def _poll_once(self) -> None:
        now = self._clock()
        if self._tick_monotonic is not None:
            # Ticks that went by while the sampler was still busy with the
            # last sample are lost. Counted on the monotonic clock, so that a
            # step of the wall clock is not taken for them. Health is told how
            # long the measurement itself took: a measurement longer than the
            # period is the rate's fault, anything else a held-up sampler.
            missed = int((self._monotonic() - self._tick_monotonic) / self.period)
            if missed > 0:
                self.health.ticks_missed(now, missed, self._measurement_s)
        next_tick = (int(now / self.period) + 1) * self.period
        self._tick_monotonic = self._monotonic() + (next_tick - now)
        self._wait(next_tick - now)
        if self._stop.is_set():
            return
        started = self._clock()
        began = self._monotonic()
        measurement = self._sensor.single_measurement()
        self._measurement_s = self._monotonic() - began
        conversion = reg.xyz_conversion_s(measurement.cycle_count)
        self._measured(started + conversion / 2.0, measurement)

    def _continuous_once(self) -> None:
        measurement = self._sensor.read_if_ready()
        if measurement is None:
            self._wait(min(self.period / 4.0, 0.05))
            return
        self._measured(self._clock(), measurement)

    def _measured(self, t: float, m: Measurement) -> None:
        """Hold a new sample until a read-back confirms its gain, reading back
        now if waiting for the next sample would hold the oldest one longer
        than READ_BACK_EVERY_S (so after every sample at up to 1 Hz)."""
        self._consecutive = 0
        if not self._held:
            self._held_since = self._monotonic()
        self._held.append((t, m))
        if self._monotonic() - self._held_since + self.period < READ_BACK_EVERY_S:
            return
        self._read_back()
        if self._sensor is not None:
            # Samples got all the way through: the end of any run of
            # failures, and with it the backoff.
            self._backoff = BACKOFF_MIN_S
            self._unexpected = None

    def _deliver(self, t: float, m: Measurement) -> None:
        x, y, z = m.x_nt, m.y_nt, m.z_nt
        self.health.sample(t, x, y, z)
        self.on_sample(int(round(t * 1000)), x, y, z)

    def _failed(self, exc: Exception) -> None:
        if isinstance(exc, DataReadyTimeout):
            kind = "timeout"
        else:
            kind = "I2C error" if isinstance(exc, OSError) else "sensor error"
        self.health.error(kind, str(exc))
        # A failed sample is not a late one: no ticks are counted as missed
        # across it.
        self._tick_monotonic = None
        self._consecutive += 1
        log.warning("sample failed (%d in a row): %s", self._consecutive, exc)
        if self._consecutive >= REINIT_AFTER:
            log.warning("dropping the sensor after %d failures; will look for it again", self._consecutive)
            self._settle_held()
            self._drop_sensor()
            self._wait(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX_S)
