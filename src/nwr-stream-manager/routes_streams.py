"""Requests about streams: finding stations, and adding, changing, testing and removing streams and their outputs."""

from __future__ import annotations

from urllib.parse import ParseResult

from .baseline_pages import OUTPUT_FORM_PATHS, STREAM_FORM_PATHS, WIZARD_FORM_PATH
from .routing import route


class StreamRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", *STREAM_FORM_PATHS)
    def _post_stream_form(self, parsed: ParseResult) -> None:
        self._handle_stream_form(parsed.path)

    @route("POST", *OUTPUT_FORM_PATHS)
    def _post_output_form(self, parsed: ParseResult) -> None:
        self._handle_output_form(parsed.path)

    @route("POST", WIZARD_FORM_PATH)
    def _post_wizard_form(self, parsed: ParseResult) -> None:
        self._handle_wizard_form()

    @route("GET", "/api/stations")
    def _get_stations(self, parsed: ParseResult) -> None:
        try:
            limit = int(self._query_value(parsed, "limit", "50"))
        except ValueError:
            limit = 50
        self._send_json(self.service.search_stations(self._query_value(parsed, "q"), limit))

    @route("GET", "/api/streams")
    def _get_streams(self, parsed: ParseResult) -> None:
        self._send_json(
            self.service.stream_status(
                read_only=self._current_account_is_read_only(),
                account_id=self._current_account_id(),
            )
        )

    @route("POST", "/api/streams")
    def _post_stream(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.add_stream(self._read_json()))

    @route("PATCH", "/api/streams")
    def _patch_stream(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_stream(self._read_json()))

    @route("DELETE", "/api/streams")
    def _delete_stream(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.remove_stream(self._query_value(parsed, "id")))

    @route("POST", "/api/stream-output")
    def _post_stream_output(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.add_stream_output(self._read_json()))

    @route("PATCH", "/api/stream-output")
    def _patch_stream_output(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_stream_output(self._read_json()))

    @route("DELETE", "/api/stream-output")
    def _delete_stream_output(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.remove_stream_output(self._query_value(parsed, "stream_id"), self._query_value(parsed, "output_id"))
        )

    @route("POST", "/api/icecast-auth")
    def _post_icecast_auth_test(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.test_icecast_auth_payload(self._read_json()))

    @route("POST", "/api/stream-soundcard-preview")
    def _post_soundcard_preview(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.upsert_soundcard_preview_stream(self._read_json()))

    @route("POST", "/api/stream-soundcard-preview/discard")
    def _post_soundcard_preview_discard(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.discard_soundcard_preview_stream(str(self._read_json().get("preview_id", "")))
        )

    @route("DELETE", "/api/stream-soundcard-preview")
    def _delete_soundcard_preview(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.discard_soundcard_preview_stream(self._query_value(parsed, "preview_id")))

    @route("POST", "/api/stream-test-mode")
    def _post_stream_test_mode(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_stream_test_mode(self._read_json()))

    @route("PATCH", "/api/audio-effects")
    def _patch_audio_effects(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_audio_effects(self._read_json()))
