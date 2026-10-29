from __future__ import annotations

import base64
import html
import http.client
import importlib
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

import pytest

from test_auth import load_web_control_module
from test_page_rendering import StandInService

IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
WINDOWS_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


class DeviceAndPlaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.alerts = importlib.import_module("nwr_stream_manager.sign_in_alerts")

    def test_devices_are_named_from_what_the_browser_says(self) -> None:
        describe = self.alerts.describe_device
        self.assertEqual(describe(IPHONE), "Apple iPhone, Safari")
        self.assertEqual(describe(WINDOWS_CHROME), "Windows computer, Chrome")
        self.assertEqual(describe(WINDOWS_CHROME + " Edg/130.0.0.0"), "Windows computer, Microsoft Edge")
        self.assertEqual(
            describe("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"),
            "Android phone or tablet, Chrome",
        )
        self.assertEqual(describe("Mozilla/5.0 (X11; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0"), "Linux computer, Firefox")
        self.assertEqual(describe("curl/8.9.1"), "a program (curl), not a web browser")
        self.assertEqual(describe("python-requests/2.32"), "a program (Python), not a web browser")
        self.assertEqual(describe(""), "an unrecognized device, which didn't say what it was")
        self.assertEqual(describe("SomethingElse/1.0"), "an unrecognized device")

    def test_places_are_named_by_whether_the_request_was_proven_local(self) -> None:
        nearby = self.alerts.nearby_without_lookup
        self.assertEqual(nearby("192.168.1.20", True), "on your local network")
        self.assertEqual(nearby("100.101.102.103", True), "on your tailnet")
        self.assertEqual(nearby("127.0.0.1", False), "on this computer (or a tunnel on it)")
        # The same addresses, not proven local: an internet provider's carrier-grade NAT, or a VPN.
        self.assertEqual(nearby("100.101.102.103", False), "on an internet provider's shared network")
        self.assertEqual(nearby("10.8.0.5", False), "on a private network that isn't your local network, such as a VPN")
        self.assertIsNone(nearby("31.13.64.9", False))  # looked up

    def test_addresses_the_internet_doesnt_know_are_never_looked_up(self) -> None:
        fetched = []
        places = self.alerts.AddressLocations(fetch=lambda address: fetched.append(address) or {})
        found = []
        for address in ("192.168.1.20", "100.101.102.103", "127.0.0.1", "fe80::1"):
            places.look_up(address, found.append)
        self.assertEqual(found, ["from an unknown place"] * 4)
        self.assertEqual(fetched, [])

    def test_public_addresses_are_looked_up_once_and_not_too_often(self) -> None:
        fetched = []
        done = threading.Event()

        def fetch(address):
            fetched.append(address)
            return {"city": "Kyiv", "region": "Kyiv City", "country": "UA"}

        places = self.alerts.AddressLocations(fetch=fetch)
        found = []
        places.look_up("31.13.64.9", lambda text: (found.append(text), done.set()))
        self.assertTrue(done.wait(5))
        self.assertIn(found[0], {"near Kyiv, Kyiv City, Ukraine", "near Kyiv, Kyiv City, UA"})
        places.look_up("31.13.64.9", found.append)  # remembered
        self.assertEqual(fetched, ["31.13.64.9"])
        for number in range(40):
            places.look_up(f"31.13.65.{number}", lambda text: None)
        time.sleep(0.2)
        self.assertEqual(len(fetched), self.alerts.LOCATION_LOOKUPS_PER_MINUTE)


class AccountAttemptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = self.wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        self.owner = self.accounts.verify_basic_account("owner", "password1")
        self.accounts.create_account("someone", False)
        self.someone = next(record for record in map(self.accounts.account_by_id, (2,)) if record)

    def wrong(self, username: str, count: int, address: str = "203.0.113.9", agent: str = IPHONE, first: int = 0) -> None:
        for number in range(first, first + count):
            # Each a different password: the same one again isn't another guess.
            self.accounts.verify_basic_account(username, f"wrong{number}", attempt={"address": address, "user_agent": agent})

    def test_a_few_wrong_passwords_arent_worth_telling(self) -> None:
        self.wrong("owner", 3)
        self.assertEqual(self.accounts.sign_in_attempts_for(self.owner)["own"], [])
        self.accounts.forget_few_sign_in_attempts(self.owner.id)
        self.wrong("owner", 1)
        self.assertEqual(self.accounts.sign_in_attempts_for(self.owner)["own"], [])

    def test_more_than_three_are_told_with_who_sent_them(self) -> None:
        self.wrong("owner", 4)
        own = self.accounts.sign_in_attempts_for(self.owner)["own"]
        self.assertEqual(len(own), 4)
        self.assertEqual((own[0]["address"], own[0]["device"], own[0]["kind"]), ("203.0.113.9", "Apple iPhone, Safari", "password"))
        # Signing in doesn't make them go away; being told does.
        self.accounts.forget_few_sign_in_attempts(self.owner.id)
        self.assertEqual(len(self.accounts.sign_in_attempts_for(self.owner)["own"]), 4)
        self.accounts.sign_in_attempts_seen(self.owner, own[-1]["id"])
        self.assertEqual(self.accounts.sign_in_attempts_for(self.owner)["own"], [])

    def test_the_owner_hears_about_other_accounts_and_usernames_that_arent(self) -> None:
        self.wrong("someone", 4)
        self.wrong("admin", 5, address="198.51.100.7", agent="curl/8.9.1")
        self.wrong("guest", 2)
        others = self.accounts.sign_in_attempts_for(self.owner)["others"]
        self.assertEqual({row["username"] for row in others}, {"someone", "admin"})
        self.assertIsNone(next(row for row in others if row["username"] == "admin")["account_id"])
        self.assertEqual(len(self.accounts.sign_in_attempts_for(self.someone)["own"]), 4)
        self.assertEqual(self.accounts.sign_in_attempts_for(self.someone)["others"], [])
        # The owner being told doesn't tell the account, or the other way around.
        self.accounts.sign_in_attempts_seen(self.owner, max(row["id"] for row in others) + 2)
        self.assertEqual(self.accounts.sign_in_attempts_for(self.owner)["others"], [])
        self.assertEqual(len(self.accounts.sign_in_attempts_for(self.someone)["own"]), 4)
        # guest wasn't shown, so its two go on adding up.
        self.wrong("guest", 2, address="198.51.100.8", first=10)
        self.assertEqual(len(self.accounts.sign_in_attempts_for(self.owner)["others"]), 4)


class SignInAlertPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.accounts = self.wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.accounts.create_admin("owner", "password1", "password1")
        service = StandInService(accounts=self.accounts, auth_sessions=self.wc.AuthSessionStore(), instance_id="run1")
        handler = type("AlertTestHandler", (self.wc.RtlControlHandler,), {"service": service})
        server = self.wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        self.cookies: dict[str, str] = {}

    def send(self, method, path, password="password1", form=None, **headers):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        headers = {name.replace("_", "-"): value for name, value in headers.items()}
        headers["Authorization"] = "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()
        headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in self.cookies.items())
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

    def test_after_more_than_three_wrong_passwords_the_next_sign_in_is_told_first(self) -> None:
        for number in range(3):
            self.assertEqual(self.send("GET", "/", password=f"wrong{number}", User_Agent=IPHONE)[0].status, 401)
        # A tunnel on this computer passes on who it was really from.
        self.send("GET", "/", password="wrong3", User_Agent=WINDOWS_CHROME, X_Forwarded_For="31.13.64.9")
        response, page = self.send("GET", "/?view=streams")
        self.assertEqual(response.status, 200)
        self.assertIn("<h1 id=\"sign_in_alert_title\">Someone tried to sign in</h1>", page)
        text = html.unescape(page)
        self.assertIn("there were 4 wrong passwords for your account, owner", text)
        # 127.0.0.1 counts as the local network in these tests (see conftest).
        self.assertIn("3 wrong passwords</strong> from 127.0.0.1, on your local network. Device: Apple iPhone, Safari. When: ", text)
        self.assertIn("1 wrong password</strong> from 31.13.64.9, from an unknown place. Device: Windows computer, Chrome. When: ", text)
        self.assertIn('href="/two-step">set up two-step verification</a>', page)
        token = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
        up_to = re.search(r'id="sign_in_alert_up_to" type="hidden" name="up_to" value="(\d+)"', page).group(1)
        response, _ = self.send("POST", "/sign-in-alert", form={"form_token": token, "up_to": up_to, "next": "/?view=streams"})
        self.assertEqual((response.status, response.getheader("Location")), (303, "/?view=streams"))
        response, page = self.send("GET", "/")
        self.assertNotIn("sign_in_alert_title", page)
        self.assertIn('id="view_dashboard"', page)

    @pytest.mark.real_local_network
    def test_wrong_codes_add_up_however_many_sessions_are_started(self) -> None:
        two_step = importlib.import_module("nwr_stream_manager.two_step")
        owner = self.accounts.verify_basic_account("owner", "password1")
        secret = self.accounts.start_two_step_setup(owner.id)
        self.accounts.enable_two_step(owner.id, secret, two_step.code_at(secret, two_step.current_step() - 1))
        right = {two_step.code_at(secret, two_step.current_step() + offset) for offset in (-1, 0, 1)}
        wrong = [code for code in (f"{number:06d}" for number in range(100)) if code not in right][:5]
        for code in wrong:
            # A new session each time, with nothing kept from the last.
            self.cookies = {}
            response, page = self.send("GET", "/")
            token = re.search(r'name="form_token" value="([^"]+)"', page).group(1)
            # The code sent with no session, so this request starts one too.
            self.cookies.pop(f"nwrstmgr_session_{self.accounts.cookie_id}", None)
            self.send("POST", "/two-step", form={"form_token": token, "action": "verify", "code": code, "next": "/"})
        self.assertEqual(len(self.accounts.sign_in_attempts_for(owner)["own"]), 5)

    def test_a_few_wrong_passwords_then_the_right_one_say_nothing(self) -> None:
        for number in range(3):
            self.send("GET", "/", password=f"wrong{number}")
        response, page = self.send("GET", "/")
        self.assertNotIn("sign_in_alert_title", page)
        # Forgotten at that sign-in, so one more later isn't the fourth.
        self.cookies = {}
        self.send("GET", "/", password="wrong9")
        response, page = self.send("GET", "/")
        self.assertNotIn("sign_in_alert_title", page)


if __name__ == "__main__":
    unittest.main()
