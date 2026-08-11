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

    def test_async_buffer_size_is_small_for_low_latency_callbacks(self) -> None:
        config = self.rtl.RtlConfig(serial="dummy", sample_rate=240_040)
        buffer_size = self.rtl.RtlCaptureSource._rtl_async_buffer_size(config)
        self.assertEqual(buffer_size % 512, 0)
        self.assertLess(buffer_size, config.read_chunk_bytes)
        self.assertLessEqual(buffer_size, 10_240)

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


if __name__ == "__main__":
    unittest.main()
