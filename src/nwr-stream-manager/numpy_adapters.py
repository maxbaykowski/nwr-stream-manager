"""Samples as numpy arrays, for the code that still works with numpy while NWR Stream
Manager moves to native C.

The receiver's DSP gives plain buffers: array.array("f") of audio, and of I/Q as I then Q.
Code not yet moved reads them through these, without copying. Each stage of the move takes
its callers off these, and the last removes this module.
"""

from __future__ import annotations

from typing import Any

import numpy as np

_FLOAT32 = ("f", "<f", "=f")


def complex64(samples: Any) -> np.ndarray:
    """I/Q as complex64: a numpy array as it is, any other float32 buffer read as I then Q."""
    if isinstance(samples, np.ndarray):
        return samples.astype(np.complex64, copy=False)
    view = memoryview(samples)
    if view.format not in _FLOAT32:
        raise TypeError(f"expected float32 I/Q, not {view.format!r}")
    if view.nbytes == 0:
        return np.empty(0, dtype=np.complex64)
    return np.frombuffer(samples, dtype=np.complex64)


def float32(samples: Any) -> np.ndarray:
    """Audio as float32: a numpy array as it is, any other float32 buffer read where it is."""
    if isinstance(samples, np.ndarray):
        return samples.astype(np.float32, copy=False)
    view = memoryview(samples)
    if view.format not in _FLOAT32:
        raise TypeError(f"expected float32 audio, not {view.format!r}")
    if view.nbytes == 0:
        return np.empty(0, dtype=np.float32)
    return np.frombuffer(samples, dtype=np.float32)
