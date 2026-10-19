"""Sounds shipped with NWR Stream Manager, checked before they are played.

Listeners hear these: the fallback audio while a station is off the air, and the
test announcement and SAME test in test mode. Each file's SHA-256 is fixed here,
so a file that has been swapped or altered is refused rather than played. Making
a different sound play then takes changing this code too, not just the file.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .config_errors import ConfigError


ASSETS_DIRECTORY = Path(__file__).resolve().parent / "assets"
# Changing a sound on purpose means updating its entry here as well; the tests
# report the new value.
PACKAGED_AUDIO_SHA256 = {
    "fallback.wav": "0268a9a11e9c699865987c4b2056a98b361c5447f3809987e5e7da0a43f0c0c9",
    "same_test.wav": "9383c3471e8405126d4acff0744ab22eaed37790df514156e5d9f4076a4ac484",
    "test_mode.wav": "f7c89ac93125ed4f5a53ae35054149ea8676ab342a8672ada6e046621c88f4e8",
}


class PackagedAudioTampered(ConfigError):
    """A packaged sound is not the one NWR Stream Manager shipped with."""


def read_packaged_audio(name: str) -> bytes:
    """The bytes of a packaged sound, only if they are exactly the shipped file.

    The file is read once and the same bytes are checked and returned, so it cannot
    be changed between the check and its use.
    """
    expected = PACKAGED_AUDIO_SHA256.get(name)
    if expected is None:
        raise PackagedAudioTampered(f"{name} is not a sound shipped with NWR Stream Manager")
    try:
        data = (ASSETS_DIRECTORY / name).read_bytes()
    except OSError as exc:
        raise ConfigError(f"{name} could not be read: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != expected:
        raise PackagedAudioTampered(
            f"{name} has been changed or replaced since NWR Stream Manager was installed, so it will not be played"
        )
    return data
