"""Stream archiving: a continuous recording of what a stream's listeners hear.

The stream's audio goes to one Opus encoder (16 kHz mono, 32 kbps) that runs for as long
as the audio keeps coming. Its packets are written to Ogg Opus files of up to an hour each,
named by when their first sample was heard. While the audio is continuous, a new hour's
file just carries on from the same encoder, so audio taken from both sides of a join is
decoded straight across it, exactly as if it had been one file.

A run of continuous audio is a segment. A segment ends when the audio stops coming (the
SDR is lost, the stream is stopped, archiving is turned off, storage runs low) or jumps
in time; the next audio starts a new one. Exports fill the time between segments with
silence, so their audio stays in step with the clock.

Every page of a file is written as it fills (about a second of audio), so a crash loses at
most that. A file cut short is still read up to its last whole page.
"""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import queue
import re
import struct
import threading
import time
import zlib
from bisect import bisect_right
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from .config import IQ_SAMPLE_RATE
from .encoder import PcmResampler
from .state_files import atomic_write_json

LOG = logging.getLogger(__name__)

ARCHIVE_SAMPLE_RATE = 16_000
ARCHIVE_BITRATE = 32_000
ARCHIVE_FRAME_SAMPLES = 320  # 20 ms
# Ogg Opus counts time at 48 kHz.
GRANULE_PER_SAMPLE = 48_000 // ARCHIVE_SAMPLE_RATE
PAGE_FRAMES = 50  # about a second of audio per Ogg page
BLOCK_SECONDS = 3600
# Audio arriving this much later (or earlier) than it should is a break in the archive.
GAP_SECONDS = 2.0
# How long the writer waits for audio before ending the segment and closing its file.
IDLE_SECONDS = 5.0
QUEUE_FRAMES = 500  # 10 seconds of the stream's 20 ms frames
EXPORT_MAX_SECONDS = 12 * 3600
# Decoding starts this many packets early when an export begins partway into a segment,
# so the decoder has settled (RFC 7845, section 4.6).
PREROLL_FRAMES = 4
INDEX_FILE_NAME = "index.json"
FILE_PATTERN = re.compile(r"^(\d{13})(?:-(\d+))?\.opus$")

RETENTION_CHOICES: tuple[tuple[str, str, int], ...] = (
    ("24h", "24 hours", 24 * 3600),
    ("1w", "1 week", 7 * 24 * 3600),
    ("30d", "30 days", 30 * 24 * 3600),
    ("180d", "180 days", 180 * 24 * 3600),
    ("1y", "1 year", 365 * 24 * 3600),
)
RETENTION_SECONDS = {value: seconds for value, _label, seconds in RETENTION_CHOICES}
RETENTION_LABELS = {value: label for value, label, _seconds in RETENTION_CHOICES}
DEFAULT_RETENTION = "1w"
EXPORT_FORMATS = (("opus", "Ogg Opus"), ("mp3", "MP3"), ("wav", "WAV"))
MP3_EXPORT_BITRATE_KBPS = 64


class StreamArchiveError(RuntimeError):
    pass


# ----- Settings ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StreamArchiveSettings:
    enabled: bool = False
    retention: str = DEFAULT_RETENTION


def validate_stream_archive_payload(raw: Any) -> StreamArchiveSettings:
    if not isinstance(raw, dict):
        raise ValueError("stream archiving settings must be an object")
    retention = str(raw.get("retention", DEFAULT_RETENTION) or DEFAULT_RETENTION).strip()
    if retention not in RETENTION_SECONDS:
        raise ValueError("Choose how long to keep archived audio.")
    return StreamArchiveSettings(enabled=bool(raw.get("enabled", False)), retention=retention)


def stream_archive_settings_from_stream(stream: dict[str, Any]) -> StreamArchiveSettings:
    raw = stream.get("archive")
    if not isinstance(raw, dict):
        return StreamArchiveSettings()
    try:
        return validate_stream_archive_payload(raw)
    except ValueError:
        return StreamArchiveSettings(enabled=bool(raw.get("enabled", False)))


def stream_archive_settings_dict(stream: dict[str, Any]) -> dict[str, Any]:
    return asdict(stream_archive_settings_from_stream(stream))


def stream_archive_directory(state_directory: Path, stream_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(stream_id)) or "stream"
    return state_directory / "stream-archives" / safe


# ----- Opus, through libopus ---------------------------------------------------------------

_OPUS_LOCK = threading.Lock()
_OPUS: ctypes.CDLL | None = None
OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE = 4002
OPUS_SET_VBR = 4006
OPUS_SET_COMPLEXITY = 4010
OPUS_GET_LOOKAHEAD = 4027


def _opus() -> ctypes.CDLL:
    """Its own handle on libopus, so these declarations don't touch live audio's."""
    global _OPUS
    with _OPUS_LOCK:
        if _OPUS is not None:
            return _OPUS
        from .webrtc import _load_opus_module

        library = ctypes.CDLL(_load_opus_module().library._name)
        library.opus_encoder_create.restype = ctypes.c_void_p
        library.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        # Variadic: only the encoder and the request are declared; the value follows.
        library.opus_encoder_ctl.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.opus_encoder_ctl.restype = ctypes.c_int
        library.opus_encode.restype = ctypes.c_int32
        library.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
        library.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        library.opus_decoder_create.restype = ctypes.c_void_p
        library.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        library.opus_decode.restype = ctypes.c_int
        library.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        library.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        _OPUS = library
        return library


class ArchiveOpusEncoder:
    """16 kHz mono Opus at 32 kbps, 20 ms a packet. The encoder picks speech or music
    coding itself: SAME headers came through best that way."""

    def __init__(self, bitrate: int = ARCHIVE_BITRATE) -> None:
        self.opus = _opus()
        error = ctypes.c_int()
        self.encoder = self.opus.opus_encoder_create(ARCHIVE_SAMPLE_RATE, 1, OPUS_APPLICATION_AUDIO, ctypes.byref(error))
        if error.value != 0 or not self.encoder:
            raise StreamArchiveError(f"Opus encoder couldn't start ({error.value})")
        for request, value in ((OPUS_SET_BITRATE, bitrate), (OPUS_SET_VBR, 1), (OPUS_SET_COMPLEXITY, 10)):
            if self.opus.opus_encoder_ctl(self.encoder, request, ctypes.c_int(value)) != 0:
                raise StreamArchiveError("Opus encoder rejected a setting")
        lookahead = ctypes.c_int()
        self.opus.opus_encoder_ctl(self.encoder, OPUS_GET_LOOKAHEAD, ctypes.byref(lookahead))
        # Samples of encoder delay: a sample heard at n comes out of the decoder at n + lookahead.
        self.lookahead = int(lookahead.value)
        self.output = ctypes.create_string_buffer(1500)

    def encode(self, frame: np.ndarray) -> bytes:
        frame = np.ascontiguousarray(frame, dtype=np.int16)
        length = self.opus.opus_encode(self.encoder, frame.ctypes.data, ARCHIVE_FRAME_SAMPLES, self.output, len(self.output))
        if length < 0:
            raise StreamArchiveError(f"Opus encoding failed ({length})")
        return self.output.raw[:length]

    def close(self) -> None:
        if self.encoder:
            self.opus.opus_encoder_destroy(self.encoder)
            self.encoder = None


class ArchiveOpusDecoder:
    def __init__(self) -> None:
        self.opus = _opus()
        error = ctypes.c_int()
        self.decoder = self.opus.opus_decoder_create(ARCHIVE_SAMPLE_RATE, 1, ctypes.byref(error))
        if error.value != 0 or not self.decoder:
            raise StreamArchiveError(f"Opus decoder couldn't start ({error.value})")
        self.pcm = np.zeros(5760, dtype=np.int16)

    def decode(self, packet: bytes) -> np.ndarray:
        count = self.opus.opus_decode(self.decoder, packet, len(packet), self.pcm.ctypes.data, len(self.pcm), 0)
        if count < 0:
            # A damaged packet: keep time going with the decoder's own concealment.
            count = self.opus.opus_decode(self.decoder, None, 0, self.pcm.ctypes.data, ARCHIVE_FRAME_SAMPLES, 0)
            if count < 0:
                return np.zeros(ARCHIVE_FRAME_SAMPLES, dtype=np.int16)
        return self.pcm[:count].copy()

    def close(self) -> None:
        if self.decoder:
            self.opus.opus_decoder_destroy(self.decoder)
            self.decoder = None


# ----- Ogg ---------------------------------------------------------------------------------

_BIT_REVERSE = bytes(int(f"{value:08b}"[::-1], 2) for value in range(256))


def ogg_crc(data: bytes) -> int:
    """Ogg's CRC-32 (polynomial 0x04C11DB7, not reflected, starting at 0), through zlib's
    reflected one: reverse each byte's bits going in, and the result's bits coming out."""
    raw = zlib.crc32(data.translate(_BIT_REVERSE), 0xFFFFFFFF) ^ 0xFFFFFFFF
    return int(f"{raw:032b}"[::-1], 2)


def ogg_page(packets: list[bytes], granule: int, serial: int, sequence: int, flags: int = 0) -> bytes:
    lacing = bytearray()
    for packet in packets:
        lacing += b"\xff" * (len(packet) // 255) + bytes([len(packet) % 255])
    if len(lacing) > 255:
        raise StreamArchiveError("too many packets for one Ogg page")
    header = b"OggS" + struct.pack("<BBqIIIB", 0, flags, granule, serial, sequence, 0, len(lacing)) + bytes(lacing)
    body = b"".join(packets)
    crc = ogg_crc(header + body)
    return header[:22] + struct.pack("<I", crc) + header[26:] + body


def opus_head(pre_skip: int) -> bytes:
    # Version 1, one channel, pre-skip (48 kHz samples), the original rate, no gain, mapping 0.
    return b"OpusHead" + struct.pack("<BBHIhB", 1, 1, pre_skip, ARCHIVE_SAMPLE_RATE, 0, 0)


def opus_tags(comments: dict[str, str]) -> bytes:
    vendor = b"NWR Stream Manager"
    body = struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", len(comments))
    for key, value in comments.items():
        entry = f"{key}={value}".encode("utf-8")
        body += struct.pack("<I", len(entry)) + entry
    return b"OpusTags" + body


def parse_opus_tags(packet: bytes) -> dict[str, str]:
    comments: dict[str, str] = {}
    try:
        if not packet.startswith(b"OpusTags"):
            return comments
        position = 8
        (vendor_length,) = struct.unpack_from("<I", packet, position)
        position += 4 + vendor_length
        (count,) = struct.unpack_from("<I", packet, position)
        position += 4
        for _ in range(count):
            (length,) = struct.unpack_from("<I", packet, position)
            position += 4
            key, _, value = packet[position : position + length].decode("utf-8", "replace").partition("=")
            comments[key] = value
            position += length
    except struct.error:
        pass
    return comments


@dataclass
class OggOpusContents:
    pre_skip: int  # 16 kHz samples
    tags: dict[str, str]
    packets: list[bytes]
    final_granule: int | None  # 48 kHz, from an end-of-stream page
    complete: bool


def read_ogg_opus(path: Path) -> OggOpusContents:
    """Every page up to the first damaged or missing one: a file cut short by a crash
    still gives everything before that."""
    data = path.read_bytes()
    packets: list[bytes] = []
    partial = b""
    position = 0
    final_granule: int | None = None
    complete = False
    while position + 27 <= len(data):
        if data[position : position + 4] != b"OggS":
            break
        count = data[position + 26]
        lacing = data[position + 27 : position + 27 + count]
        size = 27 + count + sum(lacing)
        if len(lacing) < count or position + size > len(data):
            break
        page = data[position : position + size]
        if ogg_crc(page[:22] + b"\0\0\0\0" + page[26:]) != struct.unpack_from("<I", page, 22)[0]:
            break
        body, offset = page[27 + count :], 0
        for value in lacing:
            partial += body[offset : offset + value]
            offset += value
            if value < 255:
                packets.append(partial)
                partial = b""
        if page[5] & 4:
            final_granule = struct.unpack_from("<q", page, 6)[0]
            complete = True
        position += size
    if len(packets) < 2 or not packets[0].startswith(b"OpusHead"):
        raise StreamArchiveError(f"{path.name} isn't an Ogg Opus file")
    pre_skip = struct.unpack_from("<H", packets[0], 10)[0] // GRANULE_PER_SAMPLE
    return OggOpusContents(pre_skip, parse_opus_tags(packets[1]), packets[2:], final_granule, complete)


@dataclass
class _PageTable:
    """Where each page of an archive file starts, so a seek reads only the pages it needs."""
    file_size: int = -1
    scanned: int = 0  # bytes of good pages
    packets_before: list[int] = field(default_factory=list)  # packets, headers included, before each page
    offsets: list[int] = field(default_factory=list)
    packets: int = 0
    open_packet: bool = False  # the last page scanned ends partway through a packet


PAGE_TABLES_KEPT = 64
_PAGE_TABLES: "OrderedDict[str, _PageTable]" = OrderedDict()
_PAGE_TABLES_LOCK = threading.Lock()


def _page_table(path: Path) -> _PageTable:
    """The file's pages, scanned once and then only as far as it has grown since."""
    key = str(path)
    size = path.stat().st_size
    with _PAGE_TABLES_LOCK:
        table = _PAGE_TABLES.get(key)
        if table is not None:
            _PAGE_TABLES.move_to_end(key)
    if table is not None and table.file_size == size:
        return table
    if table is None or size < table.file_size or table.open_packet:
        table = _PageTable()
    else:
        table = _PageTable(size, table.scanned, list(table.packets_before), list(table.offsets), table.packets)
    table.file_size = size
    with path.open("rb") as source:
        source.seek(table.scanned)
        position = table.scanned
        while True:
            header = source.read(27)
            if len(header) < 27 or header[:4] != b"OggS":
                break
            lacing = source.read(header[26])
            if len(lacing) < header[26]:
                break
            body = source.read(sum(lacing))
            if len(body) < sum(lacing):
                break
            if ogg_crc(header[:22] + b"\0\0\0\0" + header[26:] + lacing + body) != struct.unpack_from("<I", header, 22)[0]:
                break
            if not table.open_packet:
                table.packets_before.append(table.packets)
                table.offsets.append(position)
            ends = sum(1 for value in lacing if value < 255)
            table.packets += ends
            if lacing:
                table.open_packet = lacing[-1] == 255
            position += 27 + len(lacing) + len(body)
        table.scanned = position
    with _PAGE_TABLES_LOCK:
        _PAGE_TABLES[key] = table
        _PAGE_TABLES.move_to_end(key)
        while len(_PAGE_TABLES) > PAGE_TABLES_KEPT:
            _PAGE_TABLES.popitem(last=False)
    return table


def ogg_opus_packets(path: Path, first: int = 0) -> Iterator[bytes]:
    """A file's audio packets from the first'th on, read from the page it's in: up to the
    first damaged or missing page, as read_ogg_opus does."""
    table = _page_table(path)
    wanted = first + 2  # after OpusHead and OpusTags
    if not table.offsets or wanted >= table.packets:
        return
    index = max(0, bisect_right(table.packets_before, wanted) - 1)
    number = table.packets_before[index]
    with path.open("rb") as source:
        source.seek(table.offsets[index])
        position = table.offsets[index]
        partial = b""
        while position < table.scanned:
            header = source.read(27)
            lacing = source.read(header[26])
            body = source.read(sum(lacing))
            position += 27 + len(lacing) + len(body)
            offset = 0
            for value in lacing:
                partial += body[offset : offset + value]
                offset += value
                if value < 255:
                    if number >= wanted:
                        yield partial
                    number += 1
                    partial = b""


class OggOpusWriter:
    """Writes one Ogg Opus file a page at a time, flushing each to the file as it's made."""

    def __init__(self, path: Path, *, serial: int, pre_skip: int, tags: dict[str, str]) -> None:
        self.path = path
        self.serial = serial & 0xFFFFFFFF
        self.pre_skip = pre_skip
        self.file = open(path, "xb")
        os.chmod(path, 0o600)
        self.sequence = 0
        self.pending: list[bytes] = []
        self.packets = 0
        self.bytes = 0
        # What's on disk so far, so a download's size can be worked out without reading it.
        self.written_packets = 0
        self.payload_bytes = 0
        self.lacing_extra = 0
        self._write(ogg_page([opus_head(pre_skip * GRANULE_PER_SAMPLE)], 0, self.serial, 0, flags=2))
        self._write(ogg_page([opus_tags(tags)], 0, self.serial, 1))
        self.last_sync = time.monotonic()

    def _write(self, page: bytes) -> None:
        self.file.write(page)
        self.file.flush()
        self.bytes += len(page)
        self.sequence += 1

    def add(self, packet: bytes) -> None:
        self.pending.append(packet)
        self.packets += 1
        if len(self.pending) >= PAGE_FRAMES:
            self._write_pending()

    def _write_pending(self, *, last: bool = False, final_granule: int | None = None) -> None:
        # A page's granule position counts every decoded sample so far, the pre-skip included.
        granule = self.packets * ARCHIVE_FRAME_SAMPLES * GRANULE_PER_SAMPLE
        if last and final_granule is not None:
            granule = final_granule
        self._write(ogg_page(self.pending, granule, self.serial, self.sequence, flags=4 if last else 0))
        self.written_packets += len(self.pending)
        self.payload_bytes += sum(len(packet) for packet in self.pending)
        self.lacing_extra += sum(len(packet) // 255 for packet in self.pending)
        self.pending = []
        if time.monotonic() - self.last_sync >= 60:
            os.fsync(self.file.fileno())
            self.last_sync = time.monotonic()

    def close(self, final_granule: int | None = None) -> None:
        if self.file.closed:
            return
        try:
            self._write_pending(last=True, final_granule=final_granule)
            os.fsync(self.file.fileno())
        finally:
            self.file.close()


# ----- What's in the archive ---------------------------------------------------------------

@dataclass
class ArchiveFile:
    name: str
    start_ms: int  # when its first sample was heard
    samples: int  # 16 kHz samples of audio it holds
    segment: str  # the segment it belongs to
    first_packet: int  # its first packet's place in the segment
    lookahead: int  # the segment encoder's delay, in samples
    bytes: int
    complete: bool
    # Its packets, their bytes, and the extra lacing values the longer ones need (one for
    # each 255 bytes): what a download of it adds up to, without reading it again.
    packets: int = 0
    payload_bytes: int = 0
    lacing_extra: int = 0

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.samples * 1000 // ARCHIVE_SAMPLE_RATE


def _contents_samples(contents: OggOpusContents) -> int:
    decoded = len(contents.packets) * ARCHIVE_FRAME_SAMPLES
    if contents.final_granule is not None:
        decoded = min(decoded, contents.final_granule // GRANULE_PER_SAMPLE)
    return max(0, decoded - contents.pre_skip)


class StreamArchiveStore:
    """One stream's archive folder: which files there are and what each holds."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.lock = threading.RLock()
        self.files: dict[str, ArchiveFile] = {}
        self.open_file: str | None = None
        self.loaded = False
        # Files being downloaded, and by how many downloads: not deleted until they're done.
        self.held: dict[str, int] = {}

    def load(self) -> None:
        with self.lock:
            if self.loaded:
                return
            self.loaded = True
            known: dict[str, dict[str, Any]] = {}
            try:
                raw = json.loads((self.directory / INDEX_FILE_NAME).read_text(encoding="utf-8"))
                known = {str(item["name"]): item for item in raw.get("files", []) if isinstance(item, dict)}
            except (OSError, ValueError, KeyError, TypeError):
                pass
            if not self.directory.is_dir():
                return
            changed = False
            for path in sorted(self.directory.iterdir()):
                if not FILE_PATTERN.match(path.name):
                    continue
                item = known.get(path.name)
                if item and item.get("complete") and int(item.get("bytes", -1)) == path.stat().st_size:
                    try:
                        # An entry missing anything (from an earlier version) is read again.
                        self.files[path.name] = ArchiveFile(**{key: item[key] for key in ArchiveFile.__dataclass_fields__})
                        continue
                    except (KeyError, TypeError):
                        pass
                # Not in the index, or not finished (NWR Stream Manager stopped while writing it).
                entry = self._scan(path)
                changed = True
                if entry is not None:
                    self.files[path.name] = entry
            if changed or len(known) != len(self.files):
                self._save()

    def _scan(self, path: Path) -> ArchiveFile | None:
        try:
            contents = read_ogg_opus(path)
            tags = contents.tags
            start_ms = int(tags.get("NWRSM_START_MS") or FILE_PATTERN.match(path.name).group(1))
            first_packet = int(tags.get("NWRSM_FIRST_PACKET", "0"))
            lookahead = int(tags.get("NWRSM_LOOKAHEAD", "0"))
            return ArchiveFile(
                name=path.name,
                start_ms=start_ms,
                samples=_contents_samples(contents),
                segment=tags.get("NWRSM_SEGMENT", path.name),
                first_packet=first_packet,
                lookahead=lookahead,
                bytes=path.stat().st_size,
                complete=True,
                packets=len(contents.packets),
                payload_bytes=sum(len(packet) for packet in contents.packets),
                lacing_extra=sum(len(packet) // 255 for packet in contents.packets),
            )
        except (OSError, StreamArchiveError, ValueError, AttributeError) as exc:
            LOG.warning("archived audio file %s couldn't be read: %s", path, exc)
            return None

    def _save(self) -> None:
        if not self.directory.is_dir():
            return
        files = [asdict(item) for item in sorted(self.files.values(), key=lambda item: item.start_ms) if item.name != self.open_file]
        atomic_write_json(self.directory / INDEX_FILE_NAME, {"files": files})

    def begin_file(self, entry: ArchiveFile) -> None:
        with self.lock:
            self.load()
            self.files[entry.name] = entry
            self.open_file = entry.name

    def update_file(self, name: str, *, samples: int, writer: "OggOpusWriter", complete: bool = False) -> None:
        with self.lock:
            entry = self.files.get(name)
            if entry is None:
                return
            entry.samples = samples
            entry.bytes = writer.bytes
            entry.packets = writer.written_packets
            entry.payload_bytes = writer.payload_bytes
            entry.lacing_extra = writer.lacing_extra
            entry.complete = complete
            if complete:
                if self.open_file == name:
                    self.open_file = None
                self._save()

    def entries(self) -> list[ArchiveFile]:
        with self.lock:
            self.load()
            return sorted((ArchiveFile(**asdict(item)) for item in self.files.values()), key=lambda item: item.start_ms)

    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.entries())

    def has_audio(self) -> bool:
        return any(item.samples > 0 for item in self.entries())

    def bytes_older_than(self, cutoff_ms: int) -> int:
        return sum(item.bytes for item in self.entries() if item.end_ms <= cutoff_ms and item.name != self.open_file)

    def prune(self, cutoff_ms: int, *, include_held: bool = False) -> int:
        """Deletes every file that ends before the cutoff; returns how many bytes that freed.
        Files being played or downloaded are kept unless include_held, as when someone asks
        for audio to be deleted: those then go on in silence."""
        freed = 0
        with self.lock:
            self.load()
            for name, entry in list(self.files.items()):
                if name == self.open_file or entry.end_ms > cutoff_ms or (self.held.get(name) and not include_held):
                    continue
                try:
                    (self.directory / name).unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    LOG.warning("couldn't delete archived audio %s: %s", name, exc)
                    continue
                freed += entry.bytes
                del self.files[name]
            if freed:
                self._save()
        return freed

    def hold(self, names: list[str]) -> None:
        with self.lock:
            for name in names:
                self.held[name] = self.held.get(name, 0) + 1

    def release(self, names: list[str]) -> None:
        with self.lock:
            for name in names:
                count = self.held.get(name, 0) - 1
                if count > 0:
                    self.held[name] = count
                else:
                    self.held.pop(name, None)

    def delete_all(self) -> None:
        with self.lock:
            for name in list(self.files):
                try:
                    (self.directory / name).unlink()
                except OSError:
                    pass
            self.files = {}
            self.open_file = None
            try:
                (self.directory / INDEX_FILE_NAME).unlink()
            except OSError:
                pass
            try:
                self.directory.rmdir()
            except OSError:
                pass

    def ranges(self) -> list[tuple[int, int]]:
        """Times with archived audio, as (start, end) in milliseconds: one per segment."""
        spans: list[tuple[int, int]] = []
        last_segment = None
        for item in self.entries():
            if item.samples <= 0:
                continue
            if spans and item.segment == last_segment:
                spans[-1] = (spans[-1][0], max(spans[-1][1], item.end_ms))
            else:
                spans.append((item.start_ms, item.end_ms))
            last_segment = item.segment
        return spans


# ----- Writing -----------------------------------------------------------------------------

class StreamArchiveWriter:
    """Takes a stream's audio as listeners hear it and archives it, on a thread of its own so
    the stream never waits on the disk."""

    def __init__(
        self,
        store: StreamArchiveStore,
        *,
        input_sample_rate: int = IQ_SAMPLE_RATE,
        label: str = "",
        clock: Callable[[], float] = time.time,
        block_seconds: int = BLOCK_SECONDS,
    ) -> None:
        self.store = store
        self.input_sample_rate = int(input_sample_rate)
        self.label = label
        self.clock = clock
        self.block_seconds = block_seconds
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_FRAMES)
        self.dropped = threading.Event()
        self.closing = threading.Event()
        self.error: str | None = None
        self.thread = threading.Thread(target=self._run, name=f"stream-archive-{label or 'stream'}", daemon=True)
        self._segment = False
        self.store.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.store.directory, 0o700)
        self.thread.start()

    # Called by the stream for each frame it sends its listeners.
    def write(self, pcm: bytes) -> None:
        if self.closing.is_set():
            return
        try:
            self.queue.put_nowait((self.clock(), pcm))
        except queue.Full:
            # The disk has fallen behind: what's lost is a break in the archive.
            self.dropped.set()

    def close(self, timeout: float = 10.0) -> None:
        self.closing.set()
        try:
            self.queue.put(None, timeout=timeout)
        except queue.Full:
            pass
        self.thread.join(timeout=timeout)

    def _run(self) -> None:
        try:
            while True:
                try:
                    item = self.queue.get(timeout=IDLE_SECONDS)
                except queue.Empty:
                    # The stream sent nothing for a while: end the segment, so its file is finished.
                    self._end_segment()
                    continue
                if item is None:
                    break
                if self.dropped.is_set():
                    self.dropped.clear()
                    LOG.warning("stream archive for %s fell behind; some audio wasn't archived", self.label)
                    self._end_segment()
                arrived, pcm = item
                self._take(arrived, pcm)
        except Exception as exc:
            self.error = str(exc)
            LOG.warning("stream archive for %s stopped: %s", self.label, exc)
        finally:
            try:
                self._end_segment()
            except Exception as exc:
                LOG.warning("stream archive for %s couldn't finish its file: %s", self.label, exc)

    def _take(self, arrived: float, pcm: bytes) -> None:
        count = len(pcm) // 2
        if count <= 0:
            return
        seconds = count / self.input_sample_rate
        if self._segment:
            # How late this frame is against the segment's own clock, less the usual lateness.
            expected = self.anchor + (self.input_samples + count) / self.input_sample_rate
            lag = arrived - expected
            if abs(lag - self.lag) > GAP_SECONDS:
                self._end_segment()
            else:
                # Follows slow drift between the SDR's clock and this computer's.
                self.lag += (lag - self.lag) * 0.01
        if not self._segment:
            self._start_segment(arrived - seconds)
        self.input_samples += count
        audio = self.resampler.process(pcm)
        if audio:
            self._encode(np.frombuffer(audio, dtype="<i2"))

    def _start_segment(self, anchor: float) -> None:
        self._segment = True
        self.anchor = anchor
        self.lag = 0.0
        self.input_samples = 0
        self.resampler = PcmResampler(self.input_sample_rate, ARCHIVE_SAMPLE_RATE)
        self.encoder = ArchiveOpusEncoder()
        self.pending = np.zeros(0, dtype=np.int16)
        self.archived_samples = 0  # 16 kHz samples sent to the encoder
        self.packet_index = 0
        self.segment_id = ""
        self.file: OggOpusWriter | None = None
        self.file_entry: ArchiveFile | None = None
        self._open_file()

    def _sample_time(self, sample: int) -> float:
        """When the segment's sample (at 16 kHz) was heard."""
        return self.anchor + self.lag + sample / ARCHIVE_SAMPLE_RATE

    def _open_file(self) -> None:
        lookahead = self.encoder.lookahead
        first_sample = max(0, self.packet_index * ARCHIVE_FRAME_SAMPLES - lookahead)
        start_ms = int(round(self._sample_time(first_sample) * 1000))
        name = f"{start_ms:013d}.opus"
        suffix = 1
        while (self.store.directory / name).exists():
            name = f"{start_ms:013d}-{suffix}.opus"
            suffix += 1
        if not self.segment_id:
            self.segment_id = name
        tags = {
            "NWRSM_START_MS": str(start_ms),
            "NWRSM_SEGMENT": self.segment_id,
            "NWRSM_FIRST_PACKET": str(self.packet_index),
            "NWRSM_LOOKAHEAD": str(lookahead),
            "TITLE": f"{self.label} archive".strip(),
        }
        # Only a segment's first file starts with the encoder's delay to skip.
        pre_skip = lookahead if self.packet_index == 0 else 0
        self.file = OggOpusWriter(
            self.store.directory / name, serial=start_ms ^ (self.packet_index << 8), pre_skip=pre_skip, tags=tags
        )
        self.file_entry = ArchiveFile(
            name=name,
            start_ms=start_ms,
            samples=0,
            segment=self.segment_id,
            first_packet=self.packet_index,
            lookahead=lookahead,
            bytes=self.file.bytes,
            complete=False,
        )
        self.file_start_packet = self.packet_index
        self.block = math.floor(start_ms / 1000 / self.block_seconds)
        self.store.begin_file(self.file_entry)

    def _file_samples(self, real_end: int | None = None) -> int:
        """Audio samples the open file holds: decoded samples less any pre-skip, up to the
        segment's real end once that's known."""
        decoded_end = self.packet_index * ARCHIVE_FRAME_SAMPLES
        if real_end is not None:
            decoded_end = min(decoded_end, real_end + self.encoder.lookahead)
        start = self.file_start_packet * ARCHIVE_FRAME_SAMPLES
        if self.file_start_packet == 0:
            start += self.encoder.lookahead
        return max(0, decoded_end - start)

    def _encode(self, samples: np.ndarray) -> None:
        self.pending = np.concatenate((self.pending, samples)) if len(self.pending) else samples
        self.archived_samples += len(samples)
        frames = len(self.pending) // ARCHIVE_FRAME_SAMPLES
        for index in range(frames):
            frame = self.pending[index * ARCHIVE_FRAME_SAMPLES : (index + 1) * ARCHIVE_FRAME_SAMPLES]
            self._add_packet(self.encoder.encode(frame))
        self.pending = self.pending[frames * ARCHIVE_FRAME_SAMPLES :].copy()

    def _add_packet(self, packet: bytes) -> None:
        # A new hour starts a new file, carrying on with the same encoder.
        first_sample = max(0, self.packet_index * ARCHIVE_FRAME_SAMPLES - self.encoder.lookahead)
        block = math.floor(self._sample_time(first_sample) / self.block_seconds)
        if block != self.block and self.packet_index > self.file_start_packet:
            self._close_file()
            self._open_file()
        self.file.add(packet)
        self.packet_index += 1
        if not self.file.pending:
            # A page was just written: the index says what's on disk now.
            self.store.update_file(self.file_entry.name, samples=self._file_samples(), writer=self.file)

    def _close_file(self, real_end: int | None = None) -> None:
        if self.file is None:
            return
        final_granule = None
        if real_end is not None:
            # Trims the file's last packet to where the audio really ended.
            decoded_end = real_end + self.encoder.lookahead - self.file_start_packet * ARCHIVE_FRAME_SAMPLES
            final_granule = max(0, decoded_end) * GRANULE_PER_SAMPLE
        self.file.close(final_granule)
        samples = self._file_samples(real_end)
        self.store.update_file(self.file_entry.name, samples=samples, writer=self.file, complete=True)
        if samples <= 0:
            # Nothing heard made it in; don't keep an empty file.
            try:
                (self.store.directory / self.file_entry.name).unlink()
            except OSError:
                pass
            with self.store.lock:
                self.store.files.pop(self.file_entry.name, None)
                self.store._save()
        self.file = None

    def _end_segment(self) -> None:
        if not self._segment:
            return
        self._segment = False
        try:
            tail = self.resampler.flush()
            if tail:
                self._encode(np.frombuffer(tail, dtype="<i2"))
            real_end = self.archived_samples
            # Push the last samples out of the encoder: the rest of the frame, then one more.
            padding = (-len(self.pending)) % ARCHIVE_FRAME_SAMPLES + ARCHIVE_FRAME_SAMPLES
            self.pending = np.concatenate((self.pending, np.zeros(padding, dtype=np.int16)))
            frames = len(self.pending) // ARCHIVE_FRAME_SAMPLES
            for index in range(frames):
                frame = self.pending[index * ARCHIVE_FRAME_SAMPLES : (index + 1) * ARCHIVE_FRAME_SAMPLES]
                self.file.add(self.encoder.encode(frame))
                self.packet_index += 1
            self.pending = np.zeros(0, dtype=np.int16)
            self._close_file(real_end)
        finally:
            self.encoder.close()


# ----- Exporting ---------------------------------------------------------------------------

@dataclass
class _Piece:
    """Part of one segment that's in an export: its files, and which of its samples."""
    files: list[ArchiveFile]
    start_sample: int  # in the segment, as heard
    end_sample: int
    start_ms: float  # when start_sample was heard


def _segment_sample_ms(entry: ArchiveFile, sample: int) -> float:
    first = max(0, entry.first_packet * ARCHIVE_FRAME_SAMPLES - entry.lookahead)
    return entry.start_ms + (sample - first) * 1000 / ARCHIVE_SAMPLE_RATE


def _file_first_sample(entry: ArchiveFile) -> int:
    return max(0, entry.first_packet * ARCHIVE_FRAME_SAMPLES - entry.lookahead)


def export_pieces(entries: list[ArchiveFile], start_ms: int, end_ms: int) -> list[_Piece]:
    segments: dict[str, list[ArchiveFile]] = {}
    for entry in entries:
        if entry.samples > 0:
            segments.setdefault(entry.segment, []).append(entry)
    pieces = []
    for files in segments.values():
        files.sort(key=lambda item: item.first_packet)
        chosen = [item for item in files if item.end_ms > start_ms and item.start_ms < end_ms]
        if not chosen:
            continue
        first, last = chosen[0], chosen[-1]
        start_sample = _file_first_sample(first) + max(0, round((start_ms - first.start_ms) * ARCHIVE_SAMPLE_RATE / 1000))
        last_first = _file_first_sample(last)
        end_sample = min(
            last_first + last.samples,
            last_first + max(0, round((end_ms - last.start_ms) * ARCHIVE_SAMPLE_RATE / 1000)),
        )
        if end_sample <= start_sample:
            continue
        # Files of a segment before the chosen ones may be needed to settle the decoder.
        index = files.index(first)
        files_used = files[max(0, index - 1) :]
        files_used = [item for item in files_used if item.first_packet <= (end_sample + item.lookahead) // ARCHIVE_FRAME_SAMPLES]
        pieces.append(_Piece(files_used, start_sample, end_sample, _segment_sample_ms(first, start_sample)))
    pieces.sort(key=lambda piece: piece.start_ms)
    return pieces


def _piece_packets(directory: Path, piece: _Piece) -> Iterator[tuple[int, bytes]]:
    """The segment's packets from just before the piece through its end, with each one's
    place in the segment, read a file at a time."""
    lookahead = piece.files[0].lookahead
    first_packet = max(0, (piece.start_sample + lookahead) // ARCHIVE_FRAME_SAMPLES - PREROLL_FRAMES)
    last_packet = (piece.end_sample + lookahead - 1) // ARCHIVE_FRAME_SAMPLES
    for entry in piece.files:
        if entry.first_packet > last_packet:
            break
        if entry.packets and entry.first_packet + entry.packets <= first_packet:
            continue  # all before it
        start = max(0, first_packet - entry.first_packet)
        try:
            packets = ogg_opus_packets(directory / entry.name, start)
            number = entry.first_packet + start
            for packet in packets:
                if number > last_packet:
                    return
                yield number, packet
                number += 1
        except FileNotFoundError:
            continue  # deleted since: it's silent


def export_pcm(directory: Path, entries: list[ArchiveFile], start_ms: int, end_ms: int) -> Iterator[np.ndarray]:
    """The archived audio from start to end, 16 kHz, decoded straight across each segment's
    files, with silence for any time nothing was archived. Exactly (end - start) long."""
    total = round((end_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
    position = 0
    for piece in export_pieces(entries, start_ms, end_ms):
        offset = round((piece.start_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
        if offset > position:
            yield from _silence(min(offset, total) - position)
            position = min(offset, total)
        skip = max(0, position - offset)  # overlapping the piece before, by a little clock drift
        for chunk in _piece_pcm(directory, piece, skip, total - position):
            yield chunk
            position += len(chunk)
        if position >= total:
            return
    if position < total:
        yield from _silence(total - position)


def _piece_pcm(directory: Path, piece: _Piece, skip: int, limit: int) -> Iterator[np.ndarray]:
    """The piece's samples from skip on, up to limit of them, with silence for any file
    that's missing so the audio after it keeps its place."""
    count = max(0, min(limit, piece.end_sample - piece.start_sample - skip))
    given = 0
    wanted = piece.start_sample + skip  # the segment's next sample to give
    decoder = ArchiveOpusDecoder()
    lookahead = piece.files[0].lookahead
    try:
        for number, packet in _piece_packets(directory, piece):
            if given >= count:
                return
            pcm = decoder.decode(packet)
            # Decoded sample k of the segment is what was heard at k - lookahead.
            first = number * ARCHIVE_FRAME_SAMPLES - lookahead
            low = max(first, wanted)
            high = min(first + len(pcm), piece.end_sample)
            if high <= low:
                continue
            if low > wanted:
                missing = min(low - wanted, count - given)
                yield from _silence(missing)
                given += missing
            chunk = pcm[low - first : high - first][: count - given]
            wanted = high
            if len(chunk):
                yield chunk
                given += len(chunk)
    finally:
        decoder.close()
    if given < count:
        yield from _silence(count - given)


def _silence(count: int) -> Iterator[np.ndarray]:
    while count > 0:
        size = min(count, ARCHIVE_SAMPLE_RATE * 10)
        yield np.zeros(size, dtype=np.int16)
        count -= size


def export_wav(directory: Path, entries: list[ArchiveFile], start_ms: int, end_ms: int) -> tuple[int, Iterator[bytes]]:
    total = round((end_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
    data_bytes = total * 2
    header = (
        b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, ARCHIVE_SAMPLE_RATE, ARCHIVE_SAMPLE_RATE * 2, 2, 16)
        + b"data" + struct.pack("<I", data_bytes)
    )

    def chunks() -> Iterator[bytes]:
        yield header
        buffer = bytearray()
        for pcm in export_pcm(directory, entries, start_ms, end_ms):
            buffer += pcm.astype("<i2").tobytes()
            if len(buffer) >= 256 * 1024:
                yield bytes(buffer)
                buffer.clear()
        if buffer:
            yield bytes(buffer)

    return len(header) + data_bytes, chunks()


MP3_FRAME_SAMPLES = 576  # MPEG-2 Layer III, as 16 kHz is
# 64 kbps at 16 kHz is exactly 72 * 64000 / 16000 bytes a frame, so no frame is ever padded.
MP3_FRAME_BYTES = 72 * MP3_EXPORT_BITRATE_KBPS * 1000 // ARCHIVE_SAMPLE_RATE


def mp3_export_bytes(samples: int) -> int:
    """How big LAME's MP3 of that many samples is, before it's made: the frames the samples
    fill, plus two for the encoder's delay and its last partial frame."""
    return (math.ceil(samples / MP3_FRAME_SAMPLES) + 2) * MP3_FRAME_BYTES


def _silent_mp3_frame(header: bytes) -> bytes:
    """A frame of silence: the header LAME uses, then side information and data all zero,
    which borrows nothing from the frames before it."""
    return header[:4] + bytes(MP3_FRAME_BYTES - 4)


def export_mp3(directory: Path, entries: list[ArchiveFile], start_ms: int, end_ms: int) -> tuple[int, Iterator[bytes]]:
    from .config import IcecastConfig
    from .encoder import Mp3Encoder

    total = round((end_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
    size = mp3_export_bytes(total)

    def chunks() -> Iterator[bytes]:
        config = IcecastConfig(
            host="", port=0, mount="", username="", password="", format="mp3",
            sample_rate=ARCHIVE_SAMPLE_RATE, bitrate=MP3_EXPORT_BITRATE_KBPS,
        )
        encoder = Mp3Encoder(config, input_sample_rate=ARCHIVE_SAMPLE_RATE)
        sent = 0
        header = b""
        try:
            buffer = bytearray()
            for pcm in export_pcm(directory, entries, start_ms, end_ms):
                buffer += encoder.encode(pcm.astype("<i2").tobytes())
                if len(buffer) >= 64 * 1024:
                    header = header or bytes(buffer[:4])
                    piece = bytes(buffer[: size - sent])
                    sent += len(piece)
                    yield piece
                    buffer.clear()
            buffer += encoder.flush()
            header = header or bytes(buffer[:4])
            # Exactly the size promised: LAME makes just that, but this holds it to it.
            piece = bytes(buffer[: size - sent])
            sent += len(piece)
            if piece:
                yield piece
            while sent < size:
                frame = _silent_mp3_frame(header)[: size - sent]
                sent += len(frame)
                yield frame
        finally:
            encoder.close()

    return size, chunks()


# ----- Playing --------------------------------------------------------------------------
#
# The page plays archived audio as one long MP3 that's never stored: every browser with an
# audio player plays MP3, and only a plain audio player keeps playing on a locked iPhone.
# Each frame is exactly MP3_FRAME_BYTES and MP3_FRAME_SAMPLES long, so a byte is a time;
# a browser seeking asks for bytes from there, and they're made then. LAME's bit reservoir
# is off, so each frame stands alone and MP3 made from there joins what came before it as
# cleanly as if it had all been made at once.

PLAYBACK_GAP_MS = 60_000  # gaps up to this long play as silence; longer ones are left out
PLAYBACK_PREROLL_FRAMES = 3  # made and dropped first, for LAME to settle as it was there
PLAYBACK_BLOCK_FRAMES = 250  # 9 seconds, made whole whatever part of it is asked for
# Before a player's audio: LAME's own delay, then the decoder's.
PLAYBACK_DELAY_SAMPLES = MP3_FRAME_SAMPLES + 529


@dataclass(frozen=True)
class PlaybackPart:
    """A stretch of the player's audio: one segment's samples, or a short gap's silence.
    A segment's samples are played straight through as they were archived; the clock only
    names them, since the receiver's clock and the computer's drift apart a little."""
    position: int  # where it starts in the player's audio
    samples: int
    start_ms: float  # when it was heard, or when the gap began
    files: tuple[ArchiveFile, ...] = ()  # none for silence
    first_sample: int = 0  # its first sample in the segment


def playback_parts(entries: list[ArchiveFile]) -> list[PlaybackPart]:
    segments: dict[str, list[ArchiveFile]] = {}
    for entry in entries:
        if entry.samples > 0:
            segments.setdefault(entry.segment, []).append(entry)
    runs = sorted((sorted(files, key=lambda item: item.first_packet) for files in segments.values()), key=lambda files: files[0].start_ms)
    parts: list[PlaybackPart] = []
    position = 0
    previous_end: float | None = None
    for files in runs:
        first = _file_first_sample(files[0])
        samples = _file_first_sample(files[-1]) + files[-1].samples - first
        if samples <= 0:
            continue
        if previous_end is not None and 0 < files[0].start_ms - previous_end <= PLAYBACK_GAP_MS:
            silence = round((files[0].start_ms - previous_end) * ARCHIVE_SAMPLE_RATE / 1000)
            parts.append(PlaybackPart(position, silence, previous_end))
            position += silence
        parts.append(PlaybackPart(position, samples, float(files[0].start_ms), tuple(files), first))
        position += samples
        previous_end = max(previous_end or 0, files[-1].end_ms)
    return parts


def playback_samples(parts: list[PlaybackPart]) -> int:
    return parts[-1].position + parts[-1].samples if parts else 0


def playback_bytes(parts: list[PlaybackPart]) -> int:
    return mp3_export_bytes(playback_samples(parts))


def playback_marks(parts: list[PlaybackPart]) -> list[tuple[int, int]]:
    """(sample in the player's audio, when it was heard) at the start of each part and each
    file in it; the times between follow at 16,000 samples a second."""
    marks = []
    for part in parts:
        marks.append((part.position, round(part.start_ms)))
        for item in part.files[1:]:
            marks.append((part.position + _file_first_sample(item) - part.first_sample, item.start_ms))
    return marks


def playback_time_ms(marks: list[tuple[int, int]], sample: int) -> float:
    """When the player's sample was heard."""
    index = max(0, bisect_right([mark[0] for mark in marks], sample) - 1)
    position, start_ms = marks[index]
    return start_ms + (sample - position) * 1000 / ARCHIVE_SAMPLE_RATE


def playback_position(parts: list[PlaybackPart], when_ms: float) -> int | None:
    """Where in the player's audio a time was heard, or None if nothing was archived then."""
    for part in parts:
        if not part.files or not part.files[0].start_ms <= when_ms < part.files[-1].end_ms:
            continue
        # The file it's in, or the next one if it falls the odd millisecond between two.
        item = max((item for item in part.files if item.start_ms <= when_ms), key=lambda item: item.start_ms)
        start = part.position + _file_first_sample(item) - part.first_sample
        offset = round((when_ms - item.start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
        return start + max(0, min(offset, item.samples - 1))
    return None


def playback_audio_spans(parts: list[PlaybackPart]) -> list[tuple[int, int]]:
    """When the player's audio was heard, as (start, end) in milliseconds."""
    return [(part.files[0].start_ms, part.files[-1].end_ms) for part in parts if part.files]


def playback_files(parts: list[PlaybackPart], first: int, last: int) -> list[str]:
    """The files the player's samples first through last are made from, with the ones just
    before that the decoder starts from."""
    names = []
    for part in parts:
        for item in part.files:
            start = part.position + _file_first_sample(item) - part.first_sample
            if start + item.samples > first - ARCHIVE_SAMPLE_RATE and start <= last:
                names.append(item.name)
    return names


def playback_skips(parts: list[PlaybackPart]) -> list[tuple[int, int, int]]:
    """Gaps left out of the player's audio: (where, from, to), times in milliseconds."""
    skips = []
    for before, after in zip(parts, parts[1:]):
        end_ms = before.files[-1].end_ms if before.files else before.start_ms + before.samples * 1000 / ARCHIVE_SAMPLE_RATE
        if after.start_ms - end_ms > PLAYBACK_GAP_MS:
            skips.append((after.position, round(end_ms), round(after.start_ms)))
    return skips


def _playback_pcm(directory: Path, parts: list[PlaybackPart], first: int) -> Iterator[np.ndarray]:
    for part in parts:
        if part.position + part.samples <= first:
            continue
        offset = max(0, first - part.position)
        if not part.files:
            yield from _silence(part.samples - offset)
            continue
        piece = _Piece(list(part.files), part.first_sample + offset, part.first_sample + part.samples, part.start_ms)
        yield from _piece_pcm(directory, piece, 0, part.samples - offset)


_MP3_HEADER: bytes | None = None


def _mp3_header() -> bytes:
    """The header LAME gives each frame, for silent frames made without it."""
    global _MP3_HEADER
    if _MP3_HEADER is None:
        encoder = _playback_encoder()
        try:
            _MP3_HEADER = (encoder.encode(bytes(MP3_FRAME_SAMPLES * 6)) + encoder.flush())[:4]
        finally:
            encoder.close()
    return _MP3_HEADER


def _playback_encoder():
    from .config import IcecastConfig
    from .encoder import Mp3Encoder

    config = IcecastConfig(
        host="", port=0, mount="", username="", password="", format="mp3",
        sample_rate=ARCHIVE_SAMPLE_RATE, bitrate=MP3_EXPORT_BITRATE_KBPS,
    )
    return Mp3Encoder(config, input_sample_rate=ARCHIVE_SAMPLE_RATE, bit_reservoir=False)


def _playback_block(directory: Path, parts: list[PlaybackPart], block: int) -> bytes:
    """One block of the player's MP3, made from the same start every time it's asked for."""
    total_frames = playback_bytes(parts) // MP3_FRAME_BYTES
    first = block * PLAYBACK_BLOCK_FRAMES
    count = min(PLAYBACK_BLOCK_FRAMES, total_frames - first)
    preroll = min(PLAYBACK_PREROLL_FRAMES, first)
    wanted = (preroll + count) * MP3_FRAME_BYTES
    encoder = _playback_encoder()
    pcm_chunks = _playback_pcm(directory, parts, (first - preroll) * MP3_FRAME_SAMPLES)
    made = bytearray()
    try:
        for pcm in pcm_chunks:
            made += encoder.encode(pcm.astype("<i2").tobytes())
            if len(made) >= wanted:
                break
        else:
            made += encoder.flush()
    finally:
        pcm_chunks.close()
        encoder.close()
    data = bytes(made[preroll * MP3_FRAME_BYTES : wanted])
    # Frames past what LAME made, at the very end, are silent.
    missing = count - len(data) // MP3_FRAME_BYTES
    return data + _silent_mp3_frame(_mp3_header()) * missing


def playback_mp3(directory: Path, parts: list[PlaybackPart], first_byte: int, last_byte: int) -> Iterator[bytes]:
    """Bytes first_byte through last_byte of the player's MP3, made as they're sent. A
    browser may join them to bytes it got earlier anywhere, even partway through a frame,
    so each block is the same whichever request made it."""
    last_byte = min(last_byte, playback_bytes(parts) - 1)
    block_bytes = PLAYBACK_BLOCK_FRAMES * MP3_FRAME_BYTES
    for block in range(first_byte // block_bytes, last_byte // block_bytes + 1):
        data = _playback_block(directory, parts, block)
        base = block * block_bytes
        yield data[max(0, first_byte - base) : last_byte - base + 1]


EXPORT_PAGE_FRAMES = 40  # at most 6 lacing values a packet, so a page never needs more than 255
_SILENCE_PACKET: bytes | None = None


def _silence_packet() -> bytes:
    """One packet of silence, the same every time, so gaps add a size known in advance."""
    global _SILENCE_PACKET
    if _SILENCE_PACKET is None:
        encoder = ArchiveOpusEncoder()
        try:
            zeros = np.zeros(ARCHIVE_FRAME_SAMPLES, dtype=np.int16)
            for _ in range(20):
                packet = encoder.encode(zeros)
            _SILENCE_PACKET = packet
        finally:
            encoder.close()
    return _SILENCE_PACKET


@dataclass
class _Run:
    """Packets for an Opus download: some of one archive file's, or of silence."""
    entry: ArchiveFile | None
    first: int  # the first one in the file
    count: int


class _OggPacketStream:
    """Packets into Ogg pages, handed back as each page fills."""

    def __init__(self, serial: int) -> None:
        self.serial = serial
        self.sequence = 2
        self.pending: list[bytes] = []
        self.decoded = 0

    def add(self, packet: bytes) -> bytes:
        self.pending.append(packet)
        self.decoded += ARCHIVE_FRAME_SAMPLES
        if len(self.pending) < EXPORT_PAGE_FRAMES:
            return b""
        return self._page()

    def _page(self, final_granule: int | None = None) -> bytes:
        granule = self.decoded * GRANULE_PER_SAMPLE if final_granule is None else final_granule
        page = ogg_page(self.pending, granule, self.serial, self.sequence, flags=4 if final_granule is not None else 0)
        self.pending = []
        self.sequence += 1
        return page

    def finish(self, final_granule: int) -> bytes:
        return self._page(final_granule)


def _first_packet(piece: _Piece) -> int:
    lookahead = piece.files[0].lookahead
    return max(0, (piece.start_sample + lookahead) // ARCHIVE_FRAME_SAMPLES - PREROLL_FRAMES)


def _opus_plan(entries: list[ArchiveFile], start_ms: int, end_ms: int) -> tuple[int, int, list[_Run]]:
    """Which packets an Opus download is made of, in order: its pre-skip, its length, and its runs."""
    total = round((end_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
    pieces = export_pieces(entries, start_ms, end_ms)
    pre_skip = 0
    if pieces and round((pieces[0].start_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000) <= 0:
        # Starting partway into a segment: the decoder settles on a few earlier packets,
        # which the pre-skip then hides.
        first = pieces[0]
        pre_skip = first.start_sample + first.files[0].lookahead - _first_packet(first) * ARCHIVE_FRAME_SAMPLES
    end = pre_skip + total
    runs: list[_Run] = []
    decoded = 0
    for piece in pieces:
        lookahead = piece.files[0].lookahead
        offset = round((piece.start_ms - start_ms) * ARCHIVE_SAMPLE_RATE / 1000)
        first = _first_packet(piece)
        # Silence until the piece's first wanted sample comes out where it belongs.
        wanted = piece.start_sample + lookahead - first * ARCHIVE_FRAME_SAMPLES
        frames = round((pre_skip + offset - wanted - decoded) / ARCHIVE_FRAME_SAMPLES)
        if frames > 0:
            runs.append(_Run(None, 0, frames))
            decoded += frames * ARCHIVE_FRAME_SAMPLES
        last = (piece.end_sample + lookahead - 1) // ARCHIVE_FRAME_SAMPLES
        needed = max(0, math.ceil((end - decoded) / ARCHIVE_FRAME_SAMPLES))
        number, stop = first, min(last + 1, first + needed)
        for entry in piece.files:
            low = max(number, entry.first_packet)
            high = min(stop, entry.first_packet + entry.packets)
            if high > low:
                runs.append(_Run(entry, low - entry.first_packet, high - low))
                decoded += (high - low) * ARCHIVE_FRAME_SAMPLES
                number = high
    if decoded < end:
        runs.append(_Run(None, 0, math.ceil((end - decoded) / ARCHIVE_FRAME_SAMPLES)))
    return pre_skip, total, runs


def _opus_head_pages(pre_skip: int, serial: int) -> bytes:
    return (
        ogg_page([opus_head(pre_skip * GRANULE_PER_SAMPLE)], 0, serial, 0, flags=2)
        + ogg_page([opus_tags({"ENCODER": "NWR Stream Manager"})], 0, serial, 1)
    )


def export_opus(directory: Path, entries: list[ArchiveFile], start_ms: int, end_ms: int) -> tuple[int, Iterator[bytes]]:
    """The archived packets themselves, copied into one Ogg Opus file without re-encoding.
    Times nothing was archived become silence; the file starts and ends on the samples asked for.

    Its size is worked out first, from what the index says each file holds; only the files a
    download starts or ends partway into are read to see their packets' sizes."""
    pre_skip, total, runs = _opus_plan(entries, start_ms, end_ms)
    serial = int(start_ms) & 0xFFFFFFFF
    silence = _silence_packet()
    packets = payload = lacing_extra = 0
    for run in runs:
        packets += run.count
        if run.entry is None:
            payload += run.count * len(silence)
            lacing_extra += run.count * (len(silence) // 255)
        elif run.first == 0 and run.count == run.entry.packets:
            payload += run.entry.payload_bytes
            lacing_extra += run.entry.lacing_extra
        else:
            chosen = read_ogg_opus(directory / run.entry.name).packets[run.first : run.first + run.count]
            if len(chosen) != run.count:
                raise StreamArchiveError("Archived audio was changed while the download was being prepared.")
            payload += sum(len(packet) for packet in chosen)
            lacing_extra += sum(len(packet) // 255 for packet in chosen)
    head = _opus_head_pages(pre_skip, serial)
    # Every page has a 27-byte header and a lacing value per packet (more for long ones);
    # the last page, written at the end, may have no packets at all.
    size = len(head) + 27 * (packets // EXPORT_PAGE_FRAMES + 1) + packets + lacing_extra + payload

    def chunks() -> Iterator[bytes]:
        yield head
        stream = _OggPacketStream(serial)
        sent = len(head)
        loaded_name, loaded = "", []
        for run in runs:
            if run.entry is None:
                chosen = [silence] * run.count
            else:
                if run.entry.name != loaded_name:
                    loaded_name, loaded = run.entry.name, read_ogg_opus(directory / run.entry.name).packets
                chosen = loaded[run.first : run.first + run.count]
                if len(chosen) != run.count:
                    raise StreamArchiveError("Archived audio was changed during the download.")
            for packet in chosen:
                page = stream.add(packet)
                if page:
                    sent += len(page)
                    yield page
        page = stream.finish((pre_skip + total) * GRANULE_PER_SAMPLE)
        sent += len(page)
        if sent != size:
            raise StreamArchiveError(f"Opus download came to {sent} bytes, not the {size} promised")
        yield page

    return size, chunks()


# ----- Times for the download form ---------------------------------------------------------

def parse_local_datetime(date_text: str, time_text: str) -> float:
    """A date and time typed on the page, in this computer's time zone, as epoch seconds."""
    date_text = (date_text or "").strip()
    time_text = (time_text or "").strip().upper().replace(".", "")
    date = None
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            date = datetime.strptime(date_text, pattern).date()
            break
        except ValueError:
            continue
    if date is None:
        raise ValueError("Enter dates as YYYY-MM-DD, such as 2026-10-23.")
    moment = None
    for pattern in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p", "%I:%M%p", "%I %p", "%I%p"):
        try:
            moment = datetime.strptime(time_text, pattern).time()
            break
        except ValueError:
            continue
    if moment is None:
        raise ValueError("Enter times as HH:MM or HH:MM:SS, such as 17:40, 17:40:30 or 5:40 PM.")
    return datetime.combine(date, moment).astimezone().timestamp()
