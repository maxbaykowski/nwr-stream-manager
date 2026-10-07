from __future__ import annotations

import importlib.util
import socket
import sys
import threading
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_web_control_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    spec = importlib.util.spec_from_file_location(
        "nwr_stream_manager.web_control",
        PACKAGE_PATH / "web_control.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.web_control"] = module
    spec.loader.exec_module(module)
    return module


class OldIcecastServer:
    """Stands in for an Icecast server older than 2.4, like Broadcastify's ingest: it
    closes the connection without answering PUT, and accepts SOURCE."""

    def __init__(self) -> None:
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen()
        self.port = self.server.getsockname()[1]
        self.requests: list[str] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            with client:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = client.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request_line = data.split(b"\r\n", 1)[0].decode()
                self.requests.append(request_line)
                if request_line.startswith("SOURCE "):
                    client.sendall(b"HTTP/1.0 200 OK\r\n\r\n")

    def close(self) -> None:
        self.server.close()


class IcecastAddressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()
        cls.icecast = sys.modules["nwr_stream_manager.icecast"]

    def parse(self, server: str, port: int = 8000, mount: str = "/live"):
        return self.icecast.parse_icecast_address(server, port, mount)

    def test_accepts_a_host_host_and_port_or_a_web_address(self) -> None:
        cases = {
            "audio1.broadcastify.com": ("audio1.broadcastify.com", 8000, "/live", False),
            "  audio1.broadcastify.com  ": ("audio1.broadcastify.com", 8000, "/live", False),
            "audio1.broadcastify.com:80": ("audio1.broadcastify.com", 80, "/live", False),
            "http://audio1.broadcastify.com": ("audio1.broadcastify.com", 8000, "/live", False),
            "http://audio1.broadcastify.com:80/": ("audio1.broadcastify.com", 80, "/live", False),
            "HTTPS://Stream.Example.com": ("stream.example.com", 8000, "/live", True),
            "https://stream.example.com:8443": ("stream.example.com", 8443, "/live", True),
            "192.168.1.20": ("192.168.1.20", 8000, "/live", False),
            "http://192.168.1.20:8000/live": ("192.168.1.20", 8000, "/live", False),
        }
        for server, expected in cases.items():
            with self.subTest(server=server):
                address = self.parse(server)
                self.assertEqual((address.host, address.port, address.mount, address.tls), expected)

    def test_takes_the_mountpoint_from_the_address_when_the_field_is_empty(self) -> None:
        address = self.parse("http://audio1.broadcastify.com:80/abc123", 8000, "")
        self.assertEqual((address.host, address.port, address.mount), ("audio1.broadcastify.com", 80, "/abc123"))

    def test_explains_what_is_wrong_with_an_address_it_cannot_use(self) -> None:
        cases = {
            ("http://host/one", "/two"): "different from the mountpoint",
            ("ftp://host", "/live"): "http:// or https://",
            ("http://source:secret@host", "/live"): "username and password in their own fields",
            ("http://host/live?x=1", "/live"): "cannot contain",
            ("http://host:99999", "/live"): "port",
            ("   ", "/live"): "host is required",
        }
        for (server, mount), message in cases.items():
            with self.subTest(server=server):
                with self.assertRaisesRegex(ValueError, message):
                    self.parse(server, 8000, mount)

    def test_saving_settings_stores_a_clean_host_and_remembers_tls(self) -> None:
        base = {
            "service": "custom",
            "port": 80,
            "username": "source",
            "password": "secret",
            "mount": "/live",
            "format": "mp3",
            "sample_rate": 22050,
            "bitrate": 32,
        }
        plain = self.web_control.validate_icecast_payload(dict(base, host="http://audio1.broadcastify.com"))
        secure = self.web_control.validate_icecast_payload(dict(base, host="https://stream.example.com:8443"))

        self.assertEqual((plain["host"], plain["port"], plain["tls"]), ("audio1.broadcastify.com", 80, False))
        self.assertEqual((secure["host"], secure["port"], secure["tls"]), ("stream.example.com", 8443, True))

    def test_known_services_are_recognised_even_when_typed_as_a_web_address(self) -> None:
        with self.assertRaisesRegex(ValueError, "Please select NOAA Weather Radio Org"):
            self.web_control.validate_icecast_payload(
                {
                    "service": "custom",
                    "host": "http://wxradio.org:8000/",
                    "port": 8000,
                    "username": "source",
                    "password": "secret",
                    "mount": "/live",
                    "format": "mp3",
                }
            )

    def test_settings_saved_with_a_web_address_still_connect_and_report_correctly(self) -> None:
        # Saved before addresses were cleaned on save, as on a real install.
        output = {
            "id": "out-1",
            "enabled": True,
            "type": "icecast",
            "icecast": {
                "host": "http://audio1.broadcastify.com",
                "port": 80,
                "mount": "/abc123",
                "username": "source",
                "password": "secret",
                "format": "mp3",
                "sample_rate": 22050,
                "bitrate": 32,
            },
        }
        config = self.web_control.icecast_config_from_output(output)
        url = self.web_control.icecast_mountpoint_url(output["icecast"])
        auth = self.web_control.icecast_auth_settings(output["icecast"])

        self.assertEqual((config.host, config.port, config.mount, config.tls), ("audio1.broadcastify.com", 80, "/abc123", False))
        self.assertEqual(url, "http://audio1.broadcastify.com:80/abc123")
        self.assertEqual(auth.server, "http://audio1.broadcastify.com")

    def test_tls_settings_reach_the_live_connection_and_the_auth_test(self) -> None:
        icecast = {
            "host": "stream.example.com",
            "port": 8443,
            "tls": True,
            "mount": "/live",
            "username": "source",
            "password": "secret",
            "format": "mp3",
        }
        config = self.web_control.icecast_config_from_output({"icecast": icecast})

        self.assertTrue(config.tls)
        self.assertEqual(self.web_control.icecast_auth_settings(icecast).server, "https://stream.example.com")
        self.assertEqual(self.web_control.icecast_mountpoint_url(icecast), "https://stream.example.com:8443/live")

    def test_live_stream_connects_with_source_which_older_icecast_servers_accept(self) -> None:
        server = OldIcecastServer()
        try:
            config = self.web_control.IcecastConfig(
                host="127.0.0.1",
                port=server.port,
                mount="/live",
                username="source",
                password="secret",
                format="mp3",
                sample_rate=22050,
                bitrate=32,
            )
            source = self.web_control.IcecastSource(config, "audio/mpeg", timeout_seconds=5)
            source.connect()
            source.close()
        finally:
            server.close()

        self.assertEqual(server.requests, ["SOURCE /live HTTP/1.0"])
        self.assertEqual(source.response_status_code, 200)


if __name__ == "__main__":
    unittest.main()
