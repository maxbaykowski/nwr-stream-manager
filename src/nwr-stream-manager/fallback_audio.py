from __future__ import annotations

from dataclasses import dataclass
import io
from pathlib import Path
from typing import BinaryIO
import wave

from .config_errors import ConfigError
from .packaged_audio import read_packaged_audio


MAX_FALLBACK_AUDIO_SECONDS = 60.0
DEFAULT_FALLBACK_AUDIO = "fallback.wav"


@dataclass(frozen=True)
class FallbackAudio:
    sample_rate: int
    pcm: bytes
    duration_seconds: float
    source: str


def load_fallback_audio(path: str | None) -> FallbackAudio:
    if path is None:
        # The packaged sound is checked against the one shipped before it is used.
        return _load_wave(io.BytesIO(read_packaged_audio(DEFAULT_FALLBACK_AUDIO)), "packaged fallback.wav")
    return _load_wave_path(Path(path), path)


def load_fallback_wave_bytes(data: bytes, source: str) -> FallbackAudio:
    return _load_wave(io.BytesIO(data), source)


def _load_wave_path(path: Path, source: str) -> FallbackAudio:
    try:
        if path.is_dir():
            raise IsADirectoryError(21, "Is a directory", str(path))
        if not path.exists():
            raise FileNotFoundError(2, "No such file or directory", str(path))
        if not path.is_file():
            raise OSError(f"{path}: not a regular file")
        with path.open("rb") as fp:
            return _load_wave(fp, source)
    except OSError as exc:
        raise ConfigError(str(exc)) from exc


def _load_wave(fp: BinaryIO, source: str) -> FallbackAudio:
    try:
        with wave.open(fp, "rb") as wav:
            channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            sample_rate = wav.getframerate()
            frame_count = wav.getnframes()
            compression = wav.getcomptype()
            duration = frame_count / sample_rate if sample_rate else 0.0
            if channels != 1:
                raise ConfigError("fallback audio must be mono")
            if sample_width != 2 or compression != "NONE":
                raise ConfigError(
                    "fallback audio must be PCM signed 16-bit little-endian"
                )
            if sample_rate <= 0 or frame_count <= 0:
                raise ConfigError("fallback audio must contain audio samples")
            if duration > MAX_FALLBACK_AUDIO_SECONDS:
                raise ConfigError("fallback audio must be one minute or shorter")
            pcm = wav.readframes(frame_count)
    except wave.Error as exc:
        raise ConfigError(f"fallback audio is not a valid wave file: {exc}") from exc
    except OSError as exc:
        raise ConfigError(str(exc)) from exc
    return FallbackAudio(
        sample_rate=sample_rate,
        pcm=pcm,
        duration_seconds=duration,
        source=source,
    )
