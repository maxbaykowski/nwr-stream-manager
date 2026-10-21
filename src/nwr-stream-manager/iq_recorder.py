"""The I/Q recorder: saving raw radio data, and the recordings it makes."""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
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
    }


def format_duration_hms(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours = total // 3600
    minutes = (total % 3600) // 60
    remaining = total % 60
    if hours:
        return f"{hours}:{minutes:02d}:{remaining:02d}"
    return f"{minutes}:{remaining:02d}"


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
    if fmt not in {"cf", "s16", "u8"}:
        raise ValueError("select a valid I/Q download format")
    return fmt


def iq_recording_download_size(path: Path, fmt: str) -> int:
    cf_size = path.stat().st_size
    if cf_size % 4:
        raise ValueError("I/Q recording file is not a valid float32 stream")
    if fmt == "cf":
        return cf_size
    if fmt == "s16":
        return cf_size // 2
    if fmt == "u8":
        return cf_size // 4
    raise ValueError("select a valid I/Q download format")


def iq_recording_download_name(recording: dict[str, Any], fmt: str) -> str:
    sample_rate = int(recording.get("sample_rate", 0) or 0)
    frequency_hz = int(recording.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ) or NWR_CENTER_FREQUENCY_HZ)
    started_at = datetime.fromtimestamp(float(recording.get("started_at", time.time()) or time.time()), timezone.utc).astimezone()
    date_text = started_at.strftime("%m%d%Y")
    time_text = started_at.strftime("%H%M")
    return f"nwrstmgr-s{sample_rate}-f{frequency_hz}-{date_text}-{time_text}-{fmt}.raw"


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


def convert_iq_recording_chunks(source, fmt: str):
    fmt = validate_iq_download_format(fmt)
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


def validate_iq_recording_duration(raw: Any) -> float:
    try:
        duration = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("I/Q recording duration must be a number of seconds") from exc
    if duration < IQ_RECORDER_MIN_DURATION_SECONDS or duration > IQ_RECORDER_MAX_DURATION_SECONDS:
        raise ValueError("I/Q recording duration must be 0 for manual stop, or up to 24 hours")
    return duration
