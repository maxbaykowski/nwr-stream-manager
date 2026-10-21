"""Pages for browsers without the page script: the page itself, settings, accounts, setup, logs and the dashboard."""

from __future__ import annotations

import html
import logging
import time
from http import HTTPStatus
from typing import Any

from . import page_rendering
from .accounts import AccountRecord
from .baseline_pages import (
    account_confirmation_html,
    account_row_html,
    BASELINE_SETTINGS_SECTIONS,
    EAS_ALERT_VIEWS,
    EMPTY_ACCOUNTS_ROW,
    FALLBACK_RESULTS,
    FORM_EXPIRED_MESSAGE,
    form_message,
    INDEX_HTML,
    INDEX_VIEWS,
    IQ_RECORDER_VIEWS,
    NOTIFICATION_RESULTS,
    OUTPUT_RESULTS,
    READ_ONLY_RESTRICTED_VIEWS,
    render_account_secret_page,
    render_change_password_page,
    render_setup_page,
    SETTINGS_BACK_NAMES,
    SETTINGS_SECTION_TITLES,
    settings_sections_for,
    settings_tab_section,
    SETTINGS_TABS,
    STREAM_RESULTS,
    VIEW_NAV_ITEMS,
    VIEW_TITLES,
)
from .page_sections import dashboard_parts, iq_recorder_result


LOG = logging.getLogger(__name__)


class PageHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _handle_account_form(self, path: str) -> None:
        """The owner's account forms, for browsers without the page script."""
        accounts_page = {"view": "settings", "section": "accounts"}
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            if path == "/accounts/create":
                try:
                    account, secret = self._create_account(form.get("username", ""), form.get("read_only") == "1")
                except ValueError as exc:
                    # Shown on the form, which keeps what was entered.
                    query = {"view": "settings", "section": "create_account"}
                    result = ("create-account-result", form_message(str(exc)), "error")
                    self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
                    return
                summary = f"Account {account.get('username', '')} created."
                self._send_page(render_account_secret_page(summary, secret))
                return
            account_id = int(form.get("account_id", "0") or 0)
            if path == "/accounts/reset-password":
                account, secret = self._reset_account_password(account_id)
                self._send_page(render_account_secret_page(f"Password reset for {account.get('username', '')}.", secret))
                return
            if path == "/accounts/read-only":
                self._set_account_read_only(account_id, form.get("read_only") == "1")
                message = "Account updated."
            else:
                self._delete_account(account_id)
                message = "Account deleted."
        except Exception as exc:
            result = ("account-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(accounts_page, result=result), HTTPStatus.BAD_REQUEST)
            return
        self._send_page(self._render_index_page(accounts_page, result=("account-result", message, "success")))

    def _handle_setup_form(self) -> None:
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            self.service.accounts.create_admin(
                form.get("username", ""),
                form.get("password", ""),
                form.get("confirm_password", ""),
            )
        except Exception as exc:
            page = render_setup_page("account", self._form_token(), form_message(str(exc)), form.get("username", ""))
            self._send_page(page, HTTPStatus.BAD_REQUEST)
            return
        LOG.info("created NWR Stream Manager administrator account")
        self._send_page(render_setup_page("done", self._form_token()))

    def _handle_change_password_form(self) -> None:
        account = getattr(self, "current_account", None)
        if not isinstance(account, AccountRecord):
            self._send_auth_required()
            return
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            self._change_own_password(
                account,
                form.get("current_password", ""),
                form.get("new_password", ""),
                form.get("confirm_password", ""),
            )
        except Exception as exc:
            message = form_message(str(exc))
            if account.must_change_password:
                self._send_page(render_change_password_page(self._form_token(), message), HTTPStatus.BAD_REQUEST)
            else:
                query = {"view": "change_password"} if account.is_read_only else {"view": "settings", "section": "account"}
                result = ("change-password-result", message, "error")
                self._send_page(self._render_index_page(query, result=result), HTTPStatus.BAD_REQUEST)
            return
        # The browser is asked to sign in again, with the new password.
        self._send_redirect("/")

    def _render_index_page(
        self,
        query: dict[str, str],
        *,
        result: tuple[str, str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> str:
        """The main page, showing the view asked for, filled in for browsers without the page script.

        result is a message to show: the element for it, its text, and "success" or "error".
        form is what was entered in a form being shown again.
        """
        account = getattr(self, "current_account", None)
        read_only = isinstance(account, AccountRecord) and account.is_read_only
        view = query.get("view", "") or "dashboard"
        if view not in INDEX_VIEWS or (read_only and view in READ_ONLY_RESTRICTED_VIEWS):
            view = "dashboard"
        if view in {"stream_output", "add_stream"} and query.get("stream") and not any(
            stream.get("id") == query.get("stream") for stream in self.service.streams
        ):
            view, query = "streams", {"view": "streams"}
            result = result or ("streams-result", "Stream was not found.", "error")
        if view == "stream_output" and not self._stream_output(query.get("stream", ""), query.get("output", "")):
            # As the page script does, an output that's gone goes back to its stream.
            view, query = "stream_settings", {"view": "stream_settings", "stream": query.get("stream", "")}
            result = result or ("output-list-result", "Stream output was not found.", "error")
        if view == "stream_settings" and not any(stream.get("id") == query.get("stream") for stream in self.service.streams):
            # As the page script does, a stream that's gone goes back to the list.
            view, query = "streams", {"view": "streams"}
            result = result or ("streams-result", "Stream was not found.", "error")
        page = page_rendering.show_view(INDEX_HTML, view)
        page = page.replace("__FORM_TOKEN_INPUT__", page_rendering.form_token_input(self._form_token()))
        title = VIEW_TITLES.get(view, "Dashboard")
        # A setting that needs the page script gets the same message as a page that does.
        section = query.get("section", "")
        unsupported_setting = view == "settings" and section in SETTINGS_SECTION_TITLES and section not in BASELINE_SETTINGS_SECTIONS
        unsupported = unsupported_setting or page_rendering.view_needs_app(page, view)
        page = page_rendering.set_hidden(page, "baseline_unavailable", not unsupported)
        if unsupported_setting:
            page = page_rendering.set_hidden(page, "view_settings", True)
            title = f"{SETTINGS_SECTION_TITLES[section]} - Settings"
        if unsupported:
            # Said first, in the title, where a browser without the page script reads it.
            title = f"Unsupported by your browser - {title}"
        page = page_rendering.set_hidden(page, "nav_rtl", read_only)
        page = page_rendering.set_hidden(page, "nav_settings", read_only)
        page = page_rendering.set_hidden(page, "nav_change_password", not read_only)
        current = VIEW_NAV_ITEMS.get(view, view)
        for item in ("dashboard", "rtl", "streams", "eas_alerts", "receiver", "iq_recorder", "logs", "settings", "change_password"):
            page = page_rendering.set_attribute(page, f"nav_{item}", "aria-current", "page" if item == current else None)
        if view == "logs":
            page = self._fill_logs_view(page, read_only)
        if view == "change_password" and isinstance(account, AccountRecord):
            page = page_rendering.set_attribute(page, "change_password_cancel_link", "href", "/")
        if view == "settings" and isinstance(account, AccountRecord) and not unsupported_setting:
            page, title = self._fill_settings_view(page, account, query, form or {})
            if query.get("section") == "notifications" and not result and query.get("result") in NOTIFICATION_RESULTS:
                result = ("notification-result", NOTIFICATION_RESULTS[query["result"]], "success")
            if query.get("section") == "sdr_sharing" and not result:
                name = query.get("name", "")
                result = {
                    "shared": ("remote-sharing-result", "This RTL-SDR is now shared with paired devices.", "success"),
                    "unshared": ("remote-sharing-result", "Sharing is turned off.", "success"),
                    "stopped": ("remote-sharing-result", "Pairing stopped.", "success"),
                    "paired": ("remote-pair-outcome", f"Paired with {name}. Either device can now use the other's shared RTL-SDR.", "success"),
                    "unpaired": ("paired-devices-result", f"Unpaired {name}.", "success"),
                }.get(query.get("result", ""))
            if query.get("section") == "fallback_audio" and not result and query.get("result") in FALLBACK_RESULTS:
                element_id, message = FALLBACK_RESULTS[query["result"]]
                result = (element_id, message, "success")
        eas_streams = self.service.eas_alert_streams()["streams"]
        page = page_rendering.set_hidden(page, "nav_eas_alerts", not eas_streams)
        if view in EAS_ALERT_VIEWS:
            page, title = self._fill_eas_alert_view(page, view, query, form or {}, eas_streams, read_only, title)
        if view == "dashboard":
            page = self._fill_dashboard_view(page, read_only)
        if view == "streams":
            page, title = self._fill_streams_view(page, query, read_only, title)
            if not result and query.get("result") in STREAM_RESULTS:
                result = ("streams-result", STREAM_RESULTS[query["result"]], "success")
        if view == "stream_settings":
            page, title = self._fill_stream_settings_view(page, query, form or {}, title)
            if not result and query.get("result") == "saved":
                result = ("stream-settings-save-result", "Changes saved.", "success")
            if not result and query.get("result") in OUTPUT_RESULTS:
                result = ("output-list-result", OUTPUT_RESULTS[query["result"]], "success")
        if view == "stream_output":
            page, title = self._fill_stream_output_view(page, query, form or {})
        if view == "add_stream" and not read_only:
            page, title = self._fill_wizard_view(page, query, form or {})
        if view == "rtl":
            page, title, rtl_result = self._fill_rtl_view(page, query, form or {}, title)
            result = result or rtl_result
        recorder = self.service.redacted_iq_recorder_status() if read_only else self.service.iq_recorder_status()
        page = self._fill_iq_recording_banner(page, recorder)
        if view in IQ_RECORDER_VIEWS:
            page, title = self._fill_iq_recorder_view(page, view, query, form or {}, recorder, read_only, title)
            if view == "iq_recorder" and not result:
                result = iq_recorder_result(query, recorder)
        title = f"{title} - NWR Stream Manager"
        if result:
            element_id, message, kind = result
            page = page_rendering.set_text(page, element_id, message)
            page = page_rendering.set_attribute(page, element_id, "class", f"message {kind}")
            page = page_rendering.set_attribute(page, element_id, "role", "alert" if kind == "error" else "status")
            # In the title too, so it's the first thing read when the page loads.
            title = f"Error: {message} - {title}" if kind == "error" else f"{message} - {title}"
        return page_rendering.set_title(page, title)

    def _fill_settings_view(self, page: str, account: AccountRecord, query: dict[str, str], form: dict[str, str]) -> tuple[str, str]:
        owner = account.is_owner
        section = query.get("section", "")
        if not owner and section in {"accounts", "create_account"}:
            section = "account"
        if section not in settings_sections_for(owner):
            section = ""
        tab = settings_tab_section(section, owner)
        parent = tab if tab != section else ""
        works = section in BASELINE_SETTINGS_SECTIONS
        page = page_rendering.set_hidden(page, "settings_tab_accounts", not owner)
        page = page_rendering.set_hidden(page, "settings_tab_account", owner)
        for name in SETTINGS_TABS:
            page = page_rendering.set_attribute(page, f"settings_tab_{name}", "aria-current", "page" if name == tab else None)
            page = page_rendering.set_hidden(page, f"settings_panel_{name}", not (works and name == tab))
        page = page_rendering.set_hidden(page, "settings_accounts_page", section != "accounts")
        page = page_rendering.set_hidden(page, "settings_create_account_page", section != "create_account")
        page = page_rendering.set_hidden(page, "settings_owner_password_page", not (section == "account" and owner))
        layout = ["settings-layout"] + (["settings-detail-active"] if section else []) + (["settings-subpage-active"] if parent else [])
        page = page_rendering.set_attribute(page, "settings_layout", "class", " ".join(layout))
        page = page_rendering.set_text(page, "settings_back", f"Back to {SETTINGS_BACK_NAMES[parent]}" if parent else "Back to settings")
        page = page_rendering.set_attribute(page, "settings_back_section", "value", parent)
        page = page_rendering.set_hidden(page, "settings_choose", bool(section))
        title = f"{SETTINGS_SECTION_TITLES[section]} - Settings" if section else "Settings"
        if section == "account":
            page = self._place_password_form(page, "settings_owner_password_page" if owner else "settings_panel_account")
            page = page_rendering.set_attribute(
                page, "change_password_cancel_link", "href", "/?view=settings&section=accounts" if owner else "/?view=settings"
            )
        if section == "create_account":
            if form.get("username"):
                page = page_rendering.set_attribute(page, "new_account_username", "value", form["username"])
            if form.get("read_only") == "1":
                page = page_rendering.set_attribute(page, "new_account_read_only", "checked", "checked")
        if section == "accounts":
            page, confirm_title = self._fill_accounts_page(page, account, query)
            if confirm_title:
                title = f"{confirm_title} - {title}"
        if section == "fallback_audio":
            page = self._fill_fallback_audio_page(page, query, form)
        if section == "notifications":
            page, confirm_title = self._fill_notifications_page(page, query, form)
            if confirm_title:
                title = f"{confirm_title} - {title}"
        if section in {"sdr_sharing", "pair_device", "find_devices"}:
            page, confirm_title = self._fill_sdr_sharing_page(page, section, query, form)
            if confirm_title:
                title = f"{confirm_title} - {title}"
        return page, title

    def _place_password_form(self, page: str, home_id: str) -> str:
        # One password form serves every account; the page script moves it the same way.
        return page_rendering.move_element(page, "change_password_form", home_id)

    def _fill_accounts_page(self, page: str, requester: AccountRecord, query: dict[str, str]) -> tuple[str, str]:
        accounts = self.service.accounts.list_accounts()
        token_input = page_rendering.form_token_input(self._form_token())
        rows = [account_row_html(account, requester.id, token_input) for account in accounts]
        page = page_rendering.set_content(page, "accounts-body", "".join(rows) or EMPTY_ACCOUNTS_ROW)
        # Resetting a password and deleting an account are confirmed first.
        confirm = query.get("confirm", "")
        target = next((account for account in accounts if str(account.get("id")) == query.get("account", "")), None)
        if confirm not in {"reset", "delete"} or target is None or target.get("owner") or target.get("id") == requester.id:
            return page, ""
        content, title = account_confirmation_html(confirm, target, token_input)
        page = page_rendering.set_content(page, "account_confirm", content)
        return page_rendering.set_hidden(page, "account_confirm", False), title

    def _dashboard_parts(self, read_only: bool) -> dict[str, Any]:
        status = self.service.status(read_only=read_only) or {}
        serial = str((status.get("settings") or {}).get("serial") or "")
        sdr_label = "none"
        if serial:
            device = next((item for item in (self.service.devices() or {}).get("devices") or [] if item.get("serial") == serial), None)
            parts = [device.get("vendor"), device.get("name") or "RTL-SDR", f"serial {serial}"] if device else []
            sdr_label = ", ".join(str(part) for part in parts if part) if device else f"serial {serial}"
        return dashboard_parts(status, sdr_label, read_only, time.time())

    def _fill_dashboard_view(self, page: str, read_only: bool) -> str:
        parts = self._dashboard_parts(read_only)
        for element_id, value in parts["text"].items():
            page = page_rendering.set_text(page, element_id, value)
        for element_id, value in parts["html"].items():
            page = page_rendering.set_content(page, element_id, value)
        for element_id, value in parts["hidden"].items():
            page = page_rendering.set_hidden(page, element_id, value)
        if parts["storage_first"]:
            # Storage goes first when it's low, as the page script puts it.
            for section in ("dashboard_sdr_section", "dashboard_streams_section", "dashboard_recent_alerts_section"):
                page = page_rendering.move_element(page, section, "view_dashboard")
        return page

    def _fill_logs_view(self, page: str, read_only: bool) -> str:
        status = self.service.status(read_only=read_only)
        # Keyed as the page script keys them, so it keeps these lines rather than replacing them.
        rows = []
        seen: dict[str, int] = {}
        for line in status.get("logs") or []:
            line = str(line)
            count = seen.get(line, 0)
            seen[line] = count + 1
            key = f"{line}#{count}" if count else line
            rows.append(f'<span data-row-key="{html.escape(key)}">{html.escape(line)}\n</span>')
        page = page_rendering.set_content(page, "logs", "".join(rows))
        last = status.get("last_batch_at")
        page = page_rendering.set_text(page, "active", "active" if status.get("active") else "inactive")
        page = page_rendering.set_text(page, "chunks", str(status.get("received_chunks", 0)))
        page = page_rendering.set_text(page, "bytes", str(status.get("received_bytes", 0)))
        page = page_rendering.set_text(page, "last", f"{last:.3f}s" if isinstance(last, (int, float)) else "never")
        return page_rendering.set_text(page, "capture-error", str(status.get("capture_error") or ""))
