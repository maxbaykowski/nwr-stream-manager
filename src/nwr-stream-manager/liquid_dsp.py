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
