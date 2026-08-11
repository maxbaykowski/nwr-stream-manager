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
        source = self.webrtc.WebRtcAudioSource(max_frames=1)
        source.push_pcm(b"old")
        source.push_pcm(b"new")
        self.assertEqual(source.dropped_frames, 1)
        self.assertEqual(source.get_latest_pcm(), b"new")

    def test_server_capability_report_has_expected_shape(self) -> None:
        report = self.webrtc.server_webrtc_capabilities().to_dict()
        self.assertIn("available", report)
        self.assertIn("transport_available", report)
        self.assertIn("opus_available", report)
        self.assertEqual(report["target_bitrate_kbps"], 128)
        self.assertEqual(report["minimum_bitrate_kbps"], 32)
        self.assertEqual(report["bitrate_step_kbps"], 8)

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

    def test_pyogg_opus_encoder_encodes_20_ms_packet_when_available(self) -> None:
        try:
            encoder = self.webrtc.PyOggOpusEncoder(input_sample_rate=24_000)
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
