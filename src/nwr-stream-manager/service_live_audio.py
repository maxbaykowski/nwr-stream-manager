"""The service's live audio: monitoring a stream and the Weather Radio Receiver in a browser."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .iq_pipeline import receiver_frequency_mhz, validate_receiver_frequency
from .live_audio import LiveAudioSource
from .receiver import WeatherReceiverWorker
from .rtl import NWR_CENTER_FREQUENCY_HZ
from .same_live import SameEventQueue
from .stream_config import stream_worker_key


LOG = logging.getLogger(__name__)


class LiveAudioServiceMixin:
    """Mixed into RtlControlService."""

    def record_live_audio_feedback(self, client_id: str, mode: str, message: str, details: dict[str, Any]) -> None:
        client_id = str(client_id or "").strip()
        mode = str(mode or "").strip().lower()
        message = str(message or "").strip()
        if not client_id or mode not in {"monitor", "receiver"}:
            return
        reason = str(details.get("reason", "")).strip().lower()
        try:
            scheduled_lead_seconds = float(details.get("scheduled_lead_seconds", 0.0))
        except (TypeError, ValueError):
            scheduled_lead_seconds = 0.0
        try:
            network_receive_kbps = float(details["receive_kbps"]) if "receive_kbps" in details else None
        except (TypeError, ValueError):
            network_receive_kbps = None
        try:
            media_delivery_ratio = float(details["media_delivery_ratio"]) if "media_delivery_ratio" in details else None
        except (TypeError, ValueError):
            media_delivery_ratio = None
        with self.lock:
            feedback = self.live_audio_feedback_by_client.setdefault(
                (mode, client_id),
                {
                    "latency_drop_count": 0,
                    "above_max_drop_count": 0,
                    "max_scheduled_lead_seconds": 0.0,
                    "network_receive_kbps": None,
                    "media_delivery_ratio": None,
                    "last_feedback_at": 0.0,
                },
            )
            if (
                message == "dropped live audio frame to reduce playback latency"
                and reason in {"above-max", "post-reset-max"}
            ):
                feedback["latency_drop_count"] = int(feedback.get("latency_drop_count", 0)) + 1
                feedback["above_max_drop_count"] = int(feedback.get("above_max_drop_count", 0)) + 1
            feedback["max_scheduled_lead_seconds"] = max(
                float(feedback.get("max_scheduled_lead_seconds", 0.0)),
                scheduled_lead_seconds,
            )
            if network_receive_kbps is not None and network_receive_kbps > 0:
                previous = feedback.get("network_receive_kbps")
                feedback["network_receive_kbps"] = (
                    network_receive_kbps
                    if previous is None
                    else min(float(previous), network_receive_kbps)
                )
            if media_delivery_ratio is not None and media_delivery_ratio > 0:
                previous = feedback.get("media_delivery_ratio")
                feedback["media_delivery_ratio"] = (
                    media_delivery_ratio
                    if previous is None
                    else min(float(previous), media_delivery_ratio)
                )
            feedback["last_feedback_at"] = time.monotonic()

    def consume_live_audio_feedback(self, client_id: str, mode: str) -> dict[str, Any]:
        client_id = str(client_id or "").strip()
        mode = str(mode or "").strip().lower()
        if not client_id or mode not in {"monitor", "receiver"}:
            return {}
        with self.lock:
            return self.live_audio_feedback_by_client.pop((mode, client_id), {})

    def clear_live_audio_feedback(self, client_id: str, mode: str = "") -> None:
        client_id = str(client_id or "").strip()
        mode = str(mode or "").strip().lower()
        if not client_id:
            return
        with self.lock:
            if mode in {"monitor", "receiver"}:
                self.live_audio_feedback_by_client.pop((mode, client_id), None)
            else:
                self.live_audio_feedback_by_client.pop(("monitor", client_id), None)
                self.live_audio_feedback_by_client.pop(("receiver", client_id), None)

    def open_monitor_audio_source(
        self,
        *,
        client_id: str,
        stream_id: str,
        account_id: int | None = None,
    ) -> tuple[LiveAudioSource, None, dict[str, Any]]:
        client_id = str(client_id).strip()
        stream_id = str(stream_id).strip()
        LOG.info("WebSocket monitor start requested: client=%s stream=%s", client_id or "<missing>", stream_id or "<missing>")
        if not client_id:
            raise ValueError("monitor client id is required")
        if not stream_id:
            raise ValueError("stream id is required")
        self.stop_receiver({"client_id": client_id}, account_id=account_id)
        self.stop_monitor({"client_id": client_id}, account_id=account_id)
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id)
            stream = self._stream_locked(stream_id)
            if stream.get("enabled", True) is False:
                raise ValueError("start the stream before monitoring it")
            self.monitor_streams_by_client[client_id] = stream_id
            self.monitor_accounts_by_client[client_id] = int(account_id or 0) if account_id is not None else 0
            self._sync_stream_workers_locked()
            worker = self.stream_workers.get(stream_worker_key(stream))
            if worker is None:
                self.monitor_streams_by_client.pop(client_id, None)
                self.monitor_accounts_by_client.pop(client_id, None)
                raise ValueError("stream worker could not be started for monitoring")
            source = worker.add_monitor_source(client_id)
        return source, None, self.monitor_status(client_id)

    def stop_monitor(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("monitor client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=False)
            stopped_stream_id = self.monitor_streams_by_client.get(client_id, "")
            detached_source = self._remove_monitor_source_locked(client_id)
        self._close_detached_monitor_sources([detached_source])
        if stopped_stream_id:
            LOG.info("stopped monitor for stream %s on client %s", stopped_stream_id, client_id)
        return {"success": True, "monitoring": self.monitor_status(client_id)}

    def pause_monitor(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("monitor client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=True)
            stream_id = self.monitor_streams_by_client.get(client_id, "")
            worker = None
            if stream_id:
                for candidate in self.stream_workers.values():
                    if candidate.id == stream_id:
                        worker = candidate
                        break
            if worker is None or not worker.set_monitor_source_paused(client_id, True):
                raise ValueError("stream monitor is not active")
        LOG.info("paused WebRTC monitor for stream %s on client %s", stream_id, client_id)
        return {"success": True, "monitoring": self.monitor_status(client_id)}

    def resume_monitor(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("monitor client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=True)
            stream_id = self.monitor_streams_by_client.get(client_id, "")
            stream = self._stream_locked(stream_id) if stream_id else None
            if stream is not None and stream.get("enabled", True) is False:
                raise ValueError("start the stream before monitoring it")
            worker = None
            if stream_id:
                for candidate in self.stream_workers.values():
                    if candidate.id == stream_id:
                        worker = candidate
                        break
            if worker is None or not worker.set_monitor_source_paused(client_id, False):
                raise ValueError("stream monitor is not active")
        LOG.info("resumed WebRTC monitor for stream %s on client %s", stream_id, client_id)
        return {"success": True, "monitoring": self.monitor_status(client_id)}

    def monitor_status(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            stream_id = self.monitor_streams_by_client.get(client_id, "")
            worker = None
            if stream_id:
                for candidate in self.stream_workers.values():
                    if candidate.id == stream_id:
                        worker = candidate
                        break
            stats = worker.monitor_source_stats(client_id) if worker is not None else {}
        return {"client_id": client_id, "stream_id": stream_id, "stats": stats}

    def open_receiver_audio_source(
        self,
        *,
        client_id: str,
        frequency_hz: int,
        account_id: int | None = None,
    ) -> tuple[LiveAudioSource, SameEventQueue, dict[str, Any]]:
        client_id = str(client_id).strip()
        frequency_hz = validate_receiver_frequency(frequency_hz)
        LOG.info(
            "WebSocket weather receiver start requested: client=%s frequency=%s MHz",
            client_id or "<missing>",
            receiver_frequency_mhz(frequency_hz),
        )
        if not client_id:
            raise ValueError("receiver client id is required")
        self.stop_monitor({"client_id": client_id}, account_id=account_id)
        self.stop_receiver({"client_id": client_id}, account_id=account_id)
        event_queue = SameEventQueue()
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id)
            if self._current_iq_source() is None:
                raise ValueError("RTL-SDR capture is not active")
            fanout = self._channel_feed(frequency_hz, f"receiver:{client_id}")
        worker = WeatherReceiverWorker(
            client_id=client_id,
            fanout=fanout,
            frequency_hz=frequency_hz,
            alias_filter_strength_provider=self._alias_filter_strength,
            event_queue=event_queue,
        )
        replaced_worker: WeatherReceiverWorker | None = None
        try:
            with self.lock:
                self._ensure_webrtc_client_owner_locked(client_id, account_id)
                replaced_worker = self.receiver_workers.pop(client_id, None)
                self.receiver_accounts_by_client.pop(client_id, None)
                self.receiver_workers[client_id] = worker
                self.receiver_accounts_by_client[client_id] = int(account_id or 0) if account_id is not None else 0
            worker.start()
        except Exception:
            worker.stop()
            raise
        if replaced_worker is not None:
            self._stop_receiver_worker_async(replaced_worker, reason="replaced")
        return worker.source, event_queue, self.receiver_status(client_id)

    def tune_receiver(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        frequency_hz = validate_receiver_frequency(payload.get("frequency_hz", NWR_CENTER_FREQUENCY_HZ))
        if not client_id:
            raise ValueError("receiver client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=True)
            worker = self.receiver_workers.get(client_id)
            if worker is None:
                raise ValueError("weather radio receiver is not playing")
            worker.set_frequency(frequency_hz)
        LOG.info("tuned weather receiver client %s to %s MHz", client_id, receiver_frequency_mhz(frequency_hz))
        return {"success": True, "receiver": self.receiver_status(client_id)}

    def pause_receiver(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("receiver client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=True)
            worker = self.receiver_workers.get(client_id)
            if worker is None:
                raise ValueError("weather radio receiver is not playing")
            worker.set_paused(True)
        LOG.info("paused weather receiver client %s", client_id)
        return {"success": True, "receiver": self.receiver_status(client_id)}

    def resume_receiver(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("receiver client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=True)
            worker = self.receiver_workers.get(client_id)
            if worker is None:
                raise ValueError("weather radio receiver is not playing")
            worker.set_paused(False)
        LOG.info("resumed weather receiver client %s", client_id)
        return {"success": True, "receiver": self.receiver_status(client_id)}

    def stop_receiver(self, payload: dict[str, Any], account_id: int | None = None) -> dict[str, Any]:
        client_id = str(payload.get("client_id", "")).strip()
        if not client_id:
            raise ValueError("receiver client id is required")
        with self.lock:
            self._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=False)
            worker = self._detach_receiver_locked(client_id)
        if worker is not None:
            worker.stop()
            LOG.info("stopped weather receiver for client %s", client_id)
        return {"success": True, "receiver": self.receiver_status(client_id)}

    def receiver_status(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            worker = self.receiver_workers.get(client_id)
        if worker is None:
            return {"client_id": client_id, "playing": False}
        snapshot = worker.snapshot()
        snapshot["playing"] = True
        return snapshot

    def _ensure_webrtc_client_owner_locked(
        self,
        client_id: str,
        account_id: int | None,
        *,
        require_existing: bool = False,
    ) -> None:
        if account_id is None:
            return
        expected = int(account_id)
        owners = []
        if client_id in self.monitor_streams_by_client:
            owners.append(self.monitor_accounts_by_client.get(client_id, 0))
        if client_id in self.receiver_workers:
            owners.append(self.receiver_accounts_by_client.get(client_id, 0))
        if not owners:
            if require_existing:
                raise ValueError("WebRTC client session is not active")
            return
        if any(int(owner or 0) != expected for owner in owners):
            raise ValueError("WebRTC client session belongs to a different account")

    @staticmethod
    def _close_detached_monitor_sources(sources: list[LiveAudioSource | None]) -> None:
        for source in sources:
            if source is None:
                continue
            try:
                source.close()
            except Exception as exc:
                LOG.debug("monitor source close failed: %s", exc)

    def _remove_monitor_source_locked(self, client_id: str) -> LiveAudioSource | None:
        stream_id = self.monitor_streams_by_client.pop(client_id, "")
        self.monitor_accounts_by_client.pop(client_id, None)
        if not stream_id:
            return None
        source = None
        for worker in self.stream_workers.values():
            if worker.id == stream_id:
                detach = getattr(worker, "detach_monitor_source", None)
                if callable(detach):
                    source = detach(client_id)
                else:
                    worker.remove_monitor_source(client_id)
                break
        self._sync_stream_workers_locked()
        return source

    def _remove_monitor_sources_for_stream_locked(
        self,
        stream_id: str,
        detached_sources: list[LiveAudioSource | None] | None = None,
    ) -> list[str]:
        client_ids = [
            client_id
            for client_id, monitored_stream_id in list(self.monitor_streams_by_client.items())
            if monitored_stream_id == stream_id
        ]
        for client_id in client_ids:
            source = self._remove_monitor_source_locked(client_id)
            if detached_sources is not None:
                detached_sources.append(source)
            elif source is not None:
                source.close()
        return client_ids

    def _live_audio_session_is_current_locked(self, mode: str, client_id: str, source: LiveAudioSource | None) -> bool:
        if mode == "receiver":
            worker = self.receiver_workers.get(client_id)
            return worker is not None and worker.source is source
        stream_id = self.monitor_streams_by_client.get(client_id, "")
        for worker in self.stream_workers.values():
            if worker.id == stream_id:
                with worker.lock:
                    return worker.monitor_sources.get(client_id) is source
        return False

    def _detach_receiver_locked(self, client_id: str) -> WeatherReceiverWorker | None:
        worker = self.receiver_workers.pop(client_id, None)
        self.receiver_accounts_by_client.pop(client_id, None)
        return worker

    def _stop_receiver_worker_async(self, worker: WeatherReceiverWorker, *, reason: str) -> None:
        def cleanup() -> None:
            try:
                worker.stop()
            except Exception as exc:
                LOG.warning(
                    "weather receiver cleanup failed for client %s after %s: %s",
                    getattr(worker, "client_id", "unknown"),
                    reason,
                    exc,
                )

        threading.Thread(
            target=cleanup,
            name=f"receiver-stop-{getattr(worker, 'client_id', 'unknown')}",
            daemon=True,
        ).start()

    def _detach_receiver_workers_locked(self) -> list[WeatherReceiverWorker]:
        workers = list(self.receiver_workers.values())
        self.receiver_workers = {}
        self.receiver_accounts_by_client = {}
        return workers
