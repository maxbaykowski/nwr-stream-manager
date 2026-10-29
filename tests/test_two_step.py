from __future__ import annotations

import base64
import html
import importlib
import http.client
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

import pytest

from test_auth import load_web_control_module
from test_page_rendering import StandInService


class CodeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.two_step = importlib.import_module("nwr_stream_manager.two_step")

    def test_codes_are_the_ones_the_standard_gives(self) -> None:
        # RFC 6238's SHA-1 test values, of which authenticator apps show the last six digits.
        secret = base64.b32encode(b"12345678901234567890").decode()
        for when, code in ((59, "94287082"), (1111111109, "07081804"), (1234567890, "89005924"), (2000000000, "69279037")):
            self.assertEqual(self.two_step.code_at(secret, when // 30), code[-6:])

    def test_a_code_works_a_step_either_side_and_only_once(self) -> None:
        secret = self.two_step.new_secret()
        now = 1_800_000_000.0
        step = self.two_step.current_step(now)
        for offset in (-1, 0, 1):
            code = self.two_step.code_at(secret, step + offset)
            self.assertEqual(self.two_step.matching_step(secret, code, now=now), step + offset)
            # Not again, once it's been used.
            self.assertIsNone(self.two_step.matching_step(secret, code, last_used_step=step + offset, now=now))
        self.assertIsNone(self.two_step.matching_step(secret, self.two_step.code_at(secret, step + 2), now=now))
        # Spaces, as some apps show it, don't matter.
        code = self.two_step.code_at(secret, step)
        self.assertEqual(self.two_step.matching_step(secret, f"{code[:3]} {code[3:]}", now=now), step)

    def test_the_setup_link_names_the_account_and_which_computer_its_on(self) -> None:
        uri = self.two_step.setup_uri("ABCDEFGH", "owner", "Attic SDR")
        self.assertTrue(uri.startswith("otpauth://totp/NWR%20Stream%20Manager%3Aowner%20on%20Attic%20SDR?secret=ABCDEFGH&issuer=NWR+Stream+Manager&"))
        # NWR Stream Manager isn't said twice, and the computer is named even if the instance isn't.
        name = self.two_step.authenticator_name
        self.assertEqual(name("owner", "NWR Stream Manager on weatherpi"), "owner on weatherpi")
        self.assertEqual(name("owner", "NWR Stream Manager"), f"owner on {socket.gethostname().split('.')[0]}")

    def test_recovery_codes_are_read_however_theyre_typed(self) -> None:
        codes = self.two_step.new_recovery_codes()
        self.assertEqual(len(set(codes)), 10)
        code = codes[0]
        self.assertEqual(self.two_step.recovery_code_hash(code.upper().replace("-", " ")), self.two_step.recovery_code_hash(code))
        self.assertTrue(self.two_step.looks_like_recovery_code(code))
        self.assertFalse(self.two_step.looks_like_recovery_code("123456"))


# This computer's own addresses, in the networks the tests below say it's on.
OWN_ADDRESSES = ("192.168.1.79", "2600:1702:3bb0:99c0::25")


@pytest.mark.real_local_network
class LocalNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.module = importlib.import_module("nwr_stream_manager.local_network")

    def networks(self, *, tailscale_up: bool = False, peers: tuple[str, ...] = (), program: str = "ssh", on_the_internet: bool = False):
        found = [("wlan0", ipaddress.ip_network("192.168.1.0/24")), ("wlan0", ipaddress.ip_network("2600:1702:3bb0:99c0::/64"))]
        if tailscale_up:
            found.append(("tailscale0", ipaddress.ip_network("100.101.102.103/32")))
        else:
            found.append(("tailscale0", ipaddress.ip_network("fe80::/64")))  # there, but down
        found.append(("docker0", ipaddress.ip_network("172.17.0.0/16")))
        found.append(("tun0", ipaddress.ip_network("10.8.0.0/16")))  # a VPN service
        found.append(("wg0", ipaddress.ip_network("10.200.0.0/24")))
        found.append(("eth1", ipaddress.ip_network("45.33.32.0/24")))  # a data centre's, shared
        found.append(("eth1", ipaddress.ip_network("2a01:4f8:1c1c::/48")))
        for patcher in (
            mock.patch.object(self.module, "_ipv4_networks", return_value=[item for item in found if item[1].version == 4]),
            mock.patch.object(self.module, "_ipv6_networks", return_value=[item for item in found if item[1].version == 6]),
            mock.patch.object(self.module, "_routers", return_value={ipaddress.ip_address("192.168.1.254")}),
            mock.patch.object(self.module, "_on_the_internet", return_value=on_the_internet),
            # Tailscale on this computer sends to its peers by tailscale0, to this computer's own
            # addresses by lo, and everything else as usual.
            mock.patch.object(
                self.module, "_route_interface",
                side_effect=lambda address: "tailscale0" if str(address) in peers else "lo" if str(address) in OWN_ADDRESSES else "wlan0",
            ),
            # Whatever made a connection from this computer.
            mock.patch.object(self.module, "_connection_program", return_value=program),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        return self.module.LocalNetworks()

    def test_only_this_computers_own_networks_are_local(self) -> None:
        local = self.networks().is_local
        self.assertTrue(local("192.168.1.20", {}))
        self.assertTrue(local("2600:1702:3bb0:99c0::42", {}))
        self.assertTrue(local("::ffff:192.168.1.20", {}))
        for host in ("203.0.113.9", "10.0.0.5", "172.17.0.2", "10.8.3.4", "10.200.0.7", "45.33.32.20", "2a01:4f8:1c1c:1::5", "", "not an address"):
            with self.subTest(host=host):
                self.assertFalse(local(host, {}))

    def test_on_a_server_directly_on_the_internet_only_tailnet_devices_are(self) -> None:
        # A data centre's private and IPv6 networks can be shared with other customers.
        networks = self.networks(tailscale_up=True, peers=("100.64.0.5",), program="firefox", on_the_internet=True)
        self.assertFalse(networks.is_local("192.168.1.20", {}))
        self.assertFalse(networks.is_local("2600:1702:3bb0:99c0::42", {}))
        self.assertTrue(networks.is_local("100.64.0.5", {}))
        self.assertTrue(networks.is_local("127.0.0.1", {}, 50000))  # a browser on the server itself

    def test_this_computer_is_only_when_a_browser_on_it_connects(self) -> None:
        # A tunnel or proxy on this computer connects from it too, by any of its addresses.
        for host in ("127.0.0.1", "::1", *OWN_ADDRESSES):
            with self.subTest(host=host):
                self.assertFalse(self.networks(program="ssh").is_local(host, {}, 50000))
                self.assertFalse(self.networks(program="cloudflared").is_local(host, {}, 50000))
                self.assertFalse(self.networks(program="").is_local(host, {}, 50000))  # someone else's, or gone
                self.assertTrue(self.networks(program="firefox").is_local(host, {}, 50000))
                # Not a connection at all, but one handed over from elsewhere.
                self.assertFalse(self.networks(program="firefox").is_local(host, {}, 0))
        # A browser through a proxy on this computer is the proxy's, as far as anyone can tell.
        self.assertFalse(self.networks(program="firefox").is_local("127.0.0.1", {"X-Forwarded-For": "203.0.113.9"}, 50000))

    def test_the_router_never_is(self) -> None:
        # Some routers pass on forwarded ports from themselves.
        self.assertFalse(self.networks(program="firefox").is_local("192.168.1.254", {}, 50000))

    def test_the_program_that_made_a_real_connection_is_found(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            with socket.create_connection(server.getsockname()) as client:
                connection, peer = server.accept()
                with connection:
                    program = self.module._connection_program(ipaddress.ip_address(peer[0]), peer[1])
        self.assertEqual(program, os.path.basename(os.readlink("/proc/self/exe")))  # this test's own Python
        self.assertNotIn(program, self.module.BROWSERS)

    def test_addresses_are_read_as_proc_net_gives_them(self) -> None:
        read = self.module._proc_net_address
        self.assertEqual(read("0100007F:1F90"), (ipaddress.ip_address("127.0.0.1"), 8080))
        self.assertEqual(read("00000000000000000000000001000000:1F90"), (ipaddress.ip_address("::1"), 8080))
        self.assertEqual(read("0000000000000000FFFF00000100007F:0050"), (ipaddress.ip_address("127.0.0.1"), 80))

    def test_nothing_passed_on_by_a_proxy_is(self) -> None:
        local = self.networks().is_local
        self.assertFalse(local("192.168.1.20", {"X-Forwarded-For": "203.0.113.9"}))
        self.assertFalse(local("192.168.1.20", {"Tailscale-User-Login": "someone@example.com"}))
        # HTTPS this server took itself isn't passed on.
        own_https = importlib.import_module("nwr_stream_manager.request_origin").OWN_HTTPS_TOKEN
        self.assertTrue(local("192.168.1.20", {"X-Forwarded-Proto": "https", "X-NWR-Stream-Manager-HTTPS": own_https}))
        self.assertFalse(local("192.168.1.20", {"X-Forwarded-Proto": "https", "X-NWR-Stream-Manager-HTTPS": "1"}))

    def test_through_tailscale_serve_tailnet_users_are_and_funnel_isnt(self) -> None:
        local = self.networks().is_local
        origin = importlib.import_module("nwr_stream_manager.request_origin")
        serve = {"X-NWR-Stream-Manager-Tailscale-Serve": origin.TAILSCALE_SERVE_TOKEN, "X-Forwarded-For": "100.64.0.5", "X-Forwarded-Proto": "https"}
        self.assertTrue(local("127.0.0.1", {**serve, "Tailscale-User-Login": "max@example.com"}))
        self.assertFalse(local("127.0.0.1", {**serve, "Tailscale-Funnel-Request": "?1"}))
        self.assertFalse(local("127.0.0.1", {**serve, "Tailscale-User-Login": "max@example.com", "Tailscale-Funnel-Request": "?1"}))
        self.assertFalse(local("127.0.0.1", serve))  # a tagged device, or anything Tailscale didn't vouch for
        # The headers mean nothing without the socket.
        self.assertFalse(local("127.0.0.1", {**serve, "X-NWR-Stream-Manager-Tailscale-Serve": "guessed", "Tailscale-User-Login": "max@example.com"}))

    def test_tailnet_devices_are_while_this_computer_is_on_the_tailnet(self) -> None:
        self.assertFalse(self.networks(peers=("100.64.0.5",)).is_local("100.64.0.5", {}))  # Tailscale is down
        networks = self.networks(tailscale_up=True, peers=("100.64.0.5", "fd7a:115c:a1e0::5"))
        self.assertTrue(networks.is_local("100.64.0.5", {}))
        self.assertTrue(networks.is_local("fd7a:115c:a1e0::5", {}))

    def test_carrier_grade_nat_addresses_that_arent_tailnet_devices_arent(self) -> None:
        # The same addresses, from an internet provider's network rather than through Tailscale.
        networks = self.networks(tailscale_up=True, peers=("100.64.0.5",))
        self.assertFalse(networks.is_local("100.70.1.2", {}))
        self.assertFalse(networks.is_local("fd7a:115c:a1e0::9", {}))

    def test_through_its_own_tailnet_connection_tailnet_devices_are(self) -> None:
        request_origin = importlib.import_module("nwr_stream_manager.request_origin")
        own = {request_origin.OWN_TAILNET_HEADER: request_origin.OWN_TAILNET_TOKEN}
        networks = self.networks()
        self.assertTrue(networks.is_local("100.64.0.5", own))
        self.assertTrue(networks.is_local("fd7a:115c:a1e0::5", own))
        # Not by claiming it, nor for an address that isn't a tailnet's.
        self.assertFalse(networks.is_local("100.64.0.5", {request_origin.OWN_TAILNET_HEADER: "guess"}))
        self.assertFalse(networks.is_local("203.0.113.9", own))


class AccountTwoStepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.two_step = importlib.import_module("nwr_stream_manager.two_step")

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = self.wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        self.owner = self.accounts.verify_basic_account("owner", "password1")

    def enable(self) -> tuple[str, list[str]]:
        secret = self.accounts.start_two_step_setup(self.owner.id)
        step = self.two_step.current_step() - 1  # a code that's used up, so the next is new
        codes = self.accounts.enable_two_step(self.owner.id, secret, self.two_step.code_at(secret, step))
        return secret, codes

    def test_its_turned_on_only_with_a_code_from_the_app(self) -> None:
        secret = self.accounts.start_two_step_setup(self.owner.id)
        with self.assertRaisesRegex(ValueError, "isn't right"):
            self.accounts.enable_two_step(self.owner.id, secret, "000000" if self.two_step.code_at(secret, self.two_step.current_step()) != "000000" else "111111")
        self.assertFalse(self.accounts.two_step_enabled(self.owner.id))
        _secret, codes = self.enable()
        self.assertEqual(len(codes), 10)
        self.assertEqual(self.accounts.two_step_status(self.owner.id)["recovery_codes_left"], 10)

    def test_codes_and_recovery_codes_each_work_once(self) -> None:
        secret, codes = self.enable()
        code = self.two_step.code_at(secret, self.two_step.current_step())
        self.assertTrue(self.accounts.verify_two_step(self.owner, code))
        self.assertFalse(self.accounts.verify_two_step(self.owner, code))
        self.assertTrue(self.accounts.verify_two_step(self.owner, codes[0].upper()))
        self.assertFalse(self.accounts.verify_two_step(self.owner, codes[0]))
        self.assertEqual(self.accounts.two_step_status(self.owner.id)["recovery_codes_left"], 9)
        new = self.accounts.new_recovery_codes(self.owner.id)
        self.assertFalse(self.accounts.verify_two_step(self.owner, codes[1]))
        self.assertTrue(self.accounts.verify_two_step(self.owner, new[1]))

    def test_wrong_codes_make_it_wait_as_wrong_passwords_do(self) -> None:
        self.enable()
        with self.assertRaises(importlib.import_module("nwr_stream_manager.sign_in_limits").SignInWait) as waited:
            for attempt in range(20):
                self.accounts.verify_two_step(self.owner, f"{attempt:06d}")
        self.assertIn("Too many wrong two-step verification codes", str(waited.exception))

    def test_an_account_keeps_only_so_many_sessions(self) -> None:
        sessions = self.wc.AuthSessionStore()
        first = sessions.create(self.owner.id)
        others = [sessions.create(self.owner.id) for _ in range(200)]
        self.assertFalse(sessions.validate(first))
        self.assertTrue(sessions.validate(others[-1]))
        self.assertEqual(sum(1 for session in sessions.sessions.values() if session.account_id == self.owner.id), 50)

    def test_sessions_are_kept_when_nwr_stream_manager_restarts(self) -> None:
        sessions = self.wc.AuthSessionStore(connect=self.accounts._connect)
        token = sessions.create(self.owner.id)
        sessions.pass_two_step(token)
        signed_out = sessions.create(self.owner.id)
        sessions.invalidate_token(signed_out)
        setup = sessions.create(self.owner.id)
        sessions.allow_two_step_setup(setup)
        expired = self.wc.AuthSessionStore(1, connect=self.accounts._connect).create(self.owner.id)
        time.sleep(1.1)

        again = self.wc.AuthSessionStore(connect=self.accounts._connect)
        self.assertEqual(again.validate(token, self.accounts).id, self.owner.id)
        self.assertTrue(again.two_step_passed(token))
        self.assertTrue(again.two_step_setup_only(setup))
        self.assertFalse(again.two_step_passed(setup))
        self.assertFalse(again.validate(signed_out))
        self.assertFalse(again.validate(expired))
        # Only a hash of each is kept, so the database can't be used to sign in.
        with self.accounts._connect() as connection:
            saved = [row[0] for row in connection.execute("SELECT token_hash FROM auth_sessions")]
        self.assertEqual(len(saved), 2)
        self.assertNotIn(token, saved)
        # Signed out everywhere, there too.
        again.invalidate_account(self.owner.id)
        self.assertFalse(self.wc.AuthSessionStore(connect=self.accounts._connect).validate(token))

    def test_a_setup_code_works_once_within_a_day_and_only_the_newest(self) -> None:
        self.accounts.create_account("someone", False)
        someone = self.accounts.account_by_id(2)
        first = self.accounts.create_setup_code(someone.id)
        code = self.accounts.create_setup_code(someone.id)
        self.assertEqual(self.accounts.verify_two_step(someone, first), "")  # replaced
        self.assertEqual(self.accounts.verify_two_step(someone, code.upper().replace("-", " ")), "setup")
        self.assertFalse(self.accounts.setup_code_waiting(someone.id))
        self.assertEqual(self.accounts.verify_two_step(someone, code), "")  # used up
        code = self.accounts.create_setup_code(someone.id)
        with self.accounts._connect() as connection:
            connection.execute("UPDATE two_step_setup_codes SET expires_at = ?", (time.time() - 1,))
        self.assertFalse(self.accounts.setup_code_waiting(someone.id))
        self.assertEqual(self.accounts.verify_two_step(someone, code), "")
        # Setting up two-step verification uses up any that's waiting.
        self.accounts.create_setup_code(someone.id)
        secret = self.accounts.start_two_step_setup(someone.id)
        self.accounts.enable_two_step(someone.id, secret, self.two_step.code_at(secret, self.two_step.current_step()))
        self.assertFalse(self.accounts.setup_code_waiting(someone.id))

    def test_each_instance_keeps_its_own_cookie_names(self) -> None:
        cookie_id = self.accounts.cookie_id
        self.assertRegex(cookie_id, r"^[0-9a-f]{8}$")
        self.assertEqual(self.wc.AccountStore(self.accounts.path).cookie_id, cookie_id)  # through restarts
        with tempfile.TemporaryDirectory() as other:
            self.assertNotEqual(self.wc.AccountStore(Path(other) / "accounts.db").cookie_id, cookie_id)

    def test_remembered_devices_are_forgotten_when_the_password_changes(self) -> None:
        self.enable()
        token = self.accounts.remember_device(self.owner.id)
        self.assertTrue(self.accounts.device_remembered(self.owner.id, token))
        self.assertFalse(self.accounts.device_remembered(self.owner.id + 1, token))
        self.accounts.change_password(self.owner.id, "password1", "password2", "password2")
        self.assertFalse(self.accounts.device_remembered(self.owner.id, token))
        token = self.accounts.remember_device(self.owner.id)
        self.accounts.disable_two_step(self.owner.id)
        self.assertFalse(self.accounts.device_remembered(self.owner.id, token))


class TwoStepPageTests(unittest.TestCase):
    """Two-step verification as a browser uses it, from outside the local network and on it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.two_step = importlib.import_module("nwr_stream_manager.two_step")

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = self.wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        self.owner = self.accounts.verify_basic_account("owner", "password1")
        service = StandInService(accounts=self.accounts, auth_sessions=self.wc.AuthSessionStore(), instance_id="run1")
        handler = type("TwoStepTestHandler", (self.wc.RtlControlHandler,), {"service": service})
        server = self.wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.cookies: dict[str, str] = {}

    def send(self, method, path, form=None, auth=True, user=("owner", "password1")):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        headers = {"Cookie": "; ".join(f"{name}={value}" for name, value in self.cookies.items())}
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{user[0]}:{user[1]}".encode()).decode()
        body = None
        if form is not None:
            body = urlencode(form)
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": f"http://127.0.0.1:{self.port}"})
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        for header, value in response.getheaders():
            if header.lower() == "set-cookie":
                name, _, rest = value.partition("=")
                self.cookies[name] = rest.split(";", 1)[0]
        return response, response.read().decode("utf-8")

    def token(self, page: str) -> str:
        return re.search(r'name="form_token" value="([^"]+)"', page).group(1)

    def shown(self, page: str, section: str) -> bool:
        return re.search(rf'<section id="{section}" [^>]*hidden', page) is None

    @pytest.mark.real_local_network
    def test_from_outside_without_it_set_up_the_account_cant_be_used(self) -> None:
        response, page = self.send("GET", "/")
        self.assertEqual(response.status, 403)
        self.assertTrue(self.shown(page, "two_step_unavailable"))
        self.assertIn("Accessing NWR Stream Manager over the public internet requires two-step verification to be enabled on your account.", page)
        # No one can give the owner a setup code.
        self.assertRegex(page, r'<span id="two_step_unavailable_setup_code" hidden')
        response, body = self.send("GET", "/api/status")
        self.assertEqual((response.status, json.loads(body)["two_step"]), (403, "not_set_up"))
        # Anyone else can ask the owner for one.
        _account, password = self.accounts.create_account("someone", False)
        self.cookies = {}
        response, page = self.send("GET", "/", user=("someone", password))
        self.assertIn('<span id="two_step_unavailable_setup_code"> If you do not have access to your local network, please ask the owner for a setup code, and then sign in again.</span>', page)

    @pytest.mark.real_local_network
    def test_from_outside_the_code_is_asked_for_then_remembered(self) -> None:
        secret = self.accounts.start_two_step_setup(self.owner.id)
        self.accounts.enable_two_step(self.owner.id, secret, self.two_step.code_at(secret, self.two_step.current_step() - 1))
        response, page = self.send("GET", "/?view=logs")
        self.assertEqual(response.status, 200)
        self.assertTrue(self.shown(page, "two_step_verify"))
        self.assertIn('value="/?view=logs"', page)
        self.assertIn(f'<span id="two_step_verify_name">NWR Stream Manager, owner on {socket.gethostname().split(".")[0]}</span>', page)
        response, body = self.send("GET", "/api/status")
        self.assertEqual((response.status, json.loads(body)["two_step"]), (403, "required"))
        # Nothing but the code is taken first.
        response, _page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "disable", "confirmed": "1"})
        self.assertEqual(response.status, 403)
        self.assertTrue(self.accounts.two_step_enabled(self.owner.id))
        code = self.two_step.code_at(secret, self.two_step.current_step())
        form = {"form_token": self.token(page), "action": "verify", "code": code, "next": "/?view=logs", "remember": "1"}
        response, _page = self.send("POST", "/two-step", form)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/?view=logs"))
        device_cookie = f"nwrstmgr_device_{self.accounts.cookie_id}"
        self.assertIn(device_cookie, self.cookies)
        response, _page = self.send("GET", "/")
        self.assertEqual(response.status, 200)
        # Changes need the local network, even then.
        response, page = self.send("GET", "/two-step")
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "disable", "confirmed": "1"})
        self.assertEqual(response.status, 403)
        self.assertIn("local network", html.unescape(page))
        # A new session on the remembered device isn't asked again.
        self.cookies = {device_cookie: self.cookies[device_cookie]}
        response, _page = self.send("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertNotIn("two_step_verify", _page)
        # Without it, it is.
        self.cookies = {}
        response, page = self.send("GET", "/")
        self.assertTrue(self.shown(page, "two_step_verify"))

    @pytest.mark.real_local_network
    def test_a_notification_link_from_outside_goes_through_after_the_code(self) -> None:
        secret = self.accounts.start_two_step_setup(self.owner.id)
        self.accounts.enable_two_step(self.owner.id, secret, self.two_step.code_at(secret, self.two_step.current_step() - 1))
        response, page = self.send("GET", "/notification?view=streams")
        self.assertTrue(self.shown(page, "two_step_verify"))
        code = self.two_step.code_at(secret, self.two_step.current_step())
        form = {"form_token": self.token(page), "action": "verify", "code": code, "next": "/notification?view=streams"}
        response, _page = self.send("POST", "/two-step", form)
        self.assertEqual(response.status, 303)
        # Back at the link, it goes where the notification said, rather than asking again.
        response, page = self.send("GET", "/notification?view=streams")
        self.assertEqual(response.status, 303, page[:200])

    def someone_with_setup_code(self) -> tuple[tuple[str, str], str]:
        account, password = self.accounts.create_account("someone", False)
        return ("someone", password), self.accounts.create_setup_code(int(account["id"]))

    @pytest.mark.real_local_network
    def test_from_outside_a_setup_code_lets_two_step_be_set_up_and_nothing_else(self) -> None:
        someone, setup_code = self.someone_with_setup_code()
        response, page = self.send("GET", "/", user=someone)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.shown(page, "two_step_verify"))
        self.assertIn('<h2 id="two_step_verify_title">Enter your setup code</h2>', page)
        self.assertIn("You are accessing NWR Stream Manager via the public internet. Enter your setup code. If you didn't get a setup code, ask the owner for it.", html.unescape(page))
        self.assertIn('<span id="two_step_code_label">Setup code</span>', page)
        self.assertRegex(page, r'<label id="two_step_remember_row" class="checkbox-row" hidden')
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "verify", "code": "aaaaa-bbbbb"}, user=someone)
        self.assertEqual(response.status, 400)
        self.assertIn("That setup code isn't right.", html.unescape(page))
        response, _page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "verify", "code": setup_code}, user=someone)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/two-step"))
        # Nothing but setting up, until it's done.
        response, _page = self.send("GET", "/?view=logs", user=someone)
        self.assertEqual((response.status, response.getheader("Location")), (303, "/two-step"))
        response, body = self.send("GET", "/api/status", user=someone)
        self.assertEqual((response.status, json.loads(body)["two_step"]), (403, "required"))
        response, page = self.send("GET", "/two-step", user=someone)
        self.assertTrue(self.shown(page, "two_step_setup"))
        self.assertRegex(page, r'<p id="two_step_setup_code_intro">')
        self.assertRegex(page, r'<a id="two_step_setup_cancel"[^>]* hidden')
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "forget_devices"}, user=someone)
        self.assertEqual(response.status, 403)
        secret = re.search(r'<span id="two_step_key" class="key">([^<]+)</span>', page).group(1).replace(" ", "")
        code = self.two_step.code_at(secret, self.two_step.current_step())
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "enable", "code": code}, user=someone)
        self.assertEqual(response.status, 200)
        self.assertTrue(self.shown(page, "two_step_codes"))
        self.assertIn('<a id="two_step_codes_done" class="button-link primary" href="/">', page)
        self.assertTrue(self.accounts.two_step_enabled(2))
        # Then on, to the temporary password's change.
        response, page = self.send("GET", "/", user=someone)
        self.assertEqual(response.status, 200)
        self.assertNotIn("two_step_verify_title", page)
        # The setup code was used up.
        self.cookies = {}
        response, page = self.send("GET", "/", user=someone)
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "verify", "code": setup_code}, user=someone)
        self.assertEqual(response.status, 400)

    @pytest.mark.real_local_network
    def test_after_losing_a_phone_a_setup_code_sets_up_a_new_one(self) -> None:
        someone, setup_code = self.someone_with_setup_code()
        old = self.accounts.start_two_step_setup(2)
        self.accounts.enable_two_step(2, old, self.two_step.code_at(old, self.two_step.current_step() - 1))
        setup_code = self.accounts.create_setup_code(2)
        response, page = self.send("GET", "/", user=someone)
        self.assertIn('<h2 id="two_step_verify_title">Enter your code</h2>', page)
        self.assertRegex(page, r'<p id="two_step_code_setup_hint" class="hint">')
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "verify", "code": setup_code}, user=someone)
        response, page = self.send("GET", "/two-step", user=someone)
        secret = re.search(r'<span id="two_step_key" class="key">([^<]+)</span>', page).group(1).replace(" ", "")
        self.assertNotEqual(secret, old)
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "enable", "code": self.two_step.code_at(secret, self.two_step.current_step())}, user=someone)
        self.assertTrue(self.shown(page, "two_step_codes"))
        # The lost phone's codes don't work any more.
        self.cookies = {}
        response, page = self.send("GET", "/", user=someone)
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "verify", "code": self.two_step.code_at(old, self.two_step.current_step())}, user=someone)
        self.assertEqual(response.status, 400)

    def test_only_the_owner_creates_setup_codes(self) -> None:
        someone, _setup_code = self.someone_with_setup_code()
        response, page = self.send("GET", "/two-step")
        self.assertIn('<button type="submit">Create setup code for someone</button>', page)
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "setup_code", "account_id": "2"})
        self.assertTrue(self.shown(page, "two_step_setup_code_section"))
        self.assertIn('<h2 id="two_step_setup_code_title">Setup code for someone</h2>', page)
        code = re.search(r'<input id="two_step_setup_code_value" class="key" readonly value="([^"]+)">', page).group(1)
        self.assertEqual(self.accounts.verify_two_step(self.accounts.account_by_id(2), code), "setup")
        self.accounts.change_password(2, someone[1], "password2", "password2")
        self.cookies = {}
        response, page = self.send("GET", "/two-step", user=("someone", "password2"))
        self.assertNotIn("Create setup code", page)
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "setup_code", "account_id": "1"}, user=("someone", "password2"))
        self.assertEqual(response.status, 400)
        self.assertIn("Only the owner can create setup codes.", page)

    def test_two_instances_at_one_host_name_dont_sign_each_other_out(self) -> None:
        # Such as two computers reached through SSH tunnels to localhost: the browser keeps one set of
        # cookies for both.
        other_accounts = self.wc.AccountStore(self.accounts.path.with_name("other.db"))
        other_accounts.create_admin("owner", "password1", "password1")
        service = StandInService(accounts=other_accounts, auth_sessions=self.wc.AuthSessionStore(), instance_id="run2")
        server = self.wc.ThreadingHTTPServer(("127.0.0.1", 0), type("OtherHandler", (self.wc.RtlControlHandler,), {"service": service}))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        first_port, other_port = self.port, server.server_address[1]
        self.assertEqual(self.send("GET", "/")[0].status, 200)
        self.port = other_port
        self.assertEqual(self.send("GET", "/")[0].status, 200)
        for port in (first_port, other_port):
            self.port = port
            self.assertEqual(self.send("GET", "/", auth=False)[0].status, 200)

    def test_on_the_local_network_its_set_up_and_turned_off(self) -> None:
        response, page = self.send("GET", "/")
        self.assertEqual(response.status, 200)  # the password is enough
        response, page = self.send("GET", "/two-step")
        self.assertIn("Two-step verification is off", page)
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "start"})
        self.assertTrue(self.shown(page, "two_step_setup"))
        place = socket.gethostname().split(".")[0]
        self.assertIn(f'<span id="two_step_setup_name">owner on {place}</span>', page)
        secret = re.search(r'<span id="two_step_key" class="key">([^<]+)</span>', page).group(1).replace(" ", "")
        self.assertIn(f"secret={secret}", html.unescape(page))
        code = self.two_step.code_at(secret, self.two_step.current_step())
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "enable", "code": code})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(re.findall(r"<li>[a-z0-9]{5}-[a-z0-9]{5}</li>", page)), 10)
        self.assertTrue(self.accounts.two_step_enabled(self.owner.id))
        response, page = self.send("GET", "/two-step")
        response, page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "disable"})
        self.assertTrue(self.shown(page, "two_step_confirm"))  # asked first
        self.assertTrue(self.accounts.two_step_enabled(self.owner.id))
        response, _page = self.send("POST", "/two-step", {"form_token": self.token(page), "action": "disable", "confirmed": "1"})
        self.assertEqual((response.status, response.getheader("Location")), (303, "/two-step?result=disabled"))
        self.assertFalse(self.accounts.two_step_enabled(self.owner.id))


if __name__ == "__main__":
    unittest.main()
