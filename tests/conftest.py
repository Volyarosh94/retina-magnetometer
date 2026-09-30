"""Suite-wide fixtures.

No test may reach the network: the node app runs on an operator's LAN and the
simulator is always local, so an outbound connection from a test is a bug.
Loopback stays open because the simulator server is tested over real TCP.
(Same rule as retina-gui's suite.)
"""

from __future__ import annotations

import ipaddress
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

_real_connect = socket.socket.connect


def _is_loopback(address) -> bool:
    host = address[0] if isinstance(address, tuple) else address
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def guarded(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(address):
            raise AssertionError(f"test tried to reach the network: {address!r}")
        return _real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
