from __future__ import annotations

import base64
import http.client
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


class RestartTests(unittest.TestCase):
    """Restarting from the page: once, only for the run it was asked of, and in place."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.restart = sys.modules["nwr_stream_manager.service_restart"]

    def service(self):
        stops: list[bool] = []
        service = type("RestartStandIn", (self.restart.RestartServiceMixin, StandInService), {})(
            lock=threading.Lock(),
            instance_id="run1",
            restart_requested=threading.Event(),
            shutting_down=threading.Event(),
            stop_web_server=lambda: stops.append(True),
        )
        service.begin_shutdown = service.shutting_down.set
        return service, stops

    def test_a_restart_is_asked_of_one_run_once(self) -> None:
        service, stops = self.service()
        with self.assertRaisesRegex(ValueError, "already restarted since this page was opened"):
            service.request_restart({"instance": "run0"})
        self.assertEqual(stops, [])
        with self.assertLogs("nwr_stream_manager", level="INFO"):
            self.assertEqual(service.request_restart({"instance": "run1"}, "owner"), {"restarting": True, "instance": "run1"})
        self.assertTrue(service.restart_requested.is_set())
        self.assertTrue(service.shutting_down.is_set())
        with self.assertRaisesRegex(ValueError, "already restarting"):
            service.request_restart({"instance": "run1"})
        self.assertEqual(stops, [True])

    def test_the_same_command_replaces_this_process(self) -> None:
        with mock.patch.object(sys, "orig_argv", ["python3", "-m", "nwr_stream_manager", "--port", "9090"]), mock.patch.object(
            self.restart.os, "execv"
        ) as execv, mock.patch.object(self.restart.logging, "shutdown"):
            self.restart.restart_process()
        execv.assert_called_once_with(sys.executable, [sys.executable, "-m", "nwr_stream_manager", "--port", "9090"])

    def test_main_restarts_only_when_asked_to(self) -> None:
        with mock.patch.object(self.wc, "run_server", return_value=False), mock.patch.object(self.wc, "restart_process") as restart:
            self.assertEqual(self.wc.main(["--state", "/nonexistent/rtl-control.json"]), 0)
        restart.assert_not_called()
        with mock.patch.object(self.wc, "run_server", return_value=True), mock.patch.object(self.wc, "restart_process", return_value=1) as restart:
            self.assertEqual(self.wc.main(["--state", "/nonexistent/rtl-control.json"]), 1)
        restart.assert_called_once_with()


class RestartPageTests(unittest.TestCase):
    """Restarting, as a browser without the page script does it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        wc = self.wc
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        for name, read_only, password in (("helper", False, "password2"), ("viewer", True, "password3")):
            account, secret = self.accounts.create_account(name, read_only)
            self.accounts.change_password(account["id"], secret, password, password)
        self.requests: list[tuple] = []

        def request_restart(payload, account_name=""):
            if payload.get("instance") != "run1":
                raise ValueError("NWR Stream Manager has already restarted since this page was opened.")
            self.requests.append((payload, account_name))
            return {"restarting": True, "instance": "run1"}

        service = StandInService(
            accounts=self.accounts,
            auth_sessions=wc.AuthSessionStore(),
            status=lambda read_only=False: {},
            instance_id="run1",
            request_restart=request_restart,
        )
        handler = type("RestartPageTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def request(self, method, path, user, fields=None, cookie=""):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {"Authorization": "Basic " + base64.b64encode(":".join(user).encode()).decode()}
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

    def test_restart_is_confirmed_on_a_page_then_says_it_is_restarting(self) -> None:
        owner = ("owner", "password1")
        response, page = self.request("GET", "/", owner)
        self.assertIn('<a id="nav_restart" href="/?view=restart">Restart NWR Stream Manager</a>', page)
        self.assertLess(page.index('id="nav_change_password"'), page.index('id="nav_restart"'))
        self.assertLess(page.index('id="nav_restart"'), page.index("</span>", page.index('id="nav_more_menu"')))
        response, page = self.request("GET", "/?view=restart", owner)
        token = re.search(r"nwrstmgr_form=([^;]+)", response.getheader("Set-Cookie")).group(1)
        self.assertEqual(self.title(page), "Restart NWR Stream Manager? - NWR Stream Manager")
        self.assertIn('<input id="restart_instance" type="hidden" name="instance" value="run1">', page)
        self.assertIn('<button id="restart_confirm" type="submit" class="primary" autofocus="autofocus">Restart</button>', page)
        self.assertIn('<a id="nav_restart" href="/?view=restart" aria-current="page">', page)

        cookie = f"nwrstmgr_form={token}"
        response, page = self.request("POST", "/restart", owner, {"instance": "run0", "form_token": token}, cookie)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.title(page), "Error: NWR Stream Manager has already restarted since this page was opened. - Restart NWR Stream Manager? - NWR Stream Manager")
        self.assertEqual(self.requests, [])

        response, page = self.request("POST", "/restart", ("helper", "password2"), {"instance": "run1", "form_token": token}, cookie)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.title(page), "NWR Stream Manager is restarting - NWR Stream Manager")
        self.assertEqual(self.requests, [({"instance": "run1"}, "helper")])

    def test_read_only_accounts_cannot_restart(self) -> None:
        viewer = ("viewer", "password3")
        _, page = self.request("GET", "/", viewer)
        self.assertRegex(page, r'<a id="nav_restart" href="/\?view=restart" hidden>')
        _, page = self.request("GET", "/?view=restart", viewer)
        self.assertNotIn("Restart NWR Stream Manager? - ", self.title(page))
        for path, body in (("/restart", {"instance": "run1"}), ("/api/restart", None)):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            self.addCleanup(connection.close)
            headers = {"Authorization": "Basic " + base64.b64encode(b"viewer:password3").decode(), "Content-Type": "application/json"}
            connection.request("POST", path, body='{"instance": "run1"}', headers=headers)
            self.assertEqual(connection.getresponse().status, 403, path)
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
