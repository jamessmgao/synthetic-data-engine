"""
Refuse every network connection that doesn't stay on this machine.

Installed in every end-to-end process (and the pytest process), so no test can
reach a real model endpoint, whatever a code path does: the compose cluster
(Postgres, Redis, S3) and the API server are all on localhost; the inference
gateway is not.
"""

import ipaddress
import socket

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


class ExternalNetworkBlocked(ConnectionRefusedError):
    pass


def _local(address) -> bool:
    if not isinstance(address, tuple):  # AF_UNIX path
        return True
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:  # a hostname that wasn't resolved first
        return False


def _check(address) -> None:
    if not _local(address):
        raise ExternalNetworkBlocked(f"e2e tests may not connect to {address!r}")


def _connect(self, address):
    _check(address)
    return _real_connect(self, address)


def _connect_ex(self, address):
    _check(address)
    return _real_connect_ex(self, address)


def install() -> None:
    socket.socket.connect = _connect
    socket.socket.connect_ex = _connect_ex
