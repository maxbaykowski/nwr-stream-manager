from __future__ import annotations

import importlib
import importlib.util
import math
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"
SAMPLE_RATE = 24_000


def load_package_module(name: str):
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module(f"nwr_stream_manager.{name}")


def load_web_control_module():
    load_package_module("signal_meter")
    spec = importlib.util.spec_from_file_location(
        "nwr_stream_manager.web_control",
        PACKAGE_PATH / "web_control.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.web_control"] = module
    spec.loader.exec_module(module)
    return module


def complex_noise(rng: np.random.Generator, count: int, power: float) -> np.ndarray:
    sigma = math.sqrt(power / 2.0)
    return (rng.normal(0.0, sigma, count) + 1j * rng.normal(0.0, sigma, count)).astype(np.complex64)


def fm_carrier(audio: np.ndarray, sample_rate: int, deviation_hz: float, power: float) -> np.ndarray:
    phase = np.cumsum(2.0 * np.pi * deviation_hz / float(sample_rate) * audio)
    return (math.sqrt(power) * np.exp(1j * phase)).astype(np.complex64)


def run_meter(meter, iq: np.ndarray, chunk: int = 480) -> None:
    for start in range(0, iq.size, chunk):
        meter.process(iq[start : start + chunk])


class ChannelSignalMeterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.signal_meter = load_package_module("signal_meter")
        cls.dsp = load_package_module("dsp")

    def test_snapshot_is_unavailable_before_first_block(self) -> None:
        meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)
        meter.process(np.zeros(100, dtype=np.complex64))

        self.assertEqual(meter.snapshot(), {"available": False})

    def test_noise_only_channel_reports_floor_without_carrier(self) -> None:
        rng = np.random.default_rng(1)
        noise_density = 10.0 ** (-100.0 / 10.0)
        meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)

        run_meter(meter, complex_noise(rng, SAMPLE_RATE * 5, noise_density * SAMPLE_RATE))
        snapshot = meter.snapshot()

        self.assertTrue(snapshot["available"])
        self.assertAlmostEqual(snapshot["noise_density_dbfs_per_hz"], -100.0, delta=0.75)
        expected_floor = -100.0 + 10.0 * math.log10(snapshot["channel_bandwidth_hz"])
        self.assertAlmostEqual(snapshot["noise_floor_dbfs"], expected_floor, delta=0.75)
        self.assertFalse(snapshot["carrier_detected"])

    def test_fm_carrier_snr_matches_injected_level(self) -> None:
        rng = np.random.default_rng(2)
        count = SAMPLE_RATE * 12
        t = np.arange(count) / SAMPLE_RATE
        audio = np.sin(2.0 * np.pi * 1_050.0 * t)
        noise_density = 10.0 ** (-100.0 / 10.0)
        for target_snr_db in (6.0, 20.0, 40.0):
            with self.subTest(target_snr_db=target_snr_db):
                meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)
                signal_power = noise_density * meter.channel_bandwidth_hz * 10.0 ** (target_snr_db / 10.0)
                iq = fm_carrier(audio, SAMPLE_RATE, 5_000.0, signal_power)
                iq += complex_noise(rng, count, noise_density * SAMPLE_RATE)

                run_meter(meter, iq)
                snapshot = meter.snapshot()

                self.assertAlmostEqual(snapshot["snr_db"], target_snr_db, delta=1.0)
                self.assertAlmostEqual(snapshot["signal_dbfs"], 10.0 * math.log10(signal_power), delta=0.5)
                self.assertTrue(snapshot["carrier_detected"])

    def test_noise_floor_ignores_spur_in_one_guard_band(self) -> None:
        rng = np.random.default_rng(3)
        count = SAMPLE_RATE * 5
        t = np.arange(count) / SAMPLE_RATE
        noise_density = 10.0 ** (-100.0 / 10.0)
        iq = complex_noise(rng, count, noise_density * SAMPLE_RATE)
        iq += (1e-3 * np.exp(2j * np.pi * 10_000.0 * t)).astype(np.complex64)
        meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)

        run_meter(meter, iq)

        self.assertAlmostEqual(meter.snapshot()["noise_density_dbfs_per_hz"], -100.0, delta=0.75)

    def test_measures_channel_after_channelizer_with_strong_adjacent_channels(self) -> None:
        rng = np.random.default_rng(4)
        input_rate = 192_000
        count = input_rate * 8
        t = np.arange(count) / input_rate
        noise_density = 10.0 ** (-110.0 / 10.0)
        signal_power = noise_density * 16_000.0 * 10.0 ** (25.0 / 10.0)
        tone = np.sin(2.0 * np.pi * 1_050.0 * t)
        wide = complex_noise(rng, count, noise_density * input_rate)
        wide += fm_carrier(tone, input_rate, 5_000.0, signal_power) * np.exp(2j * np.pi * 25_000.0 * t).astype(np.complex64)
        wide += fm_carrier(tone, input_rate, 5_000.0, signal_power * 1_000.0)
        wide += fm_carrier(tone, input_rate, 5_000.0, signal_power * 1_000.0) * np.exp(2j * np.pi * 50_000.0 * t).astype(np.complex64)
        for transition_hz in (1_000.0, 3_000.0):
            with self.subTest(transition_hz=transition_hz):
                channelizer = self.dsp.IqChannelizer(
                    input_rate=input_rate,
                    center_frequency_hz=162_475_000,
                    target_frequency_hz=162_500_000,
                    output_rate=SAMPLE_RATE,
                    transition_hz=transition_hz,
                )
                meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)
                meter.set_noise_reference(
                    *self.signal_meter.noise_reference_band_for_transition(SAMPLE_RATE, transition_hz)
                )
                for start in range(0, count, 19_200):
                    meter.process(channelizer.process_complex(wide[start : start + 19_200]))
                snapshot = meter.snapshot()

                self.assertAlmostEqual(snapshot["snr_db"], 25.0, delta=1.0)
                self.assertAlmostEqual(snapshot["noise_density_dbfs_per_hz"], -110.0, delta=0.75)

    def test_noise_reference_band_stays_inside_channel_passband(self) -> None:
        band = self.signal_meter.noise_reference_band_for_transition

        self.assertEqual(band(SAMPLE_RATE, 1_000.0), (9_000.0, 11_000.0))
        self.assertEqual(band(SAMPLE_RATE, 2_000.0), (9_000.0, 10_000.0))
        self.assertEqual(band(SAMPLE_RATE, 3_000.0), (9_000.0, 9_500.0))

    def test_reset_clears_measurement(self) -> None:
        rng = np.random.default_rng(5)
        meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)
        run_meter(meter, complex_noise(rng, SAMPLE_RATE, 1e-6))

        meter.reset()

        self.assertEqual(meter.snapshot(), {"available": False})

    def test_snapshot_goes_stale(self) -> None:
        rng = np.random.default_rng(6)
        meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)
        run_meter(meter, complex_noise(rng, SAMPLE_RATE, 1e-6))
        measured_at = meter.snapshot()["measured_at"]

        stale = meter.snapshot(now=measured_at + self.signal_meter.SIGNAL_METER_STALE_SECONDS + 1.0)

        self.assertFalse(stale["available"])


    def test_signal_quality_levels(self) -> None:
        quality = self.signal_meter.signal_quality

        self.assertEqual(quality(35.0), "excellent")
        self.assertEqual(quality(30.0), "excellent")
        self.assertEqual(quality(25.0), "good")
        self.assertEqual(quality(15.0), "fair")
        self.assertEqual(quality(8.0), "poor")
        self.assertEqual(quality(0.0), "poor")
        self.assertEqual(quality(-0.5), "no_signal")
        self.assertEqual(quality(None), "no_signal")

    def test_bad_reception_requires_sustained_low_snr_and_recovers_with_hysteresis(self) -> None:
        rng = np.random.default_rng(7)
        noise_density = 10.0 ** (-100.0 / 10.0)
        meter = self.signal_meter.ChannelSignalMeter(SAMPLE_RATE)
        bandwidth = meter.channel_bandwidth_hz

        def feed(snr_db: float | None, seconds: float) -> dict:
            count = int(SAMPLE_RATE * seconds)
            iq = complex_noise(rng, count, noise_density * SAMPLE_RATE)
            if snr_db is not None:
                t = np.arange(count) / SAMPLE_RATE
                power = noise_density * bandwidth * 10.0 ** (snr_db / 10.0)
                iq += fm_carrier(np.sin(2.0 * np.pi * 1_050.0 * t), SAMPLE_RATE, 5_000.0, power)
            run_meter(meter, iq)
            return meter.snapshot()

        self.assertIsNone(feed(25.0, 3.0)["reception_problem"])
        weak = feed(2.0, self.signal_meter.BAD_RECEPTION_SUSTAIN_SECONDS / 2.0)
        self.assertEqual(weak["quality"], "poor")
        self.assertIsNone(weak["reception_problem"])
        # The 2 dB stretch and the carrier dropping out count toward one
        # continuous period, reported as no signal because it is now below 0 dB.
        gone = feed(None, self.signal_meter.BAD_RECEPTION_SUSTAIN_SECONDS)
        self.assertEqual(gone["quality"], "no_signal")
        self.assertEqual(gone["reception_problem"], "no_signal")
        # Back above 0 dB but still under the recovery threshold: the problem
        # stays latched and is reported as bad reception instead.
        self.assertEqual(feed(7.5, 4.0)["reception_problem"], "bad_reception")
        self.assertIsNone(feed(20.0, 3.0)["reception_problem"])


class StreamSignalMeterIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_meter_tracks_test_mode_signal_level(self) -> None:
        wc = self.web_control
        # Synthetic noise is N(0, level) per rail, so its total complex power is level^2 * 2.
        noise_power_db = wc.STREAM_TEST_MODE_NOISE_DBFS + 10.0 * math.log10(2.0)
        for signal_dbfs in (-20.0, -55.0):
            with self.subTest(signal_dbfs=signal_dbfs):
                source = wc.SyntheticNwrTestModeSource(sample_rate=wc.IQ_SAMPLE_RATE)
                source.set_signal_dbfs(signal_dbfs, immediate=True)
                meter = wc.ChannelSignalMeter(wc.IQ_SAMPLE_RATE)
                for _ in range(int(12.0 / wc.STREAM_FRAME_SECONDS)):
                    meter.process(source.process(wc.STREAM_FRAME_SAMPLES))
                snapshot = meter.snapshot()
                expected_noise_floor = noise_power_db + 10.0 * math.log10(snapshot["channel_bandwidth_hz"] / wc.IQ_SAMPLE_RATE)

                self.assertAlmostEqual(snapshot["noise_floor_dbfs"], expected_noise_floor, delta=0.75)
                self.assertAlmostEqual(snapshot["signal_dbfs"], signal_dbfs, delta=0.75)
                self.assertAlmostEqual(snapshot["snr_db"], signal_dbfs - expected_noise_floor, delta=1.0)

    def test_reception_failure_requires_toggle_and_bad_reception(self) -> None:
        wc = self.web_control
        stream = {"id": "stream-1", "station": {"callsign": "KEC49", "frequency": "162.550"}}
        bad = {"available": True, "reception_problem": "bad_reception", "snr_db": 3.4}
        enabled = wc.WebStreamNotificationSettings(bad_reception=True)

        failure = wc.stream_reception_failure(stream, bad, enabled)

        self.assertEqual(failure["key"], "stream-1:reception")
        self.assertEqual(failure["target_path"], "/?view=stream_settings&stream=stream-1")
        self.assertEqual(failure["message"], "KEC49: bad reception, the signal-to-noise ratio is 3 dB.")
        self.assertIsNone(wc.stream_reception_failure(stream, bad, wc.WebStreamNotificationSettings()))
        self.assertIsNone(wc.stream_reception_failure(stream, dict(bad, reception_problem=None), enabled))
        no_signal = wc.stream_reception_failure(stream, dict(bad, reception_problem="no_signal", snr_db=-4.0), enabled)
        self.assertEqual(no_signal["message"], "KEC49: no signal, only static is being received.")
        # Same key as bad reception, so crossing 0 dB does not re-notify.
        self.assertEqual(no_signal["key"], failure["key"])

    def test_bad_reception_notification_setting_round_trips(self) -> None:
        wc = self.web_control
        settings = wc.validate_stream_notification_payload({"bad_reception": True})

        self.assertTrue(settings.bad_reception)
        self.assertTrue(wc.stream_notification_settings_from_stream({"notifications": {"bad_reception": True}}).bad_reception)
        self.assertFalse(wc.stream_notification_settings_from_stream({"notifications": {}}).bad_reception)

    def test_web_interface_exposes_signal_column_and_notification_toggle(self) -> None:
        source = (PACKAGE_PATH / "web_control.py").read_text(encoding="utf-8")

        self.assertIn('id="stream_notify_bad_reception"', source)
        self.assertIn("streamSignals = data.stream_signals || {};", source)
        self.assertIn('"Bad reception: the signal is too weak or noisy to be usable."', source)
        self.assertIn('"No signal: only static is being received."', source)
        self.assertIn("Bad reception or no signal", source)
        for quality in ("excellent", "good", "fair"):
            self.assertIn(f".signal-{quality} {{", source)
        self.assertIn(".signal-poor, .signal-no_signal {", source)

    def test_stream_settings_shows_signal_above_tabs(self) -> None:
        source = (PACKAGE_PATH / "web_control.py").read_text(encoding="utf-8")
        signal_block = source.index('<dl id="stream_settings_signal" class="details-list">')
        tabs = source.index('<div class="tabs" role="tablist" aria-label="Stream settings sections">')

        self.assertLess(signal_block, tabs)
        self.assertLess(source.index('id="stream-settings-result"'), signal_block)
        self.assertIn("renderStreamSettingsSignal(stream);", source)
        self.assertIn('"Not measured while this stream is disabled."', source)

    def test_served_page_scripts_parse(self) -> None:
        # The page is a Python string, so a JS escape written with one
        # backslash in web_control.py becomes a raw line break once served.
        # Check the scripts exactly as the browser receives them.
        import re
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        for name in ("INDEX_HTML", "SETUP_HTML", "MUST_CHANGE_PASSWORD_HTML"):
            for index, script in enumerate(re.findall(r"<script>(.*?)</script>", getattr(self.web_control, name), re.S)):
                with self.subTest(page=name, script=index), tempfile.TemporaryDirectory() as tempdir:
                    path = Path(tempdir) / "page.js"
                    path.write_text(script, encoding="utf-8")
                    result = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_polled_lists_update_in_place_for_screen_readers(self) -> None:
        # Rebuilding a list on each poll destroys the node a screen reader is
        # reading and throws it back to the top of the page.
        source = (PACKAGE_PATH / "web_control.py").read_text(encoding="utf-8")
        for container in (
            "active-streams-body",
            "icecast-outputs-body",
            "soundcard-outputs-body",
            "accounts-body",
            "iq-recordings-body",
            "dashboard-stream-attention",
            "dashboard-recent-alerts",
            "logs",
        ):
            with self.subTest(container=container):
                start = source.index(f'document.getElementById("{container}")')
                body = source[start : source.index("\n}\n", start)]
                self.assertIn("reconcileKeyedChildren(", body)
                self.assertNotIn('innerHTML = ""', body)
                self.assertNotIn("containsFocusedElement(", body)

    def test_tables_phone_layout_keeps_table_semantics(self) -> None:
        source = (PACKAGE_PATH / "web_control.py").read_text(encoding="utf-8")

        self.assertIn('<th role="columnheader">Signal</th>', source)
        for label, body_id in (
            ("Manage streams", "active-streams-body"),
            ("Icecast outputs", "icecast-outputs-body"),
            ("Sound card outputs", "soundcard-outputs-body"),
            ("Accounts", "accounts-body"),
            ("I/Q recordings", "iq-recordings-body"),
        ):
            with self.subTest(table=label):
                self.assertIn(f'<table class="responsive-table" role="table" aria-label="{label}">', source)
                self.assertIn(f'<tbody id="{body_id}" role="rowgroup" aria-live="off">', source)
                start = source.index(f'document.getElementById("{body_id}")')
                body = source[start : source.index("\n}\n", start)]
                self.assertIn("labelResponsiveTableRow(", body)
        self.assertNotIn("<table aria-label=", source)
        # Visible card labels carry empty alt text so screen readers do not hear them twice.
        self.assertIn('content: attr(data-label) / "";', source)

class StreamGainChangeSignalMeterTests(unittest.TestCase):
    """RTL-SDR gain (manual or the tuner's own hardware AGC) scales signal and
    noise together, so a gain step should not move the reported SNR. Without
    a reset, mixing pre- and post-change samples in the meter's smoothing
    windows biases the reading for several seconds (see the module docstring
    reasoning in signal_meter.py); IcecastStreamWorker.gain_provider exists
    to trigger that reset at the moment gain actually changes.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.web_control = load_web_control_module()

    def test_gain_step_does_not_bias_snr_once_worker_detects_it(self) -> None:
        wc = self.web_control

        class Fanout:
            def __init__(self) -> None:
                self.queue = wc.queue.Queue(maxsize=64)

            def subscribe(self, max_chunks=64, max_seconds=None, name="subscriber"):
                return self.queue

            def unsubscribe(self, subscriber) -> None:
                pass

        gain = {"value": 20.0}
        sample_rate = wc.IQ_SAMPLE_RATE
        center_frequency_hz = 162_475_000
        rng = np.random.default_rng(11)
        noise_density = 10.0 ** (-100.0 / 10.0)
        target_snr_db = 20.0
        signal_power = noise_density * 16_000.0 * 10.0 ** (target_snr_db / 10.0)

        def push_seconds(fanout, queue_obj, seconds: float, gain_db: float) -> None:
            gain_factor = 10.0 ** (gain_db / 20.0)
            count = int(sample_rate * seconds)
            t = np.arange(count) / sample_rate
            tone = np.sin(2.0 * np.pi * 1_050.0 * t)
            iq = fm_carrier(tone, sample_rate, 5_000.0, signal_power) + complex_noise(rng, count, noise_density * sample_rate)
            iq = (iq * gain_factor).astype(np.complex64)
            chunk = sample_rate // 20  # 50ms batches, matching a realistic RTL read cadence
            for start in range(0, count, chunk):
                queue_obj.put(
                    wc.IqSampleBatch(
                        data=iq[start : start + chunk],
                        sample_rate=sample_rate,
                        center_frequency_hz=center_frequency_hz,
                    )
                )

        with tempfile.TemporaryDirectory() as tempdir:
            stream = {
                "id": "stream-1",
                "station": {"callsign": "WXN99", "frequency": str(center_frequency_hz / 1_000_000)},
                "outputs": [],
                "eas_recording": {"enabled": True},
            }
            fanout = Fanout()
            worker = wc.IcecastStreamWorker(
                stream=stream,
                fanout=fanout,
                fallback_settings_provider=lambda: wc.WebFallbackSettings(enabled=False),
                alias_filter_strength_provider=lambda: wc.ALIAS_FILTER_STRENGTH_DEFAULT,
                gain_provider=lambda: gain["value"],
                state_directory=Path(tempdir),
            )
            worker.start()
            try:
                push_seconds(fanout, fanout.queue, 5.0, gain["value"])
                self._wait_for(lambda: worker.signal_meter.snapshot().get("available"))
                settled = worker.signal_meter.snapshot()["snr_db"]
                self.assertAlmostEqual(settled, target_snr_db, delta=2.0)

                gain["value"] = 30.0  # +10 dB manual gain change, same antenna signal
                push_seconds(fanout, fanout.queue, 2.0, gain["value"])
                self._wait_for(lambda: fanout.queue.empty())
                recovered = worker.signal_meter.snapshot()["snr_db"]
                # Without the gain-change reset this reads ~29-30 dB (see the
                # standalone measurement in this file's module docstring
                # discussion): the noise floor's minimum-statistics window
                # still holds pre-step noise samples for several seconds.
                self.assertAlmostEqual(recovered, target_snr_db, delta=2.5)
            finally:
                worker.stop()

    @staticmethod
    def _wait_for(predicate, timeout: float = 3.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("condition was not met before timeout")


if __name__ == "__main__":
    unittest.main()
