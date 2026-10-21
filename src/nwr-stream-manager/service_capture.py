"""The service's radio capture: RTL-SDR settings, and starting, changing and stopping capture from the RTL-SDR or an I/Q file."""

from __future__ import annotations

import logging
import queue
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from .constants import IQ_TEST_SOURCE_MIN_SAMPLE_RATE, REMOTE_SDR_HOST_SETTINGS, SDR_SOURCE_REMOTE
from .iq_pipeline import (
    CleanIqFanout,
    IntermediateIqFanout,
    iq_batch_byte_count,
    IqFileCaptureSource,
    QUEUE_WAKEUP,
    RawRtlFanout,
    SdrChannelFeed,
    stop_workers_together,
    subscribe_raw_fanout,
    validate_alias_filter_strength,
    wake_queue,
)
from .models import IqFileSourceConfig, RtlControlSettings
from .remote_sdr import RemoteSdrError
from .rtl import NWR_CENTER_FREQUENCY_HZ, RtlCaptureSource, RtlConfig, validate_ppm_correction
from .state_files import save_settings, validate_remote_buffer_seconds, validate_sdr_source_mode


LOG = logging.getLogger(__name__)


class CaptureServiceMixin:
    """Mixed into RtlControlService."""

    def announce_settings_changed(self) -> None:
        """Tell this machine's browsers, and remote clients of its SDR, that SDR settings changed."""
        with self.settings_change_condition:
            self.settings_change_count += 1
            self.settings_change_condition.notify_all()
        server = self.remote_server
        if server is not None:
            server.notify_status_changed()

    def wait_for_settings_change(self, seen: int, timeout: float) -> int:
        """Wait until SDR settings change after `seen`, then return the new count."""
        with self.settings_change_condition:
            self.settings_change_condition.wait_for(
                lambda: self.settings_change_count != seen or self.shutting_down.is_set(), timeout
            )
            return self.settings_change_count

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._update_settings(payload)
        finally:
            self.announce_settings_changed()

    def _update_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        payload = dict(payload)
        host_changes: dict[str, Any] = {}
        with self.lock:
            previous = self.settings
            if previous.uses_remote_sdr or payload.get("source_mode") == SDR_SOURCE_REMOTE:
                next_mode = validate_sdr_source_mode(payload.get("source_mode", previous.source_mode))
                if next_mode == SDR_SOURCE_REMOTE:
                    # These belong to the remote host's SDR, not this machine's.
                    # Forward only real changes, so saving something local
                    # (a notification toggle) never needs the host to answer.
                    # When first switching to a remote SDR the form still holds
                    # this machine's values, which must not overwrite the host's.
                    requested = {key: payload.pop(key) for key in REMOTE_SDR_HOST_SETTINGS if key in payload}
                    same_host = previous.uses_remote_sdr and payload.get("remote_host_id", previous.remote_host_id) == previous.remote_host_id
                    current_host = self._remote_host_settings_locked() if same_host else None
                    host_changes = {
                        key: value for key, value in requested.items()
                        if current_host is not None and (key not in current_host or current_host[key] != value)
                    }
            settings = self._merged_settings(payload)
            self.settings = settings
            save_settings(self.state_path, settings)
            LOG.info(
                "saved RTL settings: serial=%s sample_rate=%s gain=%s ppm=%s bias_tee=%s alias_filter_strength=%s notify_sdr_failures=%s source=%s remote_access=%s",
                settings.serial or "<none>",
                settings.sample_rate,
                "auto" if settings.gain is None else f"{settings.gain:g} dB",
                settings.ppm_correction,
                settings.bias_tee,
                settings.alias_filter_strength,
                settings.notify_sdr_failures,
                settings.remote_host_id if settings.uses_remote_sdr else "local",
                settings.remote_access_enabled,
            )
            source_changed = (previous.uses_remote_sdr, previous.remote_host_id) != (
                settings.uses_remote_sdr,
                settings.remote_host_id if settings.uses_remote_sdr else "",
            ) and (previous.uses_remote_sdr or settings.uses_remote_sdr)
            if source_changed:
                # A recording belongs to one SDR. The receiver and streams
                # read through channel feeds, which follow the new SDR.
                self._stop_iq_recorder_locked()
                # Problems with the old SDR no longer apply, and the new one
                # gets a full grace period while it connects.
                self.rtl_notification_states.clear()
            if settings.uses_remote_sdr:
                if self.capture is not None:
                    self._detach_capture_locked()
                self.iq_file_source_config = None
                self._sync_remote_sdr_locked()
                self._sync_stream_workers_locked()
            elif self.iq_file_source_config is not None:
                if self.intermediate_fanout is not None:
                    self.intermediate_fanout.set_alias_filter_strength(settings.alias_filter_strength)
                self._sync_remote_sdr_locked()
            elif settings.serial:
                self._start_or_update_capture_locked()
                self._sync_remote_sdr_locked()
            else:
                self._detach_capture_locked()
                self._sync_remote_sdr_locked()
                self._sync_stream_workers_locked()  # no SDR at all: streams stop
            client = self.remote_client
        if host_changes:
            if client is None:
                raise ValueError("The remote SDR is not connected.")
            try:
                client.update_settings(host_changes)
            except RemoteSdrError as exc:
                raise ValueError(str(exc)) from exc
            with self.lock:
                self._sync_stream_workers_locked()
        return self.status()

    def _merged_settings(self, payload: dict[str, Any]) -> RtlControlSettings:
        settings = self.settings
        changes: dict[str, Any] = {}
        if "serial" in payload:
            changes["serial"] = str(payload["serial"]).strip()
        if "sample_rate" in payload:
            raise ValueError("RTL-SDR sample rate is fixed for NWR Stream Manager")
        if "ppm_correction" in payload:
            changes["ppm_correction"] = validate_ppm_correction(int(payload["ppm_correction"]))
        if "bias_tee" in payload:
            changes["bias_tee"] = bool(payload["bias_tee"])
        if "gain" in payload:
            gain = payload["gain"]
            changes["gain"] = None if gain is None or gain == "" else float(gain)
        if "alias_filter_strength" in payload:
            changes["alias_filter_strength"] = validate_alias_filter_strength(payload["alias_filter_strength"])
        if "notify_sdr_failures" in payload:
            changes["notify_sdr_failures"] = bool(payload["notify_sdr_failures"])
        if "source_mode" in payload:
            changes["source_mode"] = validate_sdr_source_mode(payload["source_mode"])
        if "remote_host_id" in payload:
            changes["remote_host_id"] = str(payload["remote_host_id"] or "").strip()
        if "remote_access_enabled" in payload:
            changes["remote_access_enabled"] = bool(payload["remote_access_enabled"])
        if "remote_buffer_seconds" in payload:
            changes["remote_buffer_seconds"] = validate_remote_buffer_seconds(payload["remote_buffer_seconds"])
        merged = replace(settings, **changes)
        if merged.source_mode == SDR_SOURCE_REMOTE and "remote_access_enabled" not in payload:
            merged = replace(merged, remote_access_enabled=False)  # nothing local to share
        if merged.source_mode == SDR_SOURCE_REMOTE:
            if not merged.remote_host_id or self.remote_devices.get(merged.remote_host_id) is None:
                raise ValueError("Pair with a remote SDR before selecting it.")
            if merged.remote_access_enabled:
                raise ValueError("Remote access can only be turned on for an RTL-SDR connected to this machine.")
        elif merged.remote_access_enabled and not merged.serial:
            raise ValueError("Select an RTL-SDR connected to this machine before turning on remote access.")
        return merged

    def _start_or_update_capture_locked(self) -> None:
        config = self.settings.to_rtl_config()
        if self.iq_file_source_config is not None or (
            self.capture is not None and not isinstance(self.capture, RtlCaptureSource)
        ):
            old_capture = self.capture
            source = self._new_rtl_capture_source(config)
            source.start()
            self.capture = source
            self.iq_file_source_config = None
            self.capture_error = None
            self._ensure_capture_pipeline_locked()
            self._sync_stream_workers_locked()
            if old_capture is not None:
                self._stop_capture_async(old_capture, None)
            LOG.info("switched capture source back to RTL-SDR serial %s", config.serial)
            return
        if self.capture is None:
            self.capture_error = None
            self.capture = self._new_rtl_capture_source(config)
            self.capture.start()
            self._ensure_capture_pipeline_locked()
            self._sync_stream_workers_locked()
            LOG.info("started RTL-SDR control capture for serial %s", config.serial)
            return
        if config != self.capture.config:
            self.capture.apply_config(config)
        if self.intermediate_fanout is not None:
            self.intermediate_fanout.set_alias_filter_strength(self.settings.alias_filter_strength)
        self.settings = self._effective_settings_locked()
        save_settings(self.state_path, self.settings)
        self._sync_stream_workers_locked()

    def _current_iq_source(self):
        """What channel feeds read from now: this machine's intermediate
        fanout, the remote SDR client, or None. Read without the lock, so feed
        threads never wait on a settings change in progress."""
        if self.settings.uses_remote_sdr:
            return self.remote_client
        return self.intermediate_fanout

    def _channel_feed(self, frequency_hz: int, name: str) -> SdrChannelFeed:
        return SdrChannelFeed(self._current_iq_source, frequency_hz, name)

    def _alias_filter_strength(self) -> int:
        with self.lock:
            if self.settings.uses_remote_sdr:
                return int(self._remote_host_settings_locked().get("alias_filter_strength", self.settings.alias_filter_strength))
            return self.settings.alias_filter_strength

    def _gain_signature(self) -> float | None:
        # None means hardware/tuner AGC is enabled. A manual gain change (or
        # switching in/out of AGC) resets each stream's signal meter, since
        # gain applies to signal and noise together and mixing pre- and
        # post-change samples in the meter's smoothing windows would bias the
        # SNR reading for several seconds. Continuous drift from the tuner's
        # own AGC while enabled is not tracked here; see the signal_meter
        # module docstring for why live gain readback is not used.
        with self.lock:
            if self.settings.uses_remote_sdr:
                return self._remote_host_settings_locked().get("gain")
            return self.settings.gain

    def _effective_settings_locked(self) -> RtlControlSettings:
        if not isinstance(self.capture, RtlCaptureSource):
            return self.settings
        config = self.capture.config
        return replace(
            self.settings,
            serial=config.serial,
            sample_rate=config.sample_rate,
            gain=config.gain,
            ppm_correction=config.ppm_correction,
            bias_tee=config.bias_tee,
        )

    def iq_test_sources(self) -> dict[str, Any]:
        self._ensure_development_iq_sources_enabled()
        directory = self.iq_test_sources_directory
        directory.mkdir(parents=True, exist_ok=True)
        files: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "name": path.name,
                    "size_bytes": stat.st_size,
                }
            )
        with self.lock:
            active = self.iq_file_source_config
            return {
                "directory": str(directory),
                "files": files,
                "active": (
                    {
                        "name": active.path.name,
                        "sample_rate": active.sample_rate,
                        "center_frequency_hz": active.center_frequency_hz,
                        "stats": self.capture.stats() if isinstance(self.capture, IqFileCaptureSource) else {},
                    }
                    if active is not None
                    else None
                ),
            }

    def start_iq_test_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_development_iq_sources_enabled()
        file_name = str(payload.get("file_name", "")).strip()
        sample_rate = int(payload.get("sample_rate", 0))
        if not file_name:
            raise ValueError("select an I/Q source file")
        if sample_rate < IQ_TEST_SOURCE_MIN_SAMPLE_RATE:
            raise ValueError(f"I/Q source sample rate must be at least {IQ_TEST_SOURCE_MIN_SAMPLE_RATE} S/s")
        path = self._iq_test_source_path(file_name)
        config = IqFileSourceConfig(
            path=path,
            sample_rate=sample_rate,
            center_frequency_hz=NWR_CENTER_FREQUENCY_HZ,
        )
        with self.lock:
            self.capture_error = None
            source = IqFileCaptureSource(config)
            source.start()
            old_capture = self.capture
            self.capture = source
            self.iq_file_source_config = config
            self._ensure_capture_pipeline_locked()
            self._sync_stream_workers_locked()
            if old_capture is not None:
                self._stop_capture_async(old_capture, None)
        LOG.info("started CF32 I/Q test source %s at %s S/s", path.name, sample_rate)
        return self.status()

    def seek_iq_test_source(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._ensure_development_iq_sources_enabled()
        seconds = float(payload.get("seconds", 0.0))
        if seconds == 0.0:
            return self.status()
        with self.lock:
            if self.iq_file_source_config is None or not isinstance(self.capture, IqFileCaptureSource):
                raise ValueError("I/Q file source is not active")
            stats = self.capture.seek_relative(seconds)
            name = self.iq_file_source_config.path.name
        LOG.info("seeked CF32 I/Q test source %s by %.3f seconds", name, seconds)
        response = self.status()
        response["iq_test_source_seek"] = stats
        return response

    def stop_iq_test_source(self) -> dict[str, Any]:
        self._ensure_development_iq_sources_enabled()
        with self.lock:
            if self.iq_file_source_config is None:
                return self.status()
            self.iq_file_source_config = None
            if self.settings.serial:
                config = self.settings.to_rtl_config()
                old_capture = self.capture
                source = self._new_rtl_capture_source(config)
                source.start()
                self.capture = source
                self.capture_error = None
                self._ensure_capture_pipeline_locked()
                self._sync_stream_workers_locked()
                if old_capture is not None:
                    self._stop_capture_async(old_capture, None)
            return self.status()

    def _new_rtl_capture_source(self, config: RtlConfig) -> RtlCaptureSource:
        source = RtlCaptureSource(config)
        source.set_device_providers(
            rtl_devices_provider=self._cached_rtl_devices,
            usb_rtl_devices_provider=self._cached_usb_rtl_devices,
        )
        return source

    def _iq_test_source_path(self, file_name: str) -> Path:
        directory = self.iq_test_sources_directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        path = (directory / file_name).resolve()
        if path.parent != directory:
            raise ValueError("invalid I/Q source file")
        if not path.is_file():
            raise FileNotFoundError("I/Q source file was not found")
        return path

    def _ensure_development_iq_sources_enabled(self) -> None:
        if not self.development_iq_sources_enabled:
            raise ValueError("I/Q file test sources are only available when running from the source tree.")

    def _stop_capture_async(
        self,
        capture: RtlCaptureSource | IqFileCaptureSource,
        drain_thread: threading.Thread | None,
    ) -> None:
        threading.Thread(
            target=self._stop_detached_capture,
            args=(capture, drain_thread),
            name="rtl-web-stop",
            daemon=True,
        ).start()

    def _ensure_capture_pipeline_locked(self) -> None:
        if self.capture is None:
            return
        if self.raw_fanout is None:
            self.raw_fanout = RawRtlFanout(self.capture)
            self.raw_fanout.start()
        elif self.raw_fanout.source is not self.capture:
            self.raw_fanout.set_source(self.capture)
            self.raw_fanout.start()
        else:
            self.raw_fanout.start()
        if self.monitor_queue is None:
            self.monitor_queue = subscribe_raw_fanout(
                self.raw_fanout,
                max_chunks=64,
                name="web-status-drain",
            )
        if self.clean_fanout is None:
            self.clean_fanout = CleanIqFanout(self.raw_fanout)
        self.clean_fanout.start()
        if self.intermediate_fanout is None:
            self.intermediate_fanout = IntermediateIqFanout(
                self.clean_fanout,
                alias_filter_strength=self.settings.alias_filter_strength,
            )
            self.intermediate_fanout.start()
        else:
            self.intermediate_fanout.set_alias_filter_strength(self.settings.alias_filter_strength)
            self.intermediate_fanout.start()
        if self.drain_thread is None or not self.drain_thread.is_alive():
            self.drain_stop.clear()
            self.drain_thread = threading.Thread(
                target=self._drain_capture,
                name="iq-web-drain",
                daemon=True,
            )
            self.drain_thread.start()

    def _detach_capture_locked(self) -> None:
        # The receiver and streams are left running: their channel feeds
        # move to whatever SDR is used next.
        self.drain_stop.set()
        self._stop_iq_recorder_locked()
        intermediate = self.intermediate_fanout
        self.intermediate_fanout = None
        clean = self.clean_fanout
        self.clean_fanout = None
        fanout = self.raw_fanout
        self.raw_fanout = None
        self.monitor_queue = None
        capture = self.capture
        self.capture = None
        drain_thread = self.drain_thread
        self.drain_thread = None
        LOG.info("stopped RTL-SDR control capture")
        if intermediate is not None:
            intermediate.stop()
        if clean is not None:
            clean.stop()
        if fanout is not None:
            fanout.stop()
        if capture is not None:
            threading.Thread(
                target=self._stop_detached_capture,
                args=(capture, drain_thread),
                name="rtl-web-stop",
                daemon=True,
            ).start()

    def stop_capture(self) -> None:
        with self.lock:
            self.drain_stop.set()
            wake_queue(self.monitor_queue)
            recorder = self.iq_recorder
            self.iq_recorder = None
            # Workers read settings under this lock while they run, so they are stopped
            # after it is released; waiting on them while holding it stalls every stop.
            workers = self._detach_receiver_workers_locked() + self._detach_stream_workers_locked()
            self.preview_streams = {}
            intermediate = self.intermediate_fanout
            self.intermediate_fanout = None
            clean = self.clean_fanout
            self.clean_fanout = None
            fanout = self.raw_fanout
            self.raw_fanout = None
            self.monitor_queue = None
            capture = self.capture
            self.capture = None
            self.iq_file_source_config = None
            drain_thread = self.drain_thread
            self.drain_thread = None
        stop_workers_together(workers)
        if recorder is not None:
            recorder.stop()
        if intermediate is not None:
            intermediate.stop()
        if clean is not None:
            clean.stop()
        if fanout is not None:
            fanout.stop()
        if capture is not None:
            capture.stop()
        if drain_thread is not None:
            drain_thread.join(timeout=2.0)
        LOG.info("stopped RTL-SDR control capture")

    @staticmethod
    def _stop_detached_capture(
        capture: RtlCaptureSource | IqFileCaptureSource,
        drain_thread: threading.Thread | None,
    ) -> None:
        capture.stop()
        if drain_thread is not None:
            drain_thread.join(timeout=2.0)

    def _drain_capture(self) -> None:
        while not self.drain_stop.is_set():
            with self.lock:
                monitor_queue = self.monitor_queue
            if monitor_queue is None:
                return
            try:
                batch = monitor_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                with self.lock:
                    self.capture_error = str(exc)
                LOG.warning("RTL-SDR control capture reported: %s", exc)
                continue
            if batch is QUEUE_WAKEUP:
                continue
            with self.lock:
                self.capture_error = None
                self.last_batch_at = batch.captured_at
                self.received_chunks += 1
                self.received_bytes += iq_batch_byte_count(batch)
