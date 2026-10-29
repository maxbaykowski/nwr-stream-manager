"""Limiting wrong passwords, so a password can't be found by trying one after another.

Each username gets a few wrong passwords free. After that, it has to wait before
the next try, and each wrong password doubles the wait, up to a limit. The wait
holds even for the right password, or it would tell a guesser when they'd found
it, but it never lasts longer than the limit, so nobody can keep the real person
out for long. A day without a wrong one starts again.

Signing in doesn't start it again, or someone guessing slowly would get their free
tries back each time the real person signed in. The count is kept in the accounts
database, so restarting NWR Stream Manager doesn't start it again either.

Each username's tries are worked out one at a time (one_at_a_time), from checking
whether it has to wait to counting the try: otherwise many tries sent at once would
all be let through before the first wrong one was counted.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Callable


FREE_WRONG_PASSWORDS = 5
FIRST_WAIT_SECONDS = 5.0
LONGEST_WAIT_SECONDS = 15 * 60.0
# Someone who has the password has only a code to guess, and a one in a million chance with each,
# so they wait longer.
LONGEST_CODE_WAIT_SECONDS = 60 * 60.0
# A username nobody has got wrong for a day starts again with its free tries.
FORGET_AFTER_SECONDS = 24 * 60 * 60.0
# Enough to remember everyone who signs in here, without letting a guesser fill memory.
MOST_USERNAMES = 10_000
# A browser keeps sending a password it was given, such as one that has since been
# changed, with each request. The same wrong password again is only one guess.
REMEMBERED_WRONG_PASSWORDS = 10
# Tries for different usernames are worked out at the same time, unless they share one of these.
ATTEMPT_LOCKS = 64


class SignInWait(Exception):
    """A username has to wait before its next try."""

    def __init__(self, seconds: float, what: str = "passwords") -> None:
        self.seconds = max(1, int(seconds + 0.999))
        super().__init__(sign_in_wait_message(self.seconds, what))


@dataclass
class _Tries:
    wrong: int = 0
    last_wrong_at: float = 0.0
    wait_until: float = 0.0
    wrong_passwords: list[bytes] = field(default_factory=list)


class SignInLimits:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        what: str = "passwords",
        *,
        longest_wait: float = LONGEST_WAIT_SECONDS,
        connect: Callable[[], sqlite3.Connection] | None = None,
        kind: str = "",
    ) -> None:
        # What's being tried, for saying so: passwords, or two-step verification codes.
        self.what = what
        self.clock = clock
        self.longest_wait = longest_wait
        self.lock = threading.Lock()
        self.tries: dict[str, _Tries] = {}
        # Where the counts are kept (a table in the accounts database), and which ones these are.
        self.connect = connect
        self.kind = kind
        self._load()
        # Wrong passwords are remembered only as a keyed hash, and the key never leaves memory.
        self.key = secrets.token_bytes(32)
        self.attempt_locks = [threading.Lock() for _ in range(ATTEMPT_LOCKS)]

    def one_at_a_time(self, username: str) -> threading.Lock:
        """Held from check to wrong_password, so a username's tries are counted one
        after another, however many are sent at once. Which lock a username has is kept secret, so
        nobody can choose a username that shares another's."""
        digest = hmac.new(self.key, _key(username).encode("utf-8", "replace"), hashlib.sha256).digest()
        return self.attempt_locks[int.from_bytes(digest[:4], "big") % ATTEMPT_LOCKS]

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
                # Never longer than the limit, even if the clock was put back.
                raise SignInWait(min(tries.wait_until - now, self.longest_wait), self.what)
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
            wait = 0.0
            if tries.wrong >= FREE_WRONG_PASSWORDS:
                wait = min(self.longest_wait, FIRST_WAIT_SECONDS * 2 ** min(tries.wrong - FREE_WRONG_PASSWORDS, 32))
                tries.wait_until = now + wait
            self._save_locked(_key(username), tries)
            return wait

    def _current_locked(self, username: str, now: float) -> _Tries | None:
        tries = self.tries.get(_key(username))
        if tries is not None and now - tries.last_wrong_at > FORGET_AFTER_SECONDS and tries.wait_until <= now:
            self._forget_locked(_key(username))
            return None
        return tries

    def _make_room_locked(self) -> None:
        if len(self.tries) < MOST_USERNAMES:
            return
        # The username whose last wrong password is oldest goes first.
        self._forget_locked(min(self.tries, key=lambda name: self.tries[name].last_wrong_at))

    def _load(self) -> None:
        if self.connect is None:
            return
        try:
            with self.connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS sign_in_limits ("
                    "kind TEXT NOT NULL, username TEXT NOT NULL, wrong INTEGER NOT NULL, last_wrong_at REAL NOT NULL, wait_until REAL NOT NULL, "
                    "PRIMARY KEY (kind, username))"
                )
                rows = connection.execute(
                    "SELECT username, wrong, last_wrong_at, wait_until FROM sign_in_limits WHERE kind = ? ORDER BY last_wrong_at DESC LIMIT ?",
                    (self.kind, MOST_USERNAMES),
                ).fetchall()
        except sqlite3.Error:
            return
        # Which wrong passwords they were isn't kept, as the key they're remembered by is new each time.
        self.tries = {str(row[0]): _Tries(int(row[1]), float(row[2]), float(row[3])) for row in rows}

    def _save_locked(self, key: str, tries: _Tries) -> None:
        if self.connect is None:
            return
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO sign_in_limits (kind, username, wrong, last_wrong_at, wait_until) VALUES (?, ?, ?, ?, ?)",
                    (self.kind, key, tries.wrong, tries.last_wrong_at, tries.wait_until),
                )
        except sqlite3.Error:
            pass  # still counted until NWR Stream Manager restarts

    def _forget_locked(self, key: str) -> None:
        del self.tries[key]
        if self.connect is None:
            return
        try:
            with self.connect() as connection:
                connection.execute("DELETE FROM sign_in_limits WHERE kind = ? AND username = ?", (self.kind, key))
        except sqlite3.Error:
            pass

    def _digest(self, username: str, password: str, stored_hash: str) -> bytes:
        text = f"{_key(username)}\0{stored_hash}\0{password}"
        return hmac.new(self.key, text.encode("utf-8", "replace"), hashlib.sha256).digest()


def _key(username: str) -> str:
    # Usernames are at most 64 characters; anything longer can't be one, but is still limited.
    return str(username)[:64]


def sign_in_wait_message(seconds: int, what: str = "passwords") -> str:
    if seconds < 60:
        wait = f"{seconds} second{'s' if seconds != 1 else ''}"
    else:
        minutes = (seconds + 59) // 60
        wait = f"{minutes} minute{'s' if minutes != 1 else ''}"
    return f"Too many wrong {what} for this username. Try again in {wait}."
