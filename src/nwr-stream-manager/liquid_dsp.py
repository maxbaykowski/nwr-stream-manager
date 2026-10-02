"""Filtering through liquid-dsp, which does the heavy per-sample work in optimized C.

Calls through ctypes release Python's global interpreter lock, so streams can filter on
separate CPU cores at the same time.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import math
import threading

import numpy as np
from numpy.typing import NDArray

LIQUID_SONAMES = ("libliquid.so.1", "libliquid.so.2", "libliquid.so")
# liquid-dsp's precise oscillator (LIQUID_VCO); the default table-based one adds
# spurious tones only 60-70 dB down.
LIQUID_PRECISE_OSCILLATOR = 1
# Recent input kept so new filter coefficients can start from the same history.
FIR_DECIMATOR_HISTORY_SAMPLES = 2048

class _FloatComplex(ctypes.Structure):
    """C99 `float complex`, which liquid-dsp takes by value for scalar arguments."""

    _fields_ = [("real", ctypes.c_float), ("imag", ctypes.c_float)]


_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()
# liquid-dsp's FFT filters plan their FFTs with FFTW where it is available, and FFTW's
# planner is not thread-safe: streams starting together on separate threads crashed the
# whole process. Creating and destroying them happens one at a time; running them is
# safe in parallel. Re-entrant, because garbage collection can destroy one filter while
# the same thread is creating another.
_fft_plan_lock = threading.RLock()


def liquid_library() -> ctypes.CDLL:
    global _library
    with _library_lock:
        if _library is not None:
            return _library
        names = [name for name in (ctypes.util.find_library("liquid"), *LIQUID_SONAMES) if name]
        errors = []
        for name in names:
            try:
                library = ctypes.CDLL(name)
                break
            except OSError as exc:
                errors.append(str(exc))
        else:
            raise OSError("liquid-dsp (libliquid) is not installed: " + "; ".join(errors))
        pointer = ctypes.c_void_p
        library.firdecim_crcf_create.restype = pointer
        library.firdecim_crcf_create.argtypes = [ctypes.c_uint, pointer, ctypes.c_uint]
        library.firdecim_crcf_execute_block.restype = ctypes.c_int
        library.firdecim_crcf_execute_block.argtypes = [pointer, pointer, ctypes.c_uint, pointer]
        library.firdecim_crcf_destroy.restype = ctypes.c_int
        library.firdecim_crcf_destroy.argtypes = [pointer]
        library.dotprod_crcf_create.restype = pointer
        library.dotprod_crcf_create.argtypes = [pointer, ctypes.c_uint]
        library.dotprod_crcf_execute.restype = ctypes.c_int
        library.dotprod_crcf_execute.argtypes = [pointer, pointer, pointer]
        library.dotprod_crcf_run4.restype = ctypes.c_int
        library.dotprod_crcf_run4.argtypes = [pointer, pointer, ctypes.c_uint, pointer]
        library.dotprod_crcf_destroy.restype = ctypes.c_int
        library.dotprod_crcf_destroy.argtypes = [pointer]
        library.iirfilt_rrrf_create_dc_blocker.restype = pointer
        library.iirfilt_rrrf_create_dc_blocker.argtypes = [ctypes.c_float]
        library.iirfilt_rrrf_set_scale.restype = ctypes.c_int
        library.iirfilt_rrrf_set_scale.argtypes = [pointer, ctypes.c_float]
        library.iirfilt_rrrf_execute_block.restype = ctypes.c_int
        library.iirfilt_rrrf_execute_block.argtypes = [pointer, pointer, ctypes.c_uint, pointer]
        library.iirfilt_rrrf_destroy.restype = ctypes.c_int
        library.iirfilt_rrrf_destroy.argtypes = [pointer]
        library.nco_crcf_create.restype = pointer
        library.nco_crcf_create.argtypes = [ctypes.c_int]
        library.nco_crcf_set_frequency.restype = ctypes.c_int
        library.nco_crcf_set_frequency.argtypes = [pointer, ctypes.c_float]
        library.nco_crcf_get_phase.restype = ctypes.c_float
        library.nco_crcf_get_phase.argtypes = [pointer]
        library.nco_crcf_set_phase.restype = ctypes.c_int
        library.nco_crcf_set_phase.argtypes = [pointer, ctypes.c_float]
        library.nco_crcf_mix_block_up.restype = ctypes.c_int
        library.nco_crcf_mix_block_up.argtypes = [pointer, pointer, pointer, ctypes.c_uint]
        library.nco_crcf_destroy.restype = ctypes.c_int
        library.nco_crcf_destroy.argtypes = [pointer]
        library.freqmod_create.restype = pointer
        library.freqmod_create.argtypes = [ctypes.c_float]
        library.freqmod_modulate_block.restype = ctypes.c_int
        library.freqmod_modulate_block.argtypes = [pointer, pointer, ctypes.c_uint, pointer]
        library.freqmod_destroy.restype = ctypes.c_int
        library.freqmod_destroy.argtypes = [pointer]
        library.freqdem_create.restype = pointer
        library.freqdem_create.argtypes = [ctypes.c_float]
        library.freqdem_reset.restype = ctypes.c_int
        library.freqdem_reset.argtypes = [pointer]
        library.freqdem_demodulate_block.restype = ctypes.c_int
        library.freqdem_demodulate_block.argtypes = [pointer, pointer, ctypes.c_uint, pointer]
        library.freqdem_destroy.restype = ctypes.c_int
        library.freqdem_destroy.argtypes = [pointer]
        library.fftfilt_rrrf_create.restype = pointer
        library.fftfilt_rrrf_create.argtypes = [pointer, ctypes.c_uint, ctypes.c_uint]
        library.fftfilt_rrrf_reset.restype = ctypes.c_int
        library.fftfilt_rrrf_reset.argtypes = [pointer]
        library.fftfilt_rrrf_execute.restype = ctypes.c_int
        library.fftfilt_rrrf_execute.argtypes = [pointer, pointer, pointer]
        library.fftfilt_rrrf_destroy.restype = ctypes.c_int
        library.fftfilt_rrrf_destroy.argtypes = [pointer]
        library.liquid_vectorcf_addscalar.restype = None
        library.liquid_vectorcf_addscalar.argtypes = [pointer, ctypes.c_uint, _FloatComplex, pointer]
        _library = library
        return library


class FirDecimator:
    """Low-pass filter complex samples and keep every `factor`-th output.

    Equivalent to filtering with `taps` and then keeping filtered samples 0, factor,
    2 * factor, ... counted from the very first input sample, across any chunk sizes.
    """

    def __init__(self, factor: int, taps: NDArray[np.float32]) -> None:
        factor = int(factor)
        if factor < 2:
            raise ValueError("liquid-dsp decimation factor must be at least 2")
        self.factor = factor
        self._library = liquid_library()
        self._lock = threading.Lock()
        self._handle: int | None = None
        self.taps = np.zeros(0, dtype=np.float32)
        self._create(taps)
        # liquid-dsp outputs on the first sample of each group of `factor`, so its outputs
        # already line up with filtered samples 0, factor, 2 * factor, ...
        self._pending = np.zeros(0, dtype=np.complex64)
        self._recent = np.zeros(0, dtype=np.complex64)

    def _create(self, taps: NDArray[np.float32]) -> None:
        taps = np.ascontiguousarray(taps, dtype=np.float32)
        if taps.ndim != 1 or taps.size == 0:
            raise ValueError("FIR taps must be a non-empty one-dimensional array")
        handle = self._library.firdecim_crcf_create(self.factor, taps.ctypes.data, taps.size)
        if not handle:
            raise RuntimeError("liquid-dsp could not create the decimating filter")
        self.taps = taps  # liquid-dsp copies the taps, but keep them for inspection
        self._handle = handle

    def process(self, samples: NDArray[np.complex64]) -> NDArray[np.complex64]:
        samples = np.asarray(samples, dtype=np.complex64)
        if samples.size == 0:
            return np.array([], dtype=np.complex64)
        with self._lock:
            work = np.concatenate((self._pending, samples)) if self._pending.size else np.ascontiguousarray(samples)
            usable = work.size - (work.size % self.factor)
            output = np.empty(usable // self.factor, dtype=np.complex64)
            if output.size:
                self._library.firdecim_crcf_execute_block(
                    self._handle, work.ctypes.data, output.size, output.ctypes.data
                )
                self._remember(work[:usable])
            self._pending = work[usable:].copy()
            return output

    def update_taps(self, taps: NDArray[np.float32]) -> None:
        """Switch to new coefficients without restarting from silence."""
        with self._lock:
            old_handle = self._handle
            self._create(taps)
            if self._recent.size:
                # Whole groups of `factor`, so the decimation phase carries on unchanged.
                discard = np.empty(self._recent.size // self.factor, dtype=np.complex64)
                self._library.firdecim_crcf_execute_block(
                    self._handle, self._recent.ctypes.data, discard.size, discard.ctypes.data
                )
            if old_handle:
                self._library.firdecim_crcf_destroy(old_handle)

    def _remember(self, consumed: NDArray[np.complex64]) -> None:
        keep = int(math.ceil(max(FIR_DECIMATOR_HISTORY_SAMPLES, self.taps.size) / self.factor)) * self.factor
        if consumed.size >= keep:
            self._recent = consumed[-keep:].copy()
        else:
            self._recent = np.concatenate((self._recent, consumed))[-keep:]

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.firdecim_crcf_destroy(handle)
            except Exception:
                pass
            self._handle = None


class IqDcRemover:
    """Remove the receiver's DC offset from complex samples at any sample rate.

    Every block (one millisecond by default), the block's average is measured and the
    running estimate of the offset moves towards it with the given time constant; the
    estimate from before the block is subtracted from it. liquid-dsp does the per-sample
    sums and subtraction; the running estimate is kept in double precision, which keeps
    even a one-second time constant accurate at the RTL-SDR's full sample rate.
    """

    def __init__(self, sample_rate: int, time_constant_seconds: float, block_seconds: float) -> None:
        self.sample_rate = int(sample_rate)
        self.time_constant_seconds = float(time_constant_seconds)
        self.block_size = max(1, int(round(self.sample_rate * float(block_seconds))))
        self.mean = 0j
        self._library = liquid_library()
        self._ones = np.ones(self.block_size, dtype=np.float32)
        self._sum = np.zeros(1, dtype=np.complex64)
        self._full_block_decay = self._decay(self.block_size)
        self._handle = self._library.dotprod_crcf_create(self._ones.ctypes.data, self.block_size)
        if not self._handle:
            raise RuntimeError("liquid-dsp could not create the DC offset averager")

    def _decay(self, samples: int) -> float:
        return math.exp(-samples / (self.sample_rate * self.time_constant_seconds))

    def process(self, samples: NDArray[np.complex64]) -> NDArray[np.complex64]:
        samples = np.ascontiguousarray(samples, dtype=np.complex64)
        output = np.empty_like(samples)
        if samples.size == 0:
            return output
        library = self._library
        item = samples.itemsize
        source, target, total = samples.ctypes.data, output.ctypes.data, self._sum.ctypes.data
        for start in range(0, samples.size, self.block_size):
            count = min(self.block_size, samples.size - start)
            if count == self.block_size:
                library.dotprod_crcf_execute(self._handle, source + start * item, total)
                decay = self._full_block_decay
            else:
                library.dotprod_crcf_run4(self._ones.ctypes.data, source + start * item, count, total)
                decay = self._decay(count)
            average = complex(self._sum[0]) / count
            offset = _FloatComplex(-self.mean.real, -self.mean.imag)
            library.liquid_vectorcf_addscalar(source + start * item, count, offset, target + start * item)
            self.mean = average + (self.mean - average) * decay
        return output

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.dotprod_crcf_destroy(handle)
            except Exception:
                pass
            self._handle = None


class AudioDcBlocker:
    """First-order DC-blocking filter for real audio: y[n] = x[n] - x[n-1] + r * y[n-1]."""

    def __init__(self, coefficient: float) -> None:
        if not 0.0 < coefficient < 1.0:
            raise ValueError("DC blocker coefficient must be between 0 and 1")
        self._library = liquid_library()
        self._handle = self._library.iirfilt_rrrf_create_dc_blocker(1.0 - float(coefficient))
        if not self._handle:
            raise RuntimeError("liquid-dsp could not create the audio DC blocker")
        # liquid-dsp turns the level down very slightly by default; keep it unchanged.
        self._library.iirfilt_rrrf_set_scale(self._handle, 1.0)

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        samples = np.ascontiguousarray(samples, dtype=np.float32)
        output = np.empty_like(samples)
        if samples.size:
            self._library.iirfilt_rrrf_execute_block(
                self._handle, samples.ctypes.data, samples.size, output.ctypes.data
            )
        return output

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.iirfilt_rrrf_destroy(handle)
            except Exception:
                pass
            self._handle = None


class Oscillator:
    """Shift complex samples up in frequency by mixing them with a precise oscillator."""

    def __init__(self, radians_per_sample: float = 0.0) -> None:
        self._library = liquid_library()
        self._lock = threading.Lock()
        self._handle = self._library.nco_crcf_create(LIQUID_PRECISE_OSCILLATOR)
        if not self._handle:
            raise RuntimeError("liquid-dsp could not create the oscillator")
        self.set_frequency(radians_per_sample)

    def set_frequency(self, radians_per_sample: float) -> None:
        with self._lock:
            self._library.nco_crcf_set_frequency(self._handle, float(radians_per_sample))

    @property
    def phase(self) -> float:
        with self._lock:
            return float(self._library.nco_crcf_get_phase(self._handle))

    @phase.setter
    def phase(self, radians: float) -> None:
        with self._lock:
            self._library.nco_crcf_set_phase(self._handle, float(radians))

    def generate(self, count: int) -> NDArray[np.complex64]:
        """The oscillator itself, exp(j * phase), for `count` samples."""
        return self.mix_up(np.ones(max(0, int(count)), dtype=np.complex64))

    def mix_up(self, samples: NDArray[np.complex64]) -> NDArray[np.complex64]:
        samples = np.ascontiguousarray(samples, dtype=np.complex64)
        output = np.empty_like(samples)
        if samples.size:
            with self._lock:
                self._library.nco_crcf_mix_block_up(
                    self._handle, samples.ctypes.data, output.ctypes.data, samples.size
                )
        return output

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.nco_crcf_destroy(handle)
            except Exception:
                pass
            self._handle = None


class FmDemodulator:
    """FM demodulation: the phase change from each sample to the next, times `gain` / pi."""

    def __init__(self, gain: float) -> None:
        self._library = liquid_library()
        # liquid-dsp outputs the phase change divided by 2 * pi * kf.
        self._handle = self._library.freqdem_create(1.0 / (2.0 * float(gain)))
        if not self._handle:
            raise RuntimeError("liquid-dsp could not create the FM demodulator")

    def reset(self) -> None:
        self._library.freqdem_reset(self._handle)

    def demodulate(self, samples: NDArray[np.complex64]) -> NDArray[np.float32]:
        samples = np.ascontiguousarray(samples, dtype=np.complex64)
        output = np.empty(samples.size, dtype=np.float32)
        if samples.size:
            self._library.freqdem_demodulate_block(
                self._handle, samples.ctypes.data, samples.size, output.ctypes.data
            )
        return output

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.freqdem_destroy(handle)
            except Exception:
                pass
            self._handle = None


class FmModulator:
    """FM modulation: each sample advances the phase by 2 * pi * kf * audio."""

    def __init__(self, kf: float) -> None:
        self._library = liquid_library()
        self._handle = self._library.freqmod_create(float(kf))
        if not self._handle:
            raise RuntimeError("liquid-dsp could not create the FM modulator")

    def modulate(self, audio: NDArray[np.float32]) -> NDArray[np.complex64]:
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        output = np.empty(audio.size, dtype=np.complex64)
        if audio.size:
            self._library.freqmod_modulate_block(self._handle, audio.ctypes.data, audio.size, output.ctypes.data)
        return output

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                library.freqmod_destroy(handle)
            except Exception:
                pass
            self._handle = None


class AudioFirFilter:
    """Filter real audio with a long FIR, using liquid-dsp's FFT filter for whole blocks.

    Chunks made of whole `block`-sample blocks, which is what every audio path sends,
    go through the FFT filter. Any other length is filtered directly from the same
    history, so the output is identical whatever lengths arrive, with no added delay.
    """

    def __init__(self, taps: NDArray[np.float32], block: int) -> None:
        taps = np.ascontiguousarray(taps, dtype=np.float32)
        if taps.ndim != 1 or taps.size == 0:
            raise ValueError("FIR taps must be a non-empty one-dimensional array")
        if int(block) < taps.size - 1:
            raise ValueError("FFT filter block must be at least the filter length minus one")
        self.taps = taps
        self.block = int(block)
        self._library = liquid_library()
        with _fft_plan_lock:
            self._handle = self._library.fftfilt_rrrf_create(taps.ctypes.data, taps.size, self.block)
        if not self._handle:
            raise RuntimeError("liquid-dsp could not create the FFT filter")
        self._history = np.zeros(taps.size - 1, dtype=np.float32)
        self._in_step = True  # the FFT filter's own state matches self._history

    def process(self, samples: NDArray[np.float32]) -> NDArray[np.float32]:
        samples = np.ascontiguousarray(samples, dtype=np.float32)
        if samples.size == 0:
            return samples.copy()
        if samples.size % self.block:
            output = np.convolve(np.concatenate((self._history, samples)), self.taps, mode="valid").astype(np.float32)
            self._in_step = False
        else:
            if not self._in_step:
                self._prime()
            output = np.empty_like(samples)
            item = samples.itemsize
            for start in range(0, samples.size, self.block):
                self._library.fftfilt_rrrf_execute(
                    self._handle, samples.ctypes.data + start * item, output.ctypes.data + start * item
                )
        self._remember(samples)
        return output

    def _prime(self) -> None:
        # Run one block ending in the remembered input, so the FFT filter carries on
        # from exactly where direct filtering left off.
        primer = np.zeros(self.block, dtype=np.float32)
        if self._history.size:
            primer[-self._history.size :] = self._history
        discard = np.empty_like(primer)
        self._library.fftfilt_rrrf_reset(self._handle)
        self._library.fftfilt_rrrf_execute(self._handle, primer.ctypes.data, discard.ctypes.data)
        self._in_step = True

    def _remember(self, samples: NDArray[np.float32]) -> None:
        keep = self._history.size
        if not keep:
            return
        if samples.size >= keep:
            self._history = samples[-keep:].copy()
        else:
            self._history = np.concatenate((self._history[samples.size :], samples))

    def __del__(self) -> None:
        handle = getattr(self, "_handle", None)
        library = getattr(self, "_library", None)
        if handle and library is not None:
            try:
                with _fft_plan_lock:
                    library.fftfilt_rrrf_destroy(handle)
            except Exception:
                pass
            self._handle = None
