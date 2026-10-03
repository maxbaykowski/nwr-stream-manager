from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import EasRecordingConfig, IQ_SAMPLE_RATE
from .encoder import PcmResampler


LOG = logging.getLogger(__name__)
EAS_RECORDER_RATE = 22050


class EasRecordingError(RuntimeError):
    """Raised when EAS recording cannot be initialized or fed."""


@dataclass
class EasRecorderOutput:
    config: EasRecordingConfig
    input_sample_rate: int = IQ_SAMPLE_RATE

    def __post_init__(self) -> None:
        if not self.config.enabled:
            raise EasRecordingError("EAS recording is disabled")
        try:
            from easrecorder import EASRecorder, RecorderSettings
        except ImportError as exc:
            raise EasRecordingError(
                "EAS recording requires the 'easrecorder' Python package"
            ) from exc

        self.resampler = PcmResampler(self.input_sample_rate, EAS_RECORDER_RATE)
        self.settings = RecorderSettings(
            rate=EAS_RECORDER_RATE,
            detect_rate=EAS_RECORDER_RATE,
            outdir=self.config.directory,
            pre_seconds=self.config.pre_seconds,
            post_seconds=self.config.post_seconds,
            max_seconds=self.config.max_seconds,
            save_format=self.config.format,
            local_time=self.config.local_time,
            index_path="index.json",
        )
        self.recorder = EASRecorder(self.settings)
        self.closed = False
        self.recorder.start()
        LOG.info("started EAS recording to %s", self.config.directory)

    def write(self, pcm: bytes) -> None:
        if self.closed:
            return
        audio = self.resampler.process(pcm)
        if audio:
            self.recorder.write(audio)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            tail = self.resampler.flush()
            if tail:
                try:
                    self.recorder.write(tail)
                except Exception as exc:
                    LOG.debug("EAS recorder flush failed during shutdown: %s", exc)
        finally:
            try:
                self.recorder.stop()
            except Exception as exc:
                LOG.debug("EAS recorder stop reported during shutdown: %s", exc)
            LOG.info("stopped EAS recording")


