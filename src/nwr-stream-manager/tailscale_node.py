"""NWR Stream Manager as a device on a tailnet of its own, through libtailscale (tsnet).

libtailscale is optional and isn't packaged by distributions; nwr-stream-manager
--build-libtailscale builds it (libtailscale/build.sh). Without it, Tailscale can't be connected
from Settings, and nothing else changes.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)

LIBRARY_ENVIRONMENT = "NWR_STREAM_MANAGER_LIBTAILSCALE"
LIBRARY_PLACES = (
    # Where nwr-stream-manager --build-libtailscale puts it, for one user or, run as root, everyone.
    Path(os.path.expanduser("~/.local/lib/nwr-stream-manager/libtailscale.so")),
    Path("/usr/local/lib/nwr-stream-manager/libtailscale.so"),
    # Where a package puts it.
    Path("/usr/lib/nwr-stream-manager/libtailscale.so"),
    Path("/usr/lib64/nwr-stream-manager/libtailscale.so"),
)
# The web interface's ports on the tailnet, so its addresses need none: http://<name>.<tailnet>.ts.net/
# and, with a certificate, https://<name>.<tailnet>.ts.net/
TAILNET_WEB_PORT = 80
TAILNET_HTTPS_PORT = 443
# Tailscale Funnel, for the public internet: one of the ports Funnel allows, so the tailnet keeps 443
# and public visitors are always told apart from the tailnet's own devices.
FUNNEL_PORT = 8443
FUNNEL_PORTS_CAPABILITY = "https://tailscale.com/cap/funnel-ports"
# Getting a certificate can take a while the first time: Tailscale has Let's Encrypt issue it.
CERTIFICATE_TIMEOUT_SECONDS = 180.0
STATUS_INTERVAL_SECONDS = 1.0
# How long to wait for a device on the tailnet to answer, when the caller gives no time.
DIAL_TIMEOUT_SECONDS = 30.0
STATE_LOG_TEXT = {"NeedsLogin": "waiting to be signed in", "Running": "connected", "Stopped": "disconnected"}

_library: ctypes.CDLL | None = None
_library_error = ""
_library_lock = threading.Lock()


def libtailscale_path() -> Path | None:
    configured = os.environ.get(LIBRARY_ENVIRONMENT, "").strip()
    if configured:
        return Path(configured)
    return next((place for place in LIBRARY_PLACES if place.is_file()), None)


def load_libtailscale() -> ctypes.CDLL | None:
    """libtailscale, or None if it isn't installed or can't be loaded."""
    global _library, _library_error
    with _library_lock:
        if _library is not None or _library_error:
            return _library
        path = libtailscale_path()
        if path is None:
            # Not remembered: once it's built, it's used without restarting.
            return None
        try:
            lib = ctypes.CDLL(str(path))
        except OSError as exc:
            _library_error = f"libtailscale couldn't be loaded: {exc}"
            LOG.warning("%s", _library_error)
            return None
        c_int, c_char_p = ctypes.c_int, ctypes.c_char_p
        for name, arguments in {
            "tailscale_new": [],
            "tailscale_start": [c_int],
            "tailscale_close": [c_int],
            "tailscale_set_dir": [c_int, c_char_p],
            "tailscale_set_hostname": [c_int, c_char_p],
            "tailscale_set_ephemeral": [c_int, c_int],
            "tailscale_set_logfd": [c_int, c_int],
            "tailscale_status_json": [c_int, ctypes.POINTER(ctypes.c_void_p)],
            "tailscale_listen": [c_int, c_char_p, c_char_p, ctypes.POINTER(c_int)],
            "tailscale_accept": [c_int, ctypes.POINTER(c_int)],
            "tailscale_dial": [c_int, c_char_p, c_char_p, ctypes.POINTER(c_int)],
            "tailscale_getremoteaddr": [c_int, c_int, c_char_p, ctypes.c_size_t],
            "tailscale_loopback": [c_int, c_char_p, ctypes.c_size_t, c_char_p, c_char_p],
            "tailscale_errmsg": [c_int, c_char_p, ctypes.c_size_t],
        }.items():
            function = getattr(lib, name)
            function.argtypes = arguments
            function.restype = c_int
        _library = lib
        LOG.info("found libtailscale at %s", path)
        return lib


def libtailscale_error() -> str:
    return _library_error or "libtailscale isn't installed"


def _port_allowed(port: int, allowed: str) -> bool:
    """Whether port is in one of Funnel's allowed ports, a port or a range such as 8000-9000."""
    low, _, high = allowed.strip().partition("-")
    try:
        return int(low) <= port <= int(high or low)
    except ValueError:
        return False


class TailnetConnection(socket.socket):
    """A connection through the tailnet, as TCP connections are used: libtailscale hands it over
    as a stream socket within this computer, which has no TCP options (Tailscale sees to those)
    and no address of its own, so it gives the tailnet address it's connected to."""

    def __init__(self, *args: Any, peer: tuple[str, int] = ("", 0), **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.peer = peer

    def setsockopt(self, level: int, *args: Any) -> None:
        if level == socket.IPPROTO_TCP:
            return
        super().setsockopt(level, *args)

    def getpeername(self) -> tuple[str, int]:
        return self.peer


_libc = ctypes.CDLL(None)
_libc.free.argtypes = [ctypes.c_void_p]
_libc.free.restype = None


class TailscaleNode:
    """One node on a tailnet: started with what's in state_dir, signed in by a link if it
    hasn't been, then handing each connection from the tailnet to the listener for its port,
    along with the address it came from."""

    def __init__(
        self,
        lib: ctypes.CDLL,
        state_dir: Path,
        hostname: str,
        listeners: dict[int, Callable[[int, str], None]],
    ) -> None:
        self.lib = lib
        self.state_dir = state_dir
        self.hostname = hostname
        self.listeners = listeners
        self.handle: int | None = None
        self.listening: dict[int, int] = {}  # port: listener
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.status: dict[str, Any] = {"BackendState": "Starting"}
        self.error = ""
        self._log_read: int | None = None
        # Servers already said to be reached through the tailnet, so it's said once each.
        self.dialed: set[str] = set()

    # -- starting and stopping ---------------------------------------------

    def start(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        handle = self.lib.tailscale_new()
        read, write = os.pipe()
        self._log_read = read
        # libtailscale keeps writing to this end; it's closed with the node.
        for setting, value in (
            (self.lib.tailscale_set_dir, str(self.state_dir).encode()),
            (self.lib.tailscale_set_hostname, self.hostname.encode()),
            (self.lib.tailscale_set_ephemeral, 0),
            (self.lib.tailscale_set_logfd, write),
        ):
            if setting(handle, value) != 0:
                raise RuntimeError(self._error(handle))
        threading.Thread(target=self._read_log, args=(read,), name="tailscale-log", daemon=True).start()
        if self.lib.tailscale_start(handle) != 0:
            raise RuntimeError(self._error(handle))
        self.handle = handle
        threading.Thread(target=self._watch, name="tailscale-status", daemon=True).start()

    def close(self) -> None:
        """Disconnects, staying signed in for the next start."""
        with self.lock:
            if self.closed.is_set():
                return
            self.closed.set()
            handle, listening = self.handle, list(self.listening.values())
        for listener in listening:
            try:
                os.close(listener)
            except OSError:
                pass
        if handle is not None:
            self.lib.tailscale_close(handle)

    def sign_out(self) -> None:
        """Signs this node out of its tailnet, then disconnects."""
        if self.handle is not None and not self.closed.is_set():
            try:
                self._local_api("POST", "logout")
            except Exception as exc:
                LOG.warning("Tailscale sign-out failed: %s", exc)
        self.close()

    # -- what it's doing ---------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """What the settings page shows: its state, the sign-in link while it waits for one,
        and its name and addresses on the tailnet once it's connected."""
        with self.lock:
            status = dict(self.status)
            error = self.error
        me = status.get("Self") or {}
        users = status.get("User") or {}
        account = (users.get(str(me.get("UserID"))) or {}).get("LoginName", "")
        state = status.get("BackendState", "")
        return {
            "state": {"Running": "connected", "NeedsLogin": "needs-login"}.get(state, "error" if error else "starting"),
            "auth_url": status.get("AuthURL", "") if state == "NeedsLogin" else "",
            "name": str(me.get("DNSName", "")).rstrip("."),
            "addresses": list(me.get("TailscaleIPs") or []),
            "account": account,
            "error": error,
        }

    def _watch(self) -> None:
        listening = False
        while not self.closed.wait(STATUS_INTERVAL_SECONDS):
            status = self._read_status()
            if status is None:
                continue
            with self.lock:
                previous = self.status.get("BackendState")
                self.status = status
            state = status.get("BackendState")
            # Only the states worth knowing about: the ones between are Tailscale's own.
            if state != previous and state in STATE_LOG_TEXT:
                LOG.info("Tailscale: %s", STATE_LOG_TEXT[state])
            if state == "Running" and not listening:
                listening = self._listen()

    def _read_status(self) -> dict[str, Any] | None:
        out = ctypes.c_void_p()
        if self.handle is None or self.lib.tailscale_status_json(self.handle, ctypes.byref(out)) != 0:
            return None
        try:
            return json.loads(ctypes.string_at(out).decode("utf-8"))
        except ValueError:
            return None
        finally:
            _libc.free(out)

    # -- connecting out ----------------------------------------------------

    def tailnet_address(self, host: str) -> str | None:
        """The Tailscale address of the device on the tailnet that host names, by its name,
        its tailnet name in full or in part, or one of its addresses; None if it's none of them."""
        with self.lock:
            status = self.status
        if status.get("BackendState") != "Running":
            return None
        wanted = host.strip().strip("[]").lower().rstrip(".")
        for peer in (status.get("Peer") or {}).values():
            addresses = [str(address) for address in peer.get("TailscaleIPs") or []]
            dns_name = str(peer.get("DNSName", "")).lower().rstrip(".")
            names = {str(peer.get("HostName", "")).lower(), dns_name, dns_name.split(".")[0]} - {""}
            if addresses and (wanted in names or wanted in addresses):
                if wanted in addresses:
                    return wanted
                return next((address for address in addresses if ":" not in address), addresses[0])
        return None

    def dial(self, host: str, port: int, timeout: float | None = None) -> socket.socket | None:
        """A connection to host through the tailnet, or None if host isn't one of its devices."""
        address = self.tailnet_address(host)
        if address is None or self.closed.is_set():
            return None
        target = (f"[{address}]:{port}" if ":" in address else f"{address}:{port}").encode()
        outcome: dict[str, Any] = {}
        finished = threading.Event()

        def run() -> None:
            connection = ctypes.c_int()
            result = self.lib.tailscale_dial(self.handle, b"tcp", target, ctypes.byref(connection))
            with self.lock:
                outcome.update(result=result, fd=connection.value, error="" if result == 0 else self._error(self.handle))
                abandoned = outcome.get("abandoned", False)
            finished.set()
            if abandoned and result == 0:
                os.close(connection.value)

        # libtailscale waits as long as it takes; this doesn't.
        threading.Thread(target=run, name="tailscale-dial", daemon=True).start()
        finished.wait(timeout or DIAL_TIMEOUT_SECONDS)
        with self.lock:
            # Decided here, not by whether the wait ended: it may finish meanwhile.
            if "result" not in outcome:
                outcome["abandoned"] = True
                raise TimeoutError(f"{host} didn't answer through Tailscale")
        if outcome["result"] != 0:
            raise ConnectionError(f"couldn't reach {host} through Tailscale: {outcome['error']}")
        connection = TailnetConnection(fileno=outcome["fd"], peer=(address, port))
        connection.settimeout(timeout)
        if host not in self.dialed:
            self.dialed.add(host)
            LOG.info("%s is on your tailnet, so it's reached through Tailscale (%s)", host, address)
        return connection

    # -- serving -----------------------------------------------------------

    def _listen(self) -> bool:
        for port, on_connection in self.listeners.items():
            if port in self.listening:
                continue  # from an earlier try
            listener = ctypes.c_int()
            if self.lib.tailscale_listen(self.handle, b"tcp", f":{port}".encode(), ctypes.byref(listener)) != 0:
                with self.lock:
                    self.error = f"couldn't listen on the tailnet: {self._error(self.handle)}"
                LOG.warning("Tailscale %s", self.error)
                return False
            with self.lock:
                self.listening[port] = listener.value
            threading.Thread(
                target=self._accept, args=(listener.value, on_connection), name=f"tailscale-accept-{port}", daemon=True
            ).start()
        with self.lock:
            self.error = ""
        if TAILNET_WEB_PORT in self.listeners:
            LOG.info("RTL-SDR control web interface available on your tailnet at http://%s/", self.snapshot()["name"])
        return True

    # -- HTTPS -------------------------------------------------------------

    def certificate_name(self) -> str:
        """The name a certificate can be had for, or "" if the tailnet doesn't have HTTPS on."""
        with self.lock:
            status = self.status
        if status.get("BackendState") != "Running":
            return ""
        return next((str(name).rstrip(".") for name in status.get("CertDomains") or [] if name), "")

    def certificate(self) -> tuple[bytes, bytes]:
        """The certificate for its name and its key, as PEM: Tailscale keeps it, and renews it
        when it's asked for close to expiring."""
        name = self.certificate_name()
        if not name:
            raise RuntimeError("HTTPS isn't turned on for this tailnet")
        pair = self._local_api("GET", f"cert/{name}?type=pair", timeout=CERTIFICATE_TIMEOUT_SECONDS)
        start = pair.find(b"-----BEGIN CERTIFICATE-----")
        if start < 0:
            raise RuntimeError("Tailscale gave no certificate")
        key, certificate = pair[:start].strip() + b"\n", pair[start:].strip() + b"\n"
        if b"PRIVATE KEY" not in key:
            raise RuntimeError("Tailscale gave no key with the certificate")
        return certificate, key

    def https_enable_url(self) -> str:
        """Where to turn HTTPS on for the tailnet, as `tailscale serve` shows it; "" if it's on, or
        it's not for this account to turn on."""
        try:
            answer = json.loads(self._local_api("POST", "query-feature?feature=serve"))
        except (OSError, ValueError, RuntimeError) as exc:
            LOG.debug("Tailscale didn't say how to turn HTTPS on: %s", exc)
            return ""
        return "" if answer.get("Complete") else str(answer.get("URL") or "")

    # -- Funnel ------------------------------------------------------------

    def _capabilities(self) -> set[str]:
        with self.lock:
            me = self.status.get("Self") or {}
        return {str(item) for item in me.get("Capabilities") or []} | {str(item) for item in (me.get("CapMap") or {})}

    def can_funnel(self, port: int = FUNNEL_PORT) -> bool:
        """Whether the tailnet lets this device use Funnel on port, as the tailscale command checks:
        HTTPS on, the funnel permission, and the port among those Funnel is allowed."""
        capabilities = self._capabilities()
        if "https" not in capabilities or "funnel" not in capabilities:
            return False
        for capability in capabilities:
            if capability.startswith(FUNNEL_PORTS_CAPABILITY + "?"):
                ports = urllib.parse.parse_qs(urllib.parse.urlsplit(capability).query).get("ports", [""])[0]
                return any(_port_allowed(port, item) for item in ports.split(","))
        return False

    def funnel_enable_url(self) -> str:
        """Where the tailnet's owner gives devices Funnel permission; "" if it's there already, or
        it's not for this account to give."""
        try:
            answer = json.loads(self._local_api("POST", "query-feature?feature=funnel"))
        except (OSError, ValueError, RuntimeError) as exc:
            LOG.debug("Tailscale didn't say how to allow Funnel: %s", exc)
            return ""
        return "" if answer.get("Complete") else str(answer.get("URL") or "")

    def set_funnel(self, target: str | None) -> None:
        """Opens FUNNEL_PORT to the internet, through Tailscale's web proxy to target, a local web
        address; or with None, closes it. The proxy says where each request came from, which is
        how NWR Stream Manager tells public visitors from everyone else."""
        name = self.certificate_name()
        config: dict[str, Any] = {}
        if target is not None:
            if not name:
                raise RuntimeError("HTTPS isn't turned on for this tailnet")
            host_port = f"{name}:{FUNNEL_PORT}"
            config = {
                "TCP": {str(FUNNEL_PORT): {"HTTPS": True}},
                "Web": {host_port: {"Handlers": {"/": {"Proxy": target}}}},
                "AllowFunnel": {host_port: True},
            }
        self._local_api("POST", "serve-config", body=json.dumps(config).encode())

    def own_address(self) -> str:
        """Its own Tailscale address, IPv4 if it has one; "" while it isn't connected."""
        with self.lock:
            status = self.status
        if status.get("BackendState") != "Running":
            return ""
        addresses = [str(address) for address in (status.get("Self") or {}).get("TailscaleIPs") or []]
        return next((address for address in addresses if ":" not in address), addresses[0] if addresses else "")

    def peer_addresses(self) -> list[str]:
        """The Tailscale addresses (IPv4) of the other devices online on the tailnet, but not
        phones and tablets, which can't be running NWR Stream Manager."""
        with self.lock:
            status = self.status
        if status.get("BackendState") != "Running":
            return []
        addresses = []
        for peer in (status.get("Peer") or {}).values():
            if not peer.get("Online") or str(peer.get("OS", "")).lower() in {"ios", "android", "ipados", "tvos"}:
                continue
            addresses.extend(str(address) for address in peer.get("TailscaleIPs") or [] if ":" not in str(address))
        return addresses

    def _accept(self, listener: int, on_connection: Callable[[int, str], None]) -> None:
        while not self.closed.is_set():
            connection = ctypes.c_int()
            if self.lib.tailscale_accept(listener, ctypes.byref(connection)) != 0:
                if not self.closed.is_set():
                    LOG.warning("Tailscale stopped taking connections: %s", self._error(self.handle))
                return
            address = ctypes.create_string_buffer(64)
            self.lib.tailscale_getremoteaddr(listener, connection.value, address, len(address))
            try:
                on_connection(connection.value, address.value.decode("ascii", "replace"))
            except Exception as exc:
                LOG.warning("a connection from the tailnet failed: %s", exc)

    # -- libtailscale ------------------------------------------------------

    def _error(self, handle: int | None) -> str:
        if handle is None:
            return "Tailscale isn't running"
        buffer = ctypes.create_string_buffer(1024)
        self.lib.tailscale_errmsg(handle, buffer, len(buffer))
        return buffer.value.decode("utf-8", "replace") or "unknown error"

    def _local_api(self, method: str, path: str, *, timeout: float = 30.0, body: bytes | None = None) -> bytes:
        address = ctypes.create_string_buffer(256)
        proxy, local = ctypes.create_string_buffer(33), ctypes.create_string_buffer(33)
        if self.lib.tailscale_loopback(self.handle, address, len(address), proxy, local) != 0:
            raise RuntimeError(self._error(self.handle))
        request = urllib.request.Request(
            f"http://{address.value.decode()}/localapi/v0/{path}",
            data=body,
            method=method,
            headers={"Sec-Tailscale": "localapi", "Authorization": "Basic " + base64.b64encode(b":" + local.value).decode()},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()

    @staticmethod
    def _read_log(read: int) -> None:
        """Tailscale's own log, kept to debugging: it's long, and says what's worth saying
        in other ways."""
        with os.fdopen(read, "r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                LOG.debug("tsnet: %s", line.rstrip())
