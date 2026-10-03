from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from numpy.typing import NDArray

from .liquid_dsp import AudioFirFilter


# The receive curve as levels in dB relative to 1 kHz, joined smoothly on a log-frequency axis.
# Tune by ear here: each row is a frequency in Hz and its level in dB.
NWR_DEEMPHASIS_VOICING: tuple[tuple[float, float], ...] = (
    (20.0, 8.5),
    (100.0, 8.5),
    (200.0, 9.2),
    (250.0, 9.3),
    (300.0, 9.0),
    (400.0, 7.7),
    (500.0, 6.0),
    (600.0, 4.6),
    (700.0, 3.2),
    (850.0, 1.4),
    (1000.0, 0.0),
    (1250.0, -1.3),
    (1600.0, -2.9),
    (2000.0, -4.4),
    (2500.0, -6.2),
    (3000.0, -8.2),
    (3500.0, -10.5),
    (4000.0, -13.2),
    (5000.0, -19.3),
    (6000.0, -25.1),
    (8000.0, -34.6),
    (12000.0, -48.0),
)
NWR_DEEMPHASIS_1KHZ_DB = -12.1
NWR_DEEMPHASIS_TAPS = 257
# Every audio path hands the effects 20 ms frames, 480 samples at 24 kHz, so the FFT
# filter works on exactly one frame at a time and adds no delay.
DEEMPHASIS_BLOCK_SAMPLES = 480


@dataclass
class DeemphasisFilter:
    sample_rate: int
    tau: float
    curve: NDArray[np.float32] = field(init=False)
    _fir: AudioFirFilter | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        self._update_curve(generate_deemphasis_curve(self.sample_rate, self.tau))

    @property
    def enabled(self) -> bool:
        return self.tau > 0

    def process_float(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        return self._filter(samples)

    def update_tau(self, tau: float) -> None:
        self.tau = float(tau)
        self._update_curve(generate_deemphasis_curve(self.sample_rate, self.tau))

    def _filter(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        if not self.enabled or self._fir is None:
            return samples
        return self._fir.process(samples)

    def _update_curve(self, curve: NDArray[np.float32]) -> None:
        self.curve = np.asarray(curve, dtype=np.float32)
        # Turning de-emphasis back on starts the filter from silence, as before.
        self._fir = AudioFirFilter(self.curve, DEEMPHASIS_BLOCK_SAMPLES) if self.curve.size > 1 else None


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

    NWR transmit audio is pre-emphasized at +6 dB/octave from 300 Hz through
    3000 Hz, and a receiver applies the inverse. A plain inverse slope sounds
    muffled, so the curve is voiced from NWR_DEEMPHASIS_VOICING instead.

    Above 1 kHz the slope steepens steadily, from about -4 dB/octave through the
    speech band to about -20 dB/octave well above 3 kHz, where the channel holds
    mostly demodulated hiss. It must not flatten out and then drop away: a level
    stretch ending in a steep roll-off is heard as a peak at its edge. Nor may it
    fall and then shelve back up. The levels are joined with a monotone cubic on
    a log-frequency axis, so the curve never overshoots between table rows.
    """
    if sample_rate <= 0:
        raise ValueError("sample_rate must be greater than 0")
    if taps < 3:
        raise ValueError("taps must be at least 3")
    taps = int(taps)
    if taps % 2 == 0:
        taps += 1

    nfft = 1
    while nfft < taps * 16:
        nfft *= 2
    frequencies = np.fft.rfftfreq(nfft, d=1.0 / float(sample_rate))
    table_hz = np.log2([hz for hz, _level in NWR_DEEMPHASIS_VOICING])
    table_db = np.array([level for _hz, level in NWR_DEEMPHASIS_VOICING])
    levels_db = _monotone_cubic(table_hz, table_db, np.log2(np.maximum(frequencies, 1.0)))
    response = np.power(10.0, (levels_db + NWR_DEEMPHASIS_1KHZ_DB) / 20.0)

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


def _monotone_cubic(
    x: NDArray[np.float64],
    y: NDArray[np.float64],
    points: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Fritsch-Carlson monotone cubic interpolation, held flat past either end."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    widths = np.diff(x)
    slopes = np.diff(y) / widths
    tangents = np.empty_like(y)
    tangents[0] = slopes[0]
    tangents[-1] = slopes[-1]
    for i in range(1, len(x) - 1):
        if slopes[i - 1] * slopes[i] <= 0.0:
            tangents[i] = 0.0
        else:
            before = 2.0 * widths[i] + widths[i - 1]
            after = widths[i] + 2.0 * widths[i - 1]
            tangents[i] = (before + after) / (before / slopes[i - 1] + after / slopes[i])
    points = np.clip(points, x[0], x[-1])
    index = np.clip(np.searchsorted(x, points) - 1, 0, len(x) - 2)
    width = widths[index]
    t = (points - x[index]) / width
    return (
        (2 * t**3 - 3 * t**2 + 1) * y[index]
        + (t**3 - 2 * t**2 + t) * width * tangents[index]
        + (-2 * t**3 + 3 * t**2) * y[index + 1]
        + (t**3 - t**2) * width * tangents[index + 1]
    )
