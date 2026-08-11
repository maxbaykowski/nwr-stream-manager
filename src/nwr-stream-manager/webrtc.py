from __future__ import annotations

import ctypes
import importlib.util
import logging
import queue
import threading
import time
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
WEBRTC_BITRATE_STEP_KBPS = 8
WEBRTC_RECOVERY_STABLE_FEEDBACKS = 12


class WebRtcError(RuntimeError):
    """Raised when WebRTC support cannot be initialized or used."""


class OpusSupportError(WebRtcError, EncoderError):
    """Raised when PyOgg/libopus cannot provide the raw Opus encoder API."""


@dataclass(frozen=True)
class WebRtcCapabilityReport:
    transport_available: bool
    opus_available: bool
    target_bitrate_kbps: int = WEBRTC_TARGET_BITRATE_KBPS
    minimum_bitrate_kbps: int = WEBRTC_MIN_BITRATE_KBPS
    bitrate_step_kbps: int = WEBRTC_BITRATE_STEP_KBPS
    transport_error: str = ""
    opus_error: str = ""

    @property
    def available(self) -> bool:
        return self.transport_available and self.opus_available

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "transport_available": self.transport_available,
            "opus_available": self.opus_available,
            "target_bitrate_kbps": self.target_bitrate_kbps,
            "minimum_bitrate_kbps": self.minimum_bitrate_kbps,
            "bitrate_step_kbps": self.bitrate_step_kbps,
            "transport_error": self.transport_error,
            "opus_error": self.opus_error,
        }


def server_webrtc_capabilities() -> WebRtcCapabilityReport:
    transport_error = ""
    opus_error = ""
    transport_available = importlib.util.find_spec("aiortc") is not None
    if not transport_available:
        transport_error = "The 'aiortc' package is not installed."
    try:
        _load_opus_module()
        opus_available = True
    except Exception as exc:
        opus_available = False
        opus_error = str(exc)
    return WebRtcCapabilityReport(
        transport_available=transport_available,
        opus_available=opus_available,
        transport_error=transport_error,
        opus_error=opus_error,
    )


class OpusBitrateController:
    def __init__(
        self,
        *,
        target_kbps: int = WEBRTC_TARGET_BITRATE_KBPS,
        minimum_kbps: int = WEBRTC_MIN_BITRATE_KBPS,
        step_kbps: int = WEBRTC_BITRATE_STEP_KBPS,
        recovery_stable_feedbacks: int = WEBRTC_RECOVERY_STABLE_FEEDBACKS,
    ) -> None:
        if minimum_kbps <= 0:
            raise ValueError("minimum bitrate must be positive")
        if target_kbps < minimum_kbps:
            raise ValueError("target bitrate must be greater than or equal to minimum")
        if step_kbps <= 0:
            raise ValueError("bitrate step must be positive")
        self.target_kbps = target_kbps
        self.minimum_kbps = minimum_kbps
        self.step_kbps = step_kbps
        self.recovery_stable_feedbacks = max(1, recovery_stable_feedbacks)
        self.current_kbps = target_kbps
        self._stable_feedbacks = 0

    def update(
        self,
        *,
        available_bitrate_bps: int | None = None,
        packet_loss_fraction: float = 0.0,
        rtt_ms: float | None = None,
    ) -> int:
        slow = False
        if available_bitrate_bps is not None:
            slow = available_bitrate_bps < self.current_kbps * 1000
        if packet_loss_fraction >= 0.03:
            slow = True
        if rtt_ms is not None and rtt_ms >= 500:
            slow = True

        if slow:
            self._stable_feedbacks = 0
            self.current_kbps = max(
                self.minimum_kbps, self.current_kbps - self.step_kbps
            )
            return self.current_kbps

        if self.current_kbps >= self.target_kbps:
            self._stable_feedbacks = 0
            return self.current_kbps

        self._stable_feedbacks += 1
        if self._stable_feedbacks >= self.recovery_stable_feedbacks:
            self.current_kbps = min(
                self.target_kbps, self.current_kbps + self.step_kbps
            )
            self._stable_feedbacks = 0
        return self.current_kbps


class PyOggOpusEncoder:
    def __init__(
        self,
        *,
        input_sample_rate: int = IQ_SAMPLE_RATE,
        bitrate_kbps: int = WEBRTC_TARGET_BITRATE_KBPS,
    ) -> None:
        self.opus = _load_opus_module()
        self.input_sample_rate = input_sample_rate
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
            WEBRTC_MIN_BITRATE_KBPS,
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
    def __init__(self, *, max_frames: int = 8) -> None:
        self.queue: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)
        self.closed = threading.Event()
        self.dropped_frames = 0
        self.last_frame_at = 0.0

    def push_pcm(self, pcm_s16le: bytes) -> None:
        if self.closed.is_set():
            return
        if self.queue.full():
            try:
                self.queue.get_nowait()
                self.dropped_frames += 1
            except queue.Empty:
                pass
        try:
            self.queue.put_nowait(pcm_s16le)
            self.last_frame_at = time.monotonic()
        except queue.Full:
            self.dropped_frames += 1

    def get_latest_pcm(self, timeout: float = WEBRTC_FRAME_SECONDS) -> bytes:
        if self.closed.is_set():
            return b""
        try:
            return self.queue.get(timeout=timeout)
        except queue.Empty:
            return b"\x00\x00" * round(IQ_SAMPLE_RATE * WEBRTC_FRAME_SECONDS)

    def close(self) -> None:
        self.closed.set()


class AiortcSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, Any] = {}
        self.lock = threading.Lock()

    async def accept_offer(
        self,
        *,
        session_id: str,
        sdp: str,
        type: str = "offer",
        tracks: tuple[Any, ...] = (),
    ) -> dict[str, str]:
        aiortc = _load_aiortc()
        peer = aiortc.RTCPeerConnection()
        for track in tracks:
            peer.addTrack(track)
        await peer.setRemoteDescription(aiortc.RTCSessionDescription(sdp=sdp, type=type))
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        with self.lock:
            old_peer = self.sessions.pop(session_id, None)
            self.sessions[session_id] = peer
        if old_peer is not None:
            await old_peer.close()
        return {
            "sdp": peer.localDescription.sdp,
            "type": peer.localDescription.type,
        }

    async def close(self, session_id: str) -> None:
        with self.lock:
            peer = self.sessions.pop(session_id, None)
        if peer is not None:
            await peer.close()

    async def close_all(self) -> None:
        with self.lock:
            peers = list(self.sessions.values())
            self.sessions.clear()
        for peer in peers:
            await peer.close()


def bitrate_feedback_from_stats(stats: Any) -> dict[str, float | int | None]:
    available_bitrate_bps = getattr(stats, "availableOutgoingBitrate", None)
    rtt_seconds = getattr(stats, "roundTripTime", None)
    packets_lost = getattr(stats, "packetsLost", None)
    packets_sent = getattr(stats, "packetsSent", None)
    packet_loss_fraction = 0.0
    if packets_lost is not None and packets_sent:
        packet_loss_fraction = max(0.0, float(packets_lost)) / max(1.0, float(packets_sent))
    return {
        "available_bitrate_bps": int(available_bitrate_bps) if available_bitrate_bps is not None else None,
        "packet_loss_fraction": packet_loss_fraction,
        "rtt_ms": float(rtt_seconds) * 1000.0 if rtt_seconds is not None else None,
    }


def _load_opus_module():
    try:
        from pyogg import opus
    except ImportError as exc:
        raise OpusSupportError("PyOgg with Opus support is required") from exc
    required = (
        "opus_encoder_create",
        "opus_encoder_ctl",
        "opus_encode",
        "opus_encoder_destroy",
        "opus_strerror",
    )
    missing = [name for name in required if not hasattr(opus, name)]
    if missing:
        raise OpusSupportError(
            "PyOgg Opus bindings are missing required symbols: " + ", ".join(missing)
        )
    return opus


def _load_aiortc():
    try:
        import aiortc
    except ImportError as exc:
        raise WebRtcError("The 'aiortc' package is required for WebRTC transport") from exc
    return aiortc
