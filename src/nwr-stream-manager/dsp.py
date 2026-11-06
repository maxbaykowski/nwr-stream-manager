"""The receiver's DSP: moving a station to the centre, decimating it to the channel rate,
and the filters for that, through liquid-dsp and NWR Stream Manager's own native library.

I/Q is float32 I then Q, and audio float32, in any buffer going in (array.array, memoryview,
bytes, or still a numpy array from code not yet moved off numpy); both come out as
array.array("f").
"""

from __future__ import annotations

import array
import math
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from . import native
from .liquid_dsp import (
    FirDecimator,
    FirFilter as _LiquidFirFilter,
    IqDcRemover,
    Oscillator,
    float_taps,
    kaiser_lowpass,
    kaiser_window,
)


# Float32 I then Q, and float32 audio.
ComplexArray = array.array
FloatArray = array.array


DEFAULT_OUTPUT_SAMPLE_RATE = 24_000
DEFAULT_ALIAS_TRANSITION_HZ = 1_000.0
DEFAULT_ALIAS_ATTENUATION_DB = 80.0
WIDE_DECIMATOR_MIN_TRANSITION_HZ = 8_000.0
WIDE_DECIMATOR_ALIAS_ATTENUATION_DB = 70.0
DEFAULT_DC_BLOCK_TIME_CONSTANT_SECONDS = 1.0
DEFAULT_DC_BLOCK_MAX_BLOCK_SECONDS = 0.001
STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE = 96_000.0
STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE = 192_000.0
RATIONAL_INTERPOLATOR_TAPS = 16
RATIONAL_INTERPOLATOR_ATTENUATION_DB = 60.0
FAST_INTERMEDIATE_INPUT_RATE = 1_536_000
FAST_INTERMEDIATE_OUTPUT_RATE = 192_000
FAST_INTERMEDIATE_HALFBAND_TAPS = 15


def iq_count(samples: Any) -> int:
    """How many I/Q pairs float32 I/Q holds."""
    return memoryview(samples).nbytes // 8


def iq_array(samples: Any) -> array.array:
    """Float32 I/Q as an array.array("f"): itself if it already is one, otherwise a copy."""
    if isinstance(samples, array.array) and samples.typecode == "f":
        return samples
    return native.float_samples(samples)


def rtl_u8_to_iq(chunk: bytes | bytearray | memoryview) -> array.array:
    """RTL-SDR unsigned 8-bit I/Q bytes as float32 I then Q, from -1 to 1."""
    pairs = len(chunk) // 2
    iq = native.floats(2 * pairs)
    if pairs:
        native.u8_iq_to_f32(chunk, iq, pairs)
    return iq


def iq_samples(data: Any) -> array.array:
    """A batch's I/Q as float32 I then Q: bytes are the RTL-SDR's own 8-bit samples;
    anything else is already float32 I/Q."""
    if isinstance(data, (bytes, bytearray)) or (isinstance(data, memoryview) and data.format in ("B", "b", "c")):
        return rtl_u8_to_iq(data)
    return iq_array(data)


def iq_bytes(samples: Any) -> bytes:
    """Float32 I/Q as bytes, I then Q, as .cf32 files hold it."""
    view = memoryview(iq_array(samples))
    return view.cast("B").tobytes() if view.nbytes else b""


@dataclass
class IqDcBlocker:
    sample_rate: int
    time_constant_seconds: float = DEFAULT_DC_BLOCK_TIME_CONSTANT_SECONDS
    max_block_seconds: float = DEFAULT_DC_BLOCK_MAX_BLOCK_SECONDS
    _remover: IqDcRemover = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        if self.time_constant_seconds <= 0.0:
            raise ValueError("time_constant_seconds must be greater than 0")
        if self.max_block_seconds <= 0.0:
            raise ValueError("max_block_seconds must be greater than 0")
        self._remover = IqDcRemover(self.sample_rate, self.time_constant_seconds, self.max_block_seconds)

    def process(self, samples: Any) -> ComplexArray:
        return self._remover.process(samples)


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
    # liquid-dsp's Kaiser-windowed sinc, with the same Kaiser window for the attenuation as
    # _kaiser_beta, scaled to pass 0 Hz unchanged.
    return _unity_gain(kaiser_lowpass(taps, cutoff_hz / float(sample_rate), attenuation_db))


def _unity_gain(taps: array.array) -> array.array:
    total = math.fsum(taps)
    return array.array("f", (value / total for value in taps))


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
    cutoff = output_nyquist - (transition_hz / 2.0)
    if cutoff <= 0.0:
        raise ValueError("transition_hz must be smaller than output Nyquist")
    return design_lowpass_taps(
        input_rate,
        cutoff,
        transition_hz,
        attenuation_db=attenuation_db,
    )


class FrequencyShifter:
    """Move the signal at -offset_hz to 0 Hz by mixing it with an oscillator."""

    def __init__(self, sample_rate: int, offset_hz: float) -> None:
        self.sample_rate = int(sample_rate)
        self._oscillator = Oscillator()
        self.offset_hz = offset_hz

    @property
    def offset_hz(self) -> float:
        return self._offset_hz

    @offset_hz.setter
    def offset_hz(self, offset_hz: float) -> None:
        # Retuning keeps the oscillator's phase, so the shift carries on without a click.
        self._offset_hz = float(offset_hz)
        self._oscillator.set_frequency(2.0 * math.pi * self._offset_hz / float(self.sample_rate))

    @property
    def phase(self) -> float:
        return self._oscillator.phase

    @phase.setter
    def phase(self, radians: float) -> None:
        self._oscillator.phase = radians

    def process(self, samples: Any) -> ComplexArray:
        if self._offset_hz == 0.0 or iq_count(samples) == 0:
            return iq_array(samples)
        return self._oscillator.mix_up(samples)


class FirFilter(_LiquidFirFilter):
    """Filter complex samples with real taps, through liquid-dsp."""


@dataclass
class IntegerDecimator:
    factor: int
    fir: FirFilter | FirDecimator

    def __post_init__(self) -> None:
        # liquid-dsp filters and decimates in one pass; a factor of 1 is just a filter.
        if int(self.factor) >= 2 and isinstance(self.fir, FirFilter):
            self.fir = FirDecimator(int(self.factor), self.fir.taps)

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

    def process(self, samples: Any) -> ComplexArray:
        if iq_count(samples) == 0:
            return array.array("f")
        return self.fir.process(samples)

    def update_alias_filter(
        self,
        input_rate: int,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        *,
        transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
        attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    ) -> None:
        if input_rate % output_rate != 0:
            raise ValueError("input_rate is not an integer multiple of output_rate")
        factor = input_rate // output_rate
        if factor != self.factor:
            raise ValueError("integer decimation factor cannot change during alias filter update")
        self.fir.update_taps(
            design_alias_filter_taps(
                input_rate,
                output_rate,
                transition_hz=transition_hz,
                attenuation_db=attenuation_db,
            )
        )


def design_halfband_taps(
    taps_count: int = FAST_INTERMEDIATE_HALFBAND_TAPS,
    *,
    attenuation_db: float = 60.0,
) -> FloatArray:
    if taps_count < 7 or taps_count % 4 != 3:
        raise ValueError("half-band FIR tap count must be 4m+3 and at least 7")
    response = [0.5 * value for value in kaiser_lowpass(taps_count, 0.25, attenuation_db)]
    center = taps_count // 2
    response[center] = 0.5
    for index in range(taps_count):
        if index != center and (index - center) % 2 == 0:
            response[index] = 0.0
    total = math.fsum(response)
    return array.array("f", (value / total for value in response))


class HalfBandDecimator2x:
    """Decimate by two with a half-band filter."""

    def __init__(self, taps: FloatArray) -> None:
        self.fir = FirDecimator(2, self._checked(taps))

    @staticmethod
    def _checked(taps: Any) -> FloatArray:
        taps = float_taps(taps)
        if len(taps) < 7 or len(taps) % 4 != 3:
            raise ValueError("half-band FIR taps must be one-dimensional with 4m+3 taps")
        return taps

    @property
    def taps(self) -> FloatArray:
        return self.fir.taps

    @property
    def is_integer_decimation(self) -> bool:
        return True

    def update_taps(self, taps: FloatArray) -> None:
        self.fir.update_taps(self._checked(taps))

    def process(self, samples: Any) -> ComplexArray:
        if iq_count(samples) == 0:
            return array.array("f")
        return self.fir.process(samples)


@dataclass
class FixedFactor8IntermediateDecimator:
    input_rate: int = FAST_INTERMEDIATE_INPUT_RATE
    output_rate: int = FAST_INTERMEDIATE_OUTPUT_RATE
    transition_hz: float = WIDE_DECIMATOR_MIN_TRANSITION_HZ
    attenuation_db: float = WIDE_DECIMATOR_ALIAS_ATTENUATION_DB
    first_stage: HalfBandDecimator2x = field(init=False)
    second_stage: HalfBandDecimator2x = field(init=False)
    final_stage: IntegerDecimator = field(init=False)

    def __post_init__(self) -> None:
        if self.input_rate != FAST_INTERMEDIATE_INPUT_RATE or self.output_rate != FAST_INTERMEDIATE_OUTPUT_RATE:
            raise ValueError("fixed intermediate decimator only supports 1536000 to 192000 S/s")
        halfband = design_halfband_taps(
            FAST_INTERMEDIATE_HALFBAND_TAPS,
            attenuation_db=min(60.0, float(self.attenuation_db)),
        )
        self.first_stage = HalfBandDecimator2x(halfband)
        self.second_stage = HalfBandDecimator2x(halfband)
        self.final_stage = IntegerDecimator(
            factor=2,
            fir=FirFilter(
                design_alias_filter_taps(
                    self.input_rate // 4,
                    self.output_rate,
                    transition_hz=self.transition_hz,
                    attenuation_db=self.attenuation_db,
                )
            ),
        )

    @property
    def is_integer_decimation(self) -> bool:
        return True

    def process(self, samples: Any) -> ComplexArray:
        if iq_count(samples) == 0:
            return array.array("f")
        return self.final_stage.process(self.second_stage.process(self.first_stage.process(samples)))

    def update_alias_filter(
        self,
        input_rate: int,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        *,
        transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
        attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    ) -> None:
        if int(input_rate) != self.input_rate or int(output_rate) != self.output_rate:
            raise ValueError("fixed intermediate decimator rates cannot change during alias filter update")
        self.transition_hz = float(transition_hz)
        self.attenuation_db = float(attenuation_db)
        halfband = design_halfband_taps(
            FAST_INTERMEDIATE_HALFBAND_TAPS,
            attenuation_db=min(60.0, float(attenuation_db)),
        )
        self.first_stage.update_taps(halfband)
        self.second_stage.update_taps(halfband)
        self.final_stage.update_alias_filter(
            self.input_rate // 4,
            self.output_rate,
            transition_hz=self.transition_hz,
            attenuation_db=self.attenuation_db,
        )


@dataclass
class TwoStageChannelDecimator:
    """Decimate one channel by 4 or more in two steps instead of one long filter.

    The first step is a short filter that only has to keep the final channel flat and
    stop anything that would fold back into it; the second, at twice the output rate,
    is the sharp filter that sets the channel edge. It does the same job as a single
    filter at the input rate with roughly a third of the work.
    """

    input_rate: int
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB
    first_stage: IntegerDecimator = field(init=False)
    final_stage: IntegerDecimator = field(init=False)
    intermediate_rate: int = field(init=False)

    def __post_init__(self) -> None:
        if self.input_rate % (self.output_rate * 2) != 0:
            raise ValueError("input_rate must be a multiple of twice output_rate")
        self.intermediate_rate = self.output_rate * 2
        self.first_stage = IntegerDecimator(
            factor=self.input_rate // self.intermediate_rate,
            fir=FirFilter(self._design_first_stage_taps()),
        )
        self.final_stage = IntegerDecimator(factor=2, fir=FirFilter(self._design_final_stage_taps()))

    def _design_first_stage_taps(self) -> FloatArray:
        # Flat up to the output's Nyquist (the channel edge) and fully stopped from where
        # signals would fold back onto it at the intermediate rate; the final stage
        # removes everything in between.
        keep_hz = self.output_rate / 2.0
        stop_hz = self.intermediate_rate - keep_hz
        return design_lowpass_taps(
            self.input_rate,
            (keep_hz + stop_hz) / 2.0,
            stop_hz - keep_hz,
            attenuation_db=self.attenuation_db,
        )

    def _design_final_stage_taps(self) -> FloatArray:
        return design_alias_filter_taps(
            self.intermediate_rate,
            self.output_rate,
            transition_hz=self.transition_hz,
            attenuation_db=self.attenuation_db,
        )

    @property
    def is_integer_decimation(self) -> bool:
        return True

    def process(self, samples: Any) -> ComplexArray:
        if iq_count(samples) == 0:
            return array.array("f")
        return self.final_stage.process(self.first_stage.process(samples))

    def update_alias_filter(
        self,
        input_rate: int,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        *,
        transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
        attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    ) -> None:
        if int(input_rate) != self.input_rate or int(output_rate) != self.output_rate:
            raise ValueError("two-stage decimator rates cannot change during alias filter update")
        self.transition_hz = float(transition_hz)
        self.attenuation_db = float(attenuation_db)
        self.first_stage.fir.update_taps(self._design_first_stage_taps())
        self.final_stage.fir.update_taps(self._design_final_stage_taps())


def _uses_two_stage_channel_decimator(input_rate: int, output_rate: int) -> bool:
    return input_rate % (output_rate * 2) == 0 and input_rate // output_rate >= 4


@dataclass
class IdentityDecimator:
    @property
    def is_integer_decimation(self) -> bool:
        return True

    def process(self, samples: Any) -> ComplexArray:
        return iq_array(samples)


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
    _filtered_tail: ComplexArray = field(init=False)
    _interp_left: int = field(init=False)
    _interp_right: int = field(init=False)
    _interp_window: FloatArray = field(init=False)

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
        self._filtered_tail = array.array("f")
        self._interp_left = (RATIONAL_INTERPOLATOR_TAPS // 2) - 1
        self._interp_right = RATIONAL_INTERPOLATOR_TAPS - self._interp_left - 1
        self._interp_window = kaiser_window(
            RATIONAL_INTERPOLATOR_TAPS,
            _kaiser_beta(RATIONAL_INTERPOLATOR_ATTENUATION_DB),
        )

    @property
    def is_integer_decimation(self) -> bool:
        return False

    def process(self, samples: Any) -> ComplexArray:
        if iq_count(samples) == 0:
            return array.array("f")
        start = self._input_samples_seen
        filtered = self.fir.process(samples)
        end = start + iq_count(filtered)
        work = self._filtered_tail + filtered if self._filtered_tail else filtered
        work_start = start - iq_count(self._filtered_tail)
        left_taps = self._interp_left
        right_taps = self._interp_right
        first_position = work_start + left_taps
        max_position = work_start + iq_count(work) - right_taps
        step = float(self.input_rate) / float(self.output_rate)
        position = self._next_output_position
        if position < first_position:
            missed = math.ceil((first_position - position) / step)
            position += missed * step
        self._input_samples_seen = end
        self._filtered_tail = work[-2 * RATIONAL_INTERPOLATOR_TAPS :]

        if position >= max_position:
            self._next_output_position = position
            return array.array("f")

        output_count = int(math.ceil((max_position - position) / step))
        self._next_output_position = float(position + step * output_count)
        return native.cf32_interpolate(
            work, self._interp_window, left_taps, float(work_start), position, step, output_count
        )

    def update_alias_filter(
        self,
        input_rate: int,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        *,
        transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
        attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    ) -> None:
        if float(input_rate) != float(self.input_rate) or int(output_rate) != int(self.output_rate):
            raise ValueError("rational resampler rates cannot change during alias filter update")
        self.transition_hz = float(transition_hz)
        self.attenuation_db = float(attenuation_db)
        self.fir.update_taps(
            design_alias_filter_taps(
                input_rate,
                output_rate,
                transition_hz=transition_hz,
                attenuation_db=attenuation_db,
            )
        )


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
            fir=FirFilter(
                self._design_first_stage_taps(
                    self.input_rate,
                    self.intermediate_rate,
                    self.output_rate,
                    self.transition_hz,
                    self.attenuation_db,
                )
            ),
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

    def process(self, samples: Any) -> ComplexArray:
        if iq_count(samples) == 0:
            return array.array("f")
        return self.final_stage.process(self.first_stage.process(samples))

    def update_alias_filter(
        self,
        input_rate: int,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        *,
        transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
        attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
    ) -> None:
        if int(input_rate) != int(self.input_rate) or int(output_rate) != int(self.output_rate):
            raise ValueError("staged decimator rates cannot change during alias filter update")
        self.transition_hz = float(transition_hz)
        self.attenuation_db = float(attenuation_db)
        self.first_stage.fir.update_taps(
            self._design_first_stage_taps(
                self.input_rate,
                self.intermediate_rate,
                self.output_rate,
                self.transition_hz,
                self.attenuation_db,
            )
        )
        update_decimator_alias_filter(
            self.final_stage,
            int(round(self.intermediate_rate)),
            output_rate,
            transition_hz=transition_hz,
            attenuation_db=attenuation_db,
        )

    @staticmethod
    def _choose_first_stage_factor(input_rate: int, output_rate: int) -> int:
        min_intermediate = max(STAGED_DECIMATOR_MIN_INTERMEDIATE_RATE, float(output_rate))
        max_intermediate = max(STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE, float(output_rate) * 1.5)
        if input_rate <= max_intermediate:
            return 1
        target = max(1, int(input_rate // min_intermediate))
        best_factor = 1
        best_score = float("inf")
        best_integer_factor = 1
        best_integer_score = float("inf")
        target_intermediate = (min_intermediate + max_intermediate) / 2.0
        for factor in range(1, target + 1):
            intermediate = float(input_rate) / float(factor)
            if intermediate < min_intermediate:
                continue
            if intermediate > max_intermediate:
                score = intermediate - max_intermediate
            else:
                score = abs(intermediate - target_intermediate)
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
    def _design_first_stage_taps(
        input_rate: int,
        intermediate_rate: float,
        output_rate: int,
        requested_transition_hz: float,
        attenuation_db: float = 60.0,
    ) -> FloatArray:
        if intermediate_rate >= input_rate:
            return array.array("f", [1.0])
        intermediate_nyquist = intermediate_rate / 2.0
        output_nyquist = float(output_rate) / 2.0
        if output_rate >= STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
            guard_hz = max(8_000.0, float(requested_transition_hz))
            cutoff = max(1_000.0, output_nyquist - (guard_hz / 2.0))
            cutoff = min(cutoff, intermediate_nyquist * 0.9)
        else:
            cutoff = min(20_000.0, intermediate_nyquist * 0.45)
            cutoff = max(14_000.0, cutoff)
        transition = max(8_000.0, intermediate_nyquist - cutoff)
        if cutoff + transition >= intermediate_nyquist:
            transition = max(1_000.0, intermediate_nyquist - cutoff)
        return design_lowpass_taps(
            input_rate,
            cutoff,
            transition,
            attenuation_db=min(60.0, float(attenuation_db)),
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
) -> IdentityDecimator | IntegerDecimator | RationalResampler | StagedDecimator | FixedFactor8IntermediateDecimator | TwoStageChannelDecimator:
    if input_rate <= 0 or output_rate <= 0:
        raise ValueError("input_rate and output_rate must be greater than 0")
    if input_rate < output_rate:
        raise ValueError("input_rate must be greater than or equal to output_rate")
    if input_rate == output_rate:
        return IdentityDecimator()
    if input_rate == FAST_INTERMEDIATE_INPUT_RATE and output_rate == FAST_INTERMEDIATE_OUTPUT_RATE:
        transition_hz = _wide_decimator_transition_hz(output_rate, transition_hz)
        attenuation_db = min(float(attenuation_db), WIDE_DECIMATOR_ALIAS_ATTENUATION_DB)
        return FixedFactor8IntermediateDecimator(
            input_rate=input_rate,
            output_rate=output_rate,
            transition_hz=transition_hz,
            attenuation_db=attenuation_db,
        )
    if output_rate >= STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
        transition_hz = _wide_decimator_transition_hz(output_rate, transition_hz)
        attenuation_db = min(float(attenuation_db), WIDE_DECIMATOR_ALIAS_ATTENUATION_DB)
        if input_rate % output_rate == 0:
            return IntegerDecimator.create(
                input_rate,
                output_rate,
                transition_hz=transition_hz,
                attenuation_db=attenuation_db,
            )
        if input_rate > output_rate * 2:
            return StagedDecimator(
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
        if _uses_two_stage_channel_decimator(input_rate, output_rate):
            return TwoStageChannelDecimator(
                input_rate,
                output_rate,
                transition_hz=transition_hz,
                attenuation_db=attenuation_db,
            )
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


def update_decimator_alias_filter(
    decimator: IdentityDecimator | IntegerDecimator | RationalResampler | StagedDecimator | FixedFactor8IntermediateDecimator | TwoStageChannelDecimator,
    input_rate: int,
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
    *,
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ,
    attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB,
) -> None:
    if isinstance(decimator, IdentityDecimator):
        return
    if output_rate >= STAGED_DECIMATOR_MAX_INTERMEDIATE_RATE:
        transition_hz = _wide_decimator_transition_hz(output_rate, transition_hz)
        attenuation_db = min(float(attenuation_db), WIDE_DECIMATOR_ALIAS_ATTENUATION_DB)
    decimator.update_alias_filter(
        input_rate,
        output_rate,
        transition_hz=transition_hz,
        attenuation_db=attenuation_db,
    )


def _wide_decimator_transition_hz(output_rate: int, requested_transition_hz: float) -> float:
    nyquist = float(output_rate) / 2.0
    transition = max(float(requested_transition_hz), WIDE_DECIMATOR_MIN_TRANSITION_HZ)
    return min(transition, nyquist * 0.45)


@dataclass
class IqChannelizer:
    input_rate: int
    center_frequency_hz: int
    target_frequency_hz: int
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE
    transition_hz: float = DEFAULT_ALIAS_TRANSITION_HZ
    alias_attenuation_db: float = DEFAULT_ALIAS_ATTENUATION_DB
    shifter: FrequencyShifter = field(init=False)
    decimator: IntegerDecimator | RationalResampler | StagedDecimator | TwoStageChannelDecimator = field(init=False)

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

    def process_complex(self, samples: Any) -> ComplexArray:
        shifted = self.shifter.process(samples)
        return self.decimator.process(shifted)

    def set_target_frequency(self, target_frequency_hz: int | float) -> None:
        self.target_frequency_hz = int(round(float(target_frequency_hz)))
        self.shifter.offset_hz = float(self.center_frequency_hz - self.target_frequency_hz)

    def update_alias_filter(
        self,
        *,
        transition_hz: float,
        attenuation_db: float | None = None,
    ) -> None:
        self.transition_hz = float(transition_hz)
        if attenuation_db is not None:
            self.alias_attenuation_db = float(attenuation_db)
        update_decimator_alias_filter(
            self.decimator,
            self.input_rate,
            self.output_rate,
            transition_hz=self.transition_hz,
            attenuation_db=self.alias_attenuation_db,
        )
