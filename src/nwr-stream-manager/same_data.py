from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SameEvent:
    code: str
    name: str
    known: bool = True

    @property
    def display_name(self) -> str:
        return self.name if self.known else f"Unknown event ({self.code})"


@dataclass(frozen=True)
class SameLocation:
    same: str
    name: str
    location_type: str
    known: bool = True
    fips: str | None = None
    state: str | None = None
    ugc: str | None = None

    @property
    def display_name(self) -> str:
        return self.name if self.known else f"Unknown location ({self.same})"


def lookup_event(code: str) -> SameEvent:
    normalized = str(code).strip().upper()
    entry = events().get(normalized)
    if entry is None:
        return SameEvent(code=normalized, name=f"Unknown event ({normalized})", known=False)
    return SameEvent(code=normalized, name=str(entry["name"]), known=True)


def lookup_location(same_code: str) -> SameLocation:
    normalized = str(same_code).strip()
    entry = locations().get(normalized)
    if entry is None:
        return SameLocation(same=normalized, name=f"Unknown location ({normalized})", location_type="unknown", known=False)
    return SameLocation(
        same=normalized,
        name=str(entry["name"]),
        location_type=str(entry["location_type"]),
        known=True,
        fips=optional_str(entry.get("fips")),
        state=optional_str(entry.get("state")),
        ugc=optional_str(entry.get("ugc")),
    )


@lru_cache(maxsize=1)
def events() -> dict[str, dict[str, Any]]:
    return load_asset_json("events.json")


@lru_cache(maxsize=1)
def locations() -> dict[str, dict[str, Any]]:
    return load_asset_json("locations.json")


def load_asset_json(name: str) -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(files(__package__).joinpath("assets", name).read_text(encoding="utf-8"))
    except (AttributeError, TypeError):
        payload = json.loads((Path(__file__).resolve().parent / "assets" / name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return payload


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
