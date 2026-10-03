from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

import math

import numpy as np
from numpy.typing import NDArray

from .liquid_dsp import Oscillator


TONE_WAVEFORMS = ("sine", "square", "triangle", "sawtooth")
TONE_MIN_FREQUENCY_HZ = 20.0
TONE_DEFAULT_FREQUENCY_HZ = 440.0
TONE_DEFAULT_AMPLITUDE_PERCENT = 25.0
TONE_TABLE_SIZE = 8192
TONE_FADE_SECONDS = 0.02


def tone_max_frequency_hz(sample_rate: float, deviation_hz: float) -> float:
    """Highest tone that fits the channel at full deviation (Carson's rule)."""
    return float(sample_rate) / 2.0 - float(deviation_hz)


@dataclass(frozen=True)
class ToneGeneratorSettings:
    enabled: bool = False
    frequency: float = TONE_DEFAULT_FREQUENCY_HZ
    amplitude: float = TONE_DEFAULT_AMPLITUDE_PERCENT


@dataclass(frozen=True)
class ToneGeneratorBankSettings:
    muted: bool = False
    generators: dict[str, ToneGeneratorSettings] = field(
        default_factory=lambda: {name: ToneGeneratorSettings() for name in TONE_WAVEFORMS}
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "muted": self.muted,
            "generators": {
                name: {
                    "enabled": settings.enabled,
                    "frequency": settings.frequency,
                    "amplitude": settings.amplitude,
                }
                for name, settings in self.generators.items()
            },
        }


def parse_tone_generator_settings(
    raw: Any,
    current: ToneGeneratorBankSettings,
    *,
    max_frequency_hz: float,
) -> ToneGeneratorBankSettings:
    """Apply a partial update, rejecting values the channel can't carry."""
    if not isinstance(raw, dict):
        raise ValueError("tone generator settings are required")
    muted = current.muted
    if "muted" in raw:
        muted = bool(raw["muted"])
    generators = dict(current.generators)
    raw_generators = raw.get("generators", {})
    if not isinstance(raw_generators, dict):
        raise ValueError("tone generators must be an object")
    for name, raw_settings in raw_generators.items():
        if name not in TONE_WAVEFORMS:
            raise ValueError(f"unknown tone generator: {name}")
        if not isinstance(raw_settings, dict):
            raise ValueError(f"{name} tone generator settings must be an object")
        settings = generators[name]
        if "enabled" in raw_settings:
            settings = replace(settings, enabled=bool(raw_settings["enabled"]))
        if "frequency" in raw_settings:
            frequency = _finite_float(raw_settings["frequency"], f"{name} frequency")
            if not TONE_MIN_FREQUENCY_HZ <= frequency <= max_frequency_hz:
                raise ValueError(
                    f"{name} frequency must be between {TONE_MIN_FREQUENCY_HZ:g} "
                    f"and {max_frequency_hz:g} Hz"
                )
            settings = replace(settings, frequency=frequency)
        if "amplitude" in raw_settings:
            amplitude = _finite_float(raw_settings["amplitude"], f"{name} amplitude")
            if not 0.0 <= amplitude <= 100.0:
                raise ValueError(f"{name} amplitude must be between 0 and 100%")
            settings = replace(settings, amplitude=amplitude)
        generators[name] = settings
    return ToneGeneratorBankSettings(muted=muted, generators=generators)


def _finite_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number") from None
    if not np.isfinite(number):
        raise ValueError(f"{label} must be a number")
    return number


def band_limited_wavetable(waveform: str, frequency_hz: float, max_frequency_hz: float) -> NDArray[np.float32]:
    """One period of the waveform, keeping only harmonics at or below max_frequency_hz.

    The peak is normalized to 1, so the amplitude setting maps directly to deviation.
    """
    harmonics = max(1, int(max_frequency_hz // max(frequency_hz, 1e-9)))
    harmonics = min(harmonics, TONE_TABLE_SIZE // 2 - 1)
    n = np.arange(1, harmonics + 1, dtype=np.float64)
    if waveform == "sine":
        coefficients = np.where(n == 1, 1.0, 0.0)
    elif waveform == "square":
        coefficients = np.where(n % 2 == 1, 1.0 / n, 0.0)
    elif waveform == "triangle":
        coefficients = np.where(n % 2 == 1, (-1.0) ** ((n - 1) // 2) / n**2, 0.0)
    elif waveform == "sawtooth":
        coefficients = (-1.0) ** (n + 1) / n
    else:
        raise ValueError(f"unknown waveform: {waveform}")
    spectrum = np.zeros(TONE_TABLE_SIZE // 2 + 1, dtype=np.complex128)
    # Sine components: X[k] = -j * N/2 * b_k gives b_k * sin(2*pi*k*t).
    spectrum[1 : harmonics + 1] = -1j * (TONE_TABLE_SIZE / 2) * coefficients
    table = np.fft.irfft(spectrum, TONE_TABLE_SIZE)
    table /= float(np.max(np.abs(table)))
    return table.astype(np.float32)


@dataclass
class _ToneVoice:
    waveform: str
    frequency: float = TONE_DEFAULT_FREQUENCY_HZ
    table: NDArray[np.float32] | None = None
    phase: float = 0.0
    level: float = 0.0
    # The sine comes straight from liquid-dsp's precise oscillator instead of a table.
    oscillator: Oscillator | None = None


class ToneGeneratorBank:
    """Sine, square, triangle and sawtooth generators mixed into test mode audio."""

    def __init__(self, *, sample_rate: int, max_frequency_hz: float) -> None:
        self.sample_rate = int(sample_rate)
        self.max_frequency_hz = float(max_frequency_hz)
        self.settings = ToneGeneratorBankSettings()
        self.voices = {name: _ToneVoice(waveform=name) for name in TONE_WAVEFORMS}

    def update(self, settings: ToneGeneratorBankSettings) -> None:
        self.settings = settings

    def target_level(self, name: str) -> float:
        settings = self.settings.generators[name]
        if self.settings.muted or not settings.enabled:
            return 0.0
        return settings.amplitude / 100.0

    def process(self, count: int) -> tuple[NDArray[np.float32], NDArray[np.float64]]:
        """Mixed tones, plus each sample's summed generator level (1.0 = full deviation)."""
        output = np.zeros(count, dtype=np.float32)
        total_levels = np.zeros(count, dtype=np.float64)
        if count <= 0:
            return output, total_levels
        fade_step = 1.0 / max(1.0, TONE_FADE_SECONDS * self.sample_rate)
        for name, voice in self.voices.items():
            target = self.target_level(name)
            if voice.level == 0.0 and target == 0.0:
                continue
            frequency = self.settings.generators[name].frequency
            # Ramp level changes so toggling or adjusting a generator never clicks.
            steps = voice.level + np.sign(target - voice.level) * fade_step * np.arange(1, count + 1)
            levels = np.clip(steps, min(voice.level, target), max(voice.level, target))
            output += (levels * self._waveform(name, voice, frequency, count)).astype(np.float32)
            total_levels += levels
            voice.level = float(levels[-1])
        return output, total_levels

    def _waveform(self, name: str, voice: _ToneVoice, frequency: float, count: int) -> NDArray[np.float64]:
        if name == "sine":
            # A pure sine has no harmonics to limit, so no table is needed. Retuning keeps
            # the oscillator's phase, so changing the frequency never clicks.
            if voice.oscillator is None or frequency != voice.frequency:
                if voice.oscillator is None:
                    voice.oscillator = Oscillator()
                voice.oscillator.set_frequency(2.0 * math.pi * frequency / self.sample_rate)
                voice.frequency = frequency
            return voice.oscillator.generate(count).imag.astype(np.float64)
        if voice.table is None or frequency != voice.frequency:
            voice.table = band_limited_wavetable(name, frequency, self.max_frequency_hz)
            voice.frequency = frequency
        phases = voice.phase + (frequency / self.sample_rate) * np.arange(count, dtype=np.float64)
        voice.phase = float((phases[-1] + frequency / self.sample_rate) % 1.0)
        return _read_table(voice.table, phases % 1.0)


def _read_table(table: NDArray[np.float32], phases: NDArray[np.float64]) -> NDArray[np.float64]:
    position = phases * len(table)
    index = position.astype(np.int64) % len(table)
    fraction = position - np.floor(position)
    following = (index + 1) % len(table)
    return table[index] * (1.0 - fraction) + table[following] * fraction
