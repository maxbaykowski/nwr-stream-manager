"""Accounts, passwords and sign-in sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from .constants import (
    ACCOUNT_ROLE_ADMIN,
    ACCOUNT_ROLE_OWNER,
    ACCOUNT_ROLE_READ_ONLY,
    ACCOUNT_ROLES,
    AUTH_SESSION_SECONDS,
    PASSWORD_HASH_SCHEME,
    PASSWORD_HASH_VERSION,
    PASSWORD_RE,
    SCRYPT_DKLEN,
    SCRYPT_N,
    SCRYPT_P,
    SCRYPT_R,
    USERNAME_RE,
)
from .sign_in_limits import LONGEST_CODE_WAIT_SECONDS, SignInLimits
from . import sign_in_alerts, two_step


LOG = logging.getLogger(__name__)
# How long setting up two-step verification can take, from showing the key to entering the app's first code.
TWO_STEP_SETUP_SECONDS = 30 * 60
# More wrong passwords or codes than this, and the account is told at its next sign-in (see handler_sign_in_alerts).
SIGN_IN_ALERT_AFTER = 3
# Wrong tries kept, oldest forgotten first, so a guesser can't fill the disk.
SIGN_IN_ATTEMPTS_KEPT = 10_000
SIGN_IN_ATTEMPTS_KEPT_SECONDS = 90 * 24 * 3600
# Plenty for every browser and device someone uses.
SESSIONS_PER_ACCOUNT = 50
# Each request moves a session's expiry on, but it's only saved again once it's moved this far.
SESSION_SAVE_SECONDS = 5 * 60


def validate_account_username(username: str) -> str:
    username = str(username).strip()
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("username may contain only letters, numbers, hyphen, and underscore")
    return username


def validate_account_password(password: str) -> str:
    password = str(password)
    if not PASSWORD_RE.fullmatch(password):
        raise ValueError("password must be 8-256 printable non-space ASCII characters")
    return password


def hash_account_password(password: str) -> str:
    password = validate_account_password(password)
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
    )
    return "scrypt$v={}$n={}$r={}$p={}$dklen={}$salt={}$hash={}".format(
        PASSWORD_HASH_VERSION,
        SCRYPT_N,
        SCRYPT_R,
        SCRYPT_P,
        SCRYPT_DKLEN,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def parse_account_password_hash(encoded_hash: str) -> dict[str, Any]:
    parts = str(encoded_hash).split("$")
    if not parts or parts[0] != PASSWORD_HASH_SCHEME:
        raise ValueError("unsupported password hash scheme")
    if len(parts) == 6:
        _scheme, n_raw, r_raw, p_raw, salt_raw, digest_raw = parts
        return {
            "scheme": PASSWORD_HASH_SCHEME,
            "version": 0,
            "n": int(n_raw),
            "r": int(r_raw),
            "p": int(p_raw),
            "dklen": None,
            "salt": base64.b64decode(salt_raw.encode("ascii"), validate=True),
            "digest": base64.b64decode(digest_raw.encode("ascii"), validate=True),
        }
    values: dict[str, str] = {}
    for part in parts[1:]:
        key, separator, value = part.partition("=")
        if not separator:
            raise ValueError("invalid password hash parameter")
        values[key] = value
    required = {"v", "n", "r", "p", "dklen", "salt", "hash"}
    if set(values) != required:
        raise ValueError("invalid password hash parameters")
    return {
        "scheme": PASSWORD_HASH_SCHEME,
        "version": int(values["v"]),
        "n": int(values["n"]),
        "r": int(values["r"]),
        "p": int(values["p"]),
        "dklen": int(values["dklen"]),
        "salt": base64.b64decode(values["salt"].encode("ascii"), validate=True),
        "digest": base64.b64decode(values["hash"].encode("ascii"), validate=True),
    }


def account_password_hash_needs_upgrade(encoded_hash: str) -> bool:
    try:
        parsed = parse_account_password_hash(encoded_hash)
    except (TypeError, ValueError, binascii.Error):
        return True
    return (
        int(parsed["version"]) < PASSWORD_HASH_VERSION
        or int(parsed["n"]) < SCRYPT_N
        or int(parsed["r"]) < SCRYPT_R
        or int(parsed["p"]) < SCRYPT_P
        or int(parsed["dklen"] or len(parsed["digest"])) < SCRYPT_DKLEN
    )


def verify_account_password(password: str, encoded_hash: str) -> bool:
    try:
        parsed = parse_account_password_hash(encoded_hash)
        expected = parsed["digest"]
        digest = hashlib.scrypt(
            str(password).encode("utf-8"),
            salt=parsed["salt"],
            n=int(parsed["n"]),
            r=int(parsed["r"]),
            p=int(parsed["p"]),
            dklen=len(expected),
        )
    except (TypeError, ValueError, binascii.Error):
        return False
    return hmac.compare_digest(digest, expected)


@dataclass(frozen=True)
class AccountRecord:
    id: int
    username: str
    role: str
    must_change_password: bool
    created_at: float
    last_accessed_at: float | None

    @property
    def is_owner(self) -> bool:
        return self.role == ACCOUNT_ROLE_OWNER

    @property
    def is_read_only(self) -> bool:
        return self.role == ACCOUNT_ROLE_READ_ONLY

    def to_public_dict(self) -> dict[str, Any]:
        if self.role == ACCOUNT_ROLE_OWNER:
            account_type = "Owner"
        elif self.role == ACCOUNT_ROLE_READ_ONLY:
            account_type = "Read-only"
        else:
            account_type = "Administrator"
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "account_type": account_type,
            "read_only": self.is_read_only,
            "owner": self.is_owner,
            "must_change_password": self.must_change_password,
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
        }


@dataclass
class AuthSession:
    account_id: int
    expires_at: float
    # Whether two-step verification was passed in this session, for requests from outside the local network.
    two_step_passed: bool = False
    # Whether a setup code was, which lets the session set up two-step verification and do nothing else.
    two_step_setup_only: bool = False
    # When it was last saved to expire, so each request doesn't have to save it again.
    saved_expires_at: float = 0.0


@lru_cache(maxsize=1)
def _unknown_username_hash() -> str:
    """A hash no password matches, checked against for usernames that don't exist."""
    return hash_account_password(secrets.token_urlsafe(32))


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.sign_in_limits = SignInLimits(connect=self._connect, kind="password")
        self.two_step_limits = SignInLimits(
            what="two-step verification codes", longest_wait=LONGEST_CODE_WAIT_SECONDS, connect=self._connect, kind="code"
        )
        # Called with each wrong try's row and address once it's saved, to find where the address is.
        self.on_sign_in_attempt: Any = None
        # Keys being set up, by account, until the app's first code shows it has them.
        self.two_step_setups: dict[int, tuple[str, float]] = {}
        self._ensure_schema()
        self._ensure_two_step_schema()
        self.cookie_id = self._load_or_make_cookie_id()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'accounts'"
            ).fetchone()
            existing_sql = str(row["sql"]) if row is not None else ""
            if row is not None and ("CHECK (id = 1)" in existing_sql or "role" not in existing_sql):
                connection.execute("ALTER TABLE accounts RENAME TO accounts_legacy")
                connection.execute(
                    """
                    CREATE TABLE accounts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        username TEXT NOT NULL UNIQUE,
                        password TEXT NOT NULL,
                        role TEXT NOT NULL DEFAULT 'administrator',
                        must_change_password INTEGER NOT NULL DEFAULT 0,
                        created_at REAL NOT NULL DEFAULT 0,
                        last_accessed_at REAL
                    )
                    """
                )
                now = time.time()
                connection.execute(
                    """
                    INSERT INTO accounts (id, username, password, role, must_change_password, created_at, last_accessed_at)
                    SELECT id, username, password, 'owner', must_change_password, ?, NULL FROM accounts_legacy
                    """,
                    (now,),
                )
                connection.execute("DROP TABLE accounts_legacy")
                connection.commit()
                return
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'administrator',
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL DEFAULT 0,
                    last_accessed_at REAL
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(accounts)").fetchall()}
            if "role" not in columns:
                connection.execute("ALTER TABLE accounts ADD COLUMN role TEXT NOT NULL DEFAULT 'administrator'")
            if "created_at" not in columns:
                connection.execute("ALTER TABLE accounts ADD COLUMN created_at REAL NOT NULL DEFAULT 0")
                connection.execute("UPDATE accounts SET created_at = ? WHERE created_at = 0", (time.time(),))
            if "last_accessed_at" not in columns:
                connection.execute("ALTER TABLE accounts ADD COLUMN last_accessed_at REAL")
            owner = connection.execute("SELECT id FROM accounts WHERE role = ? LIMIT 1", (ACCOUNT_ROLE_OWNER,)).fetchone()
            first = connection.execute("SELECT id FROM accounts ORDER BY id LIMIT 1").fetchone()
            if owner is None and first is not None:
                connection.execute("UPDATE accounts SET role = ? WHERE id = ?", (ACCOUNT_ROLE_OWNER, int(first["id"])))
            connection.commit()

    def _row_to_record(self, row: sqlite3.Row | None) -> AccountRecord | None:
        if row is None:
            return None
        role = str(row["role"] or ACCOUNT_ROLE_ADMIN)
        if role not in ACCOUNT_ROLES:
            role = ACCOUNT_ROLE_ADMIN
        last_accessed_at = row["last_accessed_at"]
        return AccountRecord(
            id=int(row["id"]),
            username=str(row["username"]),
            role=role,
            must_change_password=bool(row["must_change_password"]),
            created_at=float(row["created_at"] or 0.0),
            last_accessed_at=float(last_accessed_at) if last_accessed_at is not None else None,
        )

    def has_account(self) -> bool:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
            return row is not None

    def create_admin(self, username: str, password: str, confirm_password: str) -> None:
        username = validate_account_username(username)
        password = validate_account_password(password)
        if password != str(confirm_password):
            raise ValueError("passwords do not match")
        password_hash = hash_account_password(password)
        with self.lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None:
                raise ValueError("an administrator account already exists")
            connection.execute(
                """
                INSERT INTO accounts (username, password, role, must_change_password, created_at)
                VALUES (?, ?, ?, 0, ?)
                """,
                (username, password_hash, ACCOUNT_ROLE_OWNER, time.time()),
            )
            connection.commit()

    def _generate_secret(self) -> str:
        return secrets.token_urlsafe(12)

    def list_accounts(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY id").fetchall()
            return [record.to_public_dict() for row in rows if (record := self._row_to_record(row)) is not None]

    def account_by_id(self, account_id: int) -> AccountRecord | None:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            return self._row_to_record(row)

    def create_account(self, username: str, read_only: bool) -> tuple[dict[str, Any], str]:
        username = validate_account_username(username)
        secret = self._generate_secret()
        role = ACCOUNT_ROLE_READ_ONLY if read_only else ACCOUNT_ROLE_ADMIN
        with self.lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO accounts (username, password, role, must_change_password, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (username, hash_account_password(secret), role, time.time()),
            )
            row = connection.execute("SELECT * FROM accounts WHERE username = ? LIMIT 1", (username,)).fetchone()
            connection.commit()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account could not be created")
            return record.to_public_dict(), secret

    def reset_password(self, account_id: int) -> tuple[dict[str, Any], str]:
        secret = self._generate_secret()
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            if row is None:
                raise ValueError("account was not found")
            connection.execute(
                "UPDATE accounts SET password = ?, must_change_password = 1 WHERE id = ?",
                (hash_account_password(secret), int(account_id)),
            )
            connection.execute("DELETE FROM remembered_devices WHERE account_id = ?", (int(account_id),))
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            connection.commit()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            return record.to_public_dict(), secret

    def set_read_only(self, account_id: int, read_only: bool) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            if record.is_owner:
                raise ValueError("the owner account cannot be made read-only")
            role = ACCOUNT_ROLE_READ_ONLY if read_only else ACCOUNT_ROLE_ADMIN
            connection.execute("UPDATE accounts SET role = ? WHERE id = ?", (role, record.id))
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (record.id,)).fetchone()
            connection.commit()
            updated = self._row_to_record(row)
            if updated is None:
                raise ValueError("account was not found")
            return updated.to_public_dict()

    def delete_account(self, account_id: int, requester_id: int) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
            record = self._row_to_record(row)
            if record is None:
                raise ValueError("account was not found")
            if record.is_owner:
                raise ValueError("the owner account cannot be deleted")
            if record.id == int(requester_id):
                raise ValueError("you cannot delete the account you are using")
            connection.execute("DELETE FROM accounts WHERE id = ?", (record.id,))
            for table in ("two_step", "two_step_recovery", "two_step_setup_codes", "remembered_devices", "sign_in_attempts"):
                connection.execute(f"DELETE FROM {table} WHERE account_id = ?", (record.id,))
            connection.commit()
            return record.to_public_dict()

    def change_password(self, account_id: int, current_password: str, new_password: str, confirm_password: str) -> dict[str, Any]:
        new_password = validate_account_password(new_password)
        if new_password != str(confirm_password):
            raise ValueError("passwords do not match")
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(account_id),)).fetchone()
        record = self._row_to_record(row)
        if record is None:
            raise ValueError("account was not found")
        # Guessing the current password here is limited as signing in is: someone using a
        # signed-in browser could otherwise find the password, and keep the account after.
        password_hash = str(row["password"])
        with self.sign_in_limits.one_at_a_time(record.username):
            known_wrong = not self.sign_in_limits.check(record.username, str(current_password), password_hash)
            if known_wrong or not verify_account_password(str(current_password), password_hash):
                if not known_wrong:
                    wait = self.sign_in_limits.wrong_password(record.username, str(current_password), password_hash)
                    if wait:
                        LOG.warning("too many wrong passwords for %r; its next try has to wait %d seconds", record.username, wait)
                raise ValueError("current password is incorrect")
        new_hash = hash_account_password(new_password)
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (record.id,)).fetchone()
            if row is None or str(row["password"]) != password_hash:
                raise ValueError("the password was changed while this was being saved; try again")
            connection.execute(
                "UPDATE accounts SET password = ?, must_change_password = 0 WHERE id = ?",
                (new_hash, record.id),
            )
            connection.execute("DELETE FROM remembered_devices WHERE account_id = ?", (record.id,))
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (record.id,)).fetchone()
            connection.commit()
            updated = self._row_to_record(row)
            if updated is None:
                raise ValueError("account was not found")
            return updated.to_public_dict()

    def verify_basic_account(self, username: str, password: str, attempt: dict[str, str] | None = None) -> AccountRecord | None:
        """The account, if the password is right. Raises SignInWait after too many wrong passwords.

        attempt says who's trying (their address and user_agent), saved if the password's wrong."""
        with self.sign_in_limits.one_at_a_time(username):
            return self._verify_basic_account(username, password, attempt)

    def _verify_basic_account(self, username: str, password: str, attempt: dict[str, str] | None) -> AccountRecord | None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE username = ? LIMIT 1",
                (str(username),),
            ).fetchone()
        password_hash = str(row["password"]) if row is not None else ""
        if not self.sign_in_limits.check(username, password, password_hash):
            return None
        # Checked outside the lock, which would otherwise hold up everyone else while it's worked out.
        # A username that doesn't exist takes as long, so how long it takes doesn't say which ones do.
        right = verify_account_password(password, password_hash or _unknown_username_hash())
        if row is None or not right:
            wait = self.sign_in_limits.wrong_password(username, password, password_hash)
            if attempt is not None:
                self.record_sign_in_attempt(str(username)[:64], int(row["id"]) if row is not None else None, "password", attempt)
            if wait:
                LOG.warning("too many wrong passwords for %r; its next try has to wait %d seconds", str(username)[:64], wait)
            return None
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(row["id"]),)).fetchone()
            if row is None or str(row["password"]) != password_hash:
                # Changed or deleted while the password was being checked.
                return None
            if account_password_hash_needs_upgrade(password_hash):
                connection.execute(
                    "UPDATE accounts SET password = ? WHERE id = ?",
                    (hash_account_password(password), int(row["id"])),
                )
            connection.execute("UPDATE accounts SET last_accessed_at = ? WHERE id = ?", (time.time(), int(row["id"])))
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (int(row["id"]),)).fetchone()
            connection.commit()
            return self._row_to_record(row)

    def _load_or_make_cookie_id(self) -> str:
        """What this NWR Stream Manager's cookie names end with. Browsers keep cookies by host name, not
        port, so two NWR Stream Managers reached at the same name, such as through port forwarding or
        SSH tunnels to localhost, would otherwise replace each other's cookies. It's kept with the
        accounts, as the sessions and remembered devices the cookies stand for belong to them."""
        with self.lock, self._connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS instance (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            connection.execute("INSERT OR IGNORE INTO instance (name, value) VALUES ('cookie_id', ?)", (secrets.token_hex(4),))
            connection.commit()
            return str(connection.execute("SELECT value FROM instance WHERE name = 'cookie_id'").fetchone()["value"])

    # -- two-step verification ----------------------------------------------

    def _ensure_two_step_schema(self) -> None:
        with self.lock, self._connect() as connection:
            # The key has to be kept as it is, to work out each code; only this user can read accounts.db.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS two_step (
                    account_id INTEGER PRIMARY KEY,
                    secret TEXT NOT NULL,
                    last_step INTEGER NOT NULL DEFAULT -1,
                    enabled_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS two_step_recovery (account_id INTEGER NOT NULL, code_hash TEXT NOT NULL, PRIMARY KEY (account_id, code_hash))"
            )
            # Devices that passed two-step verification and asked to be remembered, by a hash of the cookie they keep.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS remembered_devices (token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, expires_at REAL NOT NULL)"
            )
            # Setup codes the owner gave out, one at most for each account, by hash, until used or expired.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS two_step_setup_codes (account_id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL, expires_at REAL NOT NULL)"
            )
            # Wrong passwords and codes, with who sent them, until the account (seen) and the owner
            # (owner_seen) have been told.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sign_in_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    at REAL NOT NULL,
                    username TEXT NOT NULL,
                    account_id INTEGER,
                    kind TEXT NOT NULL,
                    address TEXT NOT NULL,
                    user_agent TEXT NOT NULL,
                    device TEXT NOT NULL,
                    location TEXT NOT NULL DEFAULT '',
                    seen INTEGER NOT NULL DEFAULT 0,
                    owner_seen INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.commit()

    def start_two_step_setup(self, account_id: int) -> str:
        secret = two_step.new_secret()
        with self.lock:
            self.two_step_setups[int(account_id)] = (secret, time.monotonic() + TWO_STEP_SETUP_SECONDS)
        return secret

    def two_step_setup_secret(self, account_id: int) -> str:
        """The key being set up for account, or "" if none is, or it's been too long."""
        with self.lock:
            secret, expires = self.two_step_setups.get(int(account_id), ("", 0.0))
        return secret if expires > time.monotonic() else ""

    def two_step_enabled(self, account_id: int) -> bool:
        with self.lock, self._connect() as connection:
            return connection.execute("SELECT 1 FROM two_step WHERE account_id = ?", (int(account_id),)).fetchone() is not None

    def two_step_status(self, account_id: int) -> dict[str, Any]:
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT enabled_at FROM two_step WHERE account_id = ?", (int(account_id),)).fetchone()
            left = connection.execute("SELECT COUNT(*) FROM two_step_recovery WHERE account_id = ?", (int(account_id),)).fetchone()[0]
        return {"enabled": row is not None, "enabled_at": float(row["enabled_at"]) if row else None, "recovery_codes_left": int(left)}

    def two_step_account_ids(self) -> set[int]:
        with self.lock, self._connect() as connection:
            return {int(row["account_id"]) for row in connection.execute("SELECT account_id FROM two_step").fetchall()}

    def _replace_recovery_codes_locked(self, connection: sqlite3.Connection, account_id: int) -> list[str]:
        codes = two_step.new_recovery_codes()
        connection.execute("DELETE FROM two_step_recovery WHERE account_id = ?", (account_id,))
        connection.executemany(
            "INSERT OR IGNORE INTO two_step_recovery (account_id, code_hash) VALUES (?, ?)",
            [(account_id, two_step.recovery_code_hash(code)) for code in codes],
        )
        return codes

    def enable_two_step(self, account_id: int, secret: str, code: str) -> list[str]:
        """Turns two-step verification on with secret, once code shows the app has it. The new recovery codes."""
        step = two_step.matching_step(secret, code)
        if step is None:
            raise ValueError("That code isn't right. Check that your authenticator app has the key above, and enter the code it shows now.")
        with self.lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)).fetchone() is None:
                raise ValueError("account was not found")
            connection.execute(
                "INSERT OR REPLACE INTO two_step (account_id, secret, last_step, enabled_at) VALUES (?, ?, ?, ?)",
                (int(account_id), secret, step, time.time()),
            )
            codes = self._replace_recovery_codes_locked(connection, int(account_id))
            connection.execute("DELETE FROM two_step_setup_codes WHERE account_id = ?", (int(account_id),))
            connection.commit()
            self.two_step_setups.pop(int(account_id), None)
        return codes

    def create_setup_code(self, account_id: int) -> str:
        """A new setup code for account, in place of any it had, working once for SETUP_CODE_SECONDS."""
        code = two_step.new_setup_code()
        with self.lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM accounts WHERE id = ?", (int(account_id),)).fetchone() is None:
                raise ValueError("account was not found")
            connection.execute(
                "INSERT OR REPLACE INTO two_step_setup_codes (account_id, code_hash, expires_at) VALUES (?, ?, ?)",
                (int(account_id), two_step.recovery_code_hash(code), time.time() + two_step.SETUP_CODE_SECONDS),
            )
            connection.commit()
        return code

    def setup_code_waiting(self, account_id: int) -> bool:
        """Whether account has a setup code that hasn't been used or expired."""
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM two_step_setup_codes WHERE account_id = ? AND expires_at > ?", (int(account_id), time.time())
            ).fetchone()
        return row is not None

    def disable_two_step(self, account_id: int) -> None:
        with self.lock, self._connect() as connection:
            for table in ("two_step", "two_step_recovery", "remembered_devices"):
                connection.execute(f"DELETE FROM {table} WHERE account_id = ?", (int(account_id),))
            connection.commit()

    def new_recovery_codes(self, account_id: int) -> list[str]:
        with self.lock, self._connect() as connection:
            if connection.execute("SELECT 1 FROM two_step WHERE account_id = ?", (int(account_id),)).fetchone() is None:
                raise ValueError("two-step verification isn't turned on for this account")
            codes = self._replace_recovery_codes_locked(connection, int(account_id))
            connection.commit()
        return codes

    def verify_two_step(self, account: AccountRecord, code: str, attempt: dict[str, str] | None = None) -> str:
        """What code is for account: "code" for a right authenticator code or unused recovery code,
        "setup" for its setup code, or "" if it's neither. A recovery or setup code is used up. Raises
        SignInWait after too many wrong codes, as for passwords."""
        with self.two_step_limits.one_at_a_time(account.username):
            return self._verify_two_step(account, code, attempt)

    def _verify_two_step(self, account: AccountRecord, code: str, attempt: dict[str, str] | None) -> str:
        code = str(code).strip()[:64]
        if not self.two_step_limits.check(account.username, code):
            return ""
        with self.lock, self._connect() as connection:
            row = connection.execute("SELECT secret, last_step FROM two_step WHERE account_id = ?", (account.id,)).fetchone()
            right = ""
            looks_like_recovery = two_step.looks_like_recovery_code(code)
            if looks_like_recovery and connection.execute(
                "DELETE FROM two_step_setup_codes WHERE account_id = ? AND code_hash = ? AND expires_at > ?",
                (account.id, two_step.recovery_code_hash(code), time.time()),
            ).rowcount == 1:
                right = "setup"
                LOG.info("account %s used a two-step verification setup code", account.username)
            elif row is None:
                pass  # nothing else to check it against
            elif looks_like_recovery:
                used = connection.execute(
                    "DELETE FROM two_step_recovery WHERE account_id = ? AND code_hash = ?",
                    (account.id, two_step.recovery_code_hash(code)),
                )
                if used.rowcount == 1:
                    right = "code"
                    LOG.info("account %s used a two-step verification recovery code", account.username)
            else:
                step = two_step.matching_step(str(row["secret"]), code, int(row["last_step"]))
                if step is not None:
                    connection.execute("UPDATE two_step SET last_step = ? WHERE account_id = ?", (step, account.id))
                    right = "code"
            connection.commit()
        if not right:
            if attempt is not None:
                self.record_sign_in_attempt(account.username, account.id, "code", attempt)
            wait = self.two_step_limits.wrong_password(account.username, code)
            if wait:
                LOG.warning("too many wrong two-step verification codes for %r; its next try has to wait %d seconds", account.username, wait)
            return ""
        return right

    def remember_device(self, account_id: int) -> str:
        token = secrets.token_urlsafe(32)
        with self.lock, self._connect() as connection:
            now = time.time()
            connection.execute("DELETE FROM remembered_devices WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO remembered_devices (token_hash, account_id, expires_at) VALUES (?, ?, ?)",
                (_token_hash(token), int(account_id), now + two_step.REMEMBER_DEVICE_SECONDS),
            )
            connection.commit()
        return token

    def device_remembered(self, account_id: int, token: str) -> bool:
        if not token:
            return False
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT account_id, expires_at FROM remembered_devices WHERE token_hash = ?", (_token_hash(token),)
            ).fetchone()
        return row is not None and int(row["account_id"]) == int(account_id) and float(row["expires_at"]) > time.time()

    def forget_device(self, token: str) -> None:
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM remembered_devices WHERE token_hash = ?", (_token_hash(token),))
            connection.commit()

    def forget_devices(self, account_id: int) -> None:
        with self.lock, self._connect() as connection:
            connection.execute("DELETE FROM remembered_devices WHERE account_id = ?", (int(account_id),))
            connection.commit()

    # -- wrong passwords and codes, for telling the account ----------------------

    def record_sign_in_attempt(self, username: str, account_id: int | None, kind: str, attempt: dict[str, str]) -> None:
        address = str(attempt.get("address", ""))[:64]
        user_agent = str(attempt.get("user_agent", ""))[:512]
        device = sign_in_alerts.describe_device(user_agent)
        # Where it is, if that's known without looking it up.
        place = sign_in_alerts.nearby_without_lookup(address, bool(attempt.get("local")))
        with self.lock, self._connect() as connection:
            row_id = connection.execute(
                "INSERT INTO sign_in_attempts (at, username, account_id, kind, address, user_agent, device, location) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), username, account_id, kind, address, user_agent, device, place or ""),
            ).lastrowid
            connection.execute("DELETE FROM sign_in_attempts WHERE at < ?", (time.time() - SIGN_IN_ATTEMPTS_KEPT_SECONDS,))
            connection.execute(
                "DELETE FROM sign_in_attempts WHERE id <= (SELECT id FROM sign_in_attempts ORDER BY id DESC LIMIT 1 OFFSET ?)",
                (SIGN_IN_ATTEMPTS_KEPT,),
            )
            unseen = connection.execute(
                "SELECT COUNT(*) FROM sign_in_attempts WHERE username = ? AND seen = 0", (username,)
            ).fetchone()[0]
            connection.commit()
        what = "wrong two-step verification code" if kind == "code" else "wrong password"
        LOG.warning("%s for %r%s from %s, %s (%s)", what, username, "" if account_id is not None else " (not an account)", address, device, user_agent or "no user agent")
        if unseen == SIGN_IN_ALERT_AFTER + 1:
            LOG.warning("%r has had more than %d wrong passwords or codes; it'll be told when it next signs in", username, SIGN_IN_ALERT_AFTER)
        if place is None and self.on_sign_in_attempt is not None:
            self.on_sign_in_attempt(int(row_id), address)

    def set_sign_in_attempt_location(self, row_id: int, location: str) -> None:
        with self.lock, self._connect() as connection:
            connection.execute("UPDATE sign_in_attempts SET location = ? WHERE id = ?", (location, int(row_id)))
            connection.commit()

    def sign_in_attempts_for(self, account: AccountRecord) -> dict[str, list[dict[str, Any]]]:
        """What the account hasn't been told: its own wrong tries, if there are more than a few,
        and for the owner, other usernames' that are."""
        with self.lock, self._connect() as connection:
            own = [dict(row) for row in connection.execute(
                "SELECT * FROM sign_in_attempts WHERE account_id = ? AND seen = 0 ORDER BY id", (account.id,)
            ).fetchall()]
            others: list[dict[str, Any]] = []
            if account.is_owner:
                others = [dict(row) for row in connection.execute(
                    "SELECT * FROM sign_in_attempts WHERE (account_id IS NULL OR account_id != ?) AND owner_seen = 0 ORDER BY id", (account.id,)
                ).fetchall()]
        counts: dict[str, int] = {}
        for row in others:
            counts[row["username"]] = counts.get(row["username"], 0) + 1
        return {
            "own": own if len(own) > SIGN_IN_ALERT_AFTER else [],
            "others": [row for row in others if counts[row["username"]] > SIGN_IN_ALERT_AFTER],
        }

    def sign_in_attempts_seen(self, account: AccountRecord, up_to_id: int) -> None:
        """account has been told about every wrong try up to up_to_id."""
        with self.lock, self._connect() as connection:
            connection.execute("UPDATE sign_in_attempts SET seen = 1 WHERE account_id = ? AND id <= ?", (account.id, int(up_to_id)))
            if account.is_owner:
                # Only the usernames the owner was shown: ones with only a few go on adding up.
                connection.execute(
                    """
                    UPDATE sign_in_attempts SET owner_seen = 1
                    WHERE (account_id IS NULL OR account_id != ?) AND owner_seen = 0 AND id <= ? AND username IN (
                        SELECT username FROM sign_in_attempts
                        WHERE (account_id IS NULL OR account_id != ?) AND owner_seen = 0 AND id <= ?
                        GROUP BY username HAVING COUNT(*) > ?
                    )
                    """,
                    (account.id, int(up_to_id), account.id, int(up_to_id), SIGN_IN_ALERT_AFTER),
                )
            connection.commit()

    def forget_few_sign_in_attempts(self, account_id: int) -> None:
        """At sign-in, a few wrong tries aren't worth telling: likely typing mistakes."""
        with self.lock, self._connect() as connection:
            unseen = connection.execute("SELECT COUNT(*) FROM sign_in_attempts WHERE account_id = ? AND seen = 0", (int(account_id),)).fetchone()[0]
            if 0 < unseen <= SIGN_IN_ALERT_AFTER:
                connection.execute("UPDATE sign_in_attempts SET seen = 1 WHERE account_id = ? AND seen = 0", (int(account_id),))
                connection.commit()

    def touch_account_access(self, account_id: int, *, minimum_interval_seconds: float = 300.0) -> None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT last_accessed_at FROM accounts WHERE id = ? LIMIT 1",
                (int(account_id),),
            ).fetchone()
            if row is None:
                return
            last_accessed_at = row["last_accessed_at"]
            now = time.time()
            if last_accessed_at is None or now - float(last_accessed_at) >= minimum_interval_seconds:
                connection.execute("UPDATE accounts SET last_accessed_at = ? WHERE id = ?", (now, int(account_id)))
                connection.commit()


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


class AuthSessionStore:
    """Who's signed in, by a cookie for each browser. Sessions are kept in the accounts database, so
    restarting NWR Stream Manager signs no one out, but only as a hash of each token: someone who
    could read the database still couldn't use them."""

    def __init__(self, lifetime_seconds: float = AUTH_SESSION_SECONDS, connect: Callable[[], sqlite3.Connection] | None = None) -> None:
        self.lifetime_seconds = float(lifetime_seconds)
        # By each token's hash.
        self.sessions: dict[str, AuthSession] = {}
        self.lock = threading.Lock()
        self.connect = connect
        self._load()

    def create(self, account_id: int = 1) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.lifetime_seconds
        with self.lock:
            self._prune_locked(time.time())
            # Each password sent without a session's cookie starts one, which someone with the
            # password but not the two-step verification code could do without end.
            mine = sorted((session.expires_at, key) for key, session in self.sessions.items() if session.account_id == int(account_id))
            for _expires, key in mine[: max(0, len(mine) - SESSIONS_PER_ACCOUNT + 1)]:
                self._forget_locked(key)  # the longest unused first
            session = self.sessions[_token_hash(token)] = AuthSession(account_id=int(account_id), expires_at=expires_at)
            self._save_locked(_token_hash(token), session)
        return token

    def validate(self, token: str, accounts: AccountStore | None = None) -> AccountRecord | bool | None:
        key = _token_hash(token)
        now = time.time()
        with self.lock:
            session = self.sessions.get(key)
            if session is None:
                return False
            if session.expires_at <= now:
                self._forget_locked(key)
                return False
            session.expires_at = now + self.lifetime_seconds
            if session.expires_at - session.saved_expires_at >= SESSION_SAVE_SECONDS:
                self._save_locked(key, session)
            account_id = session.account_id
        if accounts is None:
            return True
        account = accounts.account_by_id(account_id)
        if account is None:
            self.invalidate_token(token)
            return None
        accounts.touch_account_access(account.id)
        return account

    def two_step_passed(self, token: str) -> bool:
        with self.lock:
            session = self.sessions.get(_token_hash(token))
            return session is not None and session.two_step_passed

    def pass_two_step(self, token: str) -> None:
        with self.lock:
            session = self.sessions.get(_token_hash(token))
            if session is not None:
                session.two_step_passed = True
                session.two_step_setup_only = False
                self._save_locked(_token_hash(token), session)

    def two_step_setup_only(self, token: str) -> bool:
        with self.lock:
            session = self.sessions.get(_token_hash(token))
            return session is not None and session.two_step_setup_only

    def allow_two_step_setup(self, token: str) -> None:
        with self.lock:
            session = self.sessions.get(_token_hash(token))
            if session is not None:
                session.two_step_setup_only = True
                self._save_locked(_token_hash(token), session)

    def invalidate_token(self, token: str) -> None:
        with self.lock:
            self._forget_locked(_token_hash(token))

    def invalidate_account(self, account_id: int, keep: str = "") -> None:
        """Signs the account out everywhere, except in the session keep, if it's given."""
        kept = _token_hash(keep) if keep else ""
        with self.lock:
            for key, session in list(self.sessions.items()):
                if session.account_id == int(account_id) and key != kept:
                    self._forget_locked(key)

    def cookie_header(self, name: str, token: str) -> str:
        return (
            f"{name}={token}; "
            f"Max-Age={int(self.lifetime_seconds)}; Path=/; HttpOnly; SameSite=Lax"
        )

    def _prune_locked(self, now: float) -> None:
        for key, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self._forget_locked(key)

    def _load(self) -> None:
        if self.connect is None:
            return
        try:
            with self.connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS auth_sessions ("
                    "token_hash TEXT PRIMARY KEY, account_id INTEGER NOT NULL, expires_at REAL NOT NULL, "
                    "two_step_passed INTEGER NOT NULL DEFAULT 0, two_step_setup_only INTEGER NOT NULL DEFAULT 0)"
                )
                connection.execute("DELETE FROM auth_sessions WHERE expires_at <= ?", (time.time(),))
                rows = connection.execute("SELECT * FROM auth_sessions").fetchall()
        except sqlite3.Error as exc:
            LOG.warning("couldn't load who was signed in, so everyone has to sign in again: %s", exc)
            return
        for row in rows:
            expires_at = float(row[2])
            self.sessions[str(row[0])] = AuthSession(int(row[1]), expires_at, bool(row[3]), bool(row[4]), expires_at)

    def _save_locked(self, key: str, session: AuthSession) -> None:
        session.saved_expires_at = session.expires_at
        if self.connect is None:
            return
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO auth_sessions (token_hash, account_id, expires_at, two_step_passed, two_step_setup_only) VALUES (?, ?, ?, ?, ?)",
                    (key, session.account_id, session.expires_at, int(session.two_step_passed), int(session.two_step_setup_only)),
                )
        except sqlite3.Error as exc:
            # Still signed in until NWR Stream Manager restarts.
            LOG.warning("couldn't save a sign-in: %s", exc)

    def _forget_locked(self, key: str) -> None:
        if self.sessions.pop(key, None) is None or self.connect is None:
            return
        try:
            with self.connect() as connection:
                connection.execute("DELETE FROM auth_sessions WHERE token_hash = ?", (key,))
        except sqlite3.Error as exc:
            LOG.warning("couldn't remove a sign-in: %s", exc)
