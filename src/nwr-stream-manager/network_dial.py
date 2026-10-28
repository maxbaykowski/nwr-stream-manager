"""Connecting to other servers: through the tailnet NWR Stream Manager joined, when the server is
one of its devices, and as usual otherwise.

NWR Stream Manager can be on a tailnet without the computer it runs on being on it (see
tailscale_node), so the computer alone can't reach the tailnet's devices. Whether a server is one
of them isn't guessed from its address: the tailnet says which devices it has, by name and address.
"""

from __future__ import annotations

import socket
import threading
from typing import Callable

# Given a host, a port and a timeout: a connection to it through the tailnet, or None if it
# isn't a device on the tailnet.
TailnetDialer = Callable[[str, int, "float | None"], "socket.socket | None"]

_tailnet_dialer: TailnetDialer | None = None
# NWR Stream Manager's own address on the tailnet it joined, or "" while it isn't connected.
_own_tailnet_address: Callable[[], str] | None = None
# The Tailscale addresses of the other devices online on that tailnet.
_own_tailnet_peers: Callable[[], list[str]] | None = None
_lock = threading.Lock()


def set_tailnet_dialer(
    dialer: TailnetDialer | None,
    own_address: Callable[[], str] | None = None,
    peers: Callable[[], list[str]] | None = None,
) -> None:
    global _tailnet_dialer, _own_tailnet_address, _own_tailnet_peers
    with _lock:
        _tailnet_dialer = dialer
        _own_tailnet_address = own_address
        _own_tailnet_peers = peers


def own_tailnet_peers() -> list[str]:
    """The other devices online on the tailnet NWR Stream Manager joined itself, by address."""
    with _lock:
        provider = _own_tailnet_peers
    return provider() if provider is not None else []


def own_tailnet_address() -> str:
    """NWR Stream Manager's own Tailscale address, for paired devices to reach it by; "" if it
    hasn't joined a tailnet itself."""
    with _lock:
        provider = _own_tailnet_address
    return provider() if provider is not None else ""


def create_connection(address: tuple[str, int], timeout: float | None = None, source_address=None) -> socket.socket:
    """socket.create_connection, through the tailnet for its devices."""
    with _lock:
        dialer = _tailnet_dialer
    host, port = address[0], int(address[1])
    if dialer is not None:
        connection = dialer(host, port, timeout)
        if connection is not None:
            return connection
    return socket.create_connection((host, port), timeout=timeout, source_address=source_address)
