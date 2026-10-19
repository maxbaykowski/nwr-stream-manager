"""Decoding audio files through the native libsndfile library.

libsndfile reads WAV, Ogg (Vorbis and Opus) and MP3 itself, and refuses files it
cannot make sense of, so audio people upload is decoded without trusting its name
or claimed type. Files are read from memory, never written to disk first.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import threading
from typing import NamedTuple

import numpy as np
from numpy.typing import NDArray

SNDFILE_SONAMES = ("libsndfile.so.1", "libsndfile.so")
SFM_READ = 0x10
SEEK_SET, SEEK_CUR, SEEK_END = 0, 1, 2
SF_FORMAT_TYPEMASK = 0x0FFF0000
SF_FORMAT_SUBMASK = 0x0000FFFF
SF_FORMAT_WAV = 0x010000
SF_FORMAT_WAVEX = 0x130000
SF_FORMAT_OGG = 0x200000
SF_FORMAT_MPEG = 0x230000
SF_FORMAT_VORBIS = 0x0060
SF_FORMAT_OPUS = 0x0064
SF_FORMAT_MPEG_LAYER_I = 0x0080
SF_FORMAT_MPEG_LAYER_II = 0x0081
SF_FORMAT_MPEG_LAYER_III = 0x0082

sf_count_t = ctypes.c_int64


class SfInfo(ctypes.Structure):
    _fields_ = [
        ("frames", sf_count_t),
        ("samplerate", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("format", ctypes.c_int),
        ("sections", ctypes.c_int),
        ("seekable", ctypes.c_int),
    ]


_GET_FILELEN = ctypes.CFUNCTYPE(sf_count_t, ctypes.c_void_p)
_SEEK = ctypes.CFUNCTYPE(sf_count_t, sf_count_t, ctypes.c_int, ctypes.c_void_p)
_READ = ctypes.CFUNCTYPE(sf_count_t, ctypes.c_void_p, sf_count_t, ctypes.c_void_p)
_WRITE = ctypes.CFUNCTYPE(sf_count_t, ctypes.c_void_p, sf_count_t, ctypes.c_void_p)
_TELL = ctypes.CFUNCTYPE(sf_count_t, ctypes.c_void_p)


class SfVirtualIo(ctypes.Structure):
    _fields_ = [
        ("get_filelen", _GET_FILELEN),
        ("seek", _SEEK),
        ("read", _READ),
        ("write", _WRITE),
        ("tell", _TELL),
    ]


class DecodedAudio(NamedTuple):
    samples: NDArray[np.float32]  # (frames, channels), from -1.0 to 1.0
    sample_rate: int
    channels: int
    format_code: int
    # How many frames the file says it holds, which a damaged file may not.
    declared_frames: int


class SndfileError(ValueError):
    """libsndfile could not read the audio."""


_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()


def sndfile_library() -> ctypes.CDLL:
    global _library
    with _library_lock:
        if _library is not None:
            return _library
        names = [name for name in (ctypes.util.find_library("sndfile"), *SNDFILE_SONAMES) if name]
        errors = []
        for name in names:
            try:
                library = ctypes.CDLL(name)
                break
            except OSError as exc:
                errors.append(str(exc))
        else:
            raise OSError("libsndfile is not installed: " + "; ".join(errors))
        pointer = ctypes.c_void_p
        library.sf_open_virtual.restype = pointer
        library.sf_open_virtual.argtypes = [ctypes.POINTER(SfVirtualIo), ctypes.c_int, ctypes.POINTER(SfInfo), pointer]
        library.sf_readf_float.restype = sf_count_t
        library.sf_readf_float.argtypes = [pointer, ctypes.POINTER(ctypes.c_float), sf_count_t]
        library.sf_error.restype = ctypes.c_int
        library.sf_error.argtypes = [pointer]
        library.sf_strerror.restype = ctypes.c_char_p
        library.sf_strerror.argtypes = [pointer]
        library.sf_close.restype = ctypes.c_int
        library.sf_close.argtypes = [pointer]
        _library = library
        return library


class _MemoryFile:
    """The virtual file libsndfile reads: a bytes object and a position in it."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.position = 0
        # libsndfile calls back into these while a file is open, so they must
        # outlive it.
        self.io = SfVirtualIo(
            _GET_FILELEN(lambda user: len(self.data)),
            _SEEK(self._seek),
            _READ(self._read),
            _WRITE(lambda ptr, count, user: 0),
            _TELL(lambda user: self.position),
        )

    def _seek(self, offset: int, whence: int, user) -> int:
        base = {SEEK_SET: 0, SEEK_CUR: self.position, SEEK_END: len(self.data)}.get(whence)
        if base is None or base + offset < 0:
            return -1
        self.position = base + offset
        return self.position

    def _read(self, destination, count: int, user) -> int:
        chunk = self.data[self.position : self.position + max(0, count)]
        ctypes.memmove(destination, chunk, len(chunk))
        self.position += len(chunk)
        return len(chunk)


def decode_audio(data: bytes, max_seconds: float) -> DecodedAudio:
    """Decode a whole audio file held in memory.

    Decoding stops just past
    ``max_seconds``, so a caller can tell a file is too long without decoding all
    of it.
    """
    library = sndfile_library()
    memory = _MemoryFile(data)
    info = SfInfo()
    handle = library.sf_open_virtual(ctypes.byref(memory.io), SFM_READ, ctypes.byref(info), None)
    if not handle:
        raise SndfileError(library.sf_strerror(None).decode("utf-8", "replace"))
    try:
        channels = int(info.channels)
        sample_rate = int(info.samplerate)
        if channels <= 0 or sample_rate <= 0:
            raise SndfileError("the file has no audio channels")
        max_frames = int(max_seconds * sample_rate) + 1
        chunks = []
        frames = 0
        chunk_frames = 65_536
        while frames <= max_frames:
            buffer = np.empty((chunk_frames, channels), dtype=np.float32)
            read = library.sf_readf_float(handle, buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_float)), chunk_frames)
            if read <= 0:
                break
            chunks.append(buffer[:read])
            frames += read
        if library.sf_error(handle):
            raise SndfileError(library.sf_strerror(handle).decode("utf-8", "replace"))
    finally:
        library.sf_close(handle)
    samples = np.concatenate(chunks) if chunks else np.empty((0, channels), dtype=np.float32)
    return DecodedAudio(samples, sample_rate, channels, int(info.format), int(info.frames))
