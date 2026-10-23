"""Settings pages for browsers without the page script: SDR sharing, notifications, fallback audio and the RTL-SDR."""

from __future__ import annotations

import html
import re
from datetime import datetime
from http import HTTPStatus
from typing import Any

from . import page_rendering
from .baseline_pages import (
    FORM_EXPIRED_MESSAGE,
    form_message,
    NOTIFICATION_SETUP_STEPS,
    NOTIFICATIONS_SET_UP_HTML,
    NTFY_PUBLIC_SERVER_URL,
    page_route,
    REMOTE_SDR_CHOICE_PREFIX,
    render_notice_page,
)
from .constants import REMOTE_SDR_HOST_SETTINGS
from .custom_fallback_audio import MAX_CUSTOM_FALLBACK_BYTES
from .iq_recorder import format_duration_hms
from .page_sections import (
    active_sdr_choices,
    format_uptime,
    format_gain,
    generate_notification_topic,
    notification_access_url_options_html,
    notification_custom_access_url,
    notification_settings_payload,
    ntfy_server_is_self_hosted,
    ntfy_server_url,
    paired_device_row_html,
    remote_device_name,
    remote_pair_choice,
    remote_pair_target,
    remote_sdr_status_text,
    rtl_settings_changes,
)
from .web_address import advanced_flag_text, advanced_listening_text


class SettingsPageHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _handle_custom_fallback_audio_upload(self) -> None:
        # The page sends the file as it is. Requiring this type also means another
        # site cannot send one with a plain form.
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/octet-stream":
            self.close_connection = True
            self._send_json({"error": "the announcement must be sent as application/octet-stream"}, status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self.close_connection = True
            self._send_json({"error": "the announcement's size is missing"}, status=HTTPStatus.LENGTH_REQUIRED)
            return
        if length >= MAX_CUSTOM_FALLBACK_BYTES:
            # Not read at all, so the connection cannot be reused.
            self.close_connection = True
            self._send_json({"error": "The file is 10 MB or larger. Choose a file under 10 MB."}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return
        data = self.rfile.read(length) if length > 0 else b""
        if len(data) != length:
            self.close_connection = True
            self._send_json({"error": "the announcement did not arrive completely"}, status=HTTPStatus.BAD_REQUEST)
            return
        try:
            response = self.service.save_custom_fallback_audio(data)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    def _fill_sdr_sharing_page(self, page: str, section: str, query: dict[str, str], form: dict[str, str]) -> tuple[str, str]:
        remote_sdr = self.service.status().get("remote_sdr") or {}
        sharing = remote_sdr.get("sharing") or {}
        devices = list(remote_sdr.get("devices") or [])
        # One of the tab's three pages shows, as the page script shows them.
        for name in ("sdr_sharing", "pair_device", "find_devices"):
            page = page_rendering.set_hidden(page, f"settings_{name}_page", name != section)
        if section == "pair_device":
            return self._fill_pair_device_page(page, sharing), ""
        if section == "find_devices":
            return self._fill_find_devices_page(page, form), ""
        available = bool(sharing.get("available"))
        page = page_rendering.set_hidden(page, "remote_access_unavailable", available)
        page = page_rendering.set_hidden(page, "remote_access_controls", not available)
        page = page_rendering.set_attribute(page, "remote_access_enabled", "checked", "checked" if sharing.get("enabled") else None)
        page = page_rendering.append_content(
            page, "remote_access_form", f'<input type="hidden" name="was_remote_access_enabled" value="{"1" if sharing.get("enabled") else "0"}">'
        )
        page = page_rendering.set_hidden(page, "remote_pairing_controls", not sharing.get("running"))
        # The sharing machine shows a code; the other one enters it.
        page = page_rendering.set_hidden(page, "remote_pair_section", bool(sharing.get("enabled") and available))
        if sharing.get("error"):
            page = page_rendering.set_text(page, "remote-sharing-result", str(sharing["error"]))
            page = page_rendering.set_attribute(page, "remote-sharing-result", "class", "message error")
        remote = remote_sdr.get("remote") or {}
        if devices:
            page = page_rendering.set_content(page, "paired-devices-body", "".join(paired_device_row_html(device, remote) for device in devices))
        device = next((item for item in devices if item.get("id") == query.get("device")), None)
        if query.get("confirm") != "unpair" or device is None:
            return page, ""
        name = remote_device_name(device)
        content = (
            f"<h4>Unpair {html.escape(name)}?</h4>"
            "<p>Neither device will be able to use the other's RTL-SDR until you pair them again.</p>"
            f'<form method="post" action="/sdr-sharing/unpair">{page_rendering.form_token_input(self._form_token())}'
            f'<input type="hidden" name="device" value="{html.escape(str(device.get("id", "")))}">'
            '<div class="actions"><button type="submit" class="primary" autofocus>Unpair</button>'
            '<a class="button-link" href="/?view=settings&amp;section=sdr_sharing">Cancel</a></div></form>'
        )
        page = page_rendering.set_content(page, "sdr_sharing_confirm", content)
        page = page_rendering.set_hidden(page, "sdr_sharing_confirm", False)
        for element_id in ("sdr_sharing_section", "remote_pair_section", "paired_devices_section"):
            page = page_rendering.set_hidden(page, element_id, True)
        return page, f"Unpair {name}?"

    def _fill_pair_device_page(self, page: str, sharing: dict[str, Any]) -> str:
        """The pairing code. Opening this page keeps pairing open, as the page script does by checking in."""
        pairing = sharing.get("pairing") or {}
        if pairing.get("active"):
            pairing = (self.service.keep_remote_sdr_pairing_open().get("sharing") or {}).get("pairing") or pairing
        if pairing.get("active"):
            expires = datetime.fromtimestamp(float(pairing.get("expires_at") or 0)).strftime("%I:%M %p").lstrip("0")
            page = page_rendering.set_attribute(page, "remote_pairing_code", "value", str(pairing.get("code") or ""))
            page = page_rendering.set_text(
                page,
                "remote_pairing_code_hint",
                f"Works for one device, until {expires}.",
            )
            page = page_rendering.set_text(page, "remote-pairing-status", "Refresh this page to see whether a device has paired.")
            return page_rendering.set_attribute(page, "remote-pairing-status", "data-pairing", "active")
        # Pairing has ended: say how, and offer another code.
        reason = str(pairing.get("ended_reason") or "")
        message = {
            "paired": "A device paired with this one. It's listed under Paired devices, and either device can now use the other's shared RTL-SDR.",
            "expired": "The pairing code expired.",
            "too-many-attempts": "Pairing stopped after too many incorrect codes.",
        }.get(reason, "Pairing stopped." if sharing.get("running") else "Turn on sharing before pairing another NWR Stream Manager.")
        token_input = page_rendering.form_token_input(self._form_token())
        again = (
            f'<form method="post" action="/sdr-sharing/pairing/start">{token_input}<button type="submit">Show a new code</button></form>'
            if sharing.get("running") else ""
        )
        content = (
            f'<p class="message{" success" if reason == "paired" else ""}">{html.escape(message)}</p>'
            f'<div class="actions">{again}<a class="button-link" href="/?view=settings&amp;section=sdr_sharing">Back to SDR sharing</a></div>'
        )
        page = page_rendering.set_content(page, "remote_pairing_done", content)
        page = page_rendering.set_hidden(page, "remote_pairing_done", False)
        for element_id in ("remote_pairing_code_label", "remote_pairing_code_hint", "remote_pairing_stop_form"):
            page = page_rendering.set_hidden(page, element_id, True)
        return page

    def _fill_find_devices_page(self, page: str, form: dict[str, str]) -> str:
        """Devices ready to pair, from one search; Search again searches once more."""
        try:
            hosts = list(self.service.discover_remote_sdrs().get("hosts") or [])
            searched = ""
        except Exception as exc:
            hosts, searched = [], f"Searching failed: {form_message(str(exc))}"
        choices = [
            (remote_pair_choice(host), f"{remote_device_name(host)} ({'Tailscale' if host.get('via') == 'tailscale' else 'local network'}){', already paired' if host.get('paired') else ''}")
            for host in hosts
        ] + [("manual", "Enter an address")]
        first_unpaired = next((remote_pair_choice(host) for host in hosts if not host.get("paired")), choices[0][0])
        chosen = form.get("device") if form.get("device") in {value for value, _label in choices} else first_unpaired
        page = page_rendering.set_content(
            page,
            "remote_pair_device",
            "".join(f'<option value="{html.escape(value)}"{" selected" if value == chosen else ""}>{html.escape(label)}</option>' for value, label in choices),
        )
        count = len(hosts)
        status = searched or (
            f"Found {count} device{'' if count == 1 else 's'} ready to pair." if count
            else "No devices are ready to pair yet. Once the other device shows a pairing code, select Search again."
        )
        page = page_rendering.set_text(page, "remote_pair_search_status", status)
        page = page_rendering.set_attribute(page, "remote_pair_address", "value", form.get("address", ""))
        # Shown here, as a page can't show it as it's chosen; a script, where one runs, does.
        page = page_rendering.set_hidden(page, "remote_pair_address_label", False)
        return page_rendering.set_hidden(page, "remote_pair_address_hint", False)

    def _handle_sdr_sharing_form(self, path: str) -> None:
        """Shares the RTL-SDR and pairs, for browsers without the page script."""
        form: dict[str, str] = {}
        query = {"view": "settings", "section": "sdr_sharing"}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            if path == "/sdr-sharing/pairing/start":
                query["section"] = "pair_device"
                self.service.start_remote_sdr_pairing(held=True)
                self._send_redirect(page_route("settings", section="pair_device"))
                return
            if path == "/sdr-sharing/pairing/stop":
                self.service.cancel_remote_sdr_pairing()
                done = {"result": "stopped"}
            elif path == "/sdr-sharing/pair":
                query["section"] = "find_devices"
                code = re.sub(r"\D", "", form.get("code", ""))
                target = remote_pair_target(form)
                if len(code) != 6:
                    raise ValueError("Enter the 6-digit pairing code shown on the other device.")
                before = {device.get("id") for device in self.service.remote_sdr_status().get("devices") or []}
                devices = self.service.pair_remote_sdr({**target, "code": code}).get("devices") or []
                added = next((device for device in devices if device.get("id") not in before), None)
                done = {"result": "paired", "name": remote_device_name(added)}
            elif path == "/sdr-sharing/unpair":
                device = next((item for item in self.service.remote_sdr_status().get("devices") or [] if item.get("id") == form.get("device")), None)
                if device is None:
                    raise ValueError("That device isn't paired.")
                self.service.unpair_remote_sdr_device({"device_id": device.get("id")})
                done = {"result": "unpaired", "name": remote_device_name(device)}
            else:
                enabled = form.get("remote_access_enabled") == "1"
                if enabled != (form.get("was_remote_access_enabled") == "1"):
                    self.service.update({"remote_access_enabled": enabled})
                done = {"result": "shared" if enabled else "unshared"}
        except Exception as exc:
            element = {"find_devices": "remote-pair-result", "pair_device": "remote-pairing-status"}.get(query["section"], "remote-sharing-result")
            if path == "/sdr-sharing/unpair":
                element = "paired-devices-result"
            self._send_page(self._render_index_page(query, result=(element, form_message(str(exc)), "error"), form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("settings", section="sdr_sharing", **done))

    def _fill_notifications_page(self, page: str, query: dict[str, str], form: dict[str, str]) -> tuple[str, str]:
        settings = self.service.notification_settings_snapshot().public_dict()
        options = list(self.service.notification_access_url_options().get("options") or [])
        configured = bool(settings.get("topic"))
        setup = bool(query.get("setup")) or form.get("form") == "notification_setup"
        page = page_rendering.set_hidden(page, "notification_intro", configured or setup)
        page = page_rendering.set_hidden(page, "notification_configured", not configured or setup)
        page = page_rendering.set_hidden(page, "notification_wizard", not setup)
        saved_mode = str(settings.get("access_url_mode") or "auto")
        if setup:
            return self._fill_notification_setup(page, form, settings, options), "Set up notifications"
        if not configured:
            return page, ""
        server_url = str(settings.get("server_url") or NTFY_PUBLIC_SERVER_URL)
        loaded = {
            "server_mode": "self-hosted" if ntfy_server_is_self_hosted(server_url) else "ntfy.sh",
            "server_url": server_url if ntfy_server_is_self_hosted(server_url) else "",
            "access_url_mode": saved_mode,
            "custom_access_url": str(settings.get("custom_access_url") or ""),
        }
        sent = form.get("form") == "notifications"
        shown = {name: form.get(name, value) for name, value in loaded.items()} if sent else dict(loaded)
        if sent:
            loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
        page = page_rendering.set_content(
            page,
            "notification_server_mode",
            "".join(
                f'<option value="{value}"{" selected" if value == shown["server_mode"] else ""}>{label}</option>'
                for value, label in (("ntfy.sh", "ntfy.sh"), ("self-hosted", "Self-hosted instance"))
            ),
        )
        page = page_rendering.set_attribute(page, "notification_configured_server_url", "value", shown["server_url"])
        page = page_rendering.set_attribute(page, "notification_configured_topic_secret", "value", str(settings.get("topic")))
        page = page_rendering.set_content(
            page, "notification_access_url_mode", notification_access_url_options_html(options, saved_mode, shown["access_url_mode"])
        )
        page = page_rendering.set_attribute(page, "notification_custom_access_url", "value", shown["custom_access_url"])
        # Shown here, as a page can't show them as they're chosen; a script, where one runs, does.
        for element_id in (
            "notification_configured_server_url_label", "notification_configured_server_url_hint",
            "notification_custom_access_url_label", "notification_custom_access_url_hint",
        ):
            page = page_rendering.set_hidden(page, element_id, False)
        page = page_rendering.append_content(
            page,
            "notification_settings_form",
            '<input type="hidden" name="form" value="notifications">'
            + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
        )
        if query.get("confirm") != "regenerate":
            return page, ""
        content = (
            "<h4>Regenerate the topic secret?</h4>"
            "<p>You will need to unsubscribe from the old topic and subscribe to the new one on every device.</p>"
            f'<form method="post" action="/notifications/regenerate">{page_rendering.form_token_input(self._form_token())}'
            '<div class="actions"><button type="submit" class="primary" autofocus>Regenerate topic secret</button>'
            '<a class="button-link" href="/?view=settings&amp;section=notifications">Cancel</a></div></form>'
        )
        page = page_rendering.set_content(page, "notification_confirm", content)
        page = page_rendering.set_hidden(page, "notification_confirm", False)
        page = page_rendering.set_hidden(page, "notification_configured", True)
        return page, "Regenerate the topic secret?"

    def _notification_setup_state(self, form: dict[str, str], settings: dict[str, Any]) -> dict[str, str]:
        return {
            "step": form.get("step") if form.get("step") in NOTIFICATION_SETUP_STEPS else "server_choice",
            "self_hosted": "yes" if form.get("notification_self_hosted") == "yes" else "no",
            "server_url": form.get("server_url", ""),
            "topic": form.get("topic", ""),
            "access_url_mode": form.get("access_url_mode", str(settings.get("access_url_mode") or "auto")),
            "custom_access_url": form.get("custom_access_url", str(settings.get("custom_access_url") or "")),
        }

    def _notification_setup_payload(self, state: dict[str, str], settings: dict[str, Any]) -> dict[str, Any]:
        server_url = ntfy_server_url(state["server_url"]) if state["self_hosted"] == "yes" else NTFY_PUBLIC_SERVER_URL
        custom = state["access_url_mode"] == "custom"
        return notification_settings_payload(
            settings,
            server_url=server_url,
            topic=state["topic"],
            access_url_mode=state["access_url_mode"],
            custom_access_url=notification_custom_access_url(state["custom_access_url"]) if custom else str(settings.get("custom_access_url") or ""),
        )

    def _fill_notification_setup(self, page: str, form: dict[str, str], settings: dict[str, Any], options: list[dict[str, str]]) -> str:
        """Setting up notifications a step at a time; every step's answers go with the form."""
        state = self._notification_setup_state(form if form.get("form") == "notification_setup" else {}, settings)
        step = state["step"]
        page = page_rendering.set_attribute(page, "notification_wizard_step", "value", step)
        page = page_rendering.set_attribute(page, "notification_wizard_topic", "value", state["topic"])
        page = page_rendering.append_content(page, "notification_wizard", '<input type="hidden" name="form" value="notification_setup">')
        for name in NOTIFICATION_SETUP_STEPS:
            page = page_rendering.set_hidden(page, f"notification_step_{name}", name != step)
        for value in ("no", "yes"):
            page = page_rendering.set_attribute(page, f"notification_self_hosted_{value}", "checked", "checked" if state["self_hosted"] == value else None)
        page = page_rendering.set_attribute(page, "notification_server_url", "value", state["server_url"])
        server = state["server_url"] if state["self_hosted"] == "yes" else NTFY_PUBLIC_SERVER_URL
        try:
            web_app = f"{ntfy_server_url(server)}/app"
        except ValueError:
            web_app = f"{NTFY_PUBLIC_SERVER_URL}/app"
        page = page_rendering.set_attribute(page, "notification_web_app_link", "href", web_app)
        page = page_rendering.set_attribute(page, "notification_topic_secret", "value", state["topic"])
        saved_mode = str(settings.get("access_url_mode") or "auto")
        page = page_rendering.set_content(page, "notification_wizard_access_url_mode", notification_access_url_options_html(options, saved_mode, state["access_url_mode"]))
        page = page_rendering.set_attribute(page, "notification_wizard_custom_access_url", "value", state["custom_access_url"])
        page = page_rendering.set_hidden(page, "notification_wizard_custom_access_url_label", False)
        page = page_rendering.set_hidden(page, "notification_wizard_custom_access_url_hint", False)
        # This step's buttons, going forward first so Enter goes forward.
        buttons = ["Finish" if step == "test" else "Next"]
        if step != "server_choice":
            buttons.append("Back")
        buttons.append("Cancel")
        page = page_rendering.set_content(
            page,
            "notification_wizard_baseline_actions",
            "".join(f'<input type="submit" name="setup_action" value="{label}" data-baseline-submit>' for label in buttons),
        )
        if step == "test":
            page = page_rendering.set_content(
                page, "notification_wizard_test_actions", '<input type="submit" name="setup_action" value="Send test notification" data-baseline-submit>'
            )
        return page

    def _handle_notification_form(self, path: str) -> None:
        """Sets up and changes notifications, for browsers without the page script."""
        form: dict[str, str] = {}
        settings_page = {"view": "settings", "section": "notifications"}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            settings = self.service.notification_settings_snapshot().public_dict()
            if path == "/notifications/setup":
                self._handle_notification_setup(form, settings)
                return
            if path == "/notifications/test":
                self.service.test_notification_settings(notification_settings_payload(settings))
                done = "tested"
            elif path == "/notifications/regenerate":
                self.service.update_notification_settings(notification_settings_payload(settings, topic=generate_notification_topic()))
                done = "regenerated"
            else:
                changes: dict[str, Any] = {}
                if any(form.get(name, "") != form.get(f"was_{name}", "") for name in ("server_mode", "server_url")):
                    if form.get("server_mode") == "self-hosted":
                        changes["server_url"] = ntfy_server_url(form.get("server_url", ""))
                    else:
                        changes["server_url"] = NTFY_PUBLIC_SERVER_URL
                if any(form.get(name, "") != form.get(f"was_{name}", "") for name in ("access_url_mode", "custom_access_url")):
                    changes["access_url_mode"] = form.get("access_url_mode", "auto")
                    if changes["access_url_mode"] == "custom":
                        changes["custom_access_url"] = notification_custom_access_url(form.get("custom_access_url", ""))
                if changes:
                    self.service.update_notification_settings(notification_settings_payload(settings, **changes))
                done = "saved"
        except Exception as exc:
            result = ("notification-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(settings_page, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("settings", section="notifications", result=done))

    def _handle_notification_setup(self, form: dict[str, str], settings: dict[str, Any]) -> None:
        state = self._notification_setup_state(form, settings)
        step, action = state["step"], form.get("setup_action", "")
        settings_page = {"view": "settings", "section": "notifications"}
        message: tuple[str, str] | None = None
        try:
            if action == "Cancel":
                self._send_redirect(page_route("settings", section="notifications"))
                return
            if action == "Back":
                if step == "app_install":
                    state["step"] = "server_url" if state["self_hosted"] == "yes" else "server_choice"
                else:
                    state["step"] = NOTIFICATION_SETUP_STEPS[max(0, NOTIFICATION_SETUP_STEPS.index(step) - 1)]
            elif step == "server_choice":
                state["step"] = "server_url" if state["self_hosted"] == "yes" else "app_install"
            elif step == "server_url":
                ntfy_server_url(state["server_url"])
                state["step"] = "app_install"
            elif step == "app_install":
                state["topic"] = state["topic"] or generate_notification_topic()
                state["step"] = "topic"
            elif step == "topic":
                state["step"] = "access_url"
            elif step == "access_url":
                if state["access_url_mode"] == "custom":
                    notification_custom_access_url(state["custom_access_url"])
                state["step"] = "test"
            elif action == "Send test notification":
                response = self.service.test_notification_settings(self._notification_setup_payload(state, settings))
                message = (str(response.get("message") or "Test notification sent."), "success")
            else:
                self.service.update_notification_settings(self._notification_setup_payload(state, settings))
                self._send_page(render_notice_page("Notifications are set up", NOTIFICATIONS_SET_UP_HTML, settings_page))
                return
        except Exception as exc:
            message = (form_message(str(exc)), "error")
        fields = {**form, "step": state["step"], "topic": state["topic"]}
        result = ("notification-result", message[0], message[1]) if message else None
        status = HTTPStatus.BAD_REQUEST if message and message[1] == "error" else HTTPStatus.OK
        self._send_page(self._render_index_page(settings_page, result=result, form=fields), status)

    def _fill_fallback_audio_page(self, page: str, query: dict[str, str], form: dict[str, str]) -> str:
        fallback = self.service.status().get("fallback") or {}
        available = bool(fallback.get("custom_audio_available"))
        loaded = {"fallback_audio": str(fallback.get("audio") or "default"), "loop_delay_seconds": f"{float(fallback.get('loop_delay_seconds') or 0):g}"}
        sent = form.get("form") == "fallback"
        shown = {name: form.get(name, value) for name, value in loaded.items()} if sent else dict(loaded)
        if sent:
            loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
        if query.get("result") == "custom-needed":
            shown["fallback_audio"] = "custom"
        custom = shown["fallback_audio"] == "custom"
        page = page_rendering.set_attribute(page, "fallback_audio_default", "checked", None if custom else "checked")
        page = page_rendering.set_attribute(page, "fallback_audio_custom", "checked", "checked" if custom else None)
        page = page_rendering.set_attribute(page, "fallback_loop_delay", "value", shown["loop_delay_seconds"])
        # The chosen announcement plays in the browser's own player, which a script hides where it can't.
        playable = available or not custom
        announcement = f"/api/fallback-audio?announcement={'custom' if custom else 'default'}"
        page = page_rendering.set_hidden(page, "fallback_baseline_player", not playable)
        if playable:
            page = page_rendering.set_attribute(page, "fallback_baseline_audio", "src", announcement)
            page = page_rendering.set_attribute(page, "fallback_baseline_download", "href", announcement)
        problem = str(fallback.get("custom_audio_problem") or "")
        if problem:
            text = f"{problem} The default announcement is playing instead. Upload or record a new custom announcement." if fallback.get("audio") == "custom" else problem
        elif available:
            text = f"The custom announcement is {format_duration_hms(float(fallback.get('custom_audio_seconds') or 0))} long."
        elif custom:
            text = "There's no custom announcement yet. Upload a file or record one below."
        else:
            text = ""
        page = page_rendering.set_text(page, "fallback_custom_status", text)
        page = page_rendering.set_attribute(page, "fallback_custom_status", "class", "message error" if problem else "hint")
        page = page_rendering.set_hidden(page, "fallback_custom_status", not text)
        # Uploading shows for Custom; a script, where one runs, shows it as Custom is chosen.
        page = page_rendering.set_hidden(page, "fallback_custom_controls", False)
        return page_rendering.append_content(
            page,
            "fallback_settings_form",
            '<input type="hidden" name="form" value="fallback">'
            + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
        )

    def _handle_fallback_settings_form(self) -> None:
        """Saves the fallback audio settings that were changed, for browsers without the page script."""
        form: dict[str, str] = {}
        query = {"view": "settings", "section": "fallback_audio"}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            changes: dict[str, Any] = {}
            delay = form.get("loop_delay_seconds", "").strip()
            if delay != form.get("was_loop_delay_seconds", "").strip():
                try:
                    changes["loop_delay_seconds"] = float(delay)
                except ValueError:
                    changes["loop_delay_seconds"] = -1.0
                if not 0 <= changes["loop_delay_seconds"] <= 10:
                    raise ValueError("Enter a number of seconds before restart from 0 to 10.")
            audio = form.get("fallback_audio", "default")
            done = "saved"
            if audio != form.get("was_fallback_audio", ""):
                fallback = self.service.status().get("fallback") or {}
                if audio == "custom" and not fallback.get("custom_audio_available"):
                    # Nothing to switch to yet, so the page shows how to add one.
                    done = "custom-needed"
                else:
                    changes["audio"] = audio
            if changes:
                self.service.update_fallback_settings(changes)
        except Exception as exc:
            result = ("fallback-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("settings", section="fallback_audio", result=done))

    def _fill_general_page(self, page: str, form: dict[str, str]) -> str:
        settings = self.service.general_settings()
        loaded = {name: str(settings[name]) for name in ("instance_name", "location")}
        sent = form.get("form") == "general"
        shown = {name: form.get(name, value) for name, value in loaded.items()} if sent else dict(loaded)
        if sent:
            loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
        page = page_rendering.set_attribute(page, "general_instance_name", "value", shown["instance_name"])
        page = page_rendering.set_attribute(page, "general_location", "value", shown["location"])
        uptime = self.service.uptime_seconds()
        if isinstance(uptime, (int, float)):
            page = page_rendering.set_text(page, "general_uptime", format_uptime(uptime))
        return page_rendering.append_content(
            page,
            "general_settings_form",
            '<input type="hidden" name="form" value="general">'
            + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
        )

    def _handle_general_settings_form(self) -> None:
        """Saves the General settings that were changed, for browsers without the page script."""
        form: dict[str, str] = {}
        query = {"view": "settings", "section": "general"}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            changes = {
                name: form.get(name, "") for name in ("instance_name", "location") if form.get(name, "") != form.get(f"was_{name}", "")
            }
            if changes:
                self.service.update_general_settings(changes)
        except Exception as exc:
            result = ("general-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("settings", section="general", result="saved"))

    def _fill_advanced_page(self, page: str, form: dict[str, str]) -> str:
        settings = self.service.advanced_settings()
        loaded = {"port": str(settings["port"]), "localhost_only": "1" if settings["localhost_only"] else "0"}
        sent = form.get("form") == "advanced"
        shown = dict(loaded)
        if sent:
            shown = {"port": form.get("port", loaded["port"]), "localhost_only": form.get("localhost_only", "0")}
            loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
        page = page_rendering.set_text(page, "advanced_listening", advanced_listening_text(settings))
        page = page_rendering.set_attribute(page, "advanced_port", "value", shown["port"])
        page = page_rendering.set_attribute(page, "advanced_localhost_only", "checked", "checked" if shown["localhost_only"] == "1" else None)
        for name, flag in (("advanced_port_flag", settings["port_flag"]), ("advanced_host_flag", settings["host_flag"])):
            page = page_rendering.set_text(page, name, advanced_flag_text(flag))
            page = page_rendering.set_hidden(page, name, not flag)
        return page_rendering.append_content(
            page,
            "advanced_settings_form",
            '<input type="hidden" name="form" value="advanced">'
            + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
        )

    def _handle_advanced_settings_form(self) -> None:
        """Saves the Advanced settings that were changed, for browsers without the page script."""
        form: dict[str, str] = {}
        query = {"view": "settings", "section": "advanced"}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            changes: dict[str, Any] = {}
            port = form.get("port", "").strip()
            if port != form.get("was_port", "").strip():
                changes["port"] = port
            localhost_only = form.get("localhost_only") == "1"
            if localhost_only != (form.get("was_localhost_only") == "1"):
                changes["localhost_only"] = localhost_only
            if changes:
                self.service.update_advanced_settings(changes)
        except Exception as exc:
            result = ("advanced-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("settings", section="advanced", result="saved"))

    def _handle_restart_form(self) -> None:
        """Restarts NWR Stream Manager, for browsers without the page script."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            self.service.request_restart({"instance": form.get("instance", "")}, self._current_account_name())
        except Exception as exc:
            result = ("restart-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page({"view": "restart"}, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        # Not a redirect: by the time a browser followed one, NWR Stream Manager would be gone.
        self._send_page(render_notice_page(
            "NWR Stream Manager is restarting",
            "<p>It's shutting down, and will start again in a few seconds. Select Done to go back to the dashboard. "
            "If it isn't back yet, wait a moment and refresh the page.</p>",
            {},
        ))

    def _handle_fallback_upload_form(self) -> None:
        """Uploads a custom announcement from a plain form, for browsers without the page script."""
        query = {"view": "settings", "section": "fallback_audio"}
        try:
            content_type = self.headers.get("Content-Type", "")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                length = -1
            # A form adds a little to the file, but not this much.
            if length < 0 or length > MAX_CUSTOM_FALLBACK_BYTES + 64 * 1024:
                self.close_connection = True
                raise ValueError("The file is 10 MB or larger. Choose a file under 10 MB.")
            data = self.rfile.read(length)
            if not content_type.lower().startswith("multipart/form-data"):
                raise ValueError("Choose a file to upload.")
            fields, files = page_rendering.parse_multipart_form(data, content_type)
            if not self._form_token_ok(fields):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            announcement = files.get("announcement", b"")
            if not announcement:
                raise ValueError("Choose a file to upload.")
            if len(announcement) >= MAX_CUSTOM_FALLBACK_BYTES:
                raise ValueError("The file is 10 MB or larger. Choose a file under 10 MB.")
            self.service.save_custom_fallback_audio(announcement)
        except Exception as exc:
            result = ("fallback-audio-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("settings", section="fallback_audio", result="uploaded"))

    def _fill_rtl_view(
        self, page: str, query: dict[str, str], form: dict[str, str], title: str
    ) -> tuple[str, str, tuple[str, str, str] | None]:
        status = self.service.status()
        devices = self.service.devices()
        settings = status.get("settings") or {}
        remote_sdr = status.get("remote_sdr") or {}
        uses_remote = settings.get("source_mode") == "remote" and bool(settings.get("remote_host_id"))
        local_devices = list(devices.get("devices") or [])
        gain_values = [float(value) for value in status.get("gain_values") or []]
        # What the page has now; every setting is sent with this, so only the ones changed are saved.
        serial = f"{REMOTE_SDR_CHOICE_PREFIX}{settings.get('remote_host_id')}" if uses_remote else str(settings.get("serial") or "")
        if not uses_remote and not serial and len(local_devices) == 1:
            serial = str(local_devices[0].get("serial"))
        gain = settings.get("gain")
        loaded = {
            "serial": serial,
            "gain": f"{min(gain_values, key=lambda value: abs(value - float(gain))):g}" if gain is not None and gain_values else "auto",
            "ppm_correction": str(settings.get("ppm_correction", 0)),
            "alias_filter_strength": str(settings.get("alias_filter_strength", 100)),
            "bias_tee": "1" if settings.get("bias_tee") else "0",
            "notify_sdr_failures": "1" if settings.get("notify_sdr_failures") else "0",
            "remote_buffer_seconds": f"{float(settings.get('remote_buffer_seconds') or 0):g}",
        }
        sent = form.get("form") == "rtl"
        # Shown again after a mistake, the page keeps what was entered and what it first had.
        shown = {name: form.get(name, "0" if name in {"bias_tee", "notify_sdr_failures"} else value) for name, value in loaded.items()} if sent else loaded
        if sent:
            loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
        page = page_rendering.set_content(
            page,
            "serial",
            "".join(
                f'<option value="{html.escape(value)}"{" selected" if value == shown["serial"] else ""}>{html.escape(label)}</option>'
                for value, label in active_sdr_choices(status, local_devices)
            ),
        )
        page = page_rendering.set_text(page, "device-errors", " | ".join(devices.get("errors") or []))
        reset = status.get("sdr_reset") or None
        resetting = bool(reset and reset.get("state") == "resetting")
        page = page_rendering.set_hidden(page, "reset_rtl_device", uses_remote)
        page = page_rendering.set_hidden(page, "reset_remote_sdr", not uses_remote)
        page = page_rendering.set_hidden(page, "reset_sdr_link", resetting)
        if reset:
            kind = {"finished": "success", "failed": "error"}.get(str(reset.get("state")), "")
            page = page_rendering.set_text(page, "sdr-reset-status", str(reset.get("message") or ""))
            page = page_rendering.set_attribute(page, "sdr-reset-status", "class", f"message {kind}".strip())
        page = page_rendering.set_hidden(page, "remote_sdr_controls", not uses_remote)
        page = page_rendering.set_text(page, "remote_sdr_status", remote_sdr_status_text(remote_sdr))
        page = page_rendering.set_attribute(page, "remote_buffer_seconds", "value", shown["remote_buffer_seconds"])
        options = [("auto", "Automatic gain control")] + [(f"{value:g}", format_gain(value)) for value in gain_values]
        page = page_rendering.set_content(
            page,
            "gain_choice",
            "".join(
                f'<option value="{value}"{" selected" if value == shown["gain"] else ""}>{html.escape(label)}</option>'
                for value, label in options
            ),
        )
        page = page_rendering.set_attribute(page, "ppm_correction", "value", shown["ppm_correction"])
        page = page_rendering.set_attribute(page, "alias_filter_strength", "value", shown["alias_filter_strength"])
        page = page_rendering.set_text(page, "alias_filter_strength_label", f"{shown['alias_filter_strength']}%")
        for name in ("bias_tee", "notify_sdr_failures"):
            page = page_rendering.set_attribute(page, name, "checked", "checked" if shown[name] == "1" else None)
        page = page_rendering.append_content(
            page,
            "rtl_settings_form",
            '<input type="hidden" name="form" value="rtl">'
            + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
        )
        # Save changes comes last on the page.
        notifications_shown = bool((status.get("notifications") or {}).get("topic"))
        page = page_rendering.set_hidden(page, "rtl_notifications_section", not notifications_shown)
        if notifications_shown:
            page = page_rendering.move_element(page, "rtl_save_actions", "rtl_notifications_section")
        if query.get("confirm") == "reset" and not resetting:
            content = (
                "<section><h2>Reset the RTL-SDR?</h2>"
                "<p>The RTL-SDR restarts without being unplugged. Streams and anything else using it stop for a few seconds.</p>"
                f'<form method="post" action="/rtl-settings/reset">{page_rendering.form_token_input(self._form_token())}'
                '<div class="actions"><button type="submit" class="primary" autofocus>Reset SDR</button>'
                '<a class="button-link" href="/?view=rtl">Cancel</a></div></form></section>'
            )
            page = page_rendering.set_content(page, "rtl_reset_confirm", content)
            page = page_rendering.set_hidden(page, "rtl_reset_confirm", False)
            page = page_rendering.set_hidden(page, "rtl_settings_form", True)
            return page, f"Reset the RTL-SDR? - {title}", None
        done = query.get("result", "")
        if done not in {"saved", "source", "source-only"}:
            return page, title, None
        if done == "saved":
            return page, title, ("rtl-save-result", "Changes saved.", "success")
        device = next((item for item in remote_sdr.get("devices") or [] if item.get("id") == settings.get("remote_host_id")), None)
        message = (
            f"Now using the RTL-SDR shared by {remote_device_name(device)}."
            if uses_remote
            else "Now using the RTL-SDR connected to this machine."
        )
        if done == "source-only":
            # Gain and the like belong to one RTL-SDR, so they aren't carried to another.
            message += " The gain and other settings you changed were for the other RTL-SDR, so they weren't saved. These are this one's."
        return page, title, ("rtl-save-result", message, "success")

    def _handle_rtl_form(self, path: str) -> None:
        """Saves the RTL-SDR settings, or resets it, for browsers without the page script."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            status = self.service.status()
            if path == "/rtl-settings/reset":
                # Takes as long as the reset; the page then says how it went.
                reset = self.service.reset_rtl_device(str((status.get("settings") or {}).get("serial") or ""))
                kind = "success" if reset.get("success") and reset.get("reappeared") else "error"
                message = str(reset.get("message") or "RTL-SDR reset finished.")
                self._send_page(self._render_index_page({"view": "rtl"}, result=("sdr-reset-status", message, kind)))
                return
            changes = rtl_settings_changes(form, [float(value) for value in status.get("gain_values") or []])
            done = "source" if "source_mode" in changes or "serial" in changes else "saved"
            if "source_mode" in changes:
                # A shared RTL-SDR has its own gain and the like, and so does this machine's;
                # they aren't carried from one to the other. The page then shows the new one's.
                dropped = [name for name in REMOTE_SDR_HOST_SETTINGS if name in changes]
                for name in dropped:
                    del changes[name]
                if dropped:
                    done = "source-only"
            if changes:
                self.service.update(changes)
        except Exception as exc:
            element = "sdr-reset-status" if path == "/rtl-settings/reset" else "rtl-save-result"
            result = (element, form_message(str(exc)), "error")
            self._send_page(self._render_index_page({"view": "rtl"}, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(f"/?view=rtl&result={done}")
