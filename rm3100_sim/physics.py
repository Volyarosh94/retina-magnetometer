"""The field model: what a magnetometer at a site would see, before the sensor.

Frames and units. Vectors are NED (x north, y east, z down) in nanotesla, the
geomagnetic convention, unless a name says ``sensor``. Times are Unix epoch
seconds (UTC). Distances are metres.

The model is the sum of independent sources, each a pure function of time:

    B(t) = main field (WMM2025 at the site)      ~48,600 nT at Greenville, SC
         + crustal offset (static, per site)      tens to ~100 nT
         + solar-quiet daily variation (Sq)       tens of nT, peaks near noon
         + storms                                 tens to hundreds of nT
         + local steps (a parked car, a fence)    tens of nT at one site
         + UAP passes (magnetic dipoles)          200 nT at 1 km, 1/r^3

Randomness never comes from a shared generator. Every draw is a pure function
of (seed, stream name, index), so a source can be added or removed without
shifting any other source's numbers, and a sample at a given time has the same
noise in every run with the same seed.
"""

from __future__ import annotations

import bisect
import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

Vec = tuple[float, float, float]

# μ0/4π in T·m/A. CODATA 2022 puts μ0/4π at 0.99999999987e-7; the difference is
# ten orders of magnitude below anything this model resolves.
MU0_OVER_4PI = 1e-7
TESLA_TO_NT = 1e9

# The chip's linear range (UM16 Table 3-1). Beyond it the output saturates.
SENSOR_RANGE_NT = 800_000.0


# ── Vector helpers ───────────────────────────────────────────────────────────


def add(*vectors: Vec) -> Vec:
    return (sum(v[0] for v in vectors), sum(v[1] for v in vectors), sum(v[2] for v in vectors))


def scale(v: Vec, k: float) -> Vec:
    return (v[0] * k, v[1] * k, v[2] * k)


def dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def norm(v: Vec) -> float:
    return math.sqrt(dot(v, v))


# ── Deterministic randomness ─────────────────────────────────────────────────


def uniform(seed: int, stream: str, index: int) -> float:
    """A uniform number in [0, 1), fixed by (seed, stream, index)."""
    digest = hashlib.blake2b(f"{seed}|{stream}|{index}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def gauss(seed: int, stream: str, index: int) -> float:
    """A standard normal number, fixed by (seed, stream, index) (Box–Muller)."""
    digest = hashlib.blake2b(f"{seed}|{stream}|{index}".encode(), digest_size=16).digest()
    u1 = (int.from_bytes(digest[:8], "big") + 1) / (2**64 + 1)  # (0, 1): log(0) is not an option
    u2 = int.from_bytes(digest[8:], "big") / 2**64
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def unit_vector(seed: int, stream: str, index: int = 0) -> Vec:
    """A direction drawn uniformly over the sphere."""
    z = 2.0 * uniform(seed, stream + ":z", index) - 1.0
    phi = 2.0 * math.pi * uniform(seed, stream + ":phi", index)
    r = math.sqrt(max(0.0, 1.0 - z * z))
    return (r * math.cos(phi), r * math.sin(phi), z)


# ── Time helpers ─────────────────────────────────────────────────────────────


def decimal_year(t: float) -> float:
    moment = datetime.fromtimestamp(t, tz=timezone.utc)
    start = datetime(moment.year, 1, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
    return moment.year + (t - start) / (end - start)


def local_solar_hours(t: float, longitude: float) -> float:
    """Local mean solar time in hours, 0..24: UT plus longitude / 15."""
    return ((t / 3600.0) + longitude / 15.0) % 24.0


def day_of_year(t: float) -> float:
    moment = datetime.fromtimestamp(t, tz=timezone.utc)
    return moment.timetuple().tm_yday - 1 + (moment.hour * 3600 + moment.minute * 60 + moment.second) / 86400.0


# ── Main field: WMM2025 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Site:
    latitude: float  # degrees, WGS-84 geodetic
    longitude: float  # degrees east
    altitude_m: float = 0.0  # above the WGS-84 ellipsoid


@dataclass(frozen=True)
class ReferenceField:
    """The model field at a site and time, with its derived angles."""

    x: float
    y: float
    z: float
    declination_deg: float  # east of true north
    inclination_deg: float  # below the horizontal
    total: float
    horizontal: float

    @property
    def vector(self) -> Vec:
        return (self.x, self.y, self.z)


@lru_cache(maxsize=1)
def _wmm():
    # pygeomag bundles NOAA's WMM_2025.COF unchanged (sha256 checked against
    # NCEI's download); evaluating it reproduces all 100 official WMM2025 test
    # values to 0.0007 nT.
    from pygeomag import GeoMag

    return GeoMag(coefficients_file="wmm/WMM_2025.COF")


def reference_field(site: Site, t: float) -> ReferenceField:
    """WMM2025 at a site and time. Valid 2025.0–2030.0."""
    result = _wmm().calculate(
        glat=site.latitude,
        glon=site.longitude,
        alt=site.altitude_m / 1000.0,
        time=decimal_year(t),
        allow_date_outside_lifespan=True,
    )
    return ReferenceField(
        x=result.x,
        y=result.y,
        z=result.z,
        declination_deg=result.d,
        inclination_deg=result.i,
        total=result.f,
        horizontal=result.h,
    )


def wmm_declination_uncertainty_deg(horizontal_nt: float) -> float:
    """1-sigma WMM2025 declination error: sqrt(0.26^2 + (5417 / H)^2) degrees.

    NOAA's published WMM2025 error model (ncei.noaa.gov, "WMM accuracy"); 0.35°
    at Greenville, where H is about 22,600 nT.
    """
    return math.sqrt(0.26**2 + (5417.0 / max(horizontal_nt, 1.0)) ** 2)


class MainField:
    """WMM2025 at one site, re-evaluated hourly and interpolated in between.

    The secular variation is ~0.3 nT a day, so an hourly grid is exact to far
    below the sensor's resolution while keeping a week of 1 Hz samples cheap.
    """

    STEP_S = 3600.0

    def __init__(self, site: Site):
        self.site = site
        self._cache: dict[int, Vec] = {}

    def _at_grid(self, k: int) -> Vec:
        if k not in self._cache:
            if len(self._cache) > 4096:
                self._cache.clear()
            self._cache[k] = reference_field(self.site, k * self.STEP_S).vector
        return self._cache[k]

    def __call__(self, t: float) -> Vec:
        k = math.floor(t / self.STEP_S)
        frac = t / self.STEP_S - k
        a, b = self._at_grid(k), self._at_grid(k + 1)
        return (a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac, a[2] + (b[2] - a[2]) * frac)


# ── Solar-quiet daily variation ──────────────────────────────────────────────
#
# Fitted to quiet-day 1-minute definitive data from the two INTERMAGNET
# observatories either side of Greenville (FRD Fredericksburg, VA and BSL
# Stennis, MS), averaged, over the GFZ international quiet days Q1–Q5 of each
# month: 315 station-days. Each entry is the night-baseline offset a0 and four
# solar harmonics (24, 12, 8, 6 h) as (amplitude nT, local time of that
# harmonic's maximum in hours). The fits explain 97.6–99.9 % of the variance.
# Seasons: J = May–Aug, E = Mar/Apr/Sep/Oct, D = Nov–Feb. 2019 is solar
# minimum (mean F10.7 69.7), 2024 solar maximum (mean F10.7 191.2).
# Yamazaki & Maute (2017, Space Sci Rev 206:299) describe the same shape: ΔY
# positive in the morning and negative in the afternoon in the northern
# hemisphere, ΔX minimum near local noon, larger in summer and at high solar
# activity.

_SQ_F107 = (69.7, 191.2)

_SQ = {
    # (solar, season): {component: (a0, ((A24, t24), (A12, t12), (A8, t8), (A6, t6)))}
    ("min", "J"): {
        "x": (-4.0, ((7.0, 22.06), (5.2, 3.83), (3.4, 5.74), (1.5, 0.51))),
        "y": (0.0, ((15.4, 4.44), (15.0, 7.35), (7.2, 0.34), (2.0, 2.22))),
        "z": (-1.9, ((5.3, 22.71), (5.9, 4.93), (3.3, 6.83), (0.9, 1.06))),
    },
    ("min", "E"): {
        "x": (-3.0, ((6.6, 23.31), (5.2, 5.11), (1.5, 6.92), (0.2, 1.33))),
        "y": (0.3, ((9.7, 4.89), (9.7, 7.92), (5.0, 1.01), (1.7, 3.49))),
        "z": (-1.9, ((4.3, 23.43), (4.3, 5.37), (2.5, 7.53), (0.7, 2.52))),
    },
    ("min", "D"): {
        "x": (0.7, ((2.2, 0.28), (4.0, 5.56), (1.7, 7.52), (0.8, 2.73))),
        "y": (-0.5, ((4.4, 3.89), (6.6, 8.12), (3.7, 1.23), (2.1, 3.70))),
        "z": (-1.2, ((2.9, 23.21), (2.8, 5.27), (1.8, 7.44), (0.9, 2.59))),
    },
    ("max", "J"): {
        "x": (-3.2, ((7.0, 21.12), (7.4, 3.52), (4.5, 5.91), (1.2, 5.89))),
        "y": (-0.9, ((23.6, 4.74), (19.7, 7.65), (7.7, 0.46), (2.0, 1.64))),
        "z": (-2.8, ((6.8, 23.23), (6.8, 5.21), (3.2, 6.96), (1.0, 0.46))),
    },
    ("max", "E"): {
        "x": (-5.8, ((12.0, 22.78), (8.9, 4.68), (3.5, 6.51), (0.2, 1.04))),
        "y": (-1.0, ((20.9, 5.05), (17.3, 8.09), (8.5, 1.03), (2.4, 3.30))),
        "z": (-4.1, ((7.6, 23.60), (6.2, 5.33), (3.4, 7.40), (0.9, 2.18))),
    },
    ("max", "D"): {
        "x": (-4.2, ((10.3, 0.96), (8.8, 6.13), (4.6, 7.78), (2.0, 2.76))),
        "y": (-0.9, ((11.4, 5.60), (12.5, 8.91), (6.6, 1.74), (3.4, 4.01))),
        "z": (-2.1, ((5.1, 23.44), (4.8, 5.59), (2.8, 7.71), (1.4, 2.74))),
    },
}


def _phasors(entry: tuple[float, tuple[tuple[float, float], ...]]) -> tuple[float, list[complex]]:
    """(a0, harmonics) as a0 and complex phasors, so that blending two fits
    interpolates phase properly instead of averaging clock times."""
    a0, harmonics = entry
    phasors = []
    for m, (amplitude, t_max) in enumerate(harmonics, start=1):
        phasors.append(
            amplitude * complex(math.cos(2 * math.pi * m * t_max / 24.0), -math.sin(2 * math.pi * m * t_max / 24.0))
        )
    return a0, phasors


def _blend(p: tuple[float, list[complex]], q: tuple[float, list[complex]], w: float) -> tuple[float, list[complex]]:
    return p[0] + (q[0] - p[0]) * w, [a + (b - a) * w for a, b in zip(p[1], q[1])]


@dataclass(frozen=True)
class SqModel:
    """The solar-quiet daily variation for a mid-latitude northern site.

    ``f107`` sets solar activity (69.7 → the 2019 fit, 191.2 → the 2024 fit,
    linear in between, clamped outside). The season blends smoothly through
    the year: pure J at midsummer, pure D at midwinter, pure E at the
    equinoxes. ``variability`` scales each local day's amplitude by a factor
    drawn in [1 − v, 1 + v] and shifts its phase by up to ``phase_jitter_h``,
    interpolated between local noons so that no day boundary shows a jump —
    the day-to-day spread measured at FRD and BSL is ±25–40 %.

    The fit is for ~35–40° N in North America. Elsewhere it is a plausible
    shape, not a prediction.
    """

    longitude: float
    f107: float = 120.0
    variability: float = 0.25
    phase_jitter_h: float = 1.0
    scale: float = 1.0
    seed: int = 0

    def _coefficients(self, t: float) -> dict[str, tuple[float, list[complex]]]:
        w_solar = min(1.0, max(0.0, (self.f107 - _SQ_F107[0]) / (_SQ_F107[1] - _SQ_F107[0])))
        s = math.cos(2 * math.pi * (day_of_year(t) - 182.5) / 365.25)  # +1 midsummer, −1 midwinter
        season, w_season = ("J", s) if s >= 0 else ("D", -s)
        out = {}
        for comp in ("x", "y", "z"):
            by_solar = []
            for solar in ("min", "max"):
                equinox = _phasors(_SQ[(solar, "E")][comp])
                solstice = _phasors(_SQ[(solar, season)][comp])
                by_solar.append(_blend(equinox, solstice, w_season))
            out[comp] = _blend(by_solar[0], by_solar[1], w_solar)
        return out

    def _day_factors(self, day: int) -> tuple[float, float]:
        amplitude = 1.0 + self.variability * (2.0 * uniform(self.seed, "sq-amplitude", day) - 1.0)
        shift_h = self.phase_jitter_h * (2.0 * uniform(self.seed, "sq-phase", day) - 1.0)
        return amplitude, shift_h

    def __call__(self, t: float) -> Vec:
        lt = local_solar_hours(t, self.longitude)
        # Local days are numbered from local noon, and the day's random factors
        # are interpolated between consecutive noons (cosine easing), so the
        # variation changes shape smoothly instead of at midnight.
        local_hours_since_epoch = t / 3600.0 + self.longitude / 15.0
        day_position = (local_hours_since_epoch - 12.0) / 24.0
        day = math.floor(day_position)
        frac = day_position - day
        ease = 0.5 - 0.5 * math.cos(math.pi * frac)
        a0_amp, a0_shift = self._day_factors(day)
        a1_amp, a1_shift = self._day_factors(day + 1)
        amplitude = self.scale * (a0_amp + (a1_amp - a0_amp) * ease)
        shift_h = a0_shift + (a1_shift - a0_shift) * ease

        coefficients = self._coefficients(t)
        result = []
        for comp in ("x", "y", "z"):
            a0, phasors = coefficients[comp]
            value = a0
            for m, c in enumerate(phasors, start=1):
                angle = 2 * math.pi * m * (lt - shift_h) / 24.0
                value += (c * complex(math.cos(angle), math.sin(angle))).real
            result.append(amplitude * value)
        return (result[0], result[1], result[2])


# ── Magnetic dipoles ─────────────────────────────────────────────────────────


def dipole_field_nt(moment_am2: Vec, displacement_m: Vec) -> Vec:
    """Field of a point dipole, in nT, at ``displacement_m`` from it.

    B = (μ0/4π) [3 (m·r̂) r̂ − m] / r³. On the dipole's axis |B| = (μ0/4π) 2m/r³
    (200 nT at 1 km for m = 1e9 A·m², 1.6 nT at 5 km); on its equator, half.
    Inside a metre the point-dipole idealisation means nothing, so the
    distance is floored there rather than allowed to blow up.
    """
    rx, ry, rz = displacement_m
    r2 = max(rx * rx + ry * ry + rz * rz, 1.0)
    r = math.sqrt(r2)
    m_dot_r = moment_am2[0] * rx + moment_am2[1] * ry + moment_am2[2] * rz
    k = MU0_OVER_4PI * TESLA_TO_NT / (r2 * r2 * r)
    return (
        k * (3.0 * m_dot_r * rx - moment_am2[0] * r2),
        k * (3.0 * m_dot_r * ry - moment_am2[1] * r2),
        k * (3.0 * m_dot_r * rz - moment_am2[2] * r2),
    )


def dipole_distance_band_m(field_nt: float, moment_am2: float) -> tuple[float, float]:
    """The distances at which a dipole produces ``field_nt``: equator to axis.

    The inverse of the two limiting cases of :func:`dipole_field_nt`, used to
    turn a measured peak into a range estimate.
    """
    if field_nt <= 0:
        return (math.inf, math.inf)
    equatorial = (MU0_OVER_4PI * moment_am2 * TESLA_TO_NT / field_nt) ** (1.0 / 3.0)
    return (equatorial, equatorial * 2.0 ** (1.0 / 3.0))


@dataclass(frozen=True)
class UapPass:
    """A dipole flying a straight line past the sensor at constant speed.

    Geometry relative to the sensor: level flight at ``altitude_m`` above it,
    track ``heading_deg`` from true north, horizontal closest approach
    ``closest_approach_m`` to the right of track at time ``t_cpa``. The
    dipole's direction is fixed in the local frame for the whole pass.
    """

    t_cpa: float
    closest_approach_m: float
    altitude_m: float
    speed_mps: float
    heading_deg: float
    moment: Vec  # A·m², NED

    def position(self, t: float) -> Vec:
        """Dipole position relative to the sensor, NED metres."""
        h = math.radians(self.heading_deg)
        along = (math.cos(h), math.sin(h), 0.0)
        right = (-math.sin(h), math.cos(h), 0.0)
        s = self.speed_mps * (t - self.t_cpa)
        return (
            right[0] * self.closest_approach_m + along[0] * s,
            right[1] * self.closest_approach_m + along[1] * s,
            -self.altitude_m,
        )

    def field(self, t: float) -> Vec:
        p = self.position(t)
        # Displacement from the dipole to the sensor at the origin.
        return dipole_field_nt(self.moment, (-p[0], -p[1], -p[2]))

    def slant_range_m(self, t: float) -> float:
        return norm(self.position(t))


# ── Storms ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Storm:
    """A coarse geomagnetic storm, meant to stress detectors, not to forecast.

    A sudden commencement (a positive step in X over about a minute), a main
    phase that pulls X down towards ``dst_min_nt`` over ``main_phase_h``, an
    exponential recovery over ``recovery_h``, and Pc4–Pc5 pulsations (periods
    45–600 s) riding on the main phase. At ~44° geomagnetic latitude ΔX is
    about 0.72 of Dst; Y and Z get small fixed fractions. Magnitudes follow the
    storm classes of Gonzalez et al. (1994): moderate −50 to −100 nT, intense
    below −100 nT; May 2024 reached −406 nT.
    """

    start: float
    dst_min_nt: float = -150.0
    main_phase_h: float = 6.0
    recovery_h: float = 12.0
    commencement_nt: float = 25.0
    pulsation_nt: float = 6.0
    seed: int = 0
    n_pulsations: int = 6

    def _envelope(self, age_s: float) -> float:
        main = self.main_phase_h * 3600.0
        rise = 1.0 - math.exp(-age_s / (main / 3.0))
        decay = math.exp(-max(0.0, age_s - main) / (self.recovery_h * 3600.0))
        return rise * decay

    def __call__(self, t: float) -> Vec:
        age = t - self.start
        if age < 0:
            return (0.0, 0.0, 0.0)
        commencement = self.commencement_nt * (1.0 - math.exp(-age / 60.0)) * math.exp(-age / 5400.0)
        dst = self.dst_min_nt * self._envelope(age)
        x = 0.72 * dst + commencement
        y = 0.08 * dst
        z = -0.15 * dst
        envelope = self._envelope(age)
        for k in range(self.n_pulsations):
            period = 45.0 + 555.0 * uniform(self.seed, "storm-period", k)
            amplitude = self.pulsation_nt * (0.3 + 0.7 * uniform(self.seed, "storm-amplitude", k))
            phase = 2 * math.pi * uniform(self.seed, "storm-phase", k)
            direction = unit_vector(self.seed, "storm-direction", k)
            value = amplitude * envelope * math.sin(2 * math.pi * age / period + phase)
            x += value * direction[0]
            y += value * direction[1]
            z += value * direction[2]
        return (x, y, z)


# ── Local steps ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Step:
    """A fixed field offset for a while: a car parked by the mast, a gate left
    open. Ramps in and out over ``ramp_s`` so it is a disturbance, not a
    discontinuity."""

    start: float
    duration_s: float
    delta: Vec
    ramp_s: float = 5.0

    def __call__(self, t: float) -> Vec:
        into = t - self.start
        left = self.start + self.duration_s - t
        if into <= 0 or left <= 0:
            return (0.0, 0.0, 0.0)
        weight = min(1.0, into / self.ramp_s, left / self.ramp_s)
        return scale(self.delta, weight)


# ── The whole model ──────────────────────────────────────────────────────────


@dataclass
class FieldModel:
    """Everything outside the sensor, summed. Pure: no state changes on call.

    A week of repeating passes is thousands of dipoles, and a week of 1 Hz
    samples is 600,000 evaluations, so each pass is only evaluated inside the
    window where it contributes more than ``NEGLIGIBLE_NT`` — a thousandth of
    a nanotesla, four orders of magnitude under the sensor's resolution.
    """

    site: Site
    crustal_offset: Vec = (0.0, 0.0, 0.0)
    sq: SqModel | None = None
    storms: Sequence[Storm] = field(default_factory=tuple)
    steps: Sequence[Step] = field(default_factory=tuple)
    passes: Sequence[UapPass] = field(default_factory=tuple)

    NEGLIGIBLE_NT = 1e-3

    def __post_init__(self) -> None:
        self._main = MainField(self.site)
        windows = []
        for p in self.passes:
            # Beyond this range even the dipole's axis is below NEGLIGIBLE_NT.
            reach_m = (MU0_OVER_4PI * TESLA_TO_NT * 2.0 * norm(p.moment) / self.NEGLIGIBLE_NT) ** (1.0 / 3.0)
            half_s = max(0.0, reach_m**2 - p.closest_approach_m**2 - p.altitude_m**2) ** 0.5 / p.speed_mps
            windows.append((p.t_cpa - half_s, p.t_cpa + half_s, p))
        windows.sort(key=lambda w: w[0])
        self._pass_starts = [w[0] for w in windows]
        self._pass_windows = windows
        self._longest_pass_s = max((w[1] - w[0] for w in windows), default=0.0)

    def active_passes(self, t: float) -> list[UapPass]:
        """The passes close enough at ``t`` to matter."""
        hi = bisect.bisect_right(self._pass_starts, t)
        lo = bisect.bisect_left(self._pass_starts, t - self._longest_pass_s)
        return [w[2] for w in self._pass_windows[lo:hi] if w[1] >= t]

    def uap_field(self, t: float) -> Vec:
        return add((0.0, 0.0, 0.0), *(p.field(t) for p in self.active_passes(t)))

    def disturbance(self, t: float) -> Vec:
        """Everything except the main field and crust: what a detector hunts in."""
        parts: list[Vec] = [self.uap_field(t)]
        if self.sq is not None:
            parts.append(self.sq(t))
        parts.extend(storm(t) for storm in self.storms)
        parts.extend(step(t) for step in self.steps)
        return add(*parts)

    def __call__(self, t: float) -> Vec:
        return add(self._main(t), self.crustal_offset, self.disturbance(t))


# ── Sensor mounting ──────────────────────────────────────────────────────────


def rotation_ned_from_sensor(yaw_deg: float, pitch_deg: float, roll_deg: float) -> tuple[Vec, Vec, Vec]:
    """Rows of R, where v_ned = R · v_sensor (aerospace Z-Y-X order).

    Yaw turns the sensor about down (0 = +X north, 90 = +X east), pitch about
    the turned east axis (+X up), roll about the sensor's own X (+Y down).
    ``roll 180`` is a sensor mounted upside down: +Z up.
    """
    cy, sy = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    cr, sr = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
    return (
        (cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr),
        (sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr),
        (-sp, cp * sr, cp * cr),
    )


def ned_to_sensor(rotation: tuple[Vec, Vec, Vec], v_ned: Vec) -> Vec:
    """v_sensor = Rᵀ · v_ned."""
    r = rotation
    return (
        r[0][0] * v_ned[0] + r[1][0] * v_ned[1] + r[2][0] * v_ned[2],
        r[0][1] * v_ned[0] + r[1][1] * v_ned[1] + r[2][1] * v_ned[2],
        r[0][2] * v_ned[0] + r[1][2] * v_ned[1] + r[2][2] * v_ned[2],
    )


def sensor_to_ned(rotation: tuple[Vec, Vec, Vec], v_sensor: Vec) -> Vec:
    r = rotation
    return (dot(r[0], v_sensor), dot(r[1], v_sensor), dot(r[2], v_sensor))
