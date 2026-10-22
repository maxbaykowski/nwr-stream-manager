"""Requests about the Advanced settings: the port and address the web interface listens on."""

from __future__ import annotations

from urllib.parse import ParseResult

from .baseline_pages import ADVANCED_SETTINGS_PATH
from .routing import route


class AdvancedRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", ADVANCED_SETTINGS_PATH)
    def _post_advanced_settings_form(self, parsed: ParseResult) -> None:
        self._handle_advanced_settings_form()

    @route("GET", "/api/advanced-settings")
    def _get_advanced_settings(self, parsed: ParseResult) -> None:
        self._send_json(self.service.advanced_settings())

    @route("PATCH", "/api/advanced-settings")
    def _patch_advanced_settings(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_advanced_settings(self._read_json()))
