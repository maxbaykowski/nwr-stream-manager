"""Requests about the I/Q recorder: recording, and downloading and removing recordings."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import ParseResult

from .baseline_pages import IQ_RECORDER_FORM_PATHS, IQ_RECORDING_DOWNLOAD_PATH
from .routing import route


class IqRecorderRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", *IQ_RECORDER_FORM_PATHS)
    def _post_iq_recorder_form(self, parsed: ParseResult) -> None:
        self._handle_iq_recorder_form(parsed.path)

    @route("GET", IQ_RECORDING_DOWNLOAD_PATH)
    def _get_iq_recording_download_page(self, parsed: ParseResult) -> None:
        self._handle_iq_recording_download(parsed)

    @route("GET", "/api/iq-recorder/status")
    def _get_iq_recorder_status(self, parsed: ParseResult) -> None:
        if self._current_account_is_read_only():
            self._send_json(self.service.redacted_iq_recorder_status())
        else:
            self._send_json(self.service.iq_recorder_status())

    @route("POST", "/api/iq-recorder/start")
    def _post_iq_recorder_start(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.start_iq_recording(self._read_json(), self._current_account_id()), "API I/Q recorder start"
        )

    @route("POST", "/api/iq-recorder/stop")
    def _post_iq_recorder_stop(self, parsed: ParseResult) -> None:
        self._send_json_result_ignoring_body(self.service.stop_iq_recording, "API I/Q recorder stop")

    @route("GET", "/api/iq-recordings")
    def _get_iq_recordings(self, parsed: ParseResult) -> None:
        self._send_json(self.service.iq_recordings())

    @route("GET", "/api/iq-recording-download")
    def _get_iq_recording_download(self, parsed: ParseResult) -> None:
        fmt = self._query_value(parsed, "format", "cf")
        try:
            recording, file_path, download_name, download_size = self.service.iq_recording_download_info(
                self._query_value(parsed, "id"), fmt
            )
            self._send_iq_recording(
                file_path,
                download_name,
                download_size,
                fmt,
                str(recording.get("id", "")),
                self._current_account_id(),
            )
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    @route("DELETE", "/api/iq-recording")
    def _delete_iq_recording(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.remove_iq_recording(self._query_value(parsed, "id")))
