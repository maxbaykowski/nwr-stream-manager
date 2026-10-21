"""The Weather Radio Receiver: any NWR channel, tuned and played live in a browser."""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

from .audio_effects import AudioEffectsProcessor
from .config import IQ_SAMPLE_RATE
from .constants import (
    CHANNEL_IQ_ALIAS_TRANSITION_HZ,
    LIVE_IQ_QUEUE_SECONDS,
    RECEIVER_AUDIO_CONFIG,
    STREAM_FRAME_SAMPLES,
)
from .dsp import DEFAULT_ALIAS_ATTENUATION_DB, IqChannelizer
from .iq_pipeline import (
    alias_filter_attenuation_db,
    alias_filter_transition_hz,
    batch_generation,
    drain_queue_to_latest,
    IntermediateIqFanout,
    iq_batch_complex,
    make_live_channelizer,
    QUEUE_WAKEUP,
    RawRtlFanout,
    receiver_frequency_mhz,
    subscribe_raw_fanout,
    validate_receiver_frequency,
    wake_queue,
)
from .live_audio import LiveAudioSource
from .nfm import float_to_s16, NfmDemodulator
from .rtl import RtlSampleBatch
from .same_live import SameEventQueue
from .stream_audio import FloatFrameBuffer
from .stream_worker import channel_frequency_for_batch


LOG = logging.getLogger(__name__)


class WeatherReceiverWorker:
    def __init__(
        self,
        *,
        client_id: str,
        fanout: RawRtlFanout | IntermediateIqFanout,
        frequency_hz: int,
        alias_filter_strength_provider,
        event_queue: SameEventQueue | None = None,
    ) -> None:
        self.client_id = client_id
        self.fanout = fanout
        self.alias_filter_strength_provider = alias_filter_strength_provider
        frequency_hz = validate_receiver_frequency(frequency_hz)
        prebuilt = make_live_channelizer(
            fanout=fanout,
            target_frequency_hz=frequency_hz,
            alias_filter_strength=self.alias_filter_strength_provider(),
        )
        self._initial_channelizer = prebuilt[0] if prebuilt is not None else None
        self._initial_channelizer_key = prebuilt[1] if prebuilt is not None else None
        self._initial_channelizer_alias_filter_strength = prebuilt[2] if prebuilt is not None else None
        self.queue = subscribe_raw_fanout(
            fanout,
            max_seconds=LIVE_IQ_QUEUE_SECONDS,
            name=f"receiver:{client_id}",
        )
        self.source = LiveAudioSource(sample_rate=IQ_SAMPLE_RATE, event_queue=event_queue)
        self.frequency_hz = frequency_hz
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"weather-receiver-{client_id}",
            daemon=True,
        )
        self.lock = threading.Lock()
        self.last_audio_at: float | None = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.fanout.unsubscribe(self.queue)
        wake_queue(self.queue)
        self.source.close()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)

    def set_frequency(self, frequency_hz: int) -> int:
        frequency_hz = validate_receiver_frequency(frequency_hz)
        with self.lock:
            self.frequency_hz = frequency_hz
        return frequency_hz

    def set_paused(self, paused: bool) -> None:
        self.source.set_paused(paused)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            frequency_hz = self.frequency_hz
            last_audio_at = self.last_audio_at
        return {
            "client_id": self.client_id,
            "frequency_hz": frequency_hz,
            "frequency_mhz": receiver_frequency_mhz(frequency_hz),
            "paused": self.source.is_paused(),
            "last_audio_at": last_audio_at,
            "stats": self.source.stats(),
            "raw_queue": self.raw_queue_stats(),
        }

    def raw_queue_stats(self) -> dict[str, Any]:
        subscriber_stats = getattr(self.fanout, "subscriber_stats", None)
        if subscriber_stats is None:
            return {}
        return subscriber_stats(self.queue)

    def _frequency_hz(self) -> int:
        with self.lock:
            return self.frequency_hz

    def _run(self) -> None:
        channelizer: IqChannelizer | None = self._initial_channelizer
        channelizer_key: tuple[int, int] | None = self._initial_channelizer_key
        channelizer_alias_filter_strength: int | None = self._initial_channelizer_alias_filter_strength
        channelizer_target_frequency_hz: int | None = (
            int(getattr(channelizer, "target_frequency_hz", 0)) if channelizer is not None else None
        )
        startup_backlog_drained = False
        last_slow_batch_log_at = 0.0
        demodulator = NfmDemodulator()
        effects = AudioEffectsProcessor(RECEIVER_AUDIO_CONFIG)
        frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        source_generation = getattr(self.fanout, "generation", 0)
        while not self.stop_event.is_set():
            try:
                batch: RtlSampleBatch = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                LOG.warning("weather receiver RTL-SDR source failed for client %s: %s", self.client_id, exc)
                continue
            if batch is QUEUE_WAKEUP:
                continue
            if not startup_backlog_drained:
                batch = drain_queue_to_latest(self.queue, batch)
                startup_backlog_drained = True
            current_generation = getattr(self.fanout, "generation", source_generation)
            incoming_generation = batch_generation(batch)
            if incoming_generation < current_generation:
                continue
            if incoming_generation != source_generation:
                source_generation = incoming_generation
                channelizer = None
                channelizer_key = None
                channelizer_alias_filter_strength = None
                channelizer_target_frequency_hz = None
                demodulator = NfmDemodulator()
                frame_buffer.clear()
                LOG.info("weather receiver source generation changed for client %s; reset channel state", self.client_id)
            target_frequency_hz = self._frequency_hz()
            retune_remote_feed = getattr(self.fanout, "set_target_frequency", None)
            if retune_remote_feed is not None:
                retune_remote_feed(target_frequency_hz)
            target_frequency_hz = channel_frequency_for_batch(batch, target_frequency_hz)
            alias_filter_strength = self.alias_filter_strength_provider()
            channel_transition_hz = alias_filter_transition_hz(
                CHANNEL_IQ_ALIAS_TRANSITION_HZ,
                alias_filter_strength,
            )
            channel_attenuation_db = alias_filter_attenuation_db(
                DEFAULT_ALIAS_ATTENUATION_DB,
                alias_filter_strength,
            )
            next_channelizer_key = (batch.sample_rate, batch.center_frequency_hz)
            if channelizer is None or channelizer_key != next_channelizer_key:
                # A remote retune only changes the batch's frequency; keep the
                # partly built audio frame so the switch is seamless.
                remote_retune = (
                    getattr(batch, "prechannelized", False)
                    and channelizer_key is not None
                    and channelizer_key[0] == batch.sample_rate
                )
                channelizer = IqChannelizer(
                    input_rate=batch.sample_rate,
                    center_frequency_hz=batch.center_frequency_hz,
                    target_frequency_hz=target_frequency_hz,
                    output_rate=IQ_SAMPLE_RATE,
                    transition_hz=channel_transition_hz,
                    alias_attenuation_db=channel_attenuation_db,
                )
                channelizer_key = next_channelizer_key
                channelizer_alias_filter_strength = alias_filter_strength
                channelizer_target_frequency_hz = target_frequency_hz
                if remote_retune:
                    demodulator.reset()
                else:
                    demodulator = NfmDemodulator()
                    frame_buffer.clear()
            elif channelizer_alias_filter_strength != alias_filter_strength:
                channelizer.update_alias_filter(
                    transition_hz=channel_transition_hz,
                    attenuation_db=channel_attenuation_db,
                )
                channelizer_alias_filter_strength = alias_filter_strength
            if channelizer is not None:
                if channelizer_target_frequency_hz != target_frequency_hz:
                    channelizer.set_target_frequency(target_frequency_hz)
                    channelizer_target_frequency_hz = target_frequency_hz
                    demodulator.reset()
            iq = iq_batch_complex(batch)
            process_started_at = time.monotonic()
            audio = demodulator.process(channelizer.process_complex(iq))
            process_seconds = time.monotonic() - process_started_at
            batch_seconds = float(iq.size) / float(max(1, batch.sample_rate))
            if process_seconds > batch_seconds and process_started_at - last_slow_batch_log_at >= 60.0:
                last_slow_batch_log_at = process_started_at
                LOG.warning(
                    "weather receiver DSP is slower than realtime for client %s: processed %.3fs IQ in %.3fs",
                    self.client_id,
                    batch_seconds,
                    process_seconds,
                )
            if len(audio) == 0:
                continue
            for frame in frame_buffer.push(audio):
                self.source.push_pcm(float_to_s16(effects.process(frame)))
                with self.lock:
                    self.last_audio_at = time.time()
