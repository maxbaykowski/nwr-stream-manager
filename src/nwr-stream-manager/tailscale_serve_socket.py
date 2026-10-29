"""A socket for Tailscale Serve to reach NWR Stream Manager by, which only tailscaled can use.

Serve can pass requests on to a local port, but anything on this computer connects to a port the
same way, such as another tunnel, and could add the headers Serve adds. On this socket, Linux says
which process connected (SO_PEERCRED), so a request on it is known to have come from tailscaled
running as root, and its headers can be believed: tailscaled takes them out of what a visitor
sends, then says itself whether the visitor is a signed-in user of the tailnet or came through
Funnel. See local_network.

Set up with: sudo tailscale serve --bg unix:<this socket's path>
"""

from __future__ import annotations

import logging
import os
import socket
import stat
import struct
import threading
from pathlib import Path
from typing import Callable

LOG = logging.getLogger(__name__)

SOCKET_FILE_NAME = "tailscale-serve.sock"
TAILSCALED_PROCESS_NAME = "tailscaled"
# The longest path a Unix socket can have on Linux, less its ending.
LONGEST_SOCKET_PATH = 107


def connected_process(connection: socket.socket) -> tuple[int, int]:
    """The process ID and user ID of whatever connected."""
    credentials = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, uid, _gid = struct.unpack("3i", credentials)
    return pid, uid


def is_tailscaled(pid: int, uid: int) -> bool:
    if uid != 0:
        return False
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip() == TAILSCALED_PROCESS_NAME
    except OSError:
        return False


def serve_command(path: Path) -> str:
    return f"sudo tailscale serve --bg unix:{path}"


class TailscaleServeSocket:
    """Takes connections from tailscaled on path, and hands each to on_connection as a file descriptor."""

    def __init__(self, path: Path, on_connection: Callable[[int], None], *, is_allowed: Callable[[int, int], bool] = is_tailscaled) -> None:
        self.path = path
        self.on_connection = on_connection
        self.is_allowed = is_allowed
        self.listener: socket.socket | None = None
        self.refused: set[int] = set()

    def start(self) -> bool:
        """Whether it's listening; it isn't if the path is too long or can't be used."""
        if len(os.fsencode(str(self.path))) > LONGEST_SOCKET_PATH:
            LOG.warning("the socket for Tailscale Serve can't be made: its path is too long (%s)", self.path)
            return False
        try:
            # One left over from before, such as after a crash; anything else there is left alone.
            if stat.S_ISSOCK(os.lstat(self.path).st_mode):
                os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            LOG.warning("the socket for Tailscale Serve can't be made: %s", exc)
            return False
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            # This user only, and root (tailscaled). Anything else that connects in between is refused
            # anyway, as nothing but tailscaled is let in.
            os.chmod(self.path, 0o600)
            listener.listen(64)
        except OSError as exc:
            listener.close()
            LOG.warning("the socket for Tailscale Serve can't be made: %s", exc)
            return False
        self.listener = listener
        threading.Thread(target=self._accept, args=(listener,), name="tailscale-serve-socket", daemon=True).start()
        return True

    def close(self) -> None:
        listener, self.listener = self.listener, None
        if listener is None:
            return
        try:
            listener.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        listener.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _accept(self, listener: socket.socket) -> None:
        while self.listener is listener:
            try:
                connection, _address = listener.accept()
            except OSError:
                return  # closed
            try:
                pid, uid = connected_process(connection)
            except OSError:
                connection.close()
                continue
            if not self.is_allowed(pid, uid):
                # Said once for each process, rather than for each connection it makes.
                if pid not in self.refused:
                    self.refused.add(pid)
                    LOG.warning("refused a connection to the Tailscale Serve socket from process %d (user %d): it isn't tailscaled", pid, uid)
                connection.close()
                continue
            try:
                self.on_connection(connection.detach())
            except Exception as exc:
                LOG.warning("a connection from Tailscale Serve failed: %s", exc)
