"""RM3100 register map and the datasheet tables the driver relies on.

Source: PNI "RM3100 & RM2100 Magneto-Inductive Magnetometer User Manual",
Doc 1017252 V16.0 (UM16), unless a line says otherwise. Page numbers are the
ones printed in the manual's footer.

Where the manual and the field disagree, the driver follows the reading that is
correct under both, and the comment says so. The same register addresses are
used on I2C and SPI; only SPI sets bit 7 of the address byte to read, and
sending 0x80 | reg over I2C addresses a register that does not exist (UM16
p.43's own I2C example sends 0x24, and every maintained driver agrees).
"""

import math

# ── I2C addressing (UM16 Table 4-1, p.27) ────────────────────────────────────
# 7-bit address 0b01000 SA1 SA0: the strap pins select 0x20..0x23. The manual
# contradicts itself on which pin is bit 0 (§4.3.3), so the driver probes all
# four rather than trusting a board's silkscreen.
I2C_ADDRESSES = (0x20, 0x21, 0x22, 0x23)

# ── Register addresses (UM16 Table 5-1, p.29) ────────────────────────────────
POLL = 0x00  # single-measurement trigger
CMM = 0x01  # continuous measurement mode
CCX = 0x04  # cycle count X, 16-bit MSB first; CCY and CCZ follow
CCY = 0x06
CCZ = 0x08
TMRC = 0x0B  # continuous-mode update rate
MX = 0x24  # measurement results: 3 axes x 3 bytes, auto-incrementing
MY = 0x27
MZ = 0x2A
BIST = 0x33  # built-in self test
STATUS = 0x34
HSHAKE = 0x35  # handshake / error flags
REVID = 0x36

# REVID is not given in the manual. 0x22 is what PX4, Zephyr, INAV, HamSCI's
# runMag and PNI's own Arduino quick guide expect, so it is the identity check.
EXPECTED_REVID = 0x22

# ── Bit fields ───────────────────────────────────────────────────────────────
# POLL (p.33): one bit per axis; 0x70 measures all three.
POLL_X = 0x10
POLL_Y = 0x20
POLL_Z = 0x40
POLL_XYZ = POLL_X | POLL_Y | POLL_Z

# CMM (p.31). UM16 shows bit 3 as "0" and a single DRDM bit at bit 2, yet its
# own examples write 0x79, which sets bit 3. Under the older layout (UM r04,
# the 2024 RM3100-CB manual) bits 3..2 are DRDM1..0 and 0b10 means "DRDY after
# the full sequence of enabled axes". 0x79 is therefore the value that asks for
# a complete X/Y/Z sample under either reading; the 0x71 most drivers use is
# only proven for STATUS polling, which this driver also uses.
CMM_START = 0x01
CMM_DRDM_ALL_AXES = 0x08
CMM_CMX = 0x10
CMM_CMY = 0x20
CMM_CMZ = 0x40
CMM_CONTINUOUS_XYZ = CMM_CMZ | CMM_CMY | CMM_CMX | CMM_DRDM_ALL_AXES | CMM_START  # 0x79

STATUS_DRDY = 0x80  # p.34; bits 6..0 are indeterminate

# BIST (p.35-36). The self test is armed with STE, runs on the next POLL, and
# reports per-axis pass bits that are valid only while STE still reads 1.
BIST_STE = 0x80
BIST_ZOK = 0x40
BIST_YOK = 0x20
BIST_XOK = 0x10
# STE | timeout 120 us | 4 LR periods: the value UM16 Fig 5-1 writes.
BIST_START = 0x8F

# ── Cycle count (p.5 Table 3-1, p.30) ────────────────────────────────────────
DEFAULT_CYCLE_COUNT = 200
# The register holds 0..65535. Below ~30 the result is quantisation-limited and
# above ~400 noise stops improving much (p.30); the app accepts a range that
# covers every documented and field-used value (HamSCI 400, Regoli et al. 800).
MIN_CYCLE_COUNT = 30
MAX_CYCLE_COUNT = 1000

# Table 3-1: typical values at 3.0 V, room temperature.
#   cycle count: (gain LSB/uT, noise nT, max single-axis rate Hz)
DATASHEET_TABLE = {
    50: (20.0, 30.0, 1600.0),
    100: (38.0, 20.0, 850.0),
    200: (75.0, 15.0, 440.0),
}


def gain_lsb_per_ut(cycle_count: int) -> float:
    """Counts per microtesla at a cycle count.

    PNI's formula from its own sample code (the mbed RM3100BB sample and the
    Arduino quick guide): 0.3671 * CC + 1.5. It reproduces Table 3-1 to within
    0.8 % (the table's 20 and 38 are rounded; 74.92 against 75 at the default
    200 is 0.1 %) and covers the cycle counts the table does not list. Real
    boards have been seen up to ~1.3x off this (ArduPilot a0cf4e158a); that
    is a calibration matter, not a formula one.
    """
    return 0.3671 * cycle_count + 1.5


def counts_to_nt(counts: int, cycle_count: int) -> float:
    """A 24-bit measurement result in nanotesla."""
    return counts * 1000.0 / gain_lsb_per_ut(cycle_count)


def lsb_nt(cycle_count: int) -> float:
    """The size of one count, in nanotesla (13.3 nT at the default 200)."""
    return 1000.0 / gain_lsb_per_ut(cycle_count)


def noise_nt(cycle_count: int) -> float:
    """Typical per-axis noise of one measurement, in nanotesla.

    Table 3-1 gives 30, 20 and 15 nT at 50, 100 and 200 cycles. Between those
    points this interpolates in log-log; outside them it extends the nearest
    segment's slope. That gives 11.3 nT at 400 and 8.4 nT at 800, which agrees
    with the 8.7 nT Regoli et al. (2018, GI 7:129) measured at 800 cycles.
    """
    points = sorted((cc, row[1]) for cc, row in DATASHEET_TABLE.items())
    if cycle_count <= points[1][0]:
        (c0, n0), (c1, n1) = points[0], points[1]
    else:
        (c0, n0), (c1, n1) = points[1], points[2]
    slope = math.log(n1 / n0) / math.log(c1 / c0)
    return n0 * (cycle_count / c0) ** slope


def axis_conversion_s(cycle_count: int) -> float:
    """Time to convert one axis, in seconds.

    Derived, not quoted: the reciprocal of Table 3-1's maximum single-axis rate
    is 0.625, 1.176 and 2.273 ms at 50, 100 and 200 cycles, which is linear in
    the cycle count to better than 1 % (about 2 * CC / 180 kHz, the sensor's
    oscillator, plus a fixed overhead).
    """
    return 75.7e-6 + 10.987e-6 * cycle_count


def xyz_conversion_s(cycle_count: int) -> float:
    """Time for one complete X, Y and Z measurement."""
    return 3.0 * axis_conversion_s(cycle_count)


def max_xyz_rate_hz(cycle_count: int) -> float:
    """The fastest a complete 3-axis sample can repeat (~147 Hz at 200)."""
    return 1.0 / xyz_conversion_s(cycle_count)


# ── TMRC continuous-mode rates (p.32-33, Table 5-4) ──────────────────────────
# 0x92 is ~600 Hz and every step halves it, down to 0x9F at ~0.075 Hz. The rate
# is further capped by the conversion time (p.32). Rates have ~7 % 1-sigma
# tolerance. UM16 lists 0x9E as 0.15 Hz; older manuals and the Linux driver
# carry a 0.015 Hz typo there.
TMRC_MIN = 0x92
TMRC_MAX = 0x9F
TMRC_DEFAULT = 0x96  # ~37 Hz


def tmrc_rate_hz(code: int) -> float:
    """Nominal continuous-mode rate for a TMRC value, before the CC cap."""
    if not TMRC_MIN <= code <= TMRC_MAX:
        raise ValueError(f"TMRC must be 0x{TMRC_MIN:02X}..0x{TMRC_MAX:02X}, got 0x{code:02X}")
    return 600.0 / 2 ** (code - TMRC_MIN)


def tmrc_for_rate(rate_hz: float) -> int:
    """The TMRC value whose nominal rate is closest to ``rate_hz`` (log scale)."""
    if rate_hz <= 0:
        raise ValueError("rate must be positive")
    return min(range(TMRC_MIN, TMRC_MAX + 1), key=lambda code: abs(math.log(tmrc_rate_hz(code) / rate_hz)))


def effective_continuous_rate_hz(code: int, cycle_count: int) -> float:
    """What continuous mode actually delivers: TMRC capped by conversion time."""
    return min(tmrc_rate_hz(code), max_xyz_rate_hz(cycle_count))


# ── Measurement results (p.34-35) ────────────────────────────────────────────
MEASUREMENT_BYTES = 9  # X2 X1 X0 Y2 Y1 Y0 Z2 Z1 Z0, MSB first
COUNTS_MIN = -(1 << 23)
COUNTS_MAX = (1 << 23) - 1


def decode_int24(b2: int, b1: int, b0: int) -> int:
    """Big-endian 24-bit two's complement to a Python int."""
    value = (b2 << 16) | (b1 << 8) | b0
    if value & 0x800000:
        value -= 1 << 24
    return value


def encode_int24(value: int) -> bytes:
    """Inverse of :func:`decode_int24`; the value must fit in 24 bits."""
    if not COUNTS_MIN <= value <= COUNTS_MAX:
        raise ValueError(f"{value} does not fit in 24 bits")
    return (value & 0xFFFFFF).to_bytes(3, "big")
