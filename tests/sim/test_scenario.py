"""Scenario files: parsing, validation, and reproducibility."""

import pytest

from rm3100_sim import scenario as sc

START = 1_790_000_000.0  # a fixed instant, so nothing depends on "now"

MINIMAL = {"site": {"latitude": 34.85, "longitude": -82.39}}


def build(extra=None, **kwargs):
    data = {**MINIMAL, **(extra or {})}
    return sc.from_dict(data, start=START, **kwargs)


class TestBuiltins:
    @pytest.mark.parametrize("name", sc.builtin_names())
    def test_every_builtin_loads(self, name):
        loaded = sc.load(name, start=START)
        assert loaded.name == name
        assert 40_000 < sum(v * v for v in loaded.field_model(START + 60)) ** 0.5 < 60_000

    def test_builtins_exist(self):
        assert {"quiet-day", "uap-flyby", "storm", "faults", "demo"} <= set(sc.builtin_names())

    def test_unknown_name(self):
        with pytest.raises(sc.ScenarioError, match="no scenario"):
            sc.load("does-not-exist")


class TestTimes:
    @pytest.mark.parametrize(
        "text,seconds",
        [
            (90, 90.0),
            (1.5, 1.5),
            ("90s", 90.0),
            ("5m", 300.0),
            ("2h", 7200.0),
            ("1d", 86400.0),
            ("+5m", 300.0),
            ("01:30:00", 5400.0),
            ("00:00:02.5", 2.5),
        ],
    )
    def test_durations(self, text, seconds):
        assert sc.parse_duration(text, "x") == seconds

    @pytest.mark.parametrize("bad", ["soon", "5 minutes", -1, True, None, [1]])
    def test_bad_durations(self, bad):
        with pytest.raises(sc.ScenarioError):
            sc.parse_duration(bad, "x")

    def test_relative_and_absolute_times(self):
        assert sc.parse_time("+10m", START, "x") == START + 600
        assert sc.parse_time("2026-09-30T14:00:00Z", START, "x") == 1_790_776_800.0

    def test_repeats_expand(self):
        s = build(
            {
                "events": [
                    {"type": "step", "at": "+1m", "every": "10m", "count": 3, "duration": "1m", "delta_nt": [1, 2, 3]}
                ]
            }
        )
        assert [step.start for step in s.field_model.steps] == [START + 60, START + 660, START + 1260]

    def test_repeats_default_to_a_week(self):
        s = build({"events": [{"type": "step", "at": "0s", "every": "1d", "duration": "1m", "delta_nt": [1, 0, 0]}]})
        assert len(s.field_model.steps) == 8


class TestValidation:
    def test_unknown_top_level_key_is_named(self):
        with pytest.raises(sc.ScenarioError, match="unknown key.*sitee"):
            build({"sitee": {}})

    def test_unknown_event_key_is_named_with_its_index(self):
        with pytest.raises(sc.ScenarioError, match=r"events\[0\].*altitude"):
            build({"events": [{"type": "uap_pass", "at": "+1m", "closest_approach_m": 100, "altitude": 10}]})

    def test_missing_required(self):
        with pytest.raises(sc.ScenarioError, match="latitude is required"):
            sc.from_dict({"site": {"longitude": 1.0}}, start=START)
        with pytest.raises(sc.ScenarioError, match="closest_approach_m is required"):
            build({"events": [{"type": "uap_pass", "at": "+1m"}]})
        with pytest.raises(sc.ScenarioError, match="duration is required"):
            build({"faults": [{"type": "nack", "at": "+1m"}]})

    @pytest.mark.parametrize(
        "events,match",
        [
            ([{"type": "comet", "at": "+1m"}], "type must be"),
            ([{"type": "uap_pass", "at": "+1m", "closest_approach_m": -5}], ">= 0"),
            ([{"type": "uap_pass", "at": "+1m", "closest_approach_m": 5, "speed_mps": "fast"}], "expected a number"),
            (
                [{"type": "uap_pass", "at": "+1m", "closest_approach_m": 5, "moment_direction": "up"}],
                "moment_direction",
            ),
            ([{"type": "step", "at": "+1m", "duration": "1m", "delta_nt": [1, 2]}], "three numbers"),
            ([{"type": "step", "at": "+1m", "count": 2, "duration": "1m", "delta_nt": [1, 2, 3]}], "count only"),
            ([{"type": "step"}], "at is required"),
        ],
    )
    def test_bad_events(self, events, match):
        with pytest.raises(sc.ScenarioError, match=match):
            build({"events": events})

    @pytest.mark.parametrize(
        "sensor,match",
        [
            ({"address": 0x30}, "address"),
            ({"dead_axis": "w"}, "dead_axis"),
            ({"gain_error": [0.9, 0, 0]}, "gain_error"),
            ({"mounting": {"pitch_deg": 95}}, "pitch_deg"),
        ],
    )
    def test_bad_sensor(self, sensor, match):
        with pytest.raises(sc.ScenarioError, match=match):
            build({"sensor": sensor})

    def test_bad_fault_kind(self):
        with pytest.raises(sc.ScenarioError, match="type must be one of"):
            build({"faults": [{"type": "fire", "at": "+1m", "duration": "1s"}]})

    def test_seed_must_be_an_integer(self):
        with pytest.raises(sc.ScenarioError, match="seed"):
            build({"seed": "abc"})


class TestSemantics:
    def test_overrides_win(self):
        s = sc.load("demo", start=START, seed=99)
        assert s.start == START and s.seed == 99

    def test_moment_direction_forms(self):
        s = build(
            {
                "events": [
                    {
                        "type": "uap_pass",
                        "at": "+1m",
                        "closest_approach_m": 1,
                        "moment_direction": "along_track",
                        "heading_deg": 90,
                    },
                    {
                        "type": "uap_pass",
                        "at": "+2m",
                        "closest_approach_m": 1,
                        "moment_direction": [0, 0, 2],
                        "moment_am2": 5,
                    },
                ]
            }
        )
        assert s.field_model.passes[0].moment == pytest.approx((0.0, 1e9, 0.0), abs=1e-3)
        assert s.field_model.passes[1].moment == pytest.approx((0.0, 0.0, 5.0))

    def test_random_moment_is_fixed_by_the_seed(self):
        event = {"events": [{"type": "uap_pass", "at": "+1m", "every": "5m", "count": 2, "closest_approach_m": 1}]}
        a = build(event, seed=1).field_model.passes
        b = build(event, seed=1).field_model.passes
        c = build(event, seed=2).field_model.passes
        assert a[0].moment == b[0].moment
        assert a[0].moment != a[1].moment  # each pass its own dipole
        assert a[0].moment != c[0].moment

    def test_disabled_diurnal(self):
        assert build({"field": {"diurnal": {"enabled": False}}}).field_model.sq is None

    def test_sensor_field_applies_mounting_hard_iron_and_gain(self):
        plain = build()
        mounted = build(
            {"sensor": {"mounting": {"roll_deg": 180}, "hard_iron_nt": [100, 0, 0], "gain_error": [0.1, 0, 0]}}
        )
        t = START + 30
        n = plain.sensor_field(t)
        m = mounted.sensor_field(t)
        assert m[0] == pytest.approx((n[0] + 100) * 1.1)
        assert m[1] == pytest.approx(-n[1])
        assert m[2] == pytest.approx(-n[2])

    def test_fault_windows(self):
        s = build({"faults": [{"type": "nack", "at": "+10s", "duration": "5s", "probability": 0.5}]})
        fault = s.faults[0]
        assert not fault.active(START + 9.9)
        assert fault.active(START + 10) and fault.active(START + 14.9)
        assert not fault.active(START + 15)
