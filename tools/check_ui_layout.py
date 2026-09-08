#!/usr/bin/env python3
"""Development-time browser layout checks for NWR Stream Manager.

This script intentionally stays out of the normal runtime dependency path.
Install the development extra and Playwright browser binaries before running it:

    python3 -m pip install -e '.[dev]'
    python3 -m playwright install chromium
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from urllib.parse import urlencode


DEFAULT_VIEWPORTS = (
    "iphone-se:375x667",
    "iphone-portrait:393x852",
    "android-portrait:412x915",
    "phone-landscape:852x393",
    "tablet:768x1024",
    "desktop:1280x800",
)

STREAM_SETTINGS_TABS = ("outputs", "eas", "fallback", "audio")
STATIC_SCENARIOS = (
    ("dashboard", "dashboard"),
    ("rtl", "rtl"),
    ("streams", "streams"),
    ("add-stream", "add_stream"),
    ("receiver", "receiver"),
    ("iq-recorder", "iq_recorder"),
    ("new-iq-recording", "iq_recorder_start"),
    ("logs", "logs"),
    ("eas-alerts", "eas_alerts"),
    ("eas-alert-export", "eas_alert_export"),
    ("eas-alert-delete", "eas_alert_delete"),
)


@dataclass(frozen=True)
class Viewport:
    name: str
    width: int
    height: int


@dataclass(frozen=True)
class LayoutIssue:
    viewport: str
    scenario: str
    message: str


@dataclass(frozen=True)
class LayoutScenario:
    name: str
    view: str
    params: dict[str, str]
    tab: str = ""


def parse_viewport(value: str) -> Viewport:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+):([1-9][0-9]*)x([1-9][0-9]*)", value.strip())
    if not match:
        raise argparse.ArgumentTypeError("viewport must use name:WIDTHxHEIGHT, for example iphone:393x852")
    return Viewport(match.group(1), int(match.group(2)), int(match.group(3)))


def route_url(base_url: str, view: str, params: dict[str, str] | None = None) -> str:
    base = base_url.rstrip("/")
    if view == "dashboard":
        return f"{base}/"
    query = {"view": view}
    query.update(params or {})
    return f"{base}/?{urlencode(query)}"


def safe_screenshot_name(viewport: str, view: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{viewport}-{view}")
    return f"{clean}.png"


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check NWR Stream Manager layouts in common browser viewports.")
    parser.add_argument("--url", default=os.environ.get("NWRSM_LAYOUT_URL", "http://127.0.0.1:8080"), help="Base web UI URL.")
    parser.add_argument("--username", default=os.environ.get("NWRSM_LAYOUT_USER"), help="HTTP auth username.")
    parser.add_argument("--password", default=os.environ.get("NWRSM_LAYOUT_PASSWORD"), help="HTTP auth password.")
    parser.add_argument(
        "--view",
        dest="views",
        action="append",
        help="Scenario or base view to check. May be repeated. Defaults to all static and discovered scenarios.",
    )
    parser.add_argument(
        "--viewport",
        dest="viewports",
        type=parse_viewport,
        action="append",
        help="Viewport to check as name:WIDTHxHEIGHT. May be repeated.",
    )
    parser.add_argument("--min-target-size", type=float, default=40.0, help="Minimum width and height for visible controls.")
    parser.add_argument("--screenshots", type=Path, help="Directory to write screenshots for each checked view.")
    return parser.parse_args(list(argv))


def browser_context_options(args: argparse.Namespace, viewport: Viewport) -> dict:
    options: dict = {"viewport": {"width": viewport.width, "height": viewport.height}}
    if args.username and args.password:
        options["http_credentials"] = {"username": args.username, "password": args.password}
    return options


def static_scenarios() -> list[LayoutScenario]:
    return [LayoutScenario(name, view, {}) for name, view in STATIC_SCENARIOS]


def stream_label(stream: dict[str, Any]) -> str:
    station = stream.get("station") if isinstance(stream.get("station"), dict) else {}
    label = str(station.get("callsign") or stream.get("id") or "stream")
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", label)


def first_output_id(stream: dict[str, Any]) -> str:
    outputs = stream.get("outputs") if isinstance(stream.get("outputs"), list) else []
    for output in outputs:
        if isinstance(output, dict) and output.get("id"):
            return str(output["id"])
    return ""


def api_url(base_url: str, path: str) -> str:
    return urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))


def api_json(request, base_url: str, path: str) -> Any:
    response = request.get(api_url(base_url, path))
    if response.status != 200:
        return None
    try:
        return response.json()
    except Exception:
        return None


def discover_scenarios(request, base_url: str) -> list[LayoutScenario]:
    scenarios: list[LayoutScenario] = []
    streams_payload = api_json(request, base_url, "/api/streams")
    streams = streams_payload.get("streams", []) if isinstance(streams_payload, dict) else []
    for stream in streams:
        if not isinstance(stream, dict) or not stream.get("id"):
            continue
        stream_id = str(stream["id"])
        label = stream_label(stream)
        for tab in STREAM_SETTINGS_TABS:
            scenarios.append(
                LayoutScenario(
                    f"stream-{label}-settings-{tab}",
                    "stream_settings",
                    {"stream": stream_id},
                    tab,
                )
            )
        output_id = first_output_id(stream)
        if output_id:
            scenarios.append(
                LayoutScenario(
                    f"stream-{label}-output",
                    "stream_output",
                    {"stream": stream_id, "output": output_id},
                )
            )

    eas_streams_payload = api_json(request, base_url, "/api/eas-alert-streams")
    eas_streams = eas_streams_payload.get("streams", []) if isinstance(eas_streams_payload, dict) else []
    for stream in eas_streams:
        if not isinstance(stream, dict) or not stream.get("id"):
            continue
        stream_id = str(stream["id"])
        label = stream_label(stream)
        scenarios.append(LayoutScenario(f"eas-alerts-{label}", "eas_alerts", {"stream": stream_id, "page": "1"}))
        alerts_payload = api_json(request, base_url, f"/api/eas-alerts?{urlencode({'stream_id': stream_id, 'page': 1})}")
        alerts = alerts_payload.get("alerts", []) if isinstance(alerts_payload, dict) else []
        for alert in alerts[:1]:
            if isinstance(alert, dict) and alert.get("id"):
                scenarios.append(
                    LayoutScenario(
                        f"eas-alert-{label}-{alert['id']}",
                        "eas_alert_detail",
                        {"stream": stream_id, "alert": str(alert["id"]), "page": "1"},
                    )
                )

    recordings_payload = api_json(request, base_url, "/api/iq-recordings")
    recordings = recordings_payload.get("recordings", []) if isinstance(recordings_payload, dict) else []
    for recording in recordings[:1]:
        if isinstance(recording, dict) and recording.get("id"):
            scenarios.append(
                LayoutScenario(
                    f"iq-recording-download-{recording['id']}",
                    "iq_recording_download",
                    {"recording": str(recording["id"])},
                )
            )
    return scenarios


def scenario_matches_filters(scenario: LayoutScenario, filters: Iterable[str] | None) -> bool:
    selected = set(filters or [])
    if not selected:
        return True
    return scenario.name in selected or scenario.view in selected


def visible_view_id(view: str) -> str:
    return "view_dashboard" if view == "dashboard" else f"view_{view}"


def prepare_scenario(page, scenario: LayoutScenario) -> None:
    if scenario.view == "stream_settings" and scenario.tab:
        tab_id = f"tab_{scenario.tab}"
        tab = page.locator(f"#{tab_id}")
        if tab.count():
            tab.click()
            page.wait_for_timeout(150)


def collect_layout_issues(page, viewport: Viewport, scenario: LayoutScenario, min_target_size: float) -> list[LayoutIssue]:
    raw_issues = page.evaluate(
        """
        ({minTargetSize, viewName}) => {
          const issues = [];
          const viewportWidth = document.documentElement.clientWidth;
          const scrollWidth = Math.max(document.documentElement.scrollWidth, document.body.scrollWidth);
          if (scrollWidth > viewportWidth + 1) {
            issues.push(`page overflows horizontally: scrollWidth=${scrollWidth}, viewportWidth=${viewportWidth}`);
          }

          const visible = element => {
            const style = window.getComputedStyle(element);
            if (style.display === "none" || style.visibility === "hidden" || Number(style.opacity) === 0) return false;
            const rect = element.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
          };

          const selector = "a[href], button, input, select, textarea, [role='button'], [role='menuitem']";
          const controls = Array.from(document.querySelectorAll(selector)).filter(visible);
          for (const element of controls) {
            const tagName = element.tagName.toLowerCase();
            const role = element.getAttribute("role") || "";
            const type = String(element.getAttribute("type") || "").toLowerCase();
            const isInlineLink = tagName === "a" && role !== "button" && role !== "menuitem" && !element.closest("nav");
            if (isInlineLink) continue;
            let target = element;
            if (tagName === "input" && (type === "checkbox" || type === "radio")) {
              const wrappingLabel = element.closest("label");
              const explicitLabel = element.id ? document.querySelector(`label[for="${CSS.escape(element.id)}"]`) : null;
              target = wrappingLabel || explicitLabel || element;
            }
            const rect = target.getBoundingClientRect();
            if (rect.width < minTargetSize || rect.height < minTargetSize) {
              const label = element.getAttribute("aria-label") || element.textContent.trim() || element.id || element.tagName;
              issues.push(`small control target "${label}": ${Math.round(rect.width)}x${Math.round(rect.height)}`);
            }
          }

          for (let firstIndex = 0; firstIndex < controls.length; firstIndex += 1) {
            const first = controls[firstIndex];
            const firstRect = first.getBoundingClientRect();
            for (let secondIndex = firstIndex + 1; secondIndex < controls.length; secondIndex += 1) {
              const second = controls[secondIndex];
              if (first.contains(second) || second.contains(first)) continue;
              const secondRect = second.getBoundingClientRect();
              const overlapX = Math.max(0, Math.min(firstRect.right, secondRect.right) - Math.max(firstRect.left, secondRect.left));
              const overlapY = Math.max(0, Math.min(firstRect.bottom, secondRect.bottom) - Math.max(firstRect.top, secondRect.top));
              if (overlapX > 2 && overlapY > 2) {
                const firstLabel = first.getAttribute("aria-label") || first.textContent.trim() || first.id || first.tagName;
                const secondLabel = second.getAttribute("aria-label") || second.textContent.trim() || second.id || second.tagName;
                issues.push(`controls overlap: "${firstLabel}" and "${secondLabel}"`);
              }
            }
          }

          if (viewName === "receiver") {
            const previous = document.getElementById("receiver_previous");
            const playPause = document.getElementById("receiver_play_pause");
            const next = document.getElementById("receiver_next");
            if (!previous || !playPause || !next) {
              issues.push("receiver controls are missing");
            } else {
              const rects = [previous, playPause, next].map(element => element.getBoundingClientRect());
              const sameRow = rects.every(rect => Math.abs(rect.top - rects[0].top) <= 1);
              if (!sameRow) issues.push("receiver previous/play/next controls wrapped onto multiple rows");
              const sameSize = rects.every(rect => Math.abs(rect.width - rects[0].width) <= 1 && Math.abs(rect.height - rects[0].height) <= 1);
              if (!sameSize) issues.push("receiver previous/play/next controls do not have equal button boxes");
            }
          }

          return issues;
        }
        """,
        {"minTargetSize": min_target_size, "viewName": scenario.view},
    )
    return [LayoutIssue(viewport.name, scenario.name, str(message)) for message in raw_issues]


def page_load_issues(page, response, viewport: Viewport, scenario: LayoutScenario) -> list[LayoutIssue]:
    issues: list[LayoutIssue] = []
    status = response.status if response is not None else 0
    if status == 401:
        issues.append(LayoutIssue(viewport.name, scenario.name, "authentication required; pass --username and --password"))
        return issues
    if status >= 400:
        issues.append(LayoutIssue(viewport.name, scenario.name, f"page failed to load: HTTP {status}"))
        return issues
    expected = visible_view_id(scenario.view)
    visible = page.evaluate(
        """
        selector => {
          const element = document.getElementById(selector);
          if (!element) return false;
          const style = window.getComputedStyle(element);
          const rect = element.getBoundingClientRect();
          return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
        }
        """,
        expected,
    )
    if not visible:
        issues.append(LayoutIssue(viewport.name, scenario.name, f"expected view #{expected} is not visible"))
    return issues


def run_checks(args: argparse.Namespace) -> list[LayoutIssue]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit("Playwright is not installed. Run: python3 -m pip install -e '.[dev]'") from exc

    viewports = args.viewports or [parse_viewport(value) for value in DEFAULT_VIEWPORTS]
    if args.screenshots:
        args.screenshots.mkdir(parents=True, exist_ok=True)

    issues: list[LayoutIssue] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for viewport in viewports:
                context = browser.new_context(**browser_context_options(args, viewport))
                page = context.new_page()
                try:
                    scenarios = [
                        scenario
                        for scenario in static_scenarios() + discover_scenarios(context.request, args.url)
                        if scenario_matches_filters(scenario, args.views)
                    ]
                    for scenario in scenarios:
                        response = page.goto(route_url(args.url, scenario.view, scenario.params), wait_until="domcontentloaded")
                        page.wait_for_timeout(500)
                        load_issues = page_load_issues(page, response, viewport, scenario)
                        issues.extend(load_issues)
                        if not load_issues:
                            prepare_scenario(page, scenario)
                            issues.extend(collect_layout_issues(page, viewport, scenario, args.min_target_size))
                        if args.screenshots:
                            page.screenshot(path=args.screenshots / safe_screenshot_name(viewport.name, scenario.name), full_page=True)
                finally:
                    context.close()
        finally:
            browser.close()
    return issues


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    issues = run_checks(args)
    if issues:
        for issue in issues:
            print(f"{issue.viewport}/{issue.scenario}: {issue.message}", file=sys.stderr)
        return 1
    checked_viewports = args.viewports or [parse_viewport(value) for value in DEFAULT_VIEWPORTS]
    checked_views = args.views or ["all available scenarios"]
    print(f"Layout checks passed for {', '.join(checked_views)} across {len(checked_viewports)} viewports.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
