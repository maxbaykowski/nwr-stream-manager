"""The I/Q recorder: saving raw radio data, and the recordings it makes."""

from __future__ import annotations

import json
import logging
import os
import queue
import struct
import tarfile
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .config import IQ_SAMPLE_RATE
from .constants import (
    ALIAS_FILTER_STRENGTH_DEFAULT,
    CHANNEL_IQ_ALIAS_TRANSITION_HZ,
    INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
    INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
    IQ_DOWNLOAD_CHUNK_BYTES,
    IQ_DOWNLOAD_YIELD_SECONDS,
    IQ_RECORDER_MAX_DURATION_SECONDS,
    IQ_RECORDER_MIN_DURATION_SECONDS,
    IQ_RECORDER_MODE_STREAM,
    IQ_RECORDER_QUEUE_CHUNKS,
    IQ_STORAGE_ESTIMATE_CORRECTION_FRACTION,
    IQ_STORAGE_ESTIMATE_MAX_CORRECTION_SECONDS,
    IQ_STORAGE_ESTIMATE_MIN_CORRECTION_SECONDS,
    STORAGE_CRITICAL_FREE_BYTES,
    STORAGE_CRITICAL_FREE_PERCENT,
)
from .dsp import (
    complex64_to_interleaved_f32,
    create_decimator,
    DEFAULT_ALIAS_ATTENUATION_DB,
    IqChannelizer,
    update_decimator_alias_filter,
)
from .eas_alerts import format_local_datetime, sanitize_archive_filename
from .iq_pipeline import (
    alias_filter_attenuation_db,
    alias_filter_transition_hz,
    IntermediateIqFanout,
    iq_batch_complex,
    RawRtlFanout,
    subscribe_raw_fanout,
)
from .rtl import NWR_CENTER_FREQUENCY_HZ, RtlSampleBatch
from .state_files import atomic_write_json
from .storage import storage_status_message, StorageMonitor
from .version import package_version


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class IqRecorderConfig:
    recording_id: str
    mode: str
    sample_rate: int
    duration_seconds: float
    output_path: Path
    index_path: Path
    frequency_hz: int
    stream_id: str = ""
    stream_label: str = ""
    target_frequency_hz: int | None = None
    alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT


class IqRecorderWorker:
    def __init__(
        self,
        *,
        fanout: RawRtlFanout | IntermediateIqFanout,
        config: IqRecorderConfig,
        storage_monitor: StorageMonitor,
        alias_filter_strength_provider,
    ) -> None:
        self.fanout = fanout
        self.config = config
        self.storage_monitor = storage_monitor
        self.alias_filter_strength_provider = alias_filter_strength_provider
        self.queue = subscribe_raw_fanout(
            fanout,
            max_chunks=IQ_RECORDER_QUEUE_CHUNKS,
            name=f"iq-recorder:{config.mode}",
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="iq-recorder", daemon=True)
        self.lock = threading.Lock()
        self.status_name = "recording"
        self.error: str | None = None
        self.started_at = time.time()
        self.stopped_at: float | None = None
        self.bytes_written = 0
        self.samples_written = 0
        self.batch_count = 0
        self.storage_remaining_seconds: float | None = None
        self.storage_remaining_updated_at: float | None = None

    def start(self) -> None:
        self.config.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_monitor.add_path(self.config.output_path.parent)
        self.storage_monitor.recording_started()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.fanout.unsubscribe(self.queue)
        if self.thread.ident is not None:
            self.thread.join(timeout=3.0)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            now = time.time()
            stopped_at = self.stopped_at
            elapsed = max(0.0, (stopped_at or now) - self.started_at)
            return {
                "active": self.status_name == "recording",
                "id": self.config.recording_id,
                "status": self.status_name,
                "mode": self.config.mode,
                "stream_id": self.config.stream_id,
                "stream_label": self.config.stream_label,
                "sample_rate": self.config.sample_rate,
                "frequency_hz": self.config.frequency_hz,
                "duration_seconds": self.config.duration_seconds,
                "elapsed_seconds": elapsed,
                "bytes_written": self.bytes_written,
                "samples_written": self.samples_written,
                "file_name": self.config.output_path.name,
                "started_at": self.started_at,
                "stopped_at": stopped_at,
                "batch_count": self.batch_count,
                "error": self.error,
            }

    def storage_time_remaining(self, storage: dict[str, Any], recordings_directory: Path) -> float | None:
        snapshot = self.snapshot()
        raw_remaining = estimate_iq_storage_remaining_seconds(snapshot, storage, recordings_directory)
        now = time.monotonic()
        with self.lock:
            if self.status_name != "recording":
                self.storage_remaining_seconds = None
                self.storage_remaining_updated_at = None
                return None
            next_remaining = smooth_iq_storage_remaining_seconds(
                previous=self.storage_remaining_seconds,
                elapsed_since_update=0.0 if self.storage_remaining_updated_at is None else now - self.storage_remaining_updated_at,
                raw=raw_remaining,
            )
            self.storage_remaining_seconds = next_remaining
            self.storage_remaining_updated_at = now
            return next_remaining

    def _set_finished(self, status: str, error: str | None = None) -> None:
        with self.lock:
            if self.status_name != "recording":
                return
            self.status_name = status
            self.error = error
            self.stopped_at = time.time()

    def _add_written(self, sample_count: int, byte_count: int) -> None:
        with self.lock:
            self.samples_written += sample_count
            self.bytes_written += byte_count
            self.batch_count += 1

    def _run(self) -> None:
        channelizer: IqChannelizer | None = None
        channelizer_key: tuple[int, int] | None = None
        channelizer_alias_filter_strength: int | None = None
        decimator = None
        decimator_key: tuple[int, int] | None = None
        decimator_alias_filter_strength: int | None = None
        try:
            with self.config.output_path.open("wb") as output:
                while not self.stop_event.is_set():
                    elapsed = time.time() - self.started_at
                    if self.config.duration_seconds > 0 and elapsed >= self.config.duration_seconds:
                        self._set_finished("completed")
                        break
                    if self.storage_monitor.is_critical(self.config.output_path.parent):
                        self._set_finished("needs-attention", storage_status_message("critical"))
                        break
                    try:
                        batch: RtlSampleBatch = self.queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    iq = iq_batch_complex(batch)
                    if iq.size == 0:
                        continue
                    if self.config.mode == IQ_RECORDER_MODE_STREAM:
                        if self.config.target_frequency_hz is None:
                            raise ValueError("stream recording target frequency is missing")
                        alias_filter_strength = self.alias_filter_strength_provider()
                        channel_transition_hz = alias_filter_transition_hz(
                            CHANNEL_IQ_ALIAS_TRANSITION_HZ,
                            alias_filter_strength,
                        )
                        channel_attenuation_db = alias_filter_attenuation_db(
                            DEFAULT_ALIAS_ATTENUATION_DB,
                            alias_filter_strength,
                        )
                        next_key = (
                            batch.sample_rate,
                            batch.center_frequency_hz,
                        )
                        if channelizer is None or channelizer_key != next_key:
                            channelizer = IqChannelizer(
                                input_rate=batch.sample_rate,
                                center_frequency_hz=batch.center_frequency_hz,
                                target_frequency_hz=int(self.config.target_frequency_hz),
                                output_rate=IQ_SAMPLE_RATE,
                                transition_hz=channel_transition_hz,
                                alias_attenuation_db=channel_attenuation_db,
                            )
                            channelizer_key = next_key
                            channelizer_alias_filter_strength = alias_filter_strength
                        elif channelizer_alias_filter_strength != alias_filter_strength:
                            channelizer.update_alias_filter(
                                transition_hz=channel_transition_hz,
                                attenuation_db=channel_attenuation_db,
                            )
                            channelizer_alias_filter_strength = alias_filter_strength
                        if channelizer is not None:
                            channelizer.set_target_frequency(int(self.config.target_frequency_hz))
                        recorded_iq = channelizer.process_complex(iq)
                    else:
                        if self.config.sample_rate > batch.sample_rate:
                            raise ValueError("recording sample rate is higher than the RTL-SDR sample rate")
                        if self.config.sample_rate == batch.sample_rate:
                            recorded_iq = iq
                        else:
                            alias_filter_strength = self.alias_filter_strength_provider()
                            transition_hz = alias_filter_transition_hz(
                                INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
                                alias_filter_strength,
                            )
                            attenuation_db = alias_filter_attenuation_db(
                                INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
                                alias_filter_strength,
                            )
                            next_key = (batch.sample_rate, self.config.sample_rate)
                            if decimator is None or decimator_key != next_key:
                                decimator = create_decimator(
                                    batch.sample_rate,
                                    self.config.sample_rate,
                                    transition_hz=transition_hz,
                                    attenuation_db=attenuation_db,
                                )
                                decimator_key = next_key
                                decimator_alias_filter_strength = alias_filter_strength
                            elif decimator_alias_filter_strength != alias_filter_strength:
                                update_decimator_alias_filter(
                                    decimator,
                                    batch.sample_rate,
                                    self.config.sample_rate,
                                    transition_hz=transition_hz,
                                    attenuation_db=attenuation_db,
                                )
                                decimator_alias_filter_strength = alias_filter_strength
                            recorded_iq = decimator.process(iq)
                    if recorded_iq.size == 0:
                        continue
                    data = complex64_to_interleaved_f32(recorded_iq)
                    output.write(data)
                    self._add_written(int(recorded_iq.size), len(data))
                else:
                    self._set_finished("stopped")
        except Exception as exc:
            LOG.exception("I/Q recorder failed: %s", exc)
            self._set_finished("needs-attention", str(exc))
        finally:
            self.fanout.unsubscribe(self.queue)
            self.storage_monitor.recording_stopped()
            self._write_metadata()

    def _write_metadata(self) -> None:
        snapshot = self.snapshot()
        if snapshot["bytes_written"] <= 0:
            return
        metadata = {
            "id": self.config.recording_id,
            "mode": self.config.mode,
            "stream_id": self.config.stream_id,
            "stream_label": self.config.stream_label,
            "sample_rate": self.config.sample_rate,
            "frequency_hz": self.config.frequency_hz,
            "duration_seconds": snapshot["elapsed_seconds"],
            "bytes": snapshot["bytes_written"],
            "samples": snapshot["samples_written"],
            "file_path": str(self.config.output_path),
            "started_at": snapshot["started_at"],
            "stopped_at": snapshot["stopped_at"] or time.time(),
            "status": snapshot["status"],
        }
        upsert_iq_recording_metadata(self.config.index_path, metadata)


def unique_iq_recording_path(directory: Path, file_name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cleaned = sanitize_archive_filename(file_name)
    if not cleaned.lower().endswith(".cf32"):
        cleaned = f"{Path(cleaned).stem or 'iq-recording'}.cf32"
    candidate = directory / cleaned
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while True:
        candidate = directory / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def load_iq_recording_index(index_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "recordings": []}
    if not isinstance(data, dict) or not isinstance(data.get("recordings"), list):
        raise ValueError("I/Q recording index must contain a recordings array")
    data["recordings"] = [entry for entry in data["recordings"] if isinstance(entry, dict)]
    data["version"] = int(data.get("version", 1))
    return data


def load_iq_recording_entries(index_path: Path) -> list[dict[str, Any]]:
    return list(load_iq_recording_index(index_path)["recordings"])


def upsert_iq_recording_metadata(index_path: Path, metadata: dict[str, Any]) -> None:
    data = load_iq_recording_index(index_path)
    recording_id = str(metadata.get("id", "")).strip()
    if not recording_id:
        raise ValueError("I/Q recording metadata id is required")
    replaced = False
    updated = []
    for entry in data["recordings"]:
        if str(entry.get("id", "")) == recording_id:
            updated.append(metadata)
            replaced = True
        else:
            updated.append(entry)
    if not replaced:
        updated.append(metadata)
    data["recordings"] = updated
    atomic_write_json(index_path, data)


def find_iq_recording(index_path: Path, recording_id: str) -> dict[str, Any]:
    for recording in load_iq_recording_entries(index_path):
        if str(recording.get("id", "")) == recording_id:
            return recording
    raise ValueError("I/Q recording was not found")


def safe_iq_recording_file_path(
    recordings_directory: Path,
    recording: dict[str, Any],
    *,
    require_exists: bool = True,
) -> Path:
    base = recordings_directory.resolve()
    raw_path = recording.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("I/Q recording file is missing")
    path = Path(raw_path).expanduser().resolve()
    if require_exists and not path.is_file():
        raise ValueError("I/Q recording file was not found")
    if path != base and base not in path.parents:
        raise ValueError("I/Q recording file path is outside the recording directory")
    return path


def iq_recording_summary(recording: dict[str, Any]) -> dict[str, Any]:
    started_at = float(recording.get("started_at", 0.0) or 0.0)
    return {
        "id": str(recording.get("id", "")),
        "recorded_at": format_local_datetime(datetime.fromtimestamp(started_at, timezone.utc)),
        "started_at": started_at,
        "duration_seconds": float(recording.get("duration_seconds", 0.0) or 0.0),
        "duration": format_duration_hms(float(recording.get("duration_seconds", 0.0) or 0.0)),
        "sample_rate": int(recording.get("sample_rate", 0) or 0),
        "frequency_hz": int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ),
        "mode": str(recording.get("mode", "")),
        "stream_label": str(recording.get("stream_label", "")),
        "bytes": int(recording.get("bytes", 0) or 0),
        "download_formats": iq_download_formats(
            int(recording.get("bytes", 0) or 0), int(recording.get("sample_rate", 0) or 0)
        ),
    }


def format_duration_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    remaining = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{remaining:02d}"
    return f"{minutes}:{remaining:02d}"


# Each download format, as offered on the download page.
IQ_DOWNLOAD_FORMATS = (
    ("cf", "Float 32 bit raw"),
    ("s16", "Signed 16 bit raw"),
    ("u8", "Unsigned 8-bit raw"),
    ("wav_cf", "Float 32 bit WAV"),
    ("wav_s16", "Signed 16 bit WAV"),
    ("wav_u8", "Unsigned 8-bit WAV"),
    ("sdriq", "Signed 16 bit SDRangel"),
    ("sigmf_cf", "Float 32 bit SigMF"),
    ("sigmf_s16", "Signed 16 bit SigMF"),
    ("sigmf_u8", "Unsigned 8-bit SigMF"),
)
IQ_SAMPLE_BYTES = {"cf": 4, "s16": 2, "u8": 1}
# A WAV file gives its own size in 32 bits, so it can't be any larger than this
# (less the 8 bytes that come before that size).
WAV_MAX_FILE_BYTES = 0xFFFFFFFF + 8
# Unsigned 8-bit WAV is mainly for SDR Touch, which only plays these rates at the right pitch.
WAV_U8_SAMPLE_RATES = (256_000, 512_000, 1_024_000)


def validate_iq_download_format(raw: Any) -> str:
    fmt = str(raw or "cf").strip().lower()
    aliases = {
        "float32": "cf",
        "cf32": "cf",
        "complex_float32": "cf",
        "signed16": "s16",
        "int16": "s16",
        "unsigned8": "u8",
        "uint8": "u8",
    }
    fmt = aliases.get(fmt, fmt)
    if fmt not in dict(IQ_DOWNLOAD_FORMATS):
        raise ValueError("select a valid I/Q download format")
    return fmt


def iq_sample_format(fmt: str) -> str:
    """The sample format of a download format, whatever file it's in."""
    if fmt == "sdriq":
        return "s16"
    return fmt.removeprefix("wav_").removeprefix("sigmf_")


def _systemtime(epoch_seconds: float) -> bytes:
    """A time as Windows' SYSTEMTIME, in local time, the way SDRangel writes the auxi chunk."""
    moment = datetime.fromtimestamp(max(0.0, epoch_seconds), timezone.utc).astimezone()
    return struct.pack(
        "<8H",
        moment.year,
        moment.month,
        moment.isoweekday() % 7,
        moment.day,
        moment.hour,
        moment.minute,
        moment.second,
        moment.microsecond // 1000,
    )


def iq_wav_auxi_chunk(recording: dict[str, Any]) -> bytes:
    """The "auxi" chunk SDR#, HDSDR and SpectraVue read a WAV file's centre frequency and times from."""
    started_at = float(recording.get("started_at", 0.0) or 0.0)
    stopped_at = float(recording.get("stopped_at", 0.0) or 0.0) or started_at + float(
        recording.get("duration_seconds", 0.0) or 0.0
    )
    frequency_hz = int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ)
    sample_rate = int(recording.get("sample_rate", 0) or 0)
    payload = (
        _systemtime(started_at)
        + _systemtime(stopped_at)
        # Centre frequency, A/D sample rate, IF frequency, bandwidth, I/Q offset and four unused fields.
        + struct.pack("<9I", frequency_hz, sample_rate, 0, 0, 0, 0, 0, 0, 0)
        # HDSDR's name for the next file of a split recording; there's never one.
        + bytes(96)
    )
    return b"auxi" + struct.pack("<I", len(payload)) + payload


def _wav_size(size: int) -> int:
    # A header for a file too large for WAV is only ever measured, never sent: such a
    # download is refused (see iq_download_format_problem).
    return min(size, 0xFFFFFFFF)


def iq_wav_header(fmt: str, recording: dict[str, Any], data_bytes: int) -> bytes:
    """A WAV header for I/Q samples: I in the left channel, Q in the right."""
    sample_rate = int(recording.get("sample_rate", 0) or 0)
    sample_bytes = IQ_SAMPLE_BYTES[iq_sample_format(fmt)]
    block = 2 * sample_bytes
    float_samples = iq_sample_format(fmt) == "cf"
    fmt_chunk = struct.pack(
        "<HHIIHH", 3 if float_samples else 1, 2, sample_rate, sample_rate * block, block, sample_bytes * 8
    )
    chunks = b""
    if float_samples:
        # Formats other than plain PCM have an extra size field and a "fact" chunk.
        fmt_chunk += struct.pack("<H", 0)
        chunks = b"fact" + struct.pack("<II", 4, _wav_size(data_bytes // block))
    chunks = (
        b"fmt "
        + struct.pack("<I", len(fmt_chunk))
        + fmt_chunk
        + chunks
        + iq_wav_auxi_chunk(recording)
        + b"data"
        + struct.pack("<I", _wav_size(data_bytes))
    )
    return b"RIFF" + struct.pack("<I", _wav_size(4 + len(chunks) + data_bytes)) + b"WAVE" + chunks


def iq_sdriq_header(recording: dict[str, Any]) -> bytes:
    """The header of SDRangel's .sdriq file, as its FileRecord writes it.

    Sample rate, centre frequency, start time (milliseconds since 1970), sample size
    (16 bit; the samples are signed 16 bit I/Q pairs), a filler, then a CRC-32 of the rest.
    """
    started_at = float(recording.get("started_at", 0.0) or 0.0)
    header = struct.pack(
        "<IQQII",
        int(recording.get("sample_rate", 0) or 0),
        int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ),
        max(0, round(started_at * 1000)),
        16,
        0,
    )
    return header + struct.pack("<I", zlib.crc32(header))


SIGMF_VERSION = "1.2.0"
SIGMF_DATATYPES = {"cf": "cf32_le", "s16": "ci16_le", "u8": "cu8"}


def iq_sigmf_metadata(fmt: str, recording: dict[str, Any]) -> bytes:
    """The .sigmf-meta file: what SigMF software needs to know about the samples."""
    started_at = datetime.fromtimestamp(float(recording.get("started_at", 0.0) or 0.0), timezone.utc)
    label = str(recording.get("stream_label", "") or "").strip()
    metadata = {
        "global": {
            "core:datatype": SIGMF_DATATYPES[iq_sample_format(fmt)],
            "core:sample_rate": int(recording.get("sample_rate", 0) or 0),
            "core:version": SIGMF_VERSION,
            "core:description": f"I/Q recording of {label}" if label else "I/Q recording",
            "core:recorder": f"NWR Stream Manager {package_version()}",
        },
        "captures": [
            {
                "core:sample_start": 0,
                "core:frequency": int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ),
                "core:datetime": started_at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{started_at.microsecond // 1000:03d}Z",
            }
        ],
        "annotations": [],
    }
    return (json.dumps(metadata, indent=4) + "\n").encode("utf-8")


def _tar_entry(name: str, size: int, mtime: float, directory: bool = False) -> bytes:
    entry = tarfile.TarInfo(name)
    entry.type = tarfile.DIRTYPE if directory else tarfile.REGTYPE
    entry.mode = 0o755 if directory else 0o644
    entry.size = 0 if directory else size
    entry.mtime = int(mtime)
    # POSIX.1-2001 (pax), as SigMF requires; it adds a size record only for files of 8 GiB and over.
    return entry.tobuf(tarfile.PAX_FORMAT, "utf-8", "strict")


def _tar_padding(size: int) -> bytes:
    return bytes(-size % tarfile.BLOCKSIZE)


def iq_sigmf_archive_parts(fmt: str, recording: dict[str, Any], data_bytes: int) -> tuple[bytes, bytes]:
    """A SigMF archive, less the samples: what comes before them and what comes after.

    A tar file holding a folder named after the recording, with its .sigmf-meta and
    .sigmf-data files inside. Every part's size is known up front, so the download's is too.
    """
    base = iq_recording_download_name(recording, fmt).removesuffix(".sigmf")
    mtime = float(recording.get("started_at", 0.0) or 0.0)
    metadata = iq_sigmf_metadata(fmt, recording)
    header = (
        _tar_entry(f"{base}/", 0, mtime, directory=True)
        + _tar_entry(f"{base}/{base}.sigmf-meta", len(metadata), mtime)
        + metadata
        + _tar_padding(len(metadata))
        + _tar_entry(f"{base}/{base}.sigmf-data", data_bytes, mtime)
    )
    # The data's padding, the two empty blocks that end a tar file, then padding to a whole record.
    trailer_size = len(_tar_padding(data_bytes)) + 2 * tarfile.BLOCKSIZE
    total = len(header) + data_bytes + trailer_size
    return header, bytes(trailer_size + (-total % tarfile.RECORDSIZE))


def iq_download_parts(fmt: str, recording: dict[str, Any], recording_bytes: int) -> tuple[bytes, int, bytes]:
    """A download, of a recording that takes ``recording_bytes`` as stored (float 32 bit):
    what comes before the samples, how many bytes of samples, and what comes after them."""
    data_bytes = recording_bytes // 4 * IQ_SAMPLE_BYTES[iq_sample_format(fmt)]
    if fmt.startswith("wav_"):
        return iq_wav_header(fmt, recording, data_bytes), data_bytes, b""
    if fmt == "sdriq":
        return iq_sdriq_header(recording), data_bytes, b""
    if fmt.startswith("sigmf_"):
        header, trailer = iq_sigmf_archive_parts(fmt, recording, data_bytes)
        return header, data_bytes, trailer
    return b"", data_bytes, b""


def iq_download_bytes(fmt: str, recording: dict[str, Any], recording_bytes: int) -> int:
    header, data_bytes, trailer = iq_download_parts(fmt, recording, recording_bytes)
    return len(header) + data_bytes + len(trailer)


def iq_download_format_problem(recording_bytes: int, sample_rate: int, fmt: str) -> str:
    """Why a recording can't be downloaded in ``fmt``, or "" if it can."""
    if fmt == "sdriq" or fmt.startswith("sigmf_"):
        if sample_rate <= 0:
            return "This recording's sample rate isn't known, so it can't be downloaded in that format. Choose a raw format."
        return ""
    if not fmt.startswith("wav_"):
        return ""
    if sample_rate <= 0:
        return "This recording's sample rate isn't known, so it can't be downloaded as a WAV file. Choose a raw format."
    if fmt == "wav_u8" and sample_rate not in WAV_U8_SAMPLE_RATES:
        return "Unsigned 8-bit WAV is only available for recordings at 256,000, 512,000 or 1,024,000 samples per second."
    if iq_download_bytes(fmt, {"sample_rate": sample_rate}, recording_bytes) > WAV_MAX_FILE_BYTES:
        return "This recording is too large for a WAV file in that format. Choose a smaller sample size or a raw format."
    return ""


def iq_download_formats(recording_bytes: int, sample_rate: int) -> list[dict[str, str]]:
    """The formats a recording can be downloaded in."""
    return [
        {"value": value, "label": label}
        for value, label in IQ_DOWNLOAD_FORMATS
        if not iq_download_format_problem(recording_bytes, sample_rate, value)
    ]


def iq_recording_download_size(path: Path, fmt: str, recording: dict[str, Any] | None = None) -> int:
    recording = recording or {}
    cf_size = path.stat().st_size
    if cf_size % 4:
        raise ValueError("I/Q recording file is not a valid float32 stream")
    fmt = validate_iq_download_format(fmt)
    problem = iq_download_format_problem(cf_size, int(recording.get("sample_rate", 0) or 0), fmt)
    if problem:
        raise ValueError(problem)
    return iq_download_bytes(fmt, recording, cf_size)


def iq_recording_download_name(recording: dict[str, Any], fmt: str) -> str:
    sample_rate = int(recording.get("sample_rate", 0) or 0)
    frequency_hz = int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ)
    started_at = datetime.fromtimestamp(float(recording.get("started_at", time.time()) or time.time()), timezone.utc).astimezone()
    date_text = started_at.strftime("%m%d%Y")
    time_text = started_at.strftime("%H%M")
    extension = "raw"
    if fmt.startswith("wav_"):
        extension = "wav"
    elif fmt.startswith("sigmf_"):
        extension = "sigmf"
    elif fmt == "sdriq":
        extension = "sdriq"
    # SDR++ finds the frequency in a file name as "<frequency>Hz", and other SDR software often does too.
    return f"nwrstmgr-s{sample_rate}-{frequency_hz}Hz-{date_text}-{time_text}-{iq_sample_format(fmt)}.{extension}"


def estimate_iq_storage_remaining_seconds(
    recorder: dict[str, Any],
    storage: dict[str, Any],
    recordings_directory: Path,
) -> float | None:
    if not recorder.get("active"):
        return None
    elapsed = float(recorder.get("elapsed_seconds", 0.0) or 0.0)
    bytes_written = int(recorder.get("bytes_written", 0) or 0)
    if elapsed <= 0.0 or bytes_written <= 0:
        return None
    write_rate = bytes_written / elapsed
    if write_rate <= 0.0:
        return None
    target_device = None
    try:
        target_device = int(os.stat(recordings_directory if recordings_directory.exists() else recordings_directory.parent).st_dev)
    except OSError:
        pass
    filesystems = storage.get("filesystems", []) if isinstance(storage, dict) else []
    filesystem = None
    if target_device is not None:
        for candidate in filesystems:
            if candidate.get("device_id") == target_device:
                filesystem = candidate
                break
    if filesystem is None and filesystems:
        filesystem = filesystems[0]
    if not filesystem:
        return None
    available_bytes = int(filesystem.get("available_bytes", 0) or 0)
    total_bytes = int(filesystem.get("total_bytes", 0) or 0)
    critical_by_percent = int(total_bytes * (STORAGE_CRITICAL_FREE_PERCENT / 100.0)) if total_bytes > 0 else 0
    reserve_bytes = max(STORAGE_CRITICAL_FREE_BYTES, critical_by_percent)
    usable_bytes = max(0, available_bytes - reserve_bytes)
    return usable_bytes / write_rate


def iq_storage_estimate_correction_threshold(seconds: float) -> float:
    return max(
        IQ_STORAGE_ESTIMATE_MIN_CORRECTION_SECONDS,
        min(
            IQ_STORAGE_ESTIMATE_MAX_CORRECTION_SECONDS,
            max(0.0, seconds) * IQ_STORAGE_ESTIMATE_CORRECTION_FRACTION,
        ),
    )


def smooth_iq_storage_remaining_seconds(
    *,
    previous: float | None,
    elapsed_since_update: float,
    raw: float | None,
) -> float | None:
    if raw is None:
        return previous
    if previous is None:
        return max(0.0, raw)
    expected = max(0.0, previous - max(0.0, elapsed_since_update))
    threshold = iq_storage_estimate_correction_threshold(expected)
    if raw < expected - threshold:
        return max(0.0, raw)
    if raw > expected + threshold:
        return expected + threshold
    return expected


def convert_iq_recording_chunks(source, fmt: str, header: bytes = b"", trailer: bytes = b""):
    fmt = iq_sample_format(validate_iq_download_format(fmt))
    if header:
        yield header
    carry = b""
    while True:
        raw = carry + source.read(IQ_DOWNLOAD_CHUNK_BYTES)
        if not raw:
            break
        usable = len(raw) - (len(raw) % 4)
        carry = raw[usable:]
        if usable <= 0:
            continue
        if fmt == "cf":
            yield raw[:usable]
            time.sleep(IQ_DOWNLOAD_YIELD_SECONDS)
            continue
        floats = np.frombuffer(raw[:usable], dtype="<f4")
        clipped = np.clip(floats, -1.0, 1.0)
        if fmt == "s16":
            yield np.round(clipped * 32767.0).astype("<i2").tobytes()
        else:
            yield np.round(clipped * 127.5 + 127.5).astype(np.uint8).tobytes()
        time.sleep(IQ_DOWNLOAD_YIELD_SECONDS)
    if carry:
        raise ValueError("I/Q recording file ended with a partial float32 sample")
    if trailer:
        yield trailer


def validate_iq_recording_duration(raw: Any) -> float:
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("I/Q recording duration must be a number of seconds") from exc
    if duration < IQ_RECORDER_MIN_DURATION_SECONDS or duration > IQ_RECORDER_MAX_DURATION_SECONDS:
        raise ValueError("I/Q recording duration must be 0 for manual stop, or up to 24 hours")
    return duration
