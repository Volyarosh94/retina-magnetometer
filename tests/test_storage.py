"""SQLite storage: writes, minute summaries, retention, the size cap, queries."""

import math

import pytest

from retina_magnetometer import series
from retina_magnetometer.storage import Storage

T0 = 1_790_726_400_000  # 2026-09-30T00:00:00Z in ms, minute-aligned


def make(tmp_path, **kwargs):
    options = {"raw_retention_days": 7, "rollup_retention_days": 365, "max_db_mb": 1024}
    options.update(kwargs)
    return Storage(tmp_path / "m.sqlite", **options)


def ramp(start_ms, count, step_ms=1000):
    """Samples whose values encode their index, so aggregates are checkable."""
    return [(start_ms + i * step_ms, float(i), -float(i), 1000.0 + i) for i in range(count)]


def test_schema_and_modes(tmp_path):
    s = make(tmp_path)
    import sqlite3

    db = sqlite3.connect(s.path)
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert db.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # incremental
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"


def test_write_and_read_back_raw(tmp_path):
    s = make(tmp_path)
    s.write_samples(ramp(T0, 100))
    data = s.series(T0, T0 + 100_000, max_points=1000)
    assert data["source"] == "samples" and data["bucket_ms"] == 0
    assert data["t"][0] == T0 and len(data["t"]) == 100
    assert data["x"]["mean"][5] == 5.0
    assert data["b"]["mean"][3] == pytest.approx(math.sqrt(9 + 9 + 1003**2))


def test_duplicate_timestamps_replace(tmp_path):
    s = make(tmp_path)
    s.write_samples([(T0, 1.0, 1.0, 1.0)])
    s.write_samples([(T0, 2.0, 2.0, 2.0)])
    assert s.stats()["samples"] == 1
    assert s.series(T0, T0 + 1, 10)["x"]["mean"] == [2.0]


def test_rollup_summarises_complete_minutes_once(tmp_path):
    s = make(tmp_path)
    s.write_samples(ramp(T0, 180))  # three minutes at 1 Hz
    # At T0+100 s only the first minute is complete (it ended at 60 s, plus
    # the 15 s grace for the writer's last flush).
    assert s.rollup(now_ms=T0 + 100_000) == 1
    assert s.rollup(now_ms=T0 + 100_000) == 0  # nothing new
    assert s.rollup(now_ms=T0 + 200_000) == 2
    minutes = s.series(T0, T0 + 180_000 * 60, max_points=10)
    assert minutes["source"] == "minutes"
    first = s.series(T0, T0 + 60_000 * 1500, max_points=1500)
    assert first["x"]["min"][0] == 0.0 and first["x"]["max"][0] == 59.0
    assert first["x"]["mean"][0] == pytest.approx(29.5)
    assert first["n"][0] == 60


def test_minute_buckets_weight_means_by_count(tmp_path):
    s = make(tmp_path)
    rows = [(T0 + i * 1000, 10.0, 0.0, 0.0) for i in range(60)] + [(T0 + 60_000, 70.0, 0.0, 0.0)]
    s.write_samples(rows)
    s.rollup(now_ms=T0 + 10 * 60_000)
    # Both minutes in one two-minute bucket: (60*10 + 1*70) / 61, not (10 + 70) / 2.
    data = s.series(T0, T0 + 2 * 60_000 * 2000, max_points=2000)
    assert data["x"]["mean"][0] == pytest.approx((60 * 10 + 70) / 61)


def test_bucketed_raw_keeps_min_and_max(tmp_path):
    s = make(tmp_path)
    rows = [(T0 + i * 100, 0.0, 0.0, 50_000.0) for i in range(3000)]
    rows[1234] = (T0 + 123_400, 900.0, 0.0, 50_000.0)  # a one-sample spike
    s.write_samples(rows)
    data = s.series(T0, T0 + 300_000, max_points=100)
    assert data["source"] == "samples" and data["bucket_ms"] == 3000
    assert max(v for v in data["x"]["max"] if v is not None) == 900.0
    assert max(v for v in data["x"]["mean"] if v is not None) < 900.0


def test_gaps_are_marked(tmp_path):
    s = make(tmp_path)
    s.write_samples(ramp(T0, 10) + ramp(T0 + 60_000, 10))
    data = s.series(T0, T0 + 120_000, 1000)
    assert None in data["x"]["mean"]
    assert data["n"].count(0) == 1


def test_retention(tmp_path):
    s = make(tmp_path, raw_retention_days=1, rollup_retention_days=2)
    day = 86_400_000
    s.write_samples([(T0, 1.0, 1.0, 1.0), (T0 + 2 * day, 2.0, 2.0, 2.0)])
    s.summarise_range(T0, T0 + 3 * day)
    removed = s.prune(now_ms=T0 + 2 * day + 1000)
    assert removed["samples"] == 1
    assert s.stats()["samples"] == 1
    removed = s.prune(now_ms=T0 + 5 * day)
    assert s.stats()["minutes"] == 0


def test_size_cap_drops_oldest_first(tmp_path):
    s = make(tmp_path, max_db_mb=2, raw_retention_days=3650)
    batch = []
    for i in range(300_000):
        batch.append((T0 + i * 1000, 1.0 + i, 2.0, 3.0))
        if len(batch) == 100_000:
            s.write_samples(batch)
            batch = []
    s.write_samples(batch)
    before = s.stats()
    assert before["bytes"] > s.max_bytes
    removed = s.prune(now_ms=T0 + 300_000_000)
    after = s.stats()
    assert removed["size_capped"] is True
    assert after["bytes"] <= s.max_bytes
    assert after["oldest_sample_ms"] > before["oldest_sample_ms"]
    assert after["newest_sample_ms"] == before["newest_sample_ms"]


def test_sessions_record_the_gain_in_force(tmp_path):
    s = make(tmp_path)
    s.start_session(cycle_count=400, gain=148.34, rate_hz=10.0, mode="poll", bus="/dev/i2c-1", address=0x20)
    (row,) = s.sessions()
    assert row["cycle_count"] == 400 and row["gain_lsb_per_ut"] == 148.34 and row["address"] == 0x20


def test_memory_and_disk_series_agree(tmp_path):
    s = make(tmp_path)
    rows = [(T0 + i * 250, math.sin(i / 50) * 100, 2.0 * i, -3.0) for i in range(4000)]
    s.write_samples(rows)
    for points in (100, 5000):
        disk = s.series(T0, T0 + 1_000_000, points)
        memory = series.bucket_rows(rows, T0, T0 + 1_000_000, points)
        assert disk["t"] == memory["t"]
        for axis in ("x", "y", "z", "b"):
            for key in ("min", "mean", "max"):
                assert disk[axis][key] == pytest.approx(memory[axis][key])
