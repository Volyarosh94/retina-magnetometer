"""``python -m rm3100_sim``: serve a simulated RM3100, or write simulated data.

    serve     the chip over TCP, for the node app (MAGNETOMETER_BUS=tcp://host:port)
    generate  a scenario as a CSV or JSON-lines file of samples, with the truth
    backfill  days of history straight into the node app's database
    describe  a scenario fully resolved: site field, events, faults
    list      the built-in scenarios

Everything takes ``--scenario`` (a built-in name or a YAML path), ``--seed`` and
``--start`` (``now`` or ISO 8601), which override the file; the same three
values always produce the same data.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
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


# ── serve ────────────────────────────────────────────────────────────────────


def cmd_serve(args) -> int:
    scenario = _load(args)
    model = RM3100Model(scenario, SimClock(scenario.start, speed=args.speed))
    server = SimulatorServer((args.host, args.port), model)

    def stop(signum, _frame):
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
            "stopped after %d transfers, %d measurements, %d injected NACKs, %d refused while disconnected, %d power cycles",
            stats.transfers,
            stats.measurements,
            stats.nacks_injected,
            stats.disconnected_transfers,
            stats.power_cycles,
        )
    return 0


# ── generate ─────────────────────────────────────────────────────────────────


def _samples(scenario: sc.Scenario, start: float, duration_s: float, rate_hz: float, cycle_count: int):
    """(t, x, y, z in nT as the chip reports them, noise-free field, UAP field)."""
    count = int(duration_s * rate_hz)
    cycle_counts = (cycle_count, cycle_count, cycle_count)
    for k in range(count):
        t = start + k / rate_hz
        counts = measure_counts(scenario, t, cycle_counts)
        measured = tuple(reg.counts_to_nt(c, cycle_count) for c in counts)
        yield t, measured, scenario.sensor_field(t), scenario.field_model.uap_field(t)


def cmd_generate(args) -> int:
    scenario = _load(args)
    duration = sc.parse_duration(args.duration, "--duration")
    out = sys.stdout if args.out == "-" else open(args.out, "w", newline="")  # noqa: SIM115 - closed below
    try:
        if args.format == "csv":
            writer = csv.writer(out)
            writer.writerow(["time", "t_ms", "x_nt", "y_nt", "z_nt", "x_true_nt", "y_true_nt", "z_true_nt", "uap_nt"])
            for t, m, true, uap in _samples(scenario, scenario.start, duration, args.rate, args.cycle_count):
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
            for t, m, true, uap in _samples(scenario, scenario.start, duration, args.rate, args.cycle_count):
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


def cmd_backfill(args) -> int:
    """Write simulated history ending now into the app's database, so a fresh
    install shows what a week of data looks like. The node app need not be
    stopped: SQLite serialises the writes."""
    from retina_magnetometer.storage import Storage

    end = time.time()
    days = args.days
    start = end - days * 86_400
    scenario = sc.load(args.scenario, start=start, seed=args.seed)
    storage = Storage(
        Path(args.data_dir) / "magnetometer.sqlite",
        raw_retention_days=max(days, args.raw_retention_days),
        rollup_retention_days=max(days, 365),
        max_db_mb=args.max_db_mb,
    )
    storage.start_session(
        cycle_count=args.cycle_count,
        gain=reg.gain_lsb_per_ut(args.cycle_count),
        rate_hz=args.rate,
        mode="backfill (simulated)",
        bus=f"rm3100_sim scenario {scenario.name}",
        address=scenario.sensor.address,
    )
    batch: list = []
    written = 0
    started = time.monotonic()
    for t, m, _true, _uap in _samples(scenario, start, days * 86_400, args.rate, args.cycle_count):
        batch.append((round(t * 1000), m[0], m[1], m[2]))
        if len(batch) >= 20_000:
            storage.write_samples(batch)
            written += len(batch)
            batch = []
            log.info("  %d samples (%.0f %%)", written, 100.0 * written / (days * 86_400 * args.rate))
    storage.write_samples(batch)
    written += len(batch)
    storage.summarise_range(round(start * 1000), round(end * 1000))
    log.info("backfilled %d samples over %.1f days in %.0f s", written, days, time.monotonic() - started)
    return 0


# ── describe / list ──────────────────────────────────────────────────────────


def _describe_lines(scenario: sc.Scenario) -> list[str]:
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
    passes = scenario.field_model.passes
    if passes:
        first = passes[0]
        lines.append(
            f"{len(passes)} UAP pass(es); first at {_iso(first.t_cpa)}: {first.closest_approach_m:.0f} m abeam, "
            f"{first.altitude_m:.0f} m up, {first.speed_mps:.0f} m/s, peak ~{physics.norm(first.field(first.t_cpa)):.0f} nT"
        )
    for storm in scenario.field_model.storms:
        lines.append(f"storm at {_iso(storm.start)}, Dst min {storm.dst_min_nt:g} nT")
    for step in scenario.field_model.steps[:3]:
        lines.append(f"step at {_iso(step.start)} for {step.duration_s:.0f} s, {step.delta} nT")
    if len(scenario.field_model.steps) > 3:
        lines.append(f"... and {len(scenario.field_model.steps) - 3} more steps")
    for fault in scenario.faults[:5]:
        lines.append(
            f"fault {fault.kind} at {_iso(fault.start)} for {fault.duration_s:.0f} s (p={fault.probability:g})"
        )
    if len(scenario.faults) > 5:
        lines.append(f"... and {len(scenario.faults) - 5} more faults")
    return lines


def cmd_describe(args) -> int:
    scenario = _load(args)
    print(f"{scenario.name}: {scenario.description}")
    print(f"  source {scenario.source}, seed {scenario.seed}, start {_iso(scenario.start)}")
    for line in _describe_lines(scenario):
        print(f"  {line}")
    return 0


def cmd_list(_args) -> int:
    for name in sc.builtin_names():
        scenario = sc.load(name, start=0.0)
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
    p.add_argument("--port", type=int, default=9100)
    p.add_argument("--speed", type=float, default=1.0, help="scenario seconds per real second (default 1)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("generate", help="write samples to a file")
    scenario_args(p)
    p.add_argument("--duration", default="1h")
    p.add_argument("--rate", type=float, default=1.0, help="samples per second")
    p.add_argument("--cycle-count", type=int, default=reg.DEFAULT_CYCLE_COUNT)
    p.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    p.add_argument("--out", default="-", help="file, or - for stdout")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("backfill", help="write simulated history ending now into the app's database")
    scenario_args(p, start=False)
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--rate", type=float, default=1.0)
    p.add_argument("--cycle-count", type=int, default=reg.DEFAULT_CYCLE_COUNT)
    p.add_argument("--data-dir", default="data")
    p.add_argument("--raw-retention-days", type=float, default=7.0)
    p.add_argument("--max-db-mb", type=float, default=1024.0)
    p.set_defaults(func=cmd_backfill)

    p = sub.add_parser("describe", help="print a scenario fully resolved")
    p.add_argument("scenario", nargs="?", default="demo")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--start", default=None)
    p.set_defaults(func=cmd_describe)

    p = sub.add_parser("list", help="list the built-in scenarios")
    p.set_defaults(func=cmd_list)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    try:
        return args.func(args)
    except sc.ScenarioError as exc:
        print(f"scenario error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
