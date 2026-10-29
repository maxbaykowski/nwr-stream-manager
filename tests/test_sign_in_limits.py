from __future__ import annotations

import base64
import http.client
import json
import sqlite3
import sys
import tempfile
import threading
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

    def test_codes_wait_up_to_an_hour(self) -> None:
        limits = self.limits_module.SignInLimits(clock=self.clock, longest_wait=self.limits_module.LONGEST_CODE_WAIT_SECONDS)
        waits = []
        for i in range(16):
            self.clock.now += 4000
            waits.append(limits.wrong_password("owner", f"{i:06d}"))
        self.assertEqual(waits[-4:], [1280, 2560, 3600, 3600])

    def test_a_clock_put_back_doesnt_make_it_wait_longer(self) -> None:
        self.wrong(14)
        self.clock.now -= 10 * 24 * 60 * 60
        with self.assertRaises(self.limits_module.SignInWait) as raised:
            self.limits.check("owner", "guess", "hash1")
        self.assertEqual(raised.exception.seconds, 900)

    def test_its_kept_when_nwr_stream_manager_restarts(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = Path(tempdir.name) / "accounts.db"

        def connect():
            return sqlite3.connect(path)

        limits = self.limits_module.SignInLimits(clock=self.clock, connect=connect, kind="code")
        for i in range(6):
            limits.wrong_password("owner", f"{i:06d}")
        again = self.limits_module.SignInLimits(clock=self.clock, connect=connect, kind="code")
        with self.assertRaises(self.limits_module.SignInWait) as raised:
            again.check("owner", "123456")
        self.assertEqual(raised.exception.seconds, 10)
        # Each kind is counted on its own.
        self.assertTrue(self.limits_module.SignInLimits(clock=self.clock, connect=connect, kind="password").check("owner", "x"))
        # Forgotten after a day, there too.
        self.clock.now += 24 * 60 * 60 + 11
        self.assertTrue(again.check("owner", "123456"))
        self.assertEqual(self.limits_module.SignInLimits(clock=self.clock, connect=connect, kind="code").tries, {})

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
        # Signing in doesn't give a guesser their free tries back.
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            self.assertIsNone(self.store.verify_basic_account("owner", "again"))
        with self.assertRaises(self.limits_module.SignInWait):
            self.store.verify_basic_account("owner", "password1")

    def at_once(self, count: int, attempt) -> tuple[int, int]:
        """Makes count tries at the same moment: how many were checked, and how many were made to wait."""
        start = threading.Barrier(count)
        checked, waited = [], []

        def one(number: int) -> None:
            start.wait()
            try:
                attempt(number)
                checked.append(number)
            except self.limits_module.SignInWait:
                waited.append(number)

        threads = [threading.Thread(target=one, args=(number,)) for number in range(count)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return len(checked), len(waited)

    def test_wrong_passwords_sent_at_once_are_counted_one_after_another(self) -> None:
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            self.assertEqual(self.at_once(30, lambda number: self.store.verify_basic_account("owner", f"wrong{number}")), (5, 25))

    def test_wrong_codes_sent_at_once_are_counted_one_after_another(self) -> None:
        two_step = sys.modules["nwr_stream_manager.two_step"]
        owner = self.store.verify_basic_account("owner", "password1")
        secret = self.store.start_two_step_setup(owner.id)
        self.store.enable_two_step(owner.id, secret, two_step.code_at(secret, two_step.current_step() - 1))
        right = {two_step.code_at(secret, two_step.current_step() + offset) for offset in (-1, 0, 1)}
        wrong = [code for code in (f"{number:06d}" for number in range(100)) if code not in right]
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            self.assertEqual(self.at_once(60, lambda number: self.store.verify_two_step(owner, wrong[number])), (5, 55))

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
