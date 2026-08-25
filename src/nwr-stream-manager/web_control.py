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
    from .alsa import (
        ALSA_CHANNEL_BOTH,
        ALSA_CHANNEL_LEFT,
        ALSA_CHANNEL_MODES,
        ALSA_CHANNEL_RIGHT,
        ALSA_STREAM_DEFAULT_SAMPLE_RATE,
        AlsaSharedInputConfig,
        AlsaSharedPlaybackTap,
        AlsaStreamPlaybackTap,
        AlsaStreamTapConfig,
        discover_playback_devices,
        playback_device_usb_node,
    )
    from .config import (
        AUDIO_NYQUIST_HZ,
        AudioConfig,
        EasRecordingConfig,
        IcecastConfig,
        IQ_SAMPLE_RATE,
        parse_audio_config,
    )
    from .device_probe import SharedDeviceProbe
    from .dsp import (
        ComplexArray,
        DEFAULT_ALIAS_ATTENUATION_DB,
        IqChannelizer,
        complex64_to_interleaved_f32,
        create_decimator,
        update_decimator_alias_filter,
    )
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
        reset_usb_device_node,
        reset_usb_rtl_device,
        rtl_u8_to_complex64,
        validate_ppm_correction,
    )
    from .same_data import lookup_event, lookup_location
    from .same_live import SameEventQueue, SameSuppressionProcessor
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
    alsa_module = importlib.import_module(f"{package_name}.alsa")
    config_module = importlib.import_module(f"{package_name}.config")
    device_probe_module = importlib.import_module(f"{package_name}.device_probe")
    dsp = importlib.import_module(f"{package_name}.dsp")
    eas_recording = importlib.import_module(f"{package_name}.eas_recording")
    encoder = importlib.import_module(f"{package_name}.encoder")
    fallback_audio = importlib.import_module(f"{package_name}.fallback_audio")
    icecast_module = importlib.import_module(f"{package_name}.icecast")
    nfm = importlib.import_module(f"{package_name}.nfm")
    rtl = importlib.import_module(f"{package_name}.rtl")
    same_data = importlib.import_module(f"{package_name}.same_data")
    same_live = importlib.import_module(f"{package_name}.same_live")
    webrtc = importlib.import_module(f"{package_name}.webrtc")
    AudioEffectsProcessor = audio_effects.AudioEffectsProcessor
    deemphasis_makeup_gain = audio_effects.deemphasis_makeup_gain
    ALSA_CHANNEL_BOTH = alsa_module.ALSA_CHANNEL_BOTH
    ALSA_CHANNEL_LEFT = alsa_module.ALSA_CHANNEL_LEFT
    ALSA_CHANNEL_MODES = alsa_module.ALSA_CHANNEL_MODES
    ALSA_CHANNEL_RIGHT = alsa_module.ALSA_CHANNEL_RIGHT
    ALSA_STREAM_DEFAULT_SAMPLE_RATE = alsa_module.ALSA_STREAM_DEFAULT_SAMPLE_RATE
    AlsaSharedInputConfig = alsa_module.AlsaSharedInputConfig
    AlsaSharedPlaybackTap = alsa_module.AlsaSharedPlaybackTap
    AlsaStreamPlaybackTap = alsa_module.AlsaStreamPlaybackTap
    AlsaStreamTapConfig = alsa_module.AlsaStreamTapConfig
    discover_playback_devices = alsa_module.discover_playback_devices
    playback_device_usb_node = alsa_module.playback_device_usb_node
    SharedDeviceProbe = device_probe_module.SharedDeviceProbe
    AUDIO_NYQUIST_HZ = config_module.AUDIO_NYQUIST_HZ
    AudioConfig = config_module.AudioConfig
    EasRecordingConfig = config_module.EasRecordingConfig
    IcecastConfig = config_module.IcecastConfig
    IQ_SAMPLE_RATE = config_module.IQ_SAMPLE_RATE
    parse_audio_config = config_module.parse_audio_config
    ComplexArray = dsp.ComplexArray
    DEFAULT_ALIAS_ATTENUATION_DB = dsp.DEFAULT_ALIAS_ATTENUATION_DB
    IqChannelizer = dsp.IqChannelizer
    complex64_to_interleaved_f32 = dsp.complex64_to_interleaved_f32
    create_decimator = dsp.create_decimator
    update_decimator_alias_filter = dsp.update_decimator_alias_filter
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
    reset_usb_device_node = rtl.reset_usb_device_node
    reset_usb_rtl_device = rtl.reset_usb_rtl_device
    rtl_u8_to_complex64 = rtl.rtl_u8_to_complex64
    validate_ppm_correction = rtl.validate_ppm_correction
    lookup_event = same_data.lookup_event
    lookup_location = same_data.lookup_location
    SameEventQueue = same_live.SameEventQueue
    SameSuppressionProcessor = same_live.SameSuppressionProcessor
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
STREAM_SILENCE_FLOAT_FRAME = np.zeros(STREAM_FRAME_SAMPLES, dtype=np.float32)
RTL_RESET_COMMAND_TIMEOUT_SECONDS = 5.0
RTL_RESET_REAPPEAR_TIMEOUT_SECONDS = 10.0
STREAM_WORKER_RAW_QUEUE_SECONDS = 1.50
STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS = 8
STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS = 96
LIVE_IQ_QUEUE_SECONDS = 0.75
INTERMEDIATE_SOURCE_QUEUE_CHUNKS = 2
IQ_RECORDER_QUEUE_CHUNKS = 2
INTERMEDIATE_IQ_SAMPLE_RATE = 192_000
INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ = 8_000.0
INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB = 70.0
CHANNEL_IQ_ALIAS_TRANSITION_HZ = 1_000.0
ALIAS_FILTER_STRENGTH_MIN = 0
ALIAS_FILTER_STRENGTH_MAX = 100
ALIAS_FILTER_STRENGTH_DEFAULT = 100
ALIAS_FILTER_MIN_ATTENUATION_DB = 20.0
ALIAS_FILTER_MAX_TRANSITION_SCALE = 3.0
IQ_RECORDER_RAW_QUEUE_SECONDS = 2.0
IQ_DOWNLOAD_CHUNK_BYTES = 256 * 1024
IQ_DOWNLOAD_YIELD_SECONDS = 0.001
IQ_RECORDER_MODE_STREAM = "stream"
IQ_RECORDER_MODE_SPECTRUM = "spectrum"
IQ_RECORDER_SAMPLE_RATES = (192_000, 256_000, 384_000, 512_000, 768_000, 1_024_000, DEFAULT_RTL_SAMPLE_RATE)
IQ_TEST_SOURCES_DIRECTORY_NAME = "iq-test-sources"
IQ_TEST_SOURCE_CHUNK_SECONDS = 0.05
IQ_TEST_SOURCE_MIN_SAMPLE_RATE = INTERMEDIATE_IQ_SAMPLE_RATE
IQ_RECORDER_DEFAULT_DURATION_SECONDS = 0
IQ_RECORDER_MIN_DURATION_SECONDS = 0
IQ_RECORDER_MAX_DURATION_SECONDS = 24 * 60 * 60
IQ_STORAGE_ESTIMATE_MIN_CORRECTION_SECONDS = 30.0
IQ_STORAGE_ESTIMATE_MAX_CORRECTION_SECONDS = 300.0
IQ_STORAGE_ESTIMATE_CORRECTION_FRACTION = 0.002
STREAM_IDLE_DETECTION_SECONDS = 1.0
STREAM_RECONNECT_SECONDS = 5.0
ICECAST_AUTH_CACHE_SECONDS = 600.0
SOUNDCARD_PREVIEW_TIMEOUT_SECONDS = 8.0
SOUNDCARD_PREVIEW_CLEANUP_SECONDS = 2.0
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
    alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT

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


@dataclass(frozen=True)
class IqFileSourceConfig:
    path: Path
    sample_rate: int
    center_frequency_hz: int = NWR_CENTER_FREQUENCY_HZ


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


def validate_alias_filter_strength(value: Any) -> int:
    strength = int(value)
    if not ALIAS_FILTER_STRENGTH_MIN <= strength <= ALIAS_FILTER_STRENGTH_MAX:
        raise ValueError(
            f"alias_filter_strength must be between {ALIAS_FILTER_STRENGTH_MIN} and {ALIAS_FILTER_STRENGTH_MAX}"
        )
    return strength


def alias_filter_transition_hz(base_transition_hz: float, strength: int | float) -> float:
    strength = validate_alias_filter_strength(strength)
    weak_fraction = 1.0 - (float(strength) / float(ALIAS_FILTER_STRENGTH_MAX))
    scale = 1.0 + (ALIAS_FILTER_MAX_TRANSITION_SCALE - 1.0) * weak_fraction * weak_fraction
    return float(base_transition_hz) * scale


def alias_filter_attenuation_db(base_attenuation_db: float, strength: int | float) -> float:
    strength = validate_alias_filter_strength(strength)
    strong_fraction = float(strength) / float(ALIAS_FILTER_STRENGTH_MAX)
    return ALIAS_FILTER_MIN_ATTENUATION_DB + (
        float(base_attenuation_db) - ALIAS_FILTER_MIN_ATTENUATION_DB
    ) * strong_fraction


def iq_batch_byte_count(batch: RtlSampleBatch | IqSampleBatch) -> int:
    data = batch.data
    if isinstance(data, np.ndarray):
        return int(data.nbytes)
    return len(data)


def iq_batch_sample_count(batch: RtlSampleBatch | IqSampleBatch) -> int:
    data = batch.data
    if isinstance(data, np.ndarray):
        return int(data.size)
    return len(data) // 2


def iq_batch_sample_count_from_bytes(byte_count: int, source: Any) -> int:
    if isinstance(source, IqFileCaptureSource):
        return int(byte_count) // 8
    return int(byte_count) // 2


def clear_queue_items(target: queue.Queue) -> int:
    cleared = 0
    while True:
        try:
            target.get_nowait()
            cleared += 1
        except queue.Empty:
            return cleared


def set_batch_generation(batch: Any, generation: int) -> Any:
    try:
        setattr(batch, "source_generation", int(generation))
    except Exception:
        object.__setattr__(batch, "source_generation", int(generation))
    return batch


def batch_generation(batch: Any) -> int:
    return int(getattr(batch, "source_generation", 0))


class IqFileCaptureSource:
    def __init__(
        self,
        config: IqFileSourceConfig,
        *,
        chunk_seconds: float = IQ_TEST_SOURCE_CHUNK_SECONDS,
    ) -> None:
        if int(config.sample_rate) < IQ_TEST_SOURCE_MIN_SAMPLE_RATE:
            raise ValueError(
                f"I/Q file sample rate must be at least {IQ_TEST_SOURCE_MIN_SAMPLE_RATE} S/s"
            )
        self.config = config
        self.chunk_seconds = max(0.01, float(chunk_seconds))
        self.output_queue: queue.Queue[IqSampleBatch | Exception | None] = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.stats_lock = threading.Lock()
        self.offered_batches = 0
        self.offered_bytes = 0
        self.dropped_batches = 0
        self.dropped_bytes = 0
        self.last_offer_at = 0.0
        self.file_size_bytes = 0
        self.seek_lock = threading.Lock()
        self.pending_seek_samples = 0
        self.current_sample_offset = 0

    @property
    def label(self) -> str:
        return self.config.path.name

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        path = self.config.path
        if not path.is_file():
            raise FileNotFoundError(f"I/Q source file was not found: {path.name}")
        self.file_size_bytes = path.stat().st_size
        if self.file_size_bytes < 8:
            raise ValueError("I/Q source file must contain at least one complex float32 sample")
        if self.file_size_bytes % 8 != 0:
            LOG.warning(
                "I/Q source file %s has trailing bytes that do not form a complete complex float32 sample",
                path,
            )
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="iq-file-source", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._offer(None)
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def read(self, timeout: float | None = None) -> IqSampleBatch:
        item = self.output_queue.get(timeout=timeout)
        if item is None:
            raise EOFError("I/Q file source stopped")
        if isinstance(item, Exception):
            raise item
        return item

    def stats(self) -> dict[str, int | float | None]:
        with self.stats_lock:
            duration_seconds = (
                (self.file_size_bytes // 8) / float(self.config.sample_rate)
                if self.file_size_bytes > 0 and self.config.sample_rate > 0
                else 0.0
            )
            return {
                "offered_batches": self.offered_batches,
                "offered_bytes": self.offered_bytes,
                "offered_samples": self.offered_bytes // 8,
                "dropped_batches": self.dropped_batches,
                "dropped_bytes": self.dropped_bytes,
                "dropped_samples": self.dropped_bytes // 8,
                "queue_depth": self.output_queue.qsize(),
                "queue_capacity": self.output_queue.maxsize,
                "file_size_bytes": self.file_size_bytes,
                "position_samples": self.current_sample_offset,
                "position_seconds": round(self.current_sample_offset / float(self.config.sample_rate), 3),
                "duration_seconds": round(duration_seconds, 3),
                "last_offer_age_seconds": (
                    round(time.monotonic() - self.last_offer_at, 3)
                    if self.last_offer_at
                    else None
                ),
            }

    def get_gain_values(self) -> list[float]:
        return []

    def seek_relative(self, seconds: float) -> dict[str, int | float]:
        delta_samples = int(round(float(seconds) * float(self.config.sample_rate)))
        with self.seek_lock:
            self.pending_seek_samples += delta_samples
        self._clear_output_queue()
        return self.stats()

    @staticmethod
    def _rtl_async_buffer_size(config: IqFileSourceConfig) -> int:
        sample_rate = max(1, int(config.sample_rate))
        samples = max(1, int(round(sample_rate * IQ_TEST_SOURCE_CHUNK_SECONDS)))
        return samples * 8

    def _run(self) -> None:
        samples_per_chunk = max(1, int(round(self.config.sample_rate * self.chunk_seconds)))
        floats_per_chunk = samples_per_chunk * 2
        chunk_bytes = floats_per_chunk * 4
        next_emit_at = time.monotonic()
        try:
            with self.config.path.open("rb") as handle:
                while not self.stop_event.is_set():
                    self._apply_pending_seek(handle)
                    raw = self._read_looping_chunk(handle, chunk_bytes)
                    if not raw:
                        raise ValueError("I/Q source file is empty")
                    float_count = len(raw) // 4
                    if float_count < 2:
                        continue
                    if float_count % 2:
                        raw = raw[: (float_count - 1) * 4]
                    values = np.frombuffer(raw, dtype="<f4")
                    iq = (values[0::2] + 1j * values[1::2]).astype(np.complex64, copy=False)
                    if iq.size == 0:
                        continue
                    self._offer(
                        IqSampleBatch(
                            data=iq,
                            sample_rate=self.config.sample_rate,
                            center_frequency_hz=self.config.center_frequency_hz,
                        )
                    )
                    with self.stats_lock:
                        self.current_sample_offset = handle.tell() // 8
                    next_emit_at += float(iq.size) / float(self.config.sample_rate)
                    delay = next_emit_at - time.monotonic()
                    if delay > 0:
                        self.stop_event.wait(delay)
                    elif delay < -1.0:
                        next_emit_at = time.monotonic()
        except Exception as exc:
            if not self.stop_event.is_set():
                self._offer(exc)

    def _apply_pending_seek(self, handle) -> None:
        with self.seek_lock:
            delta_samples = self.pending_seek_samples
            self.pending_seek_samples = 0
        if not delta_samples:
            return
        total_samples = self.file_size_bytes // 8
        if total_samples <= 0:
            return
        current_samples = handle.tell() // 8
        target_samples = current_samples + delta_samples
        if target_samples >= total_samples:
            target_samples = 0
        elif target_samples < 0:
            target_samples = 0
        handle.seek(target_samples * 8)
        with self.stats_lock:
            self.current_sample_offset = target_samples
        LOG.info(
            "seeked I/Q test source %s to %.3f seconds",
            self.config.path.name,
            target_samples / float(self.config.sample_rate),
        )

    def _read_looping_chunk(self, handle, size: int) -> bytes:
        parts: list[bytes] = []
        remaining = size
        while remaining > 0 and not self.stop_event.is_set():
            data = handle.read(remaining)
            if data:
                parts.append(data)
                remaining -= len(data)
                continue
            handle.seek(0)
            if not parts and handle.tell() != 0:
                break
            if self.file_size_bytes <= 0:
                break
            if self.file_size_bytes < remaining and parts:
                break
        return b"".join(parts)

    def _offer(self, item: IqSampleBatch | Exception | None) -> None:
        try:
            self.output_queue.put_nowait(item)
            if isinstance(item, IqSampleBatch):
                self._record_offered_item(item)
            return
        except queue.Full:
            pass
        try:
            dropped = self.output_queue.get_nowait()
            if isinstance(dropped, IqSampleBatch):
                self._record_dropped_item(dropped)
        except queue.Empty:
            pass
        try:
            self.output_queue.put_nowait(item)
            if isinstance(item, IqSampleBatch):
                self._record_offered_item(item)
        except queue.Full:
            if isinstance(item, IqSampleBatch):
                self._record_dropped_item(item)

    def _clear_output_queue(self) -> None:
        while True:
            try:
                dropped = self.output_queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(dropped, IqSampleBatch):
                self._record_dropped_item(dropped)

    def _record_offered_item(self, item: IqSampleBatch) -> None:
        with self.stats_lock:
            self.offered_batches += 1
            self.offered_bytes += item.data.nbytes
            self.last_offer_at = time.monotonic()

    def _record_dropped_item(self, item: IqSampleBatch) -> None:
        with self.stats_lock:
            self.dropped_batches += 1
            self.dropped_bytes += item.data.nbytes


class RawRtlFanout:
    def __init__(self, source: RtlCaptureSource | IqFileCaptureSource) -> None:
        self.source = source
        self.source_lock = threading.Lock()
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
        self.generation = 0

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

    def set_source(self, source: RtlCaptureSource | IqFileCaptureSource) -> None:
        with self.source_lock:
            if source is self.source:
                return
            self.source = source
        with self.subscribers_lock:
            self.generation += 1
            cleared = 0
            for subscriber in self.subscribers:
                cleared += clear_queue_items(subscriber)
        LOG.info("RTL-SDR raw fanout switched source generation to %s; cleared %s queued batches", self.generation, cleared)

    def _chunks_for_seconds(self, seconds: float) -> int:
        with self.source_lock:
            source = self.source
        sample_rate = max(1, int(source.config.sample_rate))
        chunk_bytes = max(512, source._rtl_async_buffer_size(source.config))
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
            with self.source_lock:
                source = self.source
            try:
                batch = source.read(timeout=0.5)
            except queue.Empty:
                continue
            except EOFError:
                with self.source_lock:
                    if source is self.source:
                        return
                continue
            except Exception as exc:
                LOG.warning("RTL-SDR raw fanout read failed: %s", exc)
                continue
            with self.subscribers_lock:
                subscribers = list(self.subscribers)
                generation = self.generation
                self.read_batches += 1
                self.read_bytes += iq_batch_byte_count(batch)
                self.last_read_at = time.monotonic()
            batch = set_batch_generation(batch, generation)
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
            with self.source_lock:
                source = self.source
            subscribers = [self._subscriber_stats_locked(subscriber) for subscriber in self.subscribers]
            return {
                "read_batches": self.read_batches,
                "read_bytes": self.read_bytes,
                "read_samples": iq_batch_sample_count_from_bytes(self.read_bytes, source),
                "subscriber_count": len(self.subscribers),
                "total_dropped_batches": self.total_dropped_batches,
                "total_dropped_bytes": self.total_dropped_bytes,
                "total_dropped_samples": iq_batch_sample_count_from_bytes(self.total_dropped_bytes, source),
                "last_read_age_seconds": (
                    round(time.monotonic() - self.last_read_at, 3)
                    if self.last_read_at
                    else None
                ),
                "subscribers": subscribers,
            }

    def _subscriber_stats_locked(self, subscriber: queue.Queue) -> dict[str, Any]:
        with self.source_lock:
            source = self.source
        return {
            "name": self.subscriber_names.get(subscriber, "subscriber"),
            "queue_depth": subscriber.qsize(),
            "queue_capacity": subscriber.maxsize,
            "max_queue_depth": self.subscriber_max_depth.get(subscriber, 0),
            "dropped_batches": self.subscriber_drops.get(subscriber, 0),
            "dropped_bytes": self.subscriber_drop_bytes.get(subscriber, 0),
            "dropped_samples": iq_batch_sample_count_from_bytes(
                self.subscriber_drop_bytes.get(subscriber, 0),
                source,
            ),
        }

    def _record_subscriber_depth(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_max_depth[subscriber] = max(
                self.subscriber_max_depth.get(subscriber, 0),
                subscriber.qsize(),
            )

    def _record_subscriber_drop(self, subscriber: queue.Queue, batch: RtlSampleBatch | IqSampleBatch) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_drops[subscriber] = self.subscriber_drops.get(subscriber, 0) + 1
            bytes_count = iq_batch_byte_count(batch)
            self.subscriber_drop_bytes[subscriber] = self.subscriber_drop_bytes.get(subscriber, 0) + bytes_count
            self.total_dropped_batches += 1
            self.total_dropped_bytes += bytes_count
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
    def __init__(
        self,
        raw_fanout: RawRtlFanout,
        output_rate: int = INTERMEDIATE_IQ_SAMPLE_RATE,
        *,
        alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT,
    ) -> None:
        self.raw_fanout = raw_fanout
        self.output_rate = int(output_rate)
        self.alias_filter_strength = validate_alias_filter_strength(alias_filter_strength)
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
        self.generation = 0

    def set_alias_filter_strength(self, value: int) -> None:
        value = validate_alias_filter_strength(value)
        with self.subscribers_lock:
            self.alias_filter_strength = value

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

    def _chunks_for_seconds(self, seconds: float) -> int:
        raw_chunks_for_seconds = getattr(self.raw_fanout, "_chunks_for_seconds", None)
        if callable(raw_chunks_for_seconds):
            return int(raw_chunks_for_seconds(seconds))
        return max(
            STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS,
            min(
                STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS,
                int(math.ceil(max(0.0, seconds) / 0.10)),
            ),
        )

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
                "alias_filter_strength": self.alias_filter_strength,
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
        decimator_alias_filter_strength: int | None = None
        raw_generation = getattr(self.raw_fanout, "generation", 0)
        last_slow_batch_log_at = 0.0
        while not self.stop_event.is_set():
            try:
                raw_batch: RtlSampleBatch | IqSampleBatch = self.raw_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                LOG.warning("intermediate IQ fanout failed to read RTL-SDR samples: %s", exc)
                continue
            current_raw_generation = getattr(self.raw_fanout, "generation", raw_generation)
            incoming_generation = batch_generation(raw_batch)
            if incoming_generation < current_raw_generation:
                continue
            if incoming_generation != raw_generation:
                raw_generation = incoming_generation
                dc_blocker = None
                decimator = None
                decimator_key = None
                decimator_alias_filter_strength = None
                with self.subscribers_lock:
                    self.generation += 1
                    cleared = 0
                    for subscriber in self.subscribers:
                        cleared += clear_queue_items(subscriber)
                LOG.info(
                    "intermediate IQ fanout switched source generation to %s; cleared %s queued batches",
                    self.generation,
                    cleared,
                )
            with self.subscribers_lock:
                alias_filter_strength = self.alias_filter_strength
            transition_hz = alias_filter_transition_hz(
                INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
                alias_filter_strength,
            )
            attenuation_db = alias_filter_attenuation_db(
                INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
                alias_filter_strength,
            )
            key = (raw_batch.sample_rate, self.output_rate)
            if decimator is None or decimator_key != key:
                dc_blocker = IqDcBlocker(raw_batch.sample_rate)
                decimator = create_decimator(
                    raw_batch.sample_rate,
                    self.output_rate,
                    transition_hz=transition_hz,
                    attenuation_db=attenuation_db,
                )
                decimator_key = key
                decimator_alias_filter_strength = alias_filter_strength
            elif decimator_alias_filter_strength != alias_filter_strength:
                update_decimator_alias_filter(
                    decimator,
                    raw_batch.sample_rate,
                    self.output_rate,
                    transition_hz=transition_hz,
                    attenuation_db=attenuation_db,
                )
                decimator_alias_filter_strength = alias_filter_strength
            iq = iq_batch_complex(raw_batch)
            if iq.size == 0:
                continue
            if dc_blocker is None:
                dc_blocker = IqDcBlocker(raw_batch.sample_rate)
            process_started_at = time.monotonic()
            output = decimator.process(dc_blocker.process(iq))
            process_seconds = time.monotonic() - process_started_at
            batch_seconds = float(iq.size) / float(max(1, raw_batch.sample_rate))
            if process_seconds > batch_seconds and process_started_at - last_slow_batch_log_at >= 10.0:
                last_slow_batch_log_at = process_started_at
                LOG.warning(
                    "intermediate IQ decimator is slower than realtime: source_rate=%s output_rate=%s processed %.3fs IQ in %.3fs",
                    raw_batch.sample_rate,
                    self.output_rate,
                    batch_seconds,
                    process_seconds,
                )
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
            batch = set_batch_generation(batch, self.generation)
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


def fanout_channel_profile(fanout: RawRtlFanout | IntermediateIqFanout) -> tuple[int, int] | None:
    if not isinstance(fanout, IntermediateIqFanout):
        return None
    center_frequency_hz = NWR_CENTER_FREQUENCY_HZ
    raw_fanout = getattr(fanout, "raw_fanout", None)
    source = getattr(raw_fanout, "source", None)
    config = getattr(source, "config", None)
    if config is not None:
        center_frequency_hz = int(getattr(config, "center_frequency_hz", center_frequency_hz))
    return int(fanout.output_rate), center_frequency_hz


def make_live_channelizer(
    *,
    fanout: RawRtlFanout | IntermediateIqFanout,
    target_frequency_hz: int,
    alias_filter_strength: int,
) -> tuple[IqChannelizer, tuple[int, int], int] | None:
    profile = fanout_channel_profile(fanout)
    if profile is None:
        return None
    input_rate, center_frequency_hz = profile
    transition_hz = alias_filter_transition_hz(
        CHANNEL_IQ_ALIAS_TRANSITION_HZ,
        alias_filter_strength,
    )
    attenuation_db = alias_filter_attenuation_db(
        DEFAULT_ALIAS_ATTENUATION_DB,
        alias_filter_strength,
    )
    return (
        IqChannelizer(
            input_rate=input_rate,
            center_frequency_hz=center_frequency_hz,
            target_frequency_hz=target_frequency_hz,
            output_rate=IQ_SAMPLE_RATE,
            transition_hz=transition_hz,
            alias_attenuation_db=attenuation_db,
        ),
        (input_rate, center_frequency_hz),
        alias_filter_strength,
    )


def drain_queue_to_latest(source: queue.Queue, first_item: Any) -> Any:
    latest = first_item
    while True:
        try:
            latest = source.get_nowait()
        except queue.Empty:
            return latest


def iq_batch_complex(batch: RtlSampleBatch | IqSampleBatch) -> ComplexArray:
    data = batch.data
    if isinstance(data, np.ndarray):
        return data.astype(np.complex64, copy=False)
    return rtl_u8_to_complex64(data)


class ComplexNfmDemodulator:
    def __init__(self) -> None:
        self.previous_sample: np.complex64 | None = None

    def reset(self) -> None:
        self.previous_sample = None

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
    alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT,
) -> dict[str, Any]:
    dc_blocker = IqDcBlocker(sample_rate)
    channelizer = IqChannelizer(
        input_rate=sample_rate,
        center_frequency_hz=center_frequency_hz,
        target_frequency_hz=frequency_hz,
        output_rate=IQ_SAMPLE_RATE,
        transition_hz=alias_filter_transition_hz(CHANNEL_IQ_ALIAS_TRANSITION_HZ, alias_filter_strength),
        alias_attenuation_db=alias_filter_attenuation_db(
            DEFAULT_ALIAS_ATTENUATION_DB,
            alias_filter_strength,
        ),
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


def load_web_fallback_audio(sample_rate: int = IQ_SAMPLE_RATE):
    audio = load_fallback_audio(None)
    if audio.sample_rate == sample_rate:
        return audio
    resampler = PcmResampler(audio.sample_rate, sample_rate)
    pcm = resampler.process(audio.pcm) + resampler.flush()
    return replace(
        audio,
        sample_rate=sample_rate,
        pcm=pcm,
        duration_seconds=len(pcm) / 2 / sample_rate,
    )


def next_web_fallback_frame(audio, state: WebFallbackPlaybackState, loop_delay_seconds: float) -> bytes:
    frame_bytes = round(audio.sample_rate * STREAM_FRAME_SECONDS) * 2
    if not audio.pcm:
        return b"\x00" * frame_bytes
    output = bytearray()
    delay_samples = round(max(0.0, loop_delay_seconds) * audio.sample_rate)
    while len(output) < frame_bytes:
        if state.delay_samples_remaining > 0:
            remaining_samples = (frame_bytes - len(output)) // 2
            silence_samples = min(remaining_samples, state.delay_samples_remaining)
            output.extend(b"\x00\x00" * silence_samples)
            state.delay_samples_remaining -= silence_samples
            continue
        if state.position >= len(audio.pcm):
            state.position = 0
            if delay_samples > 0:
                state.delay_samples_remaining = delay_samples
                continue
        chunk_size = min(frame_bytes - len(output), len(audio.pcm) - state.position)
        output.extend(audio.pcm[state.position : state.position + chunk_size])
        state.position += chunk_size
    return bytes(output)


def pcm_s16le_to_float32(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.empty(0, dtype=np.float32)
    return (np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).astype(np.float32, copy=False)


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


class SameAwareWebRtcAudioSource:
    def __init__(self, *, sample_rate: int = IQ_SAMPLE_RATE, event_queue: SameEventQueue | None = None) -> None:
        self.audio_source = WebRtcAudioSource(sample_rate=sample_rate)
        self.sample_rate = sample_rate
        self.frame_bytes = self.audio_source.frame_bytes
        self.event_queue = event_queue
        self.suppressor = (
            SameSuppressionProcessor(
                sample_rate=sample_rate,
                event_sink=event_queue.put,
            )
            if event_queue is not None
            else None
        )

    def push_pcm(self, pcm: bytes) -> None:
        if self.suppressor is None:
            self.audio_source.push_pcm(pcm)
            return
        for frame in self.suppressor.process_pcm(pcm):
            self.audio_source.push_pcm(frame)

    async def read_pcm(self, timeout: float = 0.25) -> bytes:
        return await self.audio_source.read_pcm(timeout=timeout)

    def get_latest_pcm(self, timeout: float = 0.02) -> bytes:
        return self.audio_source.get_latest_pcm(timeout=timeout)

    def stats(self) -> dict[str, Any]:
        stats = self.audio_source.stats()
        suppressor = self.suppressor
        if suppressor is not None:
            stats["same_suppression_state"] = suppressor.state
            stats["same_suppression_pending_frames"] = len(suppressor.pending)
        return stats

    def close(self) -> None:
        if self.suppressor is not None:
            self.suppressor.close()
        self.audio_source.close()


class SharedSoundcardOutputManager:
    def __init__(self, *, devices_provider=None) -> None:
        self.devices_provider = devices_provider or discover_playback_devices
        self.lock = threading.RLock()
        self.sessions: dict[str, Any] = {}
        self.outputs: dict[str, dict[str, Any]] = {}

    def sync_output(self, output_id: str, soundcard: dict[str, Any]) -> None:
        output_id = str(output_id)
        config = soundcard_tap_config_from_soundcard(soundcard)
        input_config = AlsaSharedInputConfig(
            channel_mode=config.channel_mode,
            software_volume=config.software_volume,
        )
        with self.lock:
            old = self.outputs.get(output_id)
            if old is not None and old["stable_id"] != config.stable_id:
                old_session = self.sessions.get(old["stable_id"])
                if old_session is not None:
                    old_session.unregister_input(output_id)
                    if not old_session.has_inputs():
                        self.sessions.pop(old["stable_id"], None)
                        old_session.stop()
            session = self.sessions.get(config.stable_id)
            if session is None:
                session = AlsaSharedPlaybackTap(
                    config.stable_id,
                    output_sample_rate=config.output_sample_rate,
                    devices_provider=self.devices_provider,
                )
                self.sessions[config.stable_id] = session
                session.start()
            session.register_input(output_id, input_config)
            self.outputs[output_id] = {
                "stable_id": config.stable_id,
                "channel_mode": config.channel_mode,
                "software_volume": config.software_volume,
            }

    def remove_output(self, output_id: str) -> None:
        output_id = str(output_id)
        with self.lock:
            old = self.outputs.pop(output_id, None)
            if old is None:
                return
            session = self.sessions.get(old["stable_id"])
            if session is None:
                return
            session.unregister_input(output_id)
            if not session.has_inputs():
                self.sessions.pop(old["stable_id"], None)
                session.stop()

    def push_float(self, output_id: str, samples: np.ndarray) -> None:
        with self.lock:
            old = self.outputs.get(str(output_id))
            session = self.sessions.get(old["stable_id"]) if old is not None else None
        if session is not None:
            session.push_float(str(output_id), samples)

    def snapshot(self, output_id: str) -> dict[str, Any]:
        with self.lock:
            old = self.outputs.get(str(output_id))
            session = self.sessions.get(old["stable_id"]) if old is not None else None
        return session.snapshot() if session is not None else {"status": "disabled", "error": "", "stable_id": "", "device": ""}

    def prepare_stable_id_for_reset(self, stable_id: str) -> bool:
        with self.lock:
            session = self.sessions.get(str(stable_id))
        if session is None:
            return False
        prepare = getattr(session, "prepare_for_usb_reset", None)
        if callable(prepare):
            prepare()
        else:
            session.stop()
        return True

    def stop(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions = {}
            self.outputs = {}
        for session in sessions:
            session.stop()


class IcecastStreamWorker:
    def __init__(
        self,
        *,
        stream: dict[str, Any],
        fanout: RawRtlFanout | IntermediateIqFanout,
        fallback_settings_provider,
        alias_filter_strength_provider,
        state_directory: Path,
        soundcard_devices_provider=None,
        soundcard_manager: SharedSoundcardOutputManager | None = None,
        storage_monitor: StorageMonitor | None = None,
    ) -> None:
        self.stream = stream
        self.fanout = fanout
        self.fallback_settings_provider = fallback_settings_provider
        self.alias_filter_strength_provider = alias_filter_strength_provider
        self._soundcard_devices_provider = soundcard_devices_provider or discover_playback_devices
        self.soundcard_manager = soundcard_manager
        self.state_directory = state_directory
        self.storage_monitor = storage_monitor or StorageMonitor([state_directory])
        station = stream.get("station", {})
        stream_label = station.get("callsign") or stream.get("id", "stream")
        try:
            target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
        except Exception:
            target_frequency_hz = NWR_CENTER_FREQUENCY_HZ
        prebuilt = make_live_channelizer(
            fanout=fanout,
            target_frequency_hz=target_frequency_hz,
            alias_filter_strength=self.alias_filter_strength_provider(),
        )
        self._initial_channelizer = prebuilt[0] if prebuilt is not None else None
        self._initial_channelizer_key = prebuilt[1] if prebuilt is not None else None
        self._initial_channelizer_alias_filter_strength = prebuilt[2] if prebuilt is not None else None
        self.queue = subscribe_raw_fanout(
            fanout,
            max_seconds=LIVE_IQ_QUEUE_SECONDS,
            name=f"stream:{stream_label}",
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run_pcm_producer, name=f"icecast-stream-{stream['id']}", daemon=True)
        self.outputs: dict[str, IcecastOutputWriter] = {}
        self.encoder_groups: dict[tuple[str, int, int], IcecastEncoderGroup] = {}
        self.monitor_sources: dict[str, SameAwareWebRtcAudioSource] = {}
        self.soundcard_outputs: set[str] = set()
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
        for output_id in list(self.soundcard_outputs):
            if self.soundcard_manager is not None:
                self.soundcard_manager.remove_output(output_id)
        self.soundcard_outputs = set()
        self._stop_eas_recorder()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)

    def sync_stream(self, stream: dict[str, Any]) -> None:
        with self.lock:
            self.stream = stream
        desired = {
            str(output.get("id", "")): output
            for output in stream_outputs(stream)
            if output.get("enabled", True) and output.get("type", "icecast") == "icecast"
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
        self._sync_soundcard_taps(stream)
        self._sync_eas_recorder(stream)

    def audio_config(self) -> AudioConfig:
        with self.lock:
            stream = self.stream
        return audio_config_from_stream(stream)

    def snapshots(self) -> list[dict[str, Any]]:
        snapshots = [output.snapshot() for output in list(self.outputs.values())]
        with self.lock:
            output_ids = list(self.soundcard_outputs)
        for output_id in output_ids:
            snapshot = self.soundcard_manager.snapshot(output_id) if self.soundcard_manager is not None else {"status": "disabled"}
            snapshot.update(
                {
                    "id": self.id,
                    "output_id": output_id,
                    "type": "soundcard",
                    "status": "enabled" if snapshot.get("status") == "enabled" else "needs-attention",
                    "outputs": [
                        {
                            "id": output_id,
                            "type": "soundcard",
                            "status": snapshot.get("status"),
                            "soundcard": {
                                "stable_id": snapshot.get("stable_id", ""),
                                "device": snapshot.get("device", ""),
                            },
                            "error": snapshot.get("error", ""),
                        }
                    ],
                }
            )
            snapshots.append(snapshot)
        return snapshots

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

    def add_monitor_source(self, client_id: str, event_queue: SameEventQueue | None = None) -> SameAwareWebRtcAudioSource:
        source = SameAwareWebRtcAudioSource(sample_rate=IQ_SAMPLE_RATE, event_queue=event_queue)
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

    def add_soundcard_tap(self, tap_id: str, tap: Any) -> None:
        # Compatibility hook for older focused tests. Runtime soundcards use
        # SharedSoundcardOutputManager so one ALSA device is opened once.
        if not hasattr(self, "soundcard_taps"):
            self.soundcard_taps = {}
        tap_id = str(tap_id)
        self.soundcard_taps[tap_id] = tap
        start = getattr(tap, "start", None)
        if start is not None:
            start()

    def remove_soundcard_tap(self, tap_id: str) -> None:
        tap = getattr(self, "soundcard_taps", {}).pop(str(tap_id), None)
        if tap is not None:
            close = getattr(tap, "close", None) or getattr(tap, "stop", None)
            if close is not None:
                close()

    def has_soundcard_taps(self) -> bool:
        with self.lock:
            return bool(getattr(self, "soundcard_outputs", set())) or bool(getattr(self, "soundcard_taps", {}))

    def _sync_soundcard_taps(self, stream: dict[str, Any]) -> None:
        desired = {
            str(output.get("id", "")): output
            for output in stream_outputs(stream)
            if output.get("enabled", True) and output.get("type") == "soundcard"
        }
        if not hasattr(self, "soundcard_outputs"):
            self.soundcard_outputs = set()
        if getattr(self, "soundcard_manager", None) is None:
            self._sync_legacy_soundcard_taps(desired)
            return
        for output_id in list(self.soundcard_outputs):
            if output_id not in desired:
                if self.soundcard_manager is not None:
                    self.soundcard_manager.remove_output(output_id)
                self.soundcard_outputs.discard(output_id)
        for output_id, output in desired.items():
            if self.soundcard_manager is None:
                continue
            self.soundcard_manager.sync_output(output_id, output.get("soundcard", {}))
            self.soundcard_outputs.add(output_id)

    def _sync_legacy_soundcard_taps(self, desired: dict[str, dict[str, Any]]) -> None:
        if not hasattr(self, "soundcard_taps"):
            self.soundcard_taps = {}
        for output_id in list(self.soundcard_taps):
            output = desired.get(output_id)
            tap = self.soundcard_taps[output_id]
            if output is None:
                self.remove_soundcard_tap(output_id)
                continue
            config = soundcard_tap_config_from_output(output)
            current_config = getattr(tap, "config", None)
            if current_config is None or current_config.stable_id != config.stable_id or current_config.output_sample_rate != config.output_sample_rate:
                self.remove_soundcard_tap(output_id)
                continue
            try:
                tap.set_channel_mode(config.channel_mode)
                tap.set_software_volume(config.software_volume)
            except Exception as exc:
                LOG.warning("failed to update soundcard tap %s live: %s", output_id, exc)
        for output_id, output in desired.items():
            if output_id in self.soundcard_taps:
                continue
            tap = AlsaStreamPlaybackTap(
                soundcard_tap_config_from_output(output),
                devices_provider=self._soundcard_devices_provider,
            )
            self.add_soundcard_tap(output_id, tap)

    def _run_pcm_producer(self) -> None:
        channelizer: IqChannelizer | None = self._initial_channelizer
        channelizer_key: tuple[int, int] | None = self._initial_channelizer_key
        channelizer_alias_filter_strength: int | None = self._initial_channelizer_alias_filter_strength
        channelizer_target_frequency_hz: int | None = (
            int(getattr(channelizer, "target_frequency_hz", 0)) if channelizer is not None else None
        )
        startup_backlog_drained = False
        last_slow_batch_log_at = 0.0
        demodulator = ComplexNfmDemodulator()
        audio_config = self.audio_config()
        effects = AudioEffectsProcessor(audio_config)
        frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        fallback = load_web_fallback_audio()
        fallback_state = WebFallbackPlaybackState()
        last_real_audio = time.monotonic()
        idle_output_active = False
        idle_next_frame_at: float | None = None
        source_generation = getattr(self.fanout, "generation", 0)
        while not self.stop_event.is_set():
            if not self._has_connected_outputs():
                try:
                    self.queue.get(timeout=0.5)
                except queue.Empty:
                    pass
                fallback_state.reset()
                frame_buffer.clear()
                idle_next_frame_at = None
                continue
            try:
                if idle_output_active:
                    now = time.monotonic()
                    if idle_next_frame_at is None:
                        idle_next_frame_at = now
                    queue_timeout = max(0.0, idle_next_frame_at - now)
                else:
                    queue_timeout = STREAM_IDLE_DETECTION_SECONDS
                batch: RtlSampleBatch = self.queue.get(
                    timeout=queue_timeout
                )
            except queue.Empty:
                idle_output_active = True
                now = time.monotonic()
                if idle_next_frame_at is None:
                    idle_next_frame_at = now
                frames_due = max(1, int((now - idle_next_frame_at) / STREAM_FRAME_SECONDS) + 1)
                frames_due = min(frames_due, 8)
                fallback_settings = self.fallback_settings_provider()
                idle_seconds = time.monotonic() - last_real_audio
                if not fallback_settings.enabled:
                    fallback_state.reset()
                    idle_next_frame_at = None
                    continue
                for _ in range(frames_due):
                    if not fallback_state.active and idle_seconds < fallback_settings.silence_timeout_seconds:
                        fallback_state.reset()
                        self._write_pcm(STREAM_SILENCE_FRAME, STREAM_SILENCE_FLOAT_FRAME)
                    else:
                        if not fallback_state.active:
                            fallback_state.active = True
                            with self.lock:
                                fallback_station = self.stream.get("station", {})
                            LOG.info(
                                "starting fallback audio for %s after %.1f seconds without IQ",
                                fallback_station.get("callsign"),
                                idle_seconds,
                            )
                        fallback_pcm = next_web_fallback_frame(fallback, fallback_state, fallback_settings.loop_delay_seconds)
                        self._write_pcm(fallback_pcm, pcm_s16le_to_float32(fallback_pcm))
                    idle_next_frame_at += STREAM_FRAME_SECONDS
                if idle_next_frame_at < now - 0.25:
                    idle_next_frame_at = now + STREAM_FRAME_SECONDS
                continue
            if not startup_backlog_drained:
                batch = drain_queue_to_latest(self.queue, batch)
                startup_backlog_drained = True
            current_generation = getattr(self.fanout, "generation", source_generation)
            incoming_generation = batch_generation(batch)
            if incoming_generation < current_generation:
                continue
            if incoming_generation != source_generation:
                source_generation = incoming_generation
                channelizer = None
                channelizer_key = None
                channelizer_alias_filter_strength = None
                channelizer_target_frequency_hz = None
                demodulator = ComplexNfmDemodulator()
                frame_buffer.clear()
                fallback_state.reset()
                idle_next_frame_at = None
                LOG.info(
                    "stream DSP source generation changed for %s; reset channel state",
                    self.stream.get("station", {}).get("callsign"),
                )
            idle_output_active = False
            idle_next_frame_at = None
            with self.lock:
                station = self.stream["station"]
            target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
            alias_filter_strength = self.alias_filter_strength_provider()
            channel_transition_hz = alias_filter_transition_hz(
                CHANNEL_IQ_ALIAS_TRANSITION_HZ,
                alias_filter_strength,
            )
            channel_attenuation_db = alias_filter_attenuation_db(
                DEFAULT_ALIAS_ATTENUATION_DB,
                alias_filter_strength,
            )
            next_channelizer_key = (
                batch.sample_rate,
                batch.center_frequency_hz,
            )
            if channelizer is None or channelizer_key != next_channelizer_key:
                channelizer = IqChannelizer(
                    input_rate=batch.sample_rate,
                    center_frequency_hz=batch.center_frequency_hz,
                    target_frequency_hz=target_frequency_hz,
                    output_rate=IQ_SAMPLE_RATE,
                    transition_hz=channel_transition_hz,
                    alias_attenuation_db=channel_attenuation_db,
                )
                channelizer_key = next_channelizer_key
                channelizer_alias_filter_strength = alias_filter_strength
                channelizer_target_frequency_hz = target_frequency_hz
                demodulator = ComplexNfmDemodulator()
                frame_buffer.clear()
            elif channelizer_alias_filter_strength != alias_filter_strength:
                channelizer.update_alias_filter(
                    transition_hz=channel_transition_hz,
                    attenuation_db=channel_attenuation_db,
                )
                channelizer_alias_filter_strength = alias_filter_strength
            if channelizer is not None:
                if channelizer_target_frequency_hz != target_frequency_hz:
                    channelizer.set_target_frequency(target_frequency_hz)
                    channelizer_target_frequency_hz = target_frequency_hz
                    demodulator.reset()
                    frame_buffer.clear()
            iq = iq_batch_complex(batch)
            process_started_at = time.monotonic()
            audio = demodulator.process(channelizer.process_complex(iq))
            process_seconds = time.monotonic() - process_started_at
            batch_seconds = float(iq.size) / float(max(1, batch.sample_rate))
            if process_seconds > batch_seconds and process_started_at - last_slow_batch_log_at >= 60.0:
                last_slow_batch_log_at = process_started_at
                LOG.warning(
                    "stream DSP is slower than realtime for %s: processed %.3fs IQ in %.3fs",
                    station.get("callsign"),
                    batch_seconds,
                    process_seconds,
                )
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
                processed_frame = effects.process(frame)
                self._write_pcm(float_to_s16(processed_frame), processed_frame)

    def encoder_group_for(self, config: IcecastConfig) -> "IcecastEncoderGroup":
        key = icecast_encoder_key(config)
        with self.lock:
            encoder_group = self.encoder_groups.get(key)
            if encoder_group is not None:
                return encoder_group
            encoder_group = IcecastEncoderGroup(
                key,
                config,
                input_sample_rate=IQ_SAMPLE_RATE,
            )
            self.encoder_groups[key] = encoder_group
            encoder_group.start()
            LOG.info(
                "created shared Icecast encoder group %s for %s",
                key,
                self.stream.get("station", {}).get("callsign", "unknown"),
            )
            return encoder_group

    def _write_pcm(self, pcm: bytes, float_samples: np.ndarray | None = None) -> None:
        for encoder_group in list(self.encoder_groups.values()):
            if encoder_group.has_outputs():
                queue_latest(encoder_group.pcm_queue, pcm)
        with self.lock:
            monitor_sources = list(self.monitor_sources.values())
            soundcard_output_ids = list(getattr(self, "soundcard_outputs", set()))
            soundcard_taps = list(getattr(self, "soundcard_taps", {}).values())
            eas_recorder = self.eas_recorder
        for source in monitor_sources:
            source.push_pcm(pcm)
        if float_samples is not None and self.soundcard_manager is not None:
            for output_id in soundcard_output_ids:
                try:
                    self.soundcard_manager.push_float(output_id, float_samples)
                except Exception as exc:
                    LOG.warning(
                        "soundcard output failed for %s: %s",
                        self.stream.get("station", {}).get("callsign"),
                        exc,
                    )
        for tap in soundcard_taps:
            try:
                if float_samples is not None:
                    push_float = getattr(tap, "push_float", None)
                    if push_float is not None:
                        push_float(float_samples)
                        continue
                push = getattr(tap, "push_pcm", None)
                if push is not None:
                    push(pcm)
            except Exception as exc:
                LOG.warning(
                    "soundcard tap failed for %s: %s",
                    self.stream.get("station", {}).get("callsign"),
                    exc,
                )
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
            or self.has_soundcard_taps()
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
            recorder = EasRecorderOutput(next_config, input_sample_rate=IQ_SAMPLE_RATE)
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
    alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT


class IqRecorderWorker:
    def __init__(
        self,
        *,
        fanout: RawRtlFanout | IntermediateIqFanout,
        config: IqRecorderConfig,
        storage_monitor: StorageMonitor,
        alias_filter_strength_provider,
    ) -> None:
        self.fanout = fanout
        self.config = config
        self.storage_monitor = storage_monitor
        self.alias_filter_strength_provider = alias_filter_strength_provider
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
        channelizer_key: tuple[int, int] | None = None
        channelizer_alias_filter_strength: int | None = None
        decimator = None
        decimator_key: tuple[int, int] | None = None
        decimator_alias_filter_strength: int | None = None
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
                        alias_filter_strength = self.alias_filter_strength_provider()
                        channel_transition_hz = alias_filter_transition_hz(
                            CHANNEL_IQ_ALIAS_TRANSITION_HZ,
                            alias_filter_strength,
                        )
                        channel_attenuation_db = alias_filter_attenuation_db(
                            DEFAULT_ALIAS_ATTENUATION_DB,
                            alias_filter_strength,
                        )
                        next_key = (
                            batch.sample_rate,
                            batch.center_frequency_hz,
                        )
                        if channelizer is None or channelizer_key != next_key:
                            channelizer = IqChannelizer(
                                input_rate=batch.sample_rate,
                                center_frequency_hz=batch.center_frequency_hz,
                                target_frequency_hz=int(self.config.target_frequency_hz),
                                output_rate=IQ_SAMPLE_RATE,
                                transition_hz=channel_transition_hz,
                                alias_attenuation_db=channel_attenuation_db,
                            )
                            channelizer_key = next_key
                            channelizer_alias_filter_strength = alias_filter_strength
                        elif channelizer_alias_filter_strength != alias_filter_strength:
                            channelizer.update_alias_filter(
                                transition_hz=channel_transition_hz,
                                attenuation_db=channel_attenuation_db,
                            )
                            channelizer_alias_filter_strength = alias_filter_strength
                        if channelizer is not None:
                            channelizer.set_target_frequency(int(self.config.target_frequency_hz))
                        recorded_iq = channelizer.process_complex(iq)
                    else:
                        if self.config.sample_rate > batch.sample_rate:
                            raise ValueError("recording sample rate is higher than the RTL-SDR sample rate")
                        if self.config.sample_rate == batch.sample_rate:
                            recorded_iq = iq
                        else:
                            alias_filter_strength = self.alias_filter_strength_provider()
                            transition_hz = alias_filter_transition_hz(
                                INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
                                alias_filter_strength,
                            )
                            attenuation_db = alias_filter_attenuation_db(
                                INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
                                alias_filter_strength,
                            )
                            next_key = (batch.sample_rate, self.config.sample_rate)
                            if decimator is None or decimator_key != next_key:
                                decimator = create_decimator(
                                    batch.sample_rate,
                                    self.config.sample_rate,
                                    transition_hz=transition_hz,
                                    attenuation_db=attenuation_db,
                                )
                                decimator_key = next_key
                                decimator_alias_filter_strength = alias_filter_strength
                            elif decimator_alias_filter_strength != alias_filter_strength:
                                update_decimator_alias_filter(
                                    decimator,
                                    batch.sample_rate,
                                    self.config.sample_rate,
                                    transition_hz=transition_hz,
                                    attenuation_db=attenuation_db,
                                )
                                decimator_alias_filter_strength = alias_filter_strength
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
    def __init__(
        self,
        key: tuple[str, int, int],
        config: IcecastConfig,
        *,
        input_sample_rate: int = IQ_SAMPLE_RATE,
    ) -> None:
        self.key = key
        self.config = config
        self.input_sample_rate = int(input_sample_rate)
        self.encoder = create_audio_encoder(config, input_sample_rate=self.input_sample_rate)
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
        self.connection_lock = threading.Lock()
        self.current_source: IcecastSource | None = None
        self.current_sink = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._close_current_connection()
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

    def _set_current_connection(self, source, sink) -> None:
        with self.connection_lock:
            self.current_source = source
            self.current_sink = sink

    def _close_current_connection(self) -> None:
        with self.connection_lock:
            source = self.current_source
            sink = self.current_sink
            self.current_source = None
            self.current_sink = None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
        if source is not None:
            try:
                source.close()
            except Exception:
                pass

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
                self._set_current_connection(source, sink)
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
                self._close_current_connection()
                if sink is not None:
                    try:
                        sink.close()
                    except Exception:
                        pass
                if source is not None:
                    try:
                        source.close()
                    except Exception:
                        pass
        self._set_status("disabled")


class WeatherReceiverWorker:
    def __init__(
        self,
        *,
        client_id: str,
        fanout: RawRtlFanout | IntermediateIqFanout,
        frequency_hz: int,
        alias_filter_strength_provider,
        event_queue: SameEventQueue | None = None,
    ) -> None:
        self.client_id = client_id
        self.fanout = fanout
        self.alias_filter_strength_provider = alias_filter_strength_provider
        frequency_hz = validate_receiver_frequency(frequency_hz)
        prebuilt = make_live_channelizer(
            fanout=fanout,
            target_frequency_hz=frequency_hz,
            alias_filter_strength=self.alias_filter_strength_provider(),
        )
        self._initial_channelizer = prebuilt[0] if prebuilt is not None else None
        self._initial_channelizer_key = prebuilt[1] if prebuilt is not None else None
        self._initial_channelizer_alias_filter_strength = prebuilt[2] if prebuilt is not None else None
        self.queue = subscribe_raw_fanout(
            fanout,
            max_seconds=LIVE_IQ_QUEUE_SECONDS,
            name=f"receiver:{client_id}",
        )
        self.source = SameAwareWebRtcAudioSource(sample_rate=IQ_SAMPLE_RATE, event_queue=event_queue)
        self.frequency_hz = frequency_hz
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
        channelizer: IqChannelizer | None = self._initial_channelizer
        channelizer_key: tuple[int, int] | None = self._initial_channelizer_key
        channelizer_alias_filter_strength: int | None = self._initial_channelizer_alias_filter_strength
        channelizer_target_frequency_hz: int | None = (
            int(getattr(channelizer, "target_frequency_hz", 0)) if channelizer is not None else None
        )
        startup_backlog_drained = False
        last_slow_batch_log_at = 0.0
        demodulator = ComplexNfmDemodulator()
        effects = AudioEffectsProcessor(RECEIVER_AUDIO_CONFIG)
        frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        source_generation = getattr(self.fanout, "generation", 0)
        while not self.stop_event.is_set():
            try:
                batch: RtlSampleBatch = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                LOG.warning("weather receiver RTL-SDR source failed for client %s: %s", self.client_id, exc)
                continue
            if not startup_backlog_drained:
                batch = drain_queue_to_latest(self.queue, batch)
                startup_backlog_drained = True
            current_generation = getattr(self.fanout, "generation", source_generation)
            incoming_generation = batch_generation(batch)
            if incoming_generation < current_generation:
                continue
            if incoming_generation != source_generation:
                source_generation = incoming_generation
                channelizer = None
                channelizer_key = None
                channelizer_alias_filter_strength = None
                channelizer_target_frequency_hz = None
                demodulator = ComplexNfmDemodulator()
                frame_buffer.clear()
                LOG.info("weather receiver source generation changed for client %s; reset channel state", self.client_id)
            target_frequency_hz = self._frequency_hz()
            alias_filter_strength = self.alias_filter_strength_provider()
            channel_transition_hz = alias_filter_transition_hz(
                CHANNEL_IQ_ALIAS_TRANSITION_HZ,
                alias_filter_strength,
            )
            channel_attenuation_db = alias_filter_attenuation_db(
                DEFAULT_ALIAS_ATTENUATION_DB,
                alias_filter_strength,
            )
            next_channelizer_key = (batch.sample_rate, batch.center_frequency_hz)
            if channelizer is None or channelizer_key != next_channelizer_key:
                channelizer = IqChannelizer(
                    input_rate=batch.sample_rate,
                    center_frequency_hz=batch.center_frequency_hz,
                    target_frequency_hz=target_frequency_hz,
                    output_rate=IQ_SAMPLE_RATE,
                    transition_hz=channel_transition_hz,
                    alias_attenuation_db=channel_attenuation_db,
                )
                channelizer_key = next_channelizer_key
                channelizer_alias_filter_strength = alias_filter_strength
                channelizer_target_frequency_hz = target_frequency_hz
                demodulator = ComplexNfmDemodulator()
                frame_buffer.clear()
            elif channelizer_alias_filter_strength != alias_filter_strength:
                channelizer.update_alias_filter(
                    transition_hz=channel_transition_hz,
                    attenuation_db=channel_attenuation_db,
                )
                channelizer_alias_filter_strength = alias_filter_strength
            if channelizer is not None:
                if channelizer_target_frequency_hz != target_frequency_hz:
                    channelizer.set_target_frequency(target_frequency_hz)
                    channelizer_target_frequency_hz = target_frequency_hz
                    demodulator.reset()
                    frame_buffer.clear()
            iq = iq_batch_complex(batch)
            process_started_at = time.monotonic()
            audio = demodulator.process(channelizer.process_complex(iq))
            process_seconds = time.monotonic() - process_started_at
            batch_seconds = float(iq.size) / float(max(1, batch.sample_rate))
            if process_seconds > batch_seconds and process_started_at - last_slow_batch_log_at >= 60.0:
                last_slow_batch_log_at = process_started_at
                LOG.warning(
                    "weather receiver DSP is slower than realtime for client %s: processed %.3fs IQ in %.3fs",
                    self.client_id,
                    batch_seconds,
                    process_seconds,
                )
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
        self.iq_test_sources_directory = state_path.parent / IQ_TEST_SOURCES_DIRECTORY_NAME
        self.fallback_state_path = state_path.with_name(FALLBACK_STATE_FILE_NAME)
        self.accounts = AccountStore(state_path.parent / ACCOUNTS_DATABASE_FILE_NAME)
        self.auth_sessions = AuthSessionStore()
        self.log_handler = log_handler
        self.storage_monitor = StorageMonitor(
            [state_path.parent],
            critical_callback=self._handle_critical_storage,
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
        self.streams = load_streams(self.streams_directory, self.streams_state_path)
        self.fallback_settings = load_fallback_settings(self.fallback_state_path)
        self.stations = load_station_database()
        self.capture: RtlCaptureSource | IqFileCaptureSource | None = None
        self.iq_file_source_config: IqFileSourceConfig | None = None
        self.raw_fanout: RawRtlFanout | None = None
        self.intermediate_fanout: IntermediateIqFanout | None = None
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
        self.soundcard_manager = SharedSoundcardOutputManager(devices_provider=self._cached_soundcards)
        self.icecast_auth_cache: dict[str, float] = {}
        self.reset_lock = threading.Lock()
        self.capture_error: str | None = None
        self.last_batch_at: float | None = None
        self.received_chunks = 0
        self.received_bytes = 0
        self.storage_monitor.add_path(self.iq_recordings_directory)
        self.storage_monitor.start()
        self.preview_cleanup_thread.start()
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
        self.preview_cleanup_stop.set()
        self.preview_cleanup_thread.join(timeout=2.0)
        with self.lock:
            recorder = self.iq_recorder
            self.iq_recorder = None
        if recorder is not None:
            recorder.stop()
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
        for worker in workers:
            worker.stop_eas_recording_due_to_storage(message)
        if recorder is not None:
            LOG.warning("stopping I/Q recorder because storage is critically low")
            recorder.stop()

    def _preview_cleanup_loop(self) -> None:
        while not self.preview_cleanup_stop.wait(SOUNDCARD_PREVIEW_CLEANUP_SECONDS):
            self._cleanup_expired_soundcard_previews()

    def _cleanup_expired_soundcard_previews(self) -> None:
        now = time.time()
        expired: list[tuple[str, IcecastStreamWorker | None]] = []
        with self.lock:
            for preview_id, preview in list(getattr(self, "preview_streams", {}).items()):
                heartbeat_at = float(preview.get("heartbeat_at", preview.get("updated_at", now)))
                if now - heartbeat_at <= SOUNDCARD_PREVIEW_TIMEOUT_SECONDS:
                    continue
                self.preview_streams.pop(preview_id, None)
                expired.append((preview_id, self.stream_workers.pop(stream_worker_key(preview), None)))
        for preview_id, worker in expired:
            if worker is not None:
                worker.stop()
            LOG.info("expired temporary soundcard preview %s", preview_id)

    def heartbeat_soundcard_preview_stream(self, preview_id: str) -> None:
        preview_id = str(preview_id).strip()
        if not preview_id:
            return
        with self.lock:
            preview = getattr(self, "preview_streams", {}).get(preview_id)
            if preview is not None:
                preview["heartbeat_at"] = time.time()

    def status(self) -> dict[str, Any]:
        self._cleanup_expired_soundcard_previews()
        with self.lock:
            capture = self.capture
            settings = self._effective_settings_locked()
            active = capture is not None
            gain_values = capture.get_gain_values() if isinstance(capture, RtlCaptureSource) else []
            capture_stats = capture.stats() if capture is not None else {}
            fanout_stats = self.raw_fanout.stats() if self.raw_fanout is not None else {}
            intermediate_stats = self.intermediate_fanout.stats() if self.intermediate_fanout is not None else {}
            source_kind = "iq_file" if self.iq_file_source_config is not None else "rtl"
            source_name = (
                self.iq_file_source_config.path.name
                if self.iq_file_source_config is not None
                else (settings.serial or "")
            )
            return {
                "settings": asdict(settings),
                "gain_values": gain_values,
                "active": active,
                "source": {
                    "kind": source_kind,
                    "name": source_name,
                    "sample_rate": (
                        self.iq_file_source_config.sample_rate
                        if self.iq_file_source_config is not None
                        else settings.sample_rate
                    ),
                    "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
                },
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
        snapshot = self.device_probe.snapshot()
        rtl_devices = list(snapshot.devices("rtl"))
        usb_devices = list(snapshot.devices("rtl_usb"))
        soundcards = list(snapshot.devices("alsa_playback"))
        if snapshot.error("rtl"):
            errors.append(f"librtlsdr probe failed: {snapshot.error('rtl')}")
        if snapshot.error("rtl_usb"):
            errors.append(f"USB probe failed: {snapshot.error('rtl_usb')}")
        if snapshot.error("alsa_playback"):
            errors.append(f"ALSA probe failed: {snapshot.error('alsa_playback')}")

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
        return {
            "devices": devices,
            "soundcards": [self._soundcard_device_payload(device) for device in soundcards],
            "errors": errors,
            "probed_at": snapshot.probed_at,
        }

    def _cached_rtl_devices(self) -> list[Any]:
        return list(self.device_probe.devices("rtl"))

    def _cached_usb_rtl_devices(self) -> list[Any]:
        return list(self.device_probe.devices("rtl_usb"))

    def _cached_soundcards(self) -> list[Any]:
        return list(self.device_probe.devices("alsa_playback"))

    def _refresh_soundcards(self) -> list[Any]:
        return list(self.device_probe.snapshot(force=True).devices("alsa_playback"))

    @staticmethod
    def _soundcard_device_payload(device) -> dict[str, Any]:
        return {
            "stable_id": device.stable_id,
            "hw_device": device.hw_device,
            "card_index": device.card_index,
            "pcm_device": device.pcm_device,
            "card_id": device.card_id,
            "card_name": device.card_name,
            "card_long_name": device.card_long_name,
            "pcm_id": device.pcm_id,
            "pcm_name": device.pcm_name,
            "display_name": device.display_name,
            "bus": device.bus,
            "vendor_id": device.vendor_id,
            "product_id": device.product_id,
            "serial": device.serial,
            "usb_port_path": device.usb_port_path,
            "device_path": device.device_path,
            "subdevices_count": device.subdevices_count,
            "subdevices_available": device.subdevices_available,
        }

    def _soundcard_by_stable_id(self, stable_id: str):
        stable_id = str(stable_id or "").strip()
        if not stable_id:
            raise ValueError("Select a sound card to reset.")
        matches = [device for device in self._cached_soundcards() if device.stable_id == stable_id]
        if not matches:
            matches = [device for device in self._refresh_soundcards() if device.stable_id == stable_id]
        if not matches:
            raise ValueError("Sound card is not currently connected.")
        if len(matches) > 1:
            raise ValueError("Sound card stable ID is ambiguous.")
        return matches[0]

    def reset_soundcard_device(self, stable_id: str) -> dict[str, Any]:
        if not self.reset_lock.acquire(blocking=False):
            raise ValueError("A USB device reset is already in progress.")
        try:
            return self._reset_soundcard_device(stable_id)
        finally:
            self.reset_lock.release()

    def _reset_soundcard_device(self, stable_id: str) -> dict[str, Any]:
        stable_id = str(stable_id or "").strip()
        device = self._soundcard_by_stable_id(stable_id)
        if device.bus != "usb":
            raise ValueError("Only USB sound cards can be reset.")
        usb_node = playback_device_usb_node(device)
        LOG.info("pausing soundcard outputs before USB reset for %s", stable_id)
        self.soundcard_manager.prepare_stable_id_for_reset(stable_id)
        LOG.info("resetting USB soundcard %s at %s", stable_id, usb_node)
        reset_queue: queue.Queue = queue.Queue(maxsize=1)

        def run_reset() -> None:
            try:
                reset_queue.put((reset_usb_device_node(usb_node, timeout_seconds=RTL_RESET_COMMAND_TIMEOUT_SECONDS), None))
            except BaseException as exc:
                reset_queue.put((None, exc))

        reset_thread = threading.Thread(
            target=run_reset,
            name=f"soundcard-usb-reset-{device.card_id or device.card_index}",
            daemon=True,
        )
        reset_thread.start()
        try:
            reset_method, reset_error = reset_queue.get(timeout=RTL_RESET_COMMAND_TIMEOUT_SECONDS + 1.0)
        except queue.Empty:
            message = (
                f"Sound card {device.display_name} did not finish its USB reset command. "
                "Physically unplug and replug it if it does not recover."
            )
            LOG.warning("%s", message)
            return {"success": False, "reappeared": False, "message": message, "status": self.status()}
        if reset_error is not None:
            raise reset_error
        reappeared = False
        deadline = time.monotonic() + RTL_RESET_REAPPEAR_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if any(candidate.stable_id == stable_id for candidate in self._refresh_soundcards()):
                    reappeared = True
                    break
            except Exception as exc:
                LOG.debug("ALSA probe after soundcard USB reset failed: %s", exc)
            time.sleep(0.25)
        if reappeared:
            message = f"Sound card {device.display_name} was reset using {reset_method} and reappeared."
            LOG.info("%s", message)
        else:
            message = (
                f"Sound card {device.display_name} was reset using {reset_method}, but it did not reappear. "
                "Physically unplug and replug it if it remains unavailable."
            )
            LOG.warning("%s", message)
        return {
            "success": True,
            "stable_id": stable_id,
            "reappeared": reappeared,
            "device": self._soundcard_device_payload(device),
            "message": message,
            "status": self.status(),
        }

    def reset_rtl_device(self, serial: str) -> dict[str, Any]:
        if not self.reset_lock.acquire(blocking=False):
            raise ValueError("A USB device reset is already in progress.")
        try:
            return self._reset_rtl_device(serial)
        finally:
            self.reset_lock.release()

    def _reset_rtl_device(self, serial: str) -> dict[str, Any]:
        serial = str(serial or "").strip()
        with self.lock:
            configured_serial = self.settings.serial
            should_restart_capture = bool(configured_serial and configured_serial == serial)
            capture = self.capture if should_restart_capture else None
        if not serial:
            raise ValueError("Select an RTL-SDR to reset.")
        if capture is not None:
            LOG.info("pausing RTL-SDR reader before USB reset for serial %s", serial)
            capture.restart_reader()
            if not capture.wait_until_reader_released(timeout=2.0):
                LOG.warning(
                    "RTL-SDR serial %s did not release before reset; forcing USB reset anyway",
                    serial,
                )
        LOG.info("resetting RTL-SDR USB device with serial %s", serial)
        reset_queue: queue.Queue = queue.Queue(maxsize=1)

        def run_reset() -> None:
            try:
                reset_queue.put((reset_usb_rtl_device(serial, timeout_seconds=RTL_RESET_COMMAND_TIMEOUT_SECONDS), None))
            except BaseException as exc:
                reset_queue.put((None, exc))

        reset_thread = threading.Thread(
            target=run_reset,
            name=f"rtl-usb-reset-{serial}",
            daemon=True,
        )
        reset_thread.start()
        try:
            reset_result, reset_error = reset_queue.get(timeout=RTL_RESET_COMMAND_TIMEOUT_SECONDS + 1.0)
        except queue.Empty:
            message = (
                f"RTL-SDR serial {serial} did not finish its USB reset command. "
                "The dongle may be locked up; physically unplug and replug it if it does not recover."
            )
            LOG.warning("%s", message)
            with self.lock:
                if self.settings.serial == serial:
                    self.capture_error = message
            return {
                "success": False,
                "serial": serial,
                "reappeared": False,
                "device": None,
                "message": message,
                "status": self.status(),
            }
        if reset_error is not None:
            if should_restart_capture:
                with self.lock:
                    if self.settings.serial == serial and self.capture is None:
                        self._start_or_update_capture_locked()
            raise reset_error
        if reset_result is None:
            raise RuntimeError("RTL-SDR reset failed without an error")
        reset_device, reset_method = reset_result
        reappeared = False
        deadline = time.monotonic() + RTL_RESET_REAPPEAR_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if any(device.serial == serial for device in list_usb_rtl_devices()):
                    reappeared = True
                    break
            except Exception as exc:
                LOG.debug("USB probe after RTL-SDR reset failed: %s", exc)
            time.sleep(0.25)
        with self.lock:
            if self.settings.serial == serial:
                self.capture_error = None if reappeared else (
                    f"RTL-SDR serial {serial} did not reappear after USB reset. "
                    "Physically unplug and replug the dongle if it remains unavailable."
                )
                if self.capture is None:
                    self._start_or_update_capture_locked()
        if reappeared:
            message = f"RTL-SDR serial {serial} was reset using {reset_method} and reappeared."
            LOG.info("%s", message)
        else:
            message = (
                f"RTL-SDR serial {serial} was reset using {reset_method}, but it did not reappear. "
                "Physically unplug and replug the dongle if it remains unavailable."
            )
            LOG.warning("%s", message)
        return {
            "success": True,
            "serial": serial,
            "reappeared": reappeared,
            "device": {
                "serial": reset_device.serial,
                "name": reset_device.description,
                "vendor": USB_VENDOR_NAMES.get(reset_device.vendor_id.lower(), reset_device.vendor_id),
                "vendor_id": reset_device.vendor_id,
                "product_id": reset_device.product_id,
            },
            "message": message,
            "status": self.status(),
        }

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
            if config.mode == IQ_RECORDER_MODE_STREAM:
                fanout = intermediate_fanout
            elif config.sample_rate == INTERMEDIATE_IQ_SAMPLE_RATE:
                fanout = intermediate_fanout
            else:
                fanout = raw_fanout
            if fanout is None:
                raise ValueError("RTL-SDR capture is not active")
            if self.storage_monitor.is_critical(config.output_path.parent):
                raise ValueError(storage_status_message("critical"))
            worker = IqRecorderWorker(
                fanout=fanout,
                config=config,
                storage_monitor=self.storage_monitor,
                alias_filter_strength_provider=self._alias_filter_strength,
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
            alias_filter_strength=self.settings.alias_filter_strength,
        )

    def rtl_diagnostics(self) -> dict[str, Any]:
        with self.lock:
            fanout = self.raw_fanout
            alias_filter_strength = self.settings.alias_filter_strength
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
            channel_audio_diagnostics(
                iq,
                sample_rate,
                center_frequency_hz,
                frequency_hz,
                alias_filter_strength=alias_filter_strength,
            )
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
        output_type = str(payload.get("type", "") or payload.get("output_type", "")).strip().lower()
        if not output_type:
            output_type = "soundcard" if payload.get("soundcard") is not None else "icecast"
        preview_worker = None
        preview_id = str(payload.get("preview_id", "")).strip()
        if preview_id:
            with self.lock:
                preview = getattr(self, "preview_streams", {}).pop(preview_id, None)
                if preview is not None:
                    preview_worker = self.stream_workers.pop(stream_worker_key(preview), None)
            if preview_worker is not None:
                preview_worker.stop()
        output_id = uuid.uuid4().hex
        if output_type == "icecast":
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
            output = {
                "id": output_id,
                "enabled": True,
                "type": "icecast",
                "icecast": icecast,
                "auth_validated_at": time.time(),
                "auth_signature": icecast_auth_signature(icecast),
            }
        elif output_type == "soundcard":
            soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
            with self.lock:
                soundcard = self._normalize_soundcard_output_locked(
                    "",
                    output_id,
                    soundcard,
                    enabled=True,
                )
            output = {
                "id": output_id,
                "enabled": True,
                "type": "soundcard",
                "soundcard": soundcard,
            }
        else:
            raise ValueError("stream output type is not supported")
        stream = {
            "id": uuid.uuid4().hex,
            "enabled": True,
            "station": station,
            "outputs": [output],
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

    def upsert_soundcard_preview_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        preview_id = str(payload.get("preview_id", "")).strip()
        station_key = str(payload.get("station_key", "")).strip()
        station = self._station_by_key(station_key)
        soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
        with self.lock:
            if not hasattr(self, "preview_streams"):
                self.preview_streams = {}
            preview = self.preview_streams.get(preview_id) if preview_id else None
            if preview is None:
                preview_id = uuid.uuid4().hex
                output_id = uuid.uuid4().hex
                preview = {
                    "id": f"preview-{preview_id}",
                    "preview_id": preview_id,
                    "enabled": True,
                    "station": station,
                    "outputs": [
                        {
                            "id": output_id,
                            "enabled": True,
                            "type": "soundcard",
                            "soundcard": soundcard,
                        }
                    ],
                    "audio": asdict(AudioConfig()),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "heartbeat_at": time.time(),
                }
                self.preview_streams[preview_id] = preview
            else:
                preview["station"] = station
                outputs = mutable_stream_outputs(preview)
                if not outputs:
                    outputs.append({"id": uuid.uuid4().hex, "enabled": True, "type": "soundcard"})
                outputs[0]["enabled"] = True
                outputs[0]["type"] = "soundcard"
                outputs[0]["soundcard"] = soundcard
                preview["updated_at"] = time.time()
                preview["heartbeat_at"] = time.time()
            output = mutable_stream_outputs(preview)[0]
            output["soundcard"] = self._normalize_soundcard_output_locked(
                str(preview.get("id", "")),
                str(output.get("id", "")),
                output["soundcard"],
                enabled=True,
            )
            self._sync_stream_workers_locked()
        LOG.info("updated temporary soundcard preview for %s", station.get("callsign", "unknown"))
        return {
            "success": True,
            "message": "Soundcard preview started.",
            "preview_id": preview_id,
            "soundcard": output["soundcard"],
        }

    def discard_soundcard_preview_stream(self, preview_id: str) -> dict[str, Any]:
        preview_id = str(preview_id).strip()
        with self.lock:
            preview = getattr(self, "preview_streams", {}).pop(preview_id, None)
            worker = self.stream_workers.pop(stream_worker_key(preview), None) if preview is not None else None
        if worker is not None:
            worker.stop()
            LOG.info("discarded temporary soundcard preview %s", preview_id)
        return {"success": True}

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

        event_queue = SameEventQueue()
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
            source = worker.add_monitor_source(client_id, event_queue)
            station = stream.get("station", {})

        try:
            track = create_webrtc_pcm_audio_track(source)
            answer = self.webrtc_runner.run(
                self.webrtc_sessions.accept_offer(
                    session_id=client_id,
                    sdp=sdp,
                    type=offer_type,
                    tracks=(track,),
                    event_queue=event_queue.queue,
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
        event_queue = SameEventQueue()
        with self.lock:
            fanout = self.intermediate_fanout
            if fanout is None:
                LOG.warning("weather receiver rejected for client %s: RTL-SDR capture is not active", client_id)
                raise ValueError("RTL-SDR capture is not active")
            worker = WeatherReceiverWorker(
                client_id=client_id,
                fanout=fanout,
                frequency_hz=frequency_hz,
                alias_filter_strength_provider=self._alias_filter_strength,
                event_queue=event_queue,
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
                    event_queue=event_queue.queue,
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
        output_type = str(payload.get("type", "")).strip().lower()
        with self.lock:
            stream, output = self._stream_output_locked(stream_id, output_id)
            output_type = output_type or str(output.get("type", "icecast")).strip().lower()
            if output.get("enabled", True) and not enabled:
                self._ensure_can_disable_icecast_output_locked(stream, output_id)
            if output_type == "icecast":
                icecast = validate_icecast_payload(payload.get("icecast"))
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
                output["icecast"] = icecast
            elif output_type == "soundcard":
                soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
                output["soundcard"] = self._normalize_soundcard_output_locked(
                    stream_id,
                    output_id,
                    soundcard,
                    enabled=enabled,
                )
            else:
                raise ValueError("stream output type is not supported")
            output["enabled"] = enabled
            output["type"] = output_type
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
        output_type = str(payload.get("type", "icecast")).strip().lower()
        with self.lock:
            stream = self._stream_locked(stream_id)
            if output_type == "icecast":
                icecast = validate_icecast_payload(payload.get("icecast"))
                self._reject_duplicate_icecast_locked(icecast)
            elif output_type == "soundcard":
                soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
            else:
                raise ValueError("stream output type is not supported")
        if output_type == "icecast":
            result = self.test_icecast_auth(icecast)
            if not result["success"]:
                return {
                    "success": False,
                    "message": result["message"],
                    "streams": list(self.streams),
                }
        with self.lock:
            stream = self._stream_locked(stream_id)
            output_id = uuid.uuid4().hex
            if output_type == "icecast":
                self._reject_duplicate_icecast_locked(icecast)
                output = {
                    "id": output_id,
                    "enabled": True,
                    "type": "icecast",
                    "icecast": icecast,
                    "auth_validated_at": time.time(),
                    "auth_signature": icecast_auth_signature(icecast),
                }
                message = "Icecast output added."
            else:
                soundcard = self._normalize_soundcard_output_locked(
                    stream_id,
                    output_id,
                    soundcard,
                    enabled=True,
                )
                output = {
                    "id": output_id,
                    "enabled": True,
                    "type": "soundcard",
                    "soundcard": soundcard,
                }
                message = "Soundcard output added."
            mutable_stream_outputs(stream).append(output)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": message,
            "output_id": output_id,
            "streams": list(self.streams),
        }

    def _normalize_soundcard_output_locked(
        self,
        stream_id: str,
        output_id: str,
        soundcard: dict[str, Any],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        if not enabled:
            return soundcard
        stable_id = str(soundcard.get("stable_id", ""))
        occupied = self._occupied_soundcard_channels_locked(ignore_output_id=output_id).get(stable_id, set())
        requested = soundcard_channels(soundcard.get("channel_mode", ALSA_CHANNEL_BOTH))
        if requested and requested.isdisjoint(occupied):
            return soundcard
        available = [channel for channel in (ALSA_CHANNEL_LEFT, ALSA_CHANNEL_RIGHT) if channel not in occupied]
        if not available:
            raise ValueError("That sound card has no available output channels.")
        normalized = dict(soundcard)
        if len(available) == 2 and soundcard.get("channel_mode") == ALSA_CHANNEL_BOTH:
            normalized["channel_mode"] = ALSA_CHANNEL_BOTH
        else:
            normalized["channel_mode"] = available[0]
        return normalized

    def _occupied_soundcard_channels_locked(self, *, ignore_output_id: str = "") -> dict[str, set[str]]:
        occupied: dict[str, set[str]] = {}
        for stream in list(self.streams) + list(getattr(self, "preview_streams", {}).values()):
            for output in stream_outputs(stream):
                if str(output.get("id", "")) == ignore_output_id:
                    continue
                if not output.get("enabled", True) or output.get("type") != "soundcard":
                    continue
                soundcard = output.get("soundcard", {})
                stable_id = str(soundcard.get("stable_id", "")).strip()
                if not stable_id:
                    continue
                occupied.setdefault(stable_id, set()).update(soundcard_channels(soundcard.get("channel_mode", ALSA_CHANNEL_BOTH)))
        return occupied

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
                "saved RTL settings: serial=%s sample_rate=%s gain=%s ppm=%s bias_tee=%s alias_filter_strength=%s",
                settings.serial or "<none>",
                settings.sample_rate,
                "auto" if settings.gain is None else f"{settings.gain:g} dB",
                settings.ppm_correction,
                settings.bias_tee,
                settings.alias_filter_strength,
            )
            if self.iq_file_source_config is not None:
                if self.intermediate_fanout is not None:
                    self.intermediate_fanout.set_alias_filter_strength(settings.alias_filter_strength)
                return self.status()
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
        if "alias_filter_strength" in payload:
            changes["alias_filter_strength"] = validate_alias_filter_strength(payload["alias_filter_strength"])
        return replace(settings, **changes)

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

    def _alias_filter_strength(self) -> int:
        with self.lock:
            return self.settings.alias_filter_strength

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
        if self.intermediate_fanout is None:
            self.intermediate_fanout = IntermediateIqFanout(
                self.raw_fanout,
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
            self.preview_streams = {}
            intermediate = self.intermediate_fanout
            self.intermediate_fanout = None
            fanout = self.raw_fanout
            self.raw_fanout = None
            self.monitor_queue = None
            capture = self.capture
            self.capture = None
            self.iq_file_source_config = None
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
            with self.lock:
                self.capture_error = None
                self.last_batch_at = batch.captured_at
                self.received_chunks += 1
                self.received_bytes += iq_batch_byte_count(batch)

    def _sync_stream_workers_locked(self) -> None:
        fanout = self.intermediate_fanout
        desired: dict[str, dict[str, Any]] = {}
        monitored_stream_ids = set(self.monitor_streams_by_client.values())
        if fanout is not None:
            for stream in list(self.streams) + list(getattr(self, "preview_streams", {}).values()):
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
                alias_filter_strength_provider=self._alias_filter_strength,
                soundcard_devices_provider=self._cached_soundcards,
                soundcard_manager=self.soundcard_manager,
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
            query = parse_qs(parsed.query)
            self.service.heartbeat_soundcard_preview_stream(query.get("soundcard_preview_id", [""])[0])
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
        elif path == "/api/iq-test-sources":
            self._send_json(self.service.iq_test_sources())
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

    def do_PUT(self) -> None:
        self.do_PATCH()

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
            alias_filter_strength=validate_alias_filter_strength(
                raw.get("alias_filter_strength", ALIAS_FILTER_STRENGTH_DEFAULT)
            ),
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


def whole_seconds(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a whole number of seconds")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a whole number of seconds") from exc
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError(f"{label} must be a whole number of seconds")
    return int(number)


def validate_eas_recording_payload(raw: Any) -> WebEasRecordingSettings:
    if not isinstance(raw, dict):
        raise ValueError("EAS recording settings are required")
    enabled = bool(raw.get("enabled", False))
    pre_seconds = whole_seconds(raw.get("pre_seconds", 2), "Pre-recording time")
    post_seconds = whole_seconds(raw.get("post_seconds", 5), "Post-recording time")
    max_seconds = whole_seconds(raw.get("max_seconds", 120), "Maximum recording time")
    if isinstance(max_seconds, bool):
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
    silence_timeout_seconds = whole_seconds(raw.get("silence_timeout_seconds", 30), "Fallback delay")
    loop_delay_seconds = whole_seconds(raw.get("loop_delay_seconds", 5), "Seconds before restart")
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


def validate_soundcard_output_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("soundcard settings are required")
    stable_id = str(raw.get("stable_id", "")).strip()
    if not stable_id:
        raise ValueError("select a sound card")
    channel_mode = str(raw.get("channel_mode", ALSA_CHANNEL_BOTH)).strip().lower()
    if channel_mode not in ALSA_CHANNEL_MODES:
        raise ValueError("select left, right, or both channels")
    try:
        volume = float(raw.get("volume", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("soundcard volume must be a number") from exc
    if volume < 0.0 or volume > 2.0:
        raise ValueError("soundcard volume must be from 0 through 2")
    try:
        sample_rate = int(raw.get("sample_rate", ALSA_STREAM_DEFAULT_SAMPLE_RATE))
    except (TypeError, ValueError) as exc:
        raise ValueError("soundcard sample rate must be a number") from exc
    if sample_rate < 8000 or sample_rate > 192000:
        raise ValueError("soundcard sample rate is not supported")
    return {
        "stable_id": stable_id,
        "channel_mode": channel_mode,
        "volume": volume,
        "sample_rate": sample_rate,
    }


def soundcard_channels(channel_mode: object) -> set[str]:
    mode = str(channel_mode or ALSA_CHANNEL_BOTH).strip().lower()
    if mode == ALSA_CHANNEL_LEFT:
        return {ALSA_CHANNEL_LEFT}
    if mode == ALSA_CHANNEL_RIGHT:
        return {ALSA_CHANNEL_RIGHT}
    return {ALSA_CHANNEL_LEFT, ALSA_CHANNEL_RIGHT}


def soundcard_tap_config_from_output(output: dict[str, Any]) -> AlsaStreamTapConfig:
    soundcard = validate_soundcard_output_payload(output.get("soundcard", {}))
    return soundcard_tap_config_from_soundcard(soundcard)


def soundcard_tap_config_from_soundcard(soundcard: dict[str, Any]) -> AlsaStreamTapConfig:
    return AlsaStreamTapConfig(
        stable_id=soundcard["stable_id"],
        output_sample_rate=soundcard["sample_rate"],
        channel_mode=soundcard["channel_mode"],
        software_volume=soundcard["volume"],
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
        <select id="serial" aria-describedby="serial_hint"></select>
      </label>
      <span id="serial_hint" class="hint">Select the SDR dongle by serial number.</span>
      <button id="rescan_devices" type="button">Rescan</button>
      <button id="reset_rtl_device" type="button">Reset SDR</button>
      <div id="device-errors" class="error"></div>
    </section>
    <section>
      <div class="grid">
        <div>
          <label for="gain">Gain</label>
          <input id="gain" type="range" min="0" max="0" step="1" value="0" disabled aria-describedby="gain_hint">
          <span id="gain_label" class="hint">Automatic</span>
          <span id="gain_hint" class="hint">Controls how strongly the SDR amplifies received signals. Generally this should be kept around 30-35 dB. Setting gain too high can cause interference, setting it too low can significantly degrade reception.</span>
          <label><input id="gain_auto" type="checkbox" aria-describedby="gain_auto_hint"> Automatic gain control</label>
          <span id="gain_auto_hint" class="hint">Automatically adjusts the tuner gain based on signal strength. It is best to keep this setting switched off as it can increase the gain too high, causing interference.</span>
        </div>
        <label>PPM Correction
          <input id="ppm_correction" type="number" min="-200" max="200" step="1" aria-describedby="ppm_correction_hint">
        </label>
        <span id="ppm_correction_hint" class="hint">Controls hardware frequency correction for dongles that experience frequency drift.</span>
        <div>
          <label for="alias_filter_strength">Alias filter strength</label>
          <input id="alias_filter_strength" type="range" min="0" max="100" step="1" value="100" aria-describedby="alias_filter_strength_hint">
          <span id="alias_filter_strength_label" class="hint" aria-hidden="true">100%</span>
        </div>
        <div class="row">
          <label><input id="bias_tee" type="checkbox" aria-describedby="bias_tee_hint"> Bias tee</label>
          <span id="bias_tee_hint" class="hint">Enable the RTL-SDR bias tee. Only enable this if attached hardware expects DC power.</span>
        </div>
      </div>
      <div id="alias_filter_strength_hint" class="hint">
        Controls alias filtering when decimating IQ data. Higher values reject more out-of-band signals; lower values can save CPU but may allow more aliasing near the sides of the passband.
      </div>
    </section>
    <section>
      <h3>I/Q test source</h3>
      <p class="hint">Use an interleaved complex float32 I/Q file as the receiver source for testing. Files loop until you switch back to the RTL-SDR.</p>
      <div class="grid">
        <label>Source file
          <select id="iq_test_source_file"></select>
        </label>
        <label>File sample rate in samples per second
          <input id="iq_test_source_sample_rate" type="number" min="192000" step="1" value="1536000">
        </label>
      </div>
      <div id="iq_test_source_directory" class="hint"></div>
      <div id="iq_test_source_seek_controls" class="hint" tabindex="0" role="group" aria-label="I/Q file seek controls">
        Use Up Arrow to seek forward 10 seconds, Down Arrow to seek backward 10 seconds, Page Up to seek forward 1 minute, and Page Down to seek backward 1 minute.
      </div>
      <div id="iq_test_source_position" class="hint"></div>
      <div id="iq-test-source-result" class="message"></div>
      <div class="actions">
        <button id="rescan_iq_test_sources" type="button">Rescan I/Q files</button>
        <button id="start_iq_test_source" type="button">Use I/Q file source</button>
        <button id="seek_iq_test_source_back_60" type="button">Back 1 minute</button>
        <button id="seek_iq_test_source_back_10" type="button">Back 10 seconds</button>
        <button id="seek_iq_test_source_forward_10" type="button">Forward 10 seconds</button>
        <button id="seek_iq_test_source_forward_60" type="button">Forward 1 minute</button>
        <button id="stop_iq_test_source" type="button">Return to RTL-SDR</button>
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
          <input id="iq_duration_minutes" type="number" min="0" max="1440" step="1" value="0" aria-describedby="iq_duration_hint">
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

  <div id="view_create_account" class="view" hidden>
    <section>
      <h2>Create Account</h2>
      <div id="create-account-result" class="message"></div>
      <label>Username
        <input id="new_account_username" autocomplete="off" pattern="[A-Za-z0-9_-]{1,64}">
      </label>
      <label><input id="new_account_read_only" type="checkbox"> Read-only account</label>
      <div class="actions">
        <button id="create_account" type="button">Create account</button>
        <button id="cancel_create_account" type="button">Cancel</button>
      </div>
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
            <td colspan="5" class="hint">No streams configured.</td>
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
          <button id="station_search_button" type="button" disabled>Search</button>
        </div>
        <label id="station_results_label" hidden>Matching Stations
          <select id="station_results" size="8"></select>
        </label>
        <div id="selected_station" class="hint">Enter search text to find a station.</div>
      </div>
      <div id="wizard_step_credentials" class="wizard-step" hidden>
        <p id="icecast_credentials_intro">The next step is to enter your icecast credentials for the service you want to stream to. Enter them below, then click next.</p>
        <div id="icecast_service_help" class="hint"></div>
        <fieldset id="icecast_fields">
          <legend>Icecast Credentials</legend>
          <div class="grid">
            <label id="icecast_host_label">Host
              <input id="icecast_host" type="text" autocomplete="off" aria-describedby="icecast_host_hint">
            </label>
            <span id="icecast_host_hint" class="hint">The hostname, IP address, or web URL of the Icecast server.</span>
            <label id="icecast_port_label">Port
              <input id="icecast_port" type="number" min="1" max="65535" step="1" placeholder="8000" aria-describedby="icecast_port_hint">
            </label>
            <span id="icecast_port_hint" class="hint">The port the Icecast server listens on.</span>
            <label id="icecast_username_label">Username
              <input id="icecast_username" type="text" autocomplete="username" aria-describedby="icecast_username_hint">
            </label>
            <span id="icecast_username_hint" class="hint">The username for authentication to the server.</span>
            <label id="icecast_password_label">Password
              <input id="icecast_password" type="password" autocomplete="current-password" aria-describedby="icecast_password_hint">
            </label>
            <span id="icecast_password_hint" class="hint">The password for authentication to the server.</span>
            <label id="show_icecast_password_label" class="checkbox-row">
              <input id="show_icecast_password" type="checkbox">
              Show password
            </label>
            <label id="icecast_mount_label">Mountpoint
              <input id="icecast_mount" type="text" placeholder="/station.mp3" aria-describedby="icecast_mount_hint">
            </label>
            <span id="icecast_mount_hint" class="hint">The mountpoint where the stream will be accessible to listeners.</span>
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
      <div id="wizard_step_output_type" class="wizard-step" hidden>
        <p>Select the output type.</p>
        <fieldset>
          <legend>Output type</legend>
          <label><input id="wizard_output_type_icecast" name="wizard_output_type" type="radio" value="icecast" checked aria-describedby="wizard_output_type_icecast_hint"> Icecast mountpoint</label>
          <span id="wizard_output_type_icecast_hint" class="hint">Send station audio to an Icecast mountpoint, including private servers or services like GWES Weather Radio, NOAA Weather Radio Org, and WeatherUSA.</span>
          <label><input id="wizard_output_type_soundcard" name="wizard_output_type" type="radio" value="soundcard" aria-describedby="wizard_output_type_soundcard_hint"> Sound card</label>
          <span id="wizard_output_type_soundcard_hint" class="hint">Send station audio to a local sound card, useful for feeding an Emergency Alert System ENDEC.</span>
        </fieldset>
      </div>
      <div id="wizard_step_soundcard_device" class="wizard-step" hidden>
        <p>Select the sound card that should play this stream.</p>
        <p>The sound card must be connected to the machine running NWR Stream Manager, not the phone, tablet, or computer used to access this web interface.</p>
        <label>Sound card
          <select id="wizard_soundcard_device"></select>
        </label>
        <div id="wizard_soundcard_device_hint" class="hint"></div>
      </div>
      <div id="wizard_step_soundcard_controls" class="wizard-step" hidden>
        <p>Choose which output channels to use and adjust the playback volume.</p>
        <fieldset>
          <legend>Output channels</legend>
          <label><input id="wizard_soundcard_channel_both" name="wizard_soundcard_channel" type="radio" value="both" checked> Both left and right</label>
          <label><input id="wizard_soundcard_channel_left" name="wizard_soundcard_channel" type="radio" value="left"> Left</label>
          <label><input id="wizard_soundcard_channel_right" name="wizard_soundcard_channel" type="radio" value="right"> Right</label>
        </fieldset>
        <label>Volume
          <input id="wizard_soundcard_volume" type="range" min="0" max="2" step="0.01" value="1">
        </label>
        <div id="wizard_soundcard_status" class="hint"></div>
      </div>
      <div id="wizard_step_codec" class="wizard-step" hidden>
        <p>What audio codec would you like to use for the stream format? MP3 is generally more compatible, while OGG may give better audio quality at lower internet usage.</p>
        <fieldset id="icecast_format_fieldset">
          <legend>Stream Format</legend>
          <div class="hint">Encoding format for this Icecast mountpoint.</div>
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
            <select id="icecast_sample_rate" aria-describedby="icecast_sample_rate_hint">
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
          <span id="icecast_sample_rate_hint" class="hint">The audio sample rate to encode at for this Icecast mountpoint.</span>
          <label id="icecast_bitrate_label">Bitrate
            <select id="icecast_bitrate" aria-describedby="icecast_bitrate_hint"></select>
          </label>
          <span id="icecast_bitrate_hint" class="hint">Encoder bitrate in Kbps for this Icecast mountpoint.</span>
          <label id="output_enabled_label"><input id="output_enabled" type="checkbox" checked aria-describedby="output_enabled_hint"> Output enabled</label>
          <span id="output_enabled_hint" class="hint">Enable or disable this Icecast output.</span>
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
      <h2 id="stream_settings_title">Stream Settings</h2>
      <div id="stream_settings_station" class="hint"></div>
      <label class="checkbox-row">
        <input id="stream_enabled" type="checkbox">
        Enable this stream
      </label>
      <label class="checkbox-row">
        <input id="stream_monitor_enabled" type="checkbox" aria-describedby="stream_monitor_hint">
        Monitor
      </label>
      <span id="stream_monitor_hint" class="hint">Listen to this stream's audio in your browser.</span>
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
        <label>Show outputs
          <select id="output_type_filter">
            <option value="icecast">Icecast outputs</option>
            <option value="soundcard">Sound card outputs</option>
          </select>
        </label>
        <div id="icecast_outputs_panel">
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
              <td colspan="6" class="hint">No outputs configured.</td>
            </tr>
          </tbody>
        </table>
        </div>
        <div id="soundcard_outputs_panel" hidden>
        <h3>Sound card outputs</h3>
        <table aria-label="Sound card outputs">
          <thead>
            <tr>
              <th>Sound card name</th>
              <th>Channels</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody id="soundcard-outputs-body" aria-live="off">
            <tr>
              <td colspan="4" class="hint">No sound card outputs configured.</td>
            </tr>
          </tbody>
        </table>
        </div>
        <div id="output-list-result" class="message"></div>
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
                <input id="audio_volume_enabled" type="checkbox" aria-describedby="audio_volume_enabled_hint">
                Enable volume multiplier
              </label>
              <span id="audio_volume_enabled_hint" class="hint">Controls the stream volume.</span>
              <label>Multiplier
                <input id="audio_volume_multiplier" type="number" min="0" step="0.1" aria-describedby="audio_volume_multiplier_hint">
              </label>
              <span id="audio_volume_multiplier_hint" class="hint">The volume multiplier.</span>
            </div>
            <div id="audio_effect_comfort_noise" class="audio-effect-panel" hidden>
              <h4>Comfort noise</h4>
              <label class="checkbox-row">
                <input id="audio_comfort_noise_enabled" type="checkbox" aria-describedby="audio_comfort_noise_enabled_hint">
                Enable comfort noise
              </label>
              <span id="audio_comfort_noise_enabled_hint" class="hint">Mixes very quiet white noise into demodulated audio, similar to some analog receivers.</span>
              <label>Level
                <input id="audio_comfort_noise_level" type="number" min="-80" max="-20" step="1" aria-describedby="audio_comfort_noise_level_hint">
              </label>
              <span id="audio_comfort_noise_level_hint" class="hint">Noise level in dB below full scale. Valid range is -80 through -20.</span>
            </div>
            <div id="audio_effect_deemphasis" class="audio-effect-panel" hidden>
              <h4>NFM deemphasis</h4>
              <label class="checkbox-row">
                <input id="audio_deemphasis_enabled" type="checkbox" aria-describedby="audio_deemphasis_enabled_hint">
                Enable NFM deemphasis
              </label>
              <span id="audio_deemphasis_enabled_hint" class="hint">NFM deemphasis filter for reducing high frequency content.</span>
              <label>Time constant
                <input id="audio_deemphasis_tau" type="number" min="0" max="530" step="1" aria-describedby="audio_deemphasis_tau_hint">
              </label>
              <span id="audio_deemphasis_tau_hint" class="hint">Time constant in microseconds. Valid range is 0 through 530. Higher values deemphasize audio more aggressively.</span>
            </div>
            <div id="audio_effect_highpass" class="audio-effect-panel" hidden>
              <h4>Highpass</h4>
              <label class="checkbox-row">
                <input id="audio_highpass_enabled" type="checkbox" aria-describedby="audio_highpass_enabled_hint">
                Enable highpass
              </label>
              <span id="audio_highpass_enabled_hint" class="hint">Gets rid of low-frequency audio, such as 60 Hz hums.</span>
              <label>Frequency
                <input id="audio_highpass_frequency" type="number" min="1" max="900" step="1" aria-describedby="audio_highpass_frequency_hint">
              </label>
              <span id="audio_highpass_frequency_hint" class="hint">Frequency in Hz. Frequencies below this value will be attenuated. To protect the 1050 Hz attention tone, this cannot be set above 900 Hz.</span>
              <label>Sharpness
                <input id="audio_highpass_sharpness" type="number" min="0" max="10" step="0.1" aria-describedby="audio_highpass_sharpness_hint">
              </label>
              <span id="audio_highpass_sharpness_hint" class="hint">Filter sharpness from 0 through 10. 0 is more gentle; 10 cuts off frequencies much more aggressively.</span>
            </div>
            <div id="audio_effect_lowpass" class="audio-effect-panel" hidden>
              <h4>Lowpass</h4>
              <label class="checkbox-row">
                <input id="audio_lowpass_enabled" type="checkbox" aria-describedby="audio_lowpass_enabled_hint">
                Enable lowpass
              </label>
              <span id="audio_lowpass_enabled_hint" class="hint">Gets rid of high-frequency audio. This is different from the NFM deemphasis filter.</span>
              <label>Frequency
                <input id="audio_lowpass_frequency" type="number" min="2200" max="12000" step="1" aria-describedby="audio_lowpass_frequency_hint">
              </label>
              <span id="audio_lowpass_frequency_hint" class="hint">Frequency in Hz. Frequencies above this value will be attenuated. To protect SAME tones, this cannot be set below 2200 Hz.</span>
              <label>Sharpness
                <input id="audio_lowpass_sharpness" type="number" min="0" max="10" step="0.1" aria-describedby="audio_lowpass_sharpness_hint">
              </label>
              <span id="audio_lowpass_sharpness_hint" class="hint">Filter sharpness from 0 through 10. 0 is more gentle; 10 cuts off frequencies much more aggressively.</span>
            </div>
            <div id="audio_effect_notch" class="audio-effect-panel" hidden>
              <h4>Notch filter</h4>
              <label class="checkbox-row">
                <input id="audio_notch_enabled" type="checkbox" aria-describedby="audio_notch_enabled_hint">
                Enable notch filter
              </label>
              <span id="audio_notch_enabled_hint" class="hint">Removes a narrow tone, useful for analog whines.</span>
              <label>Frequency
                <input id="audio_notch_frequency" type="number" min="1" max="12000" step="1" aria-describedby="audio_notch_frequency_hint">
              </label>
              <span id="audio_notch_frequency_hint" class="hint">Frequency in Hz. Frequencies of and near this value will be attenuated. To protect the 1050 Hz attention tone and SAME tones, this cannot be within 900-1100, 1400-1600, or 2000-2200 Hz.</span>
              <label>Sharpness
                <input id="audio_notch_sharpness" type="number" min="0" max="10" step="0.1" aria-describedby="audio_notch_sharpness_hint">
              </label>
              <span id="audio_notch_sharpness_hint" class="hint">Filter sharpness from 0 through 10. 0 is more gentle; 10 cuts off frequencies much more aggressively.</span>
            </div>
          </div>
        </div>
        <div id="audio-effects-result" class="message"></div>
      </div>
      <div id="panel_eas" class="tabpanel" role="tabpanel" aria-labelledby="tab_eas" hidden>
        <h3>EAS recording</h3>
        <div class="grid">
          <label id="eas_enabled_label" class="checkbox-row">
            <input id="eas_enabled" type="checkbox" aria-describedby="eas_enabled_hint">
            Enable EAS recording
          </label>
          <span id="eas_enabled_hint" class="hint">Record EAS alerts received from this station.</span>
          <label>Pre-recording time in seconds
            <input id="eas_pre_seconds" type="number" min="0" max="10" step="1" aria-describedby="eas_pre_seconds_hint">
          </label>
          <span id="eas_pre_seconds_hint" class="hint">Seconds of audio to prepend before the decoded SAME header. Valid range is 0 through 10.</span>
          <label>Post-recording time in seconds
            <input id="eas_post_seconds" type="number" min="0" max="10" step="1" aria-describedby="eas_post_seconds_hint">
          </label>
          <span id="eas_post_seconds_hint" class="hint">Seconds of audio to append after EOM. Valid range is 0 through 10.</span>
          <label>Maximum recording time in seconds
            <input id="eas_max_seconds" type="number" min="1" max="3600" step="1" aria-describedby="eas_max_seconds_hint">
          </label>
          <span id="eas_max_seconds_hint" class="hint">Maximum recording length for a single alert, in seconds.</span>
          <fieldset>
            <legend>Recording format</legend>
            <div class="hint">Recording format for saved alert audio.</div>
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
            <input id="fallback_enabled" type="checkbox" aria-describedby="fallback_enabled_hint">
            Enable fallback audio
          </label>
          <span id="fallback_enabled_hint" class="hint">Fallback audio is played when the stream stops receiving radio audio so listeners are not left with silence.</span>
          <label>Fallback delay in seconds
            <input id="fallback_delay" type="number" min="30" max="120" step="1" aria-describedby="fallback_delay_hint">
          </label>
          <span id="fallback_delay_hint" class="hint">The time, in seconds, to wait before fallback audio starts playing. Valid range is 30 through 120 seconds.</span>
          <label>Seconds before restart
            <input id="fallback_loop_delay" type="number" min="0" max="10" step="1" aria-describedby="fallback_loop_delay_hint">
          </label>
          <span id="fallback_loop_delay_hint" class="hint">How long to wait after the fallback audio finishes before starting it again. Set to 0 for a continuous loop with no gap.</span>
        </div>
        <div class="hint">Uses the packaged default fallback.wav audio file.</div>
        <div id="fallback-result" class="message"></div>
      </div>
    </section>
  </div>

  <div id="view_stream_output" class="view" hidden>
    <section>
      <div id="output_form_panel">
          <h2 id="output_form_title">Add output</h2>
          <div id="icecast_output_edit_panel">
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
                <input id="settings_icecast_host" type="text" autocomplete="off" aria-describedby="settings_icecast_host_hint">
              </label>
              <span id="settings_icecast_host_hint" class="hint">The hostname, IP address, or web URL of the Icecast server.</span>
              <label id="settings_icecast_port_label">Port
                <input id="settings_icecast_port" type="number" min="1" max="65535" step="1" placeholder="8000" aria-describedby="settings_icecast_port_hint">
              </label>
              <span id="settings_icecast_port_hint" class="hint">The port the Icecast server listens on.</span>
              <label id="settings_icecast_username_label">Username
                <input id="settings_icecast_username" type="text" autocomplete="username" aria-describedby="settings_icecast_username_hint">
              </label>
              <span id="settings_icecast_username_hint" class="hint">The username for authentication to the server.</span>
              <label id="settings_icecast_password_label">Password
                <input id="settings_icecast_password" type="password" autocomplete="current-password" aria-describedby="settings_icecast_password_hint">
              </label>
              <span id="settings_icecast_password_hint" class="hint">The password for authentication to the server.</span>
              <label id="settings_show_icecast_password_label" class="checkbox-row">
                <input id="settings_show_icecast_password" type="checkbox">
                Show password
              </label>
              <label id="settings_icecast_mount_label">Mountpoint
                <input id="settings_icecast_mount" type="text" placeholder="/station.mp3" aria-describedby="settings_icecast_mount_hint">
              </label>
              <span id="settings_icecast_mount_hint" class="hint">The mountpoint where the stream will be accessible to listeners.</span>
              <label id="settings_icecast_alt_label" class="checkbox-row" hidden>
                <input id="settings_icecast_alt_enabled" type="checkbox">
                Alternate stream
              </label>
              <label id="settings_icecast_alt_number_label" hidden>Alternate stream number
                <input id="settings_icecast_alt_number" type="number" min="1" max="9" step="1" value="1">
              </label>
              <fieldset id="settings_icecast_format_fieldset">
                <legend>Format</legend>
                <div class="hint">Encoding format for this Icecast mountpoint.</div>
                <label><input id="settings_icecast_format_mp3" name="settings_icecast_format" type="radio" value="mp3" checked> MP3</label>
                <label><input id="settings_icecast_format_ogg" name="settings_icecast_format" type="radio" value="ogg"> OGG</label>
              </fieldset>
              <label id="settings_icecast_sample_rate_label">Sample rate
                <select id="settings_icecast_sample_rate" aria-describedby="settings_icecast_sample_rate_hint">
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
              <span id="settings_icecast_sample_rate_hint" class="hint">The audio sample rate to encode at for this Icecast mountpoint.</span>
              <label id="settings_icecast_bitrate_label">Bitrate
                <select id="settings_icecast_bitrate" aria-describedby="settings_icecast_bitrate_hint"></select>
              </label>
              <span id="settings_icecast_bitrate_hint" class="hint">Encoder bitrate in Kbps for this Icecast mountpoint.</span>
            </div>
          </fieldset>
          <div class="actions">
            <button id="cancel_output_form" type="button">Cancel</button>
            <button id="add_output" type="button">Add output</button>
            <button id="save_output_settings" type="button" hidden>Save changes</button>
          </div>
          </div>
          <div id="soundcard_output_edit_panel" hidden>
            <fieldset>
              <legend>Sound card output</legend>
              <div class="grid">
                <label>Sound card
                  <select id="settings_soundcard_device"></select>
                </label>
                <span id="settings_soundcard_device_hint" class="hint"></span>
                <label>Volume
                  <input id="settings_soundcard_volume" type="range" min="0" max="2" step="0.01" value="1">
                </label>
                <span class="hint">This volume applies only to this sound card output.</span>
                <fieldset>
                  <legend>Channels</legend>
                  <label><input id="settings_soundcard_channel_left" name="settings_soundcard_channel" type="radio" value="left"> Left</label>
                  <label><input id="settings_soundcard_channel_both" name="settings_soundcard_channel" type="radio" value="both" checked> Both left and right</label>
                  <label><input id="settings_soundcard_channel_right" name="settings_soundcard_channel" type="radio" value="right"> Right</label>
                </fieldset>
              </div>
            </fieldset>
            <div class="actions">
              <button id="reset_soundcard_device" type="button" hidden>Reset USB sound card</button>
              <button id="cancel_soundcard_output_form" type="button">Cancel</button>
            </div>
          </div>
        </div>
        <div id="output-result" class="message"></div>
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
const controls = ["serial", "gain", "ppm_correction", "bias_tee", "gain_auto", "alias_filter_strength"];
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
const ADD_OUTPUT_TYPE_STEP = 10;
const ADD_OUTPUT_ICECAST_SERVICE_STEP = 11;
const ADD_OUTPUT_ICECAST_CODEC_STEP = 12;
const ADD_OUTPUT_ICECAST_CREDENTIALS_STEP = 13;
const ADD_OUTPUT_ICECAST_QUALITY_STEP = 14;
const ADD_OUTPUT_SOUNDCARD_DEVICE_STEP = 15;
const ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP = 16;
let applying = false;
let timer = null;
let gainValues = [];
let lastManualGain = null;
let lastControlSignature = "";
let stationResults = [];
let selectedStationKey = "";
let wizardStationOverride = null;
let soundcardDevices = [];
let configuredStreams = [];
let editingStreamId = "";
let editingOutputId = "";
let settingsStreamId = "";
let outputFormMode = "add";
let outputFormDirty = false;
let outputFormOriginalSignature = "";
let icecastOutputTableSignature = "";
let soundcardOutputTableSignature = "";
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
let wizardSoundcardOutputId = "";
let wizardSoundcardPreviewId = "";
let wizardSoundcardUpdateTimer = null;
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
let iqTestSourcesSignature = "";
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
let liveAudioHiddenAt = 0;
let liveAudioNeedsRestart = false;
let currentAccount = null;
let accountsSignature = "";
const MONITOR_UNSTABLE_TIMEOUT_MS = 30000;
const MONITOR_STATS_INTERVAL_MS = 5000;
const LIVE_AUDIO_BACKGROUND_RESTART_MS = 30000;
const WEBRTC_JITTER_BUFFER_TARGET_SECONDS = 0.1;
const SAME_MARK_HZ = 2083.3;
const SAME_SPACE_HZ = 1562.5;
const SAME_BAUD = 520.83;
const SAME_PREAMBLE_BYTE = 0xAB;
const SAME_PREAMBLE_BYTES = 16;
const SAME_TRAILING_NUL_BYTES = 3;
const SAME_CLIENT_PLAYOUT_DELAY_SECONDS = 0.45;
const SAME_LIVE_AUDIO_MUTE_TAIL_SECONDS = 1.0;
const IQ_RECORDER_SAMPLE_RATES = [192000, 256000, 384000, 512000, 768000, 1024000, 1536000];
const IQ_RECORDER_DEFAULT_SAMPLE_RATE = 192000;
const IQ_RECORDER_DEFAULT_DURATION_MINUTES = 0;
const IQ_RECORDER_PREFS_KEY = "nwr-stream-manager:iq-recorder-preferences";
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
let sameAudioContext = null;
let sameLiveAudioMuteTimer = null;
let sameLiveAudioMutedBySame = false;
let sameActiveSources = new Set();

async function ensureSameAudioContext() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (!AudioContextClass) return null;
  if (!sameAudioContext) sameAudioContext = new AudioContextClass();
  if (sameAudioContext.state === "suspended") {
    try {
      await sameAudioContext.resume();
    } catch (error) {
      logClientEvent("warning", "same", "SAME audio context resume failed", {error: error.message});
      console.debug("SAME audio context resume failed", error);
    }
  }
  return sameAudioContext;
}

function samePayloadBytes(payload) {
  const bytes = [];
  for (let index = 0; index < SAME_PREAMBLE_BYTES; index += 1) bytes.push(SAME_PREAMBLE_BYTE);
  const text = String(payload || "");
  for (let index = 0; index < text.length; index += 1) bytes.push(text.charCodeAt(index) & 0x7f);
  for (let index = 0; index < SAME_TRAILING_NUL_BYTES; index += 1) bytes.push(0);
  return bytes;
}

function generateSameBurst(payload, sampleRate) {
  const samplesPerBit = sampleRate / SAME_BAUD;
  const bytes = samePayloadBytes(payload);
  let totalSamples = 0;
  let sampleCursor = 0;
  let sampleTarget = 0;
  for (const byte of bytes) {
    for (let bitIndex = 0; bitIndex < 8; bitIndex += 1) {
      sampleTarget += samplesPerBit;
      const bitSamples = Math.round(sampleTarget) - sampleCursor;
      sampleCursor += bitSamples;
      totalSamples += bitSamples;
    }
  }
  const output = new Float32Array(totalSamples);
  let phase = 0;
  let offset = 0;
  sampleCursor = 0;
  sampleTarget = 0;
  for (const byte of bytes) {
    for (let bitIndex = 0; bitIndex < 8; bitIndex += 1) {
      sampleTarget += samplesPerBit;
      const bitSamples = Math.round(sampleTarget) - sampleCursor;
      sampleCursor += bitSamples;
      const frequency = ((byte >> bitIndex) & 1) ? SAME_MARK_HZ : SAME_SPACE_HZ;
      const step = 2 * Math.PI * frequency / sampleRate;
      for (let index = 0; index < bitSamples; index += 1) {
        output[offset] = 0.73 * Math.sin(phase);
        offset += 1;
        phase += step;
        if (phase >= 2 * Math.PI) phase -= 2 * Math.PI;
      }
    }
  }
  return output;
}

function concatenateFloatAudio(parts) {
  const total = parts.reduce((sum, part) => sum + part.length, 0);
  const output = new Float32Array(total);
  let offset = 0;
  for (const part of parts) {
    output.set(part, offset);
    offset += part.length;
  }
  return output;
}

function generateSameMessageAudio(payload, sampleRate, repetitions = 3, gapSeconds = 1.0) {
  const burst = generateSameBurst(payload, sampleRate);
  const gap = new Float32Array(Math.round(sampleRate * Number(gapSeconds || 0)));
  const parts = [];
  for (let index = 0; index < repetitions; index += 1) {
    if (index) parts.push(gap);
    parts.push(burst);
  }
  return concatenateFloatAudio(parts);
}

function shouldSuppressLiveSamePlayback() {
  return Boolean(receiverPeerConnection && receiverPaused && !monitorStreamId);
}

function clearSameLiveAudioMute() {
  if (sameLiveAudioMuteTimer) {
    clearTimeout(sameLiveAudioMuteTimer);
    sameLiveAudioMuteTimer = null;
  }
  if (!sameLiveAudioMutedBySame) return;
  sameLiveAudioMutedBySame = false;
  const audio = document.getElementById("stream_monitor_audio");
  if (audio && (monitorStreamId || (receiverPeerConnection && receiverPlaying && !receiverPaused))) {
    audio.muted = false;
  }
}

function stopGeneratedSameAudio() {
  for (const source of Array.from(sameActiveSources)) {
    try {
      source.stop();
    } catch (error) {
      console.debug("generated SAME source stop failed", error);
    }
  }
  sameActiveSources.clear();
}

function muteLiveAudioForSame(durationSeconds) {
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio) return;
  const muteMs = Math.max(0, Math.ceil(Number(durationSeconds || 0) * 1000));
  audio.muted = true;
  sameLiveAudioMutedBySame = true;
  if (sameLiveAudioMuteTimer) clearTimeout(sameLiveAudioMuteTimer);
  sameLiveAudioMuteTimer = setTimeout(() => {
    sameLiveAudioMuteTimer = null;
    clearSameLiveAudioMute();
  }, muteMs);
}

async function playSamePayload(payload, repetitions = 3, gapSeconds = 1.0) {
  if (shouldSuppressLiveSamePlayback()) {
    logClientEvent("info", "same", "skipped live SAME playback while receiver is paused", {payload});
    return;
  }
  const context = await ensureSameAudioContext();
  if (!context) return;
  if (context.state === "suspended") {
    logClientEvent("warning", "same", "SAME audio context is still suspended", {payload});
    return;
  }
  const samples = generateSameMessageAudio(payload, context.sampleRate, repetitions, gapSeconds);
  const durationSeconds = samples.length / context.sampleRate;
  muteLiveAudioForSame(SAME_CLIENT_PLAYOUT_DELAY_SECONDS + durationSeconds + SAME_LIVE_AUDIO_MUTE_TAIL_SECONDS);
  const buffer = context.createBuffer(1, samples.length, context.sampleRate);
  buffer.copyToChannel(samples, 0);
  const source = context.createBufferSource();
  source.buffer = buffer;
  source.connect(context.destination);
  sameActiveSources.add(source);
  source.addEventListener("ended", () => {
    sameActiveSources.delete(source);
  });
  source.start(context.currentTime + SAME_CLIENT_PLAYOUT_DELAY_SECONDS);
}

function handleLiveSameEvent(event) {
  if (!event || typeof event !== "object") return;
  if (event.type === "same_header") {
    const rawHeader = event.payload && event.payload.raw_header;
    if (!rawHeader) return;
    logClientEvent("info", "same", "received live SAME header event", {id: event.id, header: rawHeader});
    playSamePayload(rawHeader, Number(event.payload.repetitions || 3), Number(event.payload.gap_seconds || 1.0)).catch(error => {
      logClientEvent("warning", "same", "SAME header playback failed", {error: error.message});
    });
  } else if (event.type === "same_eom") {
    logClientEvent("info", "same", "received live SAME EOM event", {id: event.id});
    playSamePayload("NNNN", Number(event.payload && event.payload.repetitions || 3), Number(event.payload && event.payload.gap_seconds || 1.0)).catch(error => {
      logClientEvent("warning", "same", "SAME EOM playback failed", {error: error.message});
    });
  }
}

function attachLiveEventChannel(channel) {
  if (!channel) return null;
  channel.addEventListener("open", () => {
    logClientEvent("info", "same", "live SAME event channel opened");
  });
  channel.addEventListener("close", () => {
    logClientEvent("info", "same", "live SAME event channel closed");
  });
  channel.addEventListener("message", message => {
    try {
      handleLiveSameEvent(JSON.parse(message.data));
    } catch (error) {
      logClientEvent("warning", "same", "invalid live SAME event", {error: error.message});
      console.debug("invalid live SAME event", error);
    }
  });
  return channel;
}

function createLiveEventChannel(peer) {
  if (!peer || typeof peer.createDataChannel !== "function") return null;
  return attachLiveEventChannel(peer.createDataChannel("nwr-events", {ordered: true}));
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function statusRequestPath() {
  if (!wizardSoundcardPreviewId) return "/api/status";
  return `/api/status?soundcard_preview_id=${encodeURIComponent(wizardSoundcardPreviewId)}`;
}

async function requestWithTimeout(path, options = {}, timeoutMs = 15000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await request(path, {...options, signal: controller.signal});
  } catch (error) {
    if (error && error.name === "AbortError") {
      throw new Error("The RTL-SDR reset did not finish. Physically unplug and replug the dongle if it does not recover.");
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
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

function liveAudioPeerIsUnusable(peer) {
  if (!peer) return false;
  return (
    ["closed", "failed", "disconnected"].includes(peer.connectionState) ||
    ["closed", "failed", "disconnected"].includes(peer.iceConnectionState)
  );
}

function resetLiveAudioElement() {
  stopGeneratedSameAudio();
  clearSameLiveAudioMute();
  const audio = document.getElementById("stream_monitor_audio");
  if (!audio) return;
  audio.pause();
  audio.srcObject = null;
  try {
    audio.load();
  } catch (error) {
    console.debug("live audio element reset failed", error);
  }
}

function markLiveAudioHidden() {
  if (!monitorPeerConnection && !receiverPeerConnection) return;
  liveAudioHiddenAt = Date.now();
}

function markLiveAudioVisible() {
  if (!liveAudioHiddenAt) return false;
  const hiddenForMs = Date.now() - liveAudioHiddenAt;
  liveAudioHiddenAt = 0;
  if (hiddenForMs >= LIVE_AUDIO_BACKGROUND_RESTART_MS) {
    liveAudioNeedsRestart = true;
    logClientEvent("info", "live-audio", "live audio marked stale after page background", {hidden_ms: hiddenForMs});
    return true;
  }
  return false;
}

async function recoverLiveAudioAfterPageRestore() {
  const stale = markLiveAudioVisible();
  scheduleReceiverMediaSessionRefresh();
  if (!stale) return;
  if (monitorPeerConnection) {
    await stopStreamMonitor({notifyServer: true});
    setStreamResult("Monitoring was paused after the page was backgrounded. Start monitoring again to reconnect.");
  }
  if (receiverPeerConnection) {
    await stopWeatherReceiver({notifyServer: true});
    setReceiverResult("Receiver paused after the page was backgrounded. Press Play to reconnect.");
  }
  liveAudioNeedsRestart = false;
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
  if (monitorStreamId === streamId && monitorPeerConnection) {
    if (!liveAudioNeedsRestart && !liveAudioPeerIsUnusable(monitorPeerConnection)) {
      resumeMonitorPlayback();
      return;
    }
    logClientEvent("info", "monitor", "restarting stale monitor WebRTC session", {stream_id: streamId});
    await stopStreamMonitor({notifyServer: true});
    liveAudioNeedsRestart = false;
  }
  logClientEvent("info", "monitor", "monitor start requested", {stream_id: streamId});
  await stopWeatherReceiver({notifyServer: true});
  await stopStreamMonitor({notifyServer: true});
  await ensureSameAudioContext();
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
  createLiveEventChannel(peer);
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
  try {
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
  } catch (error) {
    if (monitorPeerConnection === peer) {
      monitorPeerConnection = null;
      monitorStreamId = "";
      clearMonitorWatchdogs();
      resetLiveAudioElement();
    }
    try {
      peer.close();
    } catch (closeError) {
      console.debug("failed to close failed monitor peer", closeError);
    }
    try {
      await request("/api/monitor/stop", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({client_id: pageMonitorClientId()})
      });
    } catch (stopError) {
      console.debug("monitor cleanup after failed start failed", stopError);
    }
    renderStreams(configuredStreams);
    if (settingsStreamId) renderStreamSettings();
    throw error;
  }
}

async function stopStreamMonitor(options = {}) {
  const notifyServer = options.notifyServer !== false;
  const unstable = options.unstable === true;
  const clientId = pageMonitorClientId();
  const peer = monitorPeerConnection;
  monitorPeerConnection = null;
  monitorStreamId = "";
  clearMonitorWatchdogs();
  resetLiveAudioElement();
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

function refreshReceiverMediaSession() {
  if (!receiverPeerConnection) return;
  updateReceiverMediaSession();
}

function scheduleReceiverMediaSessionRefresh() {
  if (!receiverPeerConnection) return;
  refreshReceiverMediaSession();
  for (const delay of [50, 250, 1000]) {
    window.setTimeout(refreshReceiverMediaSession, delay);
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
    if (liveAudioNeedsRestart || liveAudioPeerIsUnusable(receiverPeerConnection)) {
      logClientEvent("info", "receiver", "restarting stale receiver WebRTC session", {frequency: currentReceiverChannel().label});
      await stopWeatherReceiver({notifyServer: true});
      liveAudioNeedsRestart = false;
    } else {
      logClientEvent("info", "receiver", "receiver resume requested", {frequency: currentReceiverChannel().label});
      receiverPlaying = true;
      receiverPaused = false;
      clearReceiverUnstableTimer();
	      setReceiverAudioTracksEnabled(true);
	      const audio = document.getElementById("stream_monitor_audio");
	      if (audio && audio.srcObject) {
	        audio.muted = false;
	        await audio.play();
	      }
      startReceiverPacketStats();
      updateReceiverMediaSession();
      renderReceiverControls();
      setReceiverResult(`Listening to ${currentReceiverChannel().label}.`, "success");
      return;
    }
  }
  logClientEvent("info", "receiver", "receiver start requested", {frequency: currentReceiverChannel().label});
  await stopStreamMonitor({notifyServer: true});
  await ensureSameAudioContext();
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
  createLiveEventChannel(peer);
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
	    stopGeneratedSameAudio();
	    clearSameLiveAudioMute();
	    setReceiverAudioTracksEnabled(false);
	    if (audio) {
	      audio.muted = true;
	      audio.pause();
	    }
	    scheduleReceiverMediaSessionRefresh();
    renderReceiverControls();
    return;
  }
  receiverPeerConnection = null;
  receiverPaused = false;
  clearReceiverMediaSession();
  resetLiveAudioElement();
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

function friendlySoundcardLabel(device) {
  const card = String(device.card_long_name || device.card_name || device.card_id || "").trim();
  const pcm = String(device.pcm_name || device.pcm_id || "").trim();
  const bus = device.bus === "usb" ? "USB" : device.bus === "pci" ? "PCI" : "";
  const parts = [];
  if (card) parts.push(card);
  if (pcm && !card.toLowerCase().includes(pcm.toLowerCase())) parts.push(pcm);
  if (bus) parts.push(bus);
  const label = parts.join(", ").replace(/\\b(Generic USB|SOF[- ]DSP|sof[- ]hdadsp)\\b/gi, "").replace(/\\s+,/g, ",").replace(/\\s{2,}/g, " ").trim();
  return label || device.display_name || device.hw_device || "Sound card";
}

function soundcardChannelsForMode(mode) {
  if (mode === "left") return ["left"];
  if (mode === "right") return ["right"];
  return ["left", "right"];
}

function soundcardOccupancy(ignoreOutputId = "") {
  const occupied = new Map();
  for (const stream of configuredStreams) {
    for (const output of stream.outputs || []) {
      if ((output.type || "icecast") !== "soundcard") continue;
      if (output.enabled === false) continue;
      if (ignoreOutputId && output.id === ignoreOutputId) continue;
      const soundcard = output.soundcard || {};
      const stableId = soundcard.stable_id || "";
      if (!stableId) continue;
      if (!occupied.has(stableId)) occupied.set(stableId, new Set());
      for (const channel of soundcardChannelsForMode(soundcard.channel_mode || "both")) {
        occupied.get(stableId).add(channel);
      }
    }
  }
  return occupied;
}

function availableSoundcardChannels(stableId, ignoreOutputId = "") {
  const used = soundcardOccupancy(ignoreOutputId).get(stableId) || new Set();
  return ["left", "right"].filter(channel => !used.has(channel));
}

function soundcardDeviceHasAvailableChannels(stableId, ignoreOutputId = "") {
  return availableSoundcardChannels(stableId, ignoreOutputId).length > 0;
}

function chooseAvailableSoundcardChannel(preferred, stableId, ignoreOutputId = "") {
  const available = availableSoundcardChannels(stableId, ignoreOutputId);
  if (available.length === 0) return "";
  const preferredChannels = soundcardChannelsForMode(preferred || "both");
  if (preferred === "both" && available.length === 2) return "both";
  if (preferredChannels.length === 1 && available.includes(preferredChannels[0])) return preferredChannels[0];
  return available[0];
}

function setSoundcardChannelOptions(prefix, stableId, ignoreOutputId = "") {
  const available = availableSoundcardChannels(stableId, ignoreOutputId);
  const controls = {
    left: document.getElementById(`${prefix}_soundcard_channel_left`),
    both: document.getElementById(`${prefix}_soundcard_channel_both`),
    right: document.getElementById(`${prefix}_soundcard_channel_right`)
  };
  for (const [mode, control] of Object.entries(controls)) {
    if (!control) continue;
    const modeChannels = soundcardChannelsForMode(mode);
    const visible = stableId && modeChannels.every(channel => available.includes(channel));
    const label = control.closest("label");
    if (label) label.hidden = !visible;
    control.disabled = !visible;
    if (!visible) control.checked = false;
  }
  const current = Object.entries(controls).find(([_mode, control]) => control && control.checked && !control.disabled);
  if (!current) {
    const next = chooseAvailableSoundcardChannel("both", stableId, ignoreOutputId);
    if (next && controls[next]) controls[next].checked = true;
  }
}

function renderWizardSoundcardDevices() {
  const select = document.getElementById("wizard_soundcard_device");
  if (!select) return;
  const ignoreOutputId = wizardSoundcardOutputId || "";
  const options = soundcardDevices
    .filter(device => soundcardDeviceHasAvailableChannels(device.stable_id, ignoreOutputId))
    .map(device => ({
      value: device.stable_id,
      label: friendlySoundcardLabel(device)
    }));
  if (options.length === 0) {
    options.push({value: "", label: "No sound cards found"});
  }
  const signature = JSON.stringify(options);
  if (select.dataset.signature !== signature) {
    syncSelectOptions(select, options);
    select.dataset.signature = signature;
  }
  setText(
    "wizard_soundcard_device_hint",
    options[0] && options[0].value ? "NWR Stream Manager will open the hardware device directly." : "No sound cards have available channels."
  );
  const selectedStableId = select.value || "";
  setSoundcardChannelOptions("wizard", selectedStableId, ignoreOutputId);
}

function renderSettingsSoundcardDevices(selectedStableId = "") {
  const select = document.getElementById("settings_soundcard_device");
  if (!select) return;
  const options = [];
  const ignoreOutputId = editingOutputId || "";
  const selectedDevice = soundcardDevices.find(device => device.stable_id === selectedStableId);
  if (selectedStableId && !selectedDevice) {
    options.push({value: selectedStableId, label: `Configured sound card, ${selectedStableId} (disconnected)`});
  }
  for (const device of soundcardDevices) {
    if (device.stable_id !== selectedStableId && !soundcardDeviceHasAvailableChannels(device.stable_id, ignoreOutputId)) continue;
    options.push({value: device.stable_id, label: friendlySoundcardLabel(device)});
  }
  if (options.length === 0) {
    options.push({value: "", label: "No sound cards found"});
  }
  const signature = JSON.stringify(options);
  if (select.dataset.signature !== signature) {
    syncSelectOptions(select, options);
    select.dataset.signature = signature;
  }
  setValue("settings_soundcard_device", selectedStableId || (options[0] ? options[0].value : ""));
  setText(
    "settings_soundcard_device_hint",
    selectedStableId && !selectedDevice
      ? "The configured sound card is not currently connected."
      : options[0] && options[0].value
        ? "NWR Stream Manager will open the hardware device directly."
        : "No sound cards have available channels."
  );
  setSoundcardChannelOptions("settings", select.value || "", ignoreOutputId);
  updateSettingsSoundcardResetButton();
}

function updateSettingsSoundcardResetButton() {
  const button = document.getElementById("reset_soundcard_device");
  if (!button) return;
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  const output = selected ? selected.output : null;
  const stableId = document.getElementById("settings_soundcard_device").value || "";
  const device = soundcardDeviceForStableId(stableId);
  button.hidden = accountIsReadOnly() || outputFormMode !== "edit_soundcard" || !output || !device || device.bus !== "usb";
}

async function loadDevices(selected, options = {}) {
  const data = await request("/api/devices");
  soundcardDevices = Array.isArray(data.soundcards) ? data.soundcards : [];
  const select = document.getElementById("serial");
  const selectedDevice = data.devices.find(device => device.serial === selected);
  const optionSignature = data.devices.map(device => [device.serial, deviceLabel(device)]);
  if (selected && !selectedDevice) {
    optionSignature.unshift([selected, configuredDeviceLabel(selected)]);
  }
  const needsChoice = !selected && data.devices.length !== 1;
  const signature = JSON.stringify({options: optionSignature, needsChoice});
  if (options.force || select.dataset.signature !== signature) {
    const selectOptions = [];
    if (selected && !selectedDevice) {
      selectOptions.push({value: selected, label: configuredDeviceLabel(selected)});
    } else if (needsChoice) {
      selectOptions.push({
        value: "",
        label: data.devices.length === 0 ? "No RTL-SDR devices found" : "Select an RTL-SDR..."
      });
    }
    for (const device of data.devices) {
      selectOptions.push({value: device.serial, label: deviceLabel(device)});
    }
    syncSelectOptions(select, selectOptions);
    select.dataset.signature = signature;
  }
  const fallback = !selected && data.devices.length === 1 ? data.devices[0].serial : "";
  setValue("serial", selected || fallback);
  setText("device-errors", data.errors.join(" | "));
  renderWizardSoundcardDevices();
  if (outputFormMode === "edit_soundcard") {
    const selectedOutput = findConfiguredOutput(settingsStreamId, editingOutputId);
    renderSettingsSoundcardDevices(selectedOutput && selectedOutput.output.soundcard ? selectedOutput.output.soundcard.stable_id : "");
  }
  if (currentViewName() === "stream_settings" || currentViewName() === "stream_output") {
    icecastOutputTableSignature = "";
    soundcardOutputTableSignature = "";
    renderStreamSettings();
  }
}

async function loadIqTestSources(options = {}) {
  const data = await request("/api/iq-test-sources");
  const select = document.getElementById("iq_test_source_file");
  const sourceOptions = data.files.map(file => ({
    value: file.name,
    label: `${file.name} (${formatDecimalBytes(file.size_bytes)})`
  }));
  if (sourceOptions.length === 0) {
    sourceOptions.push({value: "", label: "No I/Q files found"});
  }
  const signature = JSON.stringify(sourceOptions);
  if (options.force || iqTestSourcesSignature !== signature) {
    syncSelectOptions(select, sourceOptions);
    iqTestSourcesSignature = signature;
  }
  const active = data.active || null;
  if (active && data.files.some(file => file.name === active.name)) {
    setValue("iq_test_source_file", active.name);
    setValue("iq_test_source_sample_rate", active.sample_rate);
  }
  setText(
    "iq_test_source_directory",
    `Place interleaved complex float32 I/Q files in ${data.directory}.`
  );
  setDisabled(document.getElementById("start_iq_test_source"), sourceOptions.length === 0 || !sourceOptions[0].value);
  return data;
}

function renderIqTestSourceStatus(data) {
  const source = data.source || {};
  const active = source.kind === "iq_file";
  const stats = data.capture_stats || {};
  setText(
    "iq-test-source-result",
    active ? `Using I/Q file source ${source.name} at ${source.sample_rate} S/s.` : ""
  );
  setText(
    "iq_test_source_position",
    active && stats.duration_seconds
      ? `Position: ${formatDuration(Number(stats.position_seconds || 0))} of ${formatDuration(Number(stats.duration_seconds || 0))}.`
      : ""
  );
  setDisabled(document.getElementById("stop_iq_test_source"), !active);
  for (const id of [
    "seek_iq_test_source_back_60",
    "seek_iq_test_source_back_10",
    "seek_iq_test_source_forward_10",
    "seek_iq_test_source_forward_60",
  ]) {
    setDisabled(document.getElementById(id), !active);
  }
}

async function seekIqTestSource(seconds) {
  setText("iq-test-source-result", seconds > 0 ? `Seeking forward ${formatDuration(seconds)}...` : `Seeking backward ${formatDuration(Math.abs(seconds))}...`);
  const data = await request("/api/iq-test-source/seek", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({seconds})
  });
  applyStatus(data, {syncControls: true});
  await loadIqTestSources({force: true});
}

function stationLabel(station) {
  const place = [station.city, station.state].filter(Boolean).join(", ");
  const site = station.site_name && station.site_name !== station.city ? `, ${station.site_name}` : "";
  return `${station.callsign} ${station.frequency} MHz, ${place}${site}`;
}

function stationSearchQuery() {
  const input = document.getElementById("station_search");
  return input ? input.value.trim() : "";
}

function updateStationSearchControls() {
  const button = document.getElementById("station_search_button");
  if (button) setDisabled(button, stationSearchQuery() === "");
}

function clearStationResults(message = "Enter search text to find a station.") {
  stationResults = [];
  selectedStationKey = "";
  const select = document.getElementById("station_results");
  if (select) syncSelectOptions(select, []);
  const label = document.getElementById("station_results_label");
  if (label) label.hidden = true;
  setText("selected_station", message);
  renderWizard();
}

async function searchStations() {
  const query = stationSearchQuery();
  updateStationSearchControls();
  if (query === "") {
    clearStationResults();
    return;
  }
  const data = await request(`/api/stations?q=${encodeURIComponent(query)}&limit=75`);
  stationResults = data.stations || [];
  const select = document.getElementById("station_results");
  const selectOptions = [];
  if (stationResults.length === 0) {
    selectOptions.push({value: "", label: "No stations found"});
  }
  for (const station of stationResults) {
    selectOptions.push({value: station.key, label: stationLabel(station)});
  }
  syncSelectOptions(select, selectOptions);
  const label = document.getElementById("station_results_label");
  if (label) label.hidden = false;
  selectedStationKey = "";
  setText("selected_station", "Select a station to continue.");
  renderWizard();
}

async function runStationSearchFromUi() {
  if (stationSearchQuery() === "") {
    updateStationSearchControls();
    clearStationResults();
    return;
  }
  await searchStations();
  setStreamResult("");
}

function selectedStation() {
  if (wizardStationOverride) return wizardStationOverride;
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
  const outputType = selectedWizardOutputType();
  if (outputType === "soundcard" && wizardMode !== "edit") {
    return {
      station_key: selectedStationKey,
      type: "soundcard",
      preview_id: wizardSoundcardPreviewId,
      soundcard: wizardSoundcardPayload()
    };
  }
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
    type: "icecast",
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

function selectedSettingsSoundcardChannelMode() {
  const selected = document.querySelector("input[name='settings_soundcard_channel']:checked");
  return selected ? selected.value : "both";
}

function settingsSoundcardPayload() {
  return {
    stable_id: document.getElementById("settings_soundcard_device").value || "",
    channel_mode: selectedSettingsSoundcardChannelMode(),
    volume: Number(document.getElementById("settings_soundcard_volume").value),
    sample_rate: 48000
  };
}

function selectedWizardOutputType() {
  const selected = document.querySelector("input[name='wizard_output_type']:checked");
  return selected ? selected.value : "icecast";
}

function selectedWizardSoundcardChannelMode() {
  const selected = document.querySelector("input[name='wizard_soundcard_channel']:checked");
  return selected ? selected.value : "both";
}

function wizardSoundcardPayload() {
  return {
    stable_id: document.getElementById("wizard_soundcard_device").value || "",
    channel_mode: selectedWizardSoundcardChannelMode(),
    volume: Number(document.getElementById("wizard_soundcard_volume").value),
    sample_rate: 48000
  };
}

function adjustWizardSoundcardChannelForDevice() {
  const stableId = document.getElementById("wizard_soundcard_device").value || "";
  const current = selectedWizardSoundcardChannelMode();
  const next = chooseAvailableSoundcardChannel(current, stableId, wizardSoundcardOutputId || "");
  setSoundcardChannelOptions("wizard", stableId, wizardSoundcardOutputId || "");
  if (next) setChecked(`wizard_soundcard_channel_${next}`, true);
}

function adjustSettingsSoundcardChannelForDevice() {
  const stableId = document.getElementById("settings_soundcard_device").value || "";
  const current = selectedSettingsSoundcardChannelMode();
  const next = chooseAvailableSoundcardChannel(current, stableId, editingOutputId || "");
  setSoundcardChannelOptions("settings", stableId, editingOutputId || "");
  if (next) setChecked(`settings_soundcard_channel_${next}`, true);
  updateSettingsSoundcardResetButton();
}

async function resetSoundcardByStableId(stableId, resultSetter = setOutputResult) {
  if (!stableId || accountIsReadOnly()) return;
  resultSetter("Resetting USB sound card...");
  const data = await requestWithTimeout("/api/soundcard-reset", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({stable_id: stableId})
  }, 15000);
  resultSetter(data.message || "Sound card reset finished.", data.success ? "success" : "error");
  applyStatus(data.status || await request(statusRequestPath()), {syncControls: false});
  await loadDevices("", {force: true});
}

function addOutputIcecastStep(streamStep) {
  if (streamStep === 1) return ADD_OUTPUT_ICECAST_SERVICE_STEP;
  if (streamStep === 2) return ADD_OUTPUT_ICECAST_CODEC_STEP;
  if (streamStep === 3) return ADD_OUTPUT_ICECAST_CREDENTIALS_STEP;
  if (streamStep === 4) return ADD_OUTPUT_ICECAST_QUALITY_STEP;
  return streamStep;
}

function streamIcecastStep(addOutputStep) {
  if (addOutputStep === ADD_OUTPUT_ICECAST_SERVICE_STEP) return 1;
  if (addOutputStep === ADD_OUTPUT_ICECAST_CODEC_STEP) return 2;
  if (addOutputStep === ADD_OUTPUT_ICECAST_CREDENTIALS_STEP) return 3;
  if (addOutputStep === ADD_OUTPUT_ICECAST_QUALITY_STEP) return 4;
  return addOutputStep;
}

function wizardUsesOutputSteps() {
  return wizardMode === "add_output" || wizardStep >= ADD_OUTPUT_TYPE_STEP;
}

function wizardIsSoundcardFlow() {
  return (
    selectedWizardOutputType() === "soundcard" &&
    (wizardMode === "add_output" || wizardMode === "add") &&
    wizardStep >= ADD_OUTPUT_TYPE_STEP
  );
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
  const listElement = document.getElementById("output-list-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  const text = String(message || "");
  for (const target of [element, listElement]) {
    if (!target) continue;
    if (target.className !== className) target.className = className;
    if (target.textContent !== text) target.textContent = text;
  }
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
    silence_timeout_seconds: numericControlValue("fallback_delay"),
    loop_delay_seconds: numericControlValue("fallback_loop_delay")
  };
}

function filterPayload(name) {
  const frequencyElement = document.getElementById(`audio_${name}_frequency`);
  return {
    enabled: document.getElementById(`audio_${name}_enabled`).checked,
    frequency: numericControlValue(frequencyElement),
    sharpness: numericControlValue(`audio_${name}_sharpness`)
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

function numericControlValue(elementOrId) {
  const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
  if (!element) return 0;
  const raw = element.dataset.userEditing === "1"
    ? element.dataset.previousValue || element.defaultValue || element.value
    : element.value;
  const value = Number(raw);
  if (Number.isFinite(value)) return value;
  return previousNumberValue(element);
}

function normalizeNumericControl(elementOrId) {
  const element = typeof elementOrId === "string" ? document.getElementById(elementOrId) : elementOrId;
  if (!element || element.value === "") {
    if (element) delete element.dataset.userEditing;
    return;
  }
  const number = Number(element.value);
  if (!Number.isFinite(number)) {
    delete element.dataset.userEditing;
    return;
  }
  const min = element.min === "" ? -Infinity : Number(element.min);
  const max = element.max === "" ? Infinity : Number(element.max);
  const minimum = Number.isFinite(min) ? min : -Infinity;
  const maximum = Number.isFinite(max) ? max : Infinity;
  const clamped = Math.min(maximum, Math.max(minimum, number));
  const step = Number(element.step);
  const normalized = Number.isFinite(step) && step > 0 && step < 1 ? String(clamped) : String(Math.round(clamped));
  if (element.value !== normalized) element.value = normalized;
  element.dataset.previousValue = normalized;
  delete element.dataset.userEditing;
}

function normalizeAudioFrequencyControl(nameOrElement) {
  const element = typeof nameOrElement === "string" ? document.getElementById(`audio_${nameOrElement}_frequency`) : nameOrElement;
  if (!element || element.value === "") return;
  const name = audioFrequencyNameForElement(element);
  if (!name) return;
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

function isNumericControl(element) {
  return Boolean(element && element.tagName === "INPUT" && element.type === "number");
}

function beginNumericTextEdit(element) {
  if (!isNumericControl(element)) return false;
  if (element.dataset.userEditing !== "1") {
    element.dataset.previousValue = element.value || element.dataset.previousValue || element.defaultValue || element.min || "0";
  }
  element.dataset.userEditing = "1";
  return true;
}

function commitNumericControlElement(element, saveCallback = null) {
  if (!isNumericControl(element)) return false;
  if (audioFrequencyNameForElement(element)) {
    commitAudioFrequencyElement(element, false);
  } else {
    normalizeNumericControl(element);
  }
  if (typeof saveCallback === "function") saveCallback();
  return true;
}

function numericSaveCallbackForElement(element) {
  if (!element || !element.id) return null;
  if (controls.includes(element.id)) return scheduleUpdate;
  if (["fallback_delay", "fallback_loop_delay"].includes(element.id)) return scheduleFallbackUpdate;
  if (["eas_pre_seconds", "eas_post_seconds", "eas_max_seconds"].includes(element.id)) return scheduleEasUpdate;
  if (element.id.startsWith("audio_")) return scheduleAudioEffectsUpdate;
  return null;
}

function commitAudioFrequencyElement(element, save = true) {
  const name = audioFrequencyNameForElement(element);
  if (!name) return false;
  normalizeAudioFrequencyControl(element);
  delete element.dataset.userEditing;
  if (save) scheduleAudioEffectsUpdate();
  return true;
}

function audioEffectsPayload() {
  return {
    volume: {
      enabled: document.getElementById("audio_volume_enabled").checked,
      multiplier: numericControlValue("audio_volume_multiplier")
    },
    comfort_noise: {
      enabled: document.getElementById("audio_comfort_noise_enabled").checked,
      level_db: numericControlValue("audio_comfort_noise_level")
    },
    deemphasis: {
      enabled: document.getElementById("audio_deemphasis_enabled").checked,
      tau: numericControlValue("audio_deemphasis_tau")
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
    pre_seconds: numericControlValue("eas_pre_seconds"),
    post_seconds: numericControlValue("eas_post_seconds"),
    max_seconds: numericControlValue("eas_max_seconds"),
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
  if (outputFormMode === "edit_soundcard") {
    outputFormDirty = false;
    document.getElementById("add_output").hidden = true;
    document.getElementById("save_output_settings").hidden = true;
    return;
  }
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

function prepareAddOutput() {
  outputFormMode = "add";
  editingOutputId = "";
  document.getElementById("icecast_output_edit_panel").hidden = false;
  document.getElementById("soundcard_output_edit_panel").hidden = true;
  clearSettingsIcecastForm();
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
  setText("output_form_title", "Add output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
}

function beginAddOutput() {
  if (!settingsStreamId) return;
  if (!prepareAddOutputWizard(settingsStreamId)) return;
  navigateTo("add_stream", {streamId: settingsStreamId}, false, true);
}

function prepareEditOutput(outputId) {
  const selected = findConfiguredOutput(settingsStreamId, outputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return false;
  }
  if ((selected.output.type || "icecast") === "soundcard") {
    return prepareEditSoundcardOutput(selected);
  }
  outputFormMode = "edit";
  editingOutputId = outputId;
  document.getElementById("icecast_output_edit_panel").hidden = false;
  document.getElementById("soundcard_output_edit_panel").hidden = true;
  setSettingsIcecastForm(selected.output.icecast || {});
  outputFormOriginalSignature = outputFormSignature();
  outputFormDirty = false;
  setText("output_form_title", "Edit output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
  return true;
}

function prepareEditSoundcardOutput(selected) {
  outputFormMode = "edit_soundcard";
  editingOutputId = selected.output.id || "";
  outputFormOriginalSignature = "";
  outputFormDirty = false;
  document.getElementById("icecast_output_edit_panel").hidden = true;
  document.getElementById("soundcard_output_edit_panel").hidden = false;
  const soundcard = selected.output.soundcard || {};
  renderSettingsSoundcardDevices(soundcard.stable_id || "");
  setValue("settings_soundcard_volume", soundcard.volume ?? 1);
  setChecked("settings_soundcard_channel_left", soundcard.channel_mode === "left");
  setChecked("settings_soundcard_channel_both", !soundcard.channel_mode || soundcard.channel_mode === "both");
  setChecked("settings_soundcard_channel_right", soundcard.channel_mode === "right");
  setText("output_form_title", "Edit sound card output");
  document.getElementById("output_form_panel").hidden = false;
  setOutputResult("");
  updateOutputFormButtons();
  loadDevices("", {force: true}).catch(error => setOutputResult(error.message, "error"));
  return true;
}

function beginEditOutput(outputId) {
  if (!settingsStreamId || !prepareEditOutput(outputId)) return;
  navigateTo("stream_output", {streamId: settingsStreamId, outputId});
}

function closeOutputForm() {
  document.getElementById("output_form_panel").hidden = true;
  document.getElementById("icecast_output_edit_panel").hidden = false;
  document.getElementById("soundcard_output_edit_panel").hidden = true;
  outputFormDirty = false;
  outputFormOriginalSignature = "";
  editingOutputId = "";
  outputFormMode = "add";
  setOutputResult("");
}

function cancelOutputForm() {
  const streamId = settingsStreamId;
  closeOutputForm();
  if (currentViewName() === "stream_output" && streamId) {
    navigateTo("stream_settings", {streamId}, false, true);
  }
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
  wizardStep = Math.max(0, Math.min(ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP, step));
  renderWizard();
}

function setWizardPanel(id, visible) {
  document.getElementById(id).hidden = !visible;
}

function renderWizard() {
  const editMode = wizardMode === "edit";
  const addOutputMode = wizardMode === "add_output";
  const soundcardMode = wizardIsSoundcardFlow();
  const outputStepMode = !editMode && (addOutputMode || wizardStep >= ADD_OUTPUT_TYPE_STEP);
  const streamStep = wizardUsesOutputSteps() ? streamIcecastStep(wizardStep) : wizardStep;
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
  const stream = addOutputMode ? configuredStreams.find(item => item.id === settingsStreamId) : null;
  const title = editMode ? "Edit Stream Output" : addOutputMode ? `Add output for ${streamCallsign(stream)}` : "Add Stream";
  setText("stream_wizard_title", title);
  if (currentViewName() === "add_stream") setPageTitle(title);
  setWizardPanel("wizard_step_station", !editMode && !addOutputMode && wizardStep === 0);
  setWizardPanel("wizard_step_output_type", outputStepMode && wizardStep === ADD_OUTPUT_TYPE_STEP);
  setWizardPanel("wizard_step_soundcard_device", soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP);
  setWizardPanel("wizard_step_soundcard_controls", soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP);
  setWizardPanel("wizard_step_service", !editMode && !soundcardMode && streamStep === 1);
  setWizardPanel("wizard_step_codec", !editMode && !soundcardMode && streamStep === 2 && needsCodecStep);
  setWizardPanel("wizard_step_credentials", !soundcardMode && (editMode || streamStep === 3 || (!needsCodecStep && streamStep === 2)));
  setWizardPanel("wizard_step_quality", !editMode && !soundcardMode && streamStep === 4 && needsQualityStep);
  document.getElementById("cancel_wizard").hidden = editMode;
  const backButton = document.getElementById("wizard_back");
  backButton.hidden = editMode || (!addOutputMode && wizardStep === 0);
  setDisabled(backButton, addOutputMode && wizardStep === ADD_OUTPUT_TYPE_STEP);
  const next = document.getElementById("wizard_next");
  const finish = document.getElementById("wizard_finish");
  next.hidden = editMode || (soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP) || (!soundcardMode && streamStep === 4) || (!soundcardMode && !needsQualityStep && streamStep >= 3);
  finish.hidden = editMode || !(soundcardMode && wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP || (!soundcardMode && (streamStep === 4 || (!needsQualityStep && streamStep >= 3))));
  document.getElementById("save_output").hidden = !editMode;
  document.getElementById("cancel_output_edit").hidden = !editMode;
  renderWizardSoundcardDevices();
  if (wizardStep === 0) {
    setDisabled(next, !selectedStation());
  } else if (wizardStep === ADD_OUTPUT_TYPE_STEP) {
    setDisabled(next, false);
  } else if (wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
    setDisabled(next, !document.getElementById("wizard_soundcard_device").value);
  } else if (!soundcardMode && (streamStep === 3 || (!needsCodecStep && streamStep === 2))) {
    setDisabled(next, !credentialsComplete());
  } else {
    setDisabled(next, false);
  }
  if (!finish.hidden) {
    setDisabled(finish, !soundcardMode && streamStep === 3 && !credentialsComplete());
  }
}

function resetStreamWizardState() {
  wizardMode = "add";
  wizardStep = 0;
  wizardDirty = true;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  editingStreamId = "";
  editingOutputId = "";
  settingsStreamId = "";
  wizardStationOverride = null;
  selectedStationKey = "";
  setValue("station_search", "");
  updateStationSearchControls();
  clearStationResults();
  clearIcecastForm();
  setChecked("wizard_output_type_icecast", true);
  setChecked("wizard_output_type_soundcard", false);
  setChecked("wizard_soundcard_channel_both", true);
  setValue("wizard_soundcard_volume", 1);
  setStreamResult("");
  renderWizard();
}

function beginStreamWizard() {
  resetStreamWizardState();
  navigateTo("add_stream", {}, false, true);
}

function finishWizard() {
  const returnStreamId = wizardMode === "add_output" ? settingsStreamId : "";
  wizardDirty = false;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  wizardStationOverride = null;
  wizardSoundcardOutputId = "";
  wizardSoundcardPreviewId = "";
  setStreamResult("");
  if (returnStreamId) {
    navigateTo("stream_settings", {streamId: returnStreamId}, false, true);
  } else {
    navigateTo("streams");
  }
}

function prepareAddOutputWizard(streamId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) {
    setOutputResult("Stream was not found.", "error");
    return false;
  }
  settingsStreamId = streamId;
  editingStreamId = streamId;
  editingOutputId = "";
  wizardMode = "add_output";
  wizardStep = ADD_OUTPUT_TYPE_STEP;
  wizardDirty = true;
  icecastAuthPassed = false;
  icecastAuthSignature = "";
  wizardSoundcardOutputId = "";
  wizardSoundcardPreviewId = "";
  wizardStationOverride = stream.station || null;
  selectedStationKey = stream.station && stream.station.key ? stream.station.key : "";
  setChecked("wizard_output_type_icecast", true);
  setChecked("wizard_output_type_soundcard", false);
  setChecked("wizard_soundcard_channel_both", true);
  setValue("wizard_soundcard_volume", 1);
  clearIcecastForm();
  setText("selected_station", `Adding output for ${streamCallsign(stream)} ${stream.station && stream.station.frequency ? `${stream.station.frequency} MHz` : ""}`);
  setStreamResult("");
  renderWizard();
  loadDevices("", {force: true}).catch(error => setStreamResult(error.message, "error"));
  return true;
}

async function authenticateWizardIcecastOutput() {
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
      const outputSteps = wizardUsesOutputSteps();
      if (service === STREAM_SERVICE_NWRORG) {
        setWizardStep(outputSteps ? ADD_OUTPUT_ICECAST_CREDENTIALS_STEP : 3);
      } else {
        setWizardStep(outputSteps ? ADD_OUTPUT_ICECAST_QUALITY_STEP : 4);
      }
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    renderWizard();
  }
}

async function createWizardSoundcardOutput() {
  if (!settingsStreamId) return;
  const button = document.getElementById("wizard_next");
  setDisabled(button, true);
  setStreamResult(wizardSoundcardOutputId ? "Updating soundcard output..." : "Adding soundcard output...");
  try {
    const data = await request("/api/stream-output", {
      method: wizardSoundcardOutputId ? "PATCH" : "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: wizardSoundcardOutputId,
        type: "soundcard",
        enabled: true,
        soundcard: wizardSoundcardPayload()
      })
    });
    renderStreams(data.streams || []);
    if (data.success) {
      wizardSoundcardOutputId = data.output_id || wizardSoundcardOutputId;
      setStreamResult(data.message, "success");
      setWizardStep(ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP);
    } else {
      setStreamResult(data.message, "error");
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
}

async function upsertWizardSoundcardPreview() {
  if (wizardMode !== "add" || selectedWizardOutputType() !== "soundcard") return false;
  const button = document.getElementById("wizard_next");
  setDisabled(button, true);
  setStreamResult(wizardSoundcardPreviewId ? "Updating soundcard preview..." : "Starting soundcard preview...");
  try {
    const data = await request("/api/stream-soundcard-preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        preview_id: wizardSoundcardPreviewId,
        station_key: selectedStationKey,
        soundcard: wizardSoundcardPayload()
      })
    });
    if (data.success) {
      wizardSoundcardPreviewId = data.preview_id || wizardSoundcardPreviewId;
      setStreamResult(data.message, "success");
      setWizardStep(ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP);
      return true;
    }
    setStreamResult(data.message || "Soundcard preview could not be started.", "error");
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
  return false;
}

async function updateWizardSoundcardPreview() {
  if (wizardMode !== "add" || !wizardSoundcardPreviewId) return;
  try {
    const data = await request("/api/stream-soundcard-preview", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        preview_id: wizardSoundcardPreviewId,
        station_key: selectedStationKey,
        soundcard: wizardSoundcardPayload()
      })
    });
    setText("wizard_soundcard_status", data.success ? "Soundcard preview updated." : data.message);
  } catch (error) {
    setText("wizard_soundcard_status", error.message);
  }
}

async function discardWizardSoundcardPreview() {
  if (!wizardSoundcardPreviewId) return;
  const previewId = wizardSoundcardPreviewId;
  wizardSoundcardPreviewId = "";
  try {
    await request(`/api/stream-soundcard-preview?preview_id=${encodeURIComponent(previewId)}`, {
      method: "DELETE"
    });
  } catch (error) {
    setStreamResult(error.message, "error");
  }
}

function sendWizardSoundcardPreviewDiscardBeacon() {
  if (!wizardSoundcardPreviewId) return;
  const previewId = wizardSoundcardPreviewId;
  wizardSoundcardPreviewId = "";
  const payload = JSON.stringify({preview_id: previewId});
  if (navigator.sendBeacon) {
    navigator.sendBeacon("/api/stream-soundcard-preview/discard", new Blob([payload], {type: "application/json"}));
    return;
  }
  fetch("/api/stream-soundcard-preview/discard", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: payload,
    keepalive: true
  }).catch(() => {});
}

async function updateWizardSoundcardOutput() {
  if (!settingsStreamId || !wizardSoundcardOutputId) return;
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: wizardSoundcardOutputId,
        type: "soundcard",
        enabled: true,
        soundcard: wizardSoundcardPayload()
      })
    });
    renderStreams(data.streams || []);
    setText("wizard_soundcard_status", data.success ? "Soundcard output updated." : data.message);
  } catch (error) {
    setText("wizard_soundcard_status", error.message);
  }
}

function scheduleWizardSoundcardUpdate() {
  wizardDirty = true;
  if (wizardMode === "add" && wizardSoundcardPreviewId) {
    if (wizardSoundcardUpdateTimer) window.clearTimeout(wizardSoundcardUpdateTimer);
    wizardSoundcardUpdateTimer = window.setTimeout(() => {
      wizardSoundcardUpdateTimer = null;
      updateWizardSoundcardPreview();
    }, 120);
    return;
  }
  if (!wizardSoundcardOutputId) {
    renderWizard();
    return;
  }
  if (wizardSoundcardUpdateTimer) window.clearTimeout(wizardSoundcardUpdateTimer);
  wizardSoundcardUpdateTimer = window.setTimeout(() => {
    wizardSoundcardUpdateTimer = null;
    updateWizardSoundcardOutput();
  }, 120);
}

let settingsSoundcardUpdateTimer = null;

async function updateSettingsSoundcardOutput() {
  if (!settingsStreamId || !editingOutputId || outputFormMode !== "edit_soundcard") return;
  const selected = findConfiguredOutput(settingsStreamId, editingOutputId);
  if (!selected) {
    setOutputResult("Stream output was not found.", "error");
    return;
  }
  try {
    const data = await request("/api/stream-output", {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        stream_id: settingsStreamId,
        output_id: editingOutputId,
        type: "soundcard",
        enabled: selected.output.enabled !== false,
        soundcard: settingsSoundcardPayload()
      })
    });
    renderStreams(data.streams || []);
    setOutputResult(data.message || "Sound card output updated.", data.success ? "success" : "error");
  } catch (error) {
    setOutputResult(error.message, "error");
  }
}

function scheduleSettingsSoundcardUpdate() {
  outputFormDirty = false;
  if (settingsSoundcardUpdateTimer) window.clearTimeout(settingsSoundcardUpdateTimer);
  settingsSoundcardUpdateTimer = window.setTimeout(() => {
    settingsSoundcardUpdateTimer = null;
    updateSettingsSoundcardOutput();
  }, 120);
}

async function discardWizardSoundcardOutput() {
  if (!settingsStreamId || !wizardSoundcardOutputId) return;
  const outputId = wizardSoundcardOutputId;
  wizardSoundcardOutputId = "";
  try {
    const data = await request(
      `/api/stream-output?stream_id=${encodeURIComponent(settingsStreamId)}&output_id=${encodeURIComponent(outputId)}`,
      {method: "DELETE"}
    );
    renderStreams(data.streams || []);
  } catch (error) {
    setStreamResult(error.message, "error");
  }
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
  if (outputId) {
    settingsStreamId = streamId;
    if (prepareEditOutput(outputId)) navigateTo("stream_output", {streamId, outputId});
    return;
  }
  showStreamSettings(streamId);
}

function showStreamSettings(streamId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) {
    setStreamResult("Stream was not found.", "error");
    return;
  }
  settingsStreamId = streamId;
  icecastOutputTableSignature = "";
  soundcardOutputTableSignature = "";
  easSignature = "";
  audioEffectsSignature = "";
  selectAudioEffect(selectedAudioEffect, false);
  closeOutputForm();
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
  renderOutputPanels();
  renderIcecastOutputsTable(stream);
  renderSoundcardOutputsTable(stream);
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

function outputStatusLabelForOutput(output, status) {
  if ((output.type || "icecast") === "soundcard" && status === "connected") return "Enabled";
  return outputStatusLabel(status);
}

function outputDestination(icecast) {
  if (!icecast || !icecast.host) return "Unknown";
  return `${icecast.host}:${icecast.port}${icecast.mount}`;
}

function soundcardDestination(soundcard) {
  const device = soundcardDevices.find(item => item.stable_id === soundcard.stable_id);
  return device ? friendlySoundcardLabel(device) : soundcard.stable_id || "Sound card";
}

function soundcardDeviceForStableId(stableId) {
  return soundcardDevices.find(item => item.stable_id === stableId) || null;
}

function soundcardOutputUsbDevice(output) {
  if (!output || output.type !== "soundcard") return null;
  const stableId = output.soundcard && output.soundcard.stable_id ? output.soundcard.stable_id : "";
  const device = soundcardDeviceForStableId(stableId);
  return device && device.bus === "usb" ? device : null;
}

function outputRowDestination(output) {
  if (output.type === "soundcard") return soundcardDestination(output.soundcard || {});
  return outputDestination(output.icecast || {});
}

function outputRowFormat(output) {
  if (output.type === "soundcard") return "Soundcard";
  return String((output.icecast || {}).format || "mp3").toUpperCase();
}

function outputRowSampleRate(output) {
  if (output.type === "soundcard") return `${(output.soundcard || {}).sample_rate || 48000} Hz`;
  return `${(output.icecast || {}).sample_rate || DEFAULT_STREAM_SAMPLE_RATE} Hz`;
}

function outputRowBitrate(output) {
  if (output.type === "soundcard") {
    const mode = (output.soundcard || {}).channel_mode || "both";
    const volume = Number((output.soundcard || {}).volume ?? 1).toFixed(2);
    return `${mode}, volume ${volume}`;
  }
  const icecast = output.icecast || {};
  return `${icecast.bitrate || DEFAULT_STREAM_BITRATES[icecast.format || "mp3"]} Kbps`;
}

function outputRows(stream) {
  if (!stream) return [];
  return streamOutputs(stream).map(output => ({
    stream,
    output,
    icecast: output.icecast || {},
    soundcard: output.soundcard || {},
    status: outputStatusFor(stream, output)
  }));
}

function outputRowsByType(stream, type) {
  return outputRows(stream).filter(row => (row.output.type || "icecast") === type);
}

function outputTableNextSignature(rows) {
  return JSON.stringify(rows.map(row => ({
    id: row.output.id || "",
    enabled: row.output.enabled !== false,
    type: row.output.type || "icecast",
    destination: outputRowDestination(row.output),
    format: outputRowFormat(row.output),
    sample_rate: outputRowSampleRate(row.output),
    bitrate: outputRowBitrate(row.output),
    bus: row.output.type === "soundcard" ? (soundcardDeviceForStableId((row.output.soundcard || {}).stable_id || "") || {}).bus || "" : "",
    status: row.status
  })));
}

function renderOutputPanels() {
  const selected = document.getElementById("output_type_filter").value || "icecast";
  document.getElementById("icecast_outputs_panel").hidden = selected !== "icecast";
  document.getElementById("soundcard_outputs_panel").hidden = selected !== "soundcard";
}

function renderIcecastOutputsTable(stream) {
  const tbody = document.getElementById("icecast-outputs-body");
  const rows = outputRowsByType(stream, "icecast");
  const nextSignature = outputTableNextSignature(rows);
  if (nextSignature === icecastOutputTableSignature) return;
  if (containsFocusedElement(tbody)) return;
  icecastOutputTableSignature = nextSignature;
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

function renderSoundcardOutputsTable(stream) {
  const tbody = document.getElementById("soundcard-outputs-body");
  const rows = outputRowsByType(stream, "soundcard");
  const nextSignature = outputTableNextSignature(rows);
  if (nextSignature === soundcardOutputTableSignature) return;
  if (containsFocusedElement(tbody)) return;
  soundcardOutputTableSignature = nextSignature;
  tbody.innerHTML = "";
  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 4;
    cell.className = "hint";
    cell.textContent = "No sound card outputs configured.";
    row.appendChild(cell);
    tbody.appendChild(row);
    return;
  }
  for (const row of rows) {
    tbody.appendChild(soundcardOutputRow(row));
  }
}

function icecastOutputRow(row) {
  const tr = document.createElement("tr");
  tr.appendChild(tableCell(outputRowDestination(row.output)));
  tr.appendChild(tableCell(outputRowFormat(row.output)));
  tr.appendChild(tableCell(outputRowSampleRate(row.output)));
  tr.appendChild(tableCell(outputRowBitrate(row.output)));
  const statusCell = tableCell(outputStatusLabelForOutput(row.output, row.status));
  statusCell.className = `status-text status-${row.status}`;
  tr.appendChild(statusCell);
  tr.appendChild(outputActionsCell(row.stream, row.output, row.status));
  return tr;
}

function soundcardChannelLabel(mode) {
  if (mode === "left") return "Left";
  if (mode === "right") return "Right";
  return "Both left and right";
}

function soundcardOutputRow(row) {
  const tr = document.createElement("tr");
  const soundcard = row.output.soundcard || {};
  tr.appendChild(tableCell(soundcardDestination(soundcard)));
  tr.appendChild(tableCell(soundcardChannelLabel(soundcard.channel_mode || "both")));
  const statusCell = tableCell(outputStatusLabelForOutput(row.output, row.status));
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
  button.setAttribute("aria-label", `More actions for ${outputRowDestination(output)}`);
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
  const reset = document.createElement("button");
  reset.type = "button";
  reset.textContent = "Reset USB sound card";
  reset.setAttribute("role", "menuitem");
  reset.dataset.action = "reset-soundcard-output";
  reset.dataset.streamId = stream.id || "";
  reset.dataset.outputId = output.id || "";
  if (!accountIsReadOnly()) {
    if (output.enabled === false || canDisableIcecastOutput(stream, output)) {
      menu.appendChild(toggle);
    }
    menu.appendChild(edit);
    if (soundcardOutputUsbDevice(output)) {
      menu.appendChild(reset);
    }
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
  if (containsFocusedElement(tbody)) return;
  activeStreamsSignature = nextSignature;
  tbody.innerHTML = "";

  if (rows.length === 0) {
    const row = document.createElement("tr");
    row.id = "active-streams-empty";
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "hint";
    cell.textContent = "No streams configured.";
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
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More actions";
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-label", `More actions for ${stationLabelForActionMenu(stream)}`);
  button.dataset.activeStreamMenu = stream.id || "";
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
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "Remove stream";
  remove.setAttribute("role", "menuitem");
  remove.dataset.action = "remove-active-stream";
  remove.dataset.streamId = stream.id || "";
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
  if (containsFocusedElement(list)) return;
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

function gainTextForIndex(index) {
  if (!gainValues.length) return "No manual gain values available";
  const safeIndex = Math.max(0, Math.min(gainValues.length - 1, Number(index) || 0));
  return `${gainValues[safeIndex]} dB`;
}

function updateGainSliderText() {
  const gain = document.getElementById("gain");
  const automatic = document.getElementById("gain_auto").checked;
  const text = automatic ? "Automatic" : gainTextForIndex(gain.value);
  setText("gain_label", text);
  setAttributeIfChanged(gain, "aria-valuetext", automatic ? "Automatic gain control" : text);
}

function storedManualGain() {
  if (lastManualGain !== null) return lastManualGain;
  try {
    const stored = Number(window.localStorage.getItem("nwr-stream-manager:last-manual-gain"));
    if (Number.isFinite(stored)) {
      lastManualGain = stored;
      return stored;
    }
  } catch (error) {
    return null;
  }
  return null;
}

function rememberManualGain(value) {
  const gain = Number(value);
  if (!Number.isFinite(gain)) return;
  lastManualGain = gain;
  try {
    window.localStorage.setItem("nwr-stream-manager:last-manual-gain", String(gain));
  } catch (error) {
    // localStorage may be unavailable; keeping the in-memory value is enough.
  }
}

function rememberManualGainFromSlider() {
  if (!gainValues.length) return;
  const index = Number(document.getElementById("gain").value);
  if (!Number.isInteger(index) || index < 0 || index >= gainValues.length) return;
  rememberManualGain(gainValues[index]);
}

function restoreRememberedManualGain() {
  if (!gainValues.length) return;
  const remembered = storedManualGain();
  if (remembered === null) return;
  setValue("gain", gainIndexFor(remembered));
}

function syncSelectOptions(select, options) {
  const active = document.activeElement;
  const existing = new Map(Array.from(select.options).map(option => [option.value, option]));
  const wanted = new Set(options.map(option => String(option.value)));
  for (const option of Array.from(select.options)) {
    if (!wanted.has(option.value)) option.remove();
  }
  let cursor = select.firstChild;
  for (const spec of options) {
    const value = String(spec.value);
    const label = String(spec.label);
    let option = existing.get(value);
    if (!option || option.parentElement !== select) {
      option = document.createElement("option");
      option.value = value;
    }
    if (option.textContent !== label) option.textContent = label;
    if (option !== cursor) {
      select.insertBefore(option, cursor);
    } else {
      cursor = cursor.nextSibling;
    }
  }
  if (active === select && document.activeElement !== select) select.focus();
}

function containsFocusedElement(element) {
  const active = document.activeElement;
  return Boolean(active && element && element.contains(active));
}

function controlSignature(data) {
  const s = data.settings;
  return JSON.stringify({
    serial: s.serial || "",
    gain: s.gain,
    ppm_correction: s.ppm_correction,
    bias_tee: s.bias_tee,
    alias_filter_strength: s.alias_filter_strength,
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
  if (element.type === "number" && element.dataset.userEditing !== "1") {
    element.dataset.previousValue = text;
  }
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

function setControlBusy(element, value) {
  if (!element) return;
  const busy = Boolean(value);
  if (busy) {
    element.dataset.busy = "true";
    element.setAttribute("aria-disabled", "true");
  } else {
    delete element.dataset.busy;
    element.removeAttribute("aria-disabled");
  }
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

function setCreateAccountResult(message, kind = "") {
  const element = document.getElementById("create-account-result");
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
    icecastOutputTableSignature = "";
    soundcardOutputTableSignature = "";
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
  return ["rtl", "add_stream", "stream_settings", "stream_output", "iq_recorder_start", "eas_alert_delete", "accounts", "create_account"].includes(view);
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
  if (containsFocusedElement(tbody)) return;
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
  const button = document.getElementById("copy_account_secret");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(secret);
    } else if (!copyTextWithSelectionFallback(input)) {
      throw new Error("clipboard API is unavailable");
    }
    setAccountResult("Temporary password copied to clipboard.", "success");
    if (button) button.focus();
  } catch (error) {
    setAccountResult("Copy failed. Select the temporary password and copy it manually.", "error");
  }
}

function copyTextWithSelectionFallback(input) {
  if (!input || !document.queryCommandSupported || !document.queryCommandSupported("copy")) {
    return false;
  }
  const active = document.activeElement;
  input.focus();
  input.select();
  input.setSelectionRange(0, input.value.length);
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch (error) {
    copied = false;
  }
  input.setSelectionRange(input.value.length, input.value.length);
  if (active && active.focus && active !== input) active.focus();
  return copied;
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
  setValue("new_account_username", "");
  setChecked("new_account_read_only", false);
  showAccountSecret(data.secret || "");
  await loadAccounts();
  navigateTo("accounts", {}, false, true);
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
    syncSelectOptions(
      select,
      streams.map(stream => ({value: stream.id || "", label: easAlertStreamLabel(stream)}))
    );
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
  if (containsFocusedElement(list)) return;
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
    setPageTitle(`Details for alert ${alert.event_type || "EAS alert"} issued on ${alert.issued_at || "an unknown time"}`);
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

function setPageTitle(title) {
  const text = String(title || "NWR Stream Manager").trim() || "NWR Stream Manager";
  const full = text === "NWR Stream Manager" ? text : `${text} - NWR Stream Manager`;
  if (document.title !== full) document.title = full;
}

function defaultViewTitle(name) {
  const titles = {
    dashboard: "Dashboard",
    rtl: "Configure RTL-SDR",
    receiver: "Weather Radio Receiver",
    iq_recorder: "I/Q Recorder",
    iq_recorder_start: "New I/Q Recording",
    iq_recording_download: "Download I/Q Recording",
    logs: "Logs",
    accounts: "Manage Accounts",
    create_account: "Create Account",
    change_password: "Change Account Password",
    streams: "Manage Streams",
    add_stream: "Add Stream",
    stream_settings: "Stream Settings",
    stream_output: "Icecast Output",
    eas_alerts: "EAS Alerts",
    eas_alert_export: "Export Alerts",
    eas_alert_delete: "Delete Alerts",
    eas_alert_detail: "EAS Alert Details"
  };
  return titles[name] || "NWR Stream Manager";
}

function streamCallsign(stream) {
  const station = stream && stream.station ? stream.station : {};
  return station.callsign || "stream";
}

function focusViewHeading(name) {
  const view = document.getElementById(`view_${name}`);
  if (!view) return;
  const heading = view.querySelector("h1, h2");
  if (!heading) return;
  if (!heading.hasAttribute("tabindex")) heading.setAttribute("tabindex", "-1");
  window.requestAnimationFrame(() => {
    try {
      heading.focus({preventScroll: true});
    } catch (error) {
      heading.focus();
    }
  });
}

function showView(name) {
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view_${name}`;
  }
  setPageTitle(defaultViewTitle(name));
  focusViewHeading(name);
  const moreButton = document.getElementById("nav_more_button");
  for (const item of document.querySelectorAll("nav [data-view]")) {
    if (
      item.dataset.view === name ||
      (item.dataset.view === "streams" && ["add_stream", "stream_settings", "stream_output"].includes(name)) ||
      (item.dataset.view === "iq_recorder" && ["iq_recorder_start", "iq_recording_download"].includes(name)) ||
      (item.dataset.view === "eas_alerts" && ["eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(name)) ||
      item.dataset.view === "accounts" && ["accounts", "create_account"].includes(name) ||
      item.dataset.view === "change_password" && name === "change_password"
    ) {
      item.setAttribute("aria-current", "page");
    } else {
      item.removeAttribute("aria-current");
    }
  }
  if (moreButton) {
    if (["receiver", "iq_recorder", "iq_recorder_start", "iq_recording_download", "logs", "accounts", "create_account", "change_password"].includes(name)) {
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
  if (name === "create_account") query.set("view", "create_account");
  if (name === "change_password") query.set("view", "change_password");
  if (name === "streams") query.set("view", "streams");
  if (name === "add_stream") {
    query.set("view", "add_stream");
    if (params.streamId) query.set("stream", params.streamId);
  }
  if (name === "stream_settings") {
    query.set("view", "stream_settings");
    if (params.streamId) query.set("stream", params.streamId);
  }
  if (name === "stream_output") {
    query.set("view", "stream_output");
    if (params.streamId) query.set("stream", params.streamId);
    if (params.outputId) query.set("output", params.outputId);
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
  if (["dashboard", "rtl", "receiver", "iq_recorder", "iq_recorder_start", "iq_recording_download", "logs", "accounts", "create_account", "change_password", "streams", "add_stream", "stream_settings", "stream_output", "eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(view)) {
    return {
      view,
      streamId: query.get("stream") || "",
      outputId: query.get("output") || "",
      alertId: query.get("alert") || "",
      recordingId: query.get("recording") || "",
      page: Math.max(1, Number(query.get("page") || 1))
    };
  }
  return {view: "dashboard", streamId: "", outputId: "", alertId: "", page: 1};
}

function routeState(view, params = {}) {
  return {
    view,
    streamId: params.streamId || "",
    outputId: params.outputId || "",
    alertId: params.alertId || "",
    recordingId: params.recordingId || "",
    page: Math.max(1, Number(params.page || 1))
  };
}

function replaceCurrentRoute(view, params = {}) {
  history.replaceState(routeState(view, params), "", routeForView(view, params));
}

function applyRoute(route) {
  if (accountIsReadOnly() && isReadOnlyRestrictedView(route.view)) {
    replaceCurrentRoute("dashboard");
    showView("dashboard");
    return;
  }
  if (route.view === "add_stream") {
    if (route.streamId) {
      if (wizardMode !== "add_output" || settingsStreamId !== route.streamId) {
        if (!prepareAddOutputWizard(route.streamId)) {
          replaceCurrentRoute("streams");
          showView("streams");
          return;
        }
      }
    } else if (wizardMode !== "add") {
      resetStreamWizardState();
    }
    showView("add_stream");
    renderWizard();
    return;
  }
  if (route.view === "stream_settings") {
    const streamId = route.streamId || settingsStreamId;
    const stream = configuredStreams.find(item => item.id === streamId);
    if (stream) {
      settingsStreamId = streamId;
      icecastOutputTableSignature = "";
      soundcardOutputTableSignature = "";
      easSignature = "";
      audioEffectsSignature = "";
      closeOutputForm();
      const station = stream.station || {};
      setText("stream_settings_title", `Edit stream ${streamCallsign(stream)}`);
      setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
      renderStreamSettings();
      showView("stream_settings");
      setPageTitle(`Edit stream ${streamCallsign(stream)}`);
      return;
    }
    closeOutputForm();
    replaceCurrentRoute("streams");
    showView("streams");
    return;
  }
  if (route.view === "stream_output") {
    const streamId = route.streamId || settingsStreamId;
    const stream = configuredStreams.find(item => item.id === streamId);
    if (!stream) {
      closeOutputForm();
      replaceCurrentRoute("streams");
      showView("streams");
      return;
    }
    if (!route.outputId) {
      if (prepareAddOutputWizard(streamId)) {
        showView("add_stream");
        renderWizard();
      } else {
        showView("streams");
      }
      return;
    }
    settingsStreamId = streamId;
    const station = stream.station || {};
    setText("stream_settings_station", `${station.callsign || "Unknown"} ${station.frequency || ""} MHz`);
    if (editingOutputId !== route.outputId || !["edit", "edit_soundcard"].includes(outputFormMode)) {
      if (!prepareEditOutput(route.outputId)) {
        closeOutputForm();
        replaceCurrentRoute("stream_settings", {streamId});
        renderStreamSettings();
        showView("stream_settings");
        setPageTitle(`Edit stream ${streamCallsign(stream)}`);
        return;
      }
    }
    const selected = findConfiguredOutput(streamId, route.outputId);
    const isSoundcard = selected && (selected.output.type || "icecast") === "soundcard";
    setText("output_form_title", `${isSoundcard ? "Edit sound card output" : "Edit output"} for ${streamCallsign(stream)}`);
    showView("stream_output");
    setPageTitle(`${isSoundcard ? "Edit sound card output" : "Edit output"} for ${streamCallsign(stream)}`);
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
  if (route.view === "rtl") {
    loadIqTestSources({force: true}).catch(error => setText("iq-test-source-result", error.message));
  }
  if (route.view === "receiver") renderReceiverControls();
  if (route.view === "iq_recorder") {
    loadIqRecordings().catch(error => setIqRecorderResult(error.message, "error"));
  }
  if (route.view === "iq_recorder_start") {
    restoreIqRecorderPreferences();
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
  if (route.view === "create_account") {
    if (!accountIsOwner()) {
      replaceCurrentRoute("change_password");
      showView("change_password");
      return;
    }
    setCreateAccountResult("");
    showView("create_account");
    return;
  }
  showView(route.view);
}

function outputFormHasUnsavedChanges() {
  return currentViewName() === "stream_output" && outputFormMode !== "edit_soundcard" && outputFormDirty;
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
  const leavingOutputForm = currentViewName() === "stream_output" && view !== "stream_output";
  if (leavingOutputForm) closeOutputForm();
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
  if (view === "add_stream" && wizardMode === "add_output") return {streamId: settingsStreamId};
  if (view === "stream_settings") return {streamId: settingsStreamId};
  if (view === "stream_output") return {streamId: settingsStreamId, outputId: editingOutputId};
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
    if (rate === IQ_RECORDER_DEFAULT_SAMPLE_RATE) option.selected = true;
    select.appendChild(option);
  }
}

function defaultIqRecorderPreferences() {
  return {
    mode: "stream",
    stream_id: "",
    sample_rate: IQ_RECORDER_DEFAULT_SAMPLE_RATE,
    duration_minutes: IQ_RECORDER_DEFAULT_DURATION_MINUTES
  };
}

function loadIqRecorderPreferences() {
  const defaults = defaultIqRecorderPreferences();
  try {
    const parsed = JSON.parse(window.localStorage.getItem(IQ_RECORDER_PREFS_KEY) || "{}");
    if (!parsed || typeof parsed !== "object") return defaults;
    const sampleRate = Number(parsed.sample_rate);
    const durationMinutes = Number(parsed.duration_minutes);
    const mode = parsed.mode === "spectrum" ? "spectrum" : "stream";
    return {
      mode,
      stream_id: String(parsed.stream_id || ""),
      sample_rate: IQ_RECORDER_SAMPLE_RATES.includes(sampleRate) ? sampleRate : defaults.sample_rate,
      duration_minutes: Number.isFinite(durationMinutes)
        ? Math.max(0, Math.min(1440, Math.round(durationMinutes)))
        : defaults.duration_minutes
    };
  } catch (error) {
    return defaults;
  }
}

function rememberIqRecorderPreferences() {
  const prefs = {
    mode: currentIqMode(),
    stream_id: document.getElementById("iq_stream_select").value || "",
    sample_rate: Number(document.getElementById("iq_sample_rate").value) || IQ_RECORDER_DEFAULT_SAMPLE_RATE,
    duration_minutes: Math.max(0, Math.min(1440, Number(document.getElementById("iq_duration_minutes").value || 0) || 0))
  };
  try {
    window.localStorage.setItem(IQ_RECORDER_PREFS_KEY, JSON.stringify(prefs));
  } catch (error) {
    // localStorage may be unavailable; the form still works with in-page values.
  }
}

function restoreIqRecorderPreferences() {
  populateIqSampleRates();
  renderIqStreamOptions(true);
  const prefs = loadIqRecorderPreferences();
  const mode = prefs.mode === "spectrum" ? "spectrum" : "stream";
  setChecked("iq_mode_stream", mode === "stream");
  setChecked("iq_mode_spectrum", mode === "spectrum");
  const streamSelect = document.getElementById("iq_stream_select");
  if (streamSelect && prefs.stream_id && Array.from(streamSelect.options).some(option => option.value === prefs.stream_id)) {
    setValue("iq_stream_select", prefs.stream_id);
  }
  setValue("iq_sample_rate", String(prefs.sample_rate));
  setValue("iq_duration_minutes", String(prefs.duration_minutes));
  renderIqModeFields();
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
  syncSelectOptions(select, options.map(item => ({value: item.id, label: item.label})));
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
  if (containsFocusedElement(tbody)) return;
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
  const minutes = Math.max(0, Math.min(1440, Number.isFinite(rawMinutes) ? rawMinutes : IQ_RECORDER_DEFAULT_DURATION_MINUTES));
  setValue("iq_duration_minutes", String(minutes));
  rememberIqRecorderPreferences();
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
    setPageTitle("Download I/Q Recording");
    return;
  }
  setText(
    "iq_download_recording_label",
    `${recording.recorded_at || "Unknown"}; ${recording.sample_rate || 0} S/s; ${recording.frequency_hz || 0} Hz`
  );
  setPageTitle(`Download I/Q recording from ${recording.recorded_at || "unknown time"}`);
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
  if (s.gain !== null) rememberManualGain(s.gain);
  const displayedGain = s.gain === null ? storedManualGain() : s.gain;
  const gainIndex = gainIndexFor(displayedGain);
  if (gain.value !== String(gainIndex)) gain.value = String(gainIndex);
  updateGainSliderText();
  setValue("ppm_correction", s.ppm_correction);
  setChecked("bias_tee", s.bias_tee);
  setValue("alias_filter_strength", s.alias_filter_strength);
  setText("alias_filter_strength_label", `${s.alias_filter_strength}%`);
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
  renderIqTestSourceStatus(data);
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
    ppm_correction: numericControlValue("ppm_correction"),
    bias_tee: document.getElementById("bias_tee").checked,
    alias_filter_strength: Number(document.getElementById("alias_filter_strength").value)
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
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
        beginNumericTextEdit(event.target);
        return;
      }
      if (id === "gain") rememberManualGainFromSlider();
      if (id === "gain_auto" && !event.target.checked) restoreRememberedManualGain();
      if (id === "gain" || id === "gain_auto") updateGainSliderText();
      scheduleUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) commitNumericControlElement(event.target, null);
      if (id === "gain") rememberManualGainFromSlider();
      if (id === "gain_auto" && !event.target.checked) restoreRememberedManualGain();
      if (id === "gain" || id === "gain_auto") updateGainSliderText();
      scheduleUpdate();
    }
  });
}

for (const id of ["fallback_enabled", "fallback_delay", "fallback_loop_delay"]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
        beginNumericTextEdit(event.target);
        return;
      }
      scheduleFallbackUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) commitNumericControlElement(event.target, null);
      scheduleFallbackUpdate();
    }
  });
}

for (const id of ["eas_enabled", "eas_pre_seconds", "eas_post_seconds", "eas_max_seconds"]) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target) && isTextEditingInputEvent(event)) {
        beginNumericTextEdit(event.target);
        return;
      }
      scheduleEasUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) commitNumericControlElement(event.target, null);
      scheduleEasUpdate();
    }
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
      if (isNumericControl(event.target)) {
        if (isTextEditingInputEvent(event) || event.target.dataset.userEditing === "1") {
          beginNumericTextEdit(event.target);
          return;
        }
        if (audioFrequencyNameForElement(event.target)) {
          commitAudioFrequencyElement(event.target, false);
        } else {
          normalizeNumericControl(event.target);
        }
      } else if (audioFrequencyNameForElement(event.target)) {
        if (isTextEditingInputEvent(event) || event.target.dataset.userEditing === "1") {
          beginNumericTextEdit(event.target);
          return;
        }
        commitAudioFrequencyElement(event.target, false);
      }
      scheduleAudioEffectsUpdate();
    }
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) {
      if (isNumericControl(event.target)) {
        commitNumericControlElement(event.target, null);
      } else {
        commitAudioFrequencyElement(event.target, false);
      }
      scheduleAudioEffectsUpdate();
    }
  });
}

document.addEventListener("keydown", event => {
  if (!isNumericControl(event.target)) return;
  if (isTextEditingKey(event)) {
    beginNumericTextEdit(event.target);
    return;
  }
  if (event.key === "Enter") {
    event.preventDefault();
    commitNumericControlElement(event.target, numericSaveCallbackForElement(event.target));
    return;
  }
  if (["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End"].includes(event.key)) {
    setTimeout(() => commitNumericControlElement(event.target, numericSaveCallbackForElement(event.target)), 0);
  }
});

document.addEventListener("blur", event => {
  if (!isNumericControl(event.target)) return;
  commitNumericControlElement(event.target, numericSaveCallbackForElement(event.target));
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
    outputId: link.dataset.outputId || "",
    alertId: link.dataset.alertId || "",
    recordingId: link.dataset.recordingId || "",
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
  setCreateAccountResult("");
  showAccountSecret("");
  setValue("new_account_username", "");
  setChecked("new_account_read_only", false);
  navigateTo("create_account");
});
document.getElementById("cancel_create_account").addEventListener("click", () => {
  navigateTo("accounts");
});
document.getElementById("create_account").addEventListener("click", async () => {
  try {
    await createAccountFromForm();
  } catch (error) {
    setCreateAccountResult(error.message, "error");
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
  radio.addEventListener("change", () => {
    renderIqModeFields();
    rememberIqRecorderPreferences();
  });
}
document.getElementById("iq_stream_select").addEventListener("change", () => {
  renderIqModeFields();
  rememberIqRecorderPreferences();
});
document.getElementById("iq_sample_rate").addEventListener("change", rememberIqRecorderPreferences);
document.getElementById("iq_duration_minutes").addEventListener("change", rememberIqRecorderPreferences);
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
  const nextRoute = event.state || routeFromLocation();
  if (!confirmDiscardNavigation()) {
    const currentView = currentViewName();
    const currentParams = currentRouteParams();
    history.pushState(routeState(currentView, currentParams), "", routeForView(currentView, currentParams));
    return;
  }
  if (currentViewName() === "stream_output" && nextRoute.view !== "stream_output") closeOutputForm();
  applyRoute(nextRoute);
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
    editStreamSettings(target.dataset.streamId);
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
    const status = await request(statusRequestPath());
    await loadDevices(status.settings.serial);
  } catch (error) {
    setText("device-errors", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("reset_rtl_device").addEventListener("click", async () => {
  const button = document.getElementById("reset_rtl_device");
  const serial = document.getElementById("serial").value;
  if (!serial) {
    setText("device-errors", "Select an RTL-SDR to reset.");
    return;
  }
  setDisabled(button, true);
  setText("device-errors", "Resetting RTL-SDR...");
  try {
    const data = await requestWithTimeout("/api/rtl-reset", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({serial})
    }, 15000);
    setText("device-errors", data.message || "RTL-SDR reset finished.");
    applyStatus(data.status || await request(statusRequestPath()), {syncControls: true});
    await loadDevices(serial);
  } catch (error) {
    setText("device-errors", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("rescan_iq_test_sources").addEventListener("click", async () => {
  const button = document.getElementById("rescan_iq_test_sources");
  setDisabled(button, true);
  try {
    await loadIqTestSources({force: true});
    setText("iq-test-source-result", "I/Q source file list updated.");
  } catch (error) {
    setText("iq-test-source-result", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("start_iq_test_source").addEventListener("click", async () => {
  const button = document.getElementById("start_iq_test_source");
  const fileName = document.getElementById("iq_test_source_file").value;
  const sampleRate = Number(document.getElementById("iq_test_source_sample_rate").value);
  if (!fileName) {
    setText("iq-test-source-result", "Select an I/Q source file.");
    return;
  }
  setDisabled(button, true);
  setText("iq-test-source-result", "Starting I/Q file source...");
  try {
    const data = await request("/api/iq-test-source", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({file_name: fileName, sample_rate: sampleRate})
    });
    applyStatus(data, {syncControls: true});
    await loadIqTestSources({force: true});
  } catch (error) {
    setText("iq-test-source-result", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("stop_iq_test_source").addEventListener("click", async () => {
  const button = document.getElementById("stop_iq_test_source");
  setDisabled(button, true);
  setText("iq-test-source-result", "Returning to RTL-SDR...");
  try {
    const data = await request("/api/iq-test-source/stop", {method: "POST"});
    applyStatus(data, {syncControls: true});
    await loadIqTestSources({force: true});
  } catch (error) {
    setText("iq-test-source-result", error.message);
  } finally {
    setDisabled(button, false);
  }
});

document.getElementById("seek_iq_test_source_back_60").addEventListener("click", async () => {
  try {
    await seekIqTestSource(-60);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("seek_iq_test_source_back_10").addEventListener("click", async () => {
  try {
    await seekIqTestSource(-10);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("seek_iq_test_source_forward_10").addEventListener("click", async () => {
  try {
    await seekIqTestSource(10);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("seek_iq_test_source_forward_60").addEventListener("click", async () => {
  try {
    await seekIqTestSource(60);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("iq_test_source_seek_controls").addEventListener("keydown", async event => {
  const keyMap = {
    ArrowUp: 10,
    ArrowDown: -10,
    PageUp: 60,
    PageDown: -60
  };
  if (!(event.key in keyMap)) return;
  event.preventDefault();
  try {
    await seekIqTestSource(keyMap[event.key]);
  } catch (error) {
    setText("iq-test-source-result", error.message);
  }
});

document.getElementById("station_search_button").addEventListener("click", async () => {
  try {
    await runStationSearchFromUi();
  } catch (error) {
    setStreamResult(error.message, "error");
  }
});

document.getElementById("station_search").addEventListener("input", () => {
  updateStationSearchControls();
  clearStationResults(stationSearchQuery() === "" ? "Enter search text to find a station." : "Press Search to find matching stations.");
});

document.getElementById("station_search").addEventListener("keydown", async event => {
  if (event.key !== "Enter") return;
  event.preventDefault();
  try {
    await runStationSearchFromUi();
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

for (const control of document.querySelectorAll("input[name='wizard_output_type']")) {
  control.addEventListener("change", () => {
    wizardDirty = true;
    wizardSoundcardOutputId = "";
    if (selectedWizardOutputType() !== "soundcard") {
      discardWizardSoundcardPreview();
    }
    renderWizard();
  });
}

document.getElementById("wizard_soundcard_device").addEventListener("change", () => {
  wizardDirty = true;
  adjustWizardSoundcardChannelForDevice();
  if (wizardSoundcardPreviewId) scheduleWizardSoundcardUpdate();
  renderWizard();
});

for (const control of document.querySelectorAll("input[name='wizard_soundcard_channel']")) {
  control.addEventListener("change", scheduleWizardSoundcardUpdate);
}

document.getElementById("wizard_soundcard_volume").addEventListener("input", scheduleWizardSoundcardUpdate);

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

document.getElementById("settings_soundcard_device").addEventListener("change", () => {
  adjustSettingsSoundcardChannelForDevice();
  scheduleSettingsSoundcardUpdate();
});

for (const control of document.querySelectorAll("input[name='settings_soundcard_channel']")) {
  control.addEventListener("change", scheduleSettingsSoundcardUpdate);
}

document.getElementById("settings_soundcard_volume").addEventListener("input", scheduleSettingsSoundcardUpdate);

document.getElementById("reset_soundcard_device").addEventListener("click", async event => {
  const button = event.currentTarget;
  const stableId = document.getElementById("settings_soundcard_device").value || "";
  if (!stableId) {
    setOutputResult("Select a sound card to reset.", "error");
    return;
  }
  setDisabled(button, true);
  try {
    await resetSoundcardByStableId(stableId);
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateSettingsSoundcardResetButton();
  }
});

document.getElementById("open_add_output").addEventListener("click", beginAddOutput);

document.getElementById("output_type_filter").addEventListener("change", () => {
  renderOutputPanels();
});

document.getElementById("cancel_output_form").addEventListener("click", cancelOutputForm);
document.getElementById("cancel_soundcard_output_form").addEventListener("click", cancelOutputForm);

document.getElementById("stream_enabled").addEventListener("change", async event => {
  if (!settingsStreamId || applying) return;
  if (event.target.dataset.busy === "true") {
    renderStreamSettings();
    return;
  }
  const enabled = event.target.checked;
  setControlBusy(event.target, true);
  try {
    await setStreamEnabled(settingsStreamId, enabled, setOutputResult);
  } catch (error) {
    setOutputResult(error.message, "error");
    const stream = currentSettingsStream();
    if (stream) setChecked("stream_enabled", stream.enabled !== false);
  } finally {
    setControlBusy(event.target, false);
  }
});

document.getElementById("stream_monitor_enabled").addEventListener("change", async event => {
  if (!settingsStreamId || applying) return;
  if (event.target.dataset.busy === "true") {
    renderStreamSettings();
    return;
  }
  setControlBusy(event.target, true);
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
    setControlBusy(event.target, false);
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
    icecastOutputTableSignature = "";
    soundcardOutputTableSignature = "";
    renderStreams(data.streams || []);
    if (data.success) {
      cancelOutputForm();
      maybeShowNwrOrgSubmissionNotice(icecast);
    }
    setOutputResult(data.message, data.success ? "success" : "error");
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
    if (data.success) {
      cancelOutputForm();
      if (oldService !== STREAM_SERVICE_NWRORG) maybeShowNwrOrgSubmissionNotice(icecast);
    }
    setOutputResult(data.message, data.success ? "success" : "error");
  } catch (error) {
    setOutputResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    updateOutputFormButtons();
  }
});

async function handleOutputTableClick(event) {
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
          type: selected.output.type || "icecast",
          icecast: selected.output.icecast,
          soundcard: selected.output.soundcard
        })
      });
      renderStreams(data.streams || []);
      setOutputResult(data.message, data.success ? "success" : "error");
    } catch (error) {
      setOutputResult(error.message, "error");
    }
    return;
  }
  if (target.dataset.action === "reset-soundcard-output") {
    closeStreamActionMenus();
    const selected = findConfiguredOutput(target.dataset.streamId, target.dataset.outputId);
    if (!selected || selected.output.type !== "soundcard") {
      setOutputResult("Sound card output was not found.", "error");
      return;
    }
    const device = soundcardOutputUsbDevice(selected.output);
    if (!device) {
      setOutputResult("Only USB sound cards can be reset.", "error");
      return;
    }
    target.disabled = true;
    try {
      await resetSoundcardByStableId(device.stable_id);
    } catch (error) {
      setOutputResult(error.message, "error");
    } finally {
      target.disabled = false;
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
}

function handleOutputTableKeydown(event) {
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
}

document.getElementById("icecast-outputs-body").addEventListener("click", handleOutputTableClick);
document.getElementById("soundcard-outputs-body").addEventListener("click", handleOutputTableClick);
document.getElementById("icecast-outputs-body").addEventListener("keydown", handleOutputTableKeydown);
document.getElementById("soundcard-outputs-body").addEventListener("keydown", handleOutputTableKeydown);

document.getElementById("cancel_wizard").addEventListener("click", async () => {
  if (wizardMode === "add_output" && selectedWizardOutputType() === "soundcard") {
    await discardWizardSoundcardOutput();
  }
  if (wizardMode === "add" && selectedWizardOutputType() === "soundcard") {
    await discardWizardSoundcardPreview();
  }
  finishWizard();
});

document.getElementById("wizard_back").addEventListener("click", async () => {
  if (wizardUsesOutputSteps()) {
    if (wizardStep === ADD_OUTPUT_TYPE_STEP) {
      if (wizardMode === "add") setWizardStep(0);
      return;
    }
    if (wizardStep === ADD_OUTPUT_ICECAST_SERVICE_STEP || wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
      if (wizardMode === "add" && wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
        await discardWizardSoundcardPreview();
      }
      setWizardStep(ADD_OUTPUT_TYPE_STEP);
      return;
    }
    if (wizardStep === ADD_OUTPUT_SOUNDCARD_CONTROLS_STEP) {
      setWizardStep(ADD_OUTPUT_SOUNDCARD_DEVICE_STEP);
      return;
    }
    const streamStep = streamIcecastStep(wizardStep);
    const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
    if (streamStep === 3 && (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG)) {
      setWizardStep(ADD_OUTPUT_ICECAST_SERVICE_STEP);
    } else {
      setWizardStep(addOutputIcecastStep(streamStep - 1));
    }
    return;
  }
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  if (wizardStep === 3 && (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG)) {
    setWizardStep(1);
  } else {
    setWizardStep(wizardStep - 1);
  }
});

document.getElementById("wizard_next").addEventListener("click", async () => {
  if (wizardUsesOutputSteps()) {
    if (wizardStep === ADD_OUTPUT_TYPE_STEP) {
      if (selectedWizardOutputType() === "soundcard") {
        setWizardStep(ADD_OUTPUT_SOUNDCARD_DEVICE_STEP);
        loadDevices("", {force: true}).catch(error => {
          setStreamResult(error.message, "error");
          renderWizard();
        });
      } else {
        setWizardStep(ADD_OUTPUT_ICECAST_SERVICE_STEP);
      }
      return;
    }
    if (wizardStep === ADD_OUTPUT_SOUNDCARD_DEVICE_STEP) {
      if (wizardMode === "add_output") {
        await createWizardSoundcardOutput();
      } else {
        await upsertWizardSoundcardPreview();
      }
      return;
    }
    const streamStep = streamIcecastStep(wizardStep);
    if (streamStep === 1) {
      const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
      if (service === STREAM_SERVICE_GWES || service === STREAM_SERVICE_NWRORG) {
        setWizardStep(ADD_OUTPUT_ICECAST_CREDENTIALS_STEP);
      } else {
        setWizardStep(ADD_OUTPUT_ICECAST_CODEC_STEP);
      }
      return;
    }
    if (streamStep === 2) {
      const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
      if (service === STREAM_SERVICE_WEATHERUSA && !document.getElementById("icecast_mount").value.trim()) {
        setValue("icecast_mount", weatherUsaMount(selectedStation(), selectedWizardFormat()));
      }
      setWizardStep(ADD_OUTPUT_ICECAST_CREDENTIALS_STEP);
      return;
    }
    if (streamStep === 3) {
      await authenticateWizardIcecastOutput();
      return;
    }
  }
  if (wizardStep === 0) {
    setWizardStep(ADD_OUTPUT_TYPE_STEP);
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
    await authenticateWizardIcecastOutput();
    return;
  }
});

document.getElementById("wizard_finish").addEventListener("click", async () => {
  if (wizardMode === "add_output" && selectedWizardOutputType() === "soundcard") {
    wizardDirty = false;
    await updateWizardSoundcardOutput();
    finishWizard();
    return;
  }
  if (wizardMode === "add" && selectedWizardOutputType() === "soundcard") {
    const button = document.getElementById("wizard_finish");
    setDisabled(button, true);
    setStreamResult("Creating stream...");
    try {
      const data = await request("/api/streams", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(streamPayload())
      });
      renderStreams(data.streams || []);
      setStreamResult(data.message, data.success ? "success" : "error");
      if (data.success) {
        wizardSoundcardPreviewId = "";
        finishWizard();
      }
    } catch (error) {
      setStreamResult(error.message, "error");
    } finally {
      setDisabled(button, false);
      renderWizard();
    }
    return;
  }
  const button = document.getElementById("wizard_finish");
  setDisabled(button, true);
  const service = document.getElementById("icecast_service").value || STREAM_SERVICE_CUSTOM;
  const addOutputMode = wizardMode === "add_output";
  setStreamResult(service === STREAM_SERVICE_NWRORG && !icecastAuthPassed ? "Testing Icecast authentication..." : addOutputMode ? "Adding output..." : "Creating stream...");
  try {
    const payload = streamPayload();
    const createdIcecast = payload.icecast;
    if (addOutputMode && duplicateOutputExists(createdIcecast)) {
      setStreamResult("An output with these credentials already exists.", "error");
      return;
    }
    if (!icecastAuthPassed || icecastAuthSignature !== icecastCredentialSignature()) {
      const signature = icecastCredentialSignature();
      const auth = await request("/api/icecast-auth", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({icecast: createdIcecast})
      });
      if (!auth.success) {
        setStreamResult(auth.message, "error");
        return;
      }
      icecastAuthPassed = true;
      icecastAuthSignature = signature;
    }
    setStreamResult(addOutputMode ? "Adding output..." : "Creating stream...");
    const data = addOutputMode
      ? await request("/api/stream-output", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({stream_id: settingsStreamId, icecast: createdIcecast})
        })
      : await request("/api/streams", {
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

function sendLiveAudioStopBeacon(options = {}) {
  const forceReceiverStop = options.forceReceiverStop === true;
  if ((unloadLiveAudioStopSent && !forceReceiverStop) || !navigator.sendBeacon) return;
  unloadLiveAudioStopSent = true;
  if (monitorStreamId && navigator.sendBeacon) {
    const payload = JSON.stringify({client_id: pageMonitorClientId()});
    navigator.sendBeacon("/api/monitor/stop", new Blob([payload], {type: "application/json"}));
  }
  if (receiverPeerConnection && (!receiverPaused || forceReceiverStop) && navigator.sendBeacon) {
    const payload = JSON.stringify({client_id: pageReceiverClientId()});
    navigator.sendBeacon("/api/receiver/stop", new Blob([payload], {type: "application/json"}));
  }
}

window.addEventListener("pagehide", event => {
  markLiveAudioHidden();
  scheduleReceiverMediaSessionRefresh();
  if (event.persisted) return;
  sendWizardSoundcardPreviewDiscardBeacon();
  if (document.visibilityState === "hidden") return;
  sendLiveAudioStopBeacon();
});

window.addEventListener("pageshow", () => {
  unloadLiveAudioStopSent = false;
  recoverLiveAudioAfterPageRestore().catch(error => console.debug("live audio page restore recovery failed", error));
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    markLiveAudioHidden();
    scheduleReceiverMediaSessionRefresh();
    return;
  }
  recoverLiveAudioAfterPageRestore().catch(error => console.debug("live audio visibility recovery failed", error));
});

const liveAudioElement = document.getElementById("stream_monitor_audio");
if (liveAudioElement) {
  liveAudioElement.addEventListener("pause", () => {
    if (receiverPaused && receiverPeerConnection) scheduleReceiverMediaSessionRefresh();
  });
}

window.addEventListener("beforeunload", event => {
  sendWizardSoundcardPreviewDiscardBeacon();
  sendLiveAudioStopBeacon({forceReceiverStop: true});
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
  const data = await request(statusRequestPath());
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
    outputId: initialRoute.outputId,
    alertId: initialRoute.alertId,
    recordingId: initialRoute.recordingId,
    page: initialRoute.page
  }, true);
  setInterval(refresh, 1000);
  setInterval(async () => {
    try {
      const status = await request(statusRequestPath());
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
