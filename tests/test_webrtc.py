from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_webrtc_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.webrtc")


class WebRtcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.webrtc = load_webrtc_module()

    def test_tcp_opus_bitrate_controller_uses_coarse_ladder_without_jumping(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=2, recovery_hold_feedbacks=0)

        self.assertEqual(controller.current_kbps, 128)
        decision = controller.update(send_seconds=0.08)
        self.assertEqual(decision.bitrate_kbps, 96)
        self.assertEqual(decision.reason, "send backpressure")
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 64)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 32)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 16)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 12)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 8)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 6)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 6)

        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 6)
        decision = controller.update(send_seconds=0.001)
        self.assertEqual(decision.bitrate_kbps, 8)
        self.assertEqual(decision.reason, "recovery probe")
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 8)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 12)

    def test_tcp_opus_bitrate_controller_responds_to_client_latency_drops(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=3, recovery_hold_feedbacks=0)

        decision = controller.update(send_seconds=0.001, latency_drop_count=1)
        self.assertEqual(decision.bitrate_kbps, 96)
        self.assertEqual(decision.reason, "client latency feedback")
        self.assertEqual(controller.update(send_seconds=0.001, above_max_drop_count=1).bitrate_kbps, 64)

        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 64)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 64)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)

    def test_tcp_opus_bitrate_controller_responds_to_throughput_feedback(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=2, recovery_hold_feedbacks=0)

        decision = controller.update(send_seconds=0.001, network_receive_kbps=90.0)
        self.assertEqual(decision.bitrate_kbps, 128)
        self.assertEqual(decision.reason, "stable")
        decision = controller.update(send_seconds=0.001, network_receive_kbps=90.0)
        self.assertEqual(decision.bitrate_kbps, 96)
        self.assertEqual(decision.reason, "client throughput feedback")
        self.assertEqual(controller.update(send_seconds=0.001, media_delivery_ratio=0.80).bitrate_kbps, 64)

        self.assertEqual(controller.update(send_seconds=0.001, network_receive_kbps=120.0, media_delivery_ratio=1.0).bitrate_kbps, 64)
        self.assertEqual(controller.update(send_seconds=0.001, network_receive_kbps=120.0, media_delivery_ratio=1.0).bitrate_kbps, 96)

    def test_tcp_opus_bitrate_controller_ignores_single_mixed_throughput_window_after_probe(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=1, recovery_hold_feedbacks=0)
        controller.current_kbps = 32

        decision = controller.update(send_seconds=0.001)
        self.assertEqual(decision.bitrate_kbps, 64)
        self.assertEqual(decision.reason, "recovery probe")

        decision = controller.update(
            send_seconds=0.001,
            network_receive_kbps=44.0,
            media_delivery_ratio=1.0,
        )
        self.assertEqual(decision.bitrate_kbps, 64)
        self.assertEqual(decision.reason, "stable")

        decision = controller.update(
            send_seconds=0.001,
            network_receive_kbps=44.0,
            media_delivery_ratio=1.0,
        )
        self.assertEqual(decision.bitrate_kbps, 32)
        self.assertEqual(decision.reason, "client throughput feedback")

    def test_tcp_opus_bitrate_controller_reacts_immediately_to_severe_throughput_feedback(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=2, recovery_hold_feedbacks=0)

        decision = controller.update(send_seconds=0.001, network_receive_kbps=70.0)
        self.assertEqual(decision.bitrate_kbps, 96)
        self.assertEqual(decision.reason, "client throughput feedback")

    def test_tcp_opus_bitrate_controller_holds_before_recovery_probe(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(
            recovery_stable_feedbacks=2,
            recovery_hold_feedbacks=3,
        )

        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 96)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)
        decision = controller.update(send_seconds=0.001)
        self.assertEqual(decision.bitrate_kbps, 128)
        self.assertEqual(decision.reason, "recovery probe")

    def test_tcp_opus_bitrate_controller_ignores_browser_feedback_during_upstream_stalls(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=2, recovery_hold_feedbacks=0)

        # The SDR or a remote SDR's link stalled: the browser sees slow
        # delivery and drops, but its own connection is fine.
        decision = controller.update(
            send_seconds=0.001,
            latency_drop_count=3,
            above_max_drop_count=3,
            network_receive_kbps=20.0,
            media_delivery_ratio=0.3,
            upstream_limited=True,
        )
        self.assertEqual(decision.bitrate_kbps, 128)
        self.assertEqual(decision.reason, "upstream hold")

        # Send backpressure is the browser's path alone, so it still counts.
        decision = controller.update(send_seconds=0.08, media_delivery_ratio=0.3, upstream_limited=True)
        self.assertEqual(decision.bitrate_kbps, 96)
        self.assertEqual(decision.reason, "send backpressure")

    def test_tcp_opus_bitrate_controller_recovery_pauses_during_upstream_stalls(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=3, recovery_hold_feedbacks=0)
        self.assertEqual(controller.update(send_seconds=0.08).bitrate_kbps, 96)

        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)
        self.assertEqual(controller.update(send_seconds=0.001).bitrate_kbps, 96)
        for _ in range(10):  # an upstream stall neither advances nor resets recovery
            self.assertEqual(controller.update(send_seconds=0.001, upstream_limited=True).bitrate_kbps, 96)
        decision = controller.update(send_seconds=0.001)
        self.assertEqual(decision.bitrate_kbps, 128)
        self.assertEqual(decision.reason, "recovery probe")

    def test_audio_source_keeps_latest_frames_when_slow_consumer_falls_behind(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=1, prebuffer_frames=0)
        source.push_pcm(b"old")
        source.push_pcm(b"new")
        self.assertEqual(source.dropped_frames, 1)
        self.assertEqual(source.get_latest_pcm(), b"new" + b"\x00" * (source.frame_bytes - 3))

    def test_audio_source_buffer_preserves_frame_order(self) -> None:
        def run_test():
            source = self.webrtc.WebRtcAudioSource(max_frames=4, prebuffer_frames=2, low_water_frames=0)
            frame_a = b"a" * source.frame_bytes
            frame_b = b"b" * source.frame_bytes
            source.push_pcm(frame_a)
            source.push_pcm(frame_b)
            first = source.read_pcm_blocking()
            second = source.read_pcm_blocking()
            return first, second, source.underrun_frames

        first, second, underruns = run_test()
        self.assertEqual(first, b"a" * len(first))
        self.assertEqual(second, b"b" * len(second))
        self.assertEqual(underruns, 0)

    def test_audio_source_reports_buffer_stats_and_underruns(self) -> None:
        def run_test():
            source = self.webrtc.WebRtcAudioSource(max_frames=2, prebuffer_frames=0, low_water_frames=0)
            source.push_pcm(b"a")
            source.read_pcm_blocking()
            source.read_pcm_blocking(timeout=0)
            return source.stats()

        stats = run_test()
        self.assertEqual(stats["pushed_frames"], 1)
        self.assertEqual(stats["read_frames"], 1)
        self.assertEqual(stats["underrun_frames"], 1)
        self.assertEqual(stats["dropped_frames"], 0)
        self.assertEqual(stats["buffered_frames"], 0)

    def test_audio_source_defaults_hold_low_latency_pcm_buffer(self) -> None:
        source = self.webrtc.WebRtcAudioSource()
        stats = source.stats()

        self.assertEqual(stats["prebuffer_frames"], 6)
        self.assertEqual(stats["target_latency_frames"], 6)
        self.assertEqual(stats["low_water_frames"], 3)
        self.assertEqual(stats["latency_high_water_frames"], 14)
        self.assertEqual(stats["latency_trim_to_frames"], 8)
        self.assertEqual(stats["max_frames"], 32)
        self.assertAlmostEqual(
            stats["target_latency_frames"] * self.webrtc.WEBRTC_FRAME_SECONDS,
            0.12,
        )

    def test_audio_source_frame_size_uses_configured_sample_rate(self) -> None:
        source = self.webrtc.WebRtcAudioSource(sample_rate=32_000)

        self.assertEqual(source.frame_bytes, 1280)
        self.assertEqual(source.stats()["sample_rate"], 32_000)

    def test_audio_source_blocking_reader_uses_startup_prebuffer(self) -> None:
        source = self.webrtc.WebRtcAudioSource(
            max_frames=4,
            prebuffer_frames=2,
            target_latency_frames=4,
            low_water_frames=0,
            prebuffer_timeout_seconds=0.2,
        )

        def delayed_push():
            import time

            time.sleep(0.03)
            source.push_pcm(b"a" * source.frame_bytes)
            source.push_pcm(b"b" * source.frame_bytes)

        thread = __import__("threading").Thread(target=delayed_push)
        thread.start()
        first = source.read_pcm_blocking(timeout=self.webrtc.WEBRTC_FRAME_SECONDS)
        thread.join(timeout=1.0)

        self.assertEqual(first, b"a" * len(first))
        self.assertEqual(source.stats()["underrun_frames"], 0)

    def test_audio_source_blocking_reader_rebuffers_after_underrun(self) -> None:
        source = self.webrtc.WebRtcAudioSource(
            max_frames=4,
            prebuffer_frames=2,
            target_latency_frames=4,
            low_water_frames=0,
            prebuffer_timeout_seconds=0.2,
        )
        first = source.read_pcm_blocking(timeout=0.01)

        def delayed_push():
            import time

            time.sleep(0.03)
            source.push_pcm(b"a" * source.frame_bytes)
            source.push_pcm(b"b" * source.frame_bytes)

        thread = __import__("threading").Thread(target=delayed_push)
        thread.start()
        second = source.read_pcm_blocking(timeout=self.webrtc.WEBRTC_FRAME_SECONDS)
        thread.join(timeout=1.0)

        self.assertEqual(first, b"\x00" * len(first))
        self.assertEqual(second, b"a" * len(second))
        self.assertEqual(source.stats()["underrun_frames"], 1)

    def test_audio_source_preserves_bursty_pcm_above_target_latency(self) -> None:
        def run_test():
            source = self.webrtc.WebRtcAudioSource(
                max_frames=8,
                prebuffer_frames=0,
                target_latency_frames=2,
                low_water_frames=0,
                latency_high_water_frames=6,
                latency_trim_to_frames=4,
            )
            for value in (b"a", b"b", b"c", b"d", b"e"):
                source.push_pcm(value)
            first = source.read_pcm_blocking()
            stats = source.stats()
            return first, stats, source.frame_bytes

        first, stats, frame_bytes = run_test()
        self.assertEqual(first, b"a" + b"\x00" * (frame_bytes - 1))
        self.assertEqual(stats["stale_frames"], 0)
        self.assertEqual(stats["buffered_frames"], 4)

    def test_audio_source_trims_only_after_latency_high_watermark(self) -> None:
        def run_test():
            source = self.webrtc.WebRtcAudioSource(
                max_frames=8,
                prebuffer_frames=0,
                target_latency_frames=2,
                low_water_frames=0,
                latency_high_water_frames=5,
                latency_trim_to_frames=4,
            )
            for value in (b"a", b"b", b"c", b"d", b"e"):
                source.push_pcm(value)
            before = source.stats()
            source.push_pcm(b"f")
            after = source.stats()
            first = source.read_pcm_blocking()
            return before, after, first, source.frame_bytes

        before, after, first, frame_bytes = run_test()
        self.assertEqual(before["stale_frames"], 0)
        self.assertEqual(before["buffered_frames"], 5)
        self.assertEqual(after["stale_frames"], 2)
        self.assertEqual(after["buffered_frames"], 4)
        self.assertEqual(first, b"c" + b"\x00" * (frame_bytes - 1))

    def test_audio_source_clear_buffer_drops_queued_audio(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=4, prebuffer_frames=0, low_water_frames=0)
        source.push_pcm(b"a" * source.frame_bytes)
        source.push_pcm(b"b" * source.frame_bytes)

        source.clear_buffer()

        stats = source.stats()
        self.assertEqual(stats["buffered_frames"], 0)
        self.assertEqual(stats["stale_frames"], 2)
        self.assertEqual(source.get_latest_pcm(timeout=0), b"\x00" * source.frame_bytes)

    def test_opus_loader_uses_system_libopus(self) -> None:
        original_module = self.webrtc._OPUS_MODULE
        self.webrtc._OPUS_MODULE = None
        try:
            try:
                opus = self.webrtc._load_system_opus_module()
            except self.webrtc.OpusSupportError as exc:
                self.skipTest(str(exc))
            self.assertTrue(hasattr(opus, "opus_encoder_create"))
            self.assertEqual(opus.OPUS_APPLICATION_AUDIO, 2049)
        finally:
            self.webrtc._OPUS_MODULE = original_module

    def test_opus_encoder_encodes_20_ms_packet_when_available(self) -> None:
        try:
            encoder = self.webrtc.OpusEncoder(input_sample_rate=24_000)
        except self.webrtc.OpusSupportError as exc:
            self.skipTest(str(exc))
        try:
            pcm = np.zeros(480, dtype="<i2").tobytes()
            packets = []
            for _ in range(4):
                packets.extend(encoder.encode(pcm))
                if packets:
                    break
            self.assertEqual(len(packets), 1)
            self.assertGreater(len(packets[0]), 0)
            encoder.set_bitrate(96)
            self.assertEqual(encoder.bitrate_kbps, 96)
        finally:
            encoder.close()


if __name__ == "__main__":
    unittest.main()
