"""SQLite storage: writes, minute summaries, retention, the size cap, queries,
and a database that cannot be opened or read."""

import math
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from retina_magnetometer import series
from retina_magnetometer import storage as storage_module
from retina_magnetometer.storage import (
    MINUTE_ROW_BYTES,
    SAMPLE_ROW_BYTES,
    SCHEMA_VERSION,
    Storage,
    StorageUnavailable,
)

T0 = 1_790_726_400_000  # 2026-09-30T00:00:00Z in ms, minute-aligned
DAY = 86_400_000


def make(tmp_path, **kwargs):
    options = {"raw_retention_days": 7, "rollup_retention_days": 365, "max_db_mb": 1024}
    options.update(kwargs)
    return Storage(tmp_path / "m.sqlite", **options)


def ramp(start_ms, count, step_ms=1000):
    """Samples whose values encode their index, so aggregates are checkable."""
    return [(start_ms + i * step_ms, float(i), -float(i), 1000.0 + i) for i in range(count)]


def readings(start_ms, count, step_ms=1000):
    """Samples that look like a sensor's: no value is a whole number, which
    SQLite would store in fewer bytes and so understate the size of a row."""
    return [
        (
            start_ms + i * step_ms,
            16012.37 + (i % 977) * 0.013,
            -2001.11 - (i % 613) * 0.007,
            43005.13 + (i % 401) * 0.011,
        )
        for i in range(count)
    ]


def add_minutes(s, start_ms, count):
    """Minute summaries written directly, with realistic (fractional) values."""
    db = sqlite3.connect(s.path, isolation_level=None)
    db.execute("BEGIN")
    db.executemany(
        "INSERT OR REPLACE INTO minutes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(start_ms + i * 60_000, 60, *(16000.123 + (i % 1000) * 0.0071 + j for j in range(12))) for i in range(count)],
    )
    db.execute("COMMIT")
    db.close()


def set_watermark(s, t_ms):
    db = sqlite3.connect(s.path, isolation_level=None)
    db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('rollup_watermark', ?)", (str(t_ms),))
    db.close()


def data_bytes(s):
    db = sqlite3.connect(s.path)
    pages, free, size = (db.execute(f"PRAGMA {p}").fetchone()[0] for p in ("page_count", "freelist_count", "page_size"))
    db.close()
    return (pages - free) * size


def test_schema_and_modes(tmp_path):
    s = make(tmp_path)
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
    # Values travel rounded to 0.01 nT.
    assert data["b"]["mean"][3] == pytest.approx(math.sqrt(9 + 9 + 1003**2), abs=0.005)


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
    # the 15 s grace for a sample on its way to the writer).
    assert s.rollup(now_ms=T0 + 100_000) == 1
    assert s.rollup(now_ms=T0 + 100_000) == 0  # nothing new
    assert s.rollup(now_ms=T0 + 200_000) == 2
    minutes = s.series(T0, T0 + 180_000 * 60, max_points=10)
    assert minutes["source"] == "minutes"
    first = s.series(T0, T0 + 60_000 * 1500, max_points=1500)
    assert first["x"]["min"][0] == 0.0 and first["x"]["max"][0] == 59.0
    assert first["x"]["mean"][0] == pytest.approx(29.5)
    assert first["n"][0] == 60


def test_rollup_stops_at_the_oldest_sample_not_yet_written(tmp_path):
    # The writer still holds everything from 90 s on: the second minute is
    # not complete on disk however long ago it ended, and a minute summarised
    # early would never see its late samples.
    s = make(tmp_path)
    s.write_samples(ramp(T0, 90))
    assert s.rollup(now_ms=T0 + 300_000, pending_from_ms=T0 + 90_000) == 1
    s.write_samples(ramp(T0 + 90_000, 90))
    assert s.rollup(now_ms=T0 + 300_000) == 2
    data = s.series(T0, T0 + 60_000 * 1500, max_points=1500)
    assert data["n"][:3] == [60, 60, 60]


def test_rows_written_behind_the_watermark_are_summarised_again(tmp_path):
    # A backfill, or samples stamped after the clock stepped back, land in
    # minutes that are already summarised; the summaries must follow them.
    s = make(tmp_path)
    s.write_samples([(T0 + i * 1000, 10.0, 0.0, 0.0) for i in range(180)])
    s.rollup(now_ms=T0 + 300_000)
    s.write_samples([(T0 + 60_000 + i * 1000 + 500, 70.0, 0.0, 0.0) for i in range(60)])
    db = sqlite3.connect(s.path)
    minutes = db.execute("SELECT t_ms, n, x_mean, x_max FROM minutes ORDER BY t_ms").fetchall()
    assert minutes == [(T0, 60, 10.0, 10.0), (T0 + 60_000, 120, 40.0, 70.0), (T0 + 120_000, 60, 10.0, 10.0)]


def test_minute_buckets_weight_means_by_count(tmp_path):
    s = make(tmp_path)
    rows = [(T0 + i * 1000, 10.0, 0.0, 0.0) for i in range(60)] + [(T0 + 60_000, 70.0, 0.0, 0.0)]
    s.write_samples(rows)
    s.rollup(now_ms=T0 + 10 * 60_000)
    # Both minutes in one two-minute bucket: (60*10 + 1*70) / 61, not (10 + 70) / 2.
    data = s.series(T0, T0 + 2 * 60_000 * 2000, max_points=2000)
    assert data["x"]["mean"][0] == pytest.approx((60 * 10 + 70) / 61, abs=0.005)


def test_bucketed_raw_keeps_min_and_max(tmp_path):
    s = make(tmp_path)
    rows = [(T0 + i * 100, 0.0, 0.0, 50_000.0) for i in range(3000)]
    rows[1234] = (T0 + 123_400, 900.0, 0.0, 50_000.0)  # a one-sample spike
    s.write_samples(rows)
    data = s.series(T0, T0 + 300_000, max_points=100)
    assert data["source"] == "samples" and data["bucket_ms"] == 3000
    assert max(v for v in data["x"]["max"] if v is not None) == 900.0
    assert max(v for v in data["x"]["mean"] if v is not None) < 900.0


def test_buckets_sit_on_a_fixed_grid(tmp_path):
    # A window that slides on by a refresh keeps its buckets: no regrouping of
    # the samples (the chart would shimmer), and a client holding the previous
    # response needs only the tail.
    s = make(tmp_path)
    rows = readings(T0, 4000)
    s.write_samples(rows)
    a = s.series(T0 + 100_000, T0 + 3_700_000, max_points=1779)
    b = s.series(T0 + 102_000, T0 + 3_702_000, max_points=1779)
    assert a["bucket_ms"] == b["bucket_ms"] == 2024
    assert all(t % 2024 == 0 for t in a["t"])
    overlap = [t for t in a["t"] if t in set(b["t"])][1:]  # past the left edge's partial bucket
    assert len(overlap) > 1700
    for t in overlap:
        i, j = a["t"].index(t), b["t"].index(t)
        assert (a["n"][i], a["x"]["min"][i], a["x"]["max"][i]) == (b["n"][j], b["x"]["min"][j], b["x"]["max"][j])
    tail = series.tail(b, a["t"][-1])
    assert tail["t"] == b["t"][b["t"].index(a["t"][-1]) :]
    assert tail["bucket_ms"] == b["bucket_ms"] and len(tail["x"]["mean"]) == len(tail["t"])


def test_gaps_are_marked(tmp_path):
    s = make(tmp_path)
    s.write_samples(ramp(T0, 10) + ramp(T0 + 60_000, 10))
    data = s.series(T0, T0 + 120_000, 1000)
    assert None in data["x"]["mean"]
    assert data["n"].count(0) == 1


def test_retention(tmp_path):
    s = make(tmp_path, raw_retention_days=1, rollup_retention_days=2)
    s.write_samples([(T0, 1.0, 1.0, 1.0), (T0 + 2 * DAY, 2.0, 2.0, 2.0)])
    s.summarise_range(T0, T0 + 3 * DAY)
    removed = s.prune(now_ms=T0 + 2 * DAY + 1000)
    assert removed["samples"] == 1
    assert s.stats()["samples"] == 1
    removed = s.prune(now_ms=T0 + 5 * DAY)
    assert s.stats()["minutes"] == 0


def test_rows_take_the_bytes_the_size_cap_assumes(tmp_path):
    s = make(tmp_path)
    empty = data_bytes(s)
    s.write_samples(readings(T0, 50_000))
    per_sample = (data_bytes(s) - empty) / 50_000
    before = data_bytes(s)
    add_minutes(s, T0, 20_000)
    per_minute = (data_bytes(s) - before) / 20_000
    assert per_sample == pytest.approx(SAMPLE_ROW_BYTES, rel=0.1)
    assert per_minute == pytest.approx(MINUTE_ROW_BYTES, rel=0.1)


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
    assert after["size_capped_ms"] == T0 + 300_000_000


def test_size_cap_removes_no_more_while_a_reader_holds_a_snapshot(tmp_path, monkeypatch):
    # A reader in a transaction (an open sqlite3 shell, a slow query) keeps
    # the WAL from being checkpointed, so the file cannot shrink while it is
    # there. The cap must still remove only what the data's size calls for,
    # and must not hold the writer for long waiting for it.
    monkeypatch.setattr(storage_module, "CHECKPOINT_WAIT_MS", 50)
    template = make(tmp_path / "template", max_db_mb=3, raw_retention_days=3650, rollup_retention_days=36500)
    template.write_samples(readings(T0, 120_000))
    template.rollup(now_ms=T0 + 120_000_000)
    assert data_bytes(template) > template.max_bytes
    for name in ("alone", "read"):
        (tmp_path / name).mkdir()
        shutil.copy(template.path, tmp_path / name / "m.sqlite")
    alone = make(tmp_path / "alone", max_db_mb=3, raw_retention_days=3650, rollup_retention_days=36500)
    read = make(tmp_path / "read", max_db_mb=3, raw_retention_days=3650, rollup_retention_days=36500)
    minutes = read.stats()["minutes"]

    expected = alone.prune(now_ms=T0 + 120_000_000)
    reader = sqlite3.connect(read.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM samples").fetchone()
    started = time.monotonic()
    removed = read.prune(now_ms=T0 + 120_000_000)
    held = time.monotonic() - started
    stats = read.stats()

    assert removed == expected and removed["size_capped"] is True
    assert 0 < removed["samples"] < 120_000 and removed["minutes"] == 0
    assert stats["minutes"] == minutes and stats["newest_sample_ms"] == T0 + 119_999_000
    assert data_bytes(read) <= read.max_bytes * 0.9
    assert held < 10  # under one 10 s busy timeout, which each step of the old loop waited out
    # The WAL could not be truncated while the reader held it; the next prune,
    # with the reader gone, returns the space, and removes nothing more.
    reader.execute("COMMIT")
    reader.close()
    assert read.prune(now_ms=T0 + 120_000_000) == {"samples": 0, "minutes": 0, "size_capped": False}
    assert read.stats()["bytes"] <= read.max_bytes


def test_size_cap_below_the_minute_table_keeps_the_newest_samples(tmp_path):
    # MAGNETOMETER_MAX_DB_MB below what the minute summaries' retention needs.
    # The minutes alone would fill the cap, so emptying the samples table could
    # not satisfy it: the oldest minutes give way, and every raw sample stays,
    # the ones not yet summarised above all.
    s = make(tmp_path, max_db_mb=4, raw_retention_days=3650, rollup_retention_days=36500)
    add_minutes(s, T0 - 30 * DAY, 30 * 1440)
    s.write_samples(readings(T0, 21_600))  # six hours at 1 Hz
    set_watermark(s, T0 + 21_600_000 - 120_000)
    before = s.stats()
    removed = s.prune(now_ms=T0 + 21_600_000)
    after = s.stats()
    assert removed["size_capped"] is True and removed["samples"] == 0
    assert after["samples"] == before["samples"] == 21_600
    assert after["newest_sample_ms"] == before["newest_sample_ms"]
    assert 0 < after["minutes"] < before["minutes"]
    assert after["oldest_minute_ms"] > before["oldest_minute_ms"]
    assert after["newest_minute_ms"] == before["newest_minute_ms"]
    assert data_bytes(s) <= s.max_bytes * 0.9 and after["bytes"] <= s.max_bytes


def test_size_cap_shares_the_room_when_both_tables_must_give_way(tmp_path):
    # Raw samples go first, but not all of them: while the minutes must shrink
    # too, the raw samples keep half the room, and only summarised ones go.
    s = make(tmp_path, max_db_mb=4, raw_retention_days=3650, rollup_retention_days=36500)
    add_minutes(s, T0 - 40 * DAY, 40 * 1440)
    s.write_samples(readings(T0, 90_000))
    watermark = T0 + 90_000_000 - 120_000
    set_watermark(s, watermark)
    removed = s.prune(now_ms=T0 + 90_000_000)
    after = s.stats()
    target = s.max_bytes * 0.9
    assert removed["samples"] > 0 and removed["minutes"] > 0
    assert after["samples"] * SAMPLE_ROW_BYTES == pytest.approx(target / 2, rel=0.1)
    assert after["minutes"] * MINUTE_ROW_BYTES == pytest.approx(target / 2, rel=0.15)
    assert after["newest_sample_ms"] == T0 + 89_999_000
    assert after["oldest_sample_ms"] < watermark  # nothing unsummarised went
    assert data_bytes(s) <= target


def test_sessions_record_the_gain_in_force(tmp_path):
    s = make(tmp_path)
    s.start_session(cycle_count=400, gain=148.34, rate_hz=10.0, mode="poll", bus="/dev/i2c-1", address=0x20)
    (row,) = s.sessions()
    assert row["cycle_count"] == 400 and row["gain_lsb_per_ut"] == 148.34 and row["address"] == 0x20


def test_sessions_travel_with_samples(tmp_path):
    s = make(tmp_path)
    session = {"cycle_count": 200, "gain": 74.92, "rate_hz": 1.0, "mode": "poll", "bus": "b", "address": 0x21}
    s.write_samples(ramp(T0, 3), sessions=[{**session, "started_ms": T0 - 500}])
    (row,) = s.sessions()
    assert row["started_ms"] == T0 - 500 and row["address"] == 0x21
    assert s.stats()["samples"] == 3


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
                assert disk[axis][key] == pytest.approx(memory[axis][key], abs=0.011)


# ── A database that cannot be opened or read ────────────────────────────────


def test_a_file_that_is_not_a_database_is_moved_aside(tmp_path):
    # An SD card fault can leave anything in the file. The app must keep
    # recording, and keep the old file for whoever wants to look at it.
    path = tmp_path / "m.sqlite"
    garbage = os.urandom(8192)
    path.write_bytes(garbage)
    (tmp_path / "m.sqlite-wal").write_bytes(os.urandom(4096))
    s = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=lambda: T0 / 1000)
    assert s.unavailable is None
    s.write_samples(ramp(T0, 10))
    stats = s.stats()
    assert stats["samples"] == 10
    kept = tmp_path / "m.sqlite.unreadable-20260930T000000Z"
    assert stats["moved_aside"] == {"at_ms": T0, "reason": "file is not a database", "kept_as": kept.name}
    assert kept.read_bytes() == garbage
    assert (tmp_path / "m.sqlite.unreadable-20260930T000000Z-wal").exists()  # its WAL went with it
    # The page keeps explaining the gap after a restart: the new database says so.
    again = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
    assert again.moved_aside is None and again.stats()["moved_aside"] == stats["moved_aside"]


def test_a_damaged_schema_is_moved_aside_and_earlier_copies_are_kept(tmp_path):
    # Nothing is ever deleted: not an earlier unreadable copy, and not a file
    # of the operator's that merely starts with the same name.
    path = tmp_path / "m.sqlite"
    s = make(tmp_path)
    s.write_samples(ramp(T0, 10))
    db = sqlite3.connect(path)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    earlier = tmp_path / "m.sqlite.unreadable-20200101T000000Z"
    earlier.write_bytes(b"an earlier unreadable copy")
    (tmp_path / "m.sqlite.unreadable-20200101T000000Z-wal").write_bytes(b"its WAL")
    notes = tmp_path / "m.sqlite.unreadable-notes.txt"
    notes.write_text("the operator's")
    with open(path, "r+b") as handle:  # page 1 past its header holds the schema
        handle.seek(100)
        handle.write(b"\xff" * 300)
    s = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=lambda: T0 / 1000 + 60)
    assert s.moved_aside["kept_as"] == "m.sqlite.unreadable-20260930T000100Z"
    assert "malformed" in s.moved_aside["reason"]
    assert earlier.read_bytes() == b"an earlier unreadable copy" and notes.read_text() == "the operator's"
    assert (tmp_path / "m.sqlite.unreadable-20200101T000000Z-wal").exists()
    stats = s.stats()
    assert stats["samples"] == 0
    # Both stamped copies are counted, with what they take; the notes are not.
    kept = [p for p in tmp_path.iterdir() if p.name.startswith("m.sqlite.unreadable-2")]
    assert stats["set_aside"] == {"count": 2, "bytes": sum(p.stat().st_size for p in kept)}


def test_a_second_unreadable_file_in_the_same_second_gets_a_name_of_its_own(tmp_path):
    path = tmp_path / "m.sqlite"
    for expected in ("m.sqlite.unreadable-20260930T000000Z", "m.sqlite.unreadable-20260930T000000Z-2"):
        path.write_bytes(os.urandom(4096))
        s = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=lambda: T0 / 1000)
        assert s.moved_aside["kept_as"] == expected
    assert s.stats()["set_aside"]["count"] == 2


def test_the_schema_version_is_kept_in_the_header(tmp_path):
    s = make(tmp_path)
    assert sqlite3.connect(s.path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # A database from before the header carried it is brought up to date.
    legacy = sqlite3.connect(s.path)
    legacy.execute("PRAGMA user_version = 0")
    legacy.close()
    make(tmp_path).write_samples(ramp(T0, 3))
    assert sqlite3.connect(s.path).execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.parametrize("readable", [True, False])
def test_a_newer_versions_database_is_left_as_it_is(tmp_path, readable):
    # After a rollback (a Mender install of an older release), the database a
    # newer version wrote must stay where it is, untouched, for that version's
    # return: not moved aside as unreadable when its schema uses something
    # this SQLite cannot parse, and not written to when it can.
    path = tmp_path / "m.sqlite"
    newer = make(tmp_path)
    newer.write_samples(ramp(T0, 10))
    db = sqlite3.connect(path, isolation_level=None)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    if not readable:
        db.execute("CREATE TABLE extra (a INTEGER)")
        db.execute("PRAGMA writable_schema = ON")
        db.execute(
            "UPDATE sqlite_master SET sql = 'CREATE TABLE extra (a INTEGER) SOME_FUTURE_OPTION' WHERE name = 'extra'"
        )
        db.execute("PRAGMA writable_schema = OFF")
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    before = path.read_bytes()
    s = Storage(path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=lambda: T0 / 1000)
    assert "written by a newer version of this app (schema 2; this one knows 1)" in s.unavailable
    with pytest.raises(StorageUnavailable, match="newer version"):
        s.write_samples(ramp(T0 + 60_000, 3))
    assert s.moved_aside is None and path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir() if "unreadable" in p.name) == []


def test_a_database_deleted_while_the_app_runs_is_started_afresh(tmp_path):
    # Deleting the file (an operator clearing the history) leaves SQLite to
    # make an empty one at the next connection; the next call creates the
    # schema in it, in WAL mode, instead of failing until a restart.
    s = make(tmp_path)
    s.write_samples(ramp(T0, 3))
    for suffix in ("", "-wal", "-shm"):
        Path(f"{s.path}{suffix}").unlink(missing_ok=True)
    with pytest.raises(sqlite3.OperationalError, match="no such table: samples"):
        s.write_samples(ramp(T0 + 3000, 3))
    s.write_samples(ramp(T0 + 6000, 3))
    stats = s.stats()
    assert stats["samples"] == 3 and stats["oldest_sample_ms"] == T0 + 6000
    assert sqlite3.connect(s.path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    s.start_session(cycle_count=200, gain=74.92, rate_hz=1.0, mode="poll", bus="b", address=0x20)
    assert len(s.sessions()) == 1


def test_a_database_replaced_while_the_app_runs_is_moved_aside_next_time(tmp_path):
    s = make(tmp_path)
    s.write_samples(ramp(T0, 3))
    for suffix in ("-wal", "-shm"):
        Path(f"{s.path}{suffix}").unlink(missing_ok=True)
    garbage = os.urandom(8192)
    s.path.write_bytes(garbage)
    with pytest.raises(sqlite3.DatabaseError):
        s.stats()
    assert s.stats()["samples"] == 0 and s.moved_aside["reason"] == "file is not a database"
    assert (s.path.parent / s.moved_aside["kept_as"]).read_bytes() == garbage


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_directory_that_cannot_be_written_is_reported_and_tried_again(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        s = Storage(locked / "data" / "m.sqlite", raw_retention_days=7, rollup_retention_days=365, max_db_mb=64)
        assert s.unavailable.startswith(f"cannot open {locked / 'data' / 'm.sqlite'}")
        for call in (lambda: s.write_samples(ramp(T0, 1)), s.stats, lambda: s.series(T0, T0 + 1000)):
            with pytest.raises(StorageUnavailable, match="cannot open"):
                call()
        assert issubclass(StorageUnavailable, sqlite3.OperationalError)  # what callers already catch
        locked.chmod(0o700)
        s.write_samples(ramp(T0, 3))  # picked up without a restart
        assert s.unavailable is None and s.stats()["samples"] == 3
    finally:
        locked.chmod(0o700)


def test_size_cap_below_an_empty_database_gives_up(tmp_path):
    # Nothing left to delete: the cap stops there rather than loop.
    s = make(tmp_path, max_db_mb=0.001)
    assert s.prune(now_ms=T0) == {"samples": 0, "minutes": 0, "size_capped": True}


def test_raw_rows_past_their_limit_are_refused(tmp_path):
    s = make(tmp_path)
    s.write_samples(ramp(T0, 100))
    assert len(s.raw_rows(T0, T0 + 100_000, 100)) == 100
    assert s.raw_rows(T0, T0 + 100_000, 99) is None


def test_gaps_are_marked_between_buckets(tmp_path):
    # Bucketed, as in the long views: an outage of more than two buckets is a
    # break in the line, placed on the grid where the next bucket would be.
    s = make(tmp_path)
    s.write_samples(ramp(T0, 1200, step_ms=100) + ramp(T0 + 600_000, 1200, step_ms=100))
    data = s.series(T0, T0 + 720_000, max_points=100)
    assert data["bucket_ms"] == 7200
    gap = data["n"].index(0)
    assert data["t"][gap] == data["t"][gap - 1] + 7200 and data["x"]["mean"][gap] is None
    assert data["t"][gap + 1] <= T0 + 600_000 < data["t"][gap + 1] + 7200  # the bucket the samples resume in
