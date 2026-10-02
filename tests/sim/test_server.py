"""The simulator over real TCP, through the app's own remote bus."""

import errno
import json
import socket
import threading
import time

import pytest

from retina_magnetometer.rm3100.bus import open_bus
from retina_magnetometer.rm3100.driver import find_rm3100
from retina_magnetometer.rm3100.remote import MAX_LINE_BYTES, RemoteI2CBus
from rm3100_sim import scenario as sc
from rm3100_sim.device import EREMOTEIO, RM3100Model, SimClock
from rm3100_sim.server import SimulatorServer

# The errnos a Linux i2c-dev adapter returns, by value: clients run on Linux
# whatever the simulator runs on, and macOS numbers EPROTO 100.
LINUX_EPROTO, LINUX_EINVAL, LINUX_EIO = 71, 22, 5


def serve(srv):
    # A short poll interval, so that shutdown() returns at once.
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()


@pytest.fixture
def server():
    scenario = sc.load("quiet-day", start=1_790_726_400.0)
    model = RM3100Model(scenario, SimClock(scenario.start))
    srv = SimulatorServer(("127.0.0.1", 0), model)
    serve(srv)
    yield srv
    srv.shutdown()
    srv.server_close()


def port_of(srv):
    return srv.server_address[1]


def raw_exchange(srv, payload: bytes) -> dict:
    with socket.create_connection(("127.0.0.1", port_of(srv)), timeout=2) as sock:
        sock.sendall(payload)
        return json.loads(sock.makefile("rb").readline())


class TestProtocol:
    def test_hello(self, server):
        bus = RemoteI2CBus("127.0.0.1", port_of(server))
        reply = bus.hello()
        assert reply["ok"] and reply["server"] == "rm3100-sim" and reply["protocol"] == 1
        assert reply["scenario"] == "quiet-day"
        bus.close()

    def test_driver_runs_over_tcp(self, server):
        bus = open_bus(f"tcp://127.0.0.1:{port_of(server)}")
        sensor = find_rm3100(bus)
        sensor.set_cycle_count(200)
        m = sensor.single_measurement()
        assert 40_000 < (m.x_nt**2 + m.y_nt**2 + m.z_nt**2) ** 0.5 < 60_000
        assert sensor.self_test().passed
        bus.close()

    def test_nack_comes_back_as_oserror_with_linux_errno(self, server):
        bus = RemoteI2CBus("127.0.0.1", port_of(server))
        with pytest.raises(OSError) as info:
            bus.transfer(0x23, b"\x36", 1)
        assert info.value.errno == EREMOTEIO
        # The connection survives a device error.
        assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
        bus.close()

    @pytest.mark.parametrize(
        "line,code",
        [
            (b"not json\n", LINUX_EPROTO),
            (b"[1, 2]\n", LINUX_EPROTO),
            (b'{"op": "dance"}\n', LINUX_EINVAL),
            (b'{"op": "transfer", "address": 300, "write": "36", "read": 1}\n', LINUX_EINVAL),
            (b'{"op": "transfer", "address": 32, "write": "zz", "read": 1}\n', LINUX_EINVAL),
            (b'{"op": "transfer", "address": 32, "write": "36", "read": 100000}\n', LINUX_EINVAL),
        ],
    )
    def test_bad_requests_are_answered_not_fatal(self, server, line, code):
        reply = raw_exchange(server, line)
        assert reply["ok"] is False and reply["errno"] == code

    @pytest.mark.parametrize("size", [MAX_LINE_BYTES, MAX_LINE_BYTES + 1, 10_000, 3 * MAX_LINE_BYTES])
    def test_an_oversized_line_gets_one_reply_and_requests_stay_in_step(self, server, size):
        with socket.create_connection(("127.0.0.1", port_of(server)), timeout=2) as sock:
            sock.sendall(b"x" * size + b"\n" + b'{"op": "hello"}\n')
            replies = sock.makefile("rb")
            first = json.loads(replies.readline())
            assert first["ok"] is False and first["errno"] == LINUX_EPROTO and "longer than" in first["error"]
            assert json.loads(replies.readline())["server"] == "rm3100-sim"  # the next reply is the next answer

    def test_a_fault_inside_the_model_is_an_io_error_not_a_dropped_connection(self, server, monkeypatch):
        def broken(address, write, read_length=0):
            raise ZeroDivisionError("model bug")

        monkeypatch.setattr(server.model, "transfer", broken)
        with socket.create_connection(("127.0.0.1", port_of(server)), timeout=2) as sock:
            sock.sendall(b'{"op": "transfer", "address": 32, "write": "36", "read": 1}\n{"op": "hello"}\n')
            replies = sock.makefile("rb")
            reply = json.loads(replies.readline())
            assert reply["ok"] is False and reply["errno"] == LINUX_EIO and "model bug" in reply["error"]
            assert json.loads(replies.readline())["ok"] is True

    def test_a_zero_cycle_count_over_tcp_keeps_the_session(self, server):
        # A legal register value (UM16 p.30): it must not cost the client its connection.
        bus = RemoteI2CBus("127.0.0.1", port_of(server))
        try:
            bus.transfer(0x20, b"\x04" + b"\x00\x00" * 3)
            bus.transfer(0x20, b"\x00\x70")
            deadline = time.monotonic() + 2
            while not bus.transfer(0x20, b"\x34", 1)[0] & 0x80:  # the conversion has run
                assert time.monotonic() < deadline
            assert bus.transfer(0x20, b"\x24", 9) == bytes(9)
            assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
        finally:
            bus.close()

    def test_a_refused_write_is_a_nack_over_tcp(self, server):
        bus = RemoteI2CBus("127.0.0.1", port_of(server))
        try:
            with pytest.raises(OSError) as info:
                bus.transfer(0x20, b"\x50\x01")  # an undefined register
            assert info.value.errno == EREMOTEIO and "NACK0" in str(info.value)
            assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
        finally:
            bus.close()


class TestClientResilience:
    def test_reconnects_after_the_server_restarts(self):
        scenario = sc.load("quiet-day", start=1_790_726_400.0)
        model = RM3100Model(scenario, SimClock(scenario.start))
        srv = SimulatorServer(("127.0.0.1", 0), model)
        port = srv.server_address[1]
        serve(srv)
        bus = RemoteI2CBus("127.0.0.1", port, timeout_s=1.0)
        assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
        srv.shutdown()
        srv.server_close()
        with pytest.raises(OSError):
            bus.transfer(0x20, b"\x36", 1)
        srv2 = SimulatorServer(("127.0.0.1", port), model)
        serve(srv2)
        try:
            assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
        finally:
            bus.close()
            srv2.shutdown()
            srv2.server_close()

    def test_connection_refused_is_an_oserror(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        bus = RemoteI2CBus("127.0.0.1", port, timeout_s=0.5)
        with pytest.raises(OSError):
            bus.transfer(0x20, b"\x36", 1)

    def test_silent_server_times_out(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        bus = RemoteI2CBus("127.0.0.1", listener.getsockname()[1], timeout_s=0.2)
        try:
            with pytest.raises(OSError) as info:
                bus.transfer(0x20, b"\x36", 1)
            assert info.value.errno == errno.ETIMEDOUT
        finally:
            bus.close()
            listener.close()
