"""Stream pages for browsers without the page script: streams, their settings and outputs, and the new stream wizard."""

from __future__ import annotations

import html
from http import HTTPStatus
from typing import Any

from . import page_rendering
from .baseline_pages import (
    FORM_EXPIRED_MESSAGE,
    form_message,
    page_route,
    render_notice_page,
    STREAM_SETTINGS_TABS,
    WIZARD_STEP_PANELS,
    WIZARD_STEPS,
)
from .config import notch_max_width
from .constants import (
    SOUNDCARD_PREVIEW_LEASE_SECONDS,
    STREAM_FALLBACK_ACTIONS,
    STREAM_SERVICE_CUSTOM,
    STREAM_SERVICE_GWES,
    STREAM_SERVICE_NAMES,
    STREAM_SERVICE_NWRORG,
    STREAM_SERVICE_WEATHERUSA,
)
from .page_sections import (
    apply_icecast_service_preset,
    available_soundcard_channels,
    fill_icecast_fields,
    fill_soundcard_channels,
    ICECAST_FORM_FIELDS,
    icecast_form_values,
    icecast_payload,
    icecast_service,
    NWRORG_SUBMISSION_HTML,
    RECEPTION_PROBLEM_DETAILS,
    soundcard_device_options_html,
    soundcard_label,
    station_choice_label,
    stream_enabled,
    stream_label,
    stream_output_count,
    stream_output_row_html,
    stream_reception_problem,
    stream_row_html,
    stream_setting_text,
    stream_setting_value,
    stream_settings_changes,
    STREAM_SETTINGS_FIELDS,
    stream_settings_groups,
    stream_signal_display,
    weatherusa_mount,
)


class StreamPageHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _fill_streams_view(self, page: str, query: dict[str, str], read_only: bool, title: str) -> tuple[str, str]:
        status = self.service.status(read_only=read_only)
        streams = list(status.get("streams") or [])
        token_input = page_rendering.form_token_input(self._form_token())
        rows = "".join(stream_row_html(stream, status, read_only, token_input) for stream in streams)
        if rows:
            page = page_rendering.set_content(page, "active-streams-body", rows)
        page = page_rendering.set_hidden(page, "open_add_stream_link", read_only)
        # Removing a stream is confirmed first.
        stream = next((item for item in streams if item.get("id") == query.get("stream")), None)
        if query.get("confirm") != "remove" or stream is None or read_only:
            return page, title
        label = html.escape(stream_label(stream))
        content = (
            f"<h3>Remove stream {label}?</h3>"
            "<p>Its outputs and settings will be permanently deleted. EAS alerts it recorded are kept.</p>"
            f'<form method="post" action="/streams/remove">{token_input}'
            f'<input type="hidden" name="stream" value="{html.escape(str(stream.get("id", "")))}">'
            '<div class="actions"><button type="submit" class="primary" autofocus>Remove stream</button>'
            '<a class="button-link" href="/?view=streams">Cancel</a></div></form>'
        )
        page = page_rendering.set_content(page, "streams_confirm", content)
        page = page_rendering.set_hidden(page, "streams_confirm", False)
        return page, f"Remove stream {stream_label(stream)}? - {title}"

    def _fill_stream_settings_view(self, page: str, query: dict[str, str], form: dict[str, str], title: str) -> tuple[str, str]:
        status = self.service.status()
        stream = next(item for item in status.get("streams") or [] if item.get("id") == query.get("stream"))
        stream_id = str(stream.get("id", ""))
        station = stream.get("station") or {}
        callsign = str(station.get("callsign") or "stream")
        page = page_rendering.set_text(page, "stream_settings_title", f"Edit stream {callsign}")
        page = page_rendering.set_text(page, "stream_settings_station", f"{station.get('callsign') or 'Unknown'} {station.get('frequency') or ''} MHz")
        page = page_rendering.set_attribute(page, "stream_settings_stream_id", "value", stream_id)
        # Laid out like Settings: the sections are links, and the one chosen shows beside
        # them, or on its own on phones. Outputs shows beside them until one is chosen.
        notifications = bool((status.get("notifications") or {}).get("topic"))
        tabs = [name for name in STREAM_SETTINGS_TABS if name != "notifications" or notifications]
        chosen = query.get("tab") if query.get("tab") in tabs else ""
        tab = chosen or "outputs"
        page = page_rendering.set_attribute(page, "stream_settings_tab", "value", chosen)
        page = page_rendering.set_hidden(page, "tab_link_notifications", not notifications)
        for name in STREAM_SETTINGS_TABS:
            link = page_route("stream_settings", stream=stream_id, tab=name)
            page = page_rendering.set_attribute(page, f"tab_link_{name}", "href", link)
            page = page_rendering.set_attribute(page, f"tab_link_{name}", "aria-current", "page" if name == tab else None)
            page = page_rendering.set_hidden(page, f"panel_{name}", name != tab)
        page = page_rendering.set_hidden(page, "panel_test_mode", True)
        layout = "settings-layout stream-settings-layout" + (" settings-detail-active" if chosen else "")
        page = page_rendering.set_attribute(page, "stream_settings_layout", "class", layout)
        page = page_rendering.set_attribute(page, "stream_settings_back_link", "href", page_route("stream_settings", stream=stream_id))
        # Signal
        signal = (status.get("stream_signals") or {}).get(stream_id) if stream_enabled(stream) else None
        display = stream_signal_display(signal)
        problem = stream_reception_problem(signal)
        text = display["text"]
        if not stream_enabled(stream):
            text = "Not measured while this stream is disabled."
        elif problem == "no_signal":
            text = RECEPTION_PROBLEM_DETAILS["no_signal"]
        elif problem:
            text = f"{display['text']}. {RECEPTION_PROBLEM_DETAILS['bad_reception']}"
        page = page_rendering.set_text(page, "stream_settings_signal_value", text)
        page = page_rendering.set_attribute(
            page, "stream_settings_signal_value", "class", f"status-text signal-{display['quality']}" if display["quality"] else "hint"
        )
        page = page_rendering.set_hidden(page, "stream_settings_signal_levels_label", not display["detail"])
        page = page_rendering.set_hidden(page, "stream_settings_signal_levels", not display["detail"])
        page = page_rendering.set_text(page, "stream_settings_signal_levels", display["detail"])
        # The settings, and what they were, so only the ones changed are saved.
        groups = stream_settings_groups(stream)
        loaded = {
            name: stream_setting_text(stream_setting_value(groups, group, key), kind)
            for name, group, key, kind in STREAM_SETTINGS_FIELDS
        }
        sent = form.get("form") == "stream_settings"
        shown = dict(loaded)
        if sent:
            # Shown again after a mistake, the page keeps what was entered and what it first had.
            for name, _group, _key, kind in STREAM_SETTINGS_FIELDS:
                shown[name] = ("1" if form.get(name) == "1" else "0") if kind == "bool" else form.get(name, loaded[name])
                loaded[name] = form.get(f"was_{name}", loaded[name])
        for name, _group, _key, kind in STREAM_SETTINGS_FIELDS:
            if kind == "bool":
                page = page_rendering.set_attribute(page, name, "checked", "checked" if shown[name] == "1" else None)
            elif name not in {"eas_format", "stream_fallback_action"}:
                page = page_rendering.set_attribute(page, name, "value", shown[name])
        for value in ("wav", "mp3"):
            page = page_rendering.set_attribute(page, f"eas_format_{value}", "checked", "checked" if shown["eas_format"] == value else None)
        for value in STREAM_FALLBACK_ACTIONS:
            page = page_rendering.set_attribute(
                page, f"stream_fallback_action_{value}", "checked", "checked" if shown["stream_fallback_action"] == value else None
            )
        # EAS recording can't be turned off when it's the stream's only output, as the page script shows.
        eas = groups["eas_recording"]
        page = page_rendering.set_hidden(page, "eas_enabled_label", bool(eas["enabled"]) and stream_output_count(stream) <= 1)
        storage_critical = (status.get("storage") or {}).get("status") == "critical"
        page = page_rendering.set_hidden(page, "eas_storage_blocked_hint", not (storage_critical and eas["enabled"]))
        page = page_rendering.set_hidden(page, "stream_notify_eas_alerts_label", not eas["enabled"])
        page = page_rendering.set_hidden(page, "stream_notify_eas_alerts_hint", not eas["enabled"])
        try:
            widest = int(notch_max_width(float(shown["audio_notch_frequency"])))
        except ValueError:
            widest = 2000
        page = page_rendering.set_text(page, "audio_notch_width_max", str(widest))
        # Outputs are listed, each linking to its own page.
        outputs = [output for output in stream.get("outputs") or [] if isinstance(output, dict)]
        soundcards = list(self.service.devices().get("soundcards") or [])
        page = page_rendering.set_hidden(page, "soundcard_outputs_panel", False)
        page = page_rendering.set_attribute(page, "open_add_output_link", "href", page_route("add_stream", stream=stream_id))
        for kind in ("icecast", "soundcard"):
            rows = "".join(
                stream_output_row_html(stream, output, status, soundcards)
                for output in outputs
                if str(output.get("type") or "icecast") == kind
            )
            if rows:
                page = page_rendering.set_content(page, f"{kind}-outputs-body", rows)
        page = page_rendering.append_content(
            page,
            "stream_settings_form",
            '<input type="hidden" name="form" value="stream_settings">'
            + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
        )
        tab_title = f"{STREAM_SETTINGS_TABS[chosen]} - " if chosen else ""
        return page, f"{tab_title}Edit stream {callsign}"

    def _handle_stream_form(self, path: str) -> None:
        """Starts, stops, removes and changes streams, for browsers without the page script."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            stream_id = form.get("stream", "")
            if path == "/streams/enabled":
                enabled = form.get("enabled") == "1"
                self.service.update_stream({"stream_id": stream_id, "enabled": enabled})
                self._send_redirect(f"/?view=streams&result={'started' if enabled else 'stopped'}")
                return
            if path == "/streams/remove":
                self.service.remove_stream(stream_id)
                self._send_redirect("/?view=streams&result=removed")
                return
            stream = next((item for item in self.service.streams if item.get("id") == stream_id), None)
            if stream is None:
                raise ValueError("Stream was not found.")
            changes = stream_settings_changes(form, stream)
            # Its own settings first, so turning the stream on uses them.
            for group, method, key in (
                ("audio", self.service.update_audio_effects, "audio"),
                ("notifications", self.service.update_stream_notifications, "notifications"),
                ("fallback_action", self.service.update_stream_fallback_action, "fallback_action"),
                ("eas_recording", self.service.update_eas_recording, "eas_recording"),
            ):
                if group in changes:
                    method({"stream_id": stream_id, key: changes[group]})
            if "enabled" in changes:
                self.service.update_stream({"stream_id": stream_id, "enabled": changes["enabled"]["enabled"]})
        except Exception as exc:
            message = form_message(str(exc))
            if path == "/streams/settings" and any(stream.get("id") == form.get("stream") for stream in self.service.streams):
                query = {"view": "stream_settings", "stream": form.get("stream", ""), "tab": form.get("tab", "")}
                result = ("stream-settings-save-result", message, "error")
            else:
                query, result = {"view": "streams"}, ("streams-result", message, "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("stream_settings", stream=stream_id, tab=form.get("tab", ""), result="saved"))

    def _stream_output(self, stream_id: str, output_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        stream = next((item for item in self.service.streams if item.get("id") == stream_id), None)
        output = next((item for item in (stream or {}).get("outputs") or [] if item.get("id") == output_id), None)
        return (stream, output) if stream and output else None

    def _fill_stream_output_view(self, page: str, query: dict[str, str], form: dict[str, str]) -> tuple[str, str]:
        stream, output = self._stream_output(query.get("stream", ""), query.get("output", ""))
        stream_id, output_id = str(stream.get("id")), str(output.get("id"))
        settings_link = page_route("stream_settings", stream=stream_id)
        sent = form.get("form") == "output"
        soundcards = list(self.service.devices().get("soundcards") or [])
        for element_id in ("settings_icecast_service_stream", "settings_icecast_form_stream", "settings_soundcard_form_stream"):
            page = page_rendering.set_attribute(page, element_id, "value", stream_id)
        for element_id in ("settings_icecast_service_output", "settings_icecast_form_output", "settings_soundcard_form_output"):
            page = page_rendering.set_attribute(page, element_id, "value", output_id)
        for element_id in ("cancel_output_form_link", "cancel_soundcard_output_form_link"):
            page = page_rendering.set_attribute(page, element_id, "href", settings_link)
        enabled = output.get("enabled") is not False
        if output.get("type") == "soundcard":
            heading = "Edit sound card output"
            soundcard = output.get("soundcard") or {}
            loaded = {
                "stable_id": str(soundcard.get("stable_id") or ""),
                "channel_mode": str(soundcard.get("channel_mode") or "both"),
                "volume": f"{float(soundcard.get('volume', 1.0)):g}",
            }
            values = dict(loaded)
            if sent:
                # Shown again after a mistake, the page keeps what was entered and what it first had.
                values.update(stable_id=form.get("stable_id", ""), channel_mode=form.get("settings_soundcard_channel", ""), volume=form.get("volume", ""))
                loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
            page = page_rendering.set_hidden(page, "icecast_output_edit_panel", True)
            page = page_rendering.set_hidden(page, "soundcard_output_edit_panel", False)
            options, chosen = soundcard_device_options_html(self.service.streams, soundcards, values["stable_id"], output_id, True)
            page = page_rendering.set_content(page, "settings_soundcard_device", options)
            connected = any(device.get("stable_id") == values["stable_id"] for device in soundcards)
            hint = (
                "The configured sound card is not connected." if values["stable_id"] and not connected
                else "NWR Stream Manager will open the hardware device directly." if chosen
                else "No sound cards have available channels."
            )
            page = page_rendering.set_text(page, "settings_soundcard_device_hint", hint)
            page = fill_soundcard_channels(page, "settings", available_soundcard_channels(self.service.streams, chosen, output_id), values["channel_mode"])
            page = page_rendering.set_attribute(page, "settings_soundcard_volume", "value", values["volume"])
            page = page_rendering.set_attribute(page, "refresh_settings_soundcards", "href", page_route("stream_output", stream=stream_id, output=output_id))
            page = page_rendering.append_content(
                page,
                "soundcard_output_edit_panel",
                '<input type="hidden" name="form" value="output">'
                + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
            )
            device = next((item for item in soundcards if item.get("stable_id") == soundcard.get("stable_id")), None)
            usb = bool(device and device.get("bus") == "usb")
            label = soundcard_label(soundcard, soundcards)
        else:
            heading = "Edit output"
            icecast = output.get("icecast") or {}
            current_service = icecast_service(icecast)
            loaded = icecast_form_values(icecast)
            service = form.get("service") if sent else query.get("service") or current_service
            if service not in STREAM_SERVICE_NAMES:
                service = current_service
            if sent:
                values = {name: form.get(name, "") for name in ICECAST_FORM_FIELDS}
                values["format"] = form.get("settings_icecast_format", loaded["format"])
                loaded = {name: form.get(f"was_{name}", value) for name, value in loaded.items()}
            elif service == current_service:
                values = dict(loaded)
            else:
                # Another service starts from its own settings, as the page script's does.
                values = {"format": "mp3", "sample_rate": "24000", "bitrate": "64", "alt_number": "1"}
            page = fill_icecast_fields(page, "settings_icecast", values, service, stream.get("station"))
            page = page_rendering.set_attribute(page, "settings_icecast_form_service", "value", service)
            page = page_rendering.set_hidden(page, "save_output_settings", False)
            page = page_rendering.append_content(
                page,
                "settings_icecast_form",
                '<input type="hidden" name="form" value="output">'
                + f'<input type="hidden" name="was_service" value="{html.escape(form.get("was_service", current_service) if sent else current_service)}">'
                + "".join(f'<input type="hidden" name="was_{name}" value="{html.escape(value)}">' for name, value in loaded.items()),
            )
            usb = False
            label = f"{icecast.get('host')}:{icecast.get('port')}{icecast.get('mount')}"
        page = page_rendering.set_text(page, "output_form_title", heading)
        title = f"{heading} {label} - Edit stream {stream_label(stream)}"
        token_input = page_rendering.form_token_input(self._form_token())
        hidden = f'{token_input}<input type="hidden" name="stream" value="{html.escape(stream_id)}"><input type="hidden" name="output" value="{html.escape(output_id)}">'
        others_enabled = stream_output_count(stream) - (1 if enabled else 0) > 0
        if query.get("confirm") == "remove" and (not enabled or others_enabled):
            content = (
                f"<h3>Remove output {html.escape(label)}?</h3>"
                "<p>The stream stops sending audio to it, and its settings are deleted.</p>"
                f'<form method="post" action="/streams/outputs/remove">{hidden}'
                '<div class="actions"><button type="submit" class="primary" autofocus>Remove output</button>'
                f'<a class="button-link" href="{html.escape(page_route("stream_output", stream=stream_id, output=output_id))}">Cancel</a></div></form>'
            )
            page = page_rendering.set_content(page, "output_confirm", content)
            page = page_rendering.set_hidden(page, "output_confirm", False)
            page = page_rendering.set_hidden(page, "icecast_output_edit_panel", True)
            page = page_rendering.set_hidden(page, "soundcard_output_edit_panel", True)
            return page, f"Remove output {label}? - {title}"
        # The output's other actions, which the page script has in its row's menu.
        actions = []
        if not enabled or others_enabled:
            verb = "Disable" if enabled else "Enable"
            actions.append(
                f'<form method="post" action="/streams/outputs/enabled">{hidden}'
                f'<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">'
                f'<button type="submit">{verb} output</button></form>'
            )
        if usb:
            actions.append(f'<form method="post" action="/streams/outputs/reset-soundcard">{hidden}<button type="submit">Reset USB sound card</button></form>')
        if not enabled or others_enabled:
            remove = page_route("stream_output", stream=stream_id, output=output_id, confirm="remove")
            actions.append(f'<a class="button-link" href="{html.escape(remove)}">Remove output</a>')
        else:
            actions.append('<p class="hint">This is the stream\'s only enabled output, so it can\'t be disabled or removed.</p>')
        page = page_rendering.set_content(page, "output_actions", f'<h3>Other actions</h3><div class="row-actions">{"".join(actions)}</div>')
        page = page_rendering.set_hidden(page, "output_actions", False)
        return page, title

    def _handle_output_form(self, path: str) -> None:
        """Changes, enables, resets and removes a stream's outputs, for browsers without the page script."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            stream_id, output_id = form.get("stream", ""), form.get("output", "")
            found = self._stream_output(stream_id, output_id)
            if found is None:
                raise ValueError("Stream output was not found.")
            stream, output = found
            current = {
                "stream_id": stream_id,
                "output_id": output_id,
                "enabled": output.get("enabled") is not False,
                "type": output.get("type") or "icecast",
                "icecast": output.get("icecast"),
                "soundcard": output.get("soundcard"),
            }
            if path == "/streams/outputs/remove":
                self.service.remove_stream_output(stream_id, output_id)
                self._send_redirect(page_route("stream_settings", stream=stream_id, result="output-removed"))
                return
            if path == "/streams/outputs/reset-soundcard":
                reset = self.service.reset_soundcard_device(str((output.get("soundcard") or {}).get("stable_id") or ""))
                result = ("output-list-result", str(reset.get("message") or "Sound card reset finished."), "success" if reset.get("success") else "error")
                self._send_page(self._render_index_page({"view": "stream_settings", "stream": stream_id}, result=result))
                return
            if path == "/streams/outputs/enabled":
                response = self.service.update_stream_output({**current, "enabled": form.get("enabled") == "1"})
            elif current["type"] == "soundcard":
                soundcard = dict(output.get("soundcard") or {})
                for name, field in (("stable_id", "stable_id"), ("channel_mode", "settings_soundcard_channel"), ("volume", "volume")):
                    if form.get(field, "") != form.get(f"was_{name}", ""):
                        soundcard[name] = form.get(field, "")
                try:
                    soundcard["volume"] = float(soundcard.get("volume", 1.0))
                except ValueError:
                    raise ValueError("Enter a volume from 0 to 2.") from None
                response = self.service.update_stream_output({**current, "soundcard": soundcard})
            else:
                # A changed service starts over with that service's settings; otherwise
                # only what was changed here is taken from the form.
                service = form.get("service", "")
                values = icecast_form_values(output.get("icecast") or {})
                form_values = {name: form.get(name, "") for name in ICECAST_FORM_FIELDS}
                form_values["format"] = form.get("settings_icecast_format", "mp3")
                if service != form.get("was_service"):
                    values = form_values
                else:
                    for name in ICECAST_FORM_FIELDS:
                        if form_values[name] != form.get(f"was_{name}", ""):
                            values[name] = form_values[name]
                icecast = apply_icecast_service_preset(icecast_payload(values), service, stream.get("station"))
                response = self.service.update_stream_output({**current, "icecast": icecast})
                if response.get("success") and service == STREAM_SERVICE_NWRORG and form.get("was_service") != STREAM_SERVICE_NWRORG:
                    self._send_page(render_notice_page(
                        "NOAA Weather Radio Org", NWRORG_SUBMISSION_HTML,
                        {"view": "stream_settings", "stream": stream_id, "result": "output-saved"},
                    ))
                    return
            if not response.get("success"):
                raise ValueError(str(response.get("message") or "The output could not be saved."))
        except Exception as exc:
            query = {"view": "stream_output", "stream": form.get("stream", ""), "output": form.get("output", "")}
            result = ("output-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form if path == "/streams/outputs/save" else None), HTTPStatus.BAD_REQUEST)
            return
        self._send_redirect(page_route("stream_settings", stream=stream_id, result="output-saved"))

    def _wizard_state(self, form: dict[str, str], adding_stream: bool) -> dict[str, str]:
        first = "station" if adding_stream else "type"
        step = form.get("step") if form.get("step") in WIZARD_STEPS else first
        return {
            "step": first if step == "station" and not adding_stream else step,
            "q": form.get("q", "").strip(),
            "station_key": form.get("station_key", ""),
            "output_type": "soundcard" if form.get("wizard_output_type") == "soundcard" else "icecast",
            "service": form.get("service") if form.get("service") in STREAM_SERVICE_NAMES else STREAM_SERVICE_CUSTOM,
            "host": form.get("host", ""),
            "port": form.get("port", ""),
            "username": form.get("username", ""),
            "password": form.get("password", ""),
            "mount": form.get("mount", ""),
            "format": "ogg" if form.get("icecast_format") == "ogg" else "mp3",
            "sample_rate": form.get("sample_rate", "24000"),
            "bitrate": form.get("bitrate", "64"),
            "alt_enabled": "1" if form.get("alt_enabled") == "1" else "",
            "alt_number": form.get("alt_number", "1"),
            "stable_id": form.get("stable_id", ""),
            "channel_mode": form.get("wizard_soundcard_channel", "both"),
            "volume": form.get("volume", "1"),
            "output_id": form.get("output_id", ""),
            "preview_id": form.get("preview_id", ""),
        }

    def _wizard_station(self, key: str) -> dict[str, Any] | None:
        return next((station for station in self.service.stations if station.get("key") == key), None) if key else None

    def _fill_wizard_view(self, page: str, query: dict[str, str], form: dict[str, str]) -> tuple[str, str]:
        """Add Stream and Add Output, one step at a time; every step's fields go with the form."""
        stream = next((item for item in self.service.streams if item.get("id") == query.get("stream")), None)
        state = self._wizard_state(form if form.get("form") == "wizard" else {}, stream is None)
        step = state["step"]
        if stream is not None:
            station = stream.get("station")
            heading = f"Add output for {(station or {}).get('callsign') or 'stream'}"
        else:
            station = self._wizard_station(state["station_key"])
            heading = "Add Stream"
        page = page_rendering.set_text(page, "stream_wizard_title", heading)
        page = page_rendering.set_attribute(page, "wizard_form_stream", "value", str(stream.get("id")) if stream else "")
        page = page_rendering.set_attribute(page, "wizard_form_step", "value", step)
        page = page_rendering.set_attribute(page, "wizard_form_output", "value", state["output_id"])
        page = page_rendering.set_attribute(page, "wizard_form_preview", "value", state["preview_id"])
        page = page_rendering.append_content(page, "stream_wizard_form", '<input type="hidden" name="form" value="wizard">')
        for name, panel in WIZARD_STEP_PANELS.items():
            page = page_rendering.set_hidden(page, panel, name != step)
        # The station: found by searching on its own step, and kept for the others.
        page = page_rendering.set_attribute(page, "station_search", "value", state["q"])
        if step == "station":
            page = page_rendering.set_content(page, "wizard_station_actions", '<input type="submit" name="wizard_action" value="Search" data-baseline-submit>')
            results = self.service.search_stations(state["q"], 75)["stations"] if state["q"] else []
        else:
            results = [station] if station and stream is None else []
        if state["q"] or results:
            options = "".join(
                f'<option value="{html.escape(str(item.get("key")))}"{" selected" if item.get("key") == state["station_key"] else ""}>'
                f"{html.escape(station_choice_label(item))}</option>"
                for item in results
            ) or '<option value="">No stations found</option>'
            page = page_rendering.set_content(page, "station_results", options)
            page = page_rendering.set_hidden(page, "station_results_label", False)
        if station and stream is None:
            page = page_rendering.set_text(page, "selected_station", f"Selected {station_choice_label(station)}")
        elif state["q"]:
            page = page_rendering.set_text(page, "selected_station", "Select a station to continue.")
        for value in ("icecast", "soundcard"):
            page = page_rendering.set_attribute(page, f"wizard_output_type_{value}", "checked", "checked" if state["output_type"] == value else None)
        page = fill_icecast_fields(page, "icecast", state, state["service"], station)
        options, chosen = soundcard_device_options_html(self.service.streams, list(self.service.devices().get("soundcards") or []), state["stable_id"], state["output_id"], False)
        page = page_rendering.set_content(page, "wizard_soundcard_device", options)
        page = page_rendering.set_text(page, "wizard_soundcard_device_hint", "NWR Stream Manager will open the hardware device directly." if chosen else "No sound cards have available channels.")
        page = fill_soundcard_channels(page, "wizard", available_soundcard_channels(self.service.streams, chosen, state["output_id"]), state["channel_mode"])
        page = page_rendering.set_attribute(page, "wizard_soundcard_volume", "value", state["volume"])
        if state["preview_id"]:
            minutes = int(SOUNDCARD_PREVIEW_LEASE_SECONDS // 60)
            page = page_rendering.set_text(
                page,
                "wizard_soundcard_status",
                f"The sound card is playing the station so you can check it. The preview stops {minutes} minutes after your last change; select Try these settings to keep it going.",
            )
        # The buttons for this step, going forward first so Enter goes forward.
        last = step in ("quality", "controls") or (step == "credentials" and state["service"] == STREAM_SERVICE_NWRORG)
        buttons = [("Finish" if last else "Next", step == "device" and not chosen)]
        if step not in ("station", "type") or (step == "type" and stream is None):
            buttons.append(("Back", False))
        buttons.append(("Cancel", False))
        page = page_rendering.set_content(
            page,
            "wizard_baseline_actions",
            "".join(f'<input type="submit" name="wizard_action" value="{label}"{" disabled" if disabled else ""} data-baseline-submit>' for label, disabled in buttons),
        )
        if step == "device":
            page = page_rendering.set_content(page, "wizard_soundcard_device_actions", '<input type="submit" name="wizard_action" value="Refresh sound card list" data-baseline-submit>')
        if step == "controls":
            page = page_rendering.set_content(page, "wizard_soundcard_controls_actions", '<input type="submit" name="wizard_action" value="Try these settings" data-baseline-submit>')
        return page, heading

    def _handle_wizard_form(self) -> None:
        """Adds a stream, or an output to one, a step at a time, for browsers without the page script."""
        form: dict[str, str] = {}
        state: dict[str, str] = {}
        message: tuple[str, str] | None = None
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            stream_id = form.get("stream", "")
            stream = next((item for item in self.service.streams if item.get("id") == stream_id), None) if stream_id else None
            if stream_id and stream is None:
                raise ValueError("Stream was not found.")
            adding_stream = stream is None
            done_link = page_route("streams") if adding_stream else page_route("stream_settings", stream=stream_id)
            state = self._wizard_state(form, adding_stream)
            step, action, service = state["step"], form.get("wizard_action", ""), state["service"]
            station = self._wizard_station(state["station_key"]) if adding_stream else stream.get("station")
            try:
                volume = float(state["volume"])
            except ValueError:
                volume = 1.0
            soundcard = {"stable_id": state["stable_id"], "channel_mode": state["channel_mode"], "volume": volume, "sample_rate": 48000}
            fixed = service in (STREAM_SERVICE_GWES, STREAM_SERVICE_NWRORG)

            def try_soundcard() -> dict[str, Any]:
                # A new stream's sound card plays a preview; an existing stream's gets its output.
                if adding_stream:
                    response = self.service.upsert_soundcard_preview_stream(
                        {"preview_id": state["preview_id"], "station_key": state["station_key"], "soundcard": soundcard, "lease": True}
                    )
                    state["preview_id"] = str(response.get("preview_id") or "")
                elif state["output_id"]:
                    response = self.service.update_stream_output(
                        {"stream_id": stream_id, "output_id": state["output_id"], "type": "soundcard", "enabled": True, "soundcard": soundcard}
                    )
                else:
                    response = self.service.add_stream_output({"stream_id": stream_id, "type": "soundcard", "soundcard": soundcard})
                    state["output_id"] = str(response.get("output_id") or "")
                if not response.get("success"):
                    raise ValueError(str(response.get("message")))
                return response

            def stop_soundcard() -> None:
                if state["preview_id"]:
                    self.service.discard_soundcard_preview_stream(state["preview_id"])
                if state["output_id"]:
                    self.service.remove_stream_output(stream_id, state["output_id"])
                state["preview_id"] = state["output_id"] = ""

            if action == "Cancel":
                stop_soundcard()
                self._send_redirect(done_link)
                return
            if action == "Back" and step == "device":
                # The sound card tried on this step stops, as another type may be chosen.
                stop_soundcard()
                state["step"] = "type"
            elif action == "Back":
                state["step"] = {
                    "type": "station", "service": "type", "codec": "service", "controls": "device", "quality": "credentials",
                    "credentials": "service" if fixed else "codec",
                }.get(step, "type")
            elif action in ("Search", "Refresh sound card list"):
                pass
            elif action == "Try these settings" and step == "controls":
                try_soundcard()
                message = ("Soundcard preview updated." if adding_stream else "Soundcard output updated.", "success")
            elif step == "station":
                if station is None:
                    raise ValueError("Select a station to continue.")
                state["step"] = "type"
            elif step == "type":
                state["step"] = "device" if state["output_type"] == "soundcard" else "service"
            elif step == "service":
                state["step"] = "credentials" if fixed else "codec"
            elif step == "codec":
                if service == STREAM_SERVICE_WEATHERUSA and not state["mount"].strip():
                    state["mount"] = weatherusa_mount(station, state["format"])
                state["step"] = "credentials"
            elif step == "device":
                response = try_soundcard()
                message = (str(response.get("message") or "Soundcard preview started."), "success")
                state["step"] = "controls"
            elif step == "controls":
                if adding_stream:
                    response = self.service.add_stream(
                        {"station_key": state["station_key"], "type": "soundcard", "preview_id": state["preview_id"], "soundcard": soundcard}
                    )
                else:
                    response = try_soundcard()
                if not response.get("success"):
                    raise ValueError(str(response.get("message")))
                self._send_redirect(page_route("streams", result="created") if adding_stream else page_route("stream_settings", stream=stream_id, result="soundcard-added"))
                return
            else:
                icecast = apply_icecast_service_preset(icecast_payload(state), service, station)
                if step == "credentials" and service != STREAM_SERVICE_NWRORG:
                    # Tested before going on, as the page script does.
                    response = self.service.test_icecast_auth_payload({"icecast": icecast})
                    if not response.get("success"):
                        raise ValueError(str(response.get("message")))
                    message = (str(response.get("message")), "success")
                    state["step"] = "quality"
                else:
                    if adding_stream:
                        response = self.service.add_stream({"station_key": state["station_key"], "type": "icecast", "icecast": icecast})
                        done = {"view": "streams", "result": "created"}
                    else:
                        response = self.service.add_stream_output({"stream_id": stream_id, "icecast": icecast})
                        done = {"view": "stream_settings", "stream": stream_id, "result": "icecast-added"}
                    if not response.get("success"):
                        raise ValueError(str(response.get("message")))
                    if service == STREAM_SERVICE_NWRORG:
                        self._send_page(render_notice_page("NOAA Weather Radio Org", NWRORG_SUBMISSION_HTML, done))
                        return
                    self._send_redirect(page_route(**done))
                    return
            fields = {**form, **{name: state[name] for name in ("step", "output_id", "preview_id", "mount")}}
            result = ("stream-result", message[0], message[1]) if message else None
            self._send_page(self._render_index_page({"view": "add_stream", "stream": stream_id}, result=result, form=fields))
        except Exception as exc:
            # What was tried before the mistake is kept track of.
            fields = {**form, **{name: state[name] for name in ("output_id", "preview_id") if name in state}}
            result = ("stream-result", form_message(str(exc)), "error")
            query = {"view": "add_stream", "stream": form.get("stream", "")}
            self._send_page(self._render_index_page(query, result=result, form=fields), HTTPStatus.BAD_REQUEST)
