from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_live_audio_playback import load_web_control_module


PAGE_FUNCTIONS = (
    # Status updates also keep General's uptime current.
    "formatUptime",
    "controlSignature",
    "numberFieldIsBeingTyped",
    "sdrSettingIsBeingChanged",
    "syncControls",
    "applyStatus",
    "currentPayload",
    "scheduleUpdate",
    "settleSdrSettingEdits",
    "saveSdrSettingEdits",
    "gainIndexFor",
    "gainTextForIndex",
    "updateGainSliderText",
    "storedManualGain",
    "rememberManualGain",
    "setValue",
    "setChecked",
    "setDisabled",
    "setAttributeIfChanged",
    "numericControlValue",
    "previousNumberValue",
    "setMessage",
    "renderSdrReset",
    "setSdrResetButtonsDisabled",
    "resetSdr",
    "renderActiveSdrChoices",
    "remoteSdrUsesRemote",
    "remoteDeviceName",
    "deviceLabel",
    "configuredDeviceLabel",
    "chooseActiveSdr",
    "saveSdrSource",
)
PAGE_STATE = (
    "const controls",
    "const SDR_SETTING_FOR_CONTROL",
    "const pendingSdrSettingEdits",
    "let statusRequestCount",
    "let sdrControlsStaleThrough",
    "let lastControlSignature",
    "let timer",
    "let gainValues",
    "let lastManualGain",
    "const SDR_RESET_REQUEST_TIMEOUT_MS",
    "let sdrResetRequestPending",
    "let sdrResetInProgress",
    "let lastSdrResetKey",
    "const REMOTE_SDR_CHOICE_PREFIX",
    "let localSdrDevices",
    "let configuredLocalSerial",
    "let remoteSdr",
    "let remoteSdrRequestPending",
)

# Two people share one SDR. The page's own settings code runs against a pretend
# page and server; each scenario acts as the other person (changing the server)
# and as this page's user (changing a control), then checks what this page shows
# and what it sends.
HARNESS = r"""
let applying = false;
let lastEasAlertRefreshAt = Date.now();
let lastIqRecordingsRefreshAt = 0;
function noop() {}
const applyAccountUi = noop, renderRemoteSdr = noop, renderLogs = noop, renderIqTestSourceStatus = noop,
  setFallbackControls = noop, setNotificationSettings = noop, setStreamTestModeStatus = noop,
  syncConfiguredStreamsFromStatus = noop, updateDashboard = noop, loadEasAlertStreams = noop,
  updateRtlNotificationControlsVisibility = noop;
function currentViewName() { return "rtl"; }
function setText(id, value) { document.getElementById(id).textContent = String(value); }
function setHidden(id, hidden) { document.getElementById(id).hidden = Boolean(hidden); }
function syncSelectOptions(select, options) { select.options = options; }

function element(id, extra = {}) {
  return {id, value: "", checked: false, disabled: false, textContent: "", dataset: {}, attributes: {},
    getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; },
    setAttribute(name, value) { this.attributes[name] = String(value); },
    ...extra};
}
const elements = {};
for (const id of ["serial", "gain", "gain_auto", "gain_label", "bias_tee", "alias_filter_strength",
  "alias_filter_strength_label", "notify_sdr_failures", "capture-error"]) elements[id] = element(id);
elements.ppm_correction = element("ppm_correction", {type: "number", tagName: "INPUT"});
const document = {activeElement: null, getElementById: id => elements[id] || (elements[id] = element(id))};

let pendingTimers = [];
function setTimeout(callback) { pendingTimers.push(callback); return pendingTimers.length; }
function clearTimeout() { pendingTimers = []; }

// The server, which both people change.
const server = {settings: {serial: "18263784", gain: 32.8, ppm_correction: 0, bias_tee: false,
  alias_filter_strength: 100, notify_sdr_failures: false}};
const GAINS = [0.0, 20.7, 28.0, 32.8, 40.2, 49.6];
function status() { return {settings: {...server.settings}, gain_values: GAINS, sdr_reset: server.reset || null}; }
async function requestWithTimeout(path, options) { return request(path, options); }
async function loadDevices() {}
function statusRequestPath() { return "/api/status"; }
// What a screen reader would announce from the reset message.
const announced = [];
for (const id of ["reset_rtl_device", "reset_remote_sdr"]) elements[id] = element(id);
elements["sdr-reset-status"] = element("sdr-reset-status");
{
  let text = "";
  Object.defineProperty(elements["sdr-reset-status"], "textContent", {
    get() { return text; },
    set(value) { text = value; if (value) announced.push(value); }
  });
}
function resetShown() {
  return {message: elements["sdr-reset-status"].textContent, kind: elements["sdr-reset-status"].className,
    disabled: elements.reset_rtl_device.disabled && elements.reset_remote_sdr.disabled};
}
const sent = [];
var request = async function (path, options) {
  const payload = JSON.parse(options.body);
  sent.push(payload);
  await null;
  Object.assign(server.settings, payload);
  return status();
};
async function refresh() { poll(); }

function poll() { applyStatus(status(), {syncControls: false, requestNumber: ++statusRequestCount}); }
// A status request sent now whose answer (made now) is delivered later.
function startPoll() {
  const data = status();
  const requestNumber = ++statusRequestCount;
  return () => applyStatus(data, {syncControls: false, requestNumber});
}
function focus(id) { document.activeElement = elements[id]; }
function userSets(id, value) {
  focus(id);
  if (typeof value === "boolean") elements[id].checked = value; else elements[id].value = String(value);
  scheduleUpdate(id);
}
async function saveNow() {
  const timers = pendingTimers;
  pendingTimers = [];
  for (const callback of timers) await callback();
}
function shown() {
  return {gain: GAINS[Number(elements.gain.value)], automatic: elements.gain_auto.checked,
    ppm: elements.ppm_correction.value, bias_tee: elements.bias_tee.checked,
    alias: elements.alias_filter_strength.value};
}
const results = {};
applyStatus(status());

(async () => {
SCENARIO
console.log(JSON.stringify({results, sent, server: server.settings, announced}));
})().catch(error => { console.error(error); process.exit(1); });
"""


class SdrSettingsSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        page = load_web_control_module().INDEX_HTML
        parts = []
        for start_text in PAGE_STATE:
            start = page.index(start_text + " ")
            parts.append(page[start : page.index(";\n", start) + 2])
        for name in PAGE_FUNCTIONS:
            start = page.index(f"function {name}(")
            if page[start - 6 : start] == "async ":
                start -= 6
            parts.append(page[start : page.index("\n}\n", start) + 2])
        cls.page_code = "\n".join(parts)

    def run_scenario(self, scenario: str) -> dict:
        import json

        script = self.page_code + HARNESS.replace("SCENARIO", scenario)
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "sync.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([self.node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_change_made_elsewhere_shows_even_on_the_focused_control(self) -> None:
        # A screen reader user rests on the gain slider while someone else changes it.
        result = self.run_scenario("""
            focus("gain");
            server.settings.gain = 40.2;
            server.settings.bias_tee = true;
            poll();
            results.after = shown();
        """)
        self.assertEqual(result["results"]["after"]["gain"], 40.2)
        self.assertTrue(result["results"]["after"]["bias_tee"])

    def test_only_the_changed_setting_is_sent(self) -> None:
        # This page still shows the old PPM when its user changes gain; sending the
        # whole form would put the old PPM back over the other person's change.
        result = self.run_scenario("""
            server.settings.ppm_correction = 12;
            userSets("gain", 4);
            await saveNow();
        """)
        self.assertEqual(result["sent"], [{"gain": 40.2}])
        self.assertEqual(result["server"]["ppm_correction"], 12)

    def test_a_change_being_saved_is_not_undone_by_a_status_update(self) -> None:
        result = self.run_scenario("""
            userSets("gain", 1);
            server.settings.ppm_correction = 5;
            poll();  // arrives before this change is saved
            results.before_save = shown();
            await saveNow();
            results.after_save = shown();
        """)
        self.assertEqual(result["results"]["before_save"]["gain"], 20.7)
        self.assertEqual(result["results"]["before_save"]["ppm"], "5")  # others' changes still show
        self.assertEqual(result["results"]["after_save"]["gain"], 20.7)

    def test_a_status_update_sent_before_the_save_cannot_bring_back_the_old_value(self) -> None:
        result = self.run_scenario("""
            const late = startPoll();  // asked for before the change below was saved
            userSets("bias_tee", true);
            await saveNow();
            late();
            results.after = shown();
        """)
        self.assertTrue(result["results"]["after"]["bias_tee"])

    def test_status_replies_are_never_held_back_when_nothing_was_saved_here(self) -> None:
        # Hours of polling with no changes made on this page: every change made
        # elsewhere shows on the first reply after it.
        result = self.run_scenario("""
            results.shown = [];
            for (let index = 0; index < 500; index += 1) {
              server.settings.ppm_correction = index;
              poll();
              results.shown.push(Number(shown().ppm));
            }
        """)
        self.assertEqual(result["results"]["shown"], list(range(500)))

    def test_status_requested_after_a_save_shows_others_changes_at_once(self) -> None:
        result = self.run_scenario("""
            userSets("gain", 4);
            await saveNow();
            server.settings.ppm_correction = 9;  // someone else, just after
            server.settings.gain = 0.0;
            poll();
            results.after = shown();
        """)
        self.assertEqual(result["results"]["after"]["ppm"], "9")
        self.assertEqual(result["results"]["after"]["gain"], 0.0)

    def test_only_status_already_on_its_way_during_a_save_is_held_back(self) -> None:
        # The reply on its way may predate the save, so it waits; the very next
        # reply shows the other person's change.
        result = self.run_scenario("""
            server.settings.ppm_correction = 9;
            const onItsWay = startPoll();
            userSets("bias_tee", true);
            await saveNow();
            onItsWay();
            results.held = shown();
            poll();
            results.next = shown();
        """)
        self.assertTrue(result["results"]["held"]["bias_tee"])  # not put back by the old reply
        self.assertEqual(result["results"]["next"]["ppm"], "9")
        self.assertTrue(result["results"]["next"]["bias_tee"])

    def test_status_after_a_failed_save_shows_what_the_sdr_really_uses(self) -> None:
        result = self.run_scenario("""
            request = async () => { throw new Error("The remote SDR did not respond."); };
            userSets("gain", 5);
            server.settings.gain = 28.0;
            await saveNow();
            results.after = shown();
            results.error = elements["sdr-settings-result"].textContent;
            request = async (path, options) => { Object.assign(server.settings, JSON.parse(options.body)); return status(); };
            userSets("gain", 3);
            await saveNow();
            results.error_after_next_save = elements["sdr-settings-result"].textContent;
        """)
        self.assertEqual(result["results"]["after"]["gain"], 28.0)
        # Shown on the SDR page, where it is announced, and not wiped by the status that follows.
        self.assertEqual(result["results"]["error"], "The remote SDR did not respond.")
        self.assertEqual(result["results"]["error_after_next_save"], "")

    def test_a_reset_started_elsewhere_shows_here_as_it_happens(self) -> None:
        result = self.run_scenario("""
            server.reset = {id: 4, state: "resetting", message: "Resetting RTL-SDR..."};
            poll();
            results.resetting = resetShown();
            poll();
            server.reset = {id: 4, state: "finished", message: "RTL-SDR serial 00000001 was reset using usbreset and reappeared."};
            poll();
            results.finished = resetShown();
            server.reset = null;  // no longer shown after a while
            poll();
            results.later = resetShown();
            server.reset = {id: 5, state: "failed", message: "no USB device found for RTL-SDR serial 00000001"};
            poll();
            results.failed = resetShown();
        """)
        shown = result["results"]
        self.assertEqual(shown["resetting"], {"message": "Resetting RTL-SDR...", "kind": "message", "disabled": True})
        self.assertEqual(shown["finished"]["message"], "RTL-SDR serial 00000001 was reset using usbreset and reappeared.")
        self.assertEqual(shown["finished"]["kind"], "message success")
        self.assertFalse(shown["finished"]["disabled"])
        self.assertEqual(shown["later"]["message"], "")
        self.assertEqual(shown["failed"]["kind"], "message error")
        # Each step is announced once, however many status checks repeat it.
        self.assertEqual(result["announced"], [
            "Resetting RTL-SDR...",
            "RTL-SDR serial 00000001 was reset using usbreset and reappeared.",
            "no USB device found for RTL-SDR serial 00000001",
        ])

    def test_the_page_that_resets_a_remote_sdr_announces_each_step_once(self) -> None:
        result = self.run_scenario("""
            let answer;
            request = (path, options) => { sent.push({path, body: JSON.parse(options.body)}); return new Promise(resolve => { answer = resolve; }); };
            const resetting = resetSdr(true);
            results.while_waiting = resetShown();
            server.reset = {id: 9, state: "resetting", message: "Resetting RTL-SDR..."};
            poll();
            server.reset = {id: 9, state: "finished", message: "RTL-SDR serial 00000001 was reset using usbreset and reappeared."};
            answer({success: true, reappeared: true, message: server.reset.message, status: status()});
            await resetting;
            poll();
            results.after = resetShown();
        """)
        self.assertEqual(result["sent"], [{"path": "/api/rtl-reset", "body": {}}])
        self.assertTrue(result["results"]["while_waiting"]["disabled"])
        self.assertEqual(result["results"]["after"]["kind"], "message success")
        self.assertFalse(result["results"]["after"]["disabled"])
        self.assertEqual(result["announced"], [
            "Resetting RTL-SDR...",
            "RTL-SDR serial 00000001 was reset using usbreset and reappeared.",
        ])

    def test_a_local_reset_needs_an_sdr_chosen(self) -> None:
        result = self.run_scenario("""
            elements.serial.value = "";
            await resetSdr(false);
            results.after = resetShown();
        """)
        self.assertEqual(result["sent"], [])
        self.assertEqual(result["results"]["after"]["message"], "Select an RTL-SDR to reset.")

    def test_a_number_being_typed_is_left_alone_while_others_still_update(self) -> None:
        result = self.run_scenario("""
            focus("ppm_correction");
            elements.ppm_correction.dataset.userEditing = "1";
            elements.ppm_correction.value = "-1";  // part way through typing -15
            server.settings.ppm_correction = 3;
            server.settings.gain = 0.0;
            poll();
            results.after = shown();
        """)
        self.assertEqual(result["results"]["after"]["ppm"], "-1")
        self.assertEqual(result["results"]["after"]["gain"], 0.0)

    def test_switching_to_automatic_gain_elsewhere_shows_here(self) -> None:
        result = self.run_scenario("""
            focus("gain_auto");
            server.settings.gain = null;
            poll();
            results.after = shown();
        """)
        self.assertTrue(result["results"]["after"]["automatic"])



EVENTS_FUNCTIONS = ("settingsEventsUrl", "startSettingsEvents", "scheduleSettingsEventsRetry", "refreshForSettingsChange")
EVENTS_STATE = (
    "const SETTINGS_EVENTS_RETRY_MIN_MS",
    "const SETTINGS_EVENTS_RETRY_MAX_MS",
    "const SERVER_SHUTDOWN_CLOSE_CODE",
    "let settingsEventsSocket",
    "let settingsEventsRetryMs",
    "let settingsChangeRefreshRunning",
    "let settingsChangeRefreshAgain",
)
# The page's connection to the server's settings events, against a pretend
# WebSocket and a status check that takes as long as the scenario says.
EVENTS_HARNESS = r"""
const window = {location: {protocol: "https:", host: "radio.example:8080"}};
const sockets = [];
class WebSocket {
  constructor(url) { this.url = url; this.listeners = {}; sockets.push(this); }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); }
}
const retries = [];
function setTimeout(callback, ms) { retries.push({callback, ms}); }
let checks = 0;
let finishCheck = null;
function refresh() { checks += 1; return new Promise(resolve => { finishCheck = resolve; }); }
const unavailable = [];
function enterServerUnavailable(reason) { unavailable.push(reason); }
async function settle() { for (let index = 0; index < 20; index += 1) await null; }
async function check() { const done = finishCheck; finishCheck = null; done(); await settle(); }
const results = {};
(async () => {
SCENARIO
console.log(JSON.stringify(results));
})().catch(error => { console.error(error); process.exit(1); });
"""


class SettingsEventsPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        page = load_web_control_module().INDEX_HTML
        parts = []
        for start_text in EVENTS_STATE:
            start = page.index(start_text + " ")
            parts.append(page[start : page.index(";\n", start) + 2])
        for name in EVENTS_FUNCTIONS:
            start = page.index(f"function {name}(")
            if page[start - 6 : start] == "async ":
                start -= 6
            parts.append(page[start : page.index("\n}\n", start) + 2])
        cls.page_code = "\n".join(parts)

    def run_scenario(self, scenario: str) -> dict:
        import json

        script = self.page_code + EVENTS_HARNESS.replace("SCENARIO", scenario)
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "events.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([self.node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_change_is_checked_at_once_and_a_burst_needs_only_one_more_check(self) -> None:
        result = self.run_scenario("""
            startSettingsEvents();
            const socket = sockets[0];
            results.url = socket.url;
            socket.emit("open");  // catches up on anything missed
            await check();
            results.after_open = checks;
            socket.emit("message", {data: '{"type":"keepalive"}'});
            results.after_keepalive = checks;
            socket.emit("message", {data: '{"type":"settings_changed"}'});
            results.after_change = checks;
            for (let index = 0; index < 5; index += 1) socket.emit("message", {data: '{"type":"settings_changed"}'});
            await check();
            await check();
            results.after_burst = checks;
            results.still_checking = finishCheck !== null;
        """)
        self.assertEqual(result["url"], "wss://radio.example:8080/api/settings-events")
        self.assertEqual(result["after_open"], 1)
        self.assertEqual(result["after_keepalive"], 1)
        self.assertEqual(result["after_change"], 2)
        self.assertEqual(result["after_burst"], 3)
        self.assertFalse(result["still_checking"])

    def test_a_lost_connection_is_retried_less_often_until_it_returns(self) -> None:
        result = self.run_scenario("""
            startSettingsEvents();
            for (let index = 0; index < 7; index += 1) {
              sockets[sockets.length - 1].emit("close");
              retries[retries.length - 1].callback();
            }
            results.delays = retries.map(retry => retry.ms);
            sockets[sockets.length - 1].emit("open");
            await settle();
            sockets[sockets.length - 1].emit("close");
            results.after_return = retries[retries.length - 1].ms;
            results.sockets = sockets.length;
        """)
        self.assertEqual(result["delays"], [1000, 2000, 4000, 8000, 16000, 30000, 30000])
        self.assertEqual(result["after_return"], 1000)
        self.assertEqual(result["sockets"], 8)  # one at a time, never two


    def test_a_server_shutting_down_is_shown_at_once(self) -> None:
        # Restarts are announced, so the page need not wait to be sure it is gone.
        result = self.run_scenario("""
            startSettingsEvents();
            sockets[0].emit("open");
            await check();
            const before = retries.length;
            sockets[0].emit("close", {code: 1012});
            results.unavailable = unavailable;
            results.retried = retries.length > before;
        """)
        self.assertEqual(result["unavailable"], ["shutdown"])
        self.assertFalse(result["retried"])

    def test_a_dropped_connection_is_not_taken_for_a_shutdown(self) -> None:
        result = self.run_scenario("""
            startSettingsEvents();
            sockets[0].emit("close", {code: 1006});
            results.unavailable = unavailable;
        """)
        self.assertEqual(result["unavailable"], [])


class SdrLooksTheSameDuringResetTests(unittest.TestCase):
    """A reset looks the same whether the RTL-SDR is on this machine or shared by another."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        cls.page = load_web_control_module().INDEX_HTML

    def remote_status_line(self, sdr: dict) -> str:
        import json

        start = self.page.index("function remoteSdrStatusText(")
        code = self.page[start : self.page.index("\n}\n", start) + 2]
        script = (
            "function remoteSdrUsesRemote() { return true; }\n"
            "function remoteDeviceName(device) { return device.name; }\n"
            + code
            + "const remote = {device_id: 'h', status: {host_name: 'weather-pi', host_id: 'h', reachable: true, sdr: "
            + json.dumps(sdr)
            + "}};\nconsole.log(remoteSdrStatusText(remote, [{id: 'h', name: 'weather-pi'}]));\n"
        )
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "line.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([self.node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_the_remote_sdr_is_not_called_disconnected_while_it_resets(self) -> None:
        resetting = {"id": 1, "state": "resetting", "message": "Resetting RTL-SDR..."}
        for problem in ("disconnected", "no-data"):
            with self.subTest(problem=problem):
                self.assertEqual(
                    self.remote_status_line({"device_name": "RTL-SDR Blog V4", "problem": problem, "reset": resetting}),
                    "Connected to weather-pi. Using RTL-SDR Blog V4.",
                )

    def test_a_remote_sdr_still_missing_after_its_reset_is_reported(self) -> None:
        failed = {"id": 1, "state": "failed", "message": "RTL-SDR serial 00000001 ... did not reappear."}
        self.assertEqual(
            self.remote_status_line({"device_name": "RTL-SDR Blog V4", "problem": "disconnected", "reset": failed}),
            "Connected to weather-pi, but its RTL-SDR is not connected.",
        )

    def test_the_local_device_list_is_not_checked_while_the_sdr_resets(self) -> None:
        # Otherwise it would briefly list the RTL-SDR as not connected.
        start = self.page.rindex("await loadDevices(status.settings.serial);")
        poll = self.page[self.page.rindex("setInterval(async () => {", 0, start) : start]
        self.assertIn("if (sdrResetRequestPending || sdrResetInProgress) return;", poll)


if __name__ == "__main__":
    unittest.main()


class ActiveSdrListTests(unittest.TestCase):
    """One list holds this machine's RTL-SDRs and the ones paired devices share."""

    setUpClass = SdrSettingsSyncTests.__dict__["setUpClass"]
    run_scenario = SdrSettingsSyncTests.run_scenario

    SETUP = """
        localSdrDevices = [{serial: "18263784", vendor: "Nooelec", name: "NESDR SMArt v5"}];
        remoteSdr = {devices: [{id: "pi", name: "weather-pi"}], remote: {source_mode: "local", device_id: ""}};
        configuredLocalSerial = "18263784";
        renderActiveSdrChoices();
        function list() {
          return {choices: elements.serial.options.map(option => option.label), selected: elements.serial.value,
            network_controls: !elements.remote_sdr_controls.hidden, local_reset: !elements.reset_rtl_device.hidden,
            network_reset: !elements.reset_remote_sdr.hidden};
        }
    """

    def test_local_and_network_sdrs_are_in_one_list(self) -> None:
        result = self.run_scenario(self.SETUP + """
            results.local = list();
            remoteSdr = {devices: remoteSdr.devices, remote: {source_mode: "remote", device_id: "pi"}};
            renderActiveSdrChoices();
            results.network = list();
        """)["results"]

        self.assertEqual(result["local"]["choices"], ["Nooelec, NESDR SMArt v5, serial 18263784", "Shared by weather-pi over the network"])
        self.assertEqual(result["local"]["selected"], "18263784")
        self.assertEqual(
            (result["local"]["network_controls"], result["local"]["local_reset"], result["local"]["network_reset"]), (False, True, False)
        )
        self.assertEqual(result["network"]["selected"], "remote:pi")
        self.assertEqual(
            (result["network"]["network_controls"], result["network"]["local_reset"], result["network"]["network_reset"]), (True, False, True)
        )

    def test_choosing_from_the_list_switches_between_local_and_network(self) -> None:
        result = self.run_scenario(self.SETUP + """
            await chooseActiveSdr("remote:pi");
            remoteSdr = {devices: remoteSdr.devices, remote: {source_mode: "remote", device_id: "pi"}};
            await chooseActiveSdr("18263784");
        """)

        self.assertEqual(result["sent"], [{"source_mode": "remote", "remote_host_id": "pi"}, {"source_mode": "local", "serial": "18263784"}])

    def test_choosing_another_local_sdr_sends_only_the_serial(self) -> None:
        result = self.run_scenario(self.SETUP + """
            elements.serial.value = "00000001";
            await chooseActiveSdr("00000001");
            await saveNow();
        """)

        self.assertEqual(result["sent"], [{"serial": "00000001"}])
