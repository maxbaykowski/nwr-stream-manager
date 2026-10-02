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
# Recent input kept so new filter coefficients can start from the same history.
FIR_DECIMATOR_HISTORY_SAMPLES = 2048

class _FloatComplex(ctypes.Structure):
    """C99 `float complex`, which liquid-dsp takes by value for scalar arguments."""

    _fields_ = [("real", ctypes.c_float), ("imag", ctypes.c_float)]


_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()


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
