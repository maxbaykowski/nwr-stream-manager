"""Serving the web interface with GNU libmicrohttpd, loaded with ctypes.

libmicrohttpd accepts connections, reads requests, and holds them to its time
and connection limits, which Python's own server doesn't: a connection that
goes quiet is closed rather than kept forever. Each request then goes through
the request handler, so every page and route is written as for Python's server:
the handler reads the request from memory and writes its response to an object
that passes it on to libmicrohttpd, all at once or bit by bit for long
downloads. A WebSocket takes over the connection itself once libmicrohttpd
hands it over.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import http.client
import io
import itertools
import logging
import os
import queue
import socket
import threading
import time
from typing import Any
from urllib.parse import quote, quote_plus


LOG = logging.getLogger(__name__)

MHD_NO = 0
MHD_YES = 1

MHD_USE_THREAD_PER_CONNECTION = 4
MHD_USE_INTERNAL_POLLING_THREAD = 8
# poll() rather than select(), which can't watch file descriptors above 1023.
MHD_USE_POLL = 64
MHD_ALLOW_UPGRADE = 32768

MHD_OPTION_END = 0
MHD_OPTION_CONNECTION_LIMIT = 2
MHD_OPTION_CONNECTION_TIMEOUT = 3
MHD_OPTION_NOTIFY_COMPLETED = 4
MHD_OPTION_LISTEN_SOCKET = 12
MHD_OPTION_ARRAY = 15

MHD_HEADER_KIND = 1
MHD_GET_ARGUMENT_KIND = 8
MHD_CONNECTION_INFO_CLIENT_ADDRESS = 2
MHD_UPGRADE_ACTION_CLOSE = 0

MHD_SIZE_UNKNOWN = 2**64 - 1
MHD_CONTENT_READER_END_OF_STREAM = -1
MHD_CONTENT_READER_END_WITH_ERROR = -2

# A connection that sends or reads nothing for this long is closed.
CONNECTION_TIMEOUT_SECONDS = 60
# Connections at once, live audio included. Not limited per address: behind a tunnel,
# everyone arrives from the same one.
CONNECTION_LIMIT = 256
# How long stopping waits for handlers to finish, so their connections are closed while
# libmicrohttpd still has them.
STOP_WAIT_SECONDS = 5.0
# The largest request body: a custom fallback announcement, the biggest upload, is under 10 MB.
MAX_REQUEST_BODY_BYTES = 12 * 1024 * 1024
# Headers the server adds itself; the handler's would be duplicates or not allowed. A second
# "Connection: Upgrade" makes it "Upgrade, Upgrade", which Safari refuses as a WebSocket.
SERVER_MANAGED_HEADERS = {"content-length", "transfer-encoding", "connection", "date", "server", "keep-alive"}


class MhdOptionItem(ctypes.Structure):
    _fields_ = [("option", ctypes.c_int), ("value", ctypes.c_ssize_t), ("ptr_value", ctypes.c_void_p)]


AccessHandler = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.c_void_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_char_p,
    ctypes.c_void_p,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.POINTER(ctypes.c_void_p),
)
RequestCompleted = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_int)
KeyValueIterator = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
ContentReader = ctypes.CFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_size_t)
UpgradeHandler = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_void_p
)


class MicrohttpdError(RuntimeError):
    pass


_library: ctypes.CDLL | None = None
_library_lock = threading.Lock()


def load_libmicrohttpd() -> ctypes.CDLL:
    global _library
    with _library_lock:
        if _library is not None:
            return _library
        # The same library the startup check looked for.
        name = ctypes.util.find_library("microhttpd") or "libmicrohttpd.so.12"
        try:
            lib = ctypes.CDLL(name)
        except OSError as exc:
            raise MicrohttpdError(f"libmicrohttpd couldn't be loaded: {exc}") from exc
        lib.MHD_start_daemon.restype = ctypes.c_void_p
        # The fixed arguments only: the options after them are variable.
        lib.MHD_start_daemon.argtypes = [
            ctypes.c_uint, ctypes.c_uint16, ctypes.c_void_p, ctypes.c_void_p, AccessHandler, ctypes.c_void_p,
        ]
        lib.MHD_stop_daemon.argtypes = [ctypes.c_void_p]
        lib.MHD_stop_daemon.restype = None
        lib.MHD_get_connection_values.argtypes = [ctypes.c_void_p, ctypes.c_int, KeyValueIterator, ctypes.c_void_p]
        lib.MHD_get_connection_values.restype = ctypes.c_int
        lib.MHD_get_connection_info.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.MHD_get_connection_info.restype = ctypes.POINTER(ctypes.c_void_p)
        lib.MHD_create_response_from_buffer_copy.argtypes = [ctypes.c_size_t, ctypes.c_void_p]
        lib.MHD_create_response_from_buffer_copy.restype = ctypes.c_void_p
        lib.MHD_create_response_from_callback.argtypes = [ctypes.c_uint64, ctypes.c_size_t, ContentReader, ctypes.c_void_p, ctypes.c_void_p]
        lib.MHD_create_response_from_callback.restype = ctypes.c_void_p
        lib.MHD_create_response_for_upgrade.argtypes = [UpgradeHandler, ctypes.c_void_p]
        lib.MHD_create_response_for_upgrade.restype = ctypes.c_void_p
        lib.MHD_add_response_header.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        lib.MHD_add_response_header.restype = ctypes.c_int
        lib.MHD_queue_response.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p]
        lib.MHD_queue_response.restype = ctypes.c_int
        lib.MHD_destroy_response.argtypes = [ctypes.c_void_p]
        lib.MHD_destroy_response.restype = None
        # Variable too; called with the action only.
        lib.MHD_upgrade_action.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.MHD_upgrade_action.restype = ctypes.c_int
        _library = lib
        return lib


class _ResponseOutput:
    """The handler's wfile: takes what it writes, and passes it on to libmicrohttpd.

    The status line and headers are read out of what's written first. The body then
    goes to libmicrohttpd through a queue, or once a WebSocket has taken over, straight
    to the connection.
    """

    def __init__(self) -> None:
        self.header_bytes = bytearray()
        self.headers_ready = threading.Event()
        self.status = 0
        self.headers: list[tuple[str, str]] = []
        self.body: queue.Queue[bytes | None] = queue.Queue(maxsize=64)
        self.finished = threading.Event()
        self.aborted = threading.Event()
        self.socket_ready = threading.Event()
        self.socket: socket.socket | None = None

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if self.aborted.is_set():
            raise BrokenPipeError("the connection was closed")
        data = bytes(data)
        if not self.headers_ready.is_set():
            self.header_bytes += data
            end = self.header_bytes.find(b"\r\n\r\n")
            if end < 0:
                return len(data)
            head, rest = bytes(self.header_bytes[:end]), bytes(self.header_bytes[end + 4 :])
            self.header_bytes.clear()
            self._read_head(head)
            self.headers_ready.set()
            if rest:
                self._send(rest)
            return len(data)
        if data:
            self._send(data)
        return len(data)

    def flush(self) -> None:
        pass

    def _read_head(self, head: bytes) -> None:
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split(" ", 2)
        self.status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 500
        for line in lines[1:]:
            name, separator, value = line.partition(":")
            if separator:
                self.headers.append((name.strip(), value.strip()))

    def _send(self, data: bytes) -> None:
        if self.status == 101:
            if not self.socket_ready.wait(30) or self.socket is None:
                raise BrokenPipeError("the WebSocket connection wasn't handed over")
            self.socket.sendall(data)
            return
        while True:
            if self.aborted.is_set():
                raise BrokenPipeError("the connection was closed")
            try:
                self.body.put(data, timeout=0.5)
                return
            except queue.Full:
                continue

    def finish(self) -> None:
        self.finished.set()
        if not self.headers_ready.is_set():
            # The handler wrote nothing at all.
            self.status = 500
            self.headers_ready.set()
        if self.status != 101:
            while not self.aborted.is_set():
                try:
                    self.body.put(None, timeout=0.5)
                    return
                except queue.Full:
                    continue

    def header(self, name: str) -> str:
        return next((value for key, value in self.headers if key.lower() == name.lower()), "")


class _ConnectionOptions:
    """Stands in for the handler's socket until a WebSocket has the real one."""

    def __init__(self) -> None:
        self.options: list[tuple[int, int, int]] = []

    def setsockopt(self, level: int, option: int, value: int) -> None:
        self.options.append((level, option, value))


class _Request:
    def __init__(self, request_id: int, connection: int, method: str, url: str, version: str) -> None:
        self.request_id = request_id
        self.connection = connection
        self.method = method
        self.url = url
        self.version = version
        self.body = bytearray()
        self.too_large = False
        self.output = _ResponseOutput()
        self.connection_options = _ConnectionOptions()
        self.pending = b""
        # Read from libmicrohttpd on its own thread, before the handler's thread starts.
        self.client_address: tuple[str, int] = ("", 0)
        self.query = ""
        self.header_block = b"\r\n"
        self.upgrade_handle: int | None = None
        self.thread: threading.Thread | None = None


class MicrohttpdServer:
    """Matches the parts of Python's ThreadingHTTPServer that NWR Stream Manager uses."""

    def __init__(self, server_address: tuple[str, int], handler_class: type) -> None:
        self.handler_class = handler_class
        self.lib = load_libmicrohttpd()
        family = socket.AF_INET6 if ":" in server_address[0] else socket.AF_INET
        listener = socket.socket(family, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(server_address)
            listener.listen(128)
        except OSError:
            listener.close()
            raise
        self.server_address = listener.getsockname()[:2]
        self.listener = listener
        self.daemon: int | None = None
        # Held while calling libmicrohttpd about a connection from a handler's thread, and
        # while stopping it, so no call is made once it has freed its connections.
        self.daemon_lock = threading.Lock()
        self.stopped = threading.Event()
        self.requests: dict[int, _Request] = {}
        self.requests_lock = threading.Lock()
        self.request_ids = itertools.count(1)
        # Kept for as long as libmicrohttpd may call them.
        self._access = AccessHandler(self._on_access)
        self._completed = RequestCompleted(self._on_completed)
        self._reader = ContentReader(self._on_read)
        self._upgrade = UpgradeHandler(self._on_upgrade)

    # -- lifecycle --------------------------------------------------------

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        flags = MHD_USE_INTERNAL_POLLING_THREAD | MHD_USE_THREAD_PER_CONNECTION | MHD_USE_POLL | MHD_ALLOW_UPGRADE
        options = (MhdOptionItem * 5)(
            MhdOptionItem(MHD_OPTION_CONNECTION_TIMEOUT, CONNECTION_TIMEOUT_SECONDS, None),
            MhdOptionItem(MHD_OPTION_CONNECTION_LIMIT, CONNECTION_LIMIT, None),
            MhdOptionItem(MHD_OPTION_LISTEN_SOCKET, self.listener.fileno(), None),
            # Two pointers: the function as the value, its data (none) as the pointer.
            MhdOptionItem(MHD_OPTION_NOTIFY_COMPLETED, ctypes.cast(self._completed, ctypes.c_void_p).value, None),
            MhdOptionItem(MHD_OPTION_END, 0, None),
        )
        self._options = options
        daemon = self.lib.MHD_start_daemon(
            flags, 0, None, None, self._access, None, ctypes.c_int(MHD_OPTION_ARRAY), options, ctypes.c_int(MHD_OPTION_END)
        )
        if not daemon:
            raise MicrohttpdError("libmicrohttpd couldn't start")
        # libmicrohttpd closes the listening socket when it stops.
        self.listener.detach()
        self.daemon = daemon
        LOG.info("serving the web interface with libmicrohttpd")
        self.stopped.wait()

    def shutdown(self) -> None:
        self.stopped.set()

    def server_close(self) -> None:
        self.stopped.set()
        with self.requests_lock:
            requests = list(self.requests.values())
        # Anything still being sent stops, so libmicrohttpd isn't left waiting on it, and
        # each handler gets to close its own connection before libmicrohttpd is stopped.
        for request in requests:
            self._abort(request)
        deadline = time.monotonic() + STOP_WAIT_SECONDS
        for request in requests:
            if request.thread is not None:
                request.thread.join(max(0.0, deadline - time.monotonic()))
        with self.daemon_lock:
            daemon, self.daemon = self.daemon, None
            if daemon is not None:
                self.lib.MHD_stop_daemon(daemon)
        if daemon is None and self.listener.fileno() >= 0:
            self.listener.close()

    # -- requests ---------------------------------------------------------

    def _on_access(self, _cls, connection, url, method, version, upload_data, upload_data_size, req_cls) -> int:
        try:
            if not req_cls[0]:
                request_id = next(self.request_ids)
                request = _Request(request_id, connection, method.decode("latin-1"), url.decode("latin-1"), version.decode("latin-1"))
                with self.requests_lock:
                    self.requests[request_id] = request
                req_cls[0] = request_id
                return MHD_YES
            request = self.requests[int(req_cls[0])]
            size = int(upload_data_size[0])
            if size:
                if len(request.body) + size > MAX_REQUEST_BODY_BYTES:
                    request.too_large = True
                elif not request.too_large:
                    request.body += ctypes.string_at(upload_data, size)
                upload_data_size[0] = 0
                return MHD_YES
            return self._respond(request)
        except Exception:
            LOG.exception("libmicrohttpd request handling failed")
            return MHD_NO

    def _respond(self, request: _Request) -> int:
        if request.too_large:
            return self._queue_buffer(request, 413, [("Content-Type", "text/plain")], b"The request is too large.")
        request.client_address = self._client_address(request.connection)
        request.query = self._query(request.connection)
        request.header_block = self._header_block(request.connection)
        request.thread = threading.Thread(target=self._run_handler, args=(request,), name="http-request", daemon=True)
        request.thread.start()
        output = request.output
        while not output.headers_ready.wait(0.5):
            if output.aborted.is_set():
                # Stopping: the connection doesn't wait on the handler any longer.
                return MHD_NO
        if output.status == 101:
            response = self.lib.MHD_create_response_for_upgrade(self._upgrade, ctypes.c_void_p(request.request_id))
            return self._queue(request, response, output.status, output.headers)
        if output.finished.is_set():
            # Done already, as most are: the whole body is waiting.
            return self._queue_buffer(request, output.status, output.headers, self._drain(output))
        length = output.header("Content-Length")
        size = int(length) if length.isdigit() else MHD_SIZE_UNKNOWN
        response = self.lib.MHD_create_response_from_callback(
            size, 64 * 1024, self._reader, ctypes.c_void_p(request.request_id), None
        )
        return self._queue(request, response, output.status, output.headers)

    def _queue_buffer(self, request: _Request, status: int, headers: list[tuple[str, str]], body: bytes) -> int:
        buffer = ctypes.create_string_buffer(body, len(body))
        response = self.lib.MHD_create_response_from_buffer_copy(len(body), buffer)
        return self._queue(request, response, status, headers)

    def _queue(self, request: _Request, response: int | None, status: int, headers: list[tuple[str, str]]) -> int:
        if not response:
            return MHD_NO
        try:
            for name, value in headers:
                if name.lower() in SERVER_MANAGED_HEADERS:
                    continue
                self.lib.MHD_add_response_header(response, name.encode("latin-1"), value.encode("latin-1"))
            return self.lib.MHD_queue_response(request.connection, int(status), response)
        finally:
            self.lib.MHD_destroy_response(response)

    @staticmethod
    def _drain(output: _ResponseOutput) -> bytes:
        chunks = []
        while True:
            chunk = output.body.get()
            if chunk is None:
                return b"".join(chunks)
            chunks.append(chunk)

    def _on_read(self, cls, _position, buffer, maximum) -> int:
        request = self.requests.get(int(cls or 0))
        if request is None:
            return MHD_CONTENT_READER_END_WITH_ERROR
        data = request.pending
        while not data:
            if request.output.aborted.is_set():
                return MHD_CONTENT_READER_END_WITH_ERROR
            try:
                chunk = request.output.body.get(timeout=0.5)
            except queue.Empty:
                continue
            if chunk is None:
                return MHD_CONTENT_READER_END_OF_STREAM
            data = chunk
        taken = data[: int(maximum)]
        request.pending = data[len(taken) :]
        ctypes.memmove(buffer, taken, len(taken))
        return len(taken)

    def _on_upgrade(self, cls, _connection, _req_cls, _extra, _extra_size, sock, upgrade_handle) -> None:
        request = self.requests.get(int(cls or 0))
        if request is None:
            self.lib.MHD_upgrade_action(upgrade_handle, MHD_UPGRADE_ACTION_CLOSE)
            return
        request.upgrade_handle = upgrade_handle
        # Our own copy, so closing it leaves libmicrohttpd's to close in turn.
        connection = socket.socket(fileno=os.dup(sock))
        connection.setblocking(True)
        for level, option, value in request.connection_options.options:
            try:
                connection.setsockopt(level, option, value)
            except OSError:
                pass
        request.output.socket = connection
        request.output.socket_ready.set()

    def _on_completed(self, _cls, _connection, req_cls, _reason) -> None:
        request_id = int(req_cls[0] or 0)
        with self.requests_lock:
            request = self.requests.get(request_id)
        if request is None:
            return
        if request.output.status == 101 and request.thread is not None and request.thread.is_alive():
            # A WebSocket outlives its request; it's forgotten once its handler is done.
            return
        with self.requests_lock:
            self.requests.pop(request_id, None)
        self._abort(request)

    def _abort(self, request: _Request) -> None:
        request.output.aborted.set()
        sock = request.output.socket
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # -- the handler ------------------------------------------------------

    def _run_handler(self, request: _Request) -> None:
        output = request.output
        try:
            handler = self._make_handler(request)
            method = getattr(handler, f"do_{request.method}", None)
            if method is None:
                handler.send_error(501, f"Unsupported method ({request.method!r})")
            else:
                method()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            LOG.exception("request handler failed for %s %s", request.method, request.url)
        finally:
            output.finish()
            if output.socket is not None:
                try:
                    output.socket.close()
                except OSError:
                    pass
            if request.upgrade_handle is not None:
                with self.daemon_lock:
                    # Once stopped, libmicrohttpd has closed and freed it already.
                    if self.daemon is not None:
                        self.lib.MHD_upgrade_action(request.upgrade_handle, MHD_UPGRADE_ACTION_CLOSE)
                with self.requests_lock:
                    self.requests.pop(request.request_id, None)

    def _make_handler(self, request: _Request) -> Any:
        handler = object.__new__(self.handler_class)
        handler.server = self
        handler.client_address = request.client_address
        handler.request = handler.connection = request.connection_options
        handler.rfile = io.BytesIO(bytes(request.body))
        handler.wfile = request.output
        handler.command = request.method
        handler.request_version = request.version
        handler.path = quote(request.url, safe="/%:@!$&'()*+,;=-._~") + (f"?{request.query}" if request.query else "")
        handler.requestline = f"{request.method} {handler.path} {request.version}"
        handler.raw_requestline = (handler.requestline + "\r\n").encode("latin-1")
        handler.headers = http.client.parse_headers(io.BytesIO(request.header_block))
        handler.close_connection = True
        return handler

    def _values(self, connection: int, kind: int) -> list[tuple[str, str | None]]:
        values: list[tuple[str, str | None]] = []

        def collect(_cls, _kind, key, value) -> int:
            values.append((key.decode("latin-1"), None if value is None else value.decode("latin-1")))
            return MHD_YES

        self.lib.MHD_get_connection_values(connection, kind, KeyValueIterator(collect), None)
        return values

    def _header_block(self, connection: int) -> bytes:
        lines = "".join(f"{key}: {value or ''}\r\n" for key, value in self._values(connection, MHD_HEADER_KIND))
        return (lines + "\r\n").encode("latin-1")

    def _query(self, connection: int) -> str:
        return "&".join(
            quote_plus(key) if value is None else f"{quote_plus(key)}={quote_plus(value)}"
            for key, value in self._values(connection, MHD_GET_ARGUMENT_KIND)
        )

    def _client_address(self, connection: int) -> tuple[str, int]:
        info = self.lib.MHD_get_connection_info(connection, MHD_CONNECTION_INFO_CLIENT_ADDRESS)
        if not info or not info[0]:
            return ("", 0)
        address = info[0]
        family = ctypes.c_ushort.from_address(address).value
        port = int.from_bytes(ctypes.string_at(address + 2, 2), "big")
        if family == socket.AF_INET:
            return (socket.inet_ntop(socket.AF_INET, ctypes.string_at(address + 4, 4)), port)
        if family == socket.AF_INET6:
            return (socket.inet_ntop(socket.AF_INET6, ctypes.string_at(address + 8, 16)), port)
        return ("", 0)


__all__ = ["MicrohttpdError", "MicrohttpdServer", "load_libmicrohttpd"]
