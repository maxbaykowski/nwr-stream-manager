"""Station audio sent live to a browser: for monitoring a stream and the Weather Radio Receiver."""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from datetime import timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from typing import Any

from .config import IQ_SAMPLE_RATE
from .constants import (
    LIVE_AUDIO_BINARY_HEADER,
    LIVE_AUDIO_BINARY_MAGIC,
    LIVE_AUDIO_CODEC_OPUS,
    LIVE_AUDIO_CODEC_PCM_S16LE,
    LIVE_AUDIO_UNACKNOWLEDGED_TIMEOUT_SECONDS,
)
from .eas_alerts import format_local_time, format_same_location_for_alert, parse_utc_datetime
from .same_data import lookup_event
from .same_live import SameAlertDecoder, SameEventQueue
from .webrtc import WebRtcAudioSource


def enrich_live_same_event(event: dict[str, Any]) -> dict[str, Any]:
    if event.get("type") not in {"same_header", "same_alert_confirmed"}:
        return event
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return event
    parsed = payload.get("parsed")
    if not isinstance(parsed, dict):
        return event
    same_event = lookup_event(str(parsed.get("event_type", "")))
    fips_codes = parsed.get("fips_codes", [])
    if not isinstance(fips_codes, (list, tuple)):
        fips_codes = []
    areas = [
        format_same_location_for_alert(str(code))
        for code in fips_codes
        if isinstance(code, str) and code.strip()
    ]
    start_time = parse_utc_datetime(str(parsed.get("start_time_utc", "")))
    duration_seconds = int(parsed.get("duration_seconds") or 0)
    expires_at = start_time + timedelta(seconds=max(0, duration_seconds))
    enriched = dict(event)
    enriched_payload = dict(payload)
    enriched_payload["display"] = {
        "event_name": same_event.display_name,
        "areas": areas,
        "expires_at": format_local_time(expires_at),
    }
    enriched["payload"] = enriched_payload
    return enriched


class LiveAudioSource:
    """Audio for one browser listening live, plus SAME alerts when it has an event queue.

    The weather radio receiver passes an event queue so it can show alerts as they are
    decoded; that runs multimon-ng behind the session. Stream monitoring does not, so it
    plays the stream exactly as it is with no decoder.
    """

    def __init__(self, *, sample_rate: int = IQ_SAMPLE_RATE, event_queue: SameEventQueue | None = None) -> None:
        self.audio_source = WebRtcAudioSource(sample_rate=sample_rate)
        self.sample_rate = sample_rate
        self.frame_bytes = self.audio_source.frame_bytes
        self.event_queue = event_queue
        self.paused = False
        self.lock = threading.Lock()
        self.alert_decoder = (
            SameAlertDecoder(
                sample_rate=sample_rate,
                event_sink=lambda event: event_queue.put(enrich_live_same_event(event)),
            )
            if event_queue is not None
            else None
        )

    def set_paused(self, paused: bool) -> None:
        paused = bool(paused)
        with self.lock:
            changed = self.paused != paused
            self.paused = paused
        if changed:
            self.audio_source.clear_buffer()

    def is_paused(self) -> bool:
        with self.lock:
            return self.paused

    def _pause_frame(self, frame: bytes) -> bytes:
        if not self.is_paused():
            return frame
        return bytes(len(frame))

    def push_pcm(self, pcm: bytes) -> None:
        if self.alert_decoder is not None:
            self.alert_decoder.process_pcm(pcm)
        self.audio_source.push_pcm(self._pause_frame(pcm))

    def read_frame(self, timeout: float = 0.25) -> bytes | None:
        frame = self.audio_source.read_frame(timeout=timeout)
        return None if frame is None else self._pause_frame(frame)

    def get_latest_pcm(self, timeout: float = 0.02) -> bytes:
        return self._pause_frame(self.audio_source.get_latest_pcm(timeout=timeout))

    def stats(self) -> dict[str, Any]:
        stats = self.audio_source.stats()
        stats["paused"] = self.is_paused()
        stats["same_alert_decoding"] = self.alert_decoder is not None
        return stats

    def close(self) -> None:
        if self.alert_decoder is not None:
            self.alert_decoder.close()
        self.audio_source.close()


class LiveAudioWebSocketError(RuntimeError):
    pass


class LiveAudioWebSocketWriter:
    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, handler: BaseHTTPRequestHandler) -> None:
        self.handler = handler

    def handshake(self) -> None:
        key = self.handler.headers.get("Sec-WebSocket-Key", "").strip()
        if not key:
            raise LiveAudioWebSocketError("missing WebSocket key")
        self._configure_socket()
        accept = base64.b64encode(hashlib.sha1((key + self.GUID).encode("ascii")).digest()).decode("ascii")
        self.handler.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.handler.send_header("Upgrade", "websocket")
        self.handler.send_header("Connection", "Upgrade")
        self.handler.send_header("Sec-WebSocket-Accept", accept)
        self.handler.end_headers()
        self.handler.close_connection = True

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(json.dumps(payload, separators=(",", ":")).encode("utf-8"), opcode=0x1)

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(payload, opcode=0x2)

    def send_audio(
        self,
        *,
        codec: str,
        sequence: int,
        duration_ms: int,
        sample_rate: int,
        payload: bytes,
    ) -> float:
        codec_id = LIVE_AUDIO_CODEC_OPUS if codec == "opus" else LIVE_AUDIO_CODEC_PCM_S16LE
        header = LIVE_AUDIO_BINARY_HEADER.pack(
            LIVE_AUDIO_BINARY_MAGIC,
            codec_id,
            0,
            max(0, min(0xFFFF, int(duration_ms))),
            int(sequence) & 0xFFFFFFFF,
            max(1, int(sample_rate)) & 0xFFFFFFFF,
        )
        started_at = time.monotonic()
        self.send_binary(header + payload)
        return time.monotonic() - started_at

    def send_close(self, code: int, reason: str = "") -> None:
        self._send_frame(struct.pack("!H", code) + reason.encode("utf-8")[:120], opcode=0x8)

    def _send_frame(self, payload: bytes, *, opcode: int = 0x1) -> None:
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(length)
        elif length <= 0xFFFF:
            header.extend((126, *struct.pack("!H", length)))
        else:
            header.extend((127, *struct.pack("!Q", length)))
        self.handler.wfile.write(bytes(header))
        self.handler.wfile.write(payload)
        self.handler.wfile.flush()

    def _configure_socket(self) -> None:
        connection = getattr(self.handler, "connection", None)
        if connection is None:
            return
        options = [
            (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
            (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        ]
        if hasattr(socket, "TCP_USER_TIMEOUT"):
            options.append((socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, round(LIVE_AUDIO_UNACKNOWLEDGED_TIMEOUT_SECONDS * 1000)))
        for level, option, value in options:
            try:
                connection.setsockopt(level, option, value)
            except OSError:
                pass
