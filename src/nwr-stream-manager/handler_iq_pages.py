"""I/Q recorder pages for browsers without the page script, and downloading recordings."""

from __future__ import annotations

import html
import logging
import re
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import page_rendering
from .accounts import AccountRecord
from .baseline_pages import FORM_EXPIRED_MESSAGE, form_message, format_decimal_bytes, iq_recorder_source_text
from .constants import IQ_RECORDER_MODE_SPECTRUM, IQ_RECORDER_SAMPLE_RATES
from .iq_recorder import convert_iq_recording_chunks, format_duration_hms
from .page_sections import iq_recording_payload_from_form, iq_recording_row_html
from .state_files import http_header_filename


LOG = logging.getLogger(__name__)


class IqRecorderPageHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _send_iq_recording(
        self,
        path: Path,
        download_name: str,
        data_length: int,
        fmt: str,
        recording_id: str,
        account_id: int | None = None,
    ) -> None:
        download_id = self.service.begin_iq_recording_download(recording_id, account_id)
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(data_length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'attachment; filename="{http_header_filename(download_name)}"')
            self._send_pending_auth_session_cookie()
            self.end_headers()
            with path.open("rb") as source:
                for chunk in convert_iq_recording_chunks(source, fmt):
                    if self.service.iq_recording_download_aborted(download_id):
                        LOG.info("I/Q recording download aborted after account revoke: %s", recording_id)
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        LOG.info("I/Q recording download disconnected before completion: %s", recording_id)
                        break
        finally:
            self.service.finish_iq_recording_download(download_id)

    def _fill_iq_recording_banner(self, page: str, recorder: dict[str, Any]) -> str:
        if not recorder.get("active"):
            return page
        page = page_rendering.set_hidden(page, "iq_recording_banner", False)
        # Where the page script can't run, a smaller script keeps the timer going from these.
        elapsed = float(recorder.get("elapsed_seconds") or 0)
        page = page_rendering.set_attribute(page, "iq_recording_banner", "data-elapsed-seconds", str(int(elapsed)))
        page = page_rendering.set_attribute(
            page, "iq_recording_banner", "data-duration-seconds", str(int(recorder.get("duration_seconds") or 0))
        )
        return page_rendering.set_text(page, "iq_recording_banner_elapsed", format_duration_hms(int(elapsed)))

    def _fill_iq_recorder_view(
        self,
        page: str,
        view: str,
        query: dict[str, str],
        form: dict[str, str],
        recorder: dict[str, Any],
        read_only: bool,
        title: str,
    ) -> tuple[str, str]:
        active = bool(recorder.get("active"))
        if view == "iq_recorder_start":
            return self._fill_iq_recorder_start(page, form, active), title
        recordings = self.service.iq_recordings()["recordings"]
        if view == "iq_recording_download":
            recording_id = query.get("recording", "")
            recording = next((item for item in recordings if item.get("id") == recording_id), None)
            page = page_rendering.set_attribute(page, "iq_download_recording_id", "value", recording_id)
            fmt = form.get("format", "cf")
            page = page_rendering.set_content(
                page,
                "iq_download_format",
                "".join(
                    f'<option value="{value}"{" selected" if value == fmt else ""}>{label}</option>'
                    for value, label in (("cf", "Float 32 bit"), ("s16", "Signed 16 bit"), ("u8", "Unsigned 8-bit"))
                ),
            )
            if recording is None:
                page = page_rendering.set_text(page, "iq_download_recording_label", "I/Q recording was not found.")
                return page_rendering.set_attribute(page, "iq_download_recording", "disabled", "disabled"), title
            recorded_at = recording.get("recorded_at") or "Unknown"
            page = page_rendering.set_text(
                page,
                "iq_download_recording_label",
                f"{recorded_at}; {recording.get('sample_rate') or 0} S/s; {recording.get('frequency_hz') or 0} Hz",
            )
            return page, f"Download I/Q recording from {recording.get('recorded_at') or 'unknown time'}"
        rows = "".join(iq_recording_row_html(recording, read_only) for recording in recordings)
        if rows:
            page = page_rendering.set_content(page, "iq-recordings-body", rows)
        # Only one recording runs at a time.
        page = page_rendering.set_hidden(page, "open_iq_start_link", read_only or active)
        page = page_rendering.set_hidden(page, "iq_recorder_active", not active)
        if active:
            remaining = recorder.get("storage_remaining_seconds")
            duration = int(recorder.get("duration_seconds") or 0)
            elapsed = int(recorder.get("elapsed_seconds") or 0)
            storage = self.service.storage_monitor.snapshot().get("filesystems") or []
            summary = storage[0].get("summary") if storage else ""
            for element_id, text in (
                ("iq_status", "Recording" if recorder.get("status") == "recording" else str(recorder.get("status", ""))),
                ("iq_source", iq_recorder_source_text(recorder)),
                ("iq_active_sample_rate", f"{recorder.get('sample_rate') or 0} S/s"),
                ("iq_file_size", format_decimal_bytes(recorder.get("bytes_written") or 0)),
                ("iq_elapsed", format_duration_hms(elapsed)),
                ("iq_remaining", "calculating" if remaining is None else format_duration_hms(int(remaining))),
                ("iq_auto_stop_remaining", format_duration_hms(max(0, duration - elapsed))),
                ("iq_storage", re.sub(r"^Storage: ", "", summary) if summary else "unknown"),
            ):
                page = page_rendering.set_text(page, element_id, text)
            page = page_rendering.set_hidden(page, "iq_auto_stop_metric", duration <= 0)
        # Removing a recording is confirmed first.
        recording = next((item for item in recordings if item.get("id") == query.get("recording")), None)
        if query.get("confirm") != "remove" or recording is None or read_only or recording.get("downloading"):
            return page, title
        recorded_at = recording.get("recorded_at") or "unknown time"
        content = (
            f"<h3>Remove this I/Q recording?</h3>"
            f"<p>The I/Q recording from {html.escape(recorded_at)} will be permanently deleted.</p>"
            f'<form method="post" action="/iq-recordings/remove">{page_rendering.form_token_input(self._form_token())}'
            f'<input type="hidden" name="recording" value="{html.escape(str(recording.get("id", "")))}">'
            f'<div class="actions"><button type="submit" class="primary" autofocus>Remove recording</button>'
            f'<a class="button-link" href="/?view=iq_recorder">Cancel</a></div></form>'
        )
        page = page_rendering.set_content(page, "iq_remove_confirm", content)
        page = page_rendering.set_hidden(page, "iq_remove_confirm", False)
        return page, f"Remove I/Q recording from {recorded_at}? - {title}"

    def _fill_iq_recorder_start(self, page: str, form: dict[str, str], active: bool) -> str:
        # Only one recording runs at a time; the page script hides the form the same way.
        page = page_rendering.set_hidden(page, "iq_recorder_idle", active)
        mode = "spectrum" if form.get("iq_recording_mode") == IQ_RECORDER_MODE_SPECTRUM else "stream"
        page = page_rendering.set_attribute(page, "iq_mode_stream", "checked", "checked" if mode == "stream" else None)
        page = page_rendering.set_attribute(page, "iq_mode_spectrum", "checked", "checked" if mode == "spectrum" else None)
        # Both sets of settings show; a script, where one runs, shows only the chosen one.
        page = page_rendering.set_hidden(page, "iq_spectrum_fields", False)
        streams = [stream for stream in self.service.streams if stream.get("enabled", True) not in (False, "false", 0)]
        options = []
        for stream in streams:
            station = stream.get("station") or {}
            frequency = f" {station['frequency']} MHz" if station.get("frequency") else ""
            selected = " selected" if str(stream.get("id", "")) == form.get("stream_id") else ""
            options.append(
                f'<option value="{html.escape(str(stream.get("id", "")))}"{selected}>'
                f'{html.escape(str(station.get("callsign") or "Unknown") + frequency)}</option>'
            )
        page = page_rendering.set_content(page, "iq_stream_select", "".join(options))
        rate = form.get("sample_rate", str(IQ_RECORDER_SAMPLE_RATES[0]))
        page = page_rendering.set_content(
            page,
            "iq_sample_rate",
            "".join(
                f'<option value="{value}"{" selected" if str(value) == rate else ""}>{value} S/s</option>'
                for value in IQ_RECORDER_SAMPLE_RATES
            ),
        )
        if "duration_minutes" in form:
            page = page_rendering.set_attribute(page, "iq_duration_minutes", "value", form["duration_minutes"])
        return page

    def _handle_iq_recorder_form(self, path: str) -> None:
        """Starts and stops I/Q recording and removes recordings, for browsers without the page script."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            if path == "/iq-recorder/start":
                account = getattr(self, "current_account", None)
                self.service.start_iq_recording(
                    iq_recording_payload_from_form(form), account.id if isinstance(account, AccountRecord) else None
                )
                done = "started"
            elif path == "/iq-recorder/stop":
                self.service.stop_iq_recording()
                done = "stopped"
            else:
                self.service.remove_iq_recording(form.get("recording", ""))
                done = "removed"
        except Exception as exc:
            if path == "/iq-recorder/start":
                query, element_id = {"view": "iq_recorder_start"}, "iq-start-result"
            else:
                query, element_id = {"view": "iq_recorder"}, "iq-recorder-result"
            result = (element_id, form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
            return
        # Sent on to a page of its own, so refreshing it to see the latest doesn't send the form again.
        self._send_redirect(f"/?view=iq_recorder&result={done}")

    def _handle_iq_recording_download(self, parsed) -> None:
        query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        try:
            recording, file_path, download_name, download_size = self.service.iq_recording_download_info(
                query.get("id", ""), query.get("format", "cf")
            )
        except Exception as exc:
            page_query = {"view": "iq_recording_download", "recording": query.get("id", "")}
            result = ("iq-download-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(page_query, result=result, form=query), HTTPStatus.BAD_REQUEST)
            return
        account = getattr(self, "current_account", None)
        self._send_iq_recording(
            file_path,
            download_name,
            download_size,
            query.get("format", "cf"),
            str(recording.get("id", "")),
            account.id if isinstance(account, AccountRecord) else None,
        )
