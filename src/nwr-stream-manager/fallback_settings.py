"""The fallback audio settings, shared by every stream, and each stream's fallback action."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .constants import (
    FALLBACK_AUDIO_CHOICES,
    FALLBACK_AUDIO_DEFAULT,
    STREAM_FALLBACK_ACTIONS,
    STREAM_FALLBACK_MAX_DELAY_SECONDS,
    STREAM_FALLBACK_MIN_DELAY_SECONDS,
    STREAM_FALLBACK_NONE,
    STREAM_FALLBACK_PLAY_AUDIO,
)
from .models import WebFallbackSettings, WebStreamFallbackAction
from .state_files import whole_seconds


LOG = logging.getLogger(__name__)


def load_fallback_settings(path: Path) -> WebFallbackSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WebFallbackSettings()
    except Exception as exc:
        LOG.warning("failed to load fallback audio settings from %s: %s", path, exc)
        return WebFallbackSettings()
    if not isinstance(raw, dict):
        return WebFallbackSettings()
    try:
        return validate_fallback_settings_payload(raw)
    except Exception as exc:
        LOG.warning("fallback audio settings in %s are invalid: %s", path, exc)
        return WebFallbackSettings()


def save_fallback_settings(path: Path, settings: WebFallbackSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_fallback_settings_payload(raw: Any) -> WebFallbackSettings:
    if not isinstance(raw, dict):
        raise ValueError("fallback audio settings are required")
    loop_delay_seconds = whole_seconds(raw.get("loop_delay_seconds", 5), "Seconds before restart")
    if not 0 <= loop_delay_seconds <= 10:
        raise ValueError("Seconds before restart must be from 0 through 10 seconds")
    audio = str(raw.get("audio", FALLBACK_AUDIO_DEFAULT)).strip()
    if audio not in FALLBACK_AUDIO_CHOICES:
        raise ValueError("Choose the default announcement or a custom announcement")
    custom_audio_sha256 = str(raw.get("custom_audio_sha256", "")).strip().lower()
    if custom_audio_sha256 and not re.fullmatch(r"[0-9a-f]{64}", custom_audio_sha256):
        raise ValueError("the custom announcement's checksum is not valid")
    return WebFallbackSettings(
        loop_delay_seconds=loop_delay_seconds,
        audio=audio,
        custom_audio_sha256=custom_audio_sha256,
    )


def validate_stream_fallback_action_payload(raw: Any) -> WebStreamFallbackAction:
    if not isinstance(raw, dict):
        raise ValueError("fallback action settings are required")
    action = str(raw.get("action", "")).strip()
    if action not in STREAM_FALLBACK_ACTIONS:
        raise ValueError("Choose what the stream should do when it has a problem")
    delay_seconds = whole_seconds(raw.get("delay_seconds", 30), "Fallback delay")
    if not STREAM_FALLBACK_MIN_DELAY_SECONDS <= delay_seconds <= STREAM_FALLBACK_MAX_DELAY_SECONDS:
        raise ValueError(
            f"Fallback delay must be from {STREAM_FALLBACK_MIN_DELAY_SECONDS} through {STREAM_FALLBACK_MAX_DELAY_SECONDS} seconds"
        )
    return WebStreamFallbackAction(action=action, delay_seconds=delay_seconds)


def stream_fallback_action_from_stream(stream: dict[str, Any]) -> WebStreamFallbackAction:
    raw = stream.get("fallback_action")
    if not isinstance(raw, dict):
        return WebStreamFallbackAction()
    action = str(raw.get("action", ""))
    try:
        delay_seconds = float(raw.get("delay_seconds", 30))
    except (TypeError, ValueError):
        delay_seconds = 30.0
    if action not in STREAM_FALLBACK_ACTIONS:
        return WebStreamFallbackAction()
    return WebStreamFallbackAction(action=action, delay_seconds=max(0.0, delay_seconds))


def legacy_stream_fallback_action(path: Path) -> WebStreamFallbackAction:
    """What streams did before each had its own fallback action.

    Fallback audio used to be on or off for every stream at once, with one delay,
    in the same file that now holds only the shared fallback audio settings.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WebStreamFallbackAction()
    except Exception:
        return WebStreamFallbackAction()
    if not isinstance(raw, dict) or "enabled" not in raw:
        return WebStreamFallbackAction()
    try:
        delay_seconds = whole_seconds(raw.get("silence_timeout_seconds", 30), "Fallback delay")
    except ValueError:
        delay_seconds = 30
    if not STREAM_FALLBACK_MIN_DELAY_SECONDS <= delay_seconds <= STREAM_FALLBACK_MAX_DELAY_SECONDS:
        delay_seconds = 30
    action = STREAM_FALLBACK_PLAY_AUDIO if raw.get("enabled") else STREAM_FALLBACK_NONE
    return WebStreamFallbackAction(action=action, delay_seconds=delay_seconds)
