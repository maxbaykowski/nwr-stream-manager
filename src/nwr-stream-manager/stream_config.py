"""Streams' settings: loading and saving them, what a stream sends to, and checking new settings."""

from __future__ import annotations

import json
import logging
import math
import re
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .alsa import (
    ALSA_CHANNEL_BOTH,
    ALSA_CHANNEL_LEFT,
    ALSA_CHANNEL_MODES,
    ALSA_CHANNEL_RIGHT,
    ALSA_STREAM_DEFAULT_SAMPLE_RATE,
    AlsaStreamTapConfig,
)
from .config import (
    AudioConfig,
    IcecastConfig,
    parse_audio_config,
    parse_notch_config,
    VOLUME_MULTIPLIER_MAX,
    VOLUME_MULTIPLIER_MIN,
)
from .constants import (
    DEFAULT_STREAM_BITRATES,
    DEFAULT_STREAM_SAMPLE_RATE,
    STATIONS_ASSET_PATH,
    STREAM_CONFIG_FILE_NAME,
    STREAM_SAMPLE_RATES,
    STREAM_SERVICE_CUSTOM,
    STREAM_SERVICE_GWES,
    STREAM_SERVICE_HOSTS,
    STREAM_SERVICE_NAMES,
    STREAM_SERVICE_NWRORG,
    STREAM_SERVICE_WEATHERUSA,
    STREAMS_DIRECTORY_MARKER_FILE_NAME,
)
from .eas_alerts import eas_recording_settings_from_stream
from .stream_archive import stream_archive_settings_from_stream
from .icecast import parse_icecast_address
from .icecastauth import IcecastSettings
from .state_files import sanitize_path_component


LOG = logging.getLogger(__name__)


def audio_config_from_stream(stream: dict[str, Any]) -> AudioConfig:
    raw = stream.get("audio")
    if not isinstance(raw, dict):
        return AudioConfig()
    try:
        return parse_audio_config(raw)
    except Exception as exc:
        LOG.warning("audio effects settings for stream %s are invalid: %s", stream.get("id"), exc)
        return AudioConfig()


def validate_audio_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("audio effects settings are required")
    try:
        return asdict(parse_audio_config(raw))
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def station_key(station: dict[str, Any]) -> str:
    return "|".join(
        str(station.get(key, "")).strip()
        for key in ("callsign", "frequency", "state", "city", "site_name")
    )


def load_station_database(path: Path = STATIONS_ASSET_PATH) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        stations = raw["stations"]
    except Exception as exc:
        LOG.warning("failed to load NWR station database from %s: %s", path, exc)
        return []
    if not isinstance(stations, list):
        LOG.warning("NWR station database in %s does not contain a stations array", path)
        return []

    loaded: list[dict[str, str]] = []
    for station in stations:
        if not isinstance(station, dict):
            continue
        normalized = {
            "callsign": str(station.get("callsign", "")).strip().upper(),
            "frequency": str(station.get("frequency", "")).strip(),
            "city": str(station.get("city", "")).strip(),
            "state": str(station.get("state", "")).strip().upper(),
            "state_name": str(station.get("state_name", "")).strip(),
            "site_name": str(station.get("site_name", "")).strip(),
            "source_url": str(station.get("source_url", "")).strip(),
        }
        if not normalized["callsign"] or not normalized["frequency"]:
            continue
        normalized["key"] = station_key(normalized)
        loaded.append(normalized)
    loaded.sort(key=lambda item: (item["callsign"], item["state"], item["city"]))
    LOG.info("loaded %s NWR stations from %s", len(loaded), path)
    return loaded


def load_streams(streams_directory: Path, legacy_path: Path) -> list[dict[str, Any]]:
    streams = load_stream_configs(streams_directory)
    if streams:
        removed = False
        for stream in streams:
            migrate_stream_notch_width(stream)
            migrate_stream_volume_multiplier(stream)
            removed = remove_stream_deemphasis_setting(stream) or removed
        if removed:
            LOG.info("removed the old NWR de-emphasis setting from saved streams; every stream now always has it")
            save_streams(streams_directory, streams)
        return streams
    if (streams_directory / STREAMS_DIRECTORY_MARKER_FILE_NAME).exists():
        return []
    streams = load_legacy_streams(legacy_path)
    for stream in streams:
        migrate_stream_notch_width(stream)
        migrate_stream_volume_multiplier(stream)
        remove_stream_deemphasis_setting(stream)
    if streams:
        LOG.info("migrating %s stream configuration(s) from %s to %s", len(streams), legacy_path, streams_directory)
        save_streams(streams_directory, streams)
    return streams


def migrate_stream_notch_width(stream: dict[str, Any]) -> None:
    # The notch used to be set by sharpness; convert it to the equivalent width.
    audio = stream.get("audio")
    notch = audio.get("notch") if isinstance(audio, dict) else None
    if not isinstance(notch, dict) or "width" in notch:
        return
    try:
        audio["notch"] = asdict(parse_notch_config(notch))
    except Exception as exc:
        LOG.warning("could not convert notch settings for stream %s: %s", stream.get("id"), exc)


def migrate_stream_volume_multiplier(stream: dict[str, Any]) -> None:
    # The multiplier used to have no limits; bring older settings into the allowed range.
    audio = stream.get("audio")
    volume = audio.get("volume") if isinstance(audio, dict) else None
    multiplier = volume.get("multiplier") if isinstance(volume, dict) else None
    if not isinstance(multiplier, (int, float)) or isinstance(multiplier, bool) or not math.isfinite(multiplier):
        return
    limited = min(max(float(multiplier), VOLUME_MULTIPLIER_MIN), VOLUME_MULTIPLIER_MAX)
    if limited != multiplier:
        LOG.info("volume multiplier %s for stream %s is out of range; using %s", multiplier, stream.get("id"), limited)
        volume["multiplier"] = limited


def remove_stream_deemphasis_setting(stream: dict[str, Any]) -> bool:
    """NWR de-emphasis used to be a setting that could be turned off; every stream now
    always has it. Return whether there was a setting to remove."""
    audio = stream.get("audio")
    if not isinstance(audio, dict):
        return False
    removed = [key for key in ("deemphasis", "deemphasis_tau") if key in audio]
    for key in removed:
        del audio[key]
    return bool(removed)


def load_stream_configs(streams_directory: Path) -> list[dict[str, Any]]:
    if not streams_directory.exists():
        return []
    streams = []
    for config_path in sorted(streams_directory.glob(f"*/{STREAM_CONFIG_FILE_NAME}")):
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOG.warning("failed to load stream configuration from %s: %s", config_path, exc)
            continue
        if not isinstance(raw, dict):
            LOG.warning("stream configuration in %s is not a JSON object", config_path)
            continue
        streams.append(raw)
    streams.sort(key=stream_sort_key)
    return streams


def load_legacy_streams(path: Path) -> list[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception as exc:
        LOG.warning("failed to load stream settings from %s: %s", path, exc)
        return []
    if not isinstance(raw, dict) or not isinstance(raw.get("streams"), list):
        return []
    streams = []
    for stream in raw["streams"]:
        if isinstance(stream, dict):
            streams.append(stream)
    streams.sort(key=stream_sort_key)
    return streams


def save_streams(streams_directory: Path, streams: list[dict[str, Any]]) -> None:
    streams_directory.mkdir(parents=True, exist_ok=True)
    (streams_directory / STREAMS_DIRECTORY_MARKER_FILE_NAME).write_text("per-stream JSON configs\n", encoding="utf-8")
    desired_paths: set[Path] = set()
    used_names: set[str] = set()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        config_path = stream_config_path(streams_directory, stream, used_names)
        desired_paths.add(config_path)
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(stream, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for config_path in streams_directory.glob(f"*/{STREAM_CONFIG_FILE_NAME}"):
        if config_path not in desired_paths:
            try:
                config_path.unlink()
            except FileNotFoundError:
                pass


def remove_stream_configs(streams_directory: Path, streams: list[dict[str, Any]]) -> None:
    streams_directory.mkdir(parents=True, exist_ok=True)
    (streams_directory / STREAMS_DIRECTORY_MARKER_FILE_NAME).write_text("per-stream JSON configs\n", encoding="utf-8")
    for stream in streams:
        for config_path in stream_config_candidates(streams_directory, stream):
            try:
                config_path.unlink()
            except FileNotFoundError:
                continue
            try:
                config_path.parent.rmdir()
            except OSError:
                pass


def stream_config_path(streams_directory: Path, stream: dict[str, Any], used_names: set[str] | None = None) -> Path:
    used_names = used_names if used_names is not None else set()
    preferred = stream_directory_name(stream)
    name = preferred
    if name in used_names:
        stream_id = str(stream.get("id", "")).strip()
        suffix = sanitize_path_component(stream_id[:8]) if stream_id else uuid.uuid4().hex[:8]
        name = f"{preferred}-{suffix}"
        counter = 2
        while name in used_names:
            name = f"{preferred}-{suffix}-{counter}"
            counter += 1
    used_names.add(name)
    return streams_directory / name / STREAM_CONFIG_FILE_NAME


def stream_config_candidates(streams_directory: Path, stream: dict[str, Any]) -> list[Path]:
    stream_id = str(stream.get("id", "")).strip()
    candidates: list[Path] = []
    if stream_id:
        for config_path in streams_directory.glob(f"*/{STREAM_CONFIG_FILE_NAME}"):
            try:
                raw = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(raw, dict) and raw.get("id") == stream_id and config_path not in candidates:
                candidates.append(config_path)
        return candidates
    candidates.append(streams_directory / stream_directory_name(stream) / STREAM_CONFIG_FILE_NAME)
    return candidates


def stream_directory_name(stream: dict[str, Any]) -> str:
    station = stream.get("station", {})
    callsign = str(station.get("callsign", "")).strip() if isinstance(station, dict) else ""
    return sanitize_path_component(callsign or str(stream.get("id", "stream")))


def stream_sort_key(stream: dict[str, Any]) -> tuple[str, str, str]:
    station = stream.get("station", {})
    if not isinstance(station, dict):
        station = {}
    return (
        str(station.get("callsign", "")),
        str(station.get("frequency", "")),
        str(stream.get("id", "")),
    )


def stream_outputs(stream: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = stream.get("outputs")
    if isinstance(outputs, list):
        return [output for output in outputs if isinstance(output, dict)]
    icecast = stream.get("icecast")
    if isinstance(icecast, dict):
        output = {
            "id": stream.get("id", uuid.uuid4().hex),
            "enabled": stream.get("enabled", True),
            "type": "icecast",
            "icecast": icecast,
        }
        stream["outputs"] = [output]
        return [output]
    return []


def mutable_stream_outputs(stream: dict[str, Any]) -> list[dict[str, Any]]:
    outputs = stream.get("outputs")
    if isinstance(outputs, list):
        return outputs
    stream_outputs(stream)
    outputs = stream.get("outputs")
    if isinstance(outputs, list):
        return outputs
    stream["outputs"] = []
    return stream["outputs"]


def public_stream_output(output: dict[str, Any]) -> dict[str, Any]:
    output_type = str(output.get("type", "icecast") or "icecast")
    public: dict[str, Any] = {
        "id": str(output.get("id", "")),
        "enabled": output.get("enabled", True) is not False,
        "type": output_type,
        "status": output.get("status", ""),
        "error": output.get("error", ""),
    }
    if output_type == "soundcard":
        soundcard = output.get("soundcard") if isinstance(output.get("soundcard"), dict) else {}
        public["soundcard"] = {
            "device": soundcard.get("device") or soundcard.get("display_name") or "",
            "channel_mode": soundcard.get("channel_mode", ""),
        }
    else:
        icecast = output.get("icecast") if isinstance(output.get("icecast"), dict) else {}
        public["icecast"] = {
            "host": icecast.get("host", ""),
            "port": icecast.get("port", ""),
            "tls": bool(icecast.get("tls")),
            "mount": icecast.get("mount", ""),
            "format": icecast.get("format", ""),
            "sample_rate": icecast.get("sample_rate", ""),
            "bitrate": icecast.get("bitrate", ""),
        }
    return public


def public_stream(stream: dict[str, Any]) -> dict[str, Any]:
    station = stream.get("station") if isinstance(stream.get("station"), dict) else {}
    eas_recording = stream.get("eas_recording") if isinstance(stream.get("eas_recording"), dict) else {}
    return {
        "id": str(stream.get("id", "")),
        "enabled": stream.get("enabled", True) is not False,
        "station": dict(station),
        "status": stream.get("status", ""),
        "outputs": [public_stream_output(output) for output in stream_outputs(stream)],
        "eas_recording": {"enabled": bool(eas_recording.get("enabled", False))},
        "archive": {"enabled": stream_archive_settings_from_stream(stream).enabled},
        "created_at": stream.get("created_at", 0),
        "updated_at": stream.get("updated_at", 0),
    }


def public_active_stream_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    public = {
        "id": str(snapshot.get("id", "")),
        "outputs": [public_stream_output(output) for output in snapshot.get("outputs", []) if isinstance(output, dict)],
        "status": snapshot.get("status", ""),
        "error": snapshot.get("error", ""),
        "started_at": snapshot.get("started_at"),
        "last_audio_at": snapshot.get("last_audio_at"),
        "raw_queue": snapshot.get("raw_queue", {}),
        "type": snapshot.get("type", ""),
        "output_id": snapshot.get("output_id", ""),
    }
    station = snapshot.get("station", {})
    if isinstance(station, dict) and (station.get("callsign") or station.get("frequency")):
        public["station"] = dict(station)
    return public


def saved_icecast_address(icecast: dict[str, Any]):
    """The clean address of saved Icecast settings, or None if they cannot be read.

    Settings saved before addresses were cleaned on save can still hold a full web
    address (http://host) in the host field; this reads both kinds.
    """
    try:
        address = parse_icecast_address(icecast.get("host", ""), icecast.get("port", 8000), icecast.get("mount", ""))
    except ValueError:
        return None
    if icecast.get("tls") and not address.tls:
        address = replace(address, tls=True)
    return address


def icecast_auth_settings(icecast: dict[str, Any]) -> IcecastSettings:
    address = saved_icecast_address(icecast)
    if address is None:
        raise ValueError("The Icecast server address is not valid")
    return IcecastSettings(
        server=f"{'https' if address.tls else 'http'}://{address.host}",
        port=str(address.port),
        username=icecast["username"],
        password=icecast["password"],
        mountpoint=address.mount,
    )


def icecast_mountpoint_url(icecast: dict[str, Any]) -> str:
    address = saved_icecast_address(icecast)
    if address is None:
        host = str(icecast.get("host", "")).strip()
        mount = str(icecast.get("mount", "")).strip()
        return f"{host}{mount}" if host else (mount or "the configured Icecast mountpoint")
    return f"{'https' if address.tls else 'http'}://{address.host}:{address.port}{address.mount}"


def friendly_rtl_device_label(device: dict[str, Any]) -> str:
    name = str(device.get("name", "")).strip()
    vendor = str(device.get("vendor", "")).strip()
    if name:
        label = name
        if vendor and vendor.lower() not in label.lower() and vendor.lower() not in {"realtek", "0bda"}:
            label = f"{vendor} {label}"
    else:
        label = vendor or "RTL-SDR"
    return re.sub(r"\s+", " ", label.replace(",", " ")).strip()


def friendly_soundcard_device_label(device: dict[str, Any]) -> str:
    card = str(device.get("card_long_name") or device.get("card_name") or device.get("card_id") or "").strip()
    pcm = str(device.get("pcm_name") or device.get("pcm_id") or "").strip()
    bus_value = str(device.get("bus", "")).strip().lower()
    bus = "USB" if bus_value == "usb" else "PCI" if bus_value == "pci" else ""
    parts: list[str] = []
    if card:
        parts.append(card)
    if pcm and pcm.lower() not in card.lower():
        parts.append(pcm)
    if bus:
        parts.append(bus)
    label = ", ".join(parts)
    label = re.sub(r"\b(Generic USB|SOF[- ]DSP|sof[- ]hdadsp)\b", "", label, flags=re.IGNORECASE)
    label = re.sub(r"\s+,", ",", label)
    label = re.sub(r"\s{2,}", " ", label).strip(" ,")
    return label or str(device.get("display_name") or device.get("device") or device.get("hw_device") or "Sound card").strip()


def enabled_output_count(
    stream: dict[str, Any],
    *,
    disabled_output_id: str | None = None,
    eas_enabled: bool | None = None,
    archive_enabled: bool | None = None,
) -> int:
    count = 0
    for output in stream_outputs(stream):
        if disabled_output_id and output.get("id") == disabled_output_id:
            continue
        if output.get("enabled", True):
            count += 1
    if eas_enabled is None:
        eas_enabled = eas_recording_settings_from_stream(stream).enabled
    if eas_enabled:
        count += 1
    # Stream archiving keeps a stream running like an output, though it isn't counted
    # among the outputs the streams list shows.
    if archive_enabled is None:
        archive_enabled = stream_archive_settings_from_stream(stream).enabled
    if archive_enabled:
        count += 1
    return count


def stream_worker_key(stream: dict[str, Any]) -> str:
    return str(stream.get("id", ""))


def icecast_encoder_key(config: IcecastConfig) -> tuple[str, int, int]:
    return config.format, config.sample_rate, config.bitrate


def icecast_output_signature(output: dict[str, Any]) -> str:
    icecast = output.get("icecast", {})
    return json.dumps(
        {
            "id": output.get("id"),
            "enabled": output.get("enabled", True),
            "host": icecast.get("host"),
            "port": icecast.get("port"),
            "tls": bool(icecast.get("tls")),
            "mount": icecast.get("mount"),
            "username": icecast.get("username"),
            "password": icecast.get("password"),
            "format": icecast.get("format"),
            "sample_rate": icecast.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE),
            "bitrate": icecast.get("bitrate", DEFAULT_STREAM_BITRATES.get(icecast.get("format", "mp3"), 64)),
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def icecast_auth_signature(icecast: dict[str, Any]) -> dict[str, Any]:
    address = saved_icecast_address(icecast)
    return {
        "host": address.host if address else icecast.get("host"),
        "port": address.port if address else icecast.get("port"),
        "tls": bool(address.tls) if address else bool(icecast.get("tls")),
        "username": icecast.get("username"),
        "password": icecast.get("password"),
        "mount": icecast.get("mount"),
    }


def icecast_auth_cache_key(icecast: dict[str, Any]) -> str:
    return json.dumps(icecast_auth_signature(icecast), sort_keys=True, separators=(",", ":"))


def icecast_auth_changed(output: dict[str, Any], icecast: dict[str, Any]) -> bool:
    return output.get("auth_signature") != icecast_auth_signature(icecast)


def icecast_config_from_output(output: dict[str, Any]) -> IcecastConfig:
    icecast = output["icecast"]
    address = saved_icecast_address(icecast)
    if address is None:
        raise ValueError(f"The Icecast server address {icecast.get('host', '')!r} is not valid")
    return IcecastConfig(
        host=address.host,
        port=address.port,
        mount=address.mount,
        tls=address.tls,
        username=icecast["username"],
        password=icecast["password"],
        format=icecast["format"],
        sample_rate=int(icecast.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE)),
        bitrate=int(icecast.get("bitrate", DEFAULT_STREAM_BITRATES[icecast["format"]])),
        enabled=bool(output.get("enabled", True)),
    )


def validate_soundcard_output_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("soundcard settings are required")
    stable_id = str(raw.get("stable_id", "")).strip()
    if not stable_id:
        raise ValueError("select a sound card")
    channel_mode = str(raw.get("channel_mode", ALSA_CHANNEL_BOTH)).strip().lower()
    if channel_mode not in ALSA_CHANNEL_MODES:
        raise ValueError("select left, right, or both channels")
    try:
        volume = float(raw.get("volume", 1.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("soundcard volume must be a number") from exc
    if volume < 0.0 or volume > 2.0:
        raise ValueError("soundcard volume must be from 0 through 2")
    try:
        sample_rate = int(raw.get("sample_rate", ALSA_STREAM_DEFAULT_SAMPLE_RATE))
    except (TypeError, ValueError) as exc:
        raise ValueError("soundcard sample rate must be a number") from exc
    if sample_rate < 8000 or sample_rate > 192000:
        raise ValueError("soundcard sample rate is not supported")
    return {
        "stable_id": stable_id,
        "channel_mode": channel_mode,
        "volume": volume,
        "sample_rate": sample_rate,
    }


def soundcard_channels(channel_mode: object) -> set[str]:
    mode = str(channel_mode or ALSA_CHANNEL_BOTH).strip().lower()
    if mode == ALSA_CHANNEL_LEFT:
        return {ALSA_CHANNEL_LEFT}
    if mode == ALSA_CHANNEL_RIGHT:
        return {ALSA_CHANNEL_RIGHT}
    return {ALSA_CHANNEL_LEFT, ALSA_CHANNEL_RIGHT}


def soundcard_tap_config_from_output(output: dict[str, Any]) -> AlsaStreamTapConfig:
    soundcard = validate_soundcard_output_payload(output.get("soundcard", {}))
    return soundcard_tap_config_from_soundcard(soundcard)


def soundcard_tap_config_from_soundcard(soundcard: dict[str, Any]) -> AlsaStreamTapConfig:
    return AlsaStreamTapConfig(
        stable_id=soundcard["stable_id"],
        output_sample_rate=soundcard["sample_rate"],
        channel_mode=soundcard["channel_mode"],
        software_volume=soundcard["volume"],
    )


def friendly_stream_error(exc: Exception) -> str:
    message = str(exc)
    if "401" in message:
        return "Invalid Icecast username or password."
    if "403" in message:
        return "The Icecast mountpoint is already occupied."
    if "404" in message:
        return "The Icecast server or mountpoint does not exist."
    if isinstance(exc, TimeoutError) or "timed out" in message.lower():
        return "The Icecast server took too long to respond."
    return message or "Stream needs attention."


def validate_icecast_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("icecast settings are required")
    service = str(raw.get("service", "")).strip().lower()
    if not service:
        service = STREAM_SERVICE_HOSTS.get(str(raw.get("host", "")).strip().lower(), STREAM_SERVICE_CUSTOM)
    if service not in STREAM_SERVICE_NAMES:
        raise ValueError("Streaming service is not supported")
    # Whatever was typed (host, host:port or a full http:// or https:// address) is
    # saved as a plain host, port and mountpoint, plus whether to use TLS.
    address = parse_icecast_address(raw.get("host", ""), raw.get("port", 8000), raw.get("mount", ""))
    host = address.host
    port = address.port
    normalized_host = host.lower()
    suggested_service = STREAM_SERVICE_HOSTS.get(normalized_host)
    if service == STREAM_SERVICE_CUSTOM and suggested_service:
        raise ValueError(f"Please select {STREAM_SERVICE_NAMES[suggested_service]} instead of typing its Icecast URL in custom Icecast setup.")
    username = str(raw.get("username", "")).strip()
    password = str(raw.get("password", ""))
    if not username:
        raise ValueError("Icecast username is required")
    if not password:
        raise ValueError("Icecast password is required")
    mount = address.mount
    if not mount:
        raise ValueError("Icecast mountpoint is required")
    if any(character.isspace() for character in mount):
        raise ValueError("Icecast mountpoint cannot contain whitespace")
    stream_format = str(raw.get("format", "ogg")).strip().lower()
    if stream_format not in {"ogg", "mp3"}:
        raise ValueError("Icecast streaming format must be OGG or MP3")
    try:
        sample_rate = int(raw.get("sample_rate", DEFAULT_STREAM_SAMPLE_RATE))
    except (TypeError, ValueError) as exc:
        raise ValueError("Icecast sample rate must be a number") from exc
    if sample_rate not in STREAM_SAMPLE_RATES:
        raise ValueError("Icecast sample rate is not supported")
    try:
        bitrate = int(raw.get("bitrate", DEFAULT_STREAM_BITRATES[stream_format]))
    except (TypeError, ValueError) as exc:
        raise ValueError("Icecast bitrate must be a number") from exc
    if bitrate < 8 or bitrate > 320 or bitrate % 8 != 0:
        raise ValueError("Icecast bitrate must be from 8 through 320 Kbps in 8 Kbps steps")
    if service == STREAM_SERVICE_GWES:
        if normalized_host != "ingest.wxr.gwes-cdn.net" or port != 10000:
            raise ValueError("GWES Weather Radio requires its predefined Icecast server.")
        if stream_format != "mp3":
            raise ValueError("GWES Weather Radio requires MP3 streaming.")
        if bitrate < 64:
            raise ValueError("GWES Weather Radio requires a bitrate of at least 64 Kbps.")
    elif service == STREAM_SERVICE_WEATHERUSA:
        if normalized_host != "radio-master.weatherusa.net" or port != 80:
            raise ValueError("WeatherUSA requires its predefined Icecast server.")
        if username != "source":
            raise ValueError("WeatherUSA requires the predefined Icecast username.")
        if bitrate < 32 or bitrate > 56:
            raise ValueError("WeatherUSA bitrate must be from 32 through 56 Kbps.")
        if sample_rate > 22050:
            raise ValueError("WeatherUSA sample rate cannot be above 22050 Hz.")
    elif service == STREAM_SERVICE_NWRORG:
        if normalized_host != "wxradio.org" or port != 8000:
            raise ValueError("NOAA Weather Radio Org requires its predefined Icecast server.")
        if username != "source" or password != "WxRadio2014":
            raise ValueError("NOAA Weather Radio Org requires its predefined Icecast credentials.")
        if stream_format != "mp3":
            raise ValueError("NOAA Weather Radio Org requires MP3 streaming.")
        if bitrate != 32:
            raise ValueError("NOAA Weather Radio Org requires a bitrate of 32 Kbps.")
        if sample_rate != 22050:
            raise ValueError("NOAA Weather Radio Org requires a sample rate of 22050 Hz.")
    return {
        "service": service,
        "host": host,
        "port": port,
        "tls": address.tls,
        "username": username,
        "password": password,
        "mount": mount,
        "format": stream_format,
        "sample_rate": sample_rate,
        "bitrate": bitrate,
    }
