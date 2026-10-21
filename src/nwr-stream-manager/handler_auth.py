"""Requests' sign-in: sessions, passwords, read-only accounts, and changes to accounts."""

from __future__ import annotations

import base64
import binascii
import logging
from http import HTTPStatus
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlparse

from .accounts import AccountRecord
from .baseline_pages import (
    ACCOUNT_FORM_PATHS,
    CHANGE_PASSWORD_FORM_PATH,
    DASHBOARD_PARTS_PATH,
    EAS_ALERT_EXPORT_PATH,
    HELP_PATH,
    IQ_RECORDING_DOWNLOAD_PATH,
    render_change_password_page,
    render_setup_page,
    SETUP_FORM_PATH,
    SIGN_IN_REQUIRED_HTML,
)
from .constants import AUTH_REALM, AUTH_SESSION_COOKIE_NAME


LOG = logging.getLogger(__name__)


class AuthHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _client_address(self) -> str:
        host = self.client_address[0] if self.client_address else self.address_string()
        port = self.client_address[1] if self.client_address and len(self.client_address) > 1 else ""
        return f"{host}:{port}" if port else host

    def _auth_ok_or_setup_response(self, path: str, method: str) -> bool:
        # One connection can carry several requests.
        self._new_form_token = ""
        allowed = self._check_auth_or_respond(path, method)
        if not allowed and method != "GET":
            # Answered without reading the request body, which would otherwise be
            # taken for the connection's next request.
            self.close_connection = True
        return allowed

    def _check_auth_or_respond(self, path: str, method: str) -> bool:
        self.current_account = None
        if path == HELP_PATH and method == "GET":
            # Help holds nothing private, and people need it before and during setup too.
            self._send_help()
            return False
        has_account = self.service.accounts.has_account()
        if path == "/api/setup-state":
            self._send_json({"setup_required": not has_account})
            return False
        if not has_account:
            if method == "GET":
                step = parse_qs(urlparse(self.path).query).get("step", [""])[0]
                self._send_page(render_setup_page("account" if step == "account" else "welcome", self._form_token()))
                return False
            if method == "POST" and path in {"/api/setup-account", SETUP_FORM_PATH}:
                return True
            self._send_json({"error": "administrator account setup is required"}, status=HTTPStatus.FORBIDDEN)
            return False
        if path == "/api/setup-account":
            self.send_error(HTTPStatus.NOT_FOUND)
            return False
        if path == SETUP_FORM_PATH:
            # Setup is already done, for example if the form was sent twice.
            self._discard_request_body()
            self._send_redirect("/")
            return False
        if path == "/notification":
            return self._notification_auth_ok_or_response(path, method)
        account = self._session_auth_valid()
        if isinstance(account, AccountRecord):
            self.current_account = account
            if not self._account_authorized_or_response(path, method, account):
                return False
            return True
        account = self._basic_auth_valid()
        if isinstance(account, AccountRecord):
            self.current_account = account
            self._pending_auth_session_cookie = self.service.auth_sessions.cookie_header(
                self.service.auth_sessions.create(account.id)
            )
            if not self._account_authorized_or_response(path, method, account):
                return False
            return True
        self._send_auth_required()
        return False

    def _session_auth_valid(self) -> AccountRecord | None:
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        try:
            cookies = SimpleCookie(cookie_header)
        except Exception:
            return None
        morsel = cookies.get(AUTH_SESSION_COOKIE_NAME)
        if morsel is None:
            return None
        account = self.service.auth_sessions.validate(morsel.value, self.service.accounts)
        return account if isinstance(account, AccountRecord) else None

    def _basic_auth_valid(self) -> AccountRecord | None:
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return None
        try:
            decoded = base64.b64decode(header[6:].strip().encode("ascii"), validate=True).decode("utf-8")
        except (UnicodeDecodeError, binascii.Error):
            return None
        username, separator, password = decoded.partition(":")
        if not separator:
            return None
        return self.service.accounts.verify_basic_account(username, password)

    def _account_authorized_or_response(self, path: str, method: str, account: AccountRecord) -> bool:
        if account.must_change_password:
            if method == "GET" and path == "/":
                self._send_page(render_change_password_page(self._form_token()))
                return False
            if path in {"/api/account/me", "/api/account/password", "/api/client-log", CHANGE_PASSWORD_FORM_PATH}:
                return True
            self._send_json({"error": "password change is required"}, status=HTTPStatus.FORBIDDEN)
            return False
        if path.startswith("/api/accounts") or path in {"/api/account-reset-password", "/api/account-read-only"} or path in ACCOUNT_FORM_PATHS:
            if not account.is_owner:
                self._send_json({"error": "owner account is required"}, status=HTTPStatus.FORBIDDEN)
                return False
            return True
        if path in {"/api/account/password", CHANGE_PASSWORD_FORM_PATH}:
            return True
        if account.is_read_only and not self._read_only_request_allowed(path, method):
            self._send_json({"error": "this account is read-only"}, status=HTTPStatus.FORBIDDEN)
            return False
        return True

    def _notification_auth_ok_or_response(self, path: str, method: str) -> bool:
        if method != "GET":
            self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return False
        if not self.headers.get("Authorization", "").startswith("Basic "):
            self._invalidate_request_auth_session_cookie()
            self._send_auth_required()
            return False
        account = self._basic_auth_valid()
        if not isinstance(account, AccountRecord):
            self._invalidate_request_auth_session_cookie()
            self._send_auth_required()
            return False
        self.current_account = account
        self._pending_auth_session_cookie = self.service.auth_sessions.cookie_header(
            self.service.auth_sessions.create(account.id)
        )
        if not self._account_authorized_or_response(path, method, account):
            return False
        return True

    def _read_only_request_allowed(self, path: str, method: str) -> bool:
        if method == "GET":
            return path in {
                "/",
                "/notification",
                "/api/status",
                "/api/stations",
                "/api/streams",
                "/api/eas-alert-streams",
                "/api/eas-alerts",
                "/api/eas-alert-bulk-options",
                "/api/eas-alert-range-count",
                "/api/eas-alert-export",
                "/api/eas-alert",
                "/api/eas-alert-audio",
                EAS_ALERT_EXPORT_PATH,
                DASHBOARD_PARTS_PATH,
                "/api/live-audio",
                "/api/settings-events",
                "/api/monitor/status",
                "/api/receiver/status",
                "/api/iq-recorder/status",
                "/api/iq-recordings",
                "/api/iq-recording-download",
                IQ_RECORDING_DOWNLOAD_PATH,
                "/api/logs",
                "/api/account/me",
            }
        if method == "POST":
            return path in {
                "/api/client-log",
                "/api/monitor/pause",
                "/api/monitor/resume",
                "/api/monitor/stop",
                "/api/receiver/tune",
                "/api/receiver/pause",
                "/api/receiver/resume",
                "/api/receiver/stop",
                "/api/account/password",
            }
        return False

    def _invalidate_request_auth_session_cookie(self) -> None:
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            try:
                cookies = SimpleCookie(cookie_header)
                morsel = cookies.get(AUTH_SESSION_COOKIE_NAME)
                if morsel is not None:
                    self.service.auth_sessions.invalidate_token(morsel.value)
            except Exception:
                pass
        self._pending_auth_session_cookie = (
            f"{AUTH_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        )

    def _send_auth_required(self) -> None:
        payload = b'{"error":"authentication required"}'
        content_type = "application/json"
        if not urlparse(self.path).path.startswith("/api/"):
            # Shown if the sign-in prompt is cancelled.
            payload = SIGN_IN_REQUIRED_HTML.encode("utf-8")
            content_type = "text/html; charset=utf-8"
        # Any request body is left unread, so the connection can't be used again.
        self.close_connection = True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", f'Basic realm="{AUTH_REALM}", charset="UTF-8"')
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", f"{AUTH_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before auth response could be written")

    def _send_notification_forbidden(self) -> None:
        data = (
            "<!doctype html><html><head><meta charset=\"utf-8\">"
            "<title>Access denied - NWR Stream Manager</title></head><body>"
            "<h1>Access denied</h1>"
            "<p>This account is read-only and cannot access output details.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.FORBIDDEN)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self._send_pending_auth_session_cookie()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before notification forbidden response could be written")

    def _send_pending_auth_session_cookie(self) -> None:
        cookie = getattr(self, "_pending_auth_session_cookie", "")
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._pending_auth_session_cookie = ""

    def _change_own_password(self, account: AccountRecord, current: str, new: str, confirm: str) -> dict[str, Any]:
        updated = self.service.accounts.change_password(account.id, current, new, confirm)
        # Signed in everywhere with the old password no longer.
        self.service.auth_sessions.invalidate_account(account.id)
        self.service.revoke_account_long_lived_resources(
            account.id,
            stop_webrtc=True,
            abort_downloads=True,
            stop_iq_recording=True,
        )
        self._pending_auth_session_cookie = f"{AUTH_SESSION_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        LOG.info("changed password for account %s", updated.get("username", ""))
        return updated

    def _create_account(self, username: str, read_only: bool) -> tuple[dict[str, Any], str]:
        account, secret = self.service.accounts.create_account(username, read_only)
        LOG.info("created account %s", account.get("username", ""))
        return account, secret

    def _current_account_id(self) -> int | None:
        account = getattr(self, "current_account", None)
        return account.id if isinstance(account, AccountRecord) else None

    def _current_account_is_read_only(self) -> bool:
        account = getattr(self, "current_account", None)
        return isinstance(account, AccountRecord) and account.is_read_only

    def _requester(self) -> AccountRecord:
        requester = getattr(self, "current_account", None)
        if not isinstance(requester, AccountRecord):
            raise ValueError("authentication is required")
        return requester

    def _sign_out_account(self, account_id: int) -> None:
        self.service.auth_sessions.invalidate_account(account_id)
        self.service.revoke_account_long_lived_resources(
            account_id,
            stop_webrtc=True,
            abort_downloads=True,
            stop_iq_recording=True,
        )

    def _reset_account_password(self, account_id: int) -> tuple[dict[str, Any], str]:
        if self._requester().id == account_id:
            raise ValueError("use change account password for your own account")
        account, secret = self.service.accounts.reset_password(account_id)
        self._sign_out_account(account_id)
        LOG.info("reset password for account %s", account.get("username", ""))
        return account, secret

    def _set_account_read_only(self, account_id: int, read_only: bool) -> dict[str, Any]:
        account = self.service.accounts.set_read_only(account_id, read_only)
        if read_only:
            self.service.revoke_account_long_lived_resources(account_id, stop_iq_recording=True)
        LOG.info("updated account %s role to %s", account.get("username", ""), account.get("role", ""))
        return account

    def _delete_account(self, account_id: int) -> dict[str, Any]:
        account = self.service.accounts.delete_account(account_id, self._requester().id)
        self._sign_out_account(account_id)
        LOG.info("deleted account %s", account.get("username", ""))
        return account
