"""The service's recorded EAS alerts: finding, exporting and removing them, and each stream's EAS recording settings."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import zipfile
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .constants import RECENT_EAS_ALERT_LIMIT, RECENT_EAS_ALERT_SECONDS
from .eas_alerts import (
    alert_download_name,
    alert_range_bounds_from_request,
    DELETE_ALERT_PRESETS,
    eas_alert_detail,
    eas_alert_id,
    eas_alert_index_path,
    eas_alert_summary,
    eas_recording_settings_from_stream,
    EXPORT_ALERT_PRESETS,
    filter_indexed_alerts,
    indexed_eas_alert_entries,
    load_eas_alert_entries,
    load_eas_alert_index,
    local_datetime_parts,
    parse_utc_datetime,
    preset_range_bounds,
    safe_eas_alert_file_path,
    unique_archive_name,
    validate_eas_recording_payload,
)
from .same_data import lookup_event
from .state_files import atomic_write_json, sanitize_path_component
from .stream_config import enabled_output_count, save_streams


LOG = logging.getLogger(__name__)


class EasAlertServiceMixin:
    """Mixed into RtlControlService."""

    def eas_alert_streams(self) -> dict[str, Any]:
        with self.lock:
            streams = [self._eas_alert_stream_summary(stream) for stream in self.streams if self._stream_has_eas_alert_index(stream)]
        return {"streams": streams}

    def eas_alerts(self, stream_id: str, page: int = 1, per_page: int = 25) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream_summary = self._eas_alert_stream_summary(stream)
        alerts = load_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))
        ordered_alerts = sorted(
            enumerate(alerts),
            key=lambda item: parse_utc_datetime(str(item[1].get("start_time_utc", ""))),
            reverse=True,
        )
        total = len(ordered_alerts)
        total_pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, total_pages)
        start = (page - 1) * per_page
        page_alerts = [
            eas_alert_summary(stream, alert, original_index)
            for original_index, alert in ordered_alerts[start : start + per_page]
        ]
        return {
            "stream": stream_summary,
            "alerts": page_alerts,
            "page": page,
            "per_page": per_page,
            "total": total,
            "total_pages": total_pages,
        }

    def eas_alert_bulk_options(self, stream_id: str) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        indexed_alerts = indexed_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        now = datetime.now(timezone.utc)
        export_presets = []
        for preset in EXPORT_ALERT_PRESETS:
            selected = filter_indexed_alerts(indexed_alerts, preset_range_bounds(preset["id"], now))
            if selected:
                export_presets.append({"id": preset["id"], "label": preset["label"], "count": len(selected)})
        delete_presets = []
        for preset in DELETE_ALERT_PRESETS:
            selected = filter_indexed_alerts(indexed_alerts, preset_range_bounds(preset["id"], now))
            if selected:
                delete_presets.append({"id": preset["id"], "label": preset["label"], "count": len(selected)})
        return {
            "export_presets": export_presets,
            "delete_presets": delete_presets,
            "total": len(indexed_alerts),
            "now": local_datetime_parts(datetime.now().astimezone()),
        }

    def eas_alert_range_count(self, stream_id: str, mode: str, start: str = "", end: str = "") -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        indexed_alerts = indexed_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        bounds = alert_range_bounds_from_request(mode, start, end)
        count = len(filter_indexed_alerts(indexed_alerts, bounds))
        return {"count": count}

    def eas_alert_export_zip(self, stream_id: str, mode: str, start: str = "", end: str = "") -> tuple[Path, str]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        indexed_alerts = indexed_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        selected = filter_indexed_alerts(indexed_alerts, alert_range_bounds_from_request(mode, start, end))
        if not selected:
            raise ValueError("No alerts were issued during this time.")
        station = stream.get("station", {})
        callsign = sanitize_path_component(str(station.get("callsign", "alerts")))
        fd, raw_path = tempfile.mkstemp(prefix=f"{callsign}-eas-alerts-", suffix=".zip")
        os.close(fd)
        zip_path = Path(raw_path)
        export_index = {"version": 1, "alerts": []}
        used_names: set[str] = set()
        try:
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for _index, alert in selected:
                    audio_path = safe_eas_alert_file_path(self.streams_directory, stream, alert)
                    archive_name = unique_archive_name(Path(str(alert.get("file_path", ""))).name, used_names)
                    archive.write(audio_path, archive_name)
                    export_entry = dict(alert)
                    export_entry["file_path"] = archive_name
                    export_index["alerts"].append(export_entry)
                archive.writestr("index.json", json.dumps(export_index, indent=2, sort_keys=True) + "\n")
        except Exception:
            zip_path.unlink(missing_ok=True)
            raise
        return zip_path, f"{callsign}-eas-alerts.zip"

    def eas_alert_detail(self, stream_id: str, alert_id: str) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream_summary = self._eas_alert_stream_summary(stream)
        alert, index, _index_path = self._eas_alert_by_id(stream, alert_id)
        return {"stream": stream_summary, "alert": eas_alert_detail(stream, alert, index)}

    def eas_alert_neighbors(self, stream_id: str, alert_id: str, per_page: int = 25) -> dict[str, Any]:
        """The alerts either side of one in the alert list, and the list page each is on."""
        with self.lock:
            stream = self._stream_locked(stream_id)
        alerts = load_eas_alert_entries(eas_alert_index_path(self.streams_directory, stream))
        ordered = sorted(
            enumerate(alerts),
            key=lambda item: parse_utc_datetime(str(item[1].get("start_time_utc", ""))),
            reverse=True,
        )
        ids = [eas_alert_id(alert, index) for index, alert in ordered]
        if alert_id not in ids:
            raise ValueError("EAS alert was not found")
        position = ids.index(alert_id)

        def neighbor(offset: int) -> dict[str, Any] | None:
            target = position + offset
            if not 0 <= target < len(ids):
                return None
            return {"id": ids[target], "page": target // per_page + 1}

        # The list is newest first, so the previous alert is the older one.
        return {"page": position // per_page + 1, "previous": neighbor(1), "next": neighbor(-1)}

    def eas_alert_audio_path(self, stream_id: str, alert_id: str) -> tuple[Path, str]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        alert, _index, _index_path = self._eas_alert_by_id(stream, alert_id)
        return safe_eas_alert_file_path(self.streams_directory, stream, alert), alert_download_name(stream, alert)

    def remove_eas_alert(self, stream_id: str, alert_id: str) -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        alert, _index, index_path = self._eas_alert_by_id(stream, alert_id)
        audio_path = safe_eas_alert_file_path(self.streams_directory, stream, alert, require_exists=False)
        try:
            audio_path.unlink()
        except FileNotFoundError:
            pass
        data = load_eas_alert_index(index_path)
        alerts = data["alerts"]
        data["alerts"] = [
            entry for position, entry in enumerate(alerts)
            if eas_alert_id(entry, position) != alert_id
        ]
        atomic_write_json(index_path, data)
        LOG.info("removed EAS alert %s for stream %s", alert_id, stream_id)
        return {"success": True}

    def remove_eas_alert_range(self, stream_id: str, mode: str, start: str = "", end: str = "") -> dict[str, Any]:
        with self.lock:
            stream = self._stream_locked(stream_id)
        index_path = eas_alert_index_path(self.streams_directory, stream)
        data = load_eas_alert_index(index_path)
        indexed_alerts = [(index, alert) for index, alert in enumerate(data["alerts"])]
        selected = filter_indexed_alerts(indexed_alerts, alert_range_bounds_from_request(mode, start, end))
        if not selected:
            raise ValueError("No alerts were issued during this time.")
        selected_indexes = {index for index, _alert in selected}
        for _index, alert in selected:
            audio_path = safe_eas_alert_file_path(self.streams_directory, stream, alert, require_exists=False)
            try:
                audio_path.unlink()
            except FileNotFoundError:
                pass
        data["alerts"] = [
            alert for index, alert in enumerate(data["alerts"])
            if index not in selected_indexes
        ]
        atomic_write_json(index_path, data)
        LOG.info("removed %s EAS alerts for stream %s", len(selected), stream_id)
        return {"success": True, "count": len(selected)}

    def _stream_has_eas_alert_index(self, stream: dict[str, Any]) -> bool:
        return (
            eas_recording_settings_from_stream(stream).enabled
            or eas_alert_index_path(self.streams_directory, stream).exists()
        )

    def _eas_alert_stream_summary(self, stream: dict[str, Any]) -> dict[str, Any]:
        station = stream.get("station", {})
        index_path = eas_alert_index_path(self.streams_directory, stream)
        count = len(load_eas_alert_entries(index_path)) if index_path.exists() else 0
        return {
            "id": stream.get("id", ""),
            "callsign": station.get("callsign", "Unknown"),
            "frequency": station.get("frequency", ""),
            "alert_count": count,
        }

    def _eas_alert_by_id(self, stream: dict[str, Any], alert_id: str) -> tuple[dict[str, Any], int, Path]:
        index_path = eas_alert_index_path(self.streams_directory, stream)
        alerts = load_eas_alert_entries(index_path)
        for index, alert in enumerate(alerts):
            if eas_alert_id(alert, index) == alert_id:
                return alert, index, index_path
        raise ValueError("EAS alert was not found")

    def update_eas_recording(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        settings = validate_eas_recording_payload(payload.get("eas_recording", payload))
        with self.lock:
            stream = self._stream_locked(stream_id)
            current_settings = eas_recording_settings_from_stream(stream)
            if current_settings.enabled and not settings.enabled:
                self._ensure_can_disable_eas_recording_locked(stream)
            stream["eas_recording"] = asdict(settings)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        LOG.info("updated EAS recording settings for stream %s", stream_id)
        return self.stream_status()

    def _ensure_can_disable_eas_recording_locked(self, stream: dict[str, Any]) -> None:
        if enabled_output_count(stream, eas_enabled=False) <= 0:
            raise ValueError("At least one output must remain enabled for each stream.")

    def _active_eas_recorders_locked(self) -> list[dict[str, Any]]:
        snapshots = []
        for worker in self.stream_workers.values():
            snapshot = worker.eas_snapshot()
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def _recent_eas_alerts_locked(self) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=RECENT_EAS_ALERT_SECONDS)
        alerts: list[dict[str, Any]] = []
        for stream in self.streams:
            station = stream.get("station", {})
            callsign = str(station.get("callsign", "Unknown"))
            index_path = eas_alert_index_path(self.streams_directory, stream)
            try:
                indexed_alerts = indexed_eas_alert_entries(index_path)
            except Exception as exc:
                LOG.warning("could not read EAS alert index for %s: %s", callsign, exc)
                continue
            for index, alert in indexed_alerts:
                issued_at = parse_utc_datetime(str(alert.get("start_time_utc", "")))
                if issued_at < cutoff:
                    continue
                event = lookup_event(str(alert.get("event_type", "")))
                alerts.append(
                    {
                        "id": eas_alert_id(alert, index),
                        "stream_id": stream.get("id", ""),
                        "callsign": callsign,
                        "frequency": station.get("frequency", ""),
                        "event_name": event.display_name,
                        "issued_at_utc": issued_at.isoformat().replace("+00:00", "Z"),
                        "issued_at_epoch": issued_at.timestamp(),
                    }
                )
        alerts.sort(key=lambda item: (-float(item["issued_at_epoch"]), str(item["callsign"])))
        return alerts[:RECENT_EAS_ALERT_LIMIT]
