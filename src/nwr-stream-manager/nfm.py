"""Narrowband FM, through liquid-dsp. Audio is float32 and I/Q float32 I then Q, in any
buffer going in, and array.array("f") coming out."""

from __future__ import annotations

import array
from typing import Any

from . import native
from .liquid_dsp import FmDemodulator, FmModulator


PCM_SCALE = 32768.0
NFM_DEVIATION_GAIN = 1.5


class NfmDemodulator:
    """Narrowband FM demodulator for every receive path."""

    def __init__(self) -> None:
        self._demodulator = FmDemodulator(NFM_DEVIATION_GAIN)
        self._started = False

    def reset(self) -> None:
        """Forget the previous sample, e.g. after retuning, so no phase jump is heard."""
        self._demodulator.reset()
        self._started = False

    def process(self, iq: Any) -> array.array:
        if memoryview(iq).nbytes < 8:
            return array.array("f")
        audio = self._demodulator.demodulate(iq)
        if not self._started:
            # The very first sample has nothing before it to measure a phase change from.
            self._started = True
            del audio[0]
        return audio


class NfmModulator:
    """Narrowband FM modulator: audio of +/-1 swings the carrier by +/-deviation_hz."""

    def __init__(self, sample_rate: int, deviation_hz: float) -> None:
        self._modulator = FmModulator(float(deviation_hz) / float(sample_rate))

    def process(self, audio: Any) -> array.array:
        clipped = native.floats(memoryview(audio).nbytes // 4)
        native.f32_clip(audio, clipped, -1.0, 1.0)
        return self._modulator.modulate(clipped)


def float_to_s16(samples: Any) -> bytes:
    """Float32 audio as 16-bit samples: scaled by 32768, clipped, then truncated."""
    return native.float_to_s16(samples, PCM_SCALE, truncate=True)
