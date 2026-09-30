"""The simulator over real TCP, through the app's own remote bus."""

import errno
import json
import socket
import threading

import pytest

from retina_magnetometer.rm3100.bus import open_bus
from retina_magnetometer.rm3100.driver import find_rm3100
from retina_magnetometer.rm3100.remote import RemoteI2CBus
from rm3100_sim import scenario as sc
from rm3100_sim.device import EREMOTEIO, RM3100Model, SimClock
from rm3100_sim.server import SimulatorServer


@pytest.fixture
def server():
    scenario = sc.load("quiet-day", start=1_790_726_400.0)
    model = RM3100Model(scenario, SimClock(scenario.start))
    srv = SimulatorServer(("127.0.0.1", 0), model)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
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
            (b"not json\n", errno.EPROTO),
            (b"[1, 2]\n", errno.EPROTO),
            (b'{"op": "dance"}\n', errno.EINVAL),
            (b'{"op": "transfer", "address": 300, "write": "36", "read": 1}\n', errno.EINVAL),
            (b'{"op": "transfer", "address": 32, "write": "zz", "read": 1}\n', errno.EINVAL),
            (b'{"op": "transfer", "address": 32, "write": "36", "read": 100000}\n', errno.EINVAL),
        ],
    )
    def test_bad_requests_are_answered_not_fatal(self, server, line, code):
        reply = raw_exchange(server, line)
        assert reply["ok"] is False and reply["errno"] == code


class TestClientResilience:
    def test_reconnects_after_the_server_restarts(self):
        scenario = sc.load("quiet-day", start=1_790_726_400.0)
        model = RM3100Model(scenario, SimClock(scenario.start))
        srv = SimulatorServer(("127.0.0.1", 0), model)
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        bus = RemoteI2CBus("127.0.0.1", port, timeout_s=1.0)
        assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
        srv.shutdown()
        srv.server_close()
        with pytest.raises(OSError):
            bus.transfer(0x20, b"\x36", 1)
        srv2 = SimulatorServer(("127.0.0.1", port), model)
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
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
