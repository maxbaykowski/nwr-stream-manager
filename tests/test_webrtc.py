from __future__ import annotations

import importlib
import asyncio
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

    def test_bitrate_controller_steps_down_to_floor(self) -> None:
        controller = self.webrtc.OpusBitrateController()
        values = [
            controller.update(available_bitrate_bps=40_000)
            for _ in range(20)
        ]
        self.assertEqual(values[:3], [120, 112, 104])
        self.assertEqual(values[-1], 40)
        floor_values = [
            controller.update(available_bitrate_bps=1_000)
            for _ in range(4)
        ]
        self.assertEqual(floor_values[-1], 32)
        self.assertEqual(controller.update(available_bitrate_bps=1_000), 32)

    def test_bitrate_controller_recovers_toward_target_after_stable_feedback(self) -> None:
        controller = self.webrtc.OpusBitrateController(recovery_stable_feedbacks=2)
        controller.update(available_bitrate_bps=20_000)
        self.assertEqual(controller.current_kbps, 120)
        controller.update(available_bitrate_bps=200_000)
        self.assertEqual(controller.current_kbps, 120)
        controller.update(available_bitrate_bps=200_000)
        self.assertEqual(controller.current_kbps, 128)

    def test_audio_source_keeps_latest_frames_when_slow_consumer_falls_behind(self) -> None:
        source = self.webrtc.WebRtcAudioSource(max_frames=1, prebuffer_frames=0)
        source.push_pcm(b"old")
        source.push_pcm(b"new")
        self.assertEqual(source.dropped_frames, 1)
        self.assertEqual(source.get_latest_pcm(), b"new" + b"\x00" * (source.frame_bytes - 3))

    def test_audio_source_async_callback_buffer_preserves_frame_order(self) -> None:
        async def run_test():
            source = self.webrtc.WebRtcAudioSource(max_frames=4, prebuffer_frames=2, low_water_frames=0)
            frame_a = b"a" * source.frame_bytes
            frame_b = b"b" * source.frame_bytes
            source.push_pcm(frame_a)
            source.push_pcm(frame_b)
            first = await source.read_pcm()
            second = await source.read_pcm()
            return first, second, source.underrun_frames

        first, second, underruns = asyncio.run(run_test())
        self.assertEqual(first, b"a" * len(first))
        self.assertEqual(second, b"b" * len(second))
        self.assertEqual(underruns, 0)

    def test_audio_source_reports_buffer_stats_and_underruns(self) -> None:
        async def run_test():
            source = self.webrtc.WebRtcAudioSource(max_frames=2, prebuffer_frames=0, low_water_frames=0)
            source.push_pcm(b"a")
            await source.read_pcm()
            await source.read_pcm(timeout=0)
            return source.stats()

        stats = asyncio.run(run_test())
        self.assertEqual(stats["pushed_frames"], 1)
        self.assertEqual(stats["read_frames"], 1)
        self.assertEqual(stats["underrun_frames"], 1)
        self.assertEqual(stats["dropped_frames"], 0)
        self.assertEqual(stats["buffered_frames"], 0)

    def test_audio_source_defaults_hold_half_second_pcm_buffer(self) -> None:
        source = self.webrtc.WebRtcAudioSource()
        stats = source.stats()

        self.assertEqual(stats["prebuffer_frames"], 25)
        self.assertEqual(stats["target_latency_frames"], 25)
        self.assertEqual(stats["low_water_frames"], 16)
        self.assertEqual(stats["max_frames"], 96)
        self.assertAlmostEqual(
            stats["target_latency_frames"] * self.webrtc.WEBRTC_FRAME_SECONDS,
            0.5,
        )

    def test_audio_source_frame_size_uses_configured_sample_rate(self) -> None:
        source = self.webrtc.WebRtcAudioSource(sample_rate=32_000)

        self.assertEqual(source.frame_bytes, 1280)
        self.assertEqual(source.stats()["sample_rate"], 32_000)

    def test_audio_source_startup_prebuffer_is_not_limited_by_track_frame_timeout(self) -> None:
        async def run_test():
            source = self.webrtc.WebRtcAudioSource(
                max_frames=4,
                prebuffer_frames=2,
                target_latency_frames=4,
                low_water_frames=0,
                prebuffer_timeout_seconds=0.2,
            )

            async def delayed_push():
                await asyncio.sleep(0.03)
                source.push_pcm(b"a" * source.frame_bytes)
                source.push_pcm(b"b" * source.frame_bytes)

            task = asyncio.create_task(delayed_push())
            first = await source.read_pcm(timeout=self_webrtc.WEBRTC_FRAME_SECONDS)
            await task
            return first, source.stats()

        self_webrtc = self.webrtc
        first, stats = asyncio.run(run_test())
        self.assertEqual(first, b"a" * len(first))
        self.assertEqual(stats["underrun_frames"], 0)

    def test_audio_source_rebuffers_after_underrun(self) -> None:
        async def run_test():
            source = self.webrtc.WebRtcAudioSource(
                max_frames=4,
                prebuffer_frames=2,
                target_latency_frames=4,
                low_water_frames=0,
                prebuffer_timeout_seconds=0.2,
            )
            first = await source.read_pcm(timeout=0.01)

            async def delayed_push():
                await asyncio.sleep(0.03)
                source.push_pcm(b"a" * source.frame_bytes)
                source.push_pcm(b"b" * source.frame_bytes)

            task = asyncio.create_task(delayed_push())
            second = await source.read_pcm(timeout=self_webrtc.WEBRTC_FRAME_SECONDS)
            await task
            return first, second, source.stats()

        self_webrtc = self.webrtc
        first, second, stats = asyncio.run(run_test())
        self.assertEqual(first, b"\x00" * len(first))
        self.assertEqual(second, b"a" * len(second))
        self.assertEqual(stats["underrun_frames"], 1)

    def test_audio_source_discards_stale_frames_to_hold_low_latency(self) -> None:
        async def run_test():
            source = self.webrtc.WebRtcAudioSource(
                max_frames=8,
                prebuffer_frames=0,
                target_latency_frames=2,
                low_water_frames=0,
            )
            for value in (b"a", b"b", b"c", b"d", b"e"):
                source.push_pcm(value)
            first = await source.read_pcm()
            stats = source.stats()
            return first, stats, source.frame_bytes

        first, stats, frame_bytes = asyncio.run(run_test())
        self.assertEqual(first, b"d" + b"\x00" * (frame_bytes - 1))
        self.assertEqual(stats["stale_frames"], 3)
        self.assertEqual(stats["buffered_frames"], 1)

    def test_webrtc_track_waits_for_complete_resampled_frame_before_padding(self) -> None:
        class Source:
            def __init__(self) -> None:
                self.reads = 0

            async def read_pcm(self):
                self.reads += 1
                return b"x"

        class ShortFirstResampler:
            def __init__(self, _input_rate, _output_rate) -> None:
                self.calls = 0

            def process(self, _pcm: bytes) -> bytes:
                self.calls += 1
                if self.calls == 1:
                    return b"\x01\x00" * 480
                return b"\x02\x00" * 480

        original_resampler = self.webrtc.PcmResampler
        self.webrtc.PcmResampler = ShortFirstResampler
        try:
            try:
                source = Source()
                track = self.webrtc.create_webrtc_pcm_audio_track(source)
            except self.webrtc.WebRtcError as exc:
                self.skipTest(str(exc))
            frame = asyncio.run(track.recv())
        finally:
            self.webrtc.PcmResampler = original_resampler

        self.assertEqual(source.reads, 2)
        self.assertEqual(frame.samples, self.webrtc.WEBRTC_OPUS_FRAME_SAMPLES)

    def test_webrtc_track_uses_short_pcm_read_timeout_to_keep_sender_alive(self) -> None:
        class Source:
            def __init__(self) -> None:
                self.timeouts = []

            async def read_pcm(self, timeout=0.25):
                self.timeouts.append(timeout)
                return b"\x00" * 960

        class FullFrameResampler:
            def __init__(self, _input_rate, _output_rate) -> None:
                pass

            def process(self, _pcm: bytes) -> bytes:
                return b"\x00" * (self_webrtc.WEBRTC_OPUS_FRAME_SAMPLES * 2)

        self_webrtc = self.webrtc
        original_resampler = self.webrtc.PcmResampler
        self.webrtc.PcmResampler = FullFrameResampler
        try:
            try:
                source = Source()
                track = self.webrtc.create_webrtc_pcm_audio_track(source)
            except self.webrtc.WebRtcError as exc:
                self.skipTest(str(exc))
            asyncio.run(track.recv())
        finally:
            self.webrtc.PcmResampler = original_resampler

        self.assertEqual(len(source.timeouts), 1)
        self.assertGreaterEqual(source.timeouts[0], 0)
        self.assertLessEqual(source.timeouts[0], self.webrtc.WEBRTC_FRAME_SECONDS)

    def test_server_capability_report_has_expected_shape(self) -> None:
        report = self.webrtc.server_webrtc_capabilities().to_dict()
        self.assertIn("available", report)
        self.assertIn("transport_available", report)
        self.assertIn("opus_available", report)
        self.assertEqual(report["target_bitrate_kbps"], 128)
        self.assertEqual(report["minimum_bitrate_kbps"], 32)
        self.assertEqual(report["bitrate_step_kbps"], 8)

    def test_session_manager_cleans_up_failed_peer(self) -> None:
        class Description:
            def __init__(self, sdp: str, type: str) -> None:
                self.sdp = sdp
                self.type = type

        class Peer:
            def __init__(self, _configuration=None) -> None:
                self.connectionState = "new"
                self.iceConnectionState = "new"
                self.localDescription = Description("answer", "answer")
                self.handlers = {}
                self.closed = False

            def on(self, event_name):
                def register(handler):
                    self.handlers[event_name] = handler
                    return handler

                return register

            def addTrack(self, track):
                return object()

            async def setRemoteDescription(self, description):
                self.remoteDescription = description

            async def createAnswer(self):
                return self.localDescription

            async def setLocalDescription(self, description):
                self.localDescription = description

            async def close(self):
                self.closed = True

        class FakeAiortc:
            RTCConfiguration = lambda self, iceServers=None: {"iceServers": iceServers or []}
            RTCSessionDescription = Description

            def __init__(self) -> None:
                self.peer = Peer()

            def RTCPeerConnection(self, configuration=None):
                return self.peer

        fake_aiortc = FakeAiortc()
        original_loader = self.webrtc._load_aiortc
        original_prefer_opus = self.webrtc._prefer_opus
        original_configure = self.webrtc._configure_sender_bitrate
        self.webrtc._load_aiortc = lambda: fake_aiortc
        self.webrtc._prefer_opus = lambda _peer: None
        self.webrtc._configure_sender_bitrate = lambda _sender, _bitrate: None
        cleanup_events = []
        try:
            manager = self.webrtc.AiortcSessionManager()

            async def run_test():
                await manager.accept_offer(
                    session_id="client-1",
                    sdp="offer",
                    on_peer_closed=lambda session_id, reason: cleanup_events.append((session_id, reason)),
                )
                self.assertIn("client-1", manager.sessions)
                fake_aiortc.peer.connectionState = "failed"
                await fake_aiortc.peer.handlers["connectionstatechange"]()
                await asyncio.sleep(0)

            asyncio.run(run_test())
        finally:
            self.webrtc._load_aiortc = original_loader
            self.webrtc._prefer_opus = original_prefer_opus
            self.webrtc._configure_sender_bitrate = original_configure

        self.assertEqual(cleanup_events, [("client-1", "connectionState=failed")])
        self.assertNotIn("client-1", manager.sessions)
        self.assertTrue(fake_aiortc.peer.closed)

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

    def test_bitrate_feedback_from_transport_stats(self) -> None:
        class Stats:
            availableOutgoingBitrate = 96_000
            roundTripTime = 0.25
            packetsLost = 2
            packetsSent = 100

        feedback = self.webrtc.bitrate_feedback_from_stats(Stats())
        self.assertEqual(feedback["available_bitrate_bps"], 96_000)
        self.assertEqual(feedback["packet_loss_fraction"], 0.02)
        self.assertEqual(feedback["rtt_ms"], 250.0)

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
