from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"

PAGE_FUNCTIONS = (
    "openLiveAudioSocket",
    "closeLiveAudioSocket",
    "setListeningReconnectingBanner",
    "liveAudioConnectionHealthy",
    "startLiveAudioWatchdog",
    "liveAudioConnectionLost",
    "currentListeningParams",
    "attemptLiveAudioReconnect",
    "resendLiveAudioPause",
    "retryLiveAudioReconnectNow",
    "cancelLiveAudioReconnect",
    "noteServerRequestFailure",
    "liveAudioSocketPeer",
    "pollReceiverPacketStats",
    "markReceiverPacketProgress",
    "startReceiverPacketStats",
    "stopReceiverForUnstableConnection",
    "scheduleReceiverUnstableStop",
    "clearReceiverUnstableTimer",
    "clearReceiverStatsTimer",
)
PAGE_STATE = (
    "const SERVER_UNREACHABLE_GRACE_MS",
    "let serverUnavailable",
    "let serverFailureCount",
    "let serverUnreachableSince",
    "let receiverPlaying",
    "let receiverUnstableTimer",
    "let receiverStatsTimer",
    "let receiverLastPacketCount",
    "let receiverLastPacketAt",
    "const MONITOR_UNSTABLE_TIMEOUT_MS",
    "const MONITOR_STATS_INTERVAL_MS",
)

# Runs the page's own live audio connection code against a pretend clock, network
# and WebSocket. While the network is down, connecting hangs (as it does on a phone
# between networks) and established connections go silent without closing. The
# scenario moves time on and changes the network; the result is when audio played,
# what the listener saw and heard about, and what the page asked the server.
HARNESS = r"""
let clock = 0;
const performance = {now: () => clock * 1000};
Date.now = () => clock * 1000;
let timers = [];
let timerId = 0;
function setTimeout(callback, ms = 0) { timers.push({id: ++timerId, due: clock + ms / 1000, callback}); return timerId; }
function setInterval(callback, ms) { timers.push({id: ++timerId, due: clock + ms / 1000, callback, every: ms / 1000}); return timerId; }
function clearTimeout(id) { timers = timers.filter(timer => timer.id !== id); }
const clearInterval = clearTimeout;
async function settle() { for (let index = 0; index < 20; index += 1) await null; }
async function advanceTo(time) {
  while (true) {
    await settle();
    const due = timers.filter(timer => timer.due <= time).sort((a, b) => a.due - b.due || a.id - b.id)[0];
    if (!due) break;
    clock = due.due;
    if (due.every) due.due += due.every;
    else clearTimeout(due.id);
    due.callback();
    await settle();
    noteView();
  }
  clock = time;
  await settle();
  noteView();
}

let network = "up";
let connectSeconds = 0.1;
const sockets = [];
class WebSocket {
  constructor(url) {
    this.url = url;
    this.n = sockets.length;
    this.readyState = WebSocket.CONNECTING;
    this.listeners = {};
    this.alive = network === "up";
    sockets.push(this);
    if (this.alive) setTimeout(() => {
      if (this.readyState !== WebSocket.CONNECTING) return;
      this.readyState = WebSocket.OPEN;
      this.emit("open");
    }, connectSeconds * 1000);
  }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  emit(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); }
  close() {
    if (this.readyState >= WebSocket.CLOSING) return;
    const wasConnecting = this.readyState === WebSocket.CONNECTING;
    this.readyState = WebSocket.CLOSED;
    setTimeout(() => {
      if (wasConnecting) this.emit("error");
      this.emit("close", {code: 1006});
    });
  }
}
WebSocket.CONNECTING = 0;
WebSocket.OPEN = 1;
WebSocket.CLOSING = 2;
WebSocket.CLOSED = 3;
// The phone moves to a new network: what was connected over the old one goes silent.
function networkSwitch() {
  network = "down";
  for (const socket of sockets) socket.alive = false;
}
function networkBack() { network = "up"; }
// The browser notices on its own that a connection is gone.
function connectionCloses() { const socket = liveAudioSocket; socket.alive = false; socket.readyState = WebSocket.CLOSED; socket.emit("close", {code: 1006}); }
// Audio every 20 ms on every connection the server still reaches.
const played = [];
async function handleLiveAudioMessage(event) {
  liveAudioPacketCount += 1;
  clearReceiverUnstableTimer();
  played.push({at: clock, socket: event.socket});
}
function serverSends() {
  for (const socket of sockets) {
    if (socket.alive && socket.readyState === WebSocket.OPEN) socket.emit("message", {socket: socket.n, data: new ArrayBuffer(16)});
  }
}
async function listenUntil(time) {
  while (clock + 0.02 <= time + 1e-9) {
    await advanceTo(Math.round((clock + 0.02) * 100) / 100);
    serverSends();
  }
}

let liveAudioContext = {state: "running", resume() { return Promise.resolve(); }};
let tapNeededToResumeAudio = false;
async function ensureLiveAudioContext() {
  // Phones may hold back resuming audio until the next tap.
  if (tapNeededToResumeAudio) await new Promise(() => {});
  return liveAudioContext;
}
async function supportsWebCodecsOpusAudio() { return true; }
function noteInsecureAudioFallback() {}
function closeLiveAudioDecoder() {}
function stopScheduledLiveAudioSources() {}
function resetLiveAudioThroughputWindow() {}
function resetLiveAudioServerAudioState() {}
function applyLiveAudioVolume() {}
function liveAudioUrl(params) { return "wss://radio.example/api/live-audio?" + new URLSearchParams(params); }
// The listener's choices, as the page holds them.
const NWR_RECEIVER_CHANNELS = [{frequency_hz: 162425000}, {frequency_hz: 162475000}, {frequency_hz: 162550000}];
let receiverChannelIndex = 1;
function currentReceiverChannel() { return NWR_RECEIVER_CHANNELS[receiverChannelIndex]; }
function pageReceiverClientId() { return "phone-receiver"; }
function pageMonitorClientId() { return "phone-monitor"; }
let monitorStreamId = "";
const console = {debug() {}, log() {}, warn() {}};
const window = {};
function scheduleMonitorUnstableStop(reason) {}
// Listening stopped with "your connection is too unstable".
const toldUnstable = [];
const results = {};
async function stopWeatherReceiver(options) {
  if (options.unstable) { toldUnstable.push(clock); results.sockets_when_stopped = sockets.length; }
  receiverPlaying = false;
  receiverPeerConnection = null;
  cancelLiveAudioReconnect();
}
const asked = [];
async function request(path, options) { asked.push(path); return {}; }
const logged = [];
function logClientEvent(level, area, message, details) { logged.push({at: clock, message, details}); }
const unavailable = [];
function enterServerUnavailable(reason) { if (serverUnavailable) return; unavailable.push({at: clock, reason}); serverUnavailable = true; }
const elements = {
  listening_reconnecting_banner: {hidden: true},
  listening_status_live: {textContent: ""}
};
const document = {getElementById: id => elements[id] || null};
const shown = [];
function noteView() {
  const view = {banner: !elements.listening_reconnecting_banner.hidden, said: elements.listening_status_live.textContent};
  const last = shown[shown.length - 1];
  if (!last || last.banner !== view.banner || last.said !== view.said) shown.push({at: clock, ...view});
}

let monitorPeerConnection = null;
let monitorPaused = false;
let receiverPeerConnection = null;
let receiverPaused = false;
async function listenToReceiver() {
  const socket = await openLiveAudioSocket(currentListeningParams("receiver"));
  receiverPeerConnection = liveAudioSocketPeer("receiver", socket);
  receiverPlaying = true;
  startReceiverPacketStats();
}
// Asking for another channel changes it on the server without a new connection.
function tuneTo(index) { receiverChannelIndex = index; }
// When audio first plays again after the given time.
function audioBackAfter(time) { const item = played.find(entry => entry.at > time); return item ? item.at : null; }

(async () => {
  const started = listenToReceiver();
  await advanceTo(0.2);
  await started;
SCENARIO
results.sockets = sockets.length;
results.asked_for = sockets.map(socket => Object.fromEntries(new URL(socket.url).searchParams));
results.shown = shown;
results.asked = asked;
results.logged = logged.map(item => item.message);
results.reasons = logged.filter(item => item.details && item.details.reason).map(item => item.details.reason);
results.unavailable = unavailable;
results.told_unstable = toldUnstable;
results.listening_on_current = Boolean(receiverPeerConnection && receiverPeerConnection.socket === liveAudioSocket && liveAudioSocket);
results.played_from = [...new Set(played.map(item => item.socket))];
console.log = undefined;
process.stdout.write(JSON.stringify(results));
})().catch(error => { process.stderr.write(String(error && error.stack || error)); process.exit(1); });
"""

LOST = "Lost the connection to NWR Stream Manager. Listening will continue when it's back."
BACK = "Reconnected to NWR Stream Manager."


def load_web_control_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    spec = importlib.util.spec_from_file_location("nwr_stream_manager.web_control", PACKAGE_PATH / "web_control.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.web_control"] = module
    spec.loader.exec_module(module)
    return module


class LiveAudioReconnectTests(unittest.TestCase):
    """Phones switching between Wi-Fi and cellular keep listening."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        page = load_web_control_module().INDEX_HTML
        parts = [line for line in re.findall(r"^(?:const LIVE_AUDIO_|let liveAudio).*$", page, re.M) if not line.startswith("let liveAudioContext ")]
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
        script = self.page_code + HARNESS.replace("SCENARIO", scenario)
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "reconnect.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([self.node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_a_network_switch_pauses_and_carries_on(self) -> None:
        # Connecting while between networks hangs; the page must not wait on that.
        for down in (5, 8, 20):
            with self.subTest(seconds_without_a_network=down):
                result = self.run_scenario(f"""
                    await listenUntil(5);
                    networkSwitch();
                    await listenUntil({5 + down});
                    networkBack();
                    await listenUntil({5 + down + 10});
                    results.back = audioBackAfter({5 + down});
                """)
                self.assertTrue(result["listening_on_current"])
                self.assertIsNotNone(result["back"], "listening never came back")
                self.assertLessEqual(result["back"] - (5 + down), 4.5)
                self.assertEqual(
                    [(view["banner"], view["said"]) for view in result["shown"]],
                    [(False, ""), (True, LOST), (False, BACK)],
                )
                self.assertEqual(result["reasons"], ["nothing received"])
                self.assertEqual(result["unavailable"], [])
                self.assertEqual(result["asked"], [])
                self.assertEqual(result["told_unstable"], [])

    def test_reconnecting_keeps_the_channel_being_listened_to(self) -> None:
        result = self.run_scenario("""
            await listenUntil(2);
            tuneTo(0);  // 162.425 MHz
            await listenUntil(4);
            connectionCloses();
            await listenUntil(6);
        """)
        self.assertEqual([item["frequency_hz"] for item in result["asked_for"]], ["162475000", "162425000"])
        self.assertTrue(result["listening_on_current"])

    def test_reconnecting_keeps_the_stream_being_monitored(self) -> None:
        result = self.run_scenario("""
            closeLiveAudioSocket();
            receiverPeerConnection = null;
            monitorStreamId = "wxn99";
            const opening = openLiveAudioSocket(currentListeningParams("monitor"));
            await advanceTo(0.5);
            monitorPeerConnection = liveAudioSocketPeer("monitor", await opening);
            await listenUntil(2);
            connectionCloses();
            await listenUntil(4);
            results.monitoring_on_current = monitorPeerConnection.socket === liveAudioSocket;
        """)
        self.assertEqual(result["asked_for"][-1], {"mode": "monitor", "client_id": "phone-monitor", "stream_id": "wxn99", "codec": "opus"})
        self.assertEqual(len(result["asked_for"]), 3)
        self.assertTrue(result["monitoring_on_current"])

    def test_back_to_back_network_switches_keep_listening(self) -> None:
        # Wi-Fi to cellular, then straight back to Wi-Fi, after listening a while.
        result = self.run_scenario("""
            await listenUntil(60);
            networkSwitch();
            await listenUntil(70);
            networkBack();
            await listenUntil(75);
            networkSwitch();
            await listenUntil(83);
            networkBack();
            await listenUntil(150);
            results.back = audioBackAfter(83);
        """)
        self.assertEqual(result["told_unstable"], [])
        self.assertTrue(result["listening_on_current"])
        self.assertLessEqual(result["back"] - 83, 4.5)

    def test_a_connection_that_never_comes_back_still_gives_up(self) -> None:
        result = self.run_scenario("""
            await listenUntil(10);
            networkSwitch();
            await listenUntil(60);
        """)
        self.assertEqual(len(result["told_unstable"]), 1)
        self.assertGreaterEqual(result["told_unstable"][0], 10 + 30)
        self.assertLessEqual(result["told_unstable"][0], 10 + 40)
        self.assertEqual(result["sockets"], result["sockets_when_stopped"])  # no more attempts after giving up
        self.assertFalse(result["listening_on_current"])

    def test_coming_back_online_retries_at_once(self) -> None:
        result = self.run_scenario("""
            await listenUntil(5);
            networkSwitch();
            await listenUntil(13);
            networkBack();
            retryLiveAudioReconnectNow();  // the browser's "online" event
            await listenUntil(20);
            results.back = audioBackAfter(13);
        """)
        self.assertLessEqual(result["back"] - 13, 0.5)

    def test_reconnecting_does_not_need_a_tap(self) -> None:
        # A phone may not resume audio until the next tap; reconnecting must not wait for it.
        result = self.run_scenario("""
            tapNeededToResumeAudio = true;
            liveAudioContext.state = "suspended";
            await listenUntil(5);
            networkSwitch();
            await listenUntil(10);
            networkBack();
            await listenUntil(20);
            results.back = audioBackAfter(10);
        """)
        self.assertIsNotNone(result["back"], "listening never came back")
        self.assertLessEqual(result["back"] - 10, 4.5)

    def test_a_blip_is_not_announced(self) -> None:
        result = self.run_scenario("""
            await listenUntil(2);
            connectionCloses();
            await listenUntil(5);
            results.back = audioBackAfter(2);
        """)
        self.assertTrue(result["listening_on_current"])
        self.assertLessEqual(result["back"] - 2, 0.5)
        self.assertEqual([(view["banner"], view["said"]) for view in result["shown"]], [(False, "")])

    def test_a_healthy_connection_is_left_alone(self) -> None:
        result = self.run_scenario("""
            await listenUntil(20);
        """)
        self.assertEqual(result["sockets"], 1)
        self.assertEqual(result["logged"], [])

    def test_stopping_while_reconnecting_stays_stopped(self) -> None:
        result = self.run_scenario("""
            connectSeconds = 2;  // a slow network: the attempt is still connecting
            await listenUntil(2);
            connectionCloses();
            await listenUntil(3);
            receiverPeerConnection = null;
            cancelLiveAudioReconnect();
            await listenUntil(10);
            results.socket_left_open = Boolean(liveAudioSocket);
            results.open = sockets.filter(socket => socket.readyState === WebSocket.OPEN).length;
        """)
        self.assertFalse(result["socket_left_open"])
        self.assertEqual(result["open"], 0)
        self.assertEqual(result["shown"][-1]["banner"], False)

    def test_a_paused_listener_stays_paused_after_reconnecting(self) -> None:
        result = self.run_scenario("""
            receiverPaused = true;
            await listenUntil(2);
            connectionCloses();
            await listenUntil(4);
        """)
        self.assertEqual(result["asked"], ["/api/receiver/pause"])

    def test_the_server_is_only_called_unreachable_after_twenty_seconds(self) -> None:
        result = self.run_scenario("""
            closeLiveAudioSocket();  // not listening
            networkSwitch();
            for (let time = 1; time <= 25; time += 1) {
              await advanceTo(time);
              noteServerRequestFailure(new TypeError("Failed to fetch"));
            }
        """)
        self.assertEqual(result["unavailable"], [{"at": 21, "reason": "unreachable"}])

    def test_status_checks_failing_while_audio_arrives_are_not_an_outage(self) -> None:
        result = self.run_scenario("""
            for (let second = 1; second <= 30; second += 1) {
              await listenUntil(second);
              noteServerRequestFailure(new TypeError("Failed to fetch"));
            }
        """)
        self.assertEqual(result["unavailable"], [])


if __name__ == "__main__":
    unittest.main()
