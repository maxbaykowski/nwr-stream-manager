from __future__ import annotations

import base64
import http.client
import importlib
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler
from unittest import mock

from test_auth import load_web_control_module


def load_server_module():
    load_web_control_module()
    return importlib.import_module("nwr_stream_manager.microhttpd_server")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: dict = {}
    finished = threading.Event()

    def log_message(self, format, *args) -> None:
        pass

    def send_body(self, body: bytes, content_type: str = "application/json", length: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/stream"):
            # Long, and sent a piece at a time, with and without a length given first.
            chunk = b"x" * 65536
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            if "length" in self.path:
                self.send_header("Content-Length", str(len(chunk) * 80))
            self.end_headers()
            try:
                for _ in range(80):
                    self.wfile.write(chunk)
            except BrokenPipeError:
                Handler.seen["stream_stopped"] = True
            finally:
                Handler.finished.set()
            return
        if self.path == "/ws":
            key = self.headers["Sec-WebSocket-Key"]
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(key.encode()).decode())
            self.end_headers()
            try:
                for number in range(1000):
                    self.wfile.write(bytes([0x82, 4]) + number.to_bytes(4, "big"))
                    time.sleep(0.01)
            except OSError:
                Handler.seen["ws_stopped"] = True
            finally:
                Handler.finished.set()
            return
        Handler.seen.update(
            path=self.path, client=self.client_address, agent=self.headers.get("User-Agent"), proto=self.headers.get("X-Forwarded-Proto")
        )
        self.send_body(json.dumps({"ok": True}).encode())

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Handler.seen["body"] = body
        self.send_body(body, "text/plain")


class MicrohttpdServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.mhd = load_server_module()
            cls.mhd.load_libmicrohttpd()
        except Exception as exc:  # pragma: no cover - only without libmicrohttpd
            raise unittest.SkipTest(f"libmicrohttpd isn't available: {exc}")

    def start(self):
        Handler.seen = {}
        Handler.finished = threading.Event()
        server = self.mhd.MicrohttpdServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while server.daemon is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.addCleanup(server.server_close)
        self.port = server.server_address[1]
        return server

    def connection(self) -> http.client.HTTPConnection:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(connection.close)
        return connection

    def test_a_request_and_its_answer(self) -> None:
        self.start()
        connection = self.connection()
        connection.request("GET", "/api/status?stream=WXN%2099&x=1&x=2", headers={"User-Agent": "test"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"ok": True})
        self.assertEqual(response.getheader("Content-Type"), "application/json")
        self.assertEqual(Handler.seen["path"], "/api/status?stream=WXN+99&x=1&x=2")
        self.assertEqual(Handler.seen["client"][0], "127.0.0.1")
        self.assertEqual(Handler.seen["agent"], "test")
        # The connection stays open for the next request.
        connection.request("POST", "/echo", body=b"hello there", headers={"Content-Type": "text/plain"})
        response = connection.getresponse()
        self.assertEqual(response.read(), b"hello there")

    def test_a_connection_accepted_elsewhere_is_served_as_from_its_own_address(self) -> None:
        # As a connection from a tailnet is: handed over, with where it really came from.
        server = self.start()
        for address in ("100.101.102.103", "fd7a:115c:a1e0::1"):
            with self.subTest(address=address):
                ours, theirs = socket.socketpair()
                self.addCleanup(ours.close)
                server.add_connection(theirs.detach(), address)
                ours.sendall(b"GET /from-the-tailnet HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
                ours.settimeout(10)
                answer = b""
                while chunk := ours.recv(65536):
                    answer += chunk
                self.assertTrue(answer.startswith(b"HTTP/1.1 200"), answer[:80])
                self.assertEqual(Handler.seen["client"][0], address)

    def test_https_for_connections_handed_over_from_the_tailnet(self) -> None:
        import ssl
        import subprocess
        import tempfile
        from pathlib import Path

        folder = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        try:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "1",
                 "-subj", "/CN=nwr-stream-manager.example.ts.net", "-keyout", str(folder / "key.pem"), "-out", str(folder / "cert.pem")],
                check=True, capture_output=True,
            )
        except (FileNotFoundError, subprocess.CalledProcessError) as exc:
            self.skipTest(f"openssl couldn't make a certificate: {exc}")
        server = self.start()
        with self.assertRaisesRegex(self.mhd.MicrohttpdError, "HTTPS isn't ready"):
            ours, theirs = socket.socketpair()
            self.addCleanup(ours.close)
            server.add_connection(theirs.detach(), "100.64.0.1", tls=True)
        server.start_tls((folder / "cert.pem").read_bytes(), (folder / "key.pem").read_bytes())
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        ours, theirs = socket.socketpair()
        server.add_connection(theirs.detach(), "100.64.0.1", tls=True)
        with context.wrap_socket(ours) as secure:
            # Claimed otherwise, it's still HTTPS.
            secure.sendall(b"GET /secure HTTP/1.1\r\nHost: x\r\nX-Forwarded-Proto: http\r\nConnection: close\r\n\r\n")
            answer = b""
            while chunk := secure.recv(65536):
                answer += chunk
        self.assertTrue(answer.startswith(b"HTTP/1.1 200"), answer[:80])
        self.assertEqual((Handler.seen["path"], Handler.seen["client"][0], Handler.seen["proto"]), ("/secure", "100.64.0.1", "https"))
        # Plain HTTP is still plain.
        connection = self.connection()
        connection.request("GET", "/plain")
        connection.getresponse().read()
        self.assertIsNone(Handler.seen["proto"])

    def test_long_answers_are_sent_a_piece_at_a_time(self) -> None:
        self.start()
        for path in ("/stream?length", "/stream"):
            with self.subTest(path=path):
                connection = self.connection()
                connection.request("GET", path)
                response = connection.getresponse()
                self.assertEqual(len(response.read()), 80 * 65536)

    def test_a_client_that_leaves_partway_stops_the_handler(self) -> None:
        self.start()
        sock = socket.create_connection(("127.0.0.1", self.port))
        sock.sendall(b"GET /stream?length HTTP/1.1\r\nHost: x\r\n\r\n")
        sock.recv(1000)
        sock.close()
        self.assertTrue(Handler.finished.wait(10))
        self.assertTrue(Handler.seen.get("stream_stopped"))

    def test_a_websocket_takes_over_the_connection(self) -> None:
        self.start()
        sock = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(sock.close)
        sock.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: abc\r\n\r\n")
        sock.settimeout(5)
        received = b""
        while received.count(b"\x82\x04") < 5:
            received += sock.recv(4096)
        head = received.split(b"\r\n\r\n")[0].decode()
        self.assertTrue(head.startswith("HTTP/1.1 101"))
        # Exactly one: "Upgrade, Upgrade" is refused by Safari.
        self.assertEqual([line for line in head.split("\r\n") if line.lower().startswith("connection:")], ["Connection: Upgrade"])
        self.assertIn("Sec-WebSocket-Accept: YWJj", head)
        sock.close()
        self.assertTrue(Handler.finished.wait(10))
        self.assertTrue(Handler.seen.get("ws_stopped"))

    def test_stopping_the_server_ends_an_open_websocket(self) -> None:
        server = self.start()
        sock = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(sock.close)
        sock.sendall(b"GET /ws HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: abc\r\n\r\n")
        sock.settimeout(5)
        sock.recv(4096)
        started = time.monotonic()
        server.shutdown()
        server.server_close()
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(Handler.finished.wait(5))

    def test_a_connection_that_goes_quiet_is_closed(self) -> None:
        with mock.patch.object(self.mhd, "CONNECTION_TIMEOUT_SECONDS", 1):
            self.start()
        sock = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(sock.close)
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        sock.settimeout(10)
        started = time.monotonic()
        self.assertEqual(sock.recv(100), b"")
        self.assertLess(time.monotonic() - started, 8)

    def test_a_body_too_large_is_refused(self) -> None:
        with mock.patch.object(self.mhd, "MAX_REQUEST_BODY_BYTES", 1000):
            self.start()
            connection = self.connection()
            connection.request("POST", "/echo", body=b"x" * 5000)
            response = connection.getresponse()
            response.read()
        self.assertEqual(response.status, 413)
        self.assertNotIn("body", Handler.seen)

    def test_a_port_in_use_is_reported_as_it_was(self) -> None:
        self.start()
        with self.assertRaises(OSError):
            self.mhd.MicrohttpdServer(("127.0.0.1", self.port), Handler)


if __name__ == "__main__":
    unittest.main()
