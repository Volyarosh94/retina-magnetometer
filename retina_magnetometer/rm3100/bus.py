"""The I2C transports the RM3100 driver runs over, and how one is chosen.

Everything the driver says to the sensor is a *transfer*: write some bytes (a
register pointer, optionally followed by data) and, for a read, read some bytes
back. Two transports implement that:

- ``LinuxI2CBus`` talks to a real sensor through Linux's i2c-dev, the
  ``/dev/i2c-N`` character device.
- ``RemoteI2CBus`` (``remote.py``) sends the same transfers over TCP to the
  RM3100 simulator.

Which one runs is configuration (``MAGNETOMETER_BUS``), so moving from the
simulator to hardware changes no code. Both report failure the way i2c-dev
does, as ``OSError`` with a Linux errno, so the driver and the sampler handle a
NACK from the simulator exactly as they would one from the wire.
"""

from __future__ import annotations

import errno
import os
import re
from typing import Protocol
from urllib.parse import urlparse


class I2CBus(Protocol):
    """A bus the driver can address a 7-bit device on."""

    description: str

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        """Write ``write`` to ``address``, then read ``read_length`` bytes.

        Raises ``OSError`` carrying a Linux errno when the device does not
        acknowledge or the bus fails.
        """
        ...

    def close(self) -> None: ...


class LinuxI2CBus:
    """A real I2C adapter through i2c-dev (``/dev/i2c-N``).

    Register reads use the ``I2C_RDWR`` ioctl, one combined transaction with a
    repeated start between the pointer write and the read. That is what
    ArduPilot's Linux HAL, the kernel's regmap and Zephyr use with this chip.
    PNI's manual draws a STOP there instead (UM16 p.28, p.43), which PX4 and
    HamSCI use; ``repeated_start=False`` selects that framing, for the case a
    board turns out to need it. Both are proven on hardware; neither is proven
    on a Pi 5's RP1 adapter yet (see docs/hardware-verification.md).
    """

    def __init__(self, path: str, *, repeated_start: bool = True):
        # Imported here so that a machine without smbus2 installed can still
        # import the package and run against the simulator.
        from smbus2 import SMBus

        if not os.path.exists(path):
            raise FileNotFoundError(errno.ENOENT, "I2C bus device not found", path)
        self.description = f"{path} ({'repeated start' if repeated_start else 'stop between'} framing)"
        self._repeated_start = repeated_start
        self._bus = SMBus(path)

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        from smbus2 import i2c_msg

        outgoing = i2c_msg.write(address, write)
        if read_length == 0:
            self._bus.i2c_rdwr(outgoing)
            return b""
        incoming = i2c_msg.read(address, read_length)
        if self._repeated_start:
            self._bus.i2c_rdwr(outgoing, incoming)
        else:
            self._bus.i2c_rdwr(outgoing)
            self._bus.i2c_rdwr(incoming)
        return bytes(incoming)

    def close(self) -> None:
        self._bus.close()


_I2C_NUMBER = re.compile(r"^(?:i2c:)?(\d+)$")


def open_bus(spec: str, *, repeated_start: bool = True, timeout_s: float = 1.0) -> I2CBus:
    """Open the bus named by a ``MAGNETOMETER_BUS`` value.

    Accepted forms:

    - ``/dev/i2c-1`` or ``i2c:///dev/i2c-1``: a Linux I2C adapter.
    - ``1`` or ``i2c:1``: shorthand for ``/dev/i2c-1``.
    - ``tcp://host:port``: the RM3100 simulator.
    """
    spec = spec.strip()
    number = _I2C_NUMBER.match(spec)
    if number:
        return LinuxI2CBus(f"/dev/i2c-{number.group(1)}", repeated_start=repeated_start)
    if spec.startswith("/"):
        return LinuxI2CBus(spec, repeated_start=repeated_start)
    parsed = urlparse(spec)
    if parsed.scheme == "i2c" and parsed.path:
        return LinuxI2CBus(parsed.path, repeated_start=repeated_start)
    if parsed.scheme == "tcp":
        if not parsed.hostname or not parsed.port:
            raise ValueError(f"a tcp bus needs a host and a port, e.g. tcp://rm3100-sim:9100 (got {spec!r})")
        from retina_magnetometer.rm3100.remote import RemoteI2CBus

        return RemoteI2CBus(parsed.hostname, parsed.port, timeout_s=timeout_s)
    raise ValueError(f"unrecognised bus {spec!r}: expected /dev/i2c-N, i2c:N or tcp://host:port")
