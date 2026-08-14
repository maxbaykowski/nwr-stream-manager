from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np
from numpy.typing import NDArray

from .config import AudioConfig, ComfortNoiseConfig, FilterConfig, IQ_SAMPLE_RATE
from .deemphasis import DeemphasisFilter


MIN_FILTER_TAPS = 5
MIN_HIGHPASS_FILTER_TAPS = 513
MAX_FILTER_TAPS = 1025
DC_BLOCK_CUTOFF_HZ = 20.0
DC_BLOCK_VECTOR_CHUNK_SAMPLES = 4096
BASE_DEEMPHASIS_MAKEUP_GAIN = 1.0
MAX_DEEMPHASIS_MAKEUP_GAIN = 2.2


@dataclass
class ComfortNoiseGenerator:
    config: ComfortNoiseConfig
    rng: np.random.Generator = field(default_factory=np.random.default_rng)

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if not self.config.enabled or len(samples) == 0:
            return samples
        level = comfort_noise_linear_level(self.config.level_db)
        noise = self.rng.normal(0.0, level, len(samples)).astype(np.float32)
        return (samples + noise).astype(np.float32, copy=False)


@dataclass
class FirFilter:
    kernel: NDArray[np.float32]
    history: NDArray[np.float32] = field(init=False)

    def __post_init__(self) -> None:
        self.history = np.zeros(max(len(self.kernel) - 1, 0), dtype=np.float32)

    def update_kernel(self, kernel: NDArray[np.float32]) -> None:
        kernel = np.asarray(kernel, dtype=np.float32)
        keep = max(len(kernel) - 1, 0)
        if keep <= 0:
            history = np.array([], dtype=np.float32)
        elif len(self.history) >= keep:
            history = self.history[-keep:].copy()
        else:
            history = np.zeros(keep, dtype=np.float32)
            if len(self.history):
                history[-len(self.history) :] = self.history
        self.kernel = kernel
        self.history = history

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if len(samples) == 0:
            return samples
        window = np.concatenate((self.history, samples))
        filtered = np.convolve(window, self.kernel, mode="full")
        start = len(self.history)
        stop = start + len(samples)
        if len(self.history):
            self.history = window[-len(self.history) :]
        filtered = filtered[start:stop].astype(np.float32, copy=False)
        return filtered


@dataclass
class DcBlocker:
    sample_rate: int = IQ_SAMPLE_RATE
    cutoff_hz: float = DC_BLOCK_CUTOFF_HZ
    _mean: float = 0.0

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.cutoff_hz <= 0:
            raise ValueError("cutoff_hz must be greater than 0")
        self.coefficient = float(np.exp(-2.0 * np.pi * self.cutoff_hz / self.sample_rate))

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if len(samples) == 0:
            return samples
        samples = samples.astype(np.float32, copy=False)
        if len(samples) <= DC_BLOCK_VECTOR_CHUNK_SAMPLES:
            return self._process_vector_chunk(samples)
        output = np.empty_like(samples, dtype=np.float32)
        for start in range(0, len(samples), DC_BLOCK_VECTOR_CHUNK_SAMPLES):
            stop = min(start + DC_BLOCK_VECTOR_CHUNK_SAMPLES, len(samples))
            output[start:stop] = self._process_vector_chunk(samples[start:stop])
        return output

    def _process_vector_chunk(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        indices = np.arange(len(samples), dtype=np.float64)
        powers = self.coefficient**indices
        weighted = np.cumsum(samples.astype(np.float64, copy=False) / powers)
        means = (self.coefficient ** (indices + 1.0)) * self._mean
        means += (1.0 - self.coefficient) * powers * weighted
        self._mean = float(means[-1])
        return (samples - means).astype(np.float32, copy=False)


@dataclass
class DeemphasisMakeupGain:
    tau: float

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if len(samples) == 0:
            return samples
        return (samples * deemphasis_makeup_gain(self.tau)).astype(np.float32, copy=False)


@dataclass
class AudioEffectsProcessor:
    config: AudioConfig
    sample_rate: int = IQ_SAMPLE_RATE

    def __post_init__(self) -> None:
        self.comfort_noise = ComfortNoiseGenerator(self.config.comfort_noise)
        self.deemphasis = DeemphasisFilter(
            self.sample_rate,
            self.config.deemphasis_tau,
        )
        self.deemphasis_makeup = DeemphasisMakeupGain(self.config.deemphasis_tau)
        self.dc_blocker = DcBlocker(self.sample_rate)
        self.highpass = _build_filter("highpass", self.config.highpass, self.sample_rate)
        self.lowpass = _build_filter("lowpass", self.config.lowpass, self.sample_rate)
        self.notch = _build_filter("notch", self.config.notch, self.sample_rate)

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        audio = self.comfort_noise.process(samples)
        audio = self.deemphasis.process_float(audio)
        audio = self.dc_blocker.process(audio)
        if self.highpass is not None:
            audio = self.highpass.process(audio)
        if self.lowpass is not None:
            audio = self.lowpass.process(audio)
        if self.notch is not None:
            audio = self.notch.process(audio)
        audio = self.deemphasis_makeup.process(audio)
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
            self.deemphasis_makeup.tau = config.deemphasis_tau
            changed.append("deemphasis")

        if config.highpass != self.config.highpass:
            self.highpass = _update_or_build_filter(
                self.highpass,
                "highpass",
                config.highpass,
                self.sample_rate,
            )
            changed.append("highpass")

        if config.lowpass != self.config.lowpass:
            self.lowpass = _update_or_build_filter(
                self.lowpass,
                "lowpass",
                config.lowpass,
                self.sample_rate,
            )
            changed.append("lowpass")

        if config.notch != self.config.notch:
            self.notch = _update_or_build_filter(
                self.notch,
                "notch",
                config.notch,
                self.sample_rate,
            )
            changed.append("notch")

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


def tap_count_for_sharpness(sharpness: float) -> int:
    return _tap_count_for_sharpness(sharpness, MIN_FILTER_TAPS)


def comfort_noise_linear_level(level_db: float) -> float:
    return float(10 ** (level_db / 20.0))


def deemphasis_makeup_gain(tau: float) -> float:
    normalized = max(0.0, min(1.0, tau / 530.0))
    return BASE_DEEMPHASIS_MAKEUP_GAIN + (
        MAX_DEEMPHASIS_MAKEUP_GAIN - BASE_DEEMPHASIS_MAKEUP_GAIN
    ) * normalized**0.85


def highpass_tap_count_for_sharpness(sharpness: float) -> int:
    normalized = max(0.0, min(1.0, sharpness / 10.0))
    taps = round(
        MIN_HIGHPASS_FILTER_TAPS
        + (MAX_FILTER_TAPS - MIN_HIGHPASS_FILTER_TAPS) * normalized**2.2
    )
    return taps if taps % 2 else taps + 1


def highpass_design_cutoff(cutoff_hz: float, sharpness: float) -> float:
    normalized = max(0.0, min(1.0, sharpness / 10.0))
    cutoff_scale = 0.2 + 0.8 * normalized**1.7
    return max(DC_BLOCK_CUTOFF_HZ, cutoff_hz * cutoff_scale)


def _tap_count_for_sharpness(sharpness: float, minimum_taps: int) -> int:
    normalized = max(0.0, min(1.0, sharpness / 10.0))
    taps = round(minimum_taps + (MAX_FILTER_TAPS - minimum_taps) * normalized**4)
    return taps if taps % 2 else taps + 1


def _build_filter(
    kind: str,
    config: FilterConfig,
    sample_rate: int,
) -> FirFilter | None:
    kernel = _filter_kernel(kind, config, sample_rate)
    return None if kernel is None else FirFilter(kernel)


def _update_or_build_filter(
    current: FirFilter | None,
    kind: str,
    config: FilterConfig,
    sample_rate: int,
) -> FirFilter | None:
    kernel = _filter_kernel(kind, config, sample_rate)
    if kernel is None:
        return None
    if current is None:
        return FirFilter(kernel)
    current.update_kernel(kernel)
    return current


def _filter_kernel(
    kind: str,
    config: FilterConfig,
    sample_rate: int,
) -> NDArray[np.float32] | None:
    if not config.enabled:
        return None
    if kind == "highpass":
        taps = highpass_tap_count_for_sharpness(config.sharpness)
        cutoff = highpass_design_cutoff(config.frequency, config.sharpness)
        return _highpass_kernel(cutoff, sample_rate, taps)
    taps = tap_count_for_sharpness(config.sharpness)
    if kind == "lowpass":
        if config.frequency >= sample_rate / 2:
            return None
        return _lowpass_kernel(config.frequency, sample_rate, taps)
    if kind == "notch":
        width = _notch_width(config.sharpness)
        low = max(1.0, config.frequency - width / 2)
        high = min(sample_rate / 2 - 1.0, config.frequency + width / 2)
        return _notch_kernel(low, high, sample_rate, taps)
    raise ValueError(f"unsupported filter kind: {kind}")


def _notch_width(sharpness: float) -> float:
    return 400.0 - 340.0 * (sharpness / 10.0)


def _lowpass_kernel(
    cutoff_hz: float,
    sample_rate: int,
    taps: int,
) -> NDArray[np.float32]:
    cutoff = cutoff_hz / sample_rate
    center = (taps - 1) / 2
    n = np.arange(taps, dtype=np.float64)
    kernel = 2 * cutoff * np.sinc(2 * cutoff * (n - center))
    kernel *= np.hamming(taps)
    kernel /= np.sum(kernel)
    return kernel.astype(np.float32)


def _highpass_kernel(
    cutoff_hz: float,
    sample_rate: int,
    taps: int,
) -> NDArray[np.float32]:
    lowpass = _lowpass_kernel(cutoff_hz, sample_rate, taps)
    highpass = -lowpass
    highpass[taps // 2] += 1.0
    return highpass.astype(np.float32)


def _notch_kernel(
    low_hz: float,
    high_hz: float,
    sample_rate: int,
    taps: int,
) -> NDArray[np.float32]:
    lowpass_low = _lowpass_kernel(low_hz, sample_rate, taps)
    lowpass_high = _lowpass_kernel(high_hz, sample_rate, taps)
    bandpass = lowpass_high - lowpass_low
    notch = -bandpass
    notch[taps // 2] += 1.0
    return notch.astype(np.float32)
