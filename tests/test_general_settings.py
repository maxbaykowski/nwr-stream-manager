from __future__ import annotations

import base64
import http.client
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from test_auth import load_web_control_module
from test_page_rendering import StandInService


class InstanceNameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.general = sys.modules["nwr_stream_manager.general_settings"]

    def test_the_default_is_named_after_this_computer(self) -> None:
        with mock.patch.object(self.general.socket, "gethostname", return_value="weatherpi.local"):
            self.assertEqual(self.general.default_instance_name(), "NWR Stream Manager on weatherpi")
        with mock.patch.object(self.general.socket, "gethostname", return_value="x" * 80):
            self.assertEqual(len(self.general.default_instance_name()), 64)
        with mock.patch.object(self.general.socket, "gethostname", return_value=""):
            self.assertEqual(self.general.default_instance_name(), "NWR Stream Manager")

    def test_names_are_tidied_and_limited(self) -> None:
        validate = self.general.validate_instance_name
        self.assertEqual(validate("  Kitchen \t  stream\n box "), "Kitchen stream box")
        self.assertEqual(validate("é" * 64), "é" * 64)
        for bad, message in (("", "Enter an instance name."), ("   ", "Enter an instance name."), ("x" * 65, "up to 64"), ("a\u200bb", "control characters")):
            with self.assertRaisesRegex(ValueError, message):
                validate(bad)

    def test_locations_are_optional_and_limited_like_names(self) -> None:
        validate = self.general.validate_location
        self.assertEqual(validate("  Attic SDR  (Grand Rapids, MI) "), "Attic SDR (Grand Rapids, MI)")
        self.assertEqual(validate("   "), "")
        with self.assertRaisesRegex(ValueError, "The location can be up to 64"):
            validate("x" * 65)

    def test_the_first_start_saves_the_default_and_later_starts_keep_it(self) -> None:
        load = self.general.load_or_initialize_general_settings
        settings = self.general.GeneralSettings
        with tempfile.TemporaryDirectory() as state:
            path = Path(state) / "general.json"
            with mock.patch.object(self.general.socket, "gethostname", return_value="weatherpi"), self.assertLogs("nwr_stream_manager", "INFO"):
                self.assertEqual(load(path), settings("NWR Stream Manager on weatherpi", ""))
            self.assertEqual(json.loads(path.read_text()), {"instance_name": "NWR Stream Manager on weatherpi", "location": ""})
            # A new host name later doesn't change a name that's already set.
            path.write_text(json.dumps({"instance_name": "NWR Stream Manager on weatherpi", "location": "Attic SDR (Grand Rapids, MI)"}))
            with mock.patch.object(self.general.socket, "gethostname", return_value="renamed"):
                self.assertEqual(load(path), settings("NWR Stream Manager on weatherpi", "Attic SDR (Grand Rapids, MI)"))
            # A location that can't be used is left empty, keeping the name.
            path.write_text(json.dumps({"instance_name": "Kitchen", "location": "x" * 65}))
            with self.assertLogs("nwr_stream_manager", "WARNING"):
                self.assertEqual(load(path), settings("Kitchen", ""))
            path.write_text("not json")
            with mock.patch.object(self.general.socket, "gethostname", return_value="renamed"), self.assertLogs("nwr_stream_manager", "WARNING"):
                self.assertEqual(load(path), settings("NWR Stream Manager on renamed", ""))


class DashboardLocationTests(unittest.TestCase):
    def test_the_location_shows_on_the_dashboard_only_once_set(self) -> None:
        load_web_control_module()
        parts = sys.modules["nwr_stream_manager.page_sections"].dashboard_parts
        shown = parts({"location": "Attic SDR (Grand Rapids, MI)", "capture_error": "gone"}, "none", False, 0)
        self.assertEqual(shown["text"]["summary_location"], "Attic SDR (Grand Rapids, MI)")
        # Shown even while the SDR needs attention and its details are hidden.
        self.assertFalse(shown["hidden"]["dashboard_location"])
        self.assertTrue(shown["hidden"]["dashboard_sdr_details"])
        self.assertTrue(parts({}, "none", True, 0)["hidden"]["dashboard_location"])


class UptimeTests(unittest.TestCase):
    CASES = {0: "less than a minute", 59: "less than a minute", 60: "1 minute", 3600: "1 hour", 3660: "1 hour, 1 minute",
             86460: "1 day, 1 minute", 2 * 86400 + 7 * 3600 + 5 * 60 + 59: "2 days, 7 hours, 5 minutes"}

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.sections = sys.modules["nwr_stream_manager.page_sections"]

    def test_uptime_reads_in_days_hours_and_minutes(self) -> None:
        for seconds, text in self.CASES.items():
            self.assertEqual(self.sections.format_uptime(seconds), text, seconds)

    def test_the_page_script_says_it_the_same_way(self) -> None:
        import shutil, subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node isn't installed")
        script = Path(__file__).resolve().parents[1] / "src" / "nwr-stream-manager" / "assets" / "app.js"
        function = re.search(r"function formatUptime[\s\S]*?\n}\n", script.read_text(encoding="utf-8")).group(0)
        program = function + f"console.log(JSON.stringify({json.dumps(list(self.CASES))}.map(formatUptime)));"
        result = subprocess.run([node, "-e", program], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout), list(self.CASES.values()))

    def test_general_settings_shows_the_version_and_uptime_below_its_form(self) -> None:
        page = self.wc.INDEX_HTML
        version = sys.modules["nwr_stream_manager"].__version__
        general = page.index('id="settings_panel_general"')
        version_at = page.index(f"<p>Version: {version}</p>")
        self.assertLess(general, page.index('id="general-result"'))
        self.assertLess(page.index('id="general-result"'), version_at)
        self.assertLess(version_at, page.index('<p>Uptime: <span id="general_uptime">'))
        self.assertLess(page.index('id="general_uptime"'), page.index('id="settings_panel_accounts"'))
        # No longer on the dashboard.
        dashboard = page[page.index('id="view_dashboard"'):page.index('id="dashboard_sdr_details"')]
        self.assertNotIn("Version:", dashboard)
        self.assertNotIn("Uptime:", dashboard)


class InstanceNameInNotificationsTests(unittest.TestCase):
    """Notifications are titled with the instance name, whatever characters it has."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.notifications = sys.modules["nwr_stream_manager.notifications"]
        cls.service_notifications = sys.modules["nwr_stream_manager.service_notifications"]

    def test_titles_that_are_not_plain_ascii_are_encoded_for_ntfy(self) -> None:
        from email.header import decode_header
        encode = self.notifications.ntfy_header_text
        self.assertEqual(encode("Kitchen Pi"), "Kitchen Pi")
        for name in ("Cuisine \u00e9t\u00e9", "Shack \U0001F4FB"):
            encoded = encode(name)
            self.assertTrue(encoded.isascii())
            ((raw, charset),) = decode_header(encoded)
            self.assertEqual(raw.decode(charset), name)

    def sent_titles(self, send):
        sent = []
        service = object.__new__(self.wc.RtlControlService)
        service.general = sys.modules["nwr_stream_manager.general_settings"].GeneralSettings("Shack \U0001F4FB")
        service.lock = threading.RLock()
        service.notification_settings = self.wc.WebNotificationSettings(enabled=True, server_url="https://ntfy.sh", topic="NWRSTMGR-test")
        service._notification_access_url_options = lambda: []
        service.notification_effective_access_url_for_settings = lambda settings: "http://192.0.2.5:8080"
        done = threading.Event()

        def capture(settings, **fields):
            sent.append(fields["title"])
            done.set()

        with mock.patch.object(self.service_notifications, "send_ntfy_notification", capture), self.assertLogs("nwr_stream_manager", "INFO"):
            send(service)
            done.wait(5)
        return sent

    def test_problem_notifications_are_titled_with_the_instance_name(self) -> None:
        titles = self.sent_titles(lambda service: service._send_stream_problem_notification_async({"message": "WXN99 is down."}))
        self.assertEqual(titles, ["Shack \U0001F4FB"])

    def test_the_test_notification_is_titled_with_the_instance_name(self) -> None:
        titles = self.sent_titles(lambda service: service.test_notification_settings({"enabled": True, "server_url": "https://ntfy.sh", "topic": "NWRSTMGR-test"}))
        self.assertEqual(titles, ["Shack \U0001F4FB"])

    def test_the_encoded_title_can_be_sent(self) -> None:
        captured = {}

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(request, timeout=0):
            captured.update(request.header_items())
            # As http.client sends it, which only takes Latin-1.
            for value in captured.values():
                value.encode("latin-1")
            return FakeResponse()

        settings = self.wc.WebNotificationSettings(enabled=True, server_url="https://ntfy.sh", topic="NWRSTMGR-test")
        with mock.patch.object(self.notifications, "urlopen", fake_urlopen):
            self.notifications.send_ntfy_notification(settings, title="Shack \U0001F4FB", message="Test")
        self.assertEqual(captured["Title"], "=?UTF-8?B?U2hhY2sg8J+Tuw==?=")


class GeneralPageTests(unittest.TestCase):
    """The instance name, as a browser without the page script sets it, and in titles."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.mixin = sys.modules["nwr_stream_manager.service_general"].GeneralServiceMixin

    def setUp(self) -> None:
        wc = self.wc
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        for name, read_only, password in (("helper", False, "password2"), ("viewer", True, "password3")):
            account, secret = self.accounts.create_account(name, read_only)
            self.accounts.change_password(account["id"], secret, password, password)
        self.path = Path(tempdir.name) / "general.json"
        self.announced: list[bool] = []
        service_class = type("GeneralStandIn", (self.mixin, StandInService), {})
        self.service = service_class(
            accounts=self.accounts,
            auth_sessions=wc.AuthSessionStore(),
            status=lambda read_only=False: {},
            lock=threading.Lock(),
            general=sys.modules["nwr_stream_manager.general_settings"].GeneralSettings("NWR Stream Manager on weatherpi"),
            general_state_path=self.path,
            announce_settings_changed=lambda: self.announced.append(True),
        )
        handler = type("GeneralPageTestHandler", (wc.RtlControlHandler,), {"service": self.service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def request(self, method, path, user=None, fields=None, cookie=""):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {}
        if user:
            headers["Authorization"] = "Basic " + base64.b64encode(":".join(user).encode()).decode()
        body = None
        if fields is not None:
            body = urlencode(fields)
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response, response.read().decode("utf-8")

    def title(self, page):
        return re.search(r"<title>(.*?)</title>", page).group(1)

    def test_settings_opens_on_general_beside_the_list(self) -> None:
        _, page = self.request("GET", "/?view=settings", ("helper", "password2"))
        self.assertEqual(self.title(page), "Settings - NWR Stream Manager on weatherpi")
        self.assertLess(page.index('id="settings_tab_general"'), page.index('id="settings_tab_accounts"'))
        self.assertRegex(page, r'<button id="settings_tab_general" [^>]*aria-current="page">General</button>')
        self.assertIn('<div id="settings_panel_general" class="settings-panel">', page)
        # Only the list shows on a phone: the layout isn't marked as showing a setting.
        self.assertIn('<div id="settings_layout" class="settings-layout">', page)
        self.assertRegex(page, r'<input id="general_instance_name" [^>]*value="NWR Stream Manager on weatherpi">')
        self.assertIn('<meta id="instance_name_meta" name="instance-name" content="NWR Stream Manager on weatherpi">', page)
        self.assertIn('<h1 id="site_heading">NWR Stream Manager on weatherpi</h1>', page)

    def test_only_a_changed_name_is_saved_and_titles_change_at_once(self) -> None:
        admin = ("helper", "password2")
        response, page = self.request("GET", "/?view=settings&section=general", admin)
        cookie_name, token = re.search(r"(nwrstmgr_form_\w+)=([^;]+)", response.getheader("Set-Cookie")).groups()
        cookie = f"{cookie_name}={token}"
        self.assertEqual(self.title(page), "General - Settings - NWR Stream Manager on weatherpi")
        self.assertIn('<input type="hidden" name="was_instance_name" value="NWR Stream Manager on weatherpi"><input type="hidden" name="was_location" value="">', page)
        fields = {
            "form": "general", "instance_name": "NWR Stream Manager on weatherpi", "was_instance_name": "NWR Stream Manager on weatherpi",
            "location": "", "was_location": "", "form_token": token,
        }
        response, _ = self.request("POST", "/general-settings", admin, fields, cookie)
        self.assertEqual(response.getheader("Location"), "/?view=settings&section=general&result=saved")
        self.assertFalse(self.path.exists())
        self.assertEqual(self.announced, [])

        response, _ = self.request("POST", "/general-settings", admin, {**fields, "instance_name": "  Kitchen   <Pi> "}, cookie)
        self.assertEqual(json.loads(self.path.read_text()), {"instance_name": "Kitchen <Pi>", "location": ""})
        self.assertEqual(self.announced, [True])
        _, page = self.request("GET", response.getheader("Location"), admin)
        self.assertEqual(self.title(page), "Changes saved. - General - Settings - Kitchen &lt;Pi&gt;")
        _, page = self.request("GET", "/", ("viewer", "password3"))
        self.assertEqual(self.title(page), "Dashboard - Kitchen &lt;Pi&gt;")
        self.assertIn('<h1 id="site_heading">Kitchen &lt;Pi&gt;</h1>', page)

        # The location is saved by itself, leaving the name as it is.
        response, _ = self.request("POST", "/general-settings", admin, {**fields, "location": "Attic SDR (Grand Rapids, MI)"}, cookie)
        self.assertEqual(json.loads(self.path.read_text()), {"instance_name": "Kitchen <Pi>", "location": "Attic SDR (Grand Rapids, MI)"})
        _, page = self.request("GET", "/?view=settings&section=general", admin)
        self.assertRegex(page, r'<input id="general_location" [^>]*value="Attic SDR \(Grand Rapids, MI\)">')

        response, page = self.request("POST", "/general-settings", admin, {**fields, "instance_name": "x" * 65}, cookie)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: The instance name can be up to 64 characters long. - General - Settings - Kitchen &lt;Pi&gt;")
        self.assertRegex(page, r'<input id="general_instance_name" [^>]*value="x{65}">')

    def test_read_only_accounts_cannot_change_it_and_signed_out_pages_dont_show_it(self) -> None:
        viewer = ("viewer", "password3")
        response, _ = self.request("PATCH", "/api/general-settings", viewer)
        self.assertEqual(response.status, 403)
        response, _ = self.request("POST", "/general-settings", viewer, {"instance_name": "Mine"})
        self.assertEqual(response.status, 403)
        self.assertFalse(self.path.exists())
        _, page = self.request("GET", "/", ("owner", "wrong"))
        self.assertNotIn("weatherpi", page)
        _, page = self.request("GET", "/help")
        self.assertNotIn("weatherpi", page)


if __name__ == "__main__":
    unittest.main()
