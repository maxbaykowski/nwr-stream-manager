from __future__ import annotations

import ctypes
import importlib
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

from test_auth import load_web_control_module


RATE = 24_000  # the stream's own rate
START = 1792771190.0  # ten seconds before the top of an hour


def tone(seconds: float, offset: float = 0.0) -> np.ndarray:
    """Speech-band audio with some movement in it, at the stream's rate."""
    t = np.arange(int(seconds * RATE)) / RATE + offset
    signal = 0.3 * np.sin(2 * np.pi * 440 * t) + 0.2 * np.sin(2 * np.pi * 1050 * t) * (np.sin(2 * np.pi * 0.5 * t) > 0)
    return (signal * 32767 * 0.5).astype("<i2")


class Clock:
    def __init__(self, start: float = START) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class StreamArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.sa = importlib.import_module("nwr_stream_manager.stream_archive")

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, self.directory, True)

    def archive(self, pieces, *, block_seconds: int = 60, start: float = START):
        """Feeds the writer 20 ms frames as a stream does; pieces are audio arrays, or
        numbers of seconds nothing arrived for."""
        store = self.sa.StreamArchiveStore(self.directory)
        clock = Clock(start)
        writer = self.sa.StreamArchiveWriter(store, label="TEST", clock=clock, block_seconds=block_seconds)
        frame = RATE // 50
        for piece in pieces:
            if not isinstance(piece, np.ndarray):
                clock.now += piece
                continue
            for index in range(0, len(piece), frame):
                clock.now += frame / RATE
                writer.write(piece[index : index + frame].tobytes())
                while writer.queue.qsize() > 200:
                    time.sleep(0.002)
        writer.close()
        return store

    def test_settings(self) -> None:
        sa = self.sa
        self.assertEqual(sa.validate_stream_archive_payload({"enabled": True, "retention": "30d"}), sa.StreamArchiveSettings(True, "30d"))
        with self.assertRaises(ValueError):
            sa.validate_stream_archive_payload({"enabled": True, "retention": "2w"})
        self.assertEqual(sa.stream_archive_settings_from_stream({}), sa.StreamArchiveSettings(False, "1w"))
        self.assertEqual([value for value, _label, _seconds in sa.RETENTION_CHOICES], ["24h", "1w", "30d", "180d", "1y"])

    def test_archiving_keeps_a_stream_running_but_isnt_listed_as_an_output(self) -> None:
        stream_config = importlib.import_module("nwr_stream_manager.stream_config")
        page_sections = importlib.import_module("nwr_stream_manager.page_sections")
        stream = {"outputs": [], "eas_recording": {"enabled": False}, "archive": {"enabled": True, "retention": "1w"}}
        self.assertEqual(stream_config.enabled_output_count(stream), 1)
        self.assertEqual(page_sections.stream_output_count(stream), 0)

    def test_ogg_crc_matches_the_ogg_definition(self) -> None:
        def reference(data: bytes) -> int:
            crc = 0
            for byte in data:
                crc ^= byte << 24
                for _ in range(8):
                    crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
            return crc

        for data in (b"", b"OggS", bytes(range(256)) * 3):
            self.assertEqual(self.sa.ogg_crc(data), reference(data))

    def test_hours_join_without_a_break_and_outages_become_segments(self) -> None:
        # 60 s, 30 s with nothing coming, then 40 s; new files every minute stand in for every hour.
        store = self.archive([tone(60), 30, tone(40, 60)])
        files = store.entries()
        segments = {item.segment for item in files}
        self.assertEqual(len(segments), 2)
        # The first segment was split at the top of the minute, carrying on with the same encoder.
        first = [item for item in files if item.segment == files[0].segment]
        self.assertEqual(len(first), 2)
        self.assertEqual(first[1].start_ms, 1792771200000 + 13)  # 20 ms packets: the first one after the boundary
        self.assertGreater(first[1].first_packet, 0)
        ranges = store.ranges()
        self.assertEqual(len(ranges), 2)
        self.assertAlmostEqual((ranges[0][1] - ranges[0][0]) / 1000, 60, delta=0.05)
        self.assertAlmostEqual((ranges[1][1] - ranges[1][0]) / 1000, 40, delta=0.05)
        self.assertAlmostEqual((ranges[1][0] - ranges[0][1]) / 1000, 30, delta=0.1)

    def test_exports_are_exactly_as_long_as_asked_and_fill_outages_with_silence(self) -> None:
        sa = self.sa
        store = self.archive([tone(60), 30, tone(40, 60)])
        start = int(START * 1000) + 5_000
        end = start + 100_000  # crosses the minute join, the outage, and into the second segment
        pcm = np.concatenate(list(sa.export_pcm(self.directory, store.entries(), start, end)))
        self.assertEqual(len(pcm), 100 * 16_000)
        quiet = pcm[(60 - 5 + 1) * 16_000 : (60 - 5 + 29) * 16_000]
        self.assertEqual(np.abs(quiet).max(), 0)
        self.assertGreater(np.abs(pcm[: 50 * 16_000]).mean(), 1000)
        self.assertGreater(np.abs(pcm[-30 * 16_000 :]).mean(), 1000)
        size, chunks = sa.export_wav(self.directory, store.entries(), start, end)
        wav = b"".join(chunks)
        self.assertEqual(len(wav), size)
        self.assertEqual(len(wav), 44 + 100 * 16_000 * 2)

    def test_audio_across_a_join_is_what_one_unbroken_file_would_give(self) -> None:
        sa = self.sa
        store = self.archive([tone(30)], block_seconds=5, start=START - 100)
        files = store.entries()
        self.assertGreater(len(files), 4)
        self.assertEqual(len({item.segment for item in files}), 1)
        # Every packet, decoded straight through with one decoder.
        packets = []
        for item in files:
            packets.extend(sa.read_ogg_opus(self.directory / item.name).packets)
        decoder = sa.ArchiveOpusDecoder()
        whole = np.concatenate([decoder.decode(packet) for packet in packets])[files[0].lookahead :]
        start = files[0].start_ms
        for low, high in ((2_000, 9_000), (10_000, 20_000), (0, 29_000)):
            exported = np.concatenate(list(sa.export_pcm(self.directory, files, start + low, start + high)))
            if low == 0:
                self.assertTrue(np.array_equal(exported, whole[: len(exported)]))
                continue
            # Starting partway in, the decoder settles on a few earlier packets first; and a
            # file's start is kept to the millisecond, so where it starts can be up to 1 ms out.
            def difference(shift: int) -> int:
                reference = whole[low * 16 + shift : high * 16 + shift]
                return int(np.abs(exported[1600:].astype(int) - reference[1600:].astype(int)).max())
            self.assertLess(min(difference(shift) for shift in range(-16, 17)), 64)

    def test_opus_download_is_the_archived_packets_trimmed_to_the_time_asked(self) -> None:
        sa = self.sa
        store = self.archive([tone(20), 10, tone(20, 20)], block_seconds=5)
        start = int(START * 1000) + 3_000
        end = start + 40_000
        size, chunks = sa.export_opus(self.directory, store.entries(), start, end)
        data = b"".join(chunks)
        self.assertEqual(len(data), size)
        path = self.directory / "export.opus"
        path.write_bytes(data)
        contents = sa.read_ogg_opus(path)
        self.assertTrue(contents.complete)
        self.assertEqual(contents.final_granule - contents.pre_skip * 3, 40 * 48_000)
        try:
            opusfile = ctypes.CDLL("libopusfile.so.0")
        except OSError:
            self.skipTest("libopusfile isn't installed")
        opusfile.op_open_file.restype = ctypes.c_void_p
        opusfile.op_open_file.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int)]
        opusfile.op_pcm_total.restype = ctypes.c_int64
        opusfile.op_pcm_total.argtypes = [ctypes.c_void_p, ctypes.c_int]
        opusfile.op_free.argtypes = [ctypes.c_void_p]
        handle = opusfile.op_open_file(str(path).encode(), None)
        self.assertTrue(handle)
        try:
            self.assertEqual(opusfile.op_pcm_total(handle, -1), 40 * 48_000)
        finally:
            opusfile.op_free(handle)

    def test_every_format_is_exactly_the_size_promised_before_it_is_made(self) -> None:
        sa = self.sa
        # Files every 5 s, an outage, and starts and ends partway into files and gaps.
        store = self.archive([tone(23), 7, tone(17, 23)], block_seconds=5)
        entries = store.entries()
        first = entries[0].start_ms
        for low, high in ((0, 47_000), (2_345, 9_876), (20_000, 33_000), (25_000, 28_000), (-4_000, 60_000), (31_500, 31_520)):
            for export in (sa.export_opus, sa.export_mp3, sa.export_wav):
                with self.subTest(export=export.__name__, low=low, high=high):
                    size, chunks = export(self.directory, entries, first + low, first + high)
                    data = b"".join(chunks)
                    self.assertEqual(len(data), size)
        self.assertEqual(sa.mp3_export_bytes(16_000), 30 * 288)

    def test_mp3_is_held_to_its_size_with_silent_frames(self) -> None:
        sa = self.sa
        frame = sa._silent_mp3_frame(bytes.fromhex("fff388c4"))
        self.assertEqual(len(frame), 288)
        path = self.directory / "silence.mp3"
        path.write_bytes(frame * 50)
        try:
            import subprocess
            result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-"], capture_output=True)
        except FileNotFoundError:
            self.skipTest("ffmpeg isn't installed")
        self.assertEqual(result.stderr, b"")
        self.assertEqual(max(np.frombuffer(result.stdout, dtype="<i2"), default=0), 0)

    def test_playing_keeps_short_gaps_as_silence_and_leaves_out_long_ones(self) -> None:
        sa = self.sa
        # 20 s, 30 s with nothing, 20 s, 90 s with nothing, 15 s; a file every 10 s.
        store = self.archive([tone(20), 30, tone(20, 20), 90, tone(15, 40)], block_seconds=10, start=START - 100)
        parts = sa.playback_parts(store.entries())
        self.assertEqual([bool(part.files) for part in parts], [True, False, True, True])
        self.assertAlmostEqual(parts[1].samples / 16_000, 30, delta=0.1)
        self.assertAlmostEqual(sa.playback_samples(parts) / 16_000, 85, delta=0.2)
        (skip,) = sa.playback_skips(parts)
        self.assertEqual(skip[0], parts[3].position)
        self.assertAlmostEqual((skip[2] - skip[1]) / 1000, 90, delta=0.1)
        # Each file's own start names the times in it.
        marks = sa.playback_marks(parts)
        self.assertGreater(len(marks), len(parts))
        for part in parts:
            for item in part.files:
                position = part.position + sa._file_first_sample(item) - part.first_sample
                self.assertAlmostEqual(sa.playback_time_ms(marks, position + 8_000), item.start_ms + 500, delta=1)

    def test_a_time_is_found_in_the_player_only_where_audio_was_archived(self) -> None:
        sa = self.sa
        store = self.archive([tone(20), 30, tone(20, 20), 90, tone(15, 40)], block_seconds=10, start=START - 100)
        parts = sa.playback_parts(store.entries())
        for part in parts:
            for item in part.files:
                start = part.position + sa._file_first_sample(item) - part.first_sample
                self.assertEqual(sa.playback_position(parts, item.start_ms + 2_500), start + 40_000)
                self.assertAlmostEqual(sa.playback_time_ms(sa.playback_marks(parts), start + 40_000), item.start_ms + 2_500, delta=0.1)
        spans = sa.playback_audio_spans(parts)
        self.assertEqual(len(spans), 3)
        # Before it, in the short gap played as silence, in the long one, and after it.
        for when in (spans[0][0] - 1, spans[0][1] + 10_000, spans[1][1] + 45_000, spans[2][1]):
            self.assertIsNone(sa.playback_position(parts, when))

    def test_the_player_gets_the_same_bytes_whichever_request_makes_them(self) -> None:
        sa = self.sa
        store = self.archive([tone(20), 30, tone(20, 20), 90, tone(15, 40)], block_seconds=10, start=START - 100)
        parts = sa.playback_parts(store.entries())
        size = sa.playback_bytes(parts)
        self.assertEqual(size % sa.MP3_FRAME_BYTES, 0)
        whole = b"".join(sa.playback_mp3(self.directory, parts, 0, size - 1))
        self.assertEqual(len(whole), size)
        # Browsers seek, and join what they get to what they had, anywhere, even partway
        # through a frame or a block; the end is held to its size like a download's.
        cuts = sorted({1, 287, 288, 72_000, 72_001, size // 3, size // 2 + 17, size - 300, size - 1})
        joined, first = b"", 0
        for cut in cuts + [size]:
            joined += b"".join(sa.playback_mp3(self.directory, parts, first, cut - 1))
            first = cut
        self.assertEqual(joined, whole)
        self.assertEqual(b"".join(sa.playback_mp3(self.directory, parts, size - 10, size + 1000)), whole[-10:])

    def test_a_player_goes_on_in_silence_where_a_file_has_been_deleted(self) -> None:
        sa = self.sa
        store = self.archive([tone(30)], block_seconds=10, start=START - 100)
        parts = sa.playback_parts(store.entries())
        size = sa.playback_bytes(parts)
        before = b"".join(sa.playback_mp3(self.directory, parts, 0, size - 1))
        (self.directory / store.entries()[1].name).unlink()
        after = b"".join(sa.playback_mp3(self.directory, parts, 0, size - 1))
        self.assertEqual(len(after), size)
        self.assertEqual(after[:72_000], before[:72_000])
        self.assertNotEqual(after, before)

    def test_files_being_downloaded_arent_pruned_until_the_download_is_done(self) -> None:
        store = self.archive([tone(15)], block_seconds=5, start=START - 100)
        files = store.entries()
        store.hold([files[0].name])
        store.prune(files[-1].end_ms + 1)
        self.assertEqual([item.name for item in store.entries()], [files[0].name])
        store.release([files[0].name])
        store.prune(files[-1].end_ms + 1)
        self.assertEqual(store.entries(), [])

    def test_a_file_cut_short_by_a_crash_is_still_read(self) -> None:
        sa = self.sa
        store = self.archive([tone(20)], block_seconds=3600, start=START - 100)
        (name,) = [item.name for item in store.entries()]
        path = self.directory / name
        data = path.read_bytes()
        path.write_bytes(data[: len(data) * 2 // 3])
        (self.directory / sa.INDEX_FILE_NAME).unlink()
        again = sa.StreamArchiveStore(self.directory)
        (entry,) = again.entries()
        self.assertGreater(entry.samples, 10 * 16_000)
        self.assertLess(entry.samples, 15 * 16_000)
        pcm = np.concatenate(list(sa.export_pcm(self.directory, again.entries(), entry.start_ms, entry.end_ms)))
        self.assertEqual(len(pcm), round((entry.end_ms - entry.start_ms) * 16))

    def test_rapidly_turning_archiving_on_and_off_leaves_good_files(self) -> None:
        sa = self.sa
        store = sa.StreamArchiveStore(self.directory)
        clock = Clock(START - 100)
        audio = tone(2)
        for _ in range(15):
            writer = sa.StreamArchiveWriter(store, label="TEST", clock=clock)
            for index in range(0, len(audio), 480):
                clock.now += 0.02
                writer.write(audio[index : index + 480].tobytes())
            writer.close()
            clock.now += 0.05
        files = store.entries()
        self.assertEqual(len(files), 15)
        self.assertEqual(len({item.name for item in files}), 15)
        for item in files:
            contents = sa.read_ogg_opus(self.directory / item.name)
            self.assertTrue(contents.complete)
            self.assertAlmostEqual(item.samples / 16_000, 2, delta=0.05)

    def test_retention_deletes_whole_files_older_than_the_cutoff(self) -> None:
        store = self.archive([tone(30)], block_seconds=10)
        files = store.entries()
        cutoff = files[1].end_ms
        freed = store.prune(cutoff)
        self.assertEqual(freed, files[0].bytes + files[1].bytes)
        self.assertEqual([item.name for item in store.entries()], [item.name for item in files[2:]])
        self.assertFalse((self.directory / files[0].name).exists())
        self.assertEqual(store.bytes_older_than(cutoff), 0)

    def test_dates_and_times_typed_on_the_page(self) -> None:
        parse = self.sa.parse_local_datetime
        self.assertEqual(parse("2026-10-23", "17:40"), parse("10/23/2026", "5:40 PM"))
        self.assertEqual(parse("2026-10-23", "5:40pm"), parse("2026-10-23", "17:40:00"))
        with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
            parse("tomorrow", "17:40")
        with self.assertRaisesRegex(ValueError, "HH:MM"):
            parse("2026-10-23", "noon")


class StreamArchivingStorageTests(unittest.TestCase):
    """Storage running low pauses archiving, like EAS recording, until there's room again."""

    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.sa = importlib.import_module("nwr_stream_manager.stream_archive")
        cls.stream_worker = importlib.import_module("nwr_stream_manager.stream_worker")

    def test_low_storage_pauses_archiving_and_finishes_its_file(self) -> None:
        sa = self.sa
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        store = sa.StreamArchiveStore(directory)

        class Storage:
            critical = False

            def add_path(self, path): pass
            def recording_started(self, path=None): pass
            def recording_stopped(self): pass
            def is_critical(self, path=None): return self.critical

        worker = self.stream_worker.IcecastStreamWorker.__new__(self.stream_worker.IcecastStreamWorker)
        worker.lock = threading.Lock()
        worker.stream = {"id": "s1", "station": {"callsign": "WXN99"}, "archive": {"enabled": True, "retention": "1w"}}
        worker.storage_monitor = Storage()
        worker.archive_store_provider = lambda stream: store
        worker._sync_archive(worker.stream)
        self.assertEqual(worker.archive_snapshot()["status"], "enabled")
        audio = tone(3)

        def feed():
            for index in range(0, len(audio), 480):
                worker._archive_pcm(worker.archive_writer, audio[index : index + 480].tobytes())
                time.sleep(0.001)

        feed()
        worker.storage_monitor.critical = True
        worker._archive_storage_checked_at = 0.0
        worker._archive_pcm(worker.archive_writer, audio[:480].tobytes())
        self.assertIsNone(worker.archive_writer)
        self.assertEqual(worker.archive_snapshot(), {"status": "paused", "error": self.stream_worker.storage_status_message("critical")})
        (first,) = store.entries()
        self.assertTrue(sa.read_ogg_opus(directory / first.name).complete)
        # Still low: syncing doesn't start it again.
        worker._sync_archive(worker.stream)
        self.assertIsNone(worker.archive_writer)
        self.assertEqual(worker.archive_snapshot()["status"], "paused")
        # Room again: it carries on, in a file of its own.
        worker.storage_monitor.critical = False
        worker._sync_archive(worker.stream)
        self.assertEqual(worker.archive_snapshot()["status"], "enabled")
        feed()
        worker._stop_archive()
        self.assertEqual(len(store.entries()), 2)


class StreamArchiveServiceTests(unittest.TestCase):
    """Settings changes, the confirmation before deleting audio, and downloads, through the service."""

    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.sa = importlib.import_module("nwr_stream_manager.stream_archive")
        cls.mixin = importlib.import_module("nwr_stream_manager.service_stream_archive")

    def service(self, directory: Path, stream: dict):
        mixin = self.mixin

        class Service(mixin.StreamArchiveServiceMixin):
            def __init__(self) -> None:
                self.state_path = directory / "rtl-control.json"
                self.streams_directory = directory / "streams"
                self.streams = [stream]
                self.stream_workers = {}
                self.lock = threading.RLock()
                self.synced = 0
                self._init_stream_archives()

            def _stream_locked(self, stream_id):
                for item in self.streams:
                    if item["id"] == stream_id:
                        return item
                raise ValueError("stream was not found")

            def _sync_stream_workers_locked(self):
                self.synced += 1

            def stream_status(self):
                return {"streams": self.streams}

        mixin.save_streams = lambda *args: None
        return Service()

    def test_a_shorter_time_that_deletes_audio_is_confirmed_first(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99", "frequency": "162.425"}, "outputs": [{"id": "o", "enabled": True}],
                  "archive": {"enabled": True, "retention": "1y"}}
        service = self.service(directory, stream)
        store = service.stream_archive_store("s1")
        # A file from two days ago, and one from now.
        clock = Clock(time.time() - 2 * 86400)
        for _ in range(2):
            writer = self.sa.StreamArchiveWriter(store, label="WXN99", clock=clock)
            audio = tone(2)
            for index in range(0, len(audio), 480):
                clock.now += 0.02
                writer.write(audio[index : index + 480].tobytes())
            writer.close()
            clock.now = time.time() - 5
        self.assertEqual(len(store.entries()), 2)
        with self.assertRaises(self.mixin.StreamArchiveConfirmationRequired) as asked:
            service.update_stream_archive({"stream_id": "s1", "archive": {"enabled": True, "retention": "24h"}})
        self.assertIn("Keeping archived audio for 24 hours deletes", str(asked.exception))
        self.assertEqual(stream["archive"]["retention"], "1y")
        self.assertEqual(len(store.entries()), 2)
        # A longer time deletes nothing, so nothing is asked.
        service.update_stream_archive({"stream_id": "s1", "archive": {"enabled": True, "retention": "180d"}})
        service.update_stream_archive({"stream_id": "s1", "archive": {"enabled": True, "retention": "24h"}, "confirm_delete": True})
        self.assertEqual(stream["archive"]["retention"], "24h")
        self.assertEqual(len(store.entries()), 1)

    def test_archived_audio_is_deleted_by_age_or_all_of_it(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99"}, "outputs": [], "archive": {"enabled": True, "retention": "1y"}}
        service = self.service(directory, stream)
        store = service.stream_archive_store("s1")
        # A file from two days ago, and one from now.
        clock = Clock(time.time() - 2 * 86400)
        for _ in range(2):
            writer = self.sa.StreamArchiveWriter(store, label="WXN99", clock=clock)
            audio = tone(2)
            for index in range(0, len(audio), 480):
                clock.now += 0.02
                writer.write(audio[index : index + 480].tobytes())
            writer.close()
            clock.now = time.time() - 5
        old, new = store.entries()
        # Only what would delete something is offered, with how much.
        choices = service.stream_archive_delete_options("s1")["choices"]
        self.assertEqual([choice["id"] for choice in choices], ["24h", "all"])
        self.assertEqual(choices[0]["label"], "Older than 24 hours")
        self.assertEqual(choices[0]["bytes"], old.bytes)
        self.assertEqual(choices[1]["label"], "Delete all archived audio")
        self.assertEqual(choices[1]["bytes"], old.bytes + new.bytes)
        with self.assertRaisesRegex(ValueError, "no archived audio that old"):
            service.delete_stream_archive_audio("s1", "1w")
        # Asked for, it goes even while it's being played.
        store.hold([old.name])
        result = service.delete_stream_archive_audio("s1", "24h")
        self.assertEqual(result["bytes"], old.bytes)
        self.assertTrue(result["message"].endswith("of archived audio deleted."))
        self.assertEqual([item.name for item in store.entries()], [new.name])
        # All of it: archiving stops for it, finishing its file, and carries on after.
        calls = []

        class Worker:
            def has_archive(self):
                return True

            def with_archive_stopped(self, action):
                calls.append("stopped")
                result = action()
                calls.append("started")
                return result

        service.stream_workers["s1"] = Worker()
        self.assertEqual(service.delete_stream_archive_audio("s1", "all")["bytes"], new.bytes)
        self.assertEqual(calls, ["stopped", "started"])
        self.assertEqual(store.entries(), [])
        self.assertEqual(service.stream_archive_delete_options("s1")["choices"], [])

    def test_archiving_cant_be_turned_off_when_its_all_that_keeps_the_stream_running(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99"}, "outputs": [], "archive": {"enabled": True, "retention": "1w"}}
        service = self.service(directory, stream)
        with self.assertRaisesRegex(ValueError, "At least one output"):
            service.update_stream_archive({"stream_id": "s1", "archive": {"enabled": False, "retention": "1w"}})

    def test_downloads_are_limited_to_12_hours_and_to_archived_times(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99"}, "outputs": [], "archive": {"enabled": True, "retention": "1w"}}
        service = self.service(directory, stream)
        with self.assertRaisesRegex(ValueError, "up to 12 hours"):
            service.stream_archive_export("s1", "2026-10-23", "00:00", "2026-10-23", "12:01", "wav")
        with self.assertRaisesRegex(ValueError, "after the start"):
            service.stream_archive_export("s1", "2026-10-23", "10:00", "2026-10-23", "09:00", "wav")
        with self.assertRaisesRegex(ValueError, "no archived audio between those times"):
            service.stream_archive_export("s1", "2026-10-23", "09:00", "2026-10-23", "10:00", "wav")


    def test_a_download_with_no_archived_audio_says_where_the_audio_is(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99"}, "outputs": [], "archive": {"enabled": True, "retention": "1w"}}
        service = self.service(directory, stream)
        store = service.stream_archive_store("s1")
        # Two seconds two days ago, then nothing until two seconds now.
        clock = Clock(time.time() - 2 * 86400)
        for _ in range(2):
            writer = self.sa.StreamArchiveWriter(store, label="WXN99", clock=clock)
            audio = tone(2)
            for index in range(0, len(audio), 480):
                clock.now += 0.02
                writer.write(audio[index : index + 480].tobytes())
            writer.close()
            clock.now = time.time() - 5

        def export(start: float, seconds: float):
            begin = datetime.fromtimestamp(start)
            finish = datetime.fromtimestamp(start + seconds)
            return service.stream_archive_export(
                "s1", f"{begin:%Y-%m-%d}", f"{begin:%H:%M:%S}", f"{finish:%Y-%m-%d}", f"{finish:%H:%M:%S}", "wav"
            )

        with self.assertRaisesRegex(ValueError, "no archived audio between those times. Nothing was archived from"):
            export(time.time() - 86400, 60)
        with self.assertRaisesRegex(ValueError, "no archived audio between those times. The archive starts at"):
            export(time.time() - 3 * 86400, 60)
        with self.assertRaisesRegex(ValueError, "no archived audio between those times. The archive ends at"):
            export(time.time() + 600, 60)
        # A time that takes in some of the audio is downloaded, its gap silent.
        name, _, length, chunks = export(time.time() - 2 * 86400 - 10, 30)
        self.assertEqual(length, 44 + 2 * 30 * 16000)
        self.assertEqual(sum(len(chunk) for chunk in chunks), length)


    def test_a_player_is_opened_then_played_by_its_address(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99"}, "outputs": [], "archive": {"enabled": True, "retention": "1w"}}
        service = self.service(directory, stream)
        with self.assertRaisesRegex(ValueError, "Nothing has been archived"):
            service.stream_archive_player("s1")
        store = service.stream_archive_store("s1")
        clock = Clock(time.time() - 200)
        writer = self.sa.StreamArchiveWriter(store, label="WXN99", clock=clock)
        audio = tone(5)
        for index in range(0, len(audio), 480):
            clock.now += 0.02
            writer.write(audio[index : index + 480].tobytes())
        writer.close()
        player = service.stream_archive_player("s1")
        self.assertTrue(player["url"].startswith("/stream-archives/play?player="))
        self.assertAlmostEqual(player["seconds"], 5, delta=0.1)
        self.assertEqual(player["skipped"], [])
        self.assertEqual(player["marks"][0]["seconds"], 0)
        self.assertEqual(service.stream_archive_player_bytes(player["id"]), player["bytes"])
        self.assertIsNone(service.stream_archive_player_bytes("someone-elses"))
        data = b"".join(service.stream_archive_play(player["id"], 100, 999))
        self.assertEqual(len(data), 900)
        # Its files are held while it's sent, and let go after.
        self.assertEqual(store.held, {})
        # Jump to time: a time in it, then times with nothing archived.
        def local(epoch_ms: float):
            moment = datetime.fromtimestamp(epoch_ms / 1000)
            return f"{moment:%Y-%m-%d}", f"{moment:%H:%M:%S}"
        start_ms = store.entries()[0].start_ms
        found = service.stream_archive_player_time(player["id"], *local(start_ms + 3_000))
        typed_ms = (start_ms + 3_000) // 1000 * 1000  # to the second, as it's typed
        self.assertAlmostEqual(found["seconds"], (typed_ms - start_ms) / 1000, delta=0.001)
        with self.assertRaisesRegex(self.mixin.StreamArchiveNoAudio, "no archived audio at that time. The archive starts at") as missing:
            service.stream_archive_player_time(player["id"], *local(start_ms - 3_600_000))
        self.assertEqual(missing.exception.next_seconds, 0)
        with self.assertRaisesRegex(self.mixin.StreamArchiveNoAudio, "The archive ends at"):
            service.stream_archive_player_time(player["id"], *local(start_ms + 20_000))
        with self.assertRaisesRegex(ValueError, "hasn't happened yet"):
            service.stream_archive_player_time(player["id"], *local(time.time() * 1000 + 3_600_000))
        with self.assertRaises(self.mixin.StreamArchivePlayerOutdated):
            service.stream_archive_player_time("closed", *local(start_ms))
        # Archived since the player opened: the page opens another, which has it.
        writer = self.sa.StreamArchiveWriter(store, label="WXN99", clock=clock)
        clock.now = time.time() - 20
        for index in range(0, len(audio), 480):
            clock.now += 0.02
            writer.write(audio[index : index + 480].tobytes())
        writer.close()
        newer = store.ranges()[-1][0] + 2_000
        with self.assertRaises(self.mixin.StreamArchivePlayerOutdated):
            service.stream_archive_player_time(player["id"], *local(newer))
        again = service.stream_archive_player("s1")
        self.assertIn("seconds", service.stream_archive_player_time(again["id"], *local(newer)))
        self.assertEqual(len(again["skipped"]), 1)

    def test_only_the_latest_gaps_of_a_minute_or_more_are_listed(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        stream = {"id": "s1", "station": {"callsign": "WXN99", "frequency": "162.425"}, "outputs": [], "archive": {"enabled": True, "retention": "1w"}}
        service = self.service(directory, stream)
        start = int(START * 1000)
        # Eight gaps of two minutes, then three of five seconds.
        ranges, at = [], start
        for length in [120_000] * 8 + [5_000] * 3 + [0]:
            ranges.append([at, at + 600_000])
            at += 600_000 + length
        summary = {"ranges": ranges, "from_text": "", "to_text": "", "storage": "1 MB"}
        service._stream_archive_summary_locked = lambda stream: summary
        archive = service.stream_archive_streams()["streams"][0]
        self.assertEqual(archive["gap_count"], 11)
        self.assertEqual(len(archive["gaps"]), 5)
        self.assertTrue(archive["gaps"][-1].endswith("(2 minutes)"))
        self.assertEqual(archive["gaps_note"], "3 earlier gaps aren't listed. 3 gaps shorter than a minute aren't listed.")
        # The end of a gap on the same day is just its time.
        self.assertEqual(archive["gaps"][0].count(", 20"), 1)


if __name__ == "__main__":
    unittest.main()
