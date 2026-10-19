"""Custom fallback announcements, uploaded as a file or recorded in the browser.

Whatever arrives is decoded with libsndfile, so only real MP3, OGG and WAV audio is
accepted, then converted to 24 kHz mono 16-bit WAV like the packaged fallback
audio. Before it is kept it is checked with multimon-ng for SAME tones: fallback
audio goes out on every stream that plays it, and SAME tones there would set off
weather radios and EAS equipment listening to those streams.

The stored file's SHA-256 is saved with the fallback audio settings and checked
every time it is loaded, the same protection the packaged sounds have.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
import shutil
import struct
import subprocess
import wave

import numpy as np

from .config import IQ_SAMPLE_RATE
from .encoder import PcmResampler
from .fallback_audio import MAX_FALLBACK_AUDIO_SECONDS
from .same_live import SAME_DECODE_RATE
from .sndfile_native import (
    SF_FORMAT_MPEG,
    SF_FORMAT_MPEG_LAYER_I,
    SF_FORMAT_MPEG_LAYER_II,
    SF_FORMAT_MPEG_LAYER_III,
    SF_FORMAT_OGG,
    SF_FORMAT_OPUS,
    SF_FORMAT_SUBMASK,
    SF_FORMAT_TYPEMASK,
    SF_FORMAT_VORBIS,
    SF_FORMAT_WAV,
    SF_FORMAT_WAVEX,
    SndfileError,
    decode_audio,
)


MAX_CUSTOM_FALLBACK_BYTES = 10 * 1024 * 1024
MAX_CUSTOM_FALLBACK_SECONDS = MAX_FALLBACK_AUDIO_SECONDS
# Encoders pad the end of a file a little; this much past a minute is trimmed
# rather than refused.
CUSTOM_FALLBACK_LENGTH_TOLERANCE_SECONDS = 0.25
SAME_SCAN_TIMEOUT_SECONDS = 60.0
UNSUPPORTED_FILE_MESSAGE = "This isn't an MP3, OGG or WAV file, or it is damaged. Choose an MP3, OGG or WAV file."


class CustomFallbackAudioError(ValueError):
    """The announcement cannot be used; the message says why, for the person who sent it."""


def _supported_format(format_code: int) -> bool:
    major = format_code & SF_FORMAT_TYPEMASK
    sub = format_code & SF_FORMAT_SUBMASK
    if major in (SF_FORMAT_WAV, SF_FORMAT_WAVEX):
        return True
    if major == SF_FORMAT_OGG:
        return sub in (SF_FORMAT_VORBIS, SF_FORMAT_OPUS)
    if major == SF_FORMAT_MPEG:
        return sub in (SF_FORMAT_MPEG_LAYER_I, SF_FORMAT_MPEG_LAYER_II, SF_FORMAT_MPEG_LAYER_III)
    return False


def _cut_short(data: bytes, format_code: int, declared_frames: int, decoded_frames: int) -> bool:
    """Whether the file stops partway, as an interrupted download or copy does.

    The decoders play what they can of such a file without complaint, so each
    format's own record of where it should end is checked.
    """
    major = format_code & SF_FORMAT_TYPEMASK
    if major == SF_FORMAT_MPEG:
        # The LAME or Xing header counts the frames; without one, libsndfile
        # estimates from the file size and a cut-short file looks complete.
        return declared_frames > 0 and decoded_frames < declared_frames * 0.9
    if major == SF_FORMAT_OGG:
        # The last page of a complete stream is marked end of stream.
        last_page = data.rfind(b"OggS")
        return last_page < 0 or last_page + 5 >= len(data) or not data[last_page + 5] & 0x04
    if major in (SF_FORMAT_WAV, SF_FORMAT_WAVEX):
        return _wave_data_cut_short(data)
    return False


def _wave_data_cut_short(data: bytes) -> bool:
    position = 12
    while position + 8 <= len(data):
        chunk_id, size = struct.unpack_from("<4sI", data, position)
        if chunk_id == b"data":
            # Some recorders leave the size unset while recording.
            if size in (0, 0xFFFFFFFF):
                return False
            return position + 8 + size > len(data)
        position += 8 + size + (size & 1)
    return False


def prepare_custom_fallback_audio(data: bytes, sample_rate: int = IQ_SAMPLE_RATE) -> bytes:
    """Check an announcement and return it as the WAV file to store."""
    if not data:
        raise CustomFallbackAudioError("The file is empty. Choose an MP3, OGG or WAV file.")
    if len(data) >= MAX_CUSTOM_FALLBACK_BYTES:
        raise CustomFallbackAudioError("The file is 10 MB or larger. Choose a file under 10 MB.")
    limit_seconds = MAX_CUSTOM_FALLBACK_SECONDS + CUSTOM_FALLBACK_LENGTH_TOLERANCE_SECONDS
    try:
        decoded = decode_audio(data, limit_seconds)
    except OSError as exc:
        raise CustomFallbackAudioError(f"Audio files can't be read on this server: {exc}") from exc
    except SndfileError as exc:
        raise CustomFallbackAudioError(UNSUPPORTED_FILE_MESSAGE) from exc
    samples, source_rate = decoded.samples, decoded.sample_rate
    if not _supported_format(decoded.format_code):
        raise CustomFallbackAudioError(UNSUPPORTED_FILE_MESSAGE)
    if len(samples) > limit_seconds * source_rate:
        raise CustomFallbackAudioError("The announcement is longer than one minute. Choose one that is one minute or shorter.")
    if _cut_short(data, decoded.format_code, decoded.declared_frames, len(samples)):
        raise CustomFallbackAudioError(
            "The file is incomplete: it ends before its audio does, as if it was not fully downloaded or copied. "
            "Choose a complete MP3, OGG or WAV file."
        )
    mono = np.nan_to_num(samples.mean(axis=1), nan=0.0, posinf=0.0, neginf=0.0)
    pcm = (np.clip(mono, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes()
    resampler = PcmResampler(source_rate, sample_rate)
    pcm = resampler.process(pcm) + resampler.flush()
    pcm = pcm[: int(MAX_CUSTOM_FALLBACK_SECONDS * sample_rate) * 2]
    if not pcm or not np.any(np.frombuffer(pcm, dtype="<i2")):
        raise CustomFallbackAudioError("The announcement is silent. Choose one with something to hear.")
    if find_same_tones(pcm, sample_rate):
        raise CustomFallbackAudioError(
            "The announcement contains SAME tones, the data bursts that set off weather radios and "
            "emergency alert equipment, so it can't be used. Remove them and try again."
        )
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def find_same_tones(pcm_s16le: bytes, sample_rate: int) -> list[str]:
    """What multimon-ng decodes as SAME in this audio, even a single partial burst."""
    if shutil.which("multimon-ng") is None:
        raise CustomFallbackAudioError("The announcement can't be checked for SAME tones because multimon-ng is not installed.")
    resampler = PcmResampler(sample_rate, SAME_DECODE_RATE)
    padding = b"\x00\x00" * SAME_DECODE_RATE  # lets a burst at either end finish decoding
    audio = padding + resampler.process(pcm_s16le) + resampler.flush() + padding
    try:
        # -v 1 also reports headers heard only once ("EAS (part):"), which a
        # quiet run leaves out.
        result = subprocess.run(
            ["multimon-ng", "-v", "1", "-t", "raw", "-a", "EAS", "-"],
            input=audio,
            capture_output=True,
            timeout=SAME_SCAN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CustomFallbackAudioError(f"The announcement couldn't be checked for SAME tones: {exc}") from exc
    if result.returncode != 0:
        raise CustomFallbackAudioError(
            f"The announcement couldn't be checked for SAME tones: multimon-ng exited with status {result.returncode}"
        )
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    return [line.strip() for line in lines if line.startswith("EAS:") or line.startswith("EAS (part):")]


def custom_fallback_audio_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_custom_fallback_audio(path: Path, expected_sha256: str) -> bytes:
    """The stored announcement's WAV file, only if it is exactly the one that was checked.

    The file is read once and the same bytes are checked and returned.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError as exc:
        raise CustomFallbackAudioError("The custom announcement file is missing.") from exc
    except OSError as exc:
        raise CustomFallbackAudioError(f"The custom announcement couldn't be read: {exc}") from exc
    if not expected_sha256 or custom_fallback_audio_sha256(data) != expected_sha256:
        raise CustomFallbackAudioError(
            "The custom announcement has been changed or replaced outside NWR Stream Manager, so it will not be played."
        )
    return data
