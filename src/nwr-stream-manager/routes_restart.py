"""Requests to restart NWR Stream Manager."""

from __future__ import annotations

from urllib.parse import ParseResult

from .baseline_pages import RESTART_PATH
from .routing import route


class RestartRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", RESTART_PATH)
    def _post_restart_form(self, parsed: ParseResult) -> None:
        self._handle_restart_form()

    @route("POST", "/api/restart")
    def _post_restart(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.request_restart(self._read_json(), self._current_account_name()), "API restart")
