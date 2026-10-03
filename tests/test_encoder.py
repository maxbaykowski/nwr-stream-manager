from __future__ import annotations

import importlib
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_modules():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return (
        importlib.import_module("nwr_stream_manager.config"),
        importlib.import_module("nwr_stream_manager.encoder"),
    )


class EncoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_module, cls.encoder_module = load_modules()

    def _mp3_config(self, sample_rate: int = 24_000, bitrate: int = 64):
        return self.config_module.IcecastConfig(
            host="example.invalid",
            port=8000,
            mount="/test.mp3",
            username="source",
            password="secret",
            format="mp3",
            sample_rate=sample_rate,
            bitrate=bitrate,
        )

    @staticmethod
    def _test_pcm(seconds: int = 4) -> bytes:
        rng = np.random.default_rng(4)
        t = np.arange(24_000 * seconds) / 24_000
        audio = (0.3 * np.sin(2 * np.pi * 440 * t) + 0.05 * rng.standard_normal(t.size)) * 32767 * 0.6
        return audio.astype("<i2").tobytes()

    def _encode(self, encoder, pcm: bytes) -> bytes:
        frames = [pcm[index : index + 960] for index in range(0, len(pcm), 960)]  # 20 ms at 24 kHz
        return b"".join(encoder.encode(frame) for frame in frames) + encoder.flush()

    def test_mp3_encoder_writes_constant_bitrate_mono_frames_with_libmp3lame(self) -> None:
        encoder = self.encoder_module.Mp3Encoder(self._mp3_config(24_000, 64))
        try:
            encoded = self._encode(encoder, self._test_pcm(4))
        finally:
            encoder.close()

        # MPEG audio frame header: 11-bit sync, then channel mode 3 (mono) in the 4th byte.
        self.assertEqual(encoded[0], 0xFF)
        self.assertEqual(encoded[1] & 0xE0, 0xE0)
        self.assertEqual(encoded[3] >> 6, 3)
        # Constant 64 kbps for 4 seconds is 32,000 bytes, give or take LAME's start and end.
        self.assertAlmostEqual(len(encoded), 32_000, delta=1_500)

    @unittest.skipUnless(importlib.util.find_spec("lameenc"), "the lameenc Python package is not installed")
    def test_mp3_encoder_output_is_identical_to_lameenc_it_replaced(self) -> None:
        import lameenc

        pcm = self._test_pcm(3)
        for sample_rate, bitrate in ((24_000, 64), (22_050, 32), (44_100, 128)):
            with self.subTest(sample_rate=sample_rate, bitrate=bitrate):
                encoder = self.encoder_module.Mp3Encoder(self._mp3_config(sample_rate, bitrate))
                try:
                    native = self._encode(encoder, pcm)
                finally:
                    encoder.close()
                reference = lameenc.Encoder()
                reference.set_bit_rate(bitrate)
                reference.set_in_sample_rate(sample_rate)
                reference.set_out_sample_rate(sample_rate)
                reference.set_channels(1)
                reference.set_quality(2)
                resampler = self.encoder_module.PcmResampler(24_000, sample_rate)
                expected = bytearray()
                for index in range(0, len(pcm), 960):
                    resampled = resampler.process(pcm[index : index + 960])
                    if resampled:
                        expected += reference.encode(resampled)
                tail = resampler.flush()
                if tail:
                    expected += reference.encode(tail)
                expected += reference.flush()

                self.assertEqual(native, bytes(expected))

    def test_mp3_encoder_can_be_closed_twice_and_flushes_nothing_after(self) -> None:
        encoder = self.encoder_module.Mp3Encoder(self._mp3_config())
        encoder.encode(self._test_pcm(1))
        encoder.close()
        encoder.close()
        self.assertEqual(encoder.flush(), b"")

    def test_ogg_vorbis_encoder_uses_system_libraries(self) -> None:
        config = self.config_module.IcecastConfig(
            host="example.invalid",
            port=8000,
            mount="/test.ogg",
            username="source",
            password="secret",
            format="ogg",
            sample_rate=24_000,
            bitrate=48,
        )
        try:
            encoder = self.encoder_module.OggVorbisEncoder(config)
        except self.encoder_module.EncoderError as exc:
            self.skipTest(str(exc))
        try:
            header = encoder.header
            self.assertTrue(header.startswith(b"OggS"))
            samples = np.zeros(24_000, dtype="<i2")
            encoded = encoder.encode(samples.tobytes())
            self.assertEqual(encoder.header, header)
            encoded += encoder.flush()
            self.assertEqual(encoder.header, header)
        finally:
            encoder.close()
        self.assertIn(b"OggS", encoded)
        self.assertGreater(len(encoded), 0)


if __name__ == "__main__":
    unittest.main()
