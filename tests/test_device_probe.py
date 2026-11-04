from __future__ import annotations

import importlib
import sys
import threading
import time
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_device_probe_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.device_probe")


class DeviceProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.device_probe = load_device_probe_module()

    def test_shared_probe_reuses_what_it_found_within_poll_interval(self) -> None:
        now = 10.0
        calls = {"rtl": 0, "alsa": 0}

        def clock() -> float:
            return now

        def rtl_probe():
            calls["rtl"] += 1
            return [f"rtl-{calls['rtl']}"]

        def alsa_probe():
            calls["alsa"] += 1
            return [f"alsa-{calls['alsa']}"]

        probe = self.device_probe.SharedDeviceProbe(
            {"rtl": rtl_probe, "alsa": alsa_probe},
            poll_interval_seconds=0.5,
            clock=clock,
        )

        first = probe.snapshot()
        second = probe.snapshot()

        self.assertEqual(calls, {"rtl": 1, "alsa": 1})
        self.assertEqual(first.devices("rtl"), ("rtl-1",))
        self.assertEqual(second.devices("alsa"), ("alsa-1",))

    def test_shared_probe_finds_devices_again_after_poll_interval(self) -> None:
        current_time = [10.0]
        calls = {"rtl": 0, "alsa": 0}

        def probe_for(name: str):
            def probe():
                calls[name] += 1
                return [calls[name]]
            return probe

        probe = self.device_probe.SharedDeviceProbe(
            {"rtl": probe_for("rtl"), "alsa": probe_for("alsa")},
            poll_interval_seconds=0.5,
            clock=lambda: current_time[0],
        )

        probe.snapshot()
        current_time[0] += 0.6
        # What was found last, straight away, while they're found again.
        self.assertEqual(probe.snapshot().devices("rtl"), (1,))
        snapshot = wait_for(lambda: probe.snapshot(), lambda found: found.devices("rtl") == (2,) and found.devices("alsa") == (2,))
        self.assertEqual(calls, {"rtl": 2, "alsa": 2})
        self.assertEqual(snapshot.devices("alsa"), (2,))
        # Forced, they're found again then and there.
        self.assertEqual(probe.devices("alsa", force=True), (3,))
        self.assertEqual(calls, {"rtl": 2, "alsa": 3})

    def test_shared_probe_records_errors_without_stopping_other_probes(self) -> None:
        probe = self.device_probe.SharedDeviceProbe(
            {
                "rtl": lambda: (_ for _ in ()).throw(RuntimeError("missing")),
                "alsa": lambda: ["card"],
            },
            labels={"rtl": "librtlsdr"},
        )

        with self.assertLogs("nwr_stream_manager.device_probe", "WARNING") as logs:
            snapshot = probe.snapshot()
            probe.snapshot(force=True)

        self.assertEqual(snapshot.devices("rtl"), ())
        self.assertIn("missing", snapshot.error("rtl"))
        self.assertEqual(snapshot.devices("alsa"), ("card",))
        # Said once, not every time.
        self.assertEqual(logs.output, ["WARNING:nwr_stream_manager.device_probe:librtlsdr probe failed: missing"])

    def test_a_probe_that_hangs_holds_up_nothing(self) -> None:
        # Such as librtlsdr reading an SDR that's stopped answering.
        release = threading.Event()
        current_time = [10.0]
        calls = {"rtl": 0}

        def rtl_probe():
            calls["rtl"] += 1
            if calls["rtl"] > 1:
                release.wait(10)
            return ["sdr"]

        probe = self.device_probe.SharedDeviceProbe(
            {"rtl": rtl_probe, "usb": lambda: ["usb-sdr"], "alsa": lambda: ["card"]},
            poll_interval_seconds=0.5,
            clock=lambda: current_time[0],
            wait_seconds=0.3,
        )
        self.assertEqual(probe.snapshot().devices("rtl"), ("sdr",))
        current_time[0] += 1
        try:
            started = time.monotonic()
            for _ in range(20):
                snapshot = probe.snapshot()
                self.assertEqual(snapshot.devices("rtl"), ("sdr",))
                self.assertEqual(snapshot.devices("usb"), ("usb-sdr",))
            self.assertLess(time.monotonic() - started, 0.2)
            # Others can still be found again then and there, while it hangs.
            self.assertEqual(probe.devices("alsa", force=True), ("card",))
            # Even forced, it isn't waited on for longer than wait_seconds.
            started = time.monotonic()
            self.assertEqual(probe.devices("rtl", force=True), ("sdr",))
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            release.set()


def wait_for(get, done, timeout: float = 2.0):
    deadline = time.monotonic() + timeout
    while True:
        value = get()
        if done(value) or time.monotonic() > deadline:
            return value
        time.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
