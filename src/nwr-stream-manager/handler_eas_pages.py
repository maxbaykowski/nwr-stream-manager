"""EAS alert pages for browsers without the page script: the list, an alert, exports and removing alerts."""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any
from urllib.parse import parse_qs

from . import page_rendering
from .baseline_pages import (
    eas_alert_stream_label,
    EAS_ALERTS_PER_PAGE,
    eas_bulk_options_html,
    eas_range_control_html,
    eas_range_datetime,
    eas_range_values,
    FORM_EXPIRED_MESSAGE,
    form_message,
    page_route,
)
from .eas_alerts import local_datetime_parts


class EasAlertPageHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _fill_eas_alert_view(
        self,
        page: str,
        view: str,
        query: dict[str, str],
        form: dict[str, str],
        streams: list[dict[str, Any]],
        read_only: bool,
        title: str,
    ) -> tuple[str, str]:
        stream = next((item for item in streams if item.get("id") == query.get("stream")), streams[0] if streams else None)
        stream_id = str(stream.get("id", "")) if stream else ""
        try:
            list_page = max(1, int(query.get("page") or 1))
        except ValueError:
            list_page = 1
        options = "".join(
            f'<option value="{html.escape(str(item.get("id", "")))}"{" selected" if item is stream else ""}>'
            f"{html.escape(eas_alert_stream_label(item))}</option>"
            for item in streams
        )
        page = page_rendering.set_content(page, "eas_alert_stream", options)
        list_link = page_route("eas_alerts", stream=stream_id, page=list_page)
        if view == "eas_alerts":
            return self._fill_eas_alert_list(page, stream_id, list_page, read_only), title
        if stream is None:
            return page, title
        if view in {"eas_alert_export", "eas_alert_delete"}:
            kind = "export" if view == "eas_alert_export" else "delete"
            bulk = self.service.eas_alert_bulk_options(stream_id)
            presets = bulk.get(f"{kind}_presets") or []
            page = page_rendering.set_text(page, f"eas_{kind}_stream_label", f"Stream: {eas_alert_stream_label(stream)}")
            page = page_rendering.set_attribute(page, f"eas_{kind}_stream", "value", stream_id)
            page = page_rendering.set_attribute(page, f"eas_{kind}_page", "value", str(list_page))
            page = page_rendering.set_content(
                page, f"eas_{kind}_options", eas_bulk_options_html(kind, presets, form.get(f"eas_{kind}_mode", ""))
            )
            # The page script shows the date range only once it's chosen.
            now = bulk.get("now") or local_datetime_parts(datetime.now().astimezone())
            start_default = local_datetime_parts(
                datetime(now["year"], now["month"], now["day"], now["hour"], now["minute"]) - timedelta(hours=24)
            )
            end = now if form.get(f"eas_{kind}_now") else None
            manual = eas_range_control_html(
                kind, "start", "Start", eas_range_values(form, kind, "start", start_default), int(now["year"])
            ) + eas_range_control_html(
                kind, "end", "End", eas_range_values({} if end else form, kind, "end", now), int(now["year"])
            )
            page = page_rendering.set_content(page, f"eas_{kind}_manual", manual)
            page = page_rendering.set_hidden(page, f"eas_{kind}_manual", False)
            page = page_rendering.set_attribute(page, f"cancel_eas_{kind}_link", "href", list_link)
            if not int(bulk.get("total") or 0):
                page = page_rendering.set_attribute(page, f"eas_{kind}_alerts", "disabled", "disabled")
            return page, title
        return self._fill_eas_alert_detail(page, stream_id, query, list_page, list_link, read_only, title)

    def _fill_eas_alert_list(self, page: str, stream_id: str, list_page: int, read_only: bool) -> str:
        if not stream_id:
            empty = '<div class="hint" data-row-key="empty">No streams record EAS alerts.</div>'
            return page_rendering.set_content(page, "eas-alert-list", empty)
        data = self.service.eas_alerts(stream_id, list_page, EAS_ALERTS_PER_PAGE)
        list_page, total_pages = int(data["page"]), int(data["total_pages"])
        rows = [
            f'<a class="stream-item" data-row-key="alert:{html.escape(alert["id"])}" data-alert-id="{html.escape(alert["id"])}" '
            f'href="{html.escape(page_route("eas_alert_detail", stream=stream_id, alert=alert["id"], page=list_page))}">'
            f'{html.escape(alert.get("summary") or "Unknown EAS alert")}</a>'
            for alert in data["alerts"]
        ]
        empty = '<div class="hint" data-row-key="empty">No EAS alerts recorded for this stream.</div>'
        page = page_rendering.set_content(page, "eas-alert-list", "".join(rows) or empty)
        page = page_rendering.set_text(page, "eas_alert_page", f"Page {list_page} of {total_pages}")
        for name, target, available in (("prev", list_page - 1, list_page > 1), ("next", list_page + 1, list_page < total_pages)):
            page = page_rendering.set_attribute(page, f"eas_alert_{name}", "disabled", None if available else "disabled")
            page = page_rendering.set_hidden(page, f"eas_alert_page_{name}_link", not available)
            page = page_rendering.set_attribute(
                page, f"eas_alert_page_{name}_link", "href", page_route("eas_alerts", stream=stream_id, page=target)
            )
        for name in ("export", "delete"):
            page = page_rendering.set_attribute(
                page, f"open_eas_{name}_link", "href", page_route(f"eas_alert_{name}", stream=stream_id, page=list_page)
            )
        return page_rendering.set_hidden(page, "open_eas_delete_link", read_only)

    def _fill_eas_alert_detail(
        self,
        page: str,
        stream_id: str,
        query: dict[str, str],
        list_page: int,
        list_link: str,
        read_only: bool,
        title: str,
    ) -> tuple[str, str]:
        alert_id = query.get("alert", "")
        page = page_rendering.set_attribute(page, "back_to_eas_alerts", "href", list_link)
        try:
            alert = self.service.eas_alert_detail(stream_id, alert_id)["alert"]
            neighbors = self.service.eas_alert_neighbors(stream_id, alert_id, EAS_ALERTS_PER_PAGE)
        except ValueError:
            page = page_rendering.set_hidden(page, "eas_alert_player", True)
            page = page_rendering.set_hidden(page, "eas_alert_download", True)
            page = page_rendering.set_hidden(page, "remove_eas_alert_link", True)
            message = "EAS alert was not found."
            page = page_rendering.set_text(page, "eas-alert-detail-result", message)
            page = page_rendering.set_attribute(page, "eas-alert-detail-result", "class", "message error")
            page = page_rendering.set_attribute(page, "eas-alert-detail-result", "role", "alert")
            return page, f"Error: {message} - {title}"
        event = alert.get("event_type") or "EAS alert"
        page = page_rendering.set_text(page, "eas_alert_detail_title", event)
        page = page_rendering.set_text(page, "eas_detail_event", alert.get("event_type") or "Unknown event")
        page = page_rendering.set_text(page, "eas_detail_areas", ", ".join(alert.get("areas") or []) or "Unknown area")
        page = page_rendering.set_text(page, "eas_detail_issued", alert.get("issued_at") or "Unknown")
        page = page_rendering.set_text(page, "eas_detail_expires", alert.get("expires_at") or "Unknown")
        page = page_rendering.set_attribute(page, "eas_alert_baseline_audio", "src", alert.get("audio_url") or "")
        page = page_rendering.set_attribute(page, "eas_alert_baseline_audio", "data-audio-type", alert.get("audio_type") or "")
        page = page_rendering.set_attribute(page, "eas_alert_download", "href", alert.get("download_url") or "#")
        for name in ("previous", "next"):
            target = neighbors.get(name)
            page = page_rendering.set_hidden(page, f"eas_alert_{name}_link", target is None)
            if target:
                link = page_route("eas_alert_detail", stream=stream_id, alert=target["id"], page=target["page"])
                page = page_rendering.set_attribute(page, f"eas_alert_{name}_link", "href", link)
        detail_link = page_route("eas_alert_detail", stream=stream_id, alert=alert_id, page=list_page)
        page = page_rendering.set_hidden(page, "remove_eas_alert_link", read_only)
        page = page_rendering.set_attribute(page, "remove_eas_alert_link", "href", f"{detail_link}&confirm=remove")
        title = f"Details for alert {event} issued on {alert.get('issued_at') or 'an unknown time'}"
        if query.get("confirm") == "remove" and not read_only:
            question = "Remove this EAS alert and its audio file?"
            content = (
                f"<h3>{question}</h3>"
                f'<form method="post" action="/eas-alerts/remove">{page_rendering.form_token_input(self._form_token())}'
                f'<input type="hidden" name="stream" value="{html.escape(stream_id)}">'
                f'<input type="hidden" name="alert" value="{html.escape(alert_id)}">'
                f'<input type="hidden" name="page" value="{list_page}">'
                f'<div class="actions"><button type="submit" class="primary" autofocus>Remove alert</button>'
                f'<a class="button-link" href="{html.escape(detail_link)}">Cancel</a></div></form>'
            )
            page = page_rendering.set_content(page, "eas_alert_remove_confirm", content)
            page = page_rendering.set_hidden(page, "eas_alert_remove_confirm", False)
            title = f"{question} - {title}"
        return page, title

    def _handle_eas_alert_export(self, parsed) -> None:
        """Exports alerts for browsers without the page script, which download them from a form."""
        query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        query["view"] = "eas_alert_export"
        if not query.get("eas_export_now"):
            try:
                zip_path, download_name = self.service.eas_alert_export_zip(*self._eas_alert_range(query, "export"))
            except Exception as exc:
                result = ("eas-export-result", form_message(str(exc)), "error")
                self._send_page(self._render_index_page(query, result=result, form=query), HTTPStatus.BAD_REQUEST)
                return
            self._send_file(zip_path, download_name, True, delete_after=True)
            return
        self._send_page(self._render_index_page(query, form=query))

    def _eas_alert_range(self, form: dict[str, str], kind: str) -> tuple[str, str, str, str]:
        """The stream and range of alerts a form chose, as the page script sends them."""
        mode = form.get(f"eas_{kind}_mode", "")
        if mode != "manual":
            return form.get("stream", ""), mode, "", ""
        now = local_datetime_parts(datetime.now().astimezone())
        start = eas_range_datetime(eas_range_values(form, kind, "start", now))
        end = eas_range_datetime(eas_range_values(form, kind, "end", now))
        return form.get("stream", ""), mode, start, end

    def _handle_eas_alert_form(self, path: str) -> None:
        """Deletes alerts for browsers without the page script."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            list_query = {"view": "eas_alerts", "stream": form.get("stream", ""), "page": form.get("page", "1")}
            if path == "/eas-alerts/remove":
                self.service.remove_eas_alert(form.get("stream", ""), form.get("alert", ""))
                self._send_page(self._render_index_page(list_query, result=("eas-alert-result", "Alert removed.", "success")))
                return
            delete_query = {"view": "eas_alert_delete", "stream": form.get("stream", ""), "page": form.get("page", "1")}
            if form.get("eas_delete_now"):
                self._send_page(self._render_index_page(delete_query, form=form))
                return
            stream_id, mode, start, end = self._eas_alert_range(form, "delete")
            if form.get("confirmed") != "1":
                count = int(self.service.eas_alert_range_count(stream_id, mode, start, end).get("count") or 0)
                if not count:
                    raise ValueError("No alerts were issued during this time.")
                self._send_page(self._render_eas_delete_confirmation(delete_query, form, count))
                return
            removed = int(self.service.remove_eas_alert_range(stream_id, mode, start, end).get("count") or 0)
        except Exception as exc:
            if path == "/eas-alerts/remove":
                query = {"view": "eas_alert_detail", "stream": form.get("stream", ""), "alert": form.get("alert", ""), "page": form.get("page", "1")}
                result = ("eas-alert-detail-result", form_message(str(exc)), "error")
            else:
                query = {"view": "eas_alert_delete", "stream": form.get("stream", ""), "page": form.get("page", "1")}
                result = ("eas-delete-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        message = f"{removed} alert{'' if removed == 1 else 's'} deleted."
        self._send_page(self._render_index_page(list_query, result=("eas-alert-result", message, "success")))

    def _render_eas_delete_confirmation(self, query: dict[str, str], form: dict[str, str], count: int) -> str:
        """Asks before deleting alerts; the page script asks in a dialog."""
        page = self._render_index_page(query, form=form)
        question = f"Delete {count} alert{'' if count == 1 else 's'}?"
        kept = "".join(
            f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(value)}">'
            for name, value in form.items()
            if name.startswith("eas_delete_") and name != "eas_delete_now" or name in {"stream", "page"}
        )
        list_link = page_route("eas_alerts", stream=query.get("stream", ""), page=query.get("page", "1"))
        content = (
            f"<h3>{html.escape(question)}</h3>"
            f"<p>{count} alert{'' if count == 1 else 's'} and their audio files will be permanently deleted.</p>"
            f'<form method="post" action="/eas-alerts/delete">{page_rendering.form_token_input(self._form_token())}{kept}'
            f'<input type="hidden" name="confirmed" value="1">'
            f'<div class="actions"><button type="submit" class="primary" autofocus>Delete alerts</button>'
            f'<a class="button-link" href="{html.escape(list_link)}">Cancel</a></div></form>'
        )
        page = page_rendering.set_content(page, "eas_delete_confirm", content)
        page = page_rendering.set_hidden(page, "eas_delete_confirm", False)
        page = page_rendering.set_hidden(page, "eas_delete_form", True)
        title = re.search(r"<title>(.*?)</title>", page).group(1)
        return page_rendering.set_title(page, f"{question} - {html.unescape(title)}")
