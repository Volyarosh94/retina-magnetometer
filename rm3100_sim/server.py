"""Serve a simulated RM3100 over TCP, speaking ``retina_magnetometer``'s
remote-I2C protocol (see ``retina_magnetometer/rm3100/remote.py``).

The node app connects with ``MAGNETOMETER_BUS=tcp://<host>:<port>`` and cannot
tell the difference from a sensor on ``/dev/i2c-1`` except by the description:
every transfer reaches the register model, and every failure comes back as the
errno a Linux adapter would return.
"""

from __future__ import annotations

import logging
import socket
import socketserver
import threading

from retina_magnetometer.rm3100.remote import MAX_LINE_BYTES, PROTOCOL_VERSION, decode, encode
from rm3100_sim.device import RM3100Model

log = logging.getLogger(__name__)

# The largest transfer a client may ask for: the whole register file twice over.
_MAX_TRANSFER_BYTES = 256

# Errnos by their Linux values, which is what an i2c-dev adapter returns and
# what the app's client expects, whatever this server runs on: macOS, for one,
# numbers EPROTO 100 where Linux has 71. EREMOTEIO comes from the model.
_EPROTO = 71
_EINVAL = 22
_EIO = 5


class _Handler(socketserver.StreamRequestHandler):
    server: SimulatorServer

    def handle(self) -> None:
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        log.info("client connected: %s", peer)
        self.server.track(self.request)
        try:
            while True:
                try:
                    line = self.rfile.readline(MAX_LINE_BYTES)
                    oversized = len(line) == MAX_LINE_BYTES and not line.endswith(b"\n")
                    if oversized and not self._skip_rest_of_line():
                        break
                except OSError:
                    break
                if not line:
                    break
                if oversized:
                    # One reply for the one request, however long, so the
                    # client's next reply is the answer to its next request.
                    reply = {"ok": False, "errno": _EPROTO, "error": f"request longer than {MAX_LINE_BYTES} bytes"}
                else:
                    reply = self.server.answer(line)
                try:
                    self.wfile.write(encode(reply))
                except OSError:
                    break
        finally:
            self.server.untrack(self.request)
            log.info("client disconnected: %s", peer)

    def _skip_rest_of_line(self) -> bool:
        """Read and drop the rest of an oversized line, a bounded chunk at a
        time. False if the client closed the connection first."""
        while True:
            chunk = self.rfile.readline(MAX_LINE_BYTES)
            if not chunk:
                return False
            if chunk.endswith(b"\n"):
                return True


class SimulatorServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], model: RM3100Model):
        self.model = model
        self._clients: set = set()
        self._clients_lock = threading.Lock()
        super().__init__(address, _Handler)

    def track(self, sock) -> None:
        with self._clients_lock:
            self._clients.add(sock)

    def untrack(self, sock) -> None:
        with self._clients_lock:
            self._clients.discard(sock)

    def server_close(self) -> None:
        """Stop listening *and* drop every client, as a process exit would.

        socketserver leaves established connections to their handler threads;
        a simulator that stops must look stopped to the app, or its recovery
        path is never exercised.
        """
        super().server_close()
        with self._clients_lock:
            clients, self._clients = list(self._clients), set()
        for sock in clients:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def answer(self, line: bytes) -> dict:
        try:
            request = decode(line)
        except ValueError as exc:
            return {"ok": False, "errno": _EPROTO, "error": f"bad request: {exc}"}
        op = request.get("op")
        if op == "hello":
            return {
                "ok": True,
                "server": "rm3100-sim",
                "protocol": PROTOCOL_VERSION,
                "scenario": self.model.scenario.name,
                "seed": self.model.scenario.seed,
                "address": self.model.address,
            }
        if op != "transfer":
            return {"ok": False, "errno": _EINVAL, "error": f"unknown op {op!r}"}
        address = request.get("address")
        read_length = request.get("read", 0)
        try:
            write = bytes.fromhex(request.get("write", ""))
        except (TypeError, ValueError):
            return {"ok": False, "errno": _EINVAL, "error": "write must be hex"}
        if (
            isinstance(address, bool)
            or not isinstance(address, int)
            or not 0 <= address <= 0x7F
            or isinstance(read_length, bool)
            or not isinstance(read_length, int)
            or not 0 <= read_length <= _MAX_TRANSFER_BYTES
            or len(write) > _MAX_TRANSFER_BYTES
        ):
            return {"ok": False, "errno": _EINVAL, "error": "address must be 0..127 and lengths 0..256"}
        try:
            data = self.model.transfer(address, write, read_length)
        except OSError as exc:
            return {"ok": False, "errno": exc.errno or _EIO, "error": exc.strerror or str(exc)}
        except Exception as exc:
            # A fault in the model is the simulator's bug, not the client's.
            # The client gets an I/O error, as a bus in trouble would give it,
            # and keeps its connection; the log gets the traceback.
            log.exception("transfer to 0x%02X failed inside the model", address)
            return {"ok": False, "errno": _EIO, "error": f"simulator fault: {exc!r}"}
        return {"ok": True, "read": data.hex()}
