from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_modules():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return (
        importlib.import_module("nwr_stream_manager.config"),
        importlib.import_module("nwr_stream_manager.encoder"),
    )


class EncoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config_module, cls.encoder_module = load_modules()

    def test_ogg_vorbis_encoder_uses_system_libraries(self) -> None:
        config = self.config_module.IcecastConfig(
            host="example.invalid",
            port=8000,
            mount="/test.ogg",
            username="source",
            password="secret",
            format="ogg",
            sample_rate=24_000,
            bitrate=48,
        )
        try:
            encoder = self.encoder_module.OggVorbisEncoder(config)
        except self.encoder_module.EncoderError as exc:
            self.skipTest(str(exc))
        try:
            header = encoder.header
            self.assertTrue(header.startswith(b"OggS"))
            samples = np.zeros(24_000, dtype="<i2")
            encoded = encoder.encode(samples.tobytes())
            self.assertEqual(encoder.header, header)
            encoded += encoder.flush()
            self.assertEqual(encoder.header, header)
        finally:
            encoder.close()
        self.assertIn(b"OggS", encoded)
        self.assertGreater(len(encoded), 0)


if __name__ == "__main__":
    unittest.main()
