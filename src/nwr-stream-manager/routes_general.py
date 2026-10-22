"""Requests about the General settings: the instance name."""

from __future__ import annotations

from urllib.parse import ParseResult

from .baseline_pages import GENERAL_SETTINGS_PATH
from .routing import route


class GeneralRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", GENERAL_SETTINGS_PATH)
    def _post_general_settings_form(self, parsed: ParseResult) -> None:
        self._handle_general_settings_form()

    @route("GET", "/api/general-settings")
    def _get_general_settings(self, parsed: ParseResult) -> None:
        self._send_json(self.service.general_settings())

    @route("PATCH", "/api/general-settings")
    def _patch_general_settings(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_general_settings(self._read_json()))
