from __future__ import annotations

import argparse
import json
import logging
import queue
import signal
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

if not __package__:
    # Run as a script from the source tree: run this file as part of its package
    # instead, so it imports the rest of NWR Stream Manager the same way as when
    # installed.
    import importlib
    import types

    package_name = "nwr_stream_manager_runtime"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[attr-defined]
    sys.modules.setdefault(package_name, package)
    package.__version__ = importlib.import_module(f"{package_name}.version").package_version()  # type: ignore[attr-defined]
    raise SystemExit(importlib.import_module(f"{package_name}.web_control").main())

import numpy as np

from . import __version__ as APP_VERSION
from .accounts import (
    account_password_hash_needs_upgrade,
    AccountRecord,
    AccountStore,
    AuthSessionStore,
    hash_account_password,
    verify_account_password,
)
from .alsa import AlsaStreamTapConfig, discover_playback_devices, playback_device_usb_node
from .audio_effects import AudioEffectsProcessor, deemphasis_makeup_gain
from .baseline_pages import (
    ACCOUNT_FORM_PATHS,
    CHANGE_PASSWORD_FORM_PATH,
    DASHBOARD_PARTS_PATH,
    EAS_ALERT_EXPORT_PATH,
    EAS_ALERT_FORM_PATHS,
    FALLBACK_SETTINGS_PATH,
    FALLBACK_UPLOAD_PATH,
    format_account_date,
    HELP_FILE_NAME,
    INDEX_HTML,
    INDEX_VIEWS,
    IQ_RECORDER_FORM_PATHS,
    IQ_RECORDING_DOWNLOAD_PATH,
    MUST_CHANGE_PASSWORD_HTML,
    NOTIFICATION_FORM_PATHS,
    OUTPUT_FORM_PATHS,
    render_notice_page,
    RTL_FORM_PATHS,
    SDR_SHARING_PATHS,
    SETUP_FORM_PATH,
    SETUP_HTML,
    STREAM_FORM_PATHS,
    WIZARD_FORM_PATH,
)
from .config import IcecastConfig, IQ_SAMPLE_RATE
from .constants import (
    ACCOUNT_ROLE_ADMIN,
    ACCOUNT_ROLE_OWNER,
    ACCOUNT_ROLE_READ_ONLY,
    ACCOUNTS_DATABASE_FILE_NAME,
    ALIAS_FILTER_MAX_TRANSITION_SCALE,
    ALIAS_FILTER_MIN_ATTENUATION_DB,
    ALIAS_FILTER_STRENGTH_DEFAULT,
    AUTH_SESSION_COOKIE_NAME,
    CHANNEL_IQ_ALIAS_TRANSITION_HZ,
    CUSTOM_FALLBACK_AUDIO_FILE_NAME,
    FALLBACK_STATE_FILE_NAME,
    INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
    INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
    INTERMEDIATE_IQ_SAMPLE_RATE,
    INTERMEDIATE_SOURCE_QUEUE_CHUNKS,
    IQ_DOWNLOAD_YIELD_SECONDS,
    IQ_RECORDER_DEFAULT_DURATION_SECONDS,
    IQ_RECORDER_MODE_SPECTRUM,
    IQ_RECORDER_MODE_STREAM,
    IQ_RECORDER_QUEUE_CHUNKS,
    IQ_RECORDER_SAMPLE_RATES,
    IQ_RECORDINGS_DIRECTORY_NAME,
    IQ_RECORDINGS_INDEX_FILE_NAME,
    IQ_TEST_SOURCES_DIRECTORY_NAME,
    LIVE_AUDIO_BINARY_HEADER,
    LIVE_AUDIO_BINARY_MAGIC,
    LIVE_AUDIO_CODEC_OPUS,
    LIVE_AUDIO_KEEPALIVE_SECONDS,
    LIVE_AUDIO_SHUTDOWN_NOTICE_SECONDS,
    LIVE_IQ_QUEUE_SECONDS,
    LOG_FILE_NAME,
    MAX_JSON_REQUEST_BYTES,
    NOTIFICATION_STATE_FILE_NAME,
    RECEIVER_AUDIO_CONFIG,
    REMOTE_HOST_CHANNEL_QUEUE_SECONDS,
    REMOTE_HOST_WIDEBAND_QUEUE_SECONDS,
    REMOTE_SDR_DIRECTORY_NAME,
    REMOTE_SDR_HOST_SETTINGS,
    RTL_RESET_REAPPEAR_TIMEOUT_SECONDS,
    RTL_RESET_RESULT_SHOW_SECONDS,
    SOUNDCARD_DEVICE_NAMES_FILE_NAME,
    SOUNDCARD_PREVIEW_CLEANUP_SECONDS,
    SOUNDCARD_PREVIEW_TIMEOUT_SECONDS,
    STORAGE_CRITICAL_FREE_BYTES,
    STREAM_FRAME_BYTES,
    STREAM_FRAME_SAMPLES,
    STREAM_FRAME_SECONDS,
    STREAM_NOTIFICATION_FAILURE_GRACE_SECONDS,
    STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS,
    STREAM_TEST_MODE_NOISE_DBFS,
    STREAM_TEST_MODE_SAME_AUDIO,
    STREAM_TEST_MODE_TONE_MAX_FREQUENCY_HZ,
    STREAM_WORKER_RAW_QUEUE_SECONDS,
    STREAMS_DIRECTORY_NAME,
    STREAMS_STATE_FILE_NAME,
)
from .custom_fallback_audio import prepare_custom_fallback_audio
from .dependency_check import check_startup_dependencies, DependencyCheckError, format_dependency_summary
from .device_probe import SharedDeviceProbe
from .diagnostics import rms_float
from .dsp import create_decimator, IqChannelizer
from .eas_alerts import (
    alert_range_bounds_from_request,
    eas_alert_detail,
    eas_alert_id,
    eas_alert_index_path,
    eas_alert_notification_id,
    eas_alert_notification_message,
    eas_alert_summary,
    format_same_location_for_alert,
    safe_eas_alert_file_path,
    stream_alerts_directory,
    validate_eas_recording_payload,
)
from .fallback_audio import FallbackAudio
from .fallback_settings import (
    load_fallback_settings,
    validate_fallback_settings_payload,
    validate_stream_fallback_action_payload,
)
from .handler_auth import AuthHandlerMixin
from .handler_eas_pages import EasAlertPageHandlerMixin
from .handler_iq_pages import IqRecorderPageHandlerMixin
from .handler_live_audio import LiveAudioHandlerMixin
from .handler_pages import PageHandlerMixin
from .handler_responses import ResponseHandlerMixin
from .handler_settings_pages import SettingsPageHandlerMixin
from .handler_stream_pages import StreamPageHandlerMixin
from .icecast import IcecastSource
from .iq_pipeline import (
    alias_filter_attenuation_db,
    alias_filter_transition_hz,
    CleanIqFanout,
    drain_queue_to_latest,
    IntermediateIqFanout,
    iq_batch_complex,
    IqFileCaptureSource,
    LocalSdrRemoteBackend,
    NullIqFanout,
    RawRtlFanout,
    stop_workers_together,
    validate_receiver_frequency,
    wake_queue,
)
from .iq_recorder import (
    convert_iq_recording_chunks,
    estimate_iq_storage_remaining_seconds,
    iq_recording_download_name,
    iq_recording_download_size,
    IqRecorderConfig,
    IqRecorderWorker,
    load_iq_recording_entries,
    smooth_iq_storage_remaining_seconds,
    validate_iq_recording_duration,
)
from .live_audio import LiveAudioSource, LiveAudioWebSocketWriter
from .models import (
    IqFileSourceConfig,
    IqSampleBatch,
    reconcile_notification_access_url_selection,
    RtlControlSettings,
    WebFallbackPlaybackState,
    WebFallbackSettings,
    WebNotificationSettings,
    WebStreamFallbackAction,
    WebStreamNotificationSettings,
)
from .network import (
    access_urls,
    DNS_CLASS_IN,
    dns_decode_name,
    dns_encode_name,
    DNS_TYPE_PTR,
    interface_is_up,
    notification_access_url_options,
    tailscale_serve_address,
)
from .nfm import NfmDemodulator
from .notifications import (
    eas_alert_detail_route_path,
    load_notification_settings,
    notification_app_url,
    notification_click_url,
    notification_next_path,
    notification_target_restricted_for_read_only,
    send_ntfy_notification,
    soundcard_notification_name,
    stream_notification_failures,
    stream_notification_settings_from_stream,
    stream_reception_failure,
    validate_notification_settings_payload,
    validate_stream_notification_payload,
)
from .packaged_audio import read_packaged_audio
from .receiver import WeatherReceiverWorker
from .remote_sdr import (
    load_or_create_identity,
    PairedDeviceStore,
    REMOTE_SDR_DISCOVERY_PORT,
    REMOTE_SDR_TCP_PORT,
    RemoteSdrClient,
    RemoteSdrServer,
)
from .rtl import (
    DEFAULT_RTL_SAMPLE_RATE,
    list_rtl_devices,
    list_usb_rtl_devices,
    NWR_CENTER_FREQUENCY_HZ,
    reset_usb_device_node,
    reset_usb_rtl_device,
    RtlCaptureSource,
    RtlConfig,
    RtlSampleBatch,
)
from .same_live import generate_same_message, SameEventQueue
from .service_capture import CaptureServiceMixin
from .service_devices import DeviceServiceMixin
from .service_eas_alerts import EasAlertServiceMixin
from .service_fallback import FallbackServiceMixin
from .service_iq_recorder import IqRecorderServiceMixin
from .service_live_audio import LiveAudioServiceMixin
from .service_notifications import NotificationServiceMixin
from .service_sdr_sharing import SdrSharingServiceMixin
from .service_streams import StreamServiceMixin
from .service_test_mode import TestModeServiceMixin
from .signal_meter import ChannelSignalMeter
from .simulated_station import (
    _test_mode_same_message_audio,
    asset_path,
    load_packaged_wav_float,
    mix_test_mode_iq_crossfade,
    shape_test_mode_program_audio,
    stream_test_mode_same_header,
    SyntheticNwrTestModeSource,
)
from .state_files import (
    default_state_path,
    load_settings,
    load_soundcard_name_cache,
    save_settings,
    systemd_directory_path,
)
from .storage import directory_size_bytes, format_storage_bytes, storage_status_message, StorageMonitor
from .stream_audio import FloatFrameBuffer, load_web_fallback_audio, next_web_fallback_frame
from .stream_config import (
    audio_config_from_stream,
    icecast_auth_settings,
    icecast_config_from_output,
    icecast_mountpoint_url,
    icecast_output_signature,
    load_station_database,
    load_stream_configs,
    load_streams,
    migrate_stream_notch_width,
    migrate_stream_volume_multiplier,
    public_active_stream_snapshot,
    public_stream,
    validate_audio_payload,
    validate_icecast_payload,
)
from .stream_worker import IcecastOutputWriter, IcecastStreamWorker, SharedSoundcardOutputManager
from .time_budget import TEST_MODE_TIME_FILE_NAME, TestModeTimeBudget
from .tone_generators import parse_tone_generator_settings, ToneGeneratorBank
from .webrtc import LIVE_AUDIO_UPSTREAM_HOLD_SECONDS


LOG = logging.getLogger(__name__)


class RingLogHandler(logging.Handler):
    def __init__(self, max_records: int = 200) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=max_records)
        self.records_lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        with self.records_lock:
            self.records.append(message)

    def snapshot(self) -> list[str]:
        with self.records_lock:
            return list(self.records)


class RtlControlService(
    LiveAudioServiceMixin,
    DeviceServiceMixin,
    IqRecorderServiceMixin,
    EasAlertServiceMixin,
    TestModeServiceMixin,
    StreamServiceMixin,
    FallbackServiceMixin,
    NotificationServiceMixin,
    CaptureServiceMixin,
    SdrSharingServiceMixin,
):
    def __init__(
        self,
        state_path: Path,
        log_handler: RingLogHandler,
        *,
        development_iq_sources_enabled: bool = False,
    ) -> None:
        self.state_path = state_path
        self.streams_state_path = state_path.with_name(STREAMS_STATE_FILE_NAME)
        self.streams_directory = state_path.parent / STREAMS_DIRECTORY_NAME
        self.iq_recordings_directory = state_path.parent / IQ_RECORDINGS_DIRECTORY_NAME
        self.iq_recordings_index_path = self.iq_recordings_directory / IQ_RECORDINGS_INDEX_FILE_NAME
        self.iq_test_sources_directory = state_path.parent / IQ_TEST_SOURCES_DIRECTORY_NAME
        self.development_iq_sources_enabled = bool(development_iq_sources_enabled)
        self.web_host = "0.0.0.0"
        self.web_port = 8080
        self.fallback_state_path = state_path.with_name(FALLBACK_STATE_FILE_NAME)
        self.custom_fallback_audio_path = state_path.with_name(CUSTOM_FALLBACK_AUDIO_FILE_NAME)
        # One custom announcement is checked and stored at a time.
        self.custom_fallback_audio_lock = threading.Lock()
        self.notification_state_path = state_path.with_name(NOTIFICATION_STATE_FILE_NAME)
        self.test_mode_time = TestModeTimeBudget(state_path.with_name(TEST_MODE_TIME_FILE_NAME))
        self.soundcard_names_path = state_path.with_name(SOUNDCARD_DEVICE_NAMES_FILE_NAME)
        self.accounts = AccountStore(state_path.parent / ACCOUNTS_DATABASE_FILE_NAME)
        self.auth_sessions = AuthSessionStore()
        self.log_handler = log_handler
        self.storage_monitor = StorageMonitor(
            [state_path.parent],
            critical_callback=self._handle_critical_storage,
            recovery_callback=self._handle_storage_recovered,
        )
        self.device_probe = SharedDeviceProbe(
            {
                "rtl": list_rtl_devices,
                "rtl_usb": list_usb_rtl_devices,
                "alsa_playback": discover_playback_devices,
            },
            poll_interval_seconds=0.5,
        )
        self.lock = threading.RLock()
        self.settings = load_settings(state_path)
        # Numbers each status sent to remote clients, so they can tell one
        # taken before a settings change from one taken after it.
        self.remote_host_status_seq = 0
        # Browsers wait on this to hear about SDR settings changes as they happen.
        self.settings_change_condition = threading.Condition()
        self.settings_change_count = 0
        self.last_remote_host_settings: dict[str, Any] | None = None
        self.streams = load_streams(self.streams_directory, self.streams_state_path)
        self._migrate_stream_fallback_actions()
        self.fallback_settings = load_fallback_settings(self.fallback_state_path)
        self.default_fallback_audio = load_web_fallback_audio()
        self.custom_fallback_audio: FallbackAudio | None = None
        self.custom_fallback_audio_problem = ""
        self._load_custom_fallback_audio()
        self.notification_settings = load_notification_settings(self.notification_state_path)
        self.stations = load_station_database()
        self.capture: RtlCaptureSource | IqFileCaptureSource | None = None
        self.iq_file_source_config: IqFileSourceConfig | None = None
        self.raw_fanout: RawRtlFanout | None = None
        self.clean_fanout: CleanIqFanout | None = None
        self.intermediate_fanout: IntermediateIqFanout | None = None
        self.test_mode_fanout = NullIqFanout()
        self.monitor_queue: queue.Queue | None = None
        self.drain_thread: threading.Thread | None = None
        self.drain_stop = threading.Event()
        self.stream_workers: dict[str, IcecastStreamWorker] = {}
        self.preview_streams: dict[str, dict[str, Any]] = {}
        self.preview_cleanup_stop = threading.Event()
        self.preview_cleanup_thread = threading.Thread(
            target=self._preview_cleanup_loop,
            name="soundcard-preview-cleanup",
            daemon=True,
        )
        self.stream_notification_stop = threading.Event()
        self.stream_notification_thread = threading.Thread(
            target=self._stream_notification_loop,
            name="stream-notification-monitor",
            daemon=True,
        )
        self.monitor_streams_by_client: dict[str, str] = {}
        self.monitor_accounts_by_client: dict[str, int] = {}
        self.receiver_workers: dict[str, WeatherReceiverWorker] = {}
        self.receiver_accounts_by_client: dict[str, int] = {}
        self.live_audio_feedback_by_client: dict[tuple[str, str], dict[str, Any]] = {}
        self.rtl_device_name_cache: dict[str, str] = {}
        self.soundcard_name_cache: dict[str, str] = load_soundcard_name_cache(self.soundcard_names_path)
        self._enrich_stream_soundcard_names_from_cache_locked()
        self.stream_notification_states: dict[str, dict[str, Any]] = {}
        self.rtl_notification_states: dict[str, dict[str, Any]] = {}
        self.stream_eas_alert_notification_seen: dict[str, set[str]] = {}
        self.stream_test_mode: dict[str, Any] | None = None
        self.shutting_down = threading.Event()
        self.live_audio_connections = 0
        self.iq_recorder: IqRecorderWorker | None = None
        self.iq_recorder_account_id: int | None = None
        self.iq_recording_downloads: set[str] = set()
        self.iq_recording_download_accounts: dict[str, int] = {}
        self.iq_recording_download_recordings: dict[str, str] = {}
        self.aborted_iq_recording_downloads: set[str] = set()
        self.soundcard_manager = SharedSoundcardOutputManager(devices_provider=self._cached_soundcards)
        self.icecast_auth_cache: dict[str, float] = {}
        self.reset_lock = threading.Lock()
        # The latest SDR reset, shown on every page here and on remote clients.
        self.sdr_reset: dict[str, Any] | None = None
        self.sdr_reset_count = 0
        self.capture_error: str | None = None
        self.last_batch_at: float | None = None
        self.received_chunks = 0
        self.received_bytes = 0
        remote_directory = state_path.parent / REMOTE_SDR_DIRECTORY_NAME
        self.remote_identity = load_or_create_identity(remote_directory)
        self.remote_devices = PairedDeviceStore(remote_directory / "paired-devices.json")
        self.remote_backend = LocalSdrRemoteBackend(self)
        self.remote_server: RemoteSdrServer | None = None
        self.remote_sdr_port = REMOTE_SDR_TCP_PORT
        self.remote_sdr_discovery_port = REMOTE_SDR_DISCOVERY_PORT
        self.remote_server_error = ""
        self.remote_client: RemoteSdrClient | None = None
        self.storage_monitor.add_path(self.iq_recordings_directory)
        self.storage_monitor.start()
        self.preview_cleanup_thread.start()
        self.stream_notification_thread.start()
        if self.settings.uses_remote_sdr:
            self._sync_remote_sdr_locked()
            self._sync_stream_workers_locked()
        elif self.settings.serial:
            self._start_or_update_capture_locked()
            self._sync_remote_sdr_locked()
        else:
            self._select_only_connected_device()

    def begin_shutdown(self) -> None:
        """Tell connected browsers the server is going away, before anything is stopped."""
        self.shutting_down.set()
        with self.settings_change_condition:
            self.settings_change_condition.notify_all()

    def _wait_for_live_audio_notices(self) -> None:
        deadline = time.monotonic() + LIVE_AUDIO_SHUTDOWN_NOTICE_SECONDS
        while time.monotonic() < deadline:
            with self.lock:
                if self.live_audio_connections <= 0:
                    return
            time.sleep(0.02)

    def close(self) -> None:
        self.begin_shutdown()
        self._wait_for_live_audio_notices()
        self.preview_cleanup_stop.set()
        self.stream_notification_stop.set()
        self.preview_cleanup_thread.join(timeout=2.0)
        self.stream_notification_thread.join(timeout=2.0)
        with self.lock:
            recorder = self.iq_recorder
            self.iq_recorder = None
        if recorder is not None:
            recorder.stop()
        with self.lock:
            server, client = self.remote_server, self.remote_client
            self.remote_server = None
            self.remote_client = None
        for remote in (server, client):
            if remote is not None:
                remote.stop()
        self.stop_capture()
        self.soundcard_manager.stop()
        self.storage_monitor.stop()

    def revoke_account_long_lived_resources(
        self,
        account_id: int,
        *,
        stop_webrtc: bool = False,
        abort_downloads: bool = False,
        stop_iq_recording: bool = False,
    ) -> None:
        account_id = int(account_id)
        with self.lock:
            monitor_client_ids = [
                client_id for client_id, owner_id in self.monitor_accounts_by_client.items()
                if owner_id == account_id
            ] if stop_webrtc else []
            receiver_client_ids = [
                client_id for client_id, owner_id in self.receiver_accounts_by_client.items()
                if owner_id == account_id
            ] if stop_webrtc else []
            aborted_downloads = [
                download_id for download_id, owner_id in self.iq_recording_download_accounts.items()
                if owner_id == account_id
            ] if abort_downloads else []
            if aborted_downloads:
                self.aborted_iq_recording_downloads.update(aborted_downloads)
            recorder = self.iq_recorder if stop_iq_recording and self.iq_recorder_account_id == account_id else None
            if recorder is not None:
                self.iq_recorder = None
                self.iq_recorder_account_id = None
        for client_id in monitor_client_ids:
            try:
                self.stop_monitor({"client_id": client_id})
            except Exception as exc:
                LOG.debug("failed to stop monitor for revoked account %s client %s: %s", account_id, client_id, exc)
        for client_id in receiver_client_ids:
            try:
                self.stop_receiver({"client_id": client_id})
            except Exception as exc:
                LOG.debug("failed to stop receiver for revoked account %s client %s: %s", account_id, client_id, exc)
        if recorder is not None:
            recorder.stop()
            LOG.info("stopped I/Q recording for revoked account %s", account_id)
        if monitor_client_ids or receiver_client_ids or aborted_downloads or recorder is not None:
            LOG.info(
                "revoked long-lived resources for account %s: monitors=%s receivers=%s downloads=%s iq_recorder=%s",
                account_id,
                len(monitor_client_ids),
                len(receiver_client_ids),
                len(aborted_downloads),
                recorder is not None,
            )

    def _handle_critical_storage(self, snapshot: dict[str, Any]) -> None:
        message = str(snapshot.get("message") or storage_status_message("critical"))
        with self.lock:
            workers = list(self.stream_workers.values())
            recorder = self.iq_recorder
            if recorder is not None:
                self.iq_recorder = None
                self.iq_recorder_account_id = None
        for worker in workers:
            worker.stop_eas_recording_due_to_storage(message)
        if recorder is not None:
            LOG.warning("stopping I/Q recorder because storage is critically low")
            recorder.stop()

    def _handle_storage_recovered(self, snapshot: dict[str, Any]) -> None:
        LOG.info("storage is no longer critically low; resyncing stream recorders")
        with self.lock:
            self._sync_stream_workers_locked()

    def _preview_cleanup_loop(self) -> None:
        while not self.preview_cleanup_stop.wait(SOUNDCARD_PREVIEW_CLEANUP_SECONDS):
            self._cleanup_expired_soundcard_previews()
            self._cleanup_expired_stream_test_mode()

    def status(self, *, read_only: bool = False) -> dict[str, Any]:
        self._cleanup_expired_soundcard_previews()
        with self.lock:
            self._cleanup_expired_stream_test_mode_locked(time.time())
            sdr_reset = self._sdr_reset_status_locked()
            capture = self.capture
            settings = self._effective_settings_locked()
            active = capture is not None
            gain_values = capture.get_gain_values() if isinstance(capture, RtlCaptureSource) else []
            capture_stats = capture.stats() if capture is not None else {}
            fanout_stats = self.raw_fanout.stats() if self.raw_fanout is not None else {}
            clean_stats = self.clean_fanout.stats() if self.clean_fanout is not None else {}
            intermediate_stats = self.intermediate_fanout.stats() if self.intermediate_fanout is not None else {}
            source_kind = "iq_file" if self.iq_file_source_config is not None else "rtl"
            source_name = (
                self.iq_file_source_config.path.name
                if self.iq_file_source_config is not None
                else (settings.serial or "")
            )
            streams = [public_stream(stream) for stream in self.streams] if read_only else list(self.streams)
            active_streams = self._active_streams_locked()
            if read_only:
                active_streams = [public_active_stream_snapshot(snapshot) for snapshot in active_streams]
            else:
                self._process_stream_notifications_locked(active_streams)
            notification_settings = self._reconcile_notification_access_url_selection_locked() if not read_only else None
            notifications = {} if read_only or notification_settings is None else notification_settings.public_dict()
            if notifications:
                notifications["effective_access_url"] = self.notification_effective_access_url()
            settings_payload = asdict(settings)
            if read_only:
                # Notification settings are for the owner and administrators only.
                settings_payload.pop("notify_sdr_failures", None)
            source_payload = {
                "kind": source_kind,
                "name": source_name,
                "sample_rate": (
                    self.iq_file_source_config.sample_rate
                    if self.iq_file_source_config is not None
                    else settings.sample_rate
                ),
                "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
            }
            capture_error = self.capture_error
            if settings.uses_remote_sdr:
                # Report the remote host's SDR as this machine's SDR.
                remote_status = self.remote_client.status() if self.remote_client is not None else {}
                remote_sdr = remote_status.get("sdr") if isinstance(remote_status.get("sdr"), dict) else {}
                host_settings = remote_sdr.get("settings") if isinstance(remote_sdr.get("settings"), dict) else {}
                settings_payload.update({key: host_settings[key] for key in REMOTE_SDR_HOST_SETTINGS if key in host_settings})
                gain_values = list(remote_sdr.get("gain_values") or [])
                active = bool(remote_status.get("reachable") and remote_sdr.get("capture_active"))
                source_payload = {
                    "kind": "remote",
                    "name": remote_status.get("host_name", ""),
                    "host_id": settings.remote_host_id,
                    "device_name": remote_sdr.get("device_name", ""),
                    "reachable": bool(remote_status.get("reachable")),
                    "sample_rate": remote_sdr.get("sample_rate", settings.sample_rate),
                    "center_frequency_hz": remote_sdr.get("center_frequency_hz", NWR_CENTER_FREQUENCY_HZ),
                }
                # A reset of the host's SDR, started there or from any machine using it.
                sdr_reset = remote_sdr.get("reset") if remote_status.get("reachable") else None
                if remote_status.get("reachable"):
                    capture_error = remote_sdr.get("capture_error") or None
                elif self.remote_client is None or remote_status.get("connecting"):
                    capture_error = None
                else:
                    capture_error = remote_status.get("error") or "The remote SDR is not reachable."
            return {
                "settings": settings_payload,
                "gain_values": gain_values,
                "active": active,
                "source": source_payload,
                "remote_sdr": {} if read_only else self._remote_sdr_status_locked(),
                "capture_error": capture_error,
                "sdr_reset": sdr_reset,
                "capture_stats": capture_stats,
                "raw_fanout_stats": fanout_stats,
                "clean_fanout_stats": clean_stats,
                "intermediate_fanout_stats": intermediate_stats,
                "last_batch_at": self.last_batch_at,
                "received_chunks": self.received_chunks,
                "received_bytes": self.received_bytes,
                "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
                "fallback": self._fallback_settings_public_locked(),
                "notifications": notifications,
                "streams": streams,
                "active_streams": active_streams,
                "stream_signals": self._stream_signals_locked(),
                "active_eas_recorders": self._active_eas_recorders_locked(),
                "test_mode": {"active": False} if read_only else self._stream_test_mode_status_locked(),
                "recent_eas_alerts": self._recent_eas_alerts_locked(),
                "storage": self._storage_status_snapshot_locked(),
                "iq_recorder": self._iq_recorder_status_locked(),
                "logs": self.log_handler.snapshot()[-80:],
                "capabilities": {
                    "development_iq_sources": self.development_iq_sources_enabled,
                },
            }

    def _storage_status_snapshot_locked(self) -> dict[str, Any]:
        snapshot = self.storage_monitor.snapshot()
        iq_bytes = directory_size_bytes(self.iq_recordings_directory)
        eas_bytes = 0
        for stream in self.streams:
            eas_bytes += directory_size_bytes(stream_alerts_directory(self.streams_directory.parent, stream))
        snapshot["usage"] = {
            "iq_recordings_bytes": iq_bytes,
            "iq_recordings": format_storage_bytes(iq_bytes),
            "eas_recordings_bytes": eas_bytes,
            "eas_recordings": format_storage_bytes(eas_bytes),
        }
        return snapshot


class RtlControlHandler(
    AuthHandlerMixin,
    LiveAudioHandlerMixin,
    ResponseHandlerMixin,
    PageHandlerMixin,
    SettingsPageHandlerMixin,
    EasAlertPageHandlerMixin,
    StreamPageHandlerMixin,
    IqRecorderPageHandlerMixin,
    BaseHTTPRequestHandler,
):
    protocol_version = "HTTP/1.1"
    service: RtlControlService

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._auth_ok_or_setup_response(path, "GET"):
            return
        if path == "/api/live-audio":
            self._handle_live_audio_websocket(parsed)
        elif path == "/api/settings-events":
            self._handle_settings_events_websocket()
        elif path == "/notification":
            target = notification_next_path(parsed.query)
            account = getattr(self, "current_account", None)
            if isinstance(account, AccountRecord) and account.is_read_only and notification_target_restricted_for_read_only(target):
                self._send_notification_forbidden()
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", target)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self._send_pending_auth_session_cookie()
            self.end_headers()
        elif path == "/":
            self._send_index_page(parsed)
        elif path == EAS_ALERT_EXPORT_PATH:
            self._handle_eas_alert_export(parsed)
        elif path == DASHBOARD_PARTS_PATH:
            # For pages without the page script that can still update the dashboard as it changes.
            account = getattr(self, "current_account", None)
            self._send_json(self._dashboard_parts(isinstance(account, AccountRecord) and account.is_read_only))
        elif path == IQ_RECORDING_DOWNLOAD_PATH:
            self._handle_iq_recording_download(parsed)
        elif path == "/api/status":
            query = parse_qs(parsed.query)
            account = getattr(self, "current_account", None)
            read_only = isinstance(account, AccountRecord) and account.is_read_only
            if not read_only:
                self.service.heartbeat_soundcard_preview_stream(query.get("soundcard_preview_id", [""])[0])
            response = self.service.status(read_only=read_only)
            if isinstance(account, AccountRecord):
                response["account"] = account.to_public_dict()
                if account.is_read_only:
                    response["iq_recorder"] = self.service.redacted_iq_recorder_status()
            self._send_json(response)
        elif path == "/api/account/me":
            account = getattr(self, "current_account", None)
            self._send_json({"account": account.to_public_dict() if isinstance(account, AccountRecord) else None})
        elif path == "/api/accounts":
            self._send_json({"accounts": self.service.accounts.list_accounts()})
        elif path == "/api/rtl/diagnostics":
            try:
                response = self.service.rtl_diagnostics()
            except Exception as exc:
                LOG.warning("RTL-SDR diagnostics failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/devices":
            self._send_json(self.service.devices())
        elif path == "/api/notification-settings":
            self._send_json({"notifications": self.service.notification_settings_snapshot().public_dict()})
        elif path == "/api/fallback-audio":
            announcement = parse_qs(parsed.query).get("announcement", [""])[0]
            try:
                data = self.service.fallback_announcement_wav(announcement)
            except ValueError as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
            self._send_audio(data, "audio/wav")
        elif path == "/api/remote-sdr":
            self._send_json(self.service.remote_sdr_status())
        elif path == "/api/notification-access-urls":
            self._send_json(self.service.notification_access_url_options())
        elif path == "/api/iq-test-sources":
            try:
                self._send_json(self.service.iq_test_sources())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
                return
        elif path == "/api/stations":
            query = parse_qs(parsed.query)
            search = query.get("q", [""])[0]
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self._send_json(self.service.search_stations(search, limit))
        elif path == "/api/streams":
            account = getattr(self, "current_account", None)
            self._send_json(
                self.service.stream_status(
                    read_only=isinstance(account, AccountRecord) and account.is_read_only,
                    account_id=account.id if isinstance(account, AccountRecord) else None,
                )
            )
        elif path == "/api/eas-alert-streams":
            self._send_json(self.service.eas_alert_streams())
        elif path == "/api/eas-alerts":
            query = parse_qs(parsed.query)
            try:
                response = self.service.eas_alerts(
                    query.get("stream_id", [""])[0],
                    int(query.get("page", ["1"])[0]),
                    int(query.get("per_page", ["25"])[0]),
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/eas-alert-bulk-options":
            query = parse_qs(parsed.query)
            try:
                response = self.service.eas_alert_bulk_options(query.get("stream_id", [""])[0])
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/eas-alert-range-count":
            query = parse_qs(parsed.query)
            try:
                response = self.service.eas_alert_range_count(
                    query.get("stream_id", [""])[0],
                    query.get("mode", [""])[0],
                    query.get("start", [""])[0],
                    query.get("end", [""])[0],
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/eas-alert-export":
            query = parse_qs(parsed.query)
            try:
                zip_path, download_name = self.service.eas_alert_export_zip(
                    query.get("stream_id", [""])[0],
                    query.get("mode", [""])[0],
                    query.get("start", [""])[0],
                    query.get("end", [""])[0],
                )
                self._send_file(zip_path, download_name, True, delete_after=True)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
        elif path == "/api/eas-alert":
            query = parse_qs(parsed.query)
            try:
                response = self.service.eas_alert_detail(
                    query.get("stream_id", [""])[0],
                    query.get("alert_id", [""])[0],
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/eas-alert-audio":
            query = parse_qs(parsed.query)
            try:
                audio_path, download_name = self.service.eas_alert_audio_path(
                    query.get("stream_id", [""])[0],
                    query.get("alert_id", [""])[0],
                )
                self._send_file(audio_path, download_name, query.get("download", ["0"])[0] == "1")
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
        elif path == "/api/monitor/status":
            query = parse_qs(parsed.query)
            try:
                response = self.service.monitor_status(query.get("client_id", [""])[0])
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/receiver/status":
            query = parse_qs(parsed.query)
            try:
                response = self.service.receiver_status(query.get("client_id", [""])[0])
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
        elif path == "/api/iq-recorder/status":
            account = getattr(self, "current_account", None)
            if isinstance(account, AccountRecord) and account.is_read_only:
                self._send_json(self.service.redacted_iq_recorder_status())
            else:
                self._send_json(self.service.iq_recorder_status())
        elif path == "/api/iq-recordings":
            self._send_json(self.service.iq_recordings())
        elif path == "/api/iq-recording-download":
            query = parse_qs(parsed.query)
            recording_id = query.get("id", [""])[0]
            fmt = query.get("format", ["cf"])[0]
            try:
                recording, file_path, download_name, download_size = self.service.iq_recording_download_info(recording_id, fmt)
                account = getattr(self, "current_account", None)
                self._send_iq_recording(
                    file_path,
                    download_name,
                    download_size,
                    fmt,
                    str(recording.get("id", "")),
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if not self._auth_ok_or_setup_response(path, "POST"):
            return
        remote_sdr_actions = {
            "/api/remote-sdr/discover": lambda payload: self.service.discover_remote_sdrs(),
            "/api/remote-sdr/pairing/start": lambda payload: self.service.start_remote_sdr_pairing(),
            "/api/remote-sdr/pairing/keepalive": lambda payload: self.service.keep_remote_sdr_pairing_open(),
            "/api/remote-sdr/pairing/cancel": lambda payload: self.service.cancel_remote_sdr_pairing(),
            "/api/remote-sdr/pair": self.service.pair_remote_sdr,
            "/api/remote-sdr/unpair": self.service.unpair_remote_sdr_device,
        }
        if path in remote_sdr_actions:
            try:
                response = remote_sdr_actions[path](self._read_json())
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/fallback-audio":
            self._handle_custom_fallback_audio_upload()
            return
        if path == SETUP_FORM_PATH:
            self._handle_setup_form()
            return
        if path == CHANGE_PASSWORD_FORM_PATH:
            self._handle_change_password_form()
            return
        if path in ACCOUNT_FORM_PATHS:
            self._handle_account_form(path)
            return
        if path in EAS_ALERT_FORM_PATHS:
            self._handle_eas_alert_form(path)
            return
        if path in IQ_RECORDER_FORM_PATHS:
            self._handle_iq_recorder_form(path)
            return
        if path in RTL_FORM_PATHS:
            self._handle_rtl_form(path)
            return
        if path in STREAM_FORM_PATHS:
            self._handle_stream_form(path)
            return
        if path in OUTPUT_FORM_PATHS:
            self._handle_output_form(path)
            return
        if path == WIZARD_FORM_PATH:
            self._handle_wizard_form()
            return
        if path in SDR_SHARING_PATHS:
            self._handle_sdr_sharing_form(path)
            return
        if path in NOTIFICATION_FORM_PATHS:
            self._handle_notification_form(path)
            return
        if path == FALLBACK_SETTINGS_PATH:
            self._handle_fallback_settings_form()
            return
        if path == FALLBACK_UPLOAD_PATH:
            self._handle_fallback_upload_form()
            return
        if path == "/api/setup-account":
            try:
                payload = self._read_json()
                self.service.accounts.create_admin(
                    str(payload.get("username", "")),
                    str(payload.get("password", "")),
                    str(payload.get("confirm_password", "")),
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOG.info("created NWR Stream Manager administrator account")
            self._send_json({"success": True})
            return
        if path == "/api/account/password":
            try:
                account = getattr(self, "current_account", None)
                if not isinstance(account, AccountRecord):
                    raise ValueError("authentication is required")
                payload = self._read_json()
                updated = self._change_own_password(
                    account,
                    str(payload.get("current_password", "")),
                    str(payload.get("new_password", "")),
                    str(payload.get("confirm_password", "")),
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"success": True, "account": updated})
            return
        if path == "/api/accounts":
            try:
                payload = self._read_json()
                account, secret = self._create_account(str(payload.get("username", "")), bool(payload.get("read_only", False)))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"success": True, "account": account, "secret": secret})
            return
        if path == "/api/account-reset-password":
            try:
                account, secret = self._reset_account_password(int(self._read_json().get("account_id", 0)))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"success": True, "account": account, "secret": secret})
            return
        if path == "/api/eas-alert-delete":
            try:
                payload = self._read_json()
                response = self.service.remove_eas_alert_range(
                    str(payload.get("stream_id", "")),
                    str(payload.get("mode", "")),
                    str(payload.get("start", "")),
                    str(payload.get("end", "")),
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/notification-test":
            try:
                payload = self._read_json()
                response = self.service.test_notification_settings(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/client-log":
            try:
                payload = self._read_json()
                level = str(payload.get("level", "info")).lower()
                area = str(payload.get("area", "client"))[:40]
                message = str(payload.get("message", ""))[:240]
                details = payload.get("details", {})
                if (
                    area == "live-audio"
                    and message in {
                        "dropped live audio frame to reduce playback latency",
                        "live audio throughput sample",
                    }
                    and isinstance(details, dict)
                ):
                    self.service.record_live_audio_feedback(
                        str(details.get("client_id", "")),
                        str(details.get("mode", "")),
                        message,
                        details,
                    )
                detail_text = json.dumps(details, sort_keys=True)[:1000] if isinstance(details, dict) else str(details)[:1000]
                log_message = "client %s from %s: %s %s" % (area, self._client_address(), message, detail_text)
                if level in {"warning", "warn", "error"}:
                    LOG.warning("%s", log_message)
                else:
                    LOG.info("%s", log_message)
            except Exception as exc:
                LOG.warning("client log failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"success": True})
            return
        if path == "/api/monitor/stop":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.stop_monitor(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API monitor stop failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/monitor/pause":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.pause_monitor(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API monitor pause failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/monitor/resume":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.resume_monitor(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API monitor resume failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/tune":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.tune_receiver(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API receiver tune failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/pause":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.pause_receiver(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API receiver pause failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/resume":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.resume_receiver(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API receiver resume failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/stop":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.stop_receiver(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API receiver stop failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/iq-recorder/start":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.start_iq_recording(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API I/Q recorder start failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/iq-recorder/stop":
            try:
                # Read even though nothing in it is used, so it isn't taken for the next request.
                self._read_json()
                response = self.service.stop_iq_recording()
            except Exception as exc:
                LOG.warning("API I/Q recorder stop failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/rtl-reset":
            try:
                payload = self._read_json()
                response = self.service.reset_rtl_device(str(payload.get("serial", "")))
            except Exception as exc:
                LOG.warning("API RTL-SDR reset failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/soundcard-reset":
            try:
                payload = self._read_json()
                response = self.service.reset_soundcard_device(str(payload.get("stable_id", "")))
            except Exception as exc:
                LOG.warning("API soundcard reset failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/iq-test-source":
            try:
                payload = self._read_json()
                response = self.service.start_iq_test_source(payload)
            except Exception as exc:
                LOG.warning("API I/Q test source start failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/iq-test-source/stop":
            try:
                response = self.service.stop_iq_test_source()
            except Exception as exc:
                LOG.warning("API I/Q test source stop failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/iq-test-source/seek":
            try:
                payload = self._read_json()
                response = self.service.seek_iq_test_source(payload)
            except Exception as exc:
                LOG.warning("API I/Q test source seek failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/icecast-auth":
            try:
                payload = self._read_json()
                response = self.service.test_icecast_auth_payload(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/stream-output":
            try:
                payload = self._read_json()
                response = self.service.add_stream_output(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/stream-soundcard-preview":
            try:
                payload = self._read_json()
                response = self.service.upsert_soundcard_preview_stream(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/stream-soundcard-preview/discard":
            try:
                payload = self._read_json()
                response = self.service.discard_soundcard_preview_stream(str(payload.get("preview_id", "")))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/stream-test-mode":
            try:
                payload = self._read_json()
                response = self.service.update_stream_test_mode(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path != "/api/streams":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            response = self.service.add_stream(payload)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not self._auth_ok_or_setup_response(parsed.path, "DELETE"):
            return
        if parsed.path == "/api/accounts":
            try:
                account = self._delete_account(int(parse_qs(parsed.query).get("account_id", ["0"])[0]))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"success": True, "account": account})
            return
        if parsed.path == "/api/eas-alert":
            try:
                query = parse_qs(parsed.query)
                response = self.service.remove_eas_alert(
                    query.get("stream_id", [""])[0],
                    query.get("alert_id", [""])[0],
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if parsed.path == "/api/stream-output":
            try:
                query = parse_qs(parsed.query)
                stream_id = query.get("stream_id", [""])[0]
                output_id = query.get("output_id", [""])[0]
                response = self.service.remove_stream_output(stream_id, output_id)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if parsed.path == "/api/stream-soundcard-preview":
            try:
                query = parse_qs(parsed.query)
                response = self.service.discard_soundcard_preview_stream(query.get("preview_id", [""])[0])
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if parsed.path == "/api/iq-recording":
            try:
                query = parse_qs(parsed.query)
                response = self.service.remove_iq_recording(query.get("id", [""])[0])
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if parsed.path != "/api/streams":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            stream_id = parse_qs(parsed.query).get("id", [""])[0]
            response = self.service.remove_stream(stream_id)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if not self._auth_ok_or_setup_response(path, "PATCH"):
            return
        if path == "/api/account-read-only":
            try:
                payload = self._read_json()
                account = self._set_account_read_only(int(payload.get("account_id", 0)), bool(payload.get("read_only", False)))
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json({"success": True, "account": account})
            return
        if path == "/api/stream-output":
            try:
                payload = self._read_json()
                response = self.service.update_stream_output(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/fallback-settings":
            try:
                payload = self._read_json()
                response = self.service.update_fallback_settings(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/notification-settings":
            try:
                payload = self._read_json()
                response = self.service.update_notification_settings(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/stream-notifications":
            try:
                payload = self._read_json()
                response = self.service.update_stream_notifications(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/stream-fallback-action":
            try:
                payload = self._read_json()
                response = self.service.update_stream_fallback_action(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/eas-recording":
            try:
                payload = self._read_json()
                response = self.service.update_eas_recording(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/audio-effects":
            try:
                payload = self._read_json()
                response = self.service.update_audio_effects(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/streams":
            try:
                payload = self._read_json()
                response = self.service.update_stream(payload)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path != "/api/settings":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            response = self.service.update(payload)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    def do_PUT(self) -> None:
        self.do_PATCH()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nwr-stream-manager",
        description="Run the NWR Stream Manager web interface.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="address to bind; use 0.0.0.0 for LAN access",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", type=Path, default=default_state_path())
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="path for the rotating server log file; defaults to the state directory",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def default_log_path(state_path: Path) -> Path:
    logs_directory = systemd_directory_path("LOGS_DIRECTORY")
    if logs_directory is not None:
        return logs_directory / LOG_FILE_NAME
    return state_path.with_name(LOG_FILE_NAME)


def configure_file_logging(path: Path) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)


def development_iq_sources_available(argv0: str | None = None) -> bool:
    """Return true only when web_control.py is executed directly from source.

    Console scripts and ``python -m nwr_stream_manager`` both execute installed
    package code through an entry point, so they intentionally do not expose the
    development-only I/Q file source even when the package was installed from a
    local checkout.
    """
    invoked = Path(argv0 if argv0 is not None else sys.argv[0])
    try:
        invoked = invoked.resolve()
        current = Path(__file__).resolve()
    except OSError:
        return False
    if invoked != current or current.name != "web_control.py":
        return False
    if current.parent.name != "nwr-stream-manager" or current.parent.parent.name != "src":
        return False
    return (current.parents[2] / "pyproject.toml").is_file()


def install_shutdown_signal_handlers(
    server: ThreadingHTTPServer,
    on_shutdown: Callable[[], None] | None = None,
) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous_handlers: dict[int, Any] = {}

    def handle_shutdown(signum, _frame) -> None:
        signal_name = signal.Signals(signum).name
        LOG.info("received %s; shutting down NWR Stream Manager", signal_name)
        if on_shutdown is not None:
            on_shutdown()
        threading.Thread(
            target=server.shutdown,
            name="http-shutdown",
            daemon=True,
        ).start()

    for signum in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if signum is None:
            continue
        previous_handlers[int(signum)] = signal.getsignal(signum)
        signal.signal(signum, handle_shutdown)
    return previous_handlers


def restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signum, handler in previous_handlers.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


def run_server(host: str, port: int, state_path: Path, verbose: bool = False, log_file: Path | None = None) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log_path = log_file or default_log_path(state_path)
    configure_file_logging(log_path)
    try:
        dependency_results = check_startup_dependencies()
    except DependencyCheckError:
        LOG.exception("NWR Stream Manager cannot start because a dependency check failed")
        raise
    LOG.info("startup dependency check passed: %s", format_dependency_summary(dependency_results))
    ring_handler = RingLogHandler()
    logging.getLogger().addHandler(ring_handler)
    development_iq_sources = development_iq_sources_available()
    service = RtlControlService(
        state_path,
        ring_handler,
        development_iq_sources_enabled=development_iq_sources,
    )
    service.web_host = host
    service.web_port = port
    RtlControlHandler.service = service
    server = ThreadingHTTPServer((host, port), RtlControlHandler)
    previous_signal_handlers = install_shutdown_signal_handlers(server, service.begin_shutdown)
    LOG.info("RTL-SDR control web interface bound to %s:%s", host, port)
    for url in access_urls(host, port):
        LOG.info("RTL-SDR control web interface available at %s", url)
    LOG.info("RTL-SDR settings will be remembered in %s", state_path)
    LOG.info("server log file is %s", log_path)
    if development_iq_sources:
        LOG.info("development I/Q file source controls are enabled")
    try:
        # Check for shutdown often, so a restart isn't held up waiting on this loop.
        server.serve_forever(poll_interval=0.1)
    finally:
        restore_signal_handlers(previous_signal_handlers)
        server.server_close()
        service.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_server(args.host, args.port, args.state, args.verbose, args.log_file)
    except DependencyCheckError:
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
