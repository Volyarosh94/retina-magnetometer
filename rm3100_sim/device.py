"""A register-level model of the RM3100, answering I2C transfers.

The model implements the chip's register semantics from the manual (UM16 §5),
so the app's driver runs against it unchanged:

- POLL starts a single measurement of the requested axes; DRDY rises after the
  conversion time for their cycle counts.
- CMM starts and stops continuous mode, which measures every TMRC interval
  (capped by conversion time). Writing TMRC or *reading* CMM while it runs
  stops it, as p.31 says — a driver that does either finds out here.
- The cycle-count registers set each axis's gain, conversion time and noise.
- Reading the results clears DRDY; so does any register write (HSHAKE DRC1
  and DRC0, both on at reset). Reading results while DRDY is low sets NACK2
  and returns the stale values.
- A write the chip cannot carry out is refused on the wire: the data byte is
  NACKed, which i2c-dev reports as EREMOTEIO, and HSHAKE says why (UM16
  §4.5.1). That is a POLL during continuous mode or a CMM write during a POLL
  (NACK1), and a write to an undefined register (NACK0).
- BIST, armed with STE and run by the next POLL, reports per-axis pass bits.
- REVID reads 0x22.

Where the manual is silent, or its letter would break what works in the field,
the model picks a reading. The one the app relies on, which
docs/hardware-verification.md lists: DRC0 clears DRDY when a write's first
data byte arrives. The manual's text (R07 §5.6.2, p.36) has it clear as the
register address arrives, but then a pointer-only write, the first half of
every register read, would clear it too, and STATUS polling, which every
maintained driver relies on, could never see DRDY rise.

The rest nothing in the app depends on. Bytes before a refused one in the same
write have taken effect: the manual says only that the chip NACKs, and that
the address increments after every byte. A write to a read-only register is
refused like one to an undefined register. Register 0x0A is the exception the
other way: Table 5-1 leaves it out, but HamSCI's rm3100-runMag writes it as an
undocumented "NOS" register (without checking whether the write was
acknowledged), so the model stores it and ignores it rather than refuse
software that uses it. Nothing clears a NACK bit but a power cycle, and NACK0
is set in the reset value the manual gives (0x1B). A cycle count of 0, which
the register accepts, counts nothing and reads 0.

Past 2030.0 the main field is extrapolated, not modelled (WMM2025's span): the
model logs a warning the first time simulated time gets there.

Measurement values come from the scenario's field at the middle of the
conversion, plus Gaussian noise, quantised to counts and saturated at the
sensor's ±800 µT range. The noise is sized so that what comes out of the
registers, rounding included, has the datasheet's noise for the axis's cycle
count. The noise for a conversion is a pure function of (seed, axis, time), so
a replay with the same seed and the same sample times is bit-identical.

Faults from the scenario are applied per transfer: NACKs, a disconnected
sensor, DRDY that never rises, and brown-outs. A disconnect ends in a power
cycle (registers back at their defaults) as does a brown-out, whether or not a
transfer happened to arrive in between; the first transfer after either finds
the chip reset.
"""

from __future__ import annotations

import errno
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from retina_magnetometer.rm3100 import registers as reg
from rm3100_sim import physics
from rm3100_sim.scenario import Scenario

log = logging.getLogger(__name__)

# Linux's "Remote I/O error", what i2c-dev returns for a NACK. Not every
# platform's errno module defines it (macOS does not), so the Linux value.
EREMOTEIO = getattr(errno, "EREMOTEIO", 121)

_AXES = ("x", "y", "z")
_POLL_BITS = (reg.POLL_X, reg.POLL_Y, reg.POLL_Z)
_BIST_OK_BITS = (reg.BIST_XOK, reg.BIST_YOK, reg.BIST_ZOK)
_NACK0, _NACK1, _NACK2 = 0x10, 0x20, 0x40
_DRC0, _DRC1 = 0x01, 0x02
_HSHAKE_RESET = 0x1B  # UM16 Table 5-1 (PX4 treats 0x0B as the default; see docs)

# Noise is keyed to the conversion time on a 0.1 ms grid: finer than any two
# conversions can be apart (1.9 ms at 50 cycles), coarse enough to be exact.
_NOISE_SLOTS_PER_S = 10_000


def input_noise_nt(cycle_count: int, noise_scale: float = 1.0) -> float:
    """The noise to add before quantisation, so that the output has the datasheet's.

    Table 3-1's noise can only have been measured on the chip's output, which
    is in counts, so it already includes the rounding to counts: 15 nT of
    noise at 200 cycles, where one count is 13.3 nT. Rounding spreads a value
    by one count over √12, so adding the full figure and then rounding would
    come out above the table at every cycle count, by 3 % at 200 and 15 % at
    30. The rounding's share is taken out first.
    """
    target = reg.noise_nt(cycle_count) * noise_scale
    return math.sqrt(max(target * target - reg.lsb_nt(cycle_count) ** 2 / 12.0, 0.0))


def measure_counts(scenario: Scenario, epoch: float, cycle_counts) -> tuple[int, int, int]:
    """What the chip converts at scenario time ``epoch``, in counts per axis.

    The scenario's field in the sensor frame, plus noise, clipped to the
    ±800 µT range, then quantised with the gain for each axis's cycle count;
    once quantised, the noise is the datasheet's for that cycle count (scaled
    by the scenario). A dead axis, or one with a cycle count of 0, reads 0.
    Shared by the register model and by ``generate``/``backfill``, so a file
    of simulated samples is exactly what the chip would have produced.
    """
    field = scenario.sensor_field(epoch)
    slot = round(epoch * _NOISE_SLOTS_PER_S)
    out = []
    for i in range(3):
        cycle_count = cycle_counts[i]
        if scenario.sensor.dead_axis == _AXES[i] or cycle_count == 0:
            out.append(0)
            continue
        sigma = input_noise_nt(cycle_count, scenario.sensor.noise_scale)
        value = field[i] + sigma * physics.gauss(scenario.seed, f"noise-{_AXES[i]}", slot)
        value = max(-physics.SENSOR_RANGE_NT, min(physics.SENSOR_RANGE_NT, value))
        counts = round(value * reg.gain_lsb_per_ut(cycle_count) / 1000.0)
        out.append(max(reg.COUNTS_MIN, min(reg.COUNTS_MAX, counts)))
    return (out[0], out[1], out[2])


class SimClock:
    """Real (monotonic) time for the chip's timing, mapped to scenario time.

    ``speed`` runs the scenario faster than real time without changing how
    long a conversion takes, so the driver's timing is exercised as on
    hardware while a day of field variation passes in minutes.
    """

    def __init__(self, epoch_start: float, *, speed: float = 1.0, monotonic: Callable[[], float] = time.monotonic):
        if not math.isfinite(speed) or speed <= 0:
            raise ValueError("speed must be a positive number")
        self._monotonic = monotonic
        self._origin = monotonic()
        self.epoch_start = epoch_start
        self.speed = speed

    def now(self) -> float:
        return self._monotonic()

    def epoch(self, monotonic_time: float) -> float:
        return self.epoch_start + (monotonic_time - self._origin) * self.speed


@dataclass
class _Pending:
    done_at: float
    started_at: float
    axes: int  # POLL bits
    bist: bool


@dataclass
class DeviceStats:
    transfers: int = 0
    measurements: int = 0
    nacks_injected: int = 0
    disconnected_transfers: int = 0
    writes_refused: int = 0
    power_cycles: int = 0


class RM3100Model:
    """One simulated chip on the bus. Thread-safe: the server may have several
    clients, and the chip has one register file."""

    def __init__(self, scenario: Scenario, clock: SimClock):
        self.scenario = scenario
        self.clock = clock
        self.address = scenario.sensor.address
        self.stats = DeviceStats()
        self._lock = threading.Lock()
        # Scenario time up to which power cycles have been applied: the chip
        # powers up now, and each transfer applies whatever happened since.
        self._cycled_until = clock.epoch(clock.now())
        self._past_the_model = False
        self._power_on_reset()

    # ── State ────────────────────────────────────────────────────────────────

    def _power_on_reset(self) -> None:
        self.cycle_counts = [reg.DEFAULT_CYCLE_COUNT] * 3
        self.tmrc = reg.TMRC_DEFAULT
        self.cmm = 0x00
        self.poll = 0x00
        self.nos = 0x00
        self.bist = 0x00
        self.hshake = _HSHAKE_RESET
        self.drdy = False
        self.results = bytearray(reg.MEASUREMENT_BYTES)
        self.pointer = 0x00
        self._pending: _Pending | None = None
        self._continuous_since: float | None = None
        self._continuous_done = 0

    @property
    def continuous(self) -> bool:
        return self._continuous_since is not None

    def _conversion_s(self, axes: int) -> float:
        return sum(reg.axis_conversion_s(self.cycle_counts[i]) for i, bit in enumerate(_POLL_BITS) if axes & bit)

    def _continuous_axes(self) -> int:
        # CMM's CMX/CMY/CMZ sit in the same bit positions as POLL's PMX/PMY/PMZ.
        return self.cmm & reg.POLL_XYZ

    def _continuous_period(self) -> float:
        return max(1.0 / reg.tmrc_rate_hz(self.tmrc), self._conversion_s(self._continuous_axes()))

    # ── Faults ───────────────────────────────────────────────────────────────

    def _check_faults(self, epoch: float) -> None:
        # Power lost since the last transfer, to a disconnect that has ended or
        # to a brown-out: the registers are at their defaults and anything the
        # host set is gone, whether or not a transfer saw it happen.
        cycles = sum(f.power_cycles_between(self._cycled_until, epoch) for f in self.scenario.faults)
        self._cycled_until = max(self._cycled_until, epoch)
        if cycles:
            self.stats.power_cycles += cycles
            self._power_on_reset()
        if any(f.kind == "disconnect" and f.active(epoch) for f in self.scenario.faults):
            self.stats.disconnected_transfers += 1
            raise OSError(EREMOTEIO, f"no acknowledge from 0x{self.address:02X} (sensor disconnected)")
        for fault in self.scenario.faults:
            if fault.kind == "nack" and fault.active(epoch):
                if physics.uniform(self.scenario.seed, "nack", self.stats.transfers) < fault.probability:
                    self.stats.nacks_injected += 1
                    raise OSError(EREMOTEIO, f"no acknowledge from 0x{self.address:02X} (injected)")

    def _drdy_stuck(self, epoch: float) -> bool:
        return any(f.kind == "stuck_drdy" and f.active(epoch) for f in self.scenario.faults)

    # ── Conversions ──────────────────────────────────────────────────────────

    def _advance(self, now: float) -> None:
        """Complete every conversion whose time has come."""
        if self._pending is not None and now >= self._pending.done_at:
            pending, self._pending = self._pending, None
            if pending.bist:
                self._complete_bist(pending.done_at)
            else:
                self._latch(pending.axes, (pending.started_at + pending.done_at) / 2.0, pending.done_at)
        if self._continuous_since is not None:
            period = self._continuous_period()
            completed = math.floor((now - self._continuous_since) / period)
            if completed > self._continuous_done:
                self._continuous_done = completed
                done_at = self._continuous_since + completed * period
                axes = self._continuous_axes()
                self._latch(axes, done_at - self._conversion_s(axes) / 2.0, done_at)

    def _latch(self, axes: int, sample_time: float, done_at: float) -> None:
        counts = measure_counts(self.scenario, self.clock.epoch(sample_time), self.cycle_counts)
        for i, bit in enumerate(_POLL_BITS):
            if axes & bit:
                self.results[3 * i : 3 * i + 3] = reg.encode_int24(counts[i])
        self.stats.measurements += 1
        if not self._drdy_stuck(self.clock.epoch(done_at)):
            self.drdy = True

    def _complete_bist(self, done_at: float) -> None:
        result = self.bist & (reg.BIST_STE | 0x0F)
        for i, ok_bit in enumerate(_BIST_OK_BITS):
            if self.scenario.sensor.dead_axis != _AXES[i]:
                result |= ok_bit
        self.bist = result
        if not self._drdy_stuck(self.clock.epoch(done_at)):
            self.drdy = True

    # ── Register writes ──────────────────────────────────────────────────────

    def _refuse(self, flag: int, why: str) -> OSError:
        """A write the chip cannot carry out: it NACKs the data byte instead
        of acknowledging it (UM16 §4.5.1), and sets the HSHAKE bit that says
        why. The transfer fails as a NACK fails on i2c-dev."""
        self.hshake |= flag
        self.stats.writes_refused += 1
        return OSError(EREMOTEIO, f"no acknowledge from 0x{self.address:02X} ({why})")

    def _write_register(self, register: int, value: int, now: float) -> None:
        if register == reg.POLL:
            if self.continuous:
                raise self._refuse(_NACK1, "POLL written during continuous mode, NACK1")
            self.poll = value
            axes = value & reg.POLL_XYZ
            if axes:
                armed = bool(self.bist & reg.BIST_STE)
                self._pending = _Pending(done_at=now + self._conversion_s(axes), started_at=now, axes=axes, bist=armed)
        elif register == reg.CMM:
            if self._pending is not None:
                raise self._refuse(_NACK1, "CMM written during a single measurement, NACK1")
            self.cmm = value
            if value & reg.CMM_START and value & reg.POLL_XYZ:
                self._continuous_since = now
                self._continuous_done = 0
            else:
                self._continuous_since = None
        elif reg.CCX <= register <= reg.CCZ + 1:
            axis = (register - reg.CCX) // 2
            current = self.cycle_counts[axis]
            if (register - reg.CCX) % 2 == 0:
                self.cycle_counts[axis] = (value << 8) | (current & 0xFF)
            else:
                self.cycle_counts[axis] = (current & 0xFF00) | value
        elif register == 0x0A:
            # NOS: left out of Table 5-1, but HamSCI's rm3100-runMag writes it
            # (see the module docstring). Stored and otherwise ignored; nothing
            # in this app depends on it.
            self.nos = value
        elif register == reg.TMRC:
            self.tmrc = value if reg.TMRC_MIN <= value <= reg.TMRC_MAX else self.tmrc
            if self.continuous:
                self._stop_continuous()
        elif register == reg.BIST:
            self.bist = value & (reg.BIST_STE | 0x0F)
        elif register == reg.HSHAKE:
            self.hshake = (self.hshake & (_NACK0 | _NACK1 | _NACK2)) | 0x08 | (value & (_DRC0 | _DRC1))
        else:
            raise self._refuse(_NACK0, f"write to undefined register 0x{register:02X}, NACK0")

    def _stop_continuous(self) -> None:
        self._continuous_since = None
        self.cmm &= ~reg.CMM_START

    # ── Register reads ───────────────────────────────────────────────────────

    def _read_register(self, register: int) -> int:
        if register == reg.POLL:
            return self.poll
        if register == reg.CMM:
            value = self.cmm
            if self.continuous:
                # UM16 p.31: reading CMM terminates continuous mode.
                self._stop_continuous()
            return value
        if reg.CCX <= register <= reg.CCZ + 1:
            axis = (register - reg.CCX) // 2
            word = self.cycle_counts[axis]
            return (word >> 8) & 0xFF if (register - reg.CCX) % 2 == 0 else word & 0xFF
        if register == 0x0A:
            return self.nos
        if register == reg.TMRC:
            return self.tmrc
        if reg.MX <= register < reg.MX + reg.MEASUREMENT_BYTES:
            return self.results[register - reg.MX]
        if register == reg.BIST:
            return self.bist
        if register == reg.STATUS:
            return reg.STATUS_DRDY if self.drdy else 0x00
        if register == reg.HSHAKE:
            return self.hshake
        if register == reg.REVID:
            return reg.EXPECTED_REVID
        return 0x00

    # ── The bus interface ────────────────────────────────────────────────────

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        """One I2C transaction: optional pointer and data, optional read."""
        with self._lock:
            now = self.clock.now()
            epoch = self.clock.epoch(now)
            self.stats.transfers += 1
            if epoch >= physics.WMM_VALID_UNTIL and not self._past_the_model:
                self._past_the_model = True
                log.warning(
                    "simulated time has reached 2030.0, the end of WMM2025: "
                    "from here on the main field is extrapolated, not modelled"
                )
            if address != self.address:
                raise OSError(EREMOTEIO, f"no acknowledge from 0x{address:02X}")
            self._check_faults(epoch)
            self._advance(now)
            if write:
                self.pointer = write[0]
                data = write[1:]
                # DRC0 clears DRDY when a write's first data byte arrives,
                # before that byte is taken or refused. The manual says as the
                # register address arrives, but then a pointer-only write, the
                # first half of every read, would clear it too (module docstring).
                if data and self.hshake & _DRC0:
                    self.drdy = False
                for value in data:
                    self._write_register(self.pointer, value, now)
                    self.pointer += 1
            if not read_length:
                return b""
            out = bytearray()
            reads_results = self.pointer <= reg.MX < self.pointer + read_length
            if reads_results and not self.drdy:
                self.hshake |= _NACK2
            for offset in range(read_length):
                out.append(self._read_register(self.pointer + offset))
            if reads_results and self.hshake & _DRC1:
                self.drdy = False
            self.pointer += read_length
            return bytes(out)
