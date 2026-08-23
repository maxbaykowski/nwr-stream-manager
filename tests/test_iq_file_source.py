from __future__ import annotations

import importlib.util
import queue
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_web_control_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    spec = importlib.util.spec_from_file_location(
        "nwr_stream_manager.web_control",
        PACKAGE_PATH / "web_control.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.web_control"] = module
    spec.loader.exec_module(module)
    return module


class IqFileSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_iq_file_source_reads_interleaved_cf32_and_loops(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "test.cf32"
            values = np.array([1.0, 0.5, -0.25, 0.75], dtype="<f4")
            path.write_bytes(values.tobytes())
            source = self.web_control.IqFileCaptureSource(
                self.web_control.IqFileSourceConfig(path=path, sample_rate=192_000),
                chunk_seconds=0.001,
            )
            try:
                source.start()
                batch = source.read(timeout=1.0)
            finally:
                source.stop()

        self.assertEqual(batch.sample_rate, 192_000)
        self.assertEqual(batch.center_frequency_hz, self.web_control.NWR_CENTER_FREQUENCY_HZ)
        self.assertEqual(batch.data.dtype, np.complex64)
        self.assertGreaterEqual(batch.data.size, 2)
        np.testing.assert_allclose(batch.data[:2], np.array([1.0 + 0.5j, -0.25 + 0.75j], dtype=np.complex64))

    def test_iq_file_source_seek_uses_sample_rate_and_aligns_to_cf32_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "test.cf32"
            sample_rate = 192_000
            total_samples = sample_rate * 2
            path.write_bytes(np.zeros(total_samples * 2, dtype="<f4").tobytes())
            source = self.web_control.IqFileCaptureSource(
                self.web_control.IqFileSourceConfig(path=path, sample_rate=sample_rate),
                chunk_seconds=0.001,
            )
            source.file_size_bytes = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(10 * 8)
                source.seek_relative(1.0)
                source._apply_pending_seek(handle)
                self.assertEqual(handle.tell(), (sample_rate + 10) * 8)

                source.seek_relative(10.0)
                source._apply_pending_seek(handle)
                self.assertEqual(handle.tell(), 0)

                handle.seek(50 * 8)
                source.seek_relative(-10.0)
                source._apply_pending_seek(handle)
                self.assertEqual(handle.tell(), 0)

    def test_iq_batch_complex_preserves_complex_batches(self) -> None:
        samples = np.array([1.0 + 2.0j, 3.0 - 4.0j], dtype=np.complex64)
        batch = self.web_control.IqSampleBatch(
            data=samples,
            sample_rate=192_000,
            center_frequency_hz=self.web_control.NWR_CENTER_FREQUENCY_HZ,
        )

        output = self.web_control.iq_batch_complex(batch)

        self.assertIs(output, samples)

    def test_intermediate_fanout_accepts_complex_batches(self) -> None:
        class DummyFanout:
            def __init__(self) -> None:
                self.queue: queue.Queue = queue.Queue()

            def subscribe(self, **_kwargs):
                return self.queue

            def unsubscribe(self, _subscriber) -> None:
                return None

        fanout = DummyFanout()
        intermediate = self.web_control.IntermediateIqFanout(
            fanout,  # type: ignore[arg-type]
            output_rate=192_000,
        )
        subscriber = intermediate.subscribe(max_chunks=2, name="test")
        try:
            intermediate.start()
            fanout.queue.put(
                self.web_control.IqSampleBatch(
                    data=np.ones(512, dtype=np.complex64),
                    sample_rate=192_000,
                    center_frequency_hz=self.web_control.NWR_CENTER_FREQUENCY_HZ,
                    captured_at=time.monotonic(),
                )
            )
            output = subscriber.get(timeout=1.0)
        finally:
            intermediate.stop()

        self.assertEqual(output.sample_rate, 192_000)
        self.assertEqual(output.data.dtype, np.complex64)
        self.assertGreater(output.data.size, 0)

    def test_raw_fanout_source_swap_preserves_existing_subscribers(self) -> None:
        web_control = self.web_control

        class Source:
            def __init__(self, sample_rate: int) -> None:
                self.config = web_control.IqFileSourceConfig(
                    path=Path("dummy.cf32"),
                    sample_rate=sample_rate,
                )
                self.queue: queue.Queue = queue.Queue()
                self.stopped = False

            @staticmethod
            def _rtl_async_buffer_size(config) -> int:
                return max(512, int(config.sample_rate * 0.02) * 8)

            def read(self, timeout=None):
                item = self.queue.get(timeout=timeout)
                if item is None:
                    raise EOFError("stopped")
                return item

            def stop(self) -> None:
                self.stopped = True
                self.queue.put(None)

        first_source = Source(192_000)
        second_source = Source(256_000)
        fanout = web_control.RawRtlFanout(first_source)
        subscriber = fanout.subscribe(max_chunks=4, name="channel")
        try:
            fanout.start()
            first_source.queue.put(
                web_control.IqSampleBatch(
                    data=np.array([1 + 0j], dtype=np.complex64),
                    sample_rate=192_000,
                    center_frequency_hz=web_control.NWR_CENTER_FREQUENCY_HZ,
                )
            )
            first_batch = subscriber.get(timeout=1.0)

            fanout.set_source(second_source)
            first_source.stop()
            second_source.queue.put(
                web_control.IqSampleBatch(
                    data=np.array([2 + 0j], dtype=np.complex64),
                    sample_rate=256_000,
                    center_frequency_hz=web_control.NWR_CENTER_FREQUENCY_HZ,
                )
            )
            second_batch = subscriber.get(timeout=1.0)
        finally:
            fanout.stop()

        self.assertEqual(first_batch.sample_rate, 192_000)
        self.assertEqual(second_batch.sample_rate, 256_000)
        self.assertTrue(first_source.stopped)
        self.assertEqual(fanout.stats()["subscriber_count"], 1)


if __name__ == "__main__":
    unittest.main()
