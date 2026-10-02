"""``python -m rm3100_sim``: serve a simulated RM3100, or write simulated data.

    serve     the chip over TCP, for the node app (MAGNETOMETER_BUS=tcp://host:port)
    generate  a scenario as a CSV or JSON-lines file of samples, with the truth
    backfill  days of history into the node app's database, before what it holds
    describe  a scenario fully resolved: site field, events, faults
    list      the built-in scenarios

serve, generate and describe take ``--scenario`` (a built-in name or a YAML
path; describe also takes it as a plain argument), ``--seed`` and ``--start``
(``now`` or ISO 8601), which override the file; the same three values always
produce the same data. backfill takes ``--scenario`` and ``--seed``, and
places the history itself: it ends where the database's history begins, at
its oldest sample or minute summary, or now if there is none, and overwrites
nothing. list takes nothing.

Every run stays within 2025.0-2030.0, the years WMM2025 covers: generate and
backfill refuse a span that leaves them, and serve logs a warning when its
simulated time reaches 2030.0.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from retina_magnetometer.rm3100 import registers as reg
from rm3100_sim import physics
from rm3100_sim import scenario as sc
from rm3100_sim.device import RM3100Model, SimClock, measure_counts
from rm3100_sim.server import SimulatorServer

log = logging.getLogger("rm3100_sim")


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _start(value: str | None) -> float | None:
    if value in (None, "now"):
        return None if value is None else time.time()
    return sc.parse_instant(value, "--start")


def _load(args) -> sc.Scenario:
    return sc.load(args.scenario, start=_start(args.start), seed=args.seed)


# ── Argument types ───────────────────────────────────────────────────────────


# No run or history can be longer than the years WMM2025 covers.
_MODEL_DAYS = (physics.WMM_VALID_UNTIL - physics.WMM_VALID_FROM) / 86_400
# A day of scenario time in under a second of real time: beyond that, a
# server would run through the model's five years in half an hour.
_MAX_SPEED = 100_000.0


def _positive(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {text!r}")
    return value


def _at_most(high: float, what: str):
    """A positive number no larger than ``high``, for argparse."""

    def parse(text: str) -> float:
        value = _positive(text)
        if value > high:
            raise argparse.ArgumentTypeError(f"expected at most {high:,g} {what}, got {text!r}")
        return value

    return parse


def _cycle_count(text: str) -> int:
    # The range the node app accepts, so simulated data is data the app could
    # have recorded.
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not reg.MIN_CYCLE_COUNT <= value <= reg.MAX_CYCLE_COUNT:
        raise argparse.ArgumentTypeError(
            f"expected a cycle count from {reg.MIN_CYCLE_COUNT} to {reg.MAX_CYCLE_COUNT}, got {text!r}"
        )
    return value


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = -1
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"expected a TCP port from 0 to 65535, got {text!r}")
    return value


def _duration(text: str) -> float:
    try:
        seconds = sc.parse_duration(text, "duration")
    except sc.ScenarioError as exc:
        raise argparse.ArgumentTypeError(str(exc).removeprefix("duration: ")) from exc
    if seconds <= 0:
        raise argparse.ArgumentTypeError(f"expected a duration longer than zero, got {text!r}")
    return seconds


# ── serve ────────────────────────────────────────────────────────────────────


def cmd_serve(args) -> int:
    scenario = _load(args)
    model = RM3100Model(scenario, SimClock(scenario.start, speed=args.speed))
    try:
        server = SimulatorServer((args.host, args.port), model)
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc.strerror or exc}", file=sys.stderr)
        return 1

    def stop(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    log.info(
        "RM3100 simulator on %s:%d — scenario %r, seed %d, start %s, speed x%g, address 0x%02X",
        args.host,
        server.server_address[1],
        scenario.name,
        scenario.seed,
        _iso(scenario.start),
        args.speed,
        scenario.sensor.address,
    )
    for line in _describe_lines(scenario):
        log.info("  %s", line)
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        server.server_close()
        stats = model.stats
        log.info(
            "stopped after %d transfers, %d measurements, %d injected NACKs, %d refused while disconnected, "
            "%d writes refused, %d power cycles",
            stats.transfers,
            stats.measurements,
            stats.nacks_injected,
            stats.disconnected_transfers,
            stats.writes_refused,
            stats.power_cycles,
        )
    return 0


# ── generate ─────────────────────────────────────────────────────────────────


def _sample_count(duration_s: float, rate_hz: float) -> int:
    return int(duration_s * rate_hz)


def _samples(scenario: sc.Scenario, start: float, duration_s: float, rate_hz: float, cycle_count: int):
    """(t, x, y, z in nT as the chip reports them, noise-free field, UAP field)."""
    cycle_counts = (cycle_count, cycle_count, cycle_count)
    for k in range(_sample_count(duration_s, rate_hz)):
        t = start + k / rate_hz
        counts = measure_counts(scenario, t, cycle_counts)
        measured = tuple(reg.counts_to_nt(c, cycle_count) for c in counts)
        yield t, measured, scenario.sensor_field(t), scenario.field_model.uap_field(t)


def cmd_generate(args) -> int:
    scenario = _load(args)
    sc.check_span(scenario.start, scenario.start + args.duration)
    if _sample_count(args.duration, args.rate) == 0:
        print(f"nothing to write: {args.duration:g} s at {args.rate:g} Hz is less than one sample", file=sys.stderr)
        return 2
    try:
        out = sys.stdout if args.out == "-" else open(args.out, "w", newline="")  # noqa: SIM115 - closed below
    except OSError as exc:
        print(f"cannot write {args.out}: {exc.strerror or exc}", file=sys.stderr)
        return 1
    try:
        if args.format == "csv":
            writer = csv.writer(out)
            writer.writerow(["time", "t_ms", "x_nt", "y_nt", "z_nt", "x_true_nt", "y_true_nt", "z_true_nt", "uap_nt"])
            for t, m, true, uap in _samples(scenario, scenario.start, args.duration, args.rate, args.cycle_count):
                writer.writerow(
                    [
                        _iso(t),
                        round(t * 1000),
                        *(f"{v:.2f}" for v in m),
                        *(f"{v:.2f}" for v in true),
                        f"{physics.norm(uap):.3f}",
                    ]
                )
        else:
            for t, m, true, uap in _samples(scenario, scenario.start, args.duration, args.rate, args.cycle_count):
                out.write(
                    json.dumps(
                        {
                            "time": _iso(t),
                            "x_nt": round(m[0], 2),
                            "y_nt": round(m[1], 2),
                            "z_nt": round(m[2], 2),
                            "truth": {
                                "x_nt": round(true[0], 2),
                                "y_nt": round(true[1], 2),
                                "z_nt": round(true[2], 2),
                                "uap_nt": round(physics.norm(uap), 3),
                            },
                        }
                    )
                    + "\n"
                )
    finally:
        if out is not sys.stdout:
            out.close()
    return 0


# ── backfill ─────────────────────────────────────────────────────────────────


def _history_start_ms(path: Path) -> int | None:
    """When the history in the app's database begins, in ms: its oldest sample
    or oldest minute summary, whichever is older. None if it holds neither.

    Read past the app's storage layer, which on opening sets up its schema and
    sets an unreadable file aside. A database with its WAL companion files
    present is open in the app (or was left by a crash) and is read through
    them; one without them was closed cleanly, so the main file holds
    everything, and it is read as immutable, which adds no companions of its
    own. Either way nothing is written.
    """
    companions = any(Path(f"{path}{suffix}").exists() for suffix in ("-wal", "-shm"))
    db = sqlite3.connect(f"{path.resolve().as_uri()}?{'mode=ro' if companions else 'immutable=1'}", uri=True)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        # One lone MIN per table, which SQLite answers from the key's index.
        oldest = [
            db.execute(f"SELECT MIN(t_ms) FROM {table}").fetchone()[0]  # noqa: S608 - fixed table names
            for table in ("samples", "minutes")
            if table in tables
        ]
    finally:
        db.close()
    held = [ms for ms in oldest if ms is not None]
    return min(held) if held else None


def cmd_backfill(args) -> int:
    """Write simulated history into the app's database, so a fresh install
    shows what a week of data looks like.

    The history ends where the stored history begins: at the oldest sample or
    the oldest minute summary, whichever is older, and now in an empty
    database. So it never overlaps anything the app recorded. Over samples it
    would interleave, as the app stamps its own at a different millisecond of
    each second; over minute summaries it would replace them, and those
    outlive their samples (seven days of raw samples, a year of minutes). The
    node app need not be stopped: SQLite serialises the writes.

    It only writes. Retention and the size cap are the app's housekeeping:
    on its next pass the app applies the node's own settings to what was
    written here, so samples past its raw retention go and their minute
    summaries stay.

    A refused run leaves no trace. The scenario and the span are checked
    before the database is looked at; the database is read without being
    changed (no schema set up, nothing set aside, no companion files); and it
    is opened for writing only once everything has passed.
    """
    from retina_magnetometer.storage import Storage

    path = Path(args.data_dir) / "magnetometer.sqlite"
    span_s = args.days * 86_400
    total = _sample_count(span_s, args.rate)
    if total == 0:
        print(f"nothing to write: {args.days:g} days at {args.rate:g} Hz is less than one sample", file=sys.stderr)
        return 2
    # Checked as if the history ended now. Ending earlier, where the stored
    # history begins, only moves the start back, so whatever fails here would
    # fail there too.
    now = time.time()
    sc.check_span(now - span_s, now)
    sc.load(args.scenario, start=now - span_s, seed=args.seed)
    end_ms = round(now * 1000)
    if path.exists():
        try:
            held = _history_start_ms(path)
        except sqlite3.Error as exc:
            print(f"cannot read the database in {args.data_dir}: {exc}; it is left as it is", file=sys.stderr)
            return 1
        if held is not None:
            end_ms = min(end_ms, held)
    end = end_ms / 1000.0
    start = end - span_s
    sc.check_span(start, end)
    scenario = sc.load(args.scenario, start=start, seed=args.seed)
    # The writer takes housekeeping settings, which nothing here uses: the app's.
    storage = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=1024)
    written = 0
    started = time.monotonic()
    try:
        # Stamped where the history it describes begins, so the app's own
        # session stays the newest.
        storage.start_session(
            cycle_count=args.cycle_count,
            gain=reg.gain_lsb_per_ut(args.cycle_count),
            rate_hz=args.rate,
            mode="backfill (simulated)",
            bus=f"rm3100_sim scenario {scenario.name}",
            address=scenario.sensor.address,
            started_ms=round(start * 1000),
        )
        batch: list = []
        for t, m, _true, _uap in _samples(scenario, start, span_s, args.rate, args.cycle_count):
            batch.append((round(t * 1000), m[0], m[1], m[2]))
            if len(batch) >= 20_000:
                storage.write_samples(batch)
                written += len(batch)
                batch = []
                log.info("  %d samples (%.0f %%)", written, 100.0 * written / total)
        storage.write_samples(batch)
        written += len(batch)
        # Summaries up to the end of the minute the history ends in. Ending at
        # a minute summary, that is the summary's own start, so none of the
        # app's is touched; ending at a sample, its minute has no summary yet,
        # and gets one that counts both series.
        storage.summarise_range(round(start * 1000), -(-end_ms // 60_000) * 60_000)
    except (OSError, sqlite3.Error) as exc:
        print(f"backfill stopped after {written} samples: {exc}", file=sys.stderr)
        return 1
    log.info(
        "backfilled %d samples over %.1f days, %s to %s, in %.0f s",
        written,
        args.days,
        _iso(start),
        _iso(end),
        time.monotonic() - started,
    )
    return 0


# ── describe / list ──────────────────────────────────────────────────────────


def _span(seconds: float) -> str:
    """A duration as a person would write it: 90 s, 10 min, 2 h, 1 d."""
    for unit, size in (("d", 86_400), ("h", 3_600), ("min", 60)):
        if seconds >= size and seconds % size == 0:
            return f"{seconds / size:g} {unit}"
    return f"{seconds:g} s"


def _when(schedule: physics.Schedule) -> str:
    if schedule.every is None:
        return f"at {_iso(schedule.first)}"
    text = f"every {_span(schedule.every)} from {_iso(schedule.first)}"
    if schedule.count is None:
        return f"{text}, without end"
    return f"{text}, {schedule.count} times (the last at {_iso(schedule.time(schedule.count - 1))})"


def _peak_nt(p: physics.UapPass) -> float:
    """The strongest field over a pass, from 4001 points across it."""
    half_s = 4.0 * max(p.slant_range_m(p.t_cpa), 1.0) / p.speed_mps
    return max(physics.norm(p.field(p.t_cpa + half_s * (k / 2000.0 - 1.0))) for k in range(4001))


def _describe_event(event: sc.Event) -> str:
    name = f" {event.label!r}" if event.label else ""
    when = _when(event.series.schedule)
    first = event.series.occurrence(0)
    if event.kind == "uap_pass":
        slant = first.slant_range_m(first.t_cpa)
        moment = physics.norm(first.moment)
        if event.random:
            # Between the dipole's equator (B = μ0 m / 4π r³) and its axis
            # (twice that), whichever way it points.
            equator = physics.MU0_OVER_4PI * physics.TESLA_TO_NT * moment / max(slant, 1.0) ** 3
            dipole = f"dipole {moment:.3g} A·m² in a random direction: peak {equator:,.0f}–{2 * equator:,.0f} nT"
        else:
            unit = physics.scale(first.moment, 1.0 / moment) if moment else (0.0, 0.0, 0.0)
            dipole = (
                f"dipole {moment:.3g} A·m² along ({unit[0]:.2f}, {unit[1]:.2f}, {unit[2]:.2f}): "
                f"peak {_peak_nt(first):,.0f} nT"
            )
        return (
            f"UAP pass{name} {when}: {first.closest_approach_m:.0f} m abeam, {first.altitude_m:.0f} m up "
            f"({slant:.0f} m slant), {first.speed_mps:g} m/s heading {first.heading_deg:g}°, {dipole}"
        )
    if event.kind == "storm":
        return (
            f"storm{name} {when}: Dst min {first.dst_min_nt:g} nT, main phase {first.main_phase_h:g} h, "
            f"recovery {first.recovery_h:g} h, commencement {first.commencement_nt:g} nT, "
            f"pulsations up to {first.pulsation_nt:g} nT"
        )
    delta = ", ".join(f"{v:g}" for v in first.delta)
    return f"step{name} {when}: [{delta}] nT for {_span(first.duration_s)}, ramped over {_span(first.ramp_s)}"


def _describe_fault(fault: sc.Fault) -> str:
    when = _when(fault.schedule)
    if fault.kind == "nack":
        return f"I2C NACKs {when}: {fault.probability * 100:g} % of transfers fail for {_span(fault.duration_s)}"
    if fault.kind == "disconnect":
        return f"disconnect {when}: off the bus for {_span(fault.duration_s)}, then back power-cycled"
    if fault.kind == "stuck_drdy":
        return f"stuck DRDY {when}: no conversion raises DRDY for {_span(fault.duration_s)}"
    return f"brown-out {when}: the registers go back to their defaults, and no transfer fails"


def _describe_lines(scenario: sc.Scenario) -> list[str]:
    """The scenario as resolved: one line per event and per fault, whatever
    their number of repeats."""
    site = scenario.field_model.site
    ref = physics.reference_field(site, scenario.start)
    m = scenario.sensor.mounting
    lines = [
        f"site {site.latitude:.4f}, {site.longitude:.4f}, {site.altitude_m:.0f} m",
        f"WMM2025 at start: X {ref.x:,.1f} Y {ref.y:,.1f} Z {ref.z:,.1f} nT, |B| {ref.total:,.1f} nT, "
        f"D {ref.declination_deg:+.2f}°, I {ref.inclination_deg:.2f}°",
        f"crustal offset {scenario.field_model.crustal_offset} nT; diurnal "
        + ("off" if scenario.field_model.sq is None else f"F10.7 {scenario.field_model.sq.f107:g}"),
        f"sensor at 0x{scenario.sensor.address:02X}, mounting yaw {m.yaw_deg:g}° pitch {m.pitch_deg:g}° roll {m.roll_deg:g}°, "
        f"noise x{scenario.sensor.noise_scale:g}"
        + (f", dead axis {scenario.sensor.dead_axis}" if scenario.sensor.dead_axis else ""),
    ]
    lines.extend(_describe_event(event) for event in scenario.events)
    lines.extend(_describe_fault(fault) for fault in scenario.faults)
    return lines


def cmd_describe(args) -> int:
    args.scenario = args.name or args.scenario or "demo"
    scenario = _load(args)
    print(f"{scenario.name}: {scenario.description}")
    print(f"  source {scenario.source}, seed {scenario.seed}, start {_iso(scenario.start)}")
    for line in _describe_lines(scenario):
        print(f"  {line}")
    return 0


def cmd_list(_args) -> int:
    for name in sc.builtin_names():
        # Only the description is wanted; any start inside the model's years will do.
        scenario = sc.load(name, start=physics.WMM_VALID_FROM)
        print(f"{name:12s} {scenario.description}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rm3100_sim", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def scenario_args(p, *, start=True):
        p.add_argument("--scenario", default="demo", help="built-in name or YAML path (default: demo)")
        p.add_argument("--seed", type=int, default=None, help="override the scenario's seed")
        if start:
            p.add_argument("--start", default=None, help="'now' or ISO 8601; overrides the scenario")

    p = sub.add_parser("serve", help="serve the chip over TCP")
    scenario_args(p)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=_port, default=9100, help="TCP port, 0 for any free one (default 9100)")
    p.add_argument(
        "--speed",
        type=_at_most(_MAX_SPEED, "scenario seconds per real second"),
        default=1.0,
        help="scenario seconds per real second (default 1)",
    )
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("generate", help="write samples to a file")
    scenario_args(p)
    p.add_argument("--duration", type=_duration, default="1h", help="90s, 5m, 2h, 1d or 01:30:00 (default 1h)")
    p.add_argument("--rate", type=_positive, default=1.0, help="samples per second, up to what the chip allows")
    p.add_argument(
        "--cycle-count",
        type=_cycle_count,
        default=reg.DEFAULT_CYCLE_COUNT,
        help=f"{reg.MIN_CYCLE_COUNT} to {reg.MAX_CYCLE_COUNT} (default {reg.DEFAULT_CYCLE_COUNT})",
    )
    p.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    p.add_argument("--out", default="-", help="file, or - for stdout")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("backfill", help="write simulated history into the app's database, before what it holds")
    scenario_args(p, start=False)
    p.add_argument("--days", type=_at_most(_MODEL_DAYS, "days, the years WMM2025 covers"), default=7.0)
    p.add_argument("--rate", type=_positive, default=1.0, help="samples per second, up to what the chip allows")
    p.add_argument(
        "--cycle-count",
        type=_cycle_count,
        default=reg.DEFAULT_CYCLE_COUNT,
        help=f"{reg.MIN_CYCLE_COUNT} to {reg.MAX_CYCLE_COUNT} (default {reg.DEFAULT_CYCLE_COUNT})",
    )
    p.add_argument("--data-dir", default="data")
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("describe", help="print a scenario fully resolved")
    which = p.add_mutually_exclusive_group()
    which.add_argument("name", nargs="?", metavar="SCENARIO", help="built-in name or YAML path (default: demo)")
    which.add_argument("--scenario", default=None, help="the same, as an option")
    p.add_argument("--seed", type=int, default=None, help="override the scenario's seed")
    p.add_argument("--start", default=None, help="'now' or ISO 8601; overrides the scenario")
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("list", help="list the built-in scenarios")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    rate = getattr(args, "rate", None)
    if rate is not None and rate > reg.max_xyz_rate_hz(args.cycle_count):
        # Data the chip could not have produced: a complete sample takes the
        # conversion time of all three axes.
        sub.choices[args.command].error(
            f"argument --rate: {rate:g} Hz is faster than the chip measures all three axes at "
            f"{args.cycle_count} cycles (at most {reg.max_xyz_rate_hz(args.cycle_count):.1f} Hz)"
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return args.func(args)
    except sc.ScenarioError as exc:
        print(f"scenario error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
