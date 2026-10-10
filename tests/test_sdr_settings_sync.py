from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_live_audio_playback import load_web_control_module


PAGE_FUNCTIONS = (
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
function status() { return {settings: {...server.settings}, gain_values: GAINS}; }
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
console.log(JSON.stringify({results, sent, server: server.settings}));
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


if __name__ == "__main__":
    unittest.main()
