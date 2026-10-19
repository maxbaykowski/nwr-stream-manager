from __future__ import annotations

import io
import json
import shutil
import sys
import tempfile
import threading
import types
import unittest
import wave
from pathlib import Path

import numpy as np

from test_auth import load_web_control_module


RATE = 24_000
SAME_HEADER = "ZCZC-WXR-TOR-026081+0030-2211907-KGRR/NWS-"


def wav_bytes(samples: np.ndarray, sample_rate: int = RATE, channels: int = 1) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes((np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
    return output.getvalue()


def tone(seconds: float, sample_rate: int = RATE, frequency: float = 440.0) -> np.ndarray:
    t = np.arange(int(seconds * sample_rate)) / sample_rate
    return (0.4 * np.sin(2 * np.pi * frequency * t)).astype(np.float32)


class CustomFallbackAudioCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()
        cls.custom = sys.modules[cls.web_control.prepare_custom_fallback_audio.__module__]
        cls.same_live = sys.modules["nwr_stream_manager.same_live"]
        cls.encoder = sys.modules["nwr_stream_manager.encoder"]
        cls.config = sys.modules["nwr_stream_manager.config"]

    def prepare(self, data: bytes):
        stored = self.custom.prepare_custom_fallback_audio(data)
        with wave.open(io.BytesIO(stored)) as wav:
            return wav.getframerate(), wav.getnchannels(), wav.getnframes() / wav.getframerate()

    def refused(self, data: bytes, message: str) -> None:
        with self.assertRaisesRegex(self.custom.CustomFallbackAudioError, message):
            self.custom.prepare_custom_fallback_audio(data)

    def encoded(self, fmt: str, samples: np.ndarray) -> bytes:
        config = self.config.IcecastConfig(
            host="localhost", port=8000, mount="/test", username="source", password="secret",
            format=fmt, sample_rate=RATE, bitrate=64,
        )
        encoder = self.encoder.create_audio_encoder(config, input_sample_rate=RATE)
        pcm = (samples * 32767).astype("<i2").tobytes()
        data = getattr(encoder, "header", b"") + encoder.encode(pcm) + encoder.flush()
        encoder.close()
        return data

    def test_wav_mp3_and_ogg_become_24_khz_mono_wav(self) -> None:
        stereo_48k = np.repeat(tone(3.0, 48_000), 2)
        for label, data in (
            ("WAV, stereo at 48 kHz", wav_bytes(stereo_48k, 48_000, channels=2)),
            ("MP3", self.encoded("mp3", tone(3.0))),
            ("OGG", self.encoded("ogg", tone(3.0))),
        ):
            with self.subTest(label):
                rate, channels, seconds = self.prepare(data)
                self.assertEqual((rate, channels), (RATE, 1))
                self.assertAlmostEqual(seconds, 3.0, delta=0.15)

    def test_one_minute_is_allowed_and_anything_longer_is_not(self) -> None:
        self.assertAlmostEqual(self.prepare(wav_bytes(tone(60.0)))[2], 60.0, places=3)
        self.refused(wav_bytes(tone(61.0)), "longer than one minute")

    def test_files_of_10_mb_or_more_are_refused_before_decoding(self) -> None:
        self.refused(b"\0" * self.custom.MAX_CUSTOM_FALLBACK_BYTES, "under 10 MB")

    def test_anything_that_is_not_an_mp3_ogg_or_wav_file_is_refused(self) -> None:
        rng = np.random.default_rng(1)
        # libsndfile reads AU files fine, but they are not one of the allowed formats.
        au = b".snd" + np.array([24, 4000, 3, 8000, 1], dtype=">u4").tobytes() + (tone(1.0, 8000) * 32767).astype(">i2").tobytes()
        for label, data in (
            ("AU", au),
            ("text", b"This is not audio.\n"),
            ("random bytes", rng.integers(0, 256, 200_000, dtype=np.uint8).tobytes()),
            ("empty", b""),
        ):
            with self.subTest(label):
                with self.assertRaises(self.custom.CustomFallbackAudioError):
                    self.custom.prepare_custom_fallback_audio(data)

    def test_files_cut_short_are_refused_as_incomplete(self) -> None:
        complete_wav = wav_bytes(tone(10.0))
        complete_ogg = self.encoded("ogg", tone(10.0))
        for label, data in (
            ("WAV", complete_wav[: len(complete_wav) // 2]),
            ("OGG", complete_ogg[: len(complete_ogg) // 2]),
        ):
            with self.subTest(label):
                self.refused(data, "incomplete")

    def test_silence_is_refused(self) -> None:
        self.refused(wav_bytes(np.zeros(RATE * 5, dtype=np.float32)), "silent")

    @unittest.skipUnless(shutil.which("multimon-ng"), "multimon-ng is not installed")
    def test_announcements_with_same_tones_are_refused(self) -> None:
        voice = tone(4.0, frequency=300.0)
        burst = self.same_live.generate_same_burst(SAME_HEADER, RATE) * 0.3
        eom = self.same_live.generate_same_message("NNNN", RATE) * 0.3
        for label, samples in (
            ("a single header burst in the middle", np.concatenate((voice, burst, voice))),
            ("a quiet full header", np.concatenate((voice, self.same_live.generate_same_message(SAME_HEADER, RATE) * 0.05))),
            ("an end of message", np.concatenate((eom, voice))),
        ):
            for fmt in ("wav", "mp3"):
                with self.subTest(label, format=fmt):
                    samples = samples.astype(np.float32)
                    data = wav_bytes(samples) if fmt == "wav" else self.encoded("mp3", samples)
                    self.refused(data, "SAME tones")

    @unittest.skipUnless(shutil.which("multimon-ng"), "multimon-ng is not installed")
    def test_the_default_announcement_has_no_same_tones(self) -> None:
        audio = self.web_control.load_web_fallback_audio()

        self.assertEqual(self.custom.find_same_tones(audio.pcm, audio.sample_rate), [])


class CustomFallbackAudioServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()
        cls.custom = sys.modules[cls.web_control.prepare_custom_fallback_audio.__module__]

    def setUp(self) -> None:
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        service = object.__new__(wc.RtlControlService)
        service.lock = threading.RLock()
        service.custom_fallback_audio_lock = threading.Lock()
        service.fallback_state_path = root / wc.FALLBACK_STATE_FILE_NAME
        service.custom_fallback_audio_path = root / wc.CUSTOM_FALLBACK_AUDIO_FILE_NAME
        service.fallback_settings = wc.WebFallbackSettings()
        service.default_fallback_audio = wc.load_web_fallback_audio()
        service.custom_fallback_audio = None
        service.custom_fallback_audio_problem = ""
        service.status = lambda: {"fallback": service._fallback_settings_public_locked()}
        self.service = service

    def reloaded(self):
        """A service started again from what was saved."""
        wc = self.web_control
        service = self.service
        service.fallback_settings = wc.load_fallback_settings(service.fallback_state_path)
        service.custom_fallback_audio = None
        service.custom_fallback_audio_problem = ""
        service._load_custom_fallback_audio()
        return service

    def test_a_saved_announcement_is_stored_and_played_from_then_on(self) -> None:
        response = self.service.save_custom_fallback_audio(wav_bytes(tone(5.0, 44_100), 44_100))

        self.assertEqual(response["fallback"]["audio"], "custom")
        self.assertTrue(response["fallback"]["custom_audio_available"])
        self.assertEqual(response["fallback"]["custom_audio_seconds"], 5.0)
        self.assertNotIn("custom_audio_sha256", response["fallback"])
        stored = json.loads(self.service.fallback_state_path.read_text())
        self.assertEqual(stored["audio"], "custom")
        self.assertEqual(stored["custom_audio_sha256"], self.custom.custom_fallback_audio_sha256(self.service.custom_fallback_audio_path.read_bytes()))
        service = self.reloaded()
        playing = service.fallback_audio_snapshot()
        self.assertEqual((playing.sample_rate, playing.source), (24_000, "custom announcement"))

    def test_a_refused_announcement_changes_nothing(self) -> None:
        with self.assertRaises(self.custom.CustomFallbackAudioError):
            self.service.save_custom_fallback_audio(b"not audio")

        self.assertFalse(self.service.custom_fallback_audio_path.exists())
        self.assertIs(self.service.fallback_audio_snapshot(), self.service.default_fallback_audio)

    def test_a_changed_announcement_file_is_not_played(self) -> None:
        self.service.save_custom_fallback_audio(wav_bytes(tone(5.0)))
        self.service.custom_fallback_audio_path.write_bytes(wav_bytes(tone(5.0, frequency=600.0)))

        service = self.reloaded()

        self.assertIs(service.fallback_audio_snapshot(), service.default_fallback_audio)
        public = service._fallback_settings_public_locked()
        self.assertFalse(public["custom_audio_available"])
        self.assertIn("changed or replaced", public["custom_audio_problem"])
        with self.assertRaisesRegex(ValueError, "changed or replaced"):
            service.fallback_announcement_wav("custom")

    def test_switching_between_the_default_and_custom_announcements(self) -> None:
        with self.assertRaisesRegex(ValueError, "Upload or record a custom announcement first"):
            self.service.update_fallback_settings({"audio": "custom"})
        self.service.save_custom_fallback_audio(wav_bytes(tone(5.0)))
        custom = self.service.fallback_audio_snapshot()

        self.service.update_fallback_settings({"audio": "default"})
        self.assertIs(self.service.fallback_audio_snapshot(), self.service.default_fallback_audio)
        self.service.update_fallback_settings({"audio": "custom"})
        self.assertIs(self.service.fallback_audio_snapshot(), custom)

    def test_the_page_cannot_set_the_announcement_checksum(self) -> None:
        self.service.save_custom_fallback_audio(wav_bytes(tone(5.0)))
        expected = self.service.fallback_settings.custom_audio_sha256

        self.service.update_fallback_settings({"loop_delay_seconds": 3, "custom_audio_sha256": "0" * 64})

        self.assertEqual(self.service.fallback_settings.custom_audio_sha256, expected)
        self.assertEqual(self.service.fallback_settings.loop_delay_seconds, 3)

    def test_both_announcements_can_be_listened_to(self) -> None:
        with self.assertRaisesRegex(ValueError, "no custom announcement"):
            self.service.fallback_announcement_wav("custom")
        default = self.service.fallback_announcement_wav("default")
        self.assertEqual(default[:4], b"RIFF")
        self.service.save_custom_fallback_audio(wav_bytes(tone(5.0)))
        self.assertEqual(self.service.fallback_announcement_wav("custom"), self.service.custom_fallback_audio_path.read_bytes())


class CustomFallbackAudioUploadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def upload(self, headers: dict[str, str], body: bytes = b""):
        handler = object.__new__(self.web_control.RtlControlHandler)
        handler.headers = headers
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        received = []
        handler.service = types.SimpleNamespace(save_custom_fallback_audio=lambda data: received.append(data) or {"ok": True})
        sent = {}
        handler._send_json = lambda payload, status=self.web_control.HTTPStatus.OK: sent.update(payload=payload, status=status)
        handler._handle_custom_fallback_audio_upload()
        return sent, received, handler.close_connection

    def test_the_file_is_passed_on_as_sent(self) -> None:
        sent, received, _closed = self.upload({"Content-Type": "application/octet-stream", "Content-Length": "4"}, b"RIFF")

        self.assertEqual(received, [b"RIFF"])
        self.assertEqual(sent["payload"], {"ok": True})

    def test_other_sites_cannot_send_one_with_a_plain_form(self) -> None:
        for content_type in ("text/plain", "multipart/form-data; boundary=x", "application/x-www-form-urlencoded", ""):
            with self.subTest(content_type=content_type):
                sent, received, _closed = self.upload({"Content-Type": content_type, "Content-Length": "4"}, b"RIFF")
                self.assertEqual(sent["status"], self.web_control.HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
                self.assertEqual(received, [])

    def test_a_file_of_10_mb_or_more_is_refused_without_reading_it(self) -> None:
        size = str(10 * 1024 * 1024)
        sent, received, closed = self.upload({"Content-Type": "application/octet-stream", "Content-Length": size})

        self.assertEqual(sent["status"], self.web_control.HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(received, [])
        self.assertTrue(closed)

    def test_read_only_accounts_cannot_upload_or_switch_announcements(self) -> None:
        handler = object.__new__(self.web_control.RtlControlHandler)

        self.assertFalse(handler._read_only_request_allowed("/api/fallback-audio", "POST"))
        self.assertFalse(handler._read_only_request_allowed("/api/fallback-audio", "GET"))
        self.assertFalse(handler._read_only_request_allowed("/api/fallback-settings", "PATCH"))


if __name__ == "__main__":
    unittest.main()
