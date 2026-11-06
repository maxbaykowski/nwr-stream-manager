"""Sample-rate conversion through the native libsoxr library.

This drives libsoxr the same way the soxr Python package does (high quality, whole
chunks in, output as early as libsoxr allows), so results are identical, without the
Python package or its numpy-only interface. Samples go in as any buffer (bytes, array.array,
memoryview) and come out as an array.array.
"""

from __future__ import annotations

import array
import ctypes
import ctypes.util
import threading
import weakref
from typing import Any

SOXR_SONAMES = ("libsoxr.so.0", "libsoxr.so")
SOXR_FLOAT32_I = 0
SOXR_INT16_I = 3
SOXR_HQ = 4  # 20-bit precision, the soxr Python package's default "HQ"
SOXR_NO_DITHER = 8
# Long chunks are fed in pieces of about this many output samples, as the Python package does.
SOXR_CHUNK_OUTPUT_SAMPLES = 48_000

# libsoxr's type, the array.array type code, and the buffer formats taken as that type.
_DATATYPES = {
    "float32": (SOXR_FLOAT32_I, "f", ("f", "<f", "=f")),
    "int16": (SOXR_INT16_I, "h", ("h", "<h", "=h")),
}
_RAW_FORMATS = ("B", "b", "c")


class _IoSpec(ctypes.Structure):
    _fields_ = [
        ("itype", ctypes.c_int),
        ("otype", ctypes.c_int),
        ("scale", ctypes.c_double),
        ("e", ctypes.c_void_p),
        ("flags", ctypes.c_ulong),
    ]


class _QualitySpec(ctypes.Structure):
    _fields_ = [
        ("precision", ctypes.c_double),
        ("phase_response", ctypes.c_double),
        ("passband_end", ctypes.c_double),
        ("stopband_begin", ctypes.c_double),
        ("e", ctypes.c_void_p),
        ("flags", ctypes.c_ulong),
    ]


_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()


def soxr_library() -> ctypes.CDLL:
    global _library
    with _library_lock:
        if _library is not None:
            return _library
        names = [name for name in (ctypes.util.find_library("soxr"), *SOXR_SONAMES) if name]
        errors = []
        for name in names:
            try:
                library = ctypes.CDLL(name)
                break
            except OSError as exc:
                errors.append(str(exc))
        else:
            raise OSError("libsoxr is not installed: " + "; ".join(errors))
        pointer = ctypes.c_void_p
        size_pointer = ctypes.POINTER(ctypes.c_size_t)
        library.soxr_io_spec.restype = _IoSpec
        library.soxr_io_spec.argtypes = [ctypes.c_int, ctypes.c_int]
        library.soxr_quality_spec.restype = _QualitySpec
        library.soxr_quality_spec.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
        library.soxr_create.restype = pointer
        library.soxr_create.argtypes = [
            ctypes.c_double,
            ctypes.c_double,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(_IoSpec),
            ctypes.POINTER(_QualitySpec),
            pointer,
        ]
        library.soxr_process.restype = ctypes.c_char_p
        library.soxr_process.argtypes = [pointer, pointer, ctypes.c_size_t, size_pointer, pointer, ctypes.c_size_t, size_pointer]
        library.soxr_delay.restype = ctypes.c_double
        library.soxr_delay.argtypes = [pointer]
        library.soxr_delete.restype = None
        library.soxr_delete.argtypes = [pointer]
        _library = library
        return library


class SoxrStream:
    """Resample one channel of audio in a stream of chunks of any length."""

    def __init__(self, input_rate: float, output_rate: float, dtype: str = "float32") -> None:
        if dtype not in _DATATYPES:
            raise ValueError(f"unsupported soxr sample type: {dtype}")
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError("sample rates must be greater than 0")
        self.input_rate = float(input_rate)
        self.output_rate = float(output_rate)
        self._datatype, self.typecode, self._formats = _DATATYPES[dtype]
        self._item = array.array(self.typecode).itemsize
        self._ratio = self.output_rate / self.input_rate
        self._chunk_input = max(1000, int(SOXR_CHUNK_OUTPUT_SAMPLES / self._ratio))
        self._library = soxr_library()
        io_spec = self._library.soxr_io_spec(self._datatype, self._datatype)
        # libsoxr dithers 16-bit output with randomness seeded from the clock, so the same
        # audio came out slightly different every time. Without it, output is repeatable
        # and matches the soxr Python package this replaced.
        io_spec.flags |= SOXR_NO_DITHER
        quality_spec = self._library.soxr_quality_spec(SOXR_HQ, 0)
        error = ctypes.c_char_p()
        self._handle = self._library.soxr_create(
            self.input_rate,
            self.output_rate,
            1,
            ctypes.byref(error),
            ctypes.byref(io_spec),
            ctypes.byref(quality_spec),
            None,
        )
        if error.value or not self._handle:
            raise RuntimeError(f"libsoxr could not create a resampler: {(error.value or b'unknown error').decode()}")
        # Freed once this is, even if it's never closed.
        self._finalizer = weakref.finalize(self, self._library.soxr_delete, self._handle)
        self._ended = False
        self._done_value = ctypes.c_size_t(0)
        self._done = ctypes.byref(self._done_value)

    def _input(self, samples: Any) -> tuple[ctypes.Array, int]:
        """The samples, read where they are, and how many there are."""
        view = memoryview(samples)
        if view.format not in self._formats and view.format not in _RAW_FORMATS:
            raise TypeError(f"expected {self.typecode!r} samples, not {view.format!r}")
        if not view.c_contiguous:
            raise ValueError("samples must be contiguous")
        view = view.cast("B")
        count = view.nbytes // self._item
        if view.readonly:
            # libsoxr only reads the input, but ctypes can only point into a buffer it may write.
            return (ctypes.c_char * view.nbytes).from_buffer_copy(view), count
        return (ctypes.c_char * view.nbytes).from_buffer(view), count

    def process(self, samples: Any, *, last: bool = False) -> array.array:
        if self._ended:
            raise RuntimeError("input after the last chunk")
        if not self._finalizer.alive:
            raise RuntimeError("the resampler is closed")
        source, count = self._input(samples)
        source_address = ctypes.addressof(source)
        item = self._item
        process = self._library.soxr_process
        handle, done = self._handle, self._done
        # Room for this chunk's output plus whatever libsoxr is still holding back, sized
        # like the soxr Python package so output arrives in the same pieces; the loops
        # below grow it in the rare case that is not enough.
        capacity = int(self._library.soxr_delay(handle) + count * self._ratio) + 1
        output = array.array(self.typecode, bytes(max(capacity, 1024) * item))
        produced = 0
        for start in range(0, count, self._chunk_input):
            piece = min(self._chunk_input, count - start)
            if produced >= len(output):
                output.frombytes(bytes(len(output) * item))
            self._check(
                process(
                    handle,
                    source_address + start * item,
                    piece,
                    None,  # libsoxr takes the whole piece and buffers what it cannot output yet
                    output.buffer_info()[0] + produced * item,
                    len(output) - produced,
                    done,
                )
            )
            produced += self._done_value.value
        if last:
            self._ended = True
            while True:
                if produced >= len(output):
                    output.frombytes(bytes(len(output) * item))
                self._check(process(handle, None, 0, None, output.buffer_info()[0] + produced * item, len(output) - produced, done))
                if self._done_value.value == 0:
                    break
                produced += self._done_value.value
        del output[produced:]
        return output

    def close(self) -> None:
        self._finalizer()

    @staticmethod
    def _check(error: bytes | None) -> None:
        if error:
            raise RuntimeError(f"libsoxr failed: {error.decode()}")
