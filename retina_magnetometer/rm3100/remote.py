"""I2C transfers over TCP, so the app can drive the RM3100 simulator.

The wire format is one JSON object per line in each direction, deliberately
readable with ``nc``:

    -> {"op": "transfer", "address": 32, "write": "24", "read": 9}
    <- {"ok": true, "read": "00a10f..."}
    <- {"ok": false, "errno": 121, "error": "no acknowledge from 0x21"}

``write`` and ``read`` payloads are hex. A failed transfer carries the errno a
Linux i2c-dev adapter would have returned, and the client raises it as
``OSError``, so everything above the transport sees one failure model whether
the sensor is simulated or soldered on. ``{"op": "hello"}`` answers with the
server's name and protocol version and is only used to describe the bus.

The server side lives in ``rm3100_sim.server``; the codec below is shared so
the two cannot drift apart.
"""

from __future__ import annotations

import errno
import json
import socket
import threading

PROTOCOL_VERSION = 1

# One line must never be allowed to grow without bound; the largest legitimate
# message is a few dozen bytes.
MAX_LINE_BYTES = 4096


def encode(message: dict) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


def decode(line: bytes) -> dict:
    message = json.loads(line)
    if not isinstance(message, dict):
        raise ValueError("expected a JSON object")
    return message


class RemoteI2CBus:
    """The client end: one persistent connection, reopened after any failure."""

    def __init__(self, host: str, port: int, *, timeout_s: float = 1.0):
        self.description = f"tcp://{host}:{port} (RM3100 simulator)"
        self._host = host
        self._port = port
        self._timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._reader = None
        # The web thread may describe the bus while the sampler is mid-transfer.
        self._lock = threading.Lock()

    def _connect(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout_s)
        sock.settimeout(self._timeout_s)
        self._sock = sock
        self._reader = sock.makefile("rb")

    def _drop(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            except OSError:
                pass
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._reader = None

    def _roundtrip(self, request: dict) -> dict:
        with self._lock:
            try:
                if self._sock is None:
                    self._connect()
                self._sock.sendall(encode(request))
                line = self._reader.readline(MAX_LINE_BYTES)
            except TimeoutError as exc:
                self._drop()
                raise OSError(errno.ETIMEDOUT, f"simulator did not answer within {self._timeout_s} s") from exc
            except OSError:
                self._drop()
                raise
            if not line:
                self._drop()
                raise OSError(errno.ECONNRESET, "simulator closed the connection")
            try:
                return decode(line)
            except ValueError as exc:
                self._drop()
                raise OSError(errno.EPROTO, f"unreadable reply from simulator: {exc}") from exc

    def transfer(self, address: int, write: bytes, read_length: int = 0) -> bytes:
        reply = self._roundtrip({"op": "transfer", "address": address, "write": write.hex(), "read": read_length})
        if not reply.get("ok"):
            raise OSError(int(reply.get("errno", errno.EIO)), str(reply.get("error", "transfer failed")))
        data = bytes.fromhex(reply.get("read", ""))
        if len(data) != read_length:
            raise OSError(errno.EPROTO, f"asked for {read_length} bytes, simulator returned {len(data)}")
        return data

    def hello(self) -> dict:
        return self._roundtrip({"op": "hello"})

    def close(self) -> None:
        with self._lock:
            self._drop()
