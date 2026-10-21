"""Requests about notifications: their settings, where they link to, and sending a test."""

from __future__ import annotations

from urllib.parse import ParseResult

from .baseline_pages import NOTIFICATION_FORM_PATHS
from .routing import route


class NotificationRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", *NOTIFICATION_FORM_PATHS)
    def _post_notification_form(self, parsed: ParseResult) -> None:
        self._handle_notification_form(parsed.path)

    @route("GET", "/api/notification-settings")
    def _get_notification_settings(self, parsed: ParseResult) -> None:
        self._send_json({"notifications": self.service.notification_settings_snapshot().public_dict()})

    @route("PATCH", "/api/notification-settings")
    def _patch_notification_settings(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_notification_settings(self._read_json()))

    @route("GET", "/api/notification-access-urls")
    def _get_notification_access_urls(self, parsed: ParseResult) -> None:
        self._send_json(self.service.notification_access_url_options())

    @route("POST", "/api/notification-test")
    def _post_notification_test(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.test_notification_settings(self._read_json()))

    @route("PATCH", "/api/stream-notifications")
    def _patch_stream_notifications(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update_stream_notifications(self._read_json()))
