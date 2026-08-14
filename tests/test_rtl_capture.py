from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_rtl_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.rtl")


class RtlCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rtl = load_rtl_module()

    def test_async_buffer_size_stays_low_latency_at_low_sample_rates(self) -> None:
        config = self.rtl.RtlConfig(serial="dummy", sample_rate=240_040)
        buffer_size = self.rtl.RtlCaptureSource._rtl_async_buffer_size(config)
        self.assertEqual(buffer_size % 512, 0)
        self.assertLess(buffer_size, config.read_chunk_bytes)
        self.assertLessEqual(buffer_size, 49_152)

    def test_async_buffer_size_is_about_one_hundred_milliseconds_at_default_sample_rate(self) -> None:
        config = self.rtl.RtlConfig(serial="dummy")
        buffer_size = self.rtl.RtlCaptureSource._rtl_async_buffer_size(config)
        self.assertEqual(buffer_size % 512, 0)
        self.assertGreaterEqual(buffer_size, 306_000)
        self.assertLessEqual(buffer_size, 308_000)

    def test_async_buffer_size_scales_up_at_high_sample_rates(self) -> None:
        low_config = self.rtl.RtlConfig(serial="dummy", sample_rate=240_040)
        high_config = self.rtl.RtlConfig(serial="dummy", sample_rate=3_200_000)

        low_buffer_size = self.rtl.RtlCaptureSource._rtl_async_buffer_size(low_config)
        high_buffer_size = self.rtl.RtlCaptureSource._rtl_async_buffer_size(high_config)

        self.assertEqual(high_buffer_size % 512, 0)
        self.assertGreater(high_buffer_size, low_buffer_size)
        self.assertGreater(high_buffer_size, high_config.read_chunk_bytes)
        self.assertLessEqual(high_buffer_size, 640_000)

    def test_capture_offer_drops_oldest_batch_instead_of_blocking_callback(self) -> None:
        source = self.rtl.RtlCaptureSource(self.rtl.RtlConfig(serial="dummy"))
        for index in range(source.output_queue.maxsize):
            data = bytes([index, index])
            self.assertTrue(source._offer(self.rtl.RtlSampleBatch(data=data, sample_rate=1, center_frequency_hz=2)))

        newest = self.rtl.RtlSampleBatch(data=b"zz", sample_rate=1, center_frequency_hz=2)
        self.assertTrue(source._offer(newest))
        stats = source.stats()
        first_remaining = source.read(timeout=0)

        self.assertEqual(first_remaining.data, b"\x01\x01")
        self.assertEqual(stats["dropped_batches"], 1)
        self.assertEqual(stats["dropped_bytes"], 2)
        self.assertEqual(stats["dropped_samples"], 1)
        self.assertEqual(stats["offered_batches"], source.output_queue.maxsize + 1)

    def test_async_reader_uses_stable_config_snapshot_for_batch_metadata(self) -> None:
        initial = self.rtl.RtlConfig(serial="dummy", sample_rate=1_024_000, center_frequency_hz=162_475_000)
        updated = self.rtl.RtlConfig(serial="dummy", sample_rate=2_048_000, center_frequency_hz=162_400_000)
        source = self.rtl.RtlCaptureSource(initial)

        class Sdr:
            dev_p = object()

        class Lib:
            def rtlsdr_read_async(self, _dev_p, callback, _context, _buffer_count, _buffer_size):
                source.config = updated
                buffer = (self_rtl.ctypes.c_ubyte * 2)(1, 2)
                callback(buffer, 2, None)
                return 0

            def rtlsdr_cancel_async(self, _dev_p):
                pass

        self_rtl = self.rtl
        original_lib = self.rtl.rtlsdr_lib
        try:
            self.rtl.rtlsdr_lib = Lib()
            source._reader_loop(Sdr())
        finally:
            self.rtl.rtlsdr_lib = original_lib

        batch = source.read(timeout=0)
        self.assertEqual(batch.sample_rate, initial.sample_rate)
        self.assertEqual(batch.center_frequency_hz, initial.center_frequency_hz)

    def test_open_rtlsdr_device_uses_compat_wrapper_when_dithering_symbol_is_missing(self) -> None:
        class PyBase:
            called = False

            def __init__(self, *args, **kwargs) -> None:
                PyBase.called = True

        class Compat:
            called_with = None

            def __init__(self, *args, **kwargs) -> None:
                Compat.called_with = (args, kwargs)

        original_base = self.rtl.BaseRtlSdr
        original_compat = self.rtl.CompatBaseRtlSdr
        original_lib = self.rtl.rtlsdr_lib
        try:
            self.rtl.BaseRtlSdr = PyBase
            self.rtl.CompatBaseRtlSdr = Compat
            self.rtl.rtlsdr_lib = object()

            sdr = self.rtl._open_rtlsdr_device(2, quiet=True)
        finally:
            self.rtl.BaseRtlSdr = original_base
            self.rtl.CompatBaseRtlSdr = original_compat
            self.rtl.rtlsdr_lib = original_lib

        self.assertIsInstance(sdr, Compat)
        self.assertFalse(PyBase.called)
        self.assertEqual(Compat.called_with, ((), {"device_index": 2, "dithering_enabled": False}))

    def test_open_rtlsdr_device_retries_compat_wrapper_when_pyrtlsdr_uses_missing_dithering(self) -> None:
        class PyBase:
            def __init__(self, *args, **kwargs) -> None:
                raise AttributeError("rtlsdr_set_dithering")

        class Compat:
            called_with = None

            def __init__(self, *args, **kwargs) -> None:
                Compat.called_with = (args, kwargs)

        class Lib:
            def rtlsdr_set_dithering(self) -> None:
                pass

        original_base = self.rtl.BaseRtlSdr
        original_compat = self.rtl.CompatBaseRtlSdr
        original_lib = self.rtl.rtlsdr_lib
        try:
            self.rtl.BaseRtlSdr = PyBase
            self.rtl.CompatBaseRtlSdr = Compat
            self.rtl.rtlsdr_lib = Lib()

            sdr = self.rtl._open_rtlsdr_device(3, quiet=True)
        finally:
            self.rtl.BaseRtlSdr = original_base
            self.rtl.CompatBaseRtlSdr = original_compat
            self.rtl.rtlsdr_lib = original_lib

        self.assertIsInstance(sdr, Compat)
        self.assertEqual(Compat.called_with, ((), {"device_index": 3, "dithering_enabled": False}))


if __name__ == "__main__":
    unittest.main()
