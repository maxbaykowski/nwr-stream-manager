"""Test mode's simulated weather radio station: its announcements, SAME tests and signal."""

from __future__ import annotations

import functools
import io
import math
import threading
import wave
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .config import IQ_SAMPLE_RATE
from .constants import (
    STREAM_TEST_MODE_AUDIO,
    STREAM_TEST_MODE_AUDIO_DRIVE,
    STREAM_TEST_MODE_AUDIO_PEAK_LIMIT,
    STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS,
    STREAM_TEST_MODE_DEVIATION_HZ,
    STREAM_TEST_MODE_LOOP_PERIOD_SECONDS,
    STREAM_TEST_MODE_MAX_SIGNAL_DBFS,
    STREAM_TEST_MODE_MIN_SIGNAL_DBFS,
    STREAM_TEST_MODE_NOISE_DBFS,
    STREAM_TEST_MODE_PRE_EOM_SILENCE_SECONDS,
    STREAM_TEST_MODE_PROGRAM_PEAK,
    STREAM_TEST_MODE_SAME_AUDIO,
    STREAM_TEST_MODE_SAME_LOCATION,
    STREAM_TEST_MODE_SAME_SENDER_ID,
    STREAM_TEST_MODE_SIGNAL_FADE_SECONDS,
    STREAM_TEST_MODE_TONE_MAX_FREQUENCY_HZ,
    STREAM_TEST_MODE_TONE_PEAK,
    STREAM_TEST_MODE_TRANSMIT_EQ_POINTS,
)
from .encoder import PcmResampler
from .nfm import NfmModulator
from .packaged_audio import read_packaged_audio
from .same_live import generate_attention_tone, generate_same_message
from .tone_generators import parse_tone_generator_settings, ToneGeneratorBank


def clamp_test_mode_signal_dbfs(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS
    return float(min(STREAM_TEST_MODE_MAX_SIGNAL_DBFS, max(STREAM_TEST_MODE_MIN_SIGNAL_DBFS, number)))


def dbfs_to_linear(dbfs: float | np.ndarray) -> float | np.ndarray:
    values = np.asarray(dbfs, dtype=np.float32)
    linear = np.power(10.0, values / 20.0)
    if linear.shape == ():
        return float(linear)
    return linear.astype(np.float32, copy=False)


def asset_path(name: str) -> Path:
    return Path(__file__).resolve().parent / "assets" / name


def load_packaged_wav_float(name: str, sample_rate: int) -> np.ndarray:
    # Only the sound shipped with NWR Stream Manager is played; see packaged_audio.
    with wave.open(io.BytesIO(read_packaged_audio(name)), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frame_count = wav.getnframes()
        compression = wav.getcomptype()
        if sample_width != 2 or compression != "NONE":
            raise ValueError(f"{name} must be PCM S16_LE WAV")
        raw = wav.readframes(frame_count)
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32, copy=False)
    if rate != sample_rate:
        pcm = np.clip(samples * 32768.0, -32768, 32767).astype("<i2").tobytes()
        resampler = PcmResampler(rate, sample_rate)
        converted = resampler.process(pcm) + resampler.flush()
        samples = np.frombuffer(converted, dtype="<i2").astype(np.float32) / 32768.0
    return np.clip(samples, -1.0, 1.0).astype(np.float32, copy=False)


def shape_test_mode_program_audio(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        return audio
    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    nfft = 1
    target = int(audio.size * 2)
    while nfft < target:
        nfft *= 2
    frequencies = np.fft.rfftfreq(nfft, d=1.0 / float(sample_rate))
    point_frequencies = np.array([point[0] for point in STREAM_TEST_MODE_TRANSMIT_EQ_POINTS], dtype=np.float64)
    point_gains = np.array([10.0 ** (point[1] / 20.0) for point in STREAM_TEST_MODE_TRANSMIT_EQ_POINTS], dtype=np.float64)
    response = np.interp(frequencies, point_frequencies, point_gains)
    spectrum = np.fft.rfft(audio.astype(np.float64), n=nfft)
    shaped = np.fft.irfft(spectrum * response, n=nfft)[: audio.size]
    shaped -= float(np.mean(shaped))
    return condition_test_mode_audio(shaped)


def condition_test_mode_audio(samples: np.ndarray, *, drive: float = STREAM_TEST_MODE_AUDIO_DRIVE) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak <= 1e-9:
        return audio.astype(np.float32, copy=False)
    driven = audio * max(0.0, float(drive))
    return (STREAM_TEST_MODE_AUDIO_PEAK_LIMIT * np.tanh(driven / STREAM_TEST_MODE_AUDIO_PEAK_LIMIT)).astype(
        np.float32,
        copy=False,
    )


def condition_test_mode_tone_audio(samples: np.ndarray) -> np.ndarray:
    audio = np.asarray(samples, dtype=np.float32)
    if audio.size == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak <= 1e-9:
        return audio.astype(np.float32, copy=False)
    gain = STREAM_TEST_MODE_TONE_PEAK / peak
    return np.clip(audio * gain, -STREAM_TEST_MODE_TONE_PEAK, STREAM_TEST_MODE_TONE_PEAK).astype(
        np.float32,
        copy=False,
    )


def mix_test_mode_iq_crossfade(
    real_iq: np.ndarray,
    test_iq: np.ndarray,
    real_weight: np.ndarray | float,
    test_weight: np.ndarray | float,
) -> np.ndarray:
    real = np.asarray(real_iq, dtype=np.complex64)
    test = np.asarray(test_iq, dtype=np.complex64)
    mixed = (real * real_weight + test * test_weight).astype(np.complex64, copy=False)
    if mixed.size == 0:
        return mixed
    real_power = float(np.mean(np.abs(real.astype(np.complex128, copy=False)) ** 2))
    test_power = float(np.mean(np.abs(test.astype(np.complex128, copy=False)) ** 2))
    mixed_power = float(np.mean(np.abs(mixed.astype(np.complex128, copy=False)) ** 2))
    real_weight_power = float(np.mean(np.asarray(real_weight, dtype=np.float32) ** 2))
    test_weight_power = float(np.mean(np.asarray(test_weight, dtype=np.float32) ** 2))
    target_power = real_power * real_weight_power + test_power * test_weight_power
    if target_power <= 1e-18 or mixed_power <= target_power:
        return mixed
    mixed *= math.sqrt(target_power / mixed_power)
    return mixed.astype(np.complex64, copy=False)


def stream_test_mode_same_header(origin_time: datetime | None = None) -> str:
    now = origin_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    timestamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
    return f"ZCZC-WXR-DMO-{STREAM_TEST_MODE_SAME_LOCATION}+0015-{timestamp}-{STREAM_TEST_MODE_SAME_SENDER_ID}-"


def _read_only(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float32)
    audio.setflags(write=False)
    return audio


# The parts of a SAME test that never change are built once and shared by every test
# mode stream; only the header, which carries the time, is built for each test.
@functools.lru_cache(maxsize=4)
def _test_mode_same_message_audio(sample_rate: int) -> np.ndarray:
    message = load_packaged_wav_float(STREAM_TEST_MODE_SAME_AUDIO, sample_rate)
    return _read_only(shape_test_mode_program_audio(message, sample_rate))


@functools.lru_cache(maxsize=4)
def _test_mode_attention_audio(sample_rate: int) -> np.ndarray:
    return _read_only(condition_test_mode_tone_audio(generate_attention_tone(sample_rate, seconds=8.0)))


@functools.lru_cache(maxsize=4)
def _test_mode_eom_audio(sample_rate: int) -> np.ndarray:
    return _read_only(condition_test_mode_tone_audio(generate_same_message("NNNN", sample_rate)))


class SyntheticNwrTestModeSource:
    def __init__(self, *, sample_rate: int = IQ_SAMPLE_RATE) -> None:
        self.sample_rate = int(sample_rate)
        self.signal_dbfs = STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS
        self.current_signal_dbfs = STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS
        self.modulator = NfmModulator(self.sample_rate, STREAM_TEST_MODE_DEVIATION_HZ)
        self.rng = np.random.default_rng()
        self.loop_audio = self._build_loop_audio()
        self.loop_position = 0
        self.alert_segments: deque[tuple[str, np.ndarray, bool]] = deque()
        self.alert_segment: tuple[str, np.ndarray, bool] | None = None
        self.alert_position = 0
        self.alert_active = False
        self.stop_requested = False
        self.stop_ready = False
        self.tones = ToneGeneratorBank(
            sample_rate=self.sample_rate,
            max_frequency_hz=STREAM_TEST_MODE_TONE_MAX_FREQUENCY_HZ,
        )
        self.lock = threading.Lock()

    def update_tones(self, raw: Any) -> None:
        with self.lock:
            settings = parse_tone_generator_settings(
                raw,
                self.tones.settings,
                max_frequency_hz=STREAM_TEST_MODE_TONE_MAX_FREQUENCY_HZ,
            )
            self.tones.update(settings)

    def set_signal_dbfs(self, value: Any, *, immediate: bool = False) -> None:
        with self.lock:
            signal_dbfs = clamp_test_mode_signal_dbfs(value)
            self.signal_dbfs = signal_dbfs
            if immediate:
                self.current_signal_dbfs = signal_dbfs

    def request_stop(self) -> dict[str, Any]:
        with self.lock:
            self.stop_requested = True
            if not self.alert_active:
                self.stop_ready = True
            return self.snapshot_locked()

    def queue_same_test(self) -> bool:
        with self.lock:
            if self.alert_active or self.stop_requested:
                return False
        # Built without holding the lock, so the audio thread keeps playing meanwhile.
        segments = deque(self._build_same_alert_segments())
        with self.lock:
            if self.alert_active or self.stop_requested:
                return False
            self.alert_segments = segments
            self.alert_segment = None
            self.alert_position = 0
            self.alert_active = True
            self.loop_position = 0
            return True

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict[str, Any]:
        return {
            "signal_dbfs": self.signal_dbfs,
            "current_signal_dbfs": self.current_signal_dbfs,
            "same_active": self.alert_active,
            "stop_requested": self.stop_requested,
            "stop_ready": self.stop_ready,
            "tones": self.tones.settings.as_dict(),
        }

    def process(self, samples: int) -> np.ndarray:
        count = max(0, int(samples))
        if count <= 0:
            return np.array([], dtype=np.complex64)
        with self.lock:
            audio = self._next_audio_locked(count)
            tones, tone_levels = self.tones.process(count)
            signal_start_dbfs, signal_end_dbfs = self._signal_ramp_locked(count)
        # Tone generators play on top of the voice and SAME audio. When together they would
        # exceed full deviation, turn the whole mix down instead of over-deviating.
        headroom = 1.0 / np.maximum(1.0, STREAM_TEST_MODE_PROGRAM_PEAK + tone_levels)
        audio = ((audio + tones) * headroom).astype(np.float32)
        return self._fm_modulate(audio, signal_start_dbfs, signal_end_dbfs)

    def _build_loop_audio(self) -> np.ndarray:
        audio = load_packaged_wav_float(STREAM_TEST_MODE_AUDIO, self.sample_rate)
        audio = shape_test_mode_program_audio(audio, self.sample_rate)
        period_samples = max(round(STREAM_TEST_MODE_LOOP_PERIOD_SECONDS * self.sample_rate), int(audio.size))
        if audio.size < period_samples:
            audio = np.concatenate((audio, np.zeros(period_samples - audio.size, dtype=np.float32)))
        return audio.astype(np.float32, copy=False)

    def _build_same_alert_segments(self) -> list[tuple[str, np.ndarray, bool]]:
        header = stream_test_mode_same_header()
        header_audio = condition_test_mode_tone_audio(generate_same_message(header, self.sample_rate))
        eom_audio = _test_mode_eom_audio(self.sample_rate)
        attention = _test_mode_attention_audio(self.sample_rate)
        message = _test_mode_same_message_audio(self.sample_rate)
        return [
            ("header", header_audio, True),
            ("header_silence", np.zeros(round(self.sample_rate * 2.0), dtype=np.float32), False),
            ("attention", attention, True),
            ("attention_silence", np.zeros(round(self.sample_rate * 0.5), dtype=np.float32), False),
            ("message", message, False),
            ("pre_eom_silence", np.zeros(round(self.sample_rate * STREAM_TEST_MODE_PRE_EOM_SILENCE_SECONDS), dtype=np.float32), False),
            ("eom", eom_audio, True),
            ("post_eom_silence", np.zeros(round(self.sample_rate * 1.0), dtype=np.float32), False),
        ]

    def _build_eom_segments(self) -> deque[tuple[str, np.ndarray, bool]]:
        eom_audio = _test_mode_eom_audio(self.sample_rate)
        return deque(
            [
                ("pre_eom_silence", np.zeros(round(self.sample_rate * STREAM_TEST_MODE_PRE_EOM_SILENCE_SECONDS), dtype=np.float32), False),
                ("eom", eom_audio, True),
                ("post_eom_silence", np.zeros(round(self.sample_rate * 1.0), dtype=np.float32), False),
            ]
        )

    def _next_audio_locked(self, count: int) -> np.ndarray:
        if self.alert_active:
            return self._next_alert_audio_locked(count)
        return self._loop_audio_locked(count)

    def _can_interrupt_same_segment_for_eom(self, kind: str, protected: bool) -> bool:
        if kind in {"eom", "post_eom_silence", "pre_eom_silence"}:
            return False
        if protected:
            return False
        pending_kinds = {segment_kind for segment_kind, _audio, _protected in self.alert_segments}
        return "attention" not in pending_kinds

    def _next_alert_audio_locked(self, count: int) -> np.ndarray:
        output = np.empty(count, dtype=np.float32)
        written = 0
        while written < count:
            if self.stop_requested and self.alert_segment is not None:
                kind, _audio, protected = self.alert_segment
                if self._can_interrupt_same_segment_for_eom(kind, protected):
                    self.alert_segments = self._build_eom_segments()
                    self.alert_segment = None
                    self.alert_position = 0
            if self.alert_segment is None:
                if not self.alert_segments:
                    self.alert_active = False
                    self.alert_position = 0
                    self.loop_position = 0
                    if self.stop_requested:
                        self.stop_ready = True
                    tail = self._loop_audio_locked(count - written)
                    output[written:] = tail
                    break
                self.alert_segment = self.alert_segments.popleft()
                self.alert_position = 0
            kind, audio, protected = self.alert_segment
            stop = min(self.alert_position + (count - written), int(audio.size))
            take = stop - self.alert_position
            if take > 0:
                output[written : written + take] = audio[self.alert_position:stop]
                written += take
                self.alert_position = stop
            if self.alert_position >= int(audio.size):
                if self.stop_requested and kind not in {"eom", "post_eom_silence"} and not self.alert_segments:
                    self.alert_segments = self._build_eom_segments()
                elif self.stop_requested and self._can_interrupt_same_segment_for_eom(kind, protected):
                    self.alert_segments = self._build_eom_segments()
                self.alert_segment = None
                self.alert_position = 0
        return output

    def _loop_audio_locked(self, count: int) -> np.ndarray:
        if self.loop_audio.size == 0:
            return np.zeros(count, dtype=np.float32)
        output = np.empty(count, dtype=np.float32)
        written = 0
        while written < count:
            available = int(self.loop_audio.size) - self.loop_position
            take = min(count - written, available)
            output[written : written + take] = self.loop_audio[self.loop_position : self.loop_position + take]
            written += take
            self.loop_position = (self.loop_position + take) % int(self.loop_audio.size)
        return output

    def _signal_ramp_locked(self, count: int) -> tuple[float, float]:
        start = float(self.current_signal_dbfs)
        target = float(self.signal_dbfs)
        if count <= 0 or abs(target - start) < 1e-6:
            self.current_signal_dbfs = target
            return start, target
        progress = min(1.0, float(count) / max(1.0, STREAM_TEST_MODE_SIGNAL_FADE_SECONDS * self.sample_rate))
        end = start + (target - start) * progress
        self.current_signal_dbfs = end
        return start, end

    def _fm_modulate(self, audio: np.ndarray, signal_start_dbfs: float, signal_end_dbfs: float) -> np.ndarray:
        carrier = self.modulator.process(audio)
        if abs(signal_end_dbfs - signal_start_dbfs) < 1e-6:
            amplitude = dbfs_to_linear(signal_end_dbfs)
        else:
            amplitude = dbfs_to_linear(np.linspace(signal_start_dbfs, signal_end_dbfs, audio.size, dtype=np.float32))
        iq = amplitude * carrier
        noise_level = dbfs_to_linear(STREAM_TEST_MODE_NOISE_DBFS)
        noise = (
            self.rng.normal(0.0, noise_level, audio.size)
            + 1j * self.rng.normal(0.0, noise_level, audio.size)
        ).astype(np.complex64)
        return (iq + noise).astype(np.complex64, copy=False)
