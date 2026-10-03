from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .config_errors import ConfigError


IQ_SAMPLE_RATE = 24000
SAME_ATTENTION_BAND_HZ = (900.0, 1100.0)
SAME_SPACE_BAND_HZ = (1400.0, 1600.0)
SAME_MARK_BAND_HZ = (2000.0, 2200.0)
PROTECTED_AUDIO_BANDS_HZ = (
    ("1050 Hz attention tone", SAME_ATTENTION_BAND_HZ),
    ("SAME space tone", SAME_SPACE_BAND_HZ),
    ("SAME mark tone", SAME_MARK_BAND_HZ),
)
# Attention tone, SAME space tone, SAME mark tone.
PROTECTED_TONES_HZ = (1050.0, 1562.5, 2083.3)
PROTECTED_TONE_MAX_LOSS_DB = 1.0
NOTCH_MIN_WIDTH_HZ = 60.0
NOTCH_MAX_WIDTH_HZ = 2000.0
NOTCH_DEFAULT_WIDTH_HZ = 100.0
AUDIO_NYQUIST_HZ = IQ_SAMPLE_RATE / 2


@dataclass(frozen=True)
class IcecastConfig:
    host: str
    port: int
    mount: str
    username: str
    password: str
    format: str
    sample_rate: int
    bitrate: int
    enabled: bool = True
    tls: bool = False
    name: str | None = None
    genre: str | None = None
    description: str | None = None
    public: bool = False

    @property
    def content_type(self) -> str:
        if self.format == "mp3":
            return "audio/mpeg"
        if self.format == "ogg":
            return "application/ogg"
        raise ConfigError(f"unsupported icecast format: {self.format}")


@dataclass(frozen=True)
class DeemphasisConfig:
    enabled: bool = True
    tau: float = 300.0


@dataclass(frozen=True)
class VolumeConfig:
    enabled: bool = False
    multiplier: float = 1.0


@dataclass(frozen=True)
class ComfortNoiseConfig:
    enabled: bool = False
    level_db: float = -40.0


@dataclass(frozen=True)
class FilterConfig:
    enabled: bool = False
    frequency: float = 0.0
    sharpness: float = 0.0


@dataclass(frozen=True)
class NotchConfig:
    enabled: bool = False
    frequency: float = 0.0
    width: float = NOTCH_DEFAULT_WIDTH_HZ


@dataclass(frozen=True)
class AudioConfig:
    deemphasis: DeemphasisConfig = field(default_factory=DeemphasisConfig)
    comfort_noise: ComfortNoiseConfig = field(default_factory=ComfortNoiseConfig)
    volume: VolumeConfig = field(default_factory=VolumeConfig)
    highpass: FilterConfig = field(default_factory=FilterConfig)
    lowpass: FilterConfig = field(
        default_factory=lambda: FilterConfig(enabled=False, frequency=3400.0, sharpness=2.0)
    )
    notch: NotchConfig = field(default_factory=NotchConfig)

    @property
    def deemphasis_tau(self) -> float:
        return self.deemphasis.tau if self.deemphasis.enabled else 0.0


@dataclass(frozen=True)
class EasRecordingConfig:
    enabled: bool = False
    pre_seconds: float = 2.0
    post_seconds: float = 5.0
    max_seconds: int = 120
    directory: str = ""
    format: str = "wav"
    local_time: bool = False


def parse_audio_config(raw: dict[str, Any]) -> AudioConfig:
    if "deemphasis_tau" in raw and "deemphasis" not in raw:
        tau = _float(raw, "deemphasis_tau")
        audio = AudioConfig(
            deemphasis=DeemphasisConfig(enabled=tau > 0, tau=tau),
            comfort_noise=parse_comfort_noise_config(raw.get("comfort_noise")),
            volume=parse_volume_config(raw.get("volume")),
            highpass=parse_filter_config(raw.get("highpass")),
            lowpass=parse_filter_config(raw.get("lowpass")),
            notch=parse_notch_config(raw.get("notch")),
        )
    else:
        audio = AudioConfig(
            deemphasis=parse_deemphasis_config(raw.get("deemphasis")),
            comfort_noise=parse_comfort_noise_config(raw.get("comfort_noise")),
            volume=parse_volume_config(raw.get("volume")),
            highpass=parse_filter_config(raw.get("highpass")),
            lowpass=parse_filter_config(raw.get("lowpass")),
            notch=parse_notch_config(raw.get("notch")),
        )
    validate_audio_config(audio)
    return audio


def parse_deemphasis_config(raw: Any) -> DeemphasisConfig:
    if raw is None:
        return DeemphasisConfig()
    raw = _object(raw, "audio.deemphasis")
    return DeemphasisConfig(
        enabled=_bool(raw, "enabled", True),
        tau=_float(raw, "tau", 530.0),
    )


def parse_volume_config(raw: Any) -> VolumeConfig:
    if raw is None:
        return VolumeConfig()
    raw = _object(raw, "audio.volume")
    return VolumeConfig(
        enabled=_bool(raw, "enabled", False),
        multiplier=_float(raw, "multiplier", 1.0),
    )


def parse_comfort_noise_config(raw: Any) -> ComfortNoiseConfig:
    if raw is None:
        return ComfortNoiseConfig()
    raw = _object(raw, "audio.comfort_noise")
    return ComfortNoiseConfig(
        enabled=_bool(raw, "enabled", False),
        level_db=_float(raw, "level_db", -40.0),
    )


def parse_filter_config(raw: Any) -> FilterConfig:
    if raw is None:
        return FilterConfig()
    raw = _object(raw, "audio filter")
    return FilterConfig(
        enabled=_bool(raw, "enabled", False),
        frequency=_float(raw, "frequency", 0.0),
        sharpness=_float(raw, "sharpness", 0.0),
    )


def parse_notch_config(raw: Any) -> NotchConfig:
    if raw is None:
        return NotchConfig()
    raw = _object(raw, "audio.notch")
    frequency = _float(raw, "frequency", 0.0)
    if "width" in raw:
        width = _float(raw, "width")
    elif "sharpness" in raw:
        # Settings saved before notch width existed; use the width that sharpness implied.
        width = 400.0 - 34.0 * min(10.0, max(0.0, _float(raw, "sharpness")))
        if _finite(frequency) and frequency > 0:
            width = min(max(width, NOTCH_MIN_WIDTH_HZ), notch_max_width(frequency))
    else:
        width = NOTCH_DEFAULT_WIDTH_HZ
    return NotchConfig(
        enabled=_bool(raw, "enabled", False),
        frequency=frequency,
        width=width,
    )


def notch_max_width(frequency: float) -> float:
    """Widest notch at this frequency that keeps the attention and SAME tones within
    PROTECTED_TONE_MAX_LOSS_DB."""
    allowed = math.sqrt(10 ** (PROTECTED_TONE_MAX_LOSS_DB / 10) - 1)
    widest = min(NOTCH_MAX_WIDTH_HZ, 2 * frequency)
    for tone in PROTECTED_TONES_HZ:
        widest = min(widest, allowed * abs(tone**2 - frequency**2) / tone)
    return max(NOTCH_MIN_WIDTH_HZ, math.floor(widest))


def validate_audio_config(config: AudioConfig) -> None:
    validate_deemphasis_config(config.deemphasis)
    validate_comfort_noise_config(config.comfort_noise)
    validate_volume_config(config.volume)
    validate_filter_config("highpass", config.highpass)
    validate_filter_config("lowpass", config.lowpass)
    validate_notch_config(config.notch)


def validate_deemphasis_config(config: DeemphasisConfig) -> None:
    if not _finite(config.tau) or not 0 <= config.tau <= 530:
        raise ConfigError("tau must be between 0 and 530")


def validate_volume_config(config: VolumeConfig) -> None:
    if not _finite(config.multiplier) or config.multiplier < 0:
        raise ConfigError("multiplier must be a finite number greater than or equal to 0")


def validate_comfort_noise_config(config: ComfortNoiseConfig) -> None:
    if not _finite(config.level_db) or not -60 <= config.level_db <= -30:
        raise ConfigError("level_db must be between -60 and -30")


def validate_filter_config(name: str, config: FilterConfig) -> None:
    if not config.enabled:
        return
    if not _finite(config.frequency) or not 0 < config.frequency <= AUDIO_NYQUIST_HZ:
        raise ConfigError(f"{name}.frequency must be between 0 and {AUDIO_NYQUIST_HZ}")
    if not _finite(config.sharpness) or not 0 <= config.sharpness <= 10:
        raise ConfigError(f"{name}.sharpness must be between 0 and 10")
    if name == "highpass" and config.frequency > SAME_ATTENTION_BAND_HZ[0]:
        raise ConfigError(
            f"highpass.frequency must be no higher than {SAME_ATTENTION_BAND_HZ[0]} Hz"
        )
    if name == "lowpass" and config.frequency < SAME_MARK_BAND_HZ[1]:
        raise ConfigError(
            f"lowpass.frequency must be no lower than {SAME_MARK_BAND_HZ[1]} Hz"
        )


def validate_notch_config(config: NotchConfig) -> None:
    if not config.enabled:
        return
    if not _finite(config.frequency) or not 0 < config.frequency <= AUDIO_NYQUIST_HZ:
        raise ConfigError(f"notch.frequency must be between 0 and {AUDIO_NYQUIST_HZ}")
    for label, (minimum, maximum) in PROTECTED_AUDIO_BANDS_HZ:
        if minimum <= config.frequency <= maximum:
            raise ConfigError(
                f"notch.frequency cannot be inside the protected {minimum}-{maximum} Hz "
                f"band for the {label}"
            )
    widest = notch_max_width(config.frequency)
    if not _finite(config.width) or not NOTCH_MIN_WIDTH_HZ <= config.width <= widest:
        raise ConfigError(
            f"notch.width must be between {NOTCH_MIN_WIDTH_HZ:g} and {widest:g} Hz "
            f"at {config.frequency:g} Hz, to protect the attention and SAME tones"
        )


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    return value


def _float(raw: dict[str, Any], key: str, default: float | int | None = None) -> float:
    value = raw.get(key, default)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    return float(value)


def _bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} must be a boolean")
    return value


def _finite(value: float) -> bool:
    return math.isfinite(value)


