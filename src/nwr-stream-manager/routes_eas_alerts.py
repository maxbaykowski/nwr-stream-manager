"""Requests about recorded EAS alerts: listing, playing, exporting and removing them, and recording settings."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import ParseResult

from .baseline_pages import EAS_ALERT_EXPORT_PATH, EAS_ALERT_FORM_PATHS
from .routing import route


class EasAlertRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("GET", EAS_ALERT_EXPORT_PATH)
    def _get_eas_alert_export_page(self, parsed: ParseResult) -> None:
        self._handle_eas_alert_export(parsed)

    @route("POST", *EAS_ALERT_FORM_PATHS)
    def _post_eas_alert_form(self, parsed: ParseResult) -> None:
        self._handle_eas_alert_form(parsed.path)

    @route("GET", "/api/eas-alert-streams")
    def _get_eas_alert_streams(self, parsed: ParseResult) -> None:
        self._send_json(self.service.eas_alert_streams())

    @route("GET", "/api/eas-alerts")
    def _get_eas_alerts(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.eas_alerts(
                self._query_value(parsed, "stream_id"),
                int(self._query_value(parsed, "page", "1")),
                int(self._query_value(parsed, "per_page", "25")),
            )
        )

    @route("GET", "/api/eas-alert-bulk-options")
    def _get_eas_alert_bulk_options(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.eas_alert_bulk_options(self._query_value(parsed, "stream_id")))

    @route("GET", "/api/eas-alert-range-count")
    def _get_eas_alert_range_count(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.eas_alert_range_count(*self._eas_alert_range_query(parsed)))

    @route("GET", "/api/eas-alert-export")
    def _get_eas_alert_export(self, parsed: ParseResult) -> None:
        try:
            zip_path, download_name = self.service.eas_alert_export_zip(*self._eas_alert_range_query(parsed))
            self._send_file(zip_path, download_name, True, delete_after=True)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    @route("GET", "/api/eas-alert")
    def _get_eas_alert(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.eas_alert_detail(self._query_value(parsed, "stream_id"), self._query_value(parsed, "alert_id"))
        )

    @route("GET", "/api/eas-alert-audio")
    def _get_eas_alert_audio(self, parsed: ParseResult) -> None:
        try:
            audio_path, download_name = self.service.eas_alert_audio_path(
                self._query_value(parsed, "stream_id"),
                self._query_value(parsed, "alert_id"),
            )
            self._send_file(audio_path, download_name, self._query_value(parsed, "download", "0") == "1")
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    @route("POST", "/api/eas-alert-delete")
    def _post_eas_alert_delete(self, parsed: ParseResult) -> None:
        def remove() -> dict:
            payload = self._read_json()
            return self.service.remove_eas_alert_range(
                str(payload.get("stream_id", "")),
                str(payload.get("mode", "")),
                str(payload.get("start", "")),
                str(payload.get("end", "")),
            )

        self._send_json_result(remove)

    @route("DELETE", "/api/eas-alert")
    def _delete_eas_alert(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.remove_eas_alert(self._query_value(parsed, "stream_id"), self._query_value(parsed, "alert_id"))
        )

    @route("PATCH", "/api/eas-recording")
    def _patch_eas_recording(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_eas_recording(self._read_json()))

    def _eas_alert_range_query(self, parsed: ParseResult) -> tuple[str, str, str, str]:
        """The stream and range of alerts a request is about: stream, mode, start and end."""
        return tuple(self._query_value(parsed, name) for name in ("stream_id", "mode", "start", "end"))  # type: ignore[return-value]
