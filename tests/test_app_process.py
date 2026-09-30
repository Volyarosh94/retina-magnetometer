"""The whole node app as a process, against the simulator over TCP.

This is the demo, minus Docker: start ``python -m retina_magnetometer`` with
the bus pointed at a simulator on a free port, wait for it to sample, read the
page's API, stop it with SIGTERM and check that nothing buffered was lost.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from rm3100_sim import scenario as sc
from rm3100_sim.device import RM3100Model, SimClock
from rm3100_sim.server import SimulatorServer

ROOT = Path(__file__).parent.parent


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
        return json.loads(response.read())


@pytest.fixture
def simulator():
    scenario = sc.load("uap-flyby", start=time.time())
    server = SimulatorServer(("127.0.0.1", 0), RM3100Model(scenario, SimClock(scenario.start)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_app_samples_the_simulator_and_stops_cleanly(simulator, tmp_path):
    port = free_port()
    env = {
        **os.environ,
        "MAGNETOMETER_BUS": f"tcp://127.0.0.1:{simulator}",
        "MAGNETOMETER_DATA_DIR": str(tmp_path),
        "MAGNETOMETER_HOST": "127.0.0.1",
        "MAGNETOMETER_PORT": str(port),
        "MAGNETOMETER_SAMPLE_RATE_HZ": "5",
        "MAGNETOMETER_FLUSH_INTERVAL_S": "60",  # nothing reaches disk until shutdown
        "MAGNETOMETER_LATITUDE": "34.85",
        "MAGNETOMETER_LONGITUDE": "-82.39",
        "MAGNETOMETER_ALTITUDE_M": "300",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "retina_magnetometer"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 20
        health = {}
        while time.time() < deadline:
            try:
                health = get(port, "/api/health")
                if health["state"] == "ok" and health["samples_total"] >= 10:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        assert health.get("state") == "ok", health
        assert health["sensor"]["revid"] == "0x22" and health["self_test"]["passed"]
        orientation = get(port, "/api/orientation")
        assert orientation["down_axis"] == "-Z"
        assert abs(orientation["heading_true_deg"] - 37.0) < 2.0
        series = get(port, "/api/series?window=60")
        # The window reaches back before the app started, so the buffer is
        # joined onto the (empty) disk: every sample so far is there even
        # though none has been flushed.
        assert series["source"] == "samples+memory" and len(series["t"]) >= 10
        samples_seen = get(port, "/api/health")["samples_total"]
    finally:
        process.send_signal(signal.SIGTERM)
        output = process.communicate(timeout=15)[0].decode()
    assert process.returncode == 0, output
    assert "stopped" in output
    # Shutdown flushed the buffer that the 60 s interval was still holding.
    import sqlite3

    stored = sqlite3.connect(tmp_path / "magnetometer.sqlite").execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert stored >= samples_seen
    assert json.loads((tmp_path / "status.json").read_text())["state"] in ("ok", "stalled")
