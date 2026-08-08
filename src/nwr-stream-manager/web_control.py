from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

if __package__:
    from .rtl import (
        DEFAULT_RTL_SAMPLE_RATE,
        NWR_CENTER_FREQUENCY_HZ,
        RTL_SAMPLE_RATE_RANGES,
        RtlConfig,
        RtlConfigError,
        RtlCaptureSource,
        list_rtl_devices,
        list_usb_rtl_devices,
        validate_ppm_correction,
        validate_rtl_sample_rate,
    )
else:
    import importlib
    import types

    package_name = "nwr_stream_manager_runtime"
    package = types.ModuleType(package_name)
    package.__path__ = [str(Path(__file__).resolve().parent)]  # type: ignore[attr-defined]
    sys.modules.setdefault(package_name, package)
    rtl = importlib.import_module(f"{package_name}.rtl")
    DEFAULT_RTL_SAMPLE_RATE = rtl.DEFAULT_RTL_SAMPLE_RATE
    NWR_CENTER_FREQUENCY_HZ = rtl.NWR_CENTER_FREQUENCY_HZ
    RTL_SAMPLE_RATE_RANGES = rtl.RTL_SAMPLE_RATE_RANGES
    RtlConfig = rtl.RtlConfig
    RtlConfigError = rtl.RtlConfigError
    RtlCaptureSource = rtl.RtlCaptureSource
    list_rtl_devices = rtl.list_rtl_devices
    list_usb_rtl_devices = rtl.list_usb_rtl_devices
    validate_ppm_correction = rtl.validate_ppm_correction
    validate_rtl_sample_rate = rtl.validate_rtl_sample_rate


LOG = logging.getLogger(__name__)
STATE_DIRECTORY_NAME = "nwr-stream-manager"
STATE_FILE_NAME = "rtl-control.json"


@dataclass(frozen=True)
class RtlControlSettings:
    serial: str = ""
    sample_rate: int = DEFAULT_RTL_SAMPLE_RATE
    gain: float | None = None
    ppm_correction: int = 0
    bias_tee: bool = False

    def to_rtl_config(self) -> RtlConfig:
        if not self.serial:
            raise RtlConfigError("select an RTL-SDR before starting capture")
        return RtlConfig(
            serial=self.serial,
            sample_rate=validate_rtl_sample_rate(self.sample_rate),
            center_frequency_hz=NWR_CENTER_FREQUENCY_HZ,
            ppm_correction=validate_ppm_correction(self.ppm_correction),
            gain=self.gain,
            bias_tee=bool(self.bias_tee),
        )


class RingLogHandler(logging.Handler):
    def __init__(self, max_records: int = 200) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=max_records)
        self.records_lock = threading.Lock()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        message = self.format(record)
        with self.records_lock:
            self.records.append(message)

    def snapshot(self) -> list[str]:
        with self.records_lock:
            return list(self.records)


class RtlControlService:
    def __init__(self, state_path: Path, log_handler: RingLogHandler) -> None:
        self.state_path = state_path
        self.log_handler = log_handler
        self.lock = threading.RLock()
        self.settings = load_settings(state_path)
        self.capture: RtlCaptureSource | None = None
        self.drain_thread: threading.Thread | None = None
        self.drain_stop = threading.Event()
        self.capture_error: str | None = None
        self.last_batch_at: float | None = None
        self.received_chunks = 0
        self.received_bytes = 0
        self._last_rate_log_at = 0.0
        if self.settings.serial:
            self._start_or_update_capture_locked()

    def close(self) -> None:
        self.stop_capture()

    def status(self) -> dict[str, Any]:
        with self.lock:
            capture = self.capture
            settings = self._effective_settings_locked()
            active = capture is not None
            gain_values = capture.get_gain_values() if capture is not None else []
            return {
                "settings": asdict(settings),
                "gain_values": gain_values,
                "active": active,
                "capture_error": self.capture_error,
                "last_batch_at": self.last_batch_at,
                "received_chunks": self.received_chunks,
                "received_bytes": self.received_bytes,
                "sample_rate_ranges": RTL_SAMPLE_RATE_RANGES,
                "center_frequency_hz": NWR_CENTER_FREQUENCY_HZ,
                "logs": self.log_handler.snapshot()[-80:],
            }

    def devices(self) -> dict[str, Any]:
        errors: list[str] = []
        rtl_devices = []
        usb_devices = []
        try:
            rtl_devices = list_rtl_devices()
        except Exception as exc:
            errors.append(f"librtlsdr probe failed: {exc}")
        try:
            usb_devices = list_usb_rtl_devices()
        except Exception as exc:
            errors.append(f"USB probe failed: {exc}")

        by_serial: dict[str, dict[str, Any]] = {}
        for device in usb_devices:
            if not device.serial:
                continue
            by_serial[device.serial] = {
                "serial": device.serial,
                "name": device.description,
                "vendor_id": device.vendor_id,
                "product_id": device.product_id,
                "source": "usb",
            }
        for device in rtl_devices:
            if not device.serial:
                continue
            entry = by_serial.setdefault(
                device.serial,
                {
                    "serial": device.serial,
                    "name": device.description,
                    "vendor_id": "",
                    "product_id": "",
                    "source": "librtlsdr",
                },
            )
            entry["name"] = device.description or entry["name"]
            entry["librtlsdr_index"] = device.index
        devices = sorted(by_serial.values(), key=lambda item: (item["name"], item["serial"]))
        return {"devices": devices, "errors": errors}

    def update(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            settings = self._merged_settings(payload)
            self.settings = settings
            save_settings(self.state_path, settings)
            LOG.info(
                "saved RTL settings: serial=%s sample_rate=%s gain=%s ppm=%s bias_tee=%s",
                settings.serial or "<none>",
                settings.sample_rate,
                "auto" if settings.gain is None else f"{settings.gain:g} dB",
                settings.ppm_correction,
                settings.bias_tee,
            )
            if settings.serial:
                self._start_or_update_capture_locked()
            else:
                self._detach_capture_locked()
            return self.status()

    def _merged_settings(self, payload: dict[str, Any]) -> RtlControlSettings:
        settings = self.settings
        changes: dict[str, Any] = {}
        if "serial" in payload:
            changes["serial"] = str(payload["serial"]).strip()
        if "sample_rate" in payload:
            changes["sample_rate"] = validate_rtl_sample_rate(int(payload["sample_rate"]))
        if "ppm_correction" in payload:
            changes["ppm_correction"] = validate_ppm_correction(int(payload["ppm_correction"]))
        if "bias_tee" in payload:
            changes["bias_tee"] = bool(payload["bias_tee"])
        if "gain" in payload:
            gain = payload["gain"]
            changes["gain"] = None if gain is None or gain == "" else float(gain)
        return replace(settings, **changes)

    def _start_or_update_capture_locked(self) -> None:
        config = self.settings.to_rtl_config()
        if self.capture is None:
            self.capture_error = None
            self.capture = RtlCaptureSource(config)
            self.capture.start()
            self.drain_stop.clear()
            self.drain_thread = threading.Thread(
                target=self._drain_capture,
                name="rtl-web-drain",
                daemon=True,
            )
            self.drain_thread.start()
            LOG.info("started RTL-SDR control capture for serial %s", config.serial)
            return
        self.capture.apply_config(config)
        self.settings = self._effective_settings_locked()
        save_settings(self.state_path, self.settings)

    def _effective_settings_locked(self) -> RtlControlSettings:
        if self.capture is None:
            return self.settings
        config = self.capture.config
        return replace(
            self.settings,
            serial=config.serial,
            sample_rate=config.sample_rate,
            gain=config.gain,
            ppm_correction=config.ppm_correction,
            bias_tee=config.bias_tee,
        )

    def _detach_capture_locked(self) -> None:
        self.drain_stop.set()
        capture = self.capture
        self.capture = None
        drain_thread = self.drain_thread
        self.drain_thread = None
        LOG.info("stopped RTL-SDR control capture")
        if capture is not None:
            threading.Thread(
                target=self._stop_detached_capture,
                args=(capture, drain_thread),
                name="rtl-web-stop",
                daemon=True,
            ).start()

    def stop_capture(self) -> None:
        with self.lock:
            self.drain_stop.set()
            capture = self.capture
            self.capture = None
            drain_thread = self.drain_thread
            self.drain_thread = None
        if capture is not None:
            capture.stop()
        if drain_thread is not None:
            drain_thread.join(timeout=2.0)
        LOG.info("stopped RTL-SDR control capture")

    @staticmethod
    def _stop_detached_capture(
        capture: RtlCaptureSource,
        drain_thread: threading.Thread | None,
    ) -> None:
        capture.stop()
        if drain_thread is not None:
            drain_thread.join(timeout=2.0)

    def _drain_capture(self) -> None:
        while not self.drain_stop.is_set():
            with self.lock:
                capture = self.capture
            if capture is None:
                return
            try:
                batch = capture.read(timeout=0.5)
            except queue.Empty:
                continue
            except EOFError:
                return
            except Exception as exc:
                with self.lock:
                    self.capture_error = str(exc)
                LOG.warning("RTL-SDR control capture reported: %s", exc)
                continue
            now = time.monotonic()
            with self.lock:
                self.capture_error = None
                self.last_batch_at = batch.captured_at
                self.received_chunks += 1
                self.received_bytes += len(batch.data)
                chunks = self.received_chunks
                total_bytes = self.received_bytes
            if now - self._last_rate_log_at >= 2.0:
                self._last_rate_log_at = now
                LOG.info(
                    "RTL-SDR capture receiving IQ: chunks=%s bytes=%s sample_rate=%s",
                    chunks,
                    total_bytes,
                    batch.sample_rate,
                )


class RtlControlHandler(BaseHTTPRequestHandler):
    service: RtlControlService

    def log_message(self, format: str, *args) -> None:
        LOG.debug("HTTP %s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(INDEX_HTML)
        elif path == "/api/status":
            self._send_json(self.service.status())
        elif path == "/api/devices":
            self._send_json(self.service.devices())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/settings":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
            response = self.service.update(payload)
        except Exception as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._send_json(response)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(length) if length else b"{}"
        payload = json.loads(data.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("request body must be a JSON object")
        return payload

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_html(self, content: str) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def default_state_path() -> Path:
    state_directory = os.environ.get("STATE_DIRECTORY")
    if state_directory:
        base = Path(state_directory.split(":", 1)[0]).expanduser()
    elif os.environ.get("XDG_STATE_HOME"):
        base = Path(os.environ["XDG_STATE_HOME"]).expanduser() / STATE_DIRECTORY_NAME
    else:
        base = Path.home() / ".local" / "state" / STATE_DIRECTORY_NAME
    return base / STATE_FILE_NAME


def load_settings(path: Path) -> RtlControlSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return RtlControlSettings()
    except Exception as exc:
        LOG.warning("failed to load RTL control settings from %s: %s", path, exc)
        return RtlControlSettings()
    if not isinstance(raw, dict):
        return RtlControlSettings()
    try:
        return RtlControlSettings(
            serial=str(raw.get("serial", "")).strip(),
            sample_rate=validate_rtl_sample_rate(int(raw.get("sample_rate", DEFAULT_RTL_SAMPLE_RATE))),
            gain=None if raw.get("gain") is None else float(raw["gain"]),
            ppm_correction=validate_ppm_correction(int(raw.get("ppm_correction", 0))),
            bias_tee=bool(raw.get("bias_tee", False)),
        )
    except Exception as exc:
        LOG.warning("RTL control settings in %s are invalid: %s", path, exc)
        return RtlControlSettings()


def save_settings(path: Path, settings: RtlControlSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nwr-stream-manager-rtl-control",
        description="Run the local RTL-SDR control web interface.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="address to bind; use 0.0.0.0 for LAN access",
    )
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--state", type=Path, default=default_state_path())
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def access_urls(host: str, port: int) -> list[str]:
    if host not in {"0.0.0.0", "::", ""}:
        return [f"http://{host}:{port}"]

    hosts = {"127.0.0.1"}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            hosts.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address:
                hosts.add(address)
    except OSError:
        pass

    ordered_hosts = sorted(hosts, key=lambda value: (value.startswith("127."), value))
    return [f"http://{address}:{port}" for address in ordered_hosts]


def run_server(host: str, port: int, state_path: Path, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ring_handler = RingLogHandler()
    logging.getLogger().addHandler(ring_handler)
    service = RtlControlService(state_path, ring_handler)
    RtlControlHandler.service = service
    server = ThreadingHTTPServer((host, port), RtlControlHandler)
    LOG.info("RTL-SDR control web interface bound to %s:%s", host, port)
    for url in access_urls(host, port):
        LOG.info("RTL-SDR control web interface available at %s", url)
    LOG.info("RTL-SDR settings will be remembered in %s", state_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_server(args.host, args.port, args.state, args.verbose)
    except KeyboardInterrupt:
        return 130
    return 0


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NWR Stream Manager RTL Control</title>
<style>
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; background: #f6f7f9; color: #14181f; }
main { max-width: 980px; margin: 0 auto; padding: 24px; }
h1 { font-size: 24px; margin: 0 0 20px; }
section { background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 18px; margin-bottom: 16px; }
label { display: grid; gap: 6px; font-weight: 600; margin-bottom: 14px; }
select, input { font: inherit; padding: 8px 10px; border: 1px solid #b9c0cc; border-radius: 6px; background: #fff; color: #14181f; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; }
.row { display: flex; align-items: center; gap: 10px; }
.row label { margin: 0; display: flex; align-items: center; gap: 8px; }
.status { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px; }
.metric { border: 1px solid #d8dde6; border-radius: 6px; padding: 10px; }
.metric b { display: block; font-size: 12px; color: #526070; text-transform: uppercase; }
pre { margin: 0; min-height: 220px; max-height: 360px; overflow: auto; background: #10151d; color: #d8f3dc; padding: 12px; border-radius: 6px; font-size: 13px; }
.error { color: #a40000; font-weight: 600; }
.hint { color: #526070; font-size: 13px; margin-top: -8px; }
@media (prefers-color-scheme: dark) {
  body { background: #101318; color: #eef2f7; }
  section, select, input { background: #181d24; color: #eef2f7; border-color: #333b48; }
  .metric { border-color: #333b48; }
  .metric b, .hint { color: #9aa8ba; }
}
</style>
</head>
<body>
<main>
  <h1>NWR Stream Manager RTL Control</h1>
  <section>
    <label>Active SDR
      <select id="serial"></select>
    </label>
    <div id="device-errors" class="error"></div>
  </section>
  <section>
    <div class="grid">
      <label>Sample Rate
        <input id="sample_rate" type="number" min="225001" max="3200000" step="1">
      </label>
      <label>Gain
        <input id="gain" type="range" min="0" max="0" step="1" value="0" disabled>
        <span id="gain_label" class="hint">Automatic</span>
      </label>
      <label>PPM Correction
        <input id="ppm_correction" type="number" min="-200" max="200" step="1">
      </label>
      <div class="row">
        <label><input id="gain_auto" type="checkbox"> Automatic gain</label>
        <label><input id="bias_tee" type="checkbox"> Bias tee</label>
      </div>
    </div>
    <div class="hint">Valid RTL-SDR sample-rate ranges: 225001-300000 S/s and 900001-3200000 S/s.</div>
  </section>
  <section>
    <div class="status" aria-live="off">
      <div class="metric"><b>Capture</b><span id="active">inactive</span></div>
      <div class="metric"><b>Chunks</b><span id="chunks">0</span></div>
      <div class="metric"><b>Bytes</b><span id="bytes">0</span></div>
      <div class="metric"><b>Last IQ</b><span id="last">never</span></div>
    </div>
    <p id="capture-error" class="error"></p>
  </section>
  <section>
    <pre id="logs" aria-live="off" aria-label="RTL-SDR log output"></pre>
  </section>
</main>
<script>
const controls = ["serial", "sample_rate", "gain", "ppm_correction", "bias_tee", "gain_auto"];
let applying = false;
let timer = null;
let gainValues = [];
let lastControlSignature = "";

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

function deviceLabel(device) {
  const vendor = [device.vendor_id, device.product_id].filter(Boolean).join(":");
  const suffix = vendor ? ` (${vendor}, serial ${device.serial})` : ` (serial ${device.serial})`;
  return `${device.name || "RTL-SDR"}${suffix}`;
}

async function loadDevices(selected) {
  const data = await request("/api/devices");
  const select = document.getElementById("serial");
  const signature = JSON.stringify(data.devices.map(device => [device.serial, deviceLabel(device)]));
  if (select.dataset.signature !== signature) {
    select.innerHTML = '<option value="">Select an RTL-SDR...</option>';
    for (const device of data.devices) {
      const option = document.createElement("option");
      option.value = device.serial;
      option.textContent = deviceLabel(device);
      select.appendChild(option);
    }
    select.dataset.signature = signature;
  }
  setValue("serial", selected || "");
  setText("device-errors", data.errors.join(" | "));
}

function gainIndexFor(value) {
  if (value === null || gainValues.length === 0) return 0;
  return gainValues.reduce((best, gain, index) =>
    Math.abs(gain - value) < Math.abs(gainValues[best] - value) ? index : best, 0);
}

function controlSignature(data) {
  const s = data.settings;
  return JSON.stringify({
    serial: s.serial || "",
    sample_rate: s.sample_rate,
    gain: s.gain,
    ppm_correction: s.ppm_correction,
    bias_tee: s.bias_tee,
    gain_values: data.gain_values || []
  });
}

function controlHasFocus() {
  const active = document.activeElement;
  return active && controls.includes(active.id);
}

function setValue(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.value !== text) element.value = text;
}

function setChecked(id, value) {
  const element = document.getElementById(id);
  const checked = Boolean(value);
  if (element.checked !== checked) element.checked = checked;
}

function setDisabled(element, value) {
  const disabled = Boolean(value);
  if (element.disabled !== disabled) element.disabled = disabled;
}

function setAttributeIfChanged(element, name, value) {
  const text = String(value);
  if (element.getAttribute(name) !== text) element.setAttribute(name, text);
}

function setText(id, value) {
  const element = document.getElementById(id);
  const text = String(value);
  if (element.textContent !== text) element.textContent = text;
}

function syncControls(data) {
  const s = data.settings;
  gainValues = data.gain_values || [];
  setValue("serial", s.serial || "");
  setValue("sample_rate", s.sample_rate);
  setChecked("gain_auto", s.gain === null);
  const gain = document.getElementById("gain");
  setAttributeIfChanged(gain, "max", Math.max(0, gainValues.length - 1));
  setDisabled(gain, s.gain === null || gainValues.length === 0);
  const gainIndex = gainIndexFor(s.gain);
  if (gain.value !== String(gainIndex)) gain.value = String(gainIndex);
  if (s.gain === null) {
    setText("gain_label", "Automatic");
  } else if (gainValues.length === 0) {
    setText("gain_label", `${s.gain} dB`);
  } else {
    setText("gain_label", `${gainValues[gainIndex]} dB`);
  }
  setValue("ppm_correction", s.ppm_correction);
  setChecked("bias_tee", s.bias_tee);
  lastControlSignature = controlSignature(data);
}

function applyStatus(data, options = {}) {
  applying = true;
  const nextSignature = controlSignature(data);
  if (
    options.syncControls ||
    lastControlSignature === "" ||
    (nextSignature !== lastControlSignature && !controlHasFocus())
  ) {
    syncControls(data);
  } else {
    gainValues = data.gain_values || gainValues;
  }
  setText("active", data.active ? "active" : "inactive");
  setText("chunks", data.received_chunks);
  setText("bytes", data.received_bytes);
  setText("last", data.last_batch_at ? `${data.last_batch_at.toFixed(3)}s` : "never");
  setText("capture-error", data.capture_error || "");
  setText("logs", data.logs.join("\\n"));
  applying = false;
}

function currentPayload() {
  const auto = document.getElementById("gain_auto").checked;
  const gainIndex = Number(document.getElementById("gain").value);
  return {
    serial: document.getElementById("serial").value,
    sample_rate: Number(document.getElementById("sample_rate").value),
    gain: auto || gainValues.length === 0 ? null : gainValues[gainIndex],
    ppm_correction: Number(document.getElementById("ppm_correction").value),
    bias_tee: document.getElementById("bias_tee").checked
  };
}

function scheduleUpdate() {
  if (applying) return;
  clearTimeout(timer);
  timer = setTimeout(async () => {
    try {
      const data = await request("/api/settings", {
        method: "PATCH",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(currentPayload())
      });
      applyStatus(data, {syncControls: true});
    } catch (error) {
      document.getElementById("capture-error").textContent = error.message;
    }
  }, 250);
}

for (const id of controls) {
  document.addEventListener("input", event => {
    if (event.target && event.target.id === id) scheduleUpdate();
  });
  document.addEventListener("change", event => {
    if (event.target && event.target.id === id) scheduleUpdate();
  });
}

async function refresh() {
  const data = await request("/api/status");
  applyStatus(data, {syncControls: false});
}

(async function init() {
  const data = await request("/api/status");
  await loadDevices(data.settings.serial);
  applyStatus(data, {syncControls: true});
  setInterval(refresh, 1000);
  setInterval(async () => {
    const status = await request("/api/status");
    if (document.activeElement.id !== "serial") {
      await loadDevices(status.settings.serial);
    }
  }, 5000);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
