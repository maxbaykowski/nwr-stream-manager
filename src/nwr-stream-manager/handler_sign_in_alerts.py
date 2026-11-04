"""Telling an account about wrong passwords and codes since it last signed in, before the page it asked for."""

from __future__ import annotations

import html
import logging
from http import HTTPStatus
from datetime import datetime, timezone
from typing import Any
from urllib.parse import ParseResult

from . import page_rendering
from .accounts import AccountRecord
from .baseline_pages import page_route, SIGN_IN_ALERT_API_PATH, SIGN_IN_ALERT_HTML, SIGN_IN_ALERT_PATH, TWO_STEP_PATH
from .eas_alerts import format_local_datetime
from .handler_two_step import _safe_next
from .routing import route

LOG = logging.getLogger(__name__)
# The most addresses and devices listed for each account; any before them are counted together.
ADDRESSES_LISTED = 10


def _when(seconds: float) -> str:
    return format_local_datetime(datetime.fromtimestamp(seconds, timezone.utc))


def _counted(rows: list[dict[str, Any]]) -> str:
    """Such as "5 wrong passwords and 2 wrong two-step verification codes"."""
    passwords = sum(1 for row in rows if row["kind"] == "password")
    codes = len(rows) - passwords
    parts = []
    if passwords:
        parts.append(f"{passwords} wrong password{'' if passwords == 1 else 's'}")
    if codes:
        parts.append(f"{codes} wrong two-step verification code{'' if codes == 1 else 's'}")
    return " and ".join(parts)


def attempts_html(rows: list[dict[str, Any]]) -> str:
    """List items, one for each address and device, in the order they first tried. Only the latest
    few are listed, so someone trying from many addresses can't make the page too long to read."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["address"], row["device"]), []).append(row)
    listed = list(groups.items())[-ADDRESSES_LISTED:]
    items = []
    for (address, device), tries in listed:
        place = next((row["location"] for row in reversed(tries) if row["location"]), "")
        place = place or "from an unknown place"
        first, last = _when(tries[0]["at"]), _when(tries[-1]["at"])
        when = first if first == last else f"{first} to {last}"
        items.append(
            f"<li><strong>{html.escape(_counted(tries))}</strong> from {html.escape(address or 'an unknown address')}, {html.escape(place)}. "
            f"Device: {html.escape(device)}. When: {html.escape(when)}.</li>"
        )
    if len(listed) < len(groups):
        unlisted = len(groups) - len(listed)
        earlier = [row for tries in list(groups.values())[:unlisted] for row in tries]
        items.insert(0, f"<li><strong>{html.escape(_counted(earlier))}</strong> from {unlisted} other {'address or device' if unlisted == 1 else 'addresses or devices'}, before these.</li>")
    return "".join(items)


class SignInAlertHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _sign_in_attempt_details(self) -> dict[str, str]:
        """Who's trying to sign in, for telling the account: their own address, even through a tunnel on
        this computer, and whether they're proven to be on the local network or the tailnet."""
        return {"address": self._visitor_host(), "user_agent": self.headers.get("User-Agent", ""), "local": "1" if self._request_proven_local() else ""}

    def _sign_in_alert_or_continue(self, path: str, method: str, account: AccountRecord) -> bool:
        """Shows the alert in place of the main page while there's something to tell; False if it did."""
        if method != "GET" or path != "/":
            return True
        attempts = self.service.accounts.sign_in_attempts_for(account)
        if not attempts["own"] and not attempts["others"]:
            return True
        self._send_page(self._render_sign_in_alert(account, attempts, self.path))
        return False

    def _render_sign_in_alert(self, account: AccountRecord, attempts: dict[str, list[dict[str, Any]]], next_path: str) -> str:
        page = SIGN_IN_ALERT_HTML.replace("__FORM_TOKEN_INPUT__", page_rendering.form_token_input(self._form_token()))
        own, others = attempts["own"], attempts["others"]
        page = page_rendering.set_hidden(page, "sign_in_alert_own", not own)
        if own:
            since = _when(own[0]["at"])
            summary = f"Since {since}, there {'was' if len(own) == 1 else 'were'} {_counted(own)} for your account, {account.username}:"
            page = page_rendering.set_text(page, "sign_in_alert_own_summary", summary)
            page = page_rendering.set_content(page, "sign_in_alert_own_list", attempts_html(own))
            if any(row["kind"] == "code" for row in own):
                page = page_rendering.set_text(
                    page,
                    "sign_in_alert_code_warning",
                    "A wrong two-step verification code means the password was right. If that wasn't you, someone knows your password: change it now.",
                )
                page = page_rendering.set_hidden(page, "sign_in_alert_code_warning", False)
            two_step = self.service.accounts.two_step_enabled(account.id)
            change = "/?view=change_password" if account.is_read_only else page_route("settings", section="account")
            advice = (
                f'If this was you, you can ignore it. If it wasn\'t, someone may be trying to guess your password: <a href="{html.escape(change)}">change it</a> '
                "to a long one you don't use anywhere else"
            )
            advice += "." if two_step else f', and <a href="{TWO_STEP_PATH}">set up two-step verification</a> from the local network.'
            page = page_rendering.set_content(page, "sign_in_alert_advice", advice)
        page = page_rendering.set_hidden(page, "sign_in_alert_others", not others)
        if others:
            names: dict[str, list[dict[str, Any]]] = {}
            unknown: list[dict[str, Any]] = []
            for row in others:
                if row["account_id"] is None:
                    unknown.append(row)
                else:
                    names.setdefault(row["username"], []).append(row)
            items = []
            for name, rows in names.items():
                items.append(f"<li><strong>{html.escape(name)}</strong>: {html.escape(_counted(rows))}<ul>{attempts_html(rows)}</ul></li>")
            if unknown:
                # Someone guessing usernames tries each only once or twice, so they're told about together.
                # Which usernames isn't said: that's no help to anyone reading, and a guesser could learn from it.
                times = "once" if len(unknown) == 1 else f"{len(unknown)} times"
                items.append(
                    f"<li><strong>Usernames that aren't accounts</strong>: someone tried to guess usernames {times}"
                    f"<ul>{attempts_html(unknown)}</ul></li>"
                )
            page = page_rendering.set_content(page, "sign_in_alert_others_list", "".join(items))
        up_to = max(row["id"] for row in own + others)
        page = page_rendering.set_attribute(page, "sign_in_alert_up_to", "value", str(up_to))
        page = page_rendering.set_attribute(page, "sign_in_alert_next", "value", _safe_next(next_path))
        return page

    @route("GET", SIGN_IN_ALERT_API_PATH)
    def _get_sign_in_alert(self, parsed: ParseResult) -> None:
        """What the alert says, for an open page to show over whatever's on it, without loading it again
        and losing anything entered."""
        account = self._requester()
        attempts = self.service.accounts.sign_in_attempts_for(account)
        if not attempts["own"] and not attempts["others"]:
            self._send_json({"waiting": False})
            return
        page = self._render_sign_in_alert(account, attempts, "/")
        # Just its sections, a heading level down, under the dialog's own heading and button.
        start = page.index("</h1>", page.index("<main")) + len("</h1>")
        content = page[start:page.index("<form", start)].replace("<h2", "<h3").replace("</h2>", "</h3>")
        up_to = max(row["id"] for row in attempts["own"] + attempts["others"])
        self._send_json({"waiting": True, "html": content.strip(), "up_to": up_to})

    @route("POST", SIGN_IN_ALERT_API_PATH)
    def _post_sign_in_alert_api(self, parsed: ParseResult) -> None:
        account = self._requester()
        try:
            up_to = int(self._read_json().get("up_to") or 0)
        except (ValueError, TypeError, AttributeError):
            self._send_json({"error": "up_to must be a number"}, status=HTTPStatus.BAD_REQUEST)
            return
        self.service.accounts.sign_in_attempts_seen(account, up_to)
        LOG.info("%s was told about wrong passwords and codes", account.username)
        self._send_json({"ok": True})

    @route("POST", SIGN_IN_ALERT_PATH)
    def _post_sign_in_alert(self, parsed: ParseResult) -> None:
        account = self._requester()
        form = self._read_form()
        if not self._form_token_ok(form):
            # Nothing's marked as told: the alert is shown again, on a page that hasn't expired.
            self._send_redirect(_safe_next(form.get("next", "/")))
            return
        try:
            up_to = int(form.get("up_to") or 0)
        except ValueError:
            up_to = 0
        self.service.accounts.sign_in_attempts_seen(account, up_to)
        LOG.info("%s was told about wrong passwords and codes", account.username)
        self._send_redirect(_safe_next(form.get("next", "/")))
