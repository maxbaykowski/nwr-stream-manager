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

    def test_same_burst_matches_the_original_phase_continuous_encoder(self) -> None:
        # The original pure-Python encoder, kept here as the reference.
        def reference(payload: str, sample_rate: int, amplitude: float = 0.73) -> np.ndarray:
            same = self.same_live
            phase, cursor, target, output = 0.0, 0, 0.0, []
            data = [same.SAME_PREAMBLE_BYTE] * same.SAME_PREAMBLE_BYTES + [ord(ch) & 0x7F for ch in payload]
            data += [0x00] * same.SAME_TRAILING_NUL_BYTES
            for byte in data:
                for bit_index in range(8):
                    target += sample_rate / same.SAME_BAUD
                    count = int(round(target)) - cursor
                    cursor += count
                    step = 2.0 * np.pi * (same.SAME_MARK_HZ if (byte >> bit_index) & 1 else same.SAME_SPACE_HZ) / sample_rate
                    for _sample in range(count):
                        output.append(amplitude * np.sin(phase))
                        phase = (phase + step) % (2.0 * np.pi)
            return np.asarray(output, dtype=np.float32)

        payload = "ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-"
        for sample_rate in (22_050, 24_000, 48_000):
            with self.subTest(sample_rate=sample_rate):
                burst = self.same_live.generate_same_burst(payload, sample_rate)
                expected = reference(payload, sample_rate)
                self.assertEqual(burst.size, expected.size)
                np.testing.assert_allclose(burst, expected, rtol=0, atol=1e-3)

    def test_attention_tone_is_a_steady_1050_hz_tone(self) -> None:
        sample_rate = 24_000
        tone = self.same_live.generate_attention_tone(sample_rate, seconds=8.0, amplitude=0.55)
        spectrum = np.abs(np.fft.rfft(tone * np.hanning(tone.size)))
        frequencies = np.fft.rfftfreq(tone.size, 1.0 / sample_rate)

        self.assertEqual(tone.size, 8 * sample_rate)
        self.assertAlmostEqual(float(frequencies[np.argmax(spectrum)]), 1050.0, delta=0.5)
        self.assertAlmostEqual(float(np.max(np.abs(tone))), 0.55, delta=0.001)

    @unittest.skipUnless(shutil.which("multimon-ng"), "multimon-ng is not installed")
    def test_generated_same_messages_decode_with_multimon_ng(self) -> None:
        import subprocess

        sample_rate = 22_050
        header = "ZCZC-WXR-RWT-026081+0030-2211907-KGRR/NWS-"
        for label, audio in (
            ("SAME header", self.same_live.generate_same_message(header, sample_rate)),
            ("end of message", self.same_live.generate_same_message("NNNN", sample_rate)),
        ):
            with self.subTest(label):
                padded = np.concatenate((np.zeros(sample_rate, dtype=np.float32), audio, np.zeros(sample_rate, dtype=np.float32)))
                pcm = np.clip(padded * 32767.0, -32768, 32767).astype("<i2").tobytes()
                decoded = subprocess.run(
                    ["multimon-ng", "-q", "-t", "raw", "-a", "EAS", "-"],
                    input=pcm,
                    capture_output=True,
                    timeout=30,
                ).stdout.decode("utf-8", "replace")
                self.assertIn(f"EAS: {header if label == 'SAME header' else 'NNNN'}", decoded)

    class _FakeDecoder:
        """Stands in for multimon-ng: records audio written and returns queued payloads."""

        def __init__(self, sample_rate: int) -> None:
            self.sample_rate = sample_rate
            self.written: list[bytes] = []
            self.payloads: list[str] = []
            self.closed = False

        def write(self, pcm: bytes) -> None:
            self.written.append(pcm)

        def poll(self) -> list[str]:
            payloads, self.payloads = self.payloads, []
            return payloads

        def close(self) -> None:
            self.closed = True

    def _alert_decoder(self, events: list | None = None):
        return self.same_live.SameAlertDecoder(
            sample_rate=24_000,
            event_sink=None if events is None else events.append,
            decoder_factory=self._FakeDecoder,
        )

    def test_one_header_is_not_yet_an_alert_but_a_matching_second_confirms_it(self) -> None:
        events = []
        decoder = self._alert_decoder(events)
        header = "ZCZC-WXR-TOR-026139+0030-2211907-KDTX/NWS-"

        first = decoder.submit_decoded_payload(header)
        second = decoder.submit_decoded_payload(header)

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(events, second)
        self.assertEqual(events[0]["type"], "same_alert_confirmed")
        self.assertEqual(events[0]["payload"]["raw_header"], header)
        self.assertEqual(events[0]["payload"]["parsed"]["event_type"], "TOR")

    def test_first_and_third_matching_headers_confirm_alert(self) -> None:
        decoder = self._alert_decoder()
        first = "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"
        second = "ZCZC-WXR-RWT-026121-026005+0030-0911515-KGRR/NWS-"

        decoder.submit_decoded_payload(first)
        decoder.submit_decoded_payload(second)
        third = decoder.submit_decoded_payload(first)

        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["type"], "same_alert_confirmed")
        self.assertEqual(third[0]["payload"]["raw_header"], first)

    def test_second_and_third_matching_headers_confirm_alert(self) -> None:
        decoder = self._alert_decoder()
        first = "ZCZC-WXR-RWT-026121+0030-0911515-KGRR/NWS-"
        second = "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"

        decoder.submit_decoded_payload(first)
        decoder.submit_decoded_payload(second)
        third = decoder.submit_decoded_payload(second)

        self.assertEqual(len(third), 1)
        self.assertEqual(third[0]["payload"]["raw_header"], second)

    def test_header_confirmation_window_expires_after_thirty_seconds(self) -> None:
        decoder = self._alert_decoder()
        header = "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"

        decoder.submit_decoded_payload(header)
        decoder.last_decoded_header_at = time.monotonic() - 31.0
        second = decoder.submit_decoded_payload(header)

        self.assertEqual(second, [])
        self.assertEqual(list(decoder.decoded_header_window), [header])

    def test_confirmed_alert_clears_the_window_and_is_not_repeated_soon_after(self) -> None:
        decoder = self._alert_decoder()
        header = "ZCZC-WXR-RWT-026121-026005-026139+0030-0911515-KGRR/NWS-"

        decoder.submit_decoded_payload(header)
        confirmed = decoder.submit_decoded_payload(header)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(list(decoder.decoded_header_window), [])
        self.assertIsNone(decoder.last_decoded_header_at)

        # The third burst of the same message must not announce the alert a second time.
        decoder.submit_decoded_payload(header)
        self.assertEqual(decoder.submit_decoded_payload(header), [])

    def test_end_of_message_resets_the_confirmation_window(self) -> None:
        decoder = self._alert_decoder()
        header = "ZCZC-WXR-RWT-026121+0030-0911515-KGRR/NWS-"

        decoder.submit_decoded_payload(header)
        self.assertEqual(decoder.submit_decoded_payload("NNNN"), [])
        self.assertEqual(decoder.submit_decoded_payload(header), [])

    def test_audio_goes_to_multimon_and_decoded_alerts_come_back(self) -> None:
        events = []
        decoder = self._alert_decoder(events)
        header = "ZCZC-WXR-RWT-026121+0030-0911515-KGRR/NWS-"
        pcm = b"\x01\x02" * 480

        decoder.decoder.payloads = [header, header]
        returned = decoder.process_pcm(pcm)
        decoder.close()

        self.assertEqual(decoder.decoder.written, [pcm])
        self.assertEqual([event["type"] for event in returned], ["same_alert_confirmed"])
        self.assertEqual(events, returned)
        self.assertTrue(decoder.decoder.closed)

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
