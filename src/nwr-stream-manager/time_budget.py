from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any


TEST_MODE_TIME_LIMIT_SECONDS = 600.0
TEST_MODE_TIME_WINDOW_SECONDS = 3600.0
TEST_MODE_TIME_FILE_NAME = "test_mode_time.json"
TEST_MODE_TIME_SAVE_INTERVAL_SECONDS = 2.0


class TestModeTimeUsedUp(ValueError):
    pass


class TestModeTimeBudget:
    """Test mode time shared by every stream: at most 10 minutes in any 60-minute span.

    Each stretch of use is remembered, and time comes back as that use becomes more than an
    hour old. Usage is saved to disk so restarts and updates don't hand out fresh time.
    """

    def __init__(
        self,
        path: Path | None,
        *,
        limit_seconds: float = TEST_MODE_TIME_LIMIT_SECONDS,
        window_seconds: float = TEST_MODE_TIME_WINDOW_SECONDS,
    ) -> None:
        self.path = path
        self.limit_seconds = float(limit_seconds)
        self.window_seconds = float(window_seconds)
        self.usage: list[tuple[float, float]] = []
        self.session_started_at: float | None = None
        self.saved_at = 0.0
        self._load()

    def used(self, now: float) -> float:
        window_start = now - self.window_seconds
        return sum(max(0.0, min(end, now) - max(start, window_start)) for start, end in self._intervals(now))

    def remaining(self, now: float) -> float:
        self.update(now)
        return max(0.0, self.limit_seconds - self.used(now))

    def update(self, now: float) -> None:
        window_start = now - self.window_seconds
        self.usage = [(start, end) for start, end in self.usage if end > window_start]
        if self.session_started_at is not None and now - self.saved_at >= TEST_MODE_TIME_SAVE_INTERVAL_SECONDS:
            self._save(now)

    def start(self, now: float) -> None:
        if self.session_started_at is not None:
            return
        if self.remaining(now) <= 0.0:
            raise TestModeTimeUsedUp(
                f"All {self.limit_seconds / 60:g} minutes of test mode for the past hour have been used"
            )
        self.session_started_at = now
        self._save(now)

    def stop(self, now: float) -> None:
        if self.session_started_at is None:
            return
        self.usage.append((self.session_started_at, max(self.session_started_at, now)))
        self.session_started_at = None
        self._save(now)

    def available_at(self, now: float) -> float | None:
        """When some time frees up again, if none is left right now."""
        if self.remaining(now) > 0.0:
            return None
        window_start = now - self.window_seconds
        starts = [start for start, end in self._intervals(now) if end > window_start]
        return max(now, min(starts) + self.window_seconds) if starts else now

    def snapshot(self, now: float) -> dict[str, Any]:
        remaining = self.remaining(now)
        usage: list[list[float | None]] = [[start, end] for start, end in self.usage]
        if self.session_started_at is not None:
            usage.append([self.session_started_at, None])
        return {
            "limit_seconds": self.limit_seconds,
            "window_seconds": self.window_seconds,
            "remaining_seconds": remaining,
            "available_at": self.available_at(now),
            "running": self.session_started_at is not None,
            "usage": usage,
            "now": now,
        }

    def _intervals(self, now: float) -> list[tuple[float, float]]:
        intervals = list(self.usage)
        if self.session_started_at is not None:
            intervals.append((self.session_started_at, max(self.session_started_at, now)))
        return intervals

    def _load(self) -> None:
        if self.path is None:
            return
        now = time.time()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            # An unreadable record must not hand out fresh time; count the full allowance as
            # just used.
            self.usage = [(now - self.limit_seconds, now)]
            return
        entries = raw.get("usage", []) if isinstance(raw, dict) else []
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            start, end = entry
            if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (start, end)):
                continue
            # A clock that jumped backwards can't be used to push old use out of the hour.
            start, end = min(float(start), now), min(float(end), now)
            if end > start:
                self.usage.append((start, end))

    def _save(self, now: float) -> None:
        self.saved_at = now
        if self.path is None:
            return
        # Save the running session as used up to now, so a crash can't give time back.
        data = {"usage": [[start, end] for start, end in self._intervals(now)]}
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(data) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
