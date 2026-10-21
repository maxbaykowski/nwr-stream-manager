from __future__ import annotations

import json
import tempfile
import threading
import time
import types
import sys
import unittest
from pathlib import Path
from unittest import mock

from test_auth import load_web_control_module
import test_signal_meter


def wait_for(predicate, timeout: float = 5.0) -> None:
    test_signal_meter.StreamGainChangeSignalMeterTests._wait_for(predicate, timeout=timeout)


class StreamFallbackActionSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def migrated(self, legacy: dict | None, streams: list[dict]) -> tuple[list[dict], list[dict]]:
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        service = object.__new__(wc.RtlControlService)
        service.fallback_state_path = root / "fallback.json"
        if legacy is not None:
            service.fallback_state_path.write_text(json.dumps(legacy), encoding="utf-8")
        service.streams_directory = root / "streams"
        service.streams = streams
        service._migrate_stream_fallback_actions()
        saved = wc.load_stream_configs(service.streams_directory)
        return service.streams, saved

    def test_existing_streams_keep_doing_what_the_shared_setting_did(self) -> None:
        for legacy, expected in (
            ({"enabled": True, "silence_timeout_seconds": 45, "loop_delay_seconds": 5}, {"action": "play_fallback_audio", "delay_seconds": 45}),
            ({"enabled": False, "silence_timeout_seconds": 60, "loop_delay_seconds": 5}, {"action": "none", "delay_seconds": 60}),
            (None, {"action": "play_fallback_audio", "delay_seconds": 30.0}),  # never changed: fallback was on
        ):
            with self.subTest(legacy=legacy):
                streams, saved = self.migrated(legacy, [{"id": "a", "station": {"callsign": "WXN99"}}])
                self.assertEqual(streams[0]["fallback_action"], expected)
                self.assertEqual(saved[0]["fallback_action"], expected)

    def test_a_stream_that_already_has_a_fallback_action_keeps_it(self) -> None:
        own = {"action": "disconnect_icecast", "delay_seconds": 90}
        streams, _saved = self.migrated(
            {"enabled": True, "silence_timeout_seconds": 45},
            [{"id": "a", "station": {"callsign": "WXN99"}, "fallback_action": dict(own)}],
        )

        self.assertEqual(streams[0]["fallback_action"], own)

    def test_shared_fallback_audio_settings_no_longer_include_the_old_per_stream_ones(self) -> None:
        settings = self.web_control.validate_fallback_settings_payload(
            {"enabled": False, "silence_timeout_seconds": 60, "loop_delay_seconds": 2}
        )

        self.assertEqual(
            self.web_control.asdict(settings),
            {"loop_delay_seconds": 2, "audio": "default", "custom_audio_sha256": ""},
        )

    def test_fallback_action_is_checked(self) -> None:
        validate = self.web_control.validate_stream_fallback_action_payload
        for action in ("none", "disconnect_icecast", "play_fallback_audio"):
            with self.subTest(action=action):
                self.assertEqual(validate({"action": action, "delay_seconds": 120}).action, action)
        with self.assertRaisesRegex(ValueError, "Choose what the stream should do"):
            validate({"action": "explode", "delay_seconds": 30})
        for delay in (29, 121):
            with self.subTest(delay=delay):
                with self.assertRaisesRegex(ValueError, "from 30 through 120 seconds"):
                    validate({"action": "disconnect_icecast", "delay_seconds": delay})

    def test_new_streams_play_fallback_audio_after_30_seconds(self) -> None:
        self.assertEqual(
            self.web_control.asdict(self.web_control.WebStreamFallbackAction()),
            {"action": "play_fallback_audio", "delay_seconds": 30.0},
        )

    def test_read_only_accounts_cannot_change_a_fallback_action(self) -> None:
        handler = object.__new__(self.web_control.RtlControlHandler)

        self.assertFalse(handler._read_only_request_allowed("/api/stream-fallback-action", "PATCH"))


class StreamFallbackActionWorkerTests(unittest.TestCase):
    """The stream worker carries out its stream's fallback action."""

    FALLBACK_SAMPLE = test_signal_meter.StreamNoSignalFallbackTests.FALLBACK_SAMPLE
    setUpClass = test_signal_meter.StreamNoSignalFallbackTests.__dict__["setUpClass"]
    setUp = test_signal_meter.StreamNoSignalFallbackTests.setUp
    push = test_signal_meter.StreamNoSignalFallbackTests.push
    kinds_since = test_signal_meter.StreamNoSignalFallbackTests.kinds_since
    mark = test_signal_meter.StreamNoSignalFallbackTests.mark

    CUSTOM_SAMPLE = 4321

    def start_worker(self, action: str, delay_seconds: float, fallback_audio_provider=None):
        wc = self.web_control
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)

        class Fanout:
            def __init__(self) -> None:
                self.queue = wc.queue.Queue(maxsize=4096)

            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return self.queue

            def unsubscribe(self, subscriber) -> None:
                pass

        fanout = Fanout()
        worker = wc.IcecastStreamWorker(
            stream={
                "id": "stream-1",
                "station": {"callsign": "WXN99", "frequency": "162.475"},
                "outputs": [],
                "fallback_action": {"action": action, "delay_seconds": delay_seconds},
            },
            fanout=fanout,
            fallback_settings_provider=lambda: wc.WebFallbackSettings(loop_delay_seconds=0.0),
            fallback_audio_provider=fallback_audio_provider,
            alias_filter_strength_provider=lambda: wc.ALIAS_FILTER_STRENGTH_DEFAULT,
            state_directory=Path(tempdir.name),
        )
        # Someone listening keeps the stream producing audio.
        worker.monitor_sources["listener"] = types.SimpleNamespace(push_pcm=lambda pcm: None, close=lambda: None)
        self.frames = []
        self.frames_lock = wc.threading.Lock()

        def capture(pcm, float_samples=None):
            if pcm[:2] == wc.np.int16(self.FALLBACK_SAMPLE).tobytes():
                kind = "fallback"
            elif pcm[:2] == wc.np.int16(self.CUSTOM_SAMPLE).tobytes():
                kind = "custom"
            else:
                kind = "silence" if not any(pcm) else "nwr"
            with self.frames_lock:
                self.frames.append((time.monotonic(), kind))

        worker._write_pcm = capture
        worker.start()
        self.addCleanup(worker.stop)
        return worker, fanout

    def set_action(self, worker, action: str, delay_seconds: float = 0.3) -> None:
        stream = dict(worker.stream)
        stream["fallback_action"] = {"action": action, "delay_seconds": delay_seconds}
        with worker.lock:
            worker.stream = stream

    def test_without_radio_audio_icecast_outputs_disconnect_after_the_delay(self) -> None:
        worker, _fanout = self.start_worker("disconnect_icecast", 0.5)

        time.sleep(0.3)
        self.assertFalse(worker.icecast_disconnected.is_set())
        wait_for(worker.icecast_disconnected.is_set)
        start = self.mark()
        time.sleep(0.3)
        # Sound cards and listeners get silence, not the fallback announcement.
        self.assertEqual(set(self.kinds_since(start)), {"silence"})

    def test_station_audio_returning_reconnects_icecast_outputs(self) -> None:
        worker, fanout = self.start_worker("disconnect_icecast", 0.3)
        wait_for(worker.icecast_disconnected.is_set)

        self.push(fanout, 25.0, 1.0)

        self.assertFalse(worker.icecast_disconnected.is_set())
        self.assertEqual(self.kinds_since(self.mark() - 5)[-1], "nwr")

    def test_losing_the_signal_disconnects_and_its_return_reconnects(self) -> None:
        worker, fanout = self.start_worker("disconnect_icecast", 0.5)
        self.push(fanout, 25.0, 1.0)
        self.push(fanout, None, 8.0)  # the meter needs a few seconds to call it lost
        self.assertTrue(worker.signal_presence.lost)

        self.push(fanout, None, 1.0, pace=True)  # lost for longer than the delay
        self.assertTrue(worker.icecast_disconnected.is_set())

        self.push(fanout, 20.0, 4.0)  # back for longer than the restore period
        self.assertFalse(worker.icecast_disconnected.is_set())

    def test_changing_the_action_while_it_is_in_effect_undoes_it(self) -> None:
        worker, _fanout = self.start_worker("disconnect_icecast", 0.3)
        wait_for(worker.icecast_disconnected.is_set)

        self.set_action(worker, "play_fallback_audio")
        wait_for(lambda: not worker.icecast_disconnected.is_set())
        start = self.mark()
        wait_for(lambda: "fallback" in self.kinds_since(start))

        self.set_action(worker, "none")
        time.sleep(0.3)
        start = self.mark()
        time.sleep(0.3)
        self.assertNotIn("fallback", self.kinds_since(start))
        self.assertFalse(worker.icecast_disconnected.is_set())

    def test_do_nothing_never_disconnects_or_plays_fallback(self) -> None:
        worker, _fanout = self.start_worker("none", 0.2)

        time.sleep(0.8)

        self.assertFalse(worker.icecast_disconnected.is_set())
        self.assertNotIn("fallback", self.kinds_since(0))

    def test_choosing_another_announcement_switches_to_it_while_it_plays(self) -> None:
        wc = self.web_control

        def announcement(sample: int):
            return wc.FallbackAudio(
                sample_rate=wc.IQ_SAMPLE_RATE,
                pcm=wc.np.full(wc.IQ_SAMPLE_RATE * 5, sample, dtype="<i2").tobytes(),
                duration_seconds=5.0,
                source="test",
            )

        default, custom = announcement(self.FALLBACK_SAMPLE), announcement(self.CUSTOM_SAMPLE)
        chosen = [default]
        self.start_worker("play_fallback_audio", 0.3, fallback_audio_provider=lambda: chosen[0])
        start = self.mark()
        wait_for(lambda: "fallback" in self.kinds_since(start))

        chosen[0] = custom
        start = self.mark()
        wait_for(lambda: "custom" in self.kinds_since(start))
        time.sleep(0.2)
        self.assertEqual(set(self.kinds_since(self.mark() - 3)), {"custom"})

    def test_icecast_outputs_held_off_still_keep_the_stream_listening(self) -> None:
        wc = self.web_control
        worker = object.__new__(wc.IcecastStreamWorker)
        worker.lock = threading.Lock()
        worker.encoder_groups = {}
        worker.monitor_sources = {}
        worker.soundcard_taps = {}
        worker.eas_recorder = None
        worker.outputs = {"icecast-1": object()}
        worker.icecast_disconnected = threading.Event()

        self.assertFalse(worker._has_connected_outputs())
        worker.icecast_disconnected.set()
        # Otherwise it would stop reading the radio and never see the station return.
        self.assertTrue(worker._has_connected_outputs())


class IcecastOutputHeldOffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_a_disconnected_output_stays_off_the_server_until_released(self) -> None:
        wc = self.web_control
        connections = []

        class Sink:
            def __init__(self) -> None:
                self.closed = False

            def write(self, data) -> None:
                pass

            def close(self) -> None:
                self.closed = True

        class Source:
            def __init__(self, config, content_type) -> None:
                pass

            def connect(self):
                sink = Sink()
                connections.append(sink)
                return sink

            def close(self) -> None:
                pass

        encoder_group = types.SimpleNamespace(header=lambda: b"", add_output=lambda output: None, remove_output=lambda output: None)
        runtime = types.SimpleNamespace(
            id="stream-1",
            lock=threading.Lock(),
            icecast_disconnected=threading.Event(),
            encoder_group_for=lambda config: encoder_group,
            _stop_unused_encoder_groups=lambda: None,
        )
        output = {
            "id": "icecast-1",
            "type": "icecast",
            "enabled": True,
            "icecast": {
                "host": "icecast.example.com",
                "port": 8000,
                "mount": "/wxn99",
                "username": "source",
                "password": "secret",
                "format": "mp3",
                "sample_rate": 24000,
                "bitrate": 64,
            },
        }
        # The output writer opens its Icecast connection where it lives.
        with mock.patch.object(sys.modules[wc.IcecastOutputWriter.__module__], "IcecastSource", Source):
            writer = wc.IcecastOutputWriter(runtime, output)
            writer.start()
            self.addCleanup(writer.stop)
            wait_for(lambda: writer.status == "enabled")

            runtime.icecast_disconnected.set()
            wait_for(lambda: writer.status == "standby")
            self.assertTrue(connections[0].closed)
            time.sleep(1.2)
            self.assertEqual(len(connections), 1)  # no retrying while held off

            runtime.icecast_disconnected.clear()
            wait_for(lambda: writer.status == "enabled" and len(connections) == 2)


if __name__ == "__main__":
    unittest.main()
