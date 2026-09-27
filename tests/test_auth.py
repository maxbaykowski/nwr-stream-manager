from __future__ import annotations

import importlib.util
import base64
import hashlib
import sys
import tempfile
import types
import unittest
import threading
import struct
from unittest.mock import patch
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


class AuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_account_store_creates_and_verifies_admin(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")

            self.assertFalse(store.has_account())
            store.create_admin("Admin_User-1", "StrongPass!1", "StrongPass!1")

            self.assertTrue(store.has_account())
            self.assertTrue(store.verify_basic_credentials("Admin_User-1", "StrongPass!1"))
            self.assertFalse(store.verify_basic_credentials("Admin_User-1", "wrong-password"))
            self.assertFalse(store.verify_basic_credentials("other", "StrongPass!1"))

    def test_account_store_rejects_invalid_usernames_and_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")

            with self.assertRaisesRegex(ValueError, "username"):
                store.create_admin("bad user", "StrongPass!1", "StrongPass!1")
            with self.assertRaisesRegex(ValueError, "password"):
                store.create_admin("admin", "short", "short")
            with self.assertRaisesRegex(ValueError, "match"):
                store.create_admin("admin", "StrongPass!1", "StrongPass!2")

    def test_setup_html_contains_required_flow_and_validation(self) -> None:
        html = self.web_control.SETUP_HTML

        self.assertIn("NWR Stream Manager Setup", html)
        self.assertIn("Welcome to NWR Stream Manager", html)
        self.assertIn("/api/setup-account", html)
        self.assertIn("usernamePattern", html)
        self.assertIn("passwordPattern", html)
        self.assertIn("setup_next", html)
        self.assertIn("/api/setup-state", html)
        self.assertIn("pageshow", html)
        self.assertIn("window.location.replace", html)

    def test_control_html_uses_existing_select_sync_helper(self) -> None:
        html = self.web_control.INDEX_HTML

        self.assertIn("function syncSelectOptions", html)
        self.assertIn("syncSelectOptions(select, optionSpecs)", html)
        self.assertNotIn("updateSelectOptions(", html)

    def test_notification_custom_access_url_accepts_missing_scheme(self) -> None:
        settings = self.web_control.validate_notification_settings_payload(
            {
                "enabled": True,
                "server_url": "https://ntfy.sh",
                "topic": "NWRSTMGR-test",
                "access_url_mode": "custom",
                "custom_access_url": "nwr.example.com",
            }
        )

        self.assertEqual(settings.custom_access_url, "https://nwr.example.com")

    def test_notification_access_url_selection_falls_back_when_unavailable(self) -> None:
        settings = self.web_control.WebNotificationSettings(
            access_url_mode="tailscale:tailscale0:100.113.206.20",
        )

        reconciled = self.web_control.reconcile_notification_access_url_selection(
            settings,
            [{"id": "interface:wlp9s0:192.168.1.79", "url": "http://192.168.1.79:8080"}],
        )

        self.assertEqual(reconciled.access_url_mode, "auto")

    def test_notification_access_url_selection_preserves_available_or_custom(self) -> None:
        selected = self.web_control.WebNotificationSettings(
            access_url_mode="tailscale:tailscale0:100.113.206.20",
        )
        custom = self.web_control.WebNotificationSettings(
            access_url_mode="custom",
            custom_access_url="https://nwr.example.com",
        )
        options = [{"id": "tailscale:tailscale0:100.113.206.20", "url": "http://100.113.206.20:8080"}]

        self.assertIs(self.web_control.reconcile_notification_access_url_selection(selected, options), selected)
        self.assertIs(self.web_control.reconcile_notification_access_url_selection(custom, []), custom)

    def test_ntfy_notification_sets_click_header(self) -> None:
        settings = self.web_control.WebNotificationSettings(
            enabled=True,
            server_url="https://ntfy.sh",
            topic="NWRSTMGR-test",
        )
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout=0):
            captured["headers"] = dict(request.header_items())
            captured["timeout"] = timeout
            return FakeResponse()

        with patch.object(self.web_control, "urlopen", fake_urlopen):
            self.web_control.send_ntfy_notification(
                settings,
                title="NWR Stream Manager",
                message="Test",
                click_url="192.0.2.5:8080",
            )

        self.assertEqual(captured["headers"]["Click"], "http://192.0.2.5:8080")

    def test_ntfy_notification_allows_click_header_with_app_query(self) -> None:
        settings = self.web_control.WebNotificationSettings(
            enabled=True,
            server_url="https://ntfy.sh",
            topic="NWRSTMGR-test",
        )
        click_url = self.web_control.notification_click_url(
            "nwr.example.com:8080",
            "/?view=stream_output&stream=stream-1&output=icecast-1",
        )
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout=0):
            captured["headers"] = dict(request.header_items())
            return FakeResponse()

        with patch.object(self.web_control, "urlopen", fake_urlopen):
            self.web_control.send_ntfy_notification(
                settings,
                title="NWR Stream Manager",
                message="Test",
                click_url=click_url,
            )

        self.assertEqual(captured["headers"]["Click"], click_url)

    def test_notification_click_url_forces_notification_auth_entry(self) -> None:
        url = self.web_control.notification_click_url(
            "nwr.example.com:8080",
            "/?view=stream_output&stream=stream-1&output=icecast-1",
        )

        self.assertEqual(
            url,
            "http://nwr.example.com:8080/notification?next=%2F%3Fview%3Dstream_output%26stream%3Dstream-1%26output%3Dicecast-1",
        )

    def test_notification_app_url_preserves_session_for_eas_alert_detail(self) -> None:
        alert_path = self.web_control.eas_alert_detail_route_path("stream-1", "alert-1")
        url = self.web_control.notification_app_url("nwr.example.com:8080", alert_path)

        self.assertEqual(alert_path, "/?view=eas_alert_detail&stream=stream-1&alert=alert-1")
        self.assertEqual(url, "http://nwr.example.com:8080/?view=eas_alert_detail&stream=stream-1&alert=alert-1")

    def test_notification_target_restricted_for_read_only_output_details(self) -> None:
        self.assertTrue(
            self.web_control.notification_target_restricted_for_read_only(
                "/?view=stream_output&stream=stream-1&output=icecast-1"
            )
        )
        self.assertFalse(self.web_control.notification_target_restricted_for_read_only("/"))

    def test_stream_notification_failures_include_enabled_icecast_failure(self) -> None:
        stream = {
            "id": "stream-1",
            "station": {"callsign": "WXN99"},
            "notifications": {"icecast_failures": True, "soundcard_failures": False},
        }
        snapshot = {
            "id": "stream-1",
            "status": "needs-attention",
            "outputs": [
                {
                    "id": "icecast-1",
                    "type": "icecast",
                    "status": "needs-attention",
                    "icecast": {"host": "example.test", "port": 8000, "mount": "/WXN99.mp3"},
                }
            ],
        }

        failures = self.web_control.stream_notification_failures(
            stream,
            snapshot,
            self.web_control.stream_notification_settings_from_stream(stream),
        )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["key"], "stream-1:icecast:icecast-1")
        self.assertIn("WXN99", failures[0]["message"])
        self.assertIn("http://example.test:8000/WXN99.mp3", failures[0]["message"])
        self.assertEqual(
            failures[0]["target_path"],
            "/?view=stream_output&stream=stream-1&output=icecast-1",
        )

    def test_rtl_failure_notification_waits_for_grace_then_repeats_hourly(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.settings = self.web_control.RtlControlSettings(serial="00000001", notify_sdr_failures=True)
        service.iq_file_source_config = None
        service.capture = None
        service.capture_error = ""
        service.last_batch_at = None
        service.rtl_notification_states = {}
        service.rtl_device_name_cache = {}
        service.devices = lambda: {"devices": [{"serial": "00000001", "name": "RTLSDRBlog Blog V4", "vendor": ""}]}
        sent: list[dict[str, object]] = []
        service._send_stream_problem_notification_async = sent.append

        service._process_rtl_notifications_locked(1000.0)
        service._process_rtl_notifications_locked(1029.0)
        self.assertEqual(sent, [])

        service._process_rtl_notifications_locked(1031.0)
        self.assertEqual(sent[-1]["message"], "RTLSDRBlog Blog V4 is not connected.")
        self.assertEqual(sent[-1]["target_path"], "/?view=rtl")

        service._process_rtl_notifications_locked(1200.0)
        self.assertEqual(len(sent), 1)
        service.capture = object()
        service.last_batch_at = 1232.0
        service._process_rtl_notifications_locked(1232.0)
        self.assertEqual(service.rtl_notification_states, {})
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1]["message"], "RTLSDRBlog Blog V4 is now connected.")
        self.assertEqual(sent[-1]["target_path"], "/?view=rtl")
        service.capture = None
        service.last_batch_at = None
        service._process_rtl_notifications_locked(1270.0)
        self.assertEqual(len(sent), 2)
        service._process_rtl_notifications_locked(1301.0)
        self.assertEqual(len(sent), 3)
        service._process_rtl_notifications_locked(1400.0)
        self.assertEqual(len(sent), 3)
        service._process_rtl_notifications_locked(4902.0)
        self.assertEqual(len(sent), 4)

    def _stream_notification_service(self, streams, signals=None):
        wc = self.web_control
        service = object.__new__(wc.RtlControlService)
        service.streams = streams
        service.stream_notification_states = {}
        service.rtl_notification_states = {}
        service.stream_eas_alert_notification_seen = set()
        service.stream_workers = {}
        service._notification_globally_ready_locked = lambda: True
        service._process_rtl_notifications_locked = lambda now: None
        service._process_recorded_eas_alert_notifications_locked = lambda: None
        service._stream_signals_locked = lambda: dict(signals or {})
        sent: list[dict[str, object]] = []
        service._send_stream_problem_notification_async = sent.append
        return service, sent

    def _run_notifications(self, service, snapshots, at: float) -> None:
        with patch.object(self.web_control.time, "monotonic", lambda: at):
            service._process_stream_notifications_locked(snapshots)

    @staticmethod
    def _icecast_snapshot(status: str) -> list[dict[str, object]]:
        return [{
            "id": "stream-1",
            "status": status,
            "outputs": [{
                "id": "out-1",
                "type": "icecast",
                "status": status,
                "icecast": {"host": "radio.example.com", "port": 8000, "mount": "/kec49"},
            }],
        }]

    def _icecast_stream(self, **notifications):
        return {
            "id": "stream-1",
            "enabled": True,
            "station": {"callsign": "KEC49"},
            "notifications": {"icecast_failures": True, **notifications},
        }

    def test_icecast_recovery_is_announced_after_the_recovery_period(self) -> None:
        stream = self._icecast_stream()
        service, sent = self._stream_notification_service([stream])
        self._run_notifications(service, self._icecast_snapshot("needs-attention"), 1000.0)
        self._run_notifications(service, self._icecast_snapshot("needs-attention"), 1031.0)
        self.assertEqual(len(sent), 1)

        self._run_notifications(service, self._icecast_snapshot("enabled"), 1040.0)
        self._run_notifications(service, self._icecast_snapshot("enabled"), 1060.0)
        self.assertEqual(len(sent), 1)  # still inside the 30 s recovery period
        self._run_notifications(service, self._icecast_snapshot("enabled"), 1062.0)

        self.assertEqual(len(sent), 2)
        self.assertEqual(
            sent[-1]["message"],
            "KEC49: successfully connected to the Icecast mountpoint at http://radio.example.com:8000/kec49.",
        )
        self.assertEqual(sent[-1]["priority"], 3)
        self.assertEqual(service.stream_notification_states, {})

    def test_no_recovery_notice_when_failure_was_never_announced(self) -> None:
        service, sent = self._stream_notification_service([self._icecast_stream()])
        self._run_notifications(service, self._icecast_snapshot("needs-attention"), 1000.0)
        self._run_notifications(service, self._icecast_snapshot("enabled"), 1010.0)
        self._run_notifications(service, self._icecast_snapshot("enabled"), 1100.0)

        self.assertEqual(sent, [])

    def test_disabling_a_failing_stream_or_its_toggle_is_not_a_recovery(self) -> None:
        for change in ("disable_stream", "toggle_off", "output_removed"):
            with self.subTest(change=change):
                stream = self._icecast_stream()
                service, sent = self._stream_notification_service([stream])
                self._run_notifications(service, self._icecast_snapshot("needs-attention"), 1000.0)
                self._run_notifications(service, self._icecast_snapshot("needs-attention"), 1031.0)
                self.assertEqual(len(sent), 1)
                snapshots = self._icecast_snapshot("disabled")
                if change == "disable_stream":
                    stream["enabled"] = False
                elif change == "toggle_off":
                    stream["notifications"]["icecast_failures"] = False
                    snapshots = self._icecast_snapshot("enabled")
                else:
                    snapshots = []
                self._run_notifications(service, snapshots, 1040.0)
                self._run_notifications(service, snapshots, 1100.0)

                self.assertEqual(len(sent), 1)
                self.assertEqual(service.stream_notification_states, {})

    def test_soundcard_and_reception_recovery_messages(self) -> None:
        stream = {
            "id": "stream-1",
            "enabled": True,
            "station": {"callsign": "KEC49"},
            "notifications": {"soundcard_failures": True, "bad_reception": True},
        }
        signals = {"stream-1": {"available": True, "reception_problem": "no_signal", "snr_db": -3.0}}
        service, sent = self._stream_notification_service([stream], signals)

        def snapshot(status):
            return [{
                "id": "stream-1",
                "status": status,
                "outputs": [{"id": "sc-1", "type": "soundcard", "status": status, "soundcard": {"display_name": "Yeti X"}}],
            }]

        self._run_notifications(service, snapshot("needs-attention"), 1000.0)
        self._run_notifications(service, snapshot("needs-attention"), 1031.0)
        self.assertEqual(
            sorted(item["message"] for item in sent),
            ["KEC49: no signal, only static is being received.", "KEC49: sound card Yeti X is not connected."],
        )
        signals["stream-1"] = {"available": True, "reception_problem": None, "snr_db": 18.0}
        self._run_notifications(service, snapshot("enabled"), 1040.0)
        self._run_notifications(service, snapshot("enabled"), 1062.0)

        self.assertEqual(
            sorted(item["message"] for item in sent[2:]),
            ["KEC49: reception has improved.", "KEC49: sound card Yeti X is now connected."],
        )

    def test_rtl_recovery_matches_the_failure_that_was_announced(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.settings = self.web_control.RtlControlSettings(serial="00000001", notify_sdr_failures=True)
        service.iq_file_source_config = None
        service.capture = None
        service.capture_error = ""
        service.last_batch_at = None
        service.rtl_notification_states = {}
        service.rtl_device_name_cache = {}
        service.devices = lambda: {"devices": []}
        sent: list[dict[str, object]] = []
        service._send_stream_problem_notification_async = sent.append

        service._process_rtl_notifications_locked(1000.0)
        service._process_rtl_notifications_locked(1031.0)
        self.assertEqual(sent[-1]["message"], "The configured RTL-SDR is not connected.")
        # Plugged back in but not streaming yet: a different problem, never announced.
        service.devices = lambda: {"devices": [{"serial": "00000001", "name": "RTLSDRBlog Blog V4", "vendor": ""}]}
        service.capture = object()
        service._process_rtl_notifications_locked(1040.0)
        service.last_batch_at = 1045.0
        service._process_rtl_notifications_locked(1045.0)
        service._process_rtl_notifications_locked(1071.0)

        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[-1]["message"], "The configured RTL-SDR is now connected.")

    def test_rtl_problem_that_clears_within_grace_sends_nothing(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.settings = self.web_control.RtlControlSettings(serial="00000001", notify_sdr_failures=True)
        service.iq_file_source_config = None
        service.capture = None
        service.capture_error = ""
        service.last_batch_at = None
        service.rtl_notification_states = {}
        service.rtl_device_name_cache = {}
        service.devices = lambda: {"devices": [{"serial": "00000001", "name": "RTLSDRBlog Blog V4", "vendor": ""}]}
        sent: list[dict[str, object]] = []
        service._send_stream_problem_notification_async = sent.append

        service._process_rtl_notifications_locked(1000.0)
        service.capture = object()
        service.last_batch_at = 1010.0
        service._process_rtl_notifications_locked(1010.0)
        service._process_rtl_notifications_locked(1100.0)

        self.assertEqual(sent, [])

    def test_rtl_notification_classifies_permission_and_no_data_failures(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.settings = self.web_control.RtlControlSettings(serial="00000001", notify_sdr_failures=True)
        service.iq_file_source_config = None
        service.capture = object()
        service.last_batch_at = 900.0
        service.rtl_notification_states = {}
        service.rtl_device_name_cache = {}
        service.devices = lambda: {"devices": [{"serial": "00000001", "name": "RTLSDRBlog Blog V4", "vendor": ""}]}

        service.capture_error = "Access denied while opening RTL-SDR"
        failure = service._rtl_notification_failure_locked(1000.0)
        self.assertIsNotNone(failure)
        self.assertEqual(failure["message"], "Insufficient permissions to access RTLSDRBlog Blog V4.")

        service.capture_error = ""
        failure = service._rtl_notification_failure_locked(1000.0)
        self.assertIsNotNone(failure)
        self.assertEqual(failure["message"], "RTLSDRBlog Blog V4 has stopped outputting data.")

        service.last_batch_at = 995.0
        self.assertIsNone(service._rtl_notification_failure_locked(1000.0))

    def test_rtl_notification_prefers_usb_disconnected_over_no_data(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.settings = self.web_control.RtlControlSettings(serial="00000001", notify_sdr_failures=True)
        service.iq_file_source_config = None
        service.capture = object()
        service.capture_error = ""
        service.last_batch_at = 900.0
        service.rtl_notification_states = {}
        service.rtl_device_name_cache = {"00000001": "RTLSDRBlog Blog V4"}
        service.devices = lambda: {"devices": []}

        failure = service._rtl_notification_failure_locked(1000.0)

        self.assertIsNotNone(failure)
        self.assertEqual(failure["key"], "rtl:00000001:disconnected")
        self.assertEqual(failure["message"], "RTLSDRBlog Blog V4 is not connected.")

    def test_rtl_notification_device_name_uses_friendly_label_without_serial(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.rtl_device_name_cache = {}

        label = service._rtl_notification_device_name_locked(
            "00000001",
            [{"serial": "00000001", "name": "RTLSDRBlog, Blog V4", "vendor": "Realtek"}],
        )

        self.assertEqual(label, "RTLSDRBlog Blog V4")
        self.assertEqual(service.rtl_device_name_cache["00000001"], "RTLSDRBlog Blog V4")
        self.assertEqual(service._rtl_notification_device_name_locked("00000001", []), "RTLSDRBlog Blog V4")
        self.assertEqual(service._rtl_notification_device_name_locked("12345678", []), "the configured RTL-SDR")

    def test_stream_notification_failures_skip_disabled_categories(self) -> None:
        stream = {
            "id": "stream-1",
            "station": {"callsign": "WXN99"},
            "notifications": {"icecast_failures": False, "soundcard_failures": False},
        }
        snapshot = {
            "id": "stream-1",
            "status": "needs-attention",
            "outputs": [
                {
                    "id": "icecast-1",
                    "type": "icecast",
                    "status": "needs-attention",
                    "icecast": {"host": "example.test", "port": 8000, "mount": "/WXN99.mp3"},
                }
            ],
        }

        self.assertEqual(
            self.web_control.stream_notification_failures(
                stream,
                snapshot,
                self.web_control.stream_notification_settings_from_stream(stream),
            ),
            [],
        )

    def test_stream_notification_failures_include_enabled_soundcard_failure(self) -> None:
        stream = {
            "id": "stream-1",
            "station": {"callsign": "WXN99"},
            "notifications": {"icecast_failures": False, "soundcard_failures": True},
        }
        snapshot = {
            "id": "stream-1",
            "outputs": [
                {
                    "id": "soundcard-1",
                    "type": "soundcard",
                    "status": "needs-attention",
                    "soundcard": {"display_name": "Yeti X"},
                }
            ],
        }

        failures = self.web_control.stream_notification_failures(
            stream,
            snapshot,
            self.web_control.stream_notification_settings_from_stream(stream),
        )

        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["key"], "stream-1:soundcard:soundcard-1")
        self.assertIn("sound card Yeti X is not connected", failures[0]["message"])
        self.assertEqual(
            failures[0]["target_path"],
            "/?view=stream_output&stream=stream-1&output=soundcard-1",
        )

    def test_soundcard_notification_name_does_not_expose_stable_id(self) -> None:
        self.assertEqual(
            self.web_control.soundcard_notification_name(
                {"soundcard": {"stable_id": "alsa:usb:Generic_USB_Audio-00", "display_name": "Yeti X, USB"}}
            ),
            "Yeti X, USB",
        )
        self.assertEqual(
            self.web_control.soundcard_notification_name({"soundcard": {"stable_id": "alsa:usb:Generic_USB_Audio-00"}}),
            "the configured sound card",
        )

    def test_soundcard_name_cache_backfills_stream_config_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            service = object.__new__(self.web_control.RtlControlService)
            service.lock = self.web_control.threading.RLock()
            service.soundcard_names_path = Path(temp_dir) / "soundcard-devices.json"
            service.streams_directory = Path(temp_dir) / "streams"
            service.preview_streams = {}
            service.soundcard_name_cache = {
                "alsa:usb:046d:0aaf:yeti-x": "Yeti X, USB",
            }
            service.streams = [
                {
                    "id": "stream-1",
                    "station": {"callsign": "WXN99"},
                    "outputs": [
                        {
                            "id": "soundcard-1",
                            "type": "soundcard",
                            "soundcard": {
                                "stable_id": "alsa:usb:046d:0aaf:yeti-x",
                                "channel_mode": "left",
                                "volume": 1.0,
                                "sample_rate": 48000,
                            },
                        }
                    ],
                }
            ]

            service._enrich_stream_soundcard_names_from_cache_locked()

            soundcard = service.streams[0]["outputs"][0]["soundcard"]
            self.assertEqual(soundcard["display_name"], "Yeti X, USB")
            saved = self.web_control.load_stream_configs(service.streams_directory)
            self.assertEqual(saved[0]["outputs"][0]["soundcard"]["display_name"], "Yeti X, USB")

    def test_stream_notification_settings_include_recorded_eas_alerts(self) -> None:
        settings = self.web_control.validate_stream_notification_payload(
            {
                "icecast_failures": True,
                "soundcard_failures": True,
                "recorded_eas_alerts": True,
            }
        )

        self.assertTrue(settings.icecast_failures)
        self.assertTrue(settings.soundcard_failures)
        self.assertTrue(settings.recorded_eas_alerts)

    def test_eas_alert_notification_message_uses_same_lookup_data(self) -> None:
        stream = {"station": {"callsign": "WXN99"}}
        alert = {
            "event_type": "SVR",
            "fips_codes": ["026005", "026139"],
            "expires_at_utc": "2026-07-26T20:30:00Z",
            "start_time_utc": "2026-07-26T20:00:00Z",
            "raw_same_header": "ZCZC-WXR-SVR-026005-026139+0030-2072000-KGRR/NWS-",
            "file_path": "/tmp/alert.wav",
        }

        message = self.web_control.eas_alert_notification_message(stream, alert)

        self.assertIn("Severe Thunderstorm Warning received on WXN99", message)
        self.assertIn("Allegan, MI", message)
        self.assertIn("Ottawa, MI", message)
        self.assertIn("until", message)

    def test_eas_alert_notification_id_is_independent_of_index_position(self) -> None:
        alert = {
            "event_type": "TOR",
            "start_time_utc": "2026-07-26T20:00:00Z",
            "expires_at_utc": "2026-07-26T20:30:00Z",
            "raw_same_header": "ZCZC-WXR-TOR-026139+0030-2072000-KGRR/NWS-",
            "file_path": "/tmp/alert.wav",
        }

        self.assertNotEqual(
            self.web_control.eas_alert_id(alert, 0),
            self.web_control.eas_alert_id(alert, 1),
        )
        self.assertEqual(
            self.web_control.eas_alert_notification_id(alert),
            self.web_control.eas_alert_notification_id(dict(alert)),
        )

    def test_stream_test_mode_same_header_uses_required_values(self) -> None:
        header = self.web_control.stream_test_mode_same_header()

        self.assertIn("-DMO-999000+", header)
        self.assertIn("-NWRSTMGR-", header)

    def test_synthetic_test_mode_source_generates_complex_iq(self) -> None:
        source = self.web_control.SyntheticNwrTestModeSource(sample_rate=self.web_control.IQ_SAMPLE_RATE)
        iq = source.process(480)

        self.assertEqual(iq.dtype, self.web_control.np.complex64)
        self.assertEqual(iq.shape, (480,))
        self.assertGreater(float(self.web_control.np.mean(self.web_control.np.abs(iq))), 0.0)
        self.assertTrue(source.queue_same_test())
        self.assertFalse(source.queue_same_test())
        self.assertTrue(source.snapshot()["same_active"])

    def test_synthetic_test_mode_source_demodulates_at_normal_level(self) -> None:
        source = self.web_control.SyntheticNwrTestModeSource(sample_rate=self.web_control.IQ_SAMPLE_RATE)
        demodulator = self.web_control.ComplexNfmDemodulator()
        effects = self.web_control.AudioEffectsProcessor(self.web_control.RECEIVER_AUDIO_CONFIG)
        frames = []
        for _ in range(int(1.0 / self.web_control.STREAM_FRAME_SECONDS)):
            iq = source.process(self.web_control.STREAM_FRAME_SAMPLES)
            frames.append(effects.process(demodulator.process(iq)))
        audio = self.web_control.np.concatenate(frames)

        self.assertGreater(float(self.web_control.np.max(self.web_control.np.abs(audio))), 0.15)
        self.assertGreater(self.web_control.rms_float(audio), 0.04)
        self.assertLess(float(self.web_control.np.mean(self.web_control.np.abs(audio) >= 1.0)), 0.001)

    def test_synthetic_test_mode_signal_change_ramps(self) -> None:
        source = self.web_control.SyntheticNwrTestModeSource(sample_rate=self.web_control.IQ_SAMPLE_RATE)
        source.set_signal_dbfs(-80.0)
        source.process(self.web_control.STREAM_FRAME_SAMPLES)
        snapshot = source.snapshot()

        self.assertEqual(snapshot["signal_dbfs"], -80.0)
        self.assertGreater(snapshot["current_signal_dbfs"], -80.0)
        self.assertLess(snapshot["current_signal_dbfs"], self.web_control.STREAM_TEST_MODE_DEFAULT_SIGNAL_DBFS)

    def test_synthetic_test_mode_signal_can_be_set_immediately(self) -> None:
        source = self.web_control.SyntheticNwrTestModeSource(sample_rate=self.web_control.IQ_SAMPLE_RATE)
        source.set_signal_dbfs(-60.0, immediate=True)
        source.process(self.web_control.STREAM_FRAME_SAMPLES)
        snapshot = source.snapshot()

        self.assertEqual(snapshot["signal_dbfs"], -60.0)
        self.assertEqual(snapshot["current_signal_dbfs"], -60.0)

    def test_test_mode_iq_crossfade_does_not_overdrive_overlap(self) -> None:
        real = self.web_control.np.ones(480, dtype=self.web_control.np.complex64)
        test = self.web_control.np.ones(480, dtype=self.web_control.np.complex64)
        real_weight = self.web_control.np.full(480, 0.5, dtype=self.web_control.np.float32)
        test_weight = self.web_control.np.full(480, 0.5, dtype=self.web_control.np.float32)

        mixed = self.web_control.mix_test_mode_iq_crossfade(real, test, real_weight, test_weight)
        mixed_power = float(self.web_control.np.mean(self.web_control.np.abs(mixed) ** 2))

        self.assertLessEqual(mixed_power, 0.5001)

    def test_synthetic_test_mode_stop_during_same_finishes_before_ready(self) -> None:
        source = self.web_control.SyntheticNwrTestModeSource(sample_rate=self.web_control.IQ_SAMPLE_RATE)
        self.assertTrue(source.queue_same_test())
        stopping = source.request_stop()

        self.assertTrue(stopping["same_active"])
        self.assertFalse(stopping["stop_ready"])

        for _ in range(int(7.0 / self.web_control.STREAM_FRAME_SECONDS)):
            source.process(self.web_control.STREAM_FRAME_SAMPLES)
        self.assertEqual(source.alert_segment[0], "attention")

        for _ in range(int(20.0 / self.web_control.STREAM_FRAME_SECONDS)):
            source.process(self.web_control.STREAM_FRAME_SAMPLES)
            snapshot = source.snapshot()
            if snapshot["stop_ready"]:
                break

        self.assertFalse(snapshot["same_active"])
        self.assertTrue(snapshot["stop_ready"])

    def test_stream_test_mode_start_cannot_steal_existing_client_session(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = threading.RLock()
        service.streams = [{"id": "stream-1", "enabled": True, "station": {"callsign": "WXN99"}}]
        service.stream_test_mode = None

        class Worker:
            def __init__(self) -> None:
                self.active = False
                self.signal_dbfs = -20.0

            def set_test_mode(self, enabled, signal_dbfs=-20.0):
                self.active = bool(enabled)
                self.signal_dbfs = float(signal_dbfs)
                return self.test_mode_snapshot()

            def test_mode_snapshot(self):
                return {
                    "active": self.active,
                    "signal_dbfs": self.signal_dbfs,
                    "same_active": False,
                    "stopping": False,
                }

        service.stream_workers = {"stream-1": Worker()}
        service._sync_stream_workers_locked = lambda: None

        first = service.update_stream_test_mode(
            {"action": "start", "stream_id": "stream-1", "client_id": "client-a"}
        )

        self.assertTrue(first["test_mode"]["active"])
        self.assertEqual(first["test_mode"]["client_id"], "client-a")
        with self.assertRaisesRegex(ValueError, "owned by another browser"):
            service.update_stream_test_mode(
                {"action": "start", "stream_id": "stream-1", "client_id": "client-b"}
            )
        self.assertEqual(service.stream_test_mode["client_id"], "client-a")

    def test_interface_is_up_accepts_unknown_operstate_with_up_flag(self) -> None:
        def fake_read_text(path, encoding=None):
            text_path = str(path)
            if text_path.endswith("/operstate"):
                return "unknown\n"
            if text_path.endswith("/flags"):
                return "0x1091\n"
            raise OSError("unexpected path")

        with patch.object(self.web_control.Path, "read_text", fake_read_text):
            self.assertTrue(self.web_control.interface_is_up("tailscale0"))

    def test_dns_ptr_response_parser_decodes_compressed_ptr_name(self) -> None:
        query_id = 0x1234
        qname = self.web_control.dns_encode_name("20.206.113.100.in-addr.arpa")
        target = self.web_control.dns_encode_name("max-thinkpad.example.ts.net")
        response = (
            struct.pack("!HHHHHH", query_id, 0x8180, 1, 1, 0, 0)
            + qname
            + struct.pack("!HH", self.web_control.DNS_TYPE_PTR, self.web_control.DNS_CLASS_IN)
            + b"\xc0\x0c"
            + struct.pack("!HHIH", self.web_control.DNS_TYPE_PTR, self.web_control.DNS_CLASS_IN, 30, len(target))
            + target
        )
        offset = 12
        _question_name, offset = self.web_control.dns_decode_name(response, offset)
        offset += 4
        _answer_name, offset = self.web_control.dns_decode_name(response, offset)
        _answer_type, _answer_class, _ttl, rdlength = struct.unpack("!HHIH", response[offset : offset + 10])
        offset += 10

        decoded, _unused = self.web_control.dns_decode_name(response, offset)

        self.assertEqual(rdlength, len(target))
        self.assertEqual(decoded, "max-thinkpad.example.ts.net")

    def test_notification_access_urls_include_tailscale_magicdns_when_detected(self) -> None:
        with (
            patch.object(self.web_control.socket, "if_nameindex", return_value=[(1, "tailscale0"), (2, "wlp9s0")]),
            patch.object(self.web_control, "interface_is_up", return_value=True),
            patch.object(self.web_control, "interface_is_physical_or_tailscale", return_value=True),
            patch.object(
                self.web_control,
                "interface_ipv4_address",
                side_effect=lambda name: "100.113.206.20" if name == "tailscale0" else "192.168.1.79",
            ),
            patch.object(self.web_control, "tailscale_magicdns_name", return_value="max-thinkpad.example.ts.net"),
        ):
            options = self.web_control.notification_access_url_options("0.0.0.0", 8080)

        self.assertEqual(options[0]["id"], "tailscale-magicdns:tailscale0:max-thinkpad.example.ts.net")
        self.assertEqual(options[0]["url"], "http://max-thinkpad.example.ts.net:8080")
        self.assertIn("Tailscale MagicDNS", options[0]["label"])
        self.assertTrue(any(option["url"] == "http://100.113.206.20:8080" for option in options))

    def test_notification_access_urls_prefer_default_route_after_tailscale(self) -> None:
        with (
            patch.object(
                self.web_control.socket,
                "if_nameindex",
                return_value=[(1, "wlp9s0"), (2, "enp0s31f6")],
            ),
            patch.object(self.web_control, "interface_is_up", return_value=True),
            patch.object(self.web_control, "interface_is_physical_or_tailscale", return_value=True),
            patch.object(
                self.web_control,
                "interface_ipv4_address",
                side_effect=lambda name: "192.168.1.79" if name == "wlp9s0" else "10.0.0.24",
            ),
            patch.object(self.web_control, "default_route_ipv4_address", return_value="10.0.0.24"),
        ):
            options = self.web_control.notification_access_url_options("0.0.0.0", 8080)

        self.assertEqual(options[0]["url"], "http://10.0.0.24:8080")
        self.assertEqual(options[0]["interface"], "enp0s31f6")

    def test_notification_access_urls_use_default_route_when_no_interfaces_match(self) -> None:
        with (
            patch.object(self.web_control.socket, "if_nameindex", return_value=[]),
            patch.object(self.web_control, "default_route_ipv4_address", return_value="172.20.10.2"),
        ):
            options = self.web_control.notification_access_url_options("0.0.0.0", 8080)

        self.assertEqual(
            options,
            [
                {
                    "id": "interface:default:172.20.10.2",
                    "label": "Default network route (172.20.10.2)",
                    "url": "http://172.20.10.2:8080",
                    "interface": "default",
                }
            ],
        )

    def test_setup_state_reports_setup_required_before_account_exists(self) -> None:
        handler = object.__new__(self.web_control.RtlControlHandler)
        handler.current_account = None
        handler.service = types.SimpleNamespace(accounts=types.SimpleNamespace(has_account=lambda: False))
        sent = {}
        handler._send_json = lambda payload, status=self.web_control.HTTPStatus.OK: sent.update(payload=payload, status=status)

        self.assertFalse(handler._auth_ok_or_setup_response("/api/setup-state", "GET"))
        self.assertEqual(sent["payload"], {"setup_required": True})

    def test_setup_state_reports_setup_complete_after_owner_exists(self) -> None:
        handler = object.__new__(self.web_control.RtlControlHandler)
        handler.current_account = None
        handler.service = types.SimpleNamespace(accounts=types.SimpleNamespace(has_account=lambda: True))
        sent = {}
        handler._send_json = lambda payload, status=self.web_control.HTTPStatus.OK: sent.update(payload=payload, status=status)

        self.assertFalse(handler._auth_ok_or_setup_response("/api/setup-state", "GET"))
        self.assertEqual(sent["payload"], {"setup_required": False})

    def test_password_hash_does_not_store_plaintext(self) -> None:
        encoded = self.web_control.hash_account_password("StrongPass!1")

        self.assertTrue(encoded.startswith("scrypt$v=1$"))
        self.assertIn("$n=16384$", encoded)
        self.assertIn("$r=8$", encoded)
        self.assertIn("$p=1$", encoded)
        self.assertIn("$dklen=32$", encoded)
        self.assertNotIn("StrongPass!1", encoded)
        self.assertFalse(self.web_control.account_password_hash_needs_upgrade(encoded))
        self.assertTrue(self.web_control.verify_account_password("StrongPass!1", encoded))
        self.assertFalse(self.web_control.verify_account_password("WrongPass!1", encoded))

    def test_legacy_password_hash_verifies_and_upgrades_on_login(self) -> None:
        password = "StrongPass!1"
        salt = b"legacy-salt"
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=1024,
            r=8,
            p=1,
            dklen=32,
        )
        legacy_hash = "scrypt$1024$8$1${}${}".format(
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        )
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            with store._connect() as connection:
                connection.execute(
                    "INSERT INTO accounts (id, username, password, must_change_password) VALUES (1, ?, ?, 0)",
                    ("admin", legacy_hash),
                )
                connection.commit()

            self.assertTrue(self.web_control.account_password_hash_needs_upgrade(legacy_hash))
            self.assertTrue(store.verify_basic_credentials("admin", password))
            with store._connect() as connection:
                upgraded = connection.execute("SELECT password FROM accounts WHERE id = 1").fetchone()["password"]

            self.assertNotEqual(upgraded, legacy_hash)
            self.assertTrue(upgraded.startswith("scrypt$v=1$"))
            self.assertFalse(self.web_control.account_password_hash_needs_upgrade(upgraded))
            self.assertTrue(self.web_control.verify_account_password(password, upgraded))

    def test_handler_validates_basic_auth_header_against_account_store(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            store.create_admin("admin", "StrongPass!1", "StrongPass!1")
            handler = object.__new__(self.web_control.RtlControlHandler)
            handler.service = types.SimpleNamespace(accounts=store, auth_sessions=self.web_control.AuthSessionStore())
            token = base64.b64encode(b"admin:StrongPass!1").decode("ascii")
            handler.headers = {"Authorization": f"Basic {token}"}

            self.assertTrue(handler._basic_auth_valid())

            bad_token = base64.b64encode(b"admin:wrong").decode("ascii")
            handler.headers = {"Authorization": f"Basic {bad_token}"}
            self.assertFalse(handler._basic_auth_valid())

    def test_session_cookie_authenticates_without_password_verification(self) -> None:
        sessions = self.web_control.AuthSessionStore()
        token = sessions.create()
        account = self.web_control.AccountRecord(
            id=1,
            username="admin",
            role=self.web_control.ACCOUNT_ROLE_OWNER,
            must_change_password=False,
            created_at=0.0,
            last_accessed_at=None,
        )
        accounts = types.SimpleNamespace(
            has_account=lambda: True,
            account_by_id=lambda account_id: account if account_id == 1 else None,
            touch_account_access=lambda _account_id: None,
            verify_basic_credentials=lambda _username, _password: self.fail("password hash should not be checked"),
        )
        handler = object.__new__(self.web_control.RtlControlHandler)
        handler.service = types.SimpleNamespace(accounts=accounts, auth_sessions=sessions)
        handler.headers = {"Cookie": f"{self.web_control.AUTH_SESSION_COOKIE_NAME}={token}"}

        self.assertTrue(handler._auth_ok_or_setup_response("/", "GET"))

    def test_successful_basic_auth_prepares_session_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            store.create_admin("admin", "StrongPass!1", "StrongPass!1")
            handler = object.__new__(self.web_control.RtlControlHandler)
            handler.service = types.SimpleNamespace(accounts=store, auth_sessions=self.web_control.AuthSessionStore())
            token = base64.b64encode(b"admin:StrongPass!1").decode("ascii")
            handler.headers = {"Authorization": f"Basic {token}"}

            self.assertTrue(handler._auth_ok_or_setup_response("/", "GET"))
            cookie = getattr(handler, "_pending_auth_session_cookie", "")
            self.assertIn(f"{self.web_control.AUTH_SESSION_COOKIE_NAME}=", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("SameSite=Lax", cookie)

    def test_owner_can_create_read_only_account_with_temporary_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            store.create_admin("owner", "StrongPass!1", "StrongPass!1")

            account, secret = store.create_account("listener_1", read_only=True)

            self.assertEqual(account["role"], self.web_control.ACCOUNT_ROLE_READ_ONLY)
            self.assertTrue(account["must_change_password"])
            self.assertGreaterEqual(len(secret), 12)
            record = store.verify_basic_account("listener_1", secret)
            self.assertIsNotNone(record)
            assert record is not None
            self.assertTrue(record.is_read_only)
            self.assertTrue(record.must_change_password)

    def test_password_change_clears_must_change_password(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            store.create_admin("owner", "StrongPass!1", "StrongPass!1")
            account, secret = store.create_account("operator", read_only=False)

            updated = store.change_password(account["id"], secret, "NewStrong!1", "NewStrong!1")

            self.assertFalse(updated["must_change_password"])
            self.assertIsNotNone(store.verify_basic_account("operator", "NewStrong!1"))
            self.assertIsNone(store.verify_basic_account("operator", secret))

    def test_owner_can_toggle_and_delete_non_owner_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            store.create_admin("owner", "StrongPass!1", "StrongPass!1")
            owner = store.verify_basic_account("owner", "StrongPass!1")
            assert owner is not None
            account, _secret = store.create_account("operator", read_only=False)

            updated = store.set_read_only(account["id"], True)
            self.assertTrue(updated["read_only"])
            removed = store.delete_account(account["id"], owner.id)

            self.assertEqual(removed["username"], "operator")
            self.assertIsNone(store.account_by_id(account["id"]))

    def test_session_reflects_read_only_role_changes_without_relogin(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = self.web_control.AccountStore(Path(tempdir) / "accounts.db")
            store.create_admin("owner", "StrongPass!1", "StrongPass!1")
            account, _secret = store.create_account("operator", read_only=True)
            sessions = self.web_control.AuthSessionStore()
            token = sessions.create(account["id"])

            current = sessions.validate(token, store)
            self.assertIsNotNone(current)
            assert isinstance(current, self.web_control.AccountRecord)
            self.assertTrue(current.is_read_only)

            store.set_read_only(account["id"], False)
            current = sessions.validate(token, store)
            self.assertIsNotNone(current)
            assert isinstance(current, self.web_control.AccountRecord)
            self.assertFalse(current.is_read_only)
            self.assertEqual(current.role, self.web_control.ACCOUNT_ROLE_ADMIN)

            store.set_read_only(account["id"], True)
            current = sessions.validate(token, store)
            self.assertIsNotNone(current)
            assert isinstance(current, self.web_control.AccountRecord)
            self.assertTrue(current.is_read_only)

    def test_iq_download_tracking_is_per_download_and_account(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = threading.RLock()
        service.iq_recording_downloads = set()
        service.iq_recording_download_accounts = {}
        service.iq_recording_download_recordings = {}
        service.aborted_iq_recording_downloads = set()

        first = service.begin_iq_recording_download("rec-1", account_id=2)
        second = service.begin_iq_recording_download("rec-1", account_id=3)

        self.assertIn("rec-1", service.iq_recording_downloads)
        service.finish_iq_recording_download(first)
        self.assertIn("rec-1", service.iq_recording_downloads)
        service.finish_iq_recording_download(second)
        self.assertNotIn("rec-1", service.iq_recording_downloads)

    def test_revoke_account_long_lived_resources_targets_account_owned_resources(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = threading.RLock()
        service.monitor_accounts_by_client = {"monitor-a": 2, "monitor-b": 3}
        service.receiver_accounts_by_client = {"receiver-a": 2, "receiver-b": 3}
        service.iq_recording_download_accounts = {"download-a": 2, "download-b": 3}
        service.iq_recording_download_recordings = {"download-a": "rec-a", "download-b": "rec-b"}
        service.aborted_iq_recording_downloads = set()
        service.iq_recorder_account_id = 2
        stopped = []

        class Recorder:
            def stop(self) -> None:
                stopped.append("recorder")

        service.iq_recorder = Recorder()
        service.stop_monitor = lambda payload: stopped.append(("monitor", payload["client_id"]))
        service.stop_receiver = lambda payload: stopped.append(("receiver", payload["client_id"]))

        service.revoke_account_long_lived_resources(
            2,
            stop_webrtc=True,
            abort_downloads=True,
            stop_iq_recording=True,
        )

        self.assertIn(("monitor", "monitor-a"), stopped)
        self.assertNotIn(("monitor", "monitor-b"), stopped)
        self.assertIn(("receiver", "receiver-a"), stopped)
        self.assertNotIn(("receiver", "receiver-b"), stopped)
        self.assertIn("download-a", service.aborted_iq_recording_downloads)
        self.assertNotIn("download-b", service.aborted_iq_recording_downloads)
        self.assertIn("recorder", stopped)
        self.assertIsNone(service.iq_recorder)
        self.assertIsNone(service.iq_recorder_account_id)

    def test_read_only_downgrade_can_stop_iq_recorder_without_stopping_allowed_sessions(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = threading.RLock()
        service.monitor_accounts_by_client = {"monitor-a": 2}
        service.receiver_accounts_by_client = {"receiver-a": 2}
        service.iq_recording_download_accounts = {"download-a": 2}
        service.iq_recording_download_recordings = {"download-a": "rec-a"}
        service.aborted_iq_recording_downloads = set()
        service.iq_recorder_account_id = 2
        stopped = []

        class Recorder:
            def stop(self) -> None:
                stopped.append("recorder")

        service.iq_recorder = Recorder()
        service.stop_monitor = lambda payload: stopped.append(("monitor", payload["client_id"]))
        service.stop_receiver = lambda payload: stopped.append(("receiver", payload["client_id"]))

        service.revoke_account_long_lived_resources(2, stop_iq_recording=True)

        self.assertEqual(stopped, ["recorder"])
        self.assertEqual(service.aborted_iq_recording_downloads, set())

    def test_read_only_accounts_cannot_delete_eas_alerts(self) -> None:
        handler = object.__new__(self.web_control.RtlControlHandler)

        self.assertFalse(handler._read_only_request_allowed("/api/eas-alert", "DELETE"))
        self.assertFalse(handler._read_only_request_allowed("/api/eas-alert-delete", "POST"))
        self.assertTrue(handler._read_only_request_allowed("/api/eas-alerts", "GET"))
        self.assertTrue(handler._read_only_request_allowed("/api/eas-alert-export", "GET"))

    def test_read_only_accounts_cannot_mutate_streams_or_iq_recordings(self) -> None:
        handler = object.__new__(self.web_control.RtlControlHandler)
        blocked = [
            ("POST", "/api/streams"),
            ("PATCH", "/api/streams"),
            ("DELETE", "/api/streams"),
            ("POST", "/api/stream-output"),
            ("PATCH", "/api/stream-output"),
            ("PUT", "/api/stream-output"),
            ("DELETE", "/api/stream-output"),
            ("POST", "/api/stream-soundcard-preview"),
            ("DELETE", "/api/stream-soundcard-preview"),
            ("POST", "/api/stream-test-mode"),
            ("POST", "/api/icecast-auth"),
            ("PATCH", "/api/fallback-settings"),
            ("PUT", "/api/fallback-settings"),
            ("PATCH", "/api/eas-recording"),
            ("PATCH", "/api/audio-effects"),
            ("PATCH", "/api/stream-notifications"),
            ("PATCH", "/api/settings"),
            ("PUT", "/api/settings"),
            ("POST", "/api/rtl-reset"),
            ("POST", "/api/soundcard-reset"),
            ("POST", "/api/iq-test-source"),
            ("POST", "/api/iq-test-source/stop"),
            ("POST", "/api/iq-test-source/seek"),
            ("POST", "/api/iq-recorder/start"),
            ("POST", "/api/iq-recorder/stop"),
            ("DELETE", "/api/iq-recording"),
        ]

        for method, path in blocked:
            with self.subTest(method=method, path=path):
                self.assertFalse(handler._read_only_request_allowed(path, method))

        allowed = [
            ("GET", "/notification"),
            ("GET", "/api/streams"),
            ("POST", "/api/monitor/start"),
            ("POST", "/api/monitor/stop"),
            ("GET", "/api/iq-recordings"),
            ("GET", "/api/iq-recording-download"),
        ]
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(handler._read_only_request_allowed(path, method))

    def test_read_only_stream_status_redacts_secrets_and_filters_monitoring(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = self.web_control.threading.RLock()
        service.streams = [
            {
                "id": "stream-1",
                "enabled": True,
                "station": {"callsign": "WXN99", "frequency": "162.475"},
                "outputs": [
                    {
                        "id": "out-1",
                        "enabled": True,
                        "type": "icecast",
                        "icecast": {
                            "host": "icecast.example",
                            "port": 8000,
                            "username": "source",
                            "password": "secret",
                            "mount": "/WXN99.mp3",
                            "format": "mp3",
                            "sample_rate": 22050,
                            "bitrate": 64,
                        },
                        "auth_signature": "secret-signature",
                    }
                ],
                "audio": {"volume": {"multiplier": 2}},
            }
        ]
        service.monitor_streams_by_client = {"client-owned": "stream-1", "client-other": "stream-2"}
        service.monitor_accounts_by_client = {"client-owned": 7, "client-other": 8}

        status = service.stream_status(read_only=True, account_id=7)
        stream = status["streams"][0]
        output = stream["outputs"][0]

        self.assertEqual(status["monitoring"], {"client-owned": "stream-1"})
        self.assertNotIn("audio", stream)
        self.assertNotIn("password", output["icecast"])
        self.assertNotIn("username", output["icecast"])
        self.assertNotIn("auth_signature", output)
        self.assertEqual(output["icecast"]["mount"], "/WXN99.mp3")

    def test_read_only_status_hides_test_mode_state(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.lock = self.web_control.threading.RLock()
        service.capture = None
        service.settings = self.web_control.RtlControlSettings()
        service._effective_settings_locked = lambda: service.settings
        service.raw_fanout = None
        service.intermediate_fanout = None
        service.iq_file_source_config = None
        service.streams = []
        service.fallback_settings = self.web_control.WebFallbackSettings()
        service.capture_error = None
        service.last_batch_at = None
        service.received_chunks = 0
        service.received_bytes = 0
        service.development_iq_sources_enabled = False
        service.log_handler = type("LogHandler", (), {"snapshot": lambda self: []})()
        now = self.web_control.time.time()
        service.stream_test_mode = {
            "stream_id": "stream-1",
            "client_id": "client-a",
            "signal_dbfs": -20.0,
            "started_at": now,
            "heartbeat_at": now,
            "last_same_at": now,
        }
        service.stream_workers = {}
        service._active_streams_locked = lambda: []
        service._active_eas_recorders_locked = lambda: []
        service._recent_eas_alerts_locked = lambda: []
        service._storage_status_snapshot_locked = lambda: {}
        service._iq_recorder_status_locked = lambda: {}

        status = service.status(read_only=True)

        self.assertEqual(status["test_mode"], {"active": False})

    def test_webrtc_client_controls_require_matching_account(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)
        service.monitor_streams_by_client = {"client-1": "stream-1"}
        service.monitor_accounts_by_client = {"client-1": 10}
        service.receiver_workers = {}
        service.receiver_accounts_by_client = {}

        service._ensure_webrtc_client_owner_locked("client-1", 10, require_existing=True)
        with self.assertRaisesRegex(ValueError, "different account"):
            service._ensure_webrtc_client_owner_locked("client-1", 11, require_existing=True)

    def test_read_only_iq_recorder_status_is_redacted(self) -> None:
        service = object.__new__(self.web_control.RtlControlService)

        redacted = service.redacted_iq_recorder_status()

        self.assertFalse(redacted["active"])
        self.assertEqual(redacted["status"], "idle")
        self.assertEqual(redacted["sample_rates"], [])
        self.assertNotIn("bytes_written", redacted)
        self.assertNotIn("elapsed_seconds", redacted)

    def test_iq_recorder_default_duration_is_manual_stop(self) -> None:
        self.assertEqual(self.web_control.IQ_RECORDER_DEFAULT_DURATION_SECONDS, 0)


if __name__ == "__main__":
    unittest.main()
