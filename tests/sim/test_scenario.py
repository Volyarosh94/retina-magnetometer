"""Scenario files: parsing, validation, and reproducibility."""

from datetime import datetime, timezone

import pytest

from rm3100_sim import physics
from rm3100_sim import scenario as sc

START = 1_790_000_000.0  # a fixed instant, so nothing depends on "now"
DAY = 86_400.0

MINIMAL = {"site": {"latitude": 34.85, "longitude": -82.39}}
QUIET = {"field": {"diurnal": {"enabled": False}}}
PASS = {"type": "uap_pass", "at": "+10m", "closest_approach_m": 400, "altitude_m": 450, "speed_mps": 120}
STORM = {"type": "storm", "at": "+1h"}
STEP = {"type": "step", "at": "+5m", "duration": "30m", "delta_nt": [35, -12, 48]}


def build(extra=None, **kwargs):
    data = {**MINIMAL, **(extra or {})}
    return sc.from_dict(data, start=START, **kwargs)


def load_yaml(tmp_path, text, **kwargs):
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    return sc.load(str(path), **kwargs)


def first(scenario, kind):
    """The first occurrence of the first event of a kind."""
    return next(e for e in scenario.events if e.kind == kind).series.occurrence(0)


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

    @pytest.mark.parametrize("bad", ["soon", "5 minutes", -1, True, None, [1], float("inf"), float("nan")])
    def test_bad_durations(self, bad):
        with pytest.raises(sc.ScenarioError):
            sc.parse_duration(bad, "x")

    @pytest.mark.parametrize("text", ["14:00", "+14:00", "1:30"])
    def test_two_part_clock_times_are_refused_as_ambiguous(self, text):
        with pytest.raises(sc.ScenarioError, match="hours and minutes or minutes and seconds"):
            sc.parse_duration(text, "x")

    def test_relative_and_absolute_times(self):
        assert sc.parse_time("+10m", START, "x") == START + 600
        assert sc.parse_time("2026-09-30T14:00:00Z", START, "x") == 1_790_776_800.0

    def test_repeats_expand(self):
        s = build({**QUIET, "events": [{**STEP, "at": "+1m", "every": "10m", "count": 3, "duration": "1m"}]})
        steps = s.field_model.steps[0]
        assert [steps.occurrence(k).start for k in range(3)] == [START + 60, START + 660, START + 1260]
        assert s.field_model.disturbance(START + 1260 + 30) != (0.0, 0.0, 0.0)
        assert s.field_model.disturbance(START + 1860 + 30) == (0.0, 0.0, 0.0)  # count stopped it

    def test_repeats_without_a_count_never_end(self):
        s = build({**QUIET, "events": [{**STEP, "at": "0s", "every": "1d", "duration": "1m", "delta_nt": [1, 0, 0]}]})
        for day in (0, 6, 7, 8, 30, 365):
            assert s.field_model.disturbance(START + day * DAY + 30)[0] == pytest.approx(1.0)

    def test_a_month_of_the_demo_has_passes_and_faults_to_the_end(self):
        # What `backfill --days 30` and `serve --speed 60` play: nothing stops after a week.
        s = sc.load("demo", start=START)
        last_day = START + 29 * DAY
        assert physics.norm(s.field_model.uap_field(last_day + 90)) > 100  # a pass at +90 s of every 10 min
        assert any(f.kind == "nack" and f.active(last_day + 15 * 60 + 10) for f in s.faults)

    def test_yaml_timestamps_for_start_and_at(self, tmp_path):
        s = load_yaml(
            tmp_path,
            "start: 2026-09-30T12:00:00Z\n"
            "site: {latitude: 34.85, longitude: -82.39}\n"
            "events:\n"
            "  - {type: step, at: 2026-09-30T12:05:00Z, duration: 1m, delta_nt: [1, 2, 3]}\n",
        )
        noon = datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp()
        assert s.start == noon
        assert s.field_model.steps[0].occurrence(0).start == noon + 300

    def test_unquoted_two_part_clock_is_not_read_as_base_60(self, tmp_path):
        # YAML 1.1 reads 14:00 as the integer 840: it must not become +14 minutes.
        text = "site: {latitude: 34.85, longitude: -82.39}\nevents:\n  - {type: storm, at: 14:00}\n"
        with pytest.raises(sc.ScenarioError, match=r"events\[0\]\.at.*14:00"):
            load_yaml(tmp_path, text, start=START)

    def test_plain_yaml_numbers_still_load(self, tmp_path):
        s = load_yaml(
            tmp_path,
            "site: {latitude: 34.85, longitude: -82.39}\nsensor: {address: 0x21}\n"
            "events:\n  - {type: uap_pass, at: 90, closest_approach_m: 1.5e+3, moment_am2: 1.0e+9}\n",
            start=START,
        )
        assert s.sensor.address == 0x21
        assert first(s, "uap_pass").t_cpa == START + 90 and first(s, "uap_pass").closest_approach_m == 1500


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
            ([{**PASS, "moment_direction": "up"}], "moment_direction"),
            ([{**PASS, "moment_direction": [0, 0, "a"]}], "moment_direction"),
            ([{**PASS, "moment_direction": [0, 0, None]}], "moment_direction"),
            ([{**PASS, "moment_direction": [True, 0, 0]}], "moment_direction"),
            ([{**PASS, "moment_direction": [float("inf"), 0, 0]}], "moment_direction"),
            ([{**PASS, "moment_direction": [0, 0, 0]}], "must not be zero"),
            ([{**PASS, "moment_am2": float("nan")}], "expected a number"),
            ([{"type": "step", "at": "+1m", "duration": "1m", "delta_nt": [1, 2]}], "three numbers"),
            ([{**STEP, "delta_nt": [float("nan"), 0, 0]}], "delta_nt"),
            ([{**STEP, "duration": "0s"}], "longer than zero"),
            ([{"type": "step", "at": "+1m", "count": 2, "duration": "1m", "delta_nt": [1, 2, 3]}], "count only"),
            ([{"type": "step"}], "at is required"),
            (["a pass"], "expected a mapping"),
            ([{**STORM, "id": True}], "expected a name"),
            ([{**STORM, "id": " "}], "expected a name"),
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
            ({"hard_iron_nt": [0, float("inf"), 0]}, "hard_iron_nt"),
        ],
    )
    def test_bad_sensor(self, sensor, match):
        with pytest.raises(sc.ScenarioError, match=match):
            build({"sensor": sensor})

    def test_non_finite_crustal_offset_is_refused(self):
        # A NaN here would pin every axis at full scale.
        with pytest.raises(sc.ScenarioError, match="crustal_offset_nt"):
            build({"field": {"crustal_offset_nt": [float("nan"), 0, 0]}})

    def test_yaml_infinity_is_refused(self, tmp_path):
        text = "site: {latitude: 34.85, longitude: -82.39}\nfield: {crustal_offset_nt: [.inf, 0, 0]}\n"
        with pytest.raises(sc.ScenarioError, match="crustal_offset_nt"):
            load_yaml(tmp_path, text, start=START)

    def test_a_fault_must_be_a_mapping(self):
        with pytest.raises(sc.ScenarioError, match=r"faults\[0\]: expected a mapping"):
            build({"faults": ["nack"]})

    def test_bad_fault_kind(self):
        with pytest.raises(sc.ScenarioError, match="type must be one of"):
            build({"faults": [{"type": "fire", "at": "+1m", "duration": "1s"}]})

    @pytest.mark.parametrize("kind", ["disconnect", "stuck_drdy", "brownout"])
    def test_probability_only_applies_to_nack_faults(self, kind):
        fault = {"type": kind, "at": "+1m", "probability": 0.5}
        if kind != "brownout":
            fault["duration"] = "10s"
        with pytest.raises(sc.ScenarioError, match="probability only applies to nack"):
            build({"faults": [fault]})

    def test_brownout_takes_no_duration(self):
        with pytest.raises(sc.ScenarioError, match="brownout is instantaneous"):
            build({"faults": [{"type": "brownout", "at": "+1m", "duration": "1s"}]})

    def test_faults_must_last(self):
        with pytest.raises(sc.ScenarioError, match="longer than zero"):
            build({"faults": [{"type": "disconnect", "at": "+1m", "duration": 0}]})

    def test_faults_take_no_id(self):
        with pytest.raises(sc.ScenarioError, match="unknown key.*id"):
            build({"faults": [{"type": "nack", "at": "+1m", "duration": "1s", "id": "x"}]})

    @pytest.mark.parametrize(
        "extra",
        [
            {"events": [{**PASS, "every": "0.001s"}]},
            {"events": [{**PASS, "every": 0.5}]},
            {"faults": [{"type": "nack", "at": "+1m", "every": "0.01s", "duration": "60s"}]},
        ],
    )
    def test_occurrences_cannot_pile_up(self, extra):
        with pytest.raises(sc.ScenarioError, match="too short"):
            build(extra)

    @pytest.mark.parametrize("every", [0, "0s", "0.0009s", 1e-300])
    def test_repeats_must_be_at_least_a_millisecond_apart(self, every):
        with pytest.raises(sc.ScenarioError, match="every must be at least 1 ms"):
            build({"faults": [{"type": "brownout", "at": "+1m", "every": every}]})

    def test_dense_but_sane_repeats_are_fine(self):
        build(
            {
                "events": [{**PASS, "every": "10s"}],
                "faults": [{"type": "nack", "at": "0s", "every": "2s", "duration": "1s"}],
            }
        )

    def test_seed_must_be_an_integer(self):
        with pytest.raises(sc.ScenarioError, match="seed"):
            build({"seed": "abc"})

    @pytest.mark.parametrize("when", ["1990-01-01T00:00:00Z", "2024-12-31T23:00:00Z", "2030-01-01T00:00:00Z"])
    def test_start_must_be_inside_wmm2025(self, when):
        with pytest.raises(sc.ScenarioError, match="WMM2025"):
            sc.from_dict({**MINIMAL, "start": when})
        with pytest.raises(sc.ScenarioError, match="WMM2025"):
            sc.from_dict(MINIMAL, start=sc.parse_instant(when, "start"))

    def test_a_bad_start_in_the_file_is_caught_even_when_overridden(self):
        # backfill always overrides the start, and must not hide a typo that serve would hit.
        with pytest.raises(sc.ScenarioError, match="scenario.start"):
            sc.from_dict({**MINIMAL, "start": "tomorrow"}, start=START)
        assert sc.from_dict({**MINIMAL, "start": "2031-01-01T00:00:00Z"}, start=START).start == START


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
        assert s.field_model.passes[0].occurrence(0).moment == pytest.approx((0.0, 1e9, 0.0), abs=1e-3)
        assert s.field_model.passes[1].occurrence(0).moment == pytest.approx((0.0, 0.0, 5.0))

    def test_random_moment_is_fixed_by_the_seed(self):
        event = {"events": [{"type": "uap_pass", "at": "+1m", "every": "5m", "count": 2, "closest_approach_m": 1}]}
        a = build(event, seed=1).field_model.passes[0]
        b = build(event, seed=1).field_model.passes[0]
        c = build(event, seed=2).field_model.passes[0]
        assert a.occurrence(0).moment == b.occurrence(0).moment
        assert a.occurrence(0).moment != a.occurrence(1).moment  # each pass its own dipole
        assert a.occurrence(0).moment != c.occurrence(0).moment

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

    def test_power_cycles_come_from_disconnect_ends_and_brownouts(self):
        s = build(
            {
                "faults": [
                    {"type": "disconnect", "at": "+10s", "every": "1m", "duration": "5s"},
                    {"type": "brownout", "at": "+30s", "every": "1m"},
                    {"type": "nack", "at": "+0s", "duration": "1h"},
                ]
            }
        )
        disconnect, brownout, nack = s.faults
        assert disconnect.power_cycles_between(START, START + 14.9) == 0
        assert disconnect.power_cycles_between(START, START + 15) == 1  # as the window closes
        assert disconnect.power_cycles_between(START + 15, START + 75) == 1
        assert brownout.power_cycles_between(START + 29, START + 30) == 1
        assert brownout.power_cycles_between(START + 30, START + 31) == 0
        assert brownout.power_cycles_between(START, START + 600) == 10
        assert not brownout.active(START + 30)  # nothing to see while it happens
        assert nack.power_cycles_between(START, START + 3600) == 0

    def test_power_cycles_are_counted_without_visiting_each_occurrence(self, monkeypatch):
        # A brown-out every millisecond and a long idle spell: the count is
        # worked out, not walked, so the first transfer after it costs nothing.
        s = build(
            {
                "faults": [
                    {"type": "brownout", "at": "0s", "every": "0.001s"},
                    {"type": "disconnect", "at": "0s", "every": "0.01s", "duration": "0.005s"},
                ]
            }
        )
        looked_up = []
        time_of = physics.Schedule.time

        def counted(schedule, k):
            looked_up.append(k)
            assert len(looked_up) <= 100, "power cycles counted one occurrence at a time"
            return time_of(schedule, k)

        monkeypatch.setattr(physics.Schedule, "time", counted)
        brownout, disconnect = s.faults
        # Ten hours: the bounds sit between occurrences, so the counts are exact.
        assert brownout.power_cycles_between(START + 0.0005, START + 36_000.0005) == 36_000_000
        assert disconnect.power_cycles_between(START + 0.0005, START + 36_000.0005) == 3_600_000


class TestIdentity:
    """An event's random numbers belong to the event, not to its place in the list."""

    def numbers(self, events, *, seed=7, start=START):
        s = sc.from_dict({**MINIMAL, "events": events}, start=start, seed=seed)
        moment = first(s, "uap_pass").moment
        storm = s.field_model.storms[0].occurrence(0)
        return moment, [storm(storm.start + age) for age in (600.0, 3 * 3600.0, 8 * 3600.0)]

    def test_inserting_removing_or_reordering_events_changes_no_other_numbers(self):
        base = self.numbers([PASS, STORM])
        assert self.numbers([STEP, PASS, STORM]) == base
        assert self.numbers([{**PASS, "at": "+3m"}, PASS, STORM])[1] == base[1]
        assert self.numbers([STORM, STEP, PASS]) == base
        two_passes = [{**PASS, "at": "+3m"}, PASS, STORM]
        s = sc.from_dict({**MINIMAL, "events": two_passes}, start=START, seed=7)
        assert s.field_model.passes[1].occurrence(0).moment == base[0]

    def test_numbers_follow_the_events_own_time(self):
        moved = self.numbers([{**PASS, "at": "+11m"}, {**STORM, "at": "+2h"}])
        base = self.numbers([PASS, STORM])
        assert moved[0] != base[0] and moved[1] != base[1]

    def test_an_id_keeps_the_numbers_whatever_the_time(self):
        named = [{**PASS, "id": "north-run"}, {**STORM, "id": "may"}]
        moved = [{**PASS, "id": "north-run", "at": "+11m"}, {**STORM, "id": "may", "at": "+2h"}]
        assert self.numbers(moved) == self.numbers(named)

    def test_relative_times_keep_their_numbers_when_the_start_moves(self):
        assert self.numbers([PASS, STORM], start=START + 3 * DAY) == self.numbers([PASS, STORM])

    def test_the_seed_still_matters(self):
        assert self.numbers([PASS, STORM], seed=8) != self.numbers([PASS, STORM])

    def test_repeats_of_a_storm_are_not_copies(self):
        s = build({"events": [{**STORM, "every": "1d", "count": 2}]}, seed=7)
        storms = s.field_model.storms[0]
        a, b = storms.occurrence(0), storms.occurrence(1)
        assert a(a.start + 3 * 3600) != b(b.start + 3 * 3600)

    def test_events_that_would_share_numbers_need_ids(self):
        with pytest.raises(sc.ScenarioError, match="give one of them an id"):
            build({"events": [PASS, {**PASS, "closest_approach_m": 900}]})
        # An id on the newcomer is enough, and leaves the first pass's numbers as they were.
        alone = build({"events": [PASS]}).field_model.passes[0].occurrence(0).moment
        s = build({"events": [PASS, {**PASS, "id": "b", "closest_approach_m": 900}]})
        assert s.field_model.passes[0].occurrence(0).moment == alone
        assert s.field_model.passes[1].occurrence(0).moment != alone

    def test_events_without_random_numbers_may_share_a_time(self):
        build({"events": [STEP, {**STEP, "delta_nt": [1, 1, 1]}, {**PASS, "moment_direction": "along_track"}, PASS]})

    def test_storms_without_pulsations_draw_nothing_and_may_share_a_time(self):
        calm = {**STORM, "pulsation_nt": 0}
        build({"events": [calm, {**calm, "dst_min_nt": -50}]})
        build({"events": [calm, STORM]})  # only one of the two draws
        with pytest.raises(sc.ScenarioError, match="give one of them an id"):
            build({"events": [STORM, {**STORM, "dst_min_nt": -50}]})

    def test_ids_are_unique(self):
        with pytest.raises(sc.ScenarioError, match="already the id"):
            build({"events": [{**STEP, "id": "car"}, {**STEP, "id": "car", "at": "+2h"}]})


class TestSeed:
    """What a seed changes: exactly the random draws, and nothing else."""

    DETERMINISTIC = {
        "field": {"crustal_offset_nt": [42, -18, 65], "diurnal": {"variability": 0, "phase_jitter_h": 0}},
        "events": [
            {**PASS, "moment_direction": [1, 2, -3]},
            {**STORM, "pulsation_nt": 0},
            STEP,
        ],
        "faults": [{"type": "disconnect", "at": "+20m", "duration": "30s"}],
    }

    def test_the_field_without_random_draws_is_the_same_for_any_seed(self):
        a, b = build(self.DETERMINISTIC, seed=1), build(self.DETERMINISTIC, seed=2)
        for t in (START + 60, START + 600, START + 4000, START + 50_000, START + 2 * DAY):
            assert a.field_model(t) == b.field_model(t)
        assert [f.active(START + 1210) for f in a.faults] == [f.active(START + 1210) for f in b.faults]

    @pytest.mark.parametrize(
        "extra",
        [
            {"field": {"diurnal": {"variability": 0.25, "phase_jitter_h": 0}}},  # each day's amplitude
            {"field": {"diurnal": {"variability": 0, "phase_jitter_h": 1.0}}},  # and its timing
            {"field": {"diurnal": {"enabled": False}}, "events": [PASS]},  # a random dipole direction
            {"field": {"diurnal": {"enabled": False}}, "events": [STORM]},  # storm pulsations
        ],
    )
    def test_each_random_draw_follows_the_seed(self, extra):
        a, b = build(extra, seed=1), build(extra, seed=2)
        times = [START + 600 + 1800 * k for k in range(48)]
        assert [a.field_model(t) for t in times] != [b.field_model(t) for t in times]
