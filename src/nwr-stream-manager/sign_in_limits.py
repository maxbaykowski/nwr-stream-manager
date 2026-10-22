"""Limiting wrong passwords, so a password can't be found by trying one after another.

Each username gets a few wrong passwords free. After that, it has to wait before
the next try, and each wrong password doubles the wait, up to a limit. The wait
holds even for the right password, or it would tell a guesser when they'd found
it, but it never lasts longer than the limit, so nobody can keep the real person
out for long. Signing in resets it.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field


FREE_WRONG_PASSWORDS = 5
FIRST_WAIT_SECONDS = 5.0
LONGEST_WAIT_SECONDS = 15 * 60.0
# A username nobody has got wrong for a day starts again with its free tries.
FORGET_AFTER_SECONDS = 24 * 60 * 60.0
# Enough to remember everyone who signs in here, without letting a guesser fill memory.
MOST_USERNAMES = 10_000
# A browser keeps sending a password it was given, such as one that has since been
# changed, with each request. The same wrong password again is only one guess.
REMEMBERED_WRONG_PASSWORDS = 10


class SignInWait(Exception):
    """A username has to wait before its next try."""

    def __init__(self, seconds: float) -> None:
        self.seconds = max(1, int(seconds + 0.999))
        super().__init__(sign_in_wait_message(self.seconds))


@dataclass
class _Tries:
    wrong: int = 0
    last_wrong_at: float = 0.0
    wait_until: float = 0.0
    wrong_passwords: list[bytes] = field(default_factory=list)


class SignInLimits:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.tries: dict[str, _Tries] = {}
        # Wrong passwords are remembered only as a keyed hash, and the key never leaves memory.
        self.key = secrets.token_bytes(32)

    def check(self, username: str, password: str, stored_hash: str = "") -> bool:
        """Raises SignInWait if this username has to wait. False if the password is one already found wrong.

        stored_hash is the account's saved password hash, so a changed password
        forgets the wrong ones from before it.
        """
        now = self.clock()
        with self.lock:
            tries = self._current_locked(username, now)
            if tries is None:
                return True
            if tries.wait_until > now:
                raise SignInWait(tries.wait_until - now)
            return self._digest(username, password, stored_hash) not in tries.wrong_passwords

    def wrong_password(self, username: str, password: str, stored_hash: str = "") -> float:
        """Counts a wrong password. Returns how long the username must now wait, or 0."""
        now = self.clock()
        digest = self._digest(username, password, stored_hash)
        with self.lock:
            tries = self._current_locked(username, now)
            if tries is None:
                self._make_room_locked()
                tries = self.tries[_key(username)] = _Tries()
            if digest in tries.wrong_passwords:
                return 0.0
            tries.wrong += 1
            tries.last_wrong_at = now
            tries.wrong_passwords = [*tries.wrong_passwords, digest][-REMEMBERED_WRONG_PASSWORDS:]
            if tries.wrong < FREE_WRONG_PASSWORDS:
                return 0.0
            wait = min(LONGEST_WAIT_SECONDS, FIRST_WAIT_SECONDS * 2 ** (tries.wrong - FREE_WRONG_PASSWORDS))
            tries.wait_until = now + wait
            return wait

    def signed_in(self, username: str) -> None:
        with self.lock:
            self.tries.pop(_key(username), None)

    def _current_locked(self, username: str, now: float) -> _Tries | None:
        tries = self.tries.get(_key(username))
        if tries is not None and now - tries.last_wrong_at > FORGET_AFTER_SECONDS and tries.wait_until <= now:
            del self.tries[_key(username)]
            return None
        return tries

    def _make_room_locked(self) -> None:
        if len(self.tries) < MOST_USERNAMES:
            return
        # The username whose last wrong password is oldest goes first.
        oldest = min(self.tries, key=lambda name: self.tries[name].last_wrong_at)
        del self.tries[oldest]

    def _digest(self, username: str, password: str, stored_hash: str) -> bytes:
        text = f"{_key(username)}\0{stored_hash}\0{password}"
        return hmac.new(self.key, text.encode("utf-8", "replace"), hashlib.sha256).digest()


def _key(username: str) -> str:
    # Usernames are at most 64 characters; anything longer can't be one, but is still limited.
    return str(username)[:64]


def sign_in_wait_message(seconds: int) -> str:
    if seconds < 60:
        wait = f"{seconds} second{'s' if seconds != 1 else ''}"
    else:
        minutes = (seconds + 59) // 60
        wait = f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"Too many wrong passwords for this username. Try again in {wait}."
