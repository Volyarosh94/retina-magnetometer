"""RM3100 driver: identify, configure and read the sensor over an ``I2CBus``.

The driver is synchronous and owns no thread; the sampler decides when to call
it. Time comes in through ``clock`` and ``sleep`` so that tests can run the
datasheet's timing without waiting for it.

Two ways to take measurements, both from UM16 §5:

- **Single measurement** (POLL): write the axes to measure, wait for DRDY,
  read the nine result bytes. The host decides when every sample happens, so
  timestamps line up with the host clock, at any rate the conversion and the
  host's own share of each sample leave room for (``max_poll_rate_hz``).
  This is the default, and what HamSCI's magnetometers do.
- **Continuous mode** (CMM): the sensor measures on its own at a TMRC rate and
  raises DRDY each time; the host polls STATUS and reads. Rates are the TMRC
  steps (600 Hz halving down to 0.075 Hz), capped by conversion time.

DRDY is watched through the STATUS register rather than the DRDY pin: RETINA
nodes have no GPIO wired to the sensor, and STATUS polling is the path every
maintained driver has proven.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from retina_magnetometer.rm3100 import registers as reg
from retina_magnetometer.rm3100.bus import I2CBus


class RM3100Error(Exception):
    """The sensor answered, but not the way an RM3100 should."""


class NotAnRM3100(RM3100Error):
    """Something acknowledged the address, but REVID was not 0x22."""


class DataReadyTimeout(RM3100Error):
    """DRDY did not rise when a measurement should have been ready."""


@dataclass(frozen=True)
class Measurement:
    """One complete X/Y/Z sample, as the sensor reported it."""

    x_counts: int
    y_counts: int
    z_counts: int
    cycle_count: int

    @property
    def x_nt(self) -> float:
        return reg.counts_to_nt(self.x_counts, self.cycle_count)

    @property
    def y_nt(self) -> float:
        return reg.counts_to_nt(self.y_counts, self.cycle_count)

    @property
    def z_nt(self) -> float:
        return reg.counts_to_nt(self.z_counts, self.cycle_count)


@dataclass(frozen=True)
class SelfTestResult:
    """The BIST verdict: per-axis LR-oscillator checks (UM16 p.35-37)."""

    ran: bool  # STE still read 1, so the per-axis bits are meaningful
    x_ok: bool
    y_ok: bool
    z_ok: bool
    raw: int

    @property
    def passed(self) -> bool:
        return self.ran and self.x_ok and self.y_ok and self.z_ok


def decode_measurement(data: bytes, cycle_count: int) -> Measurement:
    """Nine result bytes, X then Y then Z, each 24-bit big-endian signed."""
    if len(data) != reg.MEASUREMENT_BYTES:
        raise RM3100Error(f"expected {reg.MEASUREMENT_BYTES} result bytes, got {len(data)}")
    return Measurement(
        x_counts=reg.decode_int24(data[0], data[1], data[2]),
        y_counts=reg.decode_int24(data[3], data[4], data[5]),
        z_counts=reg.decode_int24(data[6], data[7], data[8]),
        cycle_count=cycle_count,
    )


class RM3100:
    """One RM3100 at one I2C address."""

    # How long past the datasheet conversion time to keep polling for DRDY.
    # Conversion time is derived from Table 3-1 and the oscillator has a
    # tolerance, so a generous fixed margin costs nothing on the happy path.
    DRDY_MARGIN_S = 0.020
    # Pause between STATUS polls: short against a conversion (6.8 ms at 200
    # cycles) without spinning a CPU core on a busy node.
    DRDY_POLL_S = 0.001
    # How much slower than Table 3-1 a chip may convert and still keep the
    # rates poll mode accepts. The conversion-time formula fits the table to
    # 1 %, but the table gives typical values; this leaves room for a part a
    # few percent off them.
    CONVERSION_TOLERANCE = 0.05
    # What a single measurement costs the host on top of the conversion: one
    # STATUS polling step (once DRDY rises, the read that sees it can come a
    # step later), and 3.3 ms for the POLL write, two STATUS reads and the
    # nine-byte result read (about 2.1 ms of bus time at 100 kHz) and the
    # host's own share of a sample.
    MEASUREMENT_OVERHEAD_S = DRDY_POLL_S + 0.0033
    # BIST has no documented completion time: DRDY rises when it ends (UM16
    # §5.6.1), Fig 5-1 waits 10 ms and PX4 polls DRDY for 26 ms. Wait for DRDY
    # up to this long, then read anyway: PX4 PR #19207 saw DRDY sometimes
    # stay low after BIST on fast CPUs. The test is started by a POLL, so the
    # wait is never shorter than a measurement at the current cycle count
    # either (33 ms at 1000): a test read before it has ended looks like a
    # failure on every axis.
    BIST_WAIT_S = 0.030
    # Continuous mode: how many sample periods DRDY may stay low before the
    # chip is taken to have stopped measuring. A brown-out (CMM resets to 0),
    # or another program reading CMM or writing TMRC (UM16 p.31), ends
    # continuous mode without a single transfer failing, and at the default
    # 200 cycles the cycle counts give no sign of it either. TMRC rates are
    # good to ~7 % (p.32), so a chip that is merely slow never gets near three.
    CONTINUOUS_TIMEOUT_PERIODS = 3

    def __init__(
        self,
        bus: I2CBus,
        address: int = reg.I2C_ADDRESSES[0],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.bus = bus
        self.address = address
        self.cycle_count = reg.DEFAULT_CYCLE_COUNT
        self._clock = clock
        self._sleep = sleep
        # Continuous mode's DRDY deadline, and the window it is re-armed with;
        # None while this driver has not started continuous mode.
        self._continuous_deadline: float | None = None
        self._continuous_timeout_s = 0.0

    @classmethod
    def max_poll_rate_hz(cls, cycle_count: int) -> float:
        """The fastest single measurements can follow each other (~87 Hz at 200).

        The conversion, with ``CONVERSION_TOLERANCE``, plus
        ``MEASUREMENT_OVERHEAD_S``. Poll mode samples on a grid, so a
        measurement that runs past the next tick costs that tick: a rate set
        just above what the chip and the bus allow delivers about half of what
        was asked.
        """
        conversion = reg.xyz_conversion_s(cycle_count) * (1.0 + cls.CONVERSION_TOLERANCE)
        return 1.0 / (conversion + cls.MEASUREMENT_OVERHEAD_S)

    # ── Register access ──────────────────────────────────────────────────────

    def _write(self, register: int, data: bytes) -> None:
        self.bus.transfer(self.address, bytes([register]) + data)

    def _read(self, register: int, length: int) -> bytes:
        return self.bus.transfer(self.address, bytes([register]), length)

    def _read_byte(self, register: int) -> int:
        return self._read(register, 1)[0]

    # ── Identity and configuration ───────────────────────────────────────────

    def read_revid(self) -> int:
        return self._read_byte(reg.REVID)

    def probe(self) -> int:
        """Confirm an RM3100 is at this address. Returns its REVID."""
        revid = self.read_revid()
        if revid != reg.EXPECTED_REVID:
            raise NotAnRM3100(
                f"device at 0x{self.address:02X} reports REVID 0x{revid:02X}, expected 0x{reg.EXPECTED_REVID:02X}"
            )
        return revid

    def read_cycle_counts(self) -> tuple[int, int, int]:
        data = self._read(reg.CCX, 6)
        return (
            int.from_bytes(data[0:2], "big"),
            int.from_bytes(data[2:4], "big"),
            int.from_bytes(data[4:6], "big"),
        )

    def set_cycle_count(self, cycle_count: int) -> None:
        """Write the same cycle count to all three axes, then read it back.

        One six-byte write from CCX, relying on auto-increment (UM16 p.30).
        The read-back matters: a write that half-lands leaves the axes at
        different gains, and every later conversion to nT would be wrong
        without an error anywhere.
        """
        if not reg.MIN_CYCLE_COUNT <= cycle_count <= reg.MAX_CYCLE_COUNT:
            raise ValueError(f"cycle count must be {reg.MIN_CYCLE_COUNT}..{reg.MAX_CYCLE_COUNT}, got {cycle_count}")
        word = cycle_count.to_bytes(2, "big")
        self._write(reg.CCX, word * 3)
        readback = self.read_cycle_counts()
        if readback != (cycle_count,) * 3:
            raise RM3100Error(f"cycle count write did not take: wrote {cycle_count}, read back {readback}")
        self.cycle_count = cycle_count

    # ── Measurements ─────────────────────────────────────────────────────────

    def data_ready(self) -> bool:
        return bool(self._read_byte(reg.STATUS) & reg.STATUS_DRDY)

    def _wait_data_ready(self, started: float, timeout_s: float) -> None:
        deadline = started + timeout_s
        while not self.data_ready():
            if self._clock() >= deadline:
                raise DataReadyTimeout(f"DRDY did not rise within {timeout_s * 1000:.1f} ms")
            self._sleep(self.DRDY_POLL_S)

    def read_result(self) -> Measurement:
        """Burst-read MX..MZ. Reading the results clears DRDY (UM16 p.38)."""
        return decode_measurement(self._read(reg.MX, reg.MEASUREMENT_BYTES), self.cycle_count)

    def single_measurement(self) -> Measurement:
        """Trigger one X/Y/Z measurement and wait for it.

        STATUS is first read when the conversion should be over. Reading it
        all through the conversion would only add bus traffic: this way a
        measurement is the POLL, usually one STATUS read, and the results.
        """
        self._write(reg.POLL, bytes([reg.POLL_XYZ]))
        started = self._clock()
        conversion = reg.xyz_conversion_s(self.cycle_count)
        self._sleep(conversion)
        self._wait_data_ready(started, conversion + self.DRDY_MARGIN_S)
        return self.read_result()

    def start_continuous(self, tmrc: int) -> None:
        """Measure continuously at a TMRC rate.

        Continuous mode is stopped first and TMRC written while it is off:
        writing TMRC during continuous mode terminates it (UM16 p.31), which
        is how PX4's order of operations has been seen to go wrong. Set the
        cycle count first: the rate, and so how long ``read_if_ready`` waits
        for DRDY, depends on it.
        """
        if not reg.TMRC_MIN <= tmrc <= reg.TMRC_MAX:
            raise ValueError(f"TMRC must be 0x{reg.TMRC_MIN:02X}..0x{reg.TMRC_MAX:02X}")
        self.stop_continuous()
        self.set_tmrc(tmrc)
        self._write(reg.CMM, bytes([reg.CMM_CONTINUOUS_XYZ]))
        period = 1.0 / reg.effective_continuous_rate_hz(tmrc, self.cycle_count)
        self._continuous_timeout_s = self.CONTINUOUS_TIMEOUT_PERIODS * period + self.DRDY_MARGIN_S
        self._continuous_deadline = self._clock() + self._continuous_timeout_s

    def stop_continuous(self) -> None:
        self._write(reg.CMM, b"\x00")
        self._continuous_deadline = None

    def set_tmrc(self, tmrc: int) -> None:
        """Write TMRC, the continuous-mode rate. Only with continuous mode
        stopped: writing TMRC while it runs ends it (UM16 p.31)."""
        if not reg.TMRC_MIN <= tmrc <= reg.TMRC_MAX:
            raise ValueError(f"TMRC must be 0x{reg.TMRC_MIN:02X}..0x{reg.TMRC_MAX:02X}")
        self._write(reg.TMRC, bytes([tmrc]))

    def read_tmrc(self) -> int:
        """Read TMRC. Unlike reading CMM, this leaves continuous mode running."""
        return self._read_byte(reg.TMRC)

    def read_if_ready(self) -> Measurement | None:
        """Continuous mode: the new sample if DRDY is up, otherwise None.

        Once continuous mode was started here, DRDY that stays low for
        ``CONTINUOUS_TIMEOUT_PERIODS`` sample periods raises
        ``DataReadyTimeout``, the same failure a single measurement reports,
        so the caller's recovery (re-initialising after a few) applies to a
        chip that has quietly stopped measuring. The wait starts again after
        each timeout: one per window, not one per look.
        """
        # When the look began. DRDY was low at some moment after it, which is
        # all a "not ready" says: a thread held up once the read is done (a
        # busy host, a slow simulator reply) must not be taken for a chip
        # that stopped. A hold-up before the read cannot mislead, as DRDY
        # stays up until the results are read.
        looked_at = self._clock()
        if self.data_ready():
            if self._continuous_deadline is not None:
                self._continuous_deadline = self._clock() + self._continuous_timeout_s
            return self.read_result()
        if self._continuous_deadline is not None and looked_at >= self._continuous_deadline:
            self._continuous_deadline = self._clock() + self._continuous_timeout_s
            raise DataReadyTimeout(
                f"DRDY has not risen for {self._continuous_timeout_s * 1000:.0f} ms "
                f"({self.CONTINUOUS_TIMEOUT_PERIODS} sample periods) in continuous mode"
            )
        return None

    # ── Self test ────────────────────────────────────────────────────────────

    def self_test(self) -> SelfTestResult:
        """Run the built-in self test (UM16 Fig 5-1) and leave BIST off.

        BIST checks that each axis's LR oscillator runs; it caught a missing
        Z coil in PX4 PR #19583. It says nothing about gain or offset.

        Continuous mode is stopped first, as PX4 does, and stays off: the
        test runs on a POLL, and a POLL written while continuous mode runs is
        NACKed and ignored (UM16 p.28, p.31). A chip a previous run left
        measuring would otherwise fail on every axis, or not answer at all.
        The wait is sized for the cycle count this driver set, so set it
        first.
        """
        self.stop_continuous()
        self._write(reg.BIST, bytes([reg.BIST_START]))
        self._write(reg.POLL, bytes([reg.POLL_XYZ]))
        wait = max(self.BIST_WAIT_S, reg.xyz_conversion_s(self.cycle_count) + self.DRDY_MARGIN_S)
        deadline = self._clock() + wait
        while self._clock() < deadline and not self.data_ready():
            self._sleep(self.DRDY_POLL_S)
        raw = self._read_byte(reg.BIST)
        self._write(reg.POLL, b"\x00")
        self._write(reg.BIST, b"\x00")
        return SelfTestResult(
            ran=bool(raw & reg.BIST_STE),
            x_ok=bool(raw & reg.BIST_XOK),
            y_ok=bool(raw & reg.BIST_YOK),
            z_ok=bool(raw & reg.BIST_ZOK),
            raw=raw,
        )


def find_rm3100(
    bus: I2CBus,
    addresses: tuple[int, ...] = reg.I2C_ADDRESSES,
    **kwargs,
) -> RM3100:
    """The first RM3100 that answers on any of ``addresses``.

    A missing device NACKs, which surfaces as ``OSError``; a device that
    answers with the wrong REVID raises ``NotAnRM3100``. If no RM3100 is
    found, a wrong REVID is what gets raised, naming every address that gave
    one: it says more than the silence elsewhere, and its value is what
    hardware bring-up needs to see. Only if nothing answered at all is the
    last address's ``OSError`` raised.
    """
    if not addresses:
        raise ValueError("no addresses to probe")
    wrong: list[NotAnRM3100] = []
    last_error: OSError | None = None
    for address in addresses:
        sensor = RM3100(bus, address, **kwargs)
        try:
            sensor.probe()
        except NotAnRM3100 as exc:
            wrong.append(exc)
            continue
        except OSError as exc:
            last_error = exc
            continue
        return sensor
    if len(wrong) == 1:
        raise wrong[0]
    if wrong:
        raise NotAnRM3100("; ".join(str(exc) for exc in wrong))
    raise last_error
