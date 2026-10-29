from __future__ import annotations

import argparse
import errno
import json
import logging
import os
import queue
import secrets
import signal
import socket
import sys
import threading
import time
import unicodedata
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import ParseResult, urlparse

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
from .alsa import AlsaStreamTapConfig, discover_playback_devices
from .audio_effects import AudioEffectsProcessor
from .baseline_pages import (
    DASHBOARD_PARTS_PATH,
    format_account_date,
    HELP_FILE_NAME,
    INDEX_HTML,
    INDEX_VIEWS,
    MUST_CHANGE_PASSWORD_HTML,
    render_notice_page,
    SETUP_HTML,
)
from .config import IcecastConfig, IQ_SAMPLE_RATE
from .constants import (
    STATE_DIRECTORY_NAME,
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
from .general_settings import general_state_path, load_or_initialize_general_settings
from .handler_auth import AuthHandlerMixin
from .handler_eas_pages import EasAlertPageHandlerMixin
from .handler_iq_pages import IqRecorderPageHandlerMixin
from .handler_live_audio import LiveAudioHandlerMixin
from .handler_pages import PageHandlerMixin
from .handler_responses import RequestBody, ResponseHandlerMixin
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
from .microhttpd_server import MicrohttpdServer
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
from .routes_accounts import AccountRoutesMixin
from .routes_advanced import AdvancedRoutesMixin
from .routes_tailscale import TailscaleRoutesMixin
from .handler_two_step import TwoStepHandlerMixin
from .handler_sign_in_alerts import SignInAlertHandlerMixin
from .sign_in_alerts import AddressLocations
from .routes_eas_alerts import EasAlertRoutesMixin
from .routes_fallback import FallbackRoutesMixin
from .routes_general import GeneralRoutesMixin
from .routes_iq_recorder import IqRecorderRoutesMixin
from .routes_live_audio import LiveAudioRoutesMixin
from .routes_stream_archive import StreamArchiveRoutesMixin
from .routes_restart import RestartRoutesMixin
from .routes_notifications import NotificationRoutesMixin
from .routes_sdr import SdrRoutesMixin
from .routes_streams import StreamRoutesMixin
from .routing import route, route_table
from .rtl import (
    DEFAULT_RTL_SAMPLE_RATE,
    list_rtl_devices,
    list_usb_rtl_devices,
    NWR_CENTER_FREQUENCY_HZ,
    reset_usb_rtl_device,
    RtlCaptureSource,
    RtlConfig,
    RtlSampleBatch,
)
from .same_live import generate_same_message, SameEventQueue
from .service_advanced import AdvancedServiceMixin
from .service_tailscale import TailscaleServiceMixin
from .tailscale_build import build_libtailscale
from .service_capture import CaptureServiceMixin
from .service_devices import DeviceServiceMixin
from .service_eas_alerts import EasAlertServiceMixin
from .service_stream_archive import StreamArchiveServiceMixin
from .service_fallback import FallbackServiceMixin
from .service_general import GeneralServiceMixin
from .service_iq_recorder import IqRecorderServiceMixin
from .service_live_audio import LiveAudioServiceMixin
from .service_notifications import NotificationServiceMixin
from .service_restart import restart_process, RESTART_RESPONSE_SECONDS, RestartServiceMixin
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
    make_owner_only,
    OWNER_ONLY_UMASK,
    save_settings,
    systemd_directory_path,
)
from .storage import directory_size_bytes, format_storage_bytes, storage_status_message, StorageMonitor
from .stream_audio import FloatFrameBuffer, load_web_fallback_audio, next_web_fallback_frame
from .stream_archive import stream_archive_directory
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
from .web_address import (
    DEFAULT_WEB_PORT,
    load_web_address_settings,
    resolve_web_address,
    validate_web_port,
    WebAddress,
    web_address_state_path,
)


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
    StreamArchiveServiceMixin,
    TestModeServiceMixin,
    StreamServiceMixin,
    FallbackServiceMixin,
    NotificationServiceMixin,
    CaptureServiceMixin,
    SdrSharingServiceMixin,
    AdvancedServiceMixin,
    TailscaleServiceMixin,
    RestartServiceMixin,
    GeneralServiceMixin,
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
        self.web_port = DEFAULT_WEB_PORT
        # Where the web interface is listening and why; run_server sets it once it knows.
        self.web_address = WebAddress(self.web_host, self.web_port)
        self.web_address_state_path = web_address_state_path(state_path)
        self.general_state_path = general_state_path(state_path)
        self.general = load_or_initialize_general_settings(self.general_state_path)
        self.fallback_state_path = state_path.with_name(FALLBACK_STATE_FILE_NAME)
        self.custom_fallback_audio_path = state_path.with_name(CUSTOM_FALLBACK_AUDIO_FILE_NAME)
        # One custom announcement is checked and stored at a time.
        self.custom_fallback_audio_lock = threading.Lock()
        self.notification_state_path = state_path.with_name(NOTIFICATION_STATE_FILE_NAME)
        self.test_mode_time = TestModeTimeBudget(state_path.with_name(TEST_MODE_TIME_FILE_NAME))
        self.soundcard_names_path = state_path.with_name(SOUNDCARD_DEVICE_NAMES_FILE_NAME)
        self.accounts = AccountStore(state_path.parent / ACCOUNTS_DATABASE_FILE_NAME)
        # Where each wrong password or code came from, looked up in the background for the account to be told.
        self.address_locations = AddressLocations()
        self.accounts.on_sign_in_attempt = self._locate_sign_in_attempt
        self.auth_sessions = AuthSessionStore(connect=self.accounts._connect)
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
        self._init_stream_archives()
        self._init_tailscale()
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
        # When this run of NWR Stream Manager started, for its uptime.
        self.started_at = time.monotonic()
        # Which run of NWR Stream Manager this is, so a restart is only ever asked of the run it was meant for.
        self.instance_id = secrets.token_hex(8)
        self.restart_requested = threading.Event()
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
        self._start_stream_archives()
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
        self._stop_stream_archives()
        self._stop_tailscale()
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
            worker.stop_archive_due_to_storage(message)
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

    def _locate_sign_in_attempt(self, row_id: int, address: str) -> None:
        def found(location: str) -> None:
            self.accounts.set_sign_in_attempt_location(row_id, location)
            LOG.info("a wrong password or code came from %s, %s", address, location)

        self.address_locations.look_up(address, found)

    def uptime_seconds(self) -> float:
        """How long this run of NWR Stream Manager has been running."""
        return time.monotonic() - self.started_at

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
                "stream_archives": {} if read_only else self.stream_archives_status_locked(),
                "stream_archives_available": self._stream_archives_available_locked(),
                "test_mode": {"active": False} if read_only else self._stream_test_mode_status_locked(),
                "recent_eas_alerts": self._recent_eas_alerts_locked(),
                "storage": self._storage_status_snapshot_locked(),
                "iq_recorder": self._iq_recorder_status_locked(),
                "logs": self.log_handler.snapshot()[-80:],
                "instance_name": self.general.instance_name,
                "location": self.general.location,
                "uptime_seconds": self.uptime_seconds(),
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
        # From what each archive knows it holds, rather than walking thousands of files each time.
        archive_bytes = sum(
            self.stream_archive_store(stream).total_bytes()
            for stream in self.streams
            if stream_archive_directory(self.state_path.parent, str(stream.get("id", ""))).exists()
        )
        snapshot["usage"] = {
            "iq_recordings_bytes": iq_bytes,
            "iq_recordings": format_storage_bytes(iq_bytes),
            "stream_archives_bytes": archive_bytes,
            "stream_archives": format_storage_bytes(archive_bytes),
            "eas_recordings_bytes": eas_bytes,
            "eas_recordings": format_storage_bytes(eas_bytes),
        }
        return snapshot


class RtlControlHandler(
    AccountRoutesMixin,
    EasAlertRoutesMixin,
    StreamArchiveRoutesMixin,
    StreamRoutesMixin,
    LiveAudioRoutesMixin,
    IqRecorderRoutesMixin,
    SdrRoutesMixin,
    NotificationRoutesMixin,
    FallbackRoutesMixin,
    AdvancedRoutesMixin,
    TailscaleRoutesMixin,
    TwoStepHandlerMixin,
    SignInAlertHandlerMixin,
    RestartRoutesMixin,
    GeneralRoutesMixin,
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
    # (method, path) -> the method that answers it; set below, once the class exists.
    ROUTES: dict[tuple[str, str], str]

    def do_GET(self) -> None:
        self._answer("GET")

    def do_POST(self) -> None:
        self._answer("POST")

    def do_DELETE(self) -> None:
        self._answer("DELETE")

    def do_PATCH(self) -> None:
        self._answer("PATCH")

    def do_PUT(self) -> None:
        self._answer("PATCH")

    def _answer(self, method: str) -> None:
        """Answer a request with the method that ROUTES lists for it."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            # Where this body ends, and so where the next request starts, can't be known.
            self.close_connection = True
            length = 0
        stream, self.rfile = self.rfile, RequestBody(self.rfile, length)
        body = self.rfile
        try:
            parsed = urlparse(self.path)
            if self._redirect_to_tailnet_https():
                return
            if not self._auth_ok_or_setup_response(parsed.path, method):
                return
            name = self.ROUTES.get((method, parsed.path))
            if name is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            getattr(self, name)(parsed)
        finally:
            self.rfile = stream
            if not self.close_connection and not body.finish():
                self.close_connection = True

    @route("GET", "/")
    def _get_page(self, parsed: ParseResult) -> None:
        self._send_index_page(parsed)

    @route("GET", "/notification")
    def _get_notification_link(self, parsed: ParseResult) -> None:
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

    @route("GET", DASHBOARD_PARTS_PATH)
    def _get_dashboard_parts(self, parsed: ParseResult) -> None:
        # For pages without the page script that can still update the dashboard as it changes.
        self._send_json(self._dashboard_parts(self._current_account_is_read_only()))

    @route("GET", "/api/status")
    def _get_status(self, parsed: ParseResult) -> None:
        account = getattr(self, "current_account", None)
        read_only = isinstance(account, AccountRecord) and account.is_read_only
        if not read_only:
            self.service.heartbeat_soundcard_preview_stream(self._query_value(parsed, "soundcard_preview_id"))
        response = self.service.status(read_only=read_only)
        if isinstance(account, AccountRecord):
            response["account"] = account.to_public_dict()
            if account.is_read_only:
                response["iq_recorder"] = self.service.redacted_iq_recorder_status()
        self._send_json(response)

    @route("POST", "/api/client-log")
    def _post_client_log(self, parsed: ParseResult) -> None:
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
                    self._current_account_id(),
                )
            detail_text = json.dumps(details, sort_keys=True)[:1000] if isinstance(details, dict) else str(details)[:1000]
            # Without line breaks, so a message can't pass itself off as other lines of the log.
            log_message = one_log_line("client %s from %s: %s %s" % (area, self._client_address(), message, detail_text))
            if level in {"warning", "warn", "error"}:
                LOG.warning("%s", log_message)
            else:
                LOG.info("%s", log_message)
        except Exception as exc:
            LOG.warning("client log failed for %s: %s", self._client_address(), exc)
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json({"success": True})


RtlControlHandler.ROUTES = route_table(RtlControlHandler)


def one_log_line(text: str) -> str:
    """Text from a page, as one line of the log: line breaks and other control characters become spaces."""
    return "".join(" " if unicodedata.category(character) in {"Cc", "Zl", "Zp"} else character for character in text)


def port_argument(value: str) -> int:
    try:
        return validate_web_port(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} isn't a port number from 1 to 65535") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nwr-stream-manager",
        description="Run the NWR Stream Manager web interface.",
    )
    # Without any of these, the address and port come from Settings, under Advanced.
    address = parser.add_mutually_exclusive_group()
    address.add_argument(
        "--host",
        default=None,
        help="address to bind, overriding the Advanced settings; use 0.0.0.0 for access from your local network",
    )
    address.add_argument(
        "--localhost-only",
        action="store_true",
        help="only accept connections from this computer, overriding the Advanced settings",
    )
    address.add_argument(
        "--bind-to-all-interfaces",
        action="store_true",
        help="accept connections on every network address, overriding the Advanced settings",
    )
    parser.add_argument(
        "--port",
        type=port_argument,
        default=None,
        help=f"port to listen on, overriding the Advanced settings (normally {DEFAULT_WEB_PORT})",
    )
    parser.add_argument("--state", type=Path, default=default_state_path())
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="path for the rotating server log file; defaults to the state directory",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--build-libtailscale",
        action="store_true",
        help="build libtailscale, which connecting to Tailscale from Settings needs, then exit",
    )
    return parser


def owned_state_paths(state_path: Path, log_path: Path) -> list[Path]:
    """Everything NWR Stream Manager keeps, and nothing else that happens to share its folder."""
    folder = state_path.parent
    names = [
        STREAMS_STATE_FILE_NAME, FALLBACK_STATE_FILE_NAME, CUSTOM_FALLBACK_AUDIO_FILE_NAME, NOTIFICATION_STATE_FILE_NAME,
        TEST_MODE_TIME_FILE_NAME, SOUNDCARD_DEVICE_NAMES_FILE_NAME,
        STREAMS_DIRECTORY_NAME, IQ_RECORDINGS_DIRECTORY_NAME, IQ_TEST_SOURCES_DIRECTORY_NAME, REMOTE_SDR_DIRECTORY_NAME,
    ]
    database = folder / ACCOUNTS_DATABASE_FILE_NAME
    paths = [state_path, log_path, database, *(database.with_name(database.name + suffix) for suffix in ("-journal", "-wal", "-shm"))]
    paths += [folder / name for name in names]
    paths += [general_state_path(state_path), web_address_state_path(state_path)]
    return paths


def owned_state_folder(state_path: Path) -> Path | None:
    """The folder NWR Stream Manager keeps its files in, when it's one of its own rather than one chosen with --state."""
    folder = state_path.parent
    if folder.name == STATE_DIRECTORY_NAME or folder == systemd_directory_path("STATE_DIRECTORY"):
        return folder
    return None


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
    server: MicrohttpdServer,
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


def stop_web_server_soon(server: MicrohttpdServer) -> None:
    # A moment first, so the page asking for a restart hears that it's happening.
    timer = threading.Timer(RESTART_RESPONSE_SECONDS, server.shutdown)
    timer.name = "http-shutdown"
    timer.daemon = True
    timer.start()


def restore_signal_handlers(previous_handlers: dict[int, Any]) -> None:
    for signum, handler in previous_handlers.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


class WebServerBindError(RuntimeError):
    """The web interface's address or port can't be used."""


def bind_web_server(host: str, port: int) -> MicrohttpdServer:
    try:
        return MicrohttpdServer((host, port), RtlControlHandler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            message = (
                f"port {port} is already in use by another program, perhaps another copy of NWR Stream Manager. "
                "Stop that program, or choose another port with --port."
            )
        elif exc.errno == errno.EACCES:
            message = f"permission to use port {port} was denied. Ports below 1024 need extra permission; choose another port with --port."
        elif exc.errno == errno.EADDRNOTAVAIL:
            message = f"{host} isn't an address of this computer. Choose another address with --host, or use 0.0.0.0 for every address."
        else:
            message = f"the web interface couldn't start on {host} port {port}: {exc.strerror or exc}"
        raise WebServerBindError(message) from exc


def run_server(
    host: str | None,
    port: int | None,
    state_path: Path,
    verbose: bool = False,
    log_file: Path | None = None,
    *,
    localhost_only: bool = False,
    bind_to_all_interfaces: bool = False,
) -> bool:
    """Runs until shut down. Returns true when it was shut down to restart."""
    # Everything created from here on is for this account only, starting with the log.
    os.umask(OWNER_ONLY_UMASK)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log_path = log_file or default_log_path(state_path)
    configure_file_logging(log_path)
    make_owner_only(owned_state_paths(state_path, log_path))
    # Just the folder, not everything in it: once it's private, nobody else can reach what's inside.
    make_owner_only([], folder=owned_state_folder(state_path))
    try:
        dependency_results = check_startup_dependencies()
    except DependencyCheckError:
        LOG.exception("NWR Stream Manager cannot start because a dependency check failed")
        raise
    LOG.info("startup dependency check passed: %s", format_dependency_summary(dependency_results))
    address = resolve_web_address(
        load_web_address_settings(web_address_state_path(state_path)),
        host=host,
        port=port,
        localhost_only=localhost_only,
        bind_to_all_interfaces=bind_to_all_interfaces,
    )
    host, port = address.host, address.port
    for flag in (address.host_flag, address.port_flag):
        if flag:
            LOG.info("%s was given, so it's used instead of the Advanced settings", flag)
    # Before anything else starts, so a second copy can't take the RTL-SDR before it finds the port in use.
    try:
        server = bind_web_server(host, port)
    except WebServerBindError as exc:
        LOG.error("NWR Stream Manager cannot start: %s", exc)
        raise
    ring_handler = RingLogHandler()
    logging.getLogger().addHandler(ring_handler)
    development_iq_sources = development_iq_sources_available()
    try:
        service = RtlControlService(
            state_path,
            ring_handler,
            development_iq_sources_enabled=development_iq_sources,
        )
    except BaseException:
        server.server_close()
        raise
    service.web_host = host
    service.web_port = port
    service.web_address = address
    RtlControlHandler.service = service
    previous_signal_handlers = install_shutdown_signal_handlers(server, service.begin_shutdown)
    service.stop_web_server = lambda: stop_web_server_soon(server)
    LOG.info("RTL-SDR control web interface bound to %s:%s", host, port)
    for url in access_urls(host, port):
        LOG.info("RTL-SDR control web interface available at %s", url)
    LOG.info("RTL-SDR settings will be remembered in %s", state_path)
    LOG.info("server log file is %s", log_path)
    if development_iq_sources:
        LOG.info("development I/Q file source controls are enabled")
    service.start_tailscale(server)
    try:
        # Check for shutdown often, so a restart isn't held up waiting on this loop.
        server.serve_forever(poll_interval=0.1)
    finally:
        restore_signal_handlers(previous_signal_handlers)
        server.server_close()
        service.close()
    return service.restart_requested.is_set()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.build_libtailscale:
        return build_libtailscale()
    try:
        restart = run_server(
            args.host,
            args.port,
            args.state,
            args.verbose,
            args.log_file,
            localhost_only=args.localhost_only,
            bind_to_all_interfaces=args.bind_to_all_interfaces,
        )
    except (DependencyCheckError, WebServerBindError):
        return 1
    except KeyboardInterrupt:
        return 130
    if restart:
        return restart_process()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
