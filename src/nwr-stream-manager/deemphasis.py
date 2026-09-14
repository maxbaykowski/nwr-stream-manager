from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray


PCM_SCALE = 32768.0
NWR_DEEMPHASIS_LOW_HZ = 300.0
NWR_DEEMPHASIS_HIGH_HZ = 3000.0
NWR_DEEMPHASIS_LOW_SHELF_END_HZ = 500.0
NWR_DEEMPHASIS_LOW_SHELF_GAIN = 0.8
NWR_DEEMPHASIS_POST_HIGH_ROLLOFF = 2.5
NWR_DEEMPHASIS_TAPS = 257


@dataclass
class DeemphasisFilter:
    sample_rate: int
    tau: float
    curve: NDArray[np.float32] = field(init=False)
    _history: NDArray[np.float32] = field(init=False)
    _pending_byte: bytes = b""

    def __post_init__(self) -> None:
        self.curve = generate_deemphasis_curve(self.sample_rate, self.tau)
        self._history = np.zeros(max(len(self.curve) - 1, 0), dtype=np.float32)

    @property
    def enabled(self) -> bool:
        return self.tau > 0

    def process(self, chunk: bytes) -> bytes:
        chunk = self._pending_byte + chunk
        if len(chunk) % 2:
            self._pending_byte = chunk[-1:]
            chunk = chunk[:-1]
        else:
            self._pending_byte = b""
        if not chunk:
            return b""

        samples = np.frombuffer(chunk, dtype="<i2").astype(np.float32) / PCM_SCALE
        filtered = self._filter(samples)
        pcm = np.clip(filtered * PCM_SCALE, -32768, 32767).astype("<i2")
        return pcm.tobytes()

    def process_float(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        return self._filter(samples)

    def update_tau(self, tau: float) -> None:
        self.tau = float(tau)
        self._update_curve(generate_deemphasis_curve(self.sample_rate, self.tau))

    def flush(self) -> bytes:
        self._pending_byte = b""
        return b""

    def _filter(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if not self.enabled:
            return samples
        window = np.concatenate((self._history, samples))
        filtered = np.convolve(window, self.curve, mode="full")
        start = len(self._history)
        stop = start + len(samples)
        self._history = window[-len(self._history) :]
        return filtered[start:stop].astype(np.float32, copy=False)

    def _update_curve(self, curve: NDArray[np.float32]) -> None:
        curve = np.asarray(curve, dtype=np.float32)
        keep = max(len(curve) - 1, 0)
        if keep <= 0:
            history = np.array([], dtype=np.float32)
        elif len(self._history) >= keep:
            history = self._history[-keep:].copy()
        else:
            history = np.zeros(keep, dtype=np.float32)
            if len(self._history):
                history[-len(self._history) :] = self._history
        self.curve = curve
        self._history = history


def generate_deemphasis_curve(sample_rate: int, tau: float) -> NDArray[np.float32]:
    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    if tau <= 0:
        return np.array([1.0], dtype=np.float32)
    return generate_nwr_deemphasis_curve(sample_rate)


def generate_nwr_deemphasis_curve(
    sample_rate: int,
    *,
    taps: int = NWR_DEEMPHASIS_TAPS,
) -> NDArray[np.float32]:
    """Return the fixed NOAA Weather Radio receive de-emphasis FIR.

    NWR transmit audio is pre-emphasized at +6 dB/octave from 300 Hz
    through 3000 Hz. The receive side applies the inverse -6 dB/octave
    curve over the same range, with unity gain below 300 Hz.

    A practical weather-radio receiver still needs to keep demodulated
    wideband hiss from sitting on a flat shelf above 3000 Hz. Above the
    specified pre-emphasis range, continue with a gentle noise taper
    instead of a sharp lowpass so upper speech detail remains audible.
    A shallow low shelf keeps the 250-350 Hz region from sounding too
    forward without removing the low-frequency body entirely. Overall
    gain is normalized conservatively to avoid introducing clipping.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    if taps < 3:
        raise ValueError("taps must be at least 3")
    taps = int(taps)
    if taps % 2 == 0:
        taps += 1
    nyquist = sample_rate / 2.0
    if nyquist <= NWR_DEEMPHASIS_LOW_HZ:
        return np.array([1.0], dtype=np.float32)

    nfft = 1
    while nfft < taps * 16:
        nfft *= 2
    frequencies = np.fft.rfftfreq(nfft, d=1.0 / float(sample_rate))
    high_hz = min(NWR_DEEMPHASIS_HIGH_HZ, nyquist)
    high_gain = NWR_DEEMPHASIS_LOW_HZ / high_hz
    response = np.ones_like(frequencies, dtype=np.float64)
    sloped = (frequencies > NWR_DEEMPHASIS_LOW_HZ) & (frequencies < high_hz)
    response[sloped] = NWR_DEEMPHASIS_LOW_HZ / frequencies[sloped]
    low_shelf = frequencies < NWR_DEEMPHASIS_LOW_SHELF_END_HZ
    if np.any(low_shelf):
        shelf_progress = np.clip(
            frequencies[low_shelf] / NWR_DEEMPHASIS_LOW_SHELF_END_HZ,
            0.0,
            1.0,
        )
        smooth = shelf_progress * shelf_progress * (3.0 - 2.0 * shelf_progress)
        response[low_shelf] *= (
            NWR_DEEMPHASIS_LOW_SHELF_GAIN
            + (1.0 - NWR_DEEMPHASIS_LOW_SHELF_GAIN) * smooth
        )
    above_high = frequencies >= high_hz
    if np.any(above_high):
        post_high_ratio = np.maximum(frequencies[above_high], high_hz) / high_hz
        response[above_high] = high_gain / np.power(
            post_high_ratio,
            NWR_DEEMPHASIS_POST_HIGH_ROLLOFF,
        )

    impulse = np.fft.irfft(response, n=nfft)
    centered = np.fft.fftshift(impulse)
    start = (nfft - taps) // 2
    kernel = centered[start : start + taps].copy()
    kernel *= np.hamming(taps)
    kernel_response = np.abs(np.fft.rfft(kernel, n=nfft))
    peak = float(np.max(kernel_response)) if len(kernel_response) else 0.0
    if peak > 1.0:
        kernel /= peak
    return kernel.astype(np.float32)
