"""RM3100 driver: identify, configure and read the sensor over an ``I2CBus``.

The driver is synchronous and owns no thread; the sampler decides when to call
it. Time comes in through ``clock`` and ``sleep`` so that tests can run the
datasheet's timing without waiting for it.

Two ways to take measurements, both from UM16 §5:

- **Single measurement** (POLL): write the axes to measure, wait for DRDY,
  read the nine result bytes. The host decides when every sample happens, so
  any rate up to the conversion limit is possible and timestamps line up with
  the host clock. This is the default, and what HamSCI's magnetometers do.
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
    """DRDY did not rise within the conversion time plus margin."""


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
    # BIST has no documented completion time; UM16 Fig 5-1 waits 10 ms and PX4
    # polls DRDY for 26 ms. Wait for DRDY up to this long, then read anyway:
    # PX4 PR #19207 saw DRDY sometimes stay low after BIST on fast CPUs.
    BIST_WAIT_S = 0.030

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

    def _wait_data_ready(self, timeout_s: float) -> None:
        deadline = self._clock() + timeout_s
        while not self.data_ready():
            if self._clock() >= deadline:
                raise DataReadyTimeout(f"DRDY did not rise within {timeout_s * 1000:.1f} ms")
            self._sleep(self.DRDY_POLL_S)

    def read_result(self) -> Measurement:
        """Burst-read MX..MZ. Reading the results clears DRDY (UM16 p.38)."""
        return decode_measurement(self._read(reg.MX, reg.MEASUREMENT_BYTES), self.cycle_count)

    def single_measurement(self) -> Measurement:
        """Trigger one X/Y/Z measurement and wait for it."""
        self._write(reg.POLL, bytes([reg.POLL_XYZ]))
        self._wait_data_ready(reg.xyz_conversion_s(self.cycle_count) + self.DRDY_MARGIN_S)
        return self.read_result()

    def start_continuous(self, tmrc: int) -> None:
        """Measure continuously at a TMRC rate.

        Continuous mode is stopped first and TMRC written while it is off:
        writing TMRC during continuous mode terminates it (UM16 p.31), which
        is how PX4's order of operations has been seen to go wrong.
        """
        if not reg.TMRC_MIN <= tmrc <= reg.TMRC_MAX:
            raise ValueError(f"TMRC must be 0x{reg.TMRC_MIN:02X}..0x{reg.TMRC_MAX:02X}")
        self.stop_continuous()
        self._write(reg.TMRC, bytes([tmrc]))
        self._write(reg.CMM, bytes([reg.CMM_CONTINUOUS_XYZ]))

    def stop_continuous(self) -> None:
        self._write(reg.CMM, b"\x00")

    def read_if_ready(self) -> Measurement | None:
        """Continuous mode: the new sample if DRDY is up, otherwise None."""
        if not self.data_ready():
            return None
        return self.read_result()

    # ── Self test ────────────────────────────────────────────────────────────

    def self_test(self) -> SelfTestResult:
        """Run the built-in self test (UM16 Fig 5-1) and leave BIST off.

        BIST checks that each axis's LR oscillator runs; it caught a missing
        Z coil in PX4 PR #19583. It says nothing about gain or offset. Run it
        with continuous mode stopped.
        """
        self._write(reg.BIST, bytes([reg.BIST_START]))
        self._write(reg.POLL, bytes([reg.POLL_XYZ]))
        deadline = self._clock() + self.BIST_WAIT_S
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
    answers with the wrong REVID raises ``NotAnRM3100``. If nothing answers at
    all, the last address's error is raised so the caller can report it.
    """
    if not addresses:
        raise ValueError("no addresses to probe")
    last_error: Exception | None = None
    for address in addresses:
        sensor = RM3100(bus, address, **kwargs)
        try:
            sensor.probe()
        except (OSError, NotAnRM3100) as exc:
            last_error = exc
            continue
        return sensor
    raise last_error
