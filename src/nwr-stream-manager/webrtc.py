from __future__ import annotations

import ctypes
import ctypes.util
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import IQ_SAMPLE_RATE
from .encoder import EncoderError, PcmResampler


LOG = logging.getLogger(__name__)

WEBRTC_OPUS_SAMPLE_RATE = 48_000
WEBRTC_OPUS_CHANNELS = 1
WEBRTC_FRAME_SECONDS = 0.02
WEBRTC_OPUS_FRAME_SAMPLES = round(WEBRTC_OPUS_SAMPLE_RATE * WEBRTC_FRAME_SECONDS)
WEBRTC_MAX_OPUS_PACKET_BYTES = 4_000
WEBRTC_TARGET_BITRATE_KBPS = 128
WEBRTC_MIN_BITRATE_KBPS = 32
LIVE_AUDIO_TCP_BITRATE_LADDER_KBPS = (6, 8, 12, 16, 32, 64, 96, 128)
LIVE_AUDIO_TCP_TARGET_BITRATE_KBPS = 128
LIVE_AUDIO_TCP_SEND_SLOW_SECONDS = 0.06
LIVE_AUDIO_TCP_RECOVERY_STABLE_FEEDBACKS = 150
LIVE_AUDIO_TCP_THROUGHPUT_MARGIN = 0.82
LIVE_AUDIO_TCP_SEVERE_THROUGHPUT_MARGIN = 0.65
LIVE_AUDIO_TCP_REALTIME_DELIVERY_FLOOR = 0.92
LIVE_AUDIO_TCP_SEVERE_REALTIME_DELIVERY_FLOOR = 0.85
LIVE_AUDIO_TCP_THROUGHPUT_LIMIT_FEEDBACKS = 2
LIVE_AUDIO_TCP_RECOVERY_HOLD_FEEDBACKS = 500
WEBRTC_MONITOR_PREBUFFER_FRAMES = 6
WEBRTC_MONITOR_TARGET_LATENCY_FRAMES = 6
WEBRTC_MONITOR_LOW_WATER_FRAMES = 3
WEBRTC_MONITOR_LATENCY_HIGH_WATER_FRAMES = 14
WEBRTC_MONITOR_LATENCY_TRIM_TO_FRAMES = 8
WEBRTC_MONITOR_MAX_BUFFER_FRAMES = 32
WEBRTC_MONITOR_PREBUFFER_TIMEOUT_SECONDS = 0.24
WEBRTC_MONITOR_REFILL_TIMEOUT_SECONDS = 0.06


@dataclass(frozen=True)
class TcpOpusBitrateDecision:
    bitrate_kbps: int
    reason: str


class WebRtcError(RuntimeError):
    """Raised when WebRTC support cannot be initialized or used."""


class OpusSupportError(WebRtcError, EncoderError):
    """Raised when libopus cannot provide the raw Opus encoder API."""


class _OpusEncoderStruct(ctypes.Structure):
    pass


class _CtypesOpusModule:
    OPUS_OK = 0
    OPUS_APPLICATION_AUDIO = 2049
    OPUS_SET_BITRATE_REQUEST = 4002
    OPUS_SET_VBR_REQUEST = 4006
    OPUS_SET_COMPLEXITY_REQUEST = 4010
    OPUS_SET_SIGNAL_REQUEST = 4024
    OPUS_SIGNAL_VOICE = 3001

    OpusEncoder = _OpusEncoderStruct
    opus_int16 = ctypes.c_int16

    def __init__(self, library: ctypes.CDLL) -> None:
        self.library = library
        self.opus_encoder_create = library.opus_encoder_create
        self.opus_encoder_ctl = library.opus_encoder_ctl
        self.opus_encode = library.opus_encode
        self.opus_encoder_destroy = library.opus_encoder_destroy
        self.opus_strerror = library.opus_strerror


_OPUS_MODULE: Any | None = None


class TcpOpusBitrateController:
    def __init__(
        self,
        *,
        ladder_kbps: tuple[int, ...] = LIVE_AUDIO_TCP_BITRATE_LADDER_KBPS,
        target_kbps: int = LIVE_AUDIO_TCP_TARGET_BITRATE_KBPS,
        slow_send_seconds: float = LIVE_AUDIO_TCP_SEND_SLOW_SECONDS,
        recovery_stable_feedbacks: int = LIVE_AUDIO_TCP_RECOVERY_STABLE_FEEDBACKS,
        recovery_hold_feedbacks: int = LIVE_AUDIO_TCP_RECOVERY_HOLD_FEEDBACKS,
    ) -> None:
        ladder = tuple(sorted({int(value) for value in ladder_kbps if int(value) > 0}))
        if not ladder:
            raise ValueError("bitrate ladder must not be empty")
        if target_kbps not in ladder:
            raise ValueError("target bitrate must be present in bitrate ladder")
        self.ladder_kbps = ladder
        self.target_kbps = int(target_kbps)
        self.slow_send_seconds = max(0.0, float(slow_send_seconds))
        self.recovery_stable_feedbacks = max(1, int(recovery_stable_feedbacks))
        self.recovery_hold_feedbacks = max(0, int(recovery_hold_feedbacks))
        self.current_kbps = self.target_kbps
        self._stable_feedbacks = 0
        self._throughput_limited_feedbacks = 0
        self._recovery_hold_remaining = 0

    def update(
        self,
        *,
        send_seconds: float,
        send_failed: bool = False,
        latency_drop_count: int = 0,
        above_max_drop_count: int = 0,
        network_receive_kbps: float | None = None,
        media_delivery_ratio: float | None = None,
    ) -> TcpOpusBitrateDecision:
        send_limited = False
        try:
            send_limited = float(send_seconds) >= self.slow_send_seconds
        except (TypeError, ValueError):
            send_limited = False
        latency_limited = int(latency_drop_count) > 0 or int(above_max_drop_count) > 0
        throughput_limited = False
        severe_throughput_limited = False
        if network_receive_kbps is not None:
            try:
                receive_kbps = float(network_receive_kbps)
                throughput_limited = receive_kbps < self.current_kbps * LIVE_AUDIO_TCP_THROUGHPUT_MARGIN
                severe_throughput_limited = receive_kbps < self.current_kbps * LIVE_AUDIO_TCP_SEVERE_THROUGHPUT_MARGIN
            except (TypeError, ValueError):
                throughput_limited = False
                severe_throughput_limited = False
        realtime_limited = False
        severe_realtime_limited = False
        if media_delivery_ratio is not None:
            try:
                delivery_ratio = float(media_delivery_ratio)
                realtime_limited = delivery_ratio < LIVE_AUDIO_TCP_REALTIME_DELIVERY_FLOOR
                severe_realtime_limited = delivery_ratio < LIVE_AUDIO_TCP_SEVERE_REALTIME_DELIVERY_FLOOR
            except (TypeError, ValueError):
                realtime_limited = False
                severe_realtime_limited = False
        throughput_feedback_limited = throughput_limited or realtime_limited
        severe_feedback_limited = severe_throughput_limited or severe_realtime_limited
        if throughput_feedback_limited:
            self._throughput_limited_feedbacks += 1
        else:
            self._throughput_limited_feedbacks = 0
        confirmed_throughput_limited = (
            severe_feedback_limited
            or throughput_feedback_limited
            and self._throughput_limited_feedbacks >= LIVE_AUDIO_TCP_THROUGHPUT_LIMIT_FEEDBACKS
        )
        slow = (
            bool(send_failed)
            or send_limited
            or latency_limited
            or confirmed_throughput_limited
        )
        index = self.ladder_kbps.index(self.current_kbps)
        target_index = self.ladder_kbps.index(self.target_kbps)

        if slow:
            self._stable_feedbacks = 0
            if index > 0:
                self.current_kbps = self.ladder_kbps[index - 1]
                self._recovery_hold_remaining = self.recovery_hold_feedbacks
            if not throughput_feedback_limited:
                self._throughput_limited_feedbacks = 0
            if latency_limited:
                reason = "client latency feedback"
            elif confirmed_throughput_limited:
                reason = "client throughput feedback"
            else:
                reason = "send backpressure"
            return TcpOpusBitrateDecision(self.current_kbps, reason)

        if throughput_feedback_limited:
            self._stable_feedbacks = 0
            return TcpOpusBitrateDecision(self.current_kbps, "stable")

        if self._recovery_hold_remaining > 0:
            self._stable_feedbacks = 0
            self._recovery_hold_remaining -= 1
            return TcpOpusBitrateDecision(self.current_kbps, "stable")

        if index >= target_index:
            self._stable_feedbacks = 0
            return TcpOpusBitrateDecision(self.current_kbps, "stable")

        self._stable_feedbacks += 1
        if self._stable_feedbacks >= self.recovery_stable_feedbacks:
            self.current_kbps = self.ladder_kbps[index + 1]
            self._stable_feedbacks = 0
            return TcpOpusBitrateDecision(self.current_kbps, "recovery probe")
        return TcpOpusBitrateDecision(self.current_kbps, "stable")


class OpusEncoder:
    def __init__(
        self,
        *,
        input_sample_rate: int = IQ_SAMPLE_RATE,
        bitrate_kbps: int = WEBRTC_TARGET_BITRATE_KBPS,
        minimum_bitrate_kbps: int = WEBRTC_MIN_BITRATE_KBPS,
    ) -> None:
        self.opus = _load_opus_module()
        self.input_sample_rate = input_sample_rate
        self.minimum_bitrate_kbps = max(1, int(minimum_bitrate_kbps))
        self.resampler = PcmResampler(input_sample_rate, WEBRTC_OPUS_SAMPLE_RATE)
        self.closed = False
        self._pending_samples = np.array([], dtype=np.int16)
        self._configure_ctypes()
        self._encoder = self._create_encoder()
        self.bitrate_kbps = 0
        self._ctl(self.opus.OPUS_SET_VBR_REQUEST, ctypes.c_int(1))
        self._ctl(self.opus.OPUS_SET_COMPLEXITY_REQUEST, ctypes.c_int(10))
        self._ctl(
            self.opus.OPUS_SET_SIGNAL_REQUEST,
            ctypes.c_int(self.opus.OPUS_SIGNAL_VOICE),
        )
        self.set_bitrate(bitrate_kbps)

    def _configure_ctypes(self) -> None:
        o = self.opus
        o.opus_encoder_create.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        o.opus_encoder_create.restype = ctypes.POINTER(o.OpusEncoder)
        o.opus_encode.argtypes = [
            ctypes.POINTER(o.OpusEncoder),
            ctypes.POINTER(o.opus_int16),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
        ]
        o.opus_encode.restype = ctypes.c_int32
        o.opus_encoder_destroy.argtypes = [ctypes.POINTER(o.OpusEncoder)]
        o.opus_encoder_destroy.restype = None
        o.opus_strerror.argtypes = [ctypes.c_int]
        o.opus_strerror.restype = ctypes.c_char_p

    def _create_encoder(self):
        error = ctypes.c_int()
        encoder = self.opus.opus_encoder_create(
            WEBRTC_OPUS_SAMPLE_RATE,
            WEBRTC_OPUS_CHANNELS,
            self.opus.OPUS_APPLICATION_AUDIO,
            ctypes.byref(error),
        )
        if error.value != self.opus.OPUS_OK or not encoder:
            raise OpusSupportError(f"opus_encoder_create failed: {self._error(error.value)}")
        return encoder

    def set_bitrate(self, bitrate_kbps: int) -> None:
        bitrate_kbps = max(
            self.minimum_bitrate_kbps,
            min(WEBRTC_TARGET_BITRATE_KBPS, int(bitrate_kbps)),
        )
        if bitrate_kbps == self.bitrate_kbps:
            return
        self._ctl(self.opus.OPUS_SET_BITRATE_REQUEST, ctypes.c_int(bitrate_kbps * 1000))
        self.bitrate_kbps = bitrate_kbps

    def encode(self, pcm_s16le: bytes) -> list[bytes]:
        if self.closed:
            raise OpusSupportError("Opus encoder is closed")
        pcm_s16le = self.resampler.process(pcm_s16le)
        samples = np.frombuffer(pcm_s16le, dtype="<i2")
        if len(self._pending_samples):
            samples = np.concatenate((self._pending_samples, samples))
        packets: list[bytes] = []
        frame_samples = WEBRTC_OPUS_FRAME_SAMPLES
        for offset in range(0, len(samples) - frame_samples + 1, frame_samples):
            frame = np.ascontiguousarray(samples[offset : offset + frame_samples])
            packets.append(self._encode_frame(frame))
        consumed = len(packets) * frame_samples
        self._pending_samples = np.ascontiguousarray(samples[consumed:])
        return packets

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.opus.opus_encoder_destroy(self._encoder)

    def _encode_frame(self, frame: np.ndarray) -> bytes:
        output = (ctypes.c_ubyte * WEBRTC_MAX_OPUS_PACKET_BYTES)()
        pointer = frame.ctypes.data_as(ctypes.POINTER(self.opus.opus_int16))
        result = self.opus.opus_encode(
            self._encoder,
            pointer,
            WEBRTC_OPUS_FRAME_SAMPLES,
            output,
            WEBRTC_MAX_OPUS_PACKET_BYTES,
        )
        if result < 0:
            raise OpusSupportError(f"opus_encode failed: {self._error(int(result))}")
        return bytes(output[:result])

    def _ctl(self, request: int, value) -> None:
        result = self.opus.opus_encoder_ctl(self._encoder, request, value)
        if result != self.opus.OPUS_OK:
            raise OpusSupportError(f"opus_encoder_ctl failed: {self._error(result)}")

    def _error(self, code: int) -> str:
        message = self.opus.opus_strerror(code)
        if isinstance(message, bytes):
            return message.decode("utf-8", "replace")
        return str(code)


class WebRtcAudioSource:
    def __init__(
        self,
        *,
        max_frames: int = WEBRTC_MONITOR_MAX_BUFFER_FRAMES,
        prebuffer_frames: int = WEBRTC_MONITOR_PREBUFFER_FRAMES,
        target_latency_frames: int = WEBRTC_MONITOR_TARGET_LATENCY_FRAMES,
        low_water_frames: int = WEBRTC_MONITOR_LOW_WATER_FRAMES,
        latency_high_water_frames: int = WEBRTC_MONITOR_LATENCY_HIGH_WATER_FRAMES,
        latency_trim_to_frames: int = WEBRTC_MONITOR_LATENCY_TRIM_TO_FRAMES,
        prebuffer_timeout_seconds: float = WEBRTC_MONITOR_PREBUFFER_TIMEOUT_SECONDS,
        refill_timeout_seconds: float = WEBRTC_MONITOR_REFILL_TIMEOUT_SECONDS,
        sample_rate: int = IQ_SAMPLE_RATE,
    ) -> None:
        self.sample_rate = max(1, int(sample_rate))
        self.max_frames = max(1, int(max_frames))
        self.prebuffer_frames = max(0, min(int(prebuffer_frames), self.max_frames))
        self.target_latency_frames = max(1, min(int(target_latency_frames), self.max_frames))
        self.low_water_frames = max(0, min(int(low_water_frames), self.max_frames))
        self.latency_high_water_frames = max(
            self.target_latency_frames + 1,
            min(int(latency_high_water_frames), self.max_frames),
        )
        self.latency_trim_to_frames = max(
            self.target_latency_frames,
            min(int(latency_trim_to_frames), self.latency_high_water_frames - 1),
        )
        self.prebuffer_timeout_seconds = max(0.0, float(prebuffer_timeout_seconds))
        self.refill_timeout_seconds = max(0.0, float(refill_timeout_seconds))
        self.frame_bytes = round(self.sample_rate * WEBRTC_FRAME_SECONDS) * 2
        self.buffer: deque[bytes] = deque()
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.pushed_frames = 0
        self.read_frames = 0
        self.dropped_frames = 0
        self.stale_frames = 0
        self.underrun_frames = 0
        self.max_buffered_frames = 0
        self.last_frame_at = 0.0
        self.last_underrun_log_at = 0.0
        self._prebuffered = False

    def push_pcm(self, pcm_s16le: bytes) -> None:
        if self.closed.is_set():
            return
        frames = self._split_frames(pcm_s16le)
        if not frames:
            return
        now = time.monotonic()
        with self.lock:
            for frame in frames:
                if len(self.buffer) >= self.max_frames:
                    self.buffer.popleft()
                    self.dropped_frames += 1
                self.buffer.append(frame)
                self.pushed_frames += 1
                self.max_buffered_frames = max(self.max_buffered_frames, len(self.buffer))
                self.last_frame_at = now
            if len(self.buffer) > self.latency_high_water_frames:
                while len(self.buffer) > self.latency_trim_to_frames:
                    self.buffer.popleft()
                    self.stale_frames += 1

    def get_latest_pcm(self, timeout: float = WEBRTC_FRAME_SECONDS) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self.closed.is_set():
            with self.lock:
                if self.buffer:
                    self.read_frames += 1
                    return self.buffer.popleft()
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.005, max(0.0, deadline - time.monotonic())))
        self.underrun_frames += 1
        self._log_underrun()
        return b"\x00" * self.frame_bytes

    def read_pcm_blocking(self, timeout: float = 0.25) -> bytes:
        if not self._prebuffered and self.prebuffer_frames:
            self._wait_for_buffer_blocking(
                self.prebuffer_frames,
                max(timeout, self.prebuffer_timeout_seconds),
            )
            self._prebuffered = True
        elif self.low_water_frames:
            with self.lock:
                buffered = len(self.buffer)
            if 0 < buffered < self.low_water_frames:
                self._wait_for_buffer_blocking(
                    self.low_water_frames,
                    max(WEBRTC_FRAME_SECONDS, self.refill_timeout_seconds),
                )
        frame = self._pop_frame_blocking(timeout)
        if frame is None:
            self.underrun_frames += 1
            self._log_underrun()
            self._prebuffered = False
            return b"\x00" * self.frame_bytes
        self.read_frames += 1
        return frame

    def clear_buffer(self) -> None:
        with self.lock:
            dropped = len(self.buffer)
            self.buffer.clear()
            self.stale_frames += dropped
            self._prebuffered = True

    def stats(self) -> dict[str, Any]:
        with self.lock:
            buffered_frames = len(self.buffer)
            last_frame_at = self.last_frame_at
            return {
                "buffered_frames": buffered_frames,
                "buffered_ms": round(buffered_frames * WEBRTC_FRAME_SECONDS * 1000.0, 1),
                "max_buffered_frames": self.max_buffered_frames,
                "max_buffered_ms": round(self.max_buffered_frames * WEBRTC_FRAME_SECONDS * 1000.0, 1),
                "max_frames": self.max_frames,
                "prebuffer_frames": self.prebuffer_frames,
                "target_latency_frames": self.target_latency_frames,
                "low_water_frames": self.low_water_frames,
                "latency_high_water_frames": self.latency_high_water_frames,
                "latency_trim_to_frames": self.latency_trim_to_frames,
                "sample_rate": self.sample_rate,
                "pushed_frames": self.pushed_frames,
                "read_frames": self.read_frames,
                "dropped_frames": self.dropped_frames,
                "stale_frames": self.stale_frames,
                "underrun_frames": self.underrun_frames,
                "last_frame_age_seconds": (
                    round(time.monotonic() - last_frame_at, 3) if last_frame_at else None
                ),
            }

    def close(self) -> None:
        self.closed.set()

    def _split_frames(self, pcm_s16le: bytes) -> list[bytes]:
        frames = []
        for offset in range(0, len(pcm_s16le), self.frame_bytes):
            frame = pcm_s16le[offset : offset + self.frame_bytes]
            if len(frame) < self.frame_bytes:
                frame += b"\x00" * (self.frame_bytes - len(frame))
            if frame:
                frames.append(frame)
        return frames

    def _wait_for_buffer_blocking(self, frame_count: int, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self.closed.is_set():
            with self.lock:
                if len(self.buffer) >= frame_count:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.005, remaining))

    def _pop_frame_blocking(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self.closed.is_set():
            with self.lock:
                if self.buffer:
                    return self.buffer.popleft()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.005, remaining))
        return None

    def _log_underrun(self) -> None:
        now = time.monotonic()
        if now - self.last_underrun_log_at < 10.0:
            return
        self.last_underrun_log_at = now
        with self.lock:
            buffered = len(self.buffer)
            pushed = self.pushed_frames
            read = self.read_frames
            dropped = self.dropped_frames
            stale = self.stale_frames
            underruns = self.underrun_frames
            last_frame_at = self.last_frame_at
        LOG.warning(
            "live audio PCM source underrun: buffered=%s pushed=%s read=%s dropped=%s stale=%s underruns=%s last_frame_age=%.3fs",
            buffered,
            pushed,
            read,
            dropped,
            stale,
            underruns,
            (now - last_frame_at) if last_frame_at else -1.0,
        )


def _load_opus_module():
    global _OPUS_MODULE
    if _OPUS_MODULE is not None:
        return _OPUS_MODULE
    _OPUS_MODULE = _load_system_opus_module()
    return _OPUS_MODULE


def _load_system_opus_module():
    required = (
        "opus_encoder_create",
        "opus_encoder_ctl",
        "opus_encode",
        "opus_encoder_destroy",
        "opus_strerror",
    )
    candidates = [
        ctypes.util.find_library("opus"),
        "libopus.so.0",
        "libopus.so",
        "opus.dll",
        "libopus.dylib",
    ]
    errors = []
    for candidate in dict.fromkeys(filter(None, candidates)):
        try:
            library = ctypes.CDLL(candidate)
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
            continue
        missing = [name for name in required if not hasattr(library, name)]
        if missing:
            errors.append(f"{candidate}: missing symbols {', '.join(missing)}")
            continue
        return _CtypesOpusModule(library)
    detail = "; ".join(errors) if errors else "ctypes could not locate libopus"
    raise OpusSupportError(f"system libopus could not be loaded: {detail}")

