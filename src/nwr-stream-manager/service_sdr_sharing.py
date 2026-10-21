"""The service's SDR sharing: sharing this machine's SDR, and pairing with and using another machine's."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .constants import REMOTE_SDR_HOST_SETTINGS
from .remote_sdr import (
    discover_hosts,
    is_tailscale_address,
    pair_with_host,
    REMOTE_SDR_TCP_PORT,
    RemoteSdrClient,
    RemoteSdrError,
    RemoteSdrServer,
)
from .rtl import NWR_CENTER_FREQUENCY_HZ, RtlCaptureSource


LOG = logging.getLogger(__name__)


class SdrSharingServiceMixin:
    """Mixed into RtlControlService."""

    def remote_host_status(self) -> dict[str, Any]:
        """This machine's SDR as reported to paired remote clients."""
        now = time.monotonic()
        with self.lock:
            settings = self._effective_settings_locked()
            capture = self.capture
            gain_values = capture.get_gain_values() if isinstance(capture, RtlCaptureSource) else []
            problem = self._local_rtl_problem_locked(now)
            serial = str(settings.serial or "")
            device_name = self._rtl_notification_device_name_locked(serial) if serial else ""
            if device_name == "the configured RTL-SDR":
                device_name = f"RTL-SDR serial {serial}"
            last_batch_at = self.last_batch_at
            capture_error = self.capture_error or ""
            sdr_reset = self._sdr_reset_status_locked()
            # Taken under the lock that settings changes hold.
            self.remote_host_status_seq += 1
            status_seq = self.remote_host_status_seq
        return {
            "status_seq": status_seq,
            "reset": sdr_reset,
            "device_name": device_name,
            "serial": serial,
            "connected": problem is None or problem["category"] != "disconnected",
            "capture_active": capture is not None,
            "capture_error": capture_error,
            "last_batch_age_seconds": round(now - float(last_batch_at), 3) if last_batch_at is not None else None,
            "sample_rate": settings.sample_rate,
            "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
            "gain_values": gain_values,
            "settings": {key: getattr(settings, key) for key in REMOTE_SDR_HOST_SETTINGS},
            "problem": problem["category"] if problem is not None else None,
        }

    def _on_remote_host_status(self, status: dict[str, Any]) -> None:
        # Someone changed the remote host's SDR, there or on another machine using it.
        host_settings = status.get("settings") if isinstance(status.get("settings"), dict) else {}
        current = {key: host_settings.get(key) for key in REMOTE_SDR_HOST_SETTINGS}
        current["gain_values"] = list(status.get("gain_values") or [])
        current["reset"] = status.get("reset")
        with self.settings_change_condition:
            changed = current != self.last_remote_host_settings
            self.last_remote_host_settings = current
        if changed:
            self.announce_settings_changed()

    def _remote_sdr_status_locked(self) -> dict[str, Any]:
        settings = self.settings
        server = self.remote_server
        client = self.remote_client
        connected = {item["id"]: item for item in (server.connected_clients() if server is not None else [])}
        in_use = settings.remote_host_id if settings.uses_remote_sdr else ""
        devices = []
        client_status = client.status() if client is not None else {}
        for device in self.remote_devices.public_list():
            device["in_use"] = device["id"] == in_use
            device["using_this_sdr"] = device["id"] in connected
            # The network actually carrying the connection, if there is one.
            live_address = ""
            if device["in_use"]:
                live_address = client_status.get("connected_address", "")
            elif device["using_this_sdr"]:
                live_address = connected[device["id"]].get("address", "")
            device["via"] = ("tailscale" if is_tailscale_address(live_address) else "lan") if live_address else ""
            devices.append(device)
        return {
            "identity": {"id": self.remote_identity.fingerprint, "name": self.remote_identity.name},
            "sharing": {
                "enabled": settings.remote_access_enabled,
                "available": bool(settings.serial) and not settings.uses_remote_sdr,
                "running": server is not None,
                "port": server.port if server is not None else None,
                "error": self.remote_server_error,
                "pairing": server.pairing_status() if server is not None else {"active": False},
            },
            "remote": {
                "source_mode": settings.source_mode,
                "device_id": in_use,
                "status": client_status or None,
            },
            "devices": devices,
        }

    def remote_sdr_status(self) -> dict[str, Any]:
        with self.lock:
            return self._remote_sdr_status_locked()

    def discover_remote_sdrs(self) -> dict[str, Any]:
        hosts = discover_hosts(port=self.remote_sdr_discovery_port, exclude_id=self.remote_identity.fingerprint)
        paired = {record["id"] for record in self.remote_devices.public_list()}
        for host in hosts:
            host["paired"] = host["id"] in paired
        return {"hosts": hosts}

    def start_remote_sdr_pairing(self, *, held: bool = False) -> dict[str, Any]:
        with self.lock:
            server = self.remote_server
        if server is None:
            raise ValueError("Turn on sharing before pairing another NWR Stream Manager.")
        server.begin_pairing(held=held)
        return self.remote_sdr_status()

    def keep_remote_sdr_pairing_open(self) -> dict[str, Any]:
        with self.lock:
            server = self.remote_server
        if server is not None:
            server.keep_pairing_open()
        return self.remote_sdr_status()

    def cancel_remote_sdr_pairing(self) -> dict[str, Any]:
        with self.lock:
            server = self.remote_server
        if server is not None:
            server.cancel_pairing()
        return self.remote_sdr_status()

    def pair_remote_sdr(self, payload: dict[str, Any]) -> dict[str, Any]:
        address = str(payload.get("address", "")).strip()
        if not address:
            raise ValueError("Select an NWR Stream Manager to pair with.")
        try:
            port = int(payload.get("port") or REMOTE_SDR_TCP_PORT)
        except (TypeError, ValueError) as exc:
            raise ValueError("The port is invalid.") from exc
        try:
            record = pair_with_host(
                address,
                port,
                str(payload.get("code", "")),
                client_id=self.remote_identity.fingerprint,
                client_name=self.remote_identity.name,
                client_port=self.remote_sdr_port or REMOTE_SDR_TCP_PORT,
                expected_host_id=str(payload.get("host_id", "")).strip(),
            )
        except (RemoteSdrError, OSError) as exc:
            raise ValueError(str(exc) or "Could not reach that NWR Stream Manager.") from exc
        if record["id"] == self.remote_identity.fingerprint:
            raise ValueError("This is the same NWR Stream Manager.")
        discovered = payload.get("addresses") if isinstance(payload.get("addresses"), list) else []
        self.remote_devices.add(
            record["id"],
            record["name"],
            token=record["token"],
            peer_token=record["peer_token"],
            address=record["address"],
            port=record["port"],
            addresses=[str(item) for item in discovered],
        )
        LOG.info("paired with %s (%s)", record.get("name", ""), address)
        return self.remote_sdr_status()

    def unpair_remote_sdr_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        device_id = str(payload.get("device_id", "")).strip()
        with self.lock:
            if self.settings.uses_remote_sdr and self.settings.remote_host_id == device_id:
                raise ValueError("Switch to another SDR before unpairing the one in use.")
            server = self.remote_server
        if server is not None:
            server.unpair(device_id)  # also disconnects it from this SDR
        else:
            self.remote_devices.remove(device_id)
        return self.remote_sdr_status()

    def _sync_remote_sdr_locked(self) -> None:
        """Start or stop the remote SDR host server and client to match settings."""
        settings = self.settings
        stopping: list[Any] = []
        want_server = settings.remote_access_enabled and not settings.uses_remote_sdr and bool(settings.serial)
        if want_server and self.remote_server is None:
            server = RemoteSdrServer(
                self.remote_identity,
                self.remote_devices,
                self.remote_backend,
                port=self.remote_sdr_port,
                discovery_port=self.remote_sdr_discovery_port,
            )
            try:
                server.start()
                self.remote_server = server
                self.remote_server_error = ""
            except OSError as exc:
                self.remote_server_error = f"Remote access could not start: {exc}"
                LOG.warning("%s", self.remote_server_error)
        elif not want_server and self.remote_server is not None:
            stopping.append(self.remote_server)
            self.remote_server = None
        if not want_server:
            self.remote_server_error = ""
        want_host = settings.remote_host_id if settings.uses_remote_sdr else ""
        if self.remote_client is not None and self.remote_client.host_id != want_host:
            stopping.append(self.remote_client)
            self.remote_client = None
        if self.remote_client is not None and self.remote_client.buffer_seconds != settings.remote_buffer_seconds:
            self.remote_client.set_buffer_seconds(settings.remote_buffer_seconds)
        if want_host and self.remote_client is None:
            self.remote_client = RemoteSdrClient(
                want_host,
                self.remote_devices,
                client_id=self.remote_identity.fingerprint,
                discovery_port=self.remote_sdr_discovery_port,
                buffer_seconds=settings.remote_buffer_seconds,
                on_status=self._on_remote_host_status,
            )
            self.remote_client.start()
        # Stopping joins threads that may be waiting on this lock, so do it
        # from another thread.
        if stopping:
            threading.Thread(
                target=lambda: [item.stop() for item in stopping],
                name="remote-sdr-stop",
                daemon=True,
            ).start()

    def _remote_host_settings_locked(self) -> dict[str, Any]:
        client = self.remote_client
        sdr = client.status().get("sdr", {}) if client is not None else {}
        settings = sdr.get("settings") if isinstance(sdr, dict) else None
        return settings if isinstance(settings, dict) else {}
