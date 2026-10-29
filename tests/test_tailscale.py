from __future__ import annotations

import importlib
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from test_auth import load_web_control_module


class TailscaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        load_web_control_module()
        cls.node_module = importlib.import_module("nwr_stream_manager.tailscale_node")
        cls.service_module = importlib.import_module("nwr_stream_manager.service_tailscale")
        cls.pages = importlib.import_module("nwr_stream_manager.page_sections")

    def setUp(self) -> None:
        # Whatever this computer has: Tailscale installed on it is tested where it's said to be.
        patcher = mock.patch.object(self.service_module, "system_tailscale_is_up", return_value=False)
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)

        class Service(self.service_module.TailscaleServiceMixin):
            def __init__(self):
                self.state_path = directory / "rtl-control.json"
                self.lock = threading.Lock()
                self._init_tailscale()

        return Service()

    def test_serve_headers_are_only_believed_while_serve_passes_on_web_requests(self) -> None:
        service = self.service()
        handed = []
        server = mock.Mock(add_connection=lambda fd, address, serve=False: (handed.append(serve), os.close(fd)))
        with mock.patch.object(self.service_module, "TailscaleServeSocket") as socket_class:
            socket_class.return_value.start.return_value = True
            service._start_tailscale_serve_socket(server)
        self.addCleanup(self.service_module.set_tailscale_serve_socket, "")
        hand_over = socket_class.call_args.args[1]

        def connect(use):
            reader, writer = os.pipe()
            os.close(reader)
            with mock.patch.object(self.service_module, "tailscale_serve_socket_use_now", return_value=use):
                hand_over(writer)
            # Closed, by the server or else by refusing it.
            with self.assertRaises(OSError):
                os.fstat(writer)

        connect("web")
        self.assertEqual(handed, [True])
        # Raw TCP: anyone could say they're on the tailnet, so nothing through it is taken.
        with self.assertLogs("nwr_stream_manager", level="WARNING") as logs:
            connect("tcp")
        self.assertEqual(handed, [True])
        self.assertIn("pass raw TCP", logs.output[0])
        # Not known: served, but as anyone else through a tunnel is, asked for a code.
        with self.assertLogs("nwr_stream_manager", level="WARNING"):
            connect(None)
        connect("")
        self.assertEqual(handed, [True, False, False])

    def test_without_the_library_it_can_not_be_connected(self) -> None:
        service = self.service()
        with mock.patch.object(self.service_module, "load_libtailscale", return_value=None):
            self.assertEqual(service.tailscale_status()["state"], "unavailable")
            with self.assertRaisesRegex(ValueError, "isn't installed"):
                service.connect_tailscale()
        # Not connected at the next start either.
        self.assertFalse(service._tailscale_enabled())

    def test_a_library_built_while_running_is_used_without_restarting(self) -> None:
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, directory, True)
        built = directory / "libtailscale.so"
        node = self.node_module
        with mock.patch.object(node, "LIBRARY_PLACES", (built,)), mock.patch.object(node, "_library", None), \
                mock.patch.object(node, "_library_error", ""), mock.patch.object(node.ctypes, "CDLL") as cdll:
            self.assertIsNone(node.load_libtailscale())
            self.assertEqual(node.libtailscale_error(), "libtailscale isn't installed")
            built.write_bytes(b"")
            self.assertIsNotNone(node.load_libtailscale())
            cdll.assert_called_once_with(str(built))

    def test_building_the_library_says_what_it_needs(self) -> None:
        build = importlib.import_module("nwr_stream_manager.tailscale_build")
        with mock.patch.object(build.shutil, "which", side_effect=lambda command: None if command in {"go", "cc", "gcc", "clang"} else "/usr/bin/git"), \
                mock.patch("builtins.print") as said, mock.patch.object(build.subprocess, "run") as run:
            self.assertEqual(build.build_libtailscale(), 1)
        run.assert_not_called()
        self.assertEqual(
            said.call_args.args[0],
            "Building libtailscale needs Go (golang-go on Debian and Raspberry Pi OS, golang on Fedora) and a C compiler (gcc). "
            "Install them, then run this again.",
        )
        self.assertTrue(build.BUILD_SCRIPT.is_file())

    def test_a_go_too_old_to_fetch_the_go_libtailscale_needs_is_said_to_be(self) -> None:
        build = importlib.import_module("nwr_stream_manager.tailscale_build")
        old = mock.Mock(stdout="go1.19.8\n")
        with mock.patch.object(build.shutil, "which", return_value="/usr/bin/tool"), mock.patch("builtins.print") as said, \
                mock.patch.object(build.subprocess, "run", return_value=old) as run:
            self.assertEqual(build.build_libtailscale(), 1)
        self.assertEqual(run.call_count, 1)  # asked its version, and built nothing
        self.assertIn("Go, version 1.19, is too old to build libtailscale", said.call_args.args[0])
        with mock.patch.object(build.subprocess, "run", return_value=mock.Mock(stdout="go1.26.10-X:nodwarf5\n")):
            self.assertEqual(build.go_version(), (1, 26))

    def test_connecting_is_remembered_and_signing_out_forgets_it(self) -> None:
        service = self.service()
        started, signed_out = [], []

        class Node:
            def __init__(self, lib, state_dir, hostname, on_connection):
                self.state_dir = state_dir
                self.closed = threading.Event()

            def start(self):
                self.state_dir.mkdir(parents=True)
                (self.state_dir / "tailscaled.state").write_text("keys")
                started.append(True)

            def snapshot(self):
                return {"state": "needs-login", "auth_url": "https://login.tailscale.com/a/abc", "name": "", "addresses": [], "account": "", "error": ""}

            def sign_out(self):
                signed_out.append(True)
                self.closed.set()

            def dial(self, host, port, timeout=None):
                return None

            def own_address(self):
                return ""

            def peer_addresses(self):
                return []

        with mock.patch.object(self.service_module, "load_libtailscale", return_value=object()), \
                mock.patch.object(self.service_module, "TailscaleNode", Node):
            status = service.connect_tailscale()
            self.assertEqual((status["state"], status["auth_url"]), ("needs-login", "https://login.tailscale.com/a/abc"))
            self.assertTrue(service._tailscale_enabled())
            service.connect_tailscale()
            self.assertEqual(started, [True])
            self.assertEqual(service.sign_out_tailscale()["state"], "off")
        self.assertEqual(signed_out, [True])
        self.assertFalse(service._tailscale_enabled())
        # Its keys are gone with it.
        self.assertFalse(service.tailscale_state_directory.exists())

    def test_with_tailscale_installed_on_the_computer_it_signs_itself_out_and_says_so(self) -> None:
        service = self.service()
        signed_out = []

        class Node:
            def sign_out(self):
                signed_out.append(True)

        service.tailscale_node = Node()
        service._save_tailscale_enabled(True)
        module = self.service_module
        with mock.patch.object(module, "system_tailscale_is_up", return_value=False):
            self.assertFalse(service._leave_tailnet_to_system_tailscale())
        self.assertEqual(signed_out, [])
        with mock.patch.object(module, "system_tailscale_is_up", return_value=True), \
                mock.patch.object(module, "load_libtailscale", return_value=object()):
            self.assertTrue(service._leave_tailnet_to_system_tailscale())
            self.assertEqual(signed_out, [True])
            self.assertIsNone(service.tailscale_node)
            self.assertFalse(service._tailscale_enabled())
            status = service.tailscale_status()
            self.assertEqual((status["state"], status["signed_out_for_system"]), ("system", True))
            self.assertIn("signed itself out", self.pages.tailscale_status_text(status))
            # Nothing more to sign out of, and connecting is refused while it's there.
            self.assertTrue(service._leave_tailnet_to_system_tailscale())
            self.assertEqual(signed_out, [True])
            with self.assertRaisesRegex(ValueError, "systemwide installation of Tailscale was detected"):
                service.connect_tailscale()
        # Found installed without ever having connected: just the message.
        fresh = self.service()
        with mock.patch.object(module, "system_tailscale_is_up", return_value=True):
            status = fresh.tailscale_status()
        self.assertEqual(
            self.pages.tailscale_status_text(status),
            "A systemwide installation of Tailscale was detected on this computer, so NWR Stream Manager uses it instead of connecting to Tailscale by itself.",
        )

    def test_a_secure_address_once_the_tailnet_has_https(self) -> None:
        import time

        service = self.service()
        module = self.service_module
        tls = []

        class Server:
            def start_tls(self, certificate, key):
                tls.append(("start", certificate, key))

            def stop_tls(self):
                tls.append(("stop",))

        class Node:
            closed = threading.Event()
            name = ""
            certificate_version = b"1"

            def snapshot(self):
                return {"state": "connected", "name": "nwr-stream-manager.example.ts.net"}

            def certificate_name(self):
                return self.name

            def https_enable_url(self):
                return "https://login.tailscale.com/f/serve?node=abc"

            def certificate(self):
                return b"CERT" + self.certificate_version, b"KEY"

            def can_funnel(self):
                return False

            def set_funnel(self, target):
                pass

        def wait_until(condition):
            deadline = time.monotonic() + 5
            while not condition() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(condition())

        node = Node()
        service.web_server = Server()
        service.tailscale_node = node
        self.addCleanup(node.closed.set)
        self.addCleanup(service.tailscale_https_wake.set)
        with mock.patch.object(module, "HTTPS_CHECK_SECONDS", 0.01), mock.patch.object(module, "CERTIFICATE_REFRESH_SECONDS", 3600):
            threading.Thread(target=service._tailscale_https_loop, args=(node,), daemon=True).start()
            wait_until(lambda: service.tailscale_https["state"] == "off")
            self.assertEqual(service.tailnet_https_name(), "")
            # Turned on, but the tailnet has HTTPS off: where to turn it on.
            service.set_tailscale_https(True)
            wait_until(lambda: service.tailscale_https["state"] == "needs-enabling")
            self.assertEqual(service.tailscale_https["enable_url"], "https://login.tailscale.com/f/serve?node=abc")
            self.assertEqual(tls, [])
            # Turned on for the tailnet: the certificate is used.
            node.name = "nwr-stream-manager.example.ts.net"
            wait_until(lambda: service.tailscale_https["state"] == "on")
            self.assertEqual(tls, [("start", b"CERT1", b"KEY")])
            self.assertEqual(service.tailnet_https_name(), "nwr-stream-manager.example.ts.net")
            # Asked for again later, the same certificate isn't started again; a renewed one is.
            with mock.patch.object(module, "CERTIFICATE_REFRESH_SECONDS", 0):
                service.tailscale_https_wake.set()
                time.sleep(0.1)
                self.assertEqual(len(tls), 1)
                node.certificate_version = b"2"
                wait_until(lambda: len(tls) == 2)
            self.assertEqual(tls[1], ("start", b"CERT2", b"KEY"))
            # Turned off: no more HTTPS, and plain HTTP stays plain.
            service.set_tailscale_https(False)
            wait_until(lambda: service.tailscale_https["state"] == "off")
            self.assertEqual(tls[-1], ("stop",))
            self.assertEqual(service.tailnet_https_name(), "")

    def test_funnel_permission_is_read_as_the_tailscale_command_reads_it(self) -> None:
        node = self.node_module.TailscaleNode(object(), Path("/nonexistent"), "nwr-stream-manager", {})

        def with_capabilities(*capabilities):
            node.status = {"BackendState": "Running", "Self": {"Capabilities": list(capabilities[:1]), "CapMap": {item: None for item in capabilities[1:]}}}
            return node.can_funnel()

        ports = "https://tailscale.com/cap/funnel-ports?ports=443,8443,10000"
        self.assertTrue(with_capabilities("https", "funnel", ports))
        self.assertTrue(with_capabilities("https", "funnel", "https://tailscale.com/cap/funnel-ports?ports=8000-9000"))
        self.assertFalse(with_capabilities("https", "funnel", "https://tailscale.com/cap/funnel-ports?ports=443"))
        self.assertFalse(with_capabilities("https", "funnel"))  # no ports allowed
        self.assertFalse(with_capabilities("https", ports))  # no funnel permission
        self.assertFalse(with_capabilities("funnel", "funnel", ports))  # no HTTPS

    def test_funnel_is_tailscales_web_proxy_on_8443_to_the_local_web_interface(self) -> None:
        import json

        node = self.node_module.TailscaleNode(object(), Path("/nonexistent"), "nwr-stream-manager", {})
        node.status = {"BackendState": "Running", "CertDomains": ["nwr-stream-manager.example.ts.net"]}
        sent = []
        node._local_api = lambda method, path, body=None, timeout=30.0: sent.append((method, path, json.loads(body))) or b""
        node.set_funnel("http://127.0.0.1:8080")
        node.set_funnel(None)
        name = "nwr-stream-manager.example.ts.net:8443"
        self.assertEqual(sent[0], ("POST", "serve-config", {
            "TCP": {"8443": {"HTTPS": True}},
            "Web": {name: {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}},
            "AllowFunnel": {name: True},
        }))
        self.assertEqual(sent[1], ("POST", "serve-config", {}))

    def test_funnel_opens_only_while_wanted_allowed_and_secure(self) -> None:
        service = self.service()
        service.web_port = 8080
        opened = []

        class Node:
            allowed = False

            def can_funnel(self):
                return self.allowed

            def funnel_enable_url(self):
                return "https://login.tailscale.com/f/funnel?node=abc"

            def set_funnel(self, target):
                opened.append(target)

            def certificate_name(self):
                return "nwr-stream-manager.example.ts.net"

        node, funnel = Node(), {"open": False, "checked": False, "url": "", "url_at": 0.0}
        step = service._tailscale_funnel_step
        # At first, whatever was saved from before is closed.
        step(node, True, funnel)
        self.assertEqual(opened, [None])
        step(node, True, funnel)
        self.assertEqual(opened, [None])
        with self.assertRaisesRegex(ValueError, "secure"):
            service.set_tailscale_funnel(True)
        service.set_tailscale_https(True)
        service.set_tailscale_funnel(True)
        step(node, True, funnel)
        self.assertEqual(service.tailscale_funnel["state"], "needs-permission")
        self.assertEqual(service.tailscale_funnel["enable_url"], "https://login.tailscale.com/f/funnel?node=abc")
        node.allowed = True
        step(node, True, funnel)
        self.assertEqual(opened[-1], "http://127.0.0.1:8080")
        self.assertEqual(service.tailscale_funnel["state"], "on")
        # Without the secure address, it closes, and the switch is off with it.
        service.set_tailscale_https(False)
        self.assertFalse(service._tailscale_funnel_wanted())
        step(node, False, funnel)
        self.assertEqual(opened[-1], None)
        self.assertEqual(service.tailscale_funnel["state"], "off")

    def test_what_the_node_says_about_itself(self) -> None:
        node = self.node_module.TailscaleNode(object(), Path("/nonexistent"), "nwr-stream-manager", {})
        node.status = {"BackendState": "NeedsLogin", "AuthURL": "https://login.tailscale.com/a/abc"}
        self.assertEqual(node.snapshot()["state"], "needs-login")
        self.assertEqual(node.snapshot()["auth_url"], "https://login.tailscale.com/a/abc")
        node.status = {
            "BackendState": "Running",
            "AuthURL": "",
            "Self": {"DNSName": "nwr-stream-manager.example.ts.net.", "TailscaleIPs": ["100.64.0.1"], "UserID": 7},
            "User": {"7": {"LoginName": "someone@example.com"}},
        }
        snapshot = node.snapshot()
        self.assertEqual(
            (snapshot["state"], snapshot["name"], snapshot["addresses"], snapshot["account"]),
            ("connected", "nwr-stream-manager.example.ts.net", ["100.64.0.1"], "someone@example.com"),
        )

    def test_servers_on_the_tailnet_are_found_by_name_or_address(self) -> None:
        node = self.node_module.TailscaleNode(object(), Path("/nonexistent"), "nwr-stream-manager", {})
        peer = {"HostName": "max-pi5", "DNSName": "max-pi5-1.example.ts.net.", "TailscaleIPs": ["100.64.0.9", "fd7a:115c:a1e0::9"]}
        node.status = {"BackendState": "Running", "Peer": {"key": peer}}
        for host in ("max-pi5", "MAX-PI5", "max-pi5-1", "max-pi5-1.example.ts.net", "max-pi5-1.example.ts.net."):
            self.assertEqual(node.tailnet_address(host), "100.64.0.9", host)
        self.assertEqual(node.tailnet_address("100.64.0.9"), "100.64.0.9")
        self.assertEqual(node.tailnet_address("[fd7a:115c:a1e0::9]"), "fd7a:115c:a1e0::9")
        # Anything else is reached as usual.
        for host in ("192.168.1.20", "icecast.example.com", "max-pi4", "100.64.0.10"):
            self.assertIsNone(node.tailnet_address(host), host)
        # And nothing goes through it until it's connected.
        node.status = {"BackendState": "NeedsLogin", "Peer": {"key": peer}}
        self.assertIsNone(node.tailnet_address("max-pi5"))

    def test_connections_go_through_the_tailnet_only_for_its_devices(self) -> None:
        import socket

        dial = importlib.import_module("nwr_stream_manager.network_dial")
        self.addCleanup(dial.set_tailnet_dialer, None)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        asked = []

        def dialer(host, port, timeout):
            asked.append((host, port))
            return theirs if host == "max-pi5" else None

        dial.set_tailnet_dialer(dialer)
        self.assertIs(dial.create_connection(("max-pi5", 8000), timeout=5), theirs)
        connection = dial.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(connection.close)
        self.assertEqual(connection.getpeername(), ("127.0.0.1", port))
        self.assertEqual(asked, [("max-pi5", 8000), ("127.0.0.1", port)])
        dial.set_tailnet_dialer(None)
        connection = dial.create_connection(("127.0.0.1", port), timeout=5)
        self.addCleanup(connection.close)
        self.assertEqual(len(asked), 2)

    def test_status_text(self) -> None:
        text = self.pages.tailscale_status_text
        self.assertEqual(text({"state": "off"}), "Not connected.")
        self.assertEqual(text({"state": "starting"}), "Connecting to Tailscale.")
        self.assertEqual(text({"state": "needs-login"}), "Waiting for you to sign in to Tailscale.")
        self.assertEqual(text({"state": "connected"}), "Connected to your tailnet.")
        self.assertEqual(text({"state": "error", "error": "no network"}), "Tailscale couldn't connect: no network.")


if __name__ == "__main__":
    unittest.main()
