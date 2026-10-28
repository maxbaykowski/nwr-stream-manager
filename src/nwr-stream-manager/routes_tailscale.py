"""Requests about Tailscale: connecting NWR Stream Manager to a tailnet, and signing it out."""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import ParseResult

from .baseline_pages import FORM_EXPIRED_MESSAGE, form_message, page_route, TAILSCALE_SETTINGS_PATH
from .routing import route


class TailscaleRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("GET", "/api/tailscale")
    def _get_tailscale(self, parsed: ParseResult) -> None:
        self._send_json(self.service.tailscale_status())

    @route("POST", "/api/tailscale")
    def _post_tailscale(self, parsed: ParseResult) -> None:
        def act():
            payload = self._read_json()
            action = str(payload.get("action", ""))
            if action == "connect":
                return self.service.connect_tailscale()
            if action == "sign_out":
                return self.service.sign_out_tailscale()
            if action == "https":
                return self.service.set_tailscale_https(bool(payload.get("enabled")))
            if action == "funnel":
                return self.service.set_tailscale_funnel(bool(payload.get("enabled")))
            raise ValueError("Choose to connect to Tailscale or sign out of it.")

        self._send_json_result(act)

    @route("POST", TAILSCALE_SETTINGS_PATH)
    def _post_tailscale_form(self, parsed: ParseResult) -> None:
        """Connects or signs out, for browsers without the page script; signing out is
        asked about on the page first."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            action = form.get("action", "")
            if action == "connect":
                self.service.connect_tailscale()
                self._send_redirect(page_route("settings", section="tailscale", result="connecting"))
                return
            if action == "https":
                # Only a change is saved, as with every other setting; opening to the internet
                # is asked about first.
                wanted = form.get("https") == "1"
                funnel = form.get("funnel") == "1" and wanted
                was_funnel = form.get("was_funnel") == "1"
                if funnel and not was_funnel and form.get("confirmed") != "1":
                    if wanted != (form.get("was_https") == "1"):
                        self.service.set_tailscale_https(wanted)
                    self._send_redirect(page_route("settings", section="tailscale", confirm="funnel"))
                    return
                if wanted != (form.get("was_https") == "1"):
                    self.service.set_tailscale_https(wanted)
                if funnel != was_funnel and (wanted or not funnel):
                    self.service.set_tailscale_funnel(funnel)
                self._send_redirect(page_route("settings", section="tailscale", result="saved"))
                return
            if action == "sign_out":
                if form.get("confirmed") != "1":
                    self._send_redirect(page_route("settings", section="tailscale", confirm="sign_out"))
                    return
                self.service.sign_out_tailscale()
                self._send_redirect(page_route("settings", section="tailscale", result="signed_out"))
                return
            raise ValueError("Choose to connect to Tailscale or sign out of it.")
        except Exception as exc:
            query = {"view": "settings", "section": "tailscale"}
            result = ("tailscale-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(query, result=result, form=form), HTTPStatus.BAD_REQUEST)
