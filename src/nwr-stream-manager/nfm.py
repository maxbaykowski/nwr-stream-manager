from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from .liquid_dsp import FmDemodulator


PCM_SCALE = 32768.0
NFM_DEVIATION_GAIN = 1.5
IQ_BYTES_PER_SAMPLE = {"f32": 8, "s16": 4}


class NfmDemodulator:
    """Narrowband FM demodulator for every receive path.

    `process` takes complex samples; `process_bytes` takes raw interleaved I/Q from a
    file or pipe in `iq_format` ("f32" or "s16"), even when a sample is split across reads.
    """

    def __init__(self, iq_format: str = "f32") -> None:
        if iq_format not in IQ_BYTES_PER_SAMPLE:
            raise ValueError(f"unsupported IQ format: {iq_format}")
        self.iq_format = iq_format
        self._demodulator = FmDemodulator(NFM_DEVIATION_GAIN)
        self._pending_bytes = b""
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

    def process_bytes(self, chunk: bytes) -> NDArray[np.float32]:
        chunk = self._pending_bytes + chunk
        bytes_per_iq_sample = IQ_BYTES_PER_SAMPLE[self.iq_format]
        aligned_size = len(chunk) - (len(chunk) % bytes_per_iq_sample)
        self._pending_bytes = chunk[aligned_size:]
        if not aligned_size:
            return np.array([], dtype=np.float32)
        return self.process(_iq_bytes_to_complex64(chunk[:aligned_size], self.iq_format))


def _iq_bytes_to_complex64(chunk: bytes, iq_format: str) -> NDArray[np.complex64]:
    if iq_format == "f32":
        iq_float = np.frombuffer(chunk, dtype="<f4")
        iq = iq_float[0::2].astype(np.complex64)
        iq += 1j * iq_float[1::2].astype(np.complex64)
        return iq
    if iq_format == "s16":
        iq_int = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
        iq_float = iq_int / PCM_SCALE
        iq = iq_float[0::2].astype(np.complex64)
        iq += 1j * iq_float[1::2].astype(np.complex64)
        return iq
    raise ValueError(f"unsupported IQ format: {iq_format}")


def float_to_s16(samples: NDArray[np.float32]) -> bytes:
    pcm = np.clip(samples * PCM_SCALE, -32768, 32767).astype("<i2")
    return pcm.tobytes()
