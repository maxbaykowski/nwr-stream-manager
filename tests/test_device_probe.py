from __future__ import annotations

import importlib
import sys
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

    def test_shared_probe_reuses_snapshot_within_poll_interval(self) -> None:
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

        self.assertIs(first, second)
        self.assertEqual(calls, {"rtl": 1, "alsa": 1})
        self.assertEqual(first.devices("rtl"), ("rtl-1",))
        self.assertEqual(first.devices("alsa"), ("alsa-1",))

    def test_shared_probe_refreshes_all_devices_after_poll_interval(self) -> None:
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
        snapshot = probe.snapshot()

        self.assertEqual(calls, {"rtl": 2, "alsa": 2})
        self.assertEqual(snapshot.devices("rtl"), (2,))
        self.assertEqual(snapshot.devices("alsa"), (2,))

    def test_shared_probe_records_errors_without_stopping_other_probes(self) -> None:
        probe = self.device_probe.SharedDeviceProbe(
            {
                "rtl": lambda: (_ for _ in ()).throw(RuntimeError("missing")),
                "alsa": lambda: ["card"],
            }
        )

        snapshot = probe.snapshot()

        self.assertEqual(snapshot.devices("rtl"), ())
        self.assertIn("missing", snapshot.error("rtl"))
        self.assertEqual(snapshot.devices("alsa"), ("card",))


if __name__ == "__main__":
    unittest.main()
