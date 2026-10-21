"""The service's test mode: putting a stream on the simulated station for a while, and ending it."""

from __future__ import annotations

import logging
import time
from typing import Any

from .constants import (
    STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS,
    STREAM_TEST_MODE_HEARTBEAT_TIMEOUT_SECONDS,
    STREAM_TEST_MODE_TIME_LIMIT_REASON,
)
from .simulated_station import clamp_test_mode_signal_dbfs
from .stream_config import stream_worker_key


LOG = logging.getLogger(__name__)


class TestModeServiceMixin:
    """Mixed into RtlControlService."""

    def _cleanup_expired_stream_test_mode(self) -> None:
        with self.lock:
            self._cleanup_expired_stream_test_mode_locked(time.time())

    def _cleanup_expired_stream_test_mode_locked(self, now: float) -> None:
        session = self.stream_test_mode
        if not session:
            return
        if session.get("stopping"):
            return
        heartbeat_at = float(session.get("heartbeat_at", session.get("started_at", now)))
        reason = ""
        if now - heartbeat_at > STREAM_TEST_MODE_HEARTBEAT_TIMEOUT_SECONDS:
            reason = "client heartbeat expired"
        elif self.test_mode_time.remaining(now) <= 0.0:
            reason = STREAM_TEST_MODE_TIME_LIMIT_REASON
        if reason:
            self._stop_stream_test_mode_locked(reason=reason)

    def _stream_test_mode_status_locked(self) -> dict[str, Any]:
        session = self.stream_test_mode
        if not session:
            return self._inactive_stream_test_mode_status_locked()
        stream_id = str(session.get("stream_id", ""))
        worker = self.stream_workers.get(stream_id)
        worker_status = worker.test_mode_snapshot() if worker is not None else {}
        if bool(session.get("stopping", False)) and not bool(worker_status.get("active", False)):
            self.stream_test_mode = None
            return self._inactive_stream_test_mode_status_locked()
        return {
            "active": True,
            "stream_id": stream_id,
            "client_id": str(session.get("client_id", "")),
            "signal_dbfs": float(worker_status.get("signal_dbfs", session.get("signal_dbfs", STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS))),
            "same_active": bool(worker_status.get("same_active", False)),
            "stopping": bool(session.get("stopping", False) or worker_status.get("stopping", False)),
            "started_at": float(session.get("started_at", 0.0) or 0.0),
            "heartbeat_at": float(session.get("heartbeat_at", 0.0) or 0.0),
            "last_same_at": float(session.get("last_same_at", 0.0) or 0.0),
            "tones": worker_status.get("tones", {}),
            "time": self.test_mode_time.snapshot(time.time()),
        }

    def _inactive_stream_test_mode_status_locked(self) -> dict[str, Any]:
        return {"active": False, "time": self.test_mode_time.snapshot(time.time())}

    def update_stream_test_mode(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = str(payload.get("action", "")).strip().lower()
        stream_id = str(payload.get("stream_id", "")).strip()
        client_id = str(payload.get("client_id", "")).strip()
        now = time.time()
        if action not in {"start", "stop", "heartbeat", "signal", "same", "tones"}:
            raise ValueError("test mode action is required")
        if not client_id:
            raise ValueError("test mode client id is required")
        with self.lock:
            self._cleanup_expired_stream_test_mode_locked(now)
            if action == "stop":
                if self.stream_test_mode and str(self.stream_test_mode.get("client_id", "")) == client_id:
                    self._stop_stream_test_mode_locked(reason="client stopped test mode")
                return {"test_mode": self._stream_test_mode_status_locked(), "streams": list(self.streams)}
            if not stream_id:
                raise ValueError("stream id is required")
            stream = self._stream_locked(stream_id)
            if stream.get("enabled", True) is False:
                raise ValueError("stream must be enabled before test mode can be started")
            if action == "start":
                signal_dbfs = clamp_test_mode_signal_dbfs(payload.get("signal_dbfs", STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS))
                if self.stream_test_mode and str(self.stream_test_mode.get("client_id", "")) != client_id:
                    raise ValueError("test mode is owned by another browser")
                if self.stream_test_mode and str(self.stream_test_mode.get("stream_id", "")) != stream_id:
                    self._stop_stream_test_mode_locked(reason="another stream entered test mode")
                # All streams share one 10-minutes-per-hour allowance.
                self.test_mode_time.start(now)
                self.stream_test_mode = {
                    "stream_id": stream_id,
                    "client_id": client_id,
                    "signal_dbfs": signal_dbfs,
                    "started_at": now,
                    "heartbeat_at": now,
                    "last_same_at": now,
                }
                self._sync_stream_workers_locked()
                worker = self.stream_workers.get(stream_worker_key(stream))
                if worker is None:
                    self.stream_test_mode = None
                    self.test_mode_time.stop(now)
                    raise ValueError("test mode stream worker could not be started")
                worker.set_test_mode(True, signal_dbfs)
                LOG.info("started test mode for %s", stream.get("station", {}).get("callsign", stream_id))
                return {"test_mode": self._stream_test_mode_status_locked(), "streams": list(self.streams)}
            session = self.stream_test_mode
            if not session or str(session.get("stream_id", "")) != stream_id:
                raise ValueError("test mode is not active for this stream")
            if str(session.get("client_id", "")) != client_id:
                raise ValueError("test mode is owned by another browser")
            session["heartbeat_at"] = now
            worker = self.stream_workers.get(stream_worker_key(stream))
            if worker is None:
                self._stop_stream_test_mode_locked(reason="test mode worker missing")
                raise ValueError("test mode stream worker is not running")
            if action == "heartbeat":
                return {"test_mode": self._stream_test_mode_status_locked(), "streams": list(self.streams)}
            if action == "signal":
                signal_dbfs = clamp_test_mode_signal_dbfs(payload.get("signal_dbfs", session.get("signal_dbfs", STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS)))
                session["signal_dbfs"] = signal_dbfs
                worker.update_test_mode_signal(signal_dbfs)
                return {"test_mode": self._stream_test_mode_status_locked(), "streams": list(self.streams)}
            if action == "tones":
                worker.update_test_mode_tones(payload.get("tones"))
                return {"test_mode": self._stream_test_mode_status_locked(), "streams": list(self.streams)}
            if action == "same":
                session["last_same_at"] = now
                worker.trigger_test_mode_same()
                LOG.info("queued SAME test in test mode for %s", stream.get("station", {}).get("callsign", stream_id))
                return {"test_mode": self._stream_test_mode_status_locked(), "streams": list(self.streams)}
        raise ValueError("unsupported test mode action")

    def _stop_stream_test_mode_locked(self, *, reason: str) -> None:
        session = self.stream_test_mode
        if not session:
            return
        # Time stops counting as soon as test mode is asked to stop, even if a SAME test
        # still has to finish with its EOMs.
        self.test_mode_time.stop(time.time())
        stream_id = str(session.get("stream_id", ""))
        worker = self.stream_workers.get(stream_id)
        if worker is not None:
            try:
                snapshot = worker.set_test_mode(False)
                if snapshot.get("active", False):
                    session["stopping"] = True
                    session["stop_reason"] = reason
                    LOG.info("stopping stream test mode for %s: %s", stream_id or "unknown", reason)
                    return
            except Exception as exc:
                LOG.debug("failed to stop stream test mode for %s: %s", stream_id, exc)
        self.stream_test_mode = None
        self._sync_stream_workers_locked()
        LOG.info("stopped stream test mode for %s: %s", stream_id or "unknown", reason)
