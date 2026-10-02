from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib.resources import as_file, files
from pathlib import Path
import wave

import numpy as np

from .config import EasRecordingConfig, IQ_SAMPLE_RATE
from .encoder import PcmResampler
from .same_live import generate_attention_tone, generate_same_burst


LOG = logging.getLogger(__name__)
EAS_RECORDER_RATE = 22050
SAME_SENDER_ID = "RTLWB000"
SAME_TEST_LOCATION = "999999"
SAME_TEST_MESSAGE_AUDIO = "assets/same_test.wav"


class EasRecordingError(RuntimeError):
    """Raised when EAS recording cannot be initialized or fed."""


@dataclass
class EasRecorderOutput:
    config: EasRecordingConfig
    input_sample_rate: int = IQ_SAMPLE_RATE

    def __post_init__(self) -> None:
        if not self.config.enabled:
            raise EasRecordingError("EAS recording is disabled")
        try:
            from easrecorder import EASRecorder, RecorderSettings
        except ImportError as exc:
            raise EasRecordingError(
                "EAS recording requires the 'easrecorder' Python package"
            ) from exc

        self.resampler = PcmResampler(self.input_sample_rate, EAS_RECORDER_RATE)
        self.settings = RecorderSettings(
            rate=EAS_RECORDER_RATE,
            detect_rate=EAS_RECORDER_RATE,
            outdir=self.config.directory,
            pre_seconds=self.config.pre_seconds,
            post_seconds=self.config.post_seconds,
            max_seconds=self.config.max_seconds,
            save_format=self.config.format,
            local_time=self.config.local_time,
            index_path="index.json",
        )
        self.recorder = EASRecorder(self.settings)
        self.closed = False
        self.recorder.start()
        LOG.info("started EAS recording to %s", self.config.directory)

    def write(self, pcm: bytes) -> None:
        if self.closed:
            return
        audio = self.resampler.process(pcm)
        if audio:
            self.recorder.write(audio)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            tail = self.resampler.flush()
            if tail:
                try:
                    self.recorder.write(tail)
                except Exception as exc:
                    LOG.debug("EAS recorder flush failed during shutdown: %s", exc)
        finally:
            try:
                self.recorder.stop()
            except Exception as exc:
                LOG.debug("EAS recorder stop reported during shutdown: %s", exc)
            LOG.info("stopped EAS recording")


def generate_same_test_audio(
    *,
    sample_rate: int = IQ_SAMPLE_RATE,
    header: str | None = None,
    origin_time: datetime | None = None,
) -> np.ndarray:
    """Generate a short local SAME test alert as mono float audio."""
    same_header = header or _same_test_header(origin_time)
    silence = np.zeros(round(sample_rate * 1.0), dtype=np.float32)
    half_second_silence = np.zeros(round(sample_rate * 0.5), dtype=np.float32)
    two_second_silence = np.zeros(round(sample_rate * 2.0), dtype=np.float32)
    burst = generate_same_burst(same_header, sample_rate, amplitude=0.75)
    eom = generate_same_burst("NNNN", sample_rate, amplitude=0.75)
    attention = generate_attention_tone(sample_rate, seconds=8.0, amplitude=0.55)
    message = _load_same_test_message_audio(sample_rate)
    audio = np.concatenate(
        (
            burst,
            silence,
            burst,
            silence,
            burst,
            silence,
            attention,
            half_second_silence,
            message,
            two_second_silence,
            eom,
            silence,
            eom,
            silence,
            eom,
        )
    )
    return np.clip(audio, -1.0, 1.0).astype(np.float32, copy=False)


def _same_test_header(origin_time: datetime | None = None) -> str:
    now = origin_time or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    timestamp = f"{now.timetuple().tm_yday:03d}{now.hour:02d}{now.minute:02d}"
    return f"ZCZC-WXR-DMO-{SAME_TEST_LOCATION}+0015-{timestamp}-{SAME_SENDER_ID}-"


@lru_cache(maxsize=4)
def _load_same_test_message_audio(sample_rate: int) -> np.ndarray:
    try:
        resource = files(__package__).joinpath(SAME_TEST_MESSAGE_AUDIO)
        with as_file(resource) as path:
            pcm, rate = _read_same_test_message_audio(path)
    except (AttributeError, TypeError):
        path = Path(__file__).resolve().parent / SAME_TEST_MESSAGE_AUDIO
        pcm, rate = _read_same_test_message_audio(path)
    if rate != sample_rate:
        resampler = PcmResampler(rate, sample_rate)
        pcm = resampler.process(pcm) + resampler.flush()
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    return samples.astype(np.float32, copy=False)


def _read_same_test_message_audio(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        rate = wav.getframerate()
        frame_count = wav.getnframes()
        compression = wav.getcomptype()
        if channels != 1 or sample_width != 2 or compression != "NONE":
            raise EasRecordingError(
                "packaged SAME test audio must be mono PCM S16_LE WAV"
            )
        return wav.readframes(frame_count), rate
