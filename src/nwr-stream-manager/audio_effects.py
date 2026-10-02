from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
from numpy.typing import NDArray

from .config import (
    AudioConfig,
    ComfortNoiseConfig,
    FilterConfig,
    IQ_SAMPLE_RATE,
    NOTCH_MIN_WIDTH_HZ,
    NotchConfig,
    PROTECTED_TONES_HZ,
    PROTECTED_TONE_MAX_LOSS_DB,
    notch_max_width,
)
from .deemphasis import DeemphasisFilter
from .liquid_dsp import AudioDcBlocker, DelayedFftFirFilter


FILTER_TAPS = 1025
# The highpass, lowpass and notch run as one combined filter through liquid-dsp's FFT
# filter, in blocks one tap shorter than the filter. That adds a constant 1023 samples
# (about 43 ms) of delay while any of them is on.
EQ_FILTER_BLOCK_SAMPLES = FILTER_TAPS - 1
EQ_FILTER_KINDS = ("highpass", "lowpass", "notch")
FILTER_DESIGN_FFT_SIZE = 16384
# Sharpness 0 is a gentle 6 dB/octave slope; 10 is as steep as the kernel allows.
FILTER_MIN_ORDER = 1.0
FILTER_MAX_ORDER = 40.0
# The kernel's own resolution widens a notch by roughly this much (added in quadrature).
NOTCH_WINDOW_WIDTH_HZ = 50.0
DC_BLOCK_CUTOFF_HZ = 20.0
NWR_DEEMPHASIS_MAKEUP_GAIN = 2.0
NWR_DEEMPHASIS_DISABLED_GAIN = 0.75
COMFORT_NOISE_BASS_CUTOFF_HZ = 900.0
COMFORT_NOISE_VECTOR_CHUNK_SAMPLES = 1024


@dataclass
class ComfortNoiseGenerator:
    config: ComfortNoiseConfig
    rng: np.random.Generator = field(default_factory=np.random.default_rng)
    sample_rate: int = IQ_SAMPLE_RATE
    _brownish_previous: float = 0.0

    def __post_init__(self) -> None:
        self._brownish_coefficient = float(
            np.exp(-2.0 * np.pi * COMFORT_NOISE_BASS_CUTOFF_HZ / self.sample_rate)
        )

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if not self.config.enabled or len(samples) == 0:
            return samples
        level = comfort_noise_linear_level(self.config.level_db)
        noise = self.rng.normal(0.0, 1.0, len(samples)).astype(np.float32)
        noise = self._brownish_noise(noise, level)
        return (samples + noise).astype(np.float32, copy=False)

    def _brownish_noise(
        self,
        noise: NDArray[np.float32],
        level: float,
    ) -> NDArray[np.float32]:
        shaped = np.empty_like(noise, dtype=np.float32)
        for start in range(0, len(noise), COMFORT_NOISE_VECTOR_CHUNK_SAMPLES):
            stop = min(start + COMFORT_NOISE_VECTOR_CHUNK_SAMPLES, len(noise))
            shaped[start:stop] = self._brownish_noise_chunk(noise[start:stop])
        rms = float(np.sqrt(np.mean(shaped.astype(np.float64) ** 2)))
        if rms > 1e-12:
            shaped *= level / rms
        else:
            shaped *= 0.0
        return shaped

    def _brownish_noise_chunk(self, noise: NDArray[np.float32]) -> NDArray[np.float32]:
        coefficient = self._brownish_coefficient
        samples = noise.astype(np.float64, copy=False)
        indices = np.arange(len(samples), dtype=np.float64)
        powers = coefficient**indices
        weighted = np.cumsum(((1.0 - coefficient) * samples) / powers)
        shaped = (coefficient ** (indices + 1.0)) * self._brownish_previous
        shaped += powers * weighted
        self._brownish_previous = float(shaped[-1])
        return shaped.astype(np.float32, copy=False)


@dataclass
class DcBlocker:
    sample_rate: int = IQ_SAMPLE_RATE
    cutoff_hz: float = DC_BLOCK_CUTOFF_HZ

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.cutoff_hz <= 0:
            raise ValueError("cutoff_hz must be greater than 0")
        self.coefficient = float(np.exp(-2.0 * np.pi * self.cutoff_hz / self.sample_rate))
        self._filter = AudioDcBlocker(self.coefficient)

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if len(samples) == 0:
            return samples
        return self._filter.process(samples)


@dataclass
class AudioEffectsProcessor:
    config: AudioConfig
    sample_rate: int = IQ_SAMPLE_RATE

    def __post_init__(self) -> None:
        self.comfort_noise = ComfortNoiseGenerator(self.config.comfort_noise, sample_rate=self.sample_rate)
        self.deemphasis = DeemphasisFilter(
            self.sample_rate,
            self.config.deemphasis_tau,
        )
        self.dc_blocker = DcBlocker(self.sample_rate)
        # Highpass, lowpass and notch combined into one filter; None while all are off.
        self.eq_filter = _build_eq_filter(None, self.config, self.sample_rate)

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        audio = self.comfort_noise.process(samples)
        audio = self.deemphasis.process_float(audio)
        audio = audio * deemphasis_makeup_gain(self.config.deemphasis_tau)
        audio = self.dc_blocker.process(audio)
        if self.eq_filter is not None:
            audio = self.eq_filter.process(audio)
        if self.config.volume.enabled:
            audio = audio * self.config.volume.multiplier
        return np.clip(audio, -1.0, 1.0).astype(np.float32, copy=False)

    def update_config(self, config: AudioConfig) -> tuple[str, ...]:
        changed: list[str] = []
        if config == self.config:
            return ()

        if config.comfort_noise != self.config.comfort_noise:
            self.comfort_noise.config = config.comfort_noise
            changed.append("comfort_noise")

        if config.deemphasis != self.config.deemphasis:
            self.deemphasis.update_tau(config.deemphasis_tau)
            changed.append("deemphasis")

        eq_changes = [kind for kind in EQ_FILTER_KINDS if getattr(config, kind) != getattr(self.config, kind)]
        if eq_changes:
            self.eq_filter = _build_eq_filter(self.eq_filter, config, self.sample_rate)
            changed.extend(eq_changes)

        if config.volume != self.config.volume:
            changed.append("volume")

        self.config = replace(
            self.config,
            deemphasis=config.deemphasis,
            comfort_noise=config.comfort_noise,
            volume=config.volume,
            highpass=config.highpass,
            lowpass=config.lowpass,
            notch=config.notch,
        )
        return tuple(changed)


def comfort_noise_linear_level(level_db: float) -> float:
    return float(10 ** (level_db / 20.0))


def deemphasis_makeup_gain(tau: float) -> float:
    return NWR_DEEMPHASIS_MAKEUP_GAIN if tau > 0 else NWR_DEEMPHASIS_DISABLED_GAIN


def highpass_order(cutoff_hz: float, sharpness: float) -> float:
    """Butterworth-style order; each step of 1 adds 6 dB/octave of slope."""
    ratios = [cutoff_hz / tone for tone in PROTECTED_TONES_HZ if tone > cutoff_hz]
    return _order_for_sharpness(sharpness, ratios)


def lowpass_order(cutoff_hz: float, sharpness: float) -> float:
    """Butterworth-style order; each step of 1 adds 6 dB/octave of slope."""
    ratios = [tone / cutoff_hz for tone in PROTECTED_TONES_HZ if tone < cutoff_hz]
    return _order_for_sharpness(sharpness, ratios)


def notch_design_width(frequency: float, width: float) -> float:
    """Width to design for so the heard -3 dB width matches the requested one."""
    width = min(max(width, NOTCH_MIN_WIDTH_HZ), notch_max_width(frequency))
    return float(np.sqrt(max(width**2 - NOTCH_WINDOW_WIDTH_HZ**2, 1.0)))


def _order_for_sharpness(sharpness: float, tone_ratios: list[float]) -> float:
    # Lowest order that keeps every protected tone within PROTECTED_TONE_MAX_LOSS_DB.
    allowed = _protected_tone_power_ratio()
    gentlest = max(
        [FILTER_MIN_ORDER]
        + [np.log(allowed) / (2 * np.log(ratio)) for ratio in tone_ratios if ratio < 1]
    )
    sharpest = max(gentlest, FILTER_MAX_ORDER)
    return _interpolate_for_sharpness(sharpness, gentlest, sharpest)


def _protected_tone_power_ratio() -> float:
    return 10 ** (PROTECTED_TONE_MAX_LOSS_DB / 10) - 1


def _interpolate_for_sharpness(sharpness: float, gentlest: float, sharpest: float) -> float:
    normalized = max(0.0, min(1.0, sharpness / 10.0))
    return float(gentlest * (sharpest / gentlest) ** normalized)


def _build_eq_filter(
    current: DelayedFftFirFilter | None,
    config: AudioConfig,
    sample_rate: int,
) -> DelayedFftFirFilter | None:
    kernel = _combined_kernel([(kind, getattr(config, kind)) for kind in EQ_FILTER_KINDS], sample_rate)
    if kernel is None:
        return None
    if current is None:
        return DelayedFftFirFilter(kernel, EQ_FILTER_BLOCK_SAMPLES)
    # Retuned in place, carrying on from the latest audio, so changing a setting is seamless.
    current.update_taps(kernel)
    return current


def _filter_kernel(
    kind: str,
    config: FilterConfig | NotchConfig,
    sample_rate: int,
) -> NDArray[np.float32] | None:
    return _combined_kernel([(kind, config)], sample_rate)


def _combined_kernel(
    filters: list[tuple[str, FilterConfig | NotchConfig]],
    sample_rate: int,
) -> NDArray[np.float32] | None:
    """One kernel for every enabled filter, designed from the product of their curves.

    The filters run one after another, so their combined curve is the product of theirs;
    designing one kernel from it costs the same however many are on.
    """
    frequencies = np.fft.rfftfreq(FILTER_DESIGN_FFT_SIZE, 1 / sample_rate)
    magnitude = None
    active: list[tuple[str, FilterConfig | NotchConfig]] = []
    for kind, config in filters:
        curve = _filter_magnitude(kind, config, sample_rate, frequencies)
        if curve is not None:
            magnitude = curve if magnitude is None else magnitude * curve
            active.append((kind, config))
    if magnitude is None:
        return None
    kernel = _kernel_from_magnitude(magnitude, FILTER_TAPS).astype(np.float64)
    kinds = {kind for kind, _config in active}
    for kind, config in active:
        if kind == "notch":
            kernel = _deepen_null(kernel, config.frequency / sample_rate)
    if "lowpass" in kinds and "highpass" not in kinds:
        # Keep the bass at exactly its original level.
        kernel = kernel / np.sum(kernel)
    return kernel.astype(np.float32)


def _filter_magnitude(
    kind: str,
    config: FilterConfig | NotchConfig,
    sample_rate: int,
    frequencies: NDArray[np.float64],
) -> NDArray[np.float64] | None:
    if not config.enabled:
        return None
    if kind == "highpass":
        order = highpass_order(config.frequency, config.sharpness)
        with np.errstate(divide="ignore", over="ignore"):
            return 1 / np.sqrt(1 + (config.frequency / frequencies) ** (2 * order))
    if kind == "lowpass":
        if config.frequency >= sample_rate / 2:
            return None
        order = lowpass_order(config.frequency, config.sharpness)
        with np.errstate(over="ignore"):
            return 1 / np.sqrt(1 + (frequencies / config.frequency) ** (2 * order))
    if kind == "notch":
        if not 0 < config.frequency < sample_rate / 2:
            return None
        q = config.frequency / notch_design_width(config.frequency, config.width)
        distance = frequencies**2 - config.frequency**2
        return np.abs(distance) / np.sqrt(distance**2 + (frequencies * config.frequency / q) ** 2)
    raise ValueError(f"unsupported filter kind: {kind}")


def _kernel_from_magnitude(magnitude: NDArray[np.float64], taps: int) -> NDArray[np.float32]:
    """Linear-phase kernel that follows an analog-style magnitude curve."""
    impulse = np.fft.irfft(magnitude, FILTER_DESIGN_FFT_SIZE)
    kernel = np.roll(impulse, taps // 2)[:taps] * np.hamming(taps)
    return kernel.astype(np.float32)


def _deepen_null(kernel: NDArray[np.float64], center: float) -> NDArray[np.float64]:
    # Windowing leaves a little of the notch frequency behind; remove exactly that much.
    n = np.arange(len(kernel), dtype=np.float64) - len(kernel) // 2
    carrier = np.cos(2 * np.pi * center * n)
    correction = carrier * np.hamming(len(kernel))
    return kernel - (kernel @ carrier) * correction / (correction @ carrier)
