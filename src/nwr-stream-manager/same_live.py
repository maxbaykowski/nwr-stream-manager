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


LOG = logging.getLogger(__name__)

SAME_MARK_HZ = 2083.3
SAME_SPACE_HZ = 1562.5
SAME_BAUD = 520.83
SAME_PREAMBLE_BYTE = 0xAB
SAME_PREAMBLE_BYTES = 16
SAME_TRAILING_NUL_BYTES = 3
SAME_REPETITIONS = 3
SAME_INTER_BURST_GAP_SECONDS = 1.0
SAME_DETECT_RATE = 22050
SAME_DETECT_MIN_SECONDS = 0.08
SAME_DETECT_END_SECONDS = 0.18
SAME_CANDIDATE_TIMEOUT_SECONDS = 12.0
SAME_EVENT_DEDUP_SECONDS = 45.0
SAME_PREAMBLE_DETECT_MIN_BITS = 28
SAME_PREAMBLE_DETECT_SECONDS = 0.36
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
    sample_offset: int
    sample_rate: int
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


class SameLiveError(RuntimeError):
    """Raised when live SAME processing fails."""


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
    phase = 0.0
    mark_step = (2.0 * math.pi * SAME_MARK_HZ) / sample_rate
    space_step = (2.0 * math.pi * SAME_SPACE_HZ) / sample_rate
    samples_per_bit = sample_rate / SAME_BAUD
    sample_cursor = 0
    sample_target = 0.0
    output: list[float] = []
    bytes_to_send = [SAME_PREAMBLE_BYTE] * SAME_PREAMBLE_BYTES
    bytes_to_send.extend(ord(ch) & 0x7F for ch in str(payload))
    bytes_to_send.extend([0x00] * SAME_TRAILING_NUL_BYTES)
    for byte in bytes_to_send:
        for bit_index in range(8):
            sample_target += samples_per_bit
            bit_samples = int(round(sample_target)) - sample_cursor
            sample_cursor += bit_samples
            step = mark_step if ((byte >> bit_index) & 1) else space_step
            for _ in range(bit_samples):
                output.append(amplitude * math.sin(phase))
                phase += step
                if phase >= 2.0 * math.pi:
                    phase -= 2.0 * math.pi
    return np.asarray(output, dtype=np.float32)


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
        detect_rate: int = SAME_DETECT_RATE,
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
        except (BrokenPipeError, OSError) as exc:
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


class SameToneDetector:
    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = int(sample_rate)
        self._cache: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self._symbol_cache: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}
        self._preamble_bits = tuple((SAME_PREAMBLE_BYTE >> bit_index) & 1 for bit_index in range(8))

    def is_same_like(self, pcm_s16le: bytes) -> bool:
        if not pcm_s16le:
            return False
        samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32)
        if samples.size < 32:
            return False
        samples -= float(np.mean(samples))
        total = float(np.dot(samples, samples))
        if total <= 1.0:
            return False
        mark_i, mark_q, space_i, space_q = self._basis(samples.size)
        mark = float(np.dot(samples, mark_i) ** 2 + np.dot(samples, mark_q) ** 2)
        space = float(np.dot(samples, space_i) ** 2 + np.dot(samples, space_q) ** 2)
        dominant_ratio = max(mark, space) / total
        weaker_ratio = min(mark, space) / total
        combined_ratio = (mark + space) / total
        if dominant_ratio < 24.0 or weaker_ratio < 4.0 or combined_ratio < 48.0:
            return False
        return True

    def has_same_preamble(self, pcm_s16le: bytes) -> bool:
        if not pcm_s16le:
            return False
        samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32)
        if samples.size < round(self.sample_rate * 0.12):
            return False
        max_samples = round(self.sample_rate * SAME_PREAMBLE_DETECT_SECONDS)
        if samples.size > max_samples:
            samples = samples[-max_samples:]
        samples -= float(np.mean(samples))
        total = float(np.dot(samples, samples))
        if total <= 1.0:
            return False
        mark_i, mark_q, space_i, space_q = self._basis(samples.size)
        mark = float(np.dot(samples, mark_i) ** 2 + np.dot(samples, mark_q) ** 2)
        space = float(np.dot(samples, space_i) ** 2 + np.dot(samples, space_q) ** 2)
        if (mark + space) / total < 0.10:
            return False
        return self._has_same_preamble_pattern(samples)

    def _has_same_preamble_pattern(self, samples: np.ndarray) -> bool:
        symbol_samples = max(8, round(self.sample_rate / SAME_BAUD))
        if samples.size < symbol_samples * SAME_PREAMBLE_DETECT_MIN_BITS:
            return False
        best_run = 0
        offset_step = max(1, symbol_samples // 6)
        for start_offset in range(0, symbol_samples, offset_step):
            symbols: list[int] = []
            confidence: list[bool] = []
            for start in range(start_offset, samples.size - symbol_samples + 1, symbol_samples):
                symbol = samples[start : start + symbol_samples]
                total = float(np.dot(symbol, symbol))
                if total <= 1.0:
                    symbols.append(0)
                    confidence.append(False)
                    continue
                mark_i, mark_q, space_i, space_q = self._symbol_basis(symbol.size)
                mark = float(np.dot(symbol, mark_i) ** 2 + np.dot(symbol, mark_q) ** 2)
                space = float(np.dot(symbol, space_i) ** 2 + np.dot(symbol, space_q) ** 2)
                symbols.append(1 if mark > space else 0)
                confidence.append(
                    max(mark, space) / total >= 0.14
                    and abs(mark - space) / max(mark, space) >= 0.10
                )
            if len(symbols) < SAME_PREAMBLE_DETECT_MIN_BITS:
                continue
            for phase in range(8):
                run = 0
                for index, bit in enumerate(symbols):
                    expected = self._preamble_bits[(index + phase) % 8]
                    if confidence[index] and bit == expected:
                        run += 1
                        best_run = max(best_run, run)
                    else:
                        run = 0
        return best_run >= SAME_PREAMBLE_DETECT_MIN_BITS

    def _symbol_basis(self, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        cached = self._symbol_cache.get(size)
        if cached is not None:
            return cached
        t = np.arange(size, dtype=np.float32) / float(self.sample_rate)
        basis = (
            np.sin(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.sin(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
        )
        self._symbol_cache[size] = basis
        return basis

    def _basis(self, size: int) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        cached = self._cache.get(size)
        if cached is not None:
            return cached
        t = np.arange(size, dtype=np.float32) / float(self.sample_rate)
        basis = (
            np.sin(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_MARK_HZ * t).astype(np.float32),
            np.sin(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
            np.cos(2.0 * np.pi * SAME_SPACE_HZ * t).astype(np.float32),
        )
        self._cache[size] = basis
        return basis


class SameSuppressionProcessor:
    def __init__(
        self,
        *,
        sample_rate: int,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
        lookbehind_seconds: float = 0.22,
        decoder_factory: Callable[[int], Any] | None = None,
        enable_decoder: bool = True,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.event_sink = event_sink
        self.frame_samples = round(self.sample_rate * 0.02)
        self.frame_bytes = self.frame_samples * 2
        self.detector = SameToneDetector(self.sample_rate)
        self.decoder = (decoder_factory or SameMultimonLiveDecoder)(self.sample_rate) if enable_decoder else None
        self.pending: deque[bytes] = deque()
        self.state = "normal"
        self.candidate_validated = False
        self.candidate_start_sample_offset = 0
        self.sample_offset = 0
        self.candidate_started_at = 0.0
        self.candidate_tone_frames = 0
        self.last_preamble_check_tone_frames = 0
        self.quiet_frames = 0
        self.min_tone_frames = max(1, round(SAME_DETECT_MIN_SECONDS / 0.02))
        self.lookbehind_frames = max(self.min_tone_frames, round(float(lookbehind_seconds) / 0.02))
        self.end_quiet_frames = max(1, round(SAME_DETECT_END_SECONDS / 0.02))
        self.last_events: dict[tuple[str, str], float] = {}

    def close(self) -> None:
        close = getattr(self.decoder, "close", None)
        if close is not None:
            close()

    def process_pcm(self, pcm_s16le: bytes) -> list[bytes]:
        output: list[bytes] = []
        for frame in self._split_frames(pcm_s16le):
            output.extend(self._process_frame(frame))
        return output

    def submit_decoded_payload(self, payload: str) -> list[dict[str, Any]]:
        payload = str(payload or "").strip()
        if payload.startswith("ZCZC"):
            parsed = parse_same_header(payload)
            if parsed is None:
                return []
            self._mark_candidate_validated()
            event = SameLiveEvent(
                type="same_header",
                id=same_event_id("header", parsed.raw_header),
                sample_offset=self.candidate_start_sample_offset,
                sample_rate=self.sample_rate,
                payload={
                    "raw_header": parsed.raw_header,
                    "parsed": asdict(parsed),
                    "repetitions": SAME_REPETITIONS,
                    "gap_seconds": SAME_INTER_BURST_GAP_SECONDS,
                },
            )
            return self._emit_once("header", parsed.raw_header, event)
        if payload.startswith("NNNN"):
            self._mark_candidate_validated()
            event = SameLiveEvent(
                type="same_eom",
                id=same_event_id("eom", "NNNN"),
                sample_offset=self.candidate_start_sample_offset,
                sample_rate=self.sample_rate,
                payload={
                    "raw_eom": "NNNN",
                    "repetitions": SAME_REPETITIONS,
                    "gap_seconds": SAME_INTER_BURST_GAP_SECONDS,
                },
            )
            return self._emit_once("eom", "NNNN", event)
        return []

    def flush(self) -> list[bytes]:
        output = list(self.pending)
        self.pending.clear()
        return output

    def _process_frame(self, frame: bytes) -> list[bytes]:
        if self.decoder is not None:
            self.decoder.write(frame)
            for payload in self.decoder.poll():
                self.submit_decoded_payload(payload)
        same_like = self.detector.is_same_like(frame)
        now = time.monotonic()
        output: list[bytes] = []
        if self.state == "suppressing":
            output.append(self._silence_like(frame))
            if same_like:
                self.quiet_frames = 0
            else:
                self.quiet_frames += 1
            timed_out = now - self.candidate_started_at > SAME_CANDIDATE_TIMEOUT_SECONDS
            if self.quiet_frames >= self.end_quiet_frames or timed_out:
                self.state = "normal"
                self.candidate_tone_frames = 0
                self.last_preamble_check_tone_frames = 0
                self.quiet_frames = 0
                self.candidate_validated = False
            self.sample_offset += len(frame) // 2
            return output
        if self.state == "normal":
            self.pending.append(frame)
            if same_like:
                self.candidate_tone_frames += 1
            else:
                self.candidate_tone_frames = 0
                self.last_preamble_check_tone_frames = 0
            if self.candidate_validated:
                self.candidate_start_sample_offset = self.sample_offset
                output.append(self._silence_like(frame))
                self.pending.clear()
                self.state = "suppressing"
                self.candidate_started_at = now
                self.candidate_validated = False
                self.quiet_frames = 0
            elif (
                self.candidate_tone_frames >= self.min_tone_frames
                and self._should_check_preamble()
                and self.detector.has_same_preamble(b"".join(self.pending))
            ):
                self.state = "candidate"
                self.candidate_started_at = now
                self.quiet_frames = 0
                self.last_preamble_check_tone_frames = 0
                first_pending_sample = self.sample_offset - (
                    max(0, len(self.pending) - 1) * self.frame_samples
                )
                self.candidate_start_sample_offset = max(0, first_pending_sample)
                while self.pending:
                    output.append(self._silence_like(self.pending.popleft()))
            else:
                while len(self.pending) > self.lookbehind_frames:
                    output.append(self.pending.popleft())
        elif self.state == "candidate":
            output.append(self._silence_like(frame))
            if same_like:
                self.quiet_frames = 0
            else:
                self.quiet_frames += 1
            timed_out = now - self.candidate_started_at > SAME_CANDIDATE_TIMEOUT_SECONDS
            if self.candidate_validated:
                self.state = "suppressing"
                self.candidate_validated = False
                self.quiet_frames = 0
            elif self.quiet_frames >= self.end_quiet_frames or timed_out:
                self.state = "normal"
                self.candidate_tone_frames = 0
                self.last_preamble_check_tone_frames = 0
                self.quiet_frames = 0
        self.sample_offset += len(frame) // 2
        return output

    def _mark_candidate_validated(self) -> None:
        self.candidate_validated = True

    def _should_check_preamble(self) -> bool:
        if self.candidate_tone_frames < self.min_tone_frames:
            return False
        if self.candidate_tone_frames - self.last_preamble_check_tone_frames < 4:
            return False
        self.last_preamble_check_tone_frames = self.candidate_tone_frames
        return True

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

    def _split_frames(self, pcm_s16le: bytes) -> list[bytes]:
        frames = []
        for offset in range(0, len(pcm_s16le), self.frame_bytes):
            frame = pcm_s16le[offset : offset + self.frame_bytes]
            if len(frame) < self.frame_bytes:
                frame += b"\x00" * (self.frame_bytes - len(frame))
            frames.append(frame)
        return frames

    @staticmethod
    def _silence_like(frame: bytes) -> bytes:
        return b"\x00" * len(frame)


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
