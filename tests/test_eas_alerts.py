from __future__ import annotations

import importlib.util
import importlib
import json
import sys
import tempfile
import time
import types
import unittest
import zipfile
from pathlib import Path
from datetime import datetime, timedelta, timezone

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_web_control_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    spec = importlib.util.spec_from_file_location(
        "nwr_stream_manager.web_control",
        PACKAGE_PATH / "web_control.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.web_control"] = module
    spec.loader.exec_module(module)
    return module


class EasAlertTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()
        cls.config = importlib.import_module("nwr_stream_manager.config")
        cls.audio_effects = importlib.import_module("nwr_stream_manager.audio_effects")

    def test_alert_summary_uses_same_event_lookup(self) -> None:
        alert = {
            "event_type": "SVR",
            "start_time_utc": "2026-08-09T19:07:00Z",
            "file_path": "/tmp/alert.wav",
        }
        summary = self.web_control.eas_alert_summary({}, alert, 0)
        self.assertEqual(summary["event_name"], "Severe Thunderstorm Warning")
        self.assertIn("Severe thunderstorm warning issued August 9, 2026 at", summary["summary"])

    def test_custom_icecast_rejects_known_service_host(self) -> None:
        with self.assertRaisesRegex(ValueError, "Please select GWES Weather Radio"):
            self.web_control.validate_icecast_payload(
                {
                    "service": "custom",
                    "host": "ingest.wxr.gwes-cdn.net",
                    "port": 10000,
                    "username": "user",
                    "password": "secret",
                    "mount": "/WXN99.mp3",
                    "format": "mp3",
                    "sample_rate": 22050,
                    "bitrate": 64,
                }
            )

    def test_raw_rtl_fanout_reports_subscriber_drops(self) -> None:
        web_control = self.web_control

        class Source:
            config = web_control.RtlConfig(serial="dummy", sample_rate=1_024_000)

            def __init__(self) -> None:
                self.items = web_control.queue.Queue()
                self.items.put(web_control.RtlSampleBatch(data=b"aa", sample_rate=1_024_000, center_frequency_hz=162_475_000))
                self.items.put(web_control.RtlSampleBatch(data=b"bb", sample_rate=1_024_000, center_frequency_hz=162_475_000))
                self.items.put(None)

            @staticmethod
            def _rtl_async_buffer_size(config):
                return web_control.RtlCaptureSource._rtl_async_buffer_size(config)

            def read(self, timeout=None):
                item = self.items.get(timeout=timeout)
                if item is None:
                    raise EOFError
                return item

        fanout = web_control.RawRtlFanout(Source())
        subscriber = fanout.subscribe(max_chunks=1, name="test-subscriber")
        fanout.start()
        fanout.thread.join(timeout=2.0)

        stats = fanout.stats()
        subscriber_stats = fanout.subscriber_stats(subscriber)
        self.assertEqual(stats["read_batches"], 2)
        self.assertEqual(stats["total_dropped_batches"], 1)
        self.assertEqual(subscriber_stats["name"], "test-subscriber")
        self.assertEqual(subscriber_stats["dropped_batches"], 1)
        self.assertEqual(subscriber.get_nowait().data, b"bb")

    def test_raw_rtl_fanout_does_not_retain_unsubscribed_queue_stats(self) -> None:
        web_control = self.web_control

        class Source:
            config = web_control.RtlConfig(serial="dummy", sample_rate=1_024_000)

            @staticmethod
            def _rtl_async_buffer_size(config):
                return web_control.RtlCaptureSource._rtl_async_buffer_size(config)

        fanout = web_control.RawRtlFanout(Source())
        subscriber = fanout.subscribe(max_chunks=1, name="stale-subscriber")
        fanout.unsubscribe(subscriber)
        batch = web_control.RtlSampleBatch(
            data=b"aa",
            sample_rate=1_024_000,
            center_frequency_hz=162_475_000,
        )

        fanout._record_subscriber_depth(subscriber)
        fanout._record_subscriber_drop(subscriber, batch)

        self.assertEqual(fanout.stats()["subscriber_count"], 0)
        self.assertEqual(fanout.stats()["total_dropped_batches"], 0)
        self.assertEqual(fanout.subscriber_stats(subscriber)["name"], "subscriber")

    def test_json_request_body_has_size_limit(self) -> None:
        web_control = self.web_control
        handler = object.__new__(web_control.RtlControlHandler)
        handler.headers = {"Content-Length": str(web_control.MAX_JSON_REQUEST_BYTES + 1)}
        handler.rfile = None

        with self.assertRaisesRegex(ValueError, "too large"):
            handler._read_json()

    def test_rtl_settings_force_fixed_sample_rate_from_saved_state(self) -> None:
        web_control = self.web_control
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "rtl-control.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "serial": "12345678",
                        "sample_rate": 1_024_000,
                        "gain": None,
                        "ppm_correction": 0,
                        "bias_tee": False,
                    }
                ),
                encoding="utf-8",
            )

            settings = web_control.load_settings(settings_path)

        self.assertEqual(settings.sample_rate, web_control.DEFAULT_RTL_SAMPLE_RATE)
        self.assertEqual(settings.to_rtl_config().sample_rate, web_control.DEFAULT_RTL_SAMPLE_RATE)
        self.assertEqual(settings.alias_filter_strength, web_control.ALIAS_FILTER_STRENGTH_DEFAULT)

    def test_rtl_settings_reject_sample_rate_update_payloads(self) -> None:
        web_control = self.web_control
        service = object.__new__(web_control.RtlControlService)
        service.settings = web_control.RtlControlSettings(serial="12345678")

        with self.assertRaisesRegex(ValueError, "sample rate is fixed"):
            service._merged_settings({"sample_rate": 1_024_000})

    def test_rtl_settings_validate_alias_filter_strength_update_payloads(self) -> None:
        web_control = self.web_control
        service = object.__new__(web_control.RtlControlService)
        service.settings = web_control.RtlControlSettings(serial="12345678")

        settings = service._merged_settings({"alias_filter_strength": 50})

        self.assertEqual(settings.alias_filter_strength, 50)
        settings = service._merged_settings({"alias_filter_strength": 0})
        self.assertEqual(settings.alias_filter_strength, 0)
        with self.assertRaisesRegex(ValueError, "alias_filter_strength"):
            service._merged_settings({"alias_filter_strength": -1})

    def test_rtl_settings_persist_alias_filter_strength(self) -> None:
        web_control = self.web_control
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "rtl-control.json"
            web_control.save_settings(
                settings_path,
                web_control.RtlControlSettings(serial="12345678", alias_filter_strength=50),
            )

            settings = web_control.load_settings(settings_path)

        self.assertEqual(settings.alias_filter_strength, 50)

    def test_alias_filter_strength_scales_all_transition_widths_from_current_default(self) -> None:
        web_control = self.web_control

        self.assertEqual(
            web_control.alias_filter_transition_hz(web_control.INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ, 100),
            web_control.INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
        )
        self.assertEqual(
            web_control.alias_filter_transition_hz(web_control.CHANNEL_IQ_ALIAS_TRANSITION_HZ, 50),
            web_control.CHANNEL_IQ_ALIAS_TRANSITION_HZ * 2,
        )
        self.assertEqual(
            web_control.alias_filter_transition_hz(web_control.CHANNEL_IQ_ALIAS_TRANSITION_HZ, 0),
            web_control.CHANNEL_IQ_ALIAS_TRANSITION_HZ * 4,
        )

    def test_storage_monitor_reports_decimal_used_and_total_storage(self) -> None:
        web_control = self.web_control

        def fake_stat(_path):
            return types.SimpleNamespace(st_dev=42)

        def fake_statvfs(_path):
            return types.SimpleNamespace(
                f_frsize=1000,
                f_bsize=1000,
                f_blocks=1_200_000_000,
                f_bfree=950_500_000,
                f_bavail=950_500_000,
                f_favail=50_000,
            )

        monitor = web_control.StorageMonitor(
            [Path("/")],
            stat_provider=fake_stat,
            statvfs_provider=fake_statvfs,
        )

        snapshot = monitor.refresh()
        filesystem = snapshot["filesystems"][0]

        self.assertEqual(snapshot["status"], "ok")
        self.assertEqual(filesystem["used_bytes"], 249_500_000_000)
        self.assertEqual(filesystem["total_bytes"], 1_200_000_000_000)
        self.assertEqual(filesystem["summary"], "Storage: 249.5 GB used of 1.2 TB (21%)")

    def test_storage_monitor_deduplicates_paths_on_same_filesystem(self) -> None:
        web_control = self.web_control

        def fake_stat(_path):
            return types.SimpleNamespace(st_dev=7)

        def fake_statvfs(_path):
            return types.SimpleNamespace(
                f_frsize=4096,
                f_bsize=4096,
                f_blocks=100_000,
                f_bfree=50_000,
                f_bavail=50_000,
                f_favail=20_000,
            )

        monitor = web_control.StorageMonitor(
            [Path("/tmp"), Path("/tmp/nwr-stream-manager")],
            stat_provider=fake_stat,
            statvfs_provider=fake_statvfs,
        )

        snapshot = monitor.refresh()

        self.assertEqual(len(snapshot["filesystems"]), 1)

    def test_storage_monitor_marks_critical_when_available_space_is_too_low(self) -> None:
        web_control = self.web_control

        def fake_stat(_path):
            return types.SimpleNamespace(st_dev=9)

        def fake_statvfs(_path):
            return types.SimpleNamespace(
                f_frsize=1000,
                f_bsize=1000,
                f_blocks=10_000_000,
                f_bfree=100_000,
                f_bavail=100_000,
                f_favail=50_000,
            )

        monitor = web_control.StorageMonitor(
            [Path("/")],
            stat_provider=fake_stat,
            statvfs_provider=fake_statvfs,
        )

        snapshot = monitor.refresh()

        self.assertEqual(snapshot["status"], "critical")
        self.assertIn("Please free up disk space", snapshot["message"])

    def test_recent_eas_alerts_filters_last_24_hours_and_sorts_by_callsign_tie(self) -> None:
        web_control = self.web_control
        with tempfile.TemporaryDirectory() as temp_dir:
            streams_dir = Path(temp_dir) / "streams"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            streams = [
                {"id": "wz", "station": {"callsign": "WZ2560", "frequency": "162.500"}},
                {"id": "wxn", "station": {"callsign": "WXN99", "frequency": "162.475"}},
                {"id": "old", "station": {"callsign": "KZZ99", "frequency": "162.450"}},
            ]
            for stream in streams:
                index_path = web_control.eas_alert_index_path(streams_dir, stream)
                index_path.parent.mkdir(parents=True, exist_ok=True)
                issued = now - timedelta(hours=25) if stream["id"] == "old" else now
                index_path.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "alerts": [
                                {
                                    "event_type": "SVR",
                                    "start_time_utc": issued.isoformat().replace("+00:00", "Z"),
                                    "file_path": str(index_path.parent / "alert.wav"),
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )

            service = object.__new__(web_control.RtlControlService)
            service.streams = streams
            service.streams_directory = streams_dir

            alerts = service._recent_eas_alerts_locked()

        self.assertEqual([alert["callsign"] for alert in alerts], ["WXN99", "WZ2560"])
        self.assertEqual(alerts[0]["event_name"], "Severe Thunderstorm Warning")

    def test_gwes_requires_mp3_and_minimum_bitrate(self) -> None:
        valid = {
            "service": "gwes",
            "host": "ingest.wxr.gwes-cdn.net",
            "port": 10000,
            "username": "user",
            "password": "secret",
            "mount": "/WXN99.mp3",
            "format": "mp3",
            "sample_rate": 24000,
            "bitrate": 64,
        }
        self.assertEqual(self.web_control.validate_icecast_payload(valid)["service"], "gwes")
        invalid = dict(valid, bitrate=56)
        with self.assertRaisesRegex(ValueError, "at least 64 Kbps"):
            self.web_control.validate_icecast_payload(invalid)
        invalid = dict(valid, format="ogg")
        with self.assertRaisesRegex(ValueError, "requires MP3"):
            self.web_control.validate_icecast_payload(invalid)

    def test_weatherusa_restricts_audio_settings(self) -> None:
        valid = {
            "service": "weatherusa",
            "host": "radio-master.weatherusa.net",
            "port": 80,
            "username": "source",
            "password": "secret",
            "mount": "/NWR/WXN99.ogg",
            "format": "ogg",
            "sample_rate": 22050,
            "bitrate": 48,
        }
        self.assertEqual(self.web_control.validate_icecast_payload(valid)["service"], "weatherusa")
        with self.assertRaisesRegex(ValueError, "cannot be above 22050"):
            self.web_control.validate_icecast_payload(dict(valid, sample_rate=24000))
        with self.assertRaisesRegex(ValueError, "32 through 56"):
            self.web_control.validate_icecast_payload(dict(valid, bitrate=64))

    def test_noaa_weather_radio_org_requires_fixed_settings(self) -> None:
        valid = {
            "service": "nwrorg",
            "host": "wxradio.org",
            "port": 8000,
            "username": "source",
            "password": "WxRadio2014",
            "mount": "/MI-WestOlive-WXN99-alt1",
            "format": "mp3",
            "sample_rate": 22050,
            "bitrate": 32,
        }
        self.assertEqual(self.web_control.validate_icecast_payload(valid)["service"], "nwrorg")
        with self.assertRaisesRegex(ValueError, "requires a bitrate of 32"):
            self.web_control.validate_icecast_payload(dict(valid, bitrate=40))
        with self.assertRaisesRegex(ValueError, "predefined Icecast credentials"):
            self.web_control.validate_icecast_payload(dict(valid, password="other"))

    def test_add_stream_output_appends_to_persisted_output_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            streams_dir = Path(temp_dir) / "streams"
            existing_output = {
                "id": "output-1",
                "enabled": True,
                "type": "icecast",
                "icecast": {
                    "service": "custom",
                    "host": "example.com",
                    "port": 8000,
                    "username": "source",
                    "password": "secret",
                    "mount": "/one",
                    "format": "mp3",
                    "sample_rate": 22050,
                    "bitrate": 32,
                },
            }
            stream = {
                "id": "stream-1",
                "enabled": True,
                "station": {"callsign": "WXN99", "frequency": "162.475"},
                "outputs": [existing_output],
            }
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]
            service.test_icecast_auth = lambda icecast: {"success": True, "message": "Authentication successful."}
            service._sync_stream_workers_locked = lambda: None

            response = service.add_stream_output(
                {
                    "stream_id": "stream-1",
                    "icecast": {
                        "service": "custom",
                        "host": "example.net",
                        "port": 8000,
                        "username": "source",
                        "password": "secret",
                        "mount": "/two",
                        "format": "mp3",
                        "sample_rate": 22050,
                        "bitrate": 32,
                    },
                }
            )

            self.assertTrue(response["success"])
            self.assertEqual(len(stream["outputs"]), 2)
            self.assertEqual(stream["outputs"][1]["icecast"]["mount"], "/two")
            saved = json.loads((streams_dir / "WXN99" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual([output["icecast"]["mount"] for output in saved["outputs"]], ["/one", "/two"])

    def test_stream_status_reports_runtime_monitoring_without_persisting_it(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = self.web_control.threading.RLock()
        service.streams = [{"id": "stream-1", "station": {"callsign": "WXN99"}}]
        service.monitor_streams_by_client = {"client-1": "stream-1"}

        status = service.stream_status()

        self.assertEqual(status["monitoring"], {"client-1": "stream-1"})
        self.assertNotIn("monitoring", service.streams[0])

    def test_stream_worker_monitor_source_receives_processed_pcm_frame(self) -> None:
        worker = object.__new__(self.web_control.IcecastStreamWorker)
        worker.encoder_groups = {}
        worker.monitor_sources = {}
        worker.eas_recorder = None
        worker.lock = self.web_control.threading.Lock()
        source = worker.add_monitor_source("client-1")

        worker._write_pcm(b"frame")

        self.assertTrue(source.get_latest_pcm(timeout=0.01).startswith(b"frame"))

    def test_stream_worker_eas_recorder_taps_processed_pcm_frame(self) -> None:
        class Recorder:
            def __init__(self) -> None:
                self.frames = []

            def write(self, pcm) -> None:
                self.frames.append(pcm)

        recorder = Recorder()
        worker = object.__new__(self.web_control.IcecastStreamWorker)
        worker.encoder_groups = {}
        worker.monitor_sources = {}
        worker.eas_recorder = recorder
        worker.lock = self.web_control.threading.Lock()

        worker._write_pcm(b"processed")

        self.assertEqual(recorder.frames, [b"processed"])

    def test_stream_worker_treats_eas_as_shared_processed_output(self) -> None:
        worker = object.__new__(self.web_control.IcecastStreamWorker)
        worker.encoder_groups = {}
        worker.monitor_sources = {}
        worker.eas_recorder = object()
        worker.lock = self.web_control.threading.Lock()

        self.assertTrue(worker._has_connected_outputs())
        self.assertFalse(hasattr(self.web_control, "EasStreamWorker"))

    def test_receiver_frequency_validation_accepts_only_nwr_channels(self) -> None:
        self.assertEqual(self.web_control.validate_receiver_frequency(162475000), 162475000)
        with self.assertRaisesRegex(ValueError, "valid NWR receiver frequency"):
            self.web_control.validate_receiver_frequency(162487500)

    def test_receiver_worker_retunes_existing_channelizer_without_replacing_shifter(self) -> None:
        worker = object.__new__(self.web_control.WeatherReceiverWorker)
        channelizer = self.web_control.IqChannelizer(
            input_rate=240040,
            center_frequency_hz=162475000,
            target_frequency_hz=162475000,
        )
        shifter = channelizer.shifter
        channelizer.shifter._phase = 1.25
        channelizer.target_frequency_hz = 162425000
        channelizer.shifter.offset_hz = float(162475000 - 162425000)

        self.assertIs(channelizer.shifter, shifter)
        self.assertEqual(channelizer.shifter.offset_hz, 50000.0)
        self.assertEqual(channelizer.shifter._phase, 1.25)

    def test_alert_detail_uses_same_location_lookup(self) -> None:
        alert = {
            "event_type": "TOR",
            "fips_codes": ["026139", "092846"],
            "start_time_utc": "2026-08-09T19:07:00Z",
            "expires_at_utc": "2026-08-09T20:07:00Z",
            "file_path": "/tmp/alert.wav",
        }
        stream = {"id": "stream-1"}
        detail = self.web_control.eas_alert_detail(stream, alert, 0)
        self.assertEqual(detail["event_type"], "Tornado Warning")
        self.assertIn("Ottawa, MI", detail["areas"])
        self.assertIn("Holland to Grand Haven MI", detail["areas"])
        self.assertIn("August 9, 2026,", detail["issued_at"])
        self.assertIn("August 9, 2026,", detail["expires_at"])
        self.assertNotIn("file_path", detail)

    def test_sample_easrecorder_alert_shape_is_pretty_printed(self) -> None:
        alert = {
            "raw_same_header": "ZCZC-WXR-SVA-092846-026005-026139-092874+0730-1841835-KGRR/NWS-",
            "event_type": "SVA",
            "originator": "WXR",
            "fips_codes": ["092846", "026005", "026139", "092874"],
            "start_time_utc": "2026-07-03T18:35:00Z",
            "duration_code": "0730",
            "duration_seconds": 27000,
            "expires_at_utc": "2026-07-04T02:05:00Z",
            "sender_id": "KGRR/NWS",
            "file_path": "/home/max/easrecorder/wxn99/SVA-07-03-2026-1435EDT.mp3",
        }
        detail = self.web_control.eas_alert_detail({"id": "stream-1"}, alert, 0)
        summary = self.web_control.eas_alert_summary({}, alert, 0)

        self.assertEqual(detail["event_type"], "Severe Thunderstorm Watch")
        self.assertIn("Holland to Grand Haven MI", detail["areas"])
        self.assertIn("Allegan, MI", detail["areas"])
        self.assertIn("Ottawa, MI", detail["areas"])
        self.assertIn("Lake Michigan from Holland to Grand Haven MI 5NM offshore to Mid Lake", detail["areas"])
        self.assertIn("Severe thunderstorm watch issued July 3, 2026 at", summary["summary"])
        self.assertIn("July 3, 2026,", detail["issued_at"])
        self.assertIn("July 3, 2026,", detail["expires_at"])

    def test_nonzero_same_subdivision_digit_resolves_to_whole_area(self) -> None:
        self.assertEqual(
            self.web_control.format_same_location_for_alert("126139"),
            "Ottawa, MI, subdivision 1",
        )

    def test_alert_audio_path_must_stay_under_alert_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = state_dir / "streams" / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            audio_path = alert_dir / "alert.wav"
            audio_path.write_bytes(b"RIFF")
            stream = {"id": "stream-1", "station": {"callsign": "WXN99"}}

            resolved = self.web_control.safe_eas_alert_file_path(
                streams_dir,
                stream,
                {"file_path": str(audio_path)},
            )
            self.assertEqual(resolved, audio_path.resolve())

            outside_path = state_dir / "outside.wav"
            outside_path.write_bytes(b"RIFF")
            with self.assertRaises(ValueError):
                self.web_control.safe_eas_alert_file_path(
                    streams_dir,
                    stream,
                    {"file_path": str(outside_path)},
                )

    def test_remove_eas_alert_deletes_audio_and_rewrites_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = streams_dir / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            audio_path = alert_dir / "alert.wav"
            audio_path.write_bytes(b"RIFF")
            alert = {
                "raw_same_header": "ZCZC-WXR-TOR-026139+0030-2211907-KDTX/NWS-",
                "event_type": "TOR",
                "fips_codes": ["026139"],
                "start_time_utc": "2026-08-09T19:07:00Z",
                "expires_at_utc": "2026-08-09T19:37:00Z",
                "file_path": str(audio_path),
            }
            (alert_dir / "index.json").write_text(
                json.dumps({"version": 1, "alerts": [alert]}, indent=2),
                encoding="utf-8",
            )
            stream = {
                "id": "stream-1",
                "station": {"callsign": "WXN99"},
                "eas_recording": {"enabled": True},
            }
            (streams_dir / "WXN99").mkdir(exist_ok=True)
            (streams_dir / "WXN99" / "config.json").write_text(
                json.dumps(stream),
                encoding="utf-8",
            )
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]
            alert_id = self.web_control.eas_alert_id(alert, 0)
            response = service.remove_eas_alert("stream-1", alert_id)

            self.assertTrue(response["success"])
            self.assertFalse(audio_path.exists())
            rewritten = json.loads((alert_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(rewritten["alerts"], [])

    def test_remove_eas_alert_allows_already_missing_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = streams_dir / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            missing_audio = alert_dir / "missing.wav"
            alert = {
                "raw_same_header": "ZCZC-WXR-TOR-026139+0030-2211907-KDTX/NWS-",
                "event_type": "TOR",
                "fips_codes": ["026139"],
                "start_time_utc": "2026-08-09T19:07:00Z",
                "expires_at_utc": "2026-08-09T19:37:00Z",
                "file_path": str(missing_audio),
            }
            (alert_dir / "index.json").write_text(
                json.dumps({"version": 1, "alerts": [alert]}, indent=2),
                encoding="utf-8",
            )
            stream = {"id": "stream-1", "station": {"callsign": "WXN99"}}
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]

            response = service.remove_eas_alert("stream-1", self.web_control.eas_alert_id(alert, 0))

            self.assertTrue(response["success"])
            rewritten = json.loads((alert_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(rewritten["alerts"], [])

    def test_eas_alerts_are_returned_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = streams_dir / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            stream = {"id": "stream-1", "station": {"callsign": "WXN99"}, "eas_recording": {"enabled": True}}
            alerts = [
                {
                    "raw_same_header": "old",
                    "event_type": "SVR",
                    "fips_codes": ["026139"],
                    "start_time_utc": "2026-07-03T19:15:00Z",
                    "expires_at_utc": "2026-07-03T20:45:00Z",
                    "file_path": str(alert_dir / "old.mp3"),
                },
                {
                    "raw_same_header": "new",
                    "event_type": "DMO",
                    "fips_codes": ["999999"],
                    "start_time_utc": "2026-08-06T04:03:00Z",
                    "expires_at_utc": "2026-08-06T04:18:00Z",
                    "file_path": str(alert_dir / "new.mp3"),
                },
            ]
            (alert_dir / "index.json").write_text(
                json.dumps({"version": 1, "alerts": alerts}, indent=2),
                encoding="utf-8",
            )
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]

            response = service.eas_alerts("stream-1", page=1, per_page=10)

            self.assertEqual([alert["event_name"] for alert in response["alerts"]], ["Practice/Demo Warning", "Severe Thunderstorm Warning"])

    def test_export_zip_contains_audio_and_sanitized_index_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = streams_dir / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            audio_path = alert_dir / "alert-one.mp3"
            audio_path.write_bytes(b"audio")
            stream = {"id": "stream-1", "station": {"callsign": "WXN99"}, "eas_recording": {"enabled": True}}
            alert = {
                "raw_same_header": "export",
                "event_type": "SVR",
                "fips_codes": ["026139"],
                "start_time_utc": "2026-08-09T19:07:00Z",
                "expires_at_utc": "2026-08-09T19:37:00Z",
                "file_path": str(audio_path),
            }
            (alert_dir / "index.json").write_text(json.dumps({"version": 1, "alerts": [alert]}), encoding="utf-8")
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]

            zip_path, download_name = service.eas_alert_export_zip("stream-1", "all")
            try:
                self.assertEqual(download_name, "WXN99-eas-alerts.zip")
                with zipfile.ZipFile(zip_path) as archive:
                    self.assertIn("alert-one.mp3", archive.namelist())
                    exported_index = json.loads(archive.read("index.json").decode("utf-8"))
                self.assertEqual(exported_index["alerts"][0]["file_path"], "alert-one.mp3")
            finally:
                zip_path.unlink(missing_ok=True)

    def test_manual_range_with_no_alerts_returns_zero_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = streams_dir / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            stream = {"id": "stream-1", "station": {"callsign": "WXN99"}}
            alert = {
                "raw_same_header": "outside-range",
                "event_type": "SVR",
                "fips_codes": ["026139"],
                "start_time_utc": "2026-08-09T19:07:00Z",
                "expires_at_utc": "2026-08-09T19:37:00Z",
                "file_path": str(alert_dir / "alert.mp3"),
            }
            (alert_dir / "index.json").write_text(json.dumps({"version": 1, "alerts": [alert]}), encoding="utf-8")
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]

            response = service.eas_alert_range_count(
                "stream-1",
                "manual",
                "2026-08-08T00:00:00",
                "2026-08-08T23:59:00",
            )

            self.assertEqual(response["count"], 0)

    def test_future_manual_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.web_control.alert_range_bounds_from_request(
                "manual",
                "2999-01-01T00:00:00",
                "2999-01-01T01:00:00",
            )

    def test_delete_range_removes_audio_and_matching_index_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir)
            streams_dir = state_dir / "streams"
            alert_dir = streams_dir / "WXN99" / "alerts"
            alert_dir.mkdir(parents=True)
            old_audio = alert_dir / "old.mp3"
            keep_audio = alert_dir / "keep.mp3"
            old_audio.write_bytes(b"old")
            keep_audio.write_bytes(b"keep")
            stream = {"id": "stream-1", "station": {"callsign": "WXN99"}}
            alerts = [
                {
                    "raw_same_header": "delete",
                    "event_type": "SVR",
                    "fips_codes": ["026139"],
                    "start_time_utc": "2026-08-01T19:07:00Z",
                    "expires_at_utc": "2026-08-01T19:37:00Z",
                    "file_path": str(old_audio),
                },
                {
                    "raw_same_header": "keep",
                    "event_type": "SVR",
                    "fips_codes": ["026139"],
                    "start_time_utc": "2026-08-09T19:07:00Z",
                    "expires_at_utc": "2026-08-09T19:37:00Z",
                    "file_path": str(keep_audio),
                },
            ]
            (alert_dir / "index.json").write_text(json.dumps({"version": 1, "alerts": alerts}), encoding="utf-8")
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]

            response = service.remove_eas_alert_range(
                "stream-1",
                "manual",
                "2026-08-01T00:00:00",
                "2026-08-01T23:59:00",
            )

            self.assertEqual(response["count"], 1)
            self.assertFalse(old_audio.exists())
            self.assertTrue(keep_audio.exists())
            rewritten = json.loads((alert_dir / "index.json").read_text(encoding="utf-8"))
            self.assertEqual([alert["raw_same_header"] for alert in rewritten["alerts"]], ["keep"])

    def test_audio_effects_reject_highpass_above_1050_protection(self) -> None:
        with self.assertRaisesRegex(ValueError, "highpass.frequency"):
            self.web_control.validate_audio_payload({
                "deemphasis": {"enabled": True, "tau": 530},
                "comfort_noise": {"enabled": False, "level_db": -40},
                "volume": {"enabled": False, "multiplier": 1},
                "highpass": {"enabled": True, "frequency": 950, "sharpness": 5},
                "lowpass": {"enabled": False, "frequency": 4000, "sharpness": 0},
                "notch": {"enabled": False, "frequency": 3000, "sharpness": 0},
            })

    def test_audio_effects_reject_lowpass_below_same_mark_tone(self) -> None:
        with self.assertRaisesRegex(ValueError, "lowpass.frequency"):
            self.web_control.validate_audio_payload({
                "deemphasis": {"enabled": True, "tau": 530},
                "comfort_noise": {"enabled": False, "level_db": -40},
                "volume": {"enabled": False, "multiplier": 1},
                "highpass": {"enabled": False, "frequency": 300, "sharpness": 0},
                "lowpass": {"enabled": True, "frequency": 1800, "sharpness": 5},
                "notch": {"enabled": False, "frequency": 3000, "sharpness": 0},
            })

    def test_audio_effects_reject_notch_inside_protected_same_band(self) -> None:
        with self.assertRaisesRegex(ValueError, "protected"):
            self.web_control.validate_audio_payload({
                "deemphasis": {"enabled": True, "tau": 530},
                "comfort_noise": {"enabled": False, "level_db": -40},
                "volume": {"enabled": False, "multiplier": 1},
                "highpass": {"enabled": False, "frequency": 300, "sharpness": 0},
                "lowpass": {"enabled": False, "frequency": 4000, "sharpness": 0},
                "notch": {"enabled": True, "frequency": 1500, "sharpness": 5},
            })

    def test_audio_effects_allow_lowpass_and_notch_up_to_24khz_nyquist(self) -> None:
        audio = self.web_control.validate_audio_payload({
            "deemphasis": {"enabled": True, "tau": 530},
            "comfort_noise": {"enabled": False, "level_db": -40},
            "volume": {"enabled": False, "multiplier": 1},
            "highpass": {"enabled": False, "frequency": 300, "sharpness": 0},
            "lowpass": {"enabled": True, "frequency": 12000, "sharpness": 1},
            "notch": {"enabled": False, "frequency": 12000, "sharpness": 1},
        })
        notch_audio = self.web_control.validate_audio_payload({
            "deemphasis": {"enabled": True, "tau": 530},
            "comfort_noise": {"enabled": False, "level_db": -40},
            "volume": {"enabled": False, "multiplier": 1},
            "highpass": {"enabled": False, "frequency": 300, "sharpness": 0},
            "lowpass": {"enabled": False, "frequency": 12000, "sharpness": 1},
            "notch": {"enabled": True, "frequency": 12000, "sharpness": 1},
        })

        self.assertEqual(audio["lowpass"]["frequency"], 12000.0)
        self.assertEqual(notch_audio["notch"]["frequency"], 12000.0)

    def test_audio_config_defaults_are_stream_creation_defaults(self) -> None:
        audio = self.config.AudioConfig()

        self.assertTrue(audio.deemphasis.enabled)
        self.assertEqual(audio.deemphasis.tau, 300.0)
        self.assertTrue(audio.lowpass.enabled)
        self.assertEqual(audio.lowpass.frequency, 3400.0)
        self.assertEqual(audio.lowpass.sharpness, 2.0)

    def test_audio_effects_update_does_not_stop_active_workers(self) -> None:
        class Worker:
            stopped = False
            synced = False

            def stop(self) -> None:
                self.stopped = True

            def sync_stream(self, stream) -> None:
                self.synced = True

        with tempfile.TemporaryDirectory() as temp_dir:
            streams_dir = Path(temp_dir) / "streams"
            stream = {
                "id": "stream-1",
                "station": {"callsign": "WXN99"},
                "outputs": [
                    {
                        "id": "output-1",
                        "enabled": True,
                        "type": "icecast",
                        "icecast": {
                            "host": "example.com",
                            "port": 8000,
                            "username": "source",
                            "password": "secret",
                            "mount": "/wxn99",
                            "format": "mp3",
                            "sample_rate": 22050,
                            "bitrate": 64,
                        },
                    }
                ],
            }
            icecast_worker = Worker()
            eas_worker = Worker()
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.streams_directory = streams_dir
            service.streams = [stream]
            service.stream_workers = {"stream-1": icecast_worker}

            response = service.update_audio_effects({
                "stream_id": "stream-1",
                "audio": {
                    "deemphasis": {"enabled": True, "tau": 530},
                    "comfort_noise": {"enabled": False, "level_db": -40},
                    "volume": {"enabled": True, "multiplier": 1.2},
                    "highpass": {"enabled": False, "frequency": 300, "sharpness": 0},
                    "lowpass": {"enabled": False, "frequency": 4000, "sharpness": 0},
                    "notch": {"enabled": False, "frequency": 3000, "sharpness": 0},
                },
            })

            self.assertFalse(icecast_worker.stopped)
            self.assertTrue(icecast_worker.synced)
            self.assertEqual(response["streams"][0]["audio"]["volume"]["multiplier"], 1.2)

    def test_audio_effects_processor_volume_update_reuses_effect_objects(self) -> None:
        config = self.config
        processor = self.web_control.AudioEffectsProcessor(config.AudioConfig())
        comfort_noise = processor.comfort_noise
        deemphasis = processor.deemphasis
        deemphasis_makeup = processor.deemphasis_makeup
        highpass = processor.highpass
        lowpass = processor.lowpass
        notch = processor.notch

        changed = processor.update_config(config.AudioConfig(
            volume=config.VolumeConfig(enabled=True, multiplier=1.5)
        ))

        self.assertEqual(changed, ("volume",))
        self.assertIs(processor.comfort_noise, comfort_noise)
        self.assertIs(processor.deemphasis, deemphasis)
        self.assertIs(processor.deemphasis_makeup, deemphasis_makeup)
        self.assertIs(processor.highpass, highpass)
        self.assertIs(processor.lowpass, lowpass)
        self.assertIs(processor.notch, notch)

    def test_audio_effects_processor_retunes_changed_fir_filter_in_place(self) -> None:
        config = self.config
        processor = self.web_control.AudioEffectsProcessor(config.AudioConfig(
            highpass=config.FilterConfig(enabled=True, frequency=300, sharpness=1),
            lowpass=config.FilterConfig(enabled=True, frequency=4000, sharpness=1),
            notch=config.FilterConfig(enabled=True, frequency=3000, sharpness=1),
        ))
        highpass = processor.highpass
        lowpass = processor.lowpass
        notch = processor.notch
        highpass_kernel = highpass.kernel.copy()
        comfort_noise = processor.comfort_noise
        deemphasis = processor.deemphasis
        deemphasis_makeup = processor.deemphasis_makeup

        changed = processor.update_config(config.AudioConfig(
            highpass=config.FilterConfig(enabled=True, frequency=350, sharpness=1),
            lowpass=config.FilterConfig(enabled=True, frequency=4000, sharpness=1),
            notch=config.FilterConfig(enabled=True, frequency=3000, sharpness=1),
        ))

        self.assertEqual(changed, ("highpass",))
        self.assertIs(processor.highpass, highpass)
        self.assertFalse(np.array_equal(processor.highpass.kernel, highpass_kernel))
        self.assertIs(processor.lowpass, lowpass)
        self.assertIs(processor.notch, notch)
        self.assertIs(processor.comfort_noise, comfort_noise)
        self.assertIs(processor.deemphasis, deemphasis)
        self.assertIs(processor.deemphasis_makeup, deemphasis_makeup)

    def test_audio_effects_processor_retunes_deemphasis_in_place(self) -> None:
        config = self.config
        processor = self.web_control.AudioEffectsProcessor(config.AudioConfig(
            deemphasis=config.DeemphasisConfig(enabled=True, tau=300),
        ))
        deemphasis = processor.deemphasis
        curve = deemphasis.curve.copy()
        makeup = processor.deemphasis_makeup

        changed = processor.update_config(config.AudioConfig(
            deemphasis=config.DeemphasisConfig(enabled=True, tau=500),
        ))

        self.assertEqual(changed, ("deemphasis",))
        self.assertIs(processor.deemphasis, deemphasis)
        self.assertIs(processor.deemphasis_makeup, makeup)
        self.assertNotEqual(processor.deemphasis.curve.tobytes(), curve.tobytes())
        self.assertEqual(processor.deemphasis.tau, 500.0)
        self.assertEqual(processor.deemphasis_makeup.tau, 500.0)

    def test_audio_dc_blocker_tracks_dc_without_damaging_audio_tone(self) -> None:
        sample_rate = 24_000
        blocker = self.audio_effects.DcBlocker(sample_rate=sample_rate)
        time_axis = np.arange(sample_rate, dtype=np.float32) / sample_rate
        tone = (0.5 + 0.25 * np.sin(2.0 * np.pi * 1000.0 * time_axis)).astype(np.float32)

        output = np.concatenate(
            [
                blocker.process(tone[index : index + 480])
                for index in range(0, tone.size, 480)
            ]
        )

        settled = output[-4800:]
        self.assertLess(abs(float(np.mean(settled))), 0.02)
        self.assertGreater(float(np.max(settled) - np.min(settled)), 0.45)

    def test_audio_dc_blocker_does_not_step_at_frame_boundaries(self) -> None:
        sample_rate = 24_000
        frame_samples = 480
        blocker = self.audio_effects.DcBlocker(sample_rate=sample_rate)
        samples = np.full(frame_samples * 3, 0.5, dtype=np.float32)

        output = np.concatenate(
            [
                blocker.process(samples[index : index + frame_samples])
                for index in range(0, samples.size, frame_samples)
            ]
        )
        differences = np.abs(np.diff(output))
        boundary_difference = float(differences[frame_samples - 1])

        self.assertLess(boundary_difference, 0.01)
        self.assertLess(boundary_difference, float(np.max(differences[: frame_samples - 1])) * 2.0)

    def test_deemphasis_makeup_gain_increases_with_time_constant(self) -> None:
        self.assertLess(
            self.web_control.deemphasis_makeup_gain(0),
            self.web_control.deemphasis_makeup_gain(500),
        )

    def test_deemphasis_makeup_uses_fixed_gain_without_auto_normalizing(self) -> None:
        config = self.config
        processor = self.web_control.AudioEffectsProcessor(config.AudioConfig(
            deemphasis=config.DeemphasisConfig(enabled=False, tau=0),
            volume=config.VolumeConfig(enabled=False, multiplier=1.0),
            lowpass=config.FilterConfig(enabled=False, frequency=3400, sharpness=0),
        ))
        times = np.arange(2048, dtype=np.float32) / config.IQ_SAMPLE_RATE
        samples = (0.1 * np.sin(2 * np.pi * 1000.0 * times)).astype(np.float32)

        output = processor.process(samples)
        second_output = processor.process(samples)

        self.assertLess(float(np.max(np.abs(second_output - output))), 0.01)
        self.assertGreater(float(np.max(np.abs(output))), 0.07)
        self.assertLess(float(np.max(np.abs(output))), 0.13)

    def test_complex_nfm_demodulator_accepts_empty_streaming_blocks(self) -> None:
        demodulator = self.web_control.ComplexNfmDemodulator()

        first = demodulator.process(np.array([1 + 0j], dtype=np.complex64))
        empty = demodulator.process(np.array([], dtype=np.complex64))
        resumed = demodulator.process(np.array([0 + 1j], dtype=np.complex64))

        self.assertEqual(len(first), 0)
        self.assertEqual(len(empty), 0)
        self.assertEqual(len(resumed), 1)

    def test_fallback_frame_uses_audio_sample_rate(self) -> None:
        audio = types.SimpleNamespace(
            pcm=b"\x01\x00" * self.web_control.STREAM_FRAME_SAMPLES,
            sample_rate=self.web_control.IQ_SAMPLE_RATE,
        )
        state = self.web_control.WebFallbackPlaybackState()

        frame = self.web_control.next_web_fallback_frame(audio, state, loop_delay_seconds=0)

        self.assertEqual(len(frame), self.web_control.STREAM_FRAME_BYTES)

    def test_icecast_outputs_with_same_encoding_share_encoder_group(self) -> None:
        web_control = self.web_control

        class Fanout:
            def subscribe(self, max_chunks=64, max_seconds=None):
                return web_control.queue.Queue(maxsize=max_chunks)

            def unsubscribe(self, subscriber):
                pass

        class Encoder:
            header = b"header"

            def encode(self, pcm):
                return pcm

            def flush(self):
                return b""

            def close(self):
                pass

        created = []
        input_rates = []
        original_create_audio_encoder = self.web_control.create_audio_encoder
        self.web_control.create_audio_encoder = (
            lambda icecast, **kwargs: input_rates.append(kwargs.get("input_sample_rate")) or created.append(icecast) or Encoder()
        )
        try:
            icecast_a = self.config.IcecastConfig(
                host="example.com",
                port=8000,
                mount="/a",
                username="source",
                password="password",
                format="mp3",
                sample_rate=22050,
                bitrate=32,
            )
            icecast_b = self.config.IcecastConfig(
                host="example.net",
                port=9000,
                mount="/b",
                username="source",
                password="password",
                format="mp3",
                sample_rate=22050,
                bitrate=32,
            )
            worker = self.web_control.IcecastStreamWorker(
                stream={"id": "stream-1", "station": {"callsign": "WXN99", "frequency": "162.475"}, "outputs": []},
                fanout=Fanout(),
                fallback_settings_provider=lambda: None,
                alias_filter_strength_provider=lambda: self.web_control.ALIAS_FILTER_STRENGTH_DEFAULT,
                state_directory=Path(tempfile.gettempdir()),
            )
            first = worker.encoder_group_for(icecast_a)
            second = worker.encoder_group_for(icecast_b)
            self.assertIs(first, second)
            self.assertEqual(first.header(), b"header")
            self.assertEqual(second.header(), b"header")
            self.assertEqual(len(created), 1)
            self.assertEqual(input_rates, [self.web_control.IQ_SAMPLE_RATE])
            worker.stop()
        finally:
            self.web_control.create_audio_encoder = original_create_audio_encoder

    def test_stream_sync_stops_only_changed_icecast_output(self) -> None:
        web_control = self.web_control

        class Fanout:
            def subscribe(self, max_chunks=64, max_seconds=None):
                return web_control.queue.Queue(maxsize=max_chunks)

            def unsubscribe(self, subscriber):
                pass

        class Writer:
            instances = []

            def __init__(self, runtime, output):
                self.output = output
                self.signature = web_control.icecast_output_signature(output)
                self.started = False
                self.stopped = False
                Writer.instances.append(self)

            def start(self):
                self.started = True

            def stop(self):
                self.stopped = True

            def snapshot(self):
                return {}

        def output(output_id, mount, bitrate=32):
            return {
                "id": output_id,
                "enabled": True,
                "type": "icecast",
                "icecast": {
                    "host": "example.com",
                    "port": 8000,
                    "username": "source",
                    "password": "password",
                    "mount": mount,
                    "format": "mp3",
                    "sample_rate": 22050,
                    "bitrate": bitrate,
                },
            }

        original_writer = web_control.IcecastOutputWriter
        web_control.IcecastOutputWriter = Writer
        try:
            stream = {
                "id": "stream-1",
                "station": {"callsign": "WXN99", "frequency": "162.475"},
                "outputs": [output("one", "/one"), output("two", "/two")],
            }
            worker = web_control.IcecastStreamWorker(
                stream=stream,
                fanout=Fanout(),
                fallback_settings_provider=lambda: None,
                alias_filter_strength_provider=lambda: web_control.ALIAS_FILTER_STRENGTH_DEFAULT,
                state_directory=Path(tempfile.gettempdir()),
            )
            worker.sync_stream(stream)
            first_writer, second_writer = Writer.instances

            updated_stream = {
                **stream,
                "outputs": [output("one", "/one"), output("two", "/two", bitrate=40)],
            }
            worker.sync_stream(updated_stream)

            self.assertFalse(first_writer.stopped)
            self.assertTrue(second_writer.stopped)
            self.assertEqual(len(Writer.instances), 3)
        finally:
            web_control.IcecastOutputWriter = original_writer

    def test_iq_recorder_writes_spectrum_cf32_from_synthetic_rtl_iq(self) -> None:
        web_control = self.web_control

        class Fanout:
            def __init__(self):
                self.queue = web_control.queue.Queue(maxsize=8)

            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return self.queue

            def unsubscribe(self, subscriber):
                pass

        class Storage:
            def add_path(self, path):
                pass

            def recording_started(self):
                pass

            def recording_stopped(self):
                pass

            def is_critical(self, path):
                return False

        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "spectrum.cf32"
            fanout = Fanout()
            sample_rate = 192_000
            iq = np.exp(1j * 2 * np.pi * 1000 * np.arange(2048, dtype=np.float32) / sample_rate).astype(np.complex64)
            raw = self._complex_to_rtl_u8(iq)
            worker = web_control.IqRecorderWorker(
                fanout=fanout,
                config=web_control.IqRecorderConfig(
                    recording_id="recording-1",
                    mode=web_control.IQ_RECORDER_MODE_SPECTRUM,
                    sample_rate=sample_rate,
                    duration_seconds=1.0,
                    output_path=output_path,
                    index_path=Path(tempdir) / "index.json",
                    frequency_hz=162_475_000,
                ),
                storage_monitor=Storage(),
                alias_filter_strength_provider=lambda: web_control.ALIAS_FILTER_STRENGTH_DEFAULT,
            )
            worker.start()
            fanout.queue.put(web_control.RtlSampleBatch(data=raw, sample_rate=sample_rate, center_frequency_hz=162_475_000))
            self._wait_for(lambda: output_path.exists() and output_path.stat().st_size >= iq.size * 8)
            worker.stop()

            self.assertEqual(output_path.stat().st_size, iq.size * 8)
            self.assertEqual(worker.snapshot()["sample_rate"], sample_rate)
            entries = web_control.load_iq_recording_entries(Path(tempdir) / "index.json")
            self.assertEqual(entries[0]["id"], "recording-1")
            self.assertEqual(entries[0]["sample_rate"], sample_rate)

    def test_iq_recorder_spectrum_decimators_support_all_recording_rates(self) -> None:
        web_control = self.web_control

        class Fanout:
            def __init__(self):
                self.queue = web_control.queue.Queue(maxsize=8)

            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return self.queue

            def unsubscribe(self, subscriber):
                pass

        class Storage:
            def add_path(self, path):
                pass

            def recording_started(self):
                pass

            def recording_stopped(self):
                pass

            def is_critical(self, path):
                return False

        input_rate = web_control.DEFAULT_RTL_SAMPLE_RATE
        input_count = 8192
        iq = np.exp(1j * 2 * np.pi * 1000 * np.arange(input_count, dtype=np.float32) / input_rate).astype(np.complex64)
        raw = self._complex_to_rtl_u8(iq)
        with tempfile.TemporaryDirectory() as tempdir:
            for sample_rate in web_control.IQ_RECORDER_SAMPLE_RATES:
                with self.subTest(sample_rate=sample_rate):
                    output_path = Path(tempdir) / f"spectrum-{sample_rate}.cf32"
                    fanout = Fanout()
                    worker = web_control.IqRecorderWorker(
                        fanout=fanout,
                        config=web_control.IqRecorderConfig(
                            recording_id=f"recording-{sample_rate}",
                            mode=web_control.IQ_RECORDER_MODE_SPECTRUM,
                            sample_rate=sample_rate,
                            duration_seconds=1.0,
                            output_path=output_path,
                            index_path=Path(tempdir) / f"index-{sample_rate}.json",
                            frequency_hz=162_475_000,
                        ),
                        storage_monitor=Storage(),
                        alias_filter_strength_provider=lambda: web_control.ALIAS_FILTER_STRENGTH_DEFAULT,
                    )
                    worker.start()
                    fanout.queue.put(web_control.RtlSampleBatch(data=raw, sample_rate=input_rate, center_frequency_hz=162_475_000))
                    self._wait_for(lambda path=output_path: path.exists() and path.stat().st_size > 0)
                    worker.stop()
                    self.assertEqual(output_path.stat().st_size % 8, 0)

    def test_intermediate_iq_fanout_decimates_once_for_multiple_channel_subscribers(self) -> None:
        web_control = self.web_control

        class RawFanout:
            def __init__(self):
                self.queue = web_control.queue.Queue(maxsize=8)

            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return self.queue

            def unsubscribe(self, subscriber):
                pass

        raw_fanout = RawFanout()
        intermediate = web_control.IntermediateIqFanout(raw_fanout)
        first = intermediate.subscribe(max_chunks=4, name="first-channel")
        second = intermediate.subscribe(max_chunks=4, name="second-channel")
        input_rate = web_control.DEFAULT_RTL_SAMPLE_RATE
        decimator = web_control.create_decimator(
            input_rate,
            web_control.INTERMEDIATE_IQ_SAMPLE_RATE,
            transition_hz=web_control.INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
            attenuation_db=web_control.INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
        )
        self.assertLess(decimator.fir.taps.size, 900)
        iq = np.exp(1j * 2 * np.pi * 1000 * np.arange(65_536, dtype=np.float32) / input_rate).astype(np.complex64)
        raw = self._complex_to_rtl_u8(iq)
        intermediate.start()
        raw_fanout.queue.put(web_control.RtlSampleBatch(data=raw, sample_rate=input_rate, center_frequency_hz=162_475_000))
        try:
            first_batch = first.get(timeout=2.0)
            second_batch = second.get(timeout=2.0)
        finally:
            intermediate.stop()

        self.assertEqual(first_batch.sample_rate, 192_000)
        self.assertEqual(second_batch.sample_rate, 192_000)
        np.testing.assert_array_equal(first_batch.data, second_batch.data)
        self.assertEqual(intermediate.stats()["output_batches"], 1)

    def test_intermediate_iq_fanout_slow_subscriber_keeps_latest_batch(self) -> None:
        web_control = self.web_control

        class RawFanout:
            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return web_control.queue.Queue(maxsize=max_chunks)

            def unsubscribe(self, subscriber):
                pass

        intermediate = web_control.IntermediateIqFanout(RawFanout())
        subscriber = intermediate.subscribe(max_chunks=1, name="slow-channel")
        first = web_control.IqSampleBatch(
            data=np.array([1 + 0j], dtype=np.complex64),
            sample_rate=192_000,
            center_frequency_hz=162_475_000,
        )
        second = web_control.IqSampleBatch(
            data=np.array([2 + 0j], dtype=np.complex64),
            sample_rate=192_000,
            center_frequency_hz=162_475_000,
        )

        intermediate._publish(first)
        intermediate._publish(second)

        latest = subscriber.get_nowait()
        self.assertEqual(latest.data[0], np.complex64(2 + 0j))
        stats = intermediate.subscriber_stats(subscriber)
        self.assertEqual(stats["queue_capacity"], 1)
        self.assertEqual(stats["dropped_batches"], 1)

    def test_iq_recorder_writes_stream_channel_cf32_from_synthetic_rtl_iq(self) -> None:
        web_control = self.web_control

        class Fanout:
            def __init__(self):
                self.queue = web_control.queue.Queue(maxsize=8)

            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return self.queue

            def unsubscribe(self, subscriber):
                pass

        class Storage:
            def add_path(self, path):
                pass

            def recording_started(self):
                pass

            def recording_stopped(self):
                pass

            def is_critical(self, path):
                return False

        with tempfile.TemporaryDirectory() as tempdir:
            output_path = Path(tempdir) / "stream.cf32"
            fanout = Fanout()
            sample_rate = 240_000
            iq = np.exp(1j * 2 * np.pi * 1000 * np.arange(48_000, dtype=np.float32) / sample_rate).astype(np.complex64)
            raw = self._complex_to_rtl_u8(iq)
            worker = web_control.IqRecorderWorker(
                fanout=fanout,
                config=web_control.IqRecorderConfig(
                    recording_id="recording-2",
                    mode=web_control.IQ_RECORDER_MODE_STREAM,
                    sample_rate=web_control.IQ_SAMPLE_RATE,
                    duration_seconds=1.0,
                    output_path=output_path,
                    index_path=Path(tempdir) / "index.json",
                    frequency_hz=162_475_000,
                    stream_id="stream-1",
                    stream_label="WXN99",
                    target_frequency_hz=162_475_000,
                ),
                storage_monitor=Storage(),
                alias_filter_strength_provider=lambda: web_control.ALIAS_FILTER_STRENGTH_DEFAULT,
            )
            worker.start()
            fanout.queue.put(web_control.RtlSampleBatch(data=raw, sample_rate=sample_rate, center_frequency_hz=162_475_000))
            self._wait_for(lambda: output_path.exists() and output_path.stat().st_size > 0)
            worker.stop()

            self.assertEqual(output_path.stat().st_size % 8, 0)
            self.assertEqual(worker.snapshot()["sample_rate"], web_control.IQ_SAMPLE_RATE)

    def test_iq_recording_download_conversion_and_name(self) -> None:
        web_control = self.web_control
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "recording.cf32"
            floats = np.array([-1.0, 0.0, 1.0, 0.5], dtype="<f4")
            path.write_bytes(floats.tobytes())
            self.assertEqual(web_control.iq_recording_download_size(path, "cf"), 16)
            self.assertEqual(web_control.iq_recording_download_size(path, "s16"), 8)
            self.assertEqual(web_control.iq_recording_download_size(path, "u8"), 4)
            with path.open("rb") as source:
                signed = b"".join(web_control.convert_iq_recording_chunks(source, "s16"))
            self.assertEqual(np.frombuffer(signed, dtype="<i2").tolist(), [-32767, 0, 32767, 16384])
            with path.open("rb") as source:
                unsigned = b"".join(web_control.convert_iq_recording_chunks(source, "u8"))
            self.assertEqual(np.frombuffer(unsigned, dtype=np.uint8).tolist(), [0, 128, 255, 191])
            sleeps = []
            original_sleep = web_control.time.sleep
            web_control.time.sleep = lambda seconds: sleeps.append(seconds)
            try:
                with path.open("rb") as source:
                    complex_float = b"".join(web_control.convert_iq_recording_chunks(source, "cf"))
            finally:
                web_control.time.sleep = original_sleep
            self.assertEqual(complex_float, floats.tobytes())
            self.assertEqual(sleeps, [web_control.IQ_DOWNLOAD_YIELD_SECONDS])
            recording = {
                "sample_rate": 240000,
                "frequency_hz": 162475000,
                "started_at": datetime(2026, 8, 13, 17, 17, tzinfo=timezone.utc).timestamp(),
            }
            name = web_control.iq_recording_download_name(recording, "cf")
            self.assertTrue(name.startswith("nwrstmgr-s240000-f162475000-"))
            self.assertTrue(name.endswith("-cf.raw"))

    def test_iq_recording_duration_allows_manual_stop_zero(self) -> None:
        self.assertEqual(self.web_control.validate_iq_recording_duration(0), 0)
        with self.assertRaisesRegex(ValueError, "manual stop"):
            self.web_control.validate_iq_recording_duration(-1)

    def test_iq_storage_remaining_estimate_uses_available_space_above_critical_reserve(self) -> None:
        recorder = {
            "active": True,
            "elapsed_seconds": 10.0,
            "bytes_written": 1_000_000,
        }
        storage = {
            "filesystems": [
                {
                    "available_bytes": self.web_control.STORAGE_CRITICAL_FREE_BYTES + 5_000_000,
                    "total_bytes": 10_000_000_000,
                }
            ]
        }
        remaining = self.web_control.estimate_iq_storage_remaining_seconds(
            recorder,
            storage,
            Path(tempfile.gettempdir()),
        )
        self.assertIsNotNone(remaining)
        self.assertGreater(float(remaining), 0.0)

    def test_iq_storage_remaining_smoothing_ignores_small_fluctuations(self) -> None:
        smoothed = self.web_control.smooth_iq_storage_remaining_seconds(
            previous=180_000.0,
            elapsed_since_update=1.0,
            raw=179_800.0,
        )
        self.assertEqual(smoothed, 179_999.0)

    def test_iq_storage_remaining_smoothing_reacts_to_large_storage_drop(self) -> None:
        smoothed = self.web_control.smooth_iq_storage_remaining_seconds(
            previous=180_000.0,
            elapsed_since_update=1.0,
            raw=170_000.0,
        )
        self.assertEqual(smoothed, 170_000.0)

    @staticmethod
    def _complex_to_rtl_u8(iq: np.ndarray) -> bytes:
        interleaved = np.empty(iq.size * 2, dtype=np.float32)
        interleaved[0::2] = np.clip(iq.real, -1.0, 1.0)
        interleaved[1::2] = np.clip(iq.imag, -1.0, 1.0)
        return np.clip(np.round(interleaved * 127.5 + 127.5), 0, 255).astype(np.uint8).tobytes()

    @staticmethod
    def _wait_for(predicate, timeout: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition was not met before timeout")


if __name__ == "__main__":
    unittest.main()
