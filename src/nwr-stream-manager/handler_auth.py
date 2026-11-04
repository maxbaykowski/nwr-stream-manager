"""Requests' sign-in: sessions, passwords, read-only accounts, and changes to accounts."""

from __future__ import annotations

import json
import logging
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from . import page_rendering
from .accounts import AccountRecord
from .baseline_pages import (
    ACCOUNT_FORM_PATHS,
    ADVANCED_SETTINGS_PATH,
    CHANGE_PASSWORD_FORM_PATH,
    DASHBOARD_PARTS_PATH,
    EAS_ALERT_EXPORT_PATH,
    HELP_AUDIO_FILES,
    HELP_PATH,
    IQ_RECORDING_DOWNLOAD_PATH,
    render_change_password_page,
    render_notice_page,
    render_setup_page,
    render_sign_in_page,
    SETUP_FORM_PATH,
    STREAM_ARCHIVE_DOWNLOAD_PATH,
    STREAM_ARCHIVE_PLAY_PATH,
    SIGN_IN_ALERT_API_PATH,
    SIGN_IN_ALERT_PATH,
    SIGN_IN_PATH,
    SIGN_OUT_PATH,
    TAILSCALE_SETTINGS_PATH,
    TWO_STEP_PATH,
)
from .constants import AUTH_SESSION_COOKIE_NAME
from .handler_two_step import _safe_next
from .request_origin import (
    ANOTHER_SITE_MESSAGE,
    forwarded_client_address,
    request_from_another_site,
    SETUP_THROUGH_PROXY_MESSAGE,
)
from .sign_in_limits import SignInWait


LOG = logging.getLogger(__name__)
# Said the same whichever was wrong, so it doesn't tell anyone which usernames are accounts.
WRONG_PASSWORD_MESSAGE = "The username or password is incorrect."
SIGN_IN_EXPIRED_MESSAGE = "This page had expired. Please sign in again."
SIGN_IN_COOKIES_MESSAGE = "To sign in, your browser has to accept cookies from NWR Stream Manager. Turn cookies on for this site, then sign in again."
SIGNED_OUT_MESSAGE = "You've signed out."


def _sign_in_next(value: str) -> str:
    """Where to go once signed in: only somewhere on this server, and not back to signing in or out."""
    target = _safe_next(value)
    if urlparse(target).path in {SIGN_IN_PATH, SIGN_OUT_PATH}:
        return "/"
    return target


class AuthHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _visitor_host(self) -> str:
        """The visitor's own address, without its port: see _client_address."""
        host = self.client_address[0] if getattr(self, "client_address", None) else ""
        headers = getattr(self, "headers", None)
        return (forwarded_client_address(str(host), headers) if headers is not None else "") or str(host)

    def _client_address(self) -> str:
        """Who sent the request, for the log: the visitor's own address when a tunnel or proxy
        on this computer passes it on, rather than the proxy's."""
        host = self.client_address[0] if self.client_address else self.address_string()
        port = self.client_address[1] if self.client_address and len(self.client_address) > 1 else ""
        headers = getattr(self, "headers", None)
        visitor = forwarded_client_address(str(host), headers) if headers is not None else ""
        if visitor:
            return visitor
        return f"{host}:{port}" if port else host

    def _auth_ok_or_setup_response(self, path: str, method: str) -> bool:
        # One connection can carry several requests.
        self._new_form_token = ""
        self._auth_session_token = ""
        self._pending_auth_session_cookie = ""
        if request_from_another_site(method, path, self.headers):
            self._send_another_site_refused(path)
            allowed = False
        else:
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
        if path in HELP_AUDIO_FILES and method == "GET":
            self._send_help_audio(path)
            return False
        has_account = self.service.accounts.has_account()
        if path == "/api/setup-state":
            # Which run this is lets a page waiting out a restart tell the new run from the old.
            self._send_json({"setup_required": not has_account, "instance": self.service.instance_id})
            return False
        if not has_account:
            if not self._request_proven_local():
                # Anyone who found a tunnel's address could create the owner account; a tunnel on this
                # computer, such as ssh -R, connects from it, so not even that is taken.
                if method != "GET":
                    LOG.warning("refused first-time setup from %s: it came through a tunnel or proxy", self._client_address())
                if path.startswith("/api/"):
                    self._send_json({"error": SETUP_THROUGH_PROXY_MESSAGE}, status=HTTPStatus.FORBIDDEN)
                else:
                    self._send_page(render_setup_page("blocked", ""), HTTPStatus.FORBIDDEN)
                return False
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
        if path == SIGN_IN_PATH:
            if method == "POST":
                self._post_sign_in()
            elif method == "GET":
                self._get_sign_in()
            else:
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return False
        if path == SIGN_OUT_PATH:
            if method == "POST":
                self._post_sign_out()
            else:
                self.send_error(HTTPStatus.METHOD_NOT_ALLOWED)
            return False
        account = self._session_auth_valid()
        if isinstance(account, AccountRecord):
            self.current_account = account
            if not self._account_authorized_or_response(path, method, account):
                return False
            return True
        self._send_auth_required()
        return False

    # -- signing in and out -------------------------------------------------

    def _get_sign_in(self) -> None:
        query = parse_qs(urlparse(self.path).query)
        next_path = _sign_in_next(query.get("next", ["/"])[0])
        if isinstance(self._session_auth_valid(), AccountRecord):
            # Already signed in, such as in another tab.
            self._send_redirect(next_path)
            return
        message = SIGNED_OUT_MESSAGE if query.get("signed_out", [""])[0] == "1" else ""
        self._send_page(render_sign_in_page(self._form_token(), next_path, message, kind="success"))

    def _post_sign_in(self) -> None:
        form = self._read_form()
        next_path = _sign_in_next(form.get("next", "/"))
        username = form.get("username", "").strip()[:64]
        remember = form.get("remember") == "1"
        if not self._form_token_ok(form):
            # A form from another site, or one left open from before this browser's form token, or
            # a browser that doesn't keep cookies at all, which can't stay signed in either.
            no_cookie = not self._cookie_value(page_rendering.FORM_TOKEN_COOKIE_NAME)
            message = SIGN_IN_COOKIES_MESSAGE if no_cookie else SIGN_IN_EXPIRED_MESSAGE
            self._send_sign_in_refused(message, next_path, username, remember, HTTPStatus.FORBIDDEN)
            return
        try:
            account = self.service.accounts.verify_password(username, form.get("password", ""), attempt=self._sign_in_attempt_details())
        except SignInWait as wait:
            self._send_sign_in_refused(str(wait), next_path, username, remember, HTTPStatus.TOO_MANY_REQUESTS, {"Retry-After": str(wait.seconds)})
            return
        if not isinstance(account, AccountRecord):
            self._send_sign_in_refused(WRONG_PASSWORD_MESSAGE, next_path, username, remember)
            return
        # A new session every time, never one the browser already had, which could have been
        # given to it by someone else, or be another account's.
        self._invalidate_request_auth_session_cookie()
        self.current_account = account
        # Not with a temporary password, which has to be changed first, signing it out.
        remember = remember and not account.must_change_password
        self._start_auth_session(account, remember=remember)
        if self._two_step_satisfied(account):
            if remember:
                self._remember_session(self._auth_session_token)
            # Signed in, two-step verification included: a few wrong passwords or codes before it,
            # from the same address, were most likely typing mistakes. Not before then, or someone with the password could
            # start session after session, and their wrong codes would never add up.
            self.service.accounts.forget_few_sign_in_attempts(account.id, self._visitor_host())
            LOG.info("%s signed in from %s%s", account.username, self._client_address(), ", and is remembered for 30 days" if remember else "")
        else:
            LOG.info("%s entered the right password from %s; two-step verification is next", account.username, self._client_address())
        # Two-step verification, a password change or a sign-in alert come next, where it's going.
        if self._sign_in_from_page():
            self._send_json({"next": next_path})
        else:
            self._send_redirect(next_path)

    def _sign_in_from_page(self) -> bool:
        # The sign-in page's script, which stays on the page when a sign-in doesn't work, so browsers
        # don't take it for one that did and offer to save a wrong password.
        return "application/json" in self.headers.get("Accept", "")

    def _send_sign_in_refused(
        self,
        message: str,
        next_path: str,
        username: str,
        remember: bool,
        status: HTTPStatus = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self._sign_in_from_page():
            # With the form token, which the page may not have had yet.
            self._send_json({"error": message, "form_token": self._form_token()}, status, headers)
        else:
            self._send_page(render_sign_in_page(self._form_token(), next_path, message, username=username, remember=remember), status, headers)

    def _post_sign_out(self) -> None:
        form = self._read_form()
        account = self._session_auth_valid()
        if not self._form_token_ok(form):
            self._send_redirect("/")
            return
        self._invalidate_request_auth_session_cookie()
        if isinstance(account, AccountRecord):
            LOG.info("%s signed out from %s", account.username, self._client_address())
        self._send_redirect(f"{SIGN_IN_PATH}?signed_out=1")

    def _send_another_site_refused(self, path: str) -> None:
        self.current_account = None
        LOG.warning(
            "refused %s %s from %s: it came from another website (%s)",
            self.command,
            path,
            self._client_address(),
            self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") or "unknown",
        )
        if path.startswith("/api/"):
            self._send_json({"error": ANOTHER_SITE_MESSAGE}, status=HTTPStatus.FORBIDDEN)
        else:
            self._send_page(render_notice_page("Nothing was changed", f"<p>{ANOTHER_SITE_MESSAGE}</p>", {}), HTTPStatus.FORBIDDEN)

    def _session_auth_valid(self) -> AccountRecord | None:
        token = self._cookie_value(AUTH_SESSION_COOKIE_NAME)
        if not token:
            return None
        account = self.service.auth_sessions.validate(token, self.service.accounts)
        if not isinstance(account, AccountRecord):
            return None
        self._auth_session_token = token
        return account

    def _start_auth_session(self, account: AccountRecord, remember: bool = False) -> None:
        self._auth_session_token = self.service.auth_sessions.create(account.id, remember=remember)
        self._pending_auth_session_cookie = self.service.auth_sessions.cookie_header(self._cookie_name(AUTH_SESSION_COOKIE_NAME), self._auth_session_token)

    def _account_authorized_or_response(self, path: str, method: str, account: AccountRecord) -> bool:
        # From outside the local network, a password isn't enough.
        if not self._two_step_ok_or_response(path, method, account):
            return False
        # Told about wrong passwords and codes since it last signed in, before anything else.
        if not self._sign_in_alert_or_continue(path, method, account):
            return False
        if account.must_change_password:
            if method == "GET" and path == "/":
                self._send_page(render_change_password_page(self._form_token()))
                return False
            # Two-step verification comes first from outside the local network, as with a temporary password
            # after a password reset, or for a new account set up with a setup code.
            if path in {"/api/account/me", "/api/account/password", "/api/client-log", CHANGE_PASSWORD_FORM_PATH, SIGN_IN_ALERT_PATH, TWO_STEP_PATH}:
                return True
            self._send_json({"error": "password change is required"}, status=HTTPStatus.FORBIDDEN)
            return False
        owner_paths = {
            "/api/account-reset-password", "/api/account-read-only", "/api/advanced-settings", ADVANCED_SETTINGS_PATH,
            "/api/tailscale", TAILSCALE_SETTINGS_PATH,
        }
        if path.startswith("/api/accounts") or path in owner_paths or path in ACCOUNT_FORM_PATHS:
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
                "/api/stream-archives",
                "/api/stream-archive-player",
                "/api/stream-archive-player-time",
                STREAM_ARCHIVE_DOWNLOAD_PATH,
                STREAM_ARCHIVE_PLAY_PATH,
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
                TWO_STEP_PATH,
                SIGN_IN_ALERT_API_PATH,
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
                TWO_STEP_PATH,
                SIGN_IN_ALERT_PATH,
                SIGN_IN_ALERT_API_PATH,
            }
        return False

    def _invalidate_request_auth_session_cookie(self) -> None:
        token = self._cookie_value(AUTH_SESSION_COOKIE_NAME)
        if token:
            self.service.auth_sessions.invalidate_token(token)
        self._pending_auth_session_cookie = f"{self._cookie_name(AUTH_SESSION_COOKIE_NAME)}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"

    def _send_auth_required(self) -> None:
        """Not signed in: the sign-in page, coming back here after, or for the page script, a
        401 that says so, which it answers by loading the page again."""
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self._send_json({"error": "Sign-in is required.", "sign_in": SIGN_IN_PATH}, status=HTTPStatus.UNAUTHORIZED)
            return
        next_path = self.path if self.command == "GET" else "/"
        self._send_redirect(f"{SIGN_IN_PATH}?next={quote(_sign_in_next(next_path), safe='')}")

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
        self._pending_auth_session_cookie = f"{self._cookie_name(AUTH_SESSION_COOKIE_NAME)}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
        LOG.info("changed password for account %s", updated.get("username", ""))
        return updated

    def _create_account(self, username: str, read_only: bool) -> tuple[dict[str, Any], str, str]:
        """The new account, its temporary password, and a setup code, for setting up two-step
        verification if it's first used from outside the local network."""
        account, secret = self.service.accounts.create_account(username, read_only)
        setup_code = self.service.accounts.create_setup_code(int(account["id"]))
        LOG.info("created account %s", account.get("username", ""))
        return account, secret, setup_code

    def _current_account_id(self) -> int | None:
        account = getattr(self, "current_account", None)
        return account.id if isinstance(account, AccountRecord) else None

    def _current_account_name(self) -> str:
        account = getattr(self, "current_account", None)
        return account.username if isinstance(account, AccountRecord) else ""

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
