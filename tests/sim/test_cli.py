"""The simulator's command line: every subcommand, and reproducibility on disk."""

import csv
import json

import pytest

from retina_magnetometer.storage import Storage
from rm3100_sim.__main__ import main

START = "2026-09-30T12:00:00Z"


def test_list(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "uap-flyby" in out and "faults" in out


def test_describe(capsys):
    assert main(["describe", "uap-flyby", "--start", START]) == 0
    out = capsys.readouterr().out
    assert "WMM2025 at start" in out and "UAP pass" in out and "yaw 37" in out


def test_generate_csv_is_reproducible(tmp_path):
    a, b, c = tmp_path / "a.csv", tmp_path / "b.csv", tmp_path / "c.csv"
    args = ["generate", "--scenario", "uap-flyby", "--start", START, "--duration", "5m", "--rate", "2"]
    assert main([*args, "--out", str(a)]) == 0
    assert main([*args, "--out", str(b)]) == 0
    assert main([*args, "--seed", "8", "--out", str(c)]) == 0
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes() != c.read_bytes()
    rows = list(csv.DictReader(a.open()))
    assert len(rows) == 600
    # The first pass (2 min in) stands far above the noise in the truth column.
    peak = max(rows, key=lambda r: float(r["uap_nt"]))
    assert float(peak["uap_nt"]) > 300 and peak["time"].startswith("2026-09-30T12:02")


def test_generate_jsonl(tmp_path):
    out = tmp_path / "s.jsonl"
    assert (
        main(
            [
                "generate",
                "--scenario",
                "quiet-day",
                "--start",
                START,
                "--duration",
                "10s",
                "--format",
                "jsonl",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 10 and set(lines[0]["truth"]) == {"x_nt", "y_nt", "z_nt", "uap_nt"}


def test_backfill_writes_history_the_app_can_read(tmp_path):
    assert (
        main(["backfill", "--scenario", "quiet-day", "--days", "0.05", "--rate", "1", "--data-dir", str(tmp_path)]) == 0
    )
    storage = Storage(tmp_path / "magnetometer.sqlite", raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
    stats = storage.stats()
    assert stats["samples"] == 4320 and stats["minutes"] >= 70
    assert storage.sessions()[0]["mode"] == "backfill (simulated)"


def test_bad_scenario_is_a_clean_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("site: {latitude: 1, longitude: 2}\nevents: [{type: comet, at: +1m}]\n")
    assert main(["describe", str(bad)]) == 2
    assert "scenario error" in capsys.readouterr().err


def test_unknown_command_exits():
    with pytest.raises(SystemExit):
        main(["dance"])
