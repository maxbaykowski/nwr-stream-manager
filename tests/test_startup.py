from __future__ import annotations

import logging
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_auth import load_web_control_module


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control_module()

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
