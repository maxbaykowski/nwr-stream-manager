"""The service's fallback audio: its settings, custom announcements, and each stream's fallback action."""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, replace
from typing import Any

from .constants import CUSTOM_FALLBACK_AUDIO_FILE_NAME, FALLBACK_AUDIO_CUSTOM, FALLBACK_AUDIO_DEFAULT
from .custom_fallback_audio import (
    custom_fallback_audio_sha256,
    CustomFallbackAudioError,
    prepare_custom_fallback_audio,
    read_custom_fallback_audio,
)
from .fallback_audio import DEFAULT_FALLBACK_AUDIO, FallbackAudio, load_fallback_wave_bytes
from .fallback_settings import (
    legacy_stream_fallback_action,
    save_fallback_settings,
    validate_fallback_settings_payload,
    validate_stream_fallback_action_payload,
)
from .models import WebFallbackSettings
from .packaged_audio import read_packaged_audio
from .stream_config import save_streams, stream_worker_key


LOG = logging.getLogger(__name__)


class FallbackServiceMixin:
    """Mixed into RtlControlService."""

    def update_fallback_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("fallback audio settings are required")
        with self.custom_fallback_audio_lock:
            with self.lock:
                current = self.fallback_settings
                has_custom = self.custom_fallback_audio is not None
            # Only what the page may change; the custom announcement's checksum
            # is the server's.
            changes = {key: payload[key] for key in ("loop_delay_seconds", "audio") if key in payload}
            settings = validate_fallback_settings_payload({**asdict(current), **changes})
            if settings.audio == FALLBACK_AUDIO_CUSTOM and current.audio != FALLBACK_AUDIO_CUSTOM and not has_custom:
                raise ValueError("Upload or record a custom announcement first.")
            with self.lock:
                self.fallback_settings = settings
                save_fallback_settings(self.fallback_state_path, settings)
        LOG.info(
            "updated fallback audio settings: announcement=%s loop_delay=%.1fs",
            settings.audio,
            settings.loop_delay_seconds,
        )
        return self.status()

    def _load_custom_fallback_audio(self) -> None:
        settings = self.fallback_settings
        audio = None
        problem = ""
        if settings.custom_audio_sha256:
            try:
                data = read_custom_fallback_audio(self.custom_fallback_audio_path, settings.custom_audio_sha256)
                audio = load_fallback_wave_bytes(data, "custom announcement")
            except ValueError as exc:
                problem = str(exc)
                if settings.audio == FALLBACK_AUDIO_CUSTOM:
                    LOG.error("fallback audio will play the default announcement: %s", problem)
                else:
                    LOG.warning("custom fallback announcement is unavailable: %s", problem)
        with self.lock:
            self.custom_fallback_audio = audio
            self.custom_fallback_audio_problem = problem

    def save_custom_fallback_audio(self, data: bytes) -> dict[str, Any]:
        """Check an uploaded or recorded announcement, store it and start using it."""
        with self.custom_fallback_audio_lock:
            try:
                wav = prepare_custom_fallback_audio(data)
            except CustomFallbackAudioError as exc:
                LOG.warning("refused a custom fallback announcement: %s", exc)
                raise
            audio = load_fallback_wave_bytes(wav, "custom announcement")
            temporary = self.custom_fallback_audio_path.with_name(f".{CUSTOM_FALLBACK_AUDIO_FILE_NAME}.tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_bytes(wav)
            os.replace(temporary, self.custom_fallback_audio_path)
            with self.lock:
                settings = replace(
                    self.fallback_settings,
                    audio=FALLBACK_AUDIO_CUSTOM,
                    custom_audio_sha256=custom_fallback_audio_sha256(wav),
                )
                save_fallback_settings(self.fallback_state_path, settings)
                self.fallback_settings = settings
                self.custom_fallback_audio = audio
                self.custom_fallback_audio_problem = ""
        LOG.info("saved a custom fallback announcement (%.1f seconds)", audio.duration_seconds)
        return self.status()

    def fallback_announcement_wav(self, announcement: str) -> bytes:
        """An announcement's audio, for listening to it on the page."""
        if announcement == FALLBACK_AUDIO_DEFAULT:
            return read_packaged_audio(DEFAULT_FALLBACK_AUDIO)
        if announcement == FALLBACK_AUDIO_CUSTOM:
            with self.lock:
                expected = self.fallback_settings.custom_audio_sha256
            if not expected:
                raise ValueError("There is no custom announcement yet.")
            return read_custom_fallback_audio(self.custom_fallback_audio_path, expected)
        raise ValueError("Choose the default announcement or the custom announcement")

    def fallback_audio_snapshot(self) -> FallbackAudio:
        """The announcement streams play as fallback audio."""
        with self.lock:
            if self.fallback_settings.audio == FALLBACK_AUDIO_CUSTOM and self.custom_fallback_audio is not None:
                return self.custom_fallback_audio
            return self.default_fallback_audio

    def _fallback_settings_public_locked(self) -> dict[str, Any]:
        settings = self.fallback_settings
        custom = self.custom_fallback_audio
        return {
            "loop_delay_seconds": settings.loop_delay_seconds,
            "audio": settings.audio,
            "custom_audio_available": custom is not None,
            "custom_audio_seconds": round(custom.duration_seconds, 1) if custom is not None else None,
            # Changes when the custom announcement is replaced, so other open pages notice.
            "custom_audio_id": settings.custom_audio_sha256[:16] if custom is not None else "",
            "custom_audio_problem": self.custom_fallback_audio_problem,
        }

    def _migrate_stream_fallback_actions(self) -> None:
        # Streams saved before each had a fallback action keep doing what the old
        # shared setting did.
        legacy = legacy_stream_fallback_action(self.fallback_state_path)
        migrated = [stream for stream in self.streams if not isinstance(stream.get("fallback_action"), dict)]
        if not migrated:
            return
        for stream in migrated:
            stream["fallback_action"] = asdict(legacy)
        save_streams(self.streams_directory, self.streams)
        LOG.info(
            "set the fallback action of %s stream(s) to %s after %.0f seconds, from the earlier fallback audio setting",
            len(migrated),
            legacy.action,
            legacy.delay_seconds,
        )

    def update_stream_fallback_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        settings = validate_stream_fallback_action_payload(payload.get("fallback_action", payload))
        with self.lock:
            stream = self._stream_locked(stream_id)
            stream["fallback_action"] = asdict(settings)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            worker = self.stream_workers.get(stream_worker_key(stream))
            if worker is not None:
                worker.sync_stream(stream)
        LOG.info(
            "updated fallback action for stream %s: %s after %.0f seconds",
            stream_id,
            settings.action,
            settings.delay_seconds,
        )
        return self.stream_status()

    def fallback_settings_snapshot(self) -> WebFallbackSettings:
        with self.lock:
            return self.fallback_settings
