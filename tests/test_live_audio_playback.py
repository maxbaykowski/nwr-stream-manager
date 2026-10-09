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

PLAYER_FUNCTIONS = (
    "stopScheduledLiveAudioSources",
    "dropLiveAudioFrameForLatency",
    "applyLiveAudioRecoveryCrossfade",
    "liveAudioFrameIsQuiet",
    "scheduleLiveAudioSamples",
    "setListeningNoAudioBanner",
    "resetLiveAudioServerAudioState",
    "handleLiveAudioServerNoAudio",
    "handleLiveAudioServerAudioBack",
    "resetLiveAudioThroughputWindow",
    "recordLiveAudioThroughput",
)

# Plays the page's own scheduler against a simulated audio clock. Each step
# advances the clock and delivers 20 ms frames, loud or quiet, or a notice from
# the server; the result is what was played, when, what was skipped, and what
# the listener was shown and told.
HARNESS = r"""
let clock = 0;
const performance = {now: () => clock * 1000};
const played = [];
const events = [];
let playedId = 0;
liveAudioContext = {
  get currentTime() { return clock; },
  createBuffer(channels, length, rate) {
    return {copyToChannel(samples) { this.samples = samples; }, length, rate};
  },
  createBufferSource() {
    return {
      buffer: null,
      connect() {},
      addEventListener() {},
      start(at) { played.push({id: frameId(this.buffer.samples), at, seconds: this.buffer.length / this.buffer.rate}); },
      stop() {}
    };
  }
};
liveAudioMode = "monitor";
const elements = {
  listening_no_audio_banner: {hidden: true},
  listening_status_live: {textContent: ""}
};
const document = {getElementById: id => elements[id] || null};
let timers = [];
let timerId = 0;
function setTimeout(callback, ms) { timers.push({id: ++timerId, due: clock + ms / 1000, callback}); return timerId; }
function clearTimeout(id) { timers = timers.filter(timer => timer.id !== id); }
function runDueTimers() {
  for (const timer of timers.filter(timer => timer.due <= clock)) {
    clearTimeout(timer.id);
    timer.callback();
  }
}
const shown = [];
function noteListenerView() {
  const view = {banner: !elements.listening_no_audio_banner.hidden, said: elements.listening_status_live.textContent};
  const last = shown[shown.length - 1];
  if (!last || last.banner !== view.banner || last.said !== view.said) shown.push({at: clock, ...view});
}
function liveAudioClientIdForCurrentMode() { return "test"; }
const throughput = [];
function logClientEvent(level, area, message, details) {
  if (message === "live audio throughput sample") throughput.push(details);
  else events.push({message, details});
}

// The frame number rides in the last sample, which the recovery crossfade leaves alone.
function frame(id, loud) {
  const level = loud ? 0.5 : 0.001;
  const samples = new Float32Array(480).fill(level);
  samples[479] = level + id * 1e-5;
  return samples;
}
function frameId(samples) {
  const last = samples[479];
  return Math.round((last - (last >= 0.4 ? 0.5 : 0.001)) / 1e-5);
}

for (const step of STEPS) {
  clock = step.at;
  runDueTimers();
  if (step.message === "source_no_audio") handleLiveAudioServerNoAudio();
  if (step.message === "source_audio_back") handleLiveAudioServerAudioBack(step.seconds);
  if (step.message === "connection_opened") resetLiveAudioThroughputWindow(0);
  for (const loud of step.frames || []) {
    recordLiveAudioThroughput(loud ? 300 : 3, 20);
    scheduleLiveAudioSamples(frame(playedId++, loud), 24000);
  }
  noteListenerView();
}
console.log(JSON.stringify({played, events, shown, throughput, sent: playedId}));
"""


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


class LiveAudioPlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.node = shutil.which("node")
        if cls.node is None:
            raise unittest.SkipTest("node is not installed")
        page = load_web_control_module().INDEX_HTML
        parts = re.findall(r"^(?:const LIVE_AUDIO_|let liveAudio).*$", page, re.M)
        for name in PLAYER_FUNCTIONS:
            start = page.index(f"function {name}(")
            parts.append(page[start : page.index("\n}\n", start) + 2])
        cls.player = "\n".join(parts)

    def play(self, steps: list[tuple]) -> dict:
        """Steps are (time, [loud, ...]) frames, or (time, notice) from the server."""
        encoded = [
            {"at": at, "message": item} if isinstance(item, str) else {"at": at, "frames": item}
            for at, item in steps
        ]
        script = self.player + "\nconst STEPS = " + json.dumps(encoded) + ";\n" + HARNESS
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "player.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run([self.node, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    @staticmethod
    def steady(start: float, count: int, loud: bool = True) -> list[tuple[float, list[bool]]]:
        return [(start + index * 0.02, [loud]) for index in range(count)]

    def gaps(self, played: list[dict]) -> list[float]:
        return [
            round(after["at"] - (before["at"] + before["seconds"]), 6)
            for before, after in zip(played, played[1:])
            if abs(after["at"] - (before["at"] + before["seconds"])) > 1e-6
        ]

    def test_late_frames_play_on_without_a_gap_or_a_skip(self) -> None:
        # Wi-Fi holds frames back for 200 ms, then delivers them all at once.
        steps = self.steady(0.0, 50)
        steps.append((1.2, [True] * 10))
        steps += self.steady(1.22, 50)
        result = self.play(steps)

        self.assertEqual(len(result["played"]), result["sent"])
        self.assertEqual(self.gaps(result["played"]), [])
        self.assertEqual(result["events"], [])

    def test_audio_that_runs_dry_restarts_once_and_catches_up_only_in_silence(self) -> None:
        # A 600 ms stall drains the queue; the backlog then arrives at once, a
        # SAME burst (loud) followed by the silence between bursts.
        steps = self.steady(0.0, 50)
        steps.append((1.6, [True] * 30))
        steps += self.steady(1.62, 30, loud=True)
        steps += self.steady(2.22, 50, loud=False)
        steps += self.steady(3.22, 50, loud=True)
        result = self.play(steps)

        played_ids = {item["id"] for item in result["played"]}
        loud_ids = set(range(0, 110)) | set(range(160, 210))
        self.assertTrue(loud_ids <= played_ids, "a loud frame was skipped")
        self.assertEqual([event["message"] for event in result["events"]][:1], ["live audio ran out before more arrived"])
        self.assertEqual(sum(event["message"] == "live audio ran out before more arrived" for event in result["events"]), 1)
        skips = [event["details"]["reason"] for event in result["events"] if event["message"].startswith("dropped")]
        self.assertEqual(set(skips), {"quiet"})
        # Back near the target once the silence was trimmed.
        last = result["played"][-1]
        self.assertLess(last["at"] + last["seconds"] - 4.2, 0.35)

    def test_frames_are_skipped_regardless_only_far_past_the_quiet_limit(self) -> None:
        # With nothing quiet to skip, only an extreme backlog forces a skip.
        steps = self.steady(0.0, 20)
        steps.append((0.4, [True] * 15))
        steps += self.steady(0.42, 100)
        result = self.play(steps)
        self.assertEqual(len(result["played"]), result["sent"])

        steps = self.steady(0.0, 20)
        steps.append((0.4, [True] * 50))
        steps += self.steady(0.42, 50)
        result = self.play(steps)
        reasons = {event["details"]["reason"] for event in result["events"] if event["message"].startswith("dropped")}
        self.assertEqual(reasons, {"above-max"})

    def messages(self, result: dict) -> list[str]:
        return [event["message"] for event in result["events"]]

    def test_a_server_outage_is_not_blamed_on_the_connection(self) -> None:
        # The server says it has no audio 250 ms after its last frame, then sends again.
        steps = self.steady(0.0, 50)
        steps.append((1.25, "source_no_audio"))
        steps.append((1.6, "source_audio_back"))
        steps += self.steady(1.6, 50)
        result = self.play(steps)

        self.assertIn("server had no audio to send", self.messages(result))
        self.assertNotIn("live audio ran out before more arrived", self.messages(result))
        # Too short to bother the listener with a banner.
        self.assertEqual(result["shown"], [{"at": 0.0, "banner": False, "said": ""}])

    def test_audio_held_up_on_the_way_is_still_blamed_on_the_connection(self) -> None:
        steps = self.steady(0.0, 50)
        steps += self.steady(1.6, 50)  # no notice: the server was sending all along
        result = self.play(steps)

        self.assertEqual(self.messages(result), ["live audio ran out before more arrived"])

    def test_time_spent_connecting_does_not_look_like_a_slow_connection(self) -> None:
        # The first audio comes a third of a second after the connection opens;
        # the server lowers the bitrate if audio seems to arrive slower than real time.
        steps = [(1.0, "connection_opened")]
        steps += self.steady(1.35, 300)
        result = self.play(steps)

        ratios = [sample["media_delivery_ratio"] for sample in result["throughput"]]
        self.assertGreaterEqual(len(ratios), 2)
        for ratio in ratios:
            self.assertGreater(ratio, 0.97)

    def test_new_connections_start_measuring_at_their_first_audio(self) -> None:
        page = load_web_control_module().INDEX_HTML
        start = page.index("async function startLiveAudioSocket(")
        self.assertIn("resetLiveAudioThroughputWindow(0);", page[start : page.index("\n}\n", start)])

    def test_a_long_outage_shows_the_banner_until_audio_returns(self) -> None:
        steps = self.steady(0.0, 50)
        steps.append((1.25, "source_no_audio"))
        steps += [(1.25 + index * 0.5, []) for index in range(1, 12)]  # time passes
        steps.append((7.0, "source_audio_back"))
        steps += self.steady(7.0, 10)
        result = self.play(steps)

        self.assertEqual(
            [(view["banner"], view["said"]) for view in result["shown"]],
            [
                (False, ""),
                (True, "No audio is coming from the SDR right now. Listening will continue when it returns."),
                (False, "Audio from the SDR is back."),
            ],
        )
        self.assertAlmostEqual(result["shown"][1]["at"], 3.25, places=2)

    def test_fallback_audio_turned_on_or_off_during_an_outage_updates_the_banner(self) -> None:
        # The SDR stays dead throughout. Turning fallback audio on makes the
        # stream send audio again; turning it off later stops it again.
        steps = [(0.0, "source_no_audio")]
        steps += [(index * 0.5, []) for index in range(1, 7)]
        steps.append((3.5, "source_audio_back"))  # fallback audio turned on
        steps += self.steady(3.5, 100)
        steps.append((5.75, "source_no_audio"))  # fallback audio turned off
        steps += [(5.75 + index * 0.5, []) for index in range(1, 7)]
        result = self.play(steps)

        self.assertEqual([view["banner"] for view in result["shown"]], [False, True, False, True])
        self.assertAlmostEqual(result["shown"][3]["at"], 7.75, places=2)


if __name__ == "__main__":
    unittest.main()
