from __future__ import annotations

import contextlib
import io
import logging
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_auth import load_web_control_module


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()
        cls.web_address = sys.modules["nwr_stream_manager.web_address"]

    def start_against_taken_port(self, state: str, *args: str) -> list[str]:
        """Starts with a port held open, so the error says which port and address it chose."""
        root = logging.getLogger()
        handlers = list(root.handlers)
        try:
            with mock.patch.object(self.wc, "check_startup_dependencies", return_value=[]), mock.patch.object(
                self.wc, "RtlControlService", side_effect=AssertionError("the service started")
            ), self.assertLogs("nwr_stream_manager", level="INFO") as logs:
                result = self.wc.main(["--state", str(Path(state) / "rtl-control.json"), *args])
        finally:
            for handler in root.handlers[len(handlers):]:
                root.removeHandler(handler)
                handler.close()
        self.assertEqual(result, 1)
        return logs.output

    def test_the_saved_port_is_used_unless_the_command_line_gives_one(self) -> None:
        with socket.socket() as taken, tempfile.TemporaryDirectory() as state:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            path = self.web_address.web_address_state_path(Path(state) / "rtl-control.json")
            self.web_address.save_web_address_settings(path, self.web_address.WebAddressSettings(port=port, localhost_only=True))
            logs = self.start_against_taken_port(state)
            self.assertIn(f"port {port} is already in use", logs[-1])
            self.assertFalse(any("was given" in line for line in logs))

            self.web_address.save_web_address_settings(path, self.web_address.WebAddressSettings(port=1, localhost_only=False))
            logs = self.start_against_taken_port(state, "--port", str(port), "--localhost-only")
            self.assertIn(f"port {port} is already in use", logs[-1])
            self.assertTrue(any("--port " + str(port) + " was given" in line for line in logs))
            self.assertTrue(any("--localhost-only was given" in line for line in logs))

    def test_the_command_line_chooses_one_address_at_most(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as context:
            self.wc.build_parser().parse_args(["--localhost-only", "--bind-to-all-interfaces"])
        self.assertEqual(context.exception.code, 2)
        with contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit):
            self.wc.build_parser().parse_args(["--port", "70000"])
        self.assertIn("isn't a port number from 1 to 65535", error.getvalue())

    def test_the_command_line_overrides_the_saved_settings(self) -> None:
        resolve = self.web_address.resolve_web_address
        saved = self.web_address.WebAddressSettings(port=9090, localhost_only=True)
        self.assertEqual(resolve(saved), self.web_address.WebAddress("127.0.0.1", 9090))
        self.assertEqual(
            resolve(saved, bind_to_all_interfaces=True, port=8080),
            self.web_address.WebAddress("0.0.0.0", 8080, "--bind-to-all-interfaces", "--port 8080"),
        )
        self.assertEqual(resolve(saved, host="192.168.1.5").host_flag, "--host 192.168.1.5")
        self.assertEqual(resolve(self.web_address.WebAddressSettings(), localhost_only=True).host, "127.0.0.1")

    def test_damaged_saved_settings_fall_back_to_the_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            path = Path(state) / "web-address.json"
            for text in ("not json", '{"port": 0}', '{"localhost_only": "yes"}', "[]"):
                path.write_text(text, encoding="utf-8")
                with self.assertLogs("nwr_stream_manager", level="WARNING"):
                    self.assertEqual(self.web_address.load_web_address_settings(path), self.web_address.WebAddressSettings())

    def test_port_in_use_is_explained_without_starting_anything(self) -> None:
        with socket.socket() as taken, tempfile.TemporaryDirectory() as state:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            root = logging.getLogger()
            handlers = list(root.handlers)
            try:
                with mock.patch.object(self.wc, "check_startup_dependencies", return_value=[]), mock.patch.object(
                    self.wc, "RtlControlService", side_effect=AssertionError("the service started")
                ), self.assertLogs("nwr_stream_manager", level="ERROR") as logs:
                    result = self.wc.main(
                        ["--host", "127.0.0.1", "--port", str(port), "--state", str(Path(state) / "rtl-control.json")]
                    )
            finally:
                for handler in root.handlers[len(handlers):]:
                    root.removeHandler(handler)
                    handler.close()

        self.assertEqual(result, 1)
        self.assertEqual(len(logs.records), 1)
        self.assertIn(f"port {port} is already in use by another program", logs.output[0])
        self.assertIsNone(logs.records[0].exc_info)


if __name__ == "__main__":
    unittest.main()
