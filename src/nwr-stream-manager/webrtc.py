from __future__ import annotations

import ctypes
import ctypes.util
import asyncio
import importlib.util
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable

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
WEBRTC_MONITOR_PREBUFFER_FRAMES = 15
WEBRTC_MONITOR_TARGET_LATENCY_FRAMES = 15
WEBRTC_MONITOR_LOW_WATER_FRAMES = 6
WEBRTC_MONITOR_MAX_BUFFER_FRAMES = 48


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


class OpusEncoder:
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
    def __init__(
        self,
        *,
        max_frames: int = WEBRTC_MONITOR_MAX_BUFFER_FRAMES,
        prebuffer_frames: int = WEBRTC_MONITOR_PREBUFFER_FRAMES,
        target_latency_frames: int = WEBRTC_MONITOR_TARGET_LATENCY_FRAMES,
        low_water_frames: int = WEBRTC_MONITOR_LOW_WATER_FRAMES,
    ) -> None:
        self.max_frames = max(1, int(max_frames))
        self.prebuffer_frames = max(0, min(int(prebuffer_frames), self.max_frames))
        self.target_latency_frames = max(1, min(int(target_latency_frames), self.max_frames))
        self.low_water_frames = max(0, min(int(low_water_frames), self.max_frames))
        self.frame_bytes = round(IQ_SAMPLE_RATE * WEBRTC_FRAME_SECONDS) * 2
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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._event: asyncio.Event | None = None
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
        self._notify_loop()

    async def read_pcm(self, timeout: float = 0.25) -> bytes:
        self._bind_loop(asyncio.get_running_loop())
        if not self._prebuffered and self.prebuffer_frames:
            await self._wait_for_buffer(self.prebuffer_frames, timeout)
            self._prebuffered = True
        elif self.low_water_frames:
            with self.lock:
                buffered = len(self.buffer)
            if 0 < buffered < self.low_water_frames:
                await self._wait_for_buffer(self.low_water_frames, WEBRTC_FRAME_SECONDS)
        self._drop_stale_frames()
        frame = await self._pop_frame(timeout)
        if frame is None:
            self.underrun_frames += 1
            return b"\x00" * self.frame_bytes
        self.read_frames += 1
        return frame

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
        return b"\x00" * self.frame_bytes

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
        self._notify_loop()

    def _split_frames(self, pcm_s16le: bytes) -> list[bytes]:
        frames = []
        for offset in range(0, len(pcm_s16le), self.frame_bytes):
            frame = pcm_s16le[offset : offset + self.frame_bytes]
            if len(frame) < self.frame_bytes:
                frame += b"\x00" * (self.frame_bytes - len(frame))
            if frame:
                frames.append(frame)
        return frames

    def _drop_stale_frames(self) -> None:
        with self.lock:
            while len(self.buffer) > self.target_latency_frames:
                self.buffer.popleft()
                self.stale_frames += 1

    def _bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is loop and self._event is not None:
            return
        self._loop = loop
        self._event = asyncio.Event()

    async def _wait_for_buffer(self, frame_count: int, timeout: float) -> None:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self.closed.is_set():
            with self.lock:
                if len(self.buffer) >= frame_count:
                    return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await self._wait_for_push(min(remaining, WEBRTC_FRAME_SECONDS))

    async def _pop_frame(self, timeout: float) -> bytes | None:
        deadline = time.monotonic() + max(0.0, timeout)
        while not self.closed.is_set():
            with self.lock:
                if self.buffer:
                    return self.buffer.popleft()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            await self._wait_for_push(min(remaining, WEBRTC_FRAME_SECONDS))
        return None

    async def _wait_for_push(self, timeout: float) -> None:
        event = self._event
        if event is None:
            await asyncio.sleep(timeout)
            return
        event.clear()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass

    def _notify_loop(self) -> None:
        if self._loop is None or self._event is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._event.set)
        except RuntimeError:
            pass


def create_webrtc_pcm_audio_track(source: WebRtcAudioSource):
    aiortc = _load_aiortc()
    av = _load_av()

    class WebRtcPcmAudioTrack(aiortc.MediaStreamTrack):
        kind = "audio"

        def __init__(self, audio_source: WebRtcAudioSource) -> None:
            super().__init__()
            self._source = audio_source
            self._resampler = PcmResampler(IQ_SAMPLE_RATE, WEBRTC_OPUS_SAMPLE_RATE)
            self._pending = bytearray()
            self._pts = 0
            self._started_at: float | None = None

        async def recv(self):
            needed_bytes = WEBRTC_OPUS_FRAME_SAMPLES * WEBRTC_OPUS_CHANNELS * 2
            if self._started_at is None:
                self._started_at = time.monotonic()
            attempts = 0
            deadline = time.monotonic() + WEBRTC_FRAME_SECONDS
            while len(self._pending) < needed_bytes and attempts < 4:
                timeout = max(0.0, deadline - time.monotonic())
                self._pending.extend(self._resampler.process(await _read_source_pcm(self._source, timeout)))
                attempts += 1
                if time.monotonic() >= deadline:
                    break
            if len(self._pending) < needed_bytes:
                pcm = bytes(self._pending) + b"\x00" * (needed_bytes - len(self._pending))
                self._pending.clear()
            else:
                pcm = bytes(self._pending[:needed_bytes])
                del self._pending[:needed_bytes]
            frame = av.AudioFrame(
                format="s16",
                layout="mono",
                samples=WEBRTC_OPUS_FRAME_SAMPLES,
            )
            frame.planes[0].update(pcm)
            frame.sample_rate = WEBRTC_OPUS_SAMPLE_RATE
            frame.pts = self._pts
            frame.time_base = Fraction(1, WEBRTC_OPUS_SAMPLE_RATE)
            self._pts += WEBRTC_OPUS_FRAME_SAMPLES
            target_time = self._started_at + self._pts / WEBRTC_OPUS_SAMPLE_RATE
            delay = target_time - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            return frame

    return WebRtcPcmAudioTrack(source)


async def _read_source_pcm(source: WebRtcAudioSource, timeout: float) -> bytes:
    try:
        return await source.read_pcm(timeout=timeout)
    except TypeError:
        return await source.read_pcm()


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
        on_peer_closed: Callable[[str, str], None] | None = None,
    ) -> dict[str, str]:
        aiortc = _load_aiortc()
        configuration = aiortc.RTCConfiguration(iceServers=[])
        peer = aiortc.RTCPeerConnection(configuration=configuration)
        stale_close_task: asyncio.Task[Any] | None = None
        stale_close_delay: float | None = None

        def peer_is_unhealthy() -> bool:
            return getattr(peer, "connectionState", "") in {"failed", "closed", "disconnected"} or getattr(
                peer,
                "iceConnectionState",
                "",
            ) in {"failed", "closed", "disconnected"}

        def cancel_stale_close() -> None:
            nonlocal stale_close_task, stale_close_delay
            if stale_close_task is not None and not stale_close_task.done():
                stale_close_task.cancel()
            stale_close_task = None
            stale_close_delay = None

        async def close_if_current(reason: str, delay: float = 0.0) -> None:
            if delay > 0:
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    return
                if not peer_is_unhealthy():
                    return
            with self.lock:
                if self.sessions.get(session_id) is not peer:
                    return
                self.sessions.pop(session_id, None)
            if on_peer_closed is not None:
                on_peer_closed(session_id, reason)
            await peer.close()

        def schedule_stale_close(reason: str, delay: float) -> None:
            nonlocal stale_close_task, stale_close_delay
            if (
                stale_close_task is not None
                and not stale_close_task.done()
                and stale_close_delay == 0.0
                and delay > 0
            ):
                return
            cancel_stale_close()
            stale_close_delay = delay
            stale_close_task = asyncio.create_task(close_if_current(reason, delay))

        @peer.on("connectionstatechange")
        async def on_connectionstatechange() -> None:
            state = getattr(peer, "connectionState", "")
            if state == "connected":
                if not peer_is_unhealthy():
                    cancel_stale_close()
            elif state in {"failed", "closed"}:
                schedule_stale_close(f"connectionState={state}", 0.0)
            elif state == "disconnected":
                schedule_stale_close(f"connectionState={state}", 30.0)

        @peer.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange() -> None:
            state = getattr(peer, "iceConnectionState", "")
            if state in {"connected", "completed"}:
                if not peer_is_unhealthy():
                    cancel_stale_close()
            elif state in {"failed", "closed"}:
                schedule_stale_close(f"iceConnectionState={state}", 0.0)
            elif state == "disconnected":
                schedule_stale_close(f"iceConnectionState={state}", 30.0)

        senders = []
        for track in tracks:
            senders.append(peer.addTrack(track))
        _prefer_opus(peer)
        await peer.setRemoteDescription(aiortc.RTCSessionDescription(sdp=sdp, type=type))
        answer = await peer.createAnswer()
        await peer.setLocalDescription(answer)
        for sender in senders:
            _configure_sender_bitrate(sender, WEBRTC_TARGET_BITRATE_KBPS)
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


class WebRtcAsyncRunner:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._run,
            name="webrtc-async-loop",
            daemon=True,
        )
        self.thread.start()

    def run(self, coroutine, timeout: float = 15.0):
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=2.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()
        pending = asyncio.all_tasks(self.loop)
        for task in pending:
            task.cancel()
        if pending:
            self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self.loop.close()


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


def _load_aiortc():
    try:
        import aiortc
    except ImportError as exc:
        raise WebRtcError("The 'aiortc' package is required for WebRTC transport") from exc
    return aiortc


def _load_av():
    try:
        import av
    except ImportError as exc:
        raise WebRtcError("The 'av' package is required for WebRTC audio frames") from exc
    return av


def _prefer_opus(peer: Any) -> None:
    try:
        from aiortc import RTCRtpSender
    except ImportError:
        return
    try:
        capabilities = RTCRtpSender.getCapabilities("audio")
        opus_codecs = [
            codec for codec in capabilities.codecs
            if str(getattr(codec, "mimeType", "")).lower() == "audio/opus"
        ]
        if not opus_codecs:
            return
        for transceiver in peer.getTransceivers():
            if transceiver.kind == "audio":
                transceiver.setCodecPreferences(opus_codecs)
    except Exception as exc:
        LOG.debug("could not force WebRTC Opus codec preference: %s", exc)


def _configure_sender_bitrate(sender: Any, bitrate_kbps: int) -> None:
    try:
        parameters = sender.getParameters()
        if not parameters.encodings:
            return
        parameters.encodings[0].maxBitrate = int(bitrate_kbps) * 1000
        result = sender.setParameters(parameters)
        if hasattr(result, "__await__"):
            LOG.debug("WebRTC sender bitrate parameter update is async and will be skipped")
    except Exception as exc:
        LOG.debug("could not set WebRTC sender bitrate to %s Kbps: %s", bitrate_kbps, exc)
