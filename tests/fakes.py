"""Test doubles shared across the suite."""

from __future__ import annotations

import errno
from collections import deque
from dataclasses import dataclass, field


class FakeClock:
    """A monotonic clock that only moves when told to (or when slept on)."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Expected:
    """One transfer the scripted bus expects, and what it answers."""

    address: int
    write: bytes
    read_length: int = 0
    reply: bytes = b""
    error: OSError | None = None


@dataclass
class ScriptedBus:
    """Fails the test on any transfer it was not told to expect, in order.

    This is the byte-level check: every register pointer, every data byte and
    every read length the driver puts on the bus is asserted exactly.
    """

    script: deque = field(default_factory=deque)
    description: str = "scripted test bus"
    log: list = field(default_factory=list)

    def expect(
        self, address: int, write: bytes, read_length: int = 0, reply: bytes = b"", error: OSError | None = None
    ):
        self.script.append(Expected(address, bytes(write), read_length, bytes(reply), error))
        return self

    def expect_status(self, address: int, *statuses: int):
        for status in statuses:
            self.expect(address, b"\x34", 1, bytes([status]))
        return self

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        self.log.append((address, bytes(write), read_length))
        assert self.script, f"unexpected transfer: address=0x{address:02X} write={write.hex()} read={read_length}"
        want = self.script.popleft()
        assert (address, bytes(write), read_length) == (want.address, want.write, want.read_length), (
            f"transfer mismatch: got address=0x{address:02X} write={write.hex()} read={read_length}, "
            f"expected address=0x{want.address:02X} write={want.write.hex()} read={want.read_length}"
        )
        if want.error is not None:
            raise want.error
        return want.reply

    def assert_done(self) -> None:
        assert not self.script, f"{len(self.script)} expected transfer(s) never happened"

    def close(self) -> None:
        pass


def nack() -> OSError:
    return OSError(121, "Remote I/O error")


class ModelBus:
    """Puts a simulated chip on a bus without TCP in between."""

    def __init__(self, model):
        self.model = model
        self.description = "in-process RM3100 model"

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        return self.model.transfer(address, write, read_length)

    def close(self) -> None:
        pass


class BrokenBus:
    """Every transfer fails the same way: a bus with nothing on it."""

    description = "bus with no device"

    def __init__(self, code: int = errno.ENXIO):
        self.code = code
        self.transfers = 0

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        self.transfers += 1
        raise OSError(self.code, "No such device or address")

    def close(self) -> None:
        pass
