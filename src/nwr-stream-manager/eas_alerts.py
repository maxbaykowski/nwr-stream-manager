"""Recorded EAS alerts: their settings, files and indexes, and finding, describing and exporting them."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import EasRecordingConfig
from .models import WebEasRecordingSettings
from .same_data import lookup_event, lookup_location
from .state_files import sanitize_path_component, whole_seconds


LOG = logging.getLogger(__name__)


EAS_ALERT_AUDIO_TYPES = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}


def eas_recording_settings_from_stream(stream: dict[str, Any]) -> WebEasRecordingSettings:
    raw = stream.get("eas_recording")
    if not isinstance(raw, dict):
        return WebEasRecordingSettings()
    try:
        return validate_eas_recording_payload(raw)
    except Exception as exc:
        LOG.warning("EAS recording settings for stream %s are invalid: %s", stream.get("id"), exc)
        return WebEasRecordingSettings()


def validate_eas_recording_payload(raw: Any) -> WebEasRecordingSettings:
    if not isinstance(raw, dict):
        raise ValueError("EAS recording settings are required")
    enabled = bool(raw.get("enabled", False))
    pre_seconds = whole_seconds(raw.get("pre_seconds", 2), "Pre-recording time")
    post_seconds = whole_seconds(raw.get("post_seconds", 5), "Post-recording time")
    max_seconds = whole_seconds(raw.get("max_seconds", 120), "Maximum recording time")
    if isinstance(max_seconds, bool):
        raise ValueError("Maximum recording time must be a whole number of seconds")
    if not 0 <= pre_seconds <= 10:
        raise ValueError("Pre-recording time must be from 0 through 10 seconds")
    if not 0 <= post_seconds <= 10:
        raise ValueError("Post-recording time must be from 0 through 10 seconds")
    if max_seconds <= 0:
        raise ValueError("Maximum recording time must be greater than 0 seconds")
    format_value = str(raw.get("format", "wav")).strip().lower()
    if format_value not in {"mp3", "wav"}:
        raise ValueError("EAS recording format must be MP3 or WAV")
    return WebEasRecordingSettings(
        enabled=enabled,
        pre_seconds=pre_seconds,
        post_seconds=post_seconds,
        max_seconds=max_seconds,
        format=format_value,
    )


def eas_config_from_stream(stream: dict[str, Any], state_directory: Path, settings: WebEasRecordingSettings) -> EasRecordingConfig:
    directory = stream_alerts_directory(state_directory, stream)
    if settings.enabled:
        directory.mkdir(parents=True, exist_ok=True)
    return EasRecordingConfig(
        enabled=settings.enabled,
        pre_seconds=settings.pre_seconds,
        post_seconds=settings.post_seconds,
        max_seconds=settings.max_seconds,
        directory=str(directory),
        format=settings.format,
        local_time=False,
    )


def stream_alerts_directory(state_directory: Path, stream: dict[str, Any]) -> Path:
    station = stream.get("station", {})
    callsign = sanitize_path_component(str(station.get("callsign", "")).strip() or str(stream.get("id", "stream")))
    return state_directory / "streams" / callsign / "alerts"


EXPORT_ALERT_PRESETS = (
    {"id": "last_24", "label": "Last 24 hours"},
    {"id": "last_7", "label": "Last 7 days"},
    {"id": "last_30", "label": "Last 30 days"},
    {"id": "all", "label": "Export all alerts"},
)
DELETE_ALERT_PRESETS = (
    {"id": "older_24", "label": "Older than 24 hours"},
    {"id": "older_7", "label": "Older than 7 days"},
    {"id": "older_30", "label": "Older than 30 days"},
    {"id": "all", "label": "Delete all alerts"},
)


def eas_alert_index_path(streams_directory: Path, stream: dict[str, Any]) -> Path:
    return stream_alerts_directory(streams_directory.parent, stream) / "index.json"


def load_eas_alert_index(index_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "alerts": []}
    if isinstance(data, list):
        data = {"version": 1, "alerts": data}
    if not isinstance(data, dict) or not isinstance(data.get("alerts"), list):
        raise ValueError("EAS alert index must contain an alerts array")
    if not all(isinstance(alert, dict) for alert in data["alerts"]):
        raise ValueError("Every EAS alert index entry must be an object")
    data["version"] = int(data.get("version", 1))
    return data


def load_eas_alert_entries(index_path: Path) -> list[dict[str, Any]]:
    return list(load_eas_alert_index(index_path)["alerts"])


def indexed_eas_alert_entries(index_path: Path) -> list[tuple[int, dict[str, Any]]]:
    return list(enumerate(load_eas_alert_entries(index_path)))


def eas_alert_id(alert: dict[str, Any], index: int) -> str:
    digest = hashlib.sha256(
        "\0".join(
            [
                str(index),
                str(alert.get("raw_same_header", "")),
                str(alert.get("start_time_utc", "")),
                str(alert.get("file_path", "")),
            ]
        ).encode("utf-8", errors="replace")
    ).hexdigest()
    return digest[:24]


def eas_alert_notification_id(alert: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        "\0".join(
            [
                str(alert.get("raw_same_header", "")),
                str(alert.get("start_time_utc", "")),
                str(alert.get("expires_at_utc", "")),
                str(alert.get("event_type", "")),
                str(alert.get("file_path", "")),
            ]
        ).encode("utf-8", errors="replace")
    ).hexdigest()
    return digest[:24]


def eas_alert_notification_message(stream: dict[str, Any], alert: dict[str, Any]) -> str:
    event = lookup_event(str(alert.get("event_type", "")))
    callsign = stream_callsign(stream)
    areas = [
        format_same_location_for_alert(str(code))
        for code in alert.get("fips_codes", [])
        if isinstance(code, str) and code.strip()
    ]
    area_text = ", ".join(areas) if areas else "an unknown area"
    expires_at = parse_utc_datetime(str(alert.get("expires_at_utc", "")))
    return f"{event.display_name} received on {callsign} for {area_text} until {format_local_time(expires_at)}."


def eas_alert_summary(stream: dict[str, Any], alert: dict[str, Any], index: int) -> dict[str, Any]:
    event = lookup_event(str(alert.get("event_type", "")))
    issued_at = parse_utc_datetime(str(alert.get("start_time_utc", "")))
    return {
        "id": eas_alert_id(alert, index),
        "summary": f"{sentence_case_event(event.display_name)} issued {format_local_datetime(issued_at, separator='at')}",
        "event_name": event.display_name,
        "issued_at": format_local_datetime(issued_at),
    }


def eas_alert_detail(stream: dict[str, Any], alert: dict[str, Any], index: int) -> dict[str, Any]:
    event = lookup_event(str(alert.get("event_type", "")))
    issued_at = parse_utc_datetime(str(alert.get("start_time_utc", "")))
    expires_at = parse_utc_datetime(str(alert.get("expires_at_utc", "")))
    areas = [
        format_same_location_for_alert(str(code))
        for code in alert.get("fips_codes", [])
        if isinstance(code, str)
    ]
    return {
        "id": eas_alert_id(alert, index),
        "event_type": event.display_name,
        "areas": areas,
        "issued_at": format_local_datetime(issued_at),
        # The hour without a leading zero, as everywhere else: 6:00 AM, not 06:00 AM.
        "issued_at_media": f"{issued_at.astimezone().strftime('%m/%d/%Y')}; {format_local_time(issued_at)}",
        "issued_at_epoch": issued_at.timestamp(),
        "expires_at": format_local_datetime(expires_at),
        "audio_url": f"/api/eas-alert-audio?stream_id={stream.get('id', '')}&alert_id={eas_alert_id(alert, index)}",
        "download_url": f"/api/eas-alert-audio?stream_id={stream.get('id', '')}&alert_id={eas_alert_id(alert, index)}&download=1",
        "audio_type": eas_alert_audio_type(alert),
    }


def format_same_location_for_alert(same_code: str) -> str:
    raw_code = str(same_code).strip()
    location = lookup_location(raw_code)
    subdivision_digit = ""
    if not location.known and re.fullmatch(r"\d{6}", raw_code) and raw_code[0] != "0":
        # The first digit can name part of an area, such as 126139 for part of Ottawa County.
        # An area that still isn't known keeps the code as it was sent.
        whole_area = lookup_location(f"0{raw_code[1:]}")
        if whole_area.known:
            location, subdivision_digit = whole_area, raw_code[0]
    if not location.known:
        return location.display_name
    if location.location_type == "marine":
        text = location.name
    elif location.location_type == "national":
        text = "All of the United States"
    elif location.location_type == "state":
        text = f"All of {location.name}"
    elif location.kind == "independent city":
        # Census names end in a lowercase "city", as in "Alexandria city", which reads like a typo.
        text = f"{location.name.removesuffix(' city')} (independent city), {location.state}"
    elif location.state and location.state not in location.name:
        text = f"{location.name}, {location.state}"
    else:
        text = location.display_name
    if subdivision_digit:
        return f"{text}, subdivision {subdivision_digit}"
    return text


def local_datetime_parts(value: datetime) -> dict[str, int]:
    return {
        "year": value.year,
        "month": value.month,
        "day": value.day,
        "hour": value.hour,
        "minute": value.minute,
    }


def preset_range_bounds(mode: str, now: datetime | None = None) -> tuple[datetime | None, datetime | None]:
    now = now or datetime.now(timezone.utc)
    if mode == "last_24":
        return now - timedelta(hours=24), now
    if mode == "last_7":
        return now - timedelta(days=7), now
    if mode == "last_30":
        return now - timedelta(days=30), now
    if mode == "older_24":
        return None, now - timedelta(hours=24)
    if mode == "older_7":
        return None, now - timedelta(days=7)
    if mode == "older_30":
        return None, now - timedelta(days=30)
    if mode == "all":
        return None, now
    raise ValueError("Unsupported EAS alert range.")


def alert_range_bounds_from_request(mode: str, start: str = "", end: str = "") -> tuple[datetime | None, datetime | None]:
    mode = str(mode).strip()
    if mode == "manual":
        start_time = parse_request_datetime(start, "Start")
        end_time = parse_request_datetime(end, "End")
        now = datetime.now(timezone.utc)
        if start_time > now or end_time > now:
            raise ValueError("Date ranges cannot be in the future.")
        if start_time > end_time:
            raise ValueError("Start date must be before the end date.")
        return start_time, end_time
    return preset_range_bounds(mode)


def parse_request_datetime(value: str, label: str) -> datetime:
    if not value:
        raise ValueError(f"{label} date and time are required.")
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{label} date and time are invalid.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.astimezone(timezone.utc)


def filter_indexed_alerts(
    indexed_alerts: list[tuple[int, dict[str, Any]]],
    bounds: tuple[datetime | None, datetime | None],
) -> list[tuple[int, dict[str, Any]]]:
    start, end = bounds
    selected = []
    for index, alert in indexed_alerts:
        issued = parse_utc_datetime(str(alert.get("start_time_utc", "")))
        if start is not None and issued < start:
            continue
        if end is not None and issued > end:
            continue
        selected.append((index, alert))
    selected.sort(key=lambda item: parse_utc_datetime(str(item[1].get("start_time_utc", ""))), reverse=True)
    return selected


def unique_archive_name(name: str, used_names: set[str]) -> str:
    cleaned = sanitize_archive_filename(name)
    stem = Path(cleaned).stem or "alert"
    suffix = Path(cleaned).suffix
    candidate = cleaned
    counter = 2
    while candidate in used_names:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    used_names.add(candidate)
    return candidate


def sanitize_archive_filename(value: str) -> str:
    name = Path(str(value)).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" ._")
    return name or "alert"


def safe_eas_alert_file_path(
    streams_directory: Path,
    stream: dict[str, Any],
    alert: dict[str, Any],
    *,
    require_exists: bool = True,
) -> Path:
    alerts_directory = stream_alerts_directory(streams_directory.parent, stream).resolve()
    raw_path = alert.get("file_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("EAS alert audio file is missing")
    path = Path(raw_path).expanduser().resolve()
    if require_exists and not path.is_file():
        raise ValueError("EAS alert audio file was not found")
    if path != alerts_directory and alerts_directory not in path.parents:
        raise ValueError("EAS alert audio path is outside the alert directory")
    return path


def alert_download_name(stream: dict[str, Any], alert: dict[str, Any]) -> str:
    station = stream.get("station", {})
    callsign = sanitize_path_component(str(station.get("callsign", "alert")))
    event = sanitize_path_component(str(alert.get("event_type", "EAS")))
    issued = sanitize_path_component(str(alert.get("start_time_utc", "")).replace(":", ""))
    suffix = Path(str(alert.get("file_path", ""))).suffix.lower()
    if suffix not in {".wav", ".mp3", ".ogg"}:
        suffix = ".wav"
    return f"{callsign}-{event}-{issued or 'alert'}{suffix}"


def parse_utc_datetime(value: str) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_local_datetime(value: datetime, *, separator: str = ",") -> str:
    local = value.astimezone()
    time_text = local.strftime("%I:%M %p %Z")
    if time_text.startswith("0"):
        time_text = time_text[1:]
    date_text = f"{local.strftime('%B')} {local.day}, {local.year}"
    if separator == ",":
        return f"{date_text}, {time_text}"
    return f"{date_text} {separator} {time_text}"


def format_local_time(value: datetime) -> str:
    local = value.astimezone()
    time_text = local.strftime("%I:%M %p %Z")
    return time_text[1:] if time_text.startswith("0") else time_text


def sentence_case_event(name: str) -> str:
    if not name:
        return "Unknown event"
    return name[:1].upper() + name[1:].lower()


def stream_callsign(stream: dict[str, Any], snapshot: dict[str, Any] | None = None) -> str:
    station = stream.get("station") if isinstance(stream.get("station"), dict) else {}
    if (not station or not station.get("callsign")) and snapshot is not None:
        station = snapshot.get("station") if isinstance(snapshot.get("station"), dict) else {}
    return str(station.get("callsign", "") or "Unknown")


def eas_alert_audio_type(alert: dict[str, Any]) -> str:
    return EAS_ALERT_AUDIO_TYPES.get(Path(str(alert.get("file_path", ""))).suffix.lower(), "audio/wav")
