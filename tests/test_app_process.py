"""The whole node app as a process, against the simulator over TCP.

This is the demo, minus Docker: start ``python -m retina_magnetometer`` with
the bus pointed at a simulator on a free port, wait for it to sample, read the
page's API, stop it with SIGTERM and check that nothing buffered was lost.
"""

import errno
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

from retina_magnetometer import __main__ as app_main
from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health
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
    # And closed the database: whole in its one file, the WAL taken into it.
    assert not (tmp_path / "magnetometer.sqlite-wal").exists()
    # Shutdown flushed the buffer that the 60 s interval was still holding.
    import sqlite3

    stored = sqlite3.connect(tmp_path / "magnetometer.sqlite").execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    assert stored >= samples_seen
    assert json.loads((tmp_path / "status.json").read_text())["state"] in ("ok", "stalled")


def serve(tmp_path, port=None, **env):
    """The app as a process with no sensor (a bus that does not exist), so
    that what it reports is about storage and configuration alone."""
    port = port or free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "retina_magnetometer"],
        cwd=ROOT,
        env={
            **os.environ,
            "MAGNETOMETER_BUS": str(tmp_path / "no-i2c-bus"),
            "MAGNETOMETER_HOST": "127.0.0.1",
            "MAGNETOMETER_PORT": str(port),
            "MAGNETOMETER_LATITUDE": "",
            "MAGNETOMETER_LONGITUDE": "",
            **env,
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return process, port


def health_when(port, ready, process):
    deadline = time.time() + 20
    health = {}
    while time.time() < deadline and process.poll() is None:
        try:
            health = get(port, "/api/health")
            if ready(health):
                return health
        except OSError:
            pass
        time.sleep(0.2)
    return health


def stop(process):
    """SIGTERM, as docker stop sends; the process must exit 0. One that does
    not exit is killed, so a failing test leaves nothing running."""
    process.send_signal(signal.SIGTERM)
    try:
        output = process.communicate(timeout=15)[0].decode()
    except subprocess.TimeoutExpired:
        process.kill()
        output = process.communicate()[0].decode()
        raise AssertionError(f"the app did not stop on SIGTERM:\n{output}") from None
    assert process.returncode == 0, output
    return output


def test_app_comes_up_with_an_unreadable_database_and_a_node_config_of_another_shape(tmp_path):
    # Each of these used to end the process at start, and restart: always
    # turned that into a crash loop with no page to say why.
    data = tmp_path / "data"
    data.mkdir()
    garbage = os.urandom(8192)
    (data / "magnetometer.sqlite").write_bytes(garbage)
    node_config = tmp_path / "config.yml"
    node_config.write_text('location: "somewhere"\n')
    process, port = serve(tmp_path, MAGNETOMETER_DATA_DIR=str(data), MAGNETOMETER_NODE_CONFIG=str(node_config))
    try:
        health = health_when(port, lambda h: h.get("storage") is not None, process)
        moved = health["storage"]["moved_aside"]
        assert moved["reason"] == "file is not a database"
        assert (data / moved["kept_as"]).read_bytes() == garbage
        assert health["state"] == "no_bus" and health["storage_error"] is None
        assert get(port, "/api/config")["location"] is None
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as page:
            assert page.status == 200
    finally:
        output = stop(process)
    assert "moved it aside" in output and "location.rx" in output
    assert json.loads((data / "status.json").read_text())["storage"]["moved_aside"] == moved


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_app_comes_up_with_a_data_directory_it_cannot_write(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    data.chmod(0o500)
    try:
        process, port = serve(tmp_path, MAGNETOMETER_DATA_DIR=str(data))
        try:
            health = health_when(port, lambda h: h.get("storage_error"), process)
            assert health["storage_error"].startswith(f"cannot open {data / 'magnetometer.sqlite'}")
            series = get(port, "/api/series?window=600")
            assert series["t"] == []
        finally:
            output = stop(process)
    finally:
        data.chmod(0o700)  # whatever happened, pytest must be able to remove it
    assert output.count("cannot open") <= 2  # reported, not repeated every few seconds


def test_app_serves_on_loopback_when_its_address_is_not_this_machines(tmp_path):
    # 192.0.2.1 is a documentation address (TEST-NET-1), assigned to nothing:
    # a valid setting that only the bind refuses. The page moves to 127.0.0.1
    # and says why, rather than the process ending in a restart loop.
    process, port = serve(tmp_path, MAGNETOMETER_HOST="192.0.2.1", MAGNETOMETER_DATA_DIR=str(tmp_path / "data"))
    try:
        health = health_when(port, lambda h: h.get("config_warnings"), process)
        (warning,) = health["config_warnings"]
        assert warning.startswith("MAGNETOMETER_HOST='192.0.2.1' cannot be listened on (")
        assert warning.endswith("): the page is served on 127.0.0.1 only")
        assert health["state"] == "no_bus"  # a warning; the state stays what the sensor makes it
    finally:
        output = stop(process)
    assert f"serving on http://127.0.0.1:{port}" in output


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost"])
def test_app_still_ends_when_its_port_is_taken(tmp_path, host):
    # Another address would not free the port: no page can be served, so the
    # process ends, as it always has.
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        process, _ = serve(tmp_path, port, MAGNETOMETER_HOST=host, MAGNETOMETER_DATA_DIR=str(tmp_path / "data"))
        try:
            output = process.communicate(timeout=30)[0].decode()
        except subprocess.TimeoutExpired:
            process.kill()
            raise AssertionError("the app kept running on a port that was taken") from None
    assert process.returncode != 0
    assert "Address already in use" in output and "served on 127.0.0.1 only" not in output


class FakeServer:
    def __init__(self, host):
        self.effective_host, self.effective_port = host, 3030


@pytest.mark.parametrize(
    "error,falls_back",
    [
        (OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address"), True),
        (ValueError("Invalid host/port specified."), True),
        (OSError(errno.EADDRINUSE, "Address already in use"), False),
        (OSError(errno.EACCES, "Permission denied"), False),
    ],
)
def test_only_an_address_that_cannot_be_used_falls_back_to_loopback(monkeypatch, error, falls_back):
    # On BSD and macOS a socket can bind 127.0.0.1 on a port another holds on
    # 0.0.0.0, so retrying after a port in use would quietly serve the page
    # on loopback instead of ending, as a taken port must.
    attempts = []

    def create_server(app, host, port, **kwargs):
        attempts.append(host)
        if len(attempts) == 1:
            raise error
        return FakeServer(host)

    monkeypatch.setattr(app_main, "create_server", create_server)
    config = from_env({"MAGNETOMETER_HOST": "10.1.2.3"})
    health = Health(config)
    if falls_back:
        assert app_main._served(app_main._listen(None, config, health)) == "http://127.0.0.1:3030"
        assert attempts == ["10.1.2.3", "127.0.0.1"]
        assert health.snapshot()["config_warnings"][0].startswith("MAGNETOMETER_HOST='10.1.2.3' cannot be listened on")
    else:
        with pytest.raises(OSError):
            app_main._listen(None, config, health)
        assert attempts == ["10.1.2.3"] and health.snapshot()["config_warnings"] == []


def test_the_address_served_is_logged_as_listened_on():
    class Several:
        effective_listen = [("::1", "3030"), ("127.0.0.1", "3030")]

    assert app_main._served(Several()) == "http://[::1]:3030, http://127.0.0.1:3030"
