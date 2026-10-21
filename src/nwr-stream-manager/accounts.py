"""Accounts, passwords and sign-in sessions."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import (
    ACCOUNT_ROLE_ADMIN,
    ACCOUNT_ROLE_OWNER,
    ACCOUNT_ROLE_READ_ONLY,
    ACCOUNT_ROLES,
    AUTH_SESSION_COOKIE_NAME,
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


class AccountStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self._ensure_schema()

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
            if not verify_account_password(str(current_password), str(row["password"])):
                raise ValueError("current password is incorrect")
            connection.execute(
                "UPDATE accounts SET password = ?, must_change_password = 0 WHERE id = ?",
                (hash_account_password(new_password), record.id),
            )
            row = connection.execute("SELECT * FROM accounts WHERE id = ? LIMIT 1", (record.id,)).fetchone()
            connection.commit()
            updated = self._row_to_record(row)
            if updated is None:
                raise ValueError("account was not found")
            return updated.to_public_dict()

    def verify_basic_account(self, username: str, password: str) -> AccountRecord | None:
        with self.lock, self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE username = ? LIMIT 1",
                (str(username),),
            ).fetchone()
            if row is None:
                return None
            password_hash = str(row["password"])
            if not verify_account_password(password, password_hash):
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


class AuthSessionStore:
    def __init__(self, lifetime_seconds: float = AUTH_SESSION_SECONDS) -> None:
        self.lifetime_seconds = float(lifetime_seconds)
        self.sessions: dict[str, AuthSession] = {}
        self.lock = threading.Lock()

    def create(self, account_id: int = 1) -> str:
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + self.lifetime_seconds
        with self.lock:
            self._prune_locked(time.time())
            self.sessions[token] = AuthSession(account_id=int(account_id), expires_at=expires_at)
        return token

    def validate(self, token: str, accounts: AccountStore | None = None) -> AccountRecord | bool | None:
        token = str(token)
        now = time.time()
        with self.lock:
            session = self.sessions.get(token)
            if session is None:
                return False
            if session.expires_at <= now:
                self.sessions.pop(token, None)
                return False
            session.expires_at = now + self.lifetime_seconds
            account_id = session.account_id
        if accounts is None:
            return True
        account = accounts.account_by_id(account_id)
        if account is None:
            self.invalidate_token(token)
            return None
        accounts.touch_account_access(account.id)
        return account

    def invalidate_token(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(str(token), None)

    def invalidate_account(self, account_id: int) -> None:
        with self.lock:
            for token, session in list(self.sessions.items()):
                if session.account_id == int(account_id):
                    self.sessions.pop(token, None)

    def cookie_header(self, token: str) -> str:
        return (
            f"{AUTH_SESSION_COOKIE_NAME}={token}; "
            f"Max-Age={int(self.lifetime_seconds)}; Path=/; HttpOnly; SameSite=Lax"
        )

    def _prune_locked(self, now: float) -> None:
        for token, session in list(self.sessions.items()):
            if session.expires_at <= now:
                self.sessions.pop(token, None)
