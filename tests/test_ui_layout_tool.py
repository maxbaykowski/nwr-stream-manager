from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path
import sys
import unittest


def load_layout_tool():
    path = Path(__file__).resolve().parents[1] / "tools" / "check_ui_layout.py"
    spec = importlib.util.spec_from_file_location("check_ui_layout", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UiLayoutToolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = load_layout_tool()

    def test_parse_viewport(self) -> None:
        viewport = self.tool.parse_viewport("iphone:393x852")
        self.assertEqual(viewport.name, "iphone")
        self.assertEqual(viewport.width, 393)
        self.assertEqual(viewport.height, 852)

    def test_parse_viewport_rejects_invalid_format(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            self.tool.parse_viewport("393x852")

    def test_route_url_for_dashboard_uses_root(self) -> None:
        self.assertEqual(self.tool.route_url("http://127.0.0.1:8080/", "dashboard"), "http://127.0.0.1:8080/")

    def test_route_url_for_view_uses_query_parameter(self) -> None:
        self.assertEqual(self.tool.route_url("http://127.0.0.1:8080", "receiver"), "http://127.0.0.1:8080/?view=receiver")

    def test_route_url_for_parameterized_view(self) -> None:
        self.assertEqual(
            self.tool.route_url("http://127.0.0.1:8080", "stream_settings", {"stream": "abc"}),
            "http://127.0.0.1:8080/?view=stream_settings&stream=abc",
        )

    def test_api_url(self) -> None:
        self.assertEqual(
            self.tool.api_url("http://127.0.0.1:8080/root", "/api/streams"),
            "http://127.0.0.1:8080/root/api/streams",
        )

    def test_safe_screenshot_name(self) -> None:
        self.assertEqual(self.tool.safe_screenshot_name("phone/portrait", "rtl"), "phone-portrait-rtl.png")

    def test_scenario_filter_matches_name_or_view(self) -> None:
        scenario = self.tool.LayoutScenario("stream-WXN99-settings-audio", "stream_settings", {"stream": "1"}, "audio")
        self.assertTrue(self.tool.scenario_matches_filters(scenario, ["stream-WXN99-settings-audio"]))
        self.assertTrue(self.tool.scenario_matches_filters(scenario, ["stream_settings"]))
        self.assertFalse(self.tool.scenario_matches_filters(scenario, ["receiver"]))

    def test_visible_view_id(self) -> None:
        self.assertEqual(self.tool.visible_view_id("dashboard"), "view_dashboard")
        self.assertEqual(self.tool.visible_view_id("stream_settings"), "view_stream_settings")

    def test_page_load_issues_reports_authentication_required(self) -> None:
        response = type("Response", (), {"status": 401})()
        page = object()
        scenario = self.tool.LayoutScenario("receiver", "receiver", {})
        issues = self.tool.page_load_issues(page, response, self.tool.Viewport("phone", 393, 852), scenario)
        self.assertEqual(len(issues), 1)
        self.assertIn("authentication required", issues[0].message)

    def test_page_load_issues_reports_http_error(self) -> None:
        response = type("Response", (), {"status": 500})()
        page = object()
        scenario = self.tool.LayoutScenario("receiver", "receiver", {})
        issues = self.tool.page_load_issues(page, response, self.tool.Viewport("phone", 393, 852), scenario)
        self.assertEqual(len(issues), 1)
        self.assertIn("HTTP 500", issues[0].message)


if __name__ == "__main__":
    unittest.main()
