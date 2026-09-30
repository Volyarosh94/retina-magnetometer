"""The JSON API and the page, through Flask's test client."""

import json
import math

import pytest

from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health
from retina_magnetometer.location import Location
from retina_magnetometer.recorder import Recorder
from retina_magnetometer.storage import Storage
from retina_magnetometer.web import Services, create_app
from rm3100_sim import physics

NOW = 1_790_769_600.0  # 2026-09-30T12:00:00Z


@pytest.fixture
def rig(tmp_path):
    config = from_env(
        {
            "MAGNETOMETER_DATA_DIR": str(tmp_path),
            "MAGNETOMETER_LATITUDE": "34.85",
            "MAGNETOMETER_LONGITUDE": "-82.39",
            "MAGNETOMETER_ALTITUDE_M": "300",
        }
    )
    health = Health(config, clock=lambda: NOW)
    storage = Storage(config.db_path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=lambda: NOW)
    recorder = Recorder(storage, health, config, clock=lambda: NOW)
    location = Location(34.85, -82.39, 300.0, "environment")
    app = create_app(
        Services(config=config, health=health, recorder=recorder, storage=storage, location=location, clock=lambda: NOW)
    )
    # Ten minutes of a sensor mounted like the demo: +X 37° east of true
    # north, upside down.
    ref = physics.reference_field(physics.Site(34.85, -82.39, 300.0), NOW)
    field = physics.ned_to_sensor(physics.rotation_ned_from_sensor(37.0, 0.0, 180.0), ref.vector)
    for i in range(600):
        t = NOW - 600 + i
        wobble = 20 * math.sin(i / 30)
        recorder.add(int(t * 1000), field[0] + wobble, field[1], field[2])
        health.sample(t, field[0] + wobble, field[1], field[2])
    return app.test_client(), recorder, storage, health


def test_page_renders(rig):
    client, *_ = rig
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "RM3100 magnetometer" in html and "plotly-basic-3.7.0.min.js" in html
    assert client.get("/static/vendor/plotly-basic-3.7.0.min.js").status_code == 200


def test_healthz(rig):
    client, *_ = rig
    assert client.get("/healthz").get_data(as_text=True) == "ok\n"


def test_series_from_memory_at_the_live_edge(rig):
    client, *_ = rig
    data = client.get("/api/series?window=600&points=2000").get_json()
    assert data["source"] == "memory"
    assert len(data["t"]) == 600
    assert data["end"] - data["start"] == 600_000


def test_series_from_disk_further_back(rig):
    client, recorder, storage, _ = rig
    recorder.flush()
    url = f"/api/series?start={int((NOW - 7200) * 1000)}&end={int(NOW * 1000)}"
    data = client.get(url + "&points=400").get_json()
    # Buckets under a minute: raw samples, the disk's joined onto the buffer's.
    assert data["source"] == "samples+memory"
    assert data["bucket_ms"] == 18_000
    assert sum(data["n"]) == 600
    storage.rollup(now_ms=int(NOW * 1000) + 60_000)
    data = client.get(url + "&points=100").get_json()
    assert data["source"] == "minutes"  # buckets over a minute: from the summaries
    assert data["bucket_ms"] == 120_000
    assert sum(data["n"]) == 600


@pytest.mark.parametrize(
    "query",
    ["window=abc", "window=5", "window=999999999", "start=10&end=5", "start=0&end=99999999999999"],
)
def test_series_rejects_nonsense(rig, query):
    client, *_ = rig
    response = client.get(f"/api/series?{query}")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_health_is_the_status_document(rig):
    client, *_ = rig
    h = client.get("/api/health").get_json()
    assert h["schema"] == 1 and h["state"] == "ok"
    assert h["samples_total"] == 600
    assert h["last_sample_age_s"] == pytest.approx(1.0)


def test_latest(rig):
    client, *_ = rig
    latest = client.get("/api/latest").get_json()
    assert latest["sample"]["b"] == pytest.approx(48_600, abs=100)


def test_orientation_recovers_the_mounting(rig):
    client, *_ = rig
    o = client.get("/api/orientation").get_json()
    assert o["down_axis"] == "-Z" and o["up_axis"] == "+Z"
    assert abs(o["heading_true_deg"] - 37.0) < 0.5
    assert o["verdict"] == "good"
    assert o["location"]["source"] == "environment"
    assert o["reference"]["model"] == "WMM2025"


def test_config_endpoint(rig):
    client, _, storage, _ = rig
    storage.start_session(cycle_count=200, gain=74.92, rate_hz=1.0, mode="poll", bus="test", address=0x20)
    c = client.get("/api/config").get_json()
    assert c["config"]["port"] == 3030
    assert c["location"]["latitude"] == 34.85
    assert c["sessions"][0]["cycle_count"] == 200


def test_status_file_is_written_atomically(rig, tmp_path):
    _, recorder, _, health = rig
    recorder._write_status()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "ok" and status["schema"] == 1
    assert oct((tmp_path / "status.json").stat().st_mode & 0o777) == "0o644"
    assert not (tmp_path / "status.json.tmp").exists()


def test_series_joins_disk_and_memory_across_the_buffer_edge(rig):
    client, recorder, storage, _ = rig
    # An hour of older samples on disk only, then the live buffer's ten minutes.
    older = [(int((NOW - 4200 + i) * 1000), 1.0, 2.0, 3.0) for i in range(3600)]
    storage.write_samples(older)
    data = client.get("/api/series?window=5400&points=20000").get_json()
    assert data["source"] == "samples+memory"
    assert sum(data["n"]) == 3600 + 600
    assert data["t"][-1] == int((NOW - 1) * 1000)  # up to the newest, unflushed sample
