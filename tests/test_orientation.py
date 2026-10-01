"""Orientation from the field: recover known mountings, and know the limits."""

from datetime import datetime, timezone

import pytest

from retina_magnetometer import orientation as o
from retina_magnetometer.location import Location
from rm3100_sim import physics

GREENVILLE = Location(34.85, -82.39, 300.0, "test")
WHEN = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)
REF = o.reference_field(GREENVILLE, WHEN)
NED = (REF.x, REF.y, REF.z)


def mounted(yaw, pitch=0.0, roll=0.0, offset=(0.0, 0.0, 0.0), scale=1.0):
    """What a sensor mounted this way measures (the simulator's own rotation)."""
    r = physics.rotation_ned_from_sensor(yaw, pitch, roll)
    v = physics.ned_to_sensor(r, NED)
    return tuple(scale * c + d for c, d in zip(v, offset))


def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


class TestReference:
    def test_matches_wmm2025_at_greenville(self):
        assert REF.total == pytest.approx(48564, abs=2)
        assert REF.declination_deg == pytest.approx(-7.03, abs=0.01)
        assert REF.inclination_deg == pytest.approx(62.31, abs=0.01)
        assert REF.declination_sigma_deg == pytest.approx(0.35, abs=0.01)


class TestRecovery:
    @pytest.mark.parametrize("yaw", [0.0, 37.0, 90.0, 181.5, 300.0])
    def test_level_z_down(self, yaw):
        est = o.estimate(mounted(yaw), 60, REF)
        assert est.down_axis == "+Z" and est.up_axis == "-Z"
        assert est.heading_axis == "+X"
        assert angle_diff(est.heading_true_deg, yaw) < 0.01
        assert angle_diff(est.heading_magnetic_deg, yaw - REF.declination_deg) < 0.01
        assert est.tilt_min_deg == pytest.approx(0.0, abs=0.01)
        assert est.verdict == "good"
        assert est.magnitude_ratio == pytest.approx(1.0)

    def test_upside_down_rotated_mount_like_the_demo(self):
        est = o.estimate(mounted(37.0, roll=180.0), 60, REF)
        assert est.down_axis == "-Z" and est.up_axis == "+Z"
        assert angle_diff(est.heading_true_deg, 37.0) < 0.01

    @pytest.mark.parametrize(
        "pitch,roll,down",
        [(0.0, 90.0, "+Y"), (0.0, -90.0, "-Y"), (90.0, 0.0, "-X"), (-90.0, 0.0, "+X")],
    )
    def test_on_its_side(self, pitch, roll, down):
        est = o.estimate(mounted(20.0, pitch, roll), 60, REF)
        assert est.down_axis == down
        assert est.tilt_min_deg == pytest.approx(0.0, abs=0.01)
        assert est.heading_axis == ("+Y" if down in ("+X", "-X") else "+X")

    def test_side_mount_heading_is_the_true_heading_of_the_heading_axis(self):
        # Pitched +90° about east: +X points up, so the heading axis is +Y,
        # which lies along the sensor's turned east axis.
        yaw = 20.0
        r = physics.rotation_ned_from_sensor(yaw, 90.0, 0.0)
        y_in_ned = physics.sensor_to_ned(r, (0.0, 1.0, 0.0))
        import math

        expected = math.degrees(math.atan2(y_in_ned[1], y_in_ned[0])) % 360
        est = o.estimate(mounted(yaw, 90.0, 0.0), 60, REF)
        assert angle_diff(est.heading_true_deg, expected) < 0.01

    def test_tilt_is_bounded_from_below(self):
        est = o.estimate(mounted(10.0, pitch=6.0), 60, REF)
        assert est.down_axis == "+Z"
        assert 0.0 < est.tilt_min_deg <= 6.0 + 1e-6
        assert est.heading_sigma_deg > o.estimate(mounted(10.0), 60, REF).heading_sigma_deg


class TestWarnings:
    def test_hard_iron_offset_flags_the_magnitude(self):
        est = o.estimate(mounted(0.0, offset=(6000.0, 0.0, 0.0)), 60, REF)
        assert est.verdict == "check"
        assert est.magnitude_ratio > 1.0
        assert any("hard-iron" in note for note in est.notes)

    def test_steep_tilt_is_flagged(self):
        est = o.estimate(mounted(0.0, pitch=30.0), 60, REF)
        assert est.verdict == "check"
        assert any("not level" in note for note in est.notes)

    def test_shallow_dip_is_ambiguous(self):
        # Near the magnetic equator the field is almost horizontal: an axis
        # pointing down and one pointing north both sit ~90° / ~0° from it.
        equatorial = o.reference_field(Location(0.0, -50.0, 0.0, "test"), WHEN)
        v = (equatorial.x, equatorial.y, equatorial.z)
        r = physics.rotation_ned_from_sensor(45.0, 0.0, 0.0)
        est = o.estimate(physics.ned_to_sensor(r, v), 60, equatorial)
        assert est.ambiguous and est.verdict == "check"

    def test_always_states_what_cannot_be_known(self):
        est = o.estimate(mounted(0.0), 60, REF)
        assert any("Rotation about the field line" in note for note in est.notes)


class TestWithoutEnough:
    def test_no_location_reports_the_field_only(self):
        est = o.estimate((1.0, 2.0, 3.0), 60, None)
        assert est.verdict == "unknown" and est.down_axis is None
        assert est.measured_total == pytest.approx(14**0.5)
        assert "location" in est.notes[0]

    def test_no_data(self):
        est = o.estimate((0.0, 0.0, 0.0), 0, REF)
        assert est.verdict == "unknown"

    def test_as_dict_is_json_ready(self):
        import json

        json.dumps(o.estimate(mounted(37.0, roll=180.0), 60, REF).as_dict())
        json.dumps(o.estimate((1.0, 2.0, 3.0), 60, None).as_dict())


class TestNoHorizontalField:
    def test_a_field_along_one_axis_gives_no_heading(self):
        # Two axes reading zero (dead coils): the field lies along the third,
        # with no horizontal part to take a heading from. It used to raise
        # ZeroDivisionError, which the page showed as HTTP 500.
        est = o.estimate((0.0, 0.0, 48_000.0), 60, REF)
        assert est.down_axis == "+Z" and est.up_axis == "-Z"
        assert est.heading_true_deg is None and est.heading_magnetic_deg is None and est.heading_sigma_deg is None
        assert est.heading_axis is None and est.verdict == "check"
        assert any("no heading" in note and "Two axes reading zero" in note for note in est.notes)
        import json

        json.dumps(est.as_dict())

    def test_a_small_horizontal_part_gives_none_either(self):
        est = o.estimate((900.0, -900.0, 48_000.0), 60, REF)
        assert est.heading_true_deg is None and est.verdict == "check"
        assert o.estimate((2100.0, 0.0, 48_000.0), 60, REF).heading_true_deg is not None

    def test_near_a_magnetic_pole_the_note_says_so(self):
        pole = o.reference_field(Location(86.0, 140.0, 0.0, "test"), WHEN)
        assert pole.horizontal < o.MIN_HORIZONTAL_NT
        est = o.estimate((pole.x, pole.y, pole.z), 60, pole)
        assert est.heading_true_deg is None
        assert any("magnetic pole" in note for note in est.notes)
