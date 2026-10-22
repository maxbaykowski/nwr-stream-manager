"""Requests about live audio: monitoring a stream, the Weather Radio Receiver, and live updates to the page."""

from __future__ import annotations

from urllib.parse import ParseResult

from .routing import route


class LiveAudioRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("GET", "/api/live-audio")
    def _get_live_audio(self, parsed: ParseResult) -> None:
        self._handle_live_audio_websocket(parsed)

    @route("GET", "/api/settings-events")
    def _get_settings_events(self, parsed: ParseResult) -> None:
        self._handle_settings_events_websocket()

    @route("GET", "/api/monitor/status")
    def _get_monitor_status(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.monitor_status(self._query_value(parsed, "client_id"), self._current_account_id()))

    @route("GET", "/api/receiver/status")
    def _get_receiver_status(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.receiver_status(self._query_value(parsed, "client_id"), self._current_account_id()))

    @route("POST", "/api/monitor/stop")
    def _post_monitor_stop(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.stop_monitor(self._read_json(), self._current_account_id()), "API monitor stop")

    @route("POST", "/api/monitor/pause")
    def _post_monitor_pause(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.pause_monitor(self._read_json(), self._current_account_id()), "API monitor pause")

    @route("POST", "/api/monitor/resume")
    def _post_monitor_resume(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.resume_monitor(self._read_json(), self._current_account_id()), "API monitor resume")

    @route("POST", "/api/receiver/tune")
    def _post_receiver_tune(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.tune_receiver(self._read_json(), self._current_account_id()), "API receiver tune")

    @route("POST", "/api/receiver/pause")
    def _post_receiver_pause(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.pause_receiver(self._read_json(), self._current_account_id()), "API receiver pause")

    @route("POST", "/api/receiver/resume")
    def _post_receiver_resume(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.resume_receiver(self._read_json(), self._current_account_id()), "API receiver resume"
        )

    @route("POST", "/api/receiver/stop")
    def _post_receiver_stop(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.stop_receiver(self._read_json(), self._current_account_id()), "API receiver stop")
