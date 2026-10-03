from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

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

    def process(self, iq: NDArray[np.complex64]) -> NDArray[np.float32]:
        if len(iq) == 0:
            return np.array([], dtype=np.float32)
        audio = self._demodulator.demodulate(iq)
        if not self._started:
            # The very first sample has nothing before it to measure a phase change from.
            self._started = True
            audio = audio[1:]
        return audio


class NfmModulator:
    """Narrowband FM modulator: audio of +/-1 swings the carrier by +/-deviation_hz."""

    def __init__(self, sample_rate: int, deviation_hz: float) -> None:
        self._modulator = FmModulator(float(deviation_hz) / float(sample_rate))

    def process(self, audio: NDArray[np.float32]) -> NDArray[np.complex64]:
        return self._modulator.modulate(np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0))


def float_to_s16(samples: NDArray[np.float32]) -> bytes:
    pcm = np.clip(samples * PCM_SCALE, -32768, 32767).astype("<i2")
    return pcm.tobytes()
