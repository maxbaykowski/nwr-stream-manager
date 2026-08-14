from __future__ import annotations

import argparse
import base64
import binascii
import hmac
import hashlib
from http.cookies import SimpleCookie
import json
import logging
import math
import mimetypes
import os
import queue
import re
import secrets
import signal
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

if __package__:
    from .audio_effects import AudioEffectsProcessor, deemphasis_makeup_gain
    from .config import (
        AUDIO_NYQUIST_HZ,
        AudioConfig,
        EasRecordingConfig,
        IcecastConfig,
        IQ_SAMPLE_RATE,
        parse_audio_config,
    )
    from .dsp import ComplexArray, IqChannelizer, complex64_to_interleaved_f32, create_decimator
    from .eas_recording import EasRecorderOutput
    from .encoder import PcmResampler, create_audio_encoder
    from .fallback_audio import load_fallback_audio
    from .icecast import IcecastSource
    from .nfm import float_to_s16
    from .rtl import (
        DEFAULT_RTL_SAMPLE_RATE,
        NWR_CENTER_FREQUENCY_HZ,
        IqDcBlocker,
        RtlConfig,
        RtlConfigError,
        RtlCaptureSource,
        RtlSampleBatch,
        list_rtl_devices,
        list_usb_rtl_devices,
        rtl_u8_to_complex64,
        validate_ppm_correction,
    )
    from .same_data import lookup_event, lookup_location
    from .webrtc import (
        AiortcSessionManager,
        WebRtcAsyncRunner,
        WebRtcAudioSource,
        WebRtcError,
        create_webrtc_pcm_audio_track,
        server_webrtc_capabilities,
    )
else:
    import importlib
    import types

    package_name = "nwr_stream_manager_runtime"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault(package_name, package)
    audio_effects = importlib.import_module(f"{package_name}.audio_effects")
    config_module = importlib.import_module(f"{package_name}.config")
    dsp = importlib.import_module(f"{package_name}.dsp")
    eas_recording = importlib.import_module(f"{package_name}.eas_recording")
    encoder = importlib.import_module(f"{package_name}.encoder")
    fallback_audio = importlib.import_module(f"{package_name}.fallback_audio")
    icecast_module = importlib.import_module(f"{package_name}.icecast")
    nfm = importlib.import_module(f"{package_name}.nfm")
    rtl = importlib.import_module(f"{package_name}.rtl")
    same_data = importlib.import_module(f"{package_name}.same_data")
    webrtc = importlib.import_module(f"{package_name}.webrtc")
    AudioEffectsProcessor = audio_effects.AudioEffectsProcessor
    deemphasis_makeup_gain = audio_effects.deemphasis_makeup_gain
    AUDIO_NYQUIST_HZ = config_module.AUDIO_NYQUIST_HZ
    AudioConfig = config_module.AudioConfig
    EasRecordingConfig = config_module.EasRecordingConfig
    IcecastConfig = config_module.IcecastConfig
    IQ_SAMPLE_RATE = config_module.IQ_SAMPLE_RATE
    parse_audio_config = config_module.parse_audio_config
    ComplexArray = dsp.ComplexArray
    IqChannelizer = dsp.IqChannelizer
    complex64_to_interleaved_f32 = dsp.complex64_to_interleaved_f32
    create_decimator = dsp.create_decimator
    EasRecorderOutput = eas_recording.EasRecorderOutput
    PcmResampler = encoder.PcmResampler
    create_audio_encoder = encoder.create_audio_encoder
    load_fallback_audio = fallback_audio.load_fallback_audio
    IcecastSource = icecast_module.IcecastSource
    float_to_s16 = nfm.float_to_s16
    DEFAULT_RTL_SAMPLE_RATE = rtl.DEFAULT_RTL_SAMPLE_RATE
    NWR_CENTER_FREQUENCY_HZ = rtl.NWR_CENTER_FREQUENCY_HZ
    IqDcBlocker = rtl.IqDcBlocker
    RtlConfig = rtl.RtlConfig
    RtlConfigError = rtl.RtlConfigError
    RtlCaptureSource = rtl.RtlCaptureSource
    RtlSampleBatch = rtl.RtlSampleBatch
    list_rtl_devices = rtl.list_rtl_devices
    list_usb_rtl_devices = rtl.list_usb_rtl_devices
    rtl_u8_to_complex64 = rtl.rtl_u8_to_complex64
    validate_ppm_correction = rtl.validate_ppm_correction
    lookup_event = same_data.lookup_event
    lookup_location = same_data.lookup_location
    AiortcSessionManager = webrtc.AiortcSessionManager
    WebRtcAsyncRunner = webrtc.WebRtcAsyncRunner
    WebRtcAudioSource = webrtc.WebRtcAudioSource
    WebRtcError = webrtc.WebRtcError
    create_webrtc_pcm_audio_track = webrtc.create_webrtc_pcm_audio_track
    server_webrtc_capabilities = webrtc.server_webrtc_capabilities

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from icecastauth import IcecastSettings, normalize_server, test_mountpoint_authentication

import numpy as np


LOG = logging.getLogger(__name__)
STATE_DIRECTORY_NAME = "nwr-stream-manager"
STATE_FILE_NAME = "rtl-control.json"
LOG_FILE_NAME = "nwr-stream-manager.log"
ACCOUNTS_DATABASE_FILE_NAME = "accounts.db"
AUTH_REALM = "NWR Stream Manager"
AUTH_SESSION_COOKIE_NAME = "nwrstmgr_session"
AUTH_SESSION_SECONDS = 12 * 60 * 60
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PASSWORD_RE = re.compile(r"^[!-~]{8,256}$")
SCRYPT_N = 16_384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
PASSWORD_HASH_SCHEME = "scrypt"
PASSWORD_HASH_VERSION = 1
ACCOUNT_ROLE_OWNER = "owner"
ACCOUNT_ROLE_ADMIN = "administrator"
ACCOUNT_ROLE_READ_ONLY = "read_only"
ACCOUNT_ROLES = {ACCOUNT_ROLE_OWNER, ACCOUNT_ROLE_ADMIN, ACCOUNT_ROLE_READ_ONLY}
STREAMS_STATE_FILE_NAME = "streams.json"
STREAMS_DIRECTORY_NAME = "streams"
STREAM_CONFIG_FILE_NAME = "config.json"
STREAMS_DIRECTORY_MARKER_FILE_NAME = ".per-stream-configs"
IQ_RECORDINGS_DIRECTORY_NAME = "iq-recordings"
IQ_RECORDINGS_INDEX_FILE_NAME = "index.json"
STORAGE_IDLE_POLL_SECONDS = 30.0
STORAGE_RECORDING_POLL_SECONDS = 2.0
STORAGE_LOW_FREE_BYTES = 1_000_000_000
STORAGE_CRITICAL_FREE_BYTES = 256_000_000
STORAGE_LOW_FREE_PERCENT = 5.0
STORAGE_CRITICAL_FREE_PERCENT = 1.0
STORAGE_LOW_INODES = 1024
STORAGE_CRITICAL_INODES = 128
STATIONS_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "nwr_stations.json"
USB_VENDOR_NAMES = {
    "0bda": "Realtek",
}
DEFAULT_STREAM_SAMPLE_RATE = 24_000
DEFAULT_STREAM_BITRATES = {
    "mp3": 64,
    "ogg": 48,
}
STREAM_SERVICE_CUSTOM = "custom"
STREAM_SERVICE_GWES = "gwes"
STREAM_SERVICE_WEATHERUSA = "weatherusa"
STREAM_SERVICE_NWRORG = "nwrorg"
STREAM_SERVICE_NAMES = {
    STREAM_SERVICE_CUSTOM: "Custom Icecast server",
    STREAM_SERVICE_GWES: "GWES Weather Radio",
    STREAM_SERVICE_WEATHERUSA: "WeatherUSA",
    STREAM_SERVICE_NWRORG: "NOAA Weather Radio Org",
}
STREAM_SERVICE_HOSTS = {
    "ingest.wxr.gwes-cdn.net": STREAM_SERVICE_GWES,
    "radio-master.weatherusa.net": STREAM_SERVICE_WEATHERUSA,
    "wxradio.org": STREAM_SERVICE_NWRORG,
}
STREAM_SAMPLE_RATES = {8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000}
STREAM_FRAME_SECONDS = 0.02
STREAM_FRAME_SAMPLES = round(IQ_SAMPLE_RATE * STREAM_FRAME_SECONDS)
STREAM_FRAME_BYTES = STREAM_FRAME_SAMPLES * 2
STREAM_SILENCE_FRAME = b"\x00" * STREAM_FRAME_BYTES
STREAM_WORKER_RAW_QUEUE_SECONDS = 1.50
STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS = 8
STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS = 96
LIVE_IQ_QUEUE_CHUNKS = 2
INTERMEDIATE_SOURCE_QUEUE_CHUNKS = 2
IQ_RECORDER_QUEUE_CHUNKS = 2
INTERMEDIATE_IQ_SAMPLE_RATE = 192_000
INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ = 18_000.0
INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB = 70.0
IQ_RECORDER_RAW_QUEUE_SECONDS = 2.0
IQ_DOWNLOAD_CHUNK_BYTES = 256 * 1024
IQ_DOWNLOAD_YIELD_SECONDS = 0.001
IQ_RECORDER_MODE_STREAM = "stream"
IQ_RECORDER_MODE_SPECTRUM = "spectrum"
IQ_RECORDER_SAMPLE_RATES = (192_000, 256_000, 384_000, 512_000, 768_000, 1_024_000, DEFAULT_RTL_SAMPLE_RATE)
IQ_RECORDER_DEFAULT_DURATION_SECONDS = 300
IQ_RECORDER_MIN_DURATION_SECONDS = 0
IQ_RECORDER_MAX_DURATION_SECONDS = 24 * 60 * 60
IQ_STORAGE_ESTIMATE_MIN_CORRECTION_SECONDS = 30.0
IQ_STORAGE_ESTIMATE_MAX_CORRECTION_SECONDS = 300.0
IQ_STORAGE_ESTIMATE_CORRECTION_FRACTION = 0.002
STREAM_IDLE_DETECTION_SECONDS = 1.0
STREAM_RECONNECT_SECONDS = 5.0
ICECAST_AUTH_CACHE_SECONDS = 600.0
MAX_JSON_REQUEST_BYTES = 1_048_576
RECENT_EAS_ALERT_SECONDS = 24 * 60 * 60
RECENT_EAS_ALERT_LIMIT = 10
NWR_RECEIVER_CHANNELS_HZ = (
    162_400_000,
    162_425_000,
    162_450_000,
    162_475_000,
    162_500_000,
    162_525_000,
    162_550_000,
)
RTL_DIAGNOSTIC_SECONDS = 1.0
RTL_DIAGNOSTIC_QUEUE_SECONDS = 1.25
RTL_DIAGNOSTIC_MIN_BATCHES = 2
RTL_DIAGNOSTIC_MAX_BATCHES = 64
RTL_DIAGNOSTIC_RAW_FFT_SIZE = 65_536
RTL_DIAGNOSTIC_CHANNEL_FFT_SIZE = 2_048
RECEIVER_AUDIO_CONFIG = parse_audio_config(
    {
        "deemphasis": {"enabled": True, "tau": 300},
        "comfort_noise": {"enabled": False, "level_db": -40},
        "volume": {"enabled": False, "multiplier": 1.0},
        "highpass": {"enabled": False, "frequency": 300, "sharpness": 0},
        "lowpass": {"enabled": True, "frequency": 3400, "sharpness": 2},
        "notch": {"enabled": False, "frequency": 3000, "sharpness": 0},
    }
)
FALLBACK_STATE_FILE_NAME = "fallback.json"


@dataclass(frozen=True)
class RtlControlSettings:
    serial: str = ""
    sample_rate: int = DEFAULT_RTL_SAMPLE_RATE
    gain: float | None = None
    ppm_correction: int = 0
    bias_tee: bool = False

    def to_rtl_config(self) -> RtlConfig:
        if not self.serial:
            raise RtlConfigError("select an RTL-SDR before starting capture")
        return RtlConfig(
            serial=self.serial,
            sample_rate=DEFAULT_RTL_SAMPLE_RATE,
            center_frequency_hz=NWR_CENTER_FREQUENCY_HZ,
            ppm_correction=validate_ppm_correction(self.ppm_correction),
            gain=self.gain,
            bias_tee=bool(self.bias_tee),
        )


@dataclass(frozen=True)
class WebFallbackSettings:
    enabled: bool = True
    silence_timeout_seconds: float = 30.0
    loop_delay_seconds: float = 5.0


@dataclass
class WebFallbackPlaybackState:
    position: int = 0
    delay_samples_remaining: int = 0
    active: bool = False

    def reset(self) -> None:
        self.position = 0
        self.delay_samples_remaining = 0
        self.active = False


@dataclass(frozen=True)
class WebEasRecordingSettings:
    enabled: bool = False
    pre_seconds: float = 2.0
    post_seconds: float = 5.0
    max_seconds: int = 120
    format: str = "wav"


@dataclass(frozen=True)
class IqSampleBatch:
    data: ComplexArray
    sample_rate: int
    center_frequency_hz: int
    captured_at: float = field(default_factory=time.monotonic)


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


class StorageMonitor:
    def __init__(
        self,
        paths: list[Path] | None = None,
        *,
        statvfs_provider=None,
        stat_provider=None,
        critical_callback=None,
        idle_poll_seconds: float = STORAGE_IDLE_POLL_SECONDS,
        recording_poll_seconds: float = STORAGE_RECORDING_POLL_SECONDS,
    ) -> None:
        self.paths = {Path(path).expanduser() for path in (paths or [])}
        self.statvfs_provider = statvfs_provider or os.statvfs
        self.stat_provider = stat_provider or os.stat
        self.critical_callback = critical_callback
        self.idle_poll_seconds = float(idle_poll_seconds)
        self.recording_poll_seconds = float(recording_poll_seconds)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_recordings = 0
        self.snapshot_data: dict[str, Any] = self._empty_snapshot()
        self.last_callback_status = ""

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.refresh()
        self.thread = threading.Thread(target=self._run, name="storage-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def add_path(self, path: Path) -> None:
        with self.lock:
            self.paths.add(Path(path).expanduser())
        self.refresh()

    def recording_started(self, path: Path | None = None) -> None:
        if path is not None:
            with self.lock:
                self.paths.add(Path(path).expanduser())
        with self.lock:
            self.active_recordings += 1
        self.refresh()

    def recording_stopped(self) -> None:
        with self.lock:
            self.active_recordings = max(0, self.active_recordings - 1)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.snapshot_data))

    def is_critical(self, path: Path | None = None) -> bool:
        snapshot = self.snapshot()
        if path is not None:
            target_device = self._device_id_for_path(Path(path))
            if target_device is not None:
                for filesystem in snapshot.get("filesystems", []):
                    if filesystem.get("device_id") == target_device:
                        return filesystem.get("status") == "critical"
        return snapshot.get("status") == "critical"

    def refresh(self) -> dict[str, Any]:
        with self.lock:
            paths = sorted(self.paths, key=lambda item: str(item))
            active_recordings = self.active_recordings
        filesystems_by_device: dict[int | str, dict[str, Any]] = {}
        errors: list[str] = []
        for path in paths:
            existing_path = self._nearest_existing_path(path)
            try:
                stat_result = self.stat_provider(existing_path)
                stats = self.statvfs_provider(existing_path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            filesystem = self._filesystem_snapshot(path, existing_path, stat_result, stats)
            device_id = filesystem["device_id"]
            current = filesystems_by_device.get(device_id)
            if current is None or len(str(filesystem["path"])) < len(str(current["path"])):
                filesystems_by_device[device_id] = filesystem
        filesystems = sorted(filesystems_by_device.values(), key=lambda item: str(item["path"]))
        status = self._aggregate_status(filesystems, errors)
        snapshot = {
            "status": status,
            "message": storage_status_message(status),
            "active_recordings": active_recordings,
            "poll_seconds": self.recording_poll_seconds if active_recordings > 0 else self.idle_poll_seconds,
            "filesystems": filesystems,
            "errors": errors,
            "updated_at": time.time(),
        }
        with self.lock:
            self.snapshot_data = snapshot
        return snapshot

    def _run(self) -> None:
        while not self.stop_event.is_set():
            snapshot = self.refresh()
            if snapshot.get("status") == "critical":
                LOG.warning("storage is critically low; recording should stop until space is freed")
                if self.critical_callback is not None and self.last_callback_status != "critical":
                    self.last_callback_status = "critical"
                    try:
                        self.critical_callback(snapshot)
                    except Exception as exc:
                        LOG.warning("storage critical callback failed: %s", exc)
            else:
                self.last_callback_status = str(snapshot.get("status", ""))
            poll_seconds = float(snapshot.get("poll_seconds", self.idle_poll_seconds))
            self.stop_event.wait(max(0.5, poll_seconds))

    def _device_id_for_path(self, path: Path) -> int | None:
        try:
            return int(self.stat_provider(self._nearest_existing_path(path)).st_dev)
        except OSError:
            return None

    def _nearest_existing_path(self, path: Path) -> Path:
        candidate = path.expanduser()
        while not candidate.exists() and candidate.parent != candidate:
            candidate = candidate.parent
        return candidate

    def _filesystem_snapshot(self, path: Path, existing_path: Path, stat_result, stats) -> dict[str, Any]:
        fragment_size = int(getattr(stats, "f_frsize", 0) or getattr(stats, "f_bsize", 0) or 1)
        total_bytes = int(stats.f_blocks) * fragment_size
        free_bytes = int(stats.f_bfree) * fragment_size
        available_bytes = int(stats.f_bavail) * fragment_size
        used_bytes = max(0, total_bytes - free_bytes)
        used_percent = (used_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
        available_percent = (available_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
        available_inodes = int(getattr(stats, "f_favail", 0))
        status = storage_filesystem_status(available_bytes, available_percent, available_inodes)
        return {
            "path": str(path),
            "stat_path": str(existing_path),
            "device_id": int(stat_result.st_dev),
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "available_bytes": available_bytes,
            "used_percent": round(used_percent, 1),
            "available_percent": round(available_percent, 1),
            "available_inodes": available_inodes,
            "status": status,
            "summary": storage_filesystem_summary(used_bytes, total_bytes, used_percent),
        }

    @staticmethod
    def _aggregate_status(filesystems: list[dict[str, Any]], errors: list[str]) -> str:
        if any(item.get("status") == "critical" for item in filesystems):
            return "critical"
        if any(item.get("status") == "low" for item in filesystems):
            return "low"
        if errors and not filesystems:
            return "unknown"
        return "ok"

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        return {
            "status": "unknown",
            "message": "",
            "active_recordings": 0,
            "poll_seconds": STORAGE_IDLE_POLL_SECONDS,
            "filesystems": [],
            "errors": [],
            "updated_at": 0.0,
        }


def storage_filesystem_status(
    available_bytes: int,
    available_percent: float,
    available_inodes: int,
) -> str:
    if (
        available_bytes <= STORAGE_CRITICAL_FREE_BYTES
        or available_percent <= STORAGE_CRITICAL_FREE_PERCENT
        or 0 < available_inodes <= STORAGE_CRITICAL_INODES
    ):
        return "critical"
    if (
        available_bytes <= STORAGE_LOW_FREE_BYTES
        or available_percent <= STORAGE_LOW_FREE_PERCENT
        or 0 < available_inodes <= STORAGE_LOW_INODES
    ):
        return "low"
    return "ok"


def storage_status_message(status: str) -> str:
    if status == "critical":
        return "Storage is critically low. Recording has been stopped. Please free up disk space."
    if status == "low":
        return "Storage is running low. Please free up disk space."
    if status == "unknown":
        return "Storage could not be checked."
    return ""


def storage_filesystem_summary(used_bytes: int, total_bytes: int, used_percent: float) -> str:
    return f"Storage: {format_storage_bytes(used_bytes)} used of {format_storage_bytes(total_bytes)} ({used_percent:.0f}%)"


def format_storage_bytes(value: int | float) -> str:
    value = float(max(0.0, value))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    unit_index = 0
    while value >= 1000.0 and unit_index < len(units) - 1:
        value /= 1000.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


class RawRtlFanout:
    def __init__(self, source: RtlCaptureSource) -> None:
        self.source = source
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_names: dict[queue.Queue, str] = {}
        self.subscriber_drops: dict[queue.Queue, int] = {}
        self.subscriber_drop_bytes: dict[queue.Queue, int] = {}
        self.subscriber_max_depth: dict[queue.Queue, int] = {}
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.read_batches = 0
        self.read_bytes = 0
        self.total_dropped_batches = 0
        self.total_dropped_bytes = 0
        self.last_read_at = 0.0
        self.last_drop_log_at = 0.0

    def subscribe(
        self,
        max_chunks: int | None = None,
        max_seconds: float | None = None,
        name: str = "subscriber",
    ) -> queue.Queue:
        if max_chunks is None:
            max_chunks = self._chunks_for_seconds(max_seconds or STREAM_WORKER_RAW_QUEUE_SECONDS)
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
            self.subscriber_names[subscriber] = name
            self.subscriber_drops[subscriber] = 0
            self.subscriber_drop_bytes[subscriber] = 0
            self.subscriber_max_depth[subscriber] = 0
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)
            self.subscriber_names.pop(subscriber, None)
            self.subscriber_drops.pop(subscriber, None)
            self.subscriber_drop_bytes.pop(subscriber, None)
            self.subscriber_max_depth.pop(subscriber, None)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="rtl-raw-fanout", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def _chunks_for_seconds(self, seconds: float) -> int:
        sample_rate = max(1, int(self.source.config.sample_rate))
        chunk_bytes = max(512, self.source._rtl_async_buffer_size(self.source.config))
        chunk_seconds = max(0.001, chunk_bytes / (2.0 * float(sample_rate)))
        return max(
            STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS,
            min(
                STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS,
                int(math.ceil(max(0.0, seconds) / chunk_seconds)),
            ),
        )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                batch = self.source.read(timeout=0.5)
            except queue.Empty:
                continue
            except EOFError:
                return
            except Exception as exc:
                LOG.warning("RTL-SDR raw fanout read failed: %s", exc)
                continue
            with self.subscribers_lock:
                subscribers = list(self.subscribers)
                self.read_batches += 1
                self.read_bytes += len(batch.data)
                self.last_read_at = time.monotonic()
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait(batch)
                    self._record_subscriber_depth(subscriber)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass
                    self._record_subscriber_drop(subscriber, batch)
                    try:
                        subscriber.put_nowait(batch)
                        self._record_subscriber_depth(subscriber)
                    except queue.Full:
                        self._record_subscriber_drop(subscriber, batch)
                        pass

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        if subscriber is None:
            return {}
        with self.subscribers_lock:
            return self._subscriber_stats_locked(subscriber)

    def stats(self) -> dict[str, Any]:
        with self.subscribers_lock:
            subscribers = [self._subscriber_stats_locked(subscriber) for subscriber in self.subscribers]
            return {
                "read_batches": self.read_batches,
                "read_bytes": self.read_bytes,
                "read_samples": self.read_bytes // 2,
                "subscriber_count": len(self.subscribers),
                "total_dropped_batches": self.total_dropped_batches,
                "total_dropped_bytes": self.total_dropped_bytes,
                "total_dropped_samples": self.total_dropped_bytes // 2,
                "last_read_age_seconds": (
                    round(time.monotonic() - self.last_read_at, 3)
                    if self.last_read_at
                    else None
                ),
                "subscribers": subscribers,
            }

    def _subscriber_stats_locked(self, subscriber: queue.Queue) -> dict[str, Any]:
        return {
            "name": self.subscriber_names.get(subscriber, "subscriber"),
            "queue_depth": subscriber.qsize(),
            "queue_capacity": subscriber.maxsize,
            "max_queue_depth": self.subscriber_max_depth.get(subscriber, 0),
            "dropped_batches": self.subscriber_drops.get(subscriber, 0),
            "dropped_bytes": self.subscriber_drop_bytes.get(subscriber, 0),
            "dropped_samples": self.subscriber_drop_bytes.get(subscriber, 0) // 2,
        }

    def _record_subscriber_depth(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_max_depth[subscriber] = max(
                self.subscriber_max_depth.get(subscriber, 0),
                subscriber.qsize(),
            )

    def _record_subscriber_drop(self, subscriber: queue.Queue, batch: RtlSampleBatch) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_drops[subscriber] = self.subscriber_drops.get(subscriber, 0) + 1
            self.subscriber_drop_bytes[subscriber] = self.subscriber_drop_bytes.get(subscriber, 0) + len(batch.data)
            self.total_dropped_batches += 1
            self.total_dropped_bytes += len(batch.data)
            name = self.subscriber_names.get(subscriber, "subscriber")
            drops = self.subscriber_drops[subscriber]
            now = time.monotonic()
            should_log = now - self.last_drop_log_at >= 60.0
            if should_log:
                self.last_drop_log_at = now
        if should_log:
            LOG.warning(
                "RTL-SDR raw fanout is dropping IQ batches for %s: subscriber_drops=%s total_drops=%s",
                name,
                drops,
                self.total_dropped_batches,
            )


class IntermediateIqFanout:
    def __init__(self, raw_fanout: RawRtlFanout, output_rate: int = INTERMEDIATE_IQ_SAMPLE_RATE) -> None:
        self.raw_fanout = raw_fanout
        self.output_rate = int(output_rate)
        self.raw_queue = subscribe_raw_fanout(
            raw_fanout,
            max_chunks=INTERMEDIATE_SOURCE_QUEUE_CHUNKS,
            name="iq-intermediate-source",
        )
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_names: dict[queue.Queue, str] = {}
        self.subscriber_drops: dict[queue.Queue, int] = {}
        self.subscriber_drop_samples: dict[queue.Queue, int] = {}
        self.subscriber_max_depth: dict[queue.Queue, int] = {}
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.read_batches = 0
        self.read_samples = 0
        self.output_batches = 0
        self.output_samples = 0
        self.total_dropped_batches = 0
        self.total_dropped_samples = 0
        self.last_output_at = 0.0
        self.last_drop_log_at = 0.0

    def subscribe(
        self,
        max_chunks: int | None = None,
        max_seconds: float | None = None,
        name: str = "subscriber",
    ) -> queue.Queue:
        if max_chunks is None:
            max_chunks = max(
                STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS,
                min(STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS, int(math.ceil((max_seconds or STREAM_WORKER_RAW_QUEUE_SECONDS) / 0.02))),
            )
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
            self.subscriber_names[subscriber] = name
            self.subscriber_drops[subscriber] = 0
            self.subscriber_drop_samples[subscriber] = 0
            self.subscriber_max_depth[subscriber] = 0
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)
            self.subscriber_names.pop(subscriber, None)
            self.subscriber_drops.pop(subscriber, None)
            self.subscriber_drop_samples.pop(subscriber, None)
            self.subscriber_max_depth.pop(subscriber, None)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="iq-intermediate-fanout", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.raw_fanout.unsubscribe(self.raw_queue)
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def stats(self) -> dict[str, Any]:
        with self.subscribers_lock:
            subscribers = [self._subscriber_stats_locked(subscriber) for subscriber in self.subscribers]
            return {
                "sample_rate": self.output_rate,
                "read_batches": self.read_batches,
                "read_samples": self.read_samples,
                "output_batches": self.output_batches,
                "output_samples": self.output_samples,
                "subscriber_count": len(self.subscribers),
                "total_dropped_batches": self.total_dropped_batches,
                "total_dropped_samples": self.total_dropped_samples,
                "last_output_age_seconds": (
                    round(time.monotonic() - self.last_output_at, 3)
                    if self.last_output_at
                    else None
                ),
                "subscribers": subscribers,
            }

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        if subscriber is None:
            return {}
        with self.subscribers_lock:
            return self._subscriber_stats_locked(subscriber)

    def _run(self) -> None:
        dc_blocker: IqDcBlocker | None = None
        decimator = None
        decimator_key: tuple[int, int] | None = None
        while not self.stop_event.is_set():
            try:
                raw_batch: RtlSampleBatch = self.raw_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                LOG.warning("intermediate IQ fanout failed to read RTL-SDR samples: %s", exc)
                continue
            key = (raw_batch.sample_rate, self.output_rate)
            if decimator is None or decimator_key != key:
                dc_blocker = IqDcBlocker(raw_batch.sample_rate)
                decimator = create_decimator(
                    raw_batch.sample_rate,
                    self.output_rate,
                    transition_hz=INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
                    attenuation_db=INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
                )
                decimator_key = key
            iq = rtl_u8_to_complex64(raw_batch.data)
            if iq.size == 0:
                continue
            if dc_blocker is None:
                dc_blocker = IqDcBlocker(raw_batch.sample_rate)
            output = decimator.process(dc_blocker.process(iq))
            with self.subscribers_lock:
                self.read_batches += 1
                self.read_samples += int(iq.size)
            if output.size == 0:
                continue
            batch = IqSampleBatch(
                data=output,
                sample_rate=self.output_rate,
                center_frequency_hz=raw_batch.center_frequency_hz,
                captured_at=raw_batch.captured_at,
            )
            self._publish(batch)

    def _publish(self, batch: IqSampleBatch) -> None:
        with self.subscribers_lock:
            subscribers = list(self.subscribers)
            self.output_batches += 1
            self.output_samples += int(batch.data.size)
            self.last_output_at = time.monotonic()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(batch)
                self._record_subscriber_depth(subscriber)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                self._record_subscriber_drop(subscriber, batch)
                try:
                    subscriber.put_nowait(batch)
                    self._record_subscriber_depth(subscriber)
                except queue.Full:
                    self._record_subscriber_drop(subscriber, batch)

    def _subscriber_stats_locked(self, subscriber: queue.Queue) -> dict[str, Any]:
        return {
            "name": self.subscriber_names.get(subscriber, "subscriber"),
            "queue_depth": subscriber.qsize(),
            "queue_capacity": subscriber.maxsize,
            "max_queue_depth": self.subscriber_max_depth.get(subscriber, 0),
            "dropped_batches": self.subscriber_drops.get(subscriber, 0),
            "dropped_samples": self.subscriber_drop_samples.get(subscriber, 0),
        }

    def _record_subscriber_depth(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_max_depth[subscriber] = max(
                self.subscriber_max_depth.get(subscriber, 0),
                subscriber.qsize(),
            )

    def _record_subscriber_drop(self, subscriber: queue.Queue, batch: IqSampleBatch) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_drops[subscriber] = self.subscriber_drops.get(subscriber, 0) + 1
            self.subscriber_drop_samples[subscriber] = self.subscriber_drop_samples.get(subscriber, 0) + int(batch.data.size)
            self.total_dropped_batches += 1
            self.total_dropped_samples += int(batch.data.size)
            name = self.subscriber_names.get(subscriber, "subscriber")
            drops = self.subscriber_drops[subscriber]
            now = time.monotonic()
            should_log = now - self.last_drop_log_at >= 60.0
            if should_log:
                self.last_drop_log_at = now
        if should_log:
            LOG.warning(
                "intermediate IQ fanout is dropping batches for %s: subscriber_drops=%s total_drops=%s",
                name,
                drops,
                self.total_dropped_batches,
            )


def subscribe_raw_fanout(
    fanout,
    *,
    max_chunks: int | None = None,
    max_seconds: float | None = None,
    name: str = "subscriber",
) -> queue.Queue:
    try:
        return fanout.subscribe(max_chunks=max_chunks, max_seconds=max_seconds, name=name)
    except TypeError:
        return fanout.subscribe(max_chunks=max_chunks, max_seconds=max_seconds)


def iq_batch_complex(batch: RtlSampleBatch | IqSampleBatch) -> ComplexArray:
    data = batch.data
    if isinstance(data, np.ndarray):
        return data.astype(np.complex64, copy=False)
    return rtl_u8_to_complex64(data)


class ComplexNfmDemodulator:
    def __init__(self) -> None:
        self.previous_sample: np.complex64 | None = None

    def process(self, iq: np.ndarray) -> np.ndarray:
        if len(iq) == 0:
            return np.array([], dtype=np.float32)
        if len(iq) < 2 and self.previous_sample is None:
            self.previous_sample = iq[-1]
            return np.array([], dtype=np.float32)
        if self.previous_sample is None:
            output = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
        else:
            output = np.empty(len(iq), dtype=np.float32)
            output[0] = np.angle(iq[0] * np.conj(self.previous_sample))
            if len(iq) > 1:
                output[1:] = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
        self.previous_sample = iq[-1]
        return (output / np.pi * 1.5).astype(np.float32, copy=False)


def rms_float(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    values = samples.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(values * values)))


def rms_complex(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    magnitudes = np.abs(samples.astype(np.complex64, copy=False)).astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(magnitudes * magnitudes)))


def peak_float(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


def raw_iq_diagnostics(iq: ComplexArray, sample_rate: int, center_frequency_hz: int, raw: bytes = b"") -> dict[str, Any]:
    if iq.size == 0:
        return {
            "sample_count": 0,
            "raw_byte_min": None,
            "raw_byte_max": None,
            "raw_byte_mean": None,
            "raw_byte_stddev": None,
            "dc_magnitude": 0.0,
            "rms": 0.0,
            "peak_magnitude": 0.0,
            "real_near_full_scale_fraction": 0.0,
            "imag_near_full_scale_fraction": 0.0,
            "channels": [],
        }
    sample_count = min(int(iq.size), RTL_DIAGNOSTIC_RAW_FFT_SIZE)
    window_iq = iq[-sample_count:].astype(np.complex64, copy=False)
    spectrum_window = np.hanning(sample_count).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft(window_iq * spectrum_window))
    frequencies = np.fft.fftshift(np.fft.fftfreq(sample_count, d=1.0 / float(sample_rate)))
    power = (np.abs(spectrum) ** 2).astype(np.float64, copy=False)
    floor = float(np.median(power)) if power.size else 0.0
    channels = []
    for frequency_hz in NWR_RECEIVER_CHANNELS_HZ:
        offset_hz = float(frequency_hz - center_frequency_hz)
        nearby = np.abs(frequencies - offset_hz) <= 6_000.0
        local_power = float(np.max(power[nearby])) if np.any(nearby) else 0.0
        channels.append(
            {
                "frequency_hz": frequency_hz,
                "frequency_mhz": receiver_frequency_mhz(frequency_hz),
                "offset_hz": offset_hz,
                "raw_power_db": power_db(local_power),
                "raw_snr_db": power_db(local_power / floor) if floor > 0.0 else None,
            }
        )
    raw_bytes = np.frombuffer(raw, dtype=np.uint8) if raw else np.array([], dtype=np.uint8)
    return {
        "sample_count": int(iq.size),
        "analysis_sample_count": sample_count,
        "raw_byte_min": int(raw_bytes.min()) if raw_bytes.size else None,
        "raw_byte_max": int(raw_bytes.max()) if raw_bytes.size else None,
        "raw_byte_mean": float(raw_bytes.mean()) if raw_bytes.size else None,
        "raw_byte_stddev": float(raw_bytes.std()) if raw_bytes.size else None,
        "dc_magnitude": float(abs(np.mean(iq, dtype=np.complex128))),
        "rms": rms_complex(iq),
        "peak_magnitude": float(np.max(np.abs(iq))),
        "real_near_full_scale_fraction": float(np.mean(np.abs(iq.real) > 0.98)),
        "imag_near_full_scale_fraction": float(np.mean(np.abs(iq.imag) > 0.98)),
        "channels": channels,
    }


def channel_audio_diagnostics(
    iq: ComplexArray,
    sample_rate: int,
    center_frequency_hz: int,
    frequency_hz: int,
) -> dict[str, Any]:
    dc_blocker = IqDcBlocker(sample_rate)
    channelizer = IqChannelizer(
        input_rate=sample_rate,
        center_frequency_hz=center_frequency_hz,
        target_frequency_hz=frequency_hz,
        output_rate=IQ_SAMPLE_RATE,
    )
    demodulator = ComplexNfmDemodulator()
    channel_iq = channelizer.process_complex(dc_blocker.process(iq))
    audio = demodulator.process(channel_iq)
    tone_peak_hz = None
    if audio.size >= 64:
        count = min(int(audio.size), RTL_DIAGNOSTIC_CHANNEL_FFT_SIZE)
        audio_window = audio[-count:] * np.hanning(count).astype(np.float32)
        spectrum = np.fft.rfft(audio_window)
        frequencies = np.fft.rfftfreq(count, d=1.0 / float(IQ_SAMPLE_RATE))
        if spectrum.size > 1:
            tone_peak_hz = float(frequencies[int(np.argmax(np.abs(spectrum[1:])) + 1)])
    return {
        "frequency_hz": frequency_hz,
        "frequency_mhz": receiver_frequency_mhz(frequency_hz),
        "offset_hz": float(frequency_hz - center_frequency_hz),
        "channelizer_mode": channelizer.mode,
        "channel_iq_samples": int(channel_iq.size),
        "channel_iq_rms": rms_complex(channel_iq),
        "channel_iq_peak": float(np.max(np.abs(channel_iq))) if channel_iq.size else 0.0,
        "audio_samples": int(audio.size),
        "audio_rms": rms_float(audio),
        "audio_peak": peak_float(audio),
        "audio_peak_hz": tone_peak_hz,
    }


def power_db(value: float) -> float | None:
    if value <= 0.0 or not math.isfinite(value):
        return None
    return float(10.0 * math.log10(value))


class FloatFrameBuffer:
    def __init__(self, frame_samples: int) -> None:
        self.frame_samples = frame_samples
        self.pending = np.empty(0, dtype=np.float32)
        self.offset = 0

    def clear(self) -> None:
        self.pending = np.empty(0, dtype=np.float32)
        self.offset = 0

    def push(self, samples: np.ndarray):
        samples = samples.astype(np.float32, copy=False)
        if self.offset:
            self.pending = self.pending[self.offset :]
            self.offset = 0
        self.pending = samples if len(self.pending) == 0 else np.concatenate((self.pending, samples))
        while len(self.pending) - self.offset >= self.frame_samples:
            frame = self.pending[self.offset : self.offset + self.frame_samples]
            self.offset += self.frame_samples
            yield frame
        if self.offset and self.offset >= len(self.pending):
            self.clear()


def load_web_fallback_audio():
    audio = load_fallback_audio(None)
    if audio.sample_rate == IQ_SAMPLE_RATE:
        return audio
    resampler = PcmResampler(audio.sample_rate, IQ_SAMPLE_RATE)
    pcm = resampler.process(audio.pcm) + resampler.flush()
    return replace(
        audio,
        sample_rate=IQ_SAMPLE_RATE,
        pcm=pcm,
        duration_seconds=len(pcm) / 2 / IQ_SAMPLE_RATE,
    )


def next_web_fallback_frame(audio, state: WebFallbackPlaybackState, loop_delay_seconds: float) -> bytes:
    if not audio.pcm:
        return STREAM_SILENCE_FRAME
    output = bytearray()
    delay_samples = round(max(0.0, loop_delay_seconds) * IQ_SAMPLE_RATE)
    while len(output) < STREAM_FRAME_BYTES:
        if state.delay_samples_remaining > 0:
            remaining_samples = (STREAM_FRAME_BYTES - len(output)) // 2
            silence_samples = min(remaining_samples, state.delay_samples_remaining)
            output.extend(b"\x00\x00" * silence_samples)
            state.delay_samples_remaining -= silence_samples
            continue
        if state.position >= len(audio.pcm):
            state.position = 0
            if delay_samples > 0:
                state.delay_samples_remaining = delay_samples
                continue
        chunk_size = min(STREAM_FRAME_BYTES - len(output), len(audio.pcm) - state.position)
        output.extend(audio.pcm[state.position : state.position + chunk_size])
        state.position += chunk_size
    return bytes(output)


def validate_account_username(username: str) -> str:
    username = str(username).strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("username may contain only letters, numbers, hyphen, and underscore")
    return username


def validate_account_password(password: str) -> str:
    password = str(password)
    if not PASSWORD_RE.fullmatch(password):
        raise ValueError("password must be 8-256 printable non-space ASCII characters")
    return password


def hash_account_password(password: str) -> str:
    password = validate_account_password(password)
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "scrypt$v={}$n={}$r={}$p={}$dklen={}$salt={}$hash={}".format(
        PASSWORD_HASH_VERSION,
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        SCRYPT_DKLEN,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def parse_account_password_hash(encoded_hash: str) -> dict[str, Any]:
    parts = str(encoded_hash).split("$")
    if not parts or parts[0] != PASSWORD_HASH_SCHEME:
        raise ValueError("unsupported password hash scheme")
    if len(parts) == 6:
        _scheme, n_raw, r_raw, p_raw, salt_raw, digest_raw = parts
        return {
            "scheme": PASSWORD_HASH_SCHEME,
            "version": 0,
            "n": int(n_raw),
            "r": int(r_raw),
            "p": int(p_raw),
            "dklen": None,
            "salt": base64.b64decode(salt_raw.encode("ascii"), validate=True),
            "digest": base64.b64decode(digest_raw.encode("ascii"), validate=True),
        }
    values: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if not separator:
            raise ValueError("invalid password hash parameter")
        values[key] = value
    required = {"v", "n", "r", "p", "dklen", "salt", "hash"}
    if set(values) != required:
        raise ValueError("invalid password hash parameters")
    return {
        "scheme": PASSWORD_HASH_SCHEME,
        "version": int(values["v"]),
        "n": int(values["n"]),
        "r": int(values["r"]),
        "p": int(values["p"]),
        "dklen": int(values["dklen"]),
        "salt": base64.b64decode(values["salt"].encode("ascii"), validate=True),
        "digest": base64.b64decode(values["hash"].encode("ascii"), validate=True),
    }


def account_password_hash_needs_upgrade(encoded_hash: str) -> bool:
    try:
        parsed = parse_account_password_hash(encoded_hash)
    except (TypeError, ValueError, binascii.Error):
        return True
    return (
        int(parsed["version"]) < PASSWORD_HASH_VERSION
        or int(parsed["n"]) < SCRYPT_N
        or int(parsed["r"]) < SCRYPT_R
        or int(parsed["p"]) < SCRYPT_P
        or int(parsed["dklen"] or len(parsed["digest"])) < SCRYPT_DKLEN
    )


def verify_account_password(password: str, encoded_hash: str) -> bool:
    try:
        parsed = parse_account_password_hash(encoded_hash)
        expected = parsed["digest"]
        digest = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=parsed["salt"],
            n=int(parsed["n"]),
            r=int(parsed["r"]),
            p=int(parsed["p"]),
            dklen=len(expected),
        )
    except (TypeError, ValueError, binascii.Error):
        return False
    return hmac.compare_digest(digest, expected)


@dataclass(frozen=True)
class AccountRecord:
    id: int
    username: str
    role: str
    must_change_password: bool
    created_at: float
    last_accessed_at: float | None

    @property
    def is_owner(self) -> bool:
        return self.role == ACCOUNT_ROLE_OWNER

    @property
    def is_read_only(self) -> bool:
        return self.role == ACCOUNT_ROLE_READ_ONLY

    def to_public_dict(self) -> dict[str, Any]:
        if self.role == ACCOUNT_ROLE_OWNER:
            account_type = "Owner"
        elif self.role == ACCOUNT_ROLE_READ_ONLY:
            account_type = "Read-only"
        else:
            account_type = "Administrator"
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "account_type": account_type,
            "read_only": self.is_read_only,
            "owner": self.is_owner,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
        }


@dataclass
class AuthSession:
    account_id: int
    expires_at: float


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
            ).fetchone()
            existing_sql = str(row["sql"]) if row is not None else ""
            if row is not None and ("CHECK (id = 1)" in existing_sql or "role" not in existing_sql):
                connection.execute("ALTER TABLE accounts RENAME TO accounts_legacy")
                connection.execute(
                    """
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'administrator',
                        must_change_password INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL DEFAULT 0,
                        last_accessed_at REAL
                    )
                    """
                )
                now = time.time()
                connection.execute(
                    """
                    INSERT INTO accounts (id, username, password, role, must_change_password, created_at, last_accessed_at)
                    SELECT id, username, password, 'owner', must_change_password, ?, NULL FROM accounts_legacy
                    """,
                    (now,),
                )
                connection.execute("DROP TABLE accounts_legacy")
                connection.commit()
                return
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'administrator',
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    last_accessed_at REAL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()}
            if "role" not in columns:
                connection.execute("ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'administrator'")
            if "created_at" not in columns:
                connection.execute("ALTER TABLE accounts ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
                connection.execute("UPDATE accounts SET created_at = ? WHERE created_at = 0", (time.time(),))
            if "last_accessed_at" not in columns:
                connection.execute("ALTER TABLE accounts ADD COLUMN last_accessed_at REAL")
            owner = connection.execute("SELECT id FROM accounts WHERE role = ? LIMIT 1", (ACCOUNT_ROLE_OWNER,)).fetchone()
            first = connection.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
            if owner is None and first is not None:
                connection.execute("UPDATE accounts SET role = ? WHERE id = ?", (ACCOUNT_ROLE_OWNER, int(first["id"])))
            connection.commit()

    def _row_to_record(self, row: sqlite3.Row | None) -> AccountRecord | None:
        if row is None:
            return None
        role = str(row["role"] or ACCOUNT_ROLE_ADMIN)
        if role not in ACCOUNT_ROLES:
            role = ACCOUNT_ROLE_ADMIN
        last_accessed_at = row["last_accessed_at"]
        return AccountRecord(
            id=int(row["id"]),
            username=str(row["username"]),
            role=role,
            must_change_password=bool(row["must_change_password"]),
            created_at=float(row["created_at"] or 0.0),
            last_accessed_at=float(last_accessed_at) if last_accessed_at is not None else None,
        )

    def has_account(self) -> bool:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
            return row is not None

    def create_admin(self, username: str, password: str, confirm_password: str) -> None:
        username = validate_account_username(username)
        password = validate_account_password(password)
        if password != str(confirm_password):
            raise ValueError("passwords do not match")
        password_hash = hash_account_password(password)
        with self.lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None:
                raise ValueError("an administrator account already exists")
            connection.execute(
                """
                INSERT INTO accounts (username, password, role, must_change_password, created_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (username, password_hash, ACCOUNT_ROLE_OWNER, time.time()),
            )
            connection.commit()

    def _generate_secret(self) -> str:
        return secrets.token_urlsafe(12)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            return [record.to_public_dict() for row in rows if (record := self._row_to_record(row)) is not None]

    def account_by_id(self, account_id: int) -> AccountRecord | None:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            return self._row_to_record(row)

    def create_account(self, username: str, read_only: bool) -> tuple[dict[str, Any], str]:
        username = validate_account_username(username)
        secret = self._generate_secret()
        role = ACCOUNT_ROLE_READ_ONLY if read_only else ACCOUNT_ROLE_ADMIN
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (username, password, role, must_change_password, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (username, hash_account_password(secret), role, time.time()),
            )
            row = connection.execute("SELECT * FROM accounts WHERE username = ? LIMIT 1", (username,)).fetchone()
            connection.commit()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account could not be created")
            return record.to_public_dict(), secret

    def reset_password(self, account_id: int) -> tuple[dict[str, Any], str]:
        secret = self._generate_secret()
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            if row is None:
                raise ValueError("account was not found")
            connection.execute(
                "UPDATE accounts SET password = ?, must_change_password = 1 WHERE id = ?",
                (hash_account_password(secret), int(account_id)),
            )
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            connection.commit()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            return record.to_public_dict(), secret

    def set_read_only(self, account_id: int, read_only: bool) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            if record.is_owner:
                raise ValueError("the owner account cannot be made read-only")
            role = ACCOUNT_ROLE_READ_ONLY if read_only else ACCOUNT_ROLE_ADMIN
            connection.execute("UPDATE accounts SET role = ? WHERE id = ?", (role, record.id))
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (record.id,)).fetchone()
            connection.commit()
            updated = self._row_to_record(row)
            if updated is None:
                raise ValueError("account was not found")
            return updated.to_public_dict()

    def delete_account(self, account_id: int, requester_id: int) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            if record.is_owner:
                raise ValueError("the owner account cannot be deleted")
            if record.id == int(requester_id):
                raise ValueError("you cannot delete the account you are using")
            connection.execute("DELETE FROM accounts WHERE id = ?", (record.id,))
            connection.commit()
            return record.to_public_dict()

    def change_password(self, account_id: int, current_password: str, new_password: str, confirm_password: str) -> dict[str, Any]:
        new_password = validate_account_password(new_password)
        if new_password != str(confirm_password):
            raise ValueError("passwords do not match")
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            if not verify_account_password(str(current_password), str(row["password"])):
                raise ValueError("current password is incorrect")
            connection.execute(
                "UPDATE accounts SET password = ?, must_change_password = 0 WHERE id = ?",
                (hash_account_password(new_password), record.id),
            )
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (record.id,)).fetchone()
            connection.commit()
            updated = self._row_to_record(row)
            if updated is None:
                raise ValueError("account was not found")
            return updated.to_public_dict()

    def verify_basic_account(self, username: str, password: str) -> AccountRecord | None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE username = ? LIMIT 1",
                (str(username),),
            ).fetchone()
            if row is None:
                return None
            password_hash = str(row["password"])
            if not verify_account_password(password, password_hash):
                return None
            if account_password_hash_needs_upgrade(password_hash):
                connection.execute(
                    "UPDATE accounts SET password = ? WHERE id = ?",
                    (hash_account_password(password), int(row["id"])),
                )
            connection.execute("UPDATE accounts SET last_accessed_at = ? WHERE id = ?", (time.time(), int(row["id"])))
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(row["id"]),)).fetchone()
            connection.commit()
            return self._row_to_record(row)

    def verify_basic_credentials(self, username: str, password: str) -> bool:
        return self.verify_basic_account(username, password) is not None

    def touch_account_access(self, account_id: int, *, minimum_interval_seconds: float = 300.0) -> None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT last_accessed_at FROM accounts WHERE id = ? LIMIT 1",
                (int(account_id),),
            ).fetchone()
            if row is None:
                return
            last_accessed_at = row["last_accessed_at"]
            now = time.time()
            if last_accessed_at is None or now - float(last_accessed_at) >= minimum_interval_seconds:
                connection.execute("UPDATE accounts SET last_accessed_at = ? WHERE id = ?", (now, int(account_id)))
                connection.commit()


class AuthSessionStore:
    def __init__(self, lifetime_seconds: float = AUTH_SESSION_SECONDS) -> None:
        self.lifetime_seconds = float(lifetime_seconds)
        self.sessions: dict[str, AuthSession] = {}
        self.lock = threading.Lock()

    def create(self, account_id: int = 1) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.lifetime_seconds
        with self.lock:
            self._prune_locked(time.time())
            self.sessions[token] = AuthSession(account_id=int(account_id), expires_at=expires_at)
        return token

    def validate(self, token: str, accounts: AccountStore | None = None) -> AccountRecord | bool | None:
        token = str(token)
        now = time.time()
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return False
            if session.expires_at <= now:
                self.sessions.pop(token, None)
                return False
            session.expires_at = now + self.lifetime_seconds
            account_id = session.account_id
        if accounts is None:
            return True
        account = accounts.account_by_id(account_id)
        if account is None:
            self.invalidate_token(token)
            return None
        accounts.touch_account_access(account.id)
        return account

    def invalidate_token(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(str(token), None)

    def invalidate_account(self, account_id: int) -> None:
        with self.lock:
            for token, session in list(self.sessions.items()):
                if session.account_id == int(account_id):
                    self.sessions.pop(token, None)

    def invalidate_all(self) -> None:
        with self.lock:
            self.sessions.clear()

    def cookie_header(self, token: str) -> str:
        return (
            f"{AUTH_SESSION_COOKIE_NAME}={token}; "
            f"Max-Age={int(self.lifetime_seconds)}; Path=/; HttpOnly; SameSite=Lax"
        )

    def _prune_locked(self, now: float) -> None:
        for token, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.sessions.pop(token, None)


class IcecastStreamWorker:
    def __init__(
        self,
        *,
        stream: dict[str, Any],
        fanout: RawRtlFanout | IntermediateIqFanout,
        fallback_settings_provider,
        state_directory: Path,
        storage_monitor: StorageMonitor | None = None,
    ) -> None:
        self.stream = stream
        self.fanout = fanout
        self.fallback_settings_provider = fallback_settings_provider
        self.state_directory = state_directory
        self.storage_monitor = storage_monitor or StorageMonitor([state_directory])
        station = stream.get("station", {})
        stream_label = station.get("callsign") or stream.get("id", "stream")
        self.queue = subscribe_raw_fanout(
            fanout,
            max_chunks=LIVE_IQ_QUEUE_CHUNKS,
            name=f"stream:{stream_label}",
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run_pcm_producer, name=f"icecast-stream-{stream['id']}", daemon=True)
        self.outputs: dict[str, IcecastOutputWriter] = {}
        self.encoder_groups: dict[tuple[str, int, int], IcecastEncoderGroup] = {}
        self.monitor_sources: dict[str, WebRtcAudioSource] = {}
        self.eas_config: EasRecordingConfig | None = None
        self.eas_recorder: EasRecorderOutput | None = None
        self.eas_status = "disabled"
        self.eas_error: str | None = None
        self.last_audio_at: float | None = None
        self.lock = threading.Lock()

    @property
    def id(self) -> str:
        return str(self.stream["id"])

    def start(self) -> None:
        self.sync_stream(self.stream)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.fanout.unsubscribe(self.queue)
        for output in list(self.outputs.values()):
            output.stop()
        self.outputs = {}
        for encoder_group in list(self.encoder_groups.values()):
            encoder_group.stop()
        self.encoder_groups = {}
        for source in list(self.monitor_sources.values()):
            source.close()
        self.monitor_sources = {}
        self._stop_eas_recorder()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)

    def sync_stream(self, stream: dict[str, Any]) -> None:
        with self.lock:
            self.stream = stream
        desired = {
            str(output.get("id", "")): output
            for output in stream_outputs(stream)
            if output.get("enabled", True)
        }
        for output_id in list(self.outputs):
            writer = self.outputs[output_id]
            output = desired.get(output_id)
            if output is None or writer.signature != icecast_output_signature(output):
                self.outputs.pop(output_id).stop()
        for output_id, output in desired.items():
            if output_id in self.outputs:
                continue
            writer = IcecastOutputWriter(self, output)
            self.outputs[output_id] = writer
            writer.start()
        self._sync_eas_recorder(stream)

    def audio_config(self) -> AudioConfig:
        with self.lock:
            stream = self.stream
        return audio_config_from_stream(stream)

    def snapshots(self) -> list[dict[str, Any]]:
        return [output.snapshot() for output in list(self.outputs.values())]

    def raw_queue_stats(self) -> dict[str, Any]:
        subscriber_stats = getattr(self.fanout, "subscriber_stats", None)
        if subscriber_stats is None:
            return {}
        return subscriber_stats(self.queue)

    def eas_snapshot(self) -> dict[str, Any] | None:
        with self.lock:
            config = self.eas_config
            status = self.eas_status
            error = self.eas_error
        if config is None:
            return None
        return {
            "id": self.id,
            "status": status,
            "error": error,
            "directory": config.directory,
        }

    def add_monitor_source(self, client_id: str) -> WebRtcAudioSource:
        source = WebRtcAudioSource()
        with self.lock:
            old_source = self.monitor_sources.pop(client_id, None)
            self.monitor_sources[client_id] = source
        if old_source is not None:
            old_source.close()
        return source

    def remove_monitor_source(self, client_id: str) -> None:
        with self.lock:
            source = self.monitor_sources.pop(client_id, None)
        if source is not None:
            source.close()

    def monitor_source_stats(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            source = self.monitor_sources.get(client_id)
        return source.stats() if source is not None else {}

    def has_monitor_sources(self) -> bool:
        with self.lock:
            return bool(self.monitor_sources)

    def _run_pcm_producer(self) -> None:
        station = self.stream["station"]
        target_frequency_hz = float(station["frequency"]) * 1_000_000
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int, int] | None = None
        demodulator = ComplexNfmDemodulator()
        audio_config = self.audio_config()
        effects = AudioEffectsProcessor(audio_config)
        frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        fallback = load_web_fallback_audio()
        fallback_state = WebFallbackPlaybackState()
        last_real_audio = time.monotonic()
        idle_output_active = False
        while not self.stop_event.is_set():
            if not self._has_connected_outputs():
                try:
                    self.queue.get(timeout=0.5)
                except queue.Empty:
                    pass
                fallback_state.reset()
                frame_buffer.clear()
                continue
            try:
                batch: RtlSampleBatch = self.queue.get(
                    timeout=STREAM_FRAME_SECONDS if idle_output_active else STREAM_IDLE_DETECTION_SECONDS
                )
            except queue.Empty:
                idle_output_active = True
                fallback_settings = self.fallback_settings_provider()
                idle_seconds = time.monotonic() - last_real_audio
                if not fallback_settings.enabled:
                    fallback_state.reset()
                    continue
                if not fallback_state.active and idle_seconds < fallback_settings.silence_timeout_seconds:
                    fallback_state.reset()
                    self._write_pcm(STREAM_SILENCE_FRAME)
                    continue
                if not fallback_state.active:
                    fallback_state.active = True
                    LOG.info("starting fallback audio for %s after %.1f seconds without IQ", station.get("callsign"), idle_seconds)
                self._write_pcm(next_web_fallback_frame(fallback, fallback_state, fallback_settings.loop_delay_seconds))
                continue
            idle_output_active = False
            next_channelizer_key = (
                batch.sample_rate,
                batch.center_frequency_hz,
                int(round(target_frequency_hz)),
            )
            if channelizer is None or channelizer_key != next_channelizer_key:
                channelizer = IqChannelizer(
                    input_rate=batch.sample_rate,
                    center_frequency_hz=batch.center_frequency_hz,
                    target_frequency_hz=int(round(target_frequency_hz)),
                    output_rate=IQ_SAMPLE_RATE,
                )
                channelizer_key = next_channelizer_key
                demodulator = ComplexNfmDemodulator()
            iq = iq_batch_complex(batch)
            audio = demodulator.process(channelizer.process_complex(iq))
            if len(audio) == 0:
                continue
            last_real_audio = time.monotonic()
            if fallback_state.active:
                LOG.info("stopping fallback audio for %s", station.get("callsign"))
            fallback_state.reset()
            with self.lock:
                self.last_audio_at = time.time()
            for frame in frame_buffer.push(audio):
                next_audio_config = self.audio_config()
                if next_audio_config != audio_config:
                    audio_config = next_audio_config
                    changed_effects = effects.update_config(audio_config)
                    LOG.info(
                        "applied audio effects update for %s without reconnecting Icecast: %s",
                        station.get("callsign"),
                        ", ".join(changed_effects) or "none",
                    )
                pcm = float_to_s16(effects.process(frame))
                self._write_pcm(pcm)

    def encoder_group_for(self, config: IcecastConfig) -> "IcecastEncoderGroup":
        key = icecast_encoder_key(config)
        with self.lock:
            encoder_group = self.encoder_groups.get(key)
            if encoder_group is not None:
                return encoder_group
            encoder_group = IcecastEncoderGroup(key, config)
            self.encoder_groups[key] = encoder_group
            encoder_group.start()
            LOG.info(
                "created shared Icecast encoder group %s for %s",
                key,
                self.stream.get("station", {}).get("callsign", "unknown"),
            )
            return encoder_group

    def _write_pcm(self, pcm: bytes) -> None:
        for encoder_group in list(self.encoder_groups.values()):
            if encoder_group.has_outputs():
                queue_latest(encoder_group.pcm_queue, pcm)
        with self.lock:
            monitor_sources = list(self.monitor_sources.values())
            eas_recorder = self.eas_recorder
        for source in monitor_sources:
            source.push_pcm(pcm)
        if eas_recorder is not None:
            recorder_config = getattr(eas_recorder, "config", None)
            if recorder_config is not None and self.storage_monitor.is_critical(Path(recorder_config.directory)):
                self.stop_eas_recording_due_to_storage(storage_status_message("critical"))
                return
            try:
                eas_recorder.write(pcm)
            except Exception as exc:
                self._set_eas_status("needs-attention", str(exc))
                LOG.warning(
                    "EAS recorder failed for %s: %s",
                    self.stream.get("station", {}).get("callsign"),
                    exc,
                )
                self._stop_eas_recorder()

    def _has_connected_outputs(self) -> bool:
        return (
            any(encoder_group.has_outputs() for encoder_group in list(self.encoder_groups.values()))
            or self.has_monitor_sources()
            or self.has_eas_recorder()
        )

    def _stop_unused_encoder_groups(self) -> None:
        for key, encoder_group in list(self.encoder_groups.items()):
            if encoder_group.has_outputs():
                continue
            self.encoder_groups.pop(key, None)
            encoder_group.stop()

    def has_eas_recorder(self) -> bool:
        with self.lock:
            return self.eas_recorder is not None

    def stop_eas_recording_due_to_storage(self, message: str) -> None:
        with self.lock:
            has_recorder = self.eas_recorder is not None
        if not has_recorder:
            return
        LOG.warning(
            "stopping EAS recorder for %s because storage is critically low",
            self.stream.get("station", {}).get("callsign"),
        )
        self._stop_eas_recorder()
        with self.lock:
            if self.eas_config is not None:
                self.eas_status = "needs-attention"
                self.eas_error = message

    def _sync_eas_recorder(self, stream: dict[str, Any]) -> None:
        settings = eas_recording_settings_from_stream(stream)
        next_config = (
            eas_config_from_stream(stream, self.state_directory, settings)
            if settings.enabled
            else None
        )
        with self.lock:
            current_config = self.eas_config
        if next_config == current_config:
            return
        self._stop_eas_recorder()
        if next_config is None:
            with self.lock:
                self.eas_config = None
                self.eas_status = "disabled"
                self.eas_error = None
            return
        self.storage_monitor.add_path(Path(next_config.directory))
        if self.storage_monitor.is_critical(Path(next_config.directory)):
            message = storage_status_message("critical")
            LOG.warning(
                "EAS recorder not started for %s because storage is critically low",
                stream.get("station", {}).get("callsign"),
            )
            with self.lock:
                self.eas_config = next_config
                self.eas_recorder = None
                self.eas_status = "needs-attention"
                self.eas_error = message
            return
        try:
            recorder = EasRecorderOutput(next_config)
        except Exception as exc:
            LOG.warning(
                "EAS recorder failed for %s: %s",
                stream.get("station", {}).get("callsign"),
                exc,
            )
            with self.lock:
                self.eas_config = next_config
                self.eas_recorder = None
                self.eas_status = "needs-attention"
                self.eas_error = str(exc)
            return
        with self.lock:
            self.eas_config = next_config
            self.eas_recorder = recorder
            self.eas_status = "enabled"
            self.eas_error = None
        self.storage_monitor.recording_started(Path(next_config.directory))
        LOG.info(
            "started EAS recorder for %s in %s",
            stream.get("station", {}).get("callsign", "unknown"),
            next_config.directory,
        )

    def _stop_eas_recorder(self) -> None:
        with self.lock:
            recorder = self.eas_recorder
            self.eas_recorder = None
            if self.eas_config is not None:
                self.eas_status = "disabled"
        if recorder is not None:
            try:
                recorder.close()
            except Exception as exc:
                LOG.debug("EAS recorder close failed: %s", exc)
            self.storage_monitor.recording_stopped()

    def _set_eas_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.eas_status = status
            self.eas_error = error


@dataclass(frozen=True)
class IqRecorderConfig:
    recording_id: str
    mode: str
    sample_rate: int
    duration_seconds: float
    output_path: Path
    index_path: Path
    frequency_hz: int
    stream_id: str = ""
    stream_label: str = ""
    target_frequency_hz: int | None = None


class IqRecorderWorker:
    def __init__(
        self,
        *,
        fanout: RawRtlFanout | IntermediateIqFanout,
        config: IqRecorderConfig,
        storage_monitor: StorageMonitor,
    ) -> None:
        self.fanout = fanout
        self.config = config
        self.storage_monitor = storage_monitor
        self.queue = subscribe_raw_fanout(
            fanout,
            max_chunks=IQ_RECORDER_QUEUE_CHUNKS,
            name=f"iq-recorder:{config.mode}",
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="iq-recorder", daemon=True)
        self.lock = threading.Lock()
        self.status_name = "recording"
        self.error: str | None = None
        self.started_at = time.time()
        self.stopped_at: float | None = None
        self.bytes_written = 0
        self.samples_written = 0
        self.batch_count = 0
        self.storage_remaining_seconds: float | None = None
        self.storage_remaining_updated_at: float | None = None

    def start(self) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_monitor.add_path(self.config.output_path.parent)
        self.storage_monitor.recording_started()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.fanout.unsubscribe(self.queue)
        if self.thread.ident is not None:
            self.thread.join(timeout=3.0)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            stopped_at = self.stopped_at
            elapsed = max(0.0, (stopped_at or now) - self.started_at)
            return {
                "active": self.status_name == "recording",
                "id": self.config.recording_id,
                "status": self.status_name,
                "mode": self.config.mode,
                "stream_id": self.config.stream_id,
                "stream_label": self.config.stream_label,
                "sample_rate": self.config.sample_rate,
                "frequency_hz": self.config.frequency_hz,
                "duration_seconds": self.config.duration_seconds,
                "elapsed_seconds": elapsed,
                "bytes_written": self.bytes_written,
                "samples_written": self.samples_written,
                "file_name": self.config.output_path.name,
                "started_at": self.started_at,
                "stopped_at": stopped_at,
                "batch_count": self.batch_count,
                "error": self.error,
            }

    def storage_time_remaining(self, storage: dict[str, Any], recordings_directory: Path) -> float | None:
        snapshot = self.snapshot()
        raw_remaining = estimate_iq_storage_remaining_seconds(snapshot, storage, recordings_directory)
        now = time.monotonic()
        with self.lock:
            if self.status_name != "recording":
                self.storage_remaining_seconds = None
                self.storage_remaining_updated_at = None
                return None
            next_remaining = smooth_iq_storage_remaining_seconds(
                previous=self.storage_remaining_seconds,
                elapsed_since_update=0.0 if self.storage_remaining_updated_at is None else now - self.storage_remaining_updated_at,
                raw=raw_remaining,
            )
            self.storage_remaining_seconds = next_remaining
            self.storage_remaining_updated_at = now
            return next_remaining

    def _set_finished(self, status: str, error: str | None = None) -> None:
        with self.lock:
            if self.status_name != "recording":
                return
            self.status_name = status
            self.error = error
            self.stopped_at = time.time()

    def _add_written(self, sample_count: int, byte_count: int) -> None:
        with self.lock:
            self.samples_written += sample_count
            self.bytes_written += byte_count
            self.batch_count += 1

    def _run(self) -> None:
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int, int] | None = None
        decimator = None
        decimator_key: tuple[int, int] | None = None
        try:
            with self.config.output_path.open("wb") as output:
                while not self.stop_event.is_set():
                    elapsed = time.time() - self.started_at
                    if self.config.duration_seconds > 0 and elapsed >= self.config.duration_seconds:
                        self._set_finished("completed")
                        break
                    if self.storage_monitor.is_critical(self.config.output_path.parent):
                        self._set_finished("needs-attention", storage_status_message("critical"))
                        break
                    try:
                        batch: RtlSampleBatch = self.queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    iq = iq_batch_complex(batch)
                    if iq.size == 0:
                        continue
                    if self.config.mode == IQ_RECORDER_MODE_STREAM:
                        if self.config.target_frequency_hz is None:
                            raise ValueError("stream recording target frequency is missing")
                        next_key = (batch.sample_rate, batch.center_frequency_hz, int(self.config.target_frequency_hz))
                        if channelizer is None or channelizer_key != next_key:
                            channelizer = IqChannelizer(
                                input_rate=batch.sample_rate,
                                center_frequency_hz=batch.center_frequency_hz,
                                target_frequency_hz=int(self.config.target_frequency_hz),
                                output_rate=IQ_SAMPLE_RATE,
                            )
                            channelizer_key = next_key
                        recorded_iq = channelizer.process_complex(iq)
                    else:
                        if self.config.sample_rate > batch.sample_rate:
                            raise ValueError("recording sample rate is higher than the RTL-SDR sample rate")
                        if self.config.sample_rate == batch.sample_rate:
                            recorded_iq = iq
                        else:
                            next_key = (batch.sample_rate, self.config.sample_rate)
                            if decimator is None or decimator_key != next_key:
                                decimator = create_decimator(batch.sample_rate, self.config.sample_rate)
                                decimator_key = next_key
                            recorded_iq = decimator.process(iq)
                    if recorded_iq.size == 0:
                        continue
                    data = complex64_to_interleaved_f32(recorded_iq)
                    output.write(data)
                    self._add_written(int(recorded_iq.size), len(data))
                else:
                    self._set_finished("stopped")
        except Exception as exc:
            LOG.exception("I/Q recorder failed: %s", exc)
            self._set_finished("needs-attention", str(exc))
        finally:
            self.fanout.unsubscribe(self.queue)
            self.storage_monitor.recording_stopped()
            self._write_metadata()

    def _write_metadata(self) -> None:
        snapshot = self.snapshot()
        if snapshot["bytes_written"] <= 0:
            return
        metadata = {
            "id": self.config.recording_id,
            "mode": self.config.mode,
            "stream_id": self.config.stream_id,
            "stream_label": self.config.stream_label,
            "sample_rate": self.config.sample_rate,
            "frequency_hz": self.config.frequency_hz,
            "duration_seconds": snapshot["elapsed_seconds"],
            "bytes": snapshot["bytes_written"],
            "samples": snapshot["samples_written"],
            "file_path": str(self.config.output_path),
            "started_at": snapshot["started_at"],
            "stopped_at": snapshot["stopped_at"] or time.time(),
            "status": snapshot["status"],
        }
        upsert_iq_recording_metadata(self.config.index_path, metadata)


class IcecastEncoderGroup:
    def __init__(self, key: tuple[str, int, int], config: IcecastConfig) -> None:
        self.key = key
        self.config = config
        self.encoder = create_audio_encoder(config)
        self.pcm_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self.outputs: list[IcecastOutputWriter] = []
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.error: Exception | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"icecast-encoder-{config.format}-{config.sample_rate}-{config.bitrate}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        self.encoder.close()

    def add_output(self, output: "IcecastOutputWriter") -> None:
        with self.lock:
            self.outputs.append(output)

    def remove_output(self, output: "IcecastOutputWriter") -> None:
        with self.lock:
            if output in self.outputs:
                self.outputs.remove(output)

    def has_outputs(self) -> bool:
        with self.lock:
            return bool(self.outputs)

    def header(self) -> bytes:
        return bytes(getattr(self.encoder, "header", b""))

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    pcm = self.pcm_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                encoded = self.encoder.encode(pcm)
                if encoded:
                    self._broadcast(encoded)
            encoded = self.encoder.flush()
            if encoded:
                self._broadcast(encoded)
        except Exception as exc:
            self.error = exc
            LOG.warning("shared Icecast encoder %s failed: %s", self.key, exc)

    def _broadcast(self, encoded: bytes) -> None:
        with self.lock:
            outputs = list(self.outputs)
        for output in outputs:
            queue_latest(output.encoded_queue, encoded)


class IcecastOutputWriter:
    def __init__(self, runtime: IcecastStreamWorker, output: dict[str, Any]) -> None:
        self.runtime = runtime
        self.output = output
        self.signature = icecast_output_signature(output)
        self.encoded_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"icecast-writer-{runtime.id}-{output.get('id', '')}",
            daemon=True,
        )
        self.status = "disabled"
        self.error: str | None = None
        self.started_at: float | None = None
        self.lock = threading.Lock()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        self._set_status("disabled")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            error = self.error
            started_at = self.started_at
        with self.runtime.lock:
            last_audio_at = self.runtime.last_audio_at
        return {
            "id": self.runtime.id,
            "station": self.runtime.stream["station"],
            "outputs": [self.output],
            "status": status,
            "error": error,
            "started_at": started_at,
            "last_audio_at": last_audio_at,
            "raw_queue": self.runtime.raw_queue_stats(),
        }

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            self.error = error
            if status == "enabled" and self.started_at is None:
                self.started_at = time.time()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            source = None
            sink = None
            encoder_group = None
            try:
                config = icecast_config_from_output(self.output)
                source = IcecastSource(config, config.content_type)
                sink = source.connect()
                encoder_group = self.runtime.encoder_group_for(config)
                header = encoder_group.header()
                if header:
                    sink.write(header)
                encoder_group.add_output(self)
                self._set_status("enabled")
                while not self.stop_event.is_set():
                    try:
                        encoded = self.encoded_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if encoded:
                        sink.write(encoded)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self._set_status("needs-attention", friendly_stream_error(exc))
                LOG.warning("Icecast output failed for %s: %s", self.output.get("id"), exc)
                self.stop_event.wait(STREAM_RECONNECT_SECONDS)
            finally:
                if encoder_group is not None:
                    encoder_group.remove_output(self)
                    self.runtime._stop_unused_encoder_groups()
                if sink is not None:
                    try:
                        sink.close()
                    except Exception:
                        pass
                if source is not None:
                    source.close()
        self._set_status("disabled")


class WeatherReceiverWorker:
    def __init__(
        self,
        *,
        client_id: str,
        fanout: RawRtlFanout | IntermediateIqFanout,
        frequency_hz: int,
    ) -> None:
        self.client_id = client_id
        self.fanout = fanout
        self.queue = subscribe_raw_fanout(
            fanout,
            max_chunks=LIVE_IQ_QUEUE_CHUNKS,
            name=f"receiver:{client_id}",
        )
        self.source = WebRtcAudioSource()
        self.frequency_hz = validate_receiver_frequency(frequency_hz)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"weather-receiver-{client_id}",
            daemon=True,
        )
        self.lock = threading.Lock()
        self.last_audio_at: float | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.fanout.unsubscribe(self.queue)
        self.source.close()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)

    def set_frequency(self, frequency_hz: int) -> int:
        frequency_hz = validate_receiver_frequency(frequency_hz)
        with self.lock:
            self.frequency_hz = frequency_hz
        return frequency_hz

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            frequency_hz = self.frequency_hz
            last_audio_at = self.last_audio_at
        return {
            "client_id": self.client_id,
            "frequency_hz": frequency_hz,
            "frequency_mhz": receiver_frequency_mhz(frequency_hz),
            "last_audio_at": last_audio_at,
            "stats": self.source.stats(),
            "raw_queue": self.raw_queue_stats(),
        }

    def raw_queue_stats(self) -> dict[str, Any]:
        subscriber_stats = getattr(self.fanout, "subscriber_stats", None)
        if subscriber_stats is None:
            return {}
        return subscriber_stats(self.queue)

    def _frequency_hz(self) -> int:
        with self.lock:
            return self.frequency_hz

    def _run(self) -> None:
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int] | None = None
        demodulator = ComplexNfmDemodulator()
        effects = AudioEffectsProcessor(RECEIVER_AUDIO_CONFIG)
        frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        while not self.stop_event.is_set():
            try:
                batch: RtlSampleBatch = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                LOG.warning("weather receiver RTL-SDR source failed for client %s: %s", self.client_id, exc)
                continue
            target_frequency_hz = self._frequency_hz()
            next_channelizer_key = (batch.sample_rate, batch.center_frequency_hz)
            if channelizer is None or channelizer_key != next_channelizer_key:
                channelizer = IqChannelizer(
                    input_rate=batch.sample_rate,
                    center_frequency_hz=batch.center_frequency_hz,
                    target_frequency_hz=target_frequency_hz,
                    output_rate=IQ_SAMPLE_RATE,
                )
                channelizer_key = next_channelizer_key
                demodulator = ComplexNfmDemodulator()
            else:
                channelizer.target_frequency_hz = target_frequency_hz
                channelizer.shifter.offset_hz = float(batch.center_frequency_hz - target_frequency_hz)
            iq = iq_batch_complex(batch)
            audio = demodulator.process(channelizer.process_complex(iq))
            if len(audio) == 0:
                continue
            for frame in frame_buffer.push(audio):
                self.source.push_pcm(float_to_s16(effects.process(frame)))
                with self.lock:
                    self.last_audio_at = time.time()


class RtlControlService:
    def __init__(self, state_path: Path, log_handler: RingLogHandler) -> None:
        self.state_path = state_path
        self.streams_state_path = state_path.with_name(STREAMS_STATE_FILE_NAME)
        self.streams_directory = state_path.parent / STREAMS_DIRECTORY_NAME
        self.iq_recordings_directory = state_path.parent / IQ_RECORDINGS_DIRECTORY_NAME
        self.iq_recordings_index_path = self.iq_recordings_directory / IQ_RECORDINGS_INDEX_FILE_NAME
        self.fallback_state_path = state_path.with_name(FALLBACK_STATE_FILE_NAME)
        self.accounts = AccountStore(state_path.parent / ACCOUNTS_DATABASE_FILE_NAME)
        self.auth_sessions = AuthSessionStore()
        self.log_handler = log_handler
        self.storage_monitor = StorageMonitor(
            [state_path.parent],
            critical_callback=self._handle_critical_storage,
        )
        self.lock = threading.RLock()
        self.settings = load_settings(state_path)
        self.streams = load_streams(self.streams_directory, self.streams_state_path)
        self.fallback_settings = load_fallback_settings(self.fallback_state_path)
        self.stations = load_station_database()
        self.capture: RtlCaptureSource | None = None
        self.raw_fanout: RawRtlFanout | None = None
        self.intermediate_fanout: IntermediateIqFanout | None = None
        self.monitor_queue: queue.Queue | None = None
        self.drain_thread: threading.Thread | None = None
        self.drain_stop = threading.Event()
        self.stream_workers: dict[str, IcecastStreamWorker] = {}
        self.monitor_streams_by_client: dict[str, str] = {}
        self.monitor_accounts_by_client: dict[str, int] = {}
        self.receiver_workers: dict[str, WeatherReceiverWorker] = {}
        self.receiver_accounts_by_client: dict[str, int] = {}
        self.iq_recorder: IqRecorderWorker | None = None
        self.iq_recorder_account_id: int | None = None
        self.iq_recording_downloads: set[str] = set()
        self.iq_recording_download_accounts: dict[str, int] = {}
        self.iq_recording_download_recordings: dict[str, str] = {}
        self.aborted_iq_recording_downloads: set[str] = set()
        self.webrtc_runner = WebRtcAsyncRunner()
        self.webrtc_sessions = AiortcSessionManager()
        self.icecast_auth_cache: dict[str, float] = {}
        self.capture_error: str | None = None
        self.last_batch_at: float | None = None
        self.received_chunks = 0
        self.received_bytes = 0
        self.storage_monitor.add_path(self.iq_recordings_directory)
        self.storage_monitor.start()
        if self.settings.serial:
            self._start_or_update_capture_locked()
        else:
            self._select_only_connected_device()

    def close(self) -> None:
        try:
            self.webrtc_runner.run(self.webrtc_sessions.close_all(), timeout=3.0)
        except Exception as exc:
            LOG.debug("WebRTC monitor cleanup failed: %s", exc)
        self.webrtc_runner.stop()
        with self.lock:
            recorder = self.iq_recorder
            self.iq_recorder = None
        if recorder is not None:
            recorder.stop()
        self.stop_capture()
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
        for worker in workers:
            worker.stop_eas_recording_due_to_storage(message)
        if recorder is not None:
            LOG.warning("stopping I/Q recorder because storage is critically low")
            recorder.stop()

    def status(self) -> dict[str, Any]:
        with self.lock:
            capture = self.capture
            settings = self._effective_settings_locked()
            active = capture is not None
            gain_values = capture.get_gain_values() if capture is not None else []
            capture_stats = capture.stats() if capture is not None else {}
            fanout_stats = self.raw_fanout.stats() if self.raw_fanout is not None else {}
            intermediate_stats = self.intermediate_fanout.stats() if self.intermediate_fanout is not None else {}
            return {
                "settings": asdict(settings),
                "gain_values": gain_values,
                "active": active,
                "capture_error": self.capture_error,
                "capture_stats": capture_stats,
                "raw_fanout_stats": fanout_stats,
                "intermediate_fanout_stats": intermediate_stats,
                "last_batch_at": self.last_batch_at,
                "received_chunks": self.received_chunks,
                "received_bytes": self.received_bytes,
                "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
                "fallback": asdict(self.fallback_settings),
                "active_streams": self._active_streams_locked(),
                "active_eas_recorders": self._active_eas_recorders_locked(),
                "recent_eas_alerts": self._recent_eas_alerts_locked(),
                "storage": self.storage_monitor.snapshot(),
                "iq_recorder": self._iq_recorder_status_locked(),
                "logs": self.log_handler.snapshot()[-80:],
            }

    def devices(self) -> dict[str, Any]:
        errors: list[str] = []
        rtl_devices = []
        usb_devices = []
        try:
            rtl_devices = list_rtl_devices()
        except Exception as exc:
            errors.append(f"librtlsdr probe failed: {exc}")
        try:
            usb_devices = list_usb_rtl_devices()
        except Exception as exc:
            errors.append(f"USB probe failed: {exc}")

        by_serial: dict[str, dict[str, Any]] = {}
        for device in usb_devices:
            if not device.serial:
                continue
            by_serial[device.serial] = {
                "serial": device.serial,
                "name": device.description,
                "vendor": USB_VENDOR_NAMES.get(device.vendor_id.lower(), device.vendor_id),
                "vendor_id": device.vendor_id,
                "product_id": device.product_id,
                "source": "usb",
            }
        for device in rtl_devices:
            if not device.serial:
                continue
            entry = by_serial.setdefault(
                device.serial,
                {
                    "serial": device.serial,
                    "name": device.description,
                    "vendor": "",
                    "vendor_id": "",
                    "product_id": "",
                    "source": "librtlsdr",
                },
            )
            entry["name"] = device.description or entry["name"]
            entry["librtlsdr_index"] = device.index
        devices = sorted(by_serial.values(), key=lambda item: (item["name"], item["serial"]))
        return {"devices": devices, "errors": errors}

    def search_stations(self, query: str, limit: int = 50) -> dict[str, Any]:
        query = query.strip().lower()
        limit = max(1, min(int(limit), 100))
        matches = []
        for station in self.stations:
            haystack = " ".join(
                str(station.get(key, ""))
                for key in ("callsign", "frequency", "city", "site_name", "state", "state_name")
            ).lower()
            callsign = str(station.get("callsign", "")).lower()
            if not query or query in haystack:
                priority = 0 if callsign.startswith(query) else 1
                matches.append((priority, station))
        matches.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("callsign", "")),
                str(item[1].get("state", "")),
                str(item[1].get("city", "")),
            )
        )
        return {"stations": [station for _, station in matches[:limit]]}

    def stream_status(self) -> dict[str, Any]:
        with self.lock:
            return {
                "streams": list(self.streams),
                "monitoring": dict(getattr(self, "monitor_streams_by_client", {})),
            }

    def iq_recorder_status(self) -> dict[str, Any]:
        with self.lock:
            return self._iq_recorder_status_locked()

    def redacted_iq_recorder_status(self) -> dict[str, Any]:
        return {
            "active": False,
            "status": "idle",
            "sample_rates": [],
            "default_duration_seconds": IQ_RECORDER_DEFAULT_DURATION_SECONDS,
        }

    def iq_recordings(self) -> dict[str, Any]:
        with self.lock:
            downloading = set(self.iq_recording_downloads)
        recordings = []
        for recording in load_iq_recording_entries(self.iq_recordings_index_path):
            item = iq_recording_summary(recording)
            item["downloading"] = item["id"] in downloading
            recordings.append(item)
        recordings.sort(key=lambda item: float(item.get("started_at", 0.0)), reverse=True)
        return {"recordings": recordings}

    def remove_iq_recording(self, recording_id: str) -> dict[str, Any]:
        recording_id = str(recording_id).strip()
        if not recording_id:
            raise ValueError("I/Q recording id is required")
        with self.lock:
            if recording_id in self.iq_recording_downloads:
                raise ValueError("This I/Q recording is currently being downloaded.")
        data = load_iq_recording_index(self.iq_recordings_index_path)
        recording = None
        remaining = []
        for entry in data["recordings"]:
            if str(entry.get("id", "")) == recording_id:
                recording = entry
            else:
                remaining.append(entry)
        if recording is None:
            raise ValueError("I/Q recording was not found")
        path = safe_iq_recording_file_path(self.iq_recordings_directory, recording, require_exists=False)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        data["recordings"] = remaining
        atomic_write_json(self.iq_recordings_index_path, data)
        LOG.info("removed I/Q recording %s", recording_id)
        return self.iq_recordings()

    def iq_recording_download_info(self, recording_id: str, fmt: str) -> tuple[dict[str, Any], Path, str, int]:
        recording_id = str(recording_id).strip()
        fmt = validate_iq_download_format(fmt)
        if not recording_id:
            raise ValueError("I/Q recording id is required")
        recording = find_iq_recording(self.iq_recordings_index_path, recording_id)
        path = safe_iq_recording_file_path(self.iq_recordings_directory, recording)
        return recording, path, iq_recording_download_name(recording, fmt), iq_recording_download_size(path, fmt)

    def begin_iq_recording_download(self, recording_id: str, account_id: int | None = None) -> str:
        download_id = uuid.uuid4().hex
        with self.lock:
            self.iq_recording_downloads.add(recording_id)
            self.iq_recording_download_accounts[download_id] = int(account_id or 0)
            self.iq_recording_download_recordings[download_id] = recording_id
        return download_id

    def finish_iq_recording_download(self, download_id: str) -> None:
        with self.lock:
            recording_id = self.iq_recording_download_recordings.pop(download_id, "")
            self.iq_recording_download_accounts.pop(download_id, None)
            self.aborted_iq_recording_downloads.discard(download_id)
            if recording_id and recording_id not in self.iq_recording_download_recordings.values():
                self.iq_recording_downloads.discard(recording_id)

    def iq_recording_download_aborted(self, download_id: str) -> bool:
        with self.lock:
            return download_id in self.aborted_iq_recording_downloads

    def _iq_recorder_status_locked(self) -> dict[str, Any]:
        worker = self.iq_recorder
        if worker is None:
            return {
                "active": False,
                "status": "idle",
                "sample_rates": list(IQ_RECORDER_SAMPLE_RATES),
                "default_duration_seconds": IQ_RECORDER_DEFAULT_DURATION_SECONDS,
            }
        snapshot = worker.snapshot()
        snapshot["sample_rates"] = list(IQ_RECORDER_SAMPLE_RATES)
        snapshot["default_duration_seconds"] = IQ_RECORDER_DEFAULT_DURATION_SECONDS
        snapshot["storage_remaining_seconds"] = worker.storage_time_remaining(
            self.storage_monitor.snapshot(),
            self.iq_recordings_directory,
        )
        return snapshot

    def start_iq_recording(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        with self.lock:
            existing = self.iq_recorder
            if existing is not None and existing.snapshot().get("active"):
                raise ValueError("I/Q recording is already in progress")
            raw_fanout = self.raw_fanout
            intermediate_fanout = self.intermediate_fanout
            config = self._iq_recorder_config_from_payload_locked(payload)
            fanout = intermediate_fanout if config.mode == IQ_RECORDER_MODE_STREAM else raw_fanout
            if fanout is None:
                raise ValueError("RTL-SDR capture is not active")
            if self.storage_monitor.is_critical(config.output_path.parent):
                raise ValueError(storage_status_message("critical"))
            worker = IqRecorderWorker(
                fanout=fanout,
                config=config,
                storage_monitor=self.storage_monitor,
            )
            self.iq_recorder = worker
            self.iq_recorder_account_id = int(account_id or 0) if account_id is not None else None
            worker.start()
        LOG.info(
            "started I/Q recording: mode=%s sample_rate=%s duration=%.1fs file=%s",
            config.mode,
            config.sample_rate,
            config.duration_seconds,
            config.output_path.name,
        )
        return {"success": True, "iq_recorder": self.iq_recorder_status()}

    def stop_iq_recording(self) -> dict[str, Any]:
        with self.lock:
            worker = self.iq_recorder
            self.iq_recorder = None
            self.iq_recorder_account_id = None
        if worker is not None:
            worker.stop()
            LOG.info("stopped I/Q recording")
        return {"success": True, "iq_recorder": self.iq_recorder_status()}

    def _iq_recorder_config_from_payload_locked(self, payload: dict[str, Any]) -> IqRecorderConfig:
        mode = str(payload.get("mode", IQ_RECORDER_MODE_STREAM)).strip().lower()
        if mode not in {IQ_RECORDER_MODE_STREAM, IQ_RECORDER_MODE_SPECTRUM}:
            raise ValueError("select a valid I/Q recording mode")
        duration_seconds = validate_iq_recording_duration(payload.get("duration_seconds", IQ_RECORDER_DEFAULT_DURATION_SECONDS))
        stream_id = ""
        stream_label = ""
        target_frequency_hz = None
        sample_rate = DEFAULT_RTL_SAMPLE_RATE
        file_label = mode
        if mode == IQ_RECORDER_MODE_STREAM:
            stream_id = str(payload.get("stream_id", "")).strip()
            stream = self._stream_locked(stream_id)
            if not stream.get("enabled", True):
                raise ValueError("select an active stream")
            station = stream.get("station", {})
            callsign = sanitize_path_component(str(station.get("callsign", "stream")))
            try:
                target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("stream station frequency is invalid") from exc
            stream_label = str(station.get("callsign", callsign))
            sample_rate = IQ_SAMPLE_RATE
            file_label = callsign
        else:
            sample_rate = validate_iq_recording_sample_rate(payload.get("sample_rate", DEFAULT_RTL_SAMPLE_RATE))
            file_label = f"spectrum-{sample_rate}"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recording_id = uuid.uuid4().hex
        frequency_hz = int(target_frequency_hz or NWR_CENTER_FREQUENCY_HZ)
        output_path = unique_iq_recording_path(self.iq_recordings_directory, f"{timestamp}-{file_label}.cf32")
        return IqRecorderConfig(
            recording_id=recording_id,
            mode=mode,
            sample_rate=sample_rate,
            duration_seconds=duration_seconds,
            output_path=output_path,
            index_path=self.iq_recordings_index_path,
            frequency_hz=frequency_hz,
            stream_id=stream_id,
            stream_label=stream_label,
            target_frequency_hz=target_frequency_hz,
        )

    def rtl_diagnostics(self) -> dict[str, Any]:
        with self.lock:
            fanout = self.raw_fanout
        if fanout is None:
            raise ValueError("RTL-SDR capture is not active")
        queue_depth = max(RTL_DIAGNOSTIC_MIN_BATCHES, min(RTL_DIAGNOSTIC_MAX_BATCHES, 16))
        subscriber = subscribe_raw_fanout(
            fanout,
            max_chunks=queue_depth,
            max_seconds=RTL_DIAGNOSTIC_QUEUE_SECONDS,
            name="rtl-diagnostics",
        )
        started = time.monotonic()
        batches: list[RtlSampleBatch] = []
        sample_rate = 0
        center_frequency_hz = 0
        target_samples = 0
        try:
            while time.monotonic() - started < RTL_DIAGNOSTIC_QUEUE_SECONDS:
                remaining = max(0.01, RTL_DIAGNOSTIC_QUEUE_SECONDS - (time.monotonic() - started))
                try:
                    batch = subscriber.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if not isinstance(batch, RtlSampleBatch):
                    continue
                if sample_rate <= 0:
                    sample_rate = int(batch.sample_rate)
                    center_frequency_hz = int(batch.center_frequency_hz)
                    target_samples = max(1, int(sample_rate * RTL_DIAGNOSTIC_SECONDS))
                if batch.sample_rate != sample_rate or batch.center_frequency_hz != center_frequency_hz:
                    batches = []
                    sample_rate = int(batch.sample_rate)
                    center_frequency_hz = int(batch.center_frequency_hz)
                    target_samples = max(1, int(sample_rate * RTL_DIAGNOSTIC_SECONDS))
                batches.append(batch)
                sample_count = sum(len(item.data) // 2 for item in batches)
                if sample_count >= target_samples:
                    break
        finally:
            unsubscribe = getattr(fanout, "unsubscribe", None)
            if unsubscribe is not None:
                unsubscribe(subscriber)
        if not batches:
            raise ValueError("RTL-SDR produced no samples for diagnostics")
        raw = b"".join(batch.data for batch in batches)
        iq = rtl_u8_to_complex64(raw)
        if target_samples > 0 and iq.size > target_samples:
            iq = iq[-target_samples:]
        raw_stats = raw_iq_diagnostics(iq, sample_rate, center_frequency_hz, raw)
        channels = [
            channel_audio_diagnostics(iq, sample_rate, center_frequency_hz, frequency_hz)
            for frequency_hz in NWR_RECEIVER_CHANNELS_HZ
        ]
        strongest = max(
            raw_stats["channels"],
            key=lambda item: float(item["raw_snr_db"] if item["raw_snr_db"] is not None else -999.0),
            default=None,
        )
        LOG.info(
            "RTL-SDR diagnostics: sample_rate=%s center=%s samples=%s raw_rms=%.4f dc=%.6f strongest=%s snr=%s",
            sample_rate,
            center_frequency_hz,
            raw_stats["sample_count"],
            raw_stats["rms"],
            raw_stats["dc_magnitude"],
            strongest.get("frequency_mhz") if strongest else "none",
            strongest.get("raw_snr_db") if strongest else None,
        )
        return {
            "sample_rate": sample_rate,
            "center_frequency_hz": center_frequency_hz,
            "duration_seconds": (iq.size / float(sample_rate)) if sample_rate > 0 else 0.0,
            "batch_count": len(batches),
            "raw": raw_stats,
            "channels": channels,
        }

    def eas_alert_streams(self) -> dict[str, Any]:
        with self.lock:
            streams = [self._eas_alert_stream_summary(stream) for stream in self.streams if self._stream_has_eas_alert_index(stream)]
        return {"streams": streams}

    def eas_alerts(self, stream_id: str, page: int = 1, per_page: int = 25) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream_summary = self._eas_alert_stream_summary(stream)
        alerts = load_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        ordered_alerts = sorted(
            enumerate(alerts),
            key=lambda item: parse_utc_datetime(str(item[1].get("start_time_utc", ""))),
            reverse=True,
        )
        total = len(ordered_alerts)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        page_alerts = [
            eas_alert_summary(stream, alert, original_index)
            for original_index, alert in ordered_alerts[start : start + per_page]
        ]
        return {
            "stream": stream_summary,
            "alerts": page_alerts,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }

    def eas_alert_bulk_options(self, stream_id: str) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        indexed_alerts = indexed_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        now = datetime.now(timezone.utc)
        export_presets = []
        for preset in EXPORT_ALERT_PRESETS:
            selected = filter_indexed_alerts(indexed_alerts, preset_range_bounds(preset["id"], now))
            if selected:
                export_presets.append({"id": preset["id"], "label": preset["label"], "count": len(selected)})
        delete_presets = []
        for preset in DELETE_ALERT_PRESETS:
            selected = filter_indexed_alerts(indexed_alerts, preset_range_bounds(preset["id"], now))
            if selected:
                delete_presets.append({"id": preset["id"], "label": preset["label"], "count": len(selected)})
        return {
            "export_presets": export_presets,
            "delete_presets": delete_presets,
            "total": len(indexed_alerts),
            "now": local_datetime_parts(datetime.now().astimezone()),
        }

    def eas_alert_range_count(self, stream_id: str, mode: str, start: str = "", end: str = "") -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        indexed_alerts = indexed_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        bounds = alert_range_bounds_from_request(mode, start, end)
        count = len(filter_indexed_alerts(indexed_alerts, bounds))
        return {"count": count}

    def eas_alert_export_zip(self, stream_id: str, mode: str, start: str = "", end: str = "") -> tuple[Path, str]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        indexed_alerts = indexed_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        selected = filter_indexed_alerts(indexed_alerts, alert_range_bounds_from_request(mode, start, end))
        if not selected:
            raise ValueError("No alerts were issued during this time.")
        station = stream.get("station", {})
        callsign = sanitize_path_component(str(station.get("callsign", "alerts")))
        fd, raw_path = tempfile.mkstemp(prefix=f"{callsign}-eas-alerts-", suffix=".zip")
        os.close(fd)
        zip_path = Path(raw_path)
        export_index = {"version": 1, "alerts": []}
        used_names: set[str] = set()
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for _index, alert in selected:
                    audio_path = safe_eas_alert_file_path(self.streams_directory, stream, alert)
                    archive_name = unique_archive_name(Path(str(alert.get("file_path", ""))).name, used_names)
                    archive.write(audio_path, archive_name)
                    export_entry = dict(alert)
                    export_entry["file_path"] = archive_name
                    export_index["alerts"].append(export_entry)
                archive.writestr("index.json", json.dumps(export_index, indent=2, sort_keys=True) + "\n")
        except Exception:
            zip_path.unlink(missing_ok=True)
            raise
        return zip_path, f"{callsign}-eas-alerts.zip"

    def eas_alert_detail(self, stream_id: str, alert_id: str) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream_summary = self._eas_alert_stream_summary(stream)
        alert, index, _index_path = self._eas_alert_by_id(stream, alert_id)
        return {"stream": stream_summary, "alert": eas_alert_detail(stream, alert, index)}

    def eas_alert_audio_path(self, stream_id: str, alert_id: str) -> tuple[Path, str]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        alert, _index, _index_path = self._eas_alert_by_id(stream, alert_id)
        return safe_eas_alert_file_path(self.streams_directory, stream, alert), alert_download_name(stream, alert)

    def remove_eas_alert(self, stream_id: str, alert_id: str) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        alert, _index, index_path = self._eas_alert_by_id(stream, alert_id)
        audio_path = safe_eas_alert_file_path(self.streams_directory, stream, alert, require_exists=False)
        try:
            audio_path.unlink()
        except FileNotFoundError:
            pass
        data = load_eas_alert_index(index_path)
        alerts = data["alerts"]
        data["alerts"] = [
            entry for position, entry in enumerate(alerts)
            if eas_alert_id(entry, position) != alert_id
        ]
        atomic_write_json(index_path, data)
        LOG.info("removed EAS alert %s for stream %s", alert_id, stream_id)
        return {"success": True}

    def remove_eas_alert_range(self, stream_id: str, mode: str, start: str = "", end: str = "") -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        index_path = eas_alert_index_path(self.streams_directory, stream)
        data = load_eas_alert_index(index_path)
        indexed_alerts = [(index, alert) for index, alert in enumerate(data["alerts"])]
        selected = filter_indexed_alerts(indexed_alerts, alert_range_bounds_from_request(mode, start, end))
        if not selected:
            raise ValueError("No alerts were issued during this time.")
        selected_indexes = {index for index, _alert in selected}
        for _index, alert in selected:
            audio_path = safe_eas_alert_file_path(self.streams_directory, stream, alert, require_exists=False)
            try:
                audio_path.unlink()
            except FileNotFoundError:
                pass
        data["alerts"] = [
            alert for index, alert in enumerate(data["alerts"])
            if index not in selected_indexes
        ]
        atomic_write_json(index_path, data)
        LOG.info("removed %s EAS alerts for stream %s", len(selected), stream_id)
        return {"success": True, "count": len(selected)}


    def _stream_has_eas_alert_index(self, stream: dict[str, Any]) -> bool:
        return (
            eas_recording_settings_from_stream(stream).enabled
            and eas_alert_index_path(self.streams_directory, stream).exists()
        )

    def _eas_alert_stream_summary(self, stream: dict[str, Any]) -> dict[str, Any]:
        station = stream.get("station", {})
        index_path = eas_alert_index_path(self.streams_directory, stream)
        count = len(load_eas_alert_entries(index_path)) if index_path.exists() else 0
        return {
            "id": stream.get("id", ""),
            "callsign": station.get("callsign", "Unknown"),
            "frequency": station.get("frequency", ""),
            "alert_count": count,
        }

    def _eas_alert_by_id(self, stream: dict[str, Any], alert_id: str) -> tuple[dict[str, Any], int, Path]:
        index_path = eas_alert_index_path(self.streams_directory, stream)
        alerts = load_eas_alert_entries(index_path)
        for index, alert in enumerate(alerts):
            if eas_alert_id(alert, index) == alert_id:
                return alert, index, index_path
        raise ValueError("EAS alert was not found")

    def add_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        station_key = str(payload.get("station_key", "")).strip()
        station = self._station_by_key(station_key)
        icecast = validate_icecast_payload(payload.get("icecast"))
        with self.lock:
            self._reject_duplicate_icecast_locked(icecast)
        auth_cache_key = icecast_auth_cache_key(icecast)
        with self.lock:
            auth_is_cached = self._icecast_auth_cached_locked(auth_cache_key)
        if auth_is_cached:
            LOG.info(
                "using cached Icecast authentication for %s:%s%s",
                icecast["host"],
                icecast["port"],
                icecast["mount"],
            )
        else:
            result = self.test_icecast_auth(icecast)
            if not result["success"]:
                return {
                    "success": False,
                    "message": result["message"],
                    "streams": list(self.streams),
                }
            with self.lock:
                self.icecast_auth_cache[auth_cache_key] = time.time()
        stream = {
            "id": uuid.uuid4().hex,
            "enabled": True,
            "station": station,
            "outputs": [
                {
                    "id": uuid.uuid4().hex,
                    "enabled": True,
                    "type": "icecast",
                    "icecast": icecast,
                    "auth_validated_at": time.time(),
                    "auth_signature": icecast_auth_signature(icecast),
                }
            ],
            "audio": asdict(AudioConfig()),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self.lock:
            self.streams.append(stream)
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": "Stream created.",
            "streams": list(self.streams),
        }

    def test_icecast_auth(self, icecast: dict[str, Any]) -> dict[str, Any]:
        settings = IcecastSettings(
            server=normalize_server(icecast["host"]),
            port=str(icecast["port"]),
            username=icecast["username"],
            password=icecast["password"],
            mountpoint=icecast["mount"],
        )
        LOG.info(
            "testing Icecast authentication to %s@%s:%s%s as %s",
            icecast["username"],
            icecast["host"],
            icecast["port"],
            icecast["mount"],
            icecast["format"],
        )
        result = test_mountpoint_authentication(settings)
        if result.success:
            LOG.info("Icecast authentication succeeded for %s:%s%s", icecast["host"], icecast["port"], icecast["mount"])
            with self.lock:
                self.icecast_auth_cache[icecast_auth_cache_key(icecast)] = time.time()
        else:
            LOG.warning("Icecast authentication failed for %s:%s%s: %s", icecast["host"], icecast["port"], icecast["mount"], result.message)
        return {"success": bool(result.success), "message": result.message}

    def test_icecast_auth_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        icecast = validate_icecast_payload(payload.get("icecast"))
        return self.test_icecast_auth(icecast)

    def update_fallback_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        settings = validate_fallback_settings_payload(payload)
        with self.lock:
            self.fallback_settings = settings
            save_fallback_settings(self.fallback_state_path, settings)
        LOG.info(
            "updated fallback audio settings: enabled=%s delay=%.1fs loop_delay=%.1fs",
            settings.enabled,
            settings.silence_timeout_seconds,
            settings.loop_delay_seconds,
        )
        return self.status()

    def fallback_settings_snapshot(self) -> WebFallbackSettings:
        with self.lock:
            return self.fallback_settings

    def start_monitor(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        stream_id = str(payload.get("stream_id", "")).strip()
        sdp = str(payload.get("sdp", "")).strip()
        offer_type = str(payload.get("type", "offer")).strip() or "offer"
        LOG.info("WebRTC monitor start requested: client=%s stream=%s", client_id or "<missing>", stream_id or "<missing>")
        if not client_id:
            raise ValueError("monitor client id is required")
        if not stream_id:
            raise ValueError("stream id is required")
        if not sdp:
            raise ValueError("WebRTC offer SDP is required")
        capabilities = server_webrtc_capabilities()
        if not capabilities.available:
            LOG.warning(
                "WebRTC monitor unavailable for client %s: transport=%s opus=%s",
                client_id,
                capabilities.transport_error or "ok",
                capabilities.opus_error or "ok",
            )
            raise ValueError(
                "WebRTC monitoring is unavailable: "
                + (capabilities.transport_error or capabilities.opus_error or "server WebRTC support is incomplete")
            )

        self.stop_receiver({"client_id": client_id})
        self.stop_monitor({"client_id": client_id})
        with self.lock:
            stream = self._stream_locked(stream_id)
            if stream.get("enabled", True) is False:
                LOG.warning("WebRTC monitor rejected for client %s: stream %s is disabled", client_id, stream_id)
                raise ValueError("start the stream before monitoring it")
            self.monitor_streams_by_client[client_id] = stream_id
            self.monitor_accounts_by_client[client_id] = int(account_id or 0) if account_id is not None else 0
            self._sync_stream_workers_locked()
            worker = self.stream_workers.get(stream_worker_key(stream))
            if worker is None:
                self.monitor_streams_by_client.pop(client_id, None)
                self.monitor_accounts_by_client.pop(client_id, None)
                LOG.warning("WebRTC monitor rejected for client %s: worker unavailable for stream %s", client_id, stream_id)
                raise ValueError("stream worker could not be started for monitoring")
            source = worker.add_monitor_source(client_id)
            station = stream.get("station", {})

        try:
            track = create_webrtc_pcm_audio_track(source)
            answer = self.webrtc_runner.run(
                self.webrtc_sessions.accept_offer(
                    session_id=client_id,
                    sdp=sdp,
                    type=offer_type,
                    tracks=(track,),
                    on_peer_closed=self._handle_webrtc_session_closed,
                )
            )
        except Exception as exc:
            with self.lock:
                self._remove_monitor_source_locked(client_id)
            LOG.exception("WebRTC monitor negotiation failed for client %s stream %s: %s", client_id, stream_id, exc)
            raise
        LOG.info(
            "started WebRTC monitor for %s on client %s",
            station.get("callsign", stream_id),
            client_id,
        )
        return {
            "success": True,
            "stream_id": stream_id,
            "answer": answer,
            "monitoring": self.monitor_status(client_id),
        }

    def stop_monitor(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("monitor client id is required")
        with self.lock:
            stopped_stream_id = self.monitor_streams_by_client.get(client_id, "")
            self._remove_monitor_source_locked(client_id)
        try:
            self.webrtc_runner.run(self.webrtc_sessions.close(client_id), timeout=3.0)
        except Exception as exc:
            LOG.debug("WebRTC monitor close failed for client %s: %s", client_id, exc)
        if stopped_stream_id:
            LOG.info("stopped WebRTC monitor for stream %s on client %s", stopped_stream_id, client_id)
        return {"success": True, "monitoring": self.monitor_status(client_id)}

    def monitor_status(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            stream_id = self.monitor_streams_by_client.get(client_id, "")
            worker = None
            if stream_id:
                for candidate in self.stream_workers.values():
                    if candidate.id == stream_id:
                        worker = candidate
                        break
            stats = worker.monitor_source_stats(client_id) if worker is not None else {}
        return {"client_id": client_id, "stream_id": stream_id, "stats": stats}

    def _icecast_auth_cached_locked(self, cache_key: str) -> bool:
        now = time.time()
        for key, timestamp in list(self.icecast_auth_cache.items()):
            if now - timestamp > ICECAST_AUTH_CACHE_SECONDS:
                self.icecast_auth_cache.pop(key, None)
        timestamp = self.icecast_auth_cache.get(cache_key)
        return timestamp is not None and now - timestamp <= ICECAST_AUTH_CACHE_SECONDS

    def remove_stream(self, stream_id: str) -> dict[str, Any]:
        stream_id = stream_id.strip()
        with self.lock:
            removed = [stream for stream in self.streams if stream.get("id") == stream_id]
            self.streams = [stream for stream in self.streams if stream.get("id") != stream_id]
            if not removed:
                raise ValueError("stream was not found")
            for client_id, monitored_stream_id in list(self.monitor_streams_by_client.items()):
                if monitored_stream_id == stream_id:
                    self._remove_monitor_source_locked(client_id)
            remove_stream_configs(self.streams_directory, removed)
            self._sync_stream_workers_locked()
        LOG.info("removed stream %s", stream_id)
        return self.stream_status()

    def start_receiver(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        sdp = str(payload.get("sdp", "")).strip()
        offer_type = str(payload.get("type", "offer")).strip() or "offer"
        frequency_hz = validate_receiver_frequency(payload.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ))
        LOG.info(
            "weather receiver start requested: client=%s frequency=%s MHz",
            client_id or "<missing>",
            receiver_frequency_mhz(frequency_hz),
        )
        if not client_id:
            raise ValueError("receiver client id is required")
        if not sdp:
            raise ValueError("WebRTC offer SDP is required")
        capabilities = server_webrtc_capabilities()
        if not capabilities.available:
            LOG.warning(
                "WebRTC receiver unavailable for client %s: transport=%s opus=%s",
                client_id,
                capabilities.transport_error or "ok",
                capabilities.opus_error or "ok",
            )
            raise ValueError(
                "WebRTC receiver is unavailable: "
                + (capabilities.transport_error or capabilities.opus_error or "server WebRTC support is incomplete")
            )
        self.stop_monitor({"client_id": client_id})
        self.stop_receiver({"client_id": client_id})
        with self.lock:
            fanout = self.intermediate_fanout
            if fanout is None:
                LOG.warning("weather receiver rejected for client %s: RTL-SDR capture is not active", client_id)
                raise ValueError("RTL-SDR capture is not active")
            worker = WeatherReceiverWorker(
                client_id=client_id,
                fanout=fanout,
                frequency_hz=frequency_hz,
            )
            self.receiver_workers[client_id] = worker
            self.receiver_accounts_by_client[client_id] = int(account_id or 0) if account_id is not None else 0
            worker.start()
        try:
            track = create_webrtc_pcm_audio_track(worker.source)
            answer = self.webrtc_runner.run(
                self.webrtc_sessions.accept_offer(
                    session_id=client_id,
                    sdp=sdp,
                    type=offer_type,
                    tracks=(track,),
                    on_peer_closed=self._handle_webrtc_session_closed,
                )
            )
        except Exception as exc:
            with self.lock:
                self._remove_receiver_locked(client_id)
            LOG.exception("weather receiver negotiation failed for client %s: %s", client_id, exc)
            raise
        LOG.info("started weather receiver for %s MHz on client %s", receiver_frequency_mhz(frequency_hz), client_id)
        return {
            "success": True,
            "answer": answer,
            "receiver": self.receiver_status(client_id),
        }

    def tune_receiver(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        frequency_hz = validate_receiver_frequency(payload.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ))
        if not client_id:
            raise ValueError("receiver client id is required")
        with self.lock:
            worker = self.receiver_workers.get(client_id)
            if worker is None:
                raise ValueError("weather radio receiver is not playing")
            worker.set_frequency(frequency_hz)
        LOG.info("tuned weather receiver client %s to %s MHz", client_id, receiver_frequency_mhz(frequency_hz))
        return {"success": True, "receiver": self.receiver_status(client_id)}

    def stop_receiver(self, payload: dict[str, Any]) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("receiver client id is required")
        with self.lock:
            stopped = self._remove_receiver_locked(client_id)
        try:
            self.webrtc_runner.run(self.webrtc_sessions.close(client_id), timeout=3.0)
        except Exception as exc:
            LOG.debug("WebRTC receiver close failed for client %s: %s", client_id, exc)
        if stopped:
            LOG.info("stopped weather receiver for client %s", client_id)
        return {"success": True, "receiver": self.receiver_status(client_id)}

    def receiver_status(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            worker = self.receiver_workers.get(client_id)
        if worker is None:
            return {"client_id": client_id, "playing": False}
        snapshot = worker.snapshot()
        snapshot["playing"] = True
        return snapshot

    def update_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        if "enabled" not in payload:
            raise ValueError("stream enabled state is required")
        enabled = bool(payload.get("enabled"))
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["enabled"] = enabled
            stream["updated_at"] = time.time()
            if not enabled:
                for client_id, monitored_stream_id in list(self.monitor_streams_by_client.items()):
                    if monitored_stream_id == stream_id:
                        self._remove_monitor_source_locked(client_id)
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        LOG.info("%s stream %s", "started" if enabled else "stopped", stream_id)
        return self.stream_status()

    def update_eas_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        settings = validate_eas_recording_payload(payload.get("eas_recording", payload))
        with self.lock:
            stream = self._stream_locked(stream_id)
            current_settings = eas_recording_settings_from_stream(stream)
            if current_settings.enabled and not settings.enabled:
                self._ensure_can_disable_eas_recording_locked(stream)
            stream["eas_recording"] = asdict(settings)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        LOG.info("updated EAS recording settings for stream %s", stream_id)
        return self.stream_status()

    def update_audio_effects(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        audio = validate_audio_payload(payload.get("audio", payload))
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["audio"] = audio
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            worker = self.stream_workers.get(stream_worker_key(stream))
            if worker is not None:
                worker.sync_stream(stream)
        LOG.info("updated audio effects for stream %s", stream_id)
        return self.stream_status()

    def update_stream_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        output_id = str(payload.get("output_id", "")).strip()
        enabled = bool(payload.get("enabled", True))
        icecast = validate_icecast_payload(payload.get("icecast"))
        with self.lock:
            stream, output = self._stream_output_locked(stream_id, output_id)
            if output.get("enabled", True) and not enabled:
                self._ensure_can_disable_icecast_output_locked(stream, output_id)
            self._reject_duplicate_icecast_locked(icecast, ignore_output_id=output_id)
            must_test_auth = icecast_auth_changed(output, icecast)
            if must_test_auth:
                settings = IcecastSettings(
                    server=normalize_server(icecast["host"]),
                    port=str(icecast["port"]),
                    username=icecast["username"],
                    password=icecast["password"],
                    mountpoint=icecast["mount"],
                )
                result = test_mountpoint_authentication(settings)
                if not result.success:
                    return {
                        "success": False,
                        "message": result.message,
                        "streams": list(self.streams),
                    }
                output["auth_validated_at"] = time.time()
                output["auth_signature"] = icecast_auth_signature(icecast)
            output["enabled"] = enabled
            output["icecast"] = icecast
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": "Stream output settings saved.",
            "streams": list(self.streams),
        }

    def add_stream_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        icecast = validate_icecast_payload(payload.get("icecast"))
        with self.lock:
            stream = self._stream_locked(stream_id)
            self._reject_duplicate_icecast_locked(icecast)
        result = self.test_icecast_auth(icecast)
        if not result["success"]:
            return {
                "success": False,
                "message": result["message"],
                "streams": list(self.streams),
            }
        with self.lock:
            stream = self._stream_locked(stream_id)
            self._reject_duplicate_icecast_locked(icecast)
            mutable_stream_outputs(stream).append(
                {
                    "id": uuid.uuid4().hex,
                    "enabled": True,
                    "type": "icecast",
                    "icecast": icecast,
                    "auth_validated_at": time.time(),
                    "auth_signature": icecast_auth_signature(icecast),
                }
            )
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": "Icecast output added.",
            "streams": list(self.streams),
        }

    def remove_stream_output(self, stream_id: str, output_id: str) -> dict[str, Any]:
        stream_id = stream_id.strip()
        output_id = output_id.strip()
        with self.lock:
            stream = self._stream_locked(stream_id)
            outputs = stream_outputs(stream)
            self._ensure_can_disable_icecast_output_locked(stream, output_id)
            before = len(outputs)
            stream["outputs"] = [output for output in outputs if output.get("id") != output_id]
            if len(stream["outputs"]) == before:
                raise ValueError("stream output was not found")
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        LOG.info("removed stream output %s from stream %s", output_id, stream_id)
        return self.stream_status()

    def _stream_locked(self, stream_id: str) -> dict[str, Any]:
        for stream in self.streams:
            if stream.get("id") == stream_id:
                return stream
        raise ValueError("stream was not found")

    def _stream_output_locked(self, stream_id: str, output_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        stream = self._stream_locked(stream_id)
        for output in stream_outputs(stream):
            if output.get("id") == output_id:
                return stream, output
        raise ValueError("stream output was not found")

    def _ensure_can_disable_icecast_output_locked(self, stream: dict[str, Any], output_id: str) -> None:
        if enabled_output_count(stream, disabled_icecast_output_id=output_id) <= 0:
            raise ValueError("At least one output must remain enabled for each stream.")

    def _ensure_can_disable_eas_recording_locked(self, stream: dict[str, Any]) -> None:
        if enabled_output_count(stream, eas_enabled=False) <= 0:
            raise ValueError("At least one output must remain enabled for each stream.")

    def _reject_duplicate_icecast_locked(self, icecast: dict[str, Any], ignore_output_id: str | None = None) -> None:
        signature = icecast_auth_signature(icecast)
        for stream in self.streams:
            for output in stream_outputs(stream):
                if ignore_output_id and output.get("id") == ignore_output_id:
                    continue
                if output.get("type", "icecast") != "icecast":
                    continue
                if icecast_auth_signature(output.get("icecast", {})) == signature:
                    raise ValueError("An output with these credentials already exists.")

    def _station_by_key(self, key: str) -> dict[str, str]:
        for station in self.stations:
            if station.get("key") == key:
                return station
        raise ValueError("select a valid NWR station")

    def _select_only_connected_device(self) -> None:
        try:
            devices = self.devices()["devices"]
        except Exception as exc:
            LOG.debug("initial RTL-SDR auto-selection failed: %s", exc)
            return
        if len(devices) != 1:
            return
        with self.lock:
            if self.settings.serial:
                return
            serial = str(devices[0]["serial"]).strip()
            if not serial:
                return
            self.settings = replace(self.settings, serial=serial)
            save_settings(self.state_path, self.settings)
            LOG.info("selected the only connected RTL-SDR: serial=%s", serial)
            self._start_or_update_capture_locked()

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = self._merged_settings(payload)
            self.settings = settings
            save_settings(self.state_path, settings)
            LOG.info(
                "saved RTL settings: serial=%s sample_rate=%s gain=%s ppm=%s bias_tee=%s",
                settings.serial or "<none>",
                settings.sample_rate,
                "auto" if settings.gain is None else f"{settings.gain:g} dB",
                settings.ppm_correction,
                settings.bias_tee,
            )
            if settings.serial:
                self._start_or_update_capture_locked()
            else:
                self._detach_capture_locked()
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
        return replace(settings, **changes)

    def _start_or_update_capture_locked(self) -> None:
        config = self.settings.to_rtl_config()
        if self.capture is None:
            self.capture_error = None
            self.capture = RtlCaptureSource(config)
            self.capture.start()
            self.raw_fanout = RawRtlFanout(self.capture)
            self.monitor_queue = subscribe_raw_fanout(
                self.raw_fanout,
                max_chunks=64,
                name="web-status-drain",
            )
            self.intermediate_fanout = IntermediateIqFanout(self.raw_fanout)
            self.intermediate_fanout.start()
            self.raw_fanout.start()
            self.drain_stop.clear()
            self.drain_thread = threading.Thread(
                target=self._drain_capture,
                name="rtl-web-drain",
                daemon=True,
            )
            self.drain_thread.start()
            self._sync_stream_workers_locked()
            LOG.info("started RTL-SDR control capture for serial %s", config.serial)
            return
        self.capture.apply_config(config)
        self.settings = self._effective_settings_locked()
        save_settings(self.state_path, self.settings)
        self._sync_stream_workers_locked()

    def _effective_settings_locked(self) -> RtlControlSettings:
        if self.capture is None:
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

    def _detach_capture_locked(self) -> None:
        self.drain_stop.set()
        if self.iq_recorder is not None:
            self.iq_recorder.stop()
            self.iq_recorder = None
        self._stop_receiver_workers_locked()
        self._stop_stream_workers_locked()
        intermediate = self.intermediate_fanout
        self.intermediate_fanout = None
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
            recorder = self.iq_recorder
            self.iq_recorder = None
            self._stop_receiver_workers_locked()
            self._stop_stream_workers_locked()
            intermediate = self.intermediate_fanout
            self.intermediate_fanout = None
            fanout = self.raw_fanout
            self.raw_fanout = None
            self.monitor_queue = None
            capture = self.capture
            self.capture = None
            drain_thread = self.drain_thread
            self.drain_thread = None
        if recorder is not None:
            recorder.stop()
        if intermediate is not None:
            intermediate.stop()
        if fanout is not None:
            fanout.stop()
        if capture is not None:
            capture.stop()
        if drain_thread is not None:
            drain_thread.join(timeout=2.0)
        LOG.info("stopped RTL-SDR control capture")

    @staticmethod
    def _stop_detached_capture(
        capture: RtlCaptureSource,
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
            with self.lock:
                self.capture_error = None
                self.last_batch_at = batch.captured_at
                self.received_chunks += 1
                self.received_bytes += len(batch.data)

    def _sync_stream_workers_locked(self) -> None:
        fanout = self.intermediate_fanout
        desired: dict[str, dict[str, Any]] = {}
        monitored_stream_ids = set(self.monitor_streams_by_client.values())
        if fanout is not None:
            for stream in self.streams:
                if not stream.get("enabled", True):
                    continue
                stream_id = str(stream.get("id", ""))
                if (
                    any(output.get("enabled", True) for output in stream_outputs(stream))
                    or stream_id in monitored_stream_ids
                    or eas_recording_settings_from_stream(stream).enabled
                ):
                    desired[stream_worker_key(stream)] = stream

        for key in list(self.stream_workers):
            if key not in desired:
                worker = self.stream_workers.pop(key)
                worker.stop()

        if fanout is None:
            return

        for key, stream in desired.items():
            worker = self.stream_workers.get(key)
            if worker is not None:
                worker.sync_stream(stream)
                continue
            worker = IcecastStreamWorker(
                stream=stream,
                fanout=fanout,
                fallback_settings_provider=self.fallback_settings_snapshot,
                state_directory=self.state_path.parent,
                storage_monitor=self.storage_monitor,
            )
            self.stream_workers[key] = worker
            worker.start()
            LOG.info(
                "started stream worker for %s",
                stream.get("station", {}).get("callsign", "unknown"),
            )

    def _remove_monitor_source_locked(self, client_id: str) -> None:
        stream_id = self.monitor_streams_by_client.pop(client_id, "")
        self.monitor_accounts_by_client.pop(client_id, None)
        if not stream_id:
            return
        for worker in self.stream_workers.values():
            if worker.id == stream_id:
                worker.remove_monitor_source(client_id)
                break
        self._sync_stream_workers_locked()

    def _remove_receiver_locked(self, client_id: str) -> bool:
        worker = self.receiver_workers.pop(client_id, None)
        self.receiver_accounts_by_client.pop(client_id, None)
        if worker is None:
            return False
        worker.stop()
        return True

    def _handle_webrtc_session_closed(self, client_id: str, reason: str) -> None:
        def cleanup() -> None:
            with self.lock:
                stopped_stream_id = self.monitor_streams_by_client.get(client_id, "")
                if stopped_stream_id:
                    self._remove_monitor_source_locked(client_id)
                stopped_receiver = self._remove_receiver_locked(client_id)
            if stopped_stream_id:
                LOG.info(
                    "cleaned up stale WebRTC monitor for stream %s on client %s after %s",
                    stopped_stream_id,
                    client_id,
                    reason,
                )
            if stopped_receiver:
                LOG.info(
                    "cleaned up stale weather receiver for client %s after %s",
                    client_id,
                    reason,
                )

        threading.Thread(
            target=cleanup,
            name=f"webrtc-cleanup-{client_id}",
            daemon=True,
        ).start()

    def _stop_stream_workers_locked(self) -> None:
        for worker in list(self.stream_workers.values()):
            worker.stop()
        self.stream_workers = {}
        self.monitor_streams_by_client = {}
        self.monitor_accounts_by_client = {}

    def _stop_receiver_workers_locked(self) -> None:
        for worker in list(self.receiver_workers.values()):
            worker.stop()
        self.receiver_workers = {}
        self.receiver_accounts_by_client = {}

    def _active_streams_locked(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for worker in self.stream_workers.values():
            if hasattr(worker, "snapshots"):
                snapshots.extend(worker.snapshots())
            else:
                snapshots.append(worker.snapshot())
        return snapshots

    def _active_eas_recorders_locked(self) -> list[dict[str, Any]]:
        snapshots = []
        for worker in self.stream_workers.values():
            snapshot = worker.eas_snapshot()
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def _recent_eas_alerts_locked(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECENT_EAS_ALERT_SECONDS)
        alerts: list[dict[str, Any]] = []
        for stream in self.streams:
            station = stream.get("station", {})
            callsign = str(station.get("callsign", "Unknown"))
            index_path = eas_alert_index_path(self.streams_directory, stream)
            try:
                indexed_alerts = indexed_eas_alert_entries(index_path)
            except Exception as exc:
                LOG.warning("could not read EAS alert index for %s: %s", callsign, exc)
                continue
            for index, alert in indexed_alerts:
                issued_at = parse_utc_datetime(str(alert.get("start_time_utc", "")))
                if issued_at < cutoff:
                    continue
                event = lookup_event(str(alert.get("event_type", "")))
                alerts.append(
                    {
                        "id": eas_alert_id(alert, index),
                        "stream_id": stream.get("id", ""),
                        "callsign": callsign,
                        "frequency": station.get("frequency", ""),
                        "event_name": event.display_name,
                        "issued_at_utc": issued_at.isoformat().replace("+00:00", "Z"),
                        "issued_at_epoch": issued_at.timestamp(),
                    }
                )
        alerts.sort(key=lambda item: (-float(item["issued_at_epoch"]), str(item["callsign"])))
        return alerts[:RECENT_EAS_ALERT_LIMIT]


class RtlControlHandler(BaseHTTPRequestHandler):
    service: RtlControlService

    def log_message(self, format: str, *args) -> None:
        LOG.debug("HTTP %s - %s", self.address_string(), format % args)

    def _client_address(self) -> str:
        host = self.client_address[0] if self.client_address else self.address_string()
        port = self.client_address[1] if self.client_address and len(self.client_address) > 1 else ""
        return f"{host}:{port}" if port else host

    def _auth_ok_or_setup_response(self, path: str, method: str) -> bool:
        self.current_account = None
        has_account = self.service.accounts.has_account()
        if not has_account:
            if method == "GET":
                self._send_html(SETUP_HTML)
                return False
            if method == "POST" and path == "/api/setup-account":
                return True
            self._send_json({"error": "administrator account setup is required"}, status=HTTPStatus.FORBIDDEN)
            return False
        if path == "/api/setup-account":
            self.send_error(HTTPStatus.NOT_FOUND)
            return False
        account = self._session_auth_valid()
        if isinstance(account, AccountRecord):
            self.current_account = account
            if not self._account_authorized_or_response(path, method, account):
                return False
            return True
        account = self._basic_auth_valid()
        if isinstance(account, AccountRecord):
            self.current_account = account
            self._pending_auth_session_cookie = self.service.auth_sessions.cookie_header(
                self.service.auth_sessions.create(account.id)
            )
            if not self._account_authorized_or_response(path, method, account):
                return False
            return True
        self._send_auth_required()
        return False

    def _session_auth_valid(self) -> AccountRecord | None:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        try:
            cookies = SimpleCookie(cookie_header)
        except Exception:
            return None
        morsel = cookies.get(AUTH_SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        account = self.service.auth_sessions.validate(morsel.value, self.service.accounts)
        return account if isinstance(account, AccountRecord) else None

    def _basic_auth_valid(self) -> AccountRecord | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:].strip().encode("ascii"), validate=True).decode("utf-8")
        except (UnicodeDecodeError, binascii.Error):
            return None
        username, separator, password = decoded.partition(":")
        if not separator:
            return None
        return self.service.accounts.verify_basic_account(username, password)

    def _account_authorized_or_response(self, path: str, method: str, account: AccountRecord) -> bool:
        if account.must_change_password:
            if method == "GET" and path == "/":
                self._send_html(MUST_CHANGE_PASSWORD_HTML)
                return False
            if path in {"/api/account/me", "/api/account/password", "/api/client-log"}:
                return True
            self._send_json({"error": "password change is required"}, status=HTTPStatus.FORBIDDEN)
            return False
        if path.startswith("/api/accounts") or path in {"/api/account-reset-password", "/api/account-read-only"}:
            if not account.is_owner:
                self._send_json({"error": "owner account is required"}, status=HTTPStatus.FORBIDDEN)
                return False
            return True
        if path == "/api/account/password":
            return True
        if account.is_read_only and not self._read_only_request_allowed(path, method):
            self._send_json({"error": "this account is read-only"}, status=HTTPStatus.FORBIDDEN)
            return False
        return True

    def _read_only_request_allowed(self, path: str, method: str) -> bool:
        if method == "GET":
            return path in {
                "/",
                "/api/status",
                "/api/stations",
                "/api/streams",
                "/api/eas-alert-streams",
                "/api/eas-alerts",
                "/api/eas-alert-bulk-options",
                "/api/eas-alert-range-count",
                "/api/eas-alert-export",
                "/api/eas-alert",
                "/api/eas-alert-audio",
                "/api/webrtc-capabilities",
                "/api/monitor/status",
                "/api/receiver/status",
                "/api/iq-recorder/status",
                "/api/iq-recordings",
                "/api/iq-recording-download",
                "/api/logs",
                "/api/account/me",
            }
        if method == "POST":
            return path in {
                "/api/client-log",
                "/api/monitor/start",
                "/api/monitor/stop",
                "/api/receiver/start",
                "/api/receiver/tune",
                "/api/receiver/stop",
                "/api/account/password",
            }
        return False

    def _send_auth_required(self) -> None:
        payload = b'{"error":"authentication required"}'
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"')
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"{AUTH_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before auth response could be written")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._auth_ok_or_setup_response(path, "GET"):
            return
        if path == "/":
            self._send_html(INDEX_HTML)
        elif path == "/api/status":
            response = self.service.status()
            account = getattr(self, "current_account", None)
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
        elif path == "/api/stations":
            query = parse_qs(parsed.query)
            search = query.get("q", [""])[0]
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            self._send_json(self.service.search_stations(search, limit))
        elif path == "/api/streams":
            self._send_json(self.service.stream_status())
        elif path == "/api/eas-alert-streams":
            self._send_json(self.service.eas_alert_streams())
        elif path == "/api/webrtc-capabilities":
            self._send_json(server_webrtc_capabilities().to_dict())
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
                updated = self.service.accounts.change_password(
                    account.id,
                    str(payload.get("current_password", "")),
                    str(payload.get("new_password", "")),
                    str(payload.get("confirm_password", "")),
                )
                self.service.auth_sessions.invalidate_account(account.id)
                self.service.revoke_account_long_lived_resources(
                    account.id,
                    stop_webrtc=True,
                    abort_downloads=True,
                    stop_iq_recording=True,
                )
                self._pending_auth_session_cookie = (
                    f"{AUTH_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOG.info("changed password for account %s", updated.get("username", ""))
            self._send_json({"success": True, "account": updated})
            return
        if path == "/api/accounts":
            try:
                payload = self._read_json()
                account, secret = self.service.accounts.create_account(
                    str(payload.get("username", "")),
                    bool(payload.get("read_only", False)),
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOG.info("created account %s", account.get("username", ""))
            self._send_json({"success": True, "account": account, "secret": secret})
            return
        if path == "/api/account-reset-password":
            try:
                requester = getattr(self, "current_account", None)
                if not isinstance(requester, AccountRecord):
                    raise ValueError("authentication is required")
                payload = self._read_json()
                account_id = int(payload.get("account_id", 0))
                if requester.id == account_id:
                    raise ValueError("use change account password for your own account")
                account, secret = self.service.accounts.reset_password(account_id)
                self.service.auth_sessions.invalidate_account(account_id)
                self.service.revoke_account_long_lived_resources(
                    account_id,
                    stop_webrtc=True,
                    abort_downloads=True,
                    stop_iq_recording=True,
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOG.info("reset password for account %s", account.get("username", ""))
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
        if path == "/api/client-log":
            try:
                payload = self._read_json()
                level = str(payload.get("level", "info")).lower()
                area = str(payload.get("area", "client"))[:40]
                message = str(payload.get("message", ""))[:240]
                details = payload.get("details", {})
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
        if path == "/api/monitor/start":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.start_monitor(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API monitor start failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/monitor/stop":
            try:
                payload = self._read_json()
                response = self.service.stop_monitor(payload)
            except Exception as exc:
                LOG.warning("API monitor stop failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/start":
            try:
                payload = self._read_json()
                account = getattr(self, "current_account", None)
                response = self.service.start_receiver(
                    payload,
                    account.id if isinstance(account, AccountRecord) else None,
                )
            except Exception as exc:
                LOG.warning("API receiver start failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/tune":
            try:
                payload = self._read_json()
                response = self.service.tune_receiver(payload)
            except Exception as exc:
                LOG.warning("API receiver tune failed for %s: %s", self._client_address(), exc)
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self._send_json(response)
            return
        if path == "/api/receiver/stop":
            try:
                payload = self._read_json()
                response = self.service.stop_receiver(payload)
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
                response = self.service.stop_iq_recording()
            except Exception as exc:
                LOG.warning("API I/Q recorder stop failed for %s: %s", self._client_address(), exc)
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
                requester = getattr(self, "current_account", None)
                if not isinstance(requester, AccountRecord):
                    raise ValueError("authentication is required")
                account_id = int(parse_qs(parsed.query).get("account_id", ["0"])[0])
                account = self.service.accounts.delete_account(account_id, requester.id)
                self.service.auth_sessions.invalidate_account(account_id)
                self.service.revoke_account_long_lived_resources(
                    account_id,
                    stop_webrtc=True,
                    abort_downloads=True,
                    stop_iq_recording=True,
                )
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOG.info("deleted account %s", account.get("username", ""))
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
                account_id = int(payload.get("account_id", 0))
                read_only = bool(payload.get("read_only", False))
                account = self.service.accounts.set_read_only(account_id, read_only)
                if read_only:
                    self.service.revoke_account_long_lived_resources(account_id, stop_iq_recording=True)
            except Exception as exc:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            LOG.info("updated account %s role to %s", account.get("username", ""), account.get("role", ""))
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

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length > MAX_JSON_REQUEST_BYTES:
            raise ValueError("request body is too large")
        data = self.rfile.read(length) if length else b"{}"
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self._send_pending_auth_session_cookie()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before JSON response could be written")

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_pending_auth_session_cookie()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before HTML response could be written")

    def _send_file(self, path: Path, download_name: str, download: bool, *, delete_after: bool = False) -> None:
        content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        data_length = path.stat().st_size
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(data_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'{disposition}; filename="{http_header_filename(download_name)}"')
            self._send_pending_auth_session_cookie()
            self.end_headers()
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        LOG.debug("client disconnected before file response could be written")
                        break
        finally:
            if delete_after:
                path.unlink(missing_ok=True)

    def _send_iq_recording(
        self,
        path: Path,
        download_name: str,
        data_length: int,
        fmt: str,
        recording_id: str,
        account_id: int | None = None,
    ) -> None:
        download_id = self.service.begin_iq_recording_download(recording_id, account_id)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(data_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'attachment; filename="{http_header_filename(download_name)}"')
            self._send_pending_auth_session_cookie()
            self.end_headers()
            with path.open("rb") as source:
                for chunk in convert_iq_recording_chunks(source, fmt):
                    if self.service.iq_recording_download_aborted(download_id):
                        LOG.info("I/Q recording download aborted after account revoke: %s", recording_id)
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        LOG.info("I/Q recording download disconnected before completion: %s", recording_id)
                        break
        finally:
            self.service.finish_iq_recording_download(download_id)

    def _send_pending_auth_session_cookie(self) -> None:
        cookie = getattr(self, "_pending_auth_session_cookie", "")
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._pending_auth_session_cookie = ""


def default_state_path() -> Path:
    systemd_state_directory = systemd_directory_path("STATE_DIRECTORY")
    if systemd_state_directory is not None:
        base = systemd_state_directory
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser() / STATE_DIRECTORY_NAME
    else:
        base = Path.home() / ".local" / "state" / STATE_DIRECTORY_NAME
    return base / STATE_FILE_NAME


def systemd_directory_path(variable: str) -> Path | None:
    value = os.environ.get(variable, "").strip()
    if not value:
        return None
    return Path(value.split(":", 1)[0]).expanduser()


def load_settings(path: Path) -> RtlControlSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RtlControlSettings()
    except Exception as exc:
        LOG.warning("failed to load RTL control settings from %s: %s", path, exc)
        return RtlControlSettings()
    if not isinstance(raw, dict):
        return RtlControlSettings()
    try:
        return RtlControlSettings(
            serial=str(raw.get("serial", "")).strip(),
            sample_rate=DEFAULT_RTL_SAMPLE_RATE,
            gain=None if raw.get("gain") is None else float(raw["gain"]),
            ppm_correction=validate_ppm_correction(int(raw.get("ppm_correction", 0))),
            bias_tee=bool(raw.get("bias_tee", False)),
        )
    except Exception as exc:
        LOG.warning("RTL control settings in %s are invalid: %s", path, exc)
        return RtlControlSettings()


def save_settings(path: Path, settings: RtlControlSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_fallback_settings(path: Path) -> WebFallbackSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WebFallbackSettings()
    except Exception as exc:
        LOG.warning("failed to load fallback audio settings from %s: %s", path, exc)
        return WebFallbackSettings()
    if not isinstance(raw, dict):
        return WebFallbackSettings()
    try:
        return validate_fallback_settings_payload(raw)
    except Exception as exc:
        LOG.warning("fallback audio settings in %s are invalid: %s", path, exc)
        return WebFallbackSettings()


def save_fallback_settings(path: Path, settings: WebFallbackSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def eas_recording_settings_from_stream(stream: dict[str, Any]) -> WebEasRecordingSettings:
    raw = stream.get("eas_recording")
    if not isinstance(raw, dict):
        return WebEasRecordingSettings()
    try:
        return validate_eas_recording_payload(raw)
    except Exception as exc:
        LOG.warning("EAS recording settings for stream %s are invalid: %s", stream.get("id"), exc)
        return WebEasRecordingSettings()


def validate_eas_recording_payload(raw: Any) -> WebEasRecordingSettings:
    if not isinstance(raw, dict):
        raise ValueError("EAS recording settings are required")
    enabled = bool(raw.get("enabled", False))
    pre_seconds = float(raw.get("pre_seconds", 2.0))
    post_seconds = float(raw.get("post_seconds", 5.0))
    max_seconds = raw.get("max_seconds", 120)
    if not isinstance(max_seconds, int) or isinstance(max_seconds, bool):
        raise ValueError("Maximum recording time must be a whole number of seconds")
    if not 0 <= pre_seconds <= 10:
        raise ValueError("Pre-recording time must be from 0 through 10 seconds")
    if not 0 <= post_seconds <= 10:
        raise ValueError("Post-recording time must be from 0 through 10 seconds")
    if max_seconds <= 0:
        raise ValueError("Maximum recording time must be greater than 0 seconds")
    format_value = str(raw.get("format", "wav")).strip().lower()
    if format_value not in {"mp3", "wav"}:
        raise ValueError("EAS recording format must be MP3 or WAV")
    return WebEasRecordingSettings(
        enabled=enabled,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        max_seconds=max_seconds,
        format=format_value,
    )


def eas_config_from_stream(stream: dict[str, Any], state_directory: Path, settings: WebEasRecordingSettings) -> EasRecordingConfig:
    directory = stream_alerts_directory(state_directory, stream)
    if settings.enabled:
        directory.mkdir(parents=True, exist_ok=True)
    return EasRecordingConfig(
        enabled=settings.enabled,
        pre_seconds=settings.pre_seconds,
        post_seconds=settings.post_seconds,
        max_seconds=settings.max_seconds,
        directory=str(directory),
        format=settings.format,
        local_time=False,
    )


def audio_config_from_stream(stream: dict[str, Any]) -> AudioConfig:
    raw = stream.get("audio")
    if not isinstance(raw, dict):
        return AudioConfig()
    try:
        return parse_audio_config(raw)
    except Exception as exc:
        LOG.warning("audio effects settings for stream %s are invalid: %s", stream.get("id"), exc)
        return AudioConfig()


def validate_audio_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("audio effects settings are required")
    try:
        return asdict(parse_audio_config(raw))
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def stream_alerts_directory(state_directory: Path, stream: dict[str, Any]) -> Path:
    station = stream.get("station", {})
    callsign = sanitize_path_component(str(station.get("callsign", "")).strip() or str(stream.get("id", "stream")))
    return state_directory / "streams" / callsign / "alerts"


EXPORT_ALERT_PRESETS = (
    {"id": "last_24", "label": "Last 24 hours"},
    {"id": "last_7", "label": "Last 7 days"},
    {"id": "last_30", "label": "Last 30 days"},
    {"id": "all", "label": "Export all alerts"},
)
DELETE_ALERT_PRESETS = (
    {"id": "older_24", "label": "Older than 24 hours"},
    {"id": "older_7", "label": "Older than 7 days"},
    {"id": "older_30", "label": "Older than 30 days"},
    {"id": "all", "label": "Delete all alerts"},
)


def eas_alert_index_path(streams_directory: Path, stream: dict[str, Any]) -> Path:
    return stream_alerts_directory(streams_directory.parent, stream) / "index.json"


def load_eas_alert_index(index_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "alerts": []}
    if isinstance(data, list):
        data = {"version": 1, "alerts": data}
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        raise ValueError("EAS alert index must contain an alerts array")
    if not all(isinstance(alert, dict) for alert in data["alerts"]):
        raise ValueError("Every EAS alert index entry must be an object")
    data["version"] = int(data.get("version", 1))
    return data


def load_eas_alert_entries(index_path: Path) -> list[dict[str, Any]]:
    return list(load_eas_alert_index(index_path)["alerts"])


def indexed_eas_alert_entries(index_path: Path) -> list[tuple[int, dict[str, Any]]]:
    return list(enumerate(load_eas_alert_entries(index_path)))


def eas_alert_id(alert: dict[str, Any], index: int) -> str:
    digest = hashlib.sha256(
        "\0".join(
            [
                str(index),
                str(alert.get("raw_same_header", "")),
                str(alert.get("start_time_utc", "")),
                str(alert.get("file_path", "")),
            ]
        ).encode("utf-8", errors="replace")
    ).hexdigest()
    return digest[:24]


def eas_alert_summary(stream: dict[str, Any], alert: dict[str, Any], index: int) -> dict[str, Any]:
    event = lookup_event(str(alert.get("event_type", "")))
    issued_at = parse_utc_datetime(str(alert.get("start_time_utc", "")))
    return {
        "id": eas_alert_id(alert, index),
        "summary": f"{sentence_case_event(event.display_name)} issued {format_local_datetime(issued_at, separator='at')}",
        "event_name": event.display_name,
        "issued_at": format_local_datetime(issued_at),
    }


def eas_alert_detail(stream: dict[str, Any], alert: dict[str, Any], index: int) -> dict[str, Any]:
    event = lookup_event(str(alert.get("event_type", "")))
    issued_at = parse_utc_datetime(str(alert.get("start_time_utc", "")))
    expires_at = parse_utc_datetime(str(alert.get("expires_at_utc", "")))
    areas = [
        format_same_location_for_alert(str(code))
        for code in alert.get("fips_codes", [])
        if isinstance(code, str)
    ]
    return {
        "id": eas_alert_id(alert, index),
        "event_type": event.display_name,
        "areas": areas,
        "issued_at": format_local_datetime(issued_at),
        "expires_at": format_local_datetime(expires_at),
        "audio_url": f"/api/eas-alert-audio?stream_id={stream.get('id', '')}&alert_id={eas_alert_id(alert, index)}",
        "download_url": f"/api/eas-alert-audio?stream_id={stream.get('id', '')}&alert_id={eas_alert_id(alert, index)}&download=1",
    }


def format_same_location_for_alert(same_code: str) -> str:
    raw_code = str(same_code).strip()
    location = lookup_location(raw_code)
    subdivision_digit = ""
    if not location.known and re.fullmatch(r"\d{6}", raw_code) and raw_code[0] != "0":
        location = lookup_location(f"0{raw_code[1:]}")
        subdivision_digit = raw_code[0] if location.known else ""
    if not location.known:
        return location.display_name
    if location.location_type == "marine":
        text = location.name
    elif location.state and location.state not in location.name:
        text = f"{location.name}, {location.state}"
    else:
        text = location.display_name
    if subdivision_digit:
        return f"{text}, subdivision {subdivision_digit}"
    return text


def local_datetime_parts(value: datetime) -> dict[str, int]:
    return {
        "year": value.year,
        "month": value.month,
        "day": value.day,
        "hour": value.hour,
        "minute": value.minute,
    }


def preset_range_bounds(mode: str, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    now = now or datetime.now(timezone.utc)
    if mode == "last_24":
        return now - timedelta(hours=24), now
    if mode == "last_7":
        return now - timedelta(days=7), now
    if mode == "last_30":
        return now - timedelta(days=30), now
    if mode == "older_24":
        return None, now - timedelta(hours=24)
    if mode == "older_7":
        return None, now - timedelta(days=7)
    if mode == "older_30":
        return None, now - timedelta(days=30)
    if mode == "all":
        return None, now
    raise ValueError("Unsupported EAS alert range.")


def alert_range_bounds_from_request(mode: str, start: str = "", end: str = "") -> tuple[datetime | None, datetime | None]:
    mode = str(mode).strip()
    if mode == "manual":
        start_time = parse_request_datetime(start, "Start")
        end_time = parse_request_datetime(end, "End")
        now = datetime.now(timezone.utc)
        if start_time > now or end_time > now:
            raise ValueError("Date ranges cannot be in the future.")
        if start_time > end_time:
            raise ValueError("Start date must be before the end date.")
        return start_time, end_time
    return preset_range_bounds(mode)


def parse_request_datetime(value: str, label: str) -> datetime:
    if not value:
        raise ValueError(f"{label} date and time are required.")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} date and time are invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def filter_indexed_alerts(
    indexed_alerts: list[tuple[int, dict[str, Any]]],
    bounds: tuple[datetime | None, datetime | None],
) -> list[tuple[int, dict[str, Any]]]:
    start, end = bounds
    selected = []
    for index, alert in indexed_alerts:
        issued = parse_utc_datetime(str(alert.get("start_time_utc", "")))
        if start is not None and issued < start:
            continue
        if end is not None and issued > end:
            continue
        selected.append((index, alert))
    selected.sort(key=lambda item: parse_utc_datetime(str(item[1].get("start_time_utc", ""))), reverse=True)
    return selected


def unique_archive_name(name: str, used_names: set[str]) -> str:
    cleaned = sanitize_archive_filename(name)
    stem = Path(cleaned).stem or "alert"
    suffix = Path(cleaned).suffix
    candidate = cleaned
    counter = 2
    while candidate in used_names:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def sanitize_archive_filename(value: str) -> str:
    name = Path(str(value)).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")
    return name or "alert"


def safe_eas_alert_file_path(
    streams_directory: Path,
    stream: dict[str, Any],
    alert: dict[str, Any],
    *,
    require_exists: bool = True,
) -> Path:
    alerts_directory = stream_alerts_directory(streams_directory.parent, stream).resolve()
    raw_path = alert.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("EAS alert audio file is missing")
    path = Path(raw_path).expanduser().resolve()
    if require_exists and not path.is_file():
        raise ValueError("EAS alert audio file was not found")
    if path != alerts_directory and alerts_directory not in path.parents:
        raise ValueError("EAS alert audio path is outside the alert directory")
    return path


def alert_download_name(stream: dict[str, Any], alert: dict[str, Any]) -> str:
    station = stream.get("station", {})
    callsign = sanitize_path_component(str(station.get("callsign", "alert")))
    event = sanitize_path_component(str(alert.get("event_type", "EAS")))
    issued = sanitize_path_component(str(alert.get("start_time_utc", "")).replace(":", ""))
    suffix = Path(str(alert.get("file_path", ""))).suffix.lower()
    if suffix not in {".wav", ".mp3", ".ogg"}:
        suffix = ".wav"
    return f"{callsign}-{event}-{issued or 'alert'}{suffix}"


def parse_utc_datetime(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_local_datetime(value: datetime, *, separator: str = ",") -> str:
    local = value.astimezone()
    time_text = local.strftime("%I:%M %p %Z")
    if time_text.startswith("0"):
        time_text = time_text[1:]
    date_text = f"{local.strftime('%B')} {local.day}, {local.year}"
    if separator == ",":
        return f"{date_text}, {time_text}"
    return f"{date_text} {separator} {time_text}"


def sentence_case_event(name: str) -> str:
    if not name:
        return "Unknown event"
    return name[:1].upper() + name[1:].lower()


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as fp:
        fp.write(data)
        temp_path = Path(fp.name)
    try:
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def http_header_filename(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9._ -]+', "_", value).replace('"', "_")


def sanitize_path_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return sanitized or "stream"


def unique_iq_recording_path(directory: Path, file_name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cleaned = sanitize_archive_filename(file_name)
    if not cleaned.lower().endswith(".cf32"):
        cleaned = f"{Path(cleaned).stem or 'iq-recording'}.cf32"
    candidate = directory / cleaned
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def load_iq_recording_index(index_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "recordings": []}
    if not isinstance(data, dict) or not isinstance(data.get("recordings"), list):
        raise ValueError("I/Q recording index must contain a recordings array")
    data["recordings"] = [entry for entry in data["recordings"] if isinstance(entry, dict)]
    data["version"] = int(data.get("version", 1))
    return data


def load_iq_recording_entries(index_path: Path) -> list[dict[str, Any]]:
    return list(load_iq_recording_index(index_path)["recordings"])


def upsert_iq_recording_metadata(index_path: Path, metadata: dict[str, Any]) -> None:
    data = load_iq_recording_index(index_path)
    recording_id = str(metadata.get("id", "")).strip()
    if not recording_id:
        raise ValueError("I/Q recording metadata id is required")
    replaced = False
    updated = []
    for entry in data["recordings"]:
        if str(entry.get("id", "")) == recording_id:
            updated.append(metadata)
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        updated.append(metadata)
    data["recordings"] = updated
    atomic_write_json(index_path, data)


def find_iq_recording(index_path: Path, recording_id: str) -> dict[str, Any]:
    for recording in load_iq_recording_entries(index_path):
        if str(recording.get("id", "")) == recording_id:
            return recording
    raise ValueError("I/Q recording was not found")


def safe_iq_recording_file_path(
    recordings_directory: Path,
    recording: dict[str, Any],
    *,
    require_exists: bool = True,
) -> Path:
    base = recordings_directory.resolve()
    raw_path = recording.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("I/Q recording file is missing")
    path = Path(raw_path).expanduser().resolve()
    if require_exists and not path.is_file():
        raise ValueError("I/Q recording file was not found")
    if path != base and base not in path.parents:
        raise ValueError("I/Q recording file path is outside the recording directory")
    return path


def iq_recording_summary(recording: dict[str, Any]) -> dict[str, Any]:
    started_at = float(recording.get("started_at", 0.0) or 0.0)
    return {
        "id": str(recording.get("id", "")),
        "recorded_at": format_local_datetime(datetime.fromtimestamp(started_at, timezone.utc)),
        "started_at": started_at,
        "duration_seconds": float(recording.get("duration_seconds", 0.0) or 0.0),
        "duration": format_duration_hms(float(recording.get("duration_seconds", 0.0) or 0.0)),
        "sample_rate": int(recording.get("sample_rate", 0) or 0),
        "frequency_hz": int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ),
        "mode": str(recording.get("mode", "")),
        "stream_label": str(recording.get("stream_label", "")),
        "bytes": int(recording.get("bytes", 0) or 0),
    }


def format_duration_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    remaining = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{remaining:02d}"
    return f"{minutes}:{remaining:02d}"


def validate_iq_download_format(raw: Any) -> str:
    fmt = str(raw or "cf").strip().lower()
    aliases = {
        "float32": "cf",
        "cf32": "cf",
        "complex_float32": "cf",
        "signed16": "s16",
        "int16": "s16",
        "unsigned8": "u8",
        "uint8": "u8",
    }
    fmt = aliases.get(fmt, fmt)
    if fmt not in {"cf", "s16", "u8"}:
        raise ValueError("select a valid I/Q download format")
    return fmt


def iq_recording_download_size(path: Path, fmt: str) -> int:
    cf_size = path.stat().st_size
    if cf_size % 4:
        raise ValueError("I/Q recording file is not a valid float32 stream")
    if fmt == "cf":
        return cf_size
    if fmt == "s16":
        return cf_size // 2
    if fmt == "u8":
        return cf_size // 4
    raise ValueError("select a valid I/Q download format")


def iq_recording_download_name(recording: dict[str, Any], fmt: str) -> str:
    sample_rate = int(recording.get("sample_rate", 0) or 0)
    frequency_hz = int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ)
    started_at = datetime.fromtimestamp(float(recording.get("started_at", time.time()) or time.time()), timezone.utc).astimezone()
    date_text = started_at.strftime("%m%d%Y")
    time_text = started_at.strftime("%H%M")
    return f"nwrstmgr-s{sample_rate}-f{frequency_hz}-{date_text}-{time_text}-{fmt}.raw"


def estimate_iq_storage_remaining_seconds(
    recorder: dict[str, Any],
    storage: dict[str, Any],
    recordings_directory: Path,
) -> float | None:
    if not recorder.get("active"):
        return None
    elapsed = float(recorder.get("elapsed_seconds", 0.0) or 0.0)
    bytes_written = int(recorder.get("bytes_written", 0) or 0)
    if elapsed <= 0.0 or bytes_written <= 0:
        return None
    write_rate = bytes_written / elapsed
    if write_rate <= 0.0:
        return None
    target_device = None
    try:
        target_device = int(os.stat(recordings_directory if recordings_directory.exists() else recordings_directory.parent).st_dev)
    except OSError:
        pass
    filesystems = storage.get("filesystems", []) if isinstance(storage, dict) else []
    filesystem = None
    if target_device is not None:
        for candidate in filesystems:
            if candidate.get("device_id") == target_device:
                filesystem = candidate
                break
    if filesystem is None and filesystems:
        filesystem = filesystems[0]
    if not filesystem:
        return None
    available_bytes = int(filesystem.get("available_bytes", 0) or 0)
    total_bytes = int(filesystem.get("total_bytes", 0) or 0)
    critical_by_percent = int(total_bytes * (STORAGE_CRITICAL_FREE_PERCENT / 100.0)) if total_bytes > 0 else 0
    reserve_bytes = max(STORAGE_CRITICAL_FREE_BYTES, critical_by_percent)
    usable_bytes = max(0, available_bytes - reserve_bytes)
    return usable_bytes / write_rate


def iq_storage_estimate_correction_threshold(seconds: float) -> float:
    return max(
        IQ_STORAGE_ESTIMATE_MIN_CORRECTION_SECONDS,
        min(
            IQ_STORAGE_ESTIMATE_MAX_CORRECTION_SECONDS,
            max(0.0, seconds) * IQ_STORAGE_ESTIMATE_CORRECTION_FRACTION,
        ),
    )


def smooth_iq_storage_remaining_seconds(
    *,
    previous: float | None,
    elapsed_since_update: float,
    raw: float | None,
) -> float | None:
    if raw is None:
        return previous
    if previous is None:
        return max(0.0, raw)
    expected = max(0.0, previous - max(0.0, elapsed_since_update))
    threshold = iq_storage_estimate_correction_threshold(expected)
    if raw < expected - threshold:
        return max(0.0, raw)
    if raw > expected + threshold:
        return expected + threshold
    return expected


def convert_iq_recording_chunks(source, fmt: str):
    fmt = validate_iq_download_format(fmt)
    carry = b""
    while True:
        raw = carry + source.read(IQ_DOWNLOAD_CHUNK_BYTES)
        if not raw:
            break
        usable = len(raw) - (len(raw) % 4)
        carry = raw[usable:]
        if usable <= 0:
            continue
        if fmt == "cf":
            yield raw[:usable]
            time.sleep(IQ_DOWNLOAD_YIELD_SECONDS)
            continue
        floats = np.frombuffer(raw[:usable], dtype="<f4")
        clipped = np.clip(floats, -1.0, 1.0)
        if fmt == "s16":
            yield np.round(clipped * 32767.0).astype("<i2").tobytes()
        else:
            yield np.round(clipped * 127.5 + 127.5).astype(np.uint8).tobytes()
        time.sleep(IQ_DOWNLOAD_YIELD_SECONDS)
    if carry:
        raise ValueError("I/Q recording file ended with a partial float32 sample")


def validate_iq_recording_duration(raw: Any) -> float:
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("I/Q recording duration must be a number of seconds") from exc
    if duration < IQ_RECORDER_MIN_DURATION_SECONDS or duration > IQ_RECORDER_MAX_DURATION_SECONDS:
        raise ValueError("I/Q recording duration must be 0 for manual stop, or up to 24 hours")
    return duration


def validate_iq_recording_sample_rate(raw: Any) -> int:
    try:
        sample_rate = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("I/Q recording sample rate must be a number") from exc
    if sample_rate not in IQ_RECORDER_SAMPLE_RATES:
        raise ValueError("select a supported I/Q recording sample rate")
    if sample_rate > DEFAULT_RTL_SAMPLE_RATE:
        raise ValueError("I/Q recording sample rate cannot be higher than the RTL-SDR sample rate")
    return sample_rate


def validate_fallback_settings_payload(raw: Any) -> WebFallbackSettings:
    if not isinstance(raw, dict):
        raise ValueError("fallback audio settings are required")
    enabled = bool(raw.get("enabled", False))
    silence_timeout_seconds = float(raw.get("silence_timeout_seconds", 30.0))
    loop_delay_seconds = float(raw.get("loop_delay_seconds", 5.0))
    if not 30 <= silence_timeout_seconds <= 120:
        raise ValueError("Fallback delay must be from 30 through 120 seconds")
    if not 0 <= loop_delay_seconds <= 10:
        raise ValueError("Seconds before restart must be from 0 through 10 seconds")
    return WebFallbackSettings(
        enabled=enabled,
        silence_timeout_seconds=silence_timeout_seconds,
        loop_delay_seconds=loop_delay_seconds,
    )


def station_key(station: dict[str, Any]) -> str:
    return "|".join(
        str(station.get(key, "")).strip()
        for key in ("callsign", "frequency", "state", "city", "site_name")
    )


def load_station_database(path: Path = STATIONS_ASSET_PATH) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stations = raw["stations"]
    except Exception as exc:
        LOG.warning("failed to load NWR station database from %s: %s", path, exc)
        return []
    if not isinstance(stations, list):
        LOG.warning("NWR station database in %s does not contain a stations array", path)
        return []

    loaded: list[dict[str, str]] = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        normalized = {
            "callsign": str(station.get("callsign", "")).strip().upper(),
            "frequency": str(station.get("frequency", "")).strip(),
            "city": str(station.get("city", "")).strip(),
            "state": str(station.get("state", "")).strip().upper(),
            "state_name": str(station.get("state_name", "")).strip(),
            "site_name": str(station.get("site_name", "")).strip(),
            "source_url": str(station.get("source_url", "")).strip(),
        }
        if not normalized["callsign"] or not normalized["frequency"]:
            continue
        normalized["key"] = station_key(normalized)
        loaded.append(normalized)
    loaded.sort(key=lambda item: (item["callsign"], item["state"], item["city"]))
    LOG.info("loaded %s NWR stations from %s", len(loaded), path)
    return loaded


def load_streams(streams_directory: Path, legacy_path: Path) -> list[dict[str, Any]]:
    streams = load_stream_configs(streams_directory)
    if streams:
        return streams
    if (streams_directory / STREAMS_DIRECTORY_MARKER_FILE_NAME).exists():
        return []
    streams = load_legacy_streams(legacy_path)
    if streams:
        LOG.info("migrating %s stream configuration(s) from %s to %s", len(streams), legacy_path, streams_directory)
        save_streams(streams_directory, streams)
    return streams


def load_stream_configs(streams_directory: Path) -> list[dict[str, Any]]:
    if not streams_directory.exists():
        return []
    streams = []
    for config_path in sorted(streams_directory.glob(f"*/{STREAM_CONFIG_FILE_NAME}")):
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("failed to load stream configuration from %s: %s", config_path, exc)
            continue
        if not isinstance(raw, dict):
            LOG.warning("stream configuration in %s is not a JSON object", config_path)
            continue
        streams.append(raw)
    streams.sort(key=stream_sort_key)
    return streams


def load_legacy_streams(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        LOG.warning("failed to load stream settings from %s: %s", path, exc)
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("streams"), list):
        return []
    streams = []
    for stream in raw["streams"]:
        if isinstance(stream, dict):
            streams.append(stream)
    streams.sort(key=stream_sort_key)
    return streams


def save_streams(streams_directory: Path, streams: list[dict[str, Any]]) -> None:
    streams_directory.mkdir(parents=True, exist_ok=True)
    (streams_directory / STREAMS_DIRECTORY_MARKER_FILE_NAME).write_text("per-stream JSON configs\n", encoding="utf-8")
    desired_paths: set[Path] = set()
    used_names: set[str] = set()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        config_path = stream_config_path(streams_directory, stream, used_names)
        desired_paths.add(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(stream, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for config_path in streams_directory.glob(f"*/{STREAM_CONFIG_FILE_NAME}"):
        if config_path not in desired_paths:
            try:
                config_path.unlink()
            except FileNotFoundError:
                pass


def remove_stream_configs(streams_directory: Path, streams: list[dict[str, Any]]) -> None:
    streams_directory.mkdir(parents=True, exist_ok=True)
    (streams_directory / STREAMS_DIRECTORY_MARKER_FILE_NAME).write_text("per-stream JSON configs\n", encoding="utf-8")
    for stream in streams:
        for config_path in stream_config_candidates(streams_directory, stream):
            try:
                config_path.unlink()
            except FileNotFoundError:
                continue
            try:
                config_path.parent.rmdir()
            except OSError:
                pass


def stream_config_path(streams_directory: Path, stream: dict[str, Any], used_names: set[str] | None = None) -> Path:
    used_names = used_names if used_names is not None else set()
    preferred = stream_directory_name(stream)
    name = preferred
    if name in used_names:
        stream_id = str(stream.get("id", "")).strip()
        suffix = sanitize_path_component(stream_id[:8]) if stream_id else uuid.uuid4().hex[:8]
        name = f"{preferred}-{suffix}"
        counter = 2
        while name in used_names:
            name = f"{preferred}-{suffix}-{counter}"
            counter += 1
    used_names.add(name)
    return streams_directory / name / STREAM_CONFIG_FILE_NAME


def stream_config_candidates(streams_directory: Path, stream: dict[str, Any]) -> list[Path]:
    stream_id = str(stream.get("id", "")).strip()
    candidates: list[Path] = []
    if stream_id:
        for config_path in streams_directory.glob(f"*/{STREAM_CONFIG_FILE_NAME}"):
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw, dict) and raw.get("id") == stream_id and config_path not in candidates:
                candidates.append(config_path)
        return candidates
    candidates.append(streams_directory / stream_directory_name(stream) / STREAM_CONFIG_FILE_NAME)
    return candidates


def stream_directory_name(stream: dict[str, Any]) -> str:
    station = stream.get("station", {})
    callsign = str(station.get("callsign", "")).strip() if isinstance(station, dict) else ""
    return sanitize_path_component(callsign or str(stream.get("id", "stream")))


def stream_sort_key(stream: dict[str, Any]) -> tuple[str, str, str]:
    station = stream.get("station", {})
    if not isinstance(station, dict):
        station = {}
    return (
        str(station.get("callsign", "")),
        str(station.get("frequency", "")),
        str(stream.get("id", "")),
    )


def stream_outputs(stream: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = stream.get("outputs")
    if isinstance(outputs, list):
        return [output for output in outputs if isinstance(output, dict)]
    icecast = stream.get("icecast")
    if isinstance(icecast, dict):
        output = {
            "id": stream.get("id", uuid.uuid4().hex),
            "enabled": stream.get("enabled", True),
            "type": "icecast",
            "icecast": icecast,
        }
        stream["outputs"] = [output]
        return [output]
    return []


def mutable_stream_outputs(stream: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = stream.get("outputs")
    if isinstance(outputs, list):
        return outputs
    stream_outputs(stream)
    outputs = stream.get("outputs")
    if isinstance(outputs, list):
        return outputs
    stream["outputs"] = []
    return stream["outputs"]


def enabled_output_count(
    stream: dict[str, Any],
    *,
    disabled_icecast_output_id: str | None = None,
    eas_enabled: bool | None = None,
) -> int:
    count = 0
    for output in stream_outputs(stream):
        if disabled_icecast_output_id and output.get("id") == disabled_icecast_output_id:
            continue
        if output.get("enabled", True):
            count += 1
    if eas_enabled is None:
        eas_enabled = eas_recording_settings_from_stream(stream).enabled
    if eas_enabled:
        count += 1
    return count


def stream_worker_key(stream: dict[str, Any]) -> str:
    return str(stream.get("id", ""))


def validate_receiver_frequency(raw: Any) -> int:
    try:
        frequency_hz = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("select a valid NWR receiver frequency") from exc
    if frequency_hz not in NWR_RECEIVER_CHANNELS_HZ:
        raise ValueError("select a valid NWR receiver frequency")
    return frequency_hz


def receiver_frequency_mhz(frequency_hz: int) -> str:
    return f"{frequency_hz / 1_000_000:.3f}"


def icecast_encoder_key(config: IcecastConfig) -> tuple[str, int, int]:
    return config.format, config.sample_rate, config.bitrate


def icecast_output_signature(output: dict[str, Any]) -> str:
    icecast = output.get("icecast", {})
    return json.dumps(
        {
            "id": output.get("id"),
            "enabled": output.get("enabled", True),
            "host": icecast.get("host"),
            "port": icecast.get("port"),
            "mount": icecast.get("mount"),
            "username": icecast.get("username"),
            "password": icecast.get("password"),
            "format": icecast.get("format"),
            "sample_rate": icecast.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE),
            "bitrate": icecast.get("bitrate", DEFAULT_STREAM_BITRATES.get(icecast.get("format", "mp3"), 64)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def queue_latest(target: queue.Queue, item: Any) -> None:
    try:
        target.put_nowait(item)
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            pass


def icecast_auth_signature(icecast: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": icecast.get("host"),
        "port": icecast.get("port"),
        "username": icecast.get("username"),
        "password": icecast.get("password"),
        "mount": icecast.get("mount"),
    }


def icecast_auth_cache_key(icecast: dict[str, Any]) -> str:
    return json.dumps(icecast_auth_signature(icecast), sort_keys=True, separators=(",", ":"))


def icecast_auth_changed(output: dict[str, Any], icecast: dict[str, Any]) -> bool:
    return output.get("auth_signature") != icecast_auth_signature(icecast)


def icecast_config_from_output(output: dict[str, Any]) -> IcecastConfig:
    icecast = output["icecast"]
    return IcecastConfig(
        host=icecast["host"],
        port=int(icecast["port"]),
        mount=icecast["mount"],
        username=icecast["username"],
        password=icecast["password"],
        format=icecast["format"],
        sample_rate=int(icecast.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE)),
        bitrate=int(icecast.get("bitrate", DEFAULT_STREAM_BITRATES[icecast["format"]])),
        enabled=bool(output.get("enabled", True)),
    )


def friendly_stream_error(exc: Exception) -> str:
    message = str(exc)
    if "401" in message:
        return "Invalid Icecast username or password."
    if "403" in message:
        return "The Icecast mountpoint is already occupied."
    if "404" in message:
        return "The Icecast server or mountpoint does not exist."
    if isinstance(exc, TimeoutError) or "timed out" in message.lower():
        return "The Icecast server took too long to respond."
    return message or "Stream needs attention."


def validate_icecast_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("icecast settings are required")
    service = str(raw.get("service", "")).strip().lower()
    if not service:
        service = STREAM_SERVICE_HOSTS.get(str(raw.get("host", "")).strip().lower(), STREAM_SERVICE_CUSTOM)
    if service not in STREAM_SERVICE_NAMES:
        raise ValueError("Streaming service is not supported")
    host = str(raw.get("host", "")).strip()
    if not host:
        raise ValueError("Icecast host is required")
    normalized_host = host.lower()
    suggested_service = STREAM_SERVICE_HOSTS.get(normalized_host)
    if service == STREAM_SERVICE_CUSTOM and suggested_service:
        raise ValueError(f"Please select {STREAM_SERVICE_NAMES[suggested_service]} instead of typing its Icecast URL in custom Icecast setup.")
    try:
        port = int(raw.get("port", 8000))
    except (TypeError, ValueError) as exc:
        raise ValueError("Icecast port must be a number") from exc
    if port < 1 or port > 65535:
        raise ValueError("Icecast port must be from 1 through 65535")
    username = str(raw.get("username", "")).strip()
    password = str(raw.get("password", ""))
    if not username:
        raise ValueError("Icecast username is required")
    if not password:
        raise ValueError("Icecast password is required")
    mount = str(raw.get("mount", "")).strip()
    if not mount:
        raise ValueError("Icecast mountpoint is required")
    if not mount.startswith("/"):
        mount = f"/{mount}"
    if any(character.isspace() for character in mount):
        raise ValueError("Icecast mountpoint cannot contain whitespace")
    stream_format = str(raw.get("format", "ogg")).strip().lower()
    if stream_format not in {"ogg", "mp3"}:
        raise ValueError("Icecast streaming format must be OGG or MP3")
    try:
        sample_rate = int(raw.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE))
    except (TypeError, ValueError) as exc:
        raise ValueError("Icecast sample rate must be a number") from exc
    if sample_rate not in STREAM_SAMPLE_RATES:
        raise ValueError("Icecast sample rate is not supported")
    try:
        bitrate = int(raw.get("bitrate", DEFAULT_STREAM_BITRATES[stream_format]))
    except (TypeError, ValueError) as exc:
        raise ValueError("Icecast bitrate must be a number") from exc
    if bitrate < 8 or bitrate > 320 or bitrate % 8 != 0:
        raise ValueError("Icecast bitrate must be from 8 through 320 Kbps in 8 Kbps steps")
    if service == STREAM_SERVICE_GWES:
        if normalized_host != "ingest.wxr.gwes-cdn.net" or port != 10000:
            raise ValueError("GWES Weather Radio requires its predefined Icecast server.")
        if stream_format != "mp3":
            raise ValueError("GWES Weather Radio requires MP3 streaming.")
        if bitrate < 64:
            raise ValueError("GWES Weather Radio requires a bitrate of at least 64 Kbps.")
    elif service == STREAM_SERVICE_WEATHERUSA:
        if normalized_host != "radio-master.weatherusa.net" or port != 80:
            raise ValueError("WeatherUSA requires its predefined Icecast server.")
        if username != "source":
            raise ValueError("WeatherUSA requires the predefined Icecast username.")
        if bitrate < 32 or bitrate > 56:
            raise ValueError("WeatherUSA bitrate must be from 32 through 56 Kbps.")
        if sample_rate > 22050:
            raise ValueError("WeatherUSA sample rate cannot be above 22050 Hz.")
    elif service == STREAM_SERVICE_NWRORG:
        if normalized_host != "wxradio.org" or port != 8000:
            raise ValueError("NOAA Weather Radio Org requires its predefined Icecast server.")
        if username != "source" or password != "WxRadio2014":
            raise ValueError("NOAA Weather Radio Org requires its predefined Icecast credentials.")
        if stream_format != "mp3":
            raise ValueError("NOAA Weather Radio Org requires MP3 streaming.")
        if bitrate != 32:
            raise ValueError("NOAA Weather Radio Org requires a bitrate of 32 Kbps.")
        if sample_rate != 22050:
            raise ValueError("NOAA Weather Radio Org requires a sample rate of 22050 Hz.")
    return {
        "service": service,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "mount": mount,
        "format": stream_format,
        "sample_rate": sample_rate,
        "bitrate": bitrate,
    }


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


def access_urls(host: str, port: int) -> list[str]:
    if host not in {"0.0.0.0", "::", ""}:
        return [f"http://{host}:{port}"]

    hosts = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            hosts.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address:
                hosts.add(address)
    except OSError:
        pass

    ordered_hosts = sorted(hosts, key=lambda value: (value.startswith("127."), value))
    return [f"http://{address}:{port}" for address in ordered_hosts]


def configure_dependency_logging() -> None:
    for name in ("aioice", "aiortc"):
        logging.getLogger(name).setLevel(logging.WARNING)


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


def install_shutdown_signal_handlers(server: ThreadingHTTPServer) -> dict[int, Any]:
    if threading.current_thread() is not threading.main_thread():
        return {}
    previous_handlers: dict[int, Any] = {}

    def handle_shutdown(signum, _frame) -> None:
        signal_name = signal.Signals(signum).name
        LOG.info("received %s; shutting down NWR Stream Manager", signal_name)
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
    configure_dependency_logging()
    log_path = log_file or default_log_path(state_path)
    configure_file_logging(log_path)
    ring_handler = RingLogHandler()
    logging.getLogger().addHandler(ring_handler)
    service = RtlControlService(state_path, ring_handler)
    RtlControlHandler.service = service
    server = ThreadingHTTPServer((host, port), RtlControlHandler)
    previous_signal_handlers = install_shutdown_signal_handlers(server)
    LOG.info("RTL-SDR control web interface bound to %s:%s", host, port)
    for url in access_urls(host, port):
        LOG.info("RTL-SDR control web interface available at %s", url)
    LOG.info("RTL-SDR settings will be remembered in %s", state_path)
    LOG.info("server log file is %s", log_path)
    try:
        server.serve_forever()
    finally:
        restore_signal_handlers(previous_signal_handlers)
        server.server_close()
        service.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_server(args.host, args.port, args.state, args.verbose, args.log_file)
    except KeyboardInterrupt:
        return 130
    return 0


SETUP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NWR Stream Manager Setup</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #f6f7f9; color: #14181f; }
main { max-width: 720px; margin: 0 auto; padding: 32px 24px; }
.panel { background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 24px; }
label { display: grid; gap: 6px; margin: 14px 0; font-weight: 700; }
input { font: inherit; padding: 10px; border: 1px solid #aab2c0; border-radius: 6px; }
.actions { display: flex; gap: 10px; margin-top: 18px; }
button { font: inherit; padding: 10px 14px; border: 1px solid #9aa4b2; border-radius: 6px; background: #fff; color: #14181f; cursor: pointer; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
.primary { background: #174a98; color: #fff; border-color: #174a98; }
.message { min-height: 1.5em; margin-top: 14px; font-weight: 700; }
.error { color: #a61b1b; }
.hint { color: #4b5563; }
@media (prefers-color-scheme: dark) {
  body { background: #111827; color: #f9fafb; }
  .panel, button, input { background: #1f2937; color: #f9fafb; border-color: #4b5563; }
  .hint { color: #cbd5e1; }
}
</style>
</head>
<body>
<main>
  <section class="panel" aria-labelledby="setup_title">
    <h1 id="setup_title">NWR Stream Manager Setup</h1>
    <div id="setup_welcome">
      <p>Welcome to NWR Stream Manager, an all new tool for streaming NOAA Weather Radio to online services! To ensure no one else on the network tampers with your streams, please set up an account. Press the next button to continue.</p>
      <div class="actions">
        <button id="welcome_next" class="primary" type="button">Next</button>
      </div>
    </div>
    <form id="setup_form" hidden>
      <p class="hint">Create the administrator account for this NWR Stream Manager.</p>
      <label>
        Username
        <input id="setup_username" name="username" autocomplete="username" required pattern="[A-Za-z0-9_-]{1,64}">
      </label>
      <label>
        Password
        <input id="setup_password" name="password" type="password" autocomplete="new-password" required minlength="8">
      </label>
      <label>
        Confirm password
        <input id="setup_confirm_password" name="confirm_password" type="password" autocomplete="new-password" required minlength="8">
      </label>
      <div class="actions">
        <button id="setup_next" class="primary" type="submit" disabled>Next</button>
      </div>
      <div id="setup_message" class="message" aria-live="polite"></div>
    </form>
    <div id="setup_done" hidden>
      <p>You're all set up! Click the finish button to log in.</p>
      <div class="actions">
        <button id="setup_finish" class="primary" type="button">Finish</button>
      </div>
    </div>
  </section>
</main>
<script>
const usernamePattern = /^[A-Za-z0-9_-]{1,64}$/;
const passwordPattern = /^[!-~]{8,256}$/;
const welcome = document.getElementById("setup_welcome");
const form = document.getElementById("setup_form");
const done = document.getElementById("setup_done");
const message = document.getElementById("setup_message");
const next = document.getElementById("setup_next");
const username = document.getElementById("setup_username");
const password = document.getElementById("setup_password");
const confirmPassword = document.getElementById("setup_confirm_password");

function validateSetupForm() {
  const valid = usernamePattern.test(username.value) &&
    passwordPattern.test(password.value) &&
    password.value === confirmPassword.value;
  next.disabled = !valid;
  if (!usernamePattern.test(username.value) && username.value) {
    message.textContent = "Username may contain only letters, numbers, hyphen, and underscore.";
    message.className = "message error";
  } else if (!passwordPattern.test(password.value) && password.value) {
    message.textContent = "Password must be at least 8 printable non-space characters.";
    message.className = "message error";
  } else if (confirmPassword.value && password.value !== confirmPassword.value) {
    message.textContent = "Passwords do not match.";
    message.className = "message error";
  } else {
    message.textContent = "";
    message.className = "message";
  }
}

document.getElementById("welcome_next").addEventListener("click", () => {
  welcome.hidden = true;
  form.hidden = false;
  username.focus();
});

for (const input of [username, password, confirmPassword]) {
  input.addEventListener("input", validateSetupForm);
}

form.addEventListener("submit", async event => {
  event.preventDefault();
  validateSetupForm();
  if (next.disabled) return;
  next.disabled = true;
  try {
    const response = await fetch("/api/setup-account", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        username: username.value,
        password: password.value,
        confirm_password: confirmPassword.value
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    form.hidden = true;
    done.hidden = false;
    document.getElementById("setup_finish").focus();
  } catch (error) {
    message.textContent = error.message;
    message.className = "message error";
    validateSetupForm();
  }
});

document.getElementById("setup_finish").addEventListener("click", () => {
  window.location.replace("/");
});
</script>
</body>
</html>
"""


MUST_CHANGE_PASSWORD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Change Account Password</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #f6f7f9; color: #14181f; }
main { max-width: 720px; margin: 0 auto; padding: 32px 24px; }
.panel { background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 24px; }
label { display: grid; gap: 6px; margin: 14px 0; font-weight: 700; }
input { font: inherit; padding: 10px; border: 1px solid #aab2c0; border-radius: 6px; }
.actions { display: flex; gap: 10px; margin-top: 18px; }
button { font: inherit; padding: 10px 14px; border: 1px solid #9aa4b2; border-radius: 6px; background: #fff; color: #14181f; cursor: pointer; }
button:disabled { opacity: 0.55; cursor: not-allowed; }
.primary { background: #174a98; color: #fff; border-color: #174a98; }
.message { min-height: 1.5em; margin-top: 14px; font-weight: 700; }
.error { color: #a61b1b; }
.hint { color: #4b5563; }
@media (prefers-color-scheme: dark) {
  body { background: #111827; color: #f9fafb; }
  .panel, button, input { background: #1f2937; color: #f9fafb; border-color: #4b5563; }
  .hint { color: #cbd5e1; }
}
</style>
</head>
<body>
<main>
  <section class="panel" aria-labelledby="password_title">
    <h1 id="password_title">Change Account Password</h1>
    <p class="hint">You must change your password to continue.</p>
    <form id="password_form">
      <label>
        Current password
        <input id="current_password" name="current_password" type="password" autocomplete="current-password" required>
      </label>
      <label>
        New password
        <input id="new_password" name="new_password" type="password" autocomplete="new-password" required minlength="8">
      </label>
      <label>
        Confirm new password
        <input id="confirm_password" name="confirm_password" type="password" autocomplete="new-password" required minlength="8">
      </label>
      <div class="actions">
        <button id="change_password" class="primary" type="submit" disabled>Change password</button>
      </div>
      <div id="password_message" class="message" aria-live="polite"></div>
    </form>
  </section>
</main>
<script>
const passwordPattern = /^[!-~]{8,256}$/;
const currentPassword = document.getElementById("current_password");
const newPassword = document.getElementById("new_password");
const confirmPassword = document.getElementById("confirm_password");
const submit = document.getElementById("change_password");
const message = document.getElementById("password_message");
function validatePasswordForm() {
  submit.disabled = !(currentPassword.value && passwordPattern.test(newPassword.value) && confirmPassword.value);
  if (newPassword.value && !passwordPattern.test(newPassword.value)) {
    message.textContent = "Password must be at least 8 printable non-space characters.";
    message.className = "message error";
  } else {
    message.textContent = "";
    message.className = "message";
  }
}
for (const input of [currentPassword, newPassword, confirmPassword]) {
  input.addEventListener("input", validatePasswordForm);
}
document.getElementById("password_form").addEventListener("submit", async event => {
  event.preventDefault();
  validatePasswordForm();
  if (submit.disabled) return;
  if (newPassword.value !== confirmPassword.value) {
    message.textContent = "Passwords do not match.";
    message.className = "message error";
    return;
  }
  submit.disabled = true;
  try {
    const response = await fetch("/api/account/password", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        current_password: currentPassword.value,
        new_password: newPassword.value,
        confirm_password: confirmPassword.value
      })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || response.statusText);
    window.location.replace("/");
  } catch (error) {
    message.textContent = error.message;
    message.className = "message error";
    validatePasswordForm();
  }
});
</script>
</body>
</html>
"""


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NWR Stream Manager</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #f6f7f9; color: #14181f; }
header { background: #fff; border-bottom: 1px solid #d8dde6; }
.topbar { max-width: 980px; margin: 0 auto; padding: 14px 24px; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
main { max-width: 980px; margin: 0 auto; padding: 24px; }
.global-status-banner { max-width: 980px; margin: 14px auto 0; padding: 12px 24px; border: 1px solid #2557a7; border-radius: 8px; background: #eaf1ff; color: #14181f; font-weight: 700; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.global-status-banner a { color: #174a98; }
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 20px; margin: 0 0 16px; }
h3 { font-size: 16px; margin: 18px 0 10px; }
section { background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
[hidden] { display: none !important; }
label { display: grid; gap: 6px; font-weight: 600; margin-bottom: 14px; }
select, input, button { font: inherit; padding: 8px 10px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; color: #14181f; }
fieldset { border: 1px solid #d8dde6; border-radius: 6px; margin: 16px 0 0; padding: 14px; }
legend { font-weight: 700; padding: 0 6px; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: 0.65; }
nav { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
nav a { font: inherit; padding: 8px 10px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; color: #14181f; text-decoration: none; }
nav a[aria-current="page"], nav button[aria-current="page"] { border-color: #2557a7; box-shadow: inset 0 -2px 0 #2557a7; }
.nav-more { position: relative; }
.nav-more-menu { position: absolute; right: 0; z-index: 15; display: grid; gap: 4px; min-width: 230px; margin-top: 6px; padding: 6px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; box-shadow: 0 8px 18px rgb(20 24 31 / 18%); }
.nav-more-menu[hidden] { display: none; }
.nav-more-menu a { border: 0; border-radius: 4px; }
.view[hidden] { display: none; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
.row { display: flex; align-items: center; gap: 10px; }
.row label { margin: 0; display: flex; align-items: center; gap: 8px; }
.actions { display: flex; flex-wrap: wrap; gap: 10px; }
.status { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.metric { border: 1px solid #d8dde6; border-radius: 6px; padding: 10px; }
.metric b { display: block; font-size: 12px; color: #526070; text-transform: uppercase; }
.stream-list { display: grid; gap: 10px; margin-top: 14px; }
.stream-item { border: 1px solid #d8dde6; border-radius: 6px; padding: 10px; }
.stream-item b { display: block; margin-bottom: 4px; }
table { width: 100%; border-collapse: collapse; }
th, td { border-bottom: 1px solid #d8dde6; padding: 10px; text-align: left; vertical-align: top; }
th { color: #526070; font-size: 12px; text-transform: uppercase; }
.status-text { font-weight: 700; }
.status-enabled { color: #0f7a34; }
.status-needs-attention { color: #b00020; }
.status-disabled { color: inherit; font-weight: 600; }
.menu-cell { position: relative; }
.stream-actions-menu { position: absolute; right: 10px; z-index: 10; display: grid; gap: 4px; min-width: 190px; margin-top: 6px; padding: 6px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; box-shadow: 0 8px 18px rgb(20 24 31 / 18%); }
.stream-actions-menu[hidden] { display: none; }
.stream-actions-menu button { width: 100%; text-align: left; border: 0; }
.details-list { display: grid; grid-template-columns: max-content 1fr; gap: 8px 14px; margin: 0 0 16px; }
.details-list dt { font-weight: 700; }
.details-list dd { margin: 0; }
.tabs { display: flex; flex-wrap: wrap; gap: 6px; border-bottom: 1px solid #d8dde6; margin: 16px 0; }
.tabs button { border-bottom-left-radius: 0; border-bottom-right-radius: 0; margin-bottom: -1px; }
.tabs button[aria-selected="true"] { border-color: #2557a7; border-bottom-color: #fff; box-shadow: inset 0 2px 0 #2557a7; }
.tabpanel[hidden] { display: none; }
.effects-layout { display: grid; grid-template-columns: minmax(180px, 240px) 1fr; gap: 16px; align-items: start; }
.effects-list { display: grid; gap: 8px; }
.effects-list button { text-align: left; }
.effects-list button[aria-current="true"] { border-color: #2557a7; box-shadow: inset 3px 0 0 #2557a7; }
.effects-detail { min-width: 0; }
.audio-effect-panel[hidden] { display: none; }
.audio-effects-back { display: none; }
pre { margin: 0; min-height: 220px; max-height: 360px; overflow: auto; background: #10151d; color: #d8f3dc; padding: 12px; border-radius: 6px; font-size: 13px; }
.error { color: #a40000; font-weight: 600; }
.message { font-weight: 600; }
.success { color: #0f7a34; }
.storage-warning { margin-top: 12px; color: #a40000; font-weight: 700; }
.status-connected { color: #0f7a34; }
.hint { color: #526070; font-size: 13px; margin-top: -8px; }
.notice-dialog { position: fixed; right: 24px; bottom: 24px; z-index: 20; max-width: min(420px, calc(100vw - 48px)); padding: 16px; border: 1px solid #b9c0cc; border-radius: 8px; background: #fff; box-shadow: 0 12px 30px rgb(20 24 31 / 22%); }
.notice-dialog h2 { font-size: 18px; margin-bottom: 8px; }
.notice-dialog p { margin: 0 0 14px; }
@media (prefers-color-scheme: dark) {
  body { background: #101318; color: #eef2f7; }
  header, section, select, input, button { background: #181d24; color: #eef2f7; border-color: #333b48; }
  .global-status-banner { background: #16233a; color: #eef2f7; border-color: #3f67a9; }
  .global-status-banner a { color: #9dc1ff; }
  fieldset, .metric, .stream-item, th, td { border-color: #333b48; }
  .metric b, .hint, th { color: #9aa8ba; }
  .status-enabled { color: #5fd27a; }
  .status-connected { color: #5fd27a; }
  .status-needs-attention { color: #ff6b7a; }
  .success { color: #5fd27a; }
  .stream-actions-menu { background: #181d24; border-color: #333b48; }
  .nav-more-menu { background: #181d24; border-color: #333b48; }
  .tabs { border-color: #333b48; }
  .tabs button[aria-selected="true"] { border-bottom-color: #181d24; }
  .notice-dialog { background: #181d24; border-color: #333b48; }
}
@media (max-width: 680px) {
  .effects-layout { display: block; }
  .effects-layout.effect-detail-active .effects-list { display: none; }
  .effects-layout:not(.effect-detail-active) .effects-detail { display: none; }
  .audio-effects-back { display: inline-block; margin-bottom: 12px; }
}
</style>
</head>
<body>
<header>
  <div class="topbar">
    <h1>NWR Stream Manager</h1>
    <nav aria-label="Main">
      <a id="nav_dashboard" href="/" data-view="dashboard" aria-current="page">Dashboard</a>
      <a id="nav_rtl" href="/?view=rtl" data-view="rtl">Configure RTL-SDR</a>
      <a id="nav_streams" href="/?view=streams" data-view="streams">Manage Streams</a>
      <a id="nav_eas_alerts" href="/?view=eas_alerts" data-view="eas_alerts" hidden>EAS alerts</a>
      <span class="nav-more">
        <button id="nav_more_button" type="button" aria-haspopup="menu" aria-expanded="false">More</button>
        <span id="nav_more_menu" class="nav-more-menu" role="menu" hidden>
          <a id="nav_receiver" href="/?view=receiver" data-view="receiver" role="menuitem">Weather Radio Receiver</a>
          <a id="nav_iq_recorder" href="/?view=iq_recorder" data-view="iq_recorder" role="menuitem">I/Q Recorder</a>
          <a id="nav_logs" href="/?view=logs" data-view="logs" role="menuitem">Logs</a>
          <a id="nav_accounts" href="/?view=accounts" data-view="accounts" role="menuitem">Manage accounts</a>
          <a id="nav_change_password" href="/?view=change_password" data-view="change_password" role="menuitem" hidden>Change account password</a>
        </span>
      </span>
    </nav>
  </div>
</header>
<div id="nwrorg_submission_dialog" class="notice-dialog" role="dialog" aria-labelledby="nwrorg_submission_title" aria-live="polite" hidden>
  <h2 id="nwrorg_submission_title">NOAA Weather Radio Org</h2>
  <p>
    If you have not already done so, you must fill out the
    <a id="nwrorg_submission_link" href="http://noaaweatherradio.org/addstream/addstream.html" target="_blank" rel="noopener noreferrer">submission form</a>
    for NOAA Weather Radio Org before your stream will appear on the website.
  </p>
  <div class="actions">
    <button id="dismiss_nwrorg_submission" type="button">Dismiss</button>
  </div>
</div>
<div id="monitor_unstable_dialog" class="notice-dialog" role="dialog" aria-labelledby="monitor_unstable_title" aria-live="assertive" hidden>
  <h2 id="monitor_unstable_title">Stream monitoring stopped</h2>
  <p>Your internet connection is too unstable for stream monitoring.</p>
  <div class="actions">
    <button id="dismiss_monitor_unstable" type="button">Dismiss</button>
  </div>
</div>
<div id="receiver_unstable_dialog" class="notice-dialog" role="dialog" aria-labelledby="receiver_unstable_title" aria-live="assertive" hidden>
  <h2 id="receiver_unstable_title">Weather radio receiver stopped</h2>
  <p>Your internet connection is too unstable for realtime listening of weather radio.</p>
  <div class="actions">
    <button id="dismiss_receiver_unstable" type="button">Dismiss</button>
  </div>
</div>
<div id="account_secret_dialog" class="notice-dialog" role="dialog" aria-labelledby="account_secret_title" aria-live="polite" hidden>
  <h2 id="account_secret_title">Temporary password</h2>
  <p>Copy it and share it with the user, you will not be able to see it again.</p>
  <label>Temporary password
    <input id="account_secret" readonly>
  </label>
  <div class="actions">
    <button id="copy_account_secret" type="button">Copy to clipboard</button>
    <button id="dismiss_account_secret" type="button">Dismiss</button>
  </div>
</div>
<div id="iq_recording_banner" class="global-status-banner" hidden>
  <span>I/Q recording in progress</span>
  <a href="/?view=iq_recorder" data-view="iq_recorder">Return to I/Q recorder</a>
  <span id="iq_recording_banner_elapsed">0:00</span>
</div>
<main>
  <div id="view_dashboard" class="view">
    <section>
      <h2>Dashboard</h2>
      <div class="status" aria-live="off">
        <div class="metric"><b>Configured SDR</b><span id="summary_sdr">none</span></div>
        <div class="metric"><b>Gain</b><span id="summary_gain">automatic</span></div>
        <div class="metric"><b>Capture</b><span id="summary_capture">inactive</span></div>
        <div class="metric"><b>Configured Streams</b><span id="summary_stream_count">0</span></div>
        <div class="metric"><b>Storage</b><span id="summary_storage">unknown</span></div>
      </div>
      <div id="storage-warning" class="storage-warning" hidden></div>
    </section>
    <section>
      <h2>Streams needing attention</h2>
      <div id="dashboard-stream-attention" class="stream-list"></div>
    </section>
    <section>
      <h2>Recently issued EAS alerts</h2>
      <div id="dashboard-recent-alerts" class="stream-list"></div>
    </section>
  </div>

  <div id="view_rtl" class="view" hidden>
    <section>
      <h2>Configure RTL-SDR</h2>
      <label>Active SDR
        <select id="serial"></select>
      </label>
      <button id="rescan_devices" type="button">Rescan</button>
      <div id="device-errors" class="error"></div>
    </section>
    <section>
      <div class="grid">
        <label>Gain
          <input id="gain" type="range" min="0" max="0" step="1" value="0" disabled>
          <span id="gain_label" class="hint">Automatic</span>
        </label>
        <label>PPM Correction
          <input id="ppm_correction" type="number" min="-200" max="200" step="1">
        </label>
        <div class="row">
          <label><input id="gain_auto" type="checkbox"> Automatic gain</label>
          <label><input id="bias_tee" type="checkbox"> Bias tee</label>
        </div>
      </div>
    </section>
  </div>

  <div id="view_receiver" class="view" hidden>
    <section>
      <h2>Weather Radio Receiver</h2>
      <p>Freely tune around the NWR band and listen right in your browser.</p>
      <div class="status" aria-live="off">
        <div class="metric"><b>Frequency</b><span id="receiver_frequency">162.475 MHz</span></div>
      </div>
      <div class="actions" aria-label="Weather radio receiver controls">
        <button id="receiver_previous" type="button">Previous channel</button>
        <button id="receiver_play_pause" type="button">Play</button>
        <button id="receiver_next" type="button">Next channel</button>
      </div>
      <div id="receiver-result" class="message"></div>
    </section>
  </div>

  <div id="view_iq_recorder" class="view" hidden>
    <section>
      <h2>I/Q Recorder</h2>
      <div id="iq-recorder-result" class="message"></div>
      <div id="iq_recorder_active" hidden>
        <div class="status" aria-live="off">
          <div class="metric"><b>Status</b><span id="iq_status">Idle</span></div>
          <div class="metric"><b>Source</b><span id="iq_source">None</span></div>
          <div class="metric"><b>Sample rate</b><span id="iq_active_sample_rate">0 S/s</span></div>
          <div class="metric"><b>File size</b><span id="iq_file_size">0 B</span></div>
          <div class="metric"><b>Elapsed</b><span id="iq_elapsed">0:00</span></div>
          <div class="metric"><b>Storage time remaining</b><span id="iq_remaining">calculating</span></div>
          <div class="metric" id="iq_auto_stop_metric" hidden><b>Automatic stop</b><span id="iq_auto_stop_remaining">0:00</span></div>
          <div class="metric"><b>Storage</b><span id="iq_storage">unknown</span></div>
        </div>
        <div class="actions">
          <button id="iq_stop_recording" type="button">Stop recording</button>
        </div>
      </div>
      <div class="actions">
        <button id="open_iq_start" type="button">New recording</button>
      </div>
      <table aria-label="I/Q recordings">
        <thead>
          <tr>
            <th>Time of recording</th>
            <th>Duration</th>
            <th>Sample rate</th>
            <th>Frequency</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="iq-recordings-body" aria-live="off">
          <tr>
            <td colspan="5" class="hint">No I/Q recordings.</td>
          </tr>
        </tbody>
      </table>
    </section>
  </div>

  <div id="view_iq_recorder_start" class="view" hidden>
    <section>
      <h2>New I/Q Recording</h2>
      <div id="iq-start-result" class="message"></div>
      <div id="iq_recorder_idle">
        <fieldset>
          <legend>Recording source</legend>
          <label><input id="iq_mode_stream" name="iq_recording_mode" type="radio" value="stream" checked> Record I/Q data from an active stream</label>
          <label><input id="iq_mode_spectrum" name="iq_recording_mode" type="radio" value="spectrum"> Record the entire spectrum</label>
        </fieldset>
        <div id="iq_stream_fields">
          <label>Stream
            <select id="iq_stream_select"></select>
          </label>
          <div class="hint">Records the selected stream's 24 kHz channel before FM demodulation.</div>
        </div>
        <div id="iq_spectrum_fields" hidden>
          <label>Sample rate
            <select id="iq_sample_rate"></select>
          </label>
          <div class="hint">Records spectrum I/Q centered at 162.475 MHz after RTL-SDR float conversion.</div>
        </div>
        <label>Stop recording after
          <input id="iq_duration_minutes" type="number" min="0" max="1440" step="1" value="5" aria-describedby="iq_duration_hint">
        </label>
        <div id="iq_duration_hint" class="hint">Minutes. Set to 0 to record until you manually stop it.</div>
        <div class="actions">
          <button id="iq_start_recording" type="button">Start recording</button>
          <button id="cancel_iq_start" type="button">Cancel</button>
        </div>
      </div>
    </section>
  </div>

  <div id="view_iq_recording_download" class="view" hidden>
    <section>
      <h2>Download I/Q Recording</h2>
      <p id="iq_download_recording_label" class="hint"></p>
      <label>Download format
        <select id="iq_download_format">
          <option value="cf">Float 32 bit</option>
          <option value="s16">Signed 16 bit</option>
          <option value="u8">Unsigned 8-bit</option>
        </select>
      </label>
      <div class="actions">
        <button id="iq_download_recording" type="button">Download recording</button>
        <button id="cancel_iq_download" type="button">Cancel</button>
      </div>
      <div id="iq-download-result" class="message"></div>
    </section>
  </div>

  <div id="view_logs" class="view" hidden>
    <section>
      <h2>Logs</h2>
      <div id="webrtc_support_status" class="hint" hidden></div>
      <div class="status" aria-live="off">
        <div class="metric"><b>Capture</b><span id="active">inactive</span></div>
        <div class="metric"><b>Chunks</b><span id="chunks">0</span></div>
        <div class="metric"><b>Bytes</b><span id="bytes">0</span></div>
        <div class="metric"><b>Last IQ</b><span id="last">never</span></div>
      </div>
      <p id="capture-error" class="error"></p>
      <pre id="logs" aria-live="off" aria-label="Server log output"></pre>
    </section>
  </div>

  <div id="view_accounts" class="view" hidden>
    <section>
      <h2>Manage Accounts</h2>
      <div id="account-result" class="message"></div>
      <div class="actions">
        <button id="open_create_account" type="button">Create account</button>
      </div>
      <div id="create_account_panel" hidden>
        <h3>Create Account</h3>
        <label>Username
          <input id="new_account_username" autocomplete="off" pattern="[A-Za-z0-9_-]{1,64}">
        </label>
        <label><input id="new_account_read_only" type="checkbox"> Read-only account</label>
        <div class="actions">
          <button id="create_account" type="button">Create account</button>
          <button id="cancel_create_account" type="button">Cancel</button>
        </div>
      </div>
      <table aria-label="Accounts">
        <thead>
          <tr>
            <th>Username</th>
            <th>Account type</th>
            <th>Last accessed</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="accounts-body" aria-live="off">
          <tr><td colspan="4" class="hint">No accounts.</td></tr>
        </tbody>
      </table>
    </section>
  </div>

  <div id="view_change_password" class="view" hidden>
    <section>
      <h2>Change Account Password</h2>
      <div id="change-password-result" class="message"></div>
      <label>Current password
        <input id="account_current_password" type="password" autocomplete="current-password">
      </label>
      <label>New password
        <input id="account_new_password" type="password" autocomplete="new-password">
      </label>
      <label>Confirm new password
        <input id="account_confirm_password" type="password" autocomplete="new-password">
      </label>
      <div class="actions">
        <button id="change_account_password" type="button">Change password</button>
        <button id="cancel_change_password" type="button">Cancel</button>
      </div>
    </section>
  </div>

  <div id="view_streams" class="view" hidden>
    <section>
      <h2>Manage Streams</h2>
      <div class="actions">
        <button id="open_add_stream" type="button">Add stream</button>
      </div>
      <table aria-label="Manage streams">
        <thead>
          <tr>
            <th>Callsign</th>
            <th>Frequency</th>
            <th>Outputs</th>
            <th>Status</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="active-streams-body" aria-live="off">
          <tr id="active-streams-empty">
            <td colspan="5" class="hint">No active streams.</td>
          </tr>
        </tbody>
      </table>
      <div id="streams-result" class="message"></div>
    </section>
  </div>

  <div id="view_add_stream" class="view" hidden>
    <section>
      <h2 id="stream_wizard_title">Add Stream</h2>
      <div id="wizard_step_station" class="wizard-step">
        <p>Let's get your stream set up and running! What station would you like to stream?</p>
        <label>Station Lookup
          <input id="station_search" type="search" placeholder="Callsign, city, state, or frequency" autocomplete="off">
        </label>
        <div class="actions">
          <button id="station_search_button" type="button">Search</button>
        </div>
        <label>Matching Stations
          <select id="station_results" size="8"></select>
        </label>
        <div id="selected_station" class="hint">Select a station to continue.</div>
      </div>
      <div id="wizard_step_credentials" class="wizard-step" hidden>
        <p id="icecast_credentials_intro">The next step is to enter your icecast credentials for the service you want to stream to. Enter them below, then click next.</p>
        <div id="icecast_service_help" class="hint"></div>
        <fieldset id="icecast_fields">
          <legend>Icecast Credentials</legend>
          <div class="grid">
            <label id="icecast_host_label">Host
              <input id="icecast_host" type="text" autocomplete="off">
            </label>
            <label id="icecast_port_label">Port
              <input id="icecast_port" type="number" min="1" max="65535" step="1" placeholder="8000">
            </label>
            <label id="icecast_username_label">Username
              <input id="icecast_username" type="text" autocomplete="username">
            </label>
            <label id="icecast_password_label">Password
              <input id="icecast_password" type="password" autocomplete="current-password">
            </label>
            <label id="show_icecast_password_label" class="checkbox-row">
              <input id="show_icecast_password" type="checkbox">
              Show password
            </label>
            <label id="icecast_mount_label">Mountpoint
              <input id="icecast_mount" type="text" placeholder="/station.mp3">
            </label>
            <label id="icecast_alt_label" class="checkbox-row" hidden>
              <input id="icecast_alt_enabled" type="checkbox">
              Alternate stream
            </label>
            <label id="icecast_alt_number_label" hidden>Alternate stream number
              <input id="icecast_alt_number" type="number" min="1" max="9" step="1" value="1">
            </label>
          </div>
        </fieldset>
      </div>
      <div id="wizard_step_service" class="wizard-step" hidden>
        <p>There are several online platforms you may stream to. You can choose to stream to one of them, or you may stream to a custom icecast server.</p>
        <label>Streaming service
          <select id="icecast_service">
            <option value="custom">Custom Icecast server</option>
            <option value="gwes">GWES Weather Radio</option>
            <option value="weatherusa">WeatherUSA</option>
            <option value="nwrorg">NOAA Weather Radio Org</option>
          </select>
        </label>
      </div>
      <div id="wizard_step_codec" class="wizard-step" hidden>
        <p>What audio codec would you like to use for the stream format? MP3 is generally more compatible, while OGG may give better audio quality at lower internet usage.</p>
        <fieldset id="icecast_format_fieldset">
          <legend>Stream Format</legend>
          <label><input id="icecast_format_mp3" name="icecast_format" type="radio" value="mp3" checked> MP3</label>
          <label><input id="icecast_format_ogg" name="icecast_format" type="radio" value="ogg"> OGG</label>
        </fieldset>
      </div>
      <div id="wizard_step_quality" class="wizard-step" hidden>
        <p>Adjust bitrate and sample rate to optimize audio quality and network usage. If you don't know what any of this means, click the finish button.</p>
        <fieldset>
          <legend>Audio Settings</legend>
        <div class="grid">
          <label id="icecast_sample_rate_label">Sample Rate
            <select id="icecast_sample_rate">
              <option value="8000">8000 Hz</option>
              <option value="11025">11025 Hz</option>
              <option value="16000">16000 Hz</option>
              <option value="22050">22050 Hz</option>
              <option value="24000" selected>24000 Hz</option>
              <option value="32000">32000 Hz</option>
              <option value="44100">44100 Hz</option>
              <option value="48000">48000 Hz</option>
            </select>
          </label>
          <label id="icecast_bitrate_label">Bitrate
            <select id="icecast_bitrate"></select>
          </label>
          <label id="output_enabled_label"><input id="output_enabled" type="checkbox" checked> Output enabled</label>
        </div>
      </fieldset>
      </div>
      <div class="actions">
        <button id="cancel_wizard" type="button">Cancel</button>
        <button id="wizard_back" type="button" hidden>Back</button>
        <button id="wizard_next" type="button">Next</button>
        <button id="wizard_finish" type="button" hidden>Finish</button>
        <button id="save_output" type="button" hidden>Save Changes</button>
        <button id="cancel_output_edit" type="button" hidden>Cancel</button>
      </div>
      <div id="stream-result" class="message"></div>
      <div id="streams-list" class="stream-list" aria-live="off"></div>
    </section>
  </div>

  <div id="view_stream_settings" class="view" hidden>
    <section>
      <h2>Stream Settings</h2>
      <div id="stream_settings_station" class="hint"></div>
      <label class="checkbox-row">
        <input id="stream_enabled" type="checkbox">
        Stream enabled
      </label>
      <label class="checkbox-row">
        <input id="stream_monitor_enabled" type="checkbox">
        Monitor
      </label>
      <div id="stream-settings-result" class="message"></div>
      <div class="tabs" role="tablist" aria-label="Stream settings sections">
        <button id="tab_outputs" type="button" role="tab" aria-selected="true" aria-controls="panel_outputs" tabindex="0">Outputs</button>
        <button id="tab_eas" type="button" role="tab" aria-selected="false" aria-controls="panel_eas" tabindex="-1">EAS Recording</button>
        <button id="tab_fallback" type="button" role="tab" aria-selected="false" aria-controls="panel_fallback" tabindex="-1">Fallback Audio</button>
        <button id="tab_audio" type="button" role="tab" aria-selected="false" aria-controls="panel_audio" tabindex="-1">Audio Effects</button>
      </div>
      <div id="panel_outputs" class="tabpanel" role="tabpanel" aria-labelledby="tab_outputs">
        <div class="actions">
          <button id="open_add_output" type="button">Add output</button>
        </div>
        <h3>Icecast outputs</h3>
        <table aria-label="Icecast outputs">
          <thead>
            <tr>
              <th>Destination</th>
              <th>Format</th>
              <th>Sample rate</th>
              <th>Bitrate</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="icecast-outputs-body" aria-live="off">
            <tr>
              <td colspan="6" class="hint">No Icecast outputs configured.</td>
            </tr>
          </tbody>
        </table>
        <div id="output_form_panel" hidden>
          <h3 id="output_form_title">Add output</h3>
          <label>Streaming service
            <select id="settings_icecast_service">
              <option value="custom">Custom Icecast server</option>
              <option value="gwes">GWES Weather Radio</option>
              <option value="weatherusa">WeatherUSA</option>
              <option value="nwrorg">NOAA Weather Radio Org</option>
            </select>
          </label>
          <div id="settings_icecast_service_help" class="hint"></div>
          <fieldset>
            <legend>Icecast output</legend>
            <div class="grid">
              <label id="settings_icecast_host_label">Host
                <input id="settings_icecast_host" type="text" autocomplete="off">
              </label>
              <label id="settings_icecast_port_label">Port
                <input id="settings_icecast_port" type="number" min="1" max="65535" step="1" placeholder="8000">
              </label>
              <label id="settings_icecast_username_label">Username
                <input id="settings_icecast_username" type="text" autocomplete="username">
              </label>
              <label id="settings_icecast_password_label">Password
                <input id="settings_icecast_password" type="password" autocomplete="current-password">
              </label>
              <label id="settings_show_icecast_password_label" class="checkbox-row">
                <input id="settings_show_icecast_password" type="checkbox">
                Show password
              </label>
              <label id="settings_icecast_mount_label">Mountpoint
                <input id="settings_icecast_mount" type="text" placeholder="/station.mp3">
              </label>
              <label id="settings_icecast_alt_label" class="checkbox-row" hidden>
                <input id="settings_icecast_alt_enabled" type="checkbox">
                Alternate stream
              </label>
              <label id="settings_icecast_alt_number_label" hidden>Alternate stream number
                <input id="settings_icecast_alt_number" type="number" min="1" max="9" step="1" value="1">
              </label>
              <fieldset id="settings_icecast_format_fieldset">
                <legend>Format</legend>
                <label><input id="settings_icecast_format_mp3" name="settings_icecast_format" type="radio" value="mp3" checked> MP3</label>
                <label><input id="settings_icecast_format_ogg" name="settings_icecast_format" type="radio" value="ogg"> OGG</label>
              </fieldset>
              <label id="settings_icecast_sample_rate_label">Sample rate
                <select id="settings_icecast_sample_rate">
                  <option value="8000">8000 Hz</option>
                  <option value="11025">11025 Hz</option>
                  <option value="16000">16000 Hz</option>
                  <option value="22050">22050 Hz</option>
                  <option value="24000" selected>24000 Hz</option>
                  <option value="32000">32000 Hz</option>
                  <option value="44100">44100 Hz</option>
                  <option value="48000">48000 Hz</option>
                </select>
              </label>
              <label id="settings_icecast_bitrate_label">Bitrate
                <select id="settings_icecast_bitrate"></select>
              </label>
            </div>
          </fieldset>
          <div class="actions">
            <button id="cancel_output_form" type="button">Cancel</button>
            <button id="add_output" type="button">Add output</button>
            <button id="save_output_settings" type="button" hidden>Save changes</button>
          </div>
        </div>
        <div id="output-result" class="message"></div>
      </div>
      <div id="panel_audio" class="tabpanel" role="tabpanel" aria-labelledby="tab_audio" hidden>
        <h3>Audio effects</h3>
        <div id="audio_effects_layout" class="effects-layout">
          <div id="audio_effects_list" class="effects-list" aria-label="Audio effects">
            <button type="button" data-audio-effect="volume">Volume multiplier</button>
            <button type="button" data-audio-effect="comfort_noise">Comfort noise</button>
            <button type="button" data-audio-effect="deemphasis">NFM deemphasis</button>
            <button type="button" data-audio-effect="highpass">Highpass</button>
            <button type="button" data-audio-effect="lowpass">Lowpass</button>
            <button type="button" data-audio-effect="notch">Notch filter</button>
          </div>
          <div class="effects-detail">
            <button id="audio_effects_back" class="audio-effects-back" type="button">Back to effects</button>
            <div id="audio_effect_volume" class="audio-effect-panel">
              <h4>Volume multiplier</h4>
              <label class="checkbox-row">
                <input id="audio_volume_enabled" type="checkbox">
                Enable volume multiplier
              </label>
              <label>Multiplier
                <input id="audio_volume_multiplier" type="number" min="0" step="0.1">
              </label>
            </div>
            <div id="audio_effect_comfort_noise" class="audio-effect-panel" hidden>
              <h4>Comfort noise</h4>
              <label class="checkbox-row">
                <input id="audio_comfort_noise_enabled" type="checkbox">
                Enable comfort noise
              </label>
              <label>Level
                <input id="audio_comfort_noise_level" type="number" min="-80" max="-20" step="1">
              </label>
            </div>
            <div id="audio_effect_deemphasis" class="audio-effect-panel" hidden>
              <h4>NFM deemphasis</h4>
              <label class="checkbox-row">
                <input id="audio_deemphasis_enabled" type="checkbox">
                Enable NFM deemphasis
              </label>
              <label>Time constant
                <input id="audio_deemphasis_tau" type="number" min="0" max="530" step="1">
              </label>
            </div>
            <div id="audio_effect_highpass" class="audio-effect-panel" hidden>
              <h4>Highpass</h4>
              <label class="checkbox-row">
                <input id="audio_highpass_enabled" type="checkbox">
                Enable highpass
              </label>
              <label>Frequency
                <input id="audio_highpass_frequency" type="number" min="1" max="900" step="1">
              </label>
              <label>Sharpness
                <input id="audio_highpass_sharpness" type="number" min="0" max="10" step="0.1">
              </label>
            </div>
            <div id="audio_effect_lowpass" class="audio-effect-panel" hidden>
              <h4>Lowpass</h4>
              <label class="checkbox-row">
                <input id="audio_lowpass_enabled" type="checkbox">
                Enable lowpass
              </label>
              <label>Frequency
                <input id="audio_lowpass_frequency" type="number" min="2200" max="12000" step="1">
              </label>
              <label>Sharpness
                <input id="audio_lowpass_sharpness" type="number" min="0" max="10" step="0.1">
              </label>
            </div>
            <div id="audio_effect_notch" class="audio-effect-panel" hidden>
              <h4>Notch filter</h4>
              <label class="checkbox-row">
                <input id="audio_notch_enabled" type="checkbox">
                Enable notch filter
              </label>
              <label>Frequency
                <input id="audio_notch_frequency" type="number" min="1" max="12000" step="1">
              </label>
              <label>Sharpness
                <input id="audio_notch_sharpness" type="number" min="0" max="10" step="0.1">
              </label>
            </div>
          </div>
        </div>
        <div id="audio-effects-result" class="message"></div>
      </div>
      <div id="panel_eas" class="tabpanel" role="tabpanel" aria-labelledby="tab_eas" hidden>
        <h3>EAS recording</h3>
        <div class="grid">
          <label id="eas_enabled_label" class="checkbox-row">
            <input id="eas_enabled" type="checkbox">
            Enable EAS recording
          </label>
          <label>Pre-recording time
            <input id="eas_pre_seconds" type="number" min="0" max="10" step="0.1">
          </label>
          <label>Post-recording time
            <input id="eas_post_seconds" type="number" min="0" max="10" step="0.1">
          </label>
          <label>Maximum recording time
            <input id="eas_max_seconds" type="number" min="1" max="3600" step="1">
          </label>
          <fieldset>
            <legend>Recording format</legend>
            <label><input id="eas_format_wav" name="eas_format" type="radio" value="wav" checked> WAV</label>
            <label><input id="eas_format_mp3" name="eas_format" type="radio" value="mp3"> MP3</label>
          </fieldset>
        </div>
        <div class="hint" id="eas_directory_hint"></div>
        <div id="eas-result" class="message"></div>
      </div>
      <div id="panel_fallback" class="tabpanel" role="tabpanel" aria-labelledby="tab_fallback" hidden>
        <h3>Fallback audio</h3>
        <div class="grid">
          <label class="checkbox-row">
            <input id="fallback_enabled" type="checkbox">
            Enable fallback audio
          </label>
          <label>Fallback delay
            <input id="fallback_delay" type="number" min="30" max="120" step="0.1">
          </label>
          <label>Seconds before restart
            <input id="fallback_loop_delay" type="number" min="0" max="10" step="0.1">
          </label>
        </div>
        <div class="hint">Uses the packaged default fallback.wav audio file.</div>
        <div id="fallback-result" class="message"></div>
      </div>
    </section>
  </div>

  <div id="view_eas_alerts" class="view" hidden>
    <section>
      <h2>EAS alerts</h2>
      <p>View and manage recorded EAS alerts.</p>
      <label>Stream
        <select id="eas_alert_stream"></select>
      </label>
      <div class="actions">
        <button id="open_eas_export" type="button">Export alerts</button>
        <button id="open_eas_delete" type="button">Delete alerts</button>
      </div>
      <div id="eas-alert-list" class="stream-list" aria-live="off"></div>
      <div class="actions">
        <button id="eas_alert_prev" type="button">Previous</button>
        <span id="eas_alert_page" class="hint">Page 1 of 1</span>
        <button id="eas_alert_next" type="button">Next</button>
      </div>
      <div id="eas-alert-result" class="message"></div>
    </section>
  </div>

  <div id="view_eas_alert_export" class="view" hidden>
    <section>
      <h2>Export alerts</h2>
      <p id="eas_export_stream_label" class="hint"></p>
      <fieldset>
        <legend>Choose alerts to export</legend>
        <div id="eas_export_options"></div>
        <div id="eas_export_manual" hidden></div>
      </fieldset>
      <div class="actions">
        <button id="eas_export_alerts" type="button">Export alerts</button>
        <button id="cancel_eas_export" type="button">Cancel</button>
      </div>
      <div id="eas-export-result" class="message"></div>
    </section>
  </div>

  <div id="view_eas_alert_delete" class="view" hidden>
    <section>
      <h2>Delete alerts</h2>
      <p id="eas_delete_stream_label" class="hint"></p>
      <fieldset>
        <legend>Choose alerts to delete</legend>
        <div id="eas_delete_options"></div>
        <div id="eas_delete_manual" hidden></div>
      </fieldset>
      <div class="actions">
        <button id="eas_delete_alerts" type="button">Delete alerts</button>
        <button id="cancel_eas_delete" type="button">Cancel</button>
      </div>
      <div id="eas-delete-result" class="message"></div>
    </section>
  </div>

  <div id="view_eas_alert_detail" class="view" hidden>
    <section>
      <h2 id="eas_alert_detail_title">EAS alert</h2>
      <dl class="details-list">
        <dt>Event type</dt><dd id="eas_detail_event">Unknown</dd>
        <dt>Areas impacted</dt><dd id="eas_detail_areas">Unknown</dd>
        <dt>Issued</dt><dd id="eas_detail_issued">Unknown</dd>
        <dt>Expires</dt><dd id="eas_detail_expires">Unknown</dd>
      </dl>
      <audio id="eas_alert_audio" controls preload="metadata"></audio>
      <div class="actions">
        <a id="eas_alert_download" role="button" href="#">Download alert</a>
        <button id="remove_eas_alert" type="button">Remove alert</button>
        <button id="back_to_eas_alerts" type="button">Back</button>
      </div>
      <div id="eas-alert-detail-result" class="message"></div>
    </section>
  </div>

  <audio id="stream_monitor_audio" autoplay playsinline hidden></audio>
</main>
<script>
const controls = ["serial", "gain", "ppm_correction", "bias_tee", "gain_auto"];
const DEFAULT_STREAM_SAMPLE_RATE = 24000;
const DEFAULT_STREAM_BITRATES = {mp3: 64, ogg: 48};
const STREAM_SERVICE_CUSTOM = "custom";
const STREAM_SERVICE_GWES = "gwes";
const STREAM_SERVICE_WEATHERUSA = "weatherusa";
const STREAM_SERVICE_NWRORG = "nwrorg";
const STREAM_SERVICE_HOSTS = {
  "ingest.wxr.gwes-cdn.net": STREAM_SERVICE_GWES,
  "radio-master.weatherusa.net": STREAM_SERVICE_WEATHERUSA,
  "wxradio.org": STREAM_SERVICE_NWRORG
};
const STREAM_SERVICE_NAMES = {
  custom: "Custom Icecast server",
  gwes: "GWES Weather Radio",
  weatherusa: "WeatherUSA",
  nwrorg: "NOAA Weather Radio Org"
};
const STREAM_SERVICE_HELP = {
  gwes: `If you do not yet have icecast credentials for streaming this station to this service, you'll need to <a href="https://forms.office.com/r/MLx6hKmnCe" target="_blank" rel="noopener noreferrer">submit your stream</a> to GWES Weather Radio and receive icecast credentials.`,
  weatherusa: `If you do not yet have icecast credentials for streaming this station to this service, you must <a href="https://www.weatherusa.net/members/new" target="_blank" rel="noopener noreferrer">create an account on WeatherUSA</a> and <a href="https://www.weatherusa.net/members/services/radio" target="_blank" rel="noopener noreferrer">create a stream</a>. Once your stream is created, you must enter the icecast credentials into this page.`,
  nwrorg: `Use the <a href="https://noaaweatherradio.org/N2radio-finder.php" target="_blank" rel="noopener noreferrer">Weather Radio Station Lookup Utility</a> from NOAA Weather Radio Org to determine what the mountpoint should be.`
};
let applying = false;
let timer = null;
let gainValues = [];
let lastControlSignature = "";
let stationResults = [];
let selectedStationKey = "";
let configuredStreams = [];
let editingStreamId = "";
let editingOutputId = "";
let settingsStreamId = "";
let outputFormMode = "add";
let outputFormDirty = false;
let outputFormOriginalSignature = "";
let outputTableSignature = "";
let activeStreamSnapshots = [];
let fallbackSignature = "";
let fallbackUpdateTimer = null;
let easSignature = "";
let easUpdateTimer = null;
let audioEffectsSignature = "";
let audioEffectsUpdateTimer = null;
let selectedAudioEffect = "volume";
let wizardStep = 0;
let wizardMode = "add";
let wizardDirty = false;
let icecastAuthPassed = false;
let icecastAuthSignature = "";
let activeStreamsSignature = "";
let dashboardAttentionSignature = "";
let dashboardRecentAlertsSignature = "";
let easAlertStreams = [];
let easAlertStreamsSignature = "";
let easAlertListSignature = "";
let easAlertStreamId = "";
let easAlertDetailId = "";
let easAlertPage = 1;
let easAlertTotalPages = 1;
let easAlertReturnPage = 1;
let lastEasAlertRefreshAt = 0;
let easBulkOptionsSignature = "";
let easBulkServerNow = null;
let iqRecorderSignature = "";
let iqStreamOptionsSignature = "";
let iqRecordings = [];
let iqRecordingsSignature = "";
let selectedIqRecordingId = "";
let lastIqRecordingsRefreshAt = 0;
let webRtcSupport = {
  browser: {webrtc: false, opus: false},
  server: {available: false}
};
let monitorClientId = "";
let monitorStreamId = "";
let monitorPeerConnection = null;
let monitorUnstableTimer = null;
let monitorStatsTimer = null;
let monitorLastPacketCount = 0;
let monitorLastPacketAt = 0;
let receiverClientId = "";
let receiverPeerConnection = null;
let receiverPlaying = false;
let receiverPaused = false;
let receiverChannelIndex = 3;
let receiverUnstableTimer = null;
let receiverStatsTimer = null;
let receiverLastPacketCount = 0;
let receiverLastPacketAt = 0;
let unloadLiveAudioStopSent = false;
let currentAccount = null;
let accountsSignature = "";
const MONITOR_UNSTABLE_TIMEOUT_MS = 30000;
const MONITOR_STATS_INTERVAL_MS = 5000;
const WEBRTC_JITTER_BUFFER_TARGET_SECONDS = 0.1;
const IQ_RECORDER_SAMPLE_RATES = [192000, 256000, 384000, 512000, 768000, 1024000, 1536000];
const NWR_RECEIVER_CHANNELS = [
  {frequency_hz: 162400000, label: "162.400 MHz"},
  {frequency_hz: 162425000, label: "162.425 MHz"},
  {frequency_hz: 162450000, label: "162.450 MHz"},
  {frequency_hz: 162475000, label: "162.475 MHz"},
  {frequency_hz: 162500000, label: "162.500 MHz"},
  {frequency_hz: 162525000, label: "162.525 MHz"},
  {frequency_hz: 162550000, label: "162.550 MHz"}
];
const EAS_ALERTS_PER_PAGE = 25;
const PROTECTED_AUDIO_BANDS = [
  {min: 900, max: 1100},
  {min: 1400, max: 1600},
  {min: 2000, max: 2200}
];

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function logClientEvent(level, area, message, details = {}) {
  fetch("/api/client-log", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({level, area, message, details})
  }).catch(() => {});
}

function formatDecimalBytes(value) {
  let bytes = Number(value || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (bytes >= 1000 && index < units.length - 1) {
    bytes /= 1000;
    index += 1;
  }
  return index === 0 ? `${Math.round(bytes)} ${units[index]}` : `${bytes.toFixed(1)} ${units[index]}`;
}

function formatDuration(seconds) {
  seconds = Math.max(0, Math.floor(Number(seconds || 0)));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const remaining = seconds % 60;
  if (hours > 0) return `${hours}:${String(minutes).padStart(2, "0")}:${String(remaining).padStart(2, "0")}`;
  return `${minutes}:${String(remaining).padStart(2, "0")}`;
}

function detectBrowserWebRtcSupport() {
  const peerConnectionClass = window.RTCPeerConnection || window.webkitRTCPeerConnection;
  const receiverClass = window.RTCRtpReceiver;
  let opus = false;
  if (receiverClass && typeof receiverClass.getCapabilities === "function") {
    const capabilities = receiverClass.getCapabilities("audio");
    opus = Boolean(
      capabilities &&
      Array.isArray(capabilities.codecs) &&
      capabilities.codecs.some(codec => String(codec.mimeType || "").toLowerCase() === "audio/opus")
    );
  }
  return {
    webrtc: Boolean(peerConnectionClass),
    opus
  };
}

async function loadWebRtcSupport() {
  const browser = detectBrowserWebRtcSupport();
  let server = {available: false, transport_available: false, opus_available: false};
  try {
    server = await request("/api/webrtc-capabilities");
  } catch (error) {
    server = {available: false, error: error.message};
  }
  webRtcSupport = {browser, server};
  window.nwrWebRtcSupport = webRtcSupport;
  const element = document.getElementById("webrtc_support_status");
  if (element) {
    element.dataset.browserWebrtc = browser.webrtc ? "true" : "false";
    element.dataset.browserOpus = browser.opus ? "true" : "false";
    element.dataset.serverWebrtc = server.available ? "true" : "false";
    element.textContent = `WebRTC browser=${browser.webrtc && browser.opus ? "available" : "unavailable"}, server=${server.available ? "available" : "unavailable"}`;
  }
  return webRtcSupport;
}

function pageMonitorClientId() {
  if (!monitorClientId) {
    monitorClientId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return monitorClientId;
}

function showMonitorUnstableDialog() {
  const dialog = document.getElementById("monitor_unstable_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const button = document.getElementById("dismiss_monitor_unstable");
  if (button) button.focus();
}

function dismissMonitorUnstableDialog() {
  const dialog = document.getElementById("monitor_unstable_dialog");
  if (dialog) dialog.hidden = true;
}

function clearMonitorUnstableTimer() {
  if (monitorUnstableTimer) {
    clearTimeout(monitorUnstableTimer);
    monitorUnstableTimer = null;
  }
}

function clearMonitorStatsTimer() {
  if (monitorStatsTimer) {
    clearInterval(monitorStatsTimer);
    monitorStatsTimer = null;
  }
}

function clearMonitorWatchdogs() {
  clearMonitorUnstableTimer();
  clearMonitorStatsTimer();
}

function resumeMonitorPlayback() {
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio || !audio.srcObject || !monitorStreamId) return;
  if (audio.paused || audio.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    audio.play().catch(error => console.debug("monitor playback resume failed", error));
  }
}

function markMonitorPacketProgress(packetCount) {
  monitorLastPacketCount = packetCount;
  monitorLastPacketAt = Date.now();
  clearMonitorUnstableTimer();
  resumeMonitorPlayback();
}

async function stopMonitorForUnstableConnection(reason = "") {
  if (!monitorStreamId) return;
  clearMonitorUnstableTimer();
  console.warn("monitor connection unstable", reason);
  await stopStreamMonitor({notifyServer: true, unstable: true});
}

function scheduleMonitorUnstableStop(reason = "") {
  if (!monitorStreamId || monitorUnstableTimer) return;
  monitorUnstableTimer = setTimeout(async () => {
    monitorUnstableTimer = null;
    await stopMonitorForUnstableConnection(reason);
  }, MONITOR_UNSTABLE_TIMEOUT_MS);
}

async function pollMonitorPacketStats() {
  const peer = monitorPeerConnection;
  if (!peer || !monitorStreamId) return;
  try {
    const stats = await peer.getStats();
    let packetCount = 0;
    stats.forEach(report => {
      if (report.type === "inbound-rtp" && (report.kind === "audio" || report.mediaType === "audio")) {
        packetCount += Number(report.packetsReceived || 0);
      }
    });
    if (packetCount > monitorLastPacketCount) {
      markMonitorPacketProgress(packetCount);
      return;
    }
    if (monitorLastPacketAt && Date.now() - monitorLastPacketAt >= MONITOR_UNSTABLE_TIMEOUT_MS) {
      await stopMonitorForUnstableConnection("no incoming audio packets");
    }
  } catch (error) {
    console.debug("monitor packet stats failed", error);
  }
}

function startMonitorPacketStats() {
  clearMonitorStatsTimer();
  monitorLastPacketCount = 0;
  monitorLastPacketAt = Date.now();
  monitorStatsTimer = setInterval(pollMonitorPacketStats, MONITOR_STATS_INTERVAL_MS);
}

async function startStreamMonitor(streamId) {
  if (monitorStreamId === streamId && monitorPeerConnection) return;
  logClientEvent("info", "monitor", "monitor start requested", {stream_id: streamId});
  await stopWeatherReceiver({notifyServer: true});
  await stopStreamMonitor({notifyServer: true});
  if (!webRtcSupport.browser.webrtc || !webRtcSupport.browser.opus) {
    logClientEvent("warning", "monitor", "browser does not support WebRTC Opus monitoring", webRtcSupport.browser);
    throw new Error("This browser does not support WebRTC Opus audio monitoring.");
  }
  if (!webRtcSupport.server.available) {
    const error = webRtcSupport.server.transport_error || webRtcSupport.server.opus_error || "Server WebRTC support is unavailable.";
    logClientEvent("warning", "monitor", "server WebRTC support unavailable", webRtcSupport.server);
    throw new Error(error);
  }
  const peer = new RTCPeerConnection({iceServers: []});
  monitorPeerConnection = peer;
  monitorStreamId = streamId;
  const audio = document.getElementById("stream_monitor_audio");
  const transceiver = peer.addTransceiver("audio", {direction: "recvonly"});
  if (transceiver.receiver && "jitterBufferTarget" in transceiver.receiver) {
    try {
      transceiver.receiver.jitterBufferTarget = WEBRTC_JITTER_BUFFER_TARGET_SECONDS;
    } catch (error) {
      console.debug("WebRTC receiver jitterBufferTarget is not writable", error);
    }
  }
  peer.addEventListener("track", event => {
    if (event.receiver && "jitterBufferTarget" in event.receiver) {
      try {
        event.receiver.jitterBufferTarget = WEBRTC_JITTER_BUFFER_TARGET_SECONDS;
      } catch (error) {
        console.debug("WebRTC track jitterBufferTarget is not writable", error);
      }
    }
    audio.srcObject = event.streams && event.streams[0] ? event.streams[0] : new MediaStream([event.track]);
    audio.hidden = true;
    audio.play().catch(error => setStreamResult(`Monitoring audio could not start: ${error.message}`, "error"));
    for (const eventName of ["waiting", "stalled", "suspend"]) {
      audio.addEventListener(eventName, () => {
        if (monitorPeerConnection === peer) scheduleMonitorUnstableStop(`audio ${eventName}`);
      });
    }
    audio.addEventListener("playing", () => {
      if (monitorPeerConnection === peer) clearMonitorUnstableTimer();
    });
    event.track.addEventListener("mute", () => {
      if (monitorPeerConnection === peer) scheduleMonitorUnstableStop("audio track muted");
    });
    event.track.addEventListener("unmute", () => {
      if (monitorPeerConnection === peer) {
        clearMonitorUnstableTimer();
        resumeMonitorPlayback();
      }
    });
  });
  peer.addEventListener("connectionstatechange", () => {
    if (monitorPeerConnection !== peer) return;
    if (["connected"].includes(peer.connectionState)) {
      clearMonitorUnstableTimer();
      return;
    }
    if (["failed", "closed", "disconnected"].includes(peer.connectionState)) {
      scheduleMonitorUnstableStop(`connectionState=${peer.connectionState}`);
    }
  });
  peer.addEventListener("iceconnectionstatechange", () => {
    if (monitorPeerConnection !== peer) return;
    if (["connected", "completed"].includes(peer.iceConnectionState)) {
      clearMonitorUnstableTimer();
      return;
    }
    if (["failed", "closed", "disconnected"].includes(peer.iceConnectionState)) {
      scheduleMonitorUnstableStop(`iceConnectionState=${peer.iceConnectionState}`);
    }
  });
  const offer = await peer.createOffer();
  await peer.setLocalDescription(offer);
  const data = await request("/api/monitor/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      client_id: pageMonitorClientId(),
      stream_id: streamId,
      sdp: peer.localDescription.sdp,
      type: peer.localDescription.type
    })
  });
  await peer.setRemoteDescription(data.answer);
  logClientEvent("info", "monitor", "monitor WebRTC answer accepted", {stream_id: streamId});
  startMonitorPacketStats();
  renderStreams(configuredStreams);
  if (settingsStreamId) renderStreamSettings();
}

async function stopStreamMonitor(options = {}) {
  const notifyServer = options.notifyServer !== false;
  const unstable = options.unstable === true;
  const clientId = pageMonitorClientId();
  const peer = monitorPeerConnection;
  monitorPeerConnection = null;
  monitorStreamId = "";
  clearMonitorWatchdogs();
  const audio = document.getElementById("stream_monitor_audio");
  if (audio) {
    audio.pause();
    audio.srcObject = null;
  }
  if (peer) peer.close();
  if (notifyServer) {
    try {
      await request("/api/monitor/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({client_id: clientId})
      });
    } catch (error) {
      console.warn("monitor stop failed", error);
    }
  }
  renderStreams(configuredStreams);
  if (settingsStreamId) renderStreamSettings();
  if (unstable) showMonitorUnstableDialog();
}

async function toggleStreamMonitor(streamId, resultHandler = setStreamResult) {
  if (monitorStreamId === streamId) {
    logClientEvent("info", "monitor", "monitor stop requested", {stream_id: streamId});
    await stopStreamMonitor();
    resultHandler("Monitoring stopped.", "success");
    return;
  }
  resultHandler("Starting monitor...");
  try {
    await startStreamMonitor(streamId);
    resultHandler("Monitoring started.", "success");
  } catch (error) {
    logClientEvent("warning", "monitor", "monitor start failed in browser", {stream_id: streamId, error: error.message});
    throw error;
  }
}

function pageReceiverClientId() {
  if (!receiverClientId) {
    receiverClientId = window.crypto && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
  }
  return receiverClientId;
}

function currentReceiverChannel() {
  return NWR_RECEIVER_CHANNELS[receiverChannelIndex] || NWR_RECEIVER_CHANNELS[3];
}

function renderReceiverControls() {
  const channel = currentReceiverChannel();
  setText("receiver_frequency", channel.label);
  const playPause = document.getElementById("receiver_play_pause");
  if (playPause) playPause.textContent = receiverPlaying ? "Pause" : "Play";
}

function setReceiverResult(message, kind = "") {
  const element = document.getElementById("receiver-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function showReceiverUnstableDialog() {
  const dialog = document.getElementById("receiver_unstable_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const button = document.getElementById("dismiss_receiver_unstable");
  if (button) button.focus();
}

function dismissReceiverUnstableDialog() {
  const dialog = document.getElementById("receiver_unstable_dialog");
  if (dialog) dialog.hidden = true;
}

function clearReceiverUnstableTimer() {
  if (receiverUnstableTimer) {
    clearTimeout(receiverUnstableTimer);
    receiverUnstableTimer = null;
  }
}

function clearReceiverStatsTimer() {
  if (receiverStatsTimer) {
    clearInterval(receiverStatsTimer);
    receiverStatsTimer = null;
  }
}

function clearReceiverWatchdogs() {
  clearReceiverUnstableTimer();
  clearReceiverStatsTimer();
}

function resumeReceiverPlayback() {
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio || !audio.srcObject || !receiverPlaying) return;
  if (audio.paused || audio.readyState < HTMLMediaElement.HAVE_CURRENT_DATA) {
    audio.play().catch(error => console.debug("receiver playback resume failed", error));
  }
}

function markReceiverPacketProgress(packetCount) {
  receiverLastPacketCount = packetCount;
  receiverLastPacketAt = Date.now();
  clearReceiverUnstableTimer();
  resumeReceiverPlayback();
}

async function stopReceiverForUnstableConnection(reason = "") {
  if (!receiverPeerConnection) return;
  clearReceiverUnstableTimer();
  console.warn("weather receiver connection unstable", reason);
  await stopWeatherReceiver({notifyServer: true, unstable: true});
}

function scheduleReceiverUnstableStop(reason = "") {
  if (!receiverPlaying || receiverPaused || receiverUnstableTimer) return;
  receiverUnstableTimer = setTimeout(async () => {
    receiverUnstableTimer = null;
    await stopReceiverForUnstableConnection(reason);
  }, MONITOR_UNSTABLE_TIMEOUT_MS);
}

async function pollReceiverPacketStats() {
  const peer = receiverPeerConnection;
  if (!peer || !receiverPlaying || receiverPaused) return;
  try {
    const stats = await peer.getStats();
    let packetCount = 0;
    stats.forEach(report => {
      if (report.type === "inbound-rtp" && (report.kind === "audio" || report.mediaType === "audio")) {
        packetCount += Number(report.packetsReceived || 0);
      }
    });
    if (packetCount > receiverLastPacketCount) {
      markReceiverPacketProgress(packetCount);
      return;
    }
    if (receiverLastPacketAt && Date.now() - receiverLastPacketAt >= MONITOR_UNSTABLE_TIMEOUT_MS) {
      await stopReceiverForUnstableConnection("no incoming audio packets");
    }
  } catch (error) {
    console.debug("receiver packet stats failed", error);
  }
}

function startReceiverPacketStats() {
  clearReceiverStatsTimer();
  receiverLastPacketCount = 0;
  receiverLastPacketAt = Date.now();
  receiverStatsTimer = setInterval(pollReceiverPacketStats, MONITOR_STATS_INTERVAL_MS);
}

function updateReceiverMediaSession() {
  if (!("mediaSession" in navigator) || !("MediaMetadata" in window) || !receiverPeerConnection) return;
  const channel = currentReceiverChannel();
  navigator.mediaSession.metadata = new MediaMetadata({
    title: channel.label,
    artist: "NOAA Weather Radio"
  });
  navigator.mediaSession.playbackState = receiverPlaying ? "playing" : "paused";
  try {
    navigator.mediaSession.setActionHandler("previoustrack", () => receiverPreviousChannel());
    navigator.mediaSession.setActionHandler("nexttrack", () => receiverNextChannel());
    navigator.mediaSession.setActionHandler("play", () => startWeatherReceiver());
    navigator.mediaSession.setActionHandler("pause", () => stopWeatherReceiver({preserveMediaSession: true}));
  } catch (error) {
    console.debug("media session action setup failed", error);
  }
}

function clearReceiverMediaSession() {
  if (!("mediaSession" in navigator)) return;
  try {
    for (const action of ["previoustrack", "nexttrack", "play", "pause"]) {
      navigator.mediaSession.setActionHandler(action, null);
    }
    navigator.mediaSession.metadata = null;
    navigator.mediaSession.playbackState = "none";
  } catch (error) {
    console.debug("media session cleanup failed", error);
  }
}

function setReceiverAudioTracksEnabled(enabled) {
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio || !audio.srcObject || typeof audio.srcObject.getAudioTracks !== "function") return;
  for (const track of audio.srcObject.getAudioTracks()) {
    track.enabled = enabled;
  }
}

async function startWeatherReceiver() {
  if (receiverPeerConnection) {
    logClientEvent("info", "receiver", "receiver resume requested", {frequency: currentReceiverChannel().label});
    receiverPlaying = true;
    receiverPaused = false;
    clearReceiverUnstableTimer();
    setReceiverAudioTracksEnabled(true);
    const audio = document.getElementById("stream_monitor_audio");
    if (audio && audio.srcObject) {
      await audio.play();
    }
    startReceiverPacketStats();
    updateReceiverMediaSession();
    renderReceiverControls();
    setReceiverResult(`Listening to ${currentReceiverChannel().label}.`, "success");
    return;
  }
  logClientEvent("info", "receiver", "receiver start requested", {frequency: currentReceiverChannel().label});
  await stopStreamMonitor({notifyServer: true});
  if (!webRtcSupport.browser.webrtc || !webRtcSupport.browser.opus) {
    logClientEvent("warning", "receiver", "browser does not support WebRTC Opus receiver", webRtcSupport.browser);
    throw new Error("This browser does not support WebRTC Opus audio.");
  }
  if (!webRtcSupport.server.available) {
    const error = webRtcSupport.server.transport_error || webRtcSupport.server.opus_error || "Server WebRTC support is unavailable.";
    logClientEvent("warning", "receiver", "server WebRTC support unavailable", webRtcSupport.server);
    throw new Error(error);
  }
  const peer = new RTCPeerConnection({iceServers: []});
  receiverPeerConnection = peer;
  receiverPlaying = true;
  receiverPaused = false;
  renderReceiverControls();
  const audio = document.getElementById("stream_monitor_audio");
  const transceiver = peer.addTransceiver("audio", {direction: "recvonly"});
  if (transceiver.receiver && "jitterBufferTarget" in transceiver.receiver) {
    try {
      transceiver.receiver.jitterBufferTarget = WEBRTC_JITTER_BUFFER_TARGET_SECONDS;
    } catch (error) {
      console.debug("WebRTC receiver jitterBufferTarget is not writable", error);
    }
  }
  peer.addEventListener("track", event => {
    event.track.enabled = !receiverPaused;
    if (event.receiver && "jitterBufferTarget" in event.receiver) {
      try {
        event.receiver.jitterBufferTarget = WEBRTC_JITTER_BUFFER_TARGET_SECONDS;
      } catch (error) {
        console.debug("WebRTC track jitterBufferTarget is not writable", error);
      }
    }
    audio.srcObject = event.streams && event.streams[0] ? event.streams[0] : new MediaStream([event.track]);
    audio.hidden = true;
    setReceiverAudioTracksEnabled(!receiverPaused);
    if (!receiverPaused) {
      audio.play().catch(error => setReceiverResult(`Receiver audio could not start: ${error.message}`, "error"));
    }
    for (const eventName of ["waiting", "stalled", "suspend"]) {
      audio.addEventListener(eventName, () => {
        if (receiverPeerConnection === peer) scheduleReceiverUnstableStop(`audio ${eventName}`);
      });
    }
    audio.addEventListener("playing", () => {
      if (receiverPeerConnection === peer) clearReceiverUnstableTimer();
    });
    event.track.addEventListener("mute", () => {
      if (receiverPeerConnection === peer) scheduleReceiverUnstableStop("audio track muted");
    });
    event.track.addEventListener("unmute", () => {
      if (receiverPeerConnection === peer) {
        clearReceiverUnstableTimer();
        resumeReceiverPlayback();
      }
    });
  });
  peer.addEventListener("connectionstatechange", () => {
    if (receiverPeerConnection !== peer) return;
    if (peer.connectionState === "connected") {
      clearReceiverUnstableTimer();
      return;
    }
    if (["failed", "closed", "disconnected"].includes(peer.connectionState)) {
      scheduleReceiverUnstableStop(`connectionState=${peer.connectionState}`);
    }
  });
  peer.addEventListener("iceconnectionstatechange", () => {
    if (receiverPeerConnection !== peer) return;
    if (["connected", "completed"].includes(peer.iceConnectionState)) {
      clearReceiverUnstableTimer();
      return;
    }
    if (["failed", "closed", "disconnected"].includes(peer.iceConnectionState)) {
      scheduleReceiverUnstableStop(`iceConnectionState=${peer.iceConnectionState}`);
    }
  });
  try {
    const offer = await peer.createOffer();
    await peer.setLocalDescription(offer);
    const channel = currentReceiverChannel();
    const data = await request("/api/receiver/start", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        client_id: pageReceiverClientId(),
        frequency_hz: channel.frequency_hz,
        sdp: peer.localDescription.sdp,
        type: peer.localDescription.type
      })
    });
    await peer.setRemoteDescription(data.answer);
    logClientEvent("info", "receiver", "receiver WebRTC answer accepted", {frequency: channel.label});
    startReceiverPacketStats();
    updateReceiverMediaSession();
    setReceiverResult(`Listening to ${channel.label}.`, "success");
  } catch (error) {
    logClientEvent("warning", "receiver", "receiver start failed in browser", {frequency: currentReceiverChannel().label, error: error.message});
    await stopWeatherReceiver({notifyServer: true});
    throw error;
  }
}

async function stopWeatherReceiver(options = {}) {
  const notifyServer = options.notifyServer !== false;
  const unstable = options.unstable === true;
  const preserveMediaSession = options.preserveMediaSession === true;
  const clientId = pageReceiverClientId();
  const peer = receiverPeerConnection;
  receiverPlaying = false;
  receiverPaused = preserveMediaSession && !!peer;
  clearReceiverWatchdogs();
  const audio = document.getElementById("stream_monitor_audio");
  if (preserveMediaSession && peer) {
    setReceiverAudioTracksEnabled(false);
    if (audio) audio.pause();
    updateReceiverMediaSession();
    renderReceiverControls();
    return;
  }
  receiverPeerConnection = null;
  receiverPaused = false;
  clearReceiverMediaSession();
  if (audio) {
    audio.pause();
    audio.srcObject = null;
  }
  if (peer) peer.close();
  if (notifyServer) {
    try {
      await request("/api/receiver/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({client_id: clientId})
      });
    } catch (error) {
      console.warn("receiver stop failed", error);
    }
  }
  renderReceiverControls();
  if (unstable) showReceiverUnstableDialog();
}

async function setReceiverChannel(index) {
  const count = NWR_RECEIVER_CHANNELS.length;
  receiverChannelIndex = ((index % count) + count) % count;
  renderReceiverControls();
  updateReceiverMediaSession();
  if (!receiverPeerConnection) return;
  const channel = currentReceiverChannel();
  await request("/api/receiver/tune", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      client_id: pageReceiverClientId(),
      frequency_hz: channel.frequency_hz
    })
  });
  setReceiverResult(`Listening to ${channel.label}.`, "success");
}

async function receiverPreviousChannel() {
  try {
    await setReceiverChannel(receiverChannelIndex - 1);
  } catch (error) {
    setReceiverResult(error.message, "error");
  }
}

async function receiverNextChannel() {
  try {
    await setReceiverChannel(receiverChannelIndex + 1);
  } catch (error) {
    setReceiverResult(error.message, "error");
  }
}

function deviceLabel(device) {
  const parts = [device.vendor, device.name || "RTL-SDR", `serial ${device.serial}`].filter(Boolean);
  return parts.join(", ");
}

function configuredDeviceLabel(serial) {
  return `Configured SDR, serial ${serial} (disconnected)`;
}

async function loadDevices(selected, options = {}) {
  const data = await request("/api/devices");
  const select = document.getElementById("serial");
  const selectedDevice = data.devices.find(device => device.serial === selected);
  const optionSignature = data.devices.map(device => [device.serial, deviceLabel(device)]);
  if (selected && !selectedDevice) {
    optionSignature.unshift([selected, configuredDeviceLabel(selected)]);
  }
  const needsChoice = !selected && data.devices.length !== 1;
  const signature = JSON.stringify({options: optionSignature, needsChoice});
  if (options.force || select.dataset.signature !== signature) {
    select.innerHTML = "";
    if (selected && !selectedDevice) {
      const option = document.createElement("option");
      option.value = selected;
      option.textContent = configuredDeviceLabel(selected);
      select.appendChild(option);
    } else if (needsChoice) {
      const option = document.createElement("option");
      option.value = "";
      option.textContent = data.devices.length === 0 ? "No RTL-SDR devices found" : "Select an RTL-SDR...";
      select.appendChild(option);
    }
    for (const device of data.devices) {
      const option = document.createElement("option");
      option.value = device.serial;
      option.textContent = deviceLabel(device);
      select.appendChild(option);
    }
    select.dataset.signature = signature;
  }
  const fallback = !selected && data.devices.length === 1 ? data.devices[0].serial : "";
  setValue("serial", selected || fallback);
  setText("device-errors", data.errors.join(" | "));
}

function stationLabel(station) {
  const place = [station.city, station.state].filter(Boolean).join(", ");
  const site = station.site_name && station.site_name !== station.city ? `, ${station.site_name}` : "";
  return `${station.callsign} ${station.frequency} MHz, ${place}${site}`;
}

async function searchStations() {
  const query = document.getElementById("station_search").value;
  const data = await request(`/api/stations?q=${encodeURIComponent(query)}&limit=75`);
  stationResults = data.stations || [];
  const select = document.getElementById("station_results");
  select.innerHTML = "";
  if (stationResults.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "No stations found";
    select.appendChild(option);
  }
  for (const station of stationResults) {
    const option = document.createElement("option");
    option.value = station.key;
    option.textContent = stationLabel(station);
    select.appendChild(option);
  }
  selectedStationKey = "";
  setText("selected_station", "Select a station to continue.");
  renderWizard();
}

function selectedStation() {
  return stationResults.find(station => station.key === selectedStationKey);
}

function chooseStation() {
  selectedStationKey = document.getElementById("station_results").value;
  const station = selectedStation();
  if (!station) {
    setText("selected_station", "Select a station to continue.");
    renderWizard();
    return;
  }
  setText("selected_station", `Selected ${stationLabel(station)}`);
  wizardDirty = true;
  renderWizard();
}

function streamPayload() {
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  const station = selectedStation();
  const selectedFormat = selectedWizardFormat();
  const icecast = applyServicePresetToIcecast({
    host: document.getElementById("icecast_host").value,
    port: Number(document.getElementById("icecast_port").value),
    username: document.getElementById("icecast_username").value,
    password: document.getElementById("icecast_password").value,
    mount: document.getElementById("icecast_mount").value,
    format: selectedFormat,
    sample_rate: Number(document.getElementById("icecast_sample_rate").value),
    bitrate: Number(document.getElementById("icecast_bitrate").value)
  }, service, station, "icecast");
  return {
    station_key: selectedStationKey,
    icecast
  };
}

function outputEditPayload() {
  return {
    stream_id: editingStreamId,
    output_id: editingOutputId,
    enabled: document.getElementById("output_enabled").checked,
    icecast: streamPayload().icecast
  };
}

async function setStreamEnabled(streamId, enabled, resultHandler = setStreamResult) {
  if (accountIsReadOnly()) {
    resultHandler("This account is read-only.", "error");
    return null;
  }
  const data = await request("/api/streams", {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({stream_id: streamId, enabled})
  });
  renderStreams(data.streams || []);
  resultHandler(enabled ? "Stream started." : "Stream stopped.", "success");
  return data;
}

function streamLabel(stream) {
  const station = stream.station;
  const output = streamOutputs(stream)[0] || {};
  const icecast = output.icecast || stream.icecast || {};
  if (!station || !icecast.host) return "Incomplete stream";
  return `${station.callsign} ${station.frequency} MHz to ${icecast.username}@${icecast.host}:${icecast.port}${icecast.mount} (${icecast.format.toUpperCase()})`;
}

function normalizeStreamStatus(status) {
  const normalized = String(status || "disabled").toLowerCase().replace(/_/g, "-");
  if (normalized === "enabled" || normalized === "needs-attention" || normalized === "disabled") {
    return normalized;
  }
  return "disabled";
}

function streamStatusLabel(status) {
  const normalized = normalizeStreamStatus(status);
  if (normalized === "enabled") return "Enabled";
  if (normalized === "needs-attention") return "Needs attention";
  return "Disabled";
}

function streamOutputCount(stream) {
  return enabledStreamOutputCount(stream);
}

function enabledIcecastOutputCount(stream) {
  return streamOutputs(stream).filter(output => output.enabled !== false).length;
}

function easRecordingEnabled(stream) {
  return Boolean(stream && stream.eas_recording && stream.eas_recording.enabled);
}

function enabledStreamOutputCount(stream) {
  return enabledIcecastOutputCount(stream) + (easRecordingEnabled(stream) ? 1 : 0);
}

function canDisableIcecastOutput(stream, output) {
  if (!output || output.enabled === false) return true;
  return enabledStreamOutputCount(stream) > 1;
}

function canRemoveIcecastOutput(stream, output) {
  if (!output || output.enabled === false) return true;
  return enabledStreamOutputCount(stream) > 1;
}

function canDisableEasRecording(stream) {
  if (!easRecordingEnabled(stream)) return true;
  return enabledStreamOutputCount(stream) > 1;
}

function populateBitrates() {
  for (const select of [document.getElementById("icecast_bitrate"), document.getElementById("settings_icecast_bitrate")]) {
    if (!select || select.options.length) continue;
    for (let bitrate = 8; bitrate <= 320; bitrate += 8) {
      const option = document.createElement("option");
      option.value = String(bitrate);
      option.textContent = `${bitrate} Kbps`;
      if (bitrate === 48) option.selected = true;
      select.appendChild(option);
    }
  }
}

function streamOutputs(stream) {
  if (Array.isArray(stream.outputs)) return stream.outputs;
  if (stream.icecast) return [{id: stream.id, enabled: stream.enabled !== false, icecast: stream.icecast}];
  return [];
}

function normalizedServiceHost(host) {
  return String(host || "").trim().toLowerCase();
}

function serviceForHost(host) {
  return STREAM_SERVICE_HOSTS[normalizedServiceHost(host)] || STREAM_SERVICE_CUSTOM;
}

function stationSiteToken(station) {
  return String((station && station.site_name) || "")
    .replace(/[^A-Za-z0-9 ]+/g, " ")
    .split(/\\s+/)
    .filter(Boolean)
    .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join("");
}

function stationCallsign(station) {
  return String((station && station.callsign) || "").trim().toUpperCase();
}

function stationState(station) {
  return String((station && station.state) || "").trim().toUpperCase();
}

function selectedWizardFormat() {
  const selectedFormat = document.querySelector("input[name='icecast_format']:checked");
  return selectedFormat ? selectedFormat.value : "mp3";
}

function selectedSettingsFormat() {
  const selectedFormat = document.querySelector("input[name='settings_icecast_format']:checked");
  return selectedFormat ? selectedFormat.value : "mp3";
}

function weatherUsaMount(station, format) {
  const callsign = stationCallsign(station) || "CALLSIGN";
  return `/NWR/${callsign}.${format || "mp3"}`;
}

function isWeatherUsaGeneratedMount(value, station) {
  const mount = String(value || "").trim();
  return mount === weatherUsaMount(station, "mp3") || mount === weatherUsaMount(station, "ogg");
}

function nwrOrgBaseMount(station) {
  const state = stationState(station) || "XX";
  const site = stationSiteToken(station) || "Site";
  const callsign = stationCallsign(station) || "CALLSIGN";
  return `/${state}-${site}-${callsign}`;
}

function nwrOrgMount(station, alternate, alternateNumber) {
  const suffix = alternate ? `-alt${Math.max(1, Math.min(9, Number(alternateNumber || 1)))}` : "";
  return `${nwrOrgBaseMount(station)}${suffix}`;
}

function serviceFromIcecast(icecast) {
  const explicit = String((icecast && icecast.service) || "").trim();
  if (explicit && STREAM_SERVICE_NAMES[explicit]) return explicit;
  return serviceForHost(icecast && icecast.host);
}

function applyServicePresetToIcecast(icecast, service, station, prefix = "icecast") {
  const payload = Object.assign({}, icecast, {service});
  const format = payload.format || "mp3";
  if (service === STREAM_SERVICE_GWES) {
    payload.host = "ingest.wxr.gwes-cdn.net";
    payload.port = 10000;
    payload.format = "mp3";
    payload.bitrate = Math.max(64, Number(payload.bitrate || 64));
    payload.sample_rate = Number(payload.sample_rate || 22050);
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    payload.host = "radio-master.weatherusa.net";
    payload.port = 80;
    payload.username = "source";
    payload.format = format;
    payload.bitrate = Math.max(32, Math.min(56, Number(payload.bitrate || (format === "mp3" ? 56 : 48))));
    payload.sample_rate = Math.min(22050, Number(payload.sample_rate || 22050));
    if (!String(payload.mount || "").trim()) payload.mount = weatherUsaMount(station, payload.format);
  } else if (service === STREAM_SERVICE_NWRORG) {
    const altEnabled = document.getElementById(`${prefix}_alt_enabled`);
    const altNumber = document.getElementById(`${prefix}_alt_number`);
    payload.host = "wxradio.org";
    payload.port = 8000;
    payload.username = "source";
    payload.password = "WxRadio2014";
    payload.format = "mp3";
    payload.bitrate = 32;
    payload.sample_rate = 22050;
    payload.mount = nwrOrgMount(station, altEnabled && altEnabled.checked, altNumber ? altNumber.value : 1);
  }
  return payload;
}

function setServiceHelp(elementId, service) {
  const element = document.getElementById(elementId);
  if (!element) return;
  element.innerHTML = STREAM_SERVICE_HELP[service] || "";
}

function setHidden(id, hidden) {
  const element = document.getElementById(id);
  if (element) element.hidden = hidden;
}

function setReadOnly(id, readOnly) {
  const element = document.getElementById(id);
  if (element) element.readOnly = readOnly;
}

function setSelectDisabled(id, disabled) {
  const element = document.getElementById(id);
  if (element) element.disabled = disabled;
}

function setOptionAvailability(selectId, predicate) {
  const select = document.getElementById(selectId);
  if (!select) return;
  let selectedStillAvailable = false;
  for (const option of select.options) {
    const allowed = predicate(Number(option.value));
    option.disabled = !allowed;
    if (option.selected && allowed) selectedStillAvailable = true;
  }
  if (!selectedStillAvailable) {
    for (const option of select.options) {
      if (!option.disabled) {
        select.value = option.value;
        break;
      }
    }
  }
}

function clampServiceFields(prefix, service) {
  if (service === STREAM_SERVICE_GWES) {
    setOptionAvailability(`${prefix}_bitrate`, value => value >= 64);
    setOptionAvailability(`${prefix}_sample_rate`, () => true);
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    setOptionAvailability(`${prefix}_bitrate`, value => value >= 32 && value <= 56);
    setOptionAvailability(`${prefix}_sample_rate`, value => value <= 22050);
  } else if (service === STREAM_SERVICE_NWRORG) {
    setOptionAvailability(`${prefix}_bitrate`, value => value === 32);
    setOptionAvailability(`${prefix}_sample_rate`, value => value === 22050);
    setValue(`${prefix}_bitrate`, 32);
    setValue(`${prefix}_sample_rate`, 22050);
  } else {
    setOptionAvailability(`${prefix}_bitrate`, () => true);
    setOptionAvailability(`${prefix}_sample_rate`, () => true);
  }
}

function applyServiceControls(prefix, service) {
  setHidden(`${prefix}_host_label`, service !== STREAM_SERVICE_CUSTOM);
  setHidden(`${prefix}_port_label`, service !== STREAM_SERVICE_CUSTOM);
  setHidden(`${prefix}_username_label`, service === STREAM_SERVICE_WEATHERUSA || service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_password_label`, service === STREAM_SERVICE_NWRORG);
  setHidden(prefix === "icecast" ? "show_icecast_password_label" : "settings_show_icecast_password_label", service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_mount_label`, service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_alt_label`, service !== STREAM_SERVICE_NWRORG);
  const alternateEnabled = document.getElementById(`${prefix}_alt_enabled`);
  setHidden(`${prefix}_alt_number_label`, service !== STREAM_SERVICE_NWRORG || !alternateEnabled || !alternateEnabled.checked);
  setHidden(`${prefix}_format_fieldset`, service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_sample_rate_label`, service === STREAM_SERVICE_NWRORG);
  setHidden(`${prefix}_bitrate_label`, service === STREAM_SERVICE_NWRORG);
  setReadOnly(`${prefix}_username`, service === STREAM_SERVICE_WEATHERUSA);
  setSelectDisabled(`${prefix}_sample_rate`, service === STREAM_SERVICE_NWRORG);
  setSelectDisabled(`${prefix}_bitrate`, service === STREAM_SERVICE_NWRORG);
  const mp3 = document.getElementById(`${prefix}_format_mp3`);
  const ogg = document.getElementById(`${prefix}_format_ogg`);
  if (mp3) mp3.disabled = service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG;
  if (ogg) ogg.disabled = service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG;
  clampServiceFields(prefix, service);
}

function findConfiguredOutput(streamId, outputId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) return null;
  const outputs = streamOutputs(stream);
  const output = outputs.find(item => item.id === outputId);
  if (!output) return null;
  return {stream, output, outputs};
}

function setIcecastForm(icecast, enabled = true) {
  const service = serviceFromIcecast(icecast);
  const station = selectedStation();
  const preset = applyServicePresetToIcecast(icecast || {}, service, station, "icecast");
  setValue("icecast_service", service);
  setValue("icecast_host", preset.host || "");
  setValue("icecast_port", preset.port || "");
  setValue("icecast_username", preset.username || "");
  setValue("icecast_password", preset.password || "");
  setValue("icecast_mount", preset.mount || "");
  const format = (icecast && icecast.format) || "mp3";
  setChecked("icecast_format_mp3", preset.format === "mp3");
  setChecked("icecast_format_ogg", preset.format === "ogg");
  setValue("icecast_sample_rate", preset.sample_rate || 24000);
  setValue("icecast_bitrate", preset.bitrate || (format === "mp3" ? 64 : 48));
  setChecked("output_enabled", enabled);
  setServiceHelp("icecast_service_help", service);
  applyServiceControls("icecast", service);
}

function settingsIcecastPayload() {
  const service = document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM;
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  const station = selected ? selected.stream.station : currentSettingsStream() ? currentSettingsStream().station : null;
  return applyServicePresetToIcecast({
    host: document.getElementById("settings_icecast_host").value,
    port: Number(document.getElementById("settings_icecast_port").value),
    username: document.getElementById("settings_icecast_username").value,
    password: document.getElementById("settings_icecast_password").value,
    mount: document.getElementById("settings_icecast_mount").value,
    format: selectedSettingsFormat(),
    sample_rate: Number(document.getElementById("settings_icecast_sample_rate").value),
    bitrate: Number(document.getElementById("settings_icecast_bitrate").value)
  }, service, station, "settings_icecast");
}

function outputFormSignature() {
  return JSON.stringify(settingsIcecastPayload());
}

function outputCredentialSignature(icecast) {
  return JSON.stringify({
    host: icecast.host,
    port: Number(icecast.port),
    username: icecast.username,
    password: icecast.password,
    mount: icecast.mount
  });
}

function duplicateOutputExists(icecast, ignoreOutputId = "") {
  const signature = outputCredentialSignature(icecast);
  for (const stream of configuredStreams) {
    for (const output of streamOutputs(stream)) {
      if (ignoreOutputId && output.id === ignoreOutputId) continue;
      if (outputCredentialSignature(output.icecast || {}) === signature) return true;
    }
  }
  return false;
}

function customServiceHostWarning(host) {
  const service = serviceForHost(host);
  if (service === STREAM_SERVICE_CUSTOM) return "";
  return `Please select ${STREAM_SERVICE_NAMES[service]} instead of typing its Icecast URL in custom Icecast setup.`;
}

function settingsCredentialsComplete() {
  const payload = settingsIcecastPayload();
  const service = document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (service === STREAM_SERVICE_CUSTOM && customServiceHostWarning(payload.host)) return false;
  return Boolean(
    payload.host.trim() &&
    payload.port >= 1 &&
    payload.port <= 65535 &&
    payload.username.trim() &&
    payload.password &&
    payload.mount.trim()
  );
}

function setSettingsIcecastForm(icecast) {
  const service = serviceFromIcecast(icecast);
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  const station = selected ? selected.stream.station : currentSettingsStream() ? currentSettingsStream().station : null;
  const preset = applyServicePresetToIcecast(icecast || {}, service, station, "settings_icecast");
  setValue("settings_icecast_service", service);
  setValue("settings_icecast_host", preset.host || "");
  setValue("settings_icecast_port", preset.port || "");
  setValue("settings_icecast_username", preset.username || "");
  setValue("settings_icecast_password", preset.password || "");
  setValue("settings_icecast_mount", preset.mount || "");
  const format = preset.format || "mp3";
  setChecked("settings_icecast_format_mp3", format === "mp3");
  setChecked("settings_icecast_format_ogg", format === "ogg");
  setValue("settings_icecast_sample_rate", preset.sample_rate || 24000);
  setValue("settings_icecast_bitrate", preset.bitrate || (format === "mp3" ? 64 : 48));
  setServiceHelp("settings_icecast_service_help", service);
  applyServiceControls("settings_icecast", service);
}

function clearSettingsIcecastForm() {
  setSettingsIcecastForm({service: STREAM_SERVICE_CUSTOM, format: "mp3", sample_rate: 24000, bitrate: 64});
  setChecked("settings_show_icecast_password", false);
  document.getElementById("settings_icecast_password").type = "password";
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
}

function setOutputResult(message, kind = "") {
  const element = document.getElementById("output-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function dismissNwrOrgSubmissionDialog() {
  const dialog = document.getElementById("nwrorg_submission_dialog");
  if (dialog) dialog.hidden = true;
}

function maybeShowNwrOrgSubmissionNotice(icecast) {
  if (!icecast || icecast.service !== STREAM_SERVICE_NWRORG) return;
  const dialog = document.getElementById("nwrorg_submission_dialog");
  if (!dialog) return;
  dialog.hidden = false;
  const link = document.getElementById("nwrorg_submission_link");
  if (link) link.focus();
}

function resetWizardForService(service) {
  const station = selectedStation();
  const current = streamPayload().icecast;
  const next = applyServicePresetToIcecast({
    service,
    format: selectedWizardFormat(),
    sample_rate: DEFAULT_STREAM_SAMPLE_RATE,
    bitrate: service === STREAM_SERVICE_WEATHERUSA ? 48 : DEFAULT_STREAM_BITRATES.mp3,
    mount: service === STREAM_SERVICE_WEATHERUSA ? weatherUsaMount(station, selectedWizardFormat()) : ""
  }, service, station, "icecast");
  if (service === STREAM_SERVICE_CUSTOM) {
    next.host = "";
    next.port = "";
    next.username = "";
    next.password = "";
    next.mount = "";
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    next.password = current.password || "";
  }
  setIcecastForm(next, true);
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  renderWizard();
}

function resetSettingsForService(service) {
  const stream = currentSettingsStream();
  const station = stream ? stream.station : null;
  const current = settingsIcecastPayload();
  const next = applyServicePresetToIcecast({
    service,
    format: selectedSettingsFormat(),
    sample_rate: DEFAULT_STREAM_SAMPLE_RATE,
    bitrate: service === STREAM_SERVICE_WEATHERUSA ? 48 : DEFAULT_STREAM_BITRATES.mp3,
    mount: service === STREAM_SERVICE_WEATHERUSA ? weatherUsaMount(station, selectedSettingsFormat()) : ""
  }, service, station, "settings_icecast");
  if (service === STREAM_SERVICE_CUSTOM) {
    next.host = "";
    next.port = "";
    next.username = "";
    next.password = "";
    next.mount = "";
  } else if (service === STREAM_SERVICE_WEATHERUSA) {
    next.password = current.password || "";
  }
  setSettingsIcecastForm(next);
  updateOutputFormButtons();
}

function setFallbackResult(message, kind = "") {
  const element = document.getElementById("fallback-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasResult(message, kind = "") {
  const element = document.getElementById("eas-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setAudioEffectsResult(message, kind = "") {
  const element = document.getElementById("audio-effects-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function fallbackPayload() {
  return {
    enabled: document.getElementById("fallback_enabled").checked,
    silence_timeout_seconds: Number(document.getElementById("fallback_delay").value),
    loop_delay_seconds: Number(document.getElementById("fallback_loop_delay").value)
  };
}

function filterPayload(name) {
  const frequencyElement = document.getElementById(`audio_${name}_frequency`);
  const frequencyValue = frequencyElement.dataset.userEditing === "1"
    ? Number(frequencyElement.dataset.previousValue || frequencyElement.defaultValue || frequencyElement.value)
    : Number(frequencyElement.value);
  return {
    enabled: document.getElementById(`audio_${name}_enabled`).checked,
    frequency: frequencyValue,
    sharpness: Number(document.getElementById(`audio_${name}_sharpness`).value)
  };
}

function clampNumber(value, minimum, maximum) {
  const number = Number(value);
  if (!Number.isFinite(number)) return minimum;
  return Math.min(maximum, Math.max(minimum, number));
}

function previousNumberValue(element) {
  const previous = Number(element.dataset.previousValue || element.defaultValue || element.min || 0);
  return Number.isFinite(previous) ? previous : 0;
}

function normalizeAudioFrequencyControl(name) {
  const element = document.getElementById(`audio_${name}_frequency`);
  if (!element || element.value === "") return;
  const previous = previousNumberValue(element);
  let value = Number(element.value);
  if (!Number.isFinite(value)) return;
  if (name === "highpass") {
    value = clampNumber(value, 1, 900);
  } else if (name === "lowpass") {
    value = clampNumber(value, 2200, 12000);
  } else if (name === "notch") {
    value = clampNumber(value, 1, 12000);
    value = skipProtectedAudioBands(value, previous);
  }
  const normalized = String(Math.round(value));
  if (element.value !== normalized) element.value = normalized;
  element.dataset.previousValue = normalized;
}

function skipProtectedAudioBands(value, previous) {
  for (const band of PROTECTED_AUDIO_BANDS) {
    if (value < band.min || value > band.max) continue;
    if (previous < band.min) return band.max + 1;
    if (previous > band.max) return band.min - 1;
    const downDistance = Math.abs(value - (band.min - 1));
    const upDistance = Math.abs((band.max + 1) - value);
    return upDistance <= downDistance ? band.max + 1 : band.min - 1;
  }
  return value;
}

function normalizeAudioFrequencyInput(event) {
  const target = event.target;
  if (!target || !target.id) return false;
  const match = target.id.match(/^audio_(highpass|lowpass|notch)_frequency$/);
  if (!match) return false;
  normalizeAudioFrequencyControl(match[1]);
  return true;
}

function audioFrequencyNameForElement(element) {
  if (!element || !element.id) return "";
  const match = element.id.match(/^audio_(highpass|lowpass|notch)_frequency$/);
  return match ? match[1] : "";
}

function isTextEditingInputEvent(event) {
  return event && typeof event.inputType === "string" && (
    event.inputType.startsWith("insert") ||
    event.inputType.startsWith("delete")
  );
}

function isTextEditingKey(event) {
  if (!event || event.ctrlKey || event.altKey || event.metaKey) return false;
  return event.key.length === 1 || ["Backspace", "Delete"].includes(event.key);
}

function commitAudioFrequencyElement(element, save = true) {
  const name = audioFrequencyNameForElement(element);
  if (!name) return false;
  normalizeAudioFrequencyControl(name);
  delete element.dataset.userEditing;
  if (save) scheduleAudioEffectsUpdate();
  return true;
}

function audioEffectsPayload() {
  return {
    volume: {
      enabled: document.getElementById("audio_volume_enabled").checked,
      multiplier: Number(document.getElementById("audio_volume_multiplier").value)
    },
    comfort_noise: {
      enabled: document.getElementById("audio_comfort_noise_enabled").checked,
      level_db: Number(document.getElementById("audio_comfort_noise_level").value)
    },
    deemphasis: {
      enabled: document.getElementById("audio_deemphasis_enabled").checked,
      tau: Number(document.getElementById("audio_deemphasis_tau").value)
    },
    highpass: filterPayload("highpass"),
    lowpass: filterPayload("lowpass"),
    notch: filterPayload("notch")
  };
}

function easPayload() {
  const selectedFormat = document.querySelector("input[name='eas_format']:checked");
  return {
    enabled: document.getElementById("eas_enabled").checked,
    pre_seconds: Number(document.getElementById("eas_pre_seconds").value),
    post_seconds: Number(document.getElementById("eas_post_seconds").value),
    max_seconds: Number(document.getElementById("eas_max_seconds").value),
    format: selectedFormat ? selectedFormat.value : "wav"
  };
}

function defaultEasSettings() {
  return {enabled: false, pre_seconds: 2, post_seconds: 5, max_seconds: 120, format: "wav"};
}

function defaultAudioEffectsSettings() {
  return {
    volume: {enabled: false, multiplier: 1.0},
    comfort_noise: {enabled: false, level_db: -40.0},
    deemphasis: {enabled: true, tau: 300.0},
    highpass: {enabled: false, frequency: 300.0, sharpness: 0.0},
    lowpass: {enabled: true, frequency: 3400.0, sharpness: 2.0},
    notch: {enabled: false, frequency: 3000.0, sharpness: 0.0}
  };
}

function audioEffectsSettingsForStream(stream) {
  const defaults = defaultAudioEffectsSettings();
  const audio = stream && stream.audio ? stream.audio : {};
  return {
    volume: Object.assign(defaults.volume, audio.volume || {}),
    comfort_noise: Object.assign(defaults.comfort_noise, audio.comfort_noise || {}),
    deemphasis: Object.assign(defaults.deemphasis, audio.deemphasis || {}),
    highpass: normalizeAudioFilterSettings(defaults.highpass, audio.highpass),
    lowpass: normalizeAudioFilterSettings(defaults.lowpass, audio.lowpass),
    notch: normalizeAudioFilterSettings(defaults.notch, audio.notch)
  };
}

function normalizeAudioFilterSettings(defaults, raw) {
  const settings = Object.assign({}, defaults, raw || {});
  if (!Number(settings.frequency)) settings.frequency = defaults.frequency;
  return settings;
}

function easSettingsForStream(stream) {
  return Object.assign(defaultEasSettings(), stream && stream.eas_recording ? stream.eas_recording : {});
}

function setAudioEffectsControls(stream) {
  const settings = audioEffectsSettingsForStream(stream);
  const nextSignature = JSON.stringify(settings);
  if (nextSignature === audioEffectsSignature) return;
  setChecked("audio_volume_enabled", settings.volume.enabled);
  setValue("audio_volume_multiplier", settings.volume.multiplier);
  setChecked("audio_comfort_noise_enabled", settings.comfort_noise.enabled);
  setValue("audio_comfort_noise_level", settings.comfort_noise.level_db);
  setChecked("audio_deemphasis_enabled", settings.deemphasis.enabled);
  setValue("audio_deemphasis_tau", settings.deemphasis.tau);
  setAudioFilterControls("highpass", settings.highpass);
  setAudioFilterControls("lowpass", settings.lowpass);
  setAudioFilterControls("notch", settings.notch);
  audioEffectsSignature = nextSignature;
}

function setAudioFilterControls(name, settings) {
  setChecked(`audio_${name}_enabled`, settings.enabled);
  setValue(`audio_${name}_frequency`, settings.frequency);
  setValue(`audio_${name}_sharpness`, settings.sharpness);
  const frequency = document.getElementById(`audio_${name}_frequency`);
  frequency.dataset.previousValue = String(settings.frequency);
}

function selectAudioEffect(name, showDetail = true) {
  selectedAudioEffect = name;
  for (const button of document.querySelectorAll("[data-audio-effect]")) {
    const selected = button.dataset.audioEffect === name;
    button.setAttribute("aria-current", selected ? "true" : "false");
  }
  for (const panel of document.querySelectorAll(".audio-effect-panel")) {
    panel.hidden = panel.id !== `audio_effect_${name}`;
  }
  document.getElementById("audio_effects_layout").classList.toggle("effect-detail-active", showDetail);
}

function setEasControls(stream) {
  const settings = easSettingsForStream(stream);
  const station = stream && stream.station ? stream.station : {};
  const directory = station.callsign ? `~/.local/state/nwr-stream-manager/streams/${station.callsign}/alerts` : "";
  const showEnabledControl = !settings.enabled || canDisableEasRecording(stream);
  const nextSignature = JSON.stringify({settings, directory, showEnabledControl});
  if (nextSignature === easSignature) return;
  setChecked("eas_enabled", settings.enabled);
  document.getElementById("eas_enabled_label").hidden = !showEnabledControl;
  setValue("eas_pre_seconds", settings.pre_seconds);
  setValue("eas_post_seconds", settings.post_seconds);
  setValue("eas_max_seconds", settings.max_seconds);
  setChecked("eas_format_wav", settings.format !== "mp3");
  setChecked("eas_format_mp3", settings.format === "mp3");
  setText("eas_directory_hint", directory ? `Alerts and index.json will be stored in ${directory}.` : "");
  easSignature = nextSignature;
}

function setFallbackControls(fallback) {
  if (!fallback) return;
  const nextSignature = JSON.stringify(fallback);
  if (nextSignature === fallbackSignature) return;
  setChecked("fallback_enabled", fallback.enabled);
  setValue("fallback_delay", fallback.silence_timeout_seconds);
  setValue("fallback_loop_delay", fallback.loop_delay_seconds);
  fallbackSignature = nextSignature;
}

function activeSettingsTab() {
  if (document.getElementById("tab_fallback").getAttribute("aria-selected") === "true") return "fallback";
  if (document.getElementById("tab_eas").getAttribute("aria-selected") === "true") return "eas";
  if (document.getElementById("tab_audio").getAttribute("aria-selected") === "true") return "audio";
  return "outputs";
}

function showSettingsTab(name) {
  for (const tab of [
    {name: "outputs", button: "tab_outputs", panel: "panel_outputs"},
    {name: "eas", button: "tab_eas", panel: "panel_eas"},
    {name: "fallback", button: "tab_fallback", panel: "panel_fallback"},
    {name: "audio", button: "tab_audio", panel: "panel_audio"}
  ]) {
    const selected = tab.name === name;
    document.getElementById(tab.button).setAttribute("aria-selected", selected ? "true" : "false");
    document.getElementById(tab.button).setAttribute("tabindex", selected ? "0" : "-1");
    document.getElementById(tab.panel).hidden = !selected;
  }
}

function updateOutputFormButtons() {
  const changed = outputFormSignature() !== outputFormOriginalSignature;
  outputFormDirty = !document.getElementById("output_form_panel").hidden && changed;
  setDisabled(document.getElementById("add_output"), !settingsCredentialsComplete());
  document.getElementById("add_output").hidden = outputFormMode !== "add";
  document.getElementById("save_output_settings").hidden = outputFormMode !== "edit" || !changed;
  setDisabled(document.getElementById("save_output_settings"), !settingsCredentialsComplete());
}

function outputFormIsOpen() {
  return !document.getElementById("output_form_panel").hidden;
}

function currentSettingsStream() {
  return configuredStreams.find(stream => stream.id === settingsStreamId) || null;
}

function beginAddOutput() {
  outputFormMode = "add";
  editingOutputId = "";
  clearSettingsIcecastForm();
  setText("output_form_title", "Add output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
}

function beginEditOutput(outputId) {
  const selected = findConfiguredOutput(settingsStreamId, outputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return;
  }
  outputFormMode = "edit";
  editingOutputId = outputId;
  setSettingsIcecastForm(selected.output.icecast || {});
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
  setText("output_form_title", "Edit output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
}

function cancelOutputForm() {
  document.getElementById("output_form_panel").hidden = true;
  outputFormDirty = false;
  outputFormOriginalSignature = "";
  editingOutputId = "";
  setOutputResult("");
}

function clearIcecastForm() {
  setIcecastForm({service: STREAM_SERVICE_CUSTOM, format: "mp3", sample_rate: 24000, bitrate: 64}, true);
  setChecked("show_icecast_password", false);
  document.getElementById("icecast_password").type = "password";
}

function icecastCredentialSignature() {
  const payload = streamPayload().icecast;
  return JSON.stringify({
    host: payload.host,
    port: payload.port,
    username: payload.username,
    password: payload.password,
    mount: payload.mount
  });
}

function credentialsComplete() {
  const payload = streamPayload().icecast;
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (service === STREAM_SERVICE_CUSTOM && customServiceHostWarning(payload.host)) return false;
  return Boolean(
    payload.host.trim() &&
    payload.port >= 1 &&
    payload.port <= 65535 &&
    payload.username.trim() &&
    payload.password &&
    payload.mount.trim()
  );
}

function setWizardStep(step) {
  wizardStep = Math.max(0, Math.min(4, step));
  renderWizard();
}

function setWizardPanel(id, visible) {
  document.getElementById(id).hidden = !visible;
}

function renderWizard() {
  const editMode = wizardMode === "edit";
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  const needsCodecStep = service === STREAM_SERVICE_CUSTOM || service === STREAM_SERVICE_WEATHERUSA;
  const needsQualityStep = service === STREAM_SERVICE_CUSTOM || service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_WEATHERUSA;
  if (service === STREAM_SERVICE_GWES) {
    setChecked("icecast_format_mp3", true);
    setChecked("icecast_format_ogg", false);
  }
  const hostWarning = service === STREAM_SERVICE_CUSTOM ? customServiceHostWarning(document.getElementById("icecast_host").value) : "";
  if (hostWarning) setStreamResult(hostWarning, "error");
  else if (document.getElementById("stream-result").textContent.startsWith("Please select ")) setStreamResult("");
  setServiceHelp("icecast_service_help", service);
  applyServiceControls("icecast", service);
  setText("stream_wizard_title", editMode ? "Edit Stream Output" : "Add Stream");
  setWizardPanel("wizard_step_station", !editMode && wizardStep === 0);
  setWizardPanel("wizard_step_service", !editMode && wizardStep === 1);
  setWizardPanel("wizard_step_codec", !editMode && wizardStep === 2 && needsCodecStep);
  setWizardPanel("wizard_step_credentials", editMode || wizardStep === 3 || (!needsCodecStep && wizardStep === 2));
  setWizardPanel("wizard_step_quality", !editMode && wizardStep === 4 && needsQualityStep);
  document.getElementById("cancel_wizard").hidden = editMode;
  document.getElementById("wizard_back").hidden = editMode || wizardStep === 0;
  document.getElementById("wizard_next").hidden = editMode || (wizardStep === 4) || (!needsQualityStep && wizardStep >= 3);
  document.getElementById("wizard_finish").hidden = editMode || !(wizardStep === 4 || (!needsQualityStep && wizardStep >= 3));
  document.getElementById("save_output").hidden = !editMode;
  document.getElementById("cancel_output_edit").hidden = !editMode;
  const next = document.getElementById("wizard_next");
  const finish = document.getElementById("wizard_finish");
  if (wizardStep === 0) {
    setDisabled(next, !selectedStation());
  } else if (wizardStep === 3 || (!needsCodecStep && wizardStep === 2)) {
    setDisabled(next, !credentialsComplete());
  } else {
    setDisabled(next, false);
  }
  if (!finish.hidden) {
    setDisabled(finish, wizardStep === 3 && !credentialsComplete());
  }
}

function beginStreamWizard() {
  wizardMode = "add";
  wizardStep = 0;
  wizardDirty = true;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  editingStreamId = "";
  editingOutputId = "";
  selectedStationKey = "";
  const stationResultsElement = document.getElementById("station_results");
  if (stationResultsElement) stationResultsElement.value = "";
  clearIcecastForm();
  setText("selected_station", "Select a station to continue.");
  setStreamResult("");
  renderWizard();
  navigateTo("add_stream", {}, false, true);
}

function finishWizard() {
  wizardDirty = false;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  setStreamResult("");
  navigateTo("streams");
}

function setOutputEditMode(enabled, selected = null) {
  wizardMode = enabled ? "edit" : "add";
  editingStreamId = enabled && selected ? selected.stream.id : "";
  editingOutputId = enabled && selected ? selected.output.id : "";
  const enabledLabel = document.getElementById("output_enabled_label");
  enabledLabel.hidden = !enabled || !selected || selected.outputs.length <= 1;
  if (!enabled) {
    setChecked("output_enabled", true);
  }
  renderWizard();
}

function editOutput(streamId, outputId) {
  const selected = findConfiguredOutput(streamId, outputId);
  if (!selected) {
    setStreamResult("Stream output was not found.", "error");
    return;
  }
  selectedStationKey = selected.stream.station.key || "";
  setIcecastForm(selected.output.icecast || {}, selected.output.enabled !== false);
  setOutputEditMode(true, selected);
  setText("selected_station", `Editing ${selected.stream.station.callsign} ${selected.stream.station.frequency} MHz`);
  wizardDirty = true;
  setStreamResult("");
  navigateTo("add_stream", {}, false, true);
}

function editStreamSettings(streamId, outputId = "") {
  showStreamSettings(streamId);
  if (outputId) beginEditOutput(outputId);
}

function showStreamSettings(streamId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) {
    setStreamResult("Stream was not found.", "error");
    return;
  }
  settingsStreamId = streamId;
  outputTableSignature = "";
  easSignature = "";
  audioEffectsSignature = "";
  selectAudioEffect(selectedAudioEffect, false);
  cancelOutputForm();
  const station = stream.station || {};
  setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
  renderStreamSettings();
  navigateTo("stream_settings", {streamId});
}

function renderStreamSettings() {
  const stream = currentSettingsStream();
  const addButton = document.getElementById("open_add_output");
  setDisabled(addButton, !stream);
  setDisabled(document.getElementById("stream_enabled"), !stream);
  setDisabled(document.getElementById("stream_monitor_enabled"), !stream || stream.enabled === false);
  if (stream) setChecked("stream_enabled", stream.enabled !== false);
  setChecked("stream_monitor_enabled", Boolean(stream && monitorStreamId === stream.id));
  setAudioEffectsControls(stream);
  setEasControls(stream);
  renderIcecastOutputsTable(stream);
}

function outputStatusFor(stream, output) {
  if (!stream || stream.enabled === false) return "disabled";
  if (!output || output.enabled === false) return "disabled";
  const active = activeStreamSnapshots.find(snapshot =>
    snapshot.id === stream.id && streamOutputs(snapshot).some(activeOutput => activeOutput.id === output.id)
  );
  const status = normalizeStreamStatus(active ? active.status : "");
  if (status === "enabled") return "connected";
  return "needs-attention";
}

function outputStatusLabel(status) {
  if (status === "connected") return "Connected";
  if (status === "needs-attention") return "Needs attention";
  return "Disabled";
}

function outputDestination(icecast) {
  if (!icecast || !icecast.host) return "Unknown";
  return `${icecast.host}:${icecast.port}${icecast.mount}`;
}

function outputRows(stream) {
  if (!stream) return [];
  return streamOutputs(stream).map(output => ({
    stream,
    output,
    icecast: output.icecast || {},
    status: outputStatusFor(stream, output)
  }));
}

function outputTableNextSignature(rows) {
  return JSON.stringify(rows.map(row => ({
    id: row.output.id || "",
    enabled: row.output.enabled !== false,
    destination: outputDestination(row.icecast),
    format: row.icecast.format || "",
    sample_rate: row.icecast.sample_rate || "",
    bitrate: row.icecast.bitrate || "",
    status: row.status
  })));
}

function renderIcecastOutputsTable(stream) {
  const tbody = document.getElementById("icecast-outputs-body");
  const rows = outputRows(stream);
  const nextSignature = outputTableNextSignature(rows);
  if (nextSignature === outputTableSignature) return;
  outputTableSignature = nextSignature;
  tbody.innerHTML = "";
  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.className = "hint";
    cell.textContent = "No Icecast outputs configured.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  for (const row of rows) {
    tbody.appendChild(icecastOutputRow(row));
  }
}

function icecastOutputRow(row) {
  const tr = document.createElement("tr");
  const icecast = row.icecast;
  tr.appendChild(tableCell(outputDestination(icecast)));
  tr.appendChild(tableCell(String(icecast.format || "mp3").toUpperCase()));
  tr.appendChild(tableCell(`${icecast.sample_rate || DEFAULT_STREAM_SAMPLE_RATE} Hz`));
  tr.appendChild(tableCell(`${icecast.bitrate || DEFAULT_STREAM_BITRATES[icecast.format || "mp3"]} Kbps`));
  const statusCell = tableCell(outputStatusLabel(row.status));
  statusCell.className = `status-text status-${row.status}`;
  tr.appendChild(statusCell);
  tr.appendChild(outputActionsCell(row.stream, row.output, row.status));
  return tr;
}

function outputActionsCell(stream, output) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${outputDestination(output.icecast || {})}`);
  button.dataset.outputMenu = output.id || "";
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = output.enabled === false ? "Enable" : "Disable";
  toggle.setAttribute("role", "menuitem");
  toggle.dataset.action = "toggle-output";
  toggle.dataset.streamId = stream.id || "";
  toggle.dataset.outputId = output.id || "";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Edit output";
  edit.setAttribute("role", "menuitem");
  edit.dataset.action = "edit-settings-output";
  edit.dataset.streamId = stream.id || "";
  edit.dataset.outputId = output.id || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove";
  remove.setAttribute("role", "menuitem");
  remove.dataset.action = "remove-output";
  remove.dataset.streamId = stream.id || "";
  remove.dataset.outputId = output.id || "";
  if (!accountIsReadOnly()) {
    if (output.enabled === false || canDisableIcecastOutput(stream, output)) {
      menu.appendChild(toggle);
    }
    menu.appendChild(edit);
    if (canRemoveIcecastOutput(stream, output)) {
      menu.appendChild(remove);
    }
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

function activeStreamRows(activeStreams, configured = configuredStreams) {
  const rows = [];
  const activeById = new Map();
  for (const stream of activeStreams || []) {
    const items = activeById.get(stream.id) || [];
    items.push(stream);
    activeById.set(stream.id, items);
  }
  for (const stream of configured || []) {
    const activeItems = activeById.get(stream.id) || [];
    if (activeItems.length) {
      const statuses = activeItems.map(item => normalizeStreamStatus(item.status));
      const status = statuses.includes("needs-attention") ? "needs-attention" : statuses.includes("enabled") ? "enabled" : "disabled";
      rows.push({
        id: stream.id,
        enabled: stream.enabled !== false,
        station: stream.station,
        outputs: streamOutputs(stream),
        status
      });
      activeById.delete(stream.id);
    } else {
      rows.push({
        id: stream.id,
        enabled: stream.enabled !== false,
        station: stream.station,
        outputs: streamOutputs(stream),
        status: stream.enabled === false ? "disabled" : "disabled"
      });
    }
  }
  for (const streams of activeById.values()) {
    for (const stream of streams) rows.push(stream);
  }
  return rows;
}

function activeStreamSignature(rows) {
  return JSON.stringify(rows.map(stream => {
    const station = stream.station || {};
    return {
      id: stream.id || "",
      enabled: stream.enabled !== false,
      callsign: station.callsign || "",
      frequency: station.frequency || "",
      outputs: streamOutputs(stream).map(output => ({
        id: output.id || "",
        enabled: output.enabled !== false,
        type: output.type || "icecast"
      })),
      monitoring: monitorStreamId === stream.id,
      status: normalizeStreamStatus(stream.status),
      read_only: accountIsReadOnly()
    };
  }));
}

function renderActiveStreams(activeStreams, configured = configuredStreams) {
  const tbody = document.getElementById("active-streams-body");
  const rows = activeStreamRows(activeStreams, configured);
  const nextSignature = activeStreamSignature(rows);
  if (nextSignature === activeStreamsSignature) {
    return;
  }
  activeStreamsSignature = nextSignature;
  tbody.innerHTML = "";

  if (rows.length === 0) {
    const row = document.createElement("tr");
    row.id = "active-streams-empty";
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "hint";
    cell.textContent = "No active streams.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  for (const stream of rows) {
    tbody.appendChild(activeStreamRow(stream));
  }
}

function dashboardAttentionSignatureFor(items) {
  return JSON.stringify(items.map(item => ({
    id: item.id || "",
    callsign: item.callsign || "",
    status: item.status || "",
    error: item.error || ""
  })));
}

function streamAttentionItems(activeStreams, configured = configuredStreams) {
  const configuredById = new Map((configured || []).map(stream => [stream.id, stream]));
  const items = [];
  for (const active of activeStreams || []) {
    if (normalizeStreamStatus(active.status) !== "needs-attention") continue;
    const configuredStream = configuredById.get(active.id) || {};
    const station = active.station || configuredStream.station || {};
    const outputs = active.outputs || [];
    const output = outputs.length ? outputs[0] : {};
    const icecast = output.icecast || {};
    const destination = icecast.host ? outputDestination(icecast) : "";
    items.push({
      id: active.id || configuredStream.id || "",
      callsign: station.callsign || "Unknown",
      frequency: station.frequency || "",
      status: active.status || "",
      destination,
      error: active.error || ""
    });
  }
  items.sort((a, b) => String(a.callsign).localeCompare(String(b.callsign)) || String(a.destination).localeCompare(String(b.destination)));
  return items;
}

function renderDashboardStreamAttention(activeStreams, configured = configuredStreams) {
  const container = document.getElementById("dashboard-stream-attention");
  const items = streamAttentionItems(activeStreams, configured);
  const nextSignature = dashboardAttentionSignatureFor(items);
  if (nextSignature === dashboardAttentionSignature) return;
  dashboardAttentionSignature = nextSignature;
  container.innerHTML = "";
  if (!items.length) {
    const healthy = document.createElement("div");
    healthy.className = "stream-item success";
    healthy.textContent = "All streams are healthy!";
    container.appendChild(healthy);
    return;
  }
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "stream-item";
    const link = document.createElement("a");
    link.href = routeForView("stream_settings", {streamId: item.id});
    link.dataset.view = "stream_settings";
    link.dataset.streamId = item.id;
    link.textContent = item.callsign;
    const detail = item.error || (item.destination ? `${item.destination} needs attention.` : "This stream needs attention.");
    row.appendChild(link);
    row.append(`: ${detail}`);
    container.appendChild(row);
  }
}

function sentenceCaseAlertName(name) {
  const text = String(name || "Unknown alert").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function relativeTimeAgo(epochSeconds) {
  const ageSeconds = Math.max(0, Math.floor((Date.now() / 1000) - Number(epochSeconds || 0)));
  const minutes = Math.max(0, Math.floor(ageSeconds / 60));
  if (minutes < 1) return "less than a minute ago";
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
  const hours = Math.floor(minutes / 60);
  return `${hours} hour${hours === 1 ? "" : "s"} ago`;
}

function recentAlertsSignature(alerts) {
  return JSON.stringify({
    now_minute: Math.floor(Date.now() / 60000),
    alerts: (alerts || []).map(alert => [
      alert.id || "",
      alert.stream_id || "",
      alert.callsign || "",
      alert.event_name || "",
      Math.floor(Number(alert.issued_at_epoch || 0) / 60)
    ])
  });
}

function renderDashboardRecentAlerts(alerts) {
  const container = document.getElementById("dashboard-recent-alerts");
  const nextSignature = recentAlertsSignature(alerts);
  if (nextSignature === dashboardRecentAlertsSignature) return;
  dashboardRecentAlertsSignature = nextSignature;
  container.innerHTML = "";
  if (!alerts || alerts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "stream-item hint";
    empty.textContent = "No EAS alerts issued in the last 24 hours.";
    container.appendChild(empty);
    return;
  }
  for (const alert of alerts) {
    const item = document.createElement("div");
    item.className = "stream-item";
    const alertLink = document.createElement("a");
    alertLink.href = routeForView("eas_alert_detail", {
      streamId: alert.stream_id || "",
      alertId: alert.id || "",
      page: 1
    });
    alertLink.dataset.view = "eas_alert_detail";
    alertLink.dataset.streamId = alert.stream_id || "";
    alertLink.dataset.alertId = alert.id || "";
    alertLink.dataset.page = "1";
    alertLink.textContent = sentenceCaseAlertName(alert.event_name);
    const allLink = document.createElement("a");
    allLink.href = routeForView("eas_alerts", {streamId: alert.stream_id || "", page: 1});
    allLink.dataset.view = "eas_alerts";
    allLink.dataset.streamId = alert.stream_id || "";
    allLink.dataset.page = "1";
    allLink.textContent = `View all EAS alerts for ${alert.callsign || "this stream"}`;
    item.appendChild(alertLink);
    item.append(` issued ${relativeTimeAgo(alert.issued_at_epoch)} on ${alert.callsign || "Unknown"}. `);
    item.appendChild(allLink);
    container.appendChild(item);
  }
}

function activeStreamRow(stream) {
  const station = stream.station || {};
  const status = normalizeStreamStatus(stream.status);
  const row = document.createElement("tr");
  row.appendChild(tableCell(station.callsign || "Unknown"));
  row.appendChild(tableCell(station.frequency ? `${station.frequency} MHz` : "Unknown"));
  row.appendChild(tableCell(streamOutputCount(stream)));
  const statusCell = tableCell(streamStatusLabel(status));
  statusCell.className = `status-text status-${status}`;
  row.appendChild(statusCell);
  row.appendChild(streamActionsCell(stream));
  return row;
}

function tableCell(value) {
  const cell = document.createElement("td");
  cell.textContent = String(value);
  return cell;
}

function streamActionsCell(stream) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const output = stream.outputs && stream.outputs.length ? stream.outputs[0] : {};
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${stationLabelForActionMenu(stream)}`);
  button.dataset.activeStreamMenu = stream.id || "";
  button.dataset.outputId = output.id || "";
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = stream.enabled === false ? "Start stream" : "Stop stream";
  toggle.setAttribute("role", "menuitem");
  toggle.dataset.action = "toggle-active-stream";
  toggle.dataset.streamId = stream.id || "";
  const monitor = document.createElement("button");
  monitor.type = "button";
  monitor.textContent = monitorStreamId === stream.id ? "Stop monitoring" : "Monitor";
  monitor.setAttribute("role", "menuitem");
  monitor.dataset.action = "toggle-monitor";
  monitor.dataset.streamId = stream.id || "";
  const edit = document.createElement("button");
  edit.type = "button";
  edit.textContent = "Edit stream settings";
  edit.setAttribute("role", "menuitem");
  edit.dataset.action = "edit-active-stream";
  edit.dataset.streamId = stream.id || "";
  edit.dataset.outputId = output.id || "";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove stream";
  remove.setAttribute("role", "menuitem");
  remove.dataset.action = "remove-active-stream";
  remove.dataset.streamId = stream.id || "";
  remove.dataset.outputId = output.id || "";
  if (!accountIsReadOnly()) {
    menu.appendChild(toggle);
  }
  menu.appendChild(monitor);
  if (!accountIsReadOnly()) {
    menu.appendChild(edit);
    menu.appendChild(remove);
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

function stationLabelForActionMenu(stream) {
  const station = stream.station || {};
  if (station.callsign && station.frequency) return `${station.callsign} ${station.frequency} MHz`;
  return station.callsign || "stream";
}

function menuItems(menu) {
  return Array.from(menu.querySelectorAll("[role='menuitem']"));
}

function openStreamActionMenu(button, focus = "first") {
  const menu = button.nextElementSibling;
  if (!menu) return;
  closeStreamActionMenus();
  menu.hidden = false;
  button.setAttribute("aria-expanded", "true");
  if (focus) {
    const items = menuItems(menu);
    const target = focus === "last" ? items[items.length - 1] : items[0];
    if (target) target.focus();
  }
}

function closeStreamActionMenu(menu, restoreFocus = true) {
  menu.hidden = true;
  const button = menu.parentElement.querySelector("button[aria-haspopup='menu']");
  if (button) {
    button.setAttribute("aria-expanded", "false");
    if (restoreFocus) button.focus();
  }
}

function closeStreamActionMenus() {
  for (const menu of document.querySelectorAll(".stream-actions-menu")) {
    closeStreamActionMenu(menu, false);
  }
}

function renderStreams(streams) {
  configuredStreams = streams || [];
  renderActiveStreams(activeStreamSnapshots, configuredStreams);
  if (settingsStreamId) renderStreamSettings();
  const list = document.getElementById("streams-list");
  list.innerHTML = "";
  if (configuredStreams.length === 0) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "No streams configured.";
    list.appendChild(empty);
    return;
  }
  for (const stream of configuredStreams) {
    const item = document.createElement("div");
    item.className = "stream-item";
    const title = document.createElement("b");
    title.textContent = streamLabel(stream);
    const details = document.createElement("div");
    details.className = "hint";
    details.textContent = `${streamOutputCount(stream)} output${streamOutputCount(stream) === 1 ? "" : "s"}.`;
    const outputs = document.createElement("div");
    outputs.className = "actions";
    if (!accountIsReadOnly()) {
      for (const output of streamOutputs(stream)) {
        const icecast = output.icecast || {};
        const edit = document.createElement("button");
        edit.type = "button";
        edit.textContent = `Edit ${icecast.mount || "Icecast output"}`;
        edit.dataset.action = "edit-output";
        edit.dataset.streamId = stream.id;
        edit.dataset.outputId = output.id;
        outputs.appendChild(edit);
      }
    }
    item.appendChild(title);
    item.appendChild(details);
    if (!accountIsReadOnly()) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.dataset.action = "remove-stream";
      remove.dataset.streamId = stream.id;
      item.appendChild(outputs);
      item.appendChild(remove);
    }
    list.appendChild(item);
  }
}

async function loadStreams() {
  const data = await request("/api/streams");
  renderStreams(data.streams || []);
  await loadEasAlertStreams({preserve: true, quiet: true});
}

function gainIndexFor(value) {
  if (value === null || gainValues.length === 0) return 0;
  return gainValues.reduce((best, gain, index) =>
    Math.abs(gain - value) < Math.abs(gainValues[best] - value) ? index : best, 0);
}

function controlSignature(data) {
  const s = data.settings;
  return JSON.stringify({
    serial: s.serial || "",
    gain: s.gain,
    ppm_correction: s.ppm_correction,
    bias_tee: s.bias_tee,
    gain_values: data.gain_values || []
  });
}

function controlHasFocus() {
  const active = document.activeElement;
  return active && controls.includes(active.id);
}

function setValue(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.value !== text) element.value = text;
}

function setChecked(id, value) {
  const element = document.getElementById(id);
  const checked = Boolean(value);
  if (element.checked !== checked) element.checked = checked;
}

function setDisabled(element, value) {
  const disabled = Boolean(value);
  if (element.disabled !== disabled) element.disabled = disabled;
}

function setAttributeIfChanged(element, name, value) {
  const text = String(value);
  if (element.getAttribute(name) !== text) element.setAttribute(name, text);
}

function setText(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.textContent !== text) element.textContent = text;
}

function accountIsOwner() {
  return currentAccount && currentAccount.role === "owner";
}

function accountIsReadOnly() {
  return currentAccount && currentAccount.read_only;
}

function formatAccountDate(epochSeconds) {
  const seconds = Number(epochSeconds || 0);
  if (!seconds) return "Never";
  return new Intl.DateTimeFormat(undefined, {month: "long", day: "numeric", year: "numeric"}).format(new Date(seconds * 1000));
}

function setAccountResult(message, kind = "") {
  const element = document.getElementById("account-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setChangePasswordResult(message, kind = "") {
  const element = document.getElementById("change-password-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function applyAccountUi(account) {
  const previousRole = currentAccount ? currentAccount.role : "";
  const previousReadOnly = currentAccount ? Boolean(currentAccount.read_only) : false;
  currentAccount = account || currentAccount;
  const owner = accountIsOwner();
  const readOnly = accountIsReadOnly();
  setHidden("nav_accounts", !owner);
  setHidden("nav_change_password", owner);
  setHidden("nav_rtl", readOnly);
  setHidden("open_add_stream", readOnly);
  setHidden("open_iq_start", readOnly);
  setHidden("iq_stop_recording", readOnly);
  setHidden("open_eas_delete", readOnly);
  setHidden("eas_delete_alerts", readOnly);
  setHidden("remove_eas_alert", readOnly);
  if (previousRole !== (currentAccount ? currentAccount.role : "") || previousReadOnly !== readOnly) {
    activeStreamsSignature = "";
    outputTableSignature = "";
    iqRecordingsSignature = "";
    accountsSignature = "";
    renderActiveStreams(activeStreamSnapshots, configuredStreams);
    renderStreams(configuredStreams);
    renderIqRecordings(iqRecordings);
  }
  if (readOnly && isReadOnlyRestrictedView(currentViewName())) {
    navigateTo("dashboard", {}, true, true);
  }
  if (!owner && currentViewName() === "accounts") {
    navigateTo("change_password", {}, true, true);
  }
}

function isReadOnlyRestrictedView(view) {
  return ["rtl", "add_stream", "stream_settings", "iq_recorder_start", "eas_alert_delete", "accounts"].includes(view);
}

function accountsTableSignature(accounts) {
  return JSON.stringify((accounts || []).map(account => ({
    id: account.id,
    username: account.username,
    role: account.role,
    last_accessed_at: account.last_accessed_at,
    must_change_password: account.must_change_password
  })));
}

function renderAccounts(accounts) {
  const tbody = document.getElementById("accounts-body");
  if (!tbody) return;
  const signature = accountsTableSignature(accounts);
  if (signature === accountsSignature) return;
  accountsSignature = signature;
  tbody.innerHTML = "";
  if (!accounts || accounts.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "hint";
    cell.textContent = "No accounts.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  for (const account of accounts) {
    const row = document.createElement("tr");
    row.appendChild(tableCell(account.username || ""));
    row.appendChild(tableCell(account.account_type || "Administrator"));
    row.appendChild(tableCell(formatAccountDate(account.last_accessed_at)));
    row.appendChild(accountActionsCell(account));
    tbody.appendChild(row);
  }
}

function accountActionsCell(account) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${account.username || "account"}`);
  button.dataset.accountMenu = String(account.id || "");
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const reset = document.createElement("button");
  reset.type = "button";
  reset.textContent = currentAccount && currentAccount.id === account.id ? "Change account password" : "Reset password";
  reset.setAttribute("role", "menuitem");
  reset.dataset.action = currentAccount && currentAccount.id === account.id ? "change-own-password" : "reset-account-password";
  reset.dataset.accountId = account.id || "";
  menu.appendChild(reset);
  if (!account.owner) {
    const readOnly = document.createElement("button");
    readOnly.type = "button";
    readOnly.textContent = account.read_only ? "Read-only account checked" : "Read-only account unchecked";
    readOnly.setAttribute("role", "menuitemcheckbox");
    readOnly.setAttribute("aria-checked", account.read_only ? "true" : "false");
    readOnly.dataset.action = "toggle-account-read-only";
    readOnly.dataset.accountId = account.id || "";
    readOnly.dataset.readOnly = account.read_only ? "0" : "1";
    menu.appendChild(readOnly);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Delete account";
    remove.setAttribute("role", "menuitem");
    remove.dataset.action = "delete-account";
    remove.dataset.accountId = account.id || "";
    menu.appendChild(remove);
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

async function loadAccounts() {
  const data = await request("/api/accounts");
  renderAccounts(data.accounts || []);
  return data;
}

function showAccountSecret(secret) {
  setValue("account_secret", secret || "");
  const dialog = document.getElementById("account_secret_dialog");
  if (dialog) dialog.hidden = !secret;
  const input = document.getElementById("account_secret");
  if (input && secret) input.focus();
}

function dismissAccountSecret() {
  const dialog = document.getElementById("account_secret_dialog");
  if (dialog) dialog.hidden = true;
  setValue("account_secret", "");
}

async function copyAccountSecret() {
  const input = document.getElementById("account_secret");
  const secret = input ? input.value : "";
  if (!secret) return;
  try {
    await navigator.clipboard.writeText(secret);
    setAccountResult("Temporary password copied to clipboard.", "success");
  } catch (error) {
    if (input) {
      input.focus();
      input.select();
    }
    setAccountResult("Copy failed. Select the temporary password and copy it manually.", "error");
  }
}

async function createAccountFromForm() {
  const data = await request("/api/accounts", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      username: document.getElementById("new_account_username").value,
      read_only: document.getElementById("new_account_read_only").checked
    })
  });
  document.getElementById("create_account_panel").hidden = true;
  setValue("new_account_username", "");
  setChecked("new_account_read_only", false);
  showAccountSecret(data.secret || "");
  await loadAccounts();
  setAccountResult("Account created.", "success");
}

async function resetAccountPassword(accountId) {
  const data = await request("/api/account-reset-password", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({account_id: Number(accountId)})
  });
  showAccountSecret(data.secret || "");
  await loadAccounts();
  setAccountResult("Password reset. The temporary password is shown below.", "success");
}

async function setAccountReadOnly(accountId, readOnly) {
  await request("/api/account-read-only", {
    method: "PATCH",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({account_id: Number(accountId), read_only: Boolean(readOnly)})
  });
  await loadAccounts();
  setAccountResult("Account updated.", "success");
}

async function deleteAccount(accountId) {
  await request(`/api/accounts?account_id=${encodeURIComponent(accountId)}`, {method: "DELETE"});
  await loadAccounts();
  setAccountResult("Account deleted.", "success");
}

async function changeOwnPasswordFromForm() {
  const newPassword = document.getElementById("account_new_password").value;
  const confirmPassword = document.getElementById("account_confirm_password").value;
  if (newPassword !== confirmPassword) {
    throw new Error("Passwords do not match.");
  }
  const data = await request("/api/account/password", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      current_password: document.getElementById("account_current_password").value,
      new_password: newPassword,
      confirm_password: confirmPassword
    })
  });
  setChangePasswordResult("Password changed. Log in again with the new password.", "success");
  window.location.replace("/");
  return data;
}

function setStreamResult(message, kind = "") {
  const element = document.getElementById("stream-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setIqRecorderResult(message, kind = "") {
  const element = document.getElementById("iq-recorder-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setIqStartResult(message, kind = "") {
  const element = document.getElementById("iq-start-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setIqDownloadResult(message, kind = "") {
  const element = document.getElementById("iq-download-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setStreamsResult(message, kind = "") {
  const element = document.getElementById("streams-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setStreamSettingsResult(message, kind = "") {
  const element = document.getElementById("stream-settings-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasAlertResult(message, kind = "") {
  const element = document.getElementById("eas-alert-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasAlertDetailResult(message, kind = "") {
  const element = document.getElementById("eas-alert-detail-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function setEasBulkResult(action, message, kind = "") {
  const element = document.getElementById(`eas-${action}-result`);
  if (!element) return;
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function easAlertStreamLabel(stream) {
  const frequency = stream.frequency ? ` ${stream.frequency} MHz` : "";
  const count = Number(stream.alert_count || 0);
  return `${stream.callsign || "Unknown"}${frequency} (${count} alert${count === 1 ? "" : "s"})`;
}

function easAlertStreamsNextSignature(streams) {
  return JSON.stringify((streams || []).map(stream => ({
    id: stream.id || "",
    callsign: stream.callsign || "",
    frequency: stream.frequency || "",
    alert_count: stream.alert_count || 0
  })));
}

function renderEasAlertStreamSelector(streams, preserve = true) {
  const select = document.getElementById("eas_alert_stream");
  const nextSignature = easAlertStreamsNextSignature(streams);
  if (select.dataset.signature !== nextSignature) {
    select.innerHTML = "";
    for (const stream of streams) {
      const option = document.createElement("option");
      option.value = stream.id || "";
      option.textContent = easAlertStreamLabel(stream);
      select.appendChild(option);
    }
    select.dataset.signature = nextSignature;
  }
  if (!streams.length) {
    easAlertStreamId = "";
    return;
  }
  const currentExists = streams.some(stream => stream.id === easAlertStreamId);
  if (!preserve || !currentExists) {
    easAlertStreamId = streams[0].id || "";
  }
  setValue("eas_alert_stream", easAlertStreamId);
}

async function loadEasAlertStreams(options = {}) {
  let data;
  try {
    data = await request("/api/eas-alert-streams");
  } catch (error) {
    if (!options.quiet) setEasAlertResult(error.message, "error");
    return;
  }
  easAlertStreams = data.streams || [];
  const hasAlerts = easAlertStreams.length > 0;
  document.getElementById("nav_eas_alerts").hidden = !hasAlerts;
  renderEasAlertStreamSelector(easAlertStreams, options.preserve !== false);
  const nextSignature = easAlertStreamsNextSignature(easAlertStreams);
  const streamListChanged = nextSignature !== easAlertStreamsSignature;
  easAlertStreamsSignature = nextSignature;
  if (!hasAlerts) {
    easAlertListSignature = "";
    document.getElementById("eas-alert-list").innerHTML = "";
    setText("eas_alert_page", "Page 1 of 1");
    setDisabled(document.getElementById("eas_alert_prev"), true);
    setDisabled(document.getElementById("eas_alert_next"), true);
    if (["eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(currentViewName())) {
      navigateTo("dashboard", {}, true, true);
    }
    return;
  }
  if (currentViewName() === "eas_alerts" && (streamListChanged || options.forceList)) {
    await loadEasAlerts({quiet: options.quiet});
  }
}

async function loadEasBulkOptions(options = {}) {
  if (!easAlertStreamId) return;
  try {
    const data = await request(`/api/eas-alert-bulk-options?stream_id=${encodeURIComponent(easAlertStreamId)}`);
    renderEasBulkOptions(data);
    updateEasBulkStreamLabels();
    if (!options.quiet) {
      setEasBulkResult("export", "");
      setEasBulkResult("delete", "");
    }
  } catch (error) {
    if (!options.quiet) {
      setEasBulkResult("export", error.message, "error");
      setEasBulkResult("delete", error.message, "error");
    }
  }
}

function renderEasBulkOptions(data) {
  easBulkServerNow = data.now || null;
  const signature = JSON.stringify(data);
  if (signature === easBulkOptionsSignature) return;
  easBulkOptionsSignature = signature;
  renderBulkOptionGroup("export", data.export_presets || [], "Export all alerts");
  renderBulkOptionGroup("delete", data.delete_presets || [], "Delete all alerts");
  setDisabled(document.getElementById("eas_export_alerts"), Number(data.total || 0) === 0);
  setDisabled(document.getElementById("eas_delete_alerts"), Number(data.total || 0) === 0);
}

function updateEasBulkStreamLabels() {
  const stream = easAlertStreams.find(item => item.id === easAlertStreamId);
  const label = stream ? `Stream: ${easAlertStreamLabel(stream)}` : "";
  setText("eas_export_stream_label", label);
  setText("eas_delete_stream_label", label);
}

function renderBulkOptionGroup(kind, presets) {
  const container = document.getElementById(`eas_${kind}_options`);
  const manual = document.getElementById(`eas_${kind}_manual`);
  container.innerHTML = "";
  for (const preset of presets) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = `eas_${kind}_mode`;
    input.value = preset.id;
    label.appendChild(input);
    label.appendChild(document.createTextNode(` ${preset.label} (${preset.count})`));
    container.appendChild(label);
  }
  const manualLabel = document.createElement("label");
  const manualInput = document.createElement("input");
  manualInput.type = "radio";
  manualInput.name = `eas_${kind}_mode`;
  manualInput.value = "manual";
  manualLabel.appendChild(manualInput);
  manualLabel.appendChild(document.createTextNode(" Manually select date range"));
  container.appendChild(manualLabel);
  buildManualRangeControls(kind, manual);
  const first = container.querySelector(`input[name='eas_${kind}_mode']`);
  if (first) first.checked = true;
  updateManualRangeVisibility(kind);
}

function buildManualRangeControls(kind, container) {
  container.innerHTML = "";
  const now = easBulkServerNow || currentDateParts();
  const start = offsetDateParts(now, -24 * 60);
  container.appendChild(dateRangeControl(kind, "start", "Start", start, false));
  container.appendChild(dateRangeControl(kind, "end", "End", now, true));
}

function dateRangeControl(kind, edge, labelText, value, includeNowButton) {
  const fieldset = document.createElement("fieldset");
  const legend = document.createElement("legend");
  legend.textContent = labelText;
  fieldset.appendChild(legend);
  const grid = document.createElement("div");
  grid.className = "grid";
  for (const spec of dateControlSpecs(value)) {
    const label = document.createElement("label");
    label.textContent = spec.label;
    const select = document.createElement("select");
    select.id = `eas_${kind}_${edge}_${spec.name}`;
    for (const optionSpec of spec.options) {
      const option = document.createElement("option");
      option.value = String(optionSpec.value);
      option.textContent = optionSpec.label;
      if (optionSpec.value === spec.value) option.selected = true;
      select.appendChild(option);
    }
    label.appendChild(select);
    grid.appendChild(label);
  }
  fieldset.appendChild(grid);
  if (includeNowButton) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "Now";
    button.dataset.bulkNow = kind;
    fieldset.appendChild(button);
  }
  return fieldset;
}

function dateControlSpecs(value) {
  const year = Number(value.year);
  const years = [];
  for (let next = year; next >= year - 10; next -= 1) years.push({value: next, label: String(next)});
  return [
    {name: "month", label: "Month", value: Number(value.month), options: Array.from({length: 12}, (_, index) => ({value: index + 1, label: String(index + 1)}))},
    {name: "day", label: "Day", value: Number(value.day), options: Array.from({length: 31}, (_, index) => ({value: index + 1, label: String(index + 1)}))},
    {name: "year", label: "Year", value: year, options: years},
    {name: "hour", label: "Hour", value: hour12(value.hour), options: Array.from({length: 12}, (_, index) => ({value: index + 1, label: String(index + 1)}))},
    {name: "minute", label: "Minute", value: Number(value.minute), options: Array.from({length: 60}, (_, index) => ({value: index, label: String(index).padStart(2, "0")}))},
    {name: "ampm", label: "AM/PM", value: Number(value.hour) >= 12 ? "PM" : "AM", options: [{value: "AM", label: "AM"}, {value: "PM", label: "PM"}]}
  ];
}

function hour12(hour) {
  const value = Number(hour) % 12;
  return value === 0 ? 12 : value;
}

function currentDateParts() {
  const now = new Date();
  return {year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate(), hour: now.getHours(), minute: now.getMinutes()};
}

function offsetDateParts(parts, offsetMinutes) {
  const date = new Date(Number(parts.year), Number(parts.month) - 1, Number(parts.day), Number(parts.hour), Number(parts.minute) + offsetMinutes);
  return {year: date.getFullYear(), month: date.getMonth() + 1, day: date.getDate(), hour: date.getHours(), minute: date.getMinutes()};
}

function setEndRangeToNow(kind) {
  const now = easBulkServerNow || currentDateParts();
  setRangeControls(kind, "end", now);
}

function setRangeControls(kind, edge, value) {
  setValue(`eas_${kind}_${edge}_month`, value.month);
  setValue(`eas_${kind}_${edge}_day`, value.day);
  setValue(`eas_${kind}_${edge}_year`, value.year);
  setValue(`eas_${kind}_${edge}_hour`, hour12(value.hour));
  setValue(`eas_${kind}_${edge}_minute`, value.minute);
  setValue(`eas_${kind}_${edge}_ampm`, Number(value.hour) >= 12 ? "PM" : "AM");
}

function selectedBulkMode(kind) {
  const selected = document.querySelector(`input[name='eas_${kind}_mode']:checked`);
  return selected ? selected.value : "";
}

function updateManualRangeVisibility(kind) {
  document.getElementById(`eas_${kind}_manual`).hidden = selectedBulkMode(kind) !== "manual";
}

function bulkRangeParams(kind) {
  const params = new URLSearchParams({stream_id: easAlertStreamId, mode: selectedBulkMode(kind)});
  if (selectedBulkMode(kind) === "manual") {
    params.set("start", localRangeDateTime(kind, "start"));
    params.set("end", localRangeDateTime(kind, "end"));
  }
  return params;
}

function localRangeDateTime(kind, edge) {
  const month = String(document.getElementById(`eas_${kind}_${edge}_month`).value).padStart(2, "0");
  const day = String(document.getElementById(`eas_${kind}_${edge}_day`).value).padStart(2, "0");
  const year = document.getElementById(`eas_${kind}_${edge}_year`).value;
  let hour = Number(document.getElementById(`eas_${kind}_${edge}_hour`).value);
  const minute = String(document.getElementById(`eas_${kind}_${edge}_minute`).value).padStart(2, "0");
  const ampm = document.getElementById(`eas_${kind}_${edge}_ampm`).value;
  if (ampm === "AM" && hour === 12) hour = 0;
  if (ampm === "PM" && hour !== 12) hour += 12;
  return `${year}-${month}-${day}T${String(hour).padStart(2, "0")}:${minute}:00`;
}

async function easBulkCount(kind) {
  return request(`/api/eas-alert-range-count?${bulkRangeParams(kind).toString()}`);
}

async function exportEasAlerts() {
  if (!easAlertStreamId) return;
  try {
    const count = await easBulkCount("export");
    if (!count.count) {
      setEasBulkResult("export", "No alerts were issued during this time.", "error");
      return;
    }
    const link = document.createElement("a");
    link.href = `/api/eas-alert-export?${bulkRangeParams("export").toString()}`;
    link.download = "";
    document.body.appendChild(link);
    link.click();
    link.remove();
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
  } catch (error) {
    setEasBulkResult("export", error.message, "error");
  }
}

async function deleteEasAlerts() {
  if (!easAlertStreamId) return;
  if (accountIsReadOnly()) {
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
    return;
  }
  try {
    const count = await easBulkCount("delete");
    if (!count.count) {
      setEasBulkResult("delete", "No alerts were issued during this time.", "error");
      return;
    }
    const confirmed = window.confirm(`${count.count} alert${count.count === 1 ? "" : "s"} and their audio files will be permanently deleted.`);
    if (!confirmed) {
      navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
      return;
    }
    const response = await request("/api/eas-alert-delete", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.fromEntries(bulkRangeParams("delete").entries()))
    });
    easAlertListSignature = "";
    await loadEasAlertStreams({preserve: true, forceList: true, quiet: true});
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage}, true, true);
  } catch (error) {
    setEasBulkResult("delete", error.message, "error");
  }
}

function easAlertListNextSignature(data) {
  return JSON.stringify({
    stream: data.stream && data.stream.id || "",
    page: data.page || 1,
    total_pages: data.total_pages || 1,
    alerts: (data.alerts || []).map(alert => [alert.id, alert.summary])
  });
}

function renderEasAlertList(data) {
  const list = document.getElementById("eas-alert-list");
  const nextSignature = easAlertListNextSignature(data);
  easAlertPage = Number(data.page || 1);
  easAlertTotalPages = Number(data.total_pages || 1);
  setText("eas_alert_page", `Page ${easAlertPage} of ${easAlertTotalPages}`);
  setDisabled(document.getElementById("eas_alert_prev"), easAlertPage <= 1);
  setDisabled(document.getElementById("eas_alert_next"), easAlertPage >= easAlertTotalPages);
  if (nextSignature === easAlertListSignature) return;
  easAlertListSignature = nextSignature;
  list.innerHTML = "";
  const alerts = data.alerts || [];
  if (alerts.length === 0) {
    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "No EAS alerts recorded for this stream.";
    list.appendChild(empty);
    return;
  }
  for (const alert of alerts) {
    const item = document.createElement("a");
    item.href = routeForView("eas_alert_detail", {
      streamId: easAlertStreamId,
      alertId: alert.id || "",
      page: easAlertPage
    });
    item.className = "stream-item";
    item.textContent = alert.summary || "Unknown EAS alert";
    item.dataset.alertId = alert.id || "";
    list.appendChild(item);
  }
}

async function loadEasAlerts(options = {}) {
  if (!easAlertStreamId) {
    renderEasAlertList({alerts: [], page: 1, total_pages: 1});
    return;
  }
  try {
    const data = await request(`/api/eas-alerts?stream_id=${encodeURIComponent(easAlertStreamId)}&page=${encodeURIComponent(easAlertPage)}&per_page=${EAS_ALERTS_PER_PAGE}`);
    renderEasAlertList(data);
    setEasAlertResult("");
  } catch (error) {
    if (!options.quiet) setEasAlertResult(error.message, "error");
  }
}

async function loadEasAlertDetail() {
  if (!easAlertStreamId || !easAlertDetailId) {
    setEasAlertDetailResult("EAS alert was not found.", "error");
    return;
  }
  try {
    const data = await request(`/api/eas-alert?stream_id=${encodeURIComponent(easAlertStreamId)}&alert_id=${encodeURIComponent(easAlertDetailId)}`);
    const alert = data.alert || {};
    setText("eas_alert_detail_title", alert.event_type || "EAS alert");
    setText("eas_detail_event", alert.event_type || "Unknown event");
    setText("eas_detail_areas", Array.isArray(alert.areas) && alert.areas.length ? alert.areas.join(", ") : "Unknown area");
    setText("eas_detail_issued", alert.issued_at || "Unknown");
    setText("eas_detail_expires", alert.expires_at || "Unknown");
    document.getElementById("eas_alert_audio").src = alert.audio_url || "";
    document.getElementById("eas_alert_download").href = alert.download_url || "#";
    setEasAlertDetailResult("");
  } catch (error) {
    setEasAlertDetailResult(error.message, "error");
  }
}

async function removeCurrentEasAlert() {
  if (!easAlertStreamId || !easAlertDetailId) return;
  if (accountIsReadOnly()) {
    setEasAlertDetailResult("This account is read-only.", "error");
    return;
  }
  const button = document.getElementById("remove_eas_alert");
  setDisabled(button, true);
  try {
    await request(`/api/eas-alert?stream_id=${encodeURIComponent(easAlertStreamId)}&alert_id=${encodeURIComponent(easAlertDetailId)}`, {
      method: "DELETE"
    });
    easAlertDetailId = "";
    easAlertListSignature = "";
    await loadEasAlertStreams({preserve: true, forceList: true, quiet: true});
    navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertReturnPage}, true, true);
  } catch (error) {
    setEasAlertDetailResult(error.message, "error");
  } finally {
    setDisabled(button, false);
  }
}

function showView(name) {
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view_${name}`;
  }
  const moreButton = document.getElementById("nav_more_button");
  for (const item of document.querySelectorAll("nav [data-view]")) {
    if (
      item.dataset.view === name ||
      (item.dataset.view === "streams" && name === "stream_settings") ||
      (item.dataset.view === "iq_recorder" && ["iq_recorder_start", "iq_recording_download"].includes(name)) ||
      (item.dataset.view === "eas_alerts" && ["eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(name)) ||
      item.dataset.view === "accounts" && name === "accounts" ||
      item.dataset.view === "change_password" && name === "change_password"
    ) {
      item.setAttribute("aria-current", "page");
    } else {
      item.removeAttribute("aria-current");
    }
  }
  if (moreButton) {
    if (["receiver", "iq_recorder", "iq_recorder_start", "iq_recording_download", "logs", "accounts", "change_password"].includes(name)) {
      moreButton.setAttribute("aria-current", "page");
    } else {
      moreButton.removeAttribute("aria-current");
    }
  }
}

function currentViewName() {
  const visible = Array.from(document.querySelectorAll(".view")).find(view => !view.hidden);
  return visible ? visible.id.replace(/^view_/, "") : "dashboard";
}

function routeForView(name, params = {}) {
  const query = new URLSearchParams();
  if (name === "dashboard") return "/";
  if (name === "rtl") query.set("view", "rtl");
  if (name === "receiver") query.set("view", "receiver");
  if (name === "iq_recorder") query.set("view", "iq_recorder");
  if (name === "iq_recorder_start") query.set("view", "iq_recorder_start");
  if (name === "iq_recording_download") {
    query.set("view", "iq_recording_download");
    if (params.recordingId) query.set("recording", params.recordingId);
  }
  if (name === "logs") query.set("view", "logs");
  if (name === "accounts") query.set("view", "accounts");
  if (name === "change_password") query.set("view", "change_password");
  if (name === "streams") query.set("view", "streams");
  if (name === "add_stream") query.set("view", "add_stream");
  if (name === "stream_settings") {
    query.set("view", "stream_settings");
    if (params.streamId) query.set("stream", params.streamId);
  }
  if (name === "eas_alerts") {
    query.set("view", "eas_alerts");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.page) query.set("page", params.page);
  }
  if (name === "eas_alert_export") {
    query.set("view", "eas_alert_export");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.page) query.set("page", params.page);
  }
  if (name === "eas_alert_delete") {
    query.set("view", "eas_alert_delete");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.page) query.set("page", params.page);
  }
  if (name === "eas_alert_detail") {
    query.set("view", "eas_alert_detail");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.alertId) query.set("alert", params.alertId);
    if (params.page) query.set("page", params.page);
  }
  const text = query.toString();
  return text ? `/?${text}` : "/";
}

function routeFromLocation() {
  const query = new URLSearchParams(window.location.search);
  const view = query.get("view") || "dashboard";
  if (["dashboard", "rtl", "receiver", "iq_recorder", "iq_recorder_start", "iq_recording_download", "logs", "accounts", "change_password", "streams", "add_stream", "stream_settings", "eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(view)) {
    return {
      view,
      streamId: query.get("stream") || "",
      alertId: query.get("alert") || "",
      recordingId: query.get("recording") || "",
      page: Math.max(1, Number(query.get("page") || 1))
    };
  }
  return {view: "dashboard", streamId: "", alertId: "", page: 1};
}

function routeState(view, params = {}) {
  return {
    view,
    streamId: params.streamId || "",
    alertId: params.alertId || "",
    recordingId: params.recordingId || "",
    page: Math.max(1, Number(params.page || 1))
  };
}

function applyRoute(route) {
  if (accountIsReadOnly() && isReadOnlyRestrictedView(route.view)) {
    showView("dashboard");
    return;
  }
  if (route.view === "stream_settings") {
    const streamId = route.streamId || settingsStreamId;
    const stream = configuredStreams.find(item => item.id === streamId);
    if (stream) {
      settingsStreamId = streamId;
      outputTableSignature = "";
      easSignature = "";
      audioEffectsSignature = "";
      cancelOutputForm();
      const station = stream.station || {};
      setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
      renderStreamSettings();
      showView("stream_settings");
      return;
    }
    showView("streams");
    return;
  }
  if (route.view === "eas_alerts") {
    easAlertStreamId = route.streamId || easAlertStreamId;
    easAlertPage = Math.max(1, Number(route.page || 1));
    showView("eas_alerts");
    loadEasAlertStreams({preserve: true, forceList: true});
    return;
  }
  if (route.view === "eas_alert_export" || route.view === "eas_alert_delete") {
    easAlertStreamId = route.streamId || easAlertStreamId;
    easAlertPage = Math.max(1, Number(route.page || easAlertPage || 1));
    easBulkOptionsSignature = "";
    showView(route.view);
    loadEasAlertStreams({preserve: true, quiet: true}).then(() => loadEasBulkOptions());
    return;
  }
  if (route.view === "eas_alert_detail") {
    easAlertStreamId = route.streamId || easAlertStreamId;
    easAlertDetailId = route.alertId || "";
    easAlertReturnPage = Math.max(1, Number(route.page || 1));
    showView("eas_alert_detail");
    loadEasAlertDetail();
    return;
  }
  if (route.view !== "stream_settings") settingsStreamId = "";
  if (route.view === "receiver") renderReceiverControls();
  if (route.view === "iq_recorder") {
    loadIqRecordings().catch(error => setIqRecorderResult(error.message, "error"));
  }
  if (route.view === "iq_recorder_start") {
    populateIqSampleRates();
    renderIqStreamOptions(true);
    renderIqModeFields();
  }
  if (route.view === "iq_recording_download") {
    selectedIqRecordingId = route.recordingId || selectedIqRecordingId;
    loadIqRecordings()
      .then(() => renderIqDownloadPage())
      .catch(error => setIqDownloadResult(error.message, "error"));
  }
  if (route.view === "accounts") {
    loadAccounts().catch(error => setAccountResult(error.message, "error"));
  }
  showView(route.view);
}

function outputFormHasUnsavedChanges() {
  return currentViewName() === "stream_settings" && outputFormDirty;
}

function wizardHasUnsavedChanges() {
  return currentViewName() === "add_stream" && wizardDirty;
}

function hasUnsavedNavigationState() {
  return Boolean(wizardHasUnsavedChanges() || outputFormHasUnsavedChanges());
}

function confirmDiscardNavigation() {
  if (!hasUnsavedNavigationState()) return true;
  return window.confirm("Discard the current setup changes?");
}

function closeNavMoreMenu(focusButton = false) {
  const button = document.getElementById("nav_more_button");
  const menu = document.getElementById("nav_more_menu");
  if (!button || !menu) return;
  menu.hidden = true;
  button.setAttribute("aria-expanded", "false");
  if (focusButton) button.focus();
}

function openNavMoreMenu(focusFirst = false) {
  const button = document.getElementById("nav_more_button");
  const menu = document.getElementById("nav_more_menu");
  if (!button || !menu) return;
  menu.hidden = false;
  button.setAttribute("aria-expanded", "true");
  if (focusFirst) {
    const first = menu.querySelector("[role='menuitem']");
    if (first) first.focus();
  }
}

function toggleNavMoreMenu(focusFirst = false) {
  const menu = document.getElementById("nav_more_menu");
  if (!menu) return;
  if (menu.hidden) {
    openNavMoreMenu(focusFirst);
  } else {
    closeNavMoreMenu();
  }
}

function navigateTo(view, params = {}, replace = false, force = false) {
  if (!replace && !force && !confirmDiscardNavigation()) return;
  if (accountIsReadOnly() && isReadOnlyRestrictedView(view)) {
    view = "dashboard";
    params = {};
    replace = true;
  }
  closeNavMoreMenu();
  const url = routeForView(view, params);
  const state = routeState(view, params);
  if (replace) {
    history.replaceState(state, "", url);
  } else {
    history.pushState(state, "", url);
  }
  applyRoute(state);
}

function currentRouteParams() {
  const view = currentViewName();
  if (view === "stream_settings") return {streamId: settingsStreamId};
  if (view === "eas_alerts") return {streamId: easAlertStreamId, page: easAlertPage};
  if (view === "eas_alert_export" || view === "eas_alert_delete") return {streamId: easAlertStreamId, page: easAlertPage};
  if (view === "eas_alert_detail") return {streamId: easAlertStreamId, alertId: easAlertDetailId, page: easAlertReturnPage};
  if (view === "iq_recording_download") return {recordingId: selectedIqRecordingId};
  return {};
}

function activeSdrLabel(settings) {
  if (!settings.serial) return "none";
  const select = document.getElementById("serial");
  const option = Array.from(select.options).find(item => item.value === settings.serial);
  return option ? option.textContent : `serial ${settings.serial}`;
}

function renderStorageSummary(storage) {
  const filesystems = storage && Array.isArray(storage.filesystems) ? storage.filesystems : [];
  const primary = filesystems.length ? filesystems[0] : null;
  setText("summary_storage", primary && primary.summary ? primary.summary.replace(/^Storage: /, "") : "unknown");
  const warning = document.getElementById("storage-warning");
  const message = storage && storage.status && storage.status !== "ok" ? storage.message || "Please free up disk space." : "";
  if (warning.textContent !== message) warning.textContent = message;
  const hidden = !message;
  if (warning.hidden !== hidden) warning.hidden = hidden;
}

function primaryStorageSummary(storage) {
  const filesystems = storage && Array.isArray(storage.filesystems) ? storage.filesystems : [];
  const primary = filesystems.length ? filesystems[0] : null;
  return primary && primary.summary ? primary.summary.replace(/^Storage: /, "") : "unknown";
}

function populateIqSampleRates() {
  const select = document.getElementById("iq_sample_rate");
  if (!select || select.options.length) return;
  for (const rate of IQ_RECORDER_SAMPLE_RATES) {
    const option = document.createElement("option");
    option.value = String(rate);
    option.textContent = `${rate} S/s`;
    if (rate === 192000) option.selected = true;
    select.appendChild(option);
  }
}

function activeIqStreamOptions() {
  return configuredStreams
    .filter(stream => stream.enabled !== false)
    .map(stream => {
      const station = stream.station || {};
      const callsign = station.callsign || "Unknown";
      const frequency = station.frequency ? `${station.frequency} MHz` : "";
      return {id: stream.id, label: `${callsign}${frequency ? ` ${frequency}` : ""}`};
    });
}

function renderIqStreamOptions(force = false) {
  const select = document.getElementById("iq_stream_select");
  if (!select) return;
  const options = activeIqStreamOptions();
  const signature = JSON.stringify(options);
  if (!force && signature === iqStreamOptionsSignature) return;
  const current = select.value;
  select.replaceChildren();
  for (const item of options) {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = item.label;
    select.appendChild(option);
  }
  if (options.some(item => item.id === current)) {
    select.value = current;
  }
  iqStreamOptionsSignature = signature;
}

function currentIqMode() {
  const selected = document.querySelector("input[name='iq_recording_mode']:checked");
  return selected ? selected.value : "stream";
}

function renderIqModeFields() {
  const mode = currentIqMode();
  setHidden("iq_stream_fields", mode !== "stream");
  setHidden("iq_spectrum_fields", mode !== "spectrum");
  const start = document.getElementById("iq_start_recording");
  if (start) {
    start.disabled = mode === "stream" && !document.getElementById("iq_stream_select").value;
  }
}

function iqRecorderSourceText(recorder) {
  if (!recorder || recorder.status === "idle") return "None";
  if (recorder.mode === "stream") return recorder.stream_label || "Stream channel";
  return "Entire spectrum at 162.475 MHz";
}

function renderIqRecorder(recorder, storage) {
  recorder = recorder || {active: false, status: "idle"};
  if (accountIsReadOnly()) recorder = {active: false, status: "idle"};
  populateIqSampleRates();
  renderIqStreamOptions();
  const active = Boolean(recorder.active);
  setHidden("iq_recording_banner", !active);
  if (active) setText("iq_recording_banner_elapsed", formatDuration(recorder.elapsed_seconds));
  setHidden("iq_recorder_active", !active);
  setHidden("iq_recorder_idle", active);
  const openStart = document.getElementById("open_iq_start");
  if (openStart) setDisabled(openStart, active);
  if (!active) {
    renderIqModeFields();
    const signature = JSON.stringify({status: recorder.status, error: recorder.error || ""});
    if (signature !== iqRecorderSignature && recorder.status && recorder.status !== "idle") {
      const message = recorder.error || (recorder.status === "completed" ? "I/Q recording completed." : "I/Q recording stopped.");
      setIqRecorderResult(message, recorder.status === "needs-attention" ? "error" : "success");
    }
    iqRecorderSignature = signature;
    return;
  }
  setText("iq_status", recorder.status === "recording" ? "Recording" : recorder.status);
  setText("iq_source", iqRecorderSourceText(recorder));
  setText("iq_active_sample_rate", `${recorder.sample_rate || 0} S/s`);
  setText("iq_file_size", formatDecimalBytes(recorder.bytes_written));
  setText("iq_elapsed", formatDuration(recorder.elapsed_seconds));
  setText("iq_remaining", recorder.storage_remaining_seconds === null || recorder.storage_remaining_seconds === undefined ? "calculating" : formatDuration(recorder.storage_remaining_seconds));
  const hasAutoStop = Number(recorder.duration_seconds || 0) > 0;
  setHidden("iq_auto_stop_metric", !hasAutoStop);
  if (hasAutoStop) {
    const autoStopSeconds = Math.max(0, Number(recorder.duration_seconds || 0) - Number(recorder.elapsed_seconds || 0));
    setText("iq_auto_stop_remaining", formatDuration(autoStopSeconds));
  }
  setText("iq_storage", primaryStorageSummary(storage));
  iqRecorderSignature = JSON.stringify({status: recorder.status, file_name: recorder.file_name});
}

function iqRecordingTableSignature(recordings) {
  return JSON.stringify((recordings || []).map(recording => ({
    id: recording.id || "",
    recorded_at: recording.recorded_at || "",
    duration: recording.duration || "",
    sample_rate: recording.sample_rate || 0,
    frequency_hz: recording.frequency_hz || 0,
    downloading: Boolean(recording.downloading)
  })));
}

function renderIqRecordings(recordings) {
  iqRecordings = recordings || [];
  const tbody = document.getElementById("iq-recordings-body");
  if (!tbody) return;
  const signature = iqRecordingTableSignature(iqRecordings);
  if (signature === iqRecordingsSignature) return;
  iqRecordingsSignature = signature;
  tbody.innerHTML = "";
  if (!iqRecordings.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "hint";
    cell.textContent = "No I/Q recordings.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  for (const recording of iqRecordings) {
    tbody.appendChild(iqRecordingRow(recording));
  }
}

function iqRecordingRow(recording) {
  const row = document.createElement("tr");
  row.appendChild(tableCell(recording.recorded_at || "Unknown"));
  row.appendChild(tableCell(recording.duration || "0:00"));
  row.appendChild(tableCell(`${recording.sample_rate || 0} S/s`));
  row.appendChild(tableCell(`${recording.frequency_hz || 0} Hz`));
  row.appendChild(iqRecordingActionsCell(recording));
  return row;
}

function iqRecordingActionsCell(recording) {
  const cell = document.createElement("td");
  cell.className = "menu-cell";
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for I/Q recording from ${recording.recorded_at || "unknown time"}`);
  button.dataset.iqRecordingMenu = recording.id || "";
  const menu = document.createElement("div");
  menu.className = "stream-actions-menu";
  menu.hidden = true;
  menu.setAttribute("role", "menu");
  const download = document.createElement("button");
  download.type = "button";
  download.textContent = "Download recording";
  download.setAttribute("role", "menuitem");
  download.dataset.action = "download-iq-recording";
  download.dataset.recordingId = recording.id || "";
  menu.appendChild(download);
  if (!recording.downloading && !accountIsReadOnly()) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove recording";
    remove.setAttribute("role", "menuitem");
    remove.dataset.action = "remove-iq-recording";
    remove.dataset.recordingId = recording.id || "";
    menu.appendChild(remove);
  }
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

async function loadIqRecordings() {
  const data = await request("/api/iq-recordings");
  renderIqRecordings(data.recordings || []);
  return data;
}

async function startIqRecording() {
  if (accountIsReadOnly()) {
    setIqStartResult("This account is read-only.", "error");
    return;
  }
  const mode = currentIqMode();
  const rawMinutes = Number(document.getElementById("iq_duration_minutes").value || 0);
  const minutes = Math.max(0, Math.min(1440, Number.isFinite(rawMinutes) ? rawMinutes : 5));
  const payload = {
    mode,
    duration_seconds: Math.round(minutes * 60)
  };
  if (mode === "stream") {
    payload.stream_id = document.getElementById("iq_stream_select").value;
  } else {
    payload.sample_rate = Number(document.getElementById("iq_sample_rate").value);
  }
  setIqStartResult("Starting I/Q recording...");
  const data = await request("/api/iq-recorder/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  renderIqRecorder(data.iq_recorder || {}, {});
  await loadIqRecordings();
  navigateTo("iq_recorder", {}, false, true);
  setIqRecorderResult("I/Q recording started.", "success");
}

async function stopIqRecording() {
  if (accountIsReadOnly()) {
    setIqRecorderResult("This account is read-only.", "error");
    return;
  }
  setIqRecorderResult("Stopping I/Q recording...");
  const data = await request("/api/iq-recorder/stop", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: "{}"
  });
  renderIqRecorder(data.iq_recorder || {}, {});
  await loadIqRecordings();
  setIqRecorderResult("I/Q recording stopped.", "success");
}

function selectedIqRecording() {
  return iqRecordings.find(recording => recording.id === selectedIqRecordingId) || null;
}

function renderIqDownloadPage() {
  const recording = selectedIqRecording();
  if (!recording) {
    setText("iq_download_recording_label", "I/Q recording was not found.");
    return;
  }
  setText(
    "iq_download_recording_label",
    `${recording.recorded_at || "Unknown"}; ${recording.sample_rate || 0} S/s; ${recording.frequency_hz || 0} Hz`
  );
}

function startBackgroundDownload(url) {
  const iframe = document.createElement("iframe");
  iframe.hidden = true;
  iframe.setAttribute("aria-hidden", "true");
  iframe.title = "";
  iframe.src = url;
  document.body.appendChild(iframe);
  setTimeout(() => {
    iframe.remove();
  }, 10 * 60 * 1000);
}

async function removeIqRecording(recordingId) {
  if (accountIsReadOnly()) {
    setIqRecorderResult("This account is read-only.", "error");
    return;
  }
  const data = await request(`/api/iq-recording?id=${encodeURIComponent(recordingId)}`, {method: "DELETE"});
  renderIqRecordings(data.recordings || []);
  setIqRecorderResult("I/Q recording removed.", "success");
}

async function downloadSelectedIqRecording() {
  const recording = selectedIqRecording();
  if (!recording) {
    setIqDownloadResult("I/Q recording was not found.", "error");
    return;
  }
  const format = document.getElementById("iq_download_format").value || "cf";
  startBackgroundDownload(`/api/iq-recording-download?id=${encodeURIComponent(recording.id)}&format=${encodeURIComponent(format)}`);
  navigateTo("iq_recorder", {}, false, true);
  setIqRecorderResult("Download started.", "success");
  setTimeout(() => loadIqRecordings().catch(() => {}), 500);
}

function updateDashboard(data) {
  const settings = data.settings;
  setText("summary_sdr", activeSdrLabel(settings));
  setText("summary_gain", settings.gain === null ? "automatic" : `${settings.gain} dB`);
  setText("summary_capture", data.active ? "active" : "inactive");
  setText("summary_stream_count", configuredStreams.length);
  renderStorageSummary(data.storage || {});
  activeStreamSnapshots = data.active_streams || [];
  renderActiveStreams(data.active_streams || [], configuredStreams);
  renderDashboardStreamAttention(data.active_streams || [], configuredStreams);
  renderDashboardRecentAlerts(data.recent_eas_alerts || []);
  renderIqRecorder(data.iq_recorder || {}, data.storage || {});
  if (settingsStreamId) renderStreamSettings();
}

function syncControls(data) {
  const s = data.settings;
  gainValues = data.gain_values || [];
  setValue("serial", s.serial || "");
  setChecked("gain_auto", s.gain === null);
  const gain = document.getElementById("gain");
  setAttributeIfChanged(gain, "max", Math.max(0, gainValues.length - 1));
  setDisabled(gain, s.gain === null || gainValues.length === 0);
  const gainIndex = gainIndexFor(s.gain);
  if (gain.value !== String(gainIndex)) gain.value = String(gainIndex);
  if (s.gain === null) {
    setText("gain_label", "Automatic");
  } else if (gainValues.length === 0) {
    setText("gain_label", `${s.gain} dB`);
  } else {
    setText("gain_label", `${gainValues[gainIndex]} dB`);
  }
  setValue("ppm_correction", s.ppm_correction);
  setChecked("bias_tee", s.bias_tee);
  lastControlSignature = controlSignature(data);
}

function applyStatus(data, options = {}) {
  applying = true;
  if (data.account) applyAccountUi(data.account);
  const nextSignature = controlSignature(data);
  if (
    options.syncControls ||
    lastControlSignature === "" ||
    (nextSignature !== lastControlSignature && !controlHasFocus())
  ) {
    syncControls(data);
  } else {
    gainValues = data.gain_values || gainValues;
  }
  setText("active", data.active ? "active" : "inactive");
  setText("chunks", data.received_chunks);
  setText("bytes", data.received_bytes);
  setText("last", data.last_batch_at ? `${data.last_batch_at.toFixed(3)}s` : "never");
  setText("capture-error", data.capture_error || "");
  setText("logs", data.logs.join("\\n"));
  setFallbackControls(data.fallback);
  updateDashboard(data);
  const now = Date.now();
  if (now - lastEasAlertRefreshAt > 10000) {
    lastEasAlertRefreshAt = now;
    loadEasAlertStreams({preserve: true, quiet: true});
  }
  if (currentViewName().startsWith("iq_") && now - lastIqRecordingsRefreshAt > 5000) {
    lastIqRecordingsRefreshAt = now;
    loadIqRecordings().catch(() => {});
  }
  applying = false;
}

function currentPayload() {
  const auto = document.getElementById("gain_auto").checked;
  const gainIndex = Number(document.getElementById("gain").value);
  return {
    serial: document.getElementById("serial").value,
    gain: auto || gainValues.length === 0 ? null : gainValues[gainIndex],
    ppm_correction: Number(document.getElementById("ppm_correction").value),
    bias_tee: document.getElementById("bias_tee").checked
  };
}

function scheduleUpdate() {
  if (applying) return;
  clearTimeout(timer);
  timer = setTimeout(async () => {
    try {
      const data = await request("/api/settings", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(currentPayload())
      });
      applyStatus(data, {syncControls: true});
    } catch (error) {
      document.getElementById("capture-error").textContent = error.message;
    }
  }, 250);
}

function scheduleFallbackUpdate() {
  if (applying) return;
  clearTimeout(fallbackUpdateTimer);
  fallbackUpdateTimer = setTimeout(async () => {
    try {
      const data = await request("/api/fallback-settings", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(fallbackPayload())
      });
      setFallbackResult("Fallback audio settings saved.", "success");
      applyStatus(data, {syncControls: false});
    } catch (error) {
      setFallbackResult(error.message, "error");
    }
  }, 250);
}

function scheduleEasUpdate() {
  if (applying || !settingsStreamId) return;
  clearTimeout(easUpdateTimer);
  easUpdateTimer = setTimeout(async () => {
    const stream = currentSettingsStream();
    const payload = easPayload();
    if (stream && easRecordingEnabled(stream) && !payload.enabled && !canDisableEasRecording(stream)) {
      setChecked("eas_enabled", true);
      setEasResult("At least one output must remain enabled for each stream.", "error");
      return;
    }
    try {
      const data = await request("/api/eas-recording", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: settingsStreamId,
          eas_recording: payload
        })
      });
      renderStreams(data.streams || []);
      setEasResult("EAS recording settings saved.", "success");
      loadEasAlertStreams({preserve: true, quiet: true});
    } catch (error) {
      setEasResult(error.message, "error");
    }
  }, 250);
}

function scheduleAudioEffectsUpdate() {
  if (applying || !settingsStreamId) return;
  clearTimeout(audioEffectsUpdateTimer);
  audioEffectsUpdateTimer = setTimeout(async () => {
    try {
      const data = await request("/api/audio-effects", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: settingsStreamId,
          audio: audioEffectsPayload()
        })
      });
      renderStreams(data.streams || []);
      setAudioEffectsResult("Audio effects saved.", "success");
    } catch (error) {
      setAudioEffectsResult(error.message, "error");
    }
  }, 250);
}

for (const id of controls) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) scheduleUpdate();
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) scheduleUpdate();
  });
}

for (const id of ["fallback_enabled", "fallback_delay", "fallback_loop_delay"]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) scheduleFallbackUpdate();
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) scheduleFallbackUpdate();
  });
}

for (const id of ["eas_enabled", "eas_pre_seconds", "eas_post_seconds", "eas_max_seconds"]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) scheduleEasUpdate();
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) scheduleEasUpdate();
  });
}

for (const formatControl of document.querySelectorAll("input[name='eas_format']")) {
  formatControl.addEventListener("change", scheduleEasUpdate);
}

for (const id of [
  "audio_volume_enabled",
  "audio_volume_multiplier",
  "audio_comfort_noise_enabled",
  "audio_comfort_noise_level",
  "audio_deemphasis_enabled",
  "audio_deemphasis_tau",
  "audio_highpass_enabled",
  "audio_highpass_frequency",
  "audio_highpass_sharpness",
  "audio_lowpass_enabled",
  "audio_lowpass_frequency",
  "audio_lowpass_sharpness",
  "audio_notch_enabled",
  "audio_notch_frequency",
  "audio_notch_sharpness"
]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (audioFrequencyNameForElement(event.target)) {
        if (isTextEditingInputEvent(event) || event.target.dataset.userEditing === "1") return;
        commitAudioFrequencyElement(event.target, false);
      }
      scheduleAudioEffectsUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      commitAudioFrequencyElement(event.target, false);
      scheduleAudioEffectsUpdate();
    }
  });
}

document.addEventListener("keydown", event => {
  if (!audioFrequencyNameForElement(event.target)) return;
  if (isTextEditingKey(event)) {
    event.target.dataset.userEditing = "1";
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    commitAudioFrequencyElement(event.target, true);
    return;
  }
  if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) {
    setTimeout(() => commitAudioFrequencyElement(event.target, true), 0);
  }
});

document.addEventListener("blur", event => {
  commitAudioFrequencyElement(event.target, true);
}, true);

document.addEventListener("click", event => {
  const link = event.target && event.target.closest("a[data-view]");
  if (!link) return;
  if (
    event.defaultPrevented ||
    event.button !== 0 ||
    event.metaKey ||
    event.ctrlKey ||
    event.shiftKey ||
    event.altKey
  ) {
    return;
  }
  event.preventDefault();
  navigateTo(link.dataset.view, {
    streamId: link.dataset.streamId || "",
    alertId: link.dataset.alertId || "",
    page: Number(link.dataset.page || 1)
  });
});

document.getElementById("nav_more_button").addEventListener("click", () => {
  toggleNavMoreMenu();
});

document.getElementById("nav_more_button").addEventListener("keydown", event => {
  if (event.key === "ArrowDown" || event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    openNavMoreMenu(true);
  }
  if (event.key === "Escape") {
    closeNavMoreMenu(true);
  }
});

document.getElementById("nav_more_menu").addEventListener("keydown", event => {
  if (event.key === "Escape") {
    event.preventDefault();
    closeNavMoreMenu(true);
    return;
  }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    const items = Array.from(document.querySelectorAll("#nav_more_menu [role='menuitem']"));
    const current = items.indexOf(document.activeElement);
    const step = event.key === "ArrowDown" ? 1 : -1;
    const next = items[(current + step + items.length) % items.length];
    if (next) next.focus();
  }
});

document.getElementById("dismiss_account_secret").addEventListener("click", dismissAccountSecret);
document.getElementById("copy_account_secret").addEventListener("click", copyAccountSecret);
document.getElementById("open_create_account").addEventListener("click", () => {
  setAccountResult("");
  showAccountSecret("");
  document.getElementById("create_account_panel").hidden = false;
  document.getElementById("new_account_username").focus();
});
document.getElementById("cancel_create_account").addEventListener("click", () => {
  document.getElementById("create_account_panel").hidden = true;
});
document.getElementById("create_account").addEventListener("click", async () => {
  try {
    await createAccountFromForm();
  } catch (error) {
    setAccountResult(error.message, "error");
  }
});
document.getElementById("change_account_password").addEventListener("click", async () => {
  try {
    await changeOwnPasswordFromForm();
  } catch (error) {
    setChangePasswordResult(error.message, "error");
  }
});
document.getElementById("cancel_change_password").addEventListener("click", () => {
  navigateTo("dashboard");
});
document.getElementById("accounts-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.accountMenu !== undefined) {
    const menu = target.nextElementSibling;
    if (!menu) return;
    if (menu.hidden) {
      openStreamActionMenu(target, "first");
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  const accountId = target.dataset.accountId || "";
  try {
    if (target.dataset.action === "change-own-password") {
      closeStreamActionMenus();
      navigateTo("change_password");
      return;
    }
    if (target.dataset.action === "reset-account-password") {
      closeStreamActionMenus();
      if (!window.confirm("Reset this account password?")) return;
      await resetAccountPassword(accountId);
      return;
    }
    if (target.dataset.action === "toggle-account-read-only") {
      closeStreamActionMenus();
      await setAccountReadOnly(accountId, target.dataset.readOnly === "1");
      return;
    }
    if (target.dataset.action === "delete-account") {
      closeStreamActionMenus();
      if (!window.confirm("Delete this account?")) return;
      await deleteAccount(accountId);
    }
  } catch (error) {
    setAccountResult(error.message, "error");
  }
});
document.getElementById("accounts-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.accountMenu !== undefined) {
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
    }
  }
});

document.getElementById("dismiss_nwrorg_submission").addEventListener("click", dismissNwrOrgSubmissionDialog);
document.getElementById("nwrorg_submission_link").addEventListener("click", dismissNwrOrgSubmissionDialog);
document.getElementById("dismiss_monitor_unstable").addEventListener("click", dismissMonitorUnstableDialog);
document.getElementById("dismiss_receiver_unstable").addEventListener("click", dismissReceiverUnstableDialog);

document.getElementById("receiver_previous").addEventListener("click", receiverPreviousChannel);
document.getElementById("receiver_next").addEventListener("click", receiverNextChannel);
document.getElementById("receiver_play_pause").addEventListener("click", async () => {
  try {
    if (receiverPlaying) {
      await stopWeatherReceiver({preserveMediaSession: true});
      setReceiverResult("Receiver paused.", "success");
    } else {
      setReceiverResult("Starting receiver...");
      await startWeatherReceiver();
    }
  } catch (error) {
    setReceiverResult(error.message, "error");
    renderReceiverControls();
  }
});

for (const radio of document.querySelectorAll("input[name='iq_recording_mode']")) {
  radio.addEventListener("change", renderIqModeFields);
}
document.getElementById("iq_stream_select").addEventListener("change", renderIqModeFields);
document.getElementById("iq_start_recording").addEventListener("click", async () => {
  try {
    await startIqRecording();
  } catch (error) {
    setIqStartResult(error.message, "error");
  }
});
document.getElementById("iq_stop_recording").addEventListener("click", async () => {
  try {
    await stopIqRecording();
  } catch (error) {
    setIqRecorderResult(error.message, "error");
  }
});
document.getElementById("open_iq_start").addEventListener("click", () => {
  setIqStartResult("");
  navigateTo("iq_recorder_start");
});
document.getElementById("cancel_iq_start").addEventListener("click", () => {
  navigateTo("iq_recorder");
});
document.getElementById("cancel_iq_download").addEventListener("click", () => {
  navigateTo("iq_recorder");
});
document.getElementById("iq_download_recording").addEventListener("click", async () => {
  try {
    await downloadSelectedIqRecording();
  } catch (error) {
    setIqDownloadResult(error.message, "error");
  }
});
document.getElementById("iq-recordings-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.iqRecordingMenu !== undefined) {
    const menu = target.nextElementSibling;
    const shouldOpen = menu.hidden;
    if (shouldOpen) {
      openStreamActionMenu(target, null);
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  if (target.dataset.action === "download-iq-recording") {
    closeStreamActionMenus();
    selectedIqRecordingId = target.dataset.recordingId;
    navigateTo("iq_recording_download", {recordingId: selectedIqRecordingId});
    return;
  }
  if (target.dataset.action === "remove-iq-recording") {
    closeStreamActionMenus();
    if (!window.confirm("Remove this I/Q recording?")) return;
    try {
      await removeIqRecording(target.dataset.recordingId);
    } catch (error) {
      setIqRecorderResult(error.message, "error");
    }
  }
});
document.getElementById("iq-recordings-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.iqRecordingMenu !== undefined) {
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (target.getAttribute("role") === "menuitem") {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
});

document.getElementById("tab_outputs").addEventListener("click", () => showSettingsTab("outputs"));
document.getElementById("tab_audio").addEventListener("click", () => showSettingsTab("audio"));
document.getElementById("tab_eas").addEventListener("click", () => showSettingsTab("eas"));
document.getElementById("tab_fallback").addEventListener("click", () => showSettingsTab("fallback"));
document.querySelector(".tabs").addEventListener("keydown", event => {
  const tabs = [document.getElementById("tab_outputs"), document.getElementById("tab_eas"), document.getElementById("tab_fallback"), document.getElementById("tab_audio")];
  const index = tabs.indexOf(event.target);
  if (index < 0) return;
  let nextIndex = index;
  if (event.key === "ArrowRight") nextIndex = (index + 1) % tabs.length;
  else if (event.key === "ArrowLeft") nextIndex = (index - 1 + tabs.length) % tabs.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = tabs.length - 1;
  else return;
  event.preventDefault();
  tabs[nextIndex].focus();
  showSettingsTab(tabs[nextIndex].id.replace(/^tab_/, ""));
});

document.getElementById("audio_effects_list").addEventListener("click", event => {
  const button = event.target && event.target.closest ? event.target.closest("[data-audio-effect]") : null;
  if (!button) return;
  selectAudioEffect(button.dataset.audioEffect, true);
});

document.getElementById("audio_effects_list").addEventListener("keydown", event => {
  const buttons = Array.from(document.querySelectorAll("[data-audio-effect]"));
  const index = buttons.indexOf(event.target);
  if (index < 0) return;
  let nextIndex = index;
  if (event.key === "ArrowDown" || event.key === "ArrowRight") nextIndex = (index + 1) % buttons.length;
  else if (event.key === "ArrowUp" || event.key === "ArrowLeft") nextIndex = (index - 1 + buttons.length) % buttons.length;
  else if (event.key === "Home") nextIndex = 0;
  else if (event.key === "End") nextIndex = buttons.length - 1;
  else return;
  event.preventDefault();
  buttons[nextIndex].focus();
  selectAudioEffect(buttons[nextIndex].dataset.audioEffect, false);
});

document.getElementById("audio_effects_back").addEventListener("click", () => {
  document.getElementById("audio_effects_layout").classList.remove("effect-detail-active");
  const selected = document.querySelector(`[data-audio-effect='${selectedAudioEffect}']`);
  if (selected) selected.focus();
});

window.addEventListener("popstate", event => {
  if (!confirmDiscardNavigation()) {
    const currentView = currentViewName();
    const currentParams = currentRouteParams();
    history.pushState(routeState(currentView, currentParams), "", routeForView(currentView, currentParams));
    return;
  }
  applyRoute(event.state || routeFromLocation());
});

document.getElementById("open_add_stream").addEventListener("click", () => {
  if (accountIsReadOnly()) return;
  beginStreamWizard();
});

document.getElementById("active-streams-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.activeStreamMenu !== undefined) {
    const menu = target.nextElementSibling;
    const shouldOpen = menu.hidden;
    if (shouldOpen) {
      openStreamActionMenu(target, null);
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  if (target.dataset.action === "edit-active-stream") {
    closeStreamActionMenus();
    if (accountIsReadOnly()) return;
    editStreamSettings(target.dataset.streamId, target.dataset.outputId);
    return;
  }
  if (target.dataset.action === "toggle-active-stream") {
    closeStreamActionMenus();
    if (accountIsReadOnly()) return;
    const stream = configuredStreams.find(item => item.id === target.dataset.streamId);
    if (!stream) {
      setStreamResult("Stream was not found.", "error");
      return;
    }
    try {
      await setStreamEnabled(target.dataset.streamId, stream.enabled === false, setStreamsResult);
    } catch (error) {
      setStreamsResult(error.message, "error");
    }
    return;
  }
  if (target.dataset.action === "toggle-monitor") {
    closeStreamActionMenus();
    try {
      await toggleStreamMonitor(target.dataset.streamId, setStreamsResult);
    } catch (error) {
      setStreamsResult(error.message, "error");
      renderStreams(configuredStreams);
      if (settingsStreamId) renderStreamSettings();
    }
    return;
  }
  if (target.dataset.action === "remove-active-stream") {
    closeStreamActionMenus();
    if (accountIsReadOnly()) return;
    try {
      const data = await request(`/api/streams?id=${encodeURIComponent(target.dataset.streamId)}`, {
        method: "DELETE"
      });
      renderStreams(data.streams || []);
      setStreamsResult("");
    } catch (error) {
      setStreamsResult(error.message, "error");
    }
  }
});

document.getElementById("active-streams-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.activeStreamMenu !== undefined) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (target.getAttribute("role") === "menuitem") {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
});

document.addEventListener("click", event => {
  if (!event.target || !event.target.closest(".nav-more")) closeNavMoreMenu();
  if (!event.target || event.target.closest(".menu-cell")) return;
  closeStreamActionMenus();
});

document.addEventListener("focusin", event => {
  if (!event.target || !event.target.closest(".nav-more")) closeNavMoreMenu();
  if (event.target && event.target.closest(".menu-cell")) return;
  closeStreamActionMenus();
});

document.getElementById("rescan_devices").addEventListener("click", async () => {
  const button = document.getElementById("rescan_devices");
  setDisabled(button, true);
  try {
    const status = await request("/api/status");
    await loadDevices(status.settings.serial, {force: true});
  } catch (error) {
    setText("device-errors", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("station_search_button").addEventListener("click", async () => {
  try {
    await searchStations();
    setStreamResult("");
  } catch (error) {
    setStreamResult(error.message, "error");
  }
});

document.getElementById("station_search").addEventListener("keydown", async event => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  try {
    await searchStations();
    setStreamResult("");
  } catch (error) {
    setStreamResult(error.message, "error");
  }
});

document.getElementById("station_results").addEventListener("change", chooseStation);

for (const formatControl of document.querySelectorAll("input[name='icecast_format']")) {
  formatControl.addEventListener("change", () => {
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    const station = selectedStation();
    const mount = document.getElementById("icecast_mount").value;
    if (service === STREAM_SERVICE_WEATHERUSA && (!mount.trim() || isWeatherUsaGeneratedMount(mount, station))) {
      setValue("icecast_mount", weatherUsaMount(station, selectedWizardFormat()));
    }
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    renderWizard();
  });
}

document.getElementById("icecast_service").addEventListener("change", event => {
  wizardDirty = true;
  resetWizardForService(event.target.value || STREAM_SERVICE_CUSTOM);
});

for (const id of ["icecast_host", "icecast_port", "icecast_username", "icecast_password", "icecast_mount"]) {
  document.getElementById(id).addEventListener("input", () => {
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    renderWizard();
  });
}

for (const id of ["icecast_sample_rate", "icecast_bitrate", "output_enabled", "icecast_alt_enabled", "icecast_alt_number"]) {
  document.getElementById(id).addEventListener("change", () => {
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    applyServiceControls("icecast", document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM);
    renderWizard();
  });
}

document.getElementById("show_icecast_password").addEventListener("change", event => {
  document.getElementById("icecast_password").type = event.target.checked ? "text" : "password";
});

document.getElementById("settings_show_icecast_password").addEventListener("change", event => {
  document.getElementById("settings_icecast_password").type = event.target.checked ? "text" : "password";
});

for (const id of [
  "settings_icecast_service",
  "settings_icecast_host",
  "settings_icecast_port",
  "settings_icecast_username",
  "settings_icecast_password",
  "settings_icecast_mount",
  "settings_icecast_sample_rate",
  "settings_icecast_bitrate",
  "settings_icecast_alt_enabled",
  "settings_icecast_alt_number"
]) {
  document.getElementById(id).addEventListener("input", updateOutputFormButtons);
  document.getElementById(id).addEventListener("change", event => {
    if (id === "settings_icecast_service") {
      resetSettingsForService(event.target.value || STREAM_SERVICE_CUSTOM);
      return;
    }
    if (id === "settings_icecast_alt_enabled" || id === "settings_icecast_alt_number") {
      applyServiceControls("settings_icecast", document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM);
    }
    updateOutputFormButtons();
  });
}

for (const formatControl of document.querySelectorAll("input[name='settings_icecast_format']")) {
  formatControl.addEventListener("change", () => {
    const service = document.getElementById("settings_icecast_service").value || STREAM_SERVICE_CUSTOM;
    const stream = currentSettingsStream();
    const station = stream ? stream.station : null;
    const mount = document.getElementById("settings_icecast_mount").value;
    if (service === STREAM_SERVICE_WEATHERUSA && (!mount.trim() || isWeatherUsaGeneratedMount(mount, station))) {
      setValue("settings_icecast_mount", weatherUsaMount(station, selectedSettingsFormat()));
    }
    updateOutputFormButtons();
  });
}

document.getElementById("open_add_output").addEventListener("click", beginAddOutput);

document.getElementById("cancel_output_form").addEventListener("click", cancelOutputForm);

document.getElementById("stream_enabled").addEventListener("change", async event => {
  if (!settingsStreamId || applying) return;
  const enabled = event.target.checked;
  setDisabled(event.target, true);
  try {
    await setStreamEnabled(settingsStreamId, enabled, setOutputResult);
  } catch (error) {
    setOutputResult(error.message, "error");
    const stream = currentSettingsStream();
    if (stream) setChecked("stream_enabled", stream.enabled !== false);
  } finally {
    setDisabled(event.target, false);
  }
});

document.getElementById("stream_monitor_enabled").addEventListener("change", async event => {
  if (!settingsStreamId || applying) return;
  setDisabled(event.target, true);
  try {
    if (event.target.checked) {
      await toggleStreamMonitor(settingsStreamId, setStreamSettingsResult);
    } else if (monitorStreamId === settingsStreamId) {
      await stopStreamMonitor();
      setStreamSettingsResult("Monitoring stopped.", "success");
    }
  } catch (error) {
    setStreamSettingsResult(error.message, "error");
  } finally {
    setDisabled(event.target, false);
    renderStreamSettings();
  }
});

document.getElementById("add_output").addEventListener("click", async () => {
  const button = document.getElementById("add_output");
  const icecast = settingsIcecastPayload();
  if (duplicateOutputExists(icecast)) {
    setOutputResult("An output with these credentials already exists.", "error");
    return;
  }
  setDisabled(button, true);
  setOutputResult("Testing Icecast authentication...");
  try {
    const data = await request("/api/stream-output", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({stream_id: settingsStreamId, icecast})
    });
    outputTableSignature = "";
    renderStreams(data.streams || []);
    setOutputResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      cancelOutputForm();
      maybeShowNwrOrgSubmissionNotice(icecast);
    }
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateOutputFormButtons();
  }
});

document.getElementById("save_output_settings").addEventListener("click", async () => {
  const button = document.getElementById("save_output_settings");
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return;
  }
  const icecast = settingsIcecastPayload();
  const oldService = serviceFromIcecast(selected.output.icecast || {});
  if (duplicateOutputExists(icecast, editingOutputId)) {
    setOutputResult("An output with these credentials already exists.", "error");
    return;
  }
  setDisabled(button, true);
  setOutputResult("Saving output settings...");
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: editingOutputId,
        enabled: selected.output.enabled !== false,
        icecast
      })
    });
    renderStreams(data.streams || []);
    setOutputResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      cancelOutputForm();
      if (oldService !== STREAM_SERVICE_NWRORG) maybeShowNwrOrgSubmissionNotice(icecast);
    }
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateOutputFormButtons();
  }
});

document.getElementById("icecast-outputs-body").addEventListener("click", async event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.outputMenu !== undefined) {
    const menu = target.nextElementSibling;
    const shouldOpen = menu.hidden;
    if (shouldOpen) {
      openStreamActionMenu(target, null);
    } else {
      closeStreamActionMenu(menu, false);
    }
    return;
  }
  if (target.dataset.action === "edit-settings-output") {
    closeStreamActionMenus();
    beginEditOutput(target.dataset.outputId);
    return;
  }
  if (target.dataset.action === "toggle-output") {
    closeStreamActionMenus();
    const selected = findConfiguredOutput(target.dataset.streamId, target.dataset.outputId);
    if (!selected) {
      setOutputResult("Stream output was not found.", "error");
      return;
    }
    const nextEnabled = selected.output.enabled === false;
    if (!nextEnabled && !canDisableIcecastOutput(selected.stream, selected.output)) {
      setOutputResult("At least one output must remain enabled for each stream.", "error");
      return;
    }
    try {
      const data = await request("/api/stream-output", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: target.dataset.streamId,
          output_id: target.dataset.outputId,
          enabled: nextEnabled,
          icecast: selected.output.icecast
        })
      });
      renderStreams(data.streams || []);
      setOutputResult(data.message, data.success ? "success" : "error");
    } catch (error) {
      setOutputResult(error.message, "error");
    }
    return;
  }
  if (target.dataset.action === "remove-output") {
    closeStreamActionMenus();
    const selected = findConfiguredOutput(target.dataset.streamId, target.dataset.outputId);
    if (!selected) {
      setOutputResult("Stream output was not found.", "error");
      return;
    }
    if (!canRemoveIcecastOutput(selected.stream, selected.output)) {
      setOutputResult("At least one output must remain enabled for each stream.", "error");
      return;
    }
    try {
      const data = await request(
        `/api/stream-output?stream_id=${encodeURIComponent(target.dataset.streamId)}&output_id=${encodeURIComponent(target.dataset.outputId)}`,
        {method: "DELETE"}
      );
      renderStreams(data.streams || []);
      cancelOutputForm();
      setOutputResult("");
    } catch (error) {
      setOutputResult(error.message, "error");
    }
  }
});

document.getElementById("icecast-outputs-body").addEventListener("keydown", event => {
  const target = event.target;
  if (!target || !target.dataset) return;
  if (target.dataset.outputMenu !== undefined) {
    if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
      event.preventDefault();
      openStreamActionMenu(target, "first");
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      openStreamActionMenu(target, "last");
      return;
    }
  }
  if (target.getAttribute("role") === "menuitem") {
    const menu = target.closest(".stream-actions-menu");
    if (!menu) return;
    const items = menuItems(menu);
    const index = items.indexOf(target);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      items[(index + 1) % items.length].focus();
      return;
    }
    if (event.key === "ArrowUp") {
      event.preventDefault();
      items[(index - 1 + items.length) % items.length].focus();
      return;
    }
    if (event.key === "Home") {
      event.preventDefault();
      items[0].focus();
      return;
    }
    if (event.key === "End") {
      event.preventDefault();
      items[items.length - 1].focus();
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      closeStreamActionMenu(menu, true);
    }
  }
});

document.getElementById("cancel_wizard").addEventListener("click", () => {
  finishWizard();
});

document.getElementById("wizard_back").addEventListener("click", () => {
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (wizardStep === 3 && (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG)) {
    setWizardStep(1);
  } else {
    setWizardStep(wizardStep - 1);
  }
});

document.getElementById("wizard_next").addEventListener("click", async () => {
  if (wizardStep === 0) {
    setWizardStep(1);
    return;
  }
  if (wizardStep === 1) {
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    if (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG) {
      setWizardStep(3);
    } else {
      setWizardStep(2);
    }
    return;
  }
  if (wizardStep === 2) {
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    if (service === STREAM_SERVICE_WEATHERUSA && !document.getElementById("icecast_mount").value.trim()) {
      setValue("icecast_mount", weatherUsaMount(selectedStation(), selectedWizardFormat()));
    }
    setWizardStep(3);
    return;
  }
  if (wizardStep === 3) {
    const button = document.getElementById("wizard_next");
    setDisabled(button, true);
    setStreamResult("Testing Icecast authentication...");
    try {
      const signature = icecastCredentialSignature();
      const data = await request("/api/icecast-auth", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({icecast: streamPayload().icecast})
      });
      setStreamResult(data.message, data.success ? "success" : "error");
      if (data.success) {
        icecastAuthPassed = true;
        icecastAuthSignature = signature;
        const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
        if (service === STREAM_SERVICE_NWRORG) {
          setWizardStep(3);
        } else {
          setWizardStep(4);
        }
      }
    } catch (error) {
      setStreamResult(error.message, "error");
    } finally {
      renderWizard();
    }
    return;
  }
});

document.getElementById("wizard_finish").addEventListener("click", async () => {
  const button = document.getElementById("wizard_finish");
  setDisabled(button, true);
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  setStreamResult(service === STREAM_SERVICE_NWRORG && !icecastAuthPassed ? "Testing Icecast authentication..." : "Creating stream...");
  try {
    if (!icecastAuthPassed || icecastAuthSignature !== icecastCredentialSignature()) {
      const signature = icecastCredentialSignature();
      const auth = await request("/api/icecast-auth", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({icecast: streamPayload().icecast})
      });
      if (!auth.success) {
        setStreamResult(auth.message, "error");
        return;
      }
      icecastAuthPassed = true;
      icecastAuthSignature = signature;
    }
    setStreamResult("Creating stream...");
    const payload = streamPayload();
    const createdIcecast = payload.icecast;
    const data = await request("/api/streams", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    renderStreams(data.streams || []);
    setStreamResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      finishWizard();
      maybeShowNwrOrgSubmissionNotice(createdIcecast);
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
});

function sendLiveAudioStopBeacon() {
  if (unloadLiveAudioStopSent || !navigator.sendBeacon) return;
  unloadLiveAudioStopSent = true;
  if (monitorStreamId && navigator.sendBeacon) {
    const payload = JSON.stringify({client_id: pageMonitorClientId()});
    navigator.sendBeacon("/api/monitor/stop", new Blob([payload], {type: "application/json"}));
  }
  if (receiverPeerConnection && navigator.sendBeacon) {
    const payload = JSON.stringify({client_id: pageReceiverClientId()});
    navigator.sendBeacon("/api/receiver/stop", new Blob([payload], {type: "application/json"}));
  }
}

window.addEventListener("pagehide", () => {
  sendLiveAudioStopBeacon();
});

window.addEventListener("beforeunload", event => {
  sendLiveAudioStopBeacon();
  if (!hasUnsavedNavigationState()) return;
  event.preventDefault();
  event.returnValue = "";
});

document.getElementById("cancel_output_edit").addEventListener("click", () => {
  setOutputEditMode(false);
  wizardDirty = false;
  setStreamResult("");
  navigateTo("streams");
});

document.getElementById("save_output").addEventListener("click", async () => {
  const button = document.getElementById("save_output");
  setDisabled(button, true);
  setStreamResult("Saving stream output settings...");
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(outputEditPayload())
    });
    renderStreams(data.streams || []);
    setStreamResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      wizardDirty = false;
      setOutputEditMode(false);
      navigateTo("streams");
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("streams-list").addEventListener("click", async event => {
  const button = event.target;
  if (!button || !button.dataset || !button.dataset.streamId) return;
  if (button.dataset.action === "edit-output") {
    editOutput(button.dataset.streamId, button.dataset.outputId);
    return;
  }
  if (button.dataset.action !== "remove-stream") return;
  try {
    const data = await request(`/api/streams?id=${encodeURIComponent(button.dataset.streamId)}`, {
      method: "DELETE"
    });
    renderStreams(data.streams || []);
    setStreamResult("");
  } catch (error) {
    setStreamResult(error.message, "error");
  }
});

document.getElementById("eas_alert_stream").addEventListener("change", event => {
  easAlertStreamId = event.target.value;
  easAlertPage = 1;
  easAlertListSignature = "";
  easBulkOptionsSignature = "";
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
});

document.getElementById("open_eas_export").addEventListener("click", () => {
  easBulkOptionsSignature = "";
  navigateTo("eas_alert_export", {streamId: easAlertStreamId, page: easAlertPage});
});

document.getElementById("open_eas_delete").addEventListener("click", () => {
  if (accountIsReadOnly()) return;
  easBulkOptionsSignature = "";
  navigateTo("eas_alert_delete", {streamId: easAlertStreamId, page: easAlertPage});
});

document.getElementById("eas_export_options").addEventListener("change", () => updateManualRangeVisibility("export"));
document.getElementById("eas_delete_options").addEventListener("change", () => updateManualRangeVisibility("delete"));

document.getElementById("eas_export_manual").addEventListener("click", event => {
  if (event.target && event.target.dataset && event.target.dataset.bulkNow) setEndRangeToNow(event.target.dataset.bulkNow);
});

document.getElementById("eas_delete_manual").addEventListener("click", event => {
  if (event.target && event.target.dataset && event.target.dataset.bulkNow) setEndRangeToNow(event.target.dataset.bulkNow);
});

document.getElementById("eas_export_alerts").addEventListener("click", exportEasAlerts);
document.getElementById("eas_delete_alerts").addEventListener("click", deleteEasAlerts);
document.getElementById("cancel_eas_export").addEventListener("click", () => {
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
});
document.getElementById("cancel_eas_delete").addEventListener("click", () => {
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
});

document.getElementById("eas_alert_prev").addEventListener("click", () => {
  if (easAlertPage <= 1) return;
  easAlertPage -= 1;
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
});

document.getElementById("eas_alert_next").addEventListener("click", () => {
  if (easAlertPage >= easAlertTotalPages) return;
  easAlertPage += 1;
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertPage});
});

document.getElementById("eas-alert-list").addEventListener("click", event => {
  const link = event.target && event.target.closest ? event.target.closest("a[data-alert-id]") : null;
  if (!link) return;
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  easAlertDetailId = link.dataset.alertId;
  easAlertReturnPage = easAlertPage;
  navigateTo("eas_alert_detail", {
    streamId: easAlertStreamId,
    alertId: easAlertDetailId,
    page: easAlertReturnPage
  });
});

document.getElementById("back_to_eas_alerts").addEventListener("click", () => {
  navigateTo("eas_alerts", {streamId: easAlertStreamId, page: easAlertReturnPage});
});

document.getElementById("remove_eas_alert").addEventListener("click", async () => {
  if (accountIsReadOnly()) return;
  if (!window.confirm("Remove this EAS alert and its audio file?")) return;
  await removeCurrentEasAlert();
});

async function refresh() {
  const data = await request("/api/status");
  applyStatus(data, {syncControls: false});
}

(async function init() {
  populateBitrates();
  populateIqSampleRates();
  selectAudioEffect("volume", false);
  renderReceiverControls();
  loadWebRtcSupport();
  const data = await request("/api/status");
  if (!data.account || !data.account.read_only) {
    await loadDevices(data.settings.serial);
  }
  await searchStations();
  await loadStreams();
  await loadEasAlertStreams({preserve: true, quiet: true});
  await loadIqRecordings();
  applyStatus(data, {syncControls: true});
  const initialRoute = routeFromLocation();
  navigateTo(initialRoute.view, {
    streamId: initialRoute.streamId,
    alertId: initialRoute.alertId,
    recordingId: initialRoute.recordingId,
    page: initialRoute.page
  }, true);
  setInterval(refresh, 1000);
  setInterval(async () => {
    try {
      const status = await request("/api/status");
      if (status.account && status.account.read_only) {
        setText("device-errors", "");
        return;
      }
      await loadDevices(status.settings.serial);
    } catch (error) {
      setText("device-errors", error.message);
    }
  }, 5000);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
