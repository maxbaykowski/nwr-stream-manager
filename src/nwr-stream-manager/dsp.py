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
DEFAULT_DC_BLOCK_TIME_CONSTANT_SECONDS = 1.0
STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE = 96_000.0
STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE = 192_000.0


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


@dataclass
class IqDcBlocker:
    sample_rate: int
    time_constant_seconds: float = DEFAULT_DC_BLOCK_TIME_CONSTANT_SECONDS
    _mean: np.complex64 = np.complex64(0.0)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.time_constant_seconds <= 0.0:
            raise ValueError("time_constant_seconds must be greater than 0")

    def process(self, samples: ComplexArray) -> ComplexArray:
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        samples = samples.astype(np.complex64, copy=False)
        block_average = np.complex128(np.mean(samples, dtype=np.complex128))
        decay = math.exp(-samples.size / (float(self.sample_rate) * self.time_constant_seconds))
        output = (samples - self._mean).astype(np.complex64, copy=False)
        self._mean = np.complex64(block_average + (np.complex128(self._mean) - block_average) * decay)
        return output


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


def _project_fir_windows(
    windows: NDArray[np.complex64],
    reversed_taps: FloatArray,
) -> ComplexArray:
    return np.sum(windows * reversed_taps, axis=1, dtype=np.complex64).astype(
        np.complex64,
        copy=False,
    )


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
    _oscillator_cache_key: tuple[int, float] | None = None
    _oscillator_cache: ComplexArray | None = None

    def process(self, samples: ComplexArray) -> ComplexArray:
        if samples.size == 0 or self.offset_hz == 0.0:
            return samples.astype(np.complex64, copy=False)
        step = 2.0 * math.pi * self.offset_hz / float(self.sample_rate)
        cache_key = (int(samples.size), float(step))
        oscillator = self._oscillator_cache if self._oscillator_cache_key == cache_key else None
        if oscillator is None:
            phases = step * np.arange(samples.size, dtype=np.float32)
            oscillator = np.exp(1j * phases).astype(np.complex64)
            self._oscillator_cache_key = cache_key
            self._oscillator_cache = oscillator
        phase_rotation = np.complex64(np.exp(1j * self._phase))
        shifted = samples * (phase_rotation * oscillator)
        self._phase = float((self._phase + step * samples.size) % (2.0 * math.pi))
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

    def project(self, samples: ComplexArray, local_indices: NDArray[np.int64]) -> ComplexArray:
        samples = samples.astype(np.complex64, copy=False)
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        work = np.concatenate((self._history, samples))
        keep = self.taps.size - 1
        self._history = work[-keep:].copy() if keep else np.array([], dtype=np.complex64)
        if local_indices.size == 0:
            return np.array([], dtype=np.complex64)
        windows = np.lib.stride_tricks.sliding_window_view(work, self.taps.size)[local_indices]
        return _project_fir_windows(windows, self.taps[::-1])


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
        samples = samples.astype(np.complex64, copy=False)
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        start = self._filtered_samples_seen
        end = start + int(samples.size)
        first_position = start + ((-start) % self.factor)
        positions = np.arange(first_position, end, self.factor, dtype=np.int64)
        local_indices = positions - start
        decimated = self.fir.project(samples, local_indices)
        self._filtered_samples_seen = end
        return decimated.astype(np.complex64, copy=False)


@dataclass
class IdentityDecimator:
    @property
    def is_integer_decimation(self) -> bool:
        return True

    def process(self, samples: ComplexArray) -> ComplexArray:
        return samples.astype(np.complex64, copy=False)


@dataclass
class RationalResampler:
    input_rate: float
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB
    fir: FirFilter = field(init=False)
    ratio: Fraction = field(init=False)
    _input_samples_seen: int = 0
    _next_output_position: float = 0.0
    _history: ComplexArray = field(init=False)

    def __post_init__(self) -> None:
        if self.input_rate <= 0 or self.output_rate <= 0:
            raise ValueError("input_rate and output_rate must be greater than 0")
        self.ratio = Fraction(float(self.output_rate) / float(self.input_rate)).limit_denominator(1_000_000)
        taps = design_alias_filter_taps(
            self.input_rate,
            self.output_rate,
            transition_hz=self.transition_hz,
            attenuation_db=self.attenuation_db,
        )
        self.fir = FirFilter(taps)
        self._history = np.zeros(self.fir.taps.size, dtype=np.complex64)

    @property
    def is_integer_decimation(self) -> bool:
        return False

    def process(self, samples: ComplexArray) -> ComplexArray:
        samples = samples.astype(np.complex64, copy=False)
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        work = np.concatenate((self._history, samples))
        work_start = self._input_samples_seen - 1
        work_end = self._input_samples_seen + int(samples.size)
        max_position = work_end - 1
        step = float(self.input_rate) / float(self.output_rate)
        position = self._next_output_position
        if position < work_start:
            missed = math.ceil((work_start - position) / step)
            position += missed * step

        if position >= max_position:
            self._next_output_position = position
            self._input_samples_seen = work_end
            self._history = work[-self.fir.taps.size :].copy()
            return np.array([], dtype=np.complex64)

        output_count = int(math.ceil((max_position - position) / step))
        positions = position + step * np.arange(output_count, dtype=np.float64)
        self._next_output_position = float(position + step * output_count)
        self._input_samples_seen = work_end
        self._history = work[-self.fir.taps.size :].copy()

        local_positions = positions - float(work_start)
        indices = np.floor(local_positions).astype(np.int64)
        fractions = (local_positions - indices).astype(np.float32)
        windows = np.lib.stride_tricks.sliding_window_view(work, self.fir.taps.size)
        reversed_taps = self.fir.taps[::-1]
        left = _project_fir_windows(windows[indices], reversed_taps)
        right = _project_fir_windows(windows[indices + 1], reversed_taps)
        return (left + (right - left) * fractions).astype(np.complex64, copy=False)


@dataclass
class StagedDecimator:
    input_rate: int
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB
    first_stage: IntegerDecimator = field(init=False)
    final_stage: IntegerDecimator | RationalResampler = field(init=False)
    intermediate_rate: float = field(init=False)

    def __post_init__(self) -> None:
        if self.input_rate <= 0 or self.output_rate <= 0:
            raise ValueError("input_rate and output_rate must be greater than 0")
        factor = self._choose_first_stage_factor(self.input_rate, self.output_rate)
        self.intermediate_rate = float(self.input_rate) / float(factor)
        self.first_stage = IntegerDecimator(
            factor=factor,
            fir=FirFilter(self._design_first_stage_taps(self.input_rate, self.intermediate_rate)),
        )
        if _is_integer_multiple(self.intermediate_rate, self.output_rate):
            final_factor = int(round(self.intermediate_rate / self.output_rate))
            self.final_stage = IntegerDecimator(
                factor=final_factor,
                fir=FirFilter(
                    design_alias_filter_taps(
                        self.intermediate_rate,
                        self.output_rate,
                        transition_hz=self.transition_hz,
                        attenuation_db=self.attenuation_db,
                    )
                ),
            )
        else:
            self.final_stage = RationalResampler(
                self.intermediate_rate,
                self.output_rate,
                transition_hz=self.transition_hz,
                attenuation_db=self.attenuation_db,
            )

    @property
    def is_integer_decimation(self) -> bool:
        return self.first_stage.is_integer_decimation and self.final_stage.is_integer_decimation

    def process(self, samples: ComplexArray) -> ComplexArray:
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        return self.final_stage.process(self.first_stage.process(samples))

    @staticmethod
    def _choose_first_stage_factor(input_rate: int, output_rate: int) -> int:
        if input_rate <= STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
            return 1
        target = max(1, int(input_rate // STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE))
        best_factor = 1
        best_score = float("inf")
        best_integer_factor = 1
        best_integer_score = float("inf")
        for factor in range(1, target + 1):
            intermediate = float(input_rate) / float(factor)
            if intermediate < STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE:
                continue
            if intermediate > STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
                score = intermediate - STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE
            else:
                score = abs(intermediate - ((STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE + STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE) / 2.0))
                if _is_integer_multiple(intermediate, output_rate) and score < best_integer_score:
                    best_integer_score = score
                    best_integer_factor = factor
            if score < best_score:
                best_score = score
                best_factor = factor
        if best_integer_score < float("inf"):
            return max(1, best_integer_factor)
        return max(1, best_factor)

    @staticmethod
    def _design_first_stage_taps(input_rate: int, intermediate_rate: float) -> FloatArray:
        if intermediate_rate >= input_rate:
            return np.array([1.0], dtype=np.float32)
        intermediate_nyquist = intermediate_rate / 2.0
        cutoff = min(20_000.0, intermediate_nyquist * 0.45)
        cutoff = max(14_000.0, cutoff)
        transition = max(8_000.0, intermediate_nyquist - cutoff)
        if cutoff + transition >= intermediate_nyquist:
            transition = max(1_000.0, intermediate_nyquist - cutoff)
        return design_lowpass_taps(
            input_rate,
            cutoff,
            transition,
            attenuation_db=60.0,
        )


def _is_integer_multiple(input_rate: float, output_rate: int) -> bool:
    ratio = float(input_rate) / float(output_rate)
    return abs(ratio - round(ratio)) < 1e-9


def create_decimator(
    input_rate: int,
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
    *,
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
) -> IdentityDecimator | IntegerDecimator | RationalResampler | StagedDecimator:
    if input_rate <= 0 or output_rate <= 0:
        raise ValueError("input_rate and output_rate must be greater than 0")
    if input_rate < output_rate:
        raise ValueError("input_rate must be greater than or equal to output_rate")
    if input_rate == output_rate:
        return IdentityDecimator()
    if output_rate >= STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
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
    if input_rate <= STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
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
    return StagedDecimator(
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
    decimator: IntegerDecimator | RationalResampler | StagedDecimator = field(init=False)

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
