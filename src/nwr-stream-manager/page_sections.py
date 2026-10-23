"""Parts of pages filled in for browsers without the page script: streams, outputs, the dashboard, and the RTL-SDR, notification and SDR sharing settings."""

from __future__ import annotations

import copy
import html
import math
import re
import secrets
from dataclasses import asdict
from typing import Any
from urllib.parse import urlencode

from . import page_rendering
from .baseline_pages import (
    form_message,
    format_decimal_bytes,
    IQ_RECORDER_RESULTS,
    NTFY_PUBLIC_SERVER_URL,
    page_route,
    REMOTE_SDR_CHOICE_PREFIX,
)
from .config import notch_max_width
from .constants import (
    IQ_RECORDER_MODE_SPECTRUM,
    IQ_RECORDER_MODE_STREAM,
    STREAM_SERVICE_CUSTOM,
    STREAM_SERVICE_GWES,
    STREAM_SERVICE_HOSTS,
    STREAM_SERVICE_NAMES,
    STREAM_SERVICE_NWRORG,
    STREAM_SERVICE_WEATHERUSA,
)
from .eas_alerts import eas_recording_settings_from_stream, validate_eas_recording_payload
from .fallback_settings import stream_fallback_action_from_stream, validate_stream_fallback_action_payload
from .models import normalize_notification_access_url
from .notifications import stream_notification_settings_from_stream, validate_stream_notification_payload
from .state_files import validate_remote_buffer_seconds
from .stream_config import soundcard_channels, validate_audio_payload


SIGNAL_QUALITY_LABELS = {"excellent": "Excellent", "good": "Good", "fair": "Fair", "poor": "Poor", "no_signal": "No signal"}
RECEPTION_PROBLEM_DETAILS = {
    "bad_reception": "Bad reception: the signal is too weak or noisy to be usable.",
    "no_signal": "No signal: only static is being received.",
}
STREAM_STATUS_LABELS = {
    "enabled": "Enabled",
    "needs-attention": "Needs attention",
    "standby": "Disconnected (no station audio)",
    "disabled": "Disabled",
}


def stream_enabled(stream: dict[str, Any]) -> bool:
    return stream.get("enabled") not in (False, "false", 0)


def stream_label(stream: dict[str, Any]) -> str:
    station = stream.get("station") or {}
    if station.get("callsign") and station.get("frequency"):
        return f"{station['callsign']} {station['frequency']} MHz"
    return str(station.get("callsign") or "stream")


def stream_signal_display(signal: dict[str, Any] | None) -> dict[str, str]:
    """A stream's signal, as the page script describes it."""
    if not signal or not signal.get("available"):
        return {"text": "No signal data", "quality": "", "detail": ""}
    snr = signal.get("snr_db")
    has_snr = isinstance(snr, (int, float)) and math.isfinite(snr)
    quality = "no_signal" if not has_snr else signal.get("quality") if signal.get("quality") in SIGNAL_QUALITY_LABELS else "poor"
    no_signal = quality == "no_signal"
    text = SIGNAL_QUALITY_LABELS["no_signal"] if no_signal else f"{SIGNAL_QUALITY_LABELS[quality]} ({round(snr)} dB SNR)"
    parts = []
    if not no_signal and isinstance(signal.get("signal_dbfs"), (int, float)):
        parts.append(f"Signal {signal['signal_dbfs']:.0f} dBFS")
    if isinstance(signal.get("noise_floor_dbfs"), (int, float)):
        parts.append(f"{'noise' if parts else 'Noise'} floor {signal['noise_floor_dbfs']:.0f} dBFS")
    return {"text": text, "quality": quality, "detail": ", ".join(parts)}


def stream_reception_problem(signal: dict[str, Any] | None) -> str | None:
    if not signal or not signal.get("available"):
        return None
    problem = signal.get("reception_problem")
    return problem if problem in RECEPTION_PROBLEM_DETAILS else None


def stream_list_status(stream: dict[str, Any], status: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """A configured stream's status and signal, as the Manage Streams list shows them."""
    signals = status.get("stream_signals") or {}
    signal = signals.get(stream.get("id")) if stream_enabled(stream) else None
    if signal and not signal.get("available"):
        signal = None
    statuses = []
    for item in status.get("active_streams") or []:
        if item.get("id") == stream.get("id"):
            value = str(item.get("status") or "disabled").lower().replace("_", "-")
            statuses.append(value if value in STREAM_STATUS_LABELS else "disabled")
    if stream_reception_problem(signal):
        statuses.append("needs-attention")
    for value in ("needs-attention", "enabled", "standby"):
        if value in statuses:
            return value, signal
    return "disabled", signal


def stream_output_count(stream: dict[str, Any]) -> int:
    outputs = stream.get("outputs") if isinstance(stream.get("outputs"), list) else []
    count = sum(1 for output in outputs if output.get("enabled") is not False)
    return count + (1 if (stream.get("eas_recording") or {}).get("enabled") else 0)


def stream_row_html(stream: dict[str, Any], status: dict[str, Any], read_only: bool, token_input: str) -> str:
    """A row of the Manage Streams list, keyed and labelled as the page script makes them."""
    station = stream.get("station") or {}
    stream_id = html.escape(str(stream.get("id", "")))
    state, signal = stream_list_status(stream, status)
    display = stream_signal_display(signal)
    label = html.escape(stream_label(stream))
    signal_class = f"status-text signal-{display['quality']}" if display["quality"] else "hint"
    signal_title = f' title="{html.escape(display["detail"])}"' if display["quality"] and display["detail"] else ""
    actions = ""
    if not read_only:
        enabled = stream_enabled(stream)
        verb = "Stop stream" if enabled else "Start stream"
        settings = urlencode({"view": "stream_settings", "stream": stream.get("id", "")})
        remove = urlencode({"view": "streams", "confirm": "remove", "stream": stream.get("id", "")})
        actions = (
            f'<form method="post" action="/streams/enabled">{token_input}'
            f'<input type="hidden" name="stream" value="{stream_id}">'
            f'<input type="hidden" name="enabled" value="{"0" if enabled else "1"}">'
            f'<button type="submit" aria-label="{verb} {label}">{verb}</button></form>'
            f'<a class="button-link" href="/?{html.escape(settings)}" aria-label="Edit stream settings for {label}">Edit stream settings</a>'
            f'<a class="button-link" href="/?{html.escape(remove)}" aria-label="Remove stream {label}">Remove stream</a>'
        )
    frequency = f"{station.get('frequency')} MHz" if station.get("frequency") else "Unknown"
    cells = (
        f'<td role="cell" data-label="Callsign">{html.escape(str(station.get("callsign") or "Unknown"))}</td>'
        f'<td role="cell" data-label="Frequency">{html.escape(frequency)}</td>'
        f'<td role="cell" data-label="Outputs">{stream_output_count(stream)}</td>'
        f'<td class="{signal_class}"{signal_title} role="cell" data-label="Signal">{html.escape(display["text"])}</td>'
        f'<td class="status-text status-{state}" role="cell" data-label="Status">{html.escape(STREAM_STATUS_LABELS[state])}</td>'
        f'<td class="menu-cell" role="cell" data-label="Actions"><div class="row-actions">{actions}</div></td>'
    )
    return f'<tr data-row-key="stream:{stream_id}" role="row">{cells}</tr>'


def station_choice_label(station: dict[str, Any]) -> str:
    """A station, as the page script lists it when choosing one."""
    place = ", ".join(str(part) for part in (station.get("city"), station.get("state")) if part)
    site = f", {station['site_name']}" if station.get("site_name") and station.get("site_name") != station.get("city") else ""
    return f"{station.get('callsign')} {station.get('frequency')} MHz, {place}{site}"


def soundcard_label(soundcard: dict[str, Any], devices: list[dict[str, Any]]) -> str:
    """A sound card's name, as the page script gives it."""
    device = next((item for item in devices if item.get("stable_id") == soundcard.get("stable_id")), None)
    if device is None:
        return str(
            soundcard.get("display_name") or soundcard.get("card_long_name") or soundcard.get("card_name")
            or soundcard.get("device") or "Sound card"
        )
    card = str(device.get("card_long_name") or device.get("card_name") or device.get("card_id") or "").strip()
    pcm = str(device.get("pcm_name") or device.get("pcm_id") or "").strip()
    bus = {"usb": "USB", "pci": "PCI"}.get(str(device.get("bus") or ""), "")
    parts = [card] if card else []
    if pcm and pcm.lower() not in card.lower():
        parts.append(pcm)
    if bus:
        parts.append(bus)
    label = re.sub(r"\b(Generic USB|SOF[- ]DSP|sof[- ]hdadsp)\b", "", ", ".join(parts), flags=re.IGNORECASE)
    label = re.sub(r"\s{2,}", " ", re.sub(r"\s+,", ",", label)).strip()
    return label or str(device.get("display_name") or device.get("hw_device") or "Sound card")


def stream_output_status(stream: dict[str, Any], output: dict[str, Any], status: dict[str, Any]) -> str:
    if not stream_enabled(stream) or output.get("enabled") is False:
        return "disabled"
    for item in status.get("active_streams") or []:
        if item.get("id") == stream.get("id") and any(active.get("id") == output.get("id") for active in item.get("outputs") or []):
            value = str(item.get("status") or "").lower().replace("_", "-")
            return {"enabled": "connected", "standby": "standby"}.get(value, "needs-attention")
    return "needs-attention"


OUTPUT_STATUS_LABELS = {
    "connected": "Connected",
    "needs-attention": "Needs attention",
    "standby": "Disconnected (no station audio)",
    "disabled": "Disabled",
}


def stream_output_row_html(stream: dict[str, Any], output: dict[str, Any], status: dict[str, Any], soundcards: list[dict[str, Any]]) -> str:
    """A row of a stream's outputs, keyed and labelled as the page script makes them."""
    state = stream_output_status(stream, output, status)
    label = OUTPUT_STATUS_LABELS[state]
    if output.get("type") == "soundcard":
        soundcard = output.get("soundcard") or {}
        label = "Enabled" if state == "connected" else label
        channels = {"left": "Left", "right": "Right"}.get(str(soundcard.get("channel_mode") or ""), "Both left and right")
        destination_label = soundcard_label(soundcard, soundcards)
        values = (("Sound card name", destination_label), ("Channels", channels))
    else:
        icecast = output.get("icecast") or {}
        destination = f"{icecast.get('host')}:{icecast.get('port')}{icecast.get('mount')}" if icecast.get("host") else "Unknown"
        fmt = str(icecast.get("format") or "mp3")
        bitrate = icecast.get("bitrate") or {"mp3": 64, "ogg": 48}.get(fmt, 64)
        destination_label = destination
        values = (
            ("Destination", destination),
            ("Format", fmt.upper()),
            ("Sample rate", f"{icecast.get('sample_rate') or 24000} Hz"),
            ("Bitrate", f"{bitrate} Kbps"),
        )
    cells = "".join(f'<td role="cell" data-label="{name}">{html.escape(str(value))}</td>' for name, value in values)
    return (
        f'<tr data-row-key="output:{html.escape(str(output.get("id", "")))}" role="row">{cells}'
        f'<td class="status-text status-{state}" role="cell" data-label="Status">{label}</td>'
        f'<td class="menu-cell" role="cell" data-label="Actions"><div class="row-actions">'
        f'<a class="button-link" href="{html.escape(page_route("stream_output", stream=stream.get("id", ""), output=output.get("id", "")))}" '
        f'aria-label="Edit output {html.escape(destination_label)}">Edit output</a></div></td></tr>'
    )


STREAM_SERVICE_HELP_HTML = {
    STREAM_SERVICE_GWES: (
        'No credentials yet? <a href="https://forms.office.com/r/MLx6hKmnCe" target="_blank" rel="noopener noreferrer">Submit your stream</a> '
        "to GWES Weather Radio."
    ),
    STREAM_SERVICE_WEATHERUSA: (
        'No credentials yet? <a href="https://www.weatherusa.net/members/new" target="_blank" rel="noopener noreferrer">Create a WeatherUSA account</a>, '
        'then <a href="https://www.weatherusa.net/members/services/radio" target="_blank" rel="noopener noreferrer">create a stream</a>.'
    ),
    STREAM_SERVICE_NWRORG: (
        'Find the mountpoint with the <a href="https://noaaweatherradio.org/N2radio-finder.php" target="_blank" rel="noopener noreferrer">'
        "Weather Radio Station Lookup Utility</a>."
    ),
}
NWRORG_SUBMISSION_HTML = (
    "<p>If you have not already done so, you must fill out the "
    '<a href="http://noaaweatherradio.org/addstream/addstream.html" target="_blank" rel="noopener noreferrer">submission form</a> '
    "for NOAA Weather Radio Org before your stream will appear on the website.</p>"
)
STREAM_SAMPLE_RATE_CHOICES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)
STREAM_BITRATE_CHOICES = tuple(range(8, 321, 8))
# The Icecast fields that a form keeps, by their form names.
ICECAST_FORM_FIELDS = ("host", "port", "username", "password", "mount", "format", "sample_rate", "bitrate", "alt_enabled", "alt_number")


def station_site_token(station: dict[str, Any] | None) -> str:
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", str((station or {}).get("site_name") or "")).split()
    return "".join(word[:1].upper() + word[1:].lower() for word in words)


def weatherusa_mount(station: dict[str, Any] | None, stream_format: str) -> str:
    callsign = str((station or {}).get("callsign") or "").strip().upper() or "CALLSIGN"
    return f"/NWR/{callsign}.{stream_format or 'mp3'}"


def nwrorg_mount(station: dict[str, Any] | None, alternate: bool, alternate_number: Any) -> str:
    state = str((station or {}).get("state") or "").strip().upper() or "XX"
    callsign = str((station or {}).get("callsign") or "").strip().upper() or "CALLSIGN"
    base = f"/{state}-{station_site_token(station) or 'Site'}-{callsign}"
    if not alternate:
        return base
    try:
        number = max(1, min(9, int(alternate_number or 1)))
    except ValueError:
        number = 1
    return f"{base}-alt{number}"


def icecast_service(icecast: dict[str, Any] | None) -> str:
    explicit = str((icecast or {}).get("service") or "").strip()
    if explicit in STREAM_SERVICE_NAMES:
        return explicit
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", str((icecast or {}).get("host") or "").strip().lower())
    return STREAM_SERVICE_HOSTS.get(re.split(r"[/:?#]", host)[0], STREAM_SERVICE_CUSTOM)


def number_or(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def apply_icecast_service_preset(icecast: dict[str, Any], service: str, station: dict[str, Any] | None) -> dict[str, Any]:
    """An Icecast output with its streaming service's fixed settings, as the page script sets them."""
    payload = {**icecast, "service": service}
    stream_format = payload.get("format") or "mp3"
    if service == STREAM_SERVICE_GWES:
        payload.update(host="ingest.wxr.gwes-cdn.net", port=10000, format="mp3")
        payload["bitrate"] = max(64, number_or(payload.get("bitrate"), 64))
        payload["sample_rate"] = number_or(payload.get("sample_rate"), 22050) or 22050
    elif service == STREAM_SERVICE_WEATHERUSA:
        payload.update(host="radio-master.weatherusa.net", port=80, username="source", format=stream_format)
        payload["bitrate"] = max(32, min(56, number_or(payload.get("bitrate"), 56 if stream_format == "mp3" else 48)))
        payload["sample_rate"] = min(22050, number_or(payload.get("sample_rate"), 22050) or 22050)
        if not str(payload.get("mount") or "").strip():
            payload["mount"] = weatherusa_mount(station, stream_format)
    elif service == STREAM_SERVICE_NWRORG:
        payload.update(host="wxradio.org", port=8000, username="source", password="WxRadio2014", format="mp3", bitrate=32, sample_rate=22050)
        payload["mount"] = nwrorg_mount(station, bool(payload.get("alt_enabled")), payload.get("alt_number"))
    return payload


def icecast_value_allowed(service: str, name: str, value: int) -> bool:
    """Whether a service allows a bitrate or sample rate, as the page script's lists offer them."""
    if service == STREAM_SERVICE_GWES:
        return value >= 64 if name == "bitrate" else True
    if service == STREAM_SERVICE_WEATHERUSA:
        return 32 <= value <= 56 if name == "bitrate" else value <= 22050
    if service == STREAM_SERVICE_NWRORG:
        return value == (32 if name == "bitrate" else 22050)
    return True


def icecast_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """An Icecast output from form fields, in the form the server checks."""
    return {
        "host": str(raw.get("host") or "").strip(),
        "port": number_or(raw.get("port"), 0),
        "username": str(raw.get("username") or ""),
        "password": str(raw.get("password") or ""),
        "mount": str(raw.get("mount") or "").strip(),
        "format": "ogg" if raw.get("format") == "ogg" else "mp3",
        "sample_rate": number_or(raw.get("sample_rate"), 24000),
        "bitrate": number_or(raw.get("bitrate"), 64),
        "alt_enabled": raw.get("alt_enabled") in (True, "1"),
        "alt_number": raw.get("alt_number") or "1",
    }


def icecast_form_values(icecast: dict[str, Any]) -> dict[str, str]:
    """An Icecast output as form fields show it."""
    host = str(icecast.get("host") or "")
    if icecast.get("tls") and host and not host.lower().startswith("https:"):
        host = f"https://{host}"
    mount = str(icecast.get("mount") or "")
    match = re.search(r"-alt([1-9])$", mount)
    return {
        "host": host,
        "port": str(icecast.get("port") or ""),
        "username": str(icecast.get("username") or ""),
        "password": str(icecast.get("password") or ""),
        "mount": mount,
        "format": str(icecast.get("format") or "mp3"),
        "sample_rate": str(icecast.get("sample_rate") or 24000),
        "bitrate": str(icecast.get("bitrate") or (64 if icecast.get("format", "mp3") == "mp3" else 48)),
        "alt_enabled": "1" if icecast_service(icecast) == STREAM_SERVICE_NWRORG and match else "",
        "alt_number": match.group(1) if match else "1",
    }


def soundcard_occupancy(streams: list[dict[str, Any]], ignore_output_id: str = "") -> dict[str, set[str]]:
    """The sound card channels in use, as the page script counts them."""
    occupied: dict[str, set[str]] = {}
    for stream in streams:
        for output in stream.get("outputs") or []:
            if output.get("type") != "soundcard" or output.get("enabled") is False or output.get("id") == ignore_output_id:
                continue
            stable_id = str((output.get("soundcard") or {}).get("stable_id") or "")
            if stable_id:
                occupied.setdefault(stable_id, set()).update(soundcard_channels((output.get("soundcard") or {}).get("channel_mode") or "both"))
    return occupied


def available_soundcard_channels(streams: list[dict[str, Any]], stable_id: str, ignore_output_id: str = "") -> list[str]:
    used = soundcard_occupancy(streams, ignore_output_id).get(stable_id, set())
    return [channel for channel in ("left", "right") if channel not in used]


def soundcard_channel_choice(preferred: str, available: list[str]) -> str:
    if not available:
        return ""
    if preferred == "both" and len(available) == 2:
        return "both"
    if preferred in available:
        return preferred
    return available[0]


def fill_icecast_fields(page: str, prefix: str, values: dict[str, str], service: str, station: dict[str, Any] | None) -> str:
    """Fills an Icecast form (prefix "icecast" in the wizard, "settings_icecast" when editing) for its
    streaming service, showing and offering what the page script does for that service."""
    preset = apply_icecast_service_preset(icecast_payload(values), service, station)
    shown = {**values, **{name: str(preset[name]) for name in ("host", "port", "username", "password", "mount") if service != STREAM_SERVICE_CUSTOM}}
    shown["format"] = str(preset["format"])
    page = page_rendering.set_content(
        page,
        f"{prefix}_service",
        "".join(
            f'<option value="{value}"{" selected" if value == service else ""}>{html.escape(name)}</option>'
            for value, name in (
                (STREAM_SERVICE_CUSTOM, "Custom Icecast server"),
                (STREAM_SERVICE_GWES, STREAM_SERVICE_NAMES[STREAM_SERVICE_GWES]),
                (STREAM_SERVICE_WEATHERUSA, STREAM_SERVICE_NAMES[STREAM_SERVICE_WEATHERUSA]),
                (STREAM_SERVICE_NWRORG, STREAM_SERVICE_NAMES[STREAM_SERVICE_NWRORG]),
            )
        ),
    )
    page = page_rendering.set_content(page, f"{prefix}_service_help", STREAM_SERVICE_HELP_HTML.get(service, ""))
    for name in ("host", "port", "username", "password", "mount"):
        page = page_rendering.set_attribute(page, f"{prefix}_{name}", "value", shown.get(name, ""))
    page = page_rendering.set_attribute(page, f"{prefix}_username", "readonly", "readonly" if service == STREAM_SERVICE_WEATHERUSA else None)
    custom = service == STREAM_SERVICE_CUSTOM
    fixed = service in (STREAM_SERVICE_GWES, STREAM_SERVICE_NWRORG)
    nwrorg = service == STREAM_SERVICE_NWRORG
    show_password = "settings_show_icecast_password_label" if prefix == "settings_icecast" else "show_icecast_password_label"
    for element_id, hidden in (
        (f"{prefix}_host_label", not custom),
        (f"{prefix}_port_label", not custom),
        (f"{prefix}_username_label", service in (STREAM_SERVICE_WEATHERUSA, STREAM_SERVICE_NWRORG)),
        (f"{prefix}_password_label", nwrorg),
        # Shown by a script where one runs, since showing the password needs it.
        (show_password, True),
        (f"{prefix}_mount_label", nwrorg),
        (f"{prefix}_alt_label", not nwrorg),
        (f"{prefix}_alt_number_label", not nwrorg),
        (f"{prefix}_format_fieldset", fixed),
        (f"{prefix}_sample_rate_label", nwrorg),
        (f"{prefix}_bitrate_label", nwrorg),
    ):
        page = page_rendering.set_hidden(page, element_id, hidden)
    for stream_format in ("mp3", "ogg"):
        page = page_rendering.set_attribute(page, f"{prefix}_format_{stream_format}", "checked", "checked" if shown["format"] == stream_format else None)
    page = page_rendering.set_attribute(page, f"{prefix}_alt_enabled", "checked", "checked" if values.get("alt_enabled") == "1" else None)
    page = page_rendering.set_attribute(page, f"{prefix}_alt_number", "value", values.get("alt_number") or "1")
    for name, choices, unit in (("sample_rate", STREAM_SAMPLE_RATE_CHOICES, "Hz"), ("bitrate", STREAM_BITRATE_CHOICES, "Kbps")):
        allowed = [value for value in choices if icecast_value_allowed(service, name, value)]
        chosen = number_or(preset[name], allowed[0])
        if chosen not in allowed:
            chosen = allowed[0]
        page = page_rendering.set_content(
            page,
            f"{prefix}_{name}",
            "".join(
                f'<option value="{value}"{" selected" if value == chosen else ""}{"" if value in allowed else " disabled"}>{value} {unit}</option>'
                for value in choices
            ),
        )
    return page


def soundcard_device_hint(devices: list[dict[str, Any]], chosen: str, problem: str, configured_missing: bool = False) -> str:
    """What the sound card choice says under it; the page script says the same."""
    if problem and not devices:
        return problem
    if configured_missing:
        return "The configured sound card is not connected."
    if chosen:
        return "Used directly, not shared with other programs."
    if devices:
        return "Every sound card's channels are in use."
    return "No sound cards found on this computer."


def soundcard_device_options_html(
    streams: list[dict[str, Any]], devices: list[dict[str, Any]], selected: str, ignore_output_id: str, keep_configured: bool
) -> tuple[str, str]:
    """The sound card list, as the page script makes it, with each card's free channels for a
    script to show; and the card it chooses."""
    options: list[tuple[str, str, list[str]]] = []
    device_ids = {str(device.get("stable_id")) for device in devices}
    if keep_configured and selected and selected not in device_ids:
        options.append((selected, f"Configured sound card, {selected} (not connected)", available_soundcard_channels(streams, selected, ignore_output_id)))
    for device in devices:
        stable_id = str(device.get("stable_id") or "")
        channels = available_soundcard_channels(streams, stable_id, ignore_output_id)
        if stable_id != selected and not channels or not stable_id:
            continue
        options.append((stable_id, soundcard_label({"stable_id": stable_id}, devices), channels))
    if not options:
        empty = "No sound cards with free channels" if devices else "No sound cards found"
        return f'<option value="">{empty}</option>', ""
    chosen = selected if any(value == selected for value, _label, _channels in options) else options[0][0]
    return "".join(
        f'<option value="{html.escape(value)}" data-channels="{" ".join(channels)}"{" selected" if value == chosen else ""}>{html.escape(label)}</option>'
        for value, label, channels in options
    ), chosen


def fill_soundcard_channels(page: str, prefix: str, available: list[str], preferred: str) -> str:
    """Offers only the channels a sound card has free, as the page script does."""
    chosen = soundcard_channel_choice(preferred, available)
    for mode in ("left", "both", "right"):
        free = all(channel in available for channel in soundcard_channels(mode))
        control = f"{prefix}_soundcard_channel_{mode}"
        page = page_rendering.set_attribute(page, control, "checked", "checked" if mode == chosen else None)
        page = page_rendering.set_attribute(page, control, "disabled", None if free else "disabled")
        page = page_rendering.set_hidden(page, f"{control}_label", not free)
    return page


# The stream settings that work without the page script: the form field, which
# settings it's in, its key there, and how to read it. Numbers have the page's limits.
STREAM_SETTINGS_FIELDS: tuple[tuple[str, str, str, str], ...] = (
    ("stream_enabled", "enabled", "enabled", "bool"),
    ("eas_enabled", "eas_recording", "enabled", "bool"),
    ("eas_pre_seconds", "eas_recording", "pre_seconds", "text"),
    ("eas_post_seconds", "eas_recording", "post_seconds", "text"),
    ("eas_max_seconds", "eas_recording", "max_seconds", "text"),
    ("eas_format", "eas_recording", "format", "text"),
    ("stream_fallback_action", "fallback_action", "action", "text"),
    ("stream_fallback_delay", "fallback_action", "delay_seconds", "text"),
    ("audio_volume_enabled", "audio", "volume.enabled", "bool"),
    ("audio_volume_multiplier", "audio", "volume.multiplier", "number"),
    ("audio_comfort_noise_enabled", "audio", "comfort_noise.enabled", "bool"),
    ("audio_comfort_noise_level", "audio", "comfort_noise.level_db", "number"),
    ("audio_highpass_enabled", "audio", "highpass.enabled", "bool"),
    ("audio_highpass_frequency", "audio", "highpass.frequency", "number"),
    ("audio_highpass_sharpness", "audio", "highpass.sharpness", "number"),
    ("audio_lowpass_enabled", "audio", "lowpass.enabled", "bool"),
    ("audio_lowpass_frequency", "audio", "lowpass.frequency", "number"),
    ("audio_lowpass_sharpness", "audio", "lowpass.sharpness", "number"),
    ("audio_notch_enabled", "audio", "notch.enabled", "bool"),
    ("audio_notch_frequency", "audio", "notch.frequency", "number"),
    ("audio_notch_width", "audio", "notch.width", "number"),
    ("stream_notify_icecast_failures", "notifications", "icecast_failures", "bool"),
    ("stream_notify_soundcard_failures", "notifications", "soundcard_failures", "bool"),
    ("stream_notify_bad_reception", "notifications", "bad_reception", "bool"),
    ("stream_notify_eas_alerts", "notifications", "recorded_eas_alerts", "bool"),
)
AUDIO_NUMBER_LIMITS = {
    "audio_volume_multiplier": (0.1, 2, "Enter a volume multiplier from 0.1 to 2."),
    "audio_comfort_noise_level": (-60, -30, "Enter a comfort noise level from -60 to -30."),
    "audio_highpass_frequency": (1, 900, "Enter a highpass frequency from 1 to 900 Hz."),
    "audio_highpass_sharpness": (0, 10, "Enter a highpass sharpness from 0 to 10."),
    "audio_lowpass_frequency": (2200, 12000, "Enter a lowpass frequency from 2200 to 12000 Hz."),
    "audio_lowpass_sharpness": (0, 10, "Enter a lowpass sharpness from 0 to 10."),
    "audio_notch_frequency": (1, 12000, "Enter a notch filter frequency from 1 to 12000 Hz."),
    "audio_notch_width": (60, 2000, "Enter a notch filter width from 60 to 2000 Hz."),
}
PROTECTED_AUDIO_BANDS_HZ = ((900, 1100), (1400, 1600), (2000, 2200))


# Audio effects a stream hasn't set, as the page script shows them.
DEFAULT_AUDIO_EFFECTS = {
    "volume": {"enabled": False, "multiplier": 1.0},
    "comfort_noise": {"enabled": False, "level_db": -40.0},
    "highpass": {"enabled": False, "frequency": 300.0, "sharpness": 0.0},
    "lowpass": {"enabled": False, "frequency": 3400.0, "sharpness": 2.0},
    "notch": {"enabled": False, "frequency": 3000.0, "width": 100.0},
}


def stream_audio_effects(stream: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A stream's audio effects, as the page script shows them: its own over the defaults,
    with a filter that has no frequency given the default one."""
    audio = stream.get("audio") if isinstance(stream.get("audio"), dict) else {}
    effects = {}
    for name, defaults in DEFAULT_AUDIO_EFFECTS.items():
        settings = {**defaults, **(audio.get(name) if isinstance(audio.get(name), dict) else {})}
        if "frequency" in defaults and not settings.get("frequency"):
            settings["frequency"] = defaults["frequency"]
        effects[name] = settings
    return effects


def stream_settings_groups(stream: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """A stream's settings, as the stream settings form shows them."""
    return {
        "enabled": {"enabled": stream_enabled(stream)},
        "eas_recording": asdict(eas_recording_settings_from_stream(stream)),
        "fallback_action": asdict(stream_fallback_action_from_stream(stream)),
        "audio": stream_audio_effects(stream),
        "notifications": asdict(stream_notification_settings_from_stream(stream)),
    }


def stream_setting_value(groups: dict[str, dict[str, Any]], group: str, key: str) -> Any:
    value: Any = groups[group]
    for part in key.split("."):
        value = value[part]
    return value


def stream_setting_text(value: Any, kind: str) -> str:
    if kind == "bool":
        return "1" if value else "0"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def stream_settings_changes(form: dict[str, str], stream: dict[str, Any]) -> dict[str, Any]:
    """The settings changed on the stream settings form, which sends them all.

    Each is compared with what the page had when it opened (its was_ field), and
    each group of settings with a change is sent in full, with only the changed
    ones taken from the form. So settings someone else has since changed are kept.
    Everything is checked before anything is saved.
    """
    groups = stream_settings_groups(stream)
    changes: dict[str, Any] = {}
    for name, group, key, kind in STREAM_SETTINGS_FIELDS:
        sent = ("1" if form.get(name) == "1" else "0") if kind == "bool" else form.get(name, "").strip()
        if sent == form.get(f"was_{name}", "").strip():
            continue
        if kind == "bool":
            value: Any = sent == "1"
        elif kind == "number":
            low, high, message = AUDIO_NUMBER_LIMITS[name]
            try:
                value = float(sent)
            except ValueError:
                raise ValueError(message) from None
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(message)
        else:
            value = sent
        settings = changes.setdefault(group, copy.deepcopy(groups[group]))
        *path, last = key.split(".")
        target = settings
        for part in path:
            target = target[part]
        target[last] = value
    audio = changes.get("audio")
    if audio is not None:
        notch = audio["notch"]
        frequency = float(notch["frequency"])
        if any(low <= frequency <= high for low, high in PROTECTED_AUDIO_BANDS_HZ):
            raise ValueError(
                "The notch filter frequency can't be within 900-1100, 1400-1600, or 2000-2200 Hz, "
                "to protect the attention tone and SAME tones."
            )
        widest = int(notch_max_width(frequency))
        if float(notch["width"]) > widest:
            raise ValueError(f"Enter a notch filter width from 60 to {widest} Hz, the widest allowed at {frequency:g} Hz.")
        changes["audio"] = validate_audio_payload(audio)
    if "eas_recording" in changes:
        changes["eas_recording"] = asdict(validate_eas_recording_payload(changes["eas_recording"]))
    if "fallback_action" in changes:
        changes["fallback_action"] = asdict(validate_stream_fallback_action_payload(changes["fallback_action"]))
    if "notifications" in changes:
        changes["notifications"] = asdict(validate_stream_notification_payload(changes["notifications"]))
    return changes


def generate_notification_topic() -> str:
    """A topic secret, made as the page script makes them."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    return "NWRSTMGR-" + "".join(secrets.choice(alphabet) for _ in range(24))


def notification_settings_payload(current: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """Every notification setting the server keeps, so changing one leaves the others as they are."""
    return {
        "enabled": True,
        "server_url": current.get("server_url") or NTFY_PUBLIC_SERVER_URL,
        "topic": current.get("topic") or "",
        "priority": current.get("priority") or 3,
        "access_url_mode": current.get("access_url_mode") or "auto",
        "custom_access_url": current.get("custom_access_url") or "",
        "access_token": "",
        "keep_access_token": bool(current.get("access_token_set")),
        "username": current.get("username") or "",
        "password": "",
        "keep_password": bool(current.get("password_set")),
        **changes,
    }


def ntfy_server_is_self_hosted(url: str) -> bool:
    return bool(url) and url.rstrip("/") != NTFY_PUBLIC_SERVER_URL


def typed_web_address(value: str) -> str:
    """A web address as typed, given https:// if it has no scheme, or "" if it isn't one,
    as the page script checks them."""
    try:
        url = normalize_notification_access_url(value)
    except ValueError:
        return ""
    return "" if any(character.isspace() for character in url) else url


def ntfy_server_url(value: str) -> str:
    if not value.strip():
        raise ValueError("Enter the URL of your self-hosted ntfy server.")
    url = typed_web_address(value)
    if not url:
        raise ValueError("Enter a valid ntfy server URL, such as ntfy.example.com or https://ntfy.example.com.")
    return url


def notification_custom_access_url(value: str) -> str:
    url = typed_web_address(value)
    if not url:
        raise ValueError("Enter a valid custom URL, such as nwr.example.com or https://nwr.example.com.")
    return url


def notification_access_url_options_html(options: list[dict[str, str]], saved: str, chosen: str) -> str:
    """The addresses notifications can open, as the page script lists them."""
    specs = [("auto", f"Automatic ({options[0]['label']})" if options else "Automatic")]
    specs += [(str(option.get("id")), str(option.get("label"))) for option in options]
    if saved not in ("auto", "custom") and not any(option.get("id") == saved for option in options):
        specs.append((saved, "Previously selected address unavailable"))
    specs.append(("custom", "Custom URL"))
    return "".join(
        f'<option value="{html.escape(value)}"{" selected" if value == chosen else ""}>{html.escape(label)}</option>'
        for value, label in specs
    )


def paired_device_status(device: dict[str, Any], remote: dict[str, Any]) -> str:
    """How a paired device is getting on, as the page script says it."""
    network = " over Tailscale" if device.get("via") == "tailscale" else ""
    if device.get("in_use"):
        reachable = (remote.get("status") or {}).get("reachable")
        return f"This machine is using its RTL-SDR{network}" if reachable else "This machine is using its RTL-SDR (not reachable)"
    if device.get("using_this_sdr"):
        return f"Using this machine's RTL-SDR{network}"
    return "Paired"


def paired_device_row_html(device: dict[str, Any], remote: dict[str, Any]) -> str:
    name = html.escape(remote_device_name(device))
    unpair = page_route("settings", section="sdr_sharing", confirm="unpair", device=device.get("id", ""))
    return (
        f'<tr data-row-key="device:{html.escape(str(device.get("id", "")))}" role="row">'
        f'<td role="cell" data-label="Name">{name}</td>'
        f'<td role="cell" data-label="Status">{html.escape(paired_device_status(device, remote))}</td>'
        f'<td role="cell" data-label="Actions"><a class="button-link" href="{html.escape(unpair)}" aria-label="Unpair {name}">Unpair</a></td></tr>'
    )


def remote_pair_choice(host: dict[str, Any]) -> str:
    """A found device as a choice in the list, carrying what pairing with it needs."""
    addresses = ",".join(str(address) for address in host.get("addresses") or [])
    return "|".join(str(part) for part in (host.get("id", ""), host.get("address", ""), host.get("port") or "", addresses))


def remote_pair_target(form: dict[str, str]) -> dict[str, Any]:
    """Where to pair: a found device, or an address entered, as the page script reads them."""
    choice = form.get("device", "")
    if choice and choice != "manual":
        host_id, address, port, addresses = (choice.split("|") + ["", "", "", ""])[:4]
        target: dict[str, Any] = {"address": address, "host_id": host_id, "addresses": [item for item in addresses.split(",") if item]}
        if port:
            target["port"] = int(port)
        return target
    match = re.match(r"^\[?([^\]]+?)\]?(?::(\d+))?$", form.get("address", "").strip())
    if not match:
        raise ValueError("Find a device or enter its address first.")
    return {"address": match.group(1), **({"port": int(match.group(2))} if match.group(2) else {})}


RECEPTION_PROBLEM_REASONS = {"bad_reception": "bad reception", "no_signal": "no signal"}


def sentence_list(items: list[str]) -> str:
    values = [str(item).strip() for item in items if str(item).strip()]
    if len(values) <= 1:
        return values[0] if values else ""
    return f"{', '.join(values[:-1])} and {values[-1]}"


def relative_time_ago(epoch_seconds: float, now: float) -> str:
    minutes = max(0, int((now - float(epoch_seconds or 0)) // 60))
    if minutes < 1:
        return "less than a minute ago"
    if minutes < 60:
        return f"{minutes} minute{'' if minutes == 1 else 's'} ago"
    hours = minutes // 60
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def stream_attention_items(status: dict[str, Any]) -> list[dict[str, Any]]:
    """The streams that need attention and why, as the page script's dashboard lists them."""
    configured = {stream.get("id"): stream for stream in status.get("streams") or []}
    items: dict[str, dict[str, Any]] = {}

    def item_for(stream_id: str, station: dict[str, Any]) -> dict[str, Any]:
        return items.setdefault(stream_id, {
            "id": stream_id, "callsign": station.get("callsign") or "Unknown", "icecast": False, "soundcard": False,
            "reception": None, "errors": [],
        })

    for active in status.get("active_streams") or []:
        if str(active.get("status") or "").lower().replace("_", "-") != "needs-attention":
            continue
        stream = configured.get(active.get("id")) or {}
        station = active.get("station") if (active.get("station") or {}).get("callsign") or (active.get("station") or {}).get("frequency") else stream.get("station") or {}
        stream_id = active.get("id") or stream.get("id") or ""
        if not stream_id:
            continue
        item = item_for(stream_id, station or {})
        if active.get("error"):
            item["errors"].append(active["error"])
        for output in active.get("outputs") or []:
            if str(output.get("status") or active.get("status") or "").lower().replace("_", "-") != "needs-attention":
                continue
            item["soundcard" if output.get("type") == "soundcard" else "icecast"] = True
    for stream in configured.values():
        signal = (status.get("stream_signals") or {}).get(stream.get("id")) if stream_enabled(stream) else None
        problem = stream_reception_problem(signal)
        if problem:
            item_for(str(stream.get("id")), stream.get("station") or {})["reception"] = problem
    result = []
    for item in items.values():
        reasons = []
        if item["reception"]:
            reasons.append(RECEPTION_PROBLEM_REASONS[item["reception"]])
        if item["icecast"]:
            reasons.append("one or more Icecast destinations failed to connect")
        if item["soundcard"]:
            reasons.append("one or more sound cards are not connected")
        if not reasons:
            detail = item["errors"][0] if item["errors"] else "This stream needs attention."
        elif len(reasons) == 1 and item["reception"]:
            detail = RECEPTION_PROBLEM_DETAILS[item["reception"]]
        else:
            sentence = sentence_list(reasons)
            detail = f"{sentence[:1].upper()}{sentence[1:]}."
        result.append({**item, "detail": detail})
    return sorted(result, key=lambda item: str(item["callsign"]))


def format_uptime(seconds: Any) -> str:
    """Such as "2 days, 7 hours, 5 minutes", leaving out what's zero; the page script says the same."""
    try:
        minutes = int(float(seconds or 0) // 60)
    except (TypeError, ValueError):
        minutes = 0
    if minutes < 1:
        return "less than a minute"
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [(days, "day"), (hours, "hour"), (minutes, "minute")]
    return ", ".join(f"{count} {unit}{'' if count == 1 else 's'}" for count, unit in parts if count)


def dashboard_parts(status: dict[str, Any], sdr_label: str, read_only: bool, now: float) -> dict[str, Any]:
    """The dashboard, as the page script fills it in: each part's text or markup, what's hidden,
    and whether storage comes first."""
    settings = status.get("settings") or {}
    source = status.get("source") or {}
    text: dict[str, str] = {}
    markup: dict[str, str] = {}
    iq_file = source.get("kind") == "iq_file"
    attention = not iq_file and bool(status.get("capture_error") or (settings.get("serial") and not status.get("active")))
    hidden = {"dashboard_sdr_attention": not attention, "dashboard_sdr_details": attention, "dashboard_configure_sdr": read_only}
    location = str(status.get("location") or "")
    text["summary_location"] = location
    hidden["dashboard_location"] = not location
    strength = f"{settings.get('alias_filter_strength', 100)}%"
    capture = "active" if status.get("active") else "inactive"
    if iq_file:
        text.update(summary_source="I/Q file", summary_sdr=str(source.get("name") or "unknown file"), summary_ppm="not applicable",
                    summary_gain="not applicable", summary_bias_tee="not applicable")
    else:
        if source.get("kind") == "remote":
            text.update(summary_source=f"Shared by {source.get('name') or 'another NWR Stream Manager'}", summary_sdr=str(source.get("device_name") or "unknown"))
        else:
            text.update(summary_source="RTL-SDR", summary_sdr=sdr_label)
        gain = settings.get("gain")
        text.update(summary_ppm=f"{settings.get('ppm_correction') or 0} PPM", summary_gain="automatic" if gain is None else f"{gain:g} dB",
                    summary_bias_tee="enabled" if settings.get("bias_tee") else "disabled")
    text.update(summary_alias_filter=strength, summary_capture=capture)
    # Streams
    streams = list(status.get("streams") or [])
    enabled = sum(1 for stream in streams if stream_enabled(stream))
    text["dashboard-stream-summary"] = (
        f"{len(streams)} stream{'' if len(streams) == 1 else 's'} configured, {enabled} enabled, {len(streams) - enabled} disabled."
        if streams else "No streams configured."
    )
    items = stream_attention_items(status)
    if not items:
        markup["dashboard-stream-attention"] = '<div class="stream-item success" data-row-key="healthy">All streams are healthy!</div>'
    else:
        names = list(dict.fromkeys(item["callsign"] for item in items))
        summary = f"Stream {names[0]} requires attention!" if len(names) == 1 else f"Streams {sentence_list(names)} require attention."
        rows = [f'<p class="status-text status-needs-attention" data-row-key="summary">{html.escape(summary)}</p>']
        for item in items:
            if read_only:
                label = f"<strong>{html.escape(item['callsign'])}</strong>"
            else:
                link = page_route("stream_settings", stream=item["id"])
                label = f'<a href="{html.escape(link)}" data-view="stream_settings" data-stream-id="{html.escape(item["id"])}">View stream details for {html.escape(item["callsign"])}</a>'
            rows.append(f'<div class="stream-item" data-row-key="stream:{html.escape(item["id"])}">{label}: {html.escape(item["detail"])}</div>')
        markup["dashboard-stream-attention"] = "".join(rows)
    # Recent alerts
    alerts = list(status.get("recent_eas_alerts") or [])
    if not alerts:
        markup["dashboard-recent-alerts"] = '<div class="stream-item hint" data-row-key="empty">No EAS alerts issued in the last 24 hours.</div>'
    else:
        rows = []
        for alert in alerts:
            name = str(alert.get("event_name") or "Unknown alert").lower()
            detail = page_route("eas_alert_detail", stream=alert.get("stream_id", ""), alert=alert.get("id", ""), page=1)
            listing = page_route("eas_alerts", stream=alert.get("stream_id", ""), page=1)
            callsign = html.escape(str(alert.get("callsign") or "Unknown"))
            rows.append(
                f'<div class="stream-item" data-row-key="alert:{html.escape(str(alert.get("stream_id", "")))}:{html.escape(str(alert.get("id", "")))}">'
                f'<a href="{html.escape(detail)}">{html.escape(name[:1].upper() + name[1:])}</a>'
                f" issued {relative_time_ago(alert.get('issued_at_epoch') or 0, now)} on {callsign}. "
                f'<a href="{html.escape(listing)}">View all EAS alerts for {callsign if alert.get("callsign") else "this stream"}</a></div>'
            )
        markup["dashboard-recent-alerts"] = "".join(rows)
    # Storage
    storage = status.get("storage") or {}
    filesystems = storage.get("filesystems") or []
    primary = filesystems[0] if filesystems else None
    text["summary_storage"] = (
        f"{format_decimal_bytes(primary.get('used_bytes') or 0)} of {format_decimal_bytes(primary.get('total_bytes') or 0)} used ({round(float(primary.get('used_percent') or 0))}%)"
        if primary else "unknown"
    )
    usage = storage.get("usage") or {}
    text["summary_iq_storage"] = str(usage.get("iq_recordings") or "0 B")
    text["summary_eas_storage"] = str(usage.get("eas_recordings") or "0 B")
    state = str(storage.get("status") or "unknown")
    warning = str(storage.get("message") or "Please free up disk space.") if storage.get("status") and state != "ok" else ""
    text["storage-warning"] = warning
    hidden["storage-warning"] = not warning
    return {"text": text, "html": markup, "hidden": hidden, "storage_first": state in ("low", "critical")}


def format_gain(value: float) -> str:
    return f"{float(value):g} dB"


def active_sdr_choices(status: dict[str, Any], local_devices: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The Active SDR list, as the page script makes it."""
    settings = status.get("settings") or {}
    remote = status.get("remote_sdr") or {}
    paired = remote.get("devices") or []
    uses_remote = settings.get("source_mode") == "remote" and bool(settings.get("remote_host_id"))
    configured = str(settings.get("serial") or "")
    choices: list[tuple[str, str]] = []
    if not uses_remote and configured and not any(device.get("serial") == configured for device in local_devices):
        choices.append((configured, f"Configured SDR, serial {configured} (not connected)"))
    elif not uses_remote and not configured and len(local_devices) != 1:
        none_found = not local_devices and not paired
        choices.append(("", "No RTL-SDR devices found" if none_found else "Select an RTL-SDR..."))
    for device in local_devices:
        parts = [device.get("vendor"), device.get("name") or "RTL-SDR", f"serial {device.get('serial')}"]
        choices.append((str(device.get("serial")), ", ".join(str(part) for part in parts if part)))
    for device in paired:
        choices.append((f"{REMOTE_SDR_CHOICE_PREFIX}{device.get('id')}", f"Shared by {remote_device_name(device)} over the network"))
    return choices


def remote_device_name(device: dict[str, Any] | None) -> str:
    return str((device or {}).get("name") or "Unnamed NWR Stream Manager")


def remote_sdr_status_text(remote_sdr: dict[str, Any]) -> str:
    """How the shared RTL-SDR in use is doing, as the page script says it."""
    remote = remote_sdr.get("remote") or {}
    if remote.get("source_mode") != "remote" or not remote.get("device_id"):
        return ""
    status = remote.get("status") or {}
    device = next((item for item in remote_sdr.get("devices") or [] if item.get("id") == remote.get("device_id")), None)
    name = status.get("host_name") or remote_device_name(device)
    if not status.get("reachable") and (status.get("connecting") or not status.get("host_id")):
        return f"Connecting to {name}..."
    if not status.get("reachable"):
        return f"{name} is not reachable. Check that it is running and sharing its RTL-SDR."
    sdr = status.get("sdr") or {}
    reset = sdr.get("reset") or {}
    problem = None if reset.get("state") == "resetting" else sdr.get("problem")
    if problem == "disconnected":
        return f"Connected to {name}, but its RTL-SDR is not connected."
    if problem:
        return f"Connected to {name}, but its RTL-SDR needs attention."
    return f"Connected to {name}. Using {sdr.get('device_name') or 'its RTL-SDR'}."


def rtl_settings_changes(form: dict[str, str], gain_values: list[float]) -> dict[str, Any]:
    """The RTL-SDR settings changed on the form, which sends them all.

    Each is compared with what the page had when it opened (its was_ field), and only
    the ones changed there are saved, as the page script saves only what's changed. So
    settings someone else has since changed are kept.
    """
    def changed(name: str, default: str = "") -> bool:
        return form.get(name, default).strip() != form.get(f"was_{name}", default).strip()

    changes: dict[str, Any] = {}
    if changed("serial"):
        chosen = form.get("serial", "")
        if chosen.startswith(REMOTE_SDR_CHOICE_PREFIX):
            changes.update(source_mode="remote", remote_host_id=chosen[len(REMOTE_SDR_CHOICE_PREFIX):])
        elif form.get("was_serial", "").startswith(REMOTE_SDR_CHOICE_PREFIX):
            changes.update(source_mode="local", serial=chosen)
        else:
            changes["serial"] = chosen
    if changed("gain", "auto"):
        gain = form.get("gain", "auto")
        if gain == "auto":
            changes["gain"] = None
        else:
            try:
                value = float(gain)
            except ValueError:
                value = None
            if value is None or not any(abs(value - float(item)) < 0.05 for item in gain_values):
                raise ValueError("Select a gain from the list.")
            changes["gain"] = value
    for name, low, high, message in (
        ("ppm_correction", -200, 200, "Enter a PPM correction from -200 to 200."),
        ("alias_filter_strength", 0, 100, "Enter an alias filter strength from 0 to 100."),
    ):
        if changed(name):
            try:
                value = int(form.get(name, "").strip())
            except ValueError:
                raise ValueError(message) from None
            if not low <= value <= high:
                raise ValueError(message)
            changes[name] = value
    for name in ("bias_tee", "notify_sdr_failures"):
        if (form.get(name) == "1") != (form.get(f"was_{name}") == "1"):
            changes[name] = form.get(name) == "1"
    if changed("remote_buffer_seconds"):
        changes["remote_buffer_seconds"] = validate_remote_buffer_seconds(form.get("remote_buffer_seconds", ""))
    return changes


def iq_recorder_result(query: dict[str, str], recorder: dict[str, Any]) -> tuple[str, str, str] | None:
    """The message the I/Q recorder shows when it opens, as the page script shows it."""
    done = query.get("result", "")
    status = str(recorder.get("status") or "idle")
    if done in IQ_RECORDER_RESULTS and (done != "started" or recorder.get("active")):
        return "iq-recorder-result", IQ_RECORDER_RESULTS[done], "success"
    if recorder.get("active") or status == "idle":
        return None
    if recorder.get("error"):
        return "iq-recorder-result", form_message(str(recorder["error"])), "error"
    message = "I/Q recording completed." if status == "completed" else "I/Q recording stopped."
    return "iq-recorder-result", message, "error" if status == "needs-attention" else "success"


def iq_recording_payload_from_form(form: dict[str, str]) -> dict[str, Any]:
    try:
        minutes = float(form.get("duration_minutes", "").strip() or 0)
    except ValueError:
        minutes = -1
    if not 0 <= minutes <= 1440:
        raise ValueError("Enter a number of minutes from 0 to 1440.")
    payload: dict[str, Any] = {
        "mode": form.get("iq_recording_mode", IQ_RECORDER_MODE_STREAM),
        "duration_seconds": round(minutes * 60),
    }
    if payload["mode"] == IQ_RECORDER_MODE_SPECTRUM:
        payload["sample_rate"] = form.get("sample_rate", "")
    elif not form.get("stream_id"):
        raise ValueError("Select an active stream.")
    else:
        payload["stream_id"] = form["stream_id"]
    return payload


def iq_recording_row_html(recording: dict[str, Any], read_only: bool) -> str:
    """A row of the recordings table, keyed and labelled as the page script makes them."""
    recorded_at = html.escape(recording.get("recorded_at") or "Unknown")
    recording_id = str(recording.get("id", ""))
    labels = ("Time of recording", "Duration", "Sample rate", "Frequency")
    values = (
        recording.get("recorded_at") or "Unknown",
        recording.get("duration") or "0:00",
        f"{recording.get('sample_rate') or 0} S/s",
        f"{recording.get('frequency_hz') or 0} Hz",
    )
    cells = "".join(
        f'<td role="cell" data-label="{label}">{html.escape(str(value))}</td>' for label, value in zip(labels, values)
    )
    download = urlencode({"view": "iq_recording_download", "recording": recording_id})
    actions = (
        f'<a class="button-link" href="/?{html.escape(download)}">Download recording'
        f'<span class="screen-reader-only"> from {recorded_at}</span></a>'
    )
    if not recording.get("downloading") and not read_only:
        remove = urlencode({"view": "iq_recorder", "confirm": "remove", "recording": recording_id})
        actions += (
            f'<a class="button-link" href="/?{html.escape(remove)}">Remove recording'
            f'<span class="screen-reader-only"> from {recorded_at}</span></a>'
        )
    return (
        f'<tr data-row-key="recording:{html.escape(recording_id)}" role="row">{cells}'
        f'<td class="menu-cell" role="cell" data-label="Actions"><div class="row-actions">{actions}</div></td></tr>'
    )
