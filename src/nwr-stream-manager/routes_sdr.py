"""Requests about the radio: RTL-SDR settings and diagnostics, devices, SDR sharing, and I/Q test files."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import ParseResult

from .baseline_pages import RTL_FORM_PATHS, SDR_SHARING_PATHS
from .routing import route


class SdrRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("POST", *RTL_FORM_PATHS)
    def _post_rtl_form(self, parsed: ParseResult) -> None:
        self._handle_rtl_form(parsed.path)

    @route("POST", *SDR_SHARING_PATHS)
    def _post_sdr_sharing_form(self, parsed: ParseResult) -> None:
        self._handle_sdr_sharing_form(parsed.path)

    @route("PATCH", "/api/settings")
    def _patch_settings(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.update(self._read_json()))

    @route("GET", "/api/rtl/diagnostics")
    def _get_rtl_diagnostics(self, parsed: ParseResult) -> None:
        self._send_json_result(self.service.rtl_diagnostics, "RTL-SDR diagnostics")

    @route("GET", "/api/devices")
    def _get_devices(self, parsed: ParseResult) -> None:
        self._send_json(self.service.devices())

    @route("POST", "/api/rtl-reset")
    def _post_rtl_reset(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.reset_rtl_device(str(self._read_json().get("serial", ""))), "API RTL-SDR reset")

    @route("POST", "/api/soundcard-reset")
    def _post_soundcard_reset(self, parsed: ParseResult) -> None:
        self._send_json_result(
            lambda: self.service.reset_soundcard_device(str(self._read_json().get("stable_id", ""))), "API soundcard reset"
        )

    @route("GET", "/api/remote-sdr")
    def _get_remote_sdr(self, parsed: ParseResult) -> None:
        self._send_json(self.service.remote_sdr_status())

    @route("POST", "/api/remote-sdr/discover")
    def _post_remote_sdr_discover(self, parsed: ParseResult) -> None:
        self._send_json_result_ignoring_body(self.service.discover_remote_sdrs)

    @route("POST", "/api/remote-sdr/pairing/start")
    def _post_remote_sdr_pairing_start(self, parsed: ParseResult) -> None:
        self._send_json_result_ignoring_body(self.service.start_remote_sdr_pairing)

    @route("POST", "/api/remote-sdr/pairing/keepalive")
    def _post_remote_sdr_pairing_keepalive(self, parsed: ParseResult) -> None:
        self._send_json_result_ignoring_body(self.service.keep_remote_sdr_pairing_open)

    @route("POST", "/api/remote-sdr/pairing/cancel")
    def _post_remote_sdr_pairing_cancel(self, parsed: ParseResult) -> None:
        self._send_json_result_ignoring_body(self.service.cancel_remote_sdr_pairing)

    @route("POST", "/api/remote-sdr/pair")
    def _post_remote_sdr_pair(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.pair_remote_sdr(self._read_json()))

    @route("POST", "/api/remote-sdr/unpair")
    def _post_remote_sdr_unpair(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.unpair_remote_sdr_device(self._read_json()))

    @route("GET", "/api/iq-test-sources")
    def _get_iq_test_sources(self, parsed: ParseResult) -> None:
        try:
            self._send_json(self.service.iq_test_sources())
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.NOT_FOUND)

    @route("POST", "/api/iq-test-source")
    def _post_iq_test_source(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.start_iq_test_source(self._read_json()), "API I/Q test source start")

    @route("POST", "/api/iq-test-source/stop")
    def _post_iq_test_source_stop(self, parsed: ParseResult) -> None:
        self._send_json_result(self.service.stop_iq_test_source, "API I/Q test source stop")

    @route("POST", "/api/iq-test-source/seek")
    def _post_iq_test_source_seek(self, parsed: ParseResult) -> None:
        self._send_json_result(lambda: self.service.seek_iq_test_source(self._read_json()), "API I/Q test source seek")
