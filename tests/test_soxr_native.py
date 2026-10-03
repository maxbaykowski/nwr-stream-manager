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


def load_module(name: str):
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module(f"nwr_stream_manager.{name}")


class SoxrNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.soxr_native = load_module("soxr_native")
        cls.encoder = load_module("encoder")

    @staticmethod
    def _stream_through(resample, data: np.ndarray, sizes: list[int]) -> list[np.ndarray]:
        parts, position = [], 0
        for size in sizes:
            parts.append(resample(data[position : position + size], False))
            position += size
        parts.append(resample(data[position:], True))
        return parts

    @unittest.skipUnless(importlib.util.find_spec("soxr"), "the soxr Python package is not installed")
    def test_matches_the_soxr_python_package_it_replaces(self) -> None:
        import soxr

        rng = np.random.default_rng(5)
        audio = (0.3 * rng.standard_normal(24_000 * 3)).astype(np.float32)
        pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16)
        sizes = ([480] * 20 + [17, 1, 3000, 999]) * 2
        for dtype, data, tolerance in (("float32", audio, 1e-5), ("int16", pcm, 1)):
            for output_rate in (8_000, 16_000, 22_050, 44_100, 48_000):
                with self.subTest(dtype=dtype, output_rate=output_rate):
                    package = soxr.ResampleStream(24_000, output_rate, 1, dtype=dtype)
                    native = self.soxr_native.SoxrStream(24_000, output_rate, dtype)
                    expected = self._stream_through(lambda x, last: package.resample_chunk(x, last=last), data, sizes)
                    actual = self._stream_through(lambda x, last: native.process(x, last=last), data, sizes)

                    # Same pieces at the same time, so nothing downstream sees a timing change.
                    self.assertEqual([part.size for part in actual], [part.size for part in expected])
                    np.testing.assert_allclose(
                        np.concatenate(actual).astype(np.float64),
                        np.concatenate(expected).astype(np.float64),
                        rtol=0,
                        atol=tolerance,  # int16: the package's own libsoxr build rounds slightly differently
                    )

    def test_16_bit_output_is_the_same_every_time(self) -> None:
        # libsoxr's default dither is seeded from the clock and the resampler's address,
        # which made the same audio come out slightly different every time it was loaded.
        rng = np.random.default_rng(8)
        pcm = np.clip(rng.standard_normal(48_000) * 6000, -32768, 32767).astype(np.int16)
        first = self.soxr_native.SoxrStream(48_000, 24_000, "int16")
        second = self.soxr_native.SoxrStream(48_000, 24_000, "int16")

        np.testing.assert_array_equal(first.process(pcm, last=True), second.process(pcm, last=True))

    def test_resamples_a_tone_cleanly_and_flushes_the_rest(self) -> None:
        stream = self.soxr_native.SoxrStream(24_000, 22_050, "float32")
        tone = (0.5 * np.sin(2 * np.pi * 1000.0 * np.arange(24_000) / 24_000)).astype(np.float32)

        parts = [stream.process(tone[index : index + 480]) for index in range(0, tone.size, 480)]
        parts.append(stream.process(np.zeros(0, dtype=np.float32), last=True))
        output = np.concatenate(parts).astype(np.float64)

        self.assertEqual(output.size, 22_050)
        settled = output[2_000:-2_000]
        spectrum = np.abs(np.fft.rfft(settled * np.hanning(settled.size)))
        frequencies = np.fft.rfftfreq(settled.size, 1 / 22_050)
        self.assertAlmostEqual(float(frequencies[np.argmax(spectrum)]), 1000.0, delta=3.0)
        self.assertAlmostEqual(float(np.max(np.abs(settled))), 0.5, delta=0.005)
        with self.assertRaises(RuntimeError):
            stream.process(tone[:480])

    def test_pcm_resampler_keeps_its_interface(self) -> None:
        resampler = self.encoder.PcmResampler(24_000, 48_000)
        pcm = (np.sin(2 * np.pi * 440.0 * np.arange(24_000) / 24_000) * 10_000).astype("<i2").tobytes()

        output = resampler.process(pcm) + resampler.flush()

        self.assertIsInstance(output, bytes)
        self.assertEqual(len(output), 48_000 * 2)
        self.assertEqual(self.encoder.PcmResampler(24_000, 24_000).process(pcm), pcm)


if __name__ == "__main__":
    unittest.main()
