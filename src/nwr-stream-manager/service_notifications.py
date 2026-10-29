"""The service's notifications: their settings, and noticing problems with streams, the RTL-SDR and recorded alerts to send."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import asdict
from typing import Any

from .constants import (
    RTL_NOTIFICATION_NO_DATA_SECONDS,
    STREAM_NOTIFICATION_FAILURE_GRACE_SECONDS,
    STREAM_NOTIFICATION_RECOVERY_SECONDS,
    STREAM_NOTIFICATION_REPEAT_SECONDS,
)
from .eas_alerts import (
    eas_alert_id,
    eas_alert_index_path,
    eas_alert_notification_id,
    eas_alert_notification_message,
    eas_recording_settings_from_stream,
    load_eas_alert_entries,
    stream_callsign,
)
from .models import reconcile_notification_access_url_selection, WebNotificationSettings
from .network import notification_access_url_options
from .notifications import (
    eas_alert_detail_route_path,
    notification_app_url,
    notification_click_url,
    rtl_problem_messages,
    save_notification_settings,
    send_ntfy_notification,
    stream_notification_failures,
    stream_notification_healthy_keys,
    stream_notification_settings_from_stream,
    stream_reception_failure,
    stream_reception_healthy,
    validate_notification_settings_payload,
    validate_stream_notification_payload,
)
from .stream_config import friendly_rtl_device_label, save_streams


LOG = logging.getLogger(__name__)


class NotificationServiceMixin:
    """Mixed into RtlControlService."""

    def _stream_notification_loop(self) -> None:
        while not self.stream_notification_stop.wait(10.0):
            try:
                with self.lock:
                    self._process_stream_notifications_locked(self._active_streams_locked())
            except Exception as exc:
                LOG.warning("stream notification monitor failed: %s", exc)

    def update_notification_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = validate_notification_settings_payload(payload, current=self.notification_settings)
            self.notification_settings = settings
            save_notification_settings(self.notification_state_path, settings)
        LOG.info(
            "updated notification settings: enabled=%s server=%s topic=%s priority=%s token=%s username=%s password=%s",
            settings.enabled,
            settings.server_url,
            "set" if settings.topic else "unset",
            settings.priority,
            "set" if settings.access_token else "unset",
            settings.username or "<none>",
            "set" if settings.password else "unset",
        )
        return self.status()

    def _notification_access_url_options(self) -> list[dict[str, str]]:
        own_tailnet = getattr(self, "own_tailnet", None)
        return notification_access_url_options(self.web_host, self.web_port, own_tailnet() if own_tailnet is not None else None)

    def _reconcile_notification_access_url_selection_locked(
        self,
        options: list[dict[str, str]] | None = None,
    ) -> WebNotificationSettings:
        if options is None:
            options = self._notification_access_url_options()
        settings = reconcile_notification_access_url_selection(self.notification_settings, options)
        unavailable = self.notification_settings.access_url_mode if settings is not self.notification_settings else ""
        if unavailable and unavailable != getattr(self, "_notification_access_url_unavailable", ""):
            # Only for now, and not saved: such as a tailnet that's still connecting after a restart.
            LOG.info("notification access URL selection %s is unavailable; using automatic until it's back", unavailable)
        self._notification_access_url_unavailable = unavailable
        return settings

    def test_notification_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = validate_notification_settings_payload(payload, current=self.notification_settings)
            settings = reconcile_notification_access_url_selection(settings, self._notification_access_url_options())
        send_ntfy_notification(
            settings,
            title=self.instance_name,
            message="This is a test notification from NWR Stream Manager.",
            priority=settings.priority,
            click_url=notification_click_url(self.notification_effective_access_url_for_settings(settings), "/"),
        )
        LOG.info("sent test notification through %s", settings.server_url)
        return {"success": True, "message": "Test notification sent."}

    def notification_access_url_options(self) -> dict[str, Any]:
        options = self._notification_access_url_options()
        with self.lock:
            settings = self._reconcile_notification_access_url_selection_locked(options)
        return {
            "options": options,
            "selected": settings.access_url_mode,
            "custom_access_url": settings.custom_access_url,
        }

    def notification_settings_snapshot(self) -> WebNotificationSettings:
        with self.lock:
            return self._reconcile_notification_access_url_selection_locked()

    def notification_effective_access_url(self) -> str:
        with self.lock:
            return self.notification_effective_access_url_for_settings(self._reconcile_notification_access_url_selection_locked())

    def notification_effective_access_url_for_settings(self, settings: WebNotificationSettings) -> str:
        if settings.access_url_mode == "custom":
            return settings.custom_access_url
        options = self._notification_access_url_options()
        settings = reconcile_notification_access_url_selection(settings, options)
        if settings.access_url_mode and settings.access_url_mode != "auto":
            for option in options:
                if option.get("id") == settings.access_url_mode:
                    return str(option.get("url", ""))
        return str(options[0].get("url", "")) if options else ""

    def update_stream_notifications(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        settings = validate_stream_notification_payload(payload.get("notifications", payload))
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["notifications"] = asdict(settings)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
        LOG.info(
            "updated stream notification settings for %s: icecast_failures=%s soundcard_failures=%s recorded_eas_alerts=%s bad_reception=%s",
            stream_id,
            settings.icecast_failures,
            settings.soundcard_failures,
            settings.recorded_eas_alerts,
            settings.bad_reception,
        )
        return self.stream_status()

    def _notification_globally_ready_locked(self) -> bool:
        self._reconcile_notification_access_url_selection_locked()
        return bool(self.notification_settings.enabled and self.notification_settings.topic)

    def _process_stream_notifications_locked(self, active_streams: list[dict[str, Any]]) -> None:
        now = time.monotonic()
        configured_by_id = {str(stream.get("id", "")): stream for stream in self.streams}
        active_failure_keys: set[str] = set()
        if not self._notification_globally_ready_locked():
            self.stream_notification_states.clear()
            self.rtl_notification_states.clear()
            self.stream_eas_alert_notification_seen.clear()
            return
        self._process_rtl_notifications_locked(now)
        failures: list[dict[str, Any]] = []
        healthy_keys: set[str] = set()
        for snapshot in active_streams:
            stream_id = str(snapshot.get("id", ""))
            stream = configured_by_id.get(stream_id)
            if not stream or stream.get("enabled", True) is False:
                continue
            settings = stream_notification_settings_from_stream(stream)
            failures.extend(stream_notification_failures(stream, snapshot, settings))
            healthy_keys |= stream_notification_healthy_keys(stream, snapshot, settings)
        for stream_id, signal in self._stream_signals_locked().items():
            stream = configured_by_id.get(stream_id)
            if not stream or stream.get("enabled", True) is False:
                continue
            settings = stream_notification_settings_from_stream(stream)
            failure = stream_reception_failure(stream, signal, settings)
            if failure is not None:
                failures.append(failure)
            elif stream_reception_healthy(stream, signal, settings):
                healthy_keys.add(f"{stream_id}:reception")
        for failure in failures:
            key = str(failure["key"])
            active_failure_keys.add(key)
            state = self.stream_notification_states.setdefault(
                key,
                {
                    "first_seen": now,
                    "last_seen": now,
                    "last_clear": 0.0,
                    "last_sent": 0.0,
                },
            )
            state["last_seen"] = now
            state["failure"] = failure
            if now - float(state.get("first_seen", now)) < STREAM_NOTIFICATION_FAILURE_GRACE_SECONDS:
                continue
            last_sent = float(state.get("last_sent", 0.0))
            if last_sent and now - last_sent < STREAM_NOTIFICATION_REPEAT_SECONDS:
                continue
            self._send_stream_problem_notification_async(failure)
            state["last_sent"] = now
        for key, state in list(self.stream_notification_states.items()):
            if key in active_failure_keys:
                continue
            last_seen = float(state.get("last_seen", 0.0))
            if now - last_seen >= STREAM_NOTIFICATION_RECOVERY_SECONDS:
                self.stream_notification_states.pop(key, None)
                # Only announce a recovery the user was told about the failure
                # for, and only when the output or signal is seen working.
                if state.get("last_sent") and key in healthy_keys:
                    self._send_stream_recovery_notification_async(state.get("failure") or {})
        self._process_recorded_eas_alert_notifications_locked()

    def _process_rtl_notifications_locked(self, now: float) -> None:
        failure = self._rtl_notification_failure_locked(now)
        if failure is None:
            # Not applicable cases (notifications off, I/Q file source, no SDR
            # selected) already cleared the states, so reaching here with a
            # state means the SDR is working again. A problem nobody was told
            # about is forgotten at once, so a later one gets its own grace
            # period instead of inheriting this one's.
            for key, state in list(self.rtl_notification_states.items()):
                if not state.get("notified_failure"):
                    self.rtl_notification_states.pop(key, None)
                    continue
                last_seen = float(state.get("last_seen", 0.0))
                if now - last_seen >= STREAM_NOTIFICATION_RECOVERY_SECONDS:
                    self.rtl_notification_states.pop(key, None)
                    notified = state.get("notified_failure")
                    if notified:
                        self._send_stream_recovery_notification_async(notified)
            return
        key = str(failure["key"])
        notified_failure = None
        for stale_key in list(self.rtl_notification_states):
            if stale_key != key:
                stale = self.rtl_notification_states.pop(stale_key, None) or {}
                notified_failure = stale.get("notified_failure") or notified_failure
        state = self.rtl_notification_states.setdefault(
            key,
            {
                "first_seen": now,
                "last_seen": now,
                "last_clear": 0.0,
                "last_sent": 0.0,
                # The failure the user was last told about, carried across a
                # change of problem (not connected, then no data) so the
                # eventual recovery notice matches what they were sent.
                "notified_failure": notified_failure,
            },
        )
        state["last_seen"] = now
        if now - float(state.get("first_seen", now)) < STREAM_NOTIFICATION_FAILURE_GRACE_SECONDS:
            return
        last_sent = float(state.get("last_sent", 0.0))
        if last_sent and now - last_sent < STREAM_NOTIFICATION_REPEAT_SECONDS:
            return
        self._send_stream_problem_notification_async(failure)
        state["last_sent"] = now
        state["notified_failure"] = failure

    def _rtl_notification_failure_locked(self, now: float) -> dict[str, Any] | None:
        if not self.settings.notify_sdr_failures:
            self.rtl_notification_states.clear()
            return None
        if self.settings.uses_remote_sdr:
            return self._remote_rtl_notification_failure_locked()
        if self.iq_file_source_config is not None:
            self.rtl_notification_states.clear()
            return None
        serial = str(self.settings.serial or "").strip()
        if not serial:
            self.rtl_notification_states.clear()
            return None
        problem = self._local_rtl_problem_locked(now)
        if problem is None:
            return None
        message, recovery_message = rtl_problem_messages(problem["category"], problem["device_name"])
        return {
            "key": f"rtl:{serial}:{problem['category']}",
            "category": problem["category"],
            "target_path": "/?view=rtl",
            "message": message,
            "recovery_message": recovery_message,
        }

    def _local_rtl_problem_locked(self, now: float) -> dict[str, str] | None:
        """What is wrong with this machine's RTL-SDR, if anything."""
        serial = str(self.settings.serial or "").strip()
        if not serial:
            return None
        devices_payload = self.devices()
        rtl_devices = list(devices_payload.get("devices", []))
        device_name = self._rtl_notification_device_name_locked(serial, rtl_devices)
        serial_connected = any(str(device.get("serial", "")).strip() == serial for device in rtl_devices)
        if not serial_connected:
            return {"category": "disconnected", "device_name": device_name}
        error = str(self.capture_error or "").strip()
        if error:
            lower = error.lower()
            if "permission" in lower or "access denied" in lower or "udev" in lower:
                category = "permission"
            elif "claim" in lower or "busy" in lower or "resource busy" in lower:
                category = "claim"
            elif "not found" in lower or "disconnect" in lower or "no such device" in lower:
                category = "disconnected"
            else:
                category = "error"
            return {"category": category, "device_name": device_name}
        if self.capture is None:
            return {"category": "disconnected", "device_name": device_name}
        last_batch_at = self.last_batch_at
        if last_batch_at is None or now - float(last_batch_at) >= RTL_NOTIFICATION_NO_DATA_SECONDS:
            return {"category": "no-data", "device_name": device_name}
        return None

    def _remote_rtl_notification_failure_locked(self) -> dict[str, Any] | None:
        host_id = self.settings.remote_host_id
        client = self.remote_client
        status = client.status() if client is not None else {}
        record = self.remote_devices.get(host_id) or {}
        host_name = status.get("host_name") or record.get("name") or "the remote SDR host"
        if client is None or status.get("connecting"):
            return None  # just selected: not unreachable until a connection attempt fails
        if not status.get("reachable"):
            category = "unreachable"
            device_name = f"the remote SDR on {host_name}"
        else:
            sdr = status.get("sdr") if isinstance(status.get("sdr"), dict) else {}
            category = str(sdr.get("problem") or "")
            if not category:
                return None
            device_name = f"{sdr.get('device_name') or 'the RTL-SDR'} on {host_name}"
        message, recovery_message = rtl_problem_messages(category, device_name)
        return {
            "key": f"remote:{host_id}:{category}",
            "category": category,
            "target_path": "/?view=rtl",
            "message": message,
            "recovery_message": recovery_message,
        }

    def _rtl_notification_device_name_locked(
        self,
        serial: str,
        devices: list[dict[str, Any]] | None = None,
    ) -> str:
        if devices is None:
            devices = list(self.devices().get("devices", []))
        for device in devices:
            if str(device.get("serial", "")).strip() != serial:
                continue
            label = friendly_rtl_device_label(device)
            if label:
                getattr(self, "rtl_device_name_cache", {})[serial] = label
                return label
        return getattr(self, "rtl_device_name_cache", {}).get(serial, "") or "the configured RTL-SDR"

    def _process_recorded_eas_alert_notifications_locked(self) -> None:
        active_stream_ids = {str(stream.get("id", "")) for stream in self.streams}
        for stream_id in list(self.stream_eas_alert_notification_seen):
            if stream_id not in active_stream_ids:
                self.stream_eas_alert_notification_seen.pop(stream_id, None)
        for stream in self.streams:
            stream_id = str(stream.get("id", ""))
            if not stream_id:
                continue
            settings = stream_notification_settings_from_stream(stream)
            eas_settings = eas_recording_settings_from_stream(stream)
            index_path = eas_alert_index_path(self.streams_directory, stream)
            try:
                alerts = load_eas_alert_entries(index_path)
            except Exception as exc:
                callsign = stream_callsign(stream)
                LOG.warning("could not read EAS alert index for notifications for %s: %s", callsign, exc)
                continue
            current_ids = {eas_alert_notification_id(alert) for alert in alerts}
            seen = self.stream_eas_alert_notification_seen.get(stream_id)
            if seen is None:
                self.stream_eas_alert_notification_seen[stream_id] = current_ids
                continue
            if not settings.recorded_eas_alerts or not eas_settings.enabled:
                self.stream_eas_alert_notification_seen[stream_id] = current_ids
                continue
            new_alerts = [
                (index, alert) for index, alert in enumerate(alerts)
                if eas_alert_notification_id(alert) not in seen
            ]
            self.stream_eas_alert_notification_seen[stream_id] = current_ids
            for index, alert in new_alerts:
                message = eas_alert_notification_message(stream, alert)
                alert_id = eas_alert_id(alert, index)
                self._send_stream_problem_notification_async(
                    {
                        "key": f"{stream_id}:eas:{eas_alert_notification_id(alert)}",
                        "message": message,
                        "priority": 3,
                        "target_path": eas_alert_detail_route_path(stream_id, alert_id),
                        "force_auth": False,
                    }
                )

    def _send_stream_recovery_notification_async(self, failure: dict[str, Any]) -> None:
        message = str(failure.get("recovery_message", "")).strip()
        if not message:
            return
        self._send_stream_problem_notification_async(
            {
                "target_path": failure.get("target_path", "/"),
                "force_auth": failure.get("force_auth", True),
                "message": message,
                "priority": 3,
            }
        )

    def _send_stream_problem_notification_async(self, failure: dict[str, Any]) -> None:
        settings = self.notification_settings
        base_url = self.notification_effective_access_url_for_settings(settings)
        target_path = str(failure.get("target_path", "/") or "/")
        force_auth = bool(failure.get("force_auth", True))
        click_url = (
            notification_click_url(base_url, target_path)
            if force_auth
            else notification_app_url(base_url, target_path)
        )
        message = str(failure.get("message", "")).strip()
        priority = int(failure.get("priority", 4) or 4)
        if not message:
            return
        # Titled with the instance name, so it's clear which NWR Stream Manager sent it.
        title = self.instance_name

        def send() -> None:
            try:
                send_ntfy_notification(
                    settings,
                    title=title,
                    message=message,
                    priority=priority,
                    click_url=click_url,
                )
                LOG.info("sent stream notification: %s", message)
            except Exception as exc:
                LOG.warning("failed to send stream notification: %s", exc)

        threading.Thread(target=send, name="stream-notification-send", daemon=True).start()
