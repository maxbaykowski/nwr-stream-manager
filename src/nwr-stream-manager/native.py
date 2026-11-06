"""NWR Stream Manager's own native library (native/nwr_native.c), through ctypes.

An installed copy has it built during installation, next to this module. Run from a
source checkout, it's built here the first time it's needed, and again whenever its source
changes, so the code and the library always match.

Every function takes buffers the caller allocates: bytes, bytearrays, array.arrays,
memoryviews, or anything else with a buffer. Sizes are checked both here and in C.

Memory: the library allocates nothing, and writes only into the buffers it's given, which
Python owns and frees as usual. Anything later that keeps state in C (a filter, say) is to
be freed by a weakref.finalize on its Python object, so it's freed even if that object is
never closed.
"""

from __future__ import annotations

import ctypes
import glob
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

PACKAGE_DIRECTORY = Path(__file__).resolve().parent
SOURCE = PACKAGE_DIRECTORY / "native" / "nwr_native.c"
# Matches NWR_NATIVE_ABI in the C source.
ABI = 2
# How it's compiled, here and by setup.py on installation:
# - fast on weak CPUs: optimized, and free to turn loops into vector instructions, since
#   nothing relies on floating-point exception flags;
# - the same results on every computer: no fused multiply-adds, and nothing that changes
#   how float arithmetic rounds (so never -ffast-math or -march=native);
# - harder to exploit: stack protection, relocations made read-only once loaded, and a stack
#   that can't run code.
COMPILE_FLAGS = [
    "-O2",
    "-std=c11",
    "-ffp-contract=off",
    "-fno-trapping-math",
    "-Wall",
    "-Wextra",
    "-fstack-protector-strong",
    "-fstack-clash-protection",
]
LINK_FLAGS = ["-Wl,-z,relro", "-Wl,-z,now", "-Wl,-z,noexecstack"]
ROUND_NEAREST = 0
ROUND_TRUNCATE = 1

_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()


class NativeLibraryError(OSError):
    """NWR Stream Manager's native library is missing, couldn't be built, or doesn't match."""


def _source_checkout() -> Path | None:
    """The checkout this is run from, if it is one."""
    root = PACKAGE_DIRECTORY.parent.parent
    if PACKAGE_DIRECTORY.parent.name == "src" and (root / "pyproject.toml").is_file():
        return root
    return None


def _installed_library() -> Path | None:
    built = sorted(glob.glob(str(PACKAGE_DIRECTORY / "_nwr_native*.so")))
    return Path(built[0]) if built else None


def _build_in_checkout(root: Path) -> Path:
    target = root / "build" / "native" / "libnwr_native.so"
    if target.exists() and target.stat().st_mtime >= SOURCE.stat().st_mtime:
        return target
    compiler = os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        raise NativeLibraryError("NWR Stream Manager's native library can't be built: no C compiler (gcc) is installed")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Built beside it and moved into place, so nothing ever loads half a library.
    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".so")
    os.close(handle)
    try:
        result = subprocess.run(
            [compiler, *COMPILE_FLAGS, "-fPIC", "-shared", *LINK_FLAGS, "-o", temporary, str(SOURCE)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise NativeLibraryError(f"NWR Stream Manager's native library didn't build: {result.stderr.strip()}")
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def native_library() -> ctypes.CDLL:
    global _library
    library = _library
    if library is not None:
        # Called for every block of audio, so without taking the lock once it's loaded.
        return library
    with _library_lock:
        if _library is not None:
            return _library
        root = _source_checkout()
        path = _build_in_checkout(root) if root is not None else _installed_library()
        if path is None:
            raise NativeLibraryError(
                "NWR Stream Manager's native library is missing. Reinstall NWR Stream Manager "
                "with a C compiler (gcc) installed, which builds it."
            )
        try:
            library = ctypes.CDLL(str(path))
        except OSError as exc:
            raise NativeLibraryError(f"NWR Stream Manager's native library couldn't be loaded: {exc}") from exc
        library.nwr_native_abi.restype = ctypes.c_int
        library.nwr_native_abi.argtypes = []
        found = library.nwr_native_abi()
        if found != ABI:
            raise NativeLibraryError(
                f"NWR Stream Manager's native library is from a different version (ABI {found}, expected {ABI}). "
                "Reinstall NWR Stream Manager."
            )
        pointer = ctypes.c_void_p
        size = ctypes.c_size_t
        status = ctypes.c_int
        library.nwr_u8_iq_to_f32.restype = status
        library.nwr_u8_iq_to_f32.argtypes = [pointer, size, pointer, size, size]
        library.nwr_f32_to_s16.restype = status
        library.nwr_f32_to_s16.argtypes = [pointer, size, pointer, size, size, ctypes.c_float, ctypes.c_int]
        library.nwr_s16_to_f32.restype = status
        library.nwr_s16_to_f32.argtypes = [pointer, size, pointer, size, size, ctypes.c_float]
        _library = library
        return library


class _Buffer:
    """A buffer's address and size, kept alive for as long as C uses it."""

    def __init__(self, buffer: Any, writable: bool = False) -> None:
        if isinstance(buffer, bytes) and not writable:
            # Bytes can't be written, but C can read them where they are.
            self._keep: Any = buffer
            self.address = ctypes.cast(ctypes.c_char_p(buffer), ctypes.c_void_p).value or 0
            self.nbytes = len(buffer)
            return
        view = memoryview(buffer)
        if not view.c_contiguous:
            raise ValueError("native library buffers must be contiguous")
        if view.format != "B" or view.ndim != 1:
            view = view.cast("B")
        if view.readonly:
            if writable:
                raise ValueError("native library output buffers must be writable")
            # Something read-only other than bytes: copied, so C has an address to read.
            view = memoryview(bytearray(view))
        self._keep = view
        self.nbytes = view.nbytes
        self.address = ctypes.addressof(ctypes.c_char.from_buffer(view)) if self.nbytes else 0


def _need(buffer: _Buffer, nbytes: int, what: str) -> None:
    if buffer.nbytes < nbytes:
        raise ValueError(f"{what} holds {buffer.nbytes} bytes; {nbytes} are needed")


def _checked(status: int, what: str) -> None:
    # The C checks everything Python does, and more, such as buffers that overlap.
    if status != 0:
        raise ValueError(f"the native library refused {what}")


def u8_iq_to_f32(data: Any, out: Any, pairs: int) -> None:
    """RTL-SDR samples, unsigned 8-bit I then Q, to interleaved float32 I/Q from -1 to 1.
    out holds pairs * 2 float32s, such as pairs complex64s."""
    source, target = _Buffer(data), _Buffer(out, writable=True)
    _need(source, pairs * 2, "the RTL-SDR samples")
    _need(target, pairs * 8, "the output")
    _checked(
        native_library().nwr_u8_iq_to_f32(source.address, source.nbytes, target.address, target.nbytes // 4, pairs),
        "converting RTL-SDR samples",
    )


def f32_to_s16(samples: Any, out: Any, scale: float, *, truncate: bool = False) -> None:
    """Float32 audio to signed 16-bit: multiplied by scale, clipped to the 16-bit range, then
    rounded to the nearest (halves to even) or, with truncate, toward zero. Something that
    isn't a number becomes silence."""
    source, target = _Buffer(samples), _Buffer(out, writable=True)
    count = source.nbytes // 4
    _need(target, count * 2, "the output")
    _checked(
        native_library().nwr_f32_to_s16(
            source.address, count, target.address, target.nbytes // 2, count, scale, ROUND_TRUNCATE if truncate else ROUND_NEAREST
        ),
        "converting audio to 16-bit",
    )


def s16_to_f32(pcm: Any, out: Any, scale: float) -> None:
    """Signed 16-bit audio to float32, divided by scale."""
    source, target = _Buffer(pcm), _Buffer(out, writable=True)
    count = source.nbytes // 2
    _need(target, count * 4, "the output")
    _checked(
        native_library().nwr_s16_to_f32(source.address, count, target.address, target.nbytes // 4, count, scale),
        "converting 16-bit audio",
    )
