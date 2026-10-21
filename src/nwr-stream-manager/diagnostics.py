"""RTL-SDR diagnostics: signal levels and spectra of raw and channel radio data."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .config import IQ_SAMPLE_RATE
from .constants import (
    ALIAS_FILTER_STRENGTH_DEFAULT,
    CHANNEL_IQ_ALIAS_TRANSITION_HZ,
    NWR_RECEIVER_CHANNELS_HZ,
    RTL_DIAGNOSTIC_CHANNEL_FFT_SIZE,
    RTL_DIAGNOSTIC_RAW_FFT_SIZE,
)
from .dsp import ComplexArray, DEFAULT_ALIAS_ATTENUATION_DB, IqChannelizer, IqDcBlocker
from .iq_pipeline import alias_filter_attenuation_db, alias_filter_transition_hz, receiver_frequency_mhz
from .nfm import NfmDemodulator


def rms_float(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    values = samples.astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(values * values)))


def rms_complex(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    magnitudes = np.abs(samples.astype(np.complex64, copy=False)).astype(np.float64, copy=False)
    return float(np.sqrt(np.mean(magnitudes * magnitudes)))


def peak_float(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 0.0
    return float(np.max(np.abs(samples)))


def raw_iq_diagnostics(iq: ComplexArray, sample_rate: int, center_frequency_hz: int, raw: bytes = b"") -> dict[str, Any]:
    if iq.size == 0:
        return {
            "sample_count": 0,
            "raw_byte_min": None,
            "raw_byte_max": None,
            "raw_byte_mean": None,
            "raw_byte_stddev": None,
            "dc_magnitude": 0.0,
            "rms": 0.0,
            "peak_magnitude": 0.0,
            "real_near_full_scale_fraction": 0.0,
            "imag_near_full_scale_fraction": 0.0,
            "channels": [],
        }
    sample_count = min(int(iq.size), RTL_DIAGNOSTIC_RAW_FFT_SIZE)
    window_iq = iq[-sample_count:].astype(np.complex64, copy=False)
    spectrum_window = np.hanning(sample_count).astype(np.float32)
    spectrum = np.fft.fftshift(np.fft.fft(window_iq * spectrum_window))
    frequencies = np.fft.fftshift(np.fft.fftfreq(sample_count, d=1.0 / float(sample_rate)))
    power = (np.abs(spectrum) ** 2).astype(np.float64, copy=False)
    floor = float(np.median(power)) if power.size else 0.0
    channels = []
    for frequency_hz in NWR_RECEIVER_CHANNELS_HZ:
        offset_hz = float(frequency_hz - center_frequency_hz)
        nearby = np.abs(frequencies - offset_hz) <= 6_000.0
        local_power = float(np.max(power[nearby])) if np.any(nearby) else 0.0
        channels.append(
            {
                "frequency_hz": frequency_hz,
                "frequency_mhz": receiver_frequency_mhz(frequency_hz),
                "offset_hz": offset_hz,
                "raw_power_db": power_db(local_power),
                "raw_snr_db": power_db(local_power / floor) if floor > 0.0 else None,
            }
        )
    raw_bytes = np.frombuffer(raw, dtype=np.uint8) if raw else np.array([], dtype=np.uint8)
    return {
        "sample_count": int(iq.size),
        "analysis_sample_count": sample_count,
        "raw_byte_min": int(raw_bytes.min()) if raw_bytes.size else None,
        "raw_byte_max": int(raw_bytes.max()) if raw_bytes.size else None,
        "raw_byte_mean": float(raw_bytes.mean()) if raw_bytes.size else None,
        "raw_byte_stddev": float(raw_bytes.std()) if raw_bytes.size else None,
        "dc_magnitude": float(abs(np.mean(iq, dtype=np.complex128))),
        "rms": rms_complex(iq),
        "peak_magnitude": float(np.max(np.abs(iq))),
        "real_near_full_scale_fraction": float(np.mean(np.abs(iq.real) > 0.98)),
        "imag_near_full_scale_fraction": float(np.mean(np.abs(iq.imag) > 0.98)),
        "channels": channels,
    }


def channel_audio_diagnostics(
    iq: ComplexArray,
    sample_rate: int,
    center_frequency_hz: int,
    frequency_hz: int,
    alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT,
) -> dict[str, Any]:
    dc_blocker = IqDcBlocker(sample_rate)
    channelizer = IqChannelizer(
        input_rate=sample_rate,
        center_frequency_hz=center_frequency_hz,
        target_frequency_hz=frequency_hz,
        output_rate=IQ_SAMPLE_RATE,
        transition_hz=alias_filter_transition_hz(CHANNEL_IQ_ALIAS_TRANSITION_HZ, alias_filter_strength),
        alias_attenuation_db=alias_filter_attenuation_db(
            DEFAULT_ALIAS_ATTENUATION_DB,
            alias_filter_strength,
        ),
    )
    demodulator = NfmDemodulator()
    channel_iq = channelizer.process_complex(dc_blocker.process(iq))
    audio = demodulator.process(channel_iq)
    tone_peak_hz = None
    if audio.size >= 64:
        count = min(int(audio.size), RTL_DIAGNOSTIC_CHANNEL_FFT_SIZE)
        audio_window = audio[-count:] * np.hanning(count).astype(np.float32)
        spectrum = np.fft.rfft(audio_window)
        frequencies = np.fft.rfftfreq(count, d=1.0 / float(IQ_SAMPLE_RATE))
        if spectrum.size > 1:
            tone_peak_hz = float(frequencies[int(np.argmax(np.abs(spectrum[1:])) + 1)])
    return {
        "frequency_hz": frequency_hz,
        "frequency_mhz": receiver_frequency_mhz(frequency_hz),
        "offset_hz": float(frequency_hz - center_frequency_hz),
        "channelizer_mode": channelizer.mode,
        "channel_iq_samples": int(channel_iq.size),
        "channel_iq_rms": rms_complex(channel_iq),
        "channel_iq_peak": float(np.max(np.abs(channel_iq))) if channel_iq.size else 0.0,
        "audio_samples": int(audio.size),
        "audio_rms": rms_float(audio),
        "audio_peak": peak_float(audio),
        "audio_peak_hz": tone_peak_hz,
    }


def power_db(value: float) -> float | None:
    if value <= 0.0 or not math.isfinite(value):
        return None
    return float(10.0 * math.log10(value))
