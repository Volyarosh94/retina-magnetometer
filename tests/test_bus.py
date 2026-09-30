"""Choosing a transport, and the i2c-dev transport's framing (on a fake SMBus)."""

import errno

import pytest

from retina_magnetometer.rm3100 import bus as bus_module
from retina_magnetometer.rm3100.bus import LinuxI2CBus, open_bus
from retina_magnetometer.rm3100.remote import RemoteI2CBus


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
