"""Reading requests and sending responses: JSON, pages, files, form tokens and redirects."""

from __future__ import annotations

import json
import logging
import mimetypes
import re
from http import HTTPStatus
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from . import page_rendering
from .baseline_pages import HELP_FILE_NAME
from .constants import MAX_JSON_REQUEST_BYTES
from .simulated_station import asset_path
from .state_files import http_header_filename


LOG = logging.getLogger(__name__)


class ResponseHandlerMixin:
    """Mixed into RtlControlHandler."""

    def log_message(self, format: str, *args) -> None:
        LOG.debug("HTTP %s - %s", self.address_string(), format % args)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length > MAX_JSON_REQUEST_BYTES:
            raise ValueError("request body is too large")
        data = self.rfile.read(length) if length else b"{}"
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self._send_pending_auth_session_cookie()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before JSON response could be written")

    def _send_audio(self, data: bytes, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self._send_pending_auth_session_cookie()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before audio response could be written")

    def _send_help(self) -> None:
        # Read on each request so edits to the help file show up without a restart.
        try:
            content = asset_path(HELP_FILE_NAME).read_text(encoding="utf-8")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._send_html(content)

    def _send_html(self, content: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self._send_pending_auth_session_cookie()
        self._send_new_form_token_cookie()
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.debug("client disconnected before HTML response could be written")

    def _send_file(self, path: Path, download_name: str, download: bool, *, delete_after: bool = False) -> None:
        content_type = mimetypes.guess_type(download_name)[0] or "application/octet-stream"
        disposition = "attachment" if download else "inline"
        data_length = path.stat().st_size
        start = 0
        end = data_length - 1
        partial = False
        range_header = "" if delete_after else str(self.headers.get("Range", "")).strip()
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or data_length <= 0:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{data_length}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            raw_start, raw_end = match.groups()
            if raw_start == "" and raw_end == "":
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{data_length}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            if raw_start == "":
                suffix_length = int(raw_end)
                if suffix_length <= 0:
                    start = data_length
                else:
                    start = max(0, data_length - suffix_length)
                end = data_length - 1
            else:
                start = int(raw_start)
                end = int(raw_end) if raw_end else data_length - 1
            if start >= data_length or start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{data_length}")
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                return
            end = min(end, data_length - 1)
            partial = True
        response_length = max(0, end - start + 1)
        try:
            self.send_response(HTTPStatus.PARTIAL_CONTENT if partial else HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(response_length))
            self.send_header("Accept-Ranges", "bytes")
            if partial:
                self.send_header("Content-Range", f"bytes {start}-{end}/{data_length}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition", f'{disposition}; filename="{http_header_filename(download_name)}"')
            self._send_pending_auth_session_cookie()
            self.end_headers()
            with path.open("rb") as source:
                source.seek(start)
                remaining = response_length
                while remaining > 0:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        LOG.debug("client disconnected before file response could be written")
                        break
                    remaining -= len(chunk)
        finally:
            if delete_after:
                path.unlink(missing_ok=True)

    def _cookie_value(self, name: str) -> str:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie", "")).get(name)
        except Exception:
            return ""
        return morsel.value if morsel is not None else ""

    def _form_token(self) -> str:
        """The token this browser's forms carry, giving it one if it has none yet."""
        token = self._cookie_value(page_rendering.FORM_TOKEN_COOKIE_NAME) or getattr(self, "_new_form_token", "")
        if not token:
            token = page_rendering.new_form_token()
            self._new_form_token = token
        return token

    def _send_new_form_token_cookie(self) -> None:
        token = getattr(self, "_new_form_token", "")
        if token:
            self.send_header("Set-Cookie", page_rendering.form_token_cookie_header(token))
            self._new_form_token = ""

    def _read_form(self) -> dict[str, str]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("invalid Content-Length header") from exc
        if length > page_rendering.MAX_FORM_REQUEST_BYTES:
            raise ValueError("request body is too large")
        return page_rendering.parse_form(self.rfile.read(length) if length > 0 else b"")

    def _form_token_ok(self, form: dict[str, str]) -> bool:
        return page_rendering.form_token_valid(
            self._cookie_value(page_rendering.FORM_TOKEN_COOKIE_NAME),
            form.get(page_rendering.FORM_TOKEN_FIELD, ""),
        )

    def _discard_request_body(self) -> None:
        # Left unread, it would be taken for the connection's next request.
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if 0 <= length <= page_rendering.MAX_FORM_REQUEST_BYTES:
            self.rfile.read(length)
        else:
            self.close_connection = True

    def _send_redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self._send_pending_auth_session_cookie()
        self.end_headers()

    def _send_page(self, page: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        """Sends a page the server filled in, adjusted for this browser."""
        if page_rendering.old_internet_explorer(self.headers.get("User-Agent", "")):
            page = page_rendering.mark_hidden_for_old_internet_explorer(page)
        self._send_html(page, status)

    def _send_index_page(self, parsed) -> None:
        query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        self._send_page(self._render_index_page(query))
