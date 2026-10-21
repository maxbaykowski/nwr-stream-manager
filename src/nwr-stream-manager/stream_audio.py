"""Audio frames for streams, and the fallback audio that fills them when the station is gone."""

from __future__ import annotations

import logging
from dataclasses import replace

import numpy as np

from .config import IQ_SAMPLE_RATE
from .constants import STREAM_FRAME_SECONDS
from .encoder import PcmResampler
from .fallback_audio import FallbackAudio, load_fallback_audio
from .models import WebFallbackPlaybackState


LOG = logging.getLogger(__name__)


class FloatFrameBuffer:
    def __init__(self, frame_samples: int) -> None:
        self.frame_samples = frame_samples
        self.pending = np.empty(0, dtype=np.float32)
        self.offset = 0

    def clear(self) -> None:
        self.pending = np.empty(0, dtype=np.float32)
        self.offset = 0

    def push(self, samples: np.ndarray):
        samples = samples.astype(np.float32, copy=False)
        if self.offset:
            self.pending = self.pending[self.offset :]
            self.offset = 0
        self.pending = samples if len(self.pending) == 0 else np.concatenate((self.pending, samples))
        while len(self.pending) - self.offset >= self.frame_samples:
            frame = self.pending[self.offset : self.offset + self.frame_samples]
            self.offset += self.frame_samples
            yield frame
        if self.offset and self.offset >= len(self.pending):
            self.clear()


class ComplexFrameBuffer:
    def __init__(self, frame_samples: int) -> None:
        self.frame_samples = frame_samples
        self.pending = np.empty(0, dtype=np.complex64)
        self.offset = 0

    def clear(self) -> None:
        self.pending = np.empty(0, dtype=np.complex64)
        self.offset = 0

    def push(self, samples: np.ndarray):
        samples = samples.astype(np.complex64, copy=False)
        if self.offset:
            self.pending = self.pending[self.offset :]
            self.offset = 0
        self.pending = samples if len(self.pending) == 0 else np.concatenate((self.pending, samples))
        while len(self.pending) - self.offset >= self.frame_samples:
            frame = self.pending[self.offset : self.offset + self.frame_samples]
            self.offset += self.frame_samples
            yield frame
        if self.offset and self.offset >= len(self.pending):
            self.clear()


def load_web_fallback_audio(sample_rate: int = IQ_SAMPLE_RATE):
    try:
        audio = load_fallback_audio(None)
    except ValueError as exc:
        # Listeners hear silence rather than a sound that is not the one shipped,
        # and the stream keeps running.
        LOG.error("fallback audio will be silent: %s", exc)
        return FallbackAudio(sample_rate=sample_rate, pcm=b"", duration_seconds=0.0, source="silence")
    if audio.sample_rate == sample_rate:
        return audio
    resampler = PcmResampler(audio.sample_rate, sample_rate)
    pcm = resampler.process(audio.pcm) + resampler.flush()
    return replace(
        audio,
        sample_rate=sample_rate,
        pcm=pcm,
        duration_seconds=len(pcm) / 2 / sample_rate,
    )


def next_web_fallback_frame(audio, state: WebFallbackPlaybackState, loop_delay_seconds: float) -> bytes:
    frame_bytes = round(audio.sample_rate * STREAM_FRAME_SECONDS) * 2
    if not audio.pcm:
        return b"\x00" * frame_bytes
    output = bytearray()
    delay_samples = round(max(0.0, loop_delay_seconds) * audio.sample_rate)
    while len(output) < frame_bytes:
        if state.delay_samples_remaining > 0:
            remaining_samples = (frame_bytes - len(output)) // 2
            silence_samples = min(remaining_samples, state.delay_samples_remaining)
            output.extend(b"\x00\x00" * silence_samples)
            state.delay_samples_remaining -= silence_samples
            continue
        if state.position >= len(audio.pcm):
            state.position = 0
            if delay_samples > 0:
                state.delay_samples_remaining = delay_samples
                continue
        chunk_size = min(frame_bytes - len(output), len(audio.pcm) - state.position)
        output.extend(audio.pcm[state.position : state.position + chunk_size])
        state.position += chunk_size
    return bytes(output)


def pcm_s16le_to_float32(pcm: bytes) -> np.ndarray:
    if not pcm:
        return np.empty(0, dtype=np.float32)
    return (np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0).astype(np.float32, copy=False)
