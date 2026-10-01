"""How the sensor is mounted, worked out from the Earth's field alone.

What one field vector can and cannot tell you
---------------------------------------------
The magnetometer measures one vector in its own frame. The World Magnetic
Model says what that vector is in the local north/east/down frame. Matching
the two fixes two of the sensor's three rotational degrees of freedom; the
third, rotation *about* the field line, leaves the measurement unchanged and
cannot be recovered without a second reference such as gravity. RETINA nodes
have no accelerometer on the sensor, so this module does not pretend to a
full attitude. What it reports, and why each part is sound:

1. **The field in the sensor frame** (magnitude and direction). Model-free.
2. **Magnitude consistency** with WMM2025. A reading far from the model
   points at a hard-iron offset from nearby steel or electronics, a gain error
   (boards have been seen ~1.3x off the datasheet), or a local anomaly. At
   Greenville the model's own 1-sigma on the total field is 138 nT (~0.3 %).
3. **Which axis points down.** At mid-latitudes the field dips steeply (62°
   at Greenville), so only one sensor axis can sit within tens of degrees of
   the field-to-vertical angle; the angular mismatch for that axis is a lower
   bound on how far the sensor is tilted from level.
4. **Heading**, assuming that axis is vertical: the direction of the field's
   horizontal part in the sensor frame is magnetic north, and WMM2025's
   declination turns it into true north. This is the compass reading of a
   level-mounted sensor, with an uncertainty that grows with the tilt bound,
   the magnitude mismatch and the model's declination error.

Near the magnetic equator (shallow dip) step 3 becomes ambiguous and the
result says so. Where the field has almost no horizontal part (near a
magnetic pole, or with two dead axes) step 4 has nothing to point along: the
result gives no heading, and says why.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache

from retina_magnetometer.location import Location

Vec = tuple[float, float, float]

_AXES: tuple[tuple[str, Vec], ...] = (
    ("+X", (1.0, 0.0, 0.0)),
    ("-X", (-1.0, 0.0, 0.0)),
    ("+Y", (0.0, 1.0, 0.0)),
    ("-Y", (0.0, -1.0, 0.0)),
    ("+Z", (0.0, 0.0, 1.0)),
    ("-Z", (0.0, 0.0, -1.0)),
)

# Thresholds for the verdict. A level mount within a few degrees is normal
# for a sensor on a bracket; beyond 10° the heading assumption is shaky.
MAGNITUDE_TOLERANCE = 0.05
TILT_WARN_DEG = 10.0
# Below this much horizontal field there is no heading worth giving: NOAA's
# blackout zone around the magnetic poles, where compasses are unreliable, is
# where H < 2,000 nT. A level sensor measures that little only there, or with
# two dead axes, or tilted until its downward axis lies along the field.
MIN_HORIZONTAL_NT = 2000.0


def _dot(a: Vec, b: Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Vec, b: Vec) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(v: Vec) -> float:
    return math.sqrt(_dot(v, v))


def _angle_deg(a: Vec, b: Vec) -> float:
    c = _dot(a, b) / (_norm(a) * _norm(b))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


@dataclass(frozen=True)
class Reference:
    """WMM2025 at the node, in nT and degrees."""

    x: float
    y: float
    z: float
    total: float
    horizontal: float
    declination_deg: float
    inclination_deg: float
    declination_sigma_deg: float
    decimal_year: float


@lru_cache(maxsize=1)
def _wmm():
    from pygeomag import GeoMag

    return GeoMag(coefficients_file="wmm/WMM_2025.COF")


def _decimal_year(moment: datetime) -> float:
    start = datetime(moment.year, 1, 1, tzinfo=timezone.utc)
    end = datetime(moment.year + 1, 1, 1, tzinfo=timezone.utc)
    return moment.year + (moment - start).total_seconds() / (end - start).total_seconds()


def reference_field(location: Location, when: datetime) -> Reference:
    year = _decimal_year(when)
    result = _wmm().calculate(
        glat=location.latitude,
        glon=location.longitude,
        alt=location.altitude_m / 1000.0,
        time=year,
        allow_date_outside_lifespan=True,
    )
    # NOAA's WMM2025 error model: sigma_D = sqrt(0.26^2 + (5417 / H)^2) degrees.
    sigma_d = math.sqrt(0.26**2 + (5417.0 / max(result.h, 1.0)) ** 2)
    return Reference(
        x=result.x,
        y=result.y,
        z=result.z,
        total=result.f,
        horizontal=result.h,
        declination_deg=result.d,
        inclination_deg=result.i,
        declination_sigma_deg=sigma_d,
        decimal_year=year,
    )


@dataclass(frozen=True)
class AxisCandidate:
    axis: str
    tilt_min_deg: float  # lower bound on the tilt if this axis were meant to point down


@dataclass(frozen=True)
class Orientation:
    measured: Vec
    measured_total: float
    samples: int
    reference: Reference | None
    magnitude_ratio: float | None
    down_axis: str | None
    tilt_min_deg: float | None
    ambiguous: bool
    heading_axis: str | None
    heading_magnetic_deg: float | None
    heading_true_deg: float | None
    heading_sigma_deg: float | None
    up_axis: str | None
    verdict: str  # "good", "check" or "unknown"
    notes: tuple[str, ...]
    candidates: tuple[AxisCandidate, ...] = ()

    def as_dict(self) -> dict:
        ref = self.reference
        return {
            "measured": {
                "x": self.measured[0],
                "y": self.measured[1],
                "z": self.measured[2],
                "total": self.measured_total,
            },
            "samples": self.samples,
            "reference": None
            if ref is None
            else {
                "model": "WMM2025",
                "x": ref.x,
                "y": ref.y,
                "z": ref.z,
                "total": ref.total,
                "horizontal": ref.horizontal,
                "declination_deg": ref.declination_deg,
                "inclination_deg": ref.inclination_deg,
                "declination_sigma_deg": ref.declination_sigma_deg,
                "decimal_year": ref.decimal_year,
            },
            "magnitude_ratio": self.magnitude_ratio,
            "down_axis": self.down_axis,
            "up_axis": self.up_axis,
            "tilt_min_deg": self.tilt_min_deg,
            "ambiguous": self.ambiguous,
            "heading_axis": self.heading_axis,
            "heading_magnetic_deg": self.heading_magnetic_deg,
            "heading_true_deg": self.heading_true_deg,
            "heading_sigma_deg": self.heading_sigma_deg,
            "verdict": self.verdict,
            "notes": list(self.notes),
            "candidates": [{"axis": c.axis, "tilt_min_deg": c.tilt_min_deg} for c in self.candidates],
        }


def _opposite(axis: str) -> str:
    return ("-" if axis[0] == "+" else "+") + axis[1]


def _bearing(degrees: float) -> float:
    """An angle as a bearing, in [0, 360). Python's ``%`` gives 360.0 itself
    for an angle a hair below 0 (-1e-14 % 360 rounds up), which is not one."""
    bearing = degrees % 360.0
    return 0.0 if bearing >= 360.0 else bearing


def estimate(measured: Vec, samples: int, reference: Reference | None) -> Orientation:
    """Orientation from a mean field vector (sensor frame, nT) and, if the
    node's location is known, the model field there."""
    total = _norm(measured)
    notes: list[str] = []
    if total <= 0:
        return Orientation(
            measured,
            0.0,
            samples,
            reference,
            None,
            None,
            None,
            False,
            None,
            None,
            None,
            None,
            None,
            "unknown",
            ("No field measured yet.",),
        )
    if reference is None:
        notes.append(
            "The node's location is not known, so there is no model field to compare with. "
            "Set location.rx in the node config or MAGNETOMETER_LATITUDE and MAGNETOMETER_LONGITUDE."
        )
        return Orientation(
            measured,
            total,
            samples,
            None,
            None,
            None,
            None,
            False,
            None,
            None,
            None,
            None,
            None,
            "unknown",
            tuple(notes),
        )

    ratio = total / reference.total
    if abs(ratio - 1.0) > MAGNITUDE_TOLERANCE:
        notes.append(
            f"The measured field is {100 * (ratio - 1):+.1f} % from the model's {reference.total:,.0f} nT. "
            "Nearby steel or electronics (a hard-iron offset), a gain error, or a local anomaly "
            "would do this, and it also skews the heading."
        )

    # The angle the field makes with "down": 90° minus the inclination.
    expected_from_down = 90.0 - reference.inclination_deg
    candidates = sorted(
        (AxisCandidate(name, abs(_angle_deg(measured, unit) - expected_from_down)) for name, unit in _AXES),
        key=lambda c: c.tilt_min_deg,
    )
    best, runner_up = candidates[0], candidates[1]
    # Two axes fitting almost equally well only happens when the dip is
    # shallow (near the magnetic equator) or the sensor sits at ~45°.
    ambiguous = runner_up.tilt_min_deg - best.tilt_min_deg < 10.0
    down = dict(_AXES)[best.axis]
    if ambiguous:
        notes.append(
            f"Both {best.axis} and {runner_up.axis} could be the downward axis; the field's dip here "
            "does not separate them. The heading below assumes the first."
        )
    if best.tilt_min_deg > TILT_WARN_DEG:
        notes.append(
            f"No axis is within {TILT_WARN_DEG:.0f}° of vertical ({best.axis} is closest, at least "
            f"{best.tilt_min_deg:.1f}° off), so the sensor is probably not level and the heading is unreliable."
        )

    # Heading of the first axis that is horizontal when `down` is vertical.
    heading_axis = "+Y" if best.axis in ("+X", "-X") else "+X"
    reference_axis = dict(_AXES)[heading_axis]
    along_down = _dot(measured, down)
    horizontal = (
        measured[0] - along_down * down[0],
        measured[1] - along_down * down[1],
        measured[2] - along_down * down[2],
    )
    h_len = _norm(horizontal)
    if h_len < MIN_HORIZONTAL_NT:
        if reference.horizontal < MIN_HORIZONTAL_NT:
            why = (
                f"This close to a magnetic pole the field is almost vertical (WMM2025's horizontal part here is "
                f"{reference.horizontal:,.0f} nT), and no compass heading is meaningful."
            )
        else:
            why = (
                f"The model expects {reference.horizontal:,.0f} nT. Two axes reading zero would do this, or a "
                "sensor tilted until its downward axis lies along the field."
            )
        notes.append(f"The field has almost no horizontal part here ({h_len:,.0f} nT), so it gives no heading. {why}")
        return Orientation(
            measured=measured,
            measured_total=total,
            samples=samples,
            reference=reference,
            magnitude_ratio=ratio,
            down_axis=best.axis,
            tilt_min_deg=best.tilt_min_deg,
            ambiguous=ambiguous,
            heading_axis=None,
            heading_magnetic_deg=None,
            heading_true_deg=None,
            heading_sigma_deg=None,
            up_axis=_opposite(best.axis),
            verdict="check",
            notes=tuple(notes),
            candidates=tuple(candidates),
        )
    north = (horizontal[0] / h_len, horizontal[1] / h_len, horizontal[2] / h_len)
    # Clockwise from magnetic north, looking down: atan2((n x r) . d, n . r).
    heading_mag = _bearing(
        math.degrees(math.atan2(_dot(_cross(north, reference_axis), down), _dot(north, reference_axis)))
    )
    heading_true = _bearing(heading_mag + reference.declination_deg)

    # Error budget, combined in quadrature: the model's declination error; a
    # tilt of tau shifts the apparent heading by up to ~tau * tan(inclination);
    # an unknown offset as large as the magnitude mismatch rotates the
    # horizontal component by up to mismatch / H.
    tilt_term = best.tilt_min_deg * math.tan(math.radians(min(abs(reference.inclination_deg), 85.0)))
    mismatch_term = math.degrees(abs(total - reference.total) / max(reference.horizontal, 1.0))
    sigma = math.sqrt(reference.declination_sigma_deg**2 + tilt_term**2 + mismatch_term**2)

    verdict = "good"
    if ambiguous or best.tilt_min_deg > TILT_WARN_DEG or abs(ratio - 1.0) > MAGNITUDE_TOLERANCE:
        verdict = "check"
    notes.append(
        "Rotation about the field line cannot be seen by a magnetometer alone; the heading assumes "
        f"{best.axis} is exactly vertical."
    )
    return Orientation(
        measured=measured,
        measured_total=total,
        samples=samples,
        reference=reference,
        magnitude_ratio=ratio,
        down_axis=best.axis,
        tilt_min_deg=best.tilt_min_deg,
        ambiguous=ambiguous,
        heading_axis=heading_axis,
        heading_magnetic_deg=heading_mag,
        heading_true_deg=heading_true,
        heading_sigma_deg=sigma,
        up_axis=_opposite(best.axis),
        verdict=verdict,
        notes=tuple(notes),
        candidates=tuple(candidates),
    )
