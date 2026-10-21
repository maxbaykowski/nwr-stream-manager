"""Requests about fallback audio: its settings, the announcements, and each stream's fallback action."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import ParseResult

from .baseline_pages import FALLBACK_SETTINGS_PATH, FALLBACK_UPLOAD_PATH
from .routing import route


class FallbackRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", FALLBACK_SETTINGS_PATH)
    def _post_fallback_settings_form(self, parsed: ParseResult) -> None:
        self._handle_fallback_settings_form()

    @route("POST", FALLBACK_UPLOAD_PATH)
    def _post_fallback_upload_form(self, parsed: ParseResult) -> None:
        self._handle_fallback_upload_form()

    @route("GET", "/api/fallback-audio")
    def _get_fallback_audio(self, parsed: ParseResult) -> None:
        try:
            data = self.service.fallback_announcement_wav(self._query_value(parsed, "announcement"))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)
            return
        self._send_audio(data, "audio/wav")

    @route("POST", "/api/fallback-audio")
    def _post_fallback_audio(self, parsed: ParseResult) -> None:
        self._handle_custom_fallback_audio_upload()

    @route("PATCH", "/api/fallback-settings")
    def _patch_fallback_settings(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_fallback_settings(self._read_json()))

    @route("PATCH", "/api/stream-fallback-action")
    def _patch_stream_fallback_action(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_stream_fallback_action(self._read_json()))
