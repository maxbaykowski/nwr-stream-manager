from __future__ import annotations

import importlib
import shutil
import struct
import sys
import time
import types
import unittest
from collections import deque
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_same_live_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.same_live")


class SameLiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.same_live = load_same_live_module()

    def test_parse_zczc_header(self) -> None:
        parsed = self.same_live.parse_same_header("ZCZC-WXR-TOR-026139+0030-2211907-KDTX/NWS-")

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.event_type, "TOR")
        self.assertEqual(parsed.originator, "WXR")
        self.assertEqual(parsed.fips_codes, ("026139",))
        self.assertEqual(parsed.duration_code, "0030")
        self.assertEqual(parsed.sender_id, "KDTX/NWS")

    def test_malformed_header_is_rejected(self) -> None:
        self.assertIsNone(self.same_live.parse_same_header("ZCZC-WXR-BAD-NOT-A-HEADER"))

    def test_normalizes_multimon_partial_header(self) -> None:
        payload = self.same_live.normalize_multimon_eas_payload(
            "EAS (part): ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"
        )

        self.assertEqual(payload, "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-")

    def test_normalizes_multimon_json_header(self) -> None:
        payload = self.same_live.normalize_multimon_eas_payload(
            '{"demod_name":"EAS","header_begin":"ZCZC",'
            '"last_message":"-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"}'
        )

        self.assertEqual(payload, "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-")

    def test_normalizes_multimon_eom(self) -> None:
        self.assertEqual(
            self.same_live.normalize_multimon_eas_payload('{"demod_name":"EAS","end_of_message":"NNNN"}'),
            "NNNN",
        )
        self.assertEqual(self.same_live.normalize_multimon_eas_payload("EAS: NNNN"), "NNNN")

    def test_multimon_decoder_restarts_after_three_same_payloads(self) -> None:
        decoder = self.same_live.SameMultimonLiveDecoder.__new__(self.same_live.SameMultimonLiveDecoder)
        decoder.lines = deque(
            [
                "EAS (part): ZCZC-WXR-RWT-026139+0030-2211907-KGRR/NWS-",
                "EAS (part): ZCZC-WXR-RWT-026139+0030-2211907-KGRR/NWS-",
                "EAS (part): ZCZC-WXR-RWT-026139+0030-2211907-KGRR/NWS-",
            ]
        )
        decoder._decoded_payload_count = 0
        decoder._reset_after_payloads = 3
        restarts = []

        def restart(_self):
            restarts.append(True)

        decoder._restart = types.MethodType(restart, decoder)

        payloads = decoder.poll()

        self.assertEqual(len(payloads), 3)
        self.assertEqual(len(restarts), 1)

    def test_multimon_decoder_does_not_restart_before_complete_burst_set(self) -> None:
        decoder = self.same_live.SameMultimonLiveDecoder.__new__(self.same_live.SameMultimonLiveDecoder)
        decoder.lines = deque(
            [
                "EAS (part): ZCZC-WXR-RWT-026139+0030-2211907-KGRR/NWS-",
                "EAS (part): ZCZC-WXR-RWT-026139+0030-2211907-KGRR/NWS-",
            ]
        )
        decoder._decoded_payload_count = 0
        decoder._reset_after_payloads = 3
        restarts = []

        def restart(_self):
            restarts.append(True)

        decoder._restart = types.MethodType(restart, decoder)

        payloads = decoder.poll()

        self.assertEqual(len(payloads), 2)
        self.assertEqual(restarts, [])

    def test_ignores_invalid_multimon_payloads(self) -> None:
        self.assertIsNone(self.same_live.normalize_multimon_eas_payload("EAS (part): ZCZC-WXR-BAD-NOT-A-HEADER"))
        self.assertIsNone(self.same_live.normalize_multimon_eas_payload('{"demod_name":"POCSAG","end_of_message":"NNNN"}'))

    def test_same_burst_uses_spec_symbol_timing(self) -> None:
        sample_rate = 48_000
        burst = self.same_live.generate_same_burst("NNNN", sample_rate)
        expected_bits = (self.same_live.SAME_PREAMBLE_BYTES + 4 + self.same_live.SAME_TRAILING_NUL_BYTES) * 8
        expected_samples = round(expected_bits * sample_rate / self.same_live.SAME_BAUD)

        self.assertLessEqual(abs(len(burst) - expected_samples), 1)

    def test_same_burst_ends_with_trailing_space_frequency(self) -> None:
        sample_rate = 48_000
        burst = self.same_live.generate_same_burst("NNNN", sample_rate)
        trailing_samples = round(
            self.same_live.SAME_TRAILING_NUL_BYTES * 8 * sample_rate / self.same_live.SAME_BAUD
        )
        tail = burst[-trailing_samples:]

        self.assertGreater(self._band_power(tail, sample_rate, self.same_live.SAME_SPACE_HZ), 5.0)
        self.assertGreater(
            self._band_power(tail, sample_rate, self.same_live.SAME_SPACE_HZ),
            self._band_power(tail, sample_rate, self.same_live.SAME_MARK_HZ),
        )

    def test_same_burst_contains_mark_and_space_frequencies(self) -> None:
        sample_rate = 48_000
        mark = self.same_live.generate_same_burst("\x7f", sample_rate)
        space = self.same_live.generate_same_burst("\x00", sample_rate)

        self.assertGreater(self._band_power(mark, sample_rate, self.same_live.SAME_MARK_HZ), 5.0)
        self.assertGreater(self._band_power(space, sample_rate, self.same_live.SAME_SPACE_HZ), 5.0)

    def test_same_message_repeats_three_times_with_gaps(self) -> None:
        sample_rate = 24_000
        payload = "NNNN"
        burst = self.same_live.generate_same_burst(payload, sample_rate)
        message = self.same_live.generate_same_message(payload, sample_rate)
        expected = len(burst) * 3 + sample_rate * 2

        self.assertEqual(len(message), expected)

    def test_detector_identifies_same_preamble(self) -> None:
        sample_rate = 24_000
        detector = self.same_live.SameToneDetector(sample_rate)
        burst = self.same_live.generate_same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", sample_rate)
        pcm = np.clip(burst[: round(sample_rate * 0.36)] * 32767.0, -32768, 32767).astype("<i2").tobytes()

        self.assertTrue(detector.has_same_preamble(pcm))

    def test_detector_rejects_voice_and_attention_tone_as_same_preamble(self) -> None:
        sample_rate = 24_000
        detector = self.same_live.SameToneDetector(sample_rate)
        t = np.arange(round(sample_rate * 0.4), dtype=np.float32) / sample_rate
        voice_like = (
            0.25 * np.sin(2 * np.pi * 420 * t)
            + 0.12 * np.sin(2 * np.pi * 930 * t)
            + 0.06 * np.sin(2 * np.pi * 1850 * t)
        )
        attention_tone = 0.35 * np.sin(2 * np.pi * 1050 * t)

        voice_pcm = np.clip(voice_like * 32767.0, -32768, 32767).astype("<i2").tobytes()
        tone_pcm = np.clip(attention_tone * 32767.0, -32768, 32767).astype("<i2").tobytes()

        self.assertFalse(detector.has_same_preamble(voice_pcm))
        self.assertFalse(detector.has_same_preamble(tone_pcm))

    def test_grr_rwt_fixture_detects_same_bursts_without_voice_false_trigger(self) -> None:
        fixture = REPO_ROOT / "GRR-RWT.wav"
        if not fixture.exists():
            self.skipTest("GRR-RWT.wav calibration fixture is not present")
        sample_rate, samples = self._read_float_wav(fixture)
        self.assertEqual(sample_rate, 24_000)
        detector = self.same_live.SameToneDetector(sample_rate)
        frame_samples = round(sample_rate * 0.02)
        lookbehind_frames = round(0.22 / 0.02)
        min_tone_frames = round(self.same_live.SAME_DETECT_MIN_SECONDS / 0.02)
        pending: list[bytes] = []
        tone_frames = 0
        triggers: list[float] = []
        for frame_index, offset in enumerate(range(0, len(samples) - frame_samples + 1, frame_samples)):
            frame = samples[offset : offset + frame_samples]
            pcm = np.clip(frame * 32767.0, -32768, 32767).astype("<i2").tobytes()
            pending.append(pcm)
            if len(pending) > lookbehind_frames:
                pending.pop(0)
            if detector.is_same_like(pcm):
                tone_frames += 1
            else:
                tone_frames = 0
            if tone_frames >= min_tone_frames and detector.has_same_preamble(b"".join(pending)):
                triggers.append(frame_index * 0.02)
                tone_frames = 0
                pending.clear()

        self.assertFalse([trigger for trigger in triggers if trigger < 8.0])
        self.assertTrue(any(10.0 <= trigger <= 18.0 for trigger in triggers), triggers)
        self.assertTrue(any(136.0 <= trigger <= 143.5 for trigger in triggers), triggers)

    def test_eom_event_generation_and_duplicate_suppression(self) -> None:
        events = []
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            event_sink=events.append,
            enable_decoder=False,
        )

        first = processor.submit_decoded_payload("NNNN")
        second = processor.submit_decoded_payload("NNNN")

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(events[0]["type"], "same_eom")

    def test_header_event_contains_raw_and_parsed_fields(self) -> None:
        events = []
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            event_sink=events.append,
            enable_decoder=False,
        )

        generated = processor.submit_decoded_payload("ZCZC-WXR-TOR-026139+0030-2211907-KDTX/NWS-")

        self.assertEqual(len(generated), 1)
        self.assertEqual(events[0]["type"], "same_header")
        self.assertEqual(events[0]["payload"]["raw_header"], "ZCZC-WXR-TOR-026139+0030-2211907-KDTX/NWS-")
        self.assertEqual(events[0]["payload"]["parsed"]["event_type"], "TOR")

    def test_partial_and_final_headers_emit_once(self) -> None:
        events = []
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            event_sink=events.append,
            enable_decoder=False,
        )
        header = "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"

        first = processor.submit_decoded_payload(header)
        second = processor.submit_decoded_payload(header)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["payload"]["raw_header"], header)

    def test_detector_candidate_feeds_silence_before_validation(self) -> None:
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            lookbehind_seconds=0.22,
            enable_decoder=False,
        )
        burst = self.same_live.generate_same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", 24_000)
        pcm = np.clip(burst * 32767.0, -32768, 32767).astype("<i2").tobytes()

        output = b"".join(processor.process_pcm(pcm) + processor.flush())

        self.assertGreater(len(output), 0)
        self.assertLess(np.frombuffer(output, dtype="<i2").std(), 1.0)

    def test_validated_same_replaces_following_frames_with_silence(self) -> None:
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            lookbehind_seconds=0.22,
            enable_decoder=False,
        )
        voice_like = (np.sin(2 * np.pi * 440 * np.arange(2400) / 24000) * 8000).astype("<i2").tobytes()

        processor.submit_decoded_payload("NNNN")
        output = b"".join(processor.process_pcm(voice_like))

        self.assertGreater(len(output), 0)
        self.assertLess(np.frombuffer(output, dtype="<i2").std(), 1.0)

    def test_validated_candidate_continues_feeding_silence(self) -> None:
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            lookbehind_seconds=0.22,
            enable_decoder=False,
        )
        burst = self.same_live.generate_same_burst("ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-", 24_000)
        pcm = np.clip(burst * 32767.0, -32768, 32767).astype("<i2").tobytes()
        first = pcm[: processor.frame_bytes * 24]
        second = pcm[processor.frame_bytes * 24 : processor.frame_bytes * 32]

        held_output = processor.process_pcm(first)
        processor.submit_decoded_payload("NNNN")
        validated_output = b"".join(processor.process_pcm(second))

        self.assertGreater(len(b"".join(held_output)), 0)
        self.assertLess(np.frombuffer(b"".join(held_output), dtype="<i2").std(), 1.0)
        self.assertGreater(len(validated_output), 0)
        self.assertLess(np.frombuffer(validated_output, dtype="<i2").std(), 1.0)

    def test_short_false_candidate_fails_open(self) -> None:
        processor = self.same_live.SameSuppressionProcessor(
            sample_rate=24_000,
            lookbehind_seconds=0.04,
            enable_decoder=False,
        )
        voice_like = (np.sin(2 * np.pi * 440 * np.arange(2400) / 24000) * 8000).astype("<i2").tobytes()

        output = b"".join(processor.process_pcm(voice_like) + processor.flush())

        self.assertGreater(np.frombuffer(output, dtype="<i2").std(), 1000.0)

    def test_multimon_decoder_reads_fixture_header_and_eom(self) -> None:
        fixture = REPO_ROOT / "GRR-RWT.wav"
        if not fixture.exists():
            self.skipTest("GRR-RWT.wav calibration fixture is not present")
        if shutil.which("multimon-ng") is None:
            self.skipTest("multimon-ng is not installed")
        sample_rate, samples = self._read_float_wav(fixture)
        pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2").tobytes()
        decoder = self.same_live.SameMultimonLiveDecoder(sample_rate)
        try:
            frame_bytes = round(sample_rate * 0.02) * 2
            payloads: list[str] = []
            for offset in range(0, len(pcm), frame_bytes):
                decoder.write(pcm[offset : offset + frame_bytes])
                payloads.extend(decoder.poll())
                if any(payload.startswith("ZCZC-WXR-RWT") for payload in payloads) and "NNNN" in payloads:
                    break
                time.sleep(0.001)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and "NNNN" not in payloads:
                payloads.extend(decoder.poll())
                time.sleep(0.02)
        finally:
            decoder.close()

        self.assertTrue(any(payload.startswith("ZCZC-WXR-RWT") for payload in payloads), payloads)
        self.assertIn("NNNN", payloads)

    @staticmethod
    def _band_power(samples: np.ndarray, sample_rate: int, frequency: float) -> float:
        window = samples[: max(256, round(sample_rate * 0.1))]
        t = np.arange(len(window), dtype=np.float32) / sample_rate
        sin = np.sin(2 * np.pi * frequency * t)
        cos = np.cos(2 * np.pi * frequency * t)
        return float(np.dot(window, sin) ** 2 + np.dot(window, cos) ** 2) / max(1, len(window))

    @staticmethod
    def _read_float_wav(path: Path) -> tuple[int, np.ndarray]:
        raw = path.read_bytes()
        if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
            raise AssertionError(f"{path} is not a RIFF/WAVE file")
        offset = 12
        sample_rate = 0
        data = b""
        while offset + 8 <= len(raw):
            chunk_id = raw[offset : offset + 4]
            chunk_size = struct.unpack_from("<I", raw, offset + 4)[0]
            chunk_start = offset + 8
            if chunk_id == b"fmt ":
                format_tag, channels, sample_rate, _byte_rate, _align, bits = struct.unpack_from(
                    "<HHIIHH",
                    raw,
                    chunk_start,
                )
                if format_tag != 3 or channels != 1 or bits != 32:
                    raise AssertionError(f"{path} must be mono IEEE float32 WAV")
            elif chunk_id == b"data":
                data = raw[chunk_start : chunk_start + chunk_size]
            offset += 8 + chunk_size + (chunk_size & 1)
        if not sample_rate or not data:
            raise AssertionError(f"{path} is missing fmt or data chunks")
        return sample_rate, np.frombuffer(data, dtype="<f4").astype(np.float32)


if __name__ == "__main__":
    unittest.main()
