from __future__ import annotations

import array
import importlib
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_native_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.native")


class NativeConversionTests(unittest.TestCase):
    """The native library gives exactly what the numpy code it replaces gave."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.native = load_native_module()
        cls.rng = np.random.default_rng(7)

    def test_rtl_samples_become_the_same_iq(self) -> None:
        raw = self.rng.integers(0, 256, 20_000, dtype=np.uint8).tobytes()
        values = np.frombuffer(raw, dtype=np.uint8).astype(np.float32)
        centered = (values - 127.5) / 127.5
        expected = centered[0::2] + 1j * centered[1::2]
        for source in (raw, bytearray(raw), memoryview(raw)):
            with self.subTest(source=type(source).__name__):
                out = np.empty(len(raw) // 2, dtype=np.complex64)
                self.native.u8_iq_to_f32(source, out, len(raw) // 2)
                np.testing.assert_array_equal(out, expected.astype(np.complex64))

    def test_float_audio_becomes_the_same_16_bit_samples(self) -> None:
        samples = (self.rng.standard_normal(50_000) * 0.7).astype(np.float32)
        # Past the limits, and exactly halfway between two steps.
        samples[:6] = [2.0, -2.0, 0.5 / 32767, 1.5 / 32767, -0.5 / 32767, -2.5 / 32767]
        rounded = np.empty(samples.size, dtype="<i2")
        self.native.f32_to_s16(samples, rounded, 32767.0)
        np.testing.assert_array_equal(rounded, np.rint(np.clip(samples * 32767.0, -32768, 32767)).astype("<i2"))
        truncated = np.empty(samples.size, dtype="<i2")
        self.native.f32_to_s16(samples, truncated, 32768.0, truncate=True)
        np.testing.assert_array_equal(truncated, np.clip(samples * 32768.0, -32768, 32767).astype("<i2"))

    def test_something_that_isnt_a_number_becomes_silence(self) -> None:
        out = array.array("h", [1, 1])
        self.native.f32_to_s16(array.array("f", [float("nan"), float("inf")]), out, 32767.0)
        self.assertEqual(list(out), [0, 32767])

    def test_16_bit_samples_become_the_same_float_audio(self) -> None:
        pcm = self.rng.integers(-32768, 32768, 10_000, dtype=np.int16)
        out = np.empty(pcm.size, dtype=np.float32)
        self.native.s16_to_f32(pcm.tobytes(), out, 32768.0)
        np.testing.assert_array_equal(out, pcm.astype(np.float32) / 32768.0)

    def test_works_without_numpy_buffers(self) -> None:
        out = array.array("f", bytes(8))
        self.native.u8_iq_to_f32(bytes([0, 255]), out, 1)
        self.assertEqual(list(out), [-1.0, 1.0])

    def test_a_buffer_too_small_is_refused_before_c_writes_past_it(self) -> None:
        with self.assertRaisesRegex(ValueError, "the output holds 8 bytes; 16 are needed"):
            self.native.u8_iq_to_f32(bytes(4), bytearray(8), 2)
        with self.assertRaisesRegex(ValueError, "RTL-SDR samples"):
            self.native.u8_iq_to_f32(bytes(2), bytearray(16), 2)
        with self.assertRaisesRegex(ValueError, "writable"):
            self.native.s16_to_f32(bytes(4), bytes(8), 32768.0)

    def test_recordings_become_the_same_16_bit_and_8_bit_iq(self) -> None:
        # I/Q recording downloads, which clip to -1 to 1 first.
        floats = (self.rng.standard_normal(40_000) * 0.8).astype("<f4")
        floats[:4] = [1.5, -1.5, 0.5 / 32767, 0.5 / 127.5 - 1.0]
        clipped = np.clip(floats, -1.0, 1.0)
        self.assertEqual(
            self.native.float_to_s16(floats.tobytes(), 32767.0, symmetric=True),
            np.round(clipped * 32767.0).astype("<i2").tobytes(),
        )
        out = bytearray(floats.size)
        self.native.f32_to_u8(floats.tobytes(), out)
        self.assertEqual(bytes(out), np.round(clipped * 127.5 + 127.5).astype(np.uint8).tobytes())

    def test_an_announcement_becomes_the_same_mono(self) -> None:
        for channels in (1, 2, 3):
            with self.subTest(channels=channels):
                samples = (self.rng.standard_normal((5_000, channels)) * 0.6).astype(np.float32)
                samples[0, 0] = np.inf
                samples[1, 0] = np.nan
                mono = self.native.floats(5_000)
                self.native.f32_downmix(samples.tobytes(), mono, channels)
                expected = np.nan_to_num(samples.mean(axis=1), nan=0.0, posinf=0.0, neginf=0.0)
                np.testing.assert_array_equal(np.asarray(mono), expected)
                self.assertEqual(
                    self.native.float_to_s16(mono, 32767.0, symmetric=True),
                    (np.clip(expected, -1.0, 1.0) * 32767.0).round().astype("<i2").tobytes(),
                )

    def test_samples_of_another_type_are_refused_rather_than_misread(self) -> None:
        with self.assertRaises(TypeError):
            self.native.float_to_s16(np.zeros(4, dtype=np.float64), 32767.0)
        with self.assertRaises(TypeError):
            self.native.s16_to_float(array.array("i", [0, 0]))
        with self.assertRaises(TypeError):
            self.native.float_samples(np.zeros(4, dtype=np.float64))

    def test_buffers_that_overlap_are_refused(self) -> None:
        shared = bytearray(64)
        with self.assertRaisesRegex(ValueError, "refused"):
            self.native.u8_iq_to_f32(shared, shared, 4)


class NativeLibraryCTests(unittest.TestCase):
    """The C checks (tests/native/nwr_native_test.c): lengths, null pointers, overlaps, sizes
    that would overflow and exact rounding, built with every warning an error, and run under
    AddressSanitizer and UndefinedBehaviorSanitizer when they're installed."""

    STRICT = ["-Wconversion", "-Wsign-conversion", "-Wshadow", "-Wstrict-prototypes", "-Werror"]

    @classmethod
    def setUpClass(cls) -> None:
        cls.native = load_native_module()
        cls.compiler = shutil.which("cc") or shutil.which("gcc")
        if cls.compiler is None:
            raise unittest.SkipTest("no C compiler")

    def build_and_run(self, extra: list[str]) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as directory:
            program = Path(directory) / "nwr_native_test"
            built = subprocess.run(
                [self.compiler, *self.native.COMPILE_FLAGS, *self.STRICT, *extra, "-o", str(program),
                 str(self.native.SOURCE), str(REPO_ROOT / "tests" / "native" / "nwr_native_test.c"), "-lm"],
                capture_output=True, text=True,
            )
            if built.returncode != 0:
                return built
            return subprocess.run([str(program)], capture_output=True, text=True, env={"ASAN_OPTIONS": "detect_leaks=1"})

    def test_the_c_checks_pass(self) -> None:
        result = self.build_and_run([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all native checks passed", result.stdout)

    def test_the_c_checks_pass_under_the_sanitizers(self) -> None:
        sanitize = ["-g", "-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "probe.c"
            probe.write_text("int main(void) { return 0; }\n")
            available = subprocess.run([self.compiler, *sanitize, "-o", str(Path(directory) / "probe"), str(probe)], capture_output=True)
        if available.returncode != 0:
            self.skipTest("AddressSanitizer and UndefinedBehaviorSanitizer aren't installed (libasan, libubsan)")
        result = self.build_and_run(sanitize)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all native checks passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
