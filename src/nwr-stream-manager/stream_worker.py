"""Streams at work: turning a station's channel into audio and sending it to Icecast and sound cards."""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from .alsa import (
    AlsaSharedInputConfig,
    AlsaSharedPlaybackTap,
    AlsaStreamPlaybackTap,
    discover_playback_devices,
)
from .audio_effects import AudioEffectsProcessor
from .config import AudioConfig, EasRecordingConfig, IcecastConfig, IQ_SAMPLE_RATE
from .constants import (
    CHANNEL_IQ_ALIAS_TRANSITION_HZ,
    LIVE_IQ_QUEUE_SECONDS,
    STREAM_FALLBACK_DISCONNECT_ICECAST,
    STREAM_FALLBACK_NONE,
    STREAM_FALLBACK_PLAY_AUDIO,
    STREAM_FRAME_SAMPLES,
    STREAM_FRAME_SECONDS,
    STREAM_IDLE_DETECTION_SECONDS,
    STREAM_RECONNECT_SECONDS,
    STREAM_SILENCE_FLOAT_FRAME,
    STREAM_SILENCE_FRAME,
    STREAM_TEST_MODE_CROSSFADE_SECONDS,
    STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS,
)
from .dsp import DEFAULT_ALIAS_ATTENUATION_DB, IqChannelizer
from .eas_alerts import eas_config_from_stream, eas_recording_settings_from_stream
from .eas_recording import EasRecorderOutput
from .encoder import create_audio_encoder
from .fallback_settings import stream_fallback_action_from_stream
from .icecast import IcecastSource
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
    subscribe_raw_fanout,
    wake_queue,
)
from .live_audio import LiveAudioSource
from .models import WebFallbackPlaybackState, WebStreamFallbackAction
from .nfm import float_to_s16, NfmDemodulator
from .rtl import NWR_CENTER_FREQUENCY_HZ, RtlSampleBatch
from .signal_meter import ChannelSignalMeter, noise_reference_band_for_transition, SignalPresenceTracker
from .storage import storage_status_message, StorageMonitor
from .stream_audio import (
    ComplexFrameBuffer,
    FloatFrameBuffer,
    load_web_fallback_audio,
    next_web_fallback_frame,
    pcm_s16le_to_float32,
)
from .stream_config import (
    audio_config_from_stream,
    friendly_stream_error,
    icecast_config_from_output,
    icecast_encoder_key,
    icecast_output_signature,
    soundcard_tap_config_from_output,
    soundcard_tap_config_from_soundcard,
    stream_outputs,
)
from .simulated_station import mix_test_mode_iq_crossfade, SyntheticNwrTestModeSource


LOG = logging.getLogger(__name__)


class SharedSoundcardOutputManager:
    def __init__(self, *, devices_provider=None) -> None:
        self.devices_provider = devices_provider or discover_playback_devices
        self.lock = threading.RLock()
        self.sessions: dict[str, Any] = {}
        self.outputs: dict[str, dict[str, Any]] = {}

    def sync_output(self, output_id: str, soundcard: dict[str, Any], owner: Any = None) -> None:
        output_id = str(output_id)
        config = soundcard_tap_config_from_soundcard(soundcard)
        input_config = AlsaSharedInputConfig(
            channel_mode=config.channel_mode,
            software_volume=config.software_volume,
        )
        with self.lock:
            old = self.outputs.get(output_id)
            if old is not None and old["stable_id"] != config.stable_id:
                old_session = self.sessions.get(old["stable_id"])
                if old_session is not None:
                    old_session.unregister_input(output_id)
                    if not old_session.has_inputs():
                        old_session.stop()
                        self.sessions.pop(old["stable_id"], None)
            session = self.sessions.get(config.stable_id)
            if session is not None and not self._session_alive(session):
                session.stop()
                self.sessions.pop(config.stable_id, None)
                session = None
            if session is None:
                session = AlsaSharedPlaybackTap(
                    config.stable_id,
                    output_sample_rate=config.output_sample_rate,
                    devices_provider=self.devices_provider,
                )
                self.sessions[config.stable_id] = session
                session.start()
                for existing_output_id, existing in self.outputs.items():
                    if existing_output_id == output_id or existing.get("stable_id") != config.stable_id:
                        continue
                    session.register_input(
                        existing_output_id,
                        AlsaSharedInputConfig(
                            channel_mode=existing["channel_mode"],
                            software_volume=existing["software_volume"],
                        ),
                    )
            session.register_input(output_id, input_config)
            self.outputs[output_id] = {
                "stable_id": config.stable_id,
                "channel_mode": config.channel_mode,
                "software_volume": config.software_volume,
                "owner": owner,
            }

    def remove_output(self, output_id: str, owner: Any = None) -> None:
        output_id = str(output_id)
        with self.lock:
            old = self.outputs.get(output_id)
            if old is None:
                return
            if owner is not None and old.get("owner") is not owner:
                return
            self.outputs.pop(output_id, None)
            session = self.sessions.get(old["stable_id"])
            if session is None:
                return
            session.unregister_input(output_id)
            if not session.has_inputs():
                session.stop()
                self.sessions.pop(old["stable_id"], None)

    def push_float(self, output_id: str, samples: np.ndarray, owner: Any = None) -> None:
        with self.lock:
            old = self.outputs.get(str(output_id))
            if old is not None and owner is not None and old.get("owner") is not owner:
                old = None
            session = self.sessions.get(old["stable_id"]) if old is not None else None
        if session is not None:
            session.push_float(str(output_id), samples)

    def snapshot(self, output_id: str) -> dict[str, Any]:
        with self.lock:
            old = self.outputs.get(str(output_id))
            session = self.sessions.get(old["stable_id"]) if old is not None else None
        return session.snapshot() if session is not None else {"status": "disabled", "error": "", "stable_id": "", "device": ""}

    def prepare_stable_id_for_reset(self, stable_id: str) -> bool:
        with self.lock:
            session = self.sessions.get(str(stable_id))
        if session is None:
            return False
        prepare = getattr(session, "prepare_for_usb_reset", None)
        if callable(prepare):
            prepare()
        else:
            session.stop()
        return True

    def stop(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
            self.sessions = {}
            self.outputs = {}
        for session in sessions:
            session.stop()

    @staticmethod
    def _session_alive(session: Any) -> bool:
        thread = getattr(session, "thread", None)
        if thread is None:
            return True
        is_alive = getattr(thread, "is_alive", None)
        return bool(is_alive()) if callable(is_alive) else True


class IcecastStreamWorker:
    def __init__(
        self,
        *,
        stream: dict[str, Any],
        fanout: RawRtlFanout | IntermediateIqFanout,
        fallback_settings_provider,
        alias_filter_strength_provider,
        state_directory: Path,
        fallback_audio_provider=None,
        gain_provider=lambda: None,
        soundcard_devices_provider=None,
        soundcard_manager: SharedSoundcardOutputManager | None = None,
        storage_monitor: StorageMonitor | None = None,
    ) -> None:
        self.stream = stream
        self.fanout = fanout
        self.fallback_settings_provider = fallback_settings_provider
        self.fallback_audio_provider = fallback_audio_provider
        self.alias_filter_strength_provider = alias_filter_strength_provider
        self.gain_provider = gain_provider
        self._soundcard_devices_provider = soundcard_devices_provider or discover_playback_devices
        self.soundcard_manager = soundcard_manager
        self.state_directory = state_directory
        self.storage_monitor = storage_monitor or StorageMonitor([state_directory])
        station = stream.get("station", {})
        stream_label = station.get("callsign") or stream.get("id", "stream")
        try:
            target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
        except Exception:
            target_frequency_hz = NWR_CENTER_FREQUENCY_HZ
        prebuilt = make_live_channelizer(
            fanout=fanout,
            target_frequency_hz=target_frequency_hz,
            alias_filter_strength=self.alias_filter_strength_provider(),
        )
        self._initial_channelizer = prebuilt[0] if prebuilt is not None else None
        self._initial_channelizer_key = prebuilt[1] if prebuilt is not None else None
        self._initial_channelizer_alias_filter_strength = prebuilt[2] if prebuilt is not None else None
        self.queue = subscribe_raw_fanout(
            fanout,
            max_seconds=LIVE_IQ_QUEUE_SECONDS,
            name=f"stream:{stream_label}",
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run_pcm_producer, name=f"icecast-stream-{stream['id']}", daemon=True)
        self.outputs: dict[str, IcecastOutputWriter] = {}
        self.encoder_groups: dict[tuple[str, int, int], IcecastEncoderGroup] = {}
        self.monitor_sources: dict[str, LiveAudioSource] = {}
        self.soundcard_outputs: set[str] = set()
        self.eas_config: EasRecordingConfig | None = None
        self.eas_recorder: EasRecorderOutput | None = None
        self.eas_status = "disabled"
        self.eas_error: str | None = None
        self.last_audio_at: float | None = None
        self.test_mode_source: SyntheticNwrTestModeSource | None = None
        self.signal_presence = SignalPresenceTracker()
        self.signal_meter = ChannelSignalMeter(IQ_SAMPLE_RATE, on_measurement=self.signal_presence.update)
        self._configure_signal_meter(
            alias_filter_transition_hz(CHANNEL_IQ_ALIAS_TRANSITION_HZ, self.alias_filter_strength_provider())
        )
        self._soundcard_owner = object()
        # Set while this stream's fallback action has its Icecast outputs disconnected.
        self.icecast_disconnected = threading.Event()
        self.lock = threading.Lock()

    @property
    def id(self) -> str:
        return str(self.stream["id"])

    def start(self) -> None:
        self.sync_stream(self.stream)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.icecast_disconnected.clear()
        self.fanout.unsubscribe(self.queue)
        wake_queue(self.queue)
        outputs = list(self.outputs.values())
        encoder_groups = list(self.encoder_groups.values())
        # Tell everything to stop before waiting on any of it, so the waits overlap.
        for output in outputs:
            output.request_stop()
        for encoder_group in encoder_groups:
            encoder_group.request_stop()
        for output in outputs:
            output.stop()
        self.outputs = {}
        for encoder_group in encoder_groups:
            encoder_group.stop()
        self.encoder_groups = {}
        for source in list(self.monitor_sources.values()):
            source.close()
        self.monitor_sources = {}
        for output_id in list(self.soundcard_outputs):
            if self.soundcard_manager is not None:
                self.soundcard_manager.remove_output(output_id, owner=self._soundcard_owner)
        self.soundcard_outputs = set()
        self._stop_eas_recorder()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)

    def sync_stream(self, stream: dict[str, Any]) -> None:
        with self.lock:
            self.stream = stream
        desired = {
            str(output.get("id", "")): output
            for output in stream_outputs(stream)
            if output.get("enabled", True) and output.get("type", "icecast") == "icecast"
        }
        for output_id in list(self.outputs):
            writer = self.outputs[output_id]
            output = desired.get(output_id)
            if output is None or writer.signature != icecast_output_signature(output):
                self.outputs.pop(output_id).stop()
        for output_id, output in desired.items():
            if output_id in self.outputs:
                continue
            writer = IcecastOutputWriter(self, output)
            self.outputs[output_id] = writer
            writer.start()
        self._sync_soundcard_taps(stream)
        self._sync_eas_recorder(stream)

    def audio_config(self) -> AudioConfig:
        with self.lock:
            stream = self.stream
        return audio_config_from_stream(stream)

    def fallback_action(self) -> WebStreamFallbackAction:
        with self.lock:
            stream = self.stream
        return stream_fallback_action_from_stream(stream)

    def _begin_fallback_action(self, action: str, seconds: float, reason: str) -> None:
        callsign = self.stream.get("station", {}).get("callsign")
        if action == STREAM_FALLBACK_DISCONNECT_ICECAST:
            LOG.info("disconnecting Icecast outputs for %s after %.1f seconds %s", callsign, seconds, reason)
            self.icecast_disconnected.set()
        else:
            LOG.info("starting fallback audio for %s after %.1f seconds %s", callsign, seconds, reason)

    def _end_fallback_action(self, action: str) -> None:
        callsign = self.stream.get("station", {}).get("callsign")
        if action == STREAM_FALLBACK_DISCONNECT_ICECAST:
            LOG.info("reconnecting Icecast outputs for %s", callsign)
            self.icecast_disconnected.clear()
        else:
            LOG.info("stopping fallback audio for %s", callsign)

    def snapshots(self) -> list[dict[str, Any]]:
        snapshots = [output.snapshot() for output in list(self.outputs.values())]
        with self.lock:
            output_ids = list(self.soundcard_outputs)
            configured_soundcards = {
                str(output.get("id", "")): dict(output.get("soundcard", {}))
                for output in stream_outputs(self.stream)
                if output.get("type") == "soundcard"
            }
        for output_id in output_ids:
            snapshot = self.soundcard_manager.snapshot(output_id) if self.soundcard_manager is not None else {"status": "disabled"}
            configured_soundcard = configured_soundcards.get(output_id, {})
            snapshot.update(
                {
                    "id": self.id,
                    "output_id": output_id,
                    "type": "soundcard",
                    "status": "enabled" if snapshot.get("status") == "enabled" else "needs-attention",
                    "outputs": [
                        {
                            "id": output_id,
                            "type": "soundcard",
                            "status": snapshot.get("status"),
                            "soundcard": {
                                "stable_id": snapshot.get("stable_id", "") or configured_soundcard.get("stable_id", ""),
                                "device": snapshot.get("device", ""),
                                "display_name": snapshot.get("display_name", "") or configured_soundcard.get("display_name", ""),
                                "card_name": snapshot.get("card_name", "") or configured_soundcard.get("card_name", ""),
                                "card_long_name": snapshot.get("card_long_name", "") or configured_soundcard.get("card_long_name", ""),
                                "pcm_name": snapshot.get("pcm_name", "") or configured_soundcard.get("pcm_name", ""),
                            },
                            "error": snapshot.get("error", ""),
                        }
                    ],
                }
            )
            snapshots.append(snapshot)
        return snapshots

    def raw_queue_stats(self) -> dict[str, Any]:
        subscriber_stats = getattr(self.fanout, "subscriber_stats", None)
        if subscriber_stats is None:
            return {}
        return subscriber_stats(self.queue)

    def signal_snapshot(self) -> dict[str, Any]:
        return self.signal_meter.snapshot()

    def _configure_signal_meter(self, channel_transition_hz: float) -> None:
        self.signal_meter.reset()
        self.signal_meter.set_noise_reference(
            *noise_reference_band_for_transition(IQ_SAMPLE_RATE, channel_transition_hz)
        )

    def eas_snapshot(self) -> dict[str, Any] | None:
        with self.lock:
            config = self.eas_config
            status = self.eas_status
            error = self.eas_error
        if config is None:
            return None
        return {
            "id": self.id,
            "status": status,
            "error": error,
            "directory": config.directory,
        }

    def add_monitor_source(self, client_id: str) -> LiveAudioSource:
        # Monitoring plays the stream as it is, so no SAME decoder runs behind it.
        source = LiveAudioSource(sample_rate=IQ_SAMPLE_RATE)
        with self.lock:
            old_source = self.monitor_sources.pop(client_id, None)
            self.monitor_sources[client_id] = source
        if old_source is not None:
            old_source.close()
        return source

    def remove_monitor_source(self, client_id: str) -> None:
        source = self.detach_monitor_source(client_id)
        if source is not None:
            source.close()

    def detach_monitor_source(self, client_id: str) -> LiveAudioSource | None:
        with self.lock:
            return self.monitor_sources.pop(client_id, None)

    def set_monitor_source_paused(self, client_id: str, paused: bool) -> bool:
        with self.lock:
            source = self.monitor_sources.get(client_id)
        if source is None:
            return False
        source.set_paused(paused)
        return True

    def monitor_source_stats(self, client_id: str) -> dict[str, Any]:
        with self.lock:
            source = self.monitor_sources.get(client_id)
        return source.stats() if source is not None else {}

    def set_test_mode(self, enabled: bool, signal_dbfs: Any = STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS) -> dict[str, Any]:
        with self.lock:
            if enabled:
                created = self.test_mode_source is None or self.test_mode_source.stop_requested
                if self.test_mode_source is None or self.test_mode_source.stop_requested:
                    self.test_mode_source = SyntheticNwrTestModeSource()
                self.test_mode_source.set_signal_dbfs(signal_dbfs, immediate=created)
            else:
                if self.test_mode_source is not None:
                    return self.test_mode_source.request_stop()
            return self.test_mode_snapshot_locked()

    def update_test_mode_signal(self, signal_dbfs: Any) -> dict[str, Any]:
        with self.lock:
            if self.test_mode_source is None:
                raise ValueError("test mode is not active for this stream")
            self.test_mode_source.set_signal_dbfs(signal_dbfs)
            return self.test_mode_snapshot_locked()

    def update_test_mode_tones(self, raw: Any) -> dict[str, Any]:
        with self.lock:
            if self.test_mode_source is None:
                raise ValueError("test mode is not active for this stream")
            self.test_mode_source.update_tones(raw)
            return self.test_mode_snapshot_locked()

    def trigger_test_mode_same(self) -> dict[str, Any]:
        with self.lock:
            if self.test_mode_source is None:
                raise ValueError("test mode is not active for this stream")
            queued = self.test_mode_source.queue_same_test()
            snapshot = self.test_mode_snapshot_locked()
        if not queued:
            raise ValueError("A SAME test is already running")
        return snapshot

    def test_mode_snapshot(self) -> dict[str, Any]:
        with self.lock:
            return self.test_mode_snapshot_locked()

    def test_mode_snapshot_locked(self) -> dict[str, Any]:
        if self.test_mode_source is None:
            return {"active": False, "same_active": False, "signal_dbfs": STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS}
        snapshot = self.test_mode_source.snapshot()
        return {
            "active": True,
            "same_active": bool(snapshot.get("same_active", False)),
            "stopping": bool(snapshot.get("stop_requested", False)),
            "stop_ready": bool(snapshot.get("stop_ready", False)),
            "signal_dbfs": float(snapshot.get("signal_dbfs", STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS)),
            "current_signal_dbfs": float(snapshot.get("current_signal_dbfs", snapshot.get("signal_dbfs", STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS))),
            "tones": snapshot.get("tones", {}),
        }

    def has_monitor_sources(self) -> bool:
        with self.lock:
            return bool(self.monitor_sources)

    def add_soundcard_tap(self, tap_id: str, tap: Any) -> None:
        # Compatibility hook for older focused tests. Runtime soundcards use
        # SharedSoundcardOutputManager so one ALSA device is opened once.
        if not hasattr(self, "soundcard_taps"):
            self.soundcard_taps = {}
        tap_id = str(tap_id)
        self.soundcard_taps[tap_id] = tap
        start = getattr(tap, "start", None)
        if start is not None:
            start()

    def remove_soundcard_tap(self, tap_id: str) -> None:
        tap = getattr(self, "soundcard_taps", {}).pop(str(tap_id), None)
        if tap is not None:
            close = getattr(tap, "close", None) or getattr(tap, "stop", None)
            if close is not None:
                close()

    def has_soundcard_taps(self) -> bool:
        with self.lock:
            return bool(getattr(self, "soundcard_outputs", set())) or bool(getattr(self, "soundcard_taps", {}))

    def _sync_soundcard_taps(self, stream: dict[str, Any]) -> None:
        desired = {
            str(output.get("id", "")): output
            for output in stream_outputs(stream)
            if output.get("enabled", True) and output.get("type") == "soundcard"
        }
        if not hasattr(self, "soundcard_outputs"):
            self.soundcard_outputs = set()
        if getattr(self, "soundcard_manager", None) is None:
            self._sync_legacy_soundcard_taps(desired)
            return
        for output_id in list(self.soundcard_outputs):
            if output_id not in desired:
                if self.soundcard_manager is not None:
                    self.soundcard_manager.remove_output(output_id, owner=self._soundcard_owner)
                self.soundcard_outputs.discard(output_id)
        for output_id, output in desired.items():
            if self.soundcard_manager is None:
                continue
            self.soundcard_manager.sync_output(output_id, output.get("soundcard", {}), owner=self._soundcard_owner)
            self.soundcard_outputs.add(output_id)

    def _sync_legacy_soundcard_taps(self, desired: dict[str, dict[str, Any]]) -> None:
        if not hasattr(self, "soundcard_taps"):
            self.soundcard_taps = {}
        for output_id in list(self.soundcard_taps):
            output = desired.get(output_id)
            tap = self.soundcard_taps[output_id]
            if output is None:
                self.remove_soundcard_tap(output_id)
                continue
            config = soundcard_tap_config_from_output(output)
            current_config = getattr(tap, "config", None)
            if current_config is None or current_config.stable_id != config.stable_id or current_config.output_sample_rate != config.output_sample_rate:
                self.remove_soundcard_tap(output_id)
                continue
            try:
                tap.set_channel_mode(config.channel_mode)
                tap.set_software_volume(config.software_volume)
            except Exception as exc:
                LOG.warning("failed to update soundcard tap %s live: %s", output_id, exc)
        for output_id, output in desired.items():
            if output_id in self.soundcard_taps:
                continue
            tap = AlsaStreamPlaybackTap(
                soundcard_tap_config_from_output(output),
                devices_provider=self._soundcard_devices_provider,
            )
            self.add_soundcard_tap(output_id, tap)

    def _run_pcm_producer(self) -> None:
        channelizer: IqChannelizer | None = self._initial_channelizer
        channelizer_key: tuple[int, int] | None = self._initial_channelizer_key
        channelizer_alias_filter_strength: int | None = self._initial_channelizer_alias_filter_strength
        channelizer_target_frequency_hz: int | None = (
            int(getattr(channelizer, "target_frequency_hz", 0)) if channelizer is not None else None
        )
        signal_meter_gain = self.gain_provider()
        startup_backlog_drained = False
        last_slow_batch_log_at = 0.0
        demodulator = NfmDemodulator()
        audio_config = self.audio_config()
        effects = AudioEffectsProcessor(audio_config)
        frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        fallback_audio_provider = self.fallback_audio_provider
        if fallback_audio_provider is None:
            packaged_fallback = load_web_fallback_audio()
            fallback_audio_provider = lambda: packaged_fallback
        fallback = fallback_audio_provider()
        fallback_state = WebFallbackPlaybackState()

        def next_fallback_frame(loop_delay_seconds: float) -> bytes:
            nonlocal fallback
            latest = fallback_audio_provider()
            if latest is not fallback:
                # A different announcement was chosen: play it from the start.
                fallback = latest
                fallback_state.position = 0
                fallback_state.delay_samples_remaining = 0
            return next_web_fallback_frame(fallback, fallback_state, loop_delay_seconds)
        # Whether this stream's fallback action is in effect, and which one.
        fallback_engaged = False
        fallback_engaged_as = STREAM_FALLBACK_NONE

        def end_fallback() -> None:
            nonlocal fallback_engaged
            if fallback_engaged:
                self._end_fallback_action(fallback_engaged_as)
                fallback_engaged = False
            fallback_state.reset()
        last_real_audio = time.monotonic()
        # When the current fallback condition began: the SDR delivering no IQ,
        # the station having no signal, or one handing over to the other. It is
        # only cleared once IQ is flowing and the signal is not lost, so a
        # dropout during a no-signal period does not restart the delay.
        fallback_since: float | None = None
        idle_output_active = False
        idle_next_frame_at: float | None = None
        source_generation = getattr(self.fanout, "generation", 0)
        test_mode_was_active = False
        test_mode_next_frame_at: float | None = None
        test_demodulator = NfmDemodulator()
        test_frame_buffer = FloatFrameBuffer(STREAM_FRAME_SAMPLES)
        real_test_iq_frame_buffer = ComplexFrameBuffer(STREAM_FRAME_SAMPLES)
        real_test_iq_frames: deque[np.ndarray] = deque()
        test_crossfade_state = "off"
        test_crossfade_position = 0
        test_crossfade_samples = max(1, int(round(STREAM_TEST_MODE_CROSSFADE_SECONDS * IQ_SAMPLE_RATE)))

        def append_real_test_frames(batch: RtlSampleBatch) -> None:
            nonlocal channelizer
            nonlocal channelizer_key
            nonlocal channelizer_alias_filter_strength
            nonlocal channelizer_target_frequency_hz
            nonlocal source_generation
            nonlocal demodulator
            nonlocal startup_backlog_drained
            current_generation = getattr(self.fanout, "generation", source_generation)
            incoming_generation = batch_generation(batch)
            if incoming_generation < current_generation:
                return
            if incoming_generation != source_generation:
                source_generation = incoming_generation
                channelizer = None
                channelizer_key = None
                channelizer_alias_filter_strength = None
                channelizer_target_frequency_hz = None
                demodulator = NfmDemodulator()
                real_test_iq_frame_buffer.clear()
                real_test_iq_frames.clear()
                startup_backlog_drained = False
            with self.lock:
                station = self.stream["station"]
            target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
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
            next_channelizer_key = (
                batch.sample_rate,
                batch.center_frequency_hz,
            )
            if channelizer is None or channelizer_key != next_channelizer_key:
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
                demodulator = NfmDemodulator()
                real_test_iq_frame_buffer.clear()
                real_test_iq_frames.clear()
            elif channelizer_alias_filter_strength != alias_filter_strength:
                channelizer.update_alias_filter(
                    transition_hz=channel_transition_hz,
                    attenuation_db=channel_attenuation_db,
                )
                channelizer_alias_filter_strength = alias_filter_strength
            if channelizer is not None and channelizer_target_frequency_hz != target_frequency_hz:
                channelizer.set_target_frequency(target_frequency_hz)
                channelizer_target_frequency_hz = target_frequency_hz
                demodulator.reset()
                real_test_iq_frame_buffer.clear()
                real_test_iq_frames.clear()
            iq = iq_batch_complex(batch)
            channel_iq = channelizer.process_complex(iq)
            for real_iq_frame in real_test_iq_frame_buffer.push(channel_iq):
                real_test_iq_frames.append(real_iq_frame)

        def next_real_test_iq_frame() -> np.ndarray:
            while not real_test_iq_frames:
                try:
                    batch = self.queue.get_nowait()
                except queue.Empty:
                    return np.zeros(STREAM_FRAME_SAMPLES, dtype=np.complex64)
                batch = drain_queue_to_latest(self.queue, batch)
                append_real_test_frames(batch)
            return real_test_iq_frames.popleft()

        def test_crossfade_weights(count: int, source: SyntheticNwrTestModeSource) -> tuple[np.ndarray | float, np.ndarray | float, bool]:
            nonlocal test_crossfade_state
            nonlocal test_crossfade_position
            if test_crossfade_state == "enter":
                start = test_crossfade_position
                stop = min(test_crossfade_samples, start + count)
                ramp = np.linspace(
                    start / test_crossfade_samples,
                    stop / test_crossfade_samples,
                    count,
                    endpoint=False,
                    dtype=np.float32,
                )
                test_crossfade_position = stop
                if stop >= test_crossfade_samples:
                    test_crossfade_state = "active"
                return (1.0 - ramp), ramp, False
            if test_crossfade_state == "exit":
                start = test_crossfade_position
                stop = min(test_crossfade_samples, start + count)
                ramp = np.linspace(
                    start / test_crossfade_samples,
                    stop / test_crossfade_samples,
                    count,
                    endpoint=False,
                    dtype=np.float32,
                )
                test_crossfade_position = stop
                complete = stop >= test_crossfade_samples
                return ramp, (1.0 - ramp), complete
            if source.stop_ready:
                test_crossfade_state = "exit"
                test_crossfade_position = 0
                return test_crossfade_weights(count, source)
            return 0.0, 1.0, False
        while not self.stop_event.is_set():
            if not self._has_connected_outputs():
                try:
                    self.queue.get(timeout=0.5)
                except queue.Empty:
                    pass
                end_fallback()
                fallback_since = None
                frame_buffer.clear()
                idle_next_frame_at = None
                test_mode_next_frame_at = None
                continue
            with self.lock:
                test_mode_source = self.test_mode_source
                test_mode_station = self.stream.get("station", {})
            if test_mode_source is not None:
                if not test_mode_was_active:
                    test_demodulator.reset()
                    self.signal_meter.reset()
                    self.signal_presence.reset()
                    test_frame_buffer.clear()
                    real_test_iq_frame_buffer.clear()
                    real_test_iq_frames.clear()
                    end_fallback()
                    fallback_since = None
                    idle_next_frame_at = None
                    test_mode_next_frame_at = time.monotonic()
                    test_crossfade_state = "enter"
                    test_crossfade_position = 0
                    LOG.info("entered test mode for %s", test_mode_station.get("callsign"))
                test_mode_was_active = True
                now = time.monotonic()
                if test_mode_next_frame_at is None:
                    test_mode_next_frame_at = now
                delay = test_mode_next_frame_at - now
                if delay > 0 and self.stop_event.wait(delay):
                    break
                test_iq = test_mode_source.process(STREAM_FRAME_SAMPLES)
                real_iq = next_real_test_iq_frame()
                real_weight, test_weight, fade_complete = test_crossfade_weights(len(test_iq), test_mode_source)
                mixed_iq = mix_test_mode_iq_crossfade(real_iq, test_iq, real_weight, test_weight)
                self.signal_meter.process(mixed_iq)
                audio = test_demodulator.process(mixed_iq)
                test_mode_next_frame_at += STREAM_FRAME_SECONDS
                if test_mode_next_frame_at < time.monotonic() - 0.25:
                    test_mode_next_frame_at = time.monotonic() + STREAM_FRAME_SECONDS
                if len(audio) == 0:
                    continue
                last_real_audio = time.monotonic()
                end_fallback()
                with self.lock:
                    self.last_audio_at = time.time()
                    station = self.stream.get("station", {})
                for frame in test_frame_buffer.push(audio):
                    next_audio_config = self.audio_config()
                    if next_audio_config != audio_config:
                        audio_config = next_audio_config
                        changed_effects = effects.update_config(audio_config)
                        LOG.info(
                            "applied audio effects update for %s without reconnecting Icecast: %s",
                            station.get("callsign"),
                            ", ".join(changed_effects) or "none",
                        )
                    processed_frame = effects.process(frame)
                    self._write_pcm(float_to_s16(processed_frame), processed_frame)
                if fade_complete:
                    with self.lock:
                        if self.test_mode_source is test_mode_source:
                            self.test_mode_source = None
                continue
            if test_mode_was_active:
                test_mode_was_active = False
                test_mode_next_frame_at = None
                test_crossfade_state = "off"
                test_crossfade_position = 0
                test_demodulator.reset()
                self._configure_signal_meter(
                    alias_filter_transition_hz(CHANNEL_IQ_ALIAS_TRANSITION_HZ, self.alias_filter_strength_provider())
                )
                self.signal_presence.reset()
                fallback_since = None
                test_frame_buffer.clear()
                real_test_iq_frame_buffer.clear()
                real_test_iq_frames.clear()
                startup_backlog_drained = False
                LOG.info("left test mode for %s", self.stream.get("station", {}).get("callsign"))
            try:
                if idle_output_active:
                    now = time.monotonic()
                    if idle_next_frame_at is None:
                        idle_next_frame_at = now
                    queue_timeout = max(0.0, idle_next_frame_at - now)
                else:
                    queue_timeout = STREAM_IDLE_DETECTION_SECONDS
                batch: RtlSampleBatch = self.queue.get(
                    timeout=queue_timeout
                )
                if batch is QUEUE_WAKEUP:
                    continue
            except queue.Empty:
                idle_output_active = True
                now = time.monotonic()
                if idle_next_frame_at is None:
                    idle_next_frame_at = now
                frames_due = max(1, int((now - idle_next_frame_at) / STREAM_FRAME_SECONDS) + 1)
                frames_due = min(frames_due, 8)
                fallback_settings = self.fallback_settings_provider()
                fallback_action = self.fallback_action()
                if fallback_engaged and fallback_engaged_as != fallback_action.action:
                    # The action was changed while it was in effect.
                    self._end_fallback_action(fallback_engaged_as)
                    fallback_engaged = False
                    fallback_state.reset()
                if fallback_since is None:
                    fallback_since = last_real_audio
                idle_seconds = time.monotonic() - fallback_since
                if fallback_action.action == STREAM_FALLBACK_NONE:
                    fallback_state.reset()
                    idle_next_frame_at = None
                    continue
                for _ in range(frames_due):
                    if not fallback_engaged and idle_seconds < fallback_action.delay_seconds:
                        fallback_state.reset()
                        self._write_pcm(STREAM_SILENCE_FRAME, STREAM_SILENCE_FLOAT_FRAME)
                    else:
                        if not fallback_engaged:
                            fallback_engaged = True
                            fallback_engaged_as = fallback_action.action
                            self._begin_fallback_action(fallback_engaged_as, idle_seconds, "without IQ or a usable signal")
                        if fallback_engaged_as == STREAM_FALLBACK_PLAY_AUDIO:
                            fallback_pcm = next_fallback_frame(fallback_settings.loop_delay_seconds)
                            self._write_pcm(fallback_pcm, pcm_s16le_to_float32(fallback_pcm))
                        else:
                            self._write_pcm(STREAM_SILENCE_FRAME, STREAM_SILENCE_FLOAT_FRAME)
                    idle_next_frame_at += STREAM_FRAME_SECONDS
                if idle_next_frame_at < now - 0.25:
                    idle_next_frame_at = now + STREAM_FRAME_SECONDS
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
                self.signal_meter.reset()
                frame_buffer.clear()
                idle_next_frame_at = None
                LOG.info(
                    "stream DSP source generation changed for %s; reset channel state",
                    self.stream.get("station", {}).get("callsign"),
                )
            idle_output_active = False
            idle_next_frame_at = None
            with self.lock:
                station = self.stream["station"]
            target_frequency_hz = int(round(float(station["frequency"]) * 1_000_000))
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
            next_channelizer_key = (
                batch.sample_rate,
                batch.center_frequency_hz,
            )
            if channelizer is None or channelizer_key != next_channelizer_key:
                if getattr(batch, "prechannelized", False) and channelizer_key is not None:
                    # The remote feed moved to a new channel: its signal
                    # history no longer applies.
                    self.signal_meter.reset()
                    self.signal_presence.reset()
                    end_fallback()
                    fallback_since = None
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
                demodulator = NfmDemodulator()
                self._configure_signal_meter(channel_transition_hz)
                frame_buffer.clear()
            elif channelizer_alias_filter_strength != alias_filter_strength:
                channelizer.update_alias_filter(
                    transition_hz=channel_transition_hz,
                    attenuation_db=channel_attenuation_db,
                )
                channelizer_alias_filter_strength = alias_filter_strength
                self._configure_signal_meter(channel_transition_hz)
            if channelizer is not None:
                if channelizer_target_frequency_hz != target_frequency_hz:
                    channelizer.set_target_frequency(target_frequency_hz)
                    channelizer_target_frequency_hz = target_frequency_hz
                    demodulator.reset()
                    self.signal_meter.reset()
                    self.signal_presence.reset()
                    end_fallback()
                    fallback_since = None
            current_gain = self.gain_provider()
            if current_gain != signal_meter_gain:
                # RTL-SDR gain scales signal and noise together, so a step
                # change would otherwise leave the meter's noise-floor and
                # band-power smoothing mixing pre- and post-change samples
                # for several seconds, biasing the reported SNR.
                signal_meter_gain = current_gain
                self._configure_signal_meter(channel_transition_hz)
            iq = iq_batch_complex(batch)
            process_started_at = time.monotonic()
            channel_iq = channelizer.process_complex(iq)
            self.signal_meter.process(channel_iq)
            audio = demodulator.process(channel_iq)
            process_seconds = time.monotonic() - process_started_at
            batch_seconds = float(iq.size) / float(max(1, batch.sample_rate))
            if process_seconds > batch_seconds and process_started_at - last_slow_batch_log_at >= 60.0:
                last_slow_batch_log_at = process_started_at
                LOG.warning(
                    "stream DSP is slower than realtime for %s: processed %.3fs IQ in %.3fs",
                    station.get("callsign"),
                    batch_seconds,
                    process_seconds,
                )
            if len(audio) == 0:
                continue
            now = time.monotonic()
            last_real_audio = now
            fallback_settings = self.fallback_settings_provider()
            fallback_action = self.fallback_action()
            if fallback_engaged and fallback_engaged_as != fallback_action.action:
                self._end_fallback_action(fallback_engaged_as)
                fallback_engaged = False
                fallback_state.reset()
            if fallback_action.action != STREAM_FALLBACK_NONE and self.signal_presence.lost:
                # Keep demodulating so the meter sees the signal return, but after
                # the fallback delay take the stream's fallback action.
                if fallback_since is None:
                    fallback_since = now
                if not fallback_engaged and now - fallback_since >= fallback_action.delay_seconds:
                    fallback_engaged = True
                    fallback_engaged_as = fallback_action.action
                    self._begin_fallback_action(fallback_engaged_as, now - fallback_since, "without a usable signal")
            else:
                if fallback_engaged:
                    self._end_fallback_action(fallback_engaged_as)
                    fallback_engaged = False
                fallback_state.reset()
                fallback_since = None
            with self.lock:
                self.last_audio_at = time.time()
            for frame in frame_buffer.push(audio):
                next_audio_config = self.audio_config()
                if next_audio_config != audio_config:
                    audio_config = next_audio_config
                    changed_effects = effects.update_config(audio_config)
                    LOG.info(
                        "applied audio effects update for %s without reconnecting Icecast: %s",
                        station.get("callsign"),
                        ", ".join(changed_effects) or "none",
                    )
                processed_frame = effects.process(frame)
                if fallback_engaged and fallback_engaged_as == STREAM_FALLBACK_PLAY_AUDIO:
                    fallback_pcm = next_fallback_frame(fallback_settings.loop_delay_seconds)
                    self._write_pcm(fallback_pcm, pcm_s16le_to_float32(fallback_pcm))
                else:
                    self._write_pcm(float_to_s16(processed_frame), processed_frame)

    def encoder_group_for(self, config: IcecastConfig) -> "IcecastEncoderGroup":
        key = icecast_encoder_key(config)
        with self.lock:
            encoder_group = self.encoder_groups.get(key)
            if encoder_group is not None:
                return encoder_group
            encoder_group = IcecastEncoderGroup(
                key,
                config,
                input_sample_rate=IQ_SAMPLE_RATE,
            )
            self.encoder_groups[key] = encoder_group
            encoder_group.start()
            LOG.info(
                "created shared Icecast encoder group %s for %s",
                key,
                self.stream.get("station", {}).get("callsign", "unknown"),
            )
            return encoder_group

    def _write_pcm(self, pcm: bytes, float_samples: np.ndarray | None = None) -> None:
        for encoder_group in list(self.encoder_groups.values()):
            if encoder_group.has_outputs():
                queue_latest(encoder_group.pcm_queue, pcm)
        with self.lock:
            monitor_sources = list(self.monitor_sources.values())
            soundcard_output_ids = list(getattr(self, "soundcard_outputs", set()))
            soundcard_taps = list(getattr(self, "soundcard_taps", {}).values())
            eas_recorder = self.eas_recorder
        for source in monitor_sources:
            source.push_pcm(pcm)
        if float_samples is not None and self.soundcard_manager is not None:
            for output_id in soundcard_output_ids:
                try:
                    self.soundcard_manager.push_float(output_id, float_samples, owner=self._soundcard_owner)
                except Exception as exc:
                    LOG.warning(
                        "soundcard output failed for %s: %s",
                        self.stream.get("station", {}).get("callsign"),
                        exc,
                    )
        for tap in soundcard_taps:
            try:
                if float_samples is not None:
                    push_float = getattr(tap, "push_float", None)
                    if push_float is not None:
                        push_float(float_samples)
                        continue
                push = getattr(tap, "push_pcm", None)
                if push is not None:
                    push(pcm)
            except Exception as exc:
                LOG.warning(
                    "soundcard tap failed for %s: %s",
                    self.stream.get("station", {}).get("callsign"),
                    exc,
                )
        if eas_recorder is not None:
            recorder_config = getattr(eas_recorder, "config", None)
            if recorder_config is not None and self.storage_monitor.is_critical(Path(recorder_config.directory)):
                self.stop_eas_recording_due_to_storage(storage_status_message("critical"))
                return
            try:
                eas_recorder.write(pcm)
            except Exception as exc:
                self._set_eas_status("needs-attention", str(exc))
                LOG.warning(
                    "EAS recorder failed for %s: %s",
                    self.stream.get("station", {}).get("callsign"),
                    exc,
                )
                self._stop_eas_recorder()

    def _has_connected_outputs(self) -> bool:
        return (
            any(encoder_group.has_outputs() for encoder_group in list(self.encoder_groups.values()))
            # Icecast outputs held off by the fallback action still count, or the
            # stream would stop listening for the station to come back.
            or (self.icecast_disconnected.is_set() and bool(self.outputs))
            or self.has_monitor_sources()
            or self.has_soundcard_taps()
            or self.has_eas_recorder()
        )

    def _stop_unused_encoder_groups(self) -> None:
        for key, encoder_group in list(self.encoder_groups.items()):
            if encoder_group.has_outputs():
                continue
            self.encoder_groups.pop(key, None)
            encoder_group.stop()

    def has_eas_recorder(self) -> bool:
        with self.lock:
            return self.eas_recorder is not None

    def stop_eas_recording_due_to_storage(self, message: str) -> None:
        with self.lock:
            has_recorder = self.eas_recorder is not None
        if not has_recorder:
            return
        LOG.warning(
            "stopping EAS recorder for %s because storage is critically low",
            self.stream.get("station", {}).get("callsign"),
        )
        self._stop_eas_recorder()
        with self.lock:
            if self.eas_config is not None:
                self.eas_status = "needs-attention"
                self.eas_error = message

    def _sync_eas_recorder(self, stream: dict[str, Any]) -> None:
        settings = eas_recording_settings_from_stream(stream)
        next_config = (
            eas_config_from_stream(stream, self.state_directory, settings)
            if settings.enabled
            else None
        )
        with self.lock:
            current_config = self.eas_config
            current_recorder = self.eas_recorder
        if next_config == current_config:
            if next_config is None:
                return
            if current_recorder is not None:
                return
            if self.storage_monitor.is_critical(Path(next_config.directory)):
                with self.lock:
                    self.eas_status = "needs-attention"
                    self.eas_error = storage_status_message("critical")
                return
        self._stop_eas_recorder()
        if next_config is None:
            with self.lock:
                self.eas_config = None
                self.eas_status = "disabled"
                self.eas_error = None
            return
        self.storage_monitor.add_path(Path(next_config.directory))
        if self.storage_monitor.is_critical(Path(next_config.directory)):
            message = storage_status_message("critical")
            LOG.warning(
                "EAS recorder not started for %s because storage is critically low",
                stream.get("station", {}).get("callsign"),
            )
            with self.lock:
                self.eas_config = next_config
                self.eas_recorder = None
                self.eas_status = "needs-attention"
                self.eas_error = message
            return
        try:
            recorder = EasRecorderOutput(next_config, input_sample_rate=IQ_SAMPLE_RATE)
        except Exception as exc:
            LOG.warning(
                "EAS recorder failed for %s: %s",
                stream.get("station", {}).get("callsign"),
                exc,
            )
            with self.lock:
                self.eas_config = next_config
                self.eas_recorder = None
                self.eas_status = "needs-attention"
                self.eas_error = str(exc)
            return
        with self.lock:
            self.eas_config = next_config
            self.eas_recorder = recorder
            self.eas_status = "enabled"
            self.eas_error = None
        self.storage_monitor.recording_started(Path(next_config.directory))
        LOG.info(
            "started EAS recorder for %s in %s",
            stream.get("station", {}).get("callsign", "unknown"),
            next_config.directory,
        )

    def _stop_eas_recorder(self) -> None:
        with self.lock:
            recorder = self.eas_recorder
            self.eas_recorder = None
            if self.eas_config is not None:
                self.eas_status = "disabled"
        if recorder is not None:
            try:
                recorder.close()
            except Exception as exc:
                LOG.debug("EAS recorder close failed: %s", exc)
            self.storage_monitor.recording_stopped()

    def _set_eas_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.eas_status = status
            self.eas_error = error


class IcecastEncoderGroup:
    def __init__(
        self,
        key: tuple[str, int, int],
        config: IcecastConfig,
        *,
        input_sample_rate: int = IQ_SAMPLE_RATE,
    ) -> None:
        self.key = key
        self.config = config
        self.input_sample_rate = int(input_sample_rate)
        self.encoder = create_audio_encoder(config, input_sample_rate=self.input_sample_rate)
        self.pcm_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self.outputs: list[IcecastOutputWriter] = []
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.error: Exception | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"icecast-encoder-{config.format}-{config.sample_rate}-{config.bitrate}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def request_stop(self) -> None:
        self.stop_event.set()
        wake_queue(self.pcm_queue)

    def stop(self) -> None:
        self.request_stop()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        self.encoder.close()

    def add_output(self, output: "IcecastOutputWriter") -> None:
        with self.lock:
            self.outputs.append(output)

    def remove_output(self, output: "IcecastOutputWriter") -> None:
        with self.lock:
            if output in self.outputs:
                self.outputs.remove(output)

    def has_outputs(self) -> bool:
        with self.lock:
            return bool(self.outputs)

    def header(self) -> bytes:
        return bytes(getattr(self.encoder, "header", b""))

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                try:
                    pcm = self.pcm_queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                if pcm is QUEUE_WAKEUP:
                    continue
                encoded = self.encoder.encode(pcm)
                if encoded:
                    self._broadcast(encoded)
            encoded = self.encoder.flush()
            if encoded:
                self._broadcast(encoded)
        except Exception as exc:
            self.error = exc
            LOG.warning("shared Icecast encoder %s failed: %s", self.key, exc)

    def _broadcast(self, encoded: bytes) -> None:
        with self.lock:
            outputs = list(self.outputs)
        for output in outputs:
            queue_latest(output.encoded_queue, encoded)


class IcecastOutputWriter:
    def __init__(self, runtime: IcecastStreamWorker, output: dict[str, Any]) -> None:
        self.runtime = runtime
        self.output = output
        self.signature = icecast_output_signature(output)
        self.encoded_queue: queue.Queue[bytes] = queue.Queue(maxsize=64)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(
            target=self._run,
            name=f"icecast-writer-{runtime.id}-{output.get('id', '')}",
            daemon=True,
        )
        self.status = "disabled"
        self.error: str | None = None
        self.started_at: float | None = None
        self.lock = threading.Lock()
        self.connection_lock = threading.Lock()
        self.current_source: IcecastSource | None = None
        self.current_sink = None

    def start(self) -> None:
        self.thread.start()

    def request_stop(self) -> None:
        self.stop_event.set()
        self._close_current_connection()
        wake_queue(self.encoded_queue)

    def stop(self) -> None:
        self.request_stop()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        self._set_status("disabled")

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            status = self.status
            error = self.error
            started_at = self.started_at
        with self.runtime.lock:
            last_audio_at = self.runtime.last_audio_at
        return {
            "id": self.runtime.id,
            "station": self.runtime.stream["station"],
            "outputs": [self.output],
            "status": status,
            "error": error,
            "started_at": started_at,
            "last_audio_at": last_audio_at,
            "raw_queue": self.runtime.raw_queue_stats(),
        }

    def _set_current_connection(self, source, sink) -> None:
        with self.connection_lock:
            self.current_source = source
            self.current_sink = sink

    def _close_current_connection(self) -> None:
        with self.connection_lock:
            source = self.current_source
            sink = self.current_sink
            self.current_source = None
            self.current_sink = None
        if sink is not None:
            try:
                sink.close()
            except Exception:
                pass
        if source is not None:
            try:
                source.close()
            except Exception:
                pass

    def _set_status(self, status: str, error: str | None = None) -> None:
        with self.lock:
            self.status = status
            self.error = error
            if status == "enabled" and self.started_at is None:
                self.started_at = time.time()

    def _run(self) -> None:
        disconnected = getattr(self.runtime, "icecast_disconnected", None)
        while not self.stop_event.is_set():
            if disconnected is not None and disconnected.is_set():
                # The stream's fallback action: stay off the Icecast server until
                # the station is back, rather than retrying as after a failure.
                self._set_status("standby")
                self.stop_event.wait(0.5)
                continue
            source = None
            sink = None
            encoder_group = None
            try:
                config = icecast_config_from_output(self.output)
                source = IcecastSource(config, config.content_type)
                sink = source.connect()
                self._set_current_connection(source, sink)
                encoder_group = self.runtime.encoder_group_for(config)
                header = encoder_group.header()
                if header:
                    sink.write(header)
                encoder_group.add_output(self)
                self._set_status("enabled")
                while not self.stop_event.is_set():
                    if disconnected is not None and disconnected.is_set():
                        LOG.info("disconnected Icecast output %s until station audio returns", self.output.get("id"))
                        break
                    try:
                        encoded = self.encoded_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if encoded is QUEUE_WAKEUP:
                        continue
                    if encoded:
                        sink.write(encoded)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self._set_status("needs-attention", friendly_stream_error(exc))
                LOG.warning("Icecast output failed for %s: %s", self.output.get("id"), exc)
                self.stop_event.wait(STREAM_RECONNECT_SECONDS)
            finally:
                if encoder_group is not None:
                    encoder_group.remove_output(self)
                    self.runtime._stop_unused_encoder_groups()
                self._close_current_connection()
                if sink is not None:
                    try:
                        sink.close()
                    except Exception:
                        pass
                if source is not None:
                    try:
                        source.close()
                    except Exception:
                        pass
        self._set_status("disabled")


def channel_frequency_for_batch(batch, wanted_hz: int) -> int:
    """The frequency to channelize a batch at.

    A remote feed arrives already channelized at the frequency it names. After
    a retune it keeps delivering the old channel until the new one arrives, so
    the batch, not the wanted frequency, says what to pass through.
    """
    if getattr(batch, "prechannelized", False):
        return int(batch.center_frequency_hz)
    return int(wanted_hz)


def queue_latest(target: queue.Queue, item: Any) -> None:
    try:
        target.put_nowait(item)
    except queue.Full:
        try:
            target.get_nowait()
        except queue.Empty:
            pass
        try:
            target.put_nowait(item)
        except queue.Full:
            pass
