"""The settings NWR Stream Manager keeps, and the data that passes between its parts."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .constants import (
    ALIAS_FILTER_STRENGTH_DEFAULT,
    FALLBACK_AUDIO_DEFAULT,
    SDR_SOURCE_LOCAL,
    SDR_SOURCE_REMOTE,
    STREAM_FALLBACK_PLAY_AUDIO,
)
from .dsp import ComplexArray
from .remote_sdr import REMOTE_IQ_BUFFER_SECONDS
from .rtl import (
    DEFAULT_RTL_SAMPLE_RATE,
    NWR_CENTER_FREQUENCY_HZ,
    RtlConfig,
    RtlConfigError,
    validate_ppm_correction,
)


@dataclass(frozen=True)
class RtlControlSettings:
    serial: str = ""
    sample_rate: int = DEFAULT_RTL_SAMPLE_RATE
    gain: float | None = None
    ppm_correction: int = 0
    bias_tee: bool = False
    alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT
    notify_sdr_failures: bool = False
    # Share this machine's SDR with paired NWR Stream Manager instances.
    remote_access_enabled: bool = False
    # "local" uses the SDR plugged into this machine; "remote" uses the SDR
    # shared by the paired instance remote_host_id.
    source_mode: str = SDR_SOURCE_LOCAL
    remote_host_id: str = ""
    # Seconds of remote IQ held back to ride out network hiccups (client side).
    remote_buffer_seconds: float = REMOTE_IQ_BUFFER_SECONDS

    @property
    def uses_remote_sdr(self) -> bool:
        return self.source_mode == SDR_SOURCE_REMOTE and bool(self.remote_host_id)

    def to_rtl_config(self) -> RtlConfig:
        if not self.serial:
            raise RtlConfigError("select an RTL-SDR before starting capture")
        return RtlConfig(
            serial=self.serial,
            sample_rate=DEFAULT_RTL_SAMPLE_RATE,
            center_frequency_hz=NWR_CENTER_FREQUENCY_HZ,
            ppm_correction=validate_ppm_correction(self.ppm_correction),
            gain=self.gain,
            bias_tee=bool(self.bias_tee),
        )


@dataclass(frozen=True)
class WebFallbackSettings:
    """Fallback audio settings shared by every stream that plays it."""

    loop_delay_seconds: float = 5.0
    audio: str = FALLBACK_AUDIO_DEFAULT
    # The custom announcement as it was checked and stored. Only the server sets
    # this, and the file is not played if it no longer matches.
    custom_audio_sha256: str = ""


@dataclass(frozen=True)
class WebStreamFallbackAction:
    action: str = STREAM_FALLBACK_PLAY_AUDIO
    delay_seconds: float = 30.0


@dataclass(frozen=True)
class WebNotificationSettings:
    enabled: bool = False
    server_url: str = "https://ntfy.sh"
    topic: str = ""
    priority: int = 3
    access_url_mode: str = "auto"
    custom_access_url: str = ""
    access_token: str = ""
    username: str = ""
    password: str = ""

    def public_dict(self) -> dict[str, Any]:
        effective_access_url = self.custom_access_url if self.access_url_mode == "custom" else ""
        return {
            "enabled": self.enabled,
            "server_url": self.server_url,
            "topic": self.topic,
            "priority": self.priority,
            "access_url_mode": self.access_url_mode,
            "custom_access_url": self.custom_access_url,
            "effective_access_url": effective_access_url,
            "access_token_set": bool(self.access_token),
            "username": self.username,
            "password_set": bool(self.password),
        }


@dataclass(frozen=True)
class WebStreamNotificationSettings:
    icecast_failures: bool = False
    soundcard_failures: bool = False
    recorded_eas_alerts: bool = False
    bad_reception: bool = False


def normalize_notification_access_url(raw: Any, *, default_scheme: str = "https") -> str:
    value = str(raw or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("NWR Stream Manager URL must start with http:// or https://")
    if parsed.query or parsed.fragment:
        raise ValueError("NWR Stream Manager URL cannot include a query string or fragment")
    return value.rstrip("/")


def normalize_notification_click_url(raw: Any, *, default_scheme: str = "http") -> str:
    value = str(raw or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"{default_scheme}://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("notification click URL must start with http:// or https://")
    return value


def reconcile_notification_access_url_selection(
    settings: WebNotificationSettings,
    options: list[dict[str, str]],
) -> WebNotificationSettings:
    mode = settings.access_url_mode or "auto"
    if mode in {"auto", "custom"}:
        return settings
    if any(option.get("id") == mode for option in options):
        return settings
    return replace(settings, access_url_mode="auto")


@dataclass
class WebFallbackPlaybackState:
    position: int = 0
    delay_samples_remaining: int = 0
    active: bool = False

    def reset(self) -> None:
        self.position = 0
        self.delay_samples_remaining = 0
        self.active = False


@dataclass(frozen=True)
class WebEasRecordingSettings:
    enabled: bool = False
    pre_seconds: float = 2.0
    post_seconds: float = 5.0
    max_seconds: int = 120
    format: str = "wav"


@dataclass(frozen=True)
class IqSampleBatch:
    data: ComplexArray
    sample_rate: int
    center_frequency_hz: int
    captured_at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class IqFileSourceConfig:
    path: Path
    sample_rate: int
    center_frequency_hz: int = NWR_CENTER_FREQUENCY_HZ
