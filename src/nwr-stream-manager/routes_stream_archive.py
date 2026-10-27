"""Requests about stream archiving: its settings, the archives there are, and downloading them."""

from __future__ import annotations

import html
import logging
import os
import re
import queue
import threading
from http import HTTPStatus
from urllib.parse import ParseResult, parse_qs

from . import page_rendering
from .baseline_pages import (
    FORM_EXPIRED_MESSAGE,
    form_message,
    page_route,
    STREAM_ARCHIVE_DELETE_PATH,
    STREAM_ARCHIVE_DOWNLOAD_PATH,
    STREAM_ARCHIVE_PLAY_PATH,
)
from .routing import route
from .service_stream_archive import StreamArchiveConfirmationRequired, StreamArchiveNoAudio, StreamArchivePlayerOutdated
from .state_files import http_header_filename

LOG = logging.getLogger(__name__)

# Exporting decodes and encodes hours of audio; it runs below the streams' own priority.
EXPORT_NICENESS = 10


class StreamArchiveRoutesMixin:
    """Mixed into RtlControlHandler."""

    @route("PATCH", "/api/stream-archive")
    def _patch_stream_archive(self, parsed: ParseResult) -> None:
        try:
            response = self.service.update_stream_archive(self._read_json())
        except StreamArchiveConfirmationRequired as needed:
            # The page asks, then sends the change again with confirm_delete.
            self._send_json({"error": str(needed), "confirm": str(needed)}, status=HTTPStatus.CONFLICT)
            return
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    @route("GET", "/api/stream-archive-delete-options")
    def _get_stream_archive_delete_options(self, parsed: ParseResult) -> None:
        stream_id = (parse_qs(parsed.query).get("stream") or [""])[0]
        try:
            self._send_json(self.service.stream_archive_delete_options(stream_id))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    @route("POST", "/api/stream-archive-delete")
    def _post_stream_archive_delete(self, parsed: ParseResult) -> None:
        try:
            payload = self._read_json()
            response = self.service.delete_stream_archive_audio(str(payload.get("stream", "")), str(payload.get("choice", "")))
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    @route("POST", STREAM_ARCHIVE_DELETE_PATH)
    def _post_stream_archive_delete_form(self, parsed: ParseResult) -> None:
        """Deletes archived audio for browsers without the page script, asking first."""
        form: dict[str, str] = {}
        try:
            form = self._read_form()
            if not self._form_token_ok(form):
                raise ValueError(FORM_EXPIRED_MESSAGE)
            stream_id, choice = form.get("stream", ""), form.get("choice", "")
            if form.get("confirmed") != "1":
                choices = self.service.stream_archive_delete_options(stream_id)["choices"]
                chosen = next((item for item in choices if item["id"] == choice), None)
                if chosen is None:
                    raise ValueError("There's no archived audio that old.")
                self._send_page(self._render_stream_archive_delete_confirmation(form, chosen))
                return
            result = self.service.delete_stream_archive_audio(stream_id, choice)
        except Exception as exc:
            query = {"view": "stream_archive_delete", "stream": form.get("stream", "")}
            self._send_page(
                self._render_index_page(query, result=("stream-archive-delete-result", form_message(str(exc)), "error"), form=form),
                HTTPStatus.BAD_REQUEST,
            )
            return
        query = {"view": "stream_archives", "stream": stream_id}
        self._send_page(self._render_index_page(query, result=("stream-archive-result", result["message"], "success")))

    def _render_stream_archive_delete_confirmation(self, form: dict[str, str], chosen: dict[str, str]) -> str:
        """Asks before deleting archived audio; the page script asks in a dialog."""
        stream_id = form.get("stream", "")
        page = self._render_index_page({"view": "stream_archive_delete", "stream": stream_id}, form=form)
        question = f"Delete {chosen['storage']} of archived audio?"
        content = (
            f"<h3>{html.escape(question)}</h3>"
            f"<p>{html.escape(chosen['storage'])} of archived audio will be permanently deleted.</p>"
            f'<form method="post" action="{STREAM_ARCHIVE_DELETE_PATH}">{page_rendering.form_token_input(self._form_token())}'
            f'<input type="hidden" name="stream" value="{html.escape(stream_id)}">'
            f'<input type="hidden" name="choice" value="{html.escape(chosen["id"])}">'
            f'<input type="hidden" name="confirmed" value="1">'
            f'<div class="actions"><button type="submit" class="primary" autofocus>Delete audio</button>'
            f'<a class="button-link" href="{html.escape(page_route("stream_archives", stream=stream_id))}">Cancel</a></div></form>'
        )
        page = page_rendering.set_content(page, "stream_archive_delete_confirm", content)
        page = page_rendering.set_hidden(page, "stream_archive_delete_confirm", False)
        page = page_rendering.set_hidden(page, "stream_archive_delete_form", True)
        title = re.search(r"<title>(.*?)</title>", page).group(1)
        return page_rendering.set_title(page, f"{question} - {html.unescape(title)}")

    @route("GET", "/api/stream-archives")
    def _get_stream_archives(self, parsed: ParseResult) -> None:
        self._send_json(self.service.stream_archive_streams())

    @route("GET", "/api/stream-archive-player")
    def _get_stream_archive_player(self, parsed: ParseResult) -> None:
        query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        try:
            self._send_json(self.service.stream_archive_player(query.get("stream", "")))
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)

    @route("GET", "/api/stream-archive-player-time")
    def _get_stream_archive_player_time(self, parsed: ParseResult) -> None:
        """Where a typed date and time is in a player's audio, for Jump to time."""
        query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        try:
            response = self.service.stream_archive_player_time(query.get("player", ""), query.get("date", ""), query.get("time", ""))
        except StreamArchiveNoAudio as missing:
            self._send_json({"error": str(missing), "next_seconds": missing.next_seconds}, status=HTTPStatus.NOT_FOUND)
            return
        except StreamArchivePlayerOutdated as outdated:
            # The page opens a new player and asks it again.
            self._send_json({"error": str(outdated), "reopen": True}, status=HTTPStatus.CONFLICT)
            return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    @route("GET", STREAM_ARCHIVE_PLAY_PATH)
    def _get_stream_archive_play(self, parsed: ParseResult) -> None:
        player_id = (parse_qs(parsed.query).get("player") or [""])[0]
        size = self.service.stream_archive_player_bytes(player_id)
        if size is None:
            self._send_json({"error": "This player has closed. Open it again."}, status=HTTPStatus.NOT_FOUND)
            return
        byte_range = self._requested_range(size)
        if byte_range is None:
            self._send_range_not_satisfiable(size)
            return
        first, last, partial = byte_range
        chunks = self.service.stream_archive_play(player_id, first, last)
        self._send_stream_archive(None, "audio/mpeg", last - first + 1, chunks, content_range=(first, last, size) if partial else None)

    @route("GET", STREAM_ARCHIVE_DOWNLOAD_PATH)
    def _get_stream_archive_download(self, parsed: ParseResult) -> None:
        query = {name: values[0] for name, values in parse_qs(parsed.query).items()}
        try:
            name, content_type, length, chunks = self.service.stream_archive_export(
                query.get("stream", ""),
                query.get("start_date", ""),
                query.get("start_time", ""),
                query.get("end_date", ""),
                query.get("end_time", ""),
                query.get("format", "opus"),
            )
        except Exception as exc:
            page_query = {"view": "stream_archives", "stream": query.get("stream", "")}
            result = ("stream-archive-result", form_message(str(exc)), "error")
            self._send_page(self._render_index_page(page_query, result=result, form=query), HTTPStatus.BAD_REQUEST)
            return
        self._send_stream_archive(name, content_type, length, chunks)

    def _send_stream_archive(
        self, name: str | None, content_type: str, length: int, chunks, *, content_range: tuple[int, int, int] | None = None
    ) -> None:
        """A download, or with no name, audio for the player: part of it if content_range
        says which part."""
        pieces: queue.Queue = queue.Queue(maxsize=8)
        stop = threading.Event()

        def produce() -> None:
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), EXPORT_NICENESS)
            except (AttributeError, OSError):
                pass
            try:
                for chunk in chunks:
                    while not stop.is_set():
                        try:
                            pieces.put(chunk, timeout=0.5)
                            break
                        except queue.Full:
                            continue
                    if stop.is_set():
                        break
                pieces.put(None)
            except Exception as exc:
                LOG.warning("stream archive download failed: %s", exc)
                pieces.put(exc)
            finally:
                close = getattr(chunks, "close", None)
                if close is not None:
                    close()

        producer = threading.Thread(target=produce, name="stream-archive-export", daemon=True)
        self.send_response(HTTPStatus.PARTIAL_CONTENT if content_range else HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if name is None:
            # Players seek by asking for the bytes from where they're going.
            self.send_header("Accept-Ranges", "bytes")
            if content_range:
                self.send_header("Content-Range", "bytes {}-{}/{}".format(*content_range))
        else:
            self.send_header("Content-Disposition", f'attachment; filename="{http_header_filename(name)}"')
        # Worked out before any of it is made, so the browser can show its progress.
        self.send_header("Content-Length", str(length))
        self._send_pending_auth_session_cookie()
        self.end_headers()
        producer.start()
        try:
            while True:
                piece = pieces.get()
                if piece is None:
                    break
                if isinstance(piece, Exception):
                    # Cut short of its length: the browser sees an incomplete download, not a wrong one.
                    self.close_connection = True
                    break
                self.wfile.write(piece)
        except OSError:
            if name is not None:
                LOG.info("stream archive download of %s ended before it finished", name)
            self.close_connection = True
        finally:
            stop.set()
            producer.join(timeout=5)
