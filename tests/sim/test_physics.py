"""The simulator's field model against the numbers it was built from."""

import math
import statistics
from datetime import datetime, timezone

import pytest

from rm3100_sim import physics

GREENVILLE = physics.Site(latitude=34.85, longitude=-82.39, altitude_m=300.0)
# 2026-09-30 00:00 UTC, the epoch the NOAA calculator values below are for.
SEP_30_2026 = datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()


def utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class TestDipole:
    def test_task_numbers_on_axis(self):
        m = (1e9, 0.0, 0.0)
        assert physics.norm(physics.dipole_field_nt(m, (1000.0, 0.0, 0.0))) == pytest.approx(200.0, rel=1e-9)
        assert physics.norm(physics.dipole_field_nt(m, (5000.0, 0.0, 0.0))) == pytest.approx(1.6, rel=1e-9)

    def test_equator_is_half_the_axis_and_antiparallel(self):
        m = (0.0, 0.0, 1e9)
        b = physics.dipole_field_nt(m, (1000.0, 0.0, 0.0))
        assert b == pytest.approx((0.0, 0.0, -100.0), abs=1e-9)

    def test_on_axis_field_is_parallel_to_moment(self):
        b = physics.dipole_field_nt((0.0, 1e9, 0.0), (0.0, -2000.0, 0.0))
        assert b[1] > 0 and abs(b[0]) < 1e-12 and abs(b[2]) < 1e-12

    @pytest.mark.parametrize("r", [300.0, 700.0, 1500.0, 4000.0])
    def test_inverse_cube(self, r):
        m = (3e8, -4e8, 5e8)
        direction = (0.6, 0.0, 0.8)
        near = physics.norm(physics.dipole_field_nt(m, physics.scale(direction, r)))
        far = physics.norm(physics.dipole_field_nt(m, physics.scale(direction, 2 * r)))
        assert near / far == pytest.approx(8.0, rel=1e-9)

    def test_distance_band_inverts_both_limits(self):
        low, high = physics.dipole_distance_band_m(100.0, 1e9)
        assert low == pytest.approx(1000.0, rel=1e-9)  # 100 nT is the equator at 1 km
        assert high == pytest.approx(1000.0 * 2 ** (1 / 3), rel=1e-9)
        assert physics.dipole_distance_band_m(0.0, 1e9) == (math.inf, math.inf)


class TestUapPass:
    def test_geometry_at_closest_approach(self):
        p = physics.UapPass(
            t_cpa=100.0,
            closest_approach_m=800.0,
            altitude_m=600.0,
            speed_mps=50.0,
            heading_deg=0.0,
            moment=(0.0, 0.0, 1e9),
        )
        pos = p.position(100.0)
        assert pos == pytest.approx((0.0, 800.0, -600.0))  # east of a northbound track, above
        assert p.slant_range_m(100.0) == pytest.approx(1000.0)
        assert p.slant_range_m(120.0) > p.slant_range_m(100.0)

    def test_field_peaks_near_closest_approach(self):
        p = physics.UapPass(
            t_cpa=0.0,
            closest_approach_m=500.0,
            altitude_m=300.0,
            speed_mps=100.0,
            heading_deg=45.0,
            moment=(0.0, 0.0, 1e9),
        )
        values = {t: physics.norm(p.field(float(t))) for t in range(-60, 61)}
        peak_t = max(values, key=values.get)
        assert abs(peak_t) <= 5
        assert values[60] < values[0] / 20


class TestMainField:
    def test_wmm2025_matches_noaa_calculator_at_greenville(self):
        # NOAA online calculator, WMM-2025, 34.85 N -82.39 E, 300 m, 2026-09-30.
        ref = physics.reference_field(GREENVILLE, SEP_30_2026)
        assert ref.x == pytest.approx(22397.7, abs=0.5)
        assert ref.y == pytest.approx(-2760.5, abs=0.5)
        assert ref.z == pytest.approx(43002.2, abs=0.5)
        assert ref.total == pytest.approx(48564.1, abs=0.5)
        assert ref.declination_deg == pytest.approx(-7.026, abs=0.002)
        assert ref.inclination_deg == pytest.approx(62.310, abs=0.002)

    def test_hourly_interpolation_is_exact_enough(self):
        field = physics.MainField(GREENVILLE)
        t = SEP_30_2026 + 1234.5
        direct = physics.reference_field(GREENVILLE, t).vector
        assert field(t) == pytest.approx(direct, abs=0.01)

    def test_declination_uncertainty_model(self):
        assert physics.wmm_declination_uncertainty_deg(22567.0) == pytest.approx(0.35, abs=0.01)


def sq_series(model, day_start, step_s=60):
    return [(t, model(t)) for t in range(int(day_start), int(day_start + 86400), step_s)]


class TestSqVariation:
    def fixed(self, **kwargs):
        # No day-to-day randomness: the fitted curve itself.
        return physics.SqModel(longitude=-82.39, variability=0.0, phase_jitter_h=0.0, **kwargs)

    def local_midnight(self, year, month, day):
        return utc(year, month, day) - (-82.39 / 15.0) * 3600.0

    def test_equinox_solar_max_ranges_match_the_fit(self):
        model = self.fixed(f107=191.2)
        series = sq_series(model, self.local_midnight(2026, 3, 21))
        for axis, expected in ((0, 32.7), (1, 77.0), (2, 24.1)):
            values = [v[axis] for _, v in series]
            assert max(values) - min(values) == pytest.approx(expected, rel=0.15)

    def test_night_is_quiet(self):
        model = self.fixed(f107=191.2)
        for hours in (0.0, 1.0, 23.0):
            v = model(self.local_midnight(2026, 9, 30) + hours * 3600.0)
            assert max(abs(c) for c in v) < 6.0

    def test_x_minimum_near_local_noon(self):
        model = self.fixed(f107=120)
        start = self.local_midnight(2026, 9, 30)
        series = sq_series(model, start)
        t_min = min(series, key=lambda item: item[1][0])[0]
        assert 9.0 <= (t_min - start) / 3600.0 <= 13.5

    def test_summer_y_positive_morning_negative_afternoon(self):
        model = self.fixed(f107=120)
        start = self.local_midnight(2026, 7, 1)
        series = sq_series(model, start)
        t_max = max(series, key=lambda item: item[1][1])[0]
        t_min = min(series, key=lambda item: item[1][1])[0]
        assert 6.0 <= (t_max - start) / 3600.0 <= 10.5
        assert 12.0 <= (t_min - start) / 3600.0 <= 16.0

    def test_solar_activity_and_season_scale_it(self):
        def y_range(model, day):
            values = [v[1] for _, v in sq_series(model, day)]
            return max(values) - min(values)

        day = self.local_midnight(2026, 9, 21)
        assert y_range(self.fixed(f107=191.2), day) > 1.5 * y_range(self.fixed(f107=69.7), day)
        summer = y_range(self.fixed(f107=100), self.local_midnight(2026, 6, 21))
        winter = y_range(self.fixed(f107=100), self.local_midnight(2026, 12, 21))
        assert summer > 1.5 * winter

    def test_continuous_second_to_second(self):
        model = physics.SqModel(longitude=-82.39, variability=0.3, phase_jitter_h=1.0, seed=5)
        start = self.local_midnight(2026, 9, 30)
        previous = model(start)
        for t in range(int(start) + 1, int(start) + 2 * 86400, 7):
            current = model(float(t))
            assert max(abs(a - b) for a, b in zip(current, previous)) < 0.2
            previous = current

    def test_day_to_day_variability_is_seeded(self):
        t = self.local_midnight(2026, 9, 30) + 11 * 3600
        a = physics.SqModel(longitude=-82.39, seed=1)(t)
        b = physics.SqModel(longitude=-82.39, seed=1)(t)
        c = physics.SqModel(longitude=-82.39, seed=2)(t)
        assert a == b
        assert a != c


class TestStormAndStep:
    def test_storm_shape(self):
        storm = physics.Storm(start=0.0, dst_min_nt=-200.0, pulsation_nt=0.0, commencement_nt=0.0)
        assert storm(-1.0) == (0.0, 0.0, 0.0)
        main_end = storm(6 * 3600.0)
        assert main_end[0] == pytest.approx(0.72 * -200.0 * (1 - math.exp(-3)), rel=1e-6)
        assert storm(30 * 3600.0)[0] > main_end[0] / 3  # recovering

    def test_storm_pulsations_are_bounded(self):
        storm = physics.Storm(start=0.0, dst_min_nt=0.0, commencement_nt=0.0, pulsation_nt=10.0, seed=4)
        values = [physics.norm(storm(float(t))) for t in range(3600, 7200, 5)]
        assert 0 < max(values) <= 10.0 * storm.n_pulsations

    def test_commencement_is_a_fast_positive_x_step(self):
        storm = physics.Storm(start=0.0, dst_min_nt=0.0, pulsation_nt=0.0, commencement_nt=30.0)
        assert storm(300.0)[0] > 25.0

    def test_step_ramps(self):
        step = physics.Step(start=100.0, duration_s=100.0, delta=(10.0, 0.0, -10.0), ramp_s=10.0)
        assert step(99.0) == (0.0, 0.0, 0.0)
        assert step(105.0) == pytest.approx((5.0, 0.0, -5.0))
        assert step(150.0) == pytest.approx((10.0, 0.0, -10.0))
        assert step(201.0) == (0.0, 0.0, 0.0)


class TestRotation:
    def test_is_orthonormal(self):
        r = physics.rotation_ned_from_sensor(37.0, -12.0, 170.0)
        for i in range(3):
            for j in range(3):
                assert physics.dot(r[i], r[j]) == pytest.approx(1.0 if i == j else 0.0, abs=1e-12)

    def test_yaw_90_points_x_east(self):
        r = physics.rotation_ned_from_sensor(90.0, 0.0, 0.0)
        assert physics.sensor_to_ned(r, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0), abs=1e-12)

    def test_roll_180_puts_z_up(self):
        r = physics.rotation_ned_from_sensor(0.0, 0.0, 180.0)
        assert physics.sensor_to_ned(r, (0.0, 0.0, 1.0)) == pytest.approx((0.0, 0.0, -1.0), abs=1e-12)

    def test_positive_pitch_raises_x(self):
        r = physics.rotation_ned_from_sensor(0.0, 30.0, 0.0)
        assert physics.sensor_to_ned(r, (1.0, 0.0, 0.0))[2] < 0

    def test_round_trip(self):
        r = physics.rotation_ned_from_sensor(-123.0, 44.0, 7.0)
        v = (22397.7, -2760.5, 43002.2)
        assert physics.sensor_to_ned(r, physics.ned_to_sensor(r, v)) == pytest.approx(v, abs=1e-9)


class TestDeterministicRandom:
    def test_gauss_is_standard_normal(self):
        draws = [physics.gauss(9, "t", i) for i in range(20_000)]
        assert statistics.fmean(draws) == pytest.approx(0.0, abs=0.03)
        assert statistics.pstdev(draws) == pytest.approx(1.0, abs=0.03)

    def test_same_key_same_number_different_stream_different_number(self):
        assert physics.gauss(1, "noise-x", 42) == physics.gauss(1, "noise-x", 42)
        assert physics.gauss(1, "noise-x", 42) != physics.gauss(1, "noise-y", 42)
        assert physics.gauss(1, "noise-x", 42) != physics.gauss(2, "noise-x", 42)

    def test_unit_vectors_are_unit_and_spread(self):
        vectors = [physics.unit_vector(3, "u", i) for i in range(2000)]
        assert all(physics.norm(v) == pytest.approx(1.0) for v in vectors)
        for axis in range(3):
            assert statistics.fmean(v[axis] for v in vectors) == pytest.approx(0.0, abs=0.05)


class TestFieldModel:
    def test_sum_of_sources(self):
        passes = (
            physics.UapPass(
                t_cpa=SEP_30_2026 + 60,
                closest_approach_m=300,
                altitude_m=300,
                speed_mps=100,
                heading_deg=0,
                moment=(0, 0, 1e9),
            ),
        )
        model = physics.FieldModel(site=GREENVILLE, crustal_offset=(10.0, 20.0, 30.0), sq=None, passes=passes)
        t = SEP_30_2026 + 60
        expected = physics.add(physics.MainField(GREENVILLE)(t), (10.0, 20.0, 30.0), passes[0].field(t))
        assert model(t) == pytest.approx(expected, abs=1e-6)
        assert model.disturbance(t) == pytest.approx(passes[0].field(t))
        assert model.uap_field(t) == pytest.approx(passes[0].field(t))

    def test_total_field_is_about_50000_nt(self):
        model = physics.FieldModel(site=GREENVILLE, sq=physics.SqModel(longitude=GREENVILLE.longitude))
        assert 48_000 < physics.norm(model(SEP_30_2026 + 43_200)) < 49_200
