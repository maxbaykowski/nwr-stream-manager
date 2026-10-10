from __future__ import annotations

import importlib
import importlib.util
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

    def test_each_read_waits_for_only_40_milliseconds_at_default_sample_rate(self) -> None:
        config = self.rtl.RtlConfig(serial="dummy")
        buffer_size = self.rtl.RtlCaptureSource._rtl_async_buffer_size(config)
        self.assertEqual(buffer_size % 512, 0)
        self.assertAlmostEqual(buffer_size / (2 * config.sample_rate), 0.04, delta=0.001)

    def test_smaller_reads_do_not_shorten_how_long_a_stall_is_ridden_out(self) -> None:
        config = self.rtl.RtlConfig(serial="dummy")
        buffer_seconds = self.rtl.RtlCaptureSource._rtl_async_buffer_size(config) / (2 * config.sample_rate)
        # Held by the SDR's USB buffers, then waiting to be processed, as before.
        self.assertGreaterEqual(self.rtl.RTL_ASYNC_BUFFER_COUNT * buffer_seconds, 1.5 - 0.01)
        source = self.rtl.RtlCaptureSource(config)
        self.assertGreaterEqual(source.output_queue.maxsize * buffer_seconds, 3.2 - 0.01)

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

    def test_serial_resolution_uses_injected_device_providers(self) -> None:
        source = self.rtl.RtlCaptureSource(self.rtl.RtlConfig(serial="abc"))
        direct_calls = {"rtl": 0, "usb": 0}

        def direct_rtl():
            direct_calls["rtl"] += 1
            return []

        def direct_usb():
            direct_calls["usb"] += 1
            return []

        source.set_device_providers(
            rtl_devices_provider=lambda: [self.rtl.RtlDeviceInfo(index=7, description="RTL", serial="abc")],
            usb_rtl_devices_provider=lambda: [
                self.rtl.UsbDeviceInfo(
                    path=Path("/sys/bus/usb/devices/1-1"),
                    vendor_id="0bda",
                    product_id="2838",
                    serial="abc",
                    description="RTL",
                )
            ],
        )
        original_rtl = self.rtl.list_rtl_devices
        original_usb = self.rtl.list_usb_rtl_devices
        try:
            self.rtl.list_rtl_devices = direct_rtl
            self.rtl.list_usb_rtl_devices = direct_usb
            index = source._resolve_serial_to_device_index("abc")
        finally:
            self.rtl.list_rtl_devices = original_rtl
            self.rtl.list_usb_rtl_devices = original_usb

        self.assertEqual(index, 7)
        self.assertEqual(direct_calls, {"rtl": 0, "usb": 0})

    def test_open_rtlsdr_device_opens_librtlsdr_directly_with_dithering_off(self) -> None:
        class Device:
            called_with = None

            def __init__(self, *args, **kwargs) -> None:
                Device.called_with = (args, kwargs)

        original_device = self.rtl.RtlSdrDevice
        original_lib = self.rtl.rtlsdr_lib
        try:
            self.rtl.RtlSdrDevice = Device
            self.rtl.rtlsdr_lib = object()
            sdr = self.rtl._open_rtlsdr_device(2, quiet=True)

            self.rtl.rtlsdr_lib = None
            with self.assertRaises(self.rtl.RtlDependencyError):
                self.rtl._open_rtlsdr_device(2, quiet=True)
        finally:
            self.rtl.RtlSdrDevice = original_device
            self.rtl.rtlsdr_lib = original_lib

        self.assertIsInstance(sdr, Device)
        self.assertEqual(Device.called_with, ((), {"device_index": 2, "dithering_enabled": False}))


if __name__ == "__main__":
    unittest.main()


class SdrQueuesHoldTheSameTimeTests(unittest.TestCase):
    """Smaller reads mean more, shorter pieces; each queue must still hold as much audio as before."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.rtl = load_rtl_module()
        spec = importlib.util.spec_from_file_location("nwr_stream_manager.web_control", PACKAGE_PATH / "web_control.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["nwr_stream_manager.web_control"] = module
        spec.loader.exec_module(module)
        cls.web = module

    def queue_seconds(self, seconds: float) -> float:
        source = self.rtl.RtlCaptureSource(self.rtl.RtlConfig(serial="dummy"))
        fanout = self.web.RawRtlFanout(source)
        chunk_seconds = source._rtl_async_buffer_size(source.config) / (2 * source.config.sample_rate)
        return fanout.subscribe(max_seconds=seconds).maxsize * chunk_seconds

    def test_queues_asked_for_a_length_of_time_get_it(self) -> None:
        for seconds in (
            self.web.LIVE_IQ_QUEUE_SECONDS,
            self.web.STREAM_WORKER_RAW_QUEUE_SECONDS,
            self.web.REMOTE_HOST_CHANNEL_QUEUE_SECONDS,
            self.web.REMOTE_HOST_WIDEBAND_QUEUE_SECONDS,
        ):
            with self.subTest(seconds=seconds):
                self.assertGreaterEqual(self.queue_seconds(seconds), seconds - 0.05)

    def test_short_queues_still_hold_a_fifth_of_a_second(self) -> None:
        chunk_seconds = self.rtl.RTL_ASYNC_BUFFER_SECONDS
        self.assertAlmostEqual(self.web.INTERMEDIATE_SOURCE_QUEUE_CHUNKS * chunk_seconds, 0.2, delta=0.01)
        self.assertAlmostEqual(self.web.IQ_RECORDER_QUEUE_CHUNKS * chunk_seconds, 0.2, delta=0.01)
