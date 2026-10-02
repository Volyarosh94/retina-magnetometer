"""The simulator's command line: every subcommand, and reproducibility on disk."""

import csv
import json
import queue
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from retina_magnetometer.rm3100.bus import open_bus
from retina_magnetometer.rm3100.driver import find_rm3100
from retina_magnetometer.storage import Storage
from rm3100_sim import __main__ as cli
from rm3100_sim.__main__ import main

ROOT = Path(__file__).parent.parent.parent
START = "2026-09-30T12:00:00Z"

# quiet-day with no day-to-day randomness in the daily variation.
FIXED_DAILY = """\
name: fixed-daily
seed: 1
site: {latitude: 34.85, longitude: -82.39, altitude_m: 300}
field:
  crustal_offset_nt: [42.0, -18.0, 65.0]
  diurnal: {variability: 0, phase_jitter_h: 0}
"""


@pytest.fixture
def commands_never_run(monkeypatch):
    """For arguments that must stop in the parser: if one ever got through,
    `serve` would sit listening on its default port and `backfill` would write
    into ./data."""
    for name in ("cmd_serve", "cmd_generate", "cmd_backfill"):
        monkeypatch.setattr(cli, name, lambda args, name=name: pytest.fail(f"{name} ran with {vars(args)}"))


def generate(tmp_path, name, *extra):
    out = tmp_path / name
    assert main(["generate", "--start", START, "--duration", "10m", "--out", str(out), *extra]) == 0
    return list(csv.DictReader(out.open()))


def columns(rows, *names):
    return [tuple(row[name] for name in names) for row in rows]


def test_list(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "uap-flyby" in out and "faults" in out


class TestDescribe:
    def test_describe(self, capsys):
        assert main(["describe", "uap-flyby", "--start", START]) == 0
        out = capsys.readouterr().out
        assert "WMM2025 at start" in out and "yaw 37" in out
        # Every pass geometry, not the first occurrence of the first one.
        for abeam in ("400 m abeam", "1300 m abeam", "2800 m abeam"):
            assert abeam in out
        assert out.count("UAP pass every 15 min") == 3 and "without end" in out

    def test_describe_names_every_fault(self, capsys):
        assert main(["describe", "faults", "--start", START]) == 0
        out = capsys.readouterr().out
        for line in ("I2C NACKs every 20 min", "disconnect every 20 min", "stuck DRDY every 20 min", "brown-out"):
            assert line in out

    def test_describe_counted_named_and_fixed_events(self, tmp_path, capsys):
        scenario = tmp_path / "counted.yaml"
        scenario.write_text(
            "site: {latitude: 34.85, longitude: -82.39}\n"
            "events:\n"
            "  - {type: uap_pass, id: overhead, at: +1m, every: 10m, count: 3, closest_approach_m: 0,\n"
            "     altitude_m: 500, speed_mps: 100, moment_direction: [0, 0, 1]}\n"
            "  - {type: step, at: +2m, every: 1h, count: 2, duration: 90s, delta_nt: [1, -2.5, 3]}\n"
        )
        assert main(["describe", str(scenario), "--start", START]) == 0
        out = capsys.readouterr().out
        assert "UAP pass 'overhead' every 10 min from 2026-09-30T12:01:00.000Z, 3 times" in out
        assert "the last at 2026-09-30T12:21:00.000Z" in out
        # Straight overhead with the dipole pointing down: on its axis at 500 m, 2 x 1e9 x 1e-7 / 500^3 T.
        assert "along (0.00, 0.00, 1.00): peak 1,600 nT" in out
        assert "step every 1 h from 2026-09-30T12:02:00.000Z, 2 times" in out
        assert "[1, -2.5, 3] nT for 90 s, ramped over 5 s" in out

    def test_describe_takes_the_scenario_either_way(self, capsys):
        assert main(["describe", "--scenario", "storm", "--start", START]) == 0
        assert "storm at 2026-09-30T12:10" in capsys.readouterr().out
        with pytest.raises(SystemExit) as info:
            main(["describe", "storm", "--scenario", "demo"])
        assert info.value.code == 2

    @pytest.mark.usefixtures("commands_never_run")
    def test_help_says_which_subcommand_takes_what(self, capsys):
        with pytest.raises(SystemExit):
            main(["--help"])
        text = capsys.readouterr().out
        assert "Everything takes" not in text and "backfill takes ``--scenario`` and ``--seed``" in text
        with pytest.raises(SystemExit) as info:
            main(["backfill", "--start", START])
        assert info.value.code == 2


class TestGenerate:
    @pytest.mark.parametrize("scenario", ["uap-flyby", "storm", "demo", "faults"])
    def test_same_arguments_same_bytes(self, tmp_path, scenario):
        a, b = tmp_path / "a.csv", tmp_path / "b.csv"
        args = ["generate", "--scenario", scenario, "--start", START, "--duration", "5m", "--rate", "2"]
        assert main([*args, "--out", str(a)]) == 0
        assert main([*args, "--out", str(b)]) == 0
        assert a.read_bytes() == b.read_bytes()

    def test_generate_csv(self, tmp_path):
        a, c = tmp_path / "a.csv", tmp_path / "c.csv"
        args = ["generate", "--scenario", "uap-flyby", "--start", START, "--duration", "5m", "--rate", "2"]
        assert main([*args, "--out", str(a)]) == 0
        assert main([*args, "--seed", "8", "--out", str(c)]) == 0
        assert a.read_bytes() != c.read_bytes()
        rows = list(csv.DictReader(a.open()))
        assert len(rows) == 600
        # The first pass (2 min in) stands far above the noise in the truth column.
        peak = max(rows, key=lambda r: float(r["uap_nt"]))
        assert float(peak["uap_nt"]) > 300 and peak["time"].startswith("2026-09-30T12:02")

    def test_generate_jsonl(self, tmp_path):
        out = tmp_path / "s.jsonl"
        args = ["--scenario", "quiet-day", "--start", START, "--duration", "10s", "--format", "jsonl"]
        assert main(["generate", *args, "--out", str(out)]) == 0
        lines = [json.loads(line) for line in out.read_text().splitlines()]
        assert len(lines) == 10 and set(lines[0]["truth"]) == {"x_nt", "y_nt", "z_nt", "uap_nt"}

    def test_a_seed_changes_the_noise_and_the_seeded_field_only(self, tmp_path):
        truth = ("x_true_nt", "y_true_nt", "z_true_nt")
        measured = ("x_nt", "y_nt", "z_nt")
        fixed = tmp_path / "fixed-daily.yaml"
        fixed.write_text(FIXED_DAILY)
        one = generate(tmp_path, "1.csv", "--scenario", str(fixed))
        two = generate(tmp_path, "2.csv", "--scenario", str(fixed), "--seed", "2")
        assert columns(one, *truth) == columns(two, *truth)  # nothing in this field is drawn
        assert columns(one, *measured) != columns(two, *measured)  # the noise is
        # quiet-day's daily variation has a seeded day-to-day spread.
        one = generate(tmp_path, "3.csv", "--scenario", "quiet-day")
        two = generate(tmp_path, "4.csv", "--scenario", "quiet-day", "--seed", "2")
        assert columns(one, *truth) != columns(two, *truth)


class TestArguments:
    @pytest.mark.usefixtures("commands_never_run")
    @pytest.mark.parametrize(
        "args",
        [
            ["generate", "--cycle-count", "0"],
            ["generate", "--cycle-count", "many"],
            ["generate", "--rate", "fast"],
            ["serve", "--port", "http"],
            ["generate", "--cycle-count", "29"],
            ["generate", "--cycle-count", "1001"],
            ["generate", "--rate", "0"],
            ["generate", "--rate", "-2"],
            ["generate", "--rate", "nan"],
            ["generate", "--duration", "0s"],
            ["generate", "--duration", "soon"],
            ["serve", "--speed", "0"],
            ["serve", "--speed", "inf"],
            ["serve", "--speed", "1e308"],
            ["serve", "--port", "70000"],
            ["backfill", "--days", "0"],
            ["backfill", "--days", "-1"],
            ["backfill", "--days", "1e308"],
            ["backfill", "--cycle-count", "0"],
            ["generate", "--rate", "1e308"],
            ["generate", "--rate", "147"],  # 200 cycles allow 146.6 complete samples a second
            ["generate", "--cycle-count", "800", "--rate", "38"],
            ["backfill", "--rate", "1e308"],
        ],
    )
    def test_out_of_range_arguments_are_usage_errors(self, args, capsys):
        with pytest.raises(SystemExit) as info:
            main(args)
        assert info.value.code == 2
        assert "error: argument" in capsys.readouterr().err

    def test_less_than_one_sample_is_an_error_not_an_empty_file(self, tmp_path, capsys):
        out = tmp_path / "s.csv"
        assert main(["generate", "--start", START, "--duration", "1s", "--rate", "0.5", "--out", str(out)]) == 2
        assert "nothing to write" in capsys.readouterr().err and not out.exists()

    def test_the_fastest_rate_the_chip_allows_is_accepted(self, tmp_path):
        out = tmp_path / "s.csv"
        assert main(["generate", "--start", START, "--duration", "1s", "--rate", "146", "--out", str(out)]) == 0
        assert len(out.read_text().splitlines()) == 147

    def test_a_span_leaving_wmm2025_is_refused(self, tmp_path, capsys):
        out = tmp_path / "s.csv"
        late = ["generate", "--start", "2029-12-31T23:59:00Z", "--out", str(out)]
        assert main([*late, "--duration", "2m"]) == 2
        assert "WMM2025" in capsys.readouterr().err and not out.exists()
        assert main([*late, "--duration", "1m"]) == 0  # up to 2030.0 itself is still the model

    @pytest.mark.parametrize("bad", ["directory", "latin-1"])
    def test_an_unreadable_scenario_file_is_a_scenario_error(self, tmp_path, capsys, bad):
        path = tmp_path / "scenario.yaml"
        if bad == "directory":
            path.mkdir()
        else:
            path.write_bytes("description: caf\xe9\n".encode("latin-1"))
        assert main(["describe", str(path)]) == 2
        assert "scenario error" in capsys.readouterr().err

    def test_unwritable_output_is_a_message_not_a_traceback(self, tmp_path, capsys):
        out = tmp_path / "missing" / "s.csv"
        assert main(["generate", "--start", START, "--duration", "10s", "--out", str(out)]) == 1
        assert "cannot write" in capsys.readouterr().err

    def test_a_start_outside_wmm2025_is_a_scenario_error(self, capsys):
        assert main(["describe", "demo", "--start", "1990-01-01T00:00Z"]) == 2
        assert "WMM2025" in capsys.readouterr().err


class TestBackfill:
    def test_backfill_writes_history_the_app_can_read(self, tmp_path):
        args = ["backfill", "--scenario", "quiet-day", "--days", "0.05", "--rate", "1", "--data-dir", str(tmp_path)]
        assert main(args) == 0
        storage = Storage(
            tmp_path / "magnetometer.sqlite", raw_retention_days=7, rollup_retention_days=365, max_db_mb=64
        )
        stats = storage.stats()
        assert stats["samples"] == 4320 and stats["minutes"] >= 70
        assert storage.sessions()[0]["mode"] == "backfill (simulated)"

    def test_backfill_ends_where_the_stored_history_begins(self, tmp_path):
        path = tmp_path / "magnetometer.sqlite"
        storage = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
        # Five minutes the app has recorded, each sample 3 ms past the second.
        first = (int(time.time()) - 300) * 1000 + 3
        storage.start_session(cycle_count=200, gain=74.92, rate_hz=1.0, mode="poll", bus="tcp://sim:9100", address=32)
        app = [(first + 1000 * k, 22_000.0, -2_700.0, 43_000.0) for k in range(300)]
        storage.write_samples(app)

        args = ["backfill", "--scenario", "quiet-day", "--days", "0.01", "--data-dir", str(tmp_path)]
        assert main(args) == 0

        rows = storage.raw_rows(0, first + 10**9, 10**6)
        backfilled = [row for row in rows if row[0] < first]
        assert len(backfilled) == 864
        assert rows[len(backfilled) :] == app  # nothing interleaved with what the app wrote
        assert first - 1000 <= backfilled[-1][0] < first  # and no gap where they meet
        with sqlite3.connect(path) as db:
            n_max, n_total = db.execute("SELECT MAX(n), SUM(n) FROM minutes").fetchone()
            meet = db.execute("SELECT n FROM minutes WHERE t_ms = ?", (first // 60_000 * 60_000,)).fetchone()[0]
        assert n_max <= 60  # no minute counts a sample twice
        in_meeting_minute = [row for row in rows if row[0] // 60_000 == first // 60_000]
        assert meet == len(in_meeting_minute)  # both series, each once
        assert n_total == len(backfilled) + len([row for row in app if row[0] // 60_000 == first // 60_000])
        sessions = storage.sessions()
        assert [s["mode"] for s in sessions] == ["poll", "backfill (simulated)"]  # the app's is still the newest
        assert sessions[1]["started_ms"] == backfilled[0][0]

    def test_backfill_leaves_minute_summaries_older_than_the_samples_alone(self, tmp_path):
        # A node older than its raw retention: the oldest samples have been
        # pruned and their minute summaries kept. Those are real data too.
        path = tmp_path / "magnetometer.sqlite"
        storage = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
        first = ((int(time.time()) - 2400) // 60) * 60_000 + 37_003  # 37 s into a minute, 40 minutes ago
        storage.write_samples([(first + 1000 * k, 1_000.0, 2_000.0, 3_000.0) for k in range(2400)])
        storage.summarise_range(first, first + 2_400_000)
        pruned_before = first + 1_200_500  # retention cuts part-way through a minute
        with sqlite3.connect(path) as db:
            db.execute("DELETE FROM samples WHERE t_ms < ?", (pruned_before,))
            real = db.execute("SELECT * FROM minutes ORDER BY t_ms").fetchall()
        oldest_minute = first // 60_000 * 60_000

        args = ["backfill", "--scenario", "quiet-day", "--days", "0.01", "--data-dir", str(tmp_path)]
        assert main(args) == 0

        with sqlite3.connect(path) as db:
            kept = db.execute("SELECT * FROM minutes WHERE t_ms >= ? ORDER BY t_ms", (oldest_minute,)).fetchall()
            new = [row[0] for row in db.execute("SELECT t_ms FROM samples WHERE t_ms < ? ORDER BY t_ms", (first,))]
            touched = db.execute(
                "SELECT COUNT(*) FROM samples WHERE t_ms >= ? AND t_ms < ?", (oldest_minute, pruned_before)
            ).fetchone()[0]
        assert kept == real  # every summary the app made is as it was
        assert touched == 0  # and no sample went in among them
        assert len(new) == 864 and oldest_minute - 1000 <= new[-1] < oldest_minute

    def test_a_scenario_typo_touches_no_database(self, tmp_path, capsys):
        data = tmp_path / "data"
        assert main(["backfill", "--scenario", "quiet-dya", "--data-dir", str(data)]) == 2
        assert "no scenario 'quiet-dya'" in capsys.readouterr().err
        assert not data.exists()

    def test_history_before_wmm2025_is_refused_before_anything_is_written(self, tmp_path, capsys):
        data = tmp_path / "data"
        assert main(["backfill", "--scenario", "quiet-day", "--days", "1500", "--data-dir", str(data)]) == 2
        assert "WMM2025" in capsys.readouterr().err
        assert not data.exists()

    @pytest.mark.parametrize(
        "database", ["the app's, closed", "the app's, open in the app", "unreadable", "without the app's other tables"]
    )
    @pytest.mark.parametrize("mistake", ["a scenario typo", "history before 2025"])
    def test_a_refused_backfill_leaves_the_database_as_it_was(self, tmp_path, database, mistake):
        # Byte for byte, and no file added or removed: no schema set up,
        # nothing set aside, no new database, no companion files. The one
        # exception is the WAL index (-shm) of a database the app has open,
        # which every reader of it updates.
        data = tmp_path / "data"
        data.mkdir()
        path = data / "magnetometer.sqlite"
        oldest = int(datetime(2025, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
        app = None
        if database.startswith("the app's"):
            written = tmp_path / "written" / "magnetometer.sqlite"
            storage = Storage(written, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
            storage.write_samples([(oldest + 1000 * k, 1.0, 2.0, 3.0) for k in range(120)])
            storage.summarise_range(oldest, oldest + 120_000)
            with sqlite3.connect(written) as db:  # everything into the main file, as a clean close leaves it
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.close()
            shutil.copyfile(written, path)
            if database.endswith("open in the app"):
                app = sqlite3.connect(path)
                app.execute("SELECT COUNT(*) FROM samples").fetchone()
        elif database == "unreadable":
            path.write_bytes(random.Random(1).randbytes(8192))
        else:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE samples (t_ms INTEGER PRIMARY KEY, x REAL, y REAL, z REAL)")
                db.execute("INSERT INTO samples VALUES (?, 1, 2, 3)", (oldest,))
            db.close()

        def contents():
            return {p.name: None if p.name.endswith("-shm") else p.read_bytes() for p in data.iterdir()}

        before = contents()
        args = ["backfill", "--data-dir", str(data)]
        if mistake == "a scenario typo":
            args += ["--scenario", "quiet-dya"]
        else:
            args += ["--scenario", "quiet-day", "--days", "2"]  # ends on 2 January 2025, so would start in 2024
        try:
            assert main(args) in (1, 2)
            assert contents() == before
        finally:
            if app is not None:
                app.close()

    def test_an_unreadable_database_is_reported_and_left_alone(self, tmp_path, capsys):
        # Setting it aside is the app's call, made when it starts, not backfill's.
        path = tmp_path / "magnetometer.sqlite"
        path.write_bytes(random.Random(2).randbytes(8192))
        assert main(["backfill", "--scenario", "quiet-day", "--days", "0.01", "--data-dir", str(tmp_path)]) == 1
        assert "cannot read the database" in capsys.readouterr().err
        assert [p.name for p in tmp_path.iterdir()] == ["magnetometer.sqlite"]

    def test_backfill_reads_a_database_the_app_has_open(self, tmp_path):
        path = tmp_path / "magnetometer.sqlite"
        storage = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
        first = (int(time.time()) - 120) * 1000 + 3
        storage.write_samples([(first + 1000 * k, 1.0, 2.0, 3.0) for k in range(100)])
        app = sqlite3.connect(path)  # the app's connection, as while it runs
        try:
            app.execute("SELECT COUNT(*) FROM samples").fetchone()
            assert main(["backfill", "--scenario", "quiet-day", "--days", "0.001", "--data-dir", str(tmp_path)]) == 0
        finally:
            app.close()
        rows = storage.raw_rows(0, first + 10**9, 10**6)
        assert len(rows) == 100 + 86 and max(row[0] for row in rows[:86]) < first

    @pytest.mark.usefixtures("commands_never_run")
    @pytest.mark.parametrize("option", ["--raw-retention-days", "--max-db-mb"])
    def test_housekeeping_is_left_to_the_app(self, option, capsys):
        # backfill only writes: the app applies the node's own retention and
        # size cap to what it wrote, on its next pass.
        with pytest.raises(SystemExit) as info:
            main(["backfill", option, "100"])
        assert info.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err


def test_bad_scenario_is_a_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("site: {latitude: 1, longitude: 2}\nevents: [{type: comet, at: +1m}]\n")
    assert main(["describe", str(bad)]) == 2
    assert "scenario error" in capsys.readouterr().err


def test_bad_moment_direction_is_a_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad_dir.yaml"
    bad.write_text(
        "site: {latitude: 1, longitude: 2}\n"
        "events: [{type: uap_pass, at: +1m, closest_approach_m: 5, moment_direction: [0, 0, a]}]\n"
    )
    assert main(["generate", "--scenario", str(bad), "--start", START, "--duration", "1s"]) == 2
    assert "moment_direction" in capsys.readouterr().err


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        main(["dance"])


def test_serve_drives_like_a_chip_and_stops_on_sigterm():
    """`serve` as docker compose runs it: a process, a port, SIGTERM to stop."""
    process = subprocess.Popen(
        [sys.executable, "-m", "rm3100_sim", "serve", "--scenario", "faults", "--start", START]
        + ["--host", "127.0.0.1", "--port", "0"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue = queue.Queue()
    reader = threading.Thread(target=lambda: [lines.put(line) for line in process.stdout], daemon=True)
    reader.start()
    seen: list[str] = []
    try:
        port = None
        deadline = time.monotonic() + 60  # generous: the machine may be busy
        while port is None and time.monotonic() < deadline and process.poll() is None:
            try:
                seen.append(lines.get(timeout=0.2))
            except queue.Empty:
                continue
            match = re.search(r"RM3100 simulator on 127\.0\.0\.1:(\d+)", seen[-1])
            port = int(match.group(1)) if match else None
        assert port, "".join(seen)
        bus = open_bus(f"tcp://127.0.0.1:{port}", timeout_s=10.0)
        try:
            sensor = find_rm3100(bus)
            assert sensor.self_test().passed
            m = sensor.single_measurement()
            assert 40_000 < (m.x_nt**2 + m.y_nt**2 + m.z_nt**2) ** 0.5 < 60_000
        finally:
            bus.close()
    finally:
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=60)
        reader.join(timeout=5)
    while not lines.empty():
        seen.append(lines.get())
    output = "".join(seen)
    assert process.returncode == 0, output
    stopped = re.search(r"stopped after (\d+) transfers", output)
    assert stopped and int(stopped.group(1)) > 0, output
    # The start-up log describes each fault once, whatever its repeats.
    assert "brown-out every 20 min" in output and output.count("disconnect every 20 min") == 1
