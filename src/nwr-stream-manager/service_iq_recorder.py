"""The service's I/Q recorder: starting and stopping recordings, and the recordings it has made."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import IQ_SAMPLE_RATE
from .constants import (
    INTERMEDIATE_IQ_SAMPLE_RATE,
    IQ_RECORDER_DEFAULT_DURATION_SECONDS,
    IQ_RECORDER_MODE_SPECTRUM,
    IQ_RECORDER_MODE_STREAM,
    IQ_RECORDER_SAMPLE_RATES,
)
from .iq_pipeline import validate_iq_recording_sample_rate
from .iq_recorder import (
    find_iq_recording,
    iq_recording_download_name,
    iq_recording_download_size,
    iq_recording_summary,
    IqRecorderConfig,
    IqRecorderWorker,
    load_iq_recording_entries,
    load_iq_recording_index,
    safe_iq_recording_file_path,
    unique_iq_recording_path,
    validate_iq_download_format,
    validate_iq_recording_duration,
)
from .rtl import DEFAULT_RTL_SAMPLE_RATE, NWR_CENTER_FREQUENCY_HZ
from .state_files import atomic_write_json, sanitize_path_component
from .storage import storage_status_message


LOG = logging.getLogger(__name__)


class IqRecorderServiceMixin:
    """Mixed into RtlControlService."""

    def iq_recorder_status(self) -> dict[str, Any]:
        with self.lock:
            return self._iq_recorder_status_locked()

    def redacted_iq_recorder_status(self) -> dict[str, Any]:
        return {
            "active": False,
            "status": "idle",
            "sample_rates": [],
            "default_duration_seconds": IQ_RECORDER_DEFAULT_DURATION_SECONDS,
        }

    def iq_recordings(self) -> dict[str, Any]:
        with self.lock:
            downloading = set(self.iq_recording_downloads)
        recordings = []
        for recording in load_iq_recording_entries(self.iq_recordings_index_path):
            item = iq_recording_summary(recording)
            item["downloading"] = item["id"] in downloading
            recordings.append(item)
        recordings.sort(key=lambda item: float(item.get("started_at", 0.0)), reverse=True)
        return {"recordings": recordings}

    def remove_iq_recording(self, recording_id: str) -> dict[str, Any]:
        recording_id = str(recording_id).strip()
        if not recording_id:
            raise ValueError("I/Q recording id is required")
        with self.lock:
            if recording_id in self.iq_recording_downloads:
                raise ValueError("This I/Q recording is currently being downloaded.")
        data = load_iq_recording_index(self.iq_recordings_index_path)
        recording = None
        remaining = []
        for entry in data["recordings"]:
            if str(entry.get("id", "")) == recording_id:
                recording = entry
            else:
                remaining.append(entry)
        if recording is None:
            raise ValueError("I/Q recording was not found")
        path = safe_iq_recording_file_path(self.iq_recordings_directory, recording, require_exists=False)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        data["recordings"] = remaining
        atomic_write_json(self.iq_recordings_index_path, data)
        LOG.info("removed I/Q recording %s", recording_id)
        return self.iq_recordings()

    def iq_recording_download_info(self, recording_id: str, fmt: str) -> tuple[dict[str, Any], Path, str, int]:
        recording_id = str(recording_id).strip()
        fmt = validate_iq_download_format(fmt)
        if not recording_id:
            raise ValueError("I/Q recording id is required")
        recording = find_iq_recording(self.iq_recordings_index_path, recording_id)
        path = safe_iq_recording_file_path(self.iq_recordings_directory, recording)
        return recording, path, iq_recording_download_name(recording, fmt), iq_recording_download_size(path, fmt)

    def begin_iq_recording_download(self, recording_id: str, account_id: int | None = None) -> str:
        download_id = uuid.uuid4().hex
        with self.lock:
            self.iq_recording_downloads.add(recording_id)
            self.iq_recording_download_accounts[download_id] = int(account_id or 0)
            self.iq_recording_download_recordings[download_id] = recording_id
        return download_id

    def finish_iq_recording_download(self, download_id: str) -> None:
        with self.lock:
            recording_id = self.iq_recording_download_recordings.pop(download_id, "")
            self.iq_recording_download_accounts.pop(download_id, None)
            self.aborted_iq_recording_downloads.discard(download_id)
            if recording_id and recording_id not in self.iq_recording_download_recordings.values():
                self.iq_recording_downloads.discard(recording_id)

    def iq_recording_download_aborted(self, download_id: str) -> bool:
        with self.lock:
            return download_id in self.aborted_iq_recording_downloads

    def _iq_recorder_status_locked(self) -> dict[str, Any]:
        worker = self.iq_recorder
        if worker is None:
            return {
                "active": False,
                "status": "idle",
                "sample_rates": list(IQ_RECORDER_SAMPLE_RATES),
                "default_duration_seconds": IQ_RECORDER_DEFAULT_DURATION_SECONDS,
            }
        snapshot = worker.snapshot()
        snapshot["sample_rates"] = list(IQ_RECORDER_SAMPLE_RATES)
        snapshot["default_duration_seconds"] = IQ_RECORDER_DEFAULT_DURATION_SECONDS
        snapshot["storage_remaining_seconds"] = worker.storage_time_remaining(
            self.storage_monitor.snapshot(),
            self.iq_recordings_directory,
        )
        return snapshot

    def start_iq_recording(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        with self.lock:
            existing = self.iq_recorder
            if existing is not None and existing.snapshot().get("active"):
                raise ValueError("I/Q recording is already in progress")
            clean_fanout = self.clean_fanout
            intermediate_fanout = self.intermediate_fanout
            config = self._iq_recorder_config_from_payload_locked(payload)
            if self.settings.uses_remote_sdr:
                # The host channelizes or decimates; the recorder passes it through.
                client = self.remote_client
                if client is None:
                    fanout = None
                elif config.mode == IQ_RECORDER_MODE_STREAM:
                    fanout = client.channel_fanout(int(config.target_frequency_hz or NWR_CENTER_FREQUENCY_HZ), name="iq-recorder")
                else:
                    fanout = client.wideband_fanout(config.sample_rate, name="iq-recorder")
            elif config.mode == IQ_RECORDER_MODE_STREAM:
                fanout = intermediate_fanout
            elif config.sample_rate == INTERMEDIATE_IQ_SAMPLE_RATE:
                fanout = intermediate_fanout
            else:
                fanout = clean_fanout
            if fanout is None:
                raise ValueError("RTL-SDR capture is not active")
            if self.storage_monitor.is_critical(config.output_path.parent):
                raise ValueError(storage_status_message("critical"))
            worker = IqRecorderWorker(
                fanout=fanout,
                config=config,
                storage_monitor=self.storage_monitor,
                alias_filter_strength_provider=self._alias_filter_strength,
            )
            self.iq_recorder = worker
            self.iq_recorder_account_id = int(account_id or 0) if account_id is not None else None
            worker.start()
        LOG.info(
            "started I/Q recording: mode=%s sample_rate=%s duration=%.1fs file=%s",
            config.mode,
            config.sample_rate,
            config.duration_seconds,
            config.output_path.name,
        )
        return {"success": True, "iq_recorder": self.iq_recorder_status()}

    def stop_iq_recording(self) -> dict[str, Any]:
        with self.lock:
            worker = self.iq_recorder
            self.iq_recorder = None
            self.iq_recorder_account_id = None
        if worker is not None:
            worker.stop()
            LOG.info("stopped I/Q recording")
        return {"success": True, "iq_recorder": self.iq_recorder_status()}

    def _iq_recorder_config_from_payload_locked(self, payload: dict[str, Any]) -> IqRecorderConfig:
        mode = str(payload.get("mode", IQ_RECORDER_MODE_STREAM)).strip().lower()
        if mode not in {IQ_RECORDER_MODE_STREAM, IQ_RECORDER_MODE_SPECTRUM}:
            raise ValueError("select a valid I/Q recording mode")
        duration_seconds = validate_iq_recording_duration(payload.get("duration_seconds", IQ_RECORDER_DEFAULT_DURATION_SECONDS))
        stream_id = ""
        stream_label = ""
        target_frequency_hz = None
        sample_rate = DEFAULT_RTL_SAMPLE_RATE
        file_label = mode
        if mode == IQ_RECORDER_MODE_STREAM:
            stream_id = str(payload.get("stream_id", "")).strip()
            stream = self._stream_locked(stream_id)
            if not stream.get("enabled", True):
                raise ValueError("select an active stream")
            station = stream.get("station", {})
            callsign = sanitize_path_component(str(station.get("callsign", "stream")))
            try:
                target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("stream station frequency is invalid") from exc
            stream_label = str(station.get("callsign", callsign))
            sample_rate = IQ_SAMPLE_RATE
            file_label = callsign
        else:
            sample_rate = validate_iq_recording_sample_rate(payload.get("sample_rate", DEFAULT_RTL_SAMPLE_RATE))
            file_label = f"spectrum-{sample_rate}"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        recording_id = uuid.uuid4().hex
        frequency_hz = int(target_frequency_hz or NWR_CENTER_FREQUENCY_HZ)
        output_path = unique_iq_recording_path(self.iq_recordings_directory, f"{timestamp}-{file_label}.cf32")
        return IqRecorderConfig(
            recording_id=recording_id,
            mode=mode,
            sample_rate=sample_rate,
            duration_seconds=duration_seconds,
            output_path=output_path,
            index_path=self.iq_recordings_index_path,
            frequency_hz=frequency_hz,
            stream_id=stream_id,
            stream_label=stream_label,
            target_frequency_hz=target_frequency_hz,
            alias_filter_strength=self.settings.alias_filter_strength,
        )

    def _stop_iq_recorder_locked(self) -> None:
        if self.iq_recorder is not None:
            self.iq_recorder.stop()
            self.iq_recorder = None
