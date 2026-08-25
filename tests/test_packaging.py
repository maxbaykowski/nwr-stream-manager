from __future__ import annotations

import importlib.util
import sys
import tomllib
import types
import unittest
from pathlib import Path
from unittest.mock import patch


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


class PackagingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_pyrtlsdrlib_is_x86_64_only(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        dependencies = project["dependencies"]
        pyrtlsdrlib = [dependency for dependency in dependencies if dependency.startswith("pyrtlsdrlib")]

        self.assertEqual(len(pyrtlsdrlib), 1)
        self.assertIn("platform_machine == 'x86_64'", pyrtlsdrlib[0])
        self.assertIn("platform_machine == 'AMD64'", pyrtlsdrlib[0])

    def test_pyrtlsdr_python_wrapper_remains_unconditional(self) -> None:
        project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]

        self.assertIn("pyrtlsdr", project["dependencies"])

    def test_state_directory_environment_sets_default_state_path(self) -> None:
        with patch.dict("os.environ", {"STATE_DIRECTORY": "/var/lib/nwr-stream-manager"}, clear=True):
            self.assertEqual(
                self.web_control.default_state_path(),
                Path("/var/lib/nwr-stream-manager/rtl-control.json"),
            )

    def test_state_directory_uses_first_systemd_path(self) -> None:
        with patch.dict("os.environ", {"STATE_DIRECTORY": "/var/lib/one:/var/lib/two"}, clear=True):
            self.assertEqual(
                self.web_control.default_state_path(),
                Path("/var/lib/one/rtl-control.json"),
            )

    def test_logs_directory_environment_sets_default_log_path(self) -> None:
        state_path = Path("/var/lib/nwr-stream-manager/rtl-control.json")
        with patch.dict("os.environ", {"LOGS_DIRECTORY": "/var/log/nwr-stream-manager"}, clear=True):
            self.assertEqual(
                self.web_control.default_log_path(state_path),
                Path("/var/log/nwr-stream-manager/nwr-stream-manager.log"),
            )

    def test_explicit_state_and_log_file_arguments_override_defaults(self) -> None:
        parser = self.web_control.build_parser()
        args = parser.parse_args(
            [
                "--state",
                "/tmp/custom-state.json",
                "--log-file",
                "/tmp/custom.log",
            ]
        )

        self.assertEqual(args.state, Path("/tmp/custom-state.json"))
        self.assertEqual(args.log_file, Path("/tmp/custom.log"))

    def test_systemd_unit_uses_dedicated_user_and_managed_directories(self) -> None:
        unit = (REPO_ROOT / "packaging/systemd/nwr-stream-manager.service").read_text(encoding="utf-8")

        self.assertIn("User=nwr-stream-manager", unit)
        self.assertIn("StateDirectory=nwr-stream-manager", unit)
        self.assertIn("LogsDirectory=nwr-stream-manager", unit)
        self.assertIn("SupplementaryGroups=plugdev audio", unit)
        self.assertIn("ExecStart=/usr/local/bin/nwr-stream-manager --host 0.0.0.0 --port 8080", unit)

    def test_systemd_unit_is_included_in_source_distribution_manifest(self) -> None:
        manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("include packaging/systemd/*.service", manifest)

    def test_iq_download_does_not_navigate_management_page(self) -> None:
        script = self.web_control.INDEX_HTML
        start = script.index("async function downloadSelectedIqRecording()")
        end = script.index("function updateDashboard", start)
        download_function = script[start:end]

        self.assertIn("startBackgroundDownload", download_function)
        self.assertNotIn("window.location.href", download_function)

    def test_browser_routes_preserve_deep_link_ids(self) -> None:
        script = self.web_control.INDEX_HTML

        self.assertIn("outputId: initialRoute.outputId", script)
        self.assertIn("outputId: link.dataset.outputId || \"\"", script)
        self.assertIn("recordingId: link.dataset.recordingId || \"\"", script)
        self.assertIn("function replaceCurrentRoute", script)

    def test_iq_recorder_defaults_and_preferences_are_in_ui(self) -> None:
        script = self.web_control.INDEX_HTML

        self.assertIn('id="iq_duration_minutes" type="number" min="0" max="1440" step="1" value="0"', script)
        self.assertIn("const IQ_RECORDER_DEFAULT_SAMPLE_RATE = 192000", script)
        self.assertIn("const IQ_RECORDER_DEFAULT_DURATION_MINUTES = 0", script)
        self.assertIn("nwr-stream-manager:iq-recorder-preferences", script)
        self.assertIn("function restoreIqRecorderPreferences", script)
        self.assertIn("rememberIqRecorderPreferences();", script)


if __name__ == "__main__":
    unittest.main()
