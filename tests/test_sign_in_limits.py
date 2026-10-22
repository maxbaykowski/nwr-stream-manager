from __future__ import annotations

import base64
import http.client
import json
import sys
import tempfile
import unittest
from pathlib import Path

from test_auth import load_web_control_module
from test_restart import RestartPageTests


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SignInLimitTests(unittest.TestCase):
    """Wrong passwords: a few free, then a wait that doubles, up to 15 minutes."""

    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.limits_module = sys.modules["nwr_stream_manager.sign_in_limits"]

    def setUp(self) -> None:
        self.clock = Clock()
        self.limits = self.limits_module.SignInLimits(clock=self.clock)

    def wrong(self, n, username="owner", stored="hash1"):
        return [self.limits.wrong_password(username, f"guess{self.clock.now}-{i}", stored) for i in range(n)]

    def test_five_wrong_passwords_are_free_then_the_wait_doubles(self) -> None:
        self.assertEqual(self.wrong(4), [0, 0, 0, 0])
        self.assertTrue(self.limits.check("owner", "anything", "hash1"))
        self.assertEqual(self.limits.wrong_password("owner", "fifth", "hash1"), 5)
        with self.assertRaises(self.limits_module.SignInWait) as raised:
            self.limits.check("owner", "the right one", "hash1")
        self.assertEqual(raised.exception.seconds, 5)
        self.assertEqual(str(raised.exception), "Too many wrong passwords for this username. Try again in 5 seconds.")
        waits = []
        for _ in range(9):
            self.clock.now += 1000
            waits.append(self.limits.wrong_password("owner", f"later{self.clock.now}", "hash1"))
        self.assertEqual(waits, [10, 20, 40, 80, 160, 320, 640, 900, 900])

    def test_waiting_doesnt_add_to_the_wait(self) -> None:
        self.wrong(5)
        for _ in range(20):
            with self.assertRaises(self.limits_module.SignInWait):
                self.limits.check("owner", "guess", "hash1")
        self.clock.now += 5
        self.assertTrue(self.limits.check("owner", "guess", "hash1"))

    def test_signing_in_starts_again(self) -> None:
        self.wrong(5)
        self.clock.now += 5
        self.limits.signed_in("owner")
        self.assertEqual(self.wrong(4), [0, 0, 0, 0])

    def test_each_username_is_limited_on_its_own(self) -> None:
        self.wrong(5, "owner")
        self.assertTrue(self.limits.check("helper", "guess", "hash2"))

    def test_the_same_wrong_password_again_is_one_guess(self) -> None:
        # A browser still holding an old password sends it with each request.
        for _ in range(20):
            self.assertEqual(self.limits.wrong_password("owner", "old password", "hash1"), 0)
        self.assertFalse(self.limits.check("owner", "old password", "hash1"))
        self.assertEqual(self.wrong(3), [0, 0, 0])
        self.assertEqual(self.limits.wrong_password("owner", "another", "hash1"), 5)

    def test_a_changed_password_forgets_the_wrong_ones(self) -> None:
        self.limits.wrong_password("owner", "new password", "hash1")
        self.assertFalse(self.limits.check("owner", "new password", "hash1"))
        self.assertTrue(self.limits.check("owner", "new password", "hash2"))

    def test_a_day_without_wrong_passwords_starts_again(self) -> None:
        self.wrong(4)
        self.clock.now += 24 * 60 * 60 + 1
        self.assertEqual(self.wrong(4), [0, 0, 0, 0])

    def test_minutes(self) -> None:
        self.assertEqual(self.limits_module.sign_in_wait_message(1), "Too many wrong passwords for this username. Try again in 1 second.")
        self.assertEqual(self.limits_module.sign_in_wait_message(60), "Too many wrong passwords for this username. Try again in 1 minute.")
        self.assertEqual(self.limits_module.sign_in_wait_message(61), "Too many wrong passwords for this username. Try again in 2 minutes.")
        self.assertEqual(self.limits_module.sign_in_wait_message(900), "Too many wrong passwords for this username. Try again in 15 minutes.")


class AccountSignInLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.limits_module = sys.modules["nwr_stream_manager.sign_in_limits"]

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.store = self.wc.AccountStore(Path(tempdir.name) / "accounts.db")
        self.store.create_admin("owner", "password1", "password1")
        self.clock = Clock()
        self.store.sign_in_limits.clock = self.clock

    def test_the_right_password_waits_too(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING") as logs:
            for i in range(5):
                self.assertIsNone(self.store.verify_basic_account("owner", f"wrong{i}"))
        self.assertIn("too many wrong passwords for 'owner'", logs.output[0])
        with self.assertRaises(self.limits_module.SignInWait):
            self.store.verify_basic_account("owner", "password1")
        self.clock.now += 5
        self.assertIsNotNone(self.store.verify_basic_account("owner", "password1"))
        # Signed in, so the free tries are back.
        for i in range(4):
            self.assertIsNone(self.store.verify_basic_account("owner", f"again{i}"))
        self.assertIsNotNone(self.store.verify_basic_account("owner", "password1"))

    def test_usernames_that_dont_exist_are_limited_the_same(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            for i in range(5):
                self.assertIsNone(self.store.verify_basic_account("nobody", f"wrong{i}"))
        with self.assertRaises(self.limits_module.SignInWait):
            self.store.verify_basic_account("nobody", "password1")
        self.assertIsNotNone(self.store.verify_basic_account("owner", "password1"))

    def test_a_new_password_works_even_if_it_was_once_wrong(self) -> None:
        self.assertIsNone(self.store.verify_basic_account("owner", "password2"))
        account = self.store.verify_basic_account("owner", "password1")
        self.store.change_password(account.id, "password1", "password2", "password2")
        self.assertIsNotNone(self.store.verify_basic_account("owner", "password2"))

    def test_changing_a_password_limits_guesses_at_the_current_one(self) -> None:
        account = self.store.verify_basic_account("owner", "password1")
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            for i in range(5):
                with self.assertRaisesRegex(ValueError, "current password is incorrect"):
                    self.store.change_password(account.id, f"guess{i}", "password2", "password2")
        # The same wait covers signing in, since both are guesses at the same password.
        with self.assertRaises(self.limits_module.SignInWait) as raised:
            self.store.change_password(account.id, "password1", "password2", "password2")
        self.assertIn("Try again in 5 seconds", str(raised.exception))
        with self.assertRaises(self.limits_module.SignInWait):
            self.store.verify_basic_account("owner", "password1")
        self.clock.now += 5
        self.store.change_password(account.id, "password1", "password2", "password2")
        self.assertIsNotNone(self.store.verify_basic_account("owner", "password2"))


class SignInWaitPageTests(unittest.TestCase):
    """What a browser gets while its username has to wait."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

    def setUp(self) -> None:
        RestartPageTests.setUp(self)  # type: ignore[arg-type]
        self.clock = Clock()
        self.accounts.sign_in_limits.clock = self.clock

    def get(self, path, password):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        auth = "Basic " + base64.b64encode(f"owner:{password}".encode()).decode()
        connection.request("GET", path, headers={"Authorization": auth})
        response = connection.getresponse()
        return response, response.read().decode("utf-8")

    def test_after_five_wrong_passwords_the_page_says_how_long_to_wait(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            for i in range(5):
                response, _page = self.get("/", f"wrong{i}")
                self.assertEqual(response.status, 401)
        response, page = self.get("/", "password1")
        self.assertEqual(response.status, 429)
        self.assertEqual(response.getheader("Retry-After"), "5")
        self.assertIsNone(response.getheader("WWW-Authenticate"))
        self.assertIn("Too many wrong passwords for this username. Try again in 5 seconds.", page)
        response, body = self.get("/api/status", "password1")
        self.assertEqual(response.status, 429)
        self.assertEqual(json.loads(body)["error"], "Too many wrong passwords for this username. Try again in 5 seconds.")
        self.clock.now += 5
        response, _page = self.get("/", "password1")
        self.assertEqual(response.status, 200)


if __name__ == "__main__":
    unittest.main()
