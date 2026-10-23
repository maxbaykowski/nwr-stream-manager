from __future__ import annotations

import importlib
import io
import json
import struct
import tarfile
import tempfile
import unittest
import wave
import zlib
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from test_auth import load_web_control_module


GB = 1_000_000_000
SIGMF = ["sigmf_cf", "sigmf_s16", "sigmf_u8"]


class IqWavDownloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.iq = importlib.import_module("nwr_stream_manager.iq_recorder")

    RECORDING = {
        "sample_rate": 256000,
        "frequency_hz": 162_425_000,
        # Tuesday 13 October 2026, 17:17:05.250 UTC, for 90 seconds.
        "started_at": datetime(2026, 10, 13, 17, 17, 5, 250000, tzinfo=timezone.utc).timestamp(),
        "stopped_at": datetime(2026, 10, 13, 17, 18, 35, 250000, tzinfo=timezone.utc).timestamp(),
    }

    def download(self, floats: list[float], fmt: str, sample_rate: int = 256000) -> bytes:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "recording.cf32"
            path.write_bytes(np.array(floats, dtype="<f4").tobytes())
            recording = {**self.RECORDING, "sample_rate": sample_rate}
            size = self.iq.iq_recording_download_size(path, fmt, recording)
            header, _, trailer = self.iq.iq_download_parts(fmt, recording, path.stat().st_size)
            with path.open("rb") as source:
                data = b"".join(self.iq.convert_iq_recording_chunks(source, fmt, header, trailer))
        self.assertEqual(len(data), size)
        return data

    def test_pcm_wav_files_hold_i_and_q_as_two_channels(self) -> None:
        floats = [-1.0, 0.0, 1.0, 0.5]
        for fmt, width, expected in (
            ("wav_s16", 2, np.array([-32767, 0, 32767, 16384], dtype="<i2").tobytes()),
            ("wav_u8", 1, bytes([0, 128, 255, 191])),
        ):
            with self.subTest(fmt=fmt):
                path = Path(tempfile.mkdtemp()) / "download.wav"
                path.write_bytes(self.download(floats, fmt))
                with wave.open(str(path)) as wav:
                    self.assertEqual(wav.getnchannels(), 2)
                    self.assertEqual(wav.getsampwidth(), width)
                    self.assertEqual(wav.getframerate(), 256000)
                    self.assertEqual(wav.getnframes(), 2)
                    self.assertEqual(wav.readframes(2), expected)

    def test_float_wav_files_are_ieee_float_with_a_fact_chunk(self) -> None:
        floats = [-1.0, 0.0, 1.0, 0.5]
        data = self.download(floats, "wav_cf")
        self.assertEqual(data[:4], b"RIFF")
        self.assertEqual(struct.unpack("<I", data[4:8])[0], len(data) - 8)
        self.assertEqual(data[8:16], b"WAVEfmt ")
        size, tag, channels, rate, byte_rate, block, bits, extra = struct.unpack("<IHHIIHHH", data[16:38])
        self.assertEqual((size, tag, channels, rate, byte_rate, block, bits, extra), (18, 3, 2, 256000, 2048000, 8, 32, 0))
        self.assertEqual(data[38:50], b"fact" + struct.pack("<II", 4, 2))
        self.assertEqual(data[50:58], b"auxi" + struct.pack("<I", 164))
        self.assertEqual(data[222:230], b"data" + struct.pack("<I", 16))
        self.assertEqual(data[230:], np.array(floats, dtype="<f4").tobytes())

    def test_the_auxi_chunk_gives_sdr_software_the_frequency_and_times(self) -> None:
        for fmt in ("wav_cf", "wav_s16", "wav_u8"):
            with self.subTest(fmt=fmt):
                data = self.download([0.0, 0.0], fmt)
                at = data.index(b"auxi")
                self.assertEqual(struct.unpack("<I", data[at + 4 : at + 8])[0], 164)
                start = struct.unpack("<8H", data[at + 8 : at + 24])
                stop = struct.unpack("<8H", data[at + 24 : at + 40])
                # Year, month, day of the week (Sunday is 0), day, hour, minute, second, millisecond,
                # in this computer's local time, as SDRangel writes it.
                local_start = datetime.fromtimestamp(self.RECORDING["started_at"], timezone.utc).astimezone()
                local_stop = datetime.fromtimestamp(self.RECORDING["stopped_at"], timezone.utc).astimezone()
                for moment, fields in ((local_start, start), (local_stop, stop)):
                    self.assertEqual(
                        fields,
                        (moment.year, moment.month, moment.isoweekday() % 7, moment.day, moment.hour, moment.minute, moment.second, 250),
                    )
                centre, sample_rate = struct.unpack("<II", data[at + 40 : at + 48])
                self.assertEqual((centre, sample_rate), (162_425_000, 256000))

    def test_only_formats_that_fit_in_a_wav_file_are_offered(self) -> None:
        def offered(recording_bytes: int) -> list[str]:
            return [item["value"] for item in self.iq.iq_download_formats(recording_bytes, 256000)]

        raw = ["cf", "s16", "u8"]
        self.assertEqual(offered(1 * GB), raw + ["wav_cf", "wav_s16", "wav_u8", "sdriq", *SIGMF])
        self.assertEqual(offered(6 * GB), raw + ["wav_s16", "wav_u8", "sdriq", *SIGMF])
        # The example from the request: a 10 GB recording fits only as 8-bit WAV.
        self.assertEqual(offered(10 * GB), raw + ["wav_u8", "sdriq", *SIGMF])
        self.assertEqual(offered(20 * GB), raw + ["sdriq", *SIGMF])
        # Right at the limit, and one sample past it.
        limit = self.iq.WAV_MAX_FILE_BYTES - len(self.iq.iq_download_parts("wav_cf", {}, 0)[0])
        self.assertIn("wav_cf", offered(limit - limit % 8))
        self.assertNotIn("wav_cf", offered(limit - limit % 8 + 8))
        # Without a sample rate, there's nothing to put in a WAV file's header.
        self.assertEqual([item["value"] for item in self.iq.iq_download_formats(1000, 0)], raw)

    def test_unsigned_8_bit_wav_is_only_offered_at_rates_sdr_touch_plays(self) -> None:
        def offered(sample_rate: int) -> list[str]:
            return [item["value"] for item in self.iq.iq_download_formats(GB, sample_rate)]

        for sample_rate in (256_000, 512_000, 1_024_000):
            with self.subTest(sample_rate=sample_rate):
                self.assertIn("wav_u8", offered(sample_rate))
        for sample_rate in (24_000, 192_000, 384_000, 768_000, 1_536_000):
            with self.subTest(sample_rate=sample_rate):
                self.assertNotIn("wav_u8", offered(sample_rate))
                self.assertIn("wav_s16", offered(sample_rate))
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "recording.cf32"
            path.write_bytes(bytes(16))
            with self.assertRaisesRegex(ValueError, "only available for recordings at 256,000, 512,000 or 1,024,000"):
                self.iq.iq_recording_download_size(path, "wav_u8", {"sample_rate": 1_536_000})

    def test_a_format_that_doesnt_fit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "recording.cf32"
            with path.open("wb") as file:
                file.truncate(6 * GB)  # Sparse, so it takes no real space.
            self.assertEqual(self.iq.iq_recording_download_size(path, "wav_s16", {"sample_rate": 256000}), 3 * GB + 44 + 172)
            with self.assertRaisesRegex(ValueError, "too large for a WAV file"):
                self.iq.iq_recording_download_size(path, "wav_cf", {"sample_rate": 256000})

    def test_sdriq_files_are_what_sdrangel_writes(self) -> None:
        floats = [-1.0, 0.0, 1.0, 0.5]
        data = self.download(floats, "sdriq")
        sample_rate, centre, started_ms, sample_size, filler, crc = struct.unpack("<IQQIII", data[:32])
        self.assertEqual((sample_rate, centre, sample_size, filler), (256000, 162_425_000, 16, 0))
        self.assertEqual(started_ms, round(self.RECORDING["started_at"] * 1000))
        # SDRangel checks a CRC-32 (Boost's crc_32_type, the same as zlib's) of the first 28 bytes.
        self.assertEqual(crc, zlib.crc32(data[:28]))
        self.assertEqual(np.frombuffer(data[32:], dtype="<i2").tolist(), [-32767, 0, 32767, 16384])

    def test_sdriq_has_no_size_limit_but_needs_a_sample_rate(self) -> None:
        self.assertIn("sdriq", [item["value"] for item in self.iq.iq_download_formats(100 * GB, 1_536_000)])
        self.assertNotIn("sdriq", [item["value"] for item in self.iq.iq_download_formats(GB, 0)])
        recording = {"sample_rate": 256000, "frequency_hz": 162475000, "started_at": 0}
        self.assertTrue(self.iq.iq_recording_download_name(recording, "sdriq").endswith("-s16.sdriq"))

    def test_sigmf_archives_hold_a_recording_sigmf_software_can_read(self) -> None:
        floats = [-1.0, 0.0, 1.0, 0.5]
        for fmt, datatype, samples in (
            ("sigmf_cf", "cf32_le", np.array(floats, dtype="<f4").tobytes()),
            ("sigmf_s16", "ci16_le", np.array([-32767, 0, 32767, 16384], dtype="<i2").tobytes()),
            ("sigmf_u8", "cu8", bytes([0, 128, 255, 191])),
        ):
            with self.subTest(fmt=fmt):
                data = self.download(floats, fmt)
                # A whole number of tar records, as tar writes them.
                self.assertEqual(len(data) % tarfile.RECORDSIZE, 0)
                base = self.iq.iq_recording_download_name(self.RECORDING, fmt).removesuffix(".sigmf")
                with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                    self.assertEqual(archive.getnames(), [base, f"{base}/{base}.sigmf-meta", f"{base}/{base}.sigmf-data"])
                    metadata = json.loads(archive.extractfile(f"{base}/{base}.sigmf-meta").read())
                    self.assertEqual(archive.extractfile(f"{base}/{base}.sigmf-data").read(), samples)
                self.assertEqual(metadata["global"]["core:datatype"], datatype)
                self.assertEqual(metadata["global"]["core:sample_rate"], 256000)
                self.assertEqual(metadata["global"]["core:version"], "1.2.0")
                self.assertEqual(
                    metadata["captures"],
                    [{"core:sample_start": 0, "core:frequency": 162_425_000, "core:datetime": "2026-10-13T17:17:05.250Z"}],
                )
                self.assertEqual(metadata["annotations"], [])

    def test_sigmf_archives_over_8_gib_give_the_data_size_in_a_pax_record(self) -> None:
        recording = {**self.RECORDING}
        header, data_bytes, trailer = self.iq.iq_download_parts("sigmf_cf", recording, 10 * GB)
        self.assertEqual(data_bytes, 10 * GB)
        self.assertEqual((len(header) + data_bytes + len(trailer)) % tarfile.RECORDSIZE, 0)
        archive = tarfile.open(fileobj=io.BytesIO(header), mode="r|")
        members = []
        try:
            for member in archive:
                members.append(member)
        except tarfile.ReadError:
            pass  # The samples themselves aren't there.
        self.assertEqual(members[-1].size, 10 * GB)
        self.assertEqual(members[-1].pax_headers.get("size"), str(10 * GB))

    def test_wav_downloads_are_named_wav(self) -> None:
        recording = {"sample_rate": 256000, "frequency_hz": 162475000, "started_at": 0}
        self.assertTrue(self.iq.iq_recording_download_name(recording, "wav_s16").endswith("-s16.wav"))
        self.assertTrue(self.iq.iq_recording_download_name(recording, "s16").endswith("-s16.raw"))

    def test_recording_summaries_list_their_formats(self) -> None:
        summary = self.iq.iq_recording_summary({"id": "a", "bytes": 10 * GB, "sample_rate": 256000})
        self.assertIn({"value": "wav_u8", "label": "Unsigned 8-bit WAV"}, summary["download_formats"])
        self.assertNotIn("wav_s16", [item["value"] for item in summary["download_formats"]])


if __name__ == "__main__":
    unittest.main()
