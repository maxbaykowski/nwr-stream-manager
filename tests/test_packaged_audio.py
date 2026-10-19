from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_auth import load_web_control_module


class PackagedAudioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()
        cls.packaged = sys.modules[cls.web_control.read_packaged_audio.__module__]
        cls.shipped = cls.packaged.ASSETS_DIRECTORY

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.assets = Path(tempdir.name)
        for name in self.packaged.PACKAGED_AUDIO_SHA256:
            shutil.copy(self.shipped / name, self.assets / name)
        patcher = mock.patch.object(self.packaged, "ASSETS_DIRECTORY", self.assets)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.web_control._test_mode_same_message_audio.cache_clear()
        self.addCleanup(self.web_control._test_mode_same_message_audio.cache_clear)

    def swap(self, name: str, replacement: str = "same_test.wav") -> None:
        # What someone swapping the file would do: a valid sound, just not this one.
        shutil.copy(self.shipped / replacement, self.assets / name)

    def test_the_shipped_sounds_match_their_fingerprints(self) -> None:
        for name, expected in self.packaged.PACKAGED_AUDIO_SHA256.items():
            with self.subTest(name=name):
                actual = hashlib.sha256((self.shipped / name).read_bytes()).hexdigest()
                self.assertEqual(
                    actual, expected, f"{name} changed; if that was on purpose, set its entry in PACKAGED_AUDIO_SHA256 to {actual}"
                )

    def test_a_swapped_fallback_is_silent_and_the_stream_keeps_running(self) -> None:
        self.swap("fallback.wav")

        with self.assertLogs(self.web_control.LOG, "ERROR") as logs:
            audio = self.web_control.load_web_fallback_audio()

        self.assertEqual(audio.pcm, b"")
        self.assertIn("fallback.wav has been changed or replaced", logs.output[0])
        frame = self.web_control.next_web_fallback_frame(audio, self.web_control.WebFallbackPlaybackState(), 0.0)
        self.assertEqual(set(frame), {0})

    def test_a_file_changed_by_one_byte_is_refused(self) -> None:
        data = bytearray((self.assets / "fallback.wav").read_bytes())
        data[-1] ^= 1
        (self.assets / "fallback.wav").write_bytes(bytes(data))

        with self.assertRaisesRegex(self.packaged.PackagedAudioTampered, "changed or replaced"):
            self.packaged.read_packaged_audio("fallback.wav")

    def test_the_fallback_is_stored_at_the_stream_rate(self) -> None:
        # Streams run at 24 kHz, so the fallback needs no conversion when it plays.
        import io
        import wave

        with wave.open(io.BytesIO(self.packaged.read_packaged_audio("fallback.wav"))) as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, self.web_control.IQ_SAMPLE_RATE))

    def test_the_untouched_fallback_still_plays(self) -> None:
        audio = self.web_control.load_web_fallback_audio()

        self.assertGreater(len(audio.pcm), 0)

    def test_swapped_test_mode_sounds_are_refused(self) -> None:
        self.swap("test_mode.wav")
        with self.assertRaisesRegex(ValueError, "test_mode.wav has been changed or replaced"):
            self.web_control.SyntheticNwrTestModeSource()

        shutil.copy(self.shipped / "test_mode.wav", self.assets / "test_mode.wav")
        self.swap("same_test.wav", replacement="test_mode.wav")
        source = self.web_control.SyntheticNwrTestModeSource()
        with self.assertRaisesRegex(ValueError, "same_test.wav has been changed or replaced"):
            source.queue_same_test()

    def test_only_shipped_sounds_can_be_read(self) -> None:
        (self.assets / "other.wav").write_bytes((self.assets / "fallback.wav").read_bytes())

        with self.assertRaisesRegex(self.packaged.PackagedAudioTampered, "not a sound shipped"):
            self.packaged.read_packaged_audio("other.wav")


if __name__ == "__main__":
    unittest.main()
