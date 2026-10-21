"""Requests about accounts: setting up the first one, signing in, and managing them."""

from __future__ import annotations

import logging
from urllib.parse import ParseResult

from .accounts import AccountRecord
from .baseline_pages import ACCOUNT_FORM_PATHS, CHANGE_PASSWORD_FORM_PATH, SETUP_FORM_PATH
from .routing import route


LOG = logging.getLogger(__name__)


class AccountRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("GET", "/api/account/me")
    def _get_own_account(self, parsed: ParseResult) -> None:
        account = getattr(self, "current_account", None)
        self._send_json({"account": account.to_public_dict() if isinstance(account, AccountRecord) else None})

    @route("GET", "/api/accounts")
    def _get_accounts(self, parsed: ParseResult) -> None:
        self._send_json({"accounts": self.service.accounts.list_accounts()})

    @route("POST", SETUP_FORM_PATH)
    def _post_setup_form(self, parsed: ParseResult) -> None:
        self._handle_setup_form()

    @route("POST", CHANGE_PASSWORD_FORM_PATH)
    def _post_change_password_form(self, parsed: ParseResult) -> None:
        self._handle_change_password_form()

    @route("POST", *ACCOUNT_FORM_PATHS)
    def _post_account_form(self, parsed: ParseResult) -> None:
        self._handle_account_form(parsed.path)

    @route("POST", "/api/setup-account")
    def _post_setup_account(self, parsed: ParseResult) -> None:
        def create_administrator() -> dict:
            payload = self._read_json()
            self.service.accounts.create_admin(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
                str(payload.get("confirm_password", "")),
            )
            LOG.info("created NWR Stream Manager administrator account")
            return {"success": True}

        self._send_json_result(create_administrator)

    @route("POST", "/api/account/password")
    def _post_own_password(self, parsed: ParseResult) -> None:
        def change_password() -> dict:
            account = self._requester()
            payload = self._read_json()
            updated = self._change_own_password(
                account,
                str(payload.get("current_password", "")),
                str(payload.get("new_password", "")),
                str(payload.get("confirm_password", "")),
            )
            return {"success": True, "account": updated}

        self._send_json_result(change_password)

    @route("POST", "/api/accounts")
    def _post_account(self, parsed: ParseResult) -> None:
        def create() -> dict:
            payload = self._read_json()
            account, secret = self._create_account(str(payload.get("username", "")), bool(payload.get("read_only", False)))
            return {"success": True, "account": account, "secret": secret}

        self._send_json_result(create)

    @route("POST", "/api/account-reset-password")
    def _post_account_password_reset(self, parsed: ParseResult) -> None:
        def reset() -> dict:
            account, secret = self._reset_account_password(int(self._read_json().get("account_id", 0)))
            return {"success": True, "account": account, "secret": secret}

        self._send_json_result(reset)

    @route("PATCH", "/api/account-read-only")
    def _patch_account_read_only(self, parsed: ParseResult) -> None:
        def change() -> dict:
            payload = self._read_json()
            account = self._set_account_read_only(int(payload.get("account_id", 0)), bool(payload.get("read_only", False)))
            return {"success": True, "account": account}

        self._send_json_result(change)

    @route("DELETE", "/api/accounts")
    def _delete_account_request(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: {"success": True, "account": self._delete_account(int(self._query_value(parsed, "account_id", "0")))}
        )
