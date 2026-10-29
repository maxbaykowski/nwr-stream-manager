"""Tailscale, from Settings: NWR Stream Manager joined to a tailnet as a device of its own."""

from __future__ import annotations

import json
import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

from .microhttpd_server import MicrohttpdError
from .network_dial import set_tailnet_dialer
from .state_files import atomic_write_json
from .remote_sdr import REMOTE_SDR_TCP_PORT, system_tailscale_is_up
from .network import set_tailscale_serve_socket, tailscale_serve_socket_use_now
from .tailscale_serve_socket import SOCKET_FILE_NAME as SERVE_SOCKET_FILE_NAME, serve_command, TailscaleServeSocket
from .tailscale_node import (
    FUNNEL_PORT,
    libtailscale_error,
    load_libtailscale,
    TAILNET_HTTPS_PORT,
    TAILNET_WEB_PORT,
    TailnetConnection,
    TailscaleNode,
)

LOG = logging.getLogger(__name__)

TAILSCALE_SETTINGS_FILE_NAME = "tailscale.json"
# The node's keys and Tailscale's own files: readable by this user only.
TAILSCALE_STATE_DIRECTORY_NAME = "tailscale"
TAILSCALE_HOSTNAME = "nwr-stream-manager"
# How often to look for Tailscale installed on this computer, which NWR Stream Manager leaves
# the tailnet to rather than being on it twice.
SYSTEM_TAILSCALE_CHECK_SECONDS = 10.0
# How long paired devices are given to hear the new address before the old one goes.
ADDRESS_NOTICE_SECONDS = 1.0
# HTTPS: how often to look again while waiting, and how often to ask Tailscale for the
# certificate once it has one, which it renews when it's close to expiring.
HTTPS_CHECK_SECONDS = 5.0
HTTPS_ENABLE_LINK_SECONDS = 60.0
HTTPS_RETRY_SECONDS = 60.0
CERTIFICATE_REFRESH_SECONDS = 24 * 3600.0
SYSTEM_TAILSCALE_MESSAGE = "A systemwide installation of Tailscale was detected on this computer, so NWR Stream Manager uses it instead of connecting to Tailscale by itself."


class TailscaleServiceMixin:
    """Mixed into RtlControlService."""

    def _init_tailscale(self) -> None:
        self.tailscale_settings_path: Path = self.state_path.parent / TAILSCALE_SETTINGS_FILE_NAME
        self.tailscale_state_directory: Path = self.state_path.parent / TAILSCALE_STATE_DIRECTORY_NAME
        self.tailscale_node: TailscaleNode | None = None
        self.web_server = None
        self.tailscale_watch_stop = threading.Event()
        self.tailscale_https: dict[str, str] = {"state": "off", "enable_url": "", "error": ""}
        self.tailscale_https_wake = threading.Event()
        self.tailscale_funnel: dict[str, str] = {"state": "off", "enable_url": "", "error": ""}
        # For Tailscale installed on this computer: Serve passes requests on by this, where tailnet users can be told from Funnel.
        self.tailscale_serve_socket_path: Path = self.state_path.parent / SERVE_SOCKET_FILE_NAME
        self.tailscale_serve_socket: TailscaleServeSocket | None = None

    def _tailscale_settings(self) -> dict[str, Any]:
        try:
            settings = json.loads(self.tailscale_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return settings if isinstance(settings, dict) else {}

    def _tailscale_enabled(self) -> bool:
        return bool(self._tailscale_settings().get("enabled"))

    def _tailscale_https_wanted(self) -> bool:
        return bool(self._tailscale_settings().get("https"))

    def _tailscale_funnel_wanted(self) -> bool:
        return bool(self._tailscale_settings().get("funnel"))

    def _save_tailscale_enabled(self, enabled: bool, *, signed_out_for_system: bool = False) -> None:
        settings = self._tailscale_settings()
        settings.update(enabled=enabled, signed_out_for_system=signed_out_for_system)
        if not enabled:
            settings["https"] = settings["funnel"] = False  # connecting again starts over
        atomic_write_json(self.tailscale_settings_path, settings)

    def start_tailscale(self, web_server: Any) -> None:
        """Connects to the tailnet at startup if it was connected before, serving web_server
        there too, unless Tailscale is installed on this computer."""
        self.web_server = web_server
        self._start_tailscale_serve_socket(web_server)
        if self._tailscale_enabled() and not self._leave_tailnet_to_system_tailscale():
            try:
                self._start_tailscale_node()
            except Exception as exc:
                LOG.warning("couldn't connect to Tailscale: %s", exc)
        threading.Thread(target=self._watch_for_system_tailscale, name="tailscale-system-check", daemon=True).start()

    def _start_tailscale_serve_socket(self, web_server: Any) -> None:
        if not hasattr(web_server, "add_connection"):
            return

        said: dict[str, str] = {"use": "web"}

        def hand_over(fd: int) -> None:
            # Where it's really from is in the headers tailscaled adds; 127.0.0.1 stands in until then.
            # Those are only believed while Serve passes on web requests, which tailscaled sets them in.
            use = tailscale_serve_socket_use_now()
            if use == "tcp":
                os.close(fd)
                if said["use"] != "tcp":
                    LOG.warning(
                        "refused connections from Tailscale Serve: it's set up to pass raw TCP to NWR Stream Manager "
                        "(tailscale serve or funnel with --tcp or --tls-terminated-tcp), which lets anyone, even through "
                        "Funnel, claim to be on your tailnet. Remove that, and use: %s", serve_command(self.tailscale_serve_socket_path),
                    )
            elif use != "web" and said["use"] != "unknown":
                LOG.warning("couldn't find out how Tailscale Serve is set up, so everyone through it is asked for a two-step verification code for now")
            said["use"] = use if use in {"web", "tcp"} else "unknown"
            if use != "tcp":
                web_server.add_connection(fd, "127.0.0.1", serve=use == "web")

        serve_socket = TailscaleServeSocket(self.tailscale_serve_socket_path, hand_over)
        if not serve_socket.start():
            return
        self.tailscale_serve_socket = serve_socket
        set_tailscale_serve_socket(self.tailscale_serve_socket_path)
        if system_tailscale_is_up():
            LOG.info("for a secure (https) address on your tailnet with Tailscale Serve, run: %s", serve_command(self.tailscale_serve_socket_path))

    def _watch_for_system_tailscale(self) -> None:
        while not self.tailscale_watch_stop.wait(SYSTEM_TAILSCALE_CHECK_SECONDS):
            try:
                self._leave_tailnet_to_system_tailscale()
            except Exception as exc:
                LOG.warning("couldn't check for Tailscale on this computer: %s", exc)

    def _leave_tailnet_to_system_tailscale(self) -> bool:
        """With Tailscale installed and connected on this computer, NWR Stream Manager signs
        itself out, so the two aren't on the tailnet at once. Whether it's installed."""
        if not system_tailscale_is_up():
            return False
        with self.lock:
            connected = self.tailscale_node is not None
        if connected or self._tailscale_enabled():
            LOG.info("Tailscale is installed and connected on this computer, so NWR Stream Manager is signing itself out of Tailscale")
            if connected:
                # Paired devices connected over the tailnet learn the computer's own Tailscale
                # address now, while the address they're using still works.
                with self.lock:
                    server = getattr(self, "remote_server", None)
                if server is not None:
                    server.notify_status_changed()
                    self.tailscale_watch_stop.wait(ADDRESS_NOTICE_SECONDS)
            self.sign_out_tailscale(signed_out_for_system=True)
        return True

    def _stop_tailscale(self) -> None:
        self.tailscale_watch_stop.set()
        if self.tailscale_serve_socket is not None:
            self.tailscale_serve_socket.close()
        with self.lock:
            node, self.tailscale_node = self.tailscale_node, None
        set_tailnet_dialer(None)
        if node is not None:
            node.close()
        self.tailscale_https_wake.set()

    def _start_tailscale_node(self) -> None:
        lib = load_libtailscale()
        if lib is None:
            raise ValueError("Tailscale can't be used: its library (libtailscale) isn't installed.")
        with self.lock:
            if self.tailscale_node is not None:
                return
            listeners = {
                TAILNET_WEB_PORT: self._tailnet_connection,
                TAILNET_HTTPS_PORT: self._tailnet_https_connection,
                REMOTE_SDR_TCP_PORT: self._tailnet_remote_sdr_connection,
            }
            node = TailscaleNode(lib, self.tailscale_state_directory, TAILSCALE_HOSTNAME, listeners)
            self.tailscale_node = node
        try:
            node.start()
        except Exception:
            with self.lock:
                self.tailscale_node = None
            raise
        # Icecast servers and paired SDRs on the tailnet are reached through it from now on, and
        # devices paired from now on are told its address there.
        set_tailnet_dialer(node.dial, node.own_address, node.peer_addresses)
        threading.Thread(target=self._tailscale_https_loop, args=(node,), name="tailscale-https", daemon=True).start()
        LOG.info("connecting to Tailscale as %s", TAILSCALE_HOSTNAME)

    def _tailnet_connection(self, fd: int, address: str) -> None:
        server = self.web_server
        if server is None:
            os.close(fd)
            return
        server.add_connection(fd, address, tailnet=True)

    def _tailnet_https_connection(self, fd: int, address: str) -> None:
        server = self.web_server
        if server is None:
            os.close(fd)
            return
        try:
            server.add_connection(fd, address, tls=True)
        except MicrohttpdError as exc:
            LOG.debug("an HTTPS connection from %s on the tailnet was closed: %s", address, exc)

    # -- HTTPS on the tailnet -------------------------------------------------

    def _set_tailscale_https(self, state: str, *, enable_url: str = "", error: str = "") -> None:
        with self.lock:
            previous = self.tailscale_https.get("state")
            self.tailscale_https = {"state": state, "enable_url": enable_url, "error": error}
        if state != previous and state == "needs-enabling":
            LOG.info("Tailscale: HTTPS is turned off for this tailnet; Settings links to where to turn it on")

    def _set_tailscale_funnel(self, state: str, *, enable_url: str = "", error: str = "") -> None:
        with self.lock:
            self.tailscale_funnel = {"state": state, "enable_url": enable_url, "error": error}

    def _local_web_address(self) -> str:
        """Where Tailscale's Funnel proxy reaches the web interface on this computer."""
        host = str(getattr(self, "web_host", "") or "")
        if host in {"", "0.0.0.0", "::", "localhost"}:
            host = "127.0.0.1"
        return f"http://{f'[{host}]' if ':' in host else host}:{getattr(self, 'web_port', 8080)}"

    def _tailscale_funnel_step(self, node: TailscaleNode, secure: bool, funnel: dict[str, Any]) -> None:
        """One look at Funnel, from the HTTPS loop: open while it's wanted and allowed, closed
        otherwise, including whatever was left open from before."""
        wanted = secure and self._tailscale_funnel_wanted()
        try:
            if wanted and not node.can_funnel():
                if funnel["open"]:
                    node.set_funnel(None)
                    funnel["open"] = False
                now = time.monotonic()
                if not funnel["url"] or now - funnel["url_at"] >= HTTPS_ENABLE_LINK_SECONDS:
                    funnel["url"], funnel["url_at"] = node.funnel_enable_url(), now
                self._set_tailscale_funnel("needs-permission", enable_url=funnel["url"])
            elif wanted and not funnel["open"]:
                node.set_funnel(self._local_web_address())
                funnel["open"] = funnel["checked"] = True
                LOG.info("Tailscale Funnel: NWR Stream Manager is reachable from the internet at https://%s:%s/", node.certificate_name(), FUNNEL_PORT)
                self._set_tailscale_funnel("on")
            elif not wanted and (funnel["open"] or not funnel["checked"]):
                node.set_funnel(None)
                if funnel["open"]:
                    LOG.info("Tailscale Funnel: NWR Stream Manager is no longer reachable from the internet")
                funnel["open"], funnel["checked"] = False, True
                self._set_tailscale_funnel("off")
        except Exception as exc:
            LOG.warning("couldn't change Tailscale Funnel: %s", exc)
            self._set_tailscale_funnel("error" if wanted else "off", error=str(exc))

    def _tailscale_https_loop(self, node: TailscaleNode) -> None:
        """While the switch is on: once the tailnet has HTTPS, a certificate, asked for again
        each day so a renewed one is used; until then, where to turn HTTPS on. Funnel follows."""
        certificate: bytes | None = None
        fetched_at = enable_url_at = 0.0
        enable_url = ""
        # Whether Funnel is open, and whether what this device had saved from before was checked.
        funnel: dict[str, Any] = {"open": False, "checked": False, "url": "", "url_at": 0.0}
        while not node.closed.is_set():
            wait = HTTPS_CHECK_SECONDS
            now = time.monotonic()
            if not self._tailscale_https_wanted():
                if certificate is not None and self.web_server is not None:
                    self.web_server.stop_tls()
                certificate = None
                self._set_tailscale_https("off")
            elif node.snapshot()["state"] != "connected":
                self._set_tailscale_https("starting")
            elif not node.certificate_name():
                if not enable_url or now - enable_url_at >= HTTPS_ENABLE_LINK_SECONDS:
                    enable_url, enable_url_at = node.https_enable_url(), now
                self._set_tailscale_https("needs-enabling", enable_url=enable_url)
            elif certificate is None or now - fetched_at >= CERTIFICATE_REFRESH_SECONDS:
                if certificate is None:
                    self._set_tailscale_https("starting")
                try:
                    new_certificate, key = node.certificate()
                    if new_certificate != certificate and self.web_server is not None:
                        self.web_server.start_tls(new_certificate, key)
                        LOG.info("Tailscale: HTTPS is ready at https://%s/", node.certificate_name())
                    certificate, fetched_at = new_certificate, now
                    self._set_tailscale_https("on")
                except Exception as exc:
                    LOG.warning("couldn't get a certificate from Tailscale: %s", exc)
                    self._set_tailscale_https("error", error=str(exc))
                    wait = HTTPS_RETRY_SECONDS
            if node.snapshot()["state"] == "connected":
                self._tailscale_funnel_step(node, certificate is not None and self._tailscale_https_wanted(), funnel)
            self.tailscale_https_wake.wait(wait)
            self.tailscale_https_wake.clear()
        if certificate is not None and self.web_server is not None:
            self.web_server.stop_tls()
        self._set_tailscale_https("off")

    def own_tailnet(self) -> dict[str, Any] | None:
        """Its own connection to a tailnet, while it's connected: its name, IPv4 address, and whether
        it serves HTTPS there itself. For where notifications open it."""
        with self.lock:
            node, https = getattr(self, "tailscale_node", None), dict(getattr(self, "tailscale_https", {}))
        if node is None:
            return None
        snapshot = node.snapshot()
        if snapshot["state"] != "connected":
            return None
        ipv4 = next((address for address in snapshot["addresses"] if ":" not in str(address)), "")
        return {"name": snapshot["name"], "ipv4": ipv4, "secure": https.get("state") == "on"}

    def tailnet_https_name(self) -> str:
        """The tailnet name NWR Stream Manager serves HTTPS at itself, or "" if it doesn't:
        plain HTTP to that name goes there."""
        with self.lock:
            node, https = self.tailscale_node, dict(self.tailscale_https)
        if node is None or https.get("state") != "on":
            return ""
        return node.certificate_name()

    def set_tailscale_https(self, enabled: bool) -> dict[str, Any]:
        settings = self._tailscale_settings()
        settings["https"] = bool(enabled)
        if not enabled:
            settings["funnel"] = False  # Funnel needs the secure address
        atomic_write_json(self.tailscale_settings_path, settings)
        LOG.info("Tailscale: secure (https) address turned %s", "on" if enabled else "off")
        self.tailscale_https_wake.set()
        return self.tailscale_status()

    def set_tailscale_funnel(self, enabled: bool) -> dict[str, Any]:
        settings = self._tailscale_settings()
        if enabled and not settings.get("https"):
            raise ValueError("Turn on the secure (https) address first: Funnel needs it.")
        settings["funnel"] = bool(enabled)
        atomic_write_json(self.tailscale_settings_path, settings)
        LOG.info("Tailscale Funnel turned %s", "on" if enabled else "off")
        self.tailscale_https_wake.set()
        return self.tailscale_status()

    def _tailnet_remote_sdr_connection(self, fd: int, address: str) -> None:
        """A paired NWR Stream Manager on the tailnet, using this one's shared RTL-SDR."""
        with self.lock:
            server = getattr(self, "remote_server", None)
        if server is None:
            os.close(fd)  # not sharing
            return
        server.accept_connection(TailnetConnection(fileno=fd, peer=(address, 0)), (address, 0))

    def tailscale_status(self) -> dict[str, Any]:
        """For Settings: whether Tailscale can be used, and if it's connected, where."""
        if system_tailscale_is_up():
            return {
                "available": False,
                "state": "system",
                "signed_out_for_system": bool(self._tailscale_settings().get("signed_out_for_system")),
                "serve_command": serve_command(self.tailscale_serve_socket_path) if self.tailscale_serve_socket is not None else "",
            }
        if load_libtailscale() is None:
            return {"available": False, "state": "unavailable", "reason": libtailscale_error()}
        with self.lock:
            node = self.tailscale_node
        if node is None:
            return {"available": True, "state": "off"}
        snapshot = node.snapshot()
        with self.lock:
            https = dict(self.tailscale_https)
            funnel = dict(self.tailscale_funnel)
        https["enabled"] = self._tailscale_https_wanted()
        funnel["enabled"] = self._tailscale_funnel_wanted()
        if snapshot["name"]:
            funnel["url"] = f"https://{snapshot['name']}:{FUNNEL_PORT}/"
        if snapshot["name"]:
            secure = https["state"] == "on"
            snapshot["url"] = f"{'https' if secure else 'http'}://{snapshot['name']}/"
        return {"available": True, **snapshot, "https": https, "funnel": funnel}

    def connect_tailscale(self) -> dict[str, Any]:
        if system_tailscale_is_up():
            raise ValueError(SYSTEM_TAILSCALE_MESSAGE)
        self._start_tailscale_node()
        # Connected again at each start from now on, until it's signed out.
        self._save_tailscale_enabled(True)
        return self.tailscale_status()

    def sign_out_tailscale(self, *, signed_out_for_system: bool = False) -> dict[str, Any]:
        """Signs out of the tailnet and forgets it: connecting again starts over."""
        with self.lock:
            node, self.tailscale_node = self.tailscale_node, None
        self._save_tailscale_enabled(False, signed_out_for_system=signed_out_for_system)
        set_tailnet_dialer(None)
        if node is not None:
            node.sign_out()
        self.tailscale_https_wake.set()
        shutil.rmtree(self.tailscale_state_directory, ignore_errors=True)
        LOG.info("signed out of Tailscale")
        return self.tailscale_status()
