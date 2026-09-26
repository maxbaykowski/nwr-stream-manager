"""Channel signal strength and noise floor measurement from channelized IQ.

The meter runs on the per-stream complex baseband produced by IqChannelizer
(24 kS/s, channel centred on 0 Hz). Every block it computes a Welch power
spectrum and splits it into:

* the signal band, |f| <= channel_half_bandwidth_hz (Carson bandwidth of an
  NWR carrier: 5 kHz deviation + ~3 kHz audio), and
* two noise reference bands in the channel guard space on either side of the
  signal band, inside the channelizer's flat passband.

The noise density is the trimmed mean of the reference bins (narrowband spurs
rejected), using whichever side is quieter so off-frequency carriers or
one-sided interference do not inflate it. Because FM sidebands can reach the
guard band during heavy modulation, the reported floor is a low percentile of
the per-block estimates over a sliding window (minimum statistics).

Signal power is the signal-band power minus the noise expected in that band.
All levels are dBFS where a full-scale complex exponential (|x| = 1) is 0 dBFS.

Reception quality is graded from the SNR in the channel bandwidth. NBFM hits
its threshold around 10 dB, so "poor" starts just above it; "bad reception"
is flagged once the SNR (or a missing carrier) stays below the bad threshold
for a sustained period, and clears with some hysteresis.

SNR is inherently invariant to RTL-SDR gain: gain scales the signal and the
noise together, so their ratio does not move once both sides of the meter
have settled. It does NOT stay accurate through the moment gain changes,
though. The noise floor's minimum-statistics window (SIGNAL_METER_NOISE_
WINDOW_SECONDS) holds several seconds of pre-change noise samples, so a
step change in gain biases the SNR reading for several seconds afterward:
a gain increase makes the old, now-too-quiet noise samples linger near the
bottom of the window, understating the floor and overstating SNR by up to
~10 dB for ~10 seconds; a gain decrease has the opposite, smaller effect.
Callers that can see gain change (IcecastStreamWorker.gain_provider) must
call reset() (or set_noise_reference(), which also resets) at the moment
it does. That covers a deliberate manual gain change or toggling the RTL's
hardware/tuner AGC on or off; it does not cover the RTL's own AGC quietly
drifting gain on its own, since the compat RTL-SDR wrapper this project
falls back to cannot read the live gain back from the device to detect
that (and even where pyrtlsdr can, polling it from another thread while a
zero-copy async read is in progress on the same device is a real-hardware
risk not worth taking for a receiver an EAS alerting pipeline depends on).
In practice this residual drift is self-limiting (the window ages out
within SIGNAL_METER_NOISE_WINDOW_SECONDS) and skews toward overstating
reception, not understating it, so it will not cause a false "bad
reception" notification; it can delay a true one by a few seconds.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import numpy as np

from .dsp import ComplexArray


SIGNAL_METER_FFT_SIZE = 1024
SIGNAL_METER_BLOCK_SECONDS = 0.25
SIGNAL_METER_CHANNEL_HALF_BANDWIDTH_HZ = 8_000.0
SIGNAL_METER_NOISE_LOW_HZ = 9_000.0
SIGNAL_METER_NOISE_HIGH_HZ = 11_000.0
SIGNAL_METER_MIN_NOISE_WIDTH_HZ = 500.0
SIGNAL_METER_NOISE_WINDOW_SECONDS = 10.0
SIGNAL_METER_NOISE_PERCENTILE = 10.0
SIGNAL_METER_SPUR_REJECT_RATIO = 3.0
SIGNAL_METER_SMOOTHING_SECONDS = 1.0
SIGNAL_METER_CARRIER_DETECT_SNR_DB = 6.0
SIGNAL_METER_STALE_SECONDS = 3.0

SIGNAL_QUALITY_EXCELLENT = "excellent"
SIGNAL_QUALITY_GOOD = "good"
SIGNAL_QUALITY_FAIR = "fair"
SIGNAL_QUALITY_POOR = "poor"
SIGNAL_QUALITY_EXCELLENT_SNR_DB = 30.0
SIGNAL_QUALITY_GOOD_SNR_DB = 20.0
SIGNAL_QUALITY_FAIR_SNR_DB = 12.0
BAD_RECEPTION_SNR_DB = 6.0
BAD_RECEPTION_RECOVERY_SNR_DB = 9.0
BAD_RECEPTION_SUSTAIN_SECONDS = 20.0


def signal_quality(snr_db: float | None) -> str:
    if snr_db is None or snr_db < SIGNAL_QUALITY_FAIR_SNR_DB:
        return SIGNAL_QUALITY_POOR
    if snr_db < SIGNAL_QUALITY_GOOD_SNR_DB:
        return SIGNAL_QUALITY_FAIR
    if snr_db < SIGNAL_QUALITY_EXCELLENT_SNR_DB:
        return SIGNAL_QUALITY_GOOD
    return SIGNAL_QUALITY_EXCELLENT


@dataclass(frozen=True)
class SignalMeasurement:
    channel_power_dbfs: float | None
    noise_floor_dbfs: float | None
    noise_density_dbfs_per_hz: float | None
    signal_dbfs: float | None
    snr_db: float | None
    cn0_db_hz: float | None
    carrier_detected: bool
    channel_bandwidth_hz: float
    measured_at: float
    quality: str = SIGNAL_QUALITY_POOR
    bad_reception: bool = False


def noise_reference_band_for_transition(
    sample_rate: int,
    transition_hz: float,
    *,
    low_hz: float = SIGNAL_METER_NOISE_LOW_HZ,
    high_hz: float = SIGNAL_METER_NOISE_HIGH_HZ,
) -> tuple[float, float]:
    """Keep the noise reference inside the channelizer's flat passband.

    The channel alias filter's cutoff sits at Nyquist - transition/2, so its
    passband ends at roughly Nyquist - transition.
    """
    passband_edge_hz = float(sample_rate) / 2.0 - float(transition_hz)
    high = min(float(high_hz), passband_edge_hz)
    return float(low_hz), max(float(low_hz) + SIGNAL_METER_MIN_NOISE_WIDTH_HZ, high)


def _power_dbfs(value: float) -> float | None:
    if value <= 0.0 or not math.isfinite(value):
        return None
    return 10.0 * math.log10(value)


def _trimmed_mean(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    median = float(np.median(values))
    kept = values[values <= median * SIGNAL_METER_SPUR_REJECT_RATIO]
    return float(np.mean(kept)) if kept.size else median


class ChannelSignalMeter:
    def __init__(
        self,
        sample_rate: int,
        *,
        channel_half_bandwidth_hz: float = SIGNAL_METER_CHANNEL_HALF_BANDWIDTH_HZ,
        noise_low_hz: float = SIGNAL_METER_NOISE_LOW_HZ,
        noise_high_hz: float = SIGNAL_METER_NOISE_HIGH_HZ,
        fft_size: int = SIGNAL_METER_FFT_SIZE,
        block_seconds: float = SIGNAL_METER_BLOCK_SECONDS,
        noise_window_seconds: float = SIGNAL_METER_NOISE_WINDOW_SECONDS,
        smoothing_seconds: float = SIGNAL_METER_SMOOTHING_SECONDS,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than 0")
        nyquist = float(sample_rate) / 2.0
        if not 0.0 < channel_half_bandwidth_hz < noise_low_hz < noise_high_hz <= nyquist:
            raise ValueError("signal band and noise reference band must be ordered inside Nyquist")
        self.sample_rate = int(sample_rate)
        self.fft_size = int(fft_size)
        self.block_samples = max(self.fft_size, int(round(block_seconds * self.sample_rate)))
        self.channel_half_bandwidth_hz = float(channel_half_bandwidth_hz)
        self._window = np.hanning(self.fft_size).astype(np.float32)
        self._window_power = float(np.sum(self._window.astype(np.float64) ** 2))
        self._frequencies = np.fft.fftfreq(self.fft_size, d=1.0 / float(self.sample_rate))
        self._bin_width_hz = float(self.sample_rate) / float(self.fft_size)
        self._signal_mask = np.abs(self._frequencies) <= self.channel_half_bandwidth_hz
        self._signal_bins = int(np.count_nonzero(self._signal_mask))
        self._noise_history: deque[float] = deque(
            maxlen=max(1, int(round(noise_window_seconds / (self.block_samples / self.sample_rate))))
        )
        self._smoothing_alpha = 1.0 - math.exp(-(self.block_samples / self.sample_rate) / max(1e-6, smoothing_seconds))
        self._pending: list[ComplexArray] = []
        self._pending_count = 0
        self._smoothed_band_power: float | None = None
        self._stream_seconds = 0.0
        self._bad_since: float | None = None
        self._bad_reception = False
        self._measurement: SignalMeasurement | None = None
        self._lock = threading.Lock()
        self.set_noise_reference(noise_low_hz, noise_high_hz)

    @property
    def channel_bandwidth_hz(self) -> float:
        return self._signal_bins * self._bin_width_hz

    def set_noise_reference(self, low_hz: float, high_hz: float) -> None:
        if not self.channel_half_bandwidth_hz < low_hz < high_hz <= self.sample_rate / 2.0:
            raise ValueError("noise reference band must sit between the signal band and Nyquist")
        magnitudes = np.abs(self._frequencies)
        in_band = (magnitudes >= low_hz) & (magnitudes <= high_hz)
        self._noise_lower_mask = in_band & (self._frequencies < 0.0)
        self._noise_upper_mask = in_band & (self._frequencies > 0.0)
        self.noise_low_hz = float(low_hz)
        self.noise_high_hz = float(high_hz)
        self._noise_history.clear()

    def reset(self) -> None:
        self._pending = []
        self._pending_count = 0
        self._noise_history.clear()
        self._smoothed_band_power = None
        self._stream_seconds = 0.0
        self._bad_since = None
        self._bad_reception = False
        with self._lock:
            self._measurement = None

    def process(self, samples: ComplexArray) -> None:
        if samples.size == 0:
            return
        self._pending.append(np.asarray(samples, dtype=np.complex64))
        self._pending_count += int(samples.size)
        if self._pending_count < self.block_samples:
            return
        work = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        blocks = work.size // self.block_samples
        for index in range(blocks):
            start = index * self.block_samples
            self._analyze_block(work[start : start + self.block_samples])
        remainder = work[blocks * self.block_samples :]
        self._pending = [remainder.copy()] if remainder.size else []
        self._pending_count = int(remainder.size)

    def measurement(self) -> SignalMeasurement | None:
        with self._lock:
            return self._measurement

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        measurement = self.measurement()
        if measurement is None:
            return {"available": False}
        now = time.time() if now is None else now
        age = max(0.0, now - measurement.measured_at)
        return {
            "available": age <= SIGNAL_METER_STALE_SECONDS,
            "channel_power_dbfs": _rounded(measurement.channel_power_dbfs),
            "noise_floor_dbfs": _rounded(measurement.noise_floor_dbfs),
            "noise_density_dbfs_per_hz": _rounded(measurement.noise_density_dbfs_per_hz),
            "signal_dbfs": _rounded(measurement.signal_dbfs),
            "snr_db": _rounded(measurement.snr_db),
            "cn0_db_hz": _rounded(measurement.cn0_db_hz),
            "carrier_detected": measurement.carrier_detected,
            "channel_bandwidth_hz": round(measurement.channel_bandwidth_hz, 1),
            "quality": measurement.quality,
            "bad_reception": measurement.bad_reception and age <= SIGNAL_METER_STALE_SECONDS,
            "measured_at": measurement.measured_at,
            "age_seconds": round(age, 3),
        }

    def _analyze_block(self, block: ComplexArray) -> None:
        hop = self.fft_size // 2
        segments = 1 + (block.size - self.fft_size) // hop
        stride = block.strides[0]
        frames = np.lib.stride_tricks.as_strided(
            block,
            shape=(segments, self.fft_size),
            strides=(stride * hop, stride),
            writeable=False,
        )
        spectrum = np.fft.fft(frames * self._window, axis=1)
        # Per-bin power normalised so the bins sum to the mean sample power.
        bin_power = np.mean(np.abs(spectrum) ** 2, axis=0) / (self.fft_size * self._window_power)

        band_power = float(np.sum(bin_power[self._signal_mask]))
        noise_per_bin = min(
            _trimmed_mean(bin_power[self._noise_lower_mask]),
            _trimmed_mean(bin_power[self._noise_upper_mask]),
        )
        self._noise_history.append(noise_per_bin)
        noise_floor_per_bin = float(np.percentile(np.fromiter(self._noise_history, dtype=np.float64), SIGNAL_METER_NOISE_PERCENTILE))

        if self._smoothed_band_power is None:
            self._smoothed_band_power = band_power
        else:
            self._smoothed_band_power += self._smoothing_alpha * (band_power - self._smoothed_band_power)
        smoothed_band_power = self._smoothed_band_power

        noise_in_band = noise_floor_per_bin * self._signal_bins
        noise_density = noise_floor_per_bin / self._bin_width_hz
        signal_power = smoothed_band_power - noise_in_band
        snr_db = _power_dbfs(signal_power / noise_in_band) if noise_in_band > 0.0 and signal_power > 0.0 else None
        self._stream_seconds += block.size / self.sample_rate
        bad_reception = self._update_bad_reception(snr_db)
        measurement = SignalMeasurement(
            channel_power_dbfs=_power_dbfs(smoothed_band_power),
            noise_floor_dbfs=_power_dbfs(noise_in_band),
            noise_density_dbfs_per_hz=_power_dbfs(noise_density),
            signal_dbfs=_power_dbfs(signal_power),
            snr_db=snr_db,
            cn0_db_hz=_power_dbfs(signal_power / noise_density) if noise_density > 0.0 and signal_power > 0.0 else None,
            carrier_detected=snr_db is not None and snr_db >= SIGNAL_METER_CARRIER_DETECT_SNR_DB,
            channel_bandwidth_hz=self.channel_bandwidth_hz,
            measured_at=time.time(),
            quality=signal_quality(snr_db),
            bad_reception=bad_reception,
        )
        with self._lock:
            self._measurement = measurement

    def _update_bad_reception(self, snr_db: float | None) -> bool:
        # Timed on processed IQ rather than wall clock so gaps in the source
        # never count toward the sustain period.
        below = snr_db is None or snr_db < BAD_RECEPTION_SNR_DB
        if self._bad_reception:
            if snr_db is not None and snr_db >= BAD_RECEPTION_RECOVERY_SNR_DB:
                self._bad_reception = False
                self._bad_since = None
            return self._bad_reception
        if not below:
            self._bad_since = None
            return False
        if self._bad_since is None:
            self._bad_since = self._stream_seconds
        if self._stream_seconds - self._bad_since >= BAD_RECEPTION_SUSTAIN_SECONDS:
            self._bad_reception = True
        return self._bad_reception


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 2)
