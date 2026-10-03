from __future__ import annotations

import hashlib
import json
import logging
import math
import queue
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from .encoder import PcmResampler
from .liquid_dsp import Oscillator


LOG = logging.getLogger(__name__)

SAME_MARK_HZ = 2083.3
SAME_SPACE_HZ = 1562.5
SAME_ATTENTION_TONE_HZ = 1050.0
SAME_BAUD = 520.83
SAME_PREAMBLE_BYTE = 0xAB
SAME_PREAMBLE_BYTES = 16
SAME_TRAILING_NUL_BYTES = 3
SAME_REPETITIONS = 3
SAME_INTER_BURST_GAP_SECONDS = 1.0
SAME_DECODE_RATE = 22050
SAME_EVENT_DEDUP_SECONDS = 45.0
SAME_HEADER_CONFIRM_TIMEOUT_SECONDS = 30.0
MULTIMON_RESET_AFTER_PAYLOADS = 3
SAME_HEADER_RE = re.compile(
    r"^ZCZC-(?P<originator>[A-Z0-9]{3})-(?P<event_type>[A-Z0-9]{3})-"
    r"(?P<fips_codes>\d{6}(?:-\d{6})*)\+(?P<duration_code>\d{4})-"
    r"(?P<timestamp>\d{7})-(?P<sender_id>[^-]{1,8})-?$"
)
SAME_HEADER_SEARCH_RE = re.compile(
    r"ZCZC-[A-Z0-9]{3}-[A-Z0-9]{3}-\d{6}(?:-\d{6})*\+\d{4}-\d{7}-[^-\s]{1,8}-?"
)
MULTIMON_JSON_EAS = "EAS"


@dataclass(frozen=True)
class SameParsedHeader:
    raw_header: str
    event_type: str
    originator: str
    fips_codes: tuple[str, ...]
    start_time_utc: str
    duration_code: str
    duration_seconds: int
    sender_id: str


@dataclass(frozen=True)
class SameLiveEvent:
    type: str
    id: str
    payload: dict[str, Any]


def generate_same_burst(
    payload: str,
    sample_rate: int,
    *,
    amplitude: float = 0.73,
) -> np.ndarray:
    """Generate one SAME AFSK burst.

    SAME uses 520.83 baud AFSK with a 2083.3 Hz mark, a 1562.5 Hz
    space, sixteen 0xAB preamble bytes, 7-bit ASCII sent LSB first,
    and three trailing NUL bytes at the space frequency.
    These values match the NWS/FCC SAME framing used by NWR receivers.
    """
    sample_rate = int(sample_rate)
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    mark_step = (2.0 * math.pi * SAME_MARK_HZ) / sample_rate
    space_step = (2.0 * math.pi * SAME_SPACE_HZ) / sample_rate
    samples_per_bit = sample_rate / SAME_BAUD
    sample_cursor = 0
    sample_target = 0.0
    bytes_to_send = [SAME_PREAMBLE_BYTE] * SAME_PREAMBLE_BYTES
    bytes_to_send.extend(ord(ch) & 0x7F for ch in str(payload))
    bytes_to_send.extend([0x00] * SAME_TRAILING_NUL_BYTES)
    # One oscillator retuned for each bit keeps the phase continuous between tones,
    # as SAME encoders do.
    oscillator = Oscillator()
    parts: list[np.ndarray] = []
    for byte in bytes_to_send:
        for bit_index in range(8):
            sample_target += samples_per_bit
            bit_samples = int(round(sample_target)) - sample_cursor
            sample_cursor += bit_samples
            oscillator.set_frequency(mark_step if ((byte >> bit_index) & 1) else space_step)
            parts.append(oscillator.generate(bit_samples))
    return (float(amplitude) * np.concatenate(parts).imag).astype(np.float32)


def generate_attention_tone(sample_rate: int, *, seconds: float = 8.0, amplitude: float = 1.0) -> np.ndarray:
    """The 1050 Hz NOAA Weather Radio attention tone."""
    sample_rate = int(sample_rate)
    if sample_rate <= 0:
        raise ValueError("sample rate must be positive")
    oscillator = Oscillator(2.0 * math.pi * SAME_ATTENTION_TONE_HZ / sample_rate)
    samples = oscillator.generate(max(0, round(float(seconds) * sample_rate)))
    return (float(amplitude) * samples.imag).astype(np.float32)


def generate_same_message(
    payload: str,
    sample_rate: int,
    *,
    repetitions: int = SAME_REPETITIONS,
    gap_seconds: float = SAME_INTER_BURST_GAP_SECONDS,
) -> np.ndarray:
    burst = generate_same_burst(payload, sample_rate)
    gap = np.zeros(max(0, round(float(gap_seconds) * int(sample_rate))), dtype=np.float32)
    parts: list[np.ndarray] = []
    for index in range(max(1, int(repetitions))):
        if index:
            parts.append(gap)
        parts.append(burst)
    return np.concatenate(parts) if parts else np.array([], dtype=np.float32)


def parse_same_header(header_line: str, *, now: datetime | None = None) -> SameParsedHeader | None:
    raw_header = str(header_line or "").strip()
    if not raw_header.startswith("ZCZC"):
        return None
    if SAME_HEADER_RE.fullmatch(raw_header) is None:
        return None
    try:
        from easrecorder import parse_same_header as eas_parse_same_header

        parsed = eas_parse_same_header(raw_header, now=now)
        start = parsed.start_time_utc
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        else:
            start = start.astimezone(timezone.utc)
        return SameParsedHeader(
            raw_header=parsed.raw_header,
            event_type=parsed.event_type,
            originator=parsed.originator,
            fips_codes=tuple(parsed.fips_codes),
            start_time_utc=start.isoformat().replace("+00:00", "Z"),
            duration_code=parsed.duration_code,
            duration_seconds=int(parsed.duration_seconds),
            sender_id=parsed.sender_id,
        )
    except Exception:
        match = SAME_HEADER_RE.fullmatch(raw_header)
        assert match is not None
        duration_code = match.group("duration_code")
        duration_seconds = (int(duration_code[:2]) * 60 + int(duration_code[2:])) * 60
        return SameParsedHeader(
            raw_header=raw_header,
            event_type=match.group("event_type"),
            originator=match.group("originator"),
            fips_codes=tuple(match.group("fips_codes").split("-")),
            start_time_utc="",
            duration_code=duration_code,
            duration_seconds=duration_seconds,
            sender_id=match.group("sender_id").strip() or "UNKNOWN",
        )


def same_event_id(kind: str, payload: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{payload}".encode("utf-8", "replace")).hexdigest()
    return digest[:24]


def normalize_multimon_eas_payload(line: str) -> str | None:
    raw_line = str(line or "").strip()
    if not raw_line:
        return None
    if raw_line.startswith("{"):
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            return None
        if payload.get("demod_name") != MULTIMON_JSON_EAS:
            return None
        if str(payload.get("end_of_message", "")).strip() == "NNNN":
            return "NNNN"
        if str(payload.get("header_begin", "")).strip() == "ZCZC":
            message = str(payload.get("last_message", "")).strip()
            candidate = "ZCZC" + message if message.startswith("-") else message
            match = SAME_HEADER_SEARCH_RE.search(candidate)
            if match is not None and parse_same_header(match.group(0)) is not None:
                return match.group(0)
        return None
    if "NNNN" in raw_line:
        return "NNNN"
    match = SAME_HEADER_SEARCH_RE.search(raw_line)
    if match is None:
        return None
    candidate = match.group(0)
    if parse_same_header(candidate) is None:
        return None
    return candidate


class SameMultimonLiveDecoder:
    def __init__(
        self,
        input_sample_rate: int,
        *,
        detect_rate: int = SAME_DECODE_RATE,
        reset_after_payloads: int = MULTIMON_RESET_AFTER_PAYLOADS,
    ) -> None:
        self.input_sample_rate = int(input_sample_rate)
        self.detect_rate = int(detect_rate)
        self.resampler = None if self.input_sample_rate == self.detect_rate else PcmResampler(
            self.input_sample_rate,
            self.detect_rate,
        )
        self.process: subprocess.Popen | None = None
        self.lines: deque[str] = deque()
        self.reader: threading.Thread | None = None
        self.disabled_reason = ""
        self._generation = 0
        self._decoded_payload_count = 0
        self._reset_after_payloads = max(1, int(reset_after_payloads))
        self._start()

    @property
    def available(self) -> bool:
        return self.process is not None

    def _start(self) -> None:
        if shutil.which("multimon-ng") is None:
            self.disabled_reason = "multimon-ng is not installed"
            return
        self._generation += 1
        generation = self._generation
        try:
            self.process = subprocess.Popen(
                ["multimon-ng", "-v", "3", "-t", "raw", "-a", "EAS", "-f", str(self.detect_rate), "-"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError as exc:
            self.disabled_reason = str(exc)
            self.process = None
            return
        self._decoded_payload_count = 0
        LOG.info("started live SAME decoder with multimon-ng at %s Hz", self.detect_rate)
        self.reader = threading.Thread(
            target=self._read_lines,
            args=(self.process, generation),
            name="same-live-decoder",
            daemon=True,
        )
        self.reader.start()

    def write(self, pcm_s16le: bytes) -> None:
        process = self.process
        if process is None or process.stdin is None or not pcm_s16le:
            return
        if self.resampler is not None:
            pcm_s16le = self.resampler.process(pcm_s16le)
            if not pcm_s16le:
                return
        try:
            process.stdin.write(pcm_s16le)
        except (BrokenPipeError, OSError, ValueError) as exc:
            # ValueError: another thread closed the decoder while this write was on its way.
            self.disabled_reason = str(exc)
            self.close()

    def poll(self) -> list[str]:
        payloads = []
        while self.lines:
            line = self.lines.popleft()
            payload = normalize_multimon_eas_payload(line)
            if payload is not None:
                LOG.info("decoded live SAME payload from multimon-ng: %s", payload)
                payloads.append(payload)
                self._decoded_payload_count += 1
        if self._decoded_payload_count >= self._reset_after_payloads:
            self._restart()
        return payloads

    def close(self) -> None:
        process = self.process
        self.process = None
        self._generation += 1
        self.lines.clear()
        self._decoded_payload_count = 0
        self._stop_process(process)

    def _restart(self) -> None:
        process = self.process
        self.process = None
        self._generation += 1
        self.lines.clear()
        self._decoded_payload_count = 0
        self._stop_process(process)
        LOG.info("restarting live SAME decoder after complete SAME burst set")
        self._start()

    @staticmethod
    def _stop_process(process: subprocess.Popen | None) -> None:
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
        except OSError:
            pass

    def _read_lines(self, process: subprocess.Popen, generation: int) -> None:
        if process.stdout is None:
            return
        while True:
            try:
                line = process.stdout.readline()
            except OSError:
                break
            if not line:
                break
            if generation == self._generation and process is self.process:
                self.lines.append(line.decode("utf-8", "ignore").rstrip("\n"))


class SameAlertDecoder:
    """Decode SAME alerts from live audio with multimon-ng, for the weather radio receiver.

    The audio itself passes through untouched. A header counts as an alert once two of
    the last three decoded headers match, within SAME_HEADER_CONFIRM_TIMEOUT_SECONDS,
    and the same alert is not reported again within SAME_EVENT_DEDUP_SECONDS.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        decoder_factory: Callable[[int], Any] | None = None,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.event_sink = event_sink
        self.decoder = (decoder_factory or SameMultimonLiveDecoder)(self.sample_rate)
        self.last_events: dict[tuple[str, str], float] = {}
        self.decoded_header_window: deque[str] = deque(maxlen=3)
        self.last_decoded_header_at: float | None = None

    def close(self) -> None:
        close = getattr(self.decoder, "close", None)
        if close is not None:
            close()

    def process_pcm(self, pcm_s16le: bytes) -> list[dict[str, Any]]:
        self.decoder.write(pcm_s16le)
        events: list[dict[str, Any]] = []
        for payload in self.decoder.poll():
            events.extend(self.submit_decoded_payload(payload))
        return events

    def submit_decoded_payload(self, payload: str) -> list[dict[str, Any]]:
        payload = str(payload or "").strip()
        if payload.startswith("ZCZC"):
            parsed = parse_same_header(payload)
            return [] if parsed is None else self._confirmed_header_events(parsed)
        if payload.startswith("NNNN"):
            # The message is over; a repeat of the same header now is a new alert.
            self.decoded_header_window.clear()
            self.last_decoded_header_at = None
        return []

    def _confirmed_header_events(self, parsed: SameParsedHeader) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (
            self.last_decoded_header_at is not None
            and now - self.last_decoded_header_at > SAME_HEADER_CONFIRM_TIMEOUT_SECONDS
        ):
            self.decoded_header_window.clear()
        self.decoded_header_window.append(parsed.raw_header)
        self.last_decoded_header_at = now
        if sum(1 for header in self.decoded_header_window if header == parsed.raw_header) < 2:
            return []
        event = SameLiveEvent(
            type="same_alert_confirmed",
            id=same_event_id("alert-confirmed", parsed.raw_header),
            payload={
                "raw_header": parsed.raw_header,
                "parsed": asdict(parsed),
            },
        )
        events = self._emit_once("alert-confirmed", parsed.raw_header, event)
        if events:
            self.decoded_header_window.clear()
            self.last_decoded_header_at = None
        return events

    def _emit_once(self, kind: str, key: str, event: SameLiveEvent) -> list[dict[str, Any]]:
        now = time.monotonic()
        dedup_key = (kind, key)
        last = self.last_events.get(dedup_key)
        if last is not None and now - last < SAME_EVENT_DEDUP_SECONDS:
            return []
        self.last_events[dedup_key] = now
        payload = asdict(event)
        LOG.info("emitting live SAME %s event %s", kind, event.id)
        if self.event_sink is not None:
            self.event_sink(payload)
        return [payload]


class SameEventQueue:
    def __init__(self) -> None:
        self.queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=128)

    def put(self, event: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            try:
                self.queue.get_nowait()
                self.queue.put_nowait(event)
            except queue.Empty:
                pass
