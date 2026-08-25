from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class ProbeResult(Generic[T]):
    devices: tuple[T, ...] = ()
    error: str = ""


@dataclass(frozen=True)
class DeviceProbeSnapshot:
    results: dict[str, ProbeResult] = field(default_factory=dict)
    probed_at: float = 0.0

    def devices(self, name: str) -> tuple:
        result = self.results.get(name)
        return result.devices if result is not None else ()

    def error(self, name: str) -> str:
        result = self.results.get(name)
        return result.error if result is not None else ""


class SharedDeviceProbe:
    def __init__(
        self,
        probes: dict[str, Callable[[], list | tuple]],
        *,
        poll_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.probes = dict(probes)
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.clock = clock
        self.lock = threading.Lock()
        self.snapshot_value = DeviceProbeSnapshot()

    def snapshot(self, *, force: bool = False) -> DeviceProbeSnapshot:
        now = self.clock()
        with self.lock:
            if (
                not force
                and self.snapshot_value.probed_at
                and now - self.snapshot_value.probed_at < self.poll_interval_seconds
            ):
                return self.snapshot_value
            results: dict[str, ProbeResult] = {}
            for name, probe in self.probes.items():
                try:
                    results[name] = ProbeResult(tuple(probe()), "")
                except Exception as exc:
                    results[name] = ProbeResult((), str(exc))
            self.snapshot_value = DeviceProbeSnapshot(results=results, probed_at=now)
            return self.snapshot_value

    def devices(self, name: str, *, force: bool = False) -> tuple:
        return self.snapshot(force=force).devices(name)
