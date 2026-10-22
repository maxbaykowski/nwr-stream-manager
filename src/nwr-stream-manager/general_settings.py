"""The General settings: the instance name, which tells one NWR Stream Manager from another, and its location."""

from __future__ import annotations

import json
import logging
import re
import socket
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .state_files import atomic_write_json


LOG = logging.getLogger(__name__)

GENERAL_STATE_FILE_NAME = "general.json"
PRODUCT_NAME = "NWR Stream Manager"
INSTANCE_NAME_MAX_LENGTH = 64
LOCATION_MAX_LENGTH = 64


@dataclass(frozen=True)
class GeneralSettings:
    instance_name: str
    # Where this NWR Stream Manager is, in the owner's own words, such as "Attic SDR (Grand Rapids, MI)".
    location: str = ""


def general_state_path(state_path: Path) -> Path:
    return state_path.with_name(GENERAL_STATE_FILE_NAME)


def default_instance_name() -> str:
    """NWR Stream Manager on this computer's host name, cut to fit if the host name is long."""
    host = socket.gethostname().split(".")[0].strip()
    if not host:
        return PRODUCT_NAME
    return f"{PRODUCT_NAME} on {host}"[:INSTANCE_NAME_MAX_LENGTH].rstrip()


def _tidy_text(value: Any, label: str, max_length: int) -> str:
    # Runs of spaces, tabs and line breaks become one space.
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    if len(text) > max_length:
        raise ValueError(f"The {label} can be up to {max_length} characters long.")
    if any(unicodedata.category(character).startswith("C") for character in text):
        raise ValueError(f"The {label} can't contain control characters.")
    return text


def validate_instance_name(value: Any) -> str:
    name = _tidy_text(value, "instance name", INSTANCE_NAME_MAX_LENGTH)
    if not name:
        raise ValueError("Enter an instance name.")
    return name


def validate_location(value: Any) -> str:
    """A location, or "" for none."""
    return _tidy_text(value, "location", LOCATION_MAX_LENGTH)


def validate_general_settings_payload(raw: Any, current: GeneralSettings) -> GeneralSettings:
    """The settings from sent values; anything left out keeps its current value."""
    if not isinstance(raw, dict):
        raise ValueError("general settings are required")
    return GeneralSettings(
        instance_name=validate_instance_name(raw["instance_name"]) if "instance_name" in raw else current.instance_name,
        location=validate_location(raw["location"]) if "location" in raw else current.location,
    )


def load_or_initialize_general_settings(path: Path) -> GeneralSettings:
    """The saved settings. The first time, the default instance name is saved as the name."""
    raw: Any = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        LOG.warning("the general settings in %s can't be read, so the defaults are used instead: %s", path, exc)
    raw = raw if isinstance(raw, dict) else {}
    try:
        location = validate_location(raw.get("location", ""))
    except ValueError as exc:
        LOG.warning("the location in %s can't be used, so it's left empty: %s", path, exc)
        location = ""
    try:
        return GeneralSettings(validate_instance_name(raw.get("instance_name")), location)
    except ValueError as exc:
        if raw:
            LOG.warning("the instance name in %s can't be used, so the default is used instead: %s", path, exc)
    settings = GeneralSettings(default_instance_name(), location)
    try:
        save_general_settings(path, settings)
        LOG.info("instance name set to %r", settings.instance_name)
    except OSError as exc:
        LOG.warning("failed to save the instance name to %s: %s", path, exc)
    return settings


def save_general_settings(path: Path, settings: GeneralSettings) -> None:
    atomic_write_json(path, asdict(settings))
