"""Scenario files: the site, the sensor, the events and the faults to simulate.

A scenario is YAML. Everything is optional except the site; the built-in
scenarios under ``rm3100_sim/scenarios/`` are the reference for the format,
and ``python -m rm3100_sim describe <name>`` prints one fully resolved.

Times in ``events`` and ``faults`` are relative to the scenario start
(``+90s``, ``+5m``, ``+01:30:00``) or absolute ISO 8601 (``2026-09-30T14:00Z``).
Durations and repeat intervals take the same relative forms. An event with
``every`` (at least 1 ms) repeats; ``count`` bounds it, and without one it
repeats for as long as anything samples the scenario.

An event's random numbers (a pass's dipole direction, a storm's pulsations)
are keyed by the event's identity, its ``id`` or else its type and ``at``,
never by its place in the list, so adding, removing or moving one event
leaves every other event's numbers alone.

Validation is strict and names the offending key, because a scenario that
silently ignores a typo produces a run that looks right and is not.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from rm3100_sim import physics
from rm3100_sim.physics import Vec

BUILTIN_DIR = Path(__file__).parent / "scenarios"

# More occurrences of one event or fault than this in progress at once is a
# slip (``every: 1`` is a second, not a minute), and would make every sample
# cost thousands of evaluations.
MAX_OVERLAP = 1000

# Repeats closer than this are finer than anything the chip or the app can
# tell apart (three axes take 1.2 ms to convert even at 30 cycles), and far
# closer ones would put consecutive occurrences at one floating-point instant.
MIN_EVERY_S = 0.001


class ScenarioError(ValueError):
    """A scenario file that cannot be simulated as written."""


# ── YAML ─────────────────────────────────────────────────────────────────────

_INT = "tag:yaml.org,2002:int"
_FLOAT = "tag:yaml.org,2002:float"

# PyYAML's YAML 1.1 number patterns without their base-60 forms.
_PLAIN_NUMBERS = {
    _INT: re.compile(
        r"""^(?:[-+]?0b[0-1_]+
            |[-+]?0[0-7_]+
            |[-+]?(?:0|[1-9][0-9_]*)
            |[-+]?0x[0-9a-fA-F_]+)$""",
        re.X,
    ),
    _FLOAT: re.compile(
        r"""^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?
            |\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?
            |[-+]?\.(?:inf|Inf|INF)
            |\.(?:nan|NaN|NAN))$""",
        re.X,
    ),
}


class _Loader(yaml.SafeLoader):
    """PyYAML's safe loader, minus YAML 1.1's base-60 numbers.

    The safe loader reads an unquoted ``14:00`` as the integer 840, so
    ``at: 14:00`` would quietly mean fourteen minutes after the start. Read
    this way it stays the text it is, and the time parser can say what is
    wrong with it.
    """


_Loader.yaml_implicit_resolvers = {
    first: [(tag, _PLAIN_NUMBERS.get(tag, pattern)) for tag, pattern in resolvers]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


# ── Parsing helpers ──────────────────────────────────────────────────────────

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_UNIT_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")
_CLOCK_DURATION = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")
_TWO_PART_CLOCK = re.compile(r"^(\d+):(\d{2})$")


def parse_duration(value: Any, where: str) -> float:
    """Seconds from ``90``, ``"90s"``, ``"5m"``, ``"2h"``, ``"1d"`` or ``"01:30:00"``."""
    if value is None:
        raise ScenarioError(f"{where} is required")
    if isinstance(value, bool):
        raise ScenarioError(f"{where}: expected a duration, got {value!r}")
    if isinstance(value, int | float):
        seconds = float(value)
    elif isinstance(value, str):
        text = value.strip().lstrip("+")
        unit = _UNIT_DURATION.match(text)
        clock = _CLOCK_DURATION.match(text)
        two_part = _TWO_PART_CLOCK.match(text)
        if unit:
            seconds = float(unit.group(1)) * _DURATION_UNITS[unit.group(2)]
        elif clock:
            seconds = int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + float(clock.group(3))
        elif two_part:
            a, b = two_part.groups()
            raise ScenarioError(
                f"{where}: {value!r} could be hours and minutes or minutes and seconds; "
                f"write {int(a):02d}:{b}:00 or 00:{int(a):02d}:{b} (or 90s, 5m, 2h, 1d)"
            )
        else:
            raise ScenarioError(f"{where}: {value!r} is not a duration (try 90s, 5m, 2h, 1d or 01:30:00)")
    else:
        raise ScenarioError(f"{where}: expected a duration, got {value!r}")
    if seconds < 0 or not math.isfinite(seconds):
        raise ScenarioError(f"{where}: duration must be a non-negative number of seconds")
    return seconds


def _is_instant(value: Any) -> bool:
    return isinstance(value, datetime) or (
        isinstance(value, str) and not value.strip().startswith("+") and ("T" in value or "-" in value)
    )


def _anchored(value: Any, start: float, where: str) -> tuple[float, str]:
    """A time, and how an event's identity names it: a relative time by its
    offset and an absolute one by its instant, so the name survives a change
    of start either way."""
    if _is_instant(value):
        instant = parse_instant(value, where)
        return instant, repr(instant)
    offset = parse_duration(value, where)
    return start + offset, f"+{offset!r}"


def parse_time(value: Any, start: float, where: str) -> float:
    """An absolute epoch time from ``+offset`` (relative to ``start``) or ISO 8601."""
    return _anchored(value, start, where)[0]


def parse_instant(value: Any, where: str) -> float:
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if not isinstance(value, str):
        raise ScenarioError(f"{where}: expected an ISO 8601 time such as 2026-09-30T14:00:00Z")
    text = value.strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ScenarioError(f"{where}: {value!r} is not an ISO 8601 time") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _instant_text(t: float) -> str:
    try:
        return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return f"{t:.0f} s from 1970"


def check_span(first: float, last: float) -> None:
    """Refuse a run from ``first`` to ``last`` that leaves the years WMM2025
    covers: the main field outside them would be extrapolated, not modelled."""
    if not physics.WMM_VALID_FROM <= first <= last <= physics.WMM_VALID_UNTIL:
        raise ScenarioError(
            f"{_instant_text(first)} to {_instant_text(last)} goes outside 2025.0-2030.0, the years WMM2025 "
            "covers: the main field there would be extrapolated, not modelled"
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


class _Section:
    """A mapping read key by key, so unknown keys can be reported afterwards."""

    def __init__(self, data: Any, where: str):
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ScenarioError(f"{where}: expected a mapping, got {type(data).__name__}")
        self.data = data
        self.where = where
        self.used: set[str] = set()

    def get(self, key: str, default: Any = None) -> Any:
        self.used.add(key)
        return self.data.get(key, default)

    def number(
        self, key: str, default: float | None = None, *, low: float | None = None, high: float | None = None
    ) -> float:
        self.used.add(key)
        value = self.data.get(key, default)
        if value is None:
            raise ScenarioError(f"{self.where}.{key} is required")
        if not _is_number(value):
            raise ScenarioError(f"{self.where}.{key}: expected a number, got {value!r}")
        if low is not None and value < low:
            raise ScenarioError(f"{self.where}.{key} must be >= {low}, got {value}")
        if high is not None and value > high:
            raise ScenarioError(f"{self.where}.{key} must be <= {high}, got {value}")
        return float(value)

    def vector(self, key: str, default: Vec = (0.0, 0.0, 0.0)) -> Vec:
        self.used.add(key)
        value = self.data.get(key, default)
        if not isinstance(value, list | tuple) or len(value) != 3 or not all(_is_number(v) for v in value):
            raise ScenarioError(f"{self.where}.{key}: expected three numbers [x, y, z], got {value!r}")
        return (float(value[0]), float(value[1]), float(value[2]))

    def finish(self) -> None:
        unknown = sorted(set(self.data) - self.used)
        if unknown:
            raise ScenarioError(f"{self.where}: unknown key(s) {', '.join(unknown)}")


# ── Resolved scenario ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Mounting:
    """How the sensor sits relative to local north/east/down (see physics)."""

    yaw_deg: float = 0.0
    pitch_deg: float = 0.0
    roll_deg: float = 0.0


@dataclass(frozen=True)
class SensorSpec:
    address: int = 0x20
    mounting: Mounting = Mounting()
    noise_scale: float = 1.0
    hard_iron_nt: Vec = (0.0, 0.0, 0.0)
    gain_error: Vec = (0.0, 0.0, 0.0)  # fractional, per sensor axis
    dead_axis: str | None = None  # "x", "y" or "z": that coil's oscillator never runs


@dataclass(frozen=True)
class Event:
    """One entry of ``events``: a storm, a step or a UAP pass, on a schedule.

    Its random numbers are keyed by ``identity`` and the repeat number, never
    by its place in the list.
    """

    kind: str  # uap_pass, storm or step
    identity: str  # "id:<id>" when the event has one, otherwise "<type>@<at>"
    series: physics.Recurring
    random: bool = False  # draws random numbers: a random dipole direction, or a storm's pulsations
    label: str | None = None  # the event's ``id``, if it has one


@dataclass(frozen=True)
class Fault:
    """An I2C-level fault, on a schedule.

    ``nack``: while it lasts, each transfer fails with probability ``probability``.
    ``disconnect``: the sensor is gone (every transfer NACKs); it comes back
    power-cycled, its registers at their defaults, whether or not a transfer
    arrived while it was away.
    ``stuck_drdy``: conversions never raise DRDY.
    ``brownout``: a power dip too short for any transfer to fail. The registers
    return to their defaults (cycle counts 200, continuous mode off) and a
    conversion under way is lost; nothing else shows.
    """

    kind: str
    schedule: physics.Schedule
    duration_s: float = 0.0  # zero for a brownout, which is instantaneous
    probability: float = 1.0

    def active(self, t: float) -> bool:
        for k in self.schedule.near(t - self.duration_s, t):
            begins = self.schedule.time(k)
            if begins <= t < begins + self.duration_s:
                return True
        return False

    def power_cycles_between(self, after: float, until: float) -> int:
        """How many times this fault power-cycles the chip in (after, until]:
        a disconnect as it ends, a brown-out as it happens. Counted in one
        step however many there were, so a long idle spell costs nothing."""
        if self.kind not in ("disconnect", "brownout"):
            return 0
        return self.schedule.occurrences_between(after, until, shift=self.duration_s)


@dataclass
class Scenario:
    name: str
    description: str
    seed: int
    start: float
    field_model: physics.FieldModel
    sensor: SensorSpec
    events: list[Event] = field(default_factory=list)
    faults: list[Fault] = field(default_factory=list)
    source: str = ""

    def sensor_field(self, t: float) -> Vec:
        """What the sensor axes see before its own noise: field, mounting,
        hard-iron offset and gain error. The device model adds the rest."""
        rotation = physics.rotation_ned_from_sensor(
            self.sensor.mounting.yaw_deg, self.sensor.mounting.pitch_deg, self.sensor.mounting.roll_deg
        )
        v = physics.ned_to_sensor(rotation, self.field_model(t))
        v = physics.add(v, self.sensor.hard_iron_nt)
        g = self.sensor.gain_error
        return (v[0] * (1 + g[0]), v[1] * (1 + g[1]), v[2] * (1 + g[2]))


# ── Loading ──────────────────────────────────────────────────────────────────


def builtin_names() -> list[str]:
    return sorted(p.stem for p in BUILTIN_DIR.glob("*.yaml"))


def resolve_path(name_or_path: str) -> Path:
    candidate = Path(name_or_path)
    if candidate.suffix in (".yaml", ".yml") and candidate.is_file():
        return candidate
    builtin = BUILTIN_DIR / f"{name_or_path}.yaml"
    if builtin.exists():
        return builtin
    raise ScenarioError(f"no scenario {name_or_path!r}: not a file, and not one of {', '.join(builtin_names())}")


def load(name_or_path: str, *, start: float | None = None, seed: int | None = None) -> Scenario:
    """Load a scenario by built-in name or path.

    ``start`` and ``seed`` override the file, so the same scenario can be
    replayed live (start = now) or reproduced exactly (fixed start and seed).
    """
    path = resolve_path(name_or_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ScenarioError(f"{path}: cannot be read: {exc}") from exc
    try:
        data = yaml.load(text, Loader=_Loader)  # noqa: S506 - _Loader is a SafeLoader
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path}: not valid YAML: {exc}") from exc
    scenario = from_dict(data, start=start, seed=seed)
    scenario.source = str(path)
    return scenario


def from_dict(data: Any, *, start: float | None = None, seed: int | None = None) -> Scenario:
    top = _Section(data, "scenario")
    name = str(top.get("name", "unnamed"))
    description = str(top.get("description", ""))
    file_seed = top.get("seed", 0)
    if isinstance(file_seed, bool) or not isinstance(file_seed, int):
        raise ScenarioError(f"scenario.seed: expected an integer, got {file_seed!r}")
    seed_value = seed if seed is not None else file_seed

    # The file's start is checked even when overridden, so that a typo in it
    # surfaces wherever the file is used, not only where it is not overridden.
    start_value = top.get("start", "now")
    file_start = None if start_value in (None, "now") else parse_instant(start_value, "scenario.start")
    if start is not None:
        start_epoch = start
    elif file_start is None:
        start_epoch = datetime.now(timezone.utc).timestamp()
    else:
        start_epoch = file_start
    if not physics.WMM_VALID_FROM <= start_epoch < physics.WMM_VALID_UNTIL:
        raise ScenarioError(
            f"the scenario starts at {_instant_text(start_epoch)}, outside 2025.0-2030.0, the years WMM2025 "
            "covers: the main field there would be extrapolated, not modelled"
        )

    site_section = _Section(top.get("site"), "scenario.site")
    site = physics.Site(
        latitude=site_section.number("latitude", low=-90, high=90),
        longitude=site_section.number("longitude", low=-180, high=180),
        altitude_m=site_section.number("altitude_m", 0.0, low=-500, high=10_000),
    )
    site_section.finish()

    field_section = _Section(top.get("field"), "scenario.field")
    crustal = field_section.vector("crustal_offset_nt")
    sq = _parse_diurnal(field_section.get("diurnal", {}), site, seed_value)
    field_section.finish()

    sensor = _parse_sensor(top.get("sensor"))

    event_list = top.get("events", []) or []
    if not isinstance(event_list, list):
        raise ScenarioError("scenario.events: expected a list")
    events: list[Event] = []
    named: dict[str, str] = {}
    drawing: dict[str, str] = {}
    for index, raw in enumerate(event_list):
        where = f"scenario.events[{index}]"
        event = _parse_event(raw, start_epoch, where, seed_value)
        if event.label is not None:
            if event.identity in named:
                raise ScenarioError(f"{where}.id: {event.label!r} is already the id of {named[event.identity]}")
            named[event.identity] = where
        if event.random:
            if event.identity in drawing:
                raise ScenarioError(
                    f"{where}: {drawing[event.identity]} is a {event.kind} at the same time, and the two would "
                    "draw the same random numbers; give one of them an id"
                )
            drawing[event.identity] = where
        events.append(event)

    fault_list = top.get("faults", []) or []
    if not isinstance(fault_list, list):
        raise ScenarioError("scenario.faults: expected a list")
    faults = [_parse_fault(raw, start_epoch, f"scenario.faults[{index}]") for index, raw in enumerate(fault_list)]

    top.finish()
    model = physics.FieldModel(
        site=site,
        crustal_offset=crustal,
        sq=sq,
        storms=tuple(e.series for e in events if e.kind == "storm"),
        steps=tuple(e.series for e in events if e.kind == "step"),
        passes=tuple(e.series for e in events if e.kind == "uap_pass"),
    )
    return Scenario(
        name=name,
        description=description,
        seed=seed_value,
        start=start_epoch,
        field_model=model,
        sensor=sensor,
        events=events,
        faults=faults,
    )


def _parse_diurnal(raw: Any, site: physics.Site, seed: int) -> physics.SqModel | None:
    section = _Section(raw, "scenario.field.diurnal")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ScenarioError("scenario.field.diurnal.enabled: expected true or false")
    model = physics.SqModel(
        longitude=site.longitude,
        f107=section.number("f107", 120.0, low=50, high=400),
        variability=section.number("variability", 0.25, low=0, high=0.9),
        phase_jitter_h=section.number("phase_jitter_h", 1.0, low=0, high=6),
        scale=section.number("scale", 1.0, low=0, high=20),
        seed=seed,
    )
    section.finish()
    return model if enabled else None


def _parse_sensor(raw: Any) -> SensorSpec:
    section = _Section(raw, "scenario.sensor")
    address = section.get("address", 0x20)
    if isinstance(address, bool) or not isinstance(address, int) or address not in (0x20, 0x21, 0x22, 0x23):
        raise ScenarioError(f"scenario.sensor.address must be 0x20..0x23, got {address!r}")
    mounting_section = _Section(section.get("mounting"), "scenario.sensor.mounting")
    mounting = Mounting(
        yaw_deg=mounting_section.number("yaw_deg", 0.0, low=-360, high=360),
        pitch_deg=mounting_section.number("pitch_deg", 0.0, low=-90, high=90),
        roll_deg=mounting_section.number("roll_deg", 0.0, low=-360, high=360),
    )
    mounting_section.finish()
    dead_axis = section.get("dead_axis")
    if dead_axis not in (None, "x", "y", "z"):
        raise ScenarioError(f"scenario.sensor.dead_axis must be x, y or z, got {dead_axis!r}")
    spec = SensorSpec(
        address=address,
        mounting=mounting,
        noise_scale=section.number("noise_scale", 1.0, low=0, high=100),
        hard_iron_nt=section.vector("hard_iron_nt"),
        gain_error=section.vector("gain_error"),
        dead_axis=dead_axis,
    )
    for g in spec.gain_error:
        if not -0.5 < g < 0.5:
            raise ScenarioError("scenario.sensor.gain_error: each axis must be within ±0.5 (±50 %)")
    section.finish()
    return spec


def _schedule(section: _Section, start: float, where: str) -> tuple[physics.Schedule, str]:
    """When an event or fault happens, and how its identity names its ``at``."""
    if "at" not in section.data:
        raise ScenarioError(f"{where}.at is required")
    first, anchor = _anchored(section.get("at"), start, f"{where}.at")
    if "every" not in section.data:
        if "count" in section.data:
            raise ScenarioError(f"{where}.count only makes sense with every")
        return physics.Schedule(first), anchor
    every = parse_duration(section.get("every"), f"{where}.every")
    if every < MIN_EVERY_S:
        raise ScenarioError(f"{where}.every must be at least {MIN_EVERY_S * 1000:g} ms, got {every:g} s")
    count = section.get("count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 1):
        raise ScenarioError(f"{where}.count must be a positive integer")
    return physics.Schedule(first, every, count), anchor


def _check_overlap(schedule: physics.Schedule, span_s: float, where: str) -> None:
    overlap = schedule.overlap(span_s)
    if overlap > MAX_OVERLAP:
        raise ScenarioError(
            f"{where}.every: {schedule.every:g} s is too short: each occurrence lasts {span_s:,.0f} s, "
            f"so {overlap:,} would be in progress at once (at most {MAX_OVERLAP:,})"
        )


_EVENT_KINDS = ("uap_pass", "storm", "step")


def _parse_event(raw: Any, start: float, where: str, seed: int) -> Event:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: expected a mapping")
    section = _Section(raw, where)
    schedule, anchor = _schedule(section, start, where)
    kind = section.get("type")
    if kind not in _EVENT_KINDS:
        raise ScenarioError(f"{where}.type must be uap_pass, storm or step, got {kind!r}")
    label = _label(section.get("id"), f"{where}.id")
    identity = f"id:{label}" if label is not None else f"{kind}@{anchor}"
    if kind == "uap_pass":
        series, random = _uap_pass(section, schedule, where, seed, identity)
    elif kind == "storm":
        series, random = _storm(section, schedule, seed, identity)
    else:
        series, random = _step(section, schedule, where), False
    section.finish()
    _check_overlap(schedule, series.lead_s + series.lag_s, where)
    return Event(kind=kind, identity=identity, series=series, random=random, label=label)


def _label(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, str | int) or not str(value).strip():
        raise ScenarioError(f"{where}: expected a name, got {value!r}")
    return str(value).strip()


def _uap_pass(
    section: _Section, schedule: physics.Schedule, where: str, seed: int, identity: str
) -> tuple[physics.Recurring, bool]:
    moment_am2 = section.number("moment_am2", 1e9, low=0)
    heading = section.number("heading_deg", 90.0, low=-360, high=360)
    geometry = {
        "closest_approach_m": section.number("closest_approach_m", low=0),
        "altitude_m": section.number("altitude_m", 300.0, low=0),
        "speed_mps": section.number("speed_mps", 100.0, low=0.1, high=10_000),
        "heading_deg": heading,
    }
    direction = section.get("moment_direction", "random")
    if direction == "random":
        # Fixed per pass, drawn from the seed and the event's identity: the
        # same pass has the same dipole in every run, different passes differ,
        # and other events coming and going leave it alone.
        stream = f"uap-moment:{identity}"

        def make(k: int, t: float) -> physics.UapPass:
            moment = physics.scale(physics.unit_vector(seed, stream, k), moment_am2)
            return physics.UapPass(t_cpa=t, moment=moment, **geometry)

    else:
        moment = physics.scale(_fixed_direction(direction, heading, f"{where}.moment_direction"), moment_am2)

        def make(k: int, t: float) -> physics.UapPass:
            return physics.UapPass(t_cpa=t, moment=moment, **geometry)

    # Every occurrence has the same geometry and dipole strength, so the same reach.
    reach = physics.UapPass(t_cpa=schedule.first, moment=(moment_am2, 0.0, 0.0), **geometry).reach_s()
    return physics.Recurring(schedule, make, lead_s=reach, lag_s=reach), direction == "random"


def _fixed_direction(direction: Any, heading_deg: float, where: str) -> Vec:
    if direction == "along_track":
        h = math.radians(heading_deg)
        return (math.cos(h), math.sin(h), 0.0)
    if isinstance(direction, list) and len(direction) == 3 and all(_is_number(v) for v in direction):
        length = math.hypot(*direction)
        if length == 0:
            raise ScenarioError(f"{where} must not be zero")
        return (direction[0] / length, direction[1] / length, direction[2] / length)
    raise ScenarioError(f"{where}: expected random, along_track or [north, east, down] numbers, got {direction!r}")


def _storm(section: _Section, schedule: physics.Schedule, seed: int, identity: str) -> tuple[physics.Recurring, bool]:
    shape = {
        "dst_min_nt": section.number("dst_min_nt", -150.0, low=-2000, high=0),
        "main_phase_h": section.number("main_phase_h", 6.0, low=0.1, high=48),
        "recovery_h": section.number("recovery_h", 12.0, low=0.1, high=240),
        "commencement_nt": section.number("commencement_nt", 25.0, low=-200, high=200),
        "pulsation_nt": section.number("pulsation_nt", 6.0, low=0, high=200),
    }

    def make(k: int, t: float) -> physics.Storm:
        # Every storm, and every repeat of one, has pulsations of its own.
        return physics.Storm(start=t, seed=seed, stream=f"storm:{identity}#{k}", **shape)

    # Only the pulsations are random: a storm without them draws nothing.
    return physics.Recurring(schedule, make, lag_s=make(0, schedule.first).reach_s()), shape["pulsation_nt"] > 0


def _step(section: _Section, schedule: physics.Schedule, where: str) -> physics.Recurring:
    duration = parse_duration(section.get("duration"), f"{where}.duration")
    if duration <= 0:
        raise ScenarioError(f"{where}.duration must be longer than zero")
    delta = section.vector("delta_nt")
    ramp = section.number("ramp_s", 5.0, low=0.001)

    def make(k: int, t: float) -> physics.Step:
        return physics.Step(start=t, duration_s=duration, delta=delta, ramp_s=ramp)

    return physics.Recurring(schedule, make, lag_s=duration)


_FAULT_KINDS = ("nack", "disconnect", "stuck_drdy", "brownout")


def _parse_fault(raw: Any, start: float, where: str) -> Fault:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: expected a mapping")
    section = _Section(raw, where)
    schedule, _anchor = _schedule(section, start, where)
    kind = section.get("type")
    if kind not in _FAULT_KINDS:
        raise ScenarioError(f"{where}.type must be one of {', '.join(_FAULT_KINDS)}, got {kind!r}")
    if kind == "brownout":
        if "duration" in raw:
            raise ScenarioError(f"{where}.duration: a brownout is instantaneous and takes no duration")
        duration = 0.0
    else:
        duration = parse_duration(section.get("duration"), f"{where}.duration")
        if duration <= 0:
            raise ScenarioError(f"{where}.duration must be longer than zero")
    if kind != "nack" and "probability" in raw:
        raise ScenarioError(f"{where}.probability only applies to nack faults")
    probability = section.number("probability", 1.0, low=0, high=1) if kind == "nack" else 1.0
    section.finish()
    _check_overlap(schedule, duration, where)
    return Fault(kind=kind, schedule=schedule, duration_s=duration, probability=probability)
