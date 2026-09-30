"""Scenario files: the site, the sensor, the events and the faults to simulate.

A scenario is YAML. Everything is optional except the site; the built-in
scenarios under ``rm3100_sim/scenarios/`` are the reference for the format,
and ``python -m rm3100_sim describe <name>`` prints one fully resolved.

Times in ``events`` and ``faults`` are relative to the scenario start
(``+90s``, ``+5m``, ``+01:30:00``) or absolute ISO 8601 (``2026-09-30T14:00Z``).
Durations and repeat intervals take the same relative forms. An event with
``every`` repeats; ``count`` bounds it (default: as many as fit in a week).

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

# Repeating events with no explicit count stop after this long: a week covers
# the longest backfill the tooling offers.
_DEFAULT_REPEAT_HORIZON_S = 7 * 86400.0


class ScenarioError(ValueError):
    """A scenario file that cannot be simulated as written."""


# ── Parsing helpers ──────────────────────────────────────────────────────────

_DURATION_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_UNIT_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhd])$")
_CLOCK_DURATION = re.compile(r"^(\d+):(\d{2}):(\d{2}(?:\.\d+)?)$")


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
        if unit:
            seconds = float(unit.group(1)) * _DURATION_UNITS[unit.group(2)]
        elif clock:
            seconds = int(clock.group(1)) * 3600 + int(clock.group(2)) * 60 + float(clock.group(3))
        else:
            raise ScenarioError(f"{where}: {value!r} is not a duration (try 90s, 5m, 2h, 1d or 01:30:00)")
    else:
        raise ScenarioError(f"{where}: expected a duration, got {value!r}")
    if seconds < 0 or not math.isfinite(seconds):
        raise ScenarioError(f"{where}: duration must be a non-negative number of seconds")
    return seconds


def parse_time(value: Any, start: float, where: str) -> float:
    """An absolute epoch time from ``+offset`` (relative to ``start``) or ISO 8601."""
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    if isinstance(value, str) and not value.strip().startswith("+") and ("T" in value or "-" in value):
        return parse_instant(value, where)
    return start + parse_duration(value, where)


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
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
            raise ScenarioError(f"{self.where}.{key}: expected a number, got {value!r}")
        if low is not None and value < low:
            raise ScenarioError(f"{self.where}.{key} must be >= {low}, got {value}")
        if high is not None and value > high:
            raise ScenarioError(f"{self.where}.{key} must be <= {high}, got {value}")
        return float(value)

    def vector(self, key: str, default: Vec = (0.0, 0.0, 0.0)) -> Vec:
        self.used.add(key)
        value = self.data.get(key, default)
        if (
            not isinstance(value, list | tuple)
            or len(value) != 3
            or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in value)
        ):
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
class Fault:
    """An I2C-level fault for a window of time.

    ``nack``: each transfer fails with probability ``probability``.
    ``disconnect``: the sensor is gone (every transfer NACKs); when it returns
    it has been power-cycled, so its registers are back at their defaults.
    ``stuck_drdy``: conversions never raise DRDY.
    """

    kind: str
    start: float
    duration_s: float
    probability: float = 1.0

    def active(self, t: float) -> bool:
        return self.start <= t < self.start + self.duration_s


@dataclass
class Scenario:
    name: str
    description: str
    seed: int
    start: float
    field_model: physics.FieldModel
    sensor: SensorSpec
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
    if candidate.suffix in (".yaml", ".yml") and candidate.exists():
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
        data = yaml.safe_load(path.read_text())
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

    start_value = top.get("start", "now")
    if start is not None:
        start_epoch = start
    elif start_value in (None, "now"):
        start_epoch = datetime.now(timezone.utc).timestamp()
    else:
        start_epoch = parse_instant(start_value, "scenario.start")

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

    storms: list[physics.Storm] = []
    steps: list[physics.Step] = []
    passes: list[physics.UapPass] = []
    events = top.get("events", []) or []
    if not isinstance(events, list):
        raise ScenarioError("scenario.events: expected a list")
    for index, raw in enumerate(events):
        where = f"scenario.events[{index}]"
        for occurrence, at in enumerate(_occurrences(raw, start_epoch, where)):
            _add_event(raw, at, where, index, occurrence, seed_value, storms, steps, passes)

    faults: list[Fault] = []
    fault_list = top.get("faults", []) or []
    if not isinstance(fault_list, list):
        raise ScenarioError("scenario.faults: expected a list")
    for index, raw in enumerate(fault_list):
        where = f"scenario.faults[{index}]"
        for at in _occurrences(raw, start_epoch, where):
            faults.append(_parse_fault(raw, at, where))

    top.finish()
    model = physics.FieldModel(
        site=site,
        crustal_offset=crustal,
        sq=sq,
        storms=tuple(storms),
        steps=tuple(steps),
        passes=tuple(passes),
    )
    return Scenario(
        name=name,
        description=description,
        seed=seed_value,
        start=start_epoch,
        field_model=model,
        sensor=sensor,
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


def _occurrences(raw: Any, start: float, where: str) -> list[float]:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: expected a mapping")
    if "at" not in raw:
        raise ScenarioError(f"{where}.at is required")
    first = parse_time(raw["at"], start, f"{where}.at")
    if "every" not in raw:
        if "count" in raw:
            raise ScenarioError(f"{where}.count only makes sense with every")
        return [first]
    every = parse_duration(raw["every"], f"{where}.every")
    if every <= 0:
        raise ScenarioError(f"{where}.every must be longer than zero")
    count = raw.get("count")
    if count is None:
        count = int(_DEFAULT_REPEAT_HORIZON_S // every) + 1
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ScenarioError(f"{where}.count must be a positive integer")
    return [first + k * every for k in range(count)]


_EVENT_KEYS = {"type", "at", "every", "count"}


def _add_event(
    raw: dict,
    at: float,
    where: str,
    index: int,
    occurrence: int,
    seed: int,
    storms: list,
    steps: list,
    passes: list,
) -> None:
    section = _Section(raw, where)
    for key in _EVENT_KEYS:
        section.used.add(key)
    kind = raw.get("type")
    if kind == "uap_pass":
        moment_am2 = section.number("moment_am2", 1e9, low=0)
        direction = section.get("moment_direction", "random")
        heading = section.number("heading_deg", 90.0, low=-360, high=360)
        if direction == "random":
            # Fixed per pass, drawn from the scenario seed: the same pass has
            # the same dipole in every run, different passes differ.
            unit = physics.unit_vector(seed, f"uap-moment:{index}", occurrence)
        elif direction == "along_track":
            h = math.radians(heading)
            unit = (math.cos(h), math.sin(h), 0.0)
        elif isinstance(direction, list) and len(direction) == 3:
            length = math.sqrt(sum(float(v) ** 2 for v in direction))
            if length == 0:
                raise ScenarioError(f"{where}.moment_direction must not be zero")
            unit = (float(direction[0]) / length, float(direction[1]) / length, float(direction[2]) / length)
        else:
            raise ScenarioError(f"{where}.moment_direction: expected random, along_track or [n, e, d]")
        passes.append(
            physics.UapPass(
                t_cpa=at,
                closest_approach_m=section.number("closest_approach_m", low=0),
                altitude_m=section.number("altitude_m", 300.0, low=0),
                speed_mps=section.number("speed_mps", 100.0, low=0.1, high=10_000),
                heading_deg=heading,
                moment=physics.scale(unit, moment_am2),
            )
        )
    elif kind == "storm":
        storms.append(
            physics.Storm(
                start=at,
                dst_min_nt=section.number("dst_min_nt", -150.0, low=-2000, high=0),
                main_phase_h=section.number("main_phase_h", 6.0, low=0.1, high=48),
                recovery_h=section.number("recovery_h", 12.0, low=0.1, high=240),
                commencement_nt=section.number("commencement_nt", 25.0, low=-200, high=200),
                pulsation_nt=section.number("pulsation_nt", 6.0, low=0, high=200),
                seed=seed * 1000 + index,
            )
        )
    elif kind == "step":
        steps.append(
            physics.Step(
                start=at,
                duration_s=parse_duration(section.get("duration"), f"{where}.duration"),
                delta=section.vector("delta_nt"),
                ramp_s=section.number("ramp_s", 5.0, low=0.001),
            )
        )
    else:
        raise ScenarioError(f"{where}.type must be uap_pass, storm or step, got {kind!r}")
    section.finish()


_FAULT_KINDS = ("nack", "disconnect", "stuck_drdy")


def _parse_fault(raw: dict, at: float, where: str) -> Fault:
    section = _Section(raw, where)
    for key in ("type", "at", "every", "count"):
        section.used.add(key)
    kind = raw.get("type")
    if kind not in _FAULT_KINDS:
        raise ScenarioError(f"{where}.type must be one of {', '.join(_FAULT_KINDS)}, got {kind!r}")
    fault = Fault(
        kind=kind,
        start=at,
        duration_s=parse_duration(section.get("duration"), f"{where}.duration"),
        probability=section.number("probability", 1.0, low=0, high=1),
    )
    section.finish()
    return fault
