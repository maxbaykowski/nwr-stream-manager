from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
import unittest

from test_auth import load_web_control_module
from test_restart import RestartPageTests


class RoleBoundaryTests(unittest.TestCase):
    """Read-only accounts can't reach privileged pages or actions, and administrators can't reach the owner's."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]

    def send(self, user, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        headers = {"Authorization": "Basic " + base64.b64encode(":".join(user).encode()).decode()}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response, response.read().decode("utf-8")

    def test_administrators_cant_reach_owner_only_settings(self) -> None:
        admin = ("helper", "password2")
        for method, path, body in (
            ("GET", "/api/accounts", None),
            ("POST", "/api/accounts", {"username": "someone", "read_only": False}),
            ("POST", "/api/account-reset-password", {"account_id": 1}),
            ("PATCH", "/api/account-read-only", {"account_id": 1, "read_only": True}),
            ("DELETE", "/api/accounts?account_id=1", None),
            ("GET", "/api/advanced-settings", None),
            ("PATCH", "/api/advanced-settings", {"port": 9999}),
        ):
            with self.subTest(method=method, path=path):
                response, body = self.send(admin, method, path, body)
                self.assertEqual(response.status, 403, body)
                self.assertEqual(json.loads(body)["error"], "owner account is required")
        self.assertEqual(len(self.accounts.list_accounts()), 3)

    def test_read_only_accounts_cant_change_anything(self) -> None:
        viewer = ("viewer", "password3")
        for method, path, body in (
            ("GET", "/api/notification-settings", None),
            ("GET", "/api/remote-sdr", None),
            ("GET", "/api/general-settings", None),
            ("PATCH", "/api/general-settings", {"instance_name": "Taken over"}),
            ("POST", "/api/restart", {"instance": "run1"}),
            ("POST", "/api/remote-sdr/pairing/start", {}),
            ("POST", "/api/iq-recorder/start", {}),
        ):
            with self.subTest(method=method, path=path):
                response, body = self.send(viewer, method, path, body)
                self.assertEqual(response.status, 403, body)
        self.assertEqual(self.requests, [])


class ClientLogTests(unittest.TestCase):
    """What a page sends to the log stays one line, so it can't pass itself off as the server's own lines."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]

    def test_line_breaks_cant_forge_log_lines(self) -> None:
        self.assertEqual(self.wc.one_log_line("a\nb\r\nc d\te"), "a b  c d e")
        forged = "hello\n2026-10-22 09:00:00,000 INFO nwr_stream_manager.service_restart: restart requested by owner"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        auth = "Basic " + base64.b64encode(b"viewer:password3").decode()
        with self.assertLogs("nwr_stream_manager", level="INFO") as logs:
            connection.request(
                "POST", "/api/client-log",
                body=json.dumps({"level": "info", "area": "page\nscript", "message": forged}),
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            response.read()
        self.assertEqual(response.status, 200)
        line = next(record.getMessage() for record in logs.records if "client page" in record.getMessage())
        self.assertNotIn("\n", line)
        self.assertIn("client page script from", line)


class LiveAudioFeedbackTests(unittest.TestCase):
    """Reports about live audio count only from whoever is listening to it."""

    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.mixin = sys.modules["nwr_stream_manager.service_live_audio"].LiveAudioServiceMixin

    def service(self):
        service = type("LiveAudioStandIn", (self.mixin,), {})()
        service.lock = threading.Lock()
        service.monitor_streams_by_client = {"listener-1": "stream-1"}
        service.monitor_accounts_by_client = {"listener-1": 2}
        service.receiver_workers = {}
        service.receiver_accounts_by_client = {}
        service.live_audio_feedback_by_client = {}
        return service

    def report(self, service, client_id, account_id, mode="monitor"):
        service.record_live_audio_feedback(
            client_id, mode, "live audio throughput sample", {"receive_kbps": 12.0}, account_id
        )

    def test_only_the_listener_can_report_on_their_live_audio(self) -> None:
        service = self.service()
        self.report(service, "listener-1", 3)
        self.assertEqual(service.live_audio_feedback_by_client, {})
        self.report(service, "listener-1", 2)
        self.assertEqual(service.live_audio_feedback_by_client[("monitor", "listener-1")]["network_receive_kbps"], 12.0)

    def test_made_up_sessions_are_ignored(self) -> None:
        service = self.service()
        for i in range(100):
            self.report(service, f"made-up-{i}", 2)
        self.report(service, "listener-1", 2, mode="receiver")
        self.assertEqual(service.live_audio_feedback_by_client, {})


    def test_status_of_someone_elses_live_audio_reads_as_none(self) -> None:
        service = self.service()
        service.stream_workers = {}
        self.assertEqual(service.monitor_status("listener-1", 2)["stream_id"], "stream-1")
        self.assertEqual(service.monitor_status("listener-1", 3), {"client_id": "listener-1", "stream_id": "", "stats": {}})
        receiver = type("Receiver", (), {"snapshot": lambda self: {"client_id": "tuner-1", "frequency_hz": 162425000}})()
        service.receiver_workers = {"tuner-1": receiver}
        service.receiver_accounts_by_client = {"tuner-1": 2}
        self.assertEqual(service.receiver_status("tuner-1", 2)["frequency_hz"], 162425000)
        self.assertEqual(service.receiver_status("tuner-1", 3), {"client_id": "tuner-1", "playing": False})


if __name__ == "__main__":
    unittest.main()
