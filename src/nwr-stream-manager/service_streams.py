"""The service's streams: adding, changing and removing streams and their outputs, and keeping their workers running."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import asdict
from typing import Any

from .alsa import ALSA_CHANNEL_BOTH, ALSA_CHANNEL_LEFT, ALSA_CHANNEL_RIGHT
from .config import AudioConfig
from .constants import (
    ICECAST_AUTH_CACHE_SECONDS,
    SOUNDCARD_PREVIEW_LEASE_SECONDS,
    SOUNDCARD_PREVIEW_TIMEOUT_SECONDS,
)
from .eas_alerts import eas_recording_settings_from_stream
from .stream_archive import stream_archive_settings_from_stream
from .icecastauth import test_mountpoint_authentication
from .live_audio import LiveAudioSource
from .models import WebStreamFallbackAction
from .rtl import NWR_CENTER_FREQUENCY_HZ
from .stream_config import (
    enabled_output_count,
    icecast_auth_cache_key,
    icecast_auth_changed,
    icecast_auth_settings,
    icecast_auth_signature,
    mutable_stream_outputs,
    public_stream,
    remove_stream_configs,
    save_streams,
    soundcard_channels,
    stream_outputs,
    stream_worker_key,
    validate_audio_payload,
    validate_icecast_payload,
    validate_soundcard_output_payload,
)
from .stream_worker import IcecastStreamWorker


LOG = logging.getLogger(__name__)


class StreamServiceMixin:
    """Mixed into RtlControlService."""

    def _cleanup_expired_soundcard_previews(self) -> None:
        now = time.time()
        expired: list[tuple[str, IcecastStreamWorker | None]] = []
        with self.lock:
            for preview_id, preview in list(getattr(self, "preview_streams", {}).items()):
                heartbeat_at = float(preview.get("heartbeat_at", preview.get("updated_at", now)))
                if now - heartbeat_at <= float(preview.get("timeout_seconds", SOUNDCARD_PREVIEW_TIMEOUT_SECONDS)):
                    continue
                self.preview_streams.pop(preview_id, None)
                expired.append((preview_id, self.stream_workers.pop(stream_worker_key(preview), None)))
        for preview_id, worker in expired:
            if worker is not None:
                worker.stop()
            LOG.info("expired temporary soundcard preview %s", preview_id)

    def heartbeat_soundcard_preview_stream(self, preview_id: str) -> None:
        preview_id = str(preview_id).strip()
        if not preview_id:
            return
        with self.lock:
            preview = getattr(self, "preview_streams", {}).get(preview_id)
            if preview is not None:
                preview["heartbeat_at"] = time.time()

    def search_stations(self, query: str, limit: int = 50) -> dict[str, Any]:
        query = query.strip().lower()
        limit = max(1, min(int(limit), 100))
        matches = []
        for station in self.stations:
            haystack = " ".join(
                str(station.get(key, ""))
                for key in ("callsign", "frequency", "city", "site_name", "state", "state_name")
            ).lower()
            callsign = str(station.get("callsign", "")).lower()
            if not query or query in haystack:
                priority = 0 if callsign.startswith(query) else 1
                matches.append((priority, station))
        matches.sort(
            key=lambda item: (
                item[0],
                str(item[1].get("callsign", "")),
                str(item[1].get("state", "")),
                str(item[1].get("city", "")),
            )
        )
        return {"stations": [station for _, station in matches[:limit]]}

    def stream_status(self, *, read_only: bool = False, account_id: int | None = None) -> dict[str, Any]:
        with self.lock:
            if read_only:
                streams = [public_stream(stream) for stream in self.streams]
                account = int(account_id or 0)
                monitoring = {
                    client_id: stream_id
                    for client_id, stream_id in getattr(self, "monitor_streams_by_client", {}).items()
                    if self.monitor_accounts_by_client.get(client_id) == account
                }
            else:
                streams = list(self.streams)
                monitoring = dict(getattr(self, "monitor_streams_by_client", {}))
            return {
                "streams": streams,
                "monitoring": monitoring,
            }

    def add_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        station_key = str(payload.get("station_key", "")).strip()
        station = self._station_by_key(station_key)
        output_type = str(payload.get("type", "") or payload.get("output_type", "")).strip().lower()
        if not output_type:
            output_type = "soundcard" if payload.get("soundcard") is not None else "icecast"
        preview_worker = None
        preview_id = str(payload.get("preview_id", "")).strip()
        if preview_id:
            with self.lock:
                preview = getattr(self, "preview_streams", {}).pop(preview_id, None)
                if preview is not None:
                    preview_worker = self.stream_workers.pop(stream_worker_key(preview), None)
            if preview_worker is not None:
                preview_worker.stop()
        output_id = uuid.uuid4().hex
        if output_type == "icecast":
            icecast = validate_icecast_payload(payload.get("icecast"))
            with self.lock:
                self._reject_duplicate_icecast_locked(icecast)
            auth_cache_key = icecast_auth_cache_key(icecast)
            with self.lock:
                auth_is_cached = self._icecast_auth_cached_locked(auth_cache_key)
            if auth_is_cached:
                LOG.info(
                    "using cached Icecast authentication for %s:%s%s",
                    icecast["host"],
                    icecast["port"],
                    icecast["mount"],
                )
            else:
                result = self.test_icecast_auth(icecast)
                if not result["success"]:
                    return {
                        "success": False,
                        "message": result["message"],
                        "streams": list(self.streams),
                    }
                with self.lock:
                    self.icecast_auth_cache[auth_cache_key] = time.time()
            output = {
                "id": output_id,
                "enabled": True,
                "type": "icecast",
                "icecast": icecast,
                "auth_validated_at": time.time(),
                "auth_signature": icecast_auth_signature(icecast),
            }
        elif output_type == "soundcard":
            soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
            with self.lock:
                soundcard = self._normalize_soundcard_output_locked(
                    "",
                    output_id,
                    soundcard,
                    enabled=True,
                )
            output = {
                "id": output_id,
                "enabled": True,
                "type": "soundcard",
                "soundcard": soundcard,
            }
        else:
            raise ValueError("stream output type is not supported")
        stream = {
            "id": uuid.uuid4().hex,
            "enabled": True,
            "station": station,
            "outputs": [output],
            "audio": asdict(AudioConfig()),
            "fallback_action": asdict(WebStreamFallbackAction()),
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self.lock:
            self.streams.append(stream)
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": "Stream created.",
            "streams": list(self.streams),
        }

    def upsert_soundcard_preview_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        preview_id = str(payload.get("preview_id", "")).strip()
        station_key = str(payload.get("station_key", "")).strip()
        station = self._station_by_key(station_key)
        soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
        timeout_seconds = SOUNDCARD_PREVIEW_LEASE_SECONDS if payload.get("lease") else SOUNDCARD_PREVIEW_TIMEOUT_SECONDS
        with self.lock:
            if not hasattr(self, "preview_streams"):
                self.preview_streams = {}
            preview = self.preview_streams.get(preview_id) if preview_id else None
            if preview is None:
                preview_id = uuid.uuid4().hex
                output_id = uuid.uuid4().hex
                preview = {
                    "id": f"preview-{preview_id}",
                    "preview_id": preview_id,
                    "enabled": True,
                    "station": station,
                    "outputs": [
                        {
                            "id": output_id,
                            "enabled": True,
                            "type": "soundcard",
                            "soundcard": soundcard,
                        }
                    ],
                    "audio": asdict(AudioConfig()),
                    "fallback_action": asdict(WebStreamFallbackAction()),
                    "created_at": time.time(),
                    "updated_at": time.time(),
                    "heartbeat_at": time.time(),
                    "timeout_seconds": timeout_seconds,
                }
                self.preview_streams[preview_id] = preview
            else:
                preview["station"] = station
                outputs = mutable_stream_outputs(preview)
                if not outputs:
                    outputs.append({"id": uuid.uuid4().hex, "enabled": True, "type": "soundcard"})
                outputs[0]["enabled"] = True
                outputs[0]["type"] = "soundcard"
                outputs[0]["soundcard"] = soundcard
                preview["updated_at"] = time.time()
                preview["heartbeat_at"] = time.time()
                preview["timeout_seconds"] = timeout_seconds
            output = mutable_stream_outputs(preview)[0]
            output["soundcard"] = self._normalize_soundcard_output_locked(
                str(preview.get("id", "")),
                str(output.get("id", "")),
                output["soundcard"],
                enabled=True,
            )
            self._sync_stream_workers_locked()
        LOG.info("updated temporary soundcard preview for %s", station.get("callsign", "unknown"))
        return {
            "success": True,
            "message": "Soundcard preview started.",
            "preview_id": preview_id,
            "soundcard": output["soundcard"],
        }

    def discard_soundcard_preview_stream(self, preview_id: str) -> dict[str, Any]:
        preview_id = str(preview_id).strip()
        with self.lock:
            preview = getattr(self, "preview_streams", {}).pop(preview_id, None)
            worker = self.stream_workers.pop(stream_worker_key(preview), None) if preview is not None else None
        if worker is not None:
            worker.stop()
            LOG.info("discarded temporary soundcard preview %s", preview_id)
        return {"success": True}

    def test_icecast_auth(self, icecast: dict[str, Any]) -> dict[str, Any]:
        settings = icecast_auth_settings(icecast)
        LOG.info(
            "testing Icecast authentication to %s@%s:%s%s as %s",
            icecast["username"],
            icecast["host"],
            icecast["port"],
            icecast["mount"],
            icecast["format"],
        )
        result = test_mountpoint_authentication(settings)
        if result.success:
            LOG.info("Icecast authentication succeeded for %s:%s%s", icecast["host"], icecast["port"], icecast["mount"])
            with self.lock:
                self.icecast_auth_cache[icecast_auth_cache_key(icecast)] = time.time()
        else:
            LOG.warning("Icecast authentication failed for %s:%s%s: %s", icecast["host"], icecast["port"], icecast["mount"], result.message)
        return {"success": bool(result.success), "message": result.message}

    def test_icecast_auth_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        icecast = validate_icecast_payload(payload.get("icecast"))
        return self.test_icecast_auth(icecast)

    def _icecast_auth_cached_locked(self, cache_key: str) -> bool:
        now = time.time()
        for key, timestamp in list(self.icecast_auth_cache.items()):
            if now - timestamp > ICECAST_AUTH_CACHE_SECONDS:
                self.icecast_auth_cache.pop(key, None)
        timestamp = self.icecast_auth_cache.get(cache_key)
        return timestamp is not None and now - timestamp <= ICECAST_AUTH_CACHE_SECONDS

    def remove_stream(self, stream_id: str) -> dict[str, Any]:
        stream_id = stream_id.strip()
        detached_monitor_sources: list[LiveAudioSource] = []
        with self.lock:
            removed = [stream for stream in self.streams if stream.get("id") == stream_id]
            self.streams = [stream for stream in self.streams if stream.get("id") != stream_id]
            if not removed:
                raise ValueError("stream was not found")
            self._remove_monitor_sources_for_stream_locked(stream_id, detached_monitor_sources)
            remove_stream_configs(self.streams_directory, removed)
            self._sync_stream_workers_locked()
        self._close_detached_monitor_sources(detached_monitor_sources)
        # Its worker is being stopped, which finishes the archive's last file; then it goes.
        threading.Thread(target=self._remove_stream_archive_soon, args=(stream_id,), daemon=True).start()
        LOG.info("removed stream %s", stream_id)
        return self.stream_status()

    def _remove_stream_archive_soon(self, stream_id: str) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.lock:
                if stream_id not in self.stream_workers and not any(s.get("id") == stream_id for s in self.streams):
                    store = self.stream_archive_stores.get(stream_id)
                    if store is None or store.open_file is None:
                        break
            time.sleep(0.2)
        try:
            self.remove_stream_archive(stream_id)
        except Exception as exc:
            LOG.warning("couldn't delete the archived audio of removed stream %s: %s", stream_id, exc)

    def update_stream(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        if "enabled" not in payload:
            raise ValueError("stream enabled state is required")
        enabled = bool(payload.get("enabled"))
        detached_monitor_sources: list[LiveAudioSource] = []
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["enabled"] = enabled
            stream["updated_at"] = time.time()
            if not enabled:
                self._remove_monitor_sources_for_stream_locked(stream_id, detached_monitor_sources)
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        self._close_detached_monitor_sources(detached_monitor_sources)
        LOG.info("%s stream %s", "started" if enabled else "stopped", stream_id)
        return self.stream_status()

    def update_audio_effects(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        audio = validate_audio_payload(payload.get("audio", payload))
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["audio"] = audio
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            worker = self.stream_workers.get(stream_worker_key(stream))
            if worker is not None:
                worker.sync_stream(stream)
        LOG.info("updated audio effects for stream %s", stream_id)
        return self.stream_status()

    def update_stream_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        output_id = str(payload.get("output_id", "")).strip()
        enabled = bool(payload.get("enabled", True))
        output_type = str(payload.get("type", "")).strip().lower()
        with self.lock:
            stream, output = self._stream_output_locked(stream_id, output_id)
            output_type = output_type or str(output.get("type", "icecast")).strip().lower()
            if output.get("enabled", True) and not enabled:
                self._ensure_can_disable_output_locked(stream, output_id)
            if output_type == "icecast":
                icecast = validate_icecast_payload(payload.get("icecast"))
                self._reject_duplicate_icecast_locked(icecast, ignore_output_id=output_id)
                must_test_auth = icecast_auth_changed(output, icecast)
                if must_test_auth:
                    result = test_mountpoint_authentication(icecast_auth_settings(icecast))
                    if not result.success:
                        return {
                            "success": False,
                            "message": result.message,
                            "streams": list(self.streams),
                        }
                    output["auth_validated_at"] = time.time()
                    output["auth_signature"] = icecast_auth_signature(icecast)
                output["icecast"] = icecast
            elif output_type == "soundcard":
                soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
                output["soundcard"] = self._normalize_soundcard_output_locked(
                    stream_id,
                    output_id,
                    soundcard,
                    enabled=enabled,
                )
            else:
                raise ValueError("stream output type is not supported")
            output["enabled"] = enabled
            output["type"] = output_type
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": "Stream output settings saved.",
            "streams": list(self.streams),
        }

    def add_stream_output(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        output_type = str(payload.get("type", "icecast")).strip().lower()
        with self.lock:
            stream = self._stream_locked(stream_id)
            if output_type == "icecast":
                icecast = validate_icecast_payload(payload.get("icecast"))
                self._reject_duplicate_icecast_locked(icecast)
            elif output_type == "soundcard":
                soundcard = validate_soundcard_output_payload(payload.get("soundcard"))
            else:
                raise ValueError("stream output type is not supported")
        if output_type == "icecast":
            result = self.test_icecast_auth(icecast)
            if not result["success"]:
                return {
                    "success": False,
                    "message": result["message"],
                    "streams": list(self.streams),
                }
        with self.lock:
            stream = self._stream_locked(stream_id)
            output_id = uuid.uuid4().hex
            if output_type == "icecast":
                self._reject_duplicate_icecast_locked(icecast)
                output = {
                    "id": output_id,
                    "enabled": True,
                    "type": "icecast",
                    "icecast": icecast,
                    "auth_validated_at": time.time(),
                    "auth_signature": icecast_auth_signature(icecast),
                }
                message = "Icecast output added."
            else:
                soundcard = self._normalize_soundcard_output_locked(
                    stream_id,
                    output_id,
                    soundcard,
                    enabled=True,
                )
                output = {
                    "id": output_id,
                    "enabled": True,
                    "type": "soundcard",
                    "soundcard": soundcard,
                }
                message = "Soundcard output added."
            mutable_stream_outputs(stream).append(output)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        return {
            "success": True,
            "message": message,
            "output_id": output_id,
            "streams": list(self.streams),
        }

    def _normalize_soundcard_output_locked(
        self,
        stream_id: str,
        output_id: str,
        soundcard: dict[str, Any],
        *,
        enabled: bool,
    ) -> dict[str, Any]:
        normalized = dict(soundcard)
        stable_id = str(normalized.get("stable_id", "")).strip()
        display_name = self._friendly_soundcard_name_for_stable_id_locked(stable_id)
        if display_name:
            normalized["display_name"] = display_name
        if not enabled:
            return normalized
        occupied = self._occupied_soundcard_channels_locked(ignore_output_id=output_id).get(stable_id, set())
        requested = soundcard_channels(normalized.get("channel_mode", ALSA_CHANNEL_BOTH))
        if requested and requested.isdisjoint(occupied):
            return normalized
        available = [channel for channel in (ALSA_CHANNEL_LEFT, ALSA_CHANNEL_RIGHT) if channel not in occupied]
        if not available:
            raise ValueError("That sound card has no available output channels.")
        if len(available) == 2 and normalized.get("channel_mode") == ALSA_CHANNEL_BOTH:
            normalized["channel_mode"] = ALSA_CHANNEL_BOTH
        else:
            normalized["channel_mode"] = available[0]
        return normalized

    def _occupied_soundcard_channels_locked(self, *, ignore_output_id: str = "") -> dict[str, set[str]]:
        occupied: dict[str, set[str]] = {}
        for stream in list(self.streams) + list(getattr(self, "preview_streams", {}).values()):
            for output in stream_outputs(stream):
                if str(output.get("id", "")) == ignore_output_id:
                    continue
                if not output.get("enabled", True) or output.get("type") != "soundcard":
                    continue
                soundcard = output.get("soundcard", {})
                stable_id = str(soundcard.get("stable_id", "")).strip()
                if not stable_id:
                    continue
                occupied.setdefault(stable_id, set()).update(soundcard_channels(soundcard.get("channel_mode", ALSA_CHANNEL_BOTH)))
        return occupied

    def remove_stream_output(self, stream_id: str, output_id: str) -> dict[str, Any]:
        stream_id = stream_id.strip()
        output_id = output_id.strip()
        with self.lock:
            stream = self._stream_locked(stream_id)
            outputs = stream_outputs(stream)
            self._ensure_can_disable_output_locked(stream, output_id)
            before = len(outputs)
            stream["outputs"] = [output for output in outputs if output.get("id") != output_id]
            if len(stream["outputs"]) == before:
                raise ValueError("stream output was not found")
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        LOG.info("removed stream output %s from stream %s", output_id, stream_id)
        return self.stream_status()

    def _stream_locked(self, stream_id: str) -> dict[str, Any]:
        for stream in self.streams:
            if stream.get("id") == stream_id:
                return stream
        raise ValueError("stream was not found")

    def _stream_output_locked(self, stream_id: str, output_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        stream = self._stream_locked(stream_id)
        for output in stream_outputs(stream):
            if output.get("id") == output_id:
                return stream, output
        raise ValueError("stream output was not found")

    def _ensure_can_disable_output_locked(self, stream: dict[str, Any], output_id: str) -> None:
        if enabled_output_count(stream, disabled_output_id=output_id) <= 0:
            raise ValueError("At least one output must remain enabled for each stream.")

    def _reject_duplicate_icecast_locked(self, icecast: dict[str, Any], ignore_output_id: str | None = None) -> None:
        signature = icecast_auth_signature(icecast)
        for stream in self.streams:
            for output in stream_outputs(stream):
                if ignore_output_id and output.get("id") == ignore_output_id:
                    continue
                if output.get("type", "icecast") != "icecast":
                    continue
                if icecast_auth_signature(output.get("icecast", {})) == signature:
                    raise ValueError("An output with these credentials already exists.")

    def _station_by_key(self, key: str) -> dict[str, str]:
        for station in self.stations:
            if station.get("key") == key:
                return station
        raise ValueError("select a valid NWR station")

    def _sync_stream_workers_locked(self) -> None:
        active_test_stream_id = str(self.stream_test_mode.get("stream_id", "")) if self.stream_test_mode else ""
        if active_test_stream_id and not any(
            str(stream.get("id", "")) == active_test_stream_id and stream.get("enabled", True)
            for stream in self.streams
        ):
            LOG.info("stopped stream test mode for %s: stream is no longer enabled", active_test_stream_id)
            self.stream_test_mode = None
            self.test_mode_time.stop(time.time())
            active_test_stream_id = ""
        remote = self.remote_client if self.settings.uses_remote_sdr else None
        fanout = None if remote is not None else (
            self.intermediate_fanout or (self.test_mode_fanout if active_test_stream_id else None)
        )
        desired: dict[str, dict[str, Any]] = {}
        monitored_stream_ids = set(self.monitor_streams_by_client.values())
        if fanout is not None or remote is not None:
            for stream in list(self.streams) + list(getattr(self, "preview_streams", {}).values()):
                if not stream.get("enabled", True):
                    continue
                stream_id = str(stream.get("id", ""))
                if (
                    any(output.get("enabled", True) for output in stream_outputs(stream))
                    or stream_id in monitored_stream_ids
                    or eas_recording_settings_from_stream(stream).enabled
                    or stream_archive_settings_from_stream(stream).enabled
                    or stream_id == active_test_stream_id
                ):
                    desired[stream_worker_key(stream)] = stream

        for key in list(self.stream_workers):
            if key not in desired:
                worker = self.stream_workers.pop(key)
                self._stop_stream_worker_async(worker, reason="retired")

        if fanout is None and remote is None:
            return

        for key, stream in desired.items():
            worker = self.stream_workers.get(key)
            if worker is not None:
                worker.sync_stream(stream)
                continue
            station = stream.get("station", {})
            try:
                frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
            except (KeyError, TypeError, ValueError):
                frequency_hz = NWR_CENTER_FREQUENCY_HZ
            worker = IcecastStreamWorker(
                stream=stream,
                fanout=self._channel_feed(frequency_hz, f"stream:{station.get('callsign') or stream.get('id', 'stream')}"),
                fallback_settings_provider=self.fallback_settings_snapshot,
                fallback_audio_provider=self.fallback_audio_snapshot,
                alias_filter_strength_provider=self._alias_filter_strength,
                gain_provider=self._gain_signature,
                soundcard_devices_provider=self._cached_soundcards,
                soundcard_manager=self.soundcard_manager,
                state_directory=self.state_path.parent,
                storage_monitor=self.storage_monitor,
                archive_store_provider=self.stream_archive_store,
            )
            self.stream_workers[key] = worker
            worker.start()
            LOG.info(
                "started stream worker for %s",
                stream.get("station", {}).get("callsign", "unknown"),
            )

    def _stop_stream_worker_async(self, worker: IcecastStreamWorker, *, reason: str) -> None:
        def cleanup() -> None:
            try:
                worker.stop()
            except Exception as exc:
                LOG.warning(
                    "stream worker cleanup failed for %s after %s: %s",
                    getattr(worker, "id", "unknown"),
                    reason,
                    exc,
                )

        threading.Thread(
            target=cleanup,
            name=f"stream-worker-stop-{getattr(worker, 'id', 'unknown')}",
            daemon=True,
        ).start()

    def _detach_stream_workers_locked(self) -> list[IcecastStreamWorker]:
        workers = list(self.stream_workers.values())
        self.stream_workers = {}
        self.monitor_streams_by_client = {}
        self.monitor_accounts_by_client = {}
        return workers

    def _active_streams_locked(self) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for worker in self.stream_workers.values():
            if hasattr(worker, "snapshots"):
                snapshots.extend(worker.snapshots())
            else:
                snapshots.append(worker.snapshot())
        return snapshots

    def _stream_signals_locked(self) -> dict[str, dict[str, Any]]:
        return {
            str(stream_id): worker.signal_snapshot()
            for stream_id, worker in self.stream_workers.items()
            if hasattr(worker, "signal_snapshot")
        }
