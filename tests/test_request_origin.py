from __future__ import annotations

import base64
import http.client
import json
import sys
import tempfile
import threading
import unittest

import pytest
from unittest import mock
from pathlib import Path

from test_auth import load_web_control_module
from test_page_rendering import StandInService
from test_restart import RestartPageTests


class RequestOriginTests(unittest.TestCase):
    """Telling requests from this server's own pages from ones another website sends."""

    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.origin = sys.modules["nwr_stream_manager.request_origin"]

    def another_site(self, method="POST", path="/api/restart", **headers) -> bool:
        headers = {name.replace("_", "-"): value for name, value in headers.items()}
        return self.origin.request_from_another_site(method, path, headers)

    def test_reading_is_never_refused(self) -> None:
        self.assertFalse(self.another_site("GET", "/api/status", Sec_Fetch_Site="cross-site", Origin="https://evil.example"))

    def test_browsers_that_say_where_a_request_came_from_are_believed(self) -> None:
        self.assertFalse(self.another_site(Sec_Fetch_Site="same-origin"))
        self.assertTrue(self.another_site(Sec_Fetch_Site="cross-site"))
        # Another site on the same domain, such as another tunnel, is still another website.
        self.assertTrue(self.another_site(Sec_Fetch_Site="same-site"))
        self.assertTrue(self.another_site("PATCH", "/api/general-settings", Sec_Fetch_Site="cross-site"))
        self.assertTrue(self.another_site("POST", "/general-settings", Sec_Fetch_Site="cross-site"))

    def test_the_origin_must_be_this_server(self) -> None:
        self.assertFalse(self.another_site(Origin="http://192.168.1.20:8080", Host="192.168.1.20:8080"))
        self.assertTrue(self.another_site(Origin="http://192.168.1.20:8081", Host="192.168.1.20:8080"))
        self.assertTrue(self.another_site(Origin="https://evil.example", Host="192.168.1.20:8080"))
        self.assertTrue(self.another_site(Origin="null", Host="192.168.1.20:8080"))
        self.assertFalse(self.another_site(Origin="http://[::1]:8080", Host="[::1]:8080"))

    def test_a_tunnel_passes_the_address_the_browser_used(self) -> None:
        # Taken over HTTPS, then passed on here as plain HTTP with the same Host.
        self.assertFalse(self.another_site(Origin="https://radio.example.com", Host="radio.example.com"))
        self.assertFalse(self.another_site(Origin="https://radio.example.com", Host="radio.example.com:443"))
        # Or with the address it was sent to here, and the browser's in X-Forwarded-Host.
        self.assertFalse(
            self.another_site(Origin="https://Radio.Example.com", Host="127.0.0.1:8080", X_Forwarded_Host="radio.example.com, 10.0.0.2")
        )
        self.assertTrue(self.another_site(Origin="https://evil.example", Host="127.0.0.1:8080", X_Forwarded_Host="radio.example.com"))

    def test_older_browsers_cant_post_a_form_to_the_api(self) -> None:
        for content_type in ("application/x-www-form-urlencoded", "multipart/form-data; boundary=x", "text/plain;charset=UTF-8"):
            with self.subTest(content_type=content_type):
                self.assertTrue(self.another_site(Content_Type=content_type))
        self.assertFalse(self.another_site(Content_Type="application/json"))
        self.assertFalse(self.another_site("POST", "/api/fallback-audio", Content_Type="application/octet-stream"))
        self.assertFalse(self.another_site("POST", "/api/iq-test-source/stop"))
        # The forms on these pages carry a token instead.
        self.assertFalse(self.another_site("POST", "/general-settings", Content_Type="application/x-www-form-urlencoded"))

    def test_live_audio_must_come_from_this_server(self) -> None:
        websocket = {"Upgrade": "websocket", "Host": "192.168.1.20:8080"}
        self.assertTrue(self.origin.request_from_another_site("GET", "/api/live-audio", {**websocket, "Origin": "https://evil.example"}))
        self.assertFalse(self.origin.request_from_another_site("GET", "/api/live-audio", {**websocket, "Origin": "http://192.168.1.20:8080"}))

    def test_https_this_server_took_itself_isnt_a_proxy(self) -> None:
        through = self.origin.request_through_proxy
        mine = {"X-Forwarded-Proto": "https", "X-NWR-Stream-Manager-HTTPS": self.origin.OWN_HTTPS_TOKEN}
        self.assertFalse(through(mine))
        # Anything else a proxy adds still counts, and the secret can't be guessed.
        self.assertTrue(through({**mine, "X-Forwarded-For": "203.0.113.9"}))
        self.assertTrue(through({"X-Forwarded-Proto": "https", "X-NWR-Stream-Manager-HTTPS": "1"}))
        self.assertTrue(through({"X-Forwarded-Proto": "https"}))

    def test_tunnels_and_proxies_say_they_passed_a_request_on(self) -> None:
        through = self.origin.request_through_proxy
        self.assertFalse(through({"Host": "192.168.1.20:8080", "Origin": "http://192.168.1.20:8080"}))
        for header in ("X-Forwarded-For", "CF-Connecting-IP", "Tailscale-User-Login", "Forwarded", "X-Real-IP", "Via"):
            with self.subTest(header=header):
                self.assertTrue(through({"Host": "radio.example.com", header: "203.0.113.9"}))
        self.assertFalse(through({"X-Forwarded-For": "  "}))

    def test_the_visitors_own_address_is_taken_from_a_proxy_on_this_computer(self) -> None:
        visitor = self.origin.forwarded_client_address
        # What Tailscale Funnel's proxy adds is the last address; earlier ones came with the request.
        self.assertEqual(visitor("127.0.0.1", {"X-Forwarded-For": "203.0.113.5"}), "203.0.113.5")
        self.assertEqual(visitor("127.0.0.1", {"X-Forwarded-For": "1.1.1.1, 203.0.113.5"}), "203.0.113.5")
        self.assertEqual(visitor("::1", {"X-Forwarded-For": "2001:db8::5"}), "2001:db8::5")
        self.assertEqual(visitor("127.0.0.1", {"Forwarded": 'for="[2001:db8::5]:4711";proto=https'}), "2001:db8::5")
        self.assertEqual(visitor("127.0.0.1", {"Forwarded": "for=198.51.100.7:4711"}), "198.51.100.7")
        self.assertEqual(visitor("127.0.0.1", {"CF-Connecting-IP": "198.51.100.8"}), "198.51.100.8")
        # Not from this computer, nothing that's an address, or no proxy at all: the connection's own address is used.
        self.assertEqual(visitor("192.168.1.20", {"X-Forwarded-For": "203.0.113.5"}), "")
        self.assertEqual(visitor("127.0.0.1", {"X-Forwarded-For": "unknown"}), "")
        self.assertEqual(visitor("127.0.0.1", {}), "")

    def test_https_is_what_the_tunnel_says_the_browser_used(self) -> None:
        over = self.origin.request_over_https
        self.assertFalse(over({"Host": "192.168.1.20:8080"}))
        self.assertTrue(over({"X-Forwarded-Proto": "https"}))
        self.assertTrue(over({"X-Forwarded-Proto": "HTTPS, http"}))
        self.assertFalse(over({"X-Forwarded-Proto": "http"}))
        self.assertTrue(over({"Forwarded": 'for=203.0.113.9;proto=https;host="radio.example.com"'}))
        self.assertFalse(over({"Forwarded": "for=203.0.113.9;proto=http, proto=https"}))
        self.assertTrue(over({"CF-Visitor": '{"scheme":"https"}'}))
        self.assertFalse(over({"CF-Visitor": '{"scheme":"http"}'}))


class AnotherSiteRequestTests(unittest.TestCase):
    """A signed-in browser can't be made to change anything by another website."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]

    def post(self, path, body, content_type, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(connection.close)
        headers = {name.replace("_", "-"): value for name, value in headers.items()}
        headers["Authorization"] = "Basic " + base64.b64encode(b"owner:password1").decode()
        headers["Content-Type"] = content_type
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return response, response.read().decode("utf-8")

    def test_another_websites_request_is_refused_before_it_does_anything(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING") as logs:
            response, body = self.post("/api/restart", '{"instance": "run1"}', "text/plain", Origin="https://evil.example")
        self.assertEqual(response.status, 403)
        self.assertEqual(json.loads(body)["error"], "This request came from another website, so nothing was changed.")
        self.assertIn("refused POST /api/restart", logs.output[0])
        self.assertEqual(self.requests, [])

    def test_older_browsers_form_disguised_as_json_is_refused(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            response, _body = self.post("/api/restart", '{"instance": "run1"}', "text/plain")
        self.assertEqual(response.status, 403)
        self.assertEqual(self.requests, [])

    def test_a_plain_form_from_another_website_gets_a_page_saying_so(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            response, page = self.post("/restart", "instance=run1", "application/x-www-form-urlencoded", Sec_Fetch_Site="cross-site")
        self.assertEqual(response.status, 403)
        self.assertIn("This request came from another website, so nothing was changed.", page)
        self.assertEqual(self.requests, [])

    def test_this_servers_own_pages_still_work(self) -> None:
        response, body = self.post(
            "/api/restart",
            '{"instance": "run1"}',
            "application/json",
            Sec_Fetch_Site="same-origin",
            Origin=f"http://127.0.0.1:{self.port}",
        )
        self.assertEqual(response.status, 200, body)
        self.assertEqual(len(self.requests), 1)



class FramingTests(unittest.TestCase):
    """No page can show these pages in a frame, so another website can't hide them under its own (clickjacking)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]

    def test_every_response_refuses_other_websites_frames(self) -> None:
        owner = ("owner", "password1")
        for path, user in (("/", owner), ("/api/status", owner), ("/help", None), ("/", ("owner", "wrong")), ("/no-such-page", owner)):
            with self.subTest(path=path, user=user):
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                self.addCleanup(connection.close)
                headers = {"Authorization": "Basic " + base64.b64encode(":".join(user).encode()).decode()} if user else {}
                connection.request("GET", path, headers=headers)
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.getheader("Content-Security-Policy"), "frame-ancestors 'none'")
                self.assertEqual(response.getheader("X-Frame-Options"), "DENY")
                self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")


class HttpsCookieTests(unittest.TestCase):
    """Over HTTPS, cookies stay on HTTPS and the browser keeps to HTTPS; over plain HTTP, neither."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]

    def get(self, path, password="password1", **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        headers = {name.replace("_", "-"): value for name, value in headers.items()}
        headers["Authorization"] = "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        response.read()
        return response

    def cookies(self, response):
        return [value for name, value in response.getheaders() if name.lower() == "set-cookie"]

    def test_over_https_every_cookie_is_secure(self) -> None:
        response = self.get("/", X_Forwarded_Proto="https", X_Forwarded_For="203.0.113.9")
        cookies = self.cookies(response)
        self.assertEqual({cookie.split("=", 1)[0] for cookie in cookies}, {f"nwrstmgr_session_{self.accounts.cookie_id}", f"nwrstmgr_form_{self.accounts.cookie_id}"})
        for cookie in cookies:
            self.assertTrue(cookie.endswith("; Secure"), cookie)
        self.assertEqual(response.getheader("Strict-Transport-Security"), "max-age=31536000")
        # Signing out clears the cookie the same way, or the browser wouldn't let it be replaced.
        response = self.get("/", password="wrong", X_Forwarded_Proto="https")
        self.assertEqual(response.status, 401)
        self.assertTrue(all(cookie.endswith("; Secure") for cookie in self.cookies(response)))

    def test_tailscale_names_get_hsts_only_when_funnel_makes_them_public(self) -> None:
        network = sys.modules["nwr_stream_manager.network"]
        name = "max-pi5.example.ts.net"
        serve_only = [{"url": f"https://{name}", "host": name, "public": False}]
        funnel = [{"url": f"https://{name}", "host": name, "public": True}]
        for addresses, expected in ((serve_only, None), (funnel, "max-age=31536000")):
            with self.subTest(public=addresses[0]["public"]), mock.patch.object(network, "tailscale_serve_addresses_for_port", return_value=addresses):
                response = self.get("/", X_Forwarded_Proto="https", Host=name, X_Forwarded_For="100.64.0.7")
                self.assertEqual(response.getheader("Strict-Transport-Security"), expected)
                # Cookies stay on HTTPS either way.
                self.assertTrue(all(cookie.endswith("; Secure") for cookie in self.cookies(response)))
        # Any other name reached over HTTPS, such as through Cloudflare Tunnel, gets it as before.
        response = self.get("/", X_Forwarded_Proto="https", Host="radio.example.com")
        self.assertEqual(response.getheader("Strict-Transport-Security"), "max-age=31536000")

    def test_over_plain_http_cookies_still_work(self) -> None:
        response = self.get("/")
        cookies = self.cookies(response)
        self.assertTrue(cookies)
        for cookie in cookies:
            self.assertNotIn("Secure", cookie)
        self.assertIsNone(response.getheader("Strict-Transport-Security"))


class LeftoverBodyTests(unittest.TestCase):
    """A body a route doesn't read can't be taken for the next request on the same connection."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]

    def exchange(self, body_size: int) -> list[int]:
        """A DELETE whose route ignores its body, then a GET, on one connection: the statuses that come back."""
        auth = "Basic " + base64.b64encode(b"owner:password1").decode()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        statuses = []
        connection.request("DELETE", "/api/iq-recording?id=nothing", body=b"x" * body_size,
                           headers={"Authorization": auth, "Content-Type": "application/json"})
        response = connection.getresponse()
        response.read()
        statuses.append(response.status)
        if response.will_close:
            return statuses
        connection.request("GET", "/api/account/me", headers={"Authorization": auth})
        response = connection.getresponse()
        response.read()
        statuses.append(response.status)
        return statuses

    def test_the_next_request_is_answered_normally(self) -> None:
        # The way the I/Q recorder's Stop once left "{}" behind, and the request after it failed.
        first, then = self.exchange(2)
        self.assertEqual(then, 200)

    def test_a_large_leftover_closes_the_connection_instead(self) -> None:
        # Closed rather than read: either the reply says so, or it closes while the rest is still being sent.
        try:
            statuses = self.exchange(200 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            return
        self.assertEqual(len(statuses), 1)


class SetupThroughProxyTests(unittest.TestCase):
    """The owner account can't be created through a tunnel, where anyone who found its address could."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = self.wc.AccountStore(Path(tempdir.name) / "accounts.db")
        service = StandInService(accounts=self.accounts, auth_sessions=self.wc.AuthSessionStore(), instance_id="run1")
        handler = type("SetupTestHandler", (self.wc.RtlControlHandler,), {"service": service})
        server = self.wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]

    def send(self, method, path, body=None, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        headers = {name.replace("_", "-"): value for name, value in headers.items()}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return response, response.read().decode("utf-8")

    def account(self, **headers):
        body = json.dumps({"username": "owner", "password": "password1", "confirm_password": "password1"})
        return self.send("POST", "/api/setup-account", body, Content_Type="application/json", **headers)

    def test_through_a_tunnel_the_setup_page_says_to_set_up_from_home(self) -> None:
        response, page = self.send("GET", "/", CF_Connecting_IP="203.0.113.9", X_Forwarded_For="203.0.113.9")
        self.assertEqual(response.status, 403)
        self.assertIn("<title>Finish setup from your own network - NWR Stream Manager Setup</title>", page)
        self.assertIn('<div id="setup_blocked">', page)
        self.assertIn('<div id="setup_welcome" hidden>', page)

    def test_through_a_tunnel_no_account_can_be_created(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING") as logs:
            response, body = self.account(X_Forwarded_For="203.0.113.9")
        self.assertEqual(response.status, 403)
        self.assertIn("can only be set up for the first time from a device on its local network", json.loads(body)["error"])
        self.assertIn("refused first-time setup", logs.output[0])
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            response, page = self.send(
                "POST", "/setup", "username=owner&password=password1&confirm_password=password1",
                Content_Type="application/x-www-form-urlencoded", Tailscale_User_Login="someone@example.com",
            )
        self.assertEqual(response.status, 403)
        self.assertIn("Finish setup from your own network", page)
        self.assertFalse(self.accounts.has_account())

    @pytest.mark.real_local_network
    def test_not_even_from_this_computer_itself(self) -> None:
        # A tunnel on this computer, such as ssh -R, connects from 127.0.0.1 with nothing added.
        response, page = self.send("GET", "/")
        self.assertEqual(response.status, 403)
        self.assertIn('<div id="setup_blocked">', page)
        response, body = self.account()
        self.assertEqual(response.status, 403)
        self.assertFalse(self.accounts.has_account())

    def test_from_the_network_setup_works_and_then_the_tunnel_needs_two_step_verification(self) -> None:
        response, page = self.send("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn('<div id="setup_blocked" hidden>', page)
        response, _body = self.account()
        self.assertEqual(response.status, 200)
        self.assertTrue(self.accounts.has_account())
        auth = "Basic " + base64.b64encode(b"owner:password1").decode()
        response, body = self.send("GET", "/api/account/me", Authorization=auth, X_Forwarded_For="203.0.113.9")
        self.assertEqual((response.status, json.loads(body)["two_step"]), (403, "not_set_up"))


if __name__ == "__main__":
    unittest.main()
