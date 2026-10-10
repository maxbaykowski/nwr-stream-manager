from __future__ import annotations

import ctypes
import ctypes.util
import importlib
import sys
import threading
import time
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

        decision = controller.update(send_seconds=0.001, media_delivery_ratio=0.9)
        self.assertEqual(decision.bitrate_kbps, 128)
        self.assertEqual(decision.reason, "stable")
        decision = controller.update(send_seconds=0.001, media_delivery_ratio=0.9)
        self.assertEqual(decision.bitrate_kbps, 96)
        self.assertEqual(decision.reason, "client throughput feedback")
        self.assertEqual(controller.update(send_seconds=0.001, media_delivery_ratio=0.80).bitrate_kbps, 64)

        self.assertEqual(controller.update(send_seconds=0.001, media_delivery_ratio=1.0).bitrate_kbps, 64)
        self.assertEqual(controller.update(send_seconds=0.001, media_delivery_ratio=1.0).bitrate_kbps, 96)

    def test_tcp_opus_bitrate_controller_keeps_bitrate_when_audio_arrives_in_real_time(self) -> None:
        # Opus spends far fewer bits than its setting on easy audio and almost none
        # on silence, as when a stream fills an SDR outage; arriving on time is what counts.
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=2, recovery_hold_feedbacks=0)

        for _ in range(10):
            decision = controller.update(send_seconds=0.0, media_delivery_ratio=1.0)
            self.assertEqual(decision.bitrate_kbps, 128)

    def test_tcp_opus_bitrate_controller_ignores_single_mixed_throughput_window_after_probe(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=1, recovery_hold_feedbacks=0)
        controller.current_kbps = 32

        decision = controller.update(send_seconds=0.001)
        self.assertEqual(decision.bitrate_kbps, 64)
        self.assertEqual(decision.reason, "recovery probe")

        decision = controller.update(send_seconds=0.001, media_delivery_ratio=0.9)
        self.assertEqual(decision.bitrate_kbps, 64)
        self.assertEqual(decision.reason, "stable")

        decision = controller.update(send_seconds=0.001, media_delivery_ratio=0.9)
        self.assertEqual(decision.bitrate_kbps, 32)
        self.assertEqual(decision.reason, "client throughput feedback")

    def test_tcp_opus_bitrate_controller_reacts_immediately_to_severe_throughput_feedback(self) -> None:
        controller = self.webrtc.TcpOpusBitrateController(recovery_stable_feedbacks=2, recovery_hold_feedbacks=0)

        decision = controller.update(send_seconds=0.001, media_delivery_ratio=0.8)
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
        source = self.webrtc.WebRtcAudioSource(max_frames=1)
        source.push_pcm(b"old")
        source.push_pcm(b"new")
        self.assertEqual(source.dropped_frames, 1)
        self.assertEqual(source.get_latest_pcm(), b"new" + b"\x00" * (source.frame_bytes - 3))

    def test_audio_source_buffer_preserves_frame_order(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=4)
        frame_a = b"a" * source.frame_bytes
        frame_b = b"b" * source.frame_bytes
        source.push_pcm(frame_a)
        source.push_pcm(frame_b)

        self.assertEqual(source.read_frame(), frame_a)
        self.assertEqual(source.read_frame(), frame_b)
        self.assertEqual(source.underrun_frames, 0)

    def test_audio_source_reports_buffer_stats_and_underruns(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=2)
        source.push_pcm(b"a")
        source.read_frame()
        source.read_frame(timeout=0)
        stats = source.stats()

        self.assertEqual(stats["pushed_frames"], 1)
        self.assertEqual(stats["read_frames"], 1)
        self.assertEqual(stats["underrun_frames"], 1)
        self.assertEqual(stats["dropped_frames"], 0)
        self.assertEqual(stats["buffered_frames"], 0)

    def test_audio_source_defaults_hold_low_latency_pcm_buffer(self) -> None:
        source = self.webrtc.WebRtcAudioSource()
        stats = source.stats()

        self.assertEqual(stats["target_latency_frames"], 6)
        self.assertEqual(stats["latency_high_water_frames"], 14)
        self.assertEqual(stats["latency_trim_to_frames"], 8)
        self.assertEqual(stats["max_frames"], 32)

    def test_audio_source_frame_size_uses_configured_sample_rate(self) -> None:
        source = self.webrtc.WebRtcAudioSource(sample_rate=32_000)

        self.assertEqual(source.frame_bytes, 1280)
        self.assertEqual(source.stats()["sample_rate"], 32_000)

    def test_audio_source_hands_over_each_frame_as_soon_as_it_arrives(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=4)

        def delayed_push():
            time.sleep(0.03)
            source.push_pcm(b"a" * source.frame_bytes)

        thread = threading.Thread(target=delayed_push)
        thread.start()
        started = time.monotonic()
        frame = source.read_frame(timeout=1.0)
        waited = time.monotonic() - started
        thread.join(timeout=1.0)

        self.assertEqual(frame, b"a" * source.frame_bytes)
        self.assertLess(waited, 0.5)  # no prebuffer to fill first

    def test_audio_source_never_makes_up_silence_for_late_audio(self) -> None:
        # A late frame is waited for, not replaced: silence sent in its place
        # would be a gap the listener hears, with the real audio piling up behind.
        source = self.webrtc.WebRtcAudioSource(max_frames=4)

        self.assertIsNone(source.read_frame(timeout=0.01))
        source.push_pcm(b"a" * source.frame_bytes)
        self.assertEqual(source.read_frame(timeout=0.01), b"a" * source.frame_bytes)
        self.assertEqual(source.stats()["read_frames"], 1)

    def test_audio_source_preserves_bursty_pcm_above_target_latency(self) -> None:
        def run_test():
            source = self.webrtc.WebRtcAudioSource(
                max_frames=8,
                target_latency_frames=2,
                latency_high_water_frames=6,
                latency_trim_to_frames=4,
            )
            for value in (b"a", b"b", b"c", b"d", b"e"):
                source.push_pcm(value)
            first = source.read_frame()
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
                target_latency_frames=2,
                latency_high_water_frames=5,
                latency_trim_to_frames=4,
            )
            for value in (b"a", b"b", b"c", b"d", b"e"):
                source.push_pcm(value)
            before = source.stats()
            source.push_pcm(b"f")
            after = source.stats()
            first = source.read_frame()
            return before, after, first, source.frame_bytes

        before, after, first, frame_bytes = run_test()
        self.assertEqual(before["stale_frames"], 0)
        self.assertEqual(before["buffered_frames"], 5)
        self.assertEqual(after["stale_frames"], 2)
        self.assertEqual(after["buffered_frames"], 4)
        self.assertEqual(first, b"c" + b"\x00" * (frame_bytes - 1))

    def test_audio_source_clear_buffer_drops_queued_audio(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=4)
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

    def test_opus_encoder_sends_each_20_ms_at_once_and_browsers_decode_it(self) -> None:
        try:
            encoder = self.webrtc.OpusEncoder(input_sample_rate=24_000)
        except self.webrtc.OpusSupportError as exc:
            self.skipTest(str(exc))
        try:
            # A 1 kHz tone, 20 ms at a time, as the receiver and streams send it.
            tone = (8000 * np.sin(2 * np.pi * 1000 * np.arange(24_000) / 24_000)).astype("<i2")
            packets = []
            for offset in range(0, tone.size, 480):
                frame_packets = encoder.encode(tone[offset : offset + 480].tobytes())
                # Nothing is held back for the next frame.
                self.assertEqual(len(frame_packets), 1)
                packets.extend(frame_packets)
            encoder.set_bitrate(96)
            self.assertEqual(encoder.bitrate_kbps, 96)
        finally:
            encoder.close()

        # Browsers decode at 48 kHz; the tone must come out intact and on pitch.
        library = ctypes.CDLL(ctypes.util.find_library("opus") or "libopus.so.0")
        library.opus_decoder_create.restype = ctypes.c_void_p
        library.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        library.opus_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int
        ]
        library.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        error = ctypes.c_int()
        decoder = library.opus_decoder_create(48_000, 1, ctypes.byref(error))
        self.assertEqual(error.value, 0)
        decoded = []
        try:
            for packet in packets:
                output = (ctypes.c_int16 * 960)()
                self.assertEqual(library.opus_decode(decoder, packet, len(packet), output, 960, 0), 960)
                decoded.append(np.frombuffer(output, dtype=np.int16).copy())
        finally:
            library.opus_decoder_destroy(decoder)
        audio = np.concatenate(decoded)[4800:].astype(np.float64)
        spectrum = np.abs(np.fft.rfft(audio * np.hanning(audio.size)))
        self.assertAlmostEqual(float(np.fft.rfftfreq(audio.size, 1 / 48_000)[np.argmax(spectrum)]), 1000.0, delta=5.0)
        self.assertGreater(np.sqrt(np.mean(audio**2)), 0.5 * 8000 / np.sqrt(2))


if __name__ == "__main__":
    unittest.main()
