from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction

import numpy as np
from numpy.typing import NDArray


ComplexArray = NDArray[np.complex64]
FloatArray = NDArray[np.float32]


DEFAULT_OUTPUT_SAMPLE_RATE = 24_000
DEFAULT_ALIAS_TRANSITION_HZ = 2_000.0
DEFAULT_ALIAS_ATTENUATION_DB = 80.0


def rtl_u8_to_complex64(chunk: bytes | bytearray | memoryview) -> ComplexArray:
    """Convert RTL-SDR unsigned 8-bit interleaved IQ bytes to complex float32."""
    usable = len(chunk) - (len(chunk) % 2)
    if usable <= 0:
        return np.array([], dtype=np.complex64)
    raw = np.frombuffer(memoryview(chunk)[:usable], dtype=np.uint8).astype(np.float32)
    centered = (raw - 127.5) / 127.5
    iq = centered[0::2].astype(np.complex64, copy=False)
    iq = iq + 1j * centered[1::2].astype(np.complex64, copy=False)
    return iq.astype(np.complex64, copy=False)


def complex64_to_interleaved_f32(samples: ComplexArray) -> bytes:
    interleaved = np.empty(samples.size * 2, dtype="<f4")
    interleaved[0::2] = samples.real
    interleaved[1::2] = samples.imag
    return interleaved.tobytes()


def design_lowpass_taps(
    sample_rate: int,
    cutoff_hz: float,
    transition_hz: float,
    *,
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    min_taps: int = 63,
    max_taps: int = 65_535,
) -> FloatArray:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    nyquist = sample_rate / 2.0
    if not 0.0 < cutoff_hz < nyquist:
        raise ValueError("cutoff_hz must be between 0 and Nyquist")
    if transition_hz <= 0.0:
        raise ValueError("transition_hz must be greater than 0")

    transition_radians = 2.0 * math.pi * transition_hz / float(sample_rate)
    taps = int(math.ceil((attenuation_db - 8.0) / (2.285 * transition_radians)))
    taps = max(min_taps, min(max_taps, taps))
    if taps % 2 == 0:
        taps += 1
    n = np.arange(taps, dtype=np.float64) - (taps - 1) / 2.0
    normalized_cutoff = cutoff_hz / float(sample_rate)
    response = 2.0 * normalized_cutoff * np.sinc(2.0 * normalized_cutoff * n)
    window = np.kaiser(taps, _kaiser_beta(attenuation_db))
    response *= window
    response /= np.sum(response)
    return response.astype(np.float32)


def _kaiser_beta(attenuation_db: float) -> float:
    if attenuation_db > 50.0:
        return 0.1102 * (attenuation_db - 8.7)
    if attenuation_db >= 21.0:
        return 0.5842 * (attenuation_db - 21.0) ** 0.4 + 0.07886 * (
            attenuation_db - 21.0
        )
    return 0.0


def design_alias_filter_taps(
    input_rate: int,
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
    *,
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
) -> FloatArray:
    output_nyquist = output_rate / 2.0
    cutoff = output_nyquist - transition_hz
    if cutoff <= 0.0:
        raise ValueError("transition_hz must be smaller than output Nyquist")
    return design_lowpass_taps(
        input_rate,
        cutoff,
        transition_hz,
        attenuation_db=attenuation_db,
    )


@dataclass
class FrequencyShifter:
    sample_rate: int
    offset_hz: float
    _phase: float = 0.0

    def process(self, samples: ComplexArray) -> ComplexArray:
        if samples.size == 0 or self.offset_hz == 0.0:
            return samples.astype(np.complex64, copy=False)
        step = 2.0 * math.pi * self.offset_hz / float(self.sample_rate)
        phases = self._phase + step * np.arange(samples.size, dtype=np.float32)
        self._phase = float((phases[-1] + step) % (2.0 * math.pi))
        shifted = samples * np.exp(1j * phases).astype(np.complex64)
        return shifted.astype(np.complex64, copy=False)


@dataclass
class FirFilter:
    taps: FloatArray
    _history: ComplexArray = field(init=False)

    def __post_init__(self) -> None:
        taps = np.asarray(self.taps, dtype=np.float32)
        if taps.ndim != 1 or taps.size == 0:
            raise ValueError("FIR taps must be a non-empty one-dimensional array")
        self.taps = taps
        self._history = np.zeros(taps.size - 1, dtype=np.complex64)

    def process(self, samples: ComplexArray) -> ComplexArray:
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        work = np.concatenate((self._history, samples.astype(np.complex64, copy=False)))
        filtered = np.convolve(work, self.taps, mode="valid").astype(np.complex64)
        keep = self.taps.size - 1
        self._history = work[-keep:].copy() if keep else np.array([], dtype=np.complex64)
        return filtered


@dataclass
class IntegerDecimator:
    factor: int
    fir: FirFilter
    _filtered_samples_seen: int = 0

    @classmethod
    def create(
        cls,
        input_rate: int,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        *,
        transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
        attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    ) -> "IntegerDecimator":
        if input_rate % output_rate != 0:
            raise ValueError("input_rate is not an integer multiple of output_rate")
        taps = design_alias_filter_taps(
            input_rate,
            output_rate,
            transition_hz=transition_hz,
            attenuation_db=attenuation_db,
        )
        return cls(input_rate // output_rate, FirFilter(taps))

    @property
    def is_integer_decimation(self) -> bool:
        return True

    def process(self, samples: ComplexArray) -> ComplexArray:
        filtered = self.fir.process(samples)
        if filtered.size == 0:
            return filtered
        offset = (-self._filtered_samples_seen) % self.factor
        decimated = filtered[offset :: self.factor]
        self._filtered_samples_seen += int(filtered.size)
        return decimated.astype(np.complex64, copy=False)


@dataclass
class RationalResampler:
    input_rate: int
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB
    fir: FirFilter = field(init=False)
    ratio: Fraction = field(init=False)
    _filtered_samples_seen: int = 0
    _next_output_position: float = 0.0
    _tail: ComplexArray = field(default_factory=lambda: np.array([], dtype=np.complex64))

    def __post_init__(self) -> None:
        if self.input_rate <= 0 or self.output_rate <= 0:
            raise ValueError("input_rate and output_rate must be greater than 0")
        self.ratio = Fraction(self.output_rate, self.input_rate)
        taps = design_alias_filter_taps(
            self.input_rate,
            self.output_rate,
            transition_hz=self.transition_hz,
            attenuation_db=self.attenuation_db,
        )
        self.fir = FirFilter(taps)

    @property
    def is_integer_decimation(self) -> bool:
        return False

    def process(self, samples: ComplexArray) -> ComplexArray:
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        filtered = self.fir.process(samples)
        if filtered.size == 0:
            return filtered
        work = (
            np.concatenate((self._tail, filtered))
            if self._tail.size
            else filtered
        )
        work_start = self._filtered_samples_seen - int(self._tail.size)
        work_end = self._filtered_samples_seen + int(filtered.size)
        max_position = work_end - 1
        step = float(self.input_rate) / float(self.output_rate)
        position = self._next_output_position
        if position < work_start:
            missed = math.ceil((work_start - position) / step)
            position += missed * step

        positions: list[float] = []
        while position < max_position:
            positions.append(position)
            position += step

        self._next_output_position = position
        self._filtered_samples_seen += int(filtered.size)
        self._tail = work[-1:].copy()
        if not positions:
            return np.array([], dtype=np.complex64)

        local_positions = np.asarray(positions, dtype=np.float64) - float(work_start)
        indices = np.floor(local_positions).astype(np.int64)
        fractions = (local_positions - indices).astype(np.float32)
        left = work[indices]
        right = work[indices + 1]
        return (left + (right - left) * fractions).astype(np.complex64, copy=False)


def create_decimator(
    input_rate: int,
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
    *,
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
) -> IntegerDecimator | RationalResampler:
    if input_rate <= 0 or output_rate <= 0:
        raise ValueError("input_rate and output_rate must be greater than 0")
    if input_rate < output_rate:
        raise ValueError("input_rate must be greater than or equal to output_rate")
    if input_rate % output_rate == 0:
        return IntegerDecimator.create(
            input_rate,
            output_rate,
            transition_hz=transition_hz,
            attenuation_db=attenuation_db,
        )
    return RationalResampler(
        input_rate,
        output_rate,
        transition_hz=transition_hz,
        attenuation_db=attenuation_db,
    )


@dataclass
class IqChannelizer:
    input_rate: int
    center_frequency_hz: int
    target_frequency_hz: int
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ
    alias_attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB
    shifter: FrequencyShifter = field(init=False)
    decimator: IntegerDecimator | RationalResampler = field(init=False)

    def __post_init__(self) -> None:
        offset = float(self.center_frequency_hz - self.target_frequency_hz)
        self.shifter = FrequencyShifter(self.input_rate, offset)
        self.decimator = create_decimator(
            self.input_rate,
            self.output_rate,
            transition_hz=self.transition_hz,
            attenuation_db=self.alias_attenuation_db,
        )

    @property
    def mode(self) -> str:
        return "integer" if self.decimator.is_integer_decimation else "fractional"

    def process_u8(self, chunk: bytes | bytearray | memoryview) -> ComplexArray:
        return self.process_complex(rtl_u8_to_complex64(chunk))

    def process_complex(self, samples: ComplexArray) -> ComplexArray:
        shifted = self.shifter.process(samples)
        return self.decimator.process(shifted)
