from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

if __package__:
    from .audio_effects import AudioEffectsProcessor
    from .config import AudioConfig, IcecastConfig, IQ_SAMPLE_RATE
    from .dsp import IqChannelizer
    from .encoder import PcmResampler, create_audio_encoder
    from .fallback_audio import load_fallback_audio
    from .icecast import IcecastSource
    from .nfm import float_to_s16
    from .rtl import (
        DEFAULT_RTL_SAMPLE_RATE,
        NWR_CENTER_FREQUENCY_HZ,
        RTL_SAMPLE_RATE_RANGES,
        IqDcBlocker,
        RtlConfig,
        RtlConfigError,
        RtlCaptureSource,
        RtlSampleBatch,
        list_rtl_devices,
        list_usb_rtl_devices,
        rtl_u8_to_complex64,
        validate_ppm_correction,
        validate_rtl_sample_rate,
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
    encoder = importlib.import_module(f"{package_name}.encoder")
    fallback_audio = importlib.import_module(f"{package_name}.fallback_audio")
    icecast_module = importlib.import_module(f"{package_name}.icecast")
    nfm = importlib.import_module(f"{package_name}.nfm")
    rtl = importlib.import_module(f"{package_name}.rtl")
    AudioEffectsProcessor = audio_effects.AudioEffectsProcessor
    AudioConfig = config_module.AudioConfig
    IcecastConfig = config_module.IcecastConfig
    IQ_SAMPLE_RATE = config_module.IQ_SAMPLE_RATE
    IqChannelizer = dsp.IqChannelizer
    PcmResampler = encoder.PcmResampler
    create_audio_encoder = encoder.create_audio_encoder
    load_fallback_audio = fallback_audio.load_fallback_audio
    IcecastSource = icecast_module.IcecastSource
    float_to_s16 = nfm.float_to_s16
    DEFAULT_RTL_SAMPLE_RATE = rtl.DEFAULT_RTL_SAMPLE_RATE
    NWR_CENTER_FREQUENCY_HZ = rtl.NWR_CENTER_FREQUENCY_HZ
    RTL_SAMPLE_RATE_RANGES = rtl.RTL_SAMPLE_RATE_RANGES
    IqDcBlocker = rtl.IqDcBlocker
    RtlConfig = rtl.RtlConfig
    RtlConfigError = rtl.RtlConfigError
    RtlCaptureSource = rtl.RtlCaptureSource
    RtlSampleBatch = rtl.RtlSampleBatch
    list_rtl_devices = rtl.list_rtl_devices
    list_usb_rtl_devices = rtl.list_usb_rtl_devices
    rtl_u8_to_complex64 = rtl.rtl_u8_to_complex64
    validate_ppm_correction = rtl.validate_ppm_correction
    validate_rtl_sample_rate = rtl.validate_rtl_sample_rate

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from icecastauth import IcecastSettings, normalize_server, test_mountpoint_authentication

import numpy as np


LOG = logging.getLogger(__name__)
STATE_DIRECTORY_NAME = "nwr-stream-manager"
STATE_FILE_NAME = "rtl-control.json"
STREAMS_STATE_FILE_NAME = "streams.json"
STATIONS_ASSET_PATH = Path(__file__).resolve().parent / "assets" / "nwr_stations.json"
USB_VENDOR_NAMES = {
    "0bda": "Realtek",
}
DEFAULT_STREAM_SAMPLE_RATE = 24_000
DEFAULT_STREAM_BITRATES = {
    "mp3": 64,
    "ogg": 48,
}
STREAM_FRAME_SECONDS = 0.02
STREAM_FRAME_SAMPLES = round(IQ_SAMPLE_RATE * STREAM_FRAME_SECONDS)
STREAM_FRAME_BYTES = STREAM_FRAME_SAMPLES * 2
STREAM_RECONNECT_SECONDS = 5.0
ICECAST_AUTH_CACHE_SECONDS = 600.0
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
            sample_rate=validate_rtl_sample_rate(self.sample_rate),
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


class RawRtlFanout:
    def __init__(self, source: RtlCaptureSource) -> None:
        self.source = source
        self.subscribers: set[queue.Queue] = set()
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def subscribe(self, max_chunks: int = 32) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)

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
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait(batch)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        subscriber.put_nowait(batch)
                    except queue.Full:
                        pass


class ComplexNfmDemodulator:
    def __init__(self) -> None:
        self.previous_sample: np.complex64 | None = None

    def process(self, iq: np.ndarray) -> np.ndarray:
        if len(iq) < 2 and self.previous_sample is None:
            if len(iq) == 1:
                self.previous_sample = iq[-1]
            return np.array([], dtype=np.float32)
        if self.previous_sample is None:
            previous = iq[:-1]
            current = iq[1:]
        else:
            previous = np.concatenate((np.array([self.previous_sample], dtype=np.complex64), iq[:-1]))
            current = iq
        self.previous_sample = iq[-1]
        demodulated = np.angle(current * np.conj(previous)).astype(np.float32)
        return (demodulated / np.pi * 1.5).astype(np.float32, copy=False)


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
        return b"\x00" * STREAM_FRAME_BYTES
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


class IcecastStreamWorker:
    def __init__(
        self,
        *,
        stream: dict[str, Any],
        output: dict[str, Any],
        fanout: RawRtlFanout,
        fallback_settings_provider,
    ) -> None:
        self.stream = stream
        self.output = output
        self.fanout = fanout
        self.fallback_settings_provider = fallback_settings_provider
        self.queue = fanout.subscribe(max_chunks=64)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"icecast-stream-{stream['id']}", daemon=True)
        self.status = "disabled"
        self.error: str | None = None
        self.started_at: float | None = None
        self.last_audio_at: float | None = None
        self.lock = threading.Lock()

    @property
    def id(self) -> str:
        return str(self.stream["id"])

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.fanout.unsubscribe(self.queue)
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        with self.lock:
            self.status = "disabled"

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            error = self.error
            started_at = self.started_at
            last_audio_at = self.last_audio_at
        return {
            "id": self.id,
            "station": self.stream["station"],
            "outputs": [self.output],
            "status": status,
            "error": error,
            "started_at": started_at,
            "last_audio_at": last_audio_at,
        }

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            self.error = error
            if status == "enabled" and self.started_at is None:
                self.started_at = time.time()

    def _run(self) -> None:
        station = self.stream["station"]
        while not self.stop_event.is_set():
            sink = None
            source = None
            encoder = None
            try:
                config = icecast_config_from_output(self.output)
                source = IcecastSource(config, config.content_type)
                sink = source.connect()
                encoder = create_audio_encoder(config)
                self._set_status("enabled")
                self._produce_encoded_audio(sink, encoder, float(station["frequency"]) * 1_000_000)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self._set_status("needs-attention", friendly_stream_error(exc))
                LOG.warning("Icecast stream worker failed for %s: %s", station.get("callsign"), exc)
                self.stop_event.wait(STREAM_RECONNECT_SECONDS)
            finally:
                if encoder is not None:
                    try:
                        encoder.close()
                    except Exception:
                        pass
                if sink is not None:
                    try:
                        sink.close()
                    except Exception:
                        pass
                if source is not None:
                    source.close()
        self._set_status("disabled")

    def _produce_encoded_audio(self, sink, encoder, target_frequency_hz: float) -> None:
        dc_blocker = IqDcBlocker()
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int, int] | None = None
        demodulator = ComplexNfmDemodulator()
        effects = AudioEffectsProcessor(AudioConfig())
        pending = np.array([], dtype=np.float32)
        header = getattr(encoder, "header", b"")
        if header:
            sink.write(header)
        fallback = load_web_fallback_audio()
        fallback_state = WebFallbackPlaybackState()
        last_real_audio = time.monotonic()
        while not self.stop_event.is_set():
            try:
                batch: RtlSampleBatch = self.queue.get(timeout=0.5)
            except queue.Empty:
                fallback_settings = self.fallback_settings_provider()
                idle_seconds = time.monotonic() - last_real_audio
                if not fallback_settings.enabled:
                    fallback_state.reset()
                    continue
                if not fallback_state.active and idle_seconds < fallback_settings.silence_timeout_seconds:
                    fallback_state.reset()
                    continue
                if not fallback_state.active:
                    fallback_state.active = True
                    LOG.info("starting fallback audio for %s after %.1f seconds without IQ", station.get("callsign"), idle_seconds)
                sink.write(encoder.encode(next_web_fallback_frame(fallback, fallback_state, fallback_settings.loop_delay_seconds)))
                continue
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
            iq = rtl_u8_to_complex64(batch.data)
            audio = demodulator.process(channelizer.process_complex(dc_blocker.process(iq)))
            if len(audio) == 0:
                continue
            last_real_audio = time.monotonic()
            if fallback_state.active:
                LOG.info("stopping fallback audio for %s", station.get("callsign"))
            fallback_state.reset()
            with self.lock:
                self.last_audio_at = time.time()
            pending = np.concatenate((pending, audio.astype(np.float32, copy=False)))
            while len(pending) >= STREAM_FRAME_SAMPLES:
                frame = pending[:STREAM_FRAME_SAMPLES]
                pending = pending[STREAM_FRAME_SAMPLES:]
                pcm = float_to_s16(effects.process(frame))
                encoded = encoder.encode(pcm)
                if encoded:
                    sink.write(encoded)


class RtlControlService:
    def __init__(self, state_path: Path, log_handler: RingLogHandler) -> None:
        self.state_path = state_path
        self.streams_state_path = state_path.with_name(STREAMS_STATE_FILE_NAME)
        self.fallback_state_path = state_path.with_name(FALLBACK_STATE_FILE_NAME)
        self.log_handler = log_handler
        self.lock = threading.RLock()
        self.settings = load_settings(state_path)
        self.streams = load_streams(self.streams_state_path)
        self.fallback_settings = load_fallback_settings(self.fallback_state_path)
        self.stations = load_station_database()
        self.capture: RtlCaptureSource | None = None
        self.raw_fanout: RawRtlFanout | None = None
        self.monitor_queue: queue.Queue | None = None
        self.drain_thread: threading.Thread | None = None
        self.drain_stop = threading.Event()
        self.stream_workers: dict[str, IcecastStreamWorker] = {}
        self.icecast_auth_cache: dict[str, float] = {}
        self.capture_error: str | None = None
        self.last_batch_at: float | None = None
        self.received_chunks = 0
        self.received_bytes = 0
        self._last_rate_log_at = 0.0
        if self.settings.serial:
            self._start_or_update_capture_locked()
        else:
            self._select_only_connected_device()

    def close(self) -> None:
        self.stop_capture()

    def status(self) -> dict[str, Any]:
        with self.lock:
            capture = self.capture
            settings = self._effective_settings_locked()
            active = capture is not None
            gain_values = capture.get_gain_values() if capture is not None else []
            return {
                "settings": asdict(settings),
                "gain_values": gain_values,
                "active": active,
                "capture_error": self.capture_error,
                "last_batch_at": self.last_batch_at,
                "received_chunks": self.received_chunks,
                "received_bytes": self.received_bytes,
                "sample_rate_ranges": RTL_SAMPLE_RATE_RANGES,
                "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
                "fallback": asdict(self.fallback_settings),
                "active_streams": self._active_streams_locked(),
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
            return {"streams": list(self.streams)}

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
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self.lock:
            self.streams.append(stream)
            save_streams(self.streams_state_path, self.streams)
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
            before = len(self.streams)
            self.streams = [stream for stream in self.streams if stream.get("id") != stream_id]
            if len(self.streams) == before:
                raise ValueError("stream was not found")
            save_streams(self.streams_state_path, self.streams)
            self._sync_stream_workers_locked()
        LOG.info("removed stream %s", stream_id)
        return self.stream_status()

    def update_stream_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        output_id = str(payload.get("output_id", "")).strip()
        enabled = bool(payload.get("enabled", True))
        icecast = validate_icecast_payload(payload.get("icecast"))
        with self.lock:
            stream, output = self._stream_output_locked(stream_id, output_id)
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
            save_streams(self.streams_state_path, self.streams)
            key = stream_worker_key(stream, output)
            worker = self.stream_workers.pop(key, None)
            if worker is not None:
                worker.stop()
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
            stream_outputs(stream).append(
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
            save_streams(self.streams_state_path, self.streams)
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
            before = len(outputs)
            stream["outputs"] = [output for output in outputs if output.get("id") != output_id]
            if len(stream["outputs"]) == before:
                raise ValueError("stream output was not found")
            stream["updated_at"] = time.time()
            key = f"{stream_id}:{output_id}"
            worker = self.stream_workers.pop(key, None)
            if worker is not None:
                worker.stop()
            save_streams(self.streams_state_path, self.streams)
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
            changes["sample_rate"] = validate_rtl_sample_rate(int(payload["sample_rate"]))
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
            self.monitor_queue = self.raw_fanout.subscribe(max_chunks=64)
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
        self._stop_stream_workers_locked()
        fanout = self.raw_fanout
        self.raw_fanout = None
        self.monitor_queue = None
        capture = self.capture
        self.capture = None
        drain_thread = self.drain_thread
        self.drain_thread = None
        LOG.info("stopped RTL-SDR control capture")
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
            self._stop_stream_workers_locked()
            fanout = self.raw_fanout
            self.raw_fanout = None
            self.monitor_queue = None
            capture = self.capture
            self.capture = None
            drain_thread = self.drain_thread
            self.drain_thread = None
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
            now = time.monotonic()
            with self.lock:
                self.capture_error = None
                self.last_batch_at = batch.captured_at
                self.received_chunks += 1
                self.received_bytes += len(batch.data)
                chunks = self.received_chunks
                total_bytes = self.received_bytes
            if now - self._last_rate_log_at >= 2.0:
                self._last_rate_log_at = now
                LOG.info(
                    "RTL-SDR capture receiving IQ: chunks=%s bytes=%s sample_rate=%s",
                    chunks,
                    total_bytes,
                    batch.sample_rate,
                )

    def _sync_stream_workers_locked(self) -> None:
        fanout = self.raw_fanout
        desired: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        if fanout is not None:
            for stream in self.streams:
                if not stream.get("enabled", True):
                    continue
                for output in stream_outputs(stream):
                    if not output.get("enabled", True):
                        continue
                    key = stream_worker_key(stream, output)
                    desired[key] = (stream, output)

        for key in list(self.stream_workers):
            if key not in desired:
                worker = self.stream_workers.pop(key)
                worker.stop()

        if fanout is None:
            return

        for key, (stream, output) in desired.items():
            if key in self.stream_workers:
                continue
            worker = IcecastStreamWorker(
                stream=stream,
                output=output,
                fanout=fanout,
                fallback_settings_provider=self.fallback_settings_snapshot,
            )
            self.stream_workers[key] = worker
            worker.start()
            LOG.info(
                "started stream worker for %s output %s",
                stream.get("station", {}).get("callsign", "unknown"),
                output.get("id"),
            )

    def _stop_stream_workers_locked(self) -> None:
        for worker in list(self.stream_workers.values()):
            worker.stop()
        self.stream_workers = {}

    def _active_streams_locked(self) -> list[dict[str, Any]]:
        return [worker.snapshot() for worker in self.stream_workers.values()]


class RtlControlHandler(BaseHTTPRequestHandler):
    service: RtlControlService

    def log_message(self, format: str, *args) -> None:
        LOG.debug("HTTP %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self._send_html(INDEX_HTML)
        elif path == "/api/status":
            self._send_json(self.service.status())
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
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
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
        length = int(self.headers.get("Content-Length", "0"))
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
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def default_state_path() -> Path:
    state_directory = os.environ.get("STATE_DIRECTORY")
    if state_directory:
        base = Path(state_directory.split(":", 1)[0]).expanduser()
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser() / STATE_DIRECTORY_NAME
    else:
        base = Path.home() / ".local" / "state" / STATE_DIRECTORY_NAME
    return base / STATE_FILE_NAME


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
            sample_rate=validate_rtl_sample_rate(int(raw.get("sample_rate", DEFAULT_RTL_SAMPLE_RATE))),
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


def load_streams(path: Path) -> list[dict[str, Any]]:
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
    return streams


def save_streams(path: Path, streams: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"streams": streams}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


def stream_worker_key(stream: dict[str, Any], output: dict[str, Any]) -> str:
    return f"{stream.get('id')}:{output.get('id')}"


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
    host = str(raw.get("host", "")).strip()
    if not host:
        raise ValueError("Icecast host is required")
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
    sample_rate = int(raw.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE))
    if sample_rate not in {8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000}:
        raise ValueError("Icecast sample rate is not supported")
    bitrate = int(raw.get("bitrate", DEFAULT_STREAM_BITRATES[stream_format]))
    if bitrate < 8 or bitrate > 320 or bitrate % 8 != 0:
        raise ValueError("Icecast bitrate must be from 8 through 320 Kbps in 8 Kbps steps")
    return {
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


def run_server(host: str, port: int, state_path: Path, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ring_handler = RingLogHandler()
    logging.getLogger().addHandler(ring_handler)
    service = RtlControlService(state_path, ring_handler)
    RtlControlHandler.service = service
    server = ThreadingHTTPServer((host, port), RtlControlHandler)
    LOG.info("RTL-SDR control web interface bound to %s:%s", host, port)
    for url in access_urls(host, port):
        LOG.info("RTL-SDR control web interface available at %s", url)
    LOG.info("RTL-SDR settings will be remembered in %s", state_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_server(args.host, args.port, args.state, args.verbose)
    except KeyboardInterrupt:
        return 130
    return 0


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
h1 { font-size: 22px; margin: 0; }
h2 { font-size: 20px; margin: 0 0 16px; }
h3 { font-size: 16px; margin: 18px 0 10px; }
section { background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
label { display: grid; gap: 6px; font-weight: 600; margin-bottom: 14px; }
select, input, button { font: inherit; padding: 8px 10px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; color: #14181f; }
fieldset { border: 1px solid #d8dde6; border-radius: 6px; margin: 16px 0 0; padding: 14px; }
legend { font-weight: 700; padding: 0 6px; }
button { cursor: pointer; }
button:disabled { cursor: default; opacity: 0.65; }
nav { display: flex; flex-wrap: wrap; gap: 8px; }
nav button[aria-current="page"] { border-color: #2557a7; box-shadow: inset 0 -2px 0 #2557a7; }
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
.tabs { display: flex; flex-wrap: wrap; gap: 6px; border-bottom: 1px solid #d8dde6; margin: 16px 0; }
.tabs button { border-bottom-left-radius: 0; border-bottom-right-radius: 0; margin-bottom: -1px; }
.tabs button[aria-selected="true"] { border-color: #2557a7; border-bottom-color: #fff; box-shadow: inset 0 2px 0 #2557a7; }
.tabpanel[hidden] { display: none; }
pre { margin: 0; min-height: 220px; max-height: 360px; overflow: auto; background: #10151d; color: #d8f3dc; padding: 12px; border-radius: 6px; font-size: 13px; }
.error { color: #a40000; font-weight: 600; }
.message { font-weight: 600; }
.success { color: #0f7a34; }
.status-connected { color: #0f7a34; }
.hint { color: #526070; font-size: 13px; margin-top: -8px; }
@media (prefers-color-scheme: dark) {
  body { background: #101318; color: #eef2f7; }
  header, section, select, input, button { background: #181d24; color: #eef2f7; border-color: #333b48; }
  fieldset, .metric, .stream-item, th, td { border-color: #333b48; }
  .metric b, .hint, th { color: #9aa8ba; }
  .status-enabled { color: #5fd27a; }
  .status-connected { color: #5fd27a; }
  .status-needs-attention { color: #ff6b7a; }
  .success { color: #5fd27a; }
  .stream-actions-menu { background: #181d24; border-color: #333b48; }
  .tabs { border-color: #333b48; }
  .tabs button[aria-selected="true"] { border-bottom-color: #181d24; }
}
</style>
</head>
<body>
<header>
  <div class="topbar">
    <h1>NWR Stream Manager</h1>
    <nav aria-label="Main">
      <button id="nav_dashboard" type="button" data-view="dashboard" aria-current="page">Dashboard</button>
      <button id="nav_rtl" type="button" data-view="rtl">Configure RTL-SDR</button>
      <button id="nav_streams" type="button" data-view="streams">Manage Streams</button>
    </nav>
  </div>
</header>
<main>
  <div id="view_dashboard" class="view">
    <section>
      <h2>Dashboard</h2>
      <div class="status" aria-live="off">
        <div class="metric"><b>Configured SDR</b><span id="summary_sdr">none</span></div>
        <div class="metric"><b>Sample Rate</b><span id="summary_sample_rate">0</span></div>
        <div class="metric"><b>Gain</b><span id="summary_gain">automatic</span></div>
        <div class="metric"><b>Active Streams</b><span id="summary_stream_count">0</span></div>
      </div>
    </section>
    <section>
      <h2>RTL-SDR Status</h2>
      <div class="status" aria-live="off">
        <div class="metric"><b>Capture</b><span id="active">inactive</span></div>
        <div class="metric"><b>Chunks</b><span id="chunks">0</span></div>
        <div class="metric"><b>Bytes</b><span id="bytes">0</span></div>
        <div class="metric"><b>Last IQ</b><span id="last">never</span></div>
      </div>
      <p id="capture-error" class="error"></p>
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
        <label>Sample Rate
          <input id="sample_rate" type="number" min="225001" max="3200000" step="1">
        </label>
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
      <div class="hint">Valid RTL-SDR sample-rate ranges: 225001-300000 S/s and 900001-3200000 S/s.</div>
    </section>
  </div>

  <div id="view_streams" class="view" hidden>
    <section>
      <h2>Manage Streams</h2>
      <div class="actions">
        <button id="open_add_stream" type="button">Add stream</button>
      </div>
      <h3>Active streams</h3>
      <table aria-label="Active streams">
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
        <p>The next step is to enter your icecast credentials for the service you want to stream to. Enter them below, then click next.</p>
        <fieldset id="icecast_fields">
          <legend>Icecast Credentials</legend>
          <div class="grid">
            <label>Host
              <input id="icecast_host" type="text" autocomplete="off">
            </label>
            <label>Port
              <input id="icecast_port" type="number" min="1" max="65535" step="1" placeholder="8000">
            </label>
            <label>Username
              <input id="icecast_username" type="text" autocomplete="username">
            </label>
            <label>Password
              <input id="icecast_password" type="password" autocomplete="current-password">
            </label>
            <label class="checkbox-row">
              <input id="show_icecast_password" type="checkbox">
              Show password
            </label>
            <label>Mountpoint
              <input id="icecast_mount" type="text" placeholder="/station.mp3">
            </label>
          </div>
        </fieldset>
      </div>
      <div id="wizard_step_codec" class="wizard-step" hidden>
        <p>What audio codec would you like to use for the stream format? MP3 is generally more compatible, while OGG may give better audio quality at lower internet usage.</p>
        <fieldset>
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
          <label>Sample Rate
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
          <label>Bitrate
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
      <div class="tabs" role="tablist" aria-label="Stream settings sections">
        <button id="tab_outputs" type="button" role="tab" aria-selected="true" aria-controls="panel_outputs" tabindex="0">Outputs</button>
        <button id="tab_fallback" type="button" role="tab" aria-selected="false" aria-controls="panel_fallback" tabindex="-1">Fallback Audio</button>
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
          <fieldset>
            <legend>Icecast output</legend>
            <div class="grid">
              <label>Host
                <input id="settings_icecast_host" type="text" autocomplete="off">
              </label>
              <label>Port
                <input id="settings_icecast_port" type="number" min="1" max="65535" step="1" placeholder="8000">
              </label>
              <label>Username
                <input id="settings_icecast_username" type="text" autocomplete="username">
              </label>
              <label>Password
                <input id="settings_icecast_password" type="password" autocomplete="current-password">
              </label>
              <label class="checkbox-row">
                <input id="settings_show_icecast_password" type="checkbox">
                Show password
              </label>
              <label>Mountpoint
                <input id="settings_icecast_mount" type="text" placeholder="/station.mp3">
              </label>
              <fieldset>
                <legend>Format</legend>
                <label><input id="settings_icecast_format_mp3" name="settings_icecast_format" type="radio" value="mp3" checked> MP3</label>
                <label><input id="settings_icecast_format_ogg" name="settings_icecast_format" type="radio" value="ogg"> OGG</label>
              </fieldset>
              <label>Sample rate
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
              <label>Bitrate
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

  <section>
    <h2>Logs</h2>
    <pre id="logs" aria-live="off" aria-label="RTL-SDR log output"></pre>
  </section>
</main>
<script>
const controls = ["serial", "sample_rate", "gain", "ppm_correction", "bias_tee", "gain_auto"];
const DEFAULT_STREAM_SAMPLE_RATE = 24000;
const DEFAULT_STREAM_BITRATES = {mp3: 64, ogg: 48};
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
let wizardStep = 0;
let wizardMode = "add";
let wizardDirty = false;
let icecastAuthPassed = false;
let icecastAuthSignature = "";
let activeStreamsSignature = "";

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
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
  const selectedFormat = document.querySelector("input[name='icecast_format']:checked");
  return {
    station_key: selectedStationKey,
    icecast: {
      host: document.getElementById("icecast_host").value,
      port: Number(document.getElementById("icecast_port").value),
      username: document.getElementById("icecast_username").value,
      password: document.getElementById("icecast_password").value,
      mount: document.getElementById("icecast_mount").value,
      format: selectedFormat ? selectedFormat.value : "mp3",
      sample_rate: Number(document.getElementById("icecast_sample_rate").value),
      bitrate: Number(document.getElementById("icecast_bitrate").value)
    }
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
  if (Array.isArray(stream.outputs)) return stream.outputs.length;
  if (Array.isArray(stream.icecast_outputs)) return stream.icecast_outputs.length;
  if (Array.isArray(stream.icecast)) return stream.icecast.length;
  if (stream.icecast) return 1;
  return 0;
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

function findConfiguredOutput(streamId, outputId) {
  const stream = configuredStreams.find(item => item.id === streamId);
  if (!stream) return null;
  const outputs = streamOutputs(stream);
  const output = outputs.find(item => item.id === outputId);
  if (!output) return null;
  return {stream, output, outputs};
}

function setIcecastForm(icecast, enabled = true) {
  setValue("icecast_host", icecast.host || "");
  setValue("icecast_port", icecast.port || "");
  setValue("icecast_username", icecast.username || "");
  setValue("icecast_password", icecast.password || "");
  setValue("icecast_mount", icecast.mount || "");
  const format = icecast.format || "mp3";
  setChecked("icecast_format_mp3", format === "mp3");
  setChecked("icecast_format_ogg", format === "ogg");
  setValue("icecast_sample_rate", icecast.sample_rate || 24000);
  setValue("icecast_bitrate", icecast.bitrate || (format === "mp3" ? 64 : 48));
  setChecked("output_enabled", enabled);
}

function settingsIcecastPayload() {
  const selectedFormat = document.querySelector("input[name='settings_icecast_format']:checked");
  return {
    host: document.getElementById("settings_icecast_host").value,
    port: Number(document.getElementById("settings_icecast_port").value),
    username: document.getElementById("settings_icecast_username").value,
    password: document.getElementById("settings_icecast_password").value,
    mount: document.getElementById("settings_icecast_mount").value,
    format: selectedFormat ? selectedFormat.value : "mp3",
    sample_rate: Number(document.getElementById("settings_icecast_sample_rate").value),
    bitrate: Number(document.getElementById("settings_icecast_bitrate").value)
  };
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

function settingsCredentialsComplete() {
  const payload = settingsIcecastPayload();
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
  setValue("settings_icecast_host", icecast.host || "");
  setValue("settings_icecast_port", icecast.port || "");
  setValue("settings_icecast_username", icecast.username || "");
  setValue("settings_icecast_password", icecast.password || "");
  setValue("settings_icecast_mount", icecast.mount || "");
  const format = icecast.format || "mp3";
  setChecked("settings_icecast_format_mp3", format === "mp3");
  setChecked("settings_icecast_format_ogg", format === "ogg");
  setValue("settings_icecast_sample_rate", icecast.sample_rate || 24000);
  setValue("settings_icecast_bitrate", icecast.bitrate || (format === "mp3" ? 64 : 48));
}

function clearSettingsIcecastForm() {
  setSettingsIcecastForm({format: "mp3", sample_rate: 24000, bitrate: 64});
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

function setFallbackResult(message, kind = "") {
  const element = document.getElementById("fallback-result");
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
  return document.getElementById("tab_fallback").getAttribute("aria-selected") === "true" ? "fallback" : "outputs";
}

function showSettingsTab(name) {
  const fallback = name === "fallback";
  document.getElementById("tab_outputs").setAttribute("aria-selected", fallback ? "false" : "true");
  document.getElementById("tab_fallback").setAttribute("aria-selected", fallback ? "true" : "false");
  document.getElementById("tab_outputs").setAttribute("tabindex", fallback ? "-1" : "0");
  document.getElementById("tab_fallback").setAttribute("tabindex", fallback ? "0" : "-1");
  document.getElementById("panel_outputs").hidden = fallback;
  document.getElementById("panel_fallback").hidden = !fallback;
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
  setIcecastForm({format: "mp3", sample_rate: 24000, bitrate: 64}, true);
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
  wizardStep = Math.max(0, Math.min(3, step));
  renderWizard();
}

function setWizardPanel(id, visible) {
  document.getElementById(id).hidden = !visible;
}

function renderWizard() {
  const editMode = wizardMode === "edit";
  setText("stream_wizard_title", editMode ? "Edit Stream Output" : "Add Stream");
  setWizardPanel("wizard_step_station", !editMode && wizardStep === 0);
  setWizardPanel("wizard_step_credentials", editMode || wizardStep === 1);
  setWizardPanel("wizard_step_codec", editMode || wizardStep === 2);
  setWizardPanel("wizard_step_quality", editMode || wizardStep === 3);
  document.getElementById("cancel_wizard").hidden = editMode;
  document.getElementById("wizard_back").hidden = editMode || wizardStep === 0;
  document.getElementById("wizard_next").hidden = editMode || wizardStep === 3;
  document.getElementById("wizard_finish").hidden = editMode || wizardStep !== 3;
  document.getElementById("save_output").hidden = !editMode;
  document.getElementById("cancel_output_edit").hidden = !editMode;
  const next = document.getElementById("wizard_next");
  if (wizardStep === 0) {
    setDisabled(next, !selectedStation());
  } else if (wizardStep === 1) {
    setDisabled(next, !credentialsComplete());
  } else {
    setDisabled(next, false);
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
  renderIcecastOutputsTable(stream);
}

function outputStatusFor(stream, output) {
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
  menu.appendChild(toggle);
  menu.appendChild(edit);
  menu.appendChild(remove);
  cell.appendChild(button);
  cell.appendChild(menu);
  return cell;
}

function activeStreamRows(activeStreams, configured = configuredStreams) {
  const rows = [];
  const activeById = new Map((activeStreams || []).map(stream => [stream.id, stream]));
  for (const stream of configured || []) {
    const active = activeById.get(stream.id);
    if (active) {
      rows.push(active);
      activeById.delete(stream.id);
    } else {
      rows.push({
        id: stream.id,
        station: stream.station,
        outputs: streamOutputs(stream),
        status: stream.enabled === false ? "disabled" : "disabled"
      });
    }
  }
  for (const stream of activeById.values()) rows.push(stream);
  return rows;
}

function activeStreamSignature(rows) {
  return JSON.stringify(rows.map(stream => {
    const station = stream.station || {};
    return {
      id: stream.id || "",
      callsign: station.callsign || "",
      frequency: station.frequency || "",
      outputs: streamOutputs(stream).map(output => ({
        id: output.id || "",
        enabled: output.enabled !== false,
        type: output.type || "icecast"
      })),
      status: normalizeStreamStatus(stream.status)
    };
  }));
}

function renderActiveStreams(activeStreams, configured = configuredStreams) {
  const tbody = document.getElementById("active-streams-body");
  const rows = activeStreamRows(activeStreams, configured);
  const nextSignature = activeStreamSignature(rows);
  const enabledCount = rows.filter(stream => normalizeStreamStatus(stream.status) === "enabled").length;
  setText("summary_stream_count", enabledCount);
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
  menu.appendChild(edit);
  menu.appendChild(remove);
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
  renderActiveStreams([], configuredStreams);
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
    details.textContent = `${streamOutputCount(stream)} Icecast output${streamOutputCount(stream) === 1 ? "" : "s"}.`;
    const outputs = document.createElement("div");
    outputs.className = "actions";
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
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.dataset.action = "remove-stream";
    remove.dataset.streamId = stream.id;
    item.appendChild(title);
    item.appendChild(details);
    item.appendChild(outputs);
    item.appendChild(remove);
    list.appendChild(item);
  }
}

async function loadStreams() {
  const data = await request("/api/streams");
  renderStreams(data.streams || []);
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
    sample_rate: s.sample_rate,
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

function setStreamResult(message, kind = "") {
  const element = document.getElementById("stream-result");
  const className = kind === "success" ? "message success" : kind === "error" ? "message error" : "message";
  if (element.className !== className) element.className = className;
  const text = String(message || "");
  if (element.textContent !== text) element.textContent = text;
}

function showView(name) {
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== `view_${name}`;
  }
  for (const button of document.querySelectorAll("nav button[data-view]")) {
    if (button.dataset.view === name || (button.dataset.view === "streams" && name === "stream_settings")) {
      button.setAttribute("aria-current", "page");
    } else {
      button.removeAttribute("aria-current");
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
  if (name === "streams") query.set("view", "streams");
  if (name === "add_stream") query.set("view", "add_stream");
  if (name === "stream_settings") {
    query.set("view", "stream_settings");
    if (params.streamId) query.set("stream", params.streamId);
  }
  const text = query.toString();
  return text ? `/?${text}` : "/";
}

function routeFromLocation() {
  const query = new URLSearchParams(window.location.search);
  const view = query.get("view") || "dashboard";
  if (["dashboard", "rtl", "streams", "add_stream", "stream_settings"].includes(view)) {
    return {view, streamId: query.get("stream") || ""};
  }
  return {view: "dashboard", streamId: ""};
}

function routeState(view, params = {}) {
  return {view, streamId: params.streamId || ""};
}

function applyRoute(route) {
  if (route.view === "stream_settings") {
    const streamId = route.streamId || settingsStreamId;
    const stream = configuredStreams.find(item => item.id === streamId);
    if (stream) {
      settingsStreamId = streamId;
      outputTableSignature = "";
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
  if (route.view !== "stream_settings") settingsStreamId = "";
  showView(route.view);
}

function hasUnsavedNavigationState() {
  return Boolean(wizardDirty || outputFormIsOpen());
}

function confirmDiscardNavigation() {
  if (!hasUnsavedNavigationState()) return true;
  return window.confirm("Discard the current setup changes?");
}

function navigateTo(view, params = {}, replace = false, force = false) {
  if (!replace && !force && !confirmDiscardNavigation()) return;
  const url = routeForView(view, params);
  const state = routeState(view, params);
  if (replace) {
    history.replaceState(state, "", url);
  } else {
    history.pushState(state, "", url);
  }
  applyRoute(state);
}

function activeSdrLabel(settings) {
  if (!settings.serial) return "none";
  const select = document.getElementById("serial");
  const option = Array.from(select.options).find(item => item.value === settings.serial);
  return option ? option.textContent : `serial ${settings.serial}`;
}

function updateDashboard(data) {
  const settings = data.settings;
  setText("summary_sdr", activeSdrLabel(settings));
  setText("summary_sample_rate", `${settings.sample_rate} S/s`);
  setText("summary_gain", settings.gain === null ? "automatic" : `${settings.gain} dB`);
  activeStreamSnapshots = data.active_streams || [];
  renderActiveStreams(data.active_streams || [], configuredStreams);
  if (settingsStreamId) renderStreamSettings();
}

function syncControls(data) {
  const s = data.settings;
  gainValues = data.gain_values || [];
  setValue("serial", s.serial || "");
  setValue("sample_rate", s.sample_rate);
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
  applying = false;
}

function currentPayload() {
  const auto = document.getElementById("gain_auto").checked;
  const gainIndex = Number(document.getElementById("gain").value);
  return {
    serial: document.getElementById("serial").value,
    sample_rate: Number(document.getElementById("sample_rate").value),
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

for (const button of document.querySelectorAll("nav button[data-view]")) {
  button.addEventListener("click", () => navigateTo(button.dataset.view));
}

document.getElementById("tab_outputs").addEventListener("click", () => showSettingsTab("outputs"));
document.getElementById("tab_fallback").addEventListener("click", () => showSettingsTab("fallback"));
document.querySelector(".tabs").addEventListener("keydown", event => {
  const tabs = [document.getElementById("tab_outputs"), document.getElementById("tab_fallback")];
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
  showSettingsTab(tabs[nextIndex].id === "tab_fallback" ? "fallback" : "outputs");
});

window.addEventListener("popstate", event => {
  if (!confirmDiscardNavigation()) {
    history.pushState(routeState(currentViewName(), {streamId: settingsStreamId}), "", routeForView(currentViewName(), {streamId: settingsStreamId}));
    return;
  }
  applyRoute(event.state || routeFromLocation());
});

document.getElementById("open_add_stream").addEventListener("click", () => {
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
    editStreamSettings(target.dataset.streamId, target.dataset.outputId);
    return;
  }
  if (target.dataset.action === "remove-active-stream") {
    closeStreamActionMenus();
    try {
      const data = await request(`/api/streams?id=${encodeURIComponent(target.dataset.streamId)}`, {
        method: "DELETE"
      });
      renderStreams(data.streams || []);
      setStreamResult("");
    } catch (error) {
      setStreamResult(error.message, "error");
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
  if (!event.target || event.target.closest(".menu-cell")) return;
  closeStreamActionMenus();
});

document.addEventListener("focusin", event => {
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
    wizardDirty = true;
    icecastAuthPassed = false;
    renderWizard();
  });
}

for (const id of ["icecast_host", "icecast_port", "icecast_username", "icecast_password", "icecast_mount"]) {
  document.getElementById(id).addEventListener("input", () => {
    wizardDirty = true;
    icecastAuthPassed = false;
    icecastAuthSignature = "";
    renderWizard();
  });
}

for (const id of ["icecast_sample_rate", "icecast_bitrate", "output_enabled"]) {
  document.getElementById(id).addEventListener("change", () => {
    wizardDirty = true;
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
  "settings_icecast_host",
  "settings_icecast_port",
  "settings_icecast_username",
  "settings_icecast_password",
  "settings_icecast_mount",
  "settings_icecast_sample_rate",
  "settings_icecast_bitrate"
]) {
  document.getElementById(id).addEventListener("input", updateOutputFormButtons);
  document.getElementById(id).addEventListener("change", updateOutputFormButtons);
}

for (const formatControl of document.querySelectorAll("input[name='settings_icecast_format']")) {
  formatControl.addEventListener("change", updateOutputFormButtons);
}

document.getElementById("open_add_output").addEventListener("click", beginAddOutput);

document.getElementById("cancel_output_form").addEventListener("click", cancelOutputForm);

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
    renderStreams(data.streams || []);
    setOutputResult(data.message, data.success ? "success" : "error");
    if (data.success) cancelOutputForm();
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
    if (data.success) cancelOutputForm();
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
    try {
      const data = await request("/api/stream-output", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          stream_id: target.dataset.streamId,
          output_id: target.dataset.outputId,
          enabled: selected.output.enabled === false,
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
  setWizardStep(wizardStep - 1);
});

document.getElementById("wizard_next").addEventListener("click", async () => {
  if (wizardStep === 0) {
    setWizardStep(1);
    return;
  }
  if (wizardStep === 1) {
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
        setWizardStep(2);
      }
    } catch (error) {
      setStreamResult(error.message, "error");
    } finally {
      renderWizard();
    }
    return;
  }
  if (wizardStep === 2) {
    setWizardStep(3);
  }
});

document.getElementById("wizard_finish").addEventListener("click", async () => {
  const button = document.getElementById("wizard_finish");
  setDisabled(button, true);
  setStreamResult("Creating stream...");
  try {
    if (!icecastAuthPassed || icecastAuthSignature !== icecastCredentialSignature()) {
      throw new Error("Icecast credentials must be tested before creating the stream.");
    }
    const data = await request("/api/streams", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(streamPayload())
    });
    renderStreams(data.streams || []);
    setStreamResult(data.message, data.success ? "success" : "error");
    if (data.success) {
      finishWizard();
    }
  } catch (error) {
    setStreamResult(error.message, "error");
  } finally {
    setDisabled(button, false);
    renderWizard();
  }
});

window.addEventListener("beforeunload", event => {
  if (!wizardDirty && !outputFormIsOpen()) return;
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

async function refresh() {
  const data = await request("/api/status");
  applyStatus(data, {syncControls: false});
}

(async function init() {
  populateBitrates();
  const data = await request("/api/status");
  await loadDevices(data.settings.serial);
  await searchStations();
  await loadStreams();
  applyStatus(data, {syncControls: true});
  const initialRoute = routeFromLocation();
  navigateTo(initialRoute.view, {streamId: initialRoute.streamId}, true);
  setInterval(refresh, 1000);
  setInterval(async () => {
    try {
      const status = await request("/api/status");
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
