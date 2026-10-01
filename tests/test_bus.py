"""Choosing a transport, the i2c-dev transport's framing (on a fake SMBus), and
the TCP transport's handling of what a simulator peer sends back."""

import errno
import socket
import threading

import pytest

from retina_magnetometer.rm3100 import bus as bus_module
from retina_magnetometer.rm3100.bus import LinuxI2CBus, open_bus
from retina_magnetometer.rm3100.remote import RemoteI2CBus, encode


class FakeSMBus:
    """Records i2c_rdwr calls the way smbus2 would issue them to the kernel."""

    instances: list = []

    def __init__(self, path):
        self.path = path
        self.calls = []
        self.closed = False
        FakeSMBus.instances.append(self)

    def i2c_rdwr(self, *messages):
        self.calls.append([(m.addr, m.flags, len(m)) for m in messages])
        for m in messages:
            if m.flags & 0x0001:  # I2C_M_RD: fill the read buffer
                for i in range(len(m)):
                    m.buf[i] = bytes([0xA0 + i])

    def close(self):
        self.closed = True


@pytest.fixture
def fake_smbus(monkeypatch):
    import smbus2

    FakeSMBus.instances = []
    monkeypatch.setattr(smbus2, "SMBus", FakeSMBus)
    monkeypatch.setattr(bus_module.os.path, "exists", lambda path: path.startswith("/dev/i2c-"))
    return FakeSMBus


def test_repeated_start_is_one_combined_transaction(fake_smbus):
    b = LinuxI2CBus("/dev/i2c-1")
    assert b.transfer(0x20, b"\x24", 3) == b"\xa0\xa1\xa2"
    (call,) = fake_smbus.instances[0].calls
    assert call == [(0x20, 0, 1), (0x20, 1, 3)]  # write pointer, read 3, one STOP


def test_stop_framing_is_two_transactions(fake_smbus):
    b = LinuxI2CBus("/dev/i2c-1", repeated_start=False)
    b.transfer(0x21, b"\x36", 1)
    assert fake_smbus.instances[0].calls == [[(0x21, 0, 1)], [(0x21, 1, 1)]]


def test_plain_write(fake_smbus):
    b = LinuxI2CBus("/dev/i2c-1")
    assert b.transfer(0x20, b"\x00\x70") == b""
    assert fake_smbus.instances[0].calls == [[(0x20, 0, 2)]]
    b.close()
    assert fake_smbus.instances[0].closed


def test_missing_device_is_file_not_found(fake_smbus):
    with pytest.raises(FileNotFoundError) as info:
        LinuxI2CBus("/dev/nope")
    assert info.value.errno == errno.ENOENT


@pytest.mark.parametrize(
    "spec,path",
    [("1", "/dev/i2c-1"), ("i2c:3", "/dev/i2c-3"), ("/dev/i2c-2", "/dev/i2c-2"), ("i2c:///dev/i2c-1", "/dev/i2c-1")],
)
def test_open_bus_linux_forms(fake_smbus, spec, path):
    b = open_bus(spec)
    assert isinstance(b, LinuxI2CBus) and fake_smbus.instances[-1].path == path


def test_open_bus_tcp_does_not_connect_yet():
    b = open_bus("tcp://rm3100-sim:9100", timeout_s=0.5)
    assert isinstance(b, RemoteI2CBus)
    assert "rm3100-sim:9100" in b.description


@pytest.mark.parametrize("spec", ["tcp://nohost", "serial:///dev/ttyUSB0", "i2c", "banana"])
def test_open_bus_rejects_the_unknown(spec):
    with pytest.raises(ValueError):
        open_bus(spec)


class Peer:
    """A loopback line server that answers each request with the next canned
    reply, standing in for a simulator that speaks the protocol badly."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.connections = 0
        self._stop = threading.Event()
        self._listener = socket.create_server(("127.0.0.1", 0))
        self._listener.settimeout(0.05)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.connections += 1
            with conn, conn.makefile("rb") as reader:
                for _line in reader:
                    if not self.replies:
                        break
                    conn.sendall(self.replies.pop(0))

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self._listener.close()


@pytest.fixture
def peer():
    started = []

    def start(*replies):
        server = Peer(replies)
        bus = RemoteI2CBus("127.0.0.1", server.port, timeout_s=1.0)
        started.append((server, bus))
        return server, bus

    yield start
    for server, bus in started:
        bus.close()
        server.close()


@pytest.mark.parametrize(
    "reply",
    [
        {"ok": True, "read": None},
        {"ok": True, "read": "zz"},
        {"ok": True, "read": 42},
        {"ok": False, "errno": "EIO", "error": "bus error"},
        {"ok": False, "errno": None},
        {"ok": False, "errno": 1.5},
        {"ok": False, "errno": True},
        {"ok": False, "errno": 0},
        {"ok": "yes", "read": "00"},
        {"read": "00"},
    ],
    ids=lambda reply: encode(reply).decode().strip(),
)
def test_a_reply_with_a_bad_field_is_a_protocol_error(peer, reply):
    # Everything above the transport handles OSError and nothing else; a
    # TypeError or ValueError from a peer's reply would end the sampler.
    _, bus = peer(encode(reply))
    with pytest.raises(OSError) as info:
        bus.transfer(0x20, b"\x36", 1)
    assert info.value.errno == errno.EPROTO
    assert "simulator" in str(info.value)


def test_after_a_bad_reply_the_next_transfer_starts_a_fresh_connection(peer):
    server, bus = peer(encode({"ok": True, "read": None}), encode({"ok": True, "read": "22"}))
    with pytest.raises(OSError):
        bus.transfer(0x20, b"\x36", 1)
    assert bus.transfer(0x20, b"\x36", 1) == b"\x22"
    assert server.connections == 2


def test_a_failed_transfer_carries_the_peers_errno(peer):
    _, bus = peer(encode({"ok": False, "errno": 121, "error": "no acknowledge from 0x21"}))
    with pytest.raises(OSError) as info:
        bus.transfer(0x21, b"\x36", 1)
    assert info.value.errno == 121 and "no acknowledge" in str(info.value)


def test_a_failure_without_an_errno_is_an_io_error(peer):
    _, bus = peer(encode({"ok": False}))
    with pytest.raises(OSError) as info:
        bus.transfer(0x20, b"\x36", 1)
    assert info.value.errno == errno.EIO


def test_a_reply_of_the_wrong_length_is_a_protocol_error(peer):
    _, bus = peer(encode({"ok": True}), encode({"ok": True, "read": "0102"}))
    for _ in range(2):
        with pytest.raises(OSError) as info:
            bus.transfer(0x20, b"\x36", 1)
        assert info.value.errno == errno.EPROTO and "asked for 1 bytes" in str(info.value)


def test_a_good_reply_reads_and_writes(peer):
    _, bus = peer(encode({"ok": True, "read": "a5"}), encode({"ok": True, "read": ""}))
    assert bus.transfer(0x20, b"\x34", 1) == b"\xa5"
    assert bus.transfer(0x20, b"\x00\x70") == b""
