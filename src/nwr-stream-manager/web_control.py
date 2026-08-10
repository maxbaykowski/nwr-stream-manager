from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mimetypes
import os
import queue
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    from .dsp import IqChannelizer
    from .eas_recording import EasRecorderOutput
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
    from .same_data import lookup_event, lookup_location
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
    AudioEffectsProcessor = audio_effects.AudioEffectsProcessor
    deemphasis_makeup_gain = audio_effects.deemphasis_makeup_gain
    AUDIO_NYQUIST_HZ = config_module.AUDIO_NYQUIST_HZ
    AudioConfig = config_module.AudioConfig
    EasRecordingConfig = config_module.EasRecordingConfig
    IcecastConfig = config_module.IcecastConfig
    IQ_SAMPLE_RATE = config_module.IQ_SAMPLE_RATE
    parse_audio_config = config_module.parse_audio_config
    IqChannelizer = dsp.IqChannelizer
    EasRecorderOutput = eas_recording.EasRecorderOutput
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
    lookup_event = same_data.lookup_event
    lookup_location = same_data.lookup_location

repo_root = Path(__file__).resolve().parents[2]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from icecastauth import IcecastSettings, normalize_server, test_mountpoint_authentication

import numpy as np


LOG = logging.getLogger(__name__)
STATE_DIRECTORY_NAME = "nwr-stream-manager"
STATE_FILE_NAME = "rtl-control.json"
STREAMS_STATE_FILE_NAME = "streams.json"
STREAMS_DIRECTORY_NAME = "streams"
STREAM_CONFIG_FILE_NAME = "config.json"
STREAMS_DIRECTORY_MARKER_FILE_NAME = ".per-stream-configs"
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
STREAM_SILENCE_FRAME = b"\x00" * STREAM_FRAME_BYTES
STREAM_IDLE_DETECTION_SECONDS = 1.0
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


@dataclass(frozen=True)
class WebEasRecordingSettings:
    enabled: bool = False
    pre_seconds: float = 2.0
    post_seconds: float = 5.0
    max_seconds: int = 120
    format: str = "wav"


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
        station = self.stream["station"]
        dc_blocker = IqDcBlocker()
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int, int] | None = None
        demodulator = ComplexNfmDemodulator()
        audio_config = audio_config_from_stream(self.stream)
        effects = AudioEffectsProcessor(audio_config)
        pending = np.array([], dtype=np.float32)
        header = getattr(encoder, "header", b"")
        if header:
            sink.write(header)
        fallback = load_web_fallback_audio()
        fallback_state = WebFallbackPlaybackState()
        last_real_audio = time.monotonic()
        idle_output_active = False
        while not self.stop_event.is_set():
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
                    encoded = encoder.encode(STREAM_SILENCE_FRAME)
                    if encoded:
                        sink.write(encoded)
                    continue
                if not fallback_state.active:
                    fallback_state.active = True
                    LOG.info("starting fallback audio for %s after %.1f seconds without IQ", station.get("callsign"), idle_seconds)
                encoded = encoder.encode(next_web_fallback_frame(fallback, fallback_state, fallback_settings.loop_delay_seconds))
                if encoded:
                    sink.write(encoded)
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
                next_audio_config = audio_config_from_stream(self.stream)
                if next_audio_config != audio_config:
                    audio_config = next_audio_config
                    changed_effects = effects.update_config(audio_config)
                    LOG.info(
                        "applied audio effects update for %s without reconnecting Icecast: %s",
                        station.get("callsign"),
                        ", ".join(changed_effects) or "none",
                    )
                pcm = float_to_s16(effects.process(frame))
                encoded = encoder.encode(pcm)
                if encoded:
                    sink.write(encoded)


class EasStreamWorker:
    def __init__(
        self,
        *,
        stream: dict[str, Any],
        config: EasRecordingConfig,
        fanout: RawRtlFanout,
        fallback_settings_provider,
    ) -> None:
        self.stream = stream
        self.config = config
        self.fanout = fanout
        self.fallback_settings_provider = fallback_settings_provider
        self.queue = fanout.subscribe(max_chunks=64)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"eas-recorder-{stream['id']}", daemon=True)
        self.status = "disabled"
        self.error: str | None = None
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

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            self.error = error

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            error = self.error
        return {
            "id": self.id,
            "status": status,
            "error": error,
            "directory": self.config.directory,
        }

    def _run(self) -> None:
        recorder = None
        try:
            recorder = EasRecorderOutput(self.config)
            self._set_status("enabled")
            self._produce_pcm(recorder)
        except Exception as exc:
            if not self.stop_event.is_set():
                self._set_status("needs-attention", str(exc))
                LOG.warning("EAS recorder failed for %s: %s", self.stream.get("station", {}).get("callsign"), exc)
        finally:
            if recorder is not None:
                try:
                    recorder.close()
                except Exception as exc:
                    LOG.debug("EAS recorder close failed: %s", exc)
            self._set_status("disabled")

    def _produce_pcm(self, recorder: EasRecorderOutput) -> None:
        station = self.stream["station"]
        target_frequency_hz = float(station["frequency"]) * 1_000_000
        dc_blocker = IqDcBlocker()
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int, int] | None = None
        demodulator = ComplexNfmDemodulator()
        audio_config = audio_config_from_stream(self.stream)
        effects = AudioEffectsProcessor(audio_config)
        pending = np.array([], dtype=np.float32)
        fallback = load_web_fallback_audio()
        fallback_state = WebFallbackPlaybackState()
        last_real_audio = time.monotonic()
        idle_output_active = False
        while not self.stop_event.is_set():
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
                    recorder.write(STREAM_SILENCE_FRAME)
                    continue
                if not fallback_state.active:
                    fallback_state.active = True
                    LOG.info("starting EAS fallback audio for %s after %.1f seconds without IQ", station.get("callsign"), idle_seconds)
                recorder.write(next_web_fallback_frame(fallback, fallback_state, fallback_settings.loop_delay_seconds))
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
            iq = rtl_u8_to_complex64(batch.data)
            audio = demodulator.process(channelizer.process_complex(dc_blocker.process(iq)))
            if len(audio) == 0:
                continue
            last_real_audio = time.monotonic()
            if fallback_state.active:
                LOG.info("stopping EAS fallback audio for %s", station.get("callsign"))
            fallback_state.reset()
            pending = np.concatenate((pending, audio.astype(np.float32, copy=False)))
            while len(pending) >= STREAM_FRAME_SAMPLES:
                frame = pending[:STREAM_FRAME_SAMPLES]
                pending = pending[STREAM_FRAME_SAMPLES:]
                next_audio_config = audio_config_from_stream(self.stream)
                if next_audio_config != audio_config:
                    audio_config = next_audio_config
                    changed_effects = effects.update_config(audio_config)
                    LOG.info(
                        "applied EAS recorder audio effects update for %s: %s",
                        station.get("callsign"),
                        ", ".join(changed_effects) or "none",
                    )
                recorder.write(float_to_s16(effects.process(frame)))


class RtlControlService:
    def __init__(self, state_path: Path, log_handler: RingLogHandler) -> None:
        self.state_path = state_path
        self.streams_state_path = state_path.with_name(STREAMS_STATE_FILE_NAME)
        self.streams_directory = state_path.parent / STREAMS_DIRECTORY_NAME
        self.fallback_state_path = state_path.with_name(FALLBACK_STATE_FILE_NAME)
        self.log_handler = log_handler
        self.lock = threading.RLock()
        self.settings = load_settings(state_path)
        self.streams = load_streams(self.streams_directory, self.streams_state_path)
        self.fallback_settings = load_fallback_settings(self.fallback_state_path)
        self.stations = load_station_database()
        self.capture: RtlCaptureSource | None = None
        self.raw_fanout: RawRtlFanout | None = None
        self.monitor_queue: queue.Queue | None = None
        self.drain_thread: threading.Thread | None = None
        self.drain_stop = threading.Event()
        self.stream_workers: dict[str, IcecastStreamWorker] = {}
        self.eas_workers: dict[str, EasStreamWorker] = {}
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
                "active_eas_recorders": self._active_eas_recorders_locked(),
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
            remove_stream_configs(self.streams_directory, removed)
            self._sync_stream_workers_locked()
        LOG.info("removed stream %s", stream_id)
        return self.stream_status()

    def update_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        if "enabled" not in payload:
            raise ValueError("stream enabled state is required")
        enabled = bool(payload.get("enabled"))
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["enabled"] = enabled
            stream["updated_at"] = time.time()
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
            key = f"{stream_id}:{output_id}"
            worker = self.stream_workers.pop(key, None)
            if worker is not None:
                worker.stop()
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
        desired_eas: dict[str, tuple[dict[str, Any], EasRecordingConfig]] = {}
        if fanout is not None:
            for stream in self.streams:
                if not stream.get("enabled", True):
                    continue
                for output in stream_outputs(stream):
                    if not output.get("enabled", True):
                        continue
                    key = stream_worker_key(stream, output)
                    desired[key] = (stream, output)
                eas_settings = eas_recording_settings_from_stream(stream)
                if eas_settings.enabled:
                    desired_eas[str(stream.get("id", ""))] = (
                        stream,
                        eas_config_from_stream(stream, self.state_path.parent, eas_settings),
                    )

        for key in list(self.stream_workers):
            if key not in desired:
                worker = self.stream_workers.pop(key)
                worker.stop()
        for key in list(self.eas_workers):
            current = self.eas_workers[key]
            desired_item = desired_eas.get(key)
            if desired_item is None or current.config != desired_item[1]:
                worker = self.eas_workers.pop(key)
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
        for key, (stream, config) in desired_eas.items():
            if key in self.eas_workers:
                continue
            worker = EasStreamWorker(
                stream=stream,
                config=config,
                fanout=fanout,
                fallback_settings_provider=self.fallback_settings_snapshot,
            )
            self.eas_workers[key] = worker
            worker.start()
            LOG.info(
                "started EAS recorder for %s in %s",
                stream.get("station", {}).get("callsign", "unknown"),
                config.directory,
            )

    def _stop_stream_workers_locked(self) -> None:
        for worker in list(self.stream_workers.values()):
            worker.stop()
        self.stream_workers = {}
        for worker in list(self.eas_workers.values()):
            worker.stop()
        self.eas_workers = {}

    def _active_streams_locked(self) -> list[dict[str, Any]]:
        return [worker.snapshot() for worker in self.stream_workers.values()]

    def _active_eas_recorders_locked(self) -> list[dict[str, Any]]:
        return [worker.snapshot() for worker in self.eas_workers.values()]


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
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
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
            self.end_headers()
            with path.open("rb") as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        finally:
            if delete_after:
                path.unlink(missing_ok=True)


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
      <button id="nav_dashboard" type="button" data-view="dashboard" aria-current="page">Dashboard</button>
      <button id="nav_rtl" type="button" data-view="rtl">Configure RTL-SDR</button>
      <button id="nav_streams" type="button" data-view="streams">Manage Streams</button>
      <button id="nav_eas_alerts" type="button" data-view="eas_alerts" hidden>EAS alerts</button>
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
      <label class="checkbox-row">
        <input id="stream_enabled" type="checkbox">
        Stream enabled
      </label>
      <div class="tabs" role="tablist" aria-label="Stream settings sections">
        <button id="tab_outputs" type="button" role="tab" aria-selected="true" aria-controls="panel_outputs" tabindex="0">Outputs</button>
        <button id="tab_audio" type="button" role="tab" aria-selected="false" aria-controls="panel_audio" tabindex="-1">Audio Effects</button>
        <button id="tab_eas" type="button" role="tab" aria-selected="false" aria-controls="panel_eas" tabindex="-1">EAS Recording</button>
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

async function setStreamEnabled(streamId, enabled, resultHandler = setStreamResult) {
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
    deemphasis: {enabled: true, tau: 530.0},
    highpass: {enabled: false, frequency: 300.0, sharpness: 0.0},
    lowpass: {enabled: false, frequency: 4000.0, sharpness: 0.0},
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
    {name: "audio", button: "tab_audio", panel: "panel_audio"},
    {name: "eas", button: "tab_eas", panel: "panel_eas"},
    {name: "fallback", button: "tab_fallback", panel: "panel_fallback"}
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
  if (stream) setChecked("stream_enabled", stream.enabled !== false);
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
  if (output.enabled === false || canDisableIcecastOutput(stream, output)) {
    menu.appendChild(toggle);
  }
  menu.appendChild(edit);
  if (canRemoveIcecastOutput(stream, output)) {
    menu.appendChild(remove);
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
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.textContent = stream.enabled === false ? "Start stream" : "Stop stream";
  toggle.setAttribute("role", "menuitem");
  toggle.dataset.action = "toggle-active-stream";
  toggle.dataset.streamId = stream.id || "";
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
  menu.appendChild(toggle);
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
    details.textContent = `${streamOutputCount(stream)} output${streamOutputCount(stream) === 1 ? "" : "s"}.`;
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
  for (const button of document.querySelectorAll("nav button[data-view]")) {
    if (
      button.dataset.view === name ||
      (button.dataset.view === "streams" && name === "stream_settings") ||
      (button.dataset.view === "eas_alerts" && ["eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(name))
    ) {
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
  if (["dashboard", "rtl", "streams", "add_stream", "stream_settings", "eas_alerts", "eas_alert_export", "eas_alert_delete", "eas_alert_detail"].includes(view)) {
    return {
      view,
      streamId: query.get("stream") || "",
      alertId: query.get("alert") || "",
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
    page: Math.max(1, Number(params.page || 1))
  };
}

function applyRoute(route) {
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

function currentRouteParams() {
  const view = currentViewName();
  if (view === "stream_settings") return {streamId: settingsStreamId};
  if (view === "eas_alerts") return {streamId: easAlertStreamId, page: easAlertPage};
  if (view === "eas_alert_export" || view === "eas_alert_delete") return {streamId: easAlertStreamId, page: easAlertPage};
  if (view === "eas_alert_detail") return {streamId: easAlertStreamId, alertId: easAlertDetailId, page: easAlertReturnPage};
  return {};
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
  const now = Date.now();
  if (now - lastEasAlertRefreshAt > 10000) {
    lastEasAlertRefreshAt = now;
    loadEasAlertStreams({preserve: true, quiet: true});
  }
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

for (const button of document.querySelectorAll("nav button[data-view]")) {
  button.addEventListener("click", () => navigateTo(button.dataset.view));
}

document.getElementById("tab_outputs").addEventListener("click", () => showSettingsTab("outputs"));
document.getElementById("tab_audio").addEventListener("click", () => showSettingsTab("audio"));
document.getElementById("tab_eas").addEventListener("click", () => showSettingsTab("eas"));
document.getElementById("tab_fallback").addEventListener("click", () => showSettingsTab("fallback"));
document.querySelector(".tabs").addEventListener("keydown", event => {
  const tabs = [document.getElementById("tab_outputs"), document.getElementById("tab_audio"), document.getElementById("tab_eas"), document.getElementById("tab_fallback")];
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
  if (target.dataset.action === "toggle-active-stream") {
    closeStreamActionMenus();
    const stream = configuredStreams.find(item => item.id === target.dataset.streamId);
    if (!stream) {
      setStreamResult("Stream was not found.", "error");
      return;
    }
    try {
      await setStreamEnabled(target.dataset.streamId, stream.enabled === false, setStreamResult);
    } catch (error) {
      setStreamResult(error.message, "error");
    }
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
  if (!window.confirm("Remove this EAS alert and its audio file?")) return;
  await removeCurrentEasAlert();
});

async function refresh() {
  const data = await request("/api/status");
  applyStatus(data, {syncControls: false});
}

(async function init() {
  populateBitrates();
  selectAudioEffect("volume", false);
  const data = await request("/api/status");
  await loadDevices(data.settings.serial);
  await searchStations();
  await loadStreams();
  await loadEasAlertStreams({preserve: true, quiet: true});
  applyStatus(data, {syncControls: true});
  const initialRoute = routeFromLocation();
  navigateTo(initialRoute.view, {
    streamId: initialRoute.streamId,
    alertId: initialRoute.alertId,
    page: initialRoute.page
  }, true);
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
