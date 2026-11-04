from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
LOG = logging.getLogger(__name__)
# How long a request waits for devices to be found when none have been yet, or when it asks
# for them to be found again. Anything slower is told what was found last.
PROBE_WAIT_SECONDS = 5.0


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


class _Probe:
    """One kind of device, found on a thread of its own."""

    def __init__(self, name: str, probe: Callable[[], list | tuple], label: str) -> None:
        self.name = name
        self.probe = probe
        self.label = label
        self.result: ProbeResult | None = None
        self.probed_at = 0.0
        # Each time it's been asked to be found again, and each time it has been.
        self.requested = 0
        self.finished = 0
        self.thread: threading.Thread | None = None


class SharedDeviceProbe:
    """The devices each probe finds, found again when they're asked for and it's been longer than
    poll_interval_seconds. Each probe runs on a thread of its own, so a slow one, such as librtlsdr
    reading an SDR that's stopped answering, holds up nothing that asks, including the other probes:
    anything asking then is told what was found last."""

    def __init__(
        self,
        probes: dict[str, Callable[[], list | tuple]],
        *,
        poll_interval_seconds: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        labels: dict[str, str] | None = None,
        wait_seconds: float = PROBE_WAIT_SECONDS,
    ) -> None:
        labels = labels or {}
        self.probes = {name: _Probe(name, probe, labels.get(name, name)) for name, probe in probes.items()}
        self.poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self.clock = clock
        self.wait_seconds = max(0.0, float(wait_seconds))
        self.condition = threading.Condition()

    def snapshot(self, *, force: bool = False, names: tuple[str, ...] | None = None) -> DeviceProbeSnapshot:
        """What was found last of each probe, or of names, once each is found if it never has
        been, or found again if forced, as long as that takes no longer than wait_seconds."""
        wanted: dict[str, int] = {}
        with self.condition:
            now = self.clock()
            for probe in self._chosen(names):
                fresh = probe.result is not None and now - probe.probed_at < self.poll_interval_seconds
                if fresh and not force:
                    continue
                # Forced, it has to be found after this was asked, not by a probe already under way.
                if probe.requested == probe.finished or force:
                    probe.requested += 1
                    self.condition.notify_all()
                if probe.thread is None:
                    probe.thread = threading.Thread(target=self._run, args=(probe,), name=f"device-probe-{probe.name}", daemon=True)
                    probe.thread.start()
                if probe.result is None or force:
                    wanted[probe.name] = probe.requested
            if wanted:
                self.condition.wait_for(
                    lambda: all(self.probes[name].finished >= number for name, number in wanted.items()),
                    timeout=self.wait_seconds,
                )
            return self._snapshot_locked()

    def devices(self, name: str, *, force: bool = False) -> tuple:
        return self.snapshot(force=force, names=(name,)).devices(name)

    def _chosen(self, names: tuple[str, ...] | None) -> list[_Probe]:
        return [probe for name, probe in self.probes.items() if names is None or name in names]

    def _snapshot_locked(self) -> DeviceProbeSnapshot:
        results = {probe.name: probe.result for probe in self.probes.values() if probe.result is not None}
        found = [probe.probed_at for probe in self.probes.values() if probe.result is not None]
        return DeviceProbeSnapshot(results=results, probed_at=min(found) if found else 0.0)

    def _run(self, probe: _Probe) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: probe.requested > probe.finished)
                wanted = probe.requested
            probed_at = self.clock()
            try:
                result = ProbeResult(tuple(probe.probe()), "")
            except Exception as exc:
                result = ProbeResult((), str(exc))
            with self.condition:
                before = probe.result.error if probe.result is not None else ""
                probe.result = result
                probe.probed_at = probed_at
                probe.finished = wanted
                self.condition.notify_all()
            # Once when it starts failing, or fails differently, and once when it works again,
            # rather than every time it's asked.
            if result.error and result.error != before:
                LOG.warning("%s probe failed: %s", probe.label, result.error)
            elif before and not result.error:
                LOG.info("%s probe works again", probe.label)
