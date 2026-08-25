from __future__ import annotations

import importlib.util
import base64
import hashlib
import sys
import tempfile
import types
import unittest
import threading
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
            ("POST", "/api/icecast-auth"),
            ("PATCH", "/api/fallback-settings"),
            ("PUT", "/api/fallback-settings"),
            ("PATCH", "/api/eas-recording"),
            ("PATCH", "/api/audio-effects"),
            ("PATCH", "/api/settings"),
            ("PUT", "/api/settings"),
            ("POST", "/api/rtl-reset"),
            ("POST", "/api/soundcard-reset"),
            ("POST", "/api/iq-recorder/start"),
            ("POST", "/api/iq-recorder/stop"),
            ("DELETE", "/api/iq-recording"),
        ]

        for method, path in blocked:
            with self.subTest(method=method, path=path):
                self.assertFalse(handler._read_only_request_allowed(path, method))

        allowed = [
            ("GET", "/api/streams"),
            ("POST", "/api/monitor/start"),
            ("POST", "/api/monitor/stop"),
            ("GET", "/api/iq-recordings"),
            ("GET", "/api/iq-recording-download"),
        ]
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(handler._read_only_request_allowed(path, method))

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
