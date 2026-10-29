"""Two-step verification: asked for whenever NWR Stream Manager is reached from outside its local
network (see local_network), and set up on a page of its own that works in every browser."""

from __future__ import annotations

import base64
import html
import logging
from http import HTTPStatus
from urllib.parse import ParseResult, parse_qs, urlencode

from . import page_rendering
from .accounts import AccountRecord
from .baseline_pages import FORM_EXPIRED_MESSAGE, show_form_error, TWO_STEP_HTML, TWO_STEP_PATH
from .local_network import local_networks
from .routing import route
from .sign_in_limits import SignInWait
from . import two_step

LOG = logging.getLogger(__name__)

DEVICE_COOKIE_NAME = "nwrstmgr_device"
TWO_STEP_REQUIRED_MESSAGE = "You're accessing NWR Stream Manager via the public internet. Please enter your authenticator code to continue."
TWO_STEP_NOT_SET_UP_MESSAGE = (
    "Accessing NWR Stream Manager over the public internet requires two-step verification to be enabled on your account. "
    "To set up two-step verification, please access NWR Stream Manager from a device on your local network."
)
SET_UP_FIRST_MESSAGE = "Set up two-step verification to continue."
LOCAL_ONLY_MESSAGE = "Two-step verification can only be changed from a device on NWR Stream Manager's local network."
# What the page says after each change, by the result in its address.
RESULTS = {
    "disabled": "Two-step verification is off.",
    "forgotten": "Remembered devices were forgotten. They'll need a code the next time they connect from outside the local network.",
    "disabled_other": "Two-step verification was turned off for that account.",
}
SETUP_CODE_HINT = "A setup code works once, within 24 hours of when it was made. If yours has been used or is older, ask the owner for a new one."
SETUP_CODE_FIELD_HINT = "A setup code works once, within 24 hours of when it was made."
# Changes that can only be made from the local network: anyone who learned a password could
# otherwise set up two-step verification for themselves, or turn it off.
LOCAL_ONLY_ACTIONS = {"start", "enable", "recovery", "disable", "disable_other"}


def _safe_next(value: str) -> str:
    """Where to go after the code: only somewhere on this server."""
    value = str(value or "/")
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 32 for c in value):
        return "/"
    return value


class TwoStepHandlerMixin:
    """Mixed into RtlControlHandler."""

    # -- whether it's needed ------------------------------------------------

    def _request_proven_local(self) -> bool:
        address = getattr(self, "client_address", None)
        host, port = (address[0], address[1]) if address and len(address) > 1 else ("", 0)
        return local_networks.is_local(str(host), self.headers, int(port or 0))

    def _device_cookie(self) -> str:
        return self._cookie_value(DEVICE_COOKIE_NAME)

    def _two_step_satisfied(self, account: AccountRecord) -> bool:
        """Whether this request can go ahead as far as two-step verification goes: it's from the local
        network, its session passed it, or it's from a device that asked to be remembered."""
        if self._request_proven_local():
            return True
        token = getattr(self, "_auth_session_token", "")
        if token and self.service.auth_sessions.two_step_passed(token):
            return True
        if self.service.accounts.device_remembered(account.id, self._device_cookie()):
            if token:
                self.service.auth_sessions.pass_two_step(token)
            return True
        return False

    def _two_step_ok_or_response(self, path: str, method: str, account: AccountRecord) -> bool:
        self._two_step_code_pending = False
        self._two_step_setup_only = False
        if self._two_step_satisfied(account):
            return True
        token = getattr(self, "_auth_session_token", "")
        if token and self.service.auth_sessions.two_step_setup_only(token):
            # A setup code was right: until two-step verification is set up, that's all there is.
            if path == TWO_STEP_PATH:
                self._two_step_setup_only = True
                return True
            if path.startswith("/api/"):
                self._send_json({"error": SET_UP_FIRST_MESSAGE, "two_step": "required"}, status=HTTPStatus.FORBIDDEN)
            else:
                self._send_redirect(TWO_STEP_PATH)
            return False
        accounts = self.service.accounts
        enabled = accounts.two_step_enabled(account.id)
        # A setup code from the owner is taken in place of a code from the app.
        can_verify = enabled or accounts.setup_code_waiting(account.id)
        if can_verify and path == TWO_STEP_PATH and method == "POST":
            self._two_step_code_pending = True
            return True  # the code itself; nothing else is taken there until it's right
        if path.startswith("/api/"):
            self._send_json(
                {"error": TWO_STEP_REQUIRED_MESSAGE if can_verify else TWO_STEP_NOT_SET_UP_MESSAGE, "two_step": "required" if can_verify else "not_set_up"},
                status=HTTPStatus.FORBIDDEN,
            )
            return False
        if not can_verify:
            LOG.warning("refused %s from %s: it's from outside the local network, and the account has no two-step verification", account.username, self._client_address())
            self._send_page(self._render_two_step_page(account, "unavailable"), HTTPStatus.FORBIDDEN)
            return False
        next_path = self.path if method == "GET" and path != TWO_STEP_PATH else "/"
        self._send_page(self._render_two_step_page(account, "verify", next_path=next_path))
        return False

    # -- the page -------------------------------------------------------------

    def _render_two_step_page(
        self,
        account: AccountRecord,
        mode: str,
        *,
        message: str = "",
        kind: str = "error",
        next_path: str = "/",
        secret: str = "",
        codes: list[str] | None = None,
        confirm: dict[str, str] | None = None,
        setup_code: tuple[str, str] | None = None,
    ) -> str:
        page = TWO_STEP_HTML.replace("__FORM_TOKEN_INPUT__", page_rendering.form_token_input(self._form_token()))
        local = self._request_proven_local()
        shown = {
            "verify": ["two_step_verify"],
            "unavailable": ["two_step_unavailable"],
            "manage": ["two_step_status_section", "two_step_others"],
            "setup": ["two_step_setup"],
            "codes": ["two_step_codes"],
            "confirm": ["two_step_confirm"],
            "setup_code": ["two_step_setup_code_section"],
        }[mode]
        for section in (
            "two_step_verify", "two_step_unavailable", "two_step_status_section", "two_step_setup", "two_step_codes", "two_step_confirm",
            "two_step_others", "two_step_setup_code_section",
        ):
            page = page_rendering.set_hidden(page, section, section not in shown)
        setup_only = getattr(self, "_two_step_setup_only", False)
        # Until the code's right, or two-step verification is set up with a setup code, there's nothing to go back to.
        page = page_rendering.set_hidden(page, "two_step_back", mode in {"verify", "unavailable"} or setup_only)
        if mode == "verify":
            page = page_rendering.set_attribute(page, "two_step_next", "value", _safe_next(next_path))
            page = page_rendering.set_text(page, "two_step_verify_name", f"NWR Stream Manager, {two_step.authenticator_name(account.username, self.service.instance_name)}")
            if not self.service.accounts.two_step_enabled(account.id):
                # Only a setup code will do.
                page = page_rendering.set_text(page, "two_step_verify_title", "Enter your setup code")
                page = page_rendering.set_text(
                    page, "two_step_verify_intro",
                    "You are accessing NWR Stream Manager via the public internet. Enter your setup code. If you didn't get a setup code, ask the owner for it.",
                )
                page = page_rendering.set_text(page, "two_step_code_label", "Setup code")
                page = page_rendering.set_text(page, "two_step_code_hint", SETUP_CODE_FIELD_HINT)
                page = page_rendering.set_hidden(page, "two_step_remember_row", True)
            elif self.service.accounts.setup_code_waiting(account.id):
                page = page_rendering.set_hidden(page, "two_step_code_setup_hint", False)
        if mode == "unavailable":
            # No one can give the owner a setup code.
            page = page_rendering.set_hidden(page, "two_step_unavailable_setup_code", account.is_owner)
        if mode == "manage":
            page = self._fill_two_step_status(page, account, local)
        if mode == "setup":
            uri = two_step.setup_uri(secret, account.username, self.service.instance_name)
            page = page_rendering.set_attribute(page, "two_step_setup_link", "href", uri)
            page = page_rendering.set_text(page, "two_step_key", two_step.grouped_secret(secret))
            page = page_rendering.set_text(page, "two_step_setup_name", two_step.authenticator_name(account.username, self.service.instance_name))
            page = page_rendering.set_hidden(page, "two_step_setup_cancel", setup_only)
            page = page_rendering.set_hidden(page, "two_step_setup_code_intro", not setup_only)
            png = two_step.qr_code_png(uri)
            if png is not None:
                page = page_rendering.set_attribute(page, "two_step_qr", "src", "data:image/png;base64," + base64.b64encode(png).decode("ascii"))
                page = page_rendering.set_hidden(page, "two_step_qr", False)
                page = page_rendering.set_hidden(page, "two_step_setup_qr_text", False)
        if mode == "codes":
            page = page_rendering.set_content(page, "two_step_code_list", "".join(f"<li>{html.escape(code)}</li>" for code in codes or []))
            # Set up with a setup code: on to what they came for.
            page = page_rendering.set_attribute(page, "two_step_codes_done", "href", "/" if setup_only else TWO_STEP_PATH)
        if mode == "setup_code" and setup_code:
            username, code = setup_code
            page = page_rendering.set_text(page, "two_step_setup_code_title", f"Setup code for {username}")
            page = page_rendering.set_text(page, "two_step_setup_code_for", username)
            page = page_rendering.set_attribute(page, "two_step_setup_code_value", "value", code)
        if mode == "confirm" and confirm:
            page = page_rendering.set_text(page, "two_step_confirm_title", confirm["title"])
            page = page_rendering.set_text(page, "two_step_confirm_text", confirm["text"])
            page = page_rendering.set_text(page, "two_step_confirm_button", confirm["button"])
            page = page_rendering.set_attribute(page, "two_step_confirm_action", "value", confirm["action"])
            page = page_rendering.set_attribute(page, "two_step_confirm_account", "value", confirm.get("account_id", ""))
        if message and kind == "error":
            page = show_form_error(page, "two_step_message", message, "Two-step verification")
        elif message:
            page = page_rendering.set_text(page, "two_step_message", message)
            page = page_rendering.set_attribute(page, "two_step_message", "class", "message success")
        return page

    def _fill_two_step_status(self, page: str, account: AccountRecord, local: bool) -> str:
        status = self.service.accounts.two_step_status(account.id)
        if status["enabled"]:
            left = status["recovery_codes_left"]
            text = f"Two-step verification is on. You have {left} unused recovery code{'' if left == 1 else 's'}."
            if left == 0:
                text += " Get new ones, so you can still sign in from outside the local network if you lose your phone."
        else:
            text = "Two-step verification is off, so this account can only be used from NWR Stream Manager's local network."
        page = page_rendering.set_text(page, "two_step_status", text)
        page = page_rendering.set_hidden(page, "two_step_remote_hint", local)
        page = page_rendering.set_hidden(page, "two_step_off_actions", status["enabled"] or not local)
        page = page_rendering.set_hidden(page, "two_step_on_actions", not status["enabled"])
        page = page_rendering.set_hidden(page, "two_step_forget_hint", not status["enabled"])
        page = page_rendering.set_hidden(page, "two_step_recovery_form", not local)
        page = page_rendering.set_hidden(page, "two_step_disable_form", not local)
        # The owner gives out setup codes, and can turn it off for someone who's lost their phone.
        others = []
        if account.is_owner:
            with_two_step = self.service.accounts.two_step_account_ids()
            for other in self.service.accounts.list_accounts():
                other_id = int(other["id"])
                if other_id == account.id:
                    continue
                name = html.escape(str(other["username"]))
                on = other_id in with_two_step
                buttons = self._other_account_button("setup_code", other_id, f"Create setup code for {name}")
                if on and local:
                    buttons += self._other_account_button("disable_other", other_id, f"Turn off for {name}")
                others.append(f"<li>{name}: {'on' if on else 'off'} {buttons}</li>")
        page = page_rendering.set_content(page, "two_step_other_accounts", "".join(others))
        return page_rendering.set_hidden(page, "two_step_others", not others)

    def _other_account_button(self, action: str, account_id: int, label: str) -> str:
        return (
            f'<form class="inline" method="post" action="{TWO_STEP_PATH}">{page_rendering.form_token_input(self._form_token())}'
            f'<input type="hidden" name="action" value="{action}"><input type="hidden" name="account_id" value="{account_id}">'
            f'<button type="submit">{label}</button></form>'
        )

    # -- requests ----------------------------------------------------------

    @route("GET", TWO_STEP_PATH)
    def _get_two_step(self, parsed: ParseResult) -> None:
        account = self._requester()
        if self._two_step_setup_only:
            self._send_setup_with_setup_code(account)
            return
        result = parse_qs(parsed.query).get("result", [""])[0]
        self._send_page(self._render_two_step_page(account, "manage", message=RESULTS.get(result, ""), kind="success"))

    def _send_setup_with_setup_code(self, account: AccountRecord, message: str = "", status: HTTPStatus = HTTPStatus.OK) -> None:
        """Setting up, after a setup code: the key being set up, or a new one if there's none yet or it took too long."""
        accounts = self.service.accounts
        secret = accounts.two_step_setup_secret(account.id) or accounts.start_two_step_setup(account.id)
        self._send_page(self._render_two_step_page(account, "setup", secret=secret, message=message), status)

    @route("POST", TWO_STEP_PATH)
    def _post_two_step(self, parsed: ParseResult) -> None:
        account = self._requester()
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
        except ValueError as exc:
            mode = "manage" if self._two_step_satisfied(account) else "verify"
            self._send_page(self._render_two_step_page(account, mode, message=str(exc)), HTTPStatus.BAD_REQUEST)
            return
        action = form.get("action", "")
        if self._two_step_setup_only:
            self._enable_with_setup_code(account, action, form)
            return
        if action == "verify":
            self._verify_two_step(account, form)
            return
        # Only the code is taken until it's right.
        if not self._two_step_satisfied(account):
            self._send_page(self._render_two_step_page(account, "verify"), HTTPStatus.FORBIDDEN)
            return
        if action in LOCAL_ONLY_ACTIONS and not self._request_proven_local():
            self._send_page(self._render_two_step_page(account, "manage", message=LOCAL_ONLY_MESSAGE), HTTPStatus.FORBIDDEN)
            return
        try:
            self._two_step_action(account, action, form)
        except ValueError as exc:
            mode = "setup" if action == "enable" and self.service.accounts.two_step_setup_secret(account.id) else "manage"
            secret = self.service.accounts.two_step_setup_secret(account.id) if mode == "setup" else ""
            self._send_page(self._render_two_step_page(account, mode, message=str(exc), secret=secret), HTTPStatus.BAD_REQUEST)

    def _verify_two_step(self, account: AccountRecord, form: dict[str, str]) -> None:
        next_path = _safe_next(form.get("next", "/"))
        if self._two_step_satisfied(account):
            self._send_redirect(next_path)
            return
        try:
            right = self.service.accounts.verify_two_step(account, form.get("code", ""), attempt=self._sign_in_attempt_details())
        except SignInWait as wait:
            self._send_page(self._render_two_step_page(account, "verify", message=str(wait), next_path=next_path), HTTPStatus.TOO_MANY_REQUESTS)
            return
        if not right:
            if self.service.accounts.two_step_enabled(account.id):
                message = "That code isn't right. Enter the code your authenticator app shows now, or one of your recovery codes."
            else:
                message = f"That setup code isn't right. {SETUP_CODE_HINT}"
            self._send_page(self._render_two_step_page(account, "verify", message=message, next_path=next_path), HTTPStatus.BAD_REQUEST)
            return
        token = getattr(self, "_auth_session_token", "")
        if right == "setup":
            # Not signed in yet: first, two-step verification is set up with a new key.
            if token:
                self.service.auth_sessions.allow_two_step_setup(token)
            LOG.info("%s used a setup code from %s, to set up two-step verification", account.username, self._client_address())
            self._send_redirect(TWO_STEP_PATH)
            return
        if token:
            self.service.auth_sessions.pass_two_step(token)
        # Signed in now: a few wrong codes or passwords before it were most likely typing mistakes.
        self.service.accounts.forget_few_sign_in_attempts(account.id)
        if form.get("remember") == "1":
            device = self.service.accounts.remember_device(account.id)
            self._pending_device_cookie = (
                f"{self._cookie_name(DEVICE_COOKIE_NAME)}={device}; Max-Age={two_step.REMEMBER_DEVICE_SECONDS}; Path=/; HttpOnly; SameSite=Lax"
            )
        LOG.info("%s passed two-step verification from %s%s", account.username, self._client_address(), ", and asked to be remembered" if form.get("remember") == "1" else "")
        self._send_redirect(next_path)

    def _enable_with_setup_code(self, account: AccountRecord, action: str, form: dict[str, str]) -> None:
        """After a setup code, the one thing a session can do: turn two-step verification on, with a new
        key in place of any it had, such as on a phone that's been lost."""
        if action != "enable":
            self._send_setup_with_setup_code(account, SET_UP_FIRST_MESSAGE, HTTPStatus.FORBIDDEN)
            return
        accounts = self.service.accounts
        secret = accounts.two_step_setup_secret(account.id)
        if not secret:
            self._send_setup_with_setup_code(account, "Setting up took too long, so there's a new key. Add it to your app again.", HTTPStatus.BAD_REQUEST)
            return
        try:
            codes = accounts.enable_two_step(account.id, secret, form.get("code", ""))
        except ValueError as exc:
            self._send_page(self._render_two_step_page(account, "setup", message=str(exc), secret=secret), HTTPStatus.BAD_REQUEST)
            return
        token = getattr(self, "_auth_session_token", "")
        if token:
            self.service.auth_sessions.pass_two_step(token)
        self._sign_out_other_sessions(account.id)
        accounts.forget_few_sign_in_attempts(account.id)
        LOG.info("%s set up two-step verification from %s with a setup code", account.username, self._client_address())
        self._send_page(self._render_two_step_page(account, "codes", codes=codes))

    def _two_step_action(self, account: AccountRecord, action: str, form: dict[str, str]) -> None:
        accounts = self.service.accounts
        confirmed = form.get("confirmed") == "1"
        if action == "start":
            if accounts.two_step_enabled(account.id):
                raise ValueError("Two-step verification is already on.")
            self._send_page(self._render_two_step_page(account, "setup", secret=accounts.start_two_step_setup(account.id)))
            return
        if action == "enable":
            secret = accounts.two_step_setup_secret(account.id)
            if not secret:
                raise ValueError("Setting up took too long, so it was stopped. Select Set up two-step verification to start again.")
            codes = accounts.enable_two_step(account.id, secret, form.get("code", ""))
            # Signed in elsewhere from outside the local network without it, as was only possible
            # before it was set up: not any more.
            self._sign_out_other_sessions(account.id)
            LOG.info("%s turned on two-step verification", account.username)
            self._send_page(self._render_two_step_page(account, "codes", codes=codes))
            return
        if action == "recovery":
            if not confirmed:
                self._send_page(self._render_two_step_page(account, "confirm", confirm={
                    "title": "Get new recovery codes?", "action": "recovery", "button": "Get new recovery codes",
                    "text": "Your current recovery codes will stop working.",
                }))
                return
            codes = accounts.new_recovery_codes(account.id)
            LOG.info("%s got new two-step verification recovery codes", account.username)
            self._send_page(self._render_two_step_page(account, "codes", codes=codes))
            return
        if action == "disable":
            if not confirmed:
                self._send_page(self._render_two_step_page(account, "confirm", confirm={
                    "title": "Turn off two-step verification?", "action": "disable", "button": "Turn off",
                    "text": "Your account will then only be usable from NWR Stream Manager's local network, and your recovery codes will stop working.",
                }))
                return
            accounts.disable_two_step(account.id)
            self._sign_out_other_sessions(account.id)
            LOG.info("%s turned off two-step verification", account.username)
            self._send_redirect(f"{TWO_STEP_PATH}?{urlencode({'result': 'disabled'})}")
            return
        if action == "disable_other":
            if not account.is_owner:
                raise ValueError("Only the owner can turn off two-step verification for another account.")
            other = accounts.account_by_id(int(form.get("account_id") or 0))
            if other is None or other.id == account.id:
                raise ValueError("account was not found")
            if not confirmed:
                self._send_page(self._render_two_step_page(account, "confirm", confirm={
                    "title": f"Turn off two-step verification for {other.username}?", "action": "disable_other",
                    "account_id": str(other.id), "button": "Turn off",
                    "text": f"{other.username} will then only be able to sign in from NWR Stream Manager's local network, until they set it up again.",
                }))
                return
            accounts.disable_two_step(other.id)
            self.service.auth_sessions.invalidate_account(other.id)
            LOG.info("%s turned off two-step verification for %s", account.username, other.username)
            self._send_redirect(f"{TWO_STEP_PATH}?{urlencode({'result': 'disabled_other'})}")
            return
        if action == "setup_code":
            if not account.is_owner:
                raise ValueError("Only the owner can create setup codes.")
            other = accounts.account_by_id(int(form.get("account_id") or 0))
            if other is None or other.id == account.id:
                raise ValueError("account was not found")
            code = accounts.create_setup_code(other.id)
            LOG.info("%s created a two-step verification setup code for %s", account.username, other.username)
            self._send_page(self._render_two_step_page(account, "setup_code", setup_code=(other.username, code)))
            return
        if action == "forget_devices":
            accounts.forget_devices(account.id)
            self._pending_device_cookie = f"{self._cookie_name(DEVICE_COOKIE_NAME)}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax"
            LOG.info("%s forgot their remembered devices", account.username)
            self._send_redirect(f"{TWO_STEP_PATH}?{urlencode({'result': 'forgotten'})}")
            return
        raise ValueError("Choose what to change.")

    def _sign_out_other_sessions(self, account_id: int) -> None:
        """Every session of account but this one, which goes on."""
        self.service.auth_sessions.invalidate_account(account_id, keep=getattr(self, "_auth_session_token", ""))

    def _send_pending_device_cookie(self) -> None:
        cookie = getattr(self, "_pending_device_cookie", "")
        if cookie:
            self.send_header("Set-Cookie", cookie)
            self._pending_device_cookie = ""


__all__ = ["DEVICE_COOKIE_NAME", "TwoStepHandlerMixin", "TWO_STEP_NOT_SET_UP_MESSAGE", "TWO_STEP_REQUIRED_MESSAGE"]

