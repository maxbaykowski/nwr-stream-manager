"""Watching the disk NWR Stream Manager saves recordings to, so it can warn and stop recording before it fills."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .constants import (
    STORAGE_CRITICAL_FREE_BYTES,
    STORAGE_CRITICAL_FREE_PERCENT,
    STORAGE_CRITICAL_INODES,
    STORAGE_IDLE_POLL_SECONDS,
    STORAGE_LOW_FREE_BYTES,
    STORAGE_LOW_FREE_PERCENT,
    STORAGE_LOW_INODES,
    STORAGE_RECORDING_POLL_SECONDS,
    STORAGE_STATUS_RECOVERY_SAMPLES,
)


LOG = logging.getLogger(__name__)


class StorageMonitor:
    def __init__(
        self,
        paths: list[Path] | None = None,
        *,
        statvfs_provider=None,
        stat_provider=None,
        critical_callback=None,
        recovery_callback=None,
        idle_poll_seconds: float = STORAGE_IDLE_POLL_SECONDS,
        recording_poll_seconds: float = STORAGE_RECORDING_POLL_SECONDS,
    ) -> None:
        self.paths = {Path(path).expanduser() for path in (paths or [])}
        self.statvfs_provider = statvfs_provider or os.statvfs
        self.stat_provider = stat_provider or os.stat
        self.critical_callback = critical_callback
        self.recovery_callback = recovery_callback
        self.idle_poll_seconds = float(idle_poll_seconds)
        self.recording_poll_seconds = float(recording_poll_seconds)
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.active_recordings = 0
        self.snapshot_data: dict[str, Any] = self._empty_snapshot()
        self.last_callback_status = ""
        self.filesystem_status_by_device: dict[int | str, str] = {}
        self.filesystem_recovery_candidates: dict[int | str, tuple[str, int]] = {}

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.refresh()
        self.thread = threading.Thread(target=self._run, name="storage-monitor", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def add_path(self, path: Path) -> None:
        with self.lock:
            self.paths.add(Path(path).expanduser())
        self.refresh()

    def recording_started(self, path: Path | None = None) -> None:
        if path is not None:
            with self.lock:
                self.paths.add(Path(path).expanduser())
        with self.lock:
            self.active_recordings += 1
        self.refresh()

    def recording_stopped(self) -> None:
        with self.lock:
            self.active_recordings = max(0, self.active_recordings - 1)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.snapshot_data))

    def is_critical(self, path: Path | None = None) -> bool:
        snapshot = self.snapshot()
        if path is not None:
            target_device = self._device_id_for_path(Path(path))
            if target_device is not None:
                for filesystem in snapshot.get("filesystems", []):
                    if filesystem.get("device_id") == target_device:
                        return filesystem.get("status") == "critical"
        return snapshot.get("status") == "critical"

    def refresh(self) -> dict[str, Any]:
        with self.lock:
            paths = sorted(self.paths, key=lambda item: str(item))
            active_recordings = self.active_recordings
        filesystems_by_device: dict[int | str, dict[str, Any]] = {}
        errors: list[str] = []
        for path in paths:
            existing_path = self._nearest_existing_path(path)
            try:
                stat_result = self.stat_provider(existing_path)
                stats = self.statvfs_provider(existing_path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            filesystem = self._filesystem_snapshot(path, existing_path, stat_result, stats)
            device_id = filesystem["device_id"]
            filesystem["raw_status"] = filesystem["status"]
            filesystem["status"] = self._stable_filesystem_status(device_id, filesystem["status"])
            current = filesystems_by_device.get(device_id)
            if current is None or len(str(filesystem["path"])) < len(str(current["path"])):
                filesystems_by_device[device_id] = filesystem
        filesystems = sorted(filesystems_by_device.values(), key=lambda item: str(item["path"]))
        status = self._aggregate_status(filesystems, errors)
        snapshot = {
            "status": status,
            "message": storage_status_message(status),
            "active_recordings": active_recordings,
            "poll_seconds": self.recording_poll_seconds if active_recordings > 0 else self.idle_poll_seconds,
            "filesystems": filesystems,
            "errors": errors,
            "updated_at": time.time(),
        }
        with self.lock:
            self.snapshot_data = snapshot
        return snapshot

    def _run(self) -> None:
        while not self.stop_event.is_set():
            snapshot = self.refresh()
            if snapshot.get("status") == "critical":
                LOG.warning("storage is critically low; recording should stop until space is freed")
                if self.critical_callback is not None and self.last_callback_status != "critical":
                    self.last_callback_status = "critical"
                    try:
                        self.critical_callback(snapshot)
                    except Exception as exc:
                        LOG.warning("storage critical callback failed: %s", exc)
            else:
                if self.last_callback_status == "critical" and self.recovery_callback is not None:
                    try:
                        self.recovery_callback(snapshot)
                    except Exception as exc:
                        LOG.warning("storage recovery callback failed: %s", exc)
                self.last_callback_status = str(snapshot.get("status", ""))
            poll_seconds = float(snapshot.get("poll_seconds", self.idle_poll_seconds))
            self.stop_event.wait(max(0.5, poll_seconds))

    def _device_id_for_path(self, path: Path) -> int | None:
        try:
            return int(self.stat_provider(self._nearest_existing_path(path)).st_dev)
        except OSError:
            return None

    def _nearest_existing_path(self, path: Path) -> Path:
        candidate = path.expanduser()
        while not candidate.exists() and candidate.parent != candidate:
            candidate = candidate.parent
        return candidate

    def _filesystem_snapshot(self, path: Path, existing_path: Path, stat_result, stats) -> dict[str, Any]:
        fragment_size = int(getattr(stats, "f_frsize", 0) or getattr(stats, "f_bsize", 0) or 1)
        total_bytes = int(stats.f_blocks) * fragment_size
        free_bytes = int(stats.f_bfree) * fragment_size
        available_bytes = int(stats.f_bavail) * fragment_size
        used_bytes = max(0, total_bytes - free_bytes)
        used_percent = (used_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
        available_percent = (available_bytes / total_bytes * 100.0) if total_bytes > 0 else 0.0
        available_inodes = int(getattr(stats, "f_favail", 0))
        status = storage_filesystem_status(available_bytes, available_percent, available_inodes)
        return {
            "path": str(path),
            "stat_path": str(existing_path),
            "device_id": int(stat_result.st_dev),
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "free_bytes": free_bytes,
            "available_bytes": available_bytes,
            "used_percent": round(used_percent, 1),
            "available_percent": round(available_percent, 1),
            "available_inodes": available_inodes,
            "status": status,
            "summary": storage_filesystem_summary(used_bytes, total_bytes, used_percent),
        }

    @staticmethod
    def _aggregate_status(filesystems: list[dict[str, Any]], errors: list[str]) -> str:
        if any(item.get("status") == "critical" for item in filesystems):
            return "critical"
        if any(item.get("status") == "low" for item in filesystems):
            return "low"
        if errors and not filesystems:
            return "unknown"
        return "ok"

    def _stable_filesystem_status(self, device_id: int | str, raw_status: str) -> str:
        severity = {"ok": 0, "low": 1, "critical": 2}
        raw = raw_status if raw_status in severity else "ok"
        current = self.filesystem_status_by_device.get(device_id)
        if current not in severity:
            self.filesystem_status_by_device[device_id] = raw
            self.filesystem_recovery_candidates.pop(device_id, None)
            return raw
        if severity[raw] >= severity[current]:
            self.filesystem_status_by_device[device_id] = raw
            self.filesystem_recovery_candidates.pop(device_id, None)
            return raw
        candidate, count = self.filesystem_recovery_candidates.get(device_id, ("", 0))
        count = count + 1 if candidate == raw else 1
        if count >= STORAGE_STATUS_RECOVERY_SAMPLES:
            self.filesystem_status_by_device[device_id] = raw
            self.filesystem_recovery_candidates.pop(device_id, None)
            return raw
        self.filesystem_recovery_candidates[device_id] = (raw, count)
        return current

    @staticmethod
    def _empty_snapshot() -> dict[str, Any]:
        return {
            "status": "unknown",
            "message": "",
            "active_recordings": 0,
            "poll_seconds": STORAGE_IDLE_POLL_SECONDS,
            "filesystems": [],
            "errors": [],
            "updated_at": 0.0,
        }


def storage_filesystem_status(
    available_bytes: int,
    available_percent: float,
    available_inodes: int,
) -> str:
    if (
        available_bytes <= STORAGE_CRITICAL_FREE_BYTES
        or available_percent <= STORAGE_CRITICAL_FREE_PERCENT
        or 0 < available_inodes <= STORAGE_CRITICAL_INODES
    ):
        return "critical"
    if (
        available_bytes <= STORAGE_LOW_FREE_BYTES
        or available_percent <= STORAGE_LOW_FREE_PERCENT
        or 0 < available_inodes <= STORAGE_LOW_INODES
    ):
        return "low"
    return "ok"


def storage_status_message(status: str) -> str:
    if status == "critical":
        return (
            "Storage is critically low. I/Q recording will be unavailable and EAS recording has been disabled "
            "for all streams. Please free up disk space."
        )
    if status == "low":
        return "Storage is running low. Please free up disk space."
    if status == "unknown":
        return "Storage could not be checked."
    return ""


def storage_filesystem_summary(used_bytes: int, total_bytes: int, used_percent: float) -> str:
    return f"Storage: {format_storage_bytes(used_bytes)} used of {format_storage_bytes(total_bytes)} ({used_percent:.0f}%)"


def format_storage_bytes(value: int | float) -> str:
    value = float(max(0.0, value))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    unit_index = 0
    while value >= 1000.0 and unit_index < len(units) - 1:
        value /= 1000.0
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    return f"{value:.1f} {units[unit_index]}"


def directory_size_bytes(path: Path) -> int:
    total = 0
    try:
        stack = [Path(path)]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.is_file(follow_symlinks=False):
                                total += int(entry.stat(follow_symlinks=False).st_size)
                        except OSError:
                            continue
            except OSError:
                continue
    except OSError:
        return 0
    return total
