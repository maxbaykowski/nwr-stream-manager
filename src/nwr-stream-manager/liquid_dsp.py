"""Filtering through liquid-dsp, which does the heavy per-sample work in optimized C.

Calls through ctypes release Python's global interpreter lock, so streams can filter on
separate CPU cores at the same time.

Samples go in as any float32 buffer (array.array, memoryview, bytes; complex I/Q as I then
Q) and come out as array.array("f"). Everything liquid-dsp creates is destroyed by a
weakref.finalize once its Python object is gone, even if that's never closed.
"""

from __future__ import annotations

import array
import ctypes
import ctypes.util
import math
import threading
import weakref
from typing import Any, Callable

from . import native

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
        library.firfilt_rrrf_create.restype = pointer
        library.firfilt_rrrf_create.argtypes = [pointer, ctypes.c_uint]
        library.firfilt_rrrf_execute_block.restype = ctypes.c_int
        library.firfilt_rrrf_execute_block.argtypes = [pointer, pointer, ctypes.c_uint, pointer]
        library.firfilt_rrrf_destroy.restype = ctypes.c_int
        library.firfilt_rrrf_destroy.argtypes = [pointer]
        library.liquid_firdes_kaiser.restype = ctypes.c_int
        library.liquid_firdes_kaiser.argtypes = [ctypes.c_uint, ctypes.c_float, ctypes.c_float, ctypes.c_float, pointer]
        library.liquid_kaiser.restype = ctypes.c_float
        library.liquid_kaiser.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_float]
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


def _destroy(destroy: Callable[[int], Any], handle: int, lock: threading.RLock | None) -> None:
    if lock is None:
        destroy(handle)
    else:
        with lock:
            destroy(handle)


class _Owned:
    """Something liquid-dsp created, destroyed once nothing uses it any more."""

    def __init__(self, handle: int | None, destroy: Callable[[int], Any], what: str, lock: threading.RLock | None = None) -> None:
        if not handle:
            raise RuntimeError(f"liquid-dsp could not create {what}")
        self.handle = handle
        self._finalizer = weakref.finalize(self, _destroy, destroy, handle, lock)


def _bytes(samples: Any) -> memoryview:
    """A float32 buffer's bytes, read where they are; anything else is refused."""
    view = memoryview(samples)
    if view.format not in native.FLOAT32 and view.format not in native.RAW:
        raise TypeError(f"expected float32 samples, not {view.format!r}")
    if not view.c_contiguous:
        raise ValueError("samples must be contiguous")
    return view if view.format == "B" and view.ndim == 1 else view.cast("B")


def _floats(data: Any) -> array.array:
    """A copy of float32 samples as an array.array("f")."""
    samples = array.array("f")
    samples.frombytes(_bytes(data))
    return samples


def float_taps(taps: Any) -> array.array:
    """Filter taps as an array.array("f"): from any float32 buffer, or a sequence of numbers."""
    if isinstance(taps, array.array) and taps.typecode == "f":
        result = array.array("f", taps)
    else:
        try:
            result = _floats(taps)
        except TypeError:
            result = array.array("f", (float(value) for value in taps))
    if not result:
        raise ValueError("FIR taps must be a non-empty one-dimensional array")
    return result


def kaiser_lowpass(count: int, cutoff: float, attenuation_db: float) -> array.array:
    """liquid-dsp's Kaiser-windowed sinc low-pass of count taps, cutoff as a fraction of the
    sample rate, not yet scaled."""
    taps = native.floats(count)
    if liquid_library().liquid_firdes_kaiser(count, cutoff, attenuation_db, 0.0, native.writable(taps).address):
        raise ValueError("liquid-dsp could not design the filter")
    return taps


def kaiser_window(count: int, beta: float) -> array.array:
    library = liquid_library()
    return array.array("f", (library.liquid_kaiser(index, count, beta) for index in range(count)))


class FirDecimator:
    """Low-pass filter complex samples and keep every `factor`-th output.

    Equivalent to filtering with `taps` and then keeping filtered samples 0, factor,
    2 * factor, ... counted from the very first input sample, across any chunk sizes.
    """

    def __init__(self, factor: int, taps: Any) -> None:
        factor = int(factor)
        if factor < 2:
            raise ValueError("liquid-dsp decimation factor must be at least 2")
        self.factor = factor
        self._library = liquid_library()
        self._lock = threading.Lock()
        self.taps = array.array("f")
        self._create(taps)
        # liquid-dsp outputs on the first sample of each group of `factor`, so its outputs
        # already line up with filtered samples 0, factor, 2 * factor, ...
        self._pending = array.array("f")
        self._recent = array.array("f")

    def _create(self, taps: Any) -> None:
        taps = float_taps(taps)
        self._filter = _Owned(
            self._library.firdecim_crcf_create(self.factor, native.readable(taps).address, len(taps)),
            self._library.firdecim_crcf_destroy,
            "the decimating filter",
        )
        self.taps = taps  # liquid-dsp copies the taps, but keep them for inspection

    def process(self, samples: Any) -> array.array:
        data = _bytes(samples)
        if not data.nbytes:
            return array.array("f")
        with self._lock:
            if self._pending:
                work: Any = self._pending
                work.frombytes(data)
                data = _bytes(work)
            usable = (data.nbytes // 8) - (data.nbytes // 8) % self.factor
            output = native.floats(2 * (usable // self.factor))
            if output:
                self._library.firdecim_crcf_execute_block(
                    self._filter.handle, native.readable(data).address, len(output) // 2, native.writable(output).address
                )
                self._remember(data[: usable * 8])
            self._pending = _floats(data[usable * 8 :])
            return output

    def update_taps(self, taps: Any) -> None:
        """Switch to new coefficients without restarting from silence."""
        with self._lock:
            self._create(taps)
            if self._recent:
                # Whole groups of `factor`, so the decimation phase carries on unchanged.
                discard = native.floats(2 * (len(self._recent) // 2 // self.factor))
                self._library.firdecim_crcf_execute_block(
                    self._filter.handle, native.readable(self._recent).address, len(discard) // 2,
                    native.writable(discard).address,
                )

    def _remember(self, consumed: memoryview) -> None:
        keep = int(math.ceil(max(FIR_DECIMATOR_HISTORY_SAMPLES, len(self.taps)) / self.factor)) * self.factor
        if consumed.nbytes >= keep * 8:
            self._recent = _floats(consumed[-keep * 8 :])
        else:
            recent = self._recent
            recent.frombytes(consumed)
            del recent[: max(0, len(recent) - keep * 2)]


class FirFilter:
    """Filter complex samples with real taps, carrying on across chunks of any length.

    NWR Stream Manager's own native filter: liquid-dsp's is several times slower where
    it's built without vector instructions, as some distributions' builds are.
    """

    def __init__(self, taps: Any) -> None:
        self.taps = float_taps(taps)
        # The last len(taps) - 1 inputs, which the next chunk, or new taps, start from.
        self._history = native.floats(2 * (len(self.taps) - 1))

    def update_taps(self, taps: Any) -> None:
        """Switch to new coefficients, starting from the same recent input."""
        taps = float_taps(taps)
        keep = 2 * (len(taps) - 1)
        if len(self._history) >= keep:
            self._history = self._history[len(self._history) - keep :]
        else:
            self._history = native.floats(keep - len(self._history)) + self._history
        self.taps = taps

    def process(self, samples: Any) -> array.array:
        data = _bytes(samples)
        if not data.nbytes:
            return array.array("f")
        work = self._history + _floats(data)
        keep = 2 * (len(self.taps) - 1)
        self._history = work[len(work) - keep :] if keep else array.array("f")
        return native.cf32_fir(work, self.taps)


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
        self._ones = array.array("f", [1.0]) * self.block_size
        self._sum = _FloatComplex()
        self._full_block_decay = self._decay(self.block_size)
        self._averager = _Owned(
            self._library.dotprod_crcf_create(native.readable(self._ones).address, self.block_size),
            self._library.dotprod_crcf_destroy,
            "the DC offset averager",
        )

    def _decay(self, samples: int) -> float:
        return math.exp(-samples / (self.sample_rate * self.time_constant_seconds))

    def process(self, samples: Any) -> array.array:
        source = native.readable(samples)
        count_total = source.nbytes // 8
        output = native.floats(2 * count_total)
        if count_total == 0:
            return output
        library = self._library
        target = native.writable(output)
        total = ctypes.addressof(self._sum)
        ones = native.readable(self._ones).address
        for start in range(0, count_total, self.block_size):
            count = min(self.block_size, count_total - start)
            if count == self.block_size:
                library.dotprod_crcf_execute(self._averager.handle, source.address + start * 8, total)
                decay = self._full_block_decay
            else:
                library.dotprod_crcf_run4(ones, source.address + start * 8, count, total)
                decay = self._decay(count)
            average = complex(self._sum.real, self._sum.imag) / count
            offset = _FloatComplex(-self.mean.real, -self.mean.imag)
            library.liquid_vectorcf_addscalar(source.address + start * 8, count, offset, target.address + start * 8)
            self.mean = average + (self.mean - average) * decay
        return output


class AudioDcBlocker:
    """First-order DC-blocking filter for real audio: y[n] = x[n] - x[n-1] + r * y[n-1]."""

    def __init__(self, coefficient: float) -> None:
        if not 0.0 < coefficient < 1.0:
            raise ValueError("DC blocker coefficient must be between 0 and 1")
        self._library = liquid_library()
        self._filter = _Owned(
            self._library.iirfilt_rrrf_create_dc_blocker(1.0 - float(coefficient)),
            self._library.iirfilt_rrrf_destroy,
            "the audio DC blocker",
        )
        # liquid-dsp turns the level down very slightly by default; keep it unchanged.
        self._library.iirfilt_rrrf_set_scale(self._filter.handle, 1.0)

    def process(self, samples: Any) -> array.array:
        source = native.readable(samples)
        output = native.floats(source.nbytes // 4)
        if output:
            self._library.iirfilt_rrrf_execute_block(
                self._filter.handle, source.address, len(output), native.writable(output).address
            )
        return output


class Oscillator:
    """Shift complex samples up in frequency by mixing them with a precise oscillator."""

    def __init__(self, radians_per_sample: float = 0.0) -> None:
        self._library = liquid_library()
        self._lock = threading.Lock()
        self._oscillator = _Owned(
            self._library.nco_crcf_create(LIQUID_PRECISE_OSCILLATOR), self._library.nco_crcf_destroy, "the oscillator"
        )
        self.set_frequency(radians_per_sample)

    def set_frequency(self, radians_per_sample: float) -> None:
        with self._lock:
            self._library.nco_crcf_set_frequency(self._oscillator.handle, float(radians_per_sample))

    @property
    def phase(self) -> float:
        with self._lock:
            return float(self._library.nco_crcf_get_phase(self._oscillator.handle))

    @phase.setter
    def phase(self, radians: float) -> None:
        with self._lock:
            self._library.nco_crcf_set_phase(self._oscillator.handle, float(radians))

    def generate(self, count: int) -> array.array:
        """The oscillator itself, exp(j * phase), for `count` samples, as I then Q."""
        return self.mix_up(array.array("f", [1.0, 0.0]) * max(0, int(count)))

    def mix_up(self, samples: Any) -> array.array:
        source = native.readable(samples)
        output = native.floats(source.nbytes // 4)
        if output:
            with self._lock:
                self._library.nco_crcf_mix_block_up(
                    self._oscillator.handle, source.address, native.writable(output).address, len(output) // 2
                )
        return output


class FmDemodulator:
    """FM demodulation: the phase change from each sample to the next, times `gain` / pi."""

    def __init__(self, gain: float) -> None:
        self._library = liquid_library()
        # liquid-dsp outputs the phase change divided by 2 * pi * kf.
        self._demodulator = _Owned(
            self._library.freqdem_create(1.0 / (2.0 * float(gain))), self._library.freqdem_destroy, "the FM demodulator"
        )

    def reset(self) -> None:
        self._library.freqdem_reset(self._demodulator.handle)

    def demodulate(self, samples: Any) -> array.array:
        source = native.readable(samples)
        output = native.floats(source.nbytes // 8)
        if output:
            self._library.freqdem_demodulate_block(
                self._demodulator.handle, source.address, len(output), native.writable(output).address
            )
        return output


class FmModulator:
    """FM modulation: each sample advances the phase by 2 * pi * kf * audio."""

    def __init__(self, kf: float) -> None:
        self._library = liquid_library()
        self._modulator = _Owned(self._library.freqmod_create(float(kf)), self._library.freqmod_destroy, "the FM modulator")

    def modulate(self, audio: Any) -> array.array:
        source = native.readable(audio)
        count = source.nbytes // 4
        output = native.floats(2 * count)
        if count:
            self._library.freqmod_modulate_block(self._modulator.handle, source.address, count, native.writable(output).address)
        return output


def _fft_filter(library: ctypes.CDLL, taps: array.array, block: int) -> _Owned:
    with _fft_plan_lock:
        handle = library.fftfilt_rrrf_create(native.readable(taps).address, len(taps), block)
    return _Owned(handle, library.fftfilt_rrrf_destroy, "the FFT filter", _fft_plan_lock)


class AudioFirFilter:
    """Filter real audio with a long FIR, using liquid-dsp's FFT filter for whole blocks.

    Chunks made of whole `block`-sample blocks, which is what every audio path sends,
    go through the FFT filter. Any other length is filtered directly from the same
    history, so the output is the same whatever lengths arrive, with no added delay.
    """

    def __init__(self, taps: Any, block: int) -> None:
        taps = float_taps(taps)
        if int(block) < len(taps) - 1:
            raise ValueError("FFT filter block must be at least the filter length minus one")
        self.taps = taps
        self.block = int(block)
        self._library = liquid_library()
        self._filter = _fft_filter(self._library, taps, self.block)
        self._history = native.floats(len(taps) - 1)
        self._in_step = True  # the FFT filter's own state matches self._history

    def process(self, samples: Any) -> array.array:
        data = _bytes(samples)
        count = data.nbytes // 4
        if count == 0:
            return array.array("f")
        output = native.floats(count)
        if count % self.block:
            self._filter_directly(data, output)
            self._in_step = False
        else:
            if not self._in_step:
                self._prime()
            source, target = native.readable(data), native.writable(output)
            for start in range(0, count, self.block):
                self._library.fftfilt_rrrf_execute(self._filter.handle, source.address + start * 4, target.address + start * 4)
        self._remember(data)
        return output

    def _filter_directly(self, data: memoryview, output: array.array) -> None:
        """A FIR filter starting from the remembered history, for lengths the FFT filter can't take."""
        library = self._library
        direct = _Owned(
            library.firfilt_rrrf_create(native.readable(self.taps).address, len(self.taps)),
            library.firfilt_rrrf_destroy,
            "the filter",
        )
        if self._history:
            discard = native.floats(len(self._history))
            library.firfilt_rrrf_execute_block(
                direct.handle, native.readable(self._history).address, len(self._history), native.writable(discard).address
            )
        library.firfilt_rrrf_execute_block(direct.handle, native.readable(data).address, len(output), native.writable(output).address)

    def _prime(self) -> None:
        # Run one block ending in the remembered input, so the FFT filter carries on
        # from exactly where direct filtering left off.
        primer = native.floats(self.block - len(self._history)) + self._history
        discard = native.floats(self.block)
        self._library.fftfilt_rrrf_reset(self._filter.handle)
        self._library.fftfilt_rrrf_execute(self._filter.handle, native.readable(primer).address, native.writable(discard).address)
        self._in_step = True

    def _remember(self, data: memoryview) -> None:
        keep = len(self._history)
        if not keep:
            return
        self._history.frombytes(data[-keep * 4 :])
        del self._history[:-keep]


class DelayedFftFirFilter:
    """Filter real audio with a FIR longer than the frames it arrives in.

    liquid-dsp's FFT filter needs blocks at least as long as the filter, so samples wait
    until a whole block has arrived. The output is delayed by a constant block - 1
    samples, which lets every call return exactly as many samples as it was given.
    """

    def __init__(self, taps: Any, block: int) -> None:
        self.block = int(block)
        self.delay = self.block - 1
        self._library = liquid_library()
        self.taps = array.array("f")
        self._create(taps)
        self._pending = array.array("f")
        self._ready = native.floats(self.delay)
        self._recent = native.floats(self.block)

    def _create(self, taps: Any) -> None:
        taps = float_taps(taps)
        if self.block < len(taps) - 1:
            raise ValueError("FFT filter block must be at least the filter length minus one")
        self._filter = _fft_filter(self._library, taps, self.block)
        self.taps = taps

    def process(self, samples: Any) -> array.array:
        data = _bytes(samples)
        count = data.nbytes // 4
        if count == 0:
            return array.array("f")
        work = self._pending
        work.frombytes(data)
        whole = len(work) - len(work) % self.block
        if whole:
            filtered = native.floats(whole)
            source, target = native.readable(work), native.writable(filtered)
            for start in range(0, whole, self.block):
                self._library.fftfilt_rrrf_execute(self._filter.handle, source.address + start * 4, target.address + start * 4)
            del source, target
            self._recent = work[whole - self.block : whole]
            self._ready.extend(filtered)
        del work[:whole]
        output = self._ready[:count]
        del self._ready[:count]
        return output

    def update_taps(self, taps: Any) -> None:
        """Switch to new taps, carrying on from the latest audio instead of silence."""
        self._create(taps)
        discard = native.floats(self.block)
        self._library.fftfilt_rrrf_execute(
            self._filter.handle, native.readable(self._recent).address, native.writable(discard).address
        )
