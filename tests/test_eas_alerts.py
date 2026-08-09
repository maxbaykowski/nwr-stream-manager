from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path


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

    def test_alert_summary_uses_same_event_lookup(self) -> None:
        alert = {
            "event_type": "SVR",
            "start_time_utc": "2026-08-09T19:07:00Z",
            "file_path": "/tmp/alert.wav",
        }
        summary = self.web_control.eas_alert_summary({}, alert, 0)
        self.assertEqual(summary["event_name"], "Severe Thunderstorm Warning")
        self.assertIn("Severe thunderstorm warning issued August 9, 2026 at", summary["summary"])

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


if __name__ == "__main__":
    unittest.main()
