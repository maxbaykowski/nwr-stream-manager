"""Notifications through ntfy: their settings, what they say, and sending them."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

from .constants import READ_ONLY_RESTRICTED_NOTIFICATION_VIEWS
from .eas_alerts import stream_callsign
from .models import (
    normalize_notification_access_url,
    normalize_notification_click_url,
    WebNotificationSettings,
    WebStreamNotificationSettings,
)
from .stream_config import icecast_mountpoint_url


LOG = logging.getLogger(__name__)


def load_notification_settings(path: Path) -> WebNotificationSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WebNotificationSettings()
    except Exception as exc:
        LOG.warning("failed to load notification settings from %s: %s", path, exc)
        return WebNotificationSettings()
    if not isinstance(raw, dict):
        return WebNotificationSettings()
    try:
        return validate_notification_settings_payload(raw)
    except Exception as exc:
        LOG.warning("notification settings in %s are invalid: %s", path, exc)
        return WebNotificationSettings()


def save_notification_settings(path: Path, settings: WebNotificationSettings) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def stream_notification_settings_from_stream(stream: dict[str, Any]) -> WebStreamNotificationSettings:
    raw = stream.get("notifications")
    if not isinstance(raw, dict):
        return WebStreamNotificationSettings()
    return WebStreamNotificationSettings(
        icecast_failures=bool(raw.get("icecast_failures", False)),
        soundcard_failures=bool(raw.get("soundcard_failures", False)),
        recorded_eas_alerts=bool(raw.get("recorded_eas_alerts", False)),
        bad_reception=bool(raw.get("bad_reception", False)),
    )


def validate_stream_notification_payload(raw: Any) -> WebStreamNotificationSettings:
    if not isinstance(raw, dict):
        raise ValueError("stream notification settings are required")
    return WebStreamNotificationSettings(
        icecast_failures=bool(raw.get("icecast_failures", False)),
        soundcard_failures=bool(raw.get("soundcard_failures", False)),
        recorded_eas_alerts=bool(raw.get("recorded_eas_alerts", False)),
        bad_reception=bool(raw.get("bad_reception", False)),
    )


def validate_notification_settings_payload(
    raw: Any,
    *,
    current: WebNotificationSettings | None = None,
) -> WebNotificationSettings:
    if not isinstance(raw, dict):
        raise ValueError("notification settings are required")
    enabled = bool(raw.get("enabled", False))
    server_url = str(raw.get("server_url", "https://ntfy.sh")).strip().rstrip("/")
    if not server_url:
        server_url = "https://ntfy.sh"
    parsed = urlparse(server_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("ntfy server URL must start with http:// or https://")
    if parsed.query or parsed.fragment:
        raise ValueError("ntfy server URL cannot include a query string or fragment")
    topic = str(raw.get("topic", "")).strip()
    if topic and ("/" in topic or "\\" in topic or any(ord(char) < 33 for char in topic)):
        raise ValueError("ntfy topic must be a single topic name without spaces or slashes")
    if len(topic) > 128:
        raise ValueError("ntfy topic must be 128 characters or fewer")
    try:
        priority = int(raw.get("priority", 3))
    except (TypeError, ValueError) as exc:
        raise ValueError("ntfy priority must be a number from 1 through 5") from exc
    if not 1 <= priority <= 5:
        raise ValueError("ntfy priority must be from 1 through 5")
    access_url_mode = str(raw.get("access_url_mode", "auto")).strip() or "auto"
    if any(ord(char) < 32 for char in access_url_mode) or len(access_url_mode) > 160:
        raise ValueError("notification access URL selection is invalid")
    custom_access_url = str(raw.get("custom_access_url", "")).strip().rstrip("/")
    if access_url_mode == "custom":
        custom_access_url = normalize_notification_access_url(custom_access_url)
    elif custom_access_url:
        try:
            custom_access_url = normalize_notification_access_url(custom_access_url)
        except ValueError:
            custom_access_url = ""
    access_token = str(raw.get("access_token", "")).strip()
    username = str(raw.get("username", "")).strip()
    password = str(raw.get("password", ""))
    if current is not None:
        if bool(raw.get("keep_access_token", False)) and not access_token:
            access_token = current.access_token
        if bool(raw.get("keep_password", False)) and not password:
            password = current.password
    if access_token and (username or password):
        raise ValueError("Use either an access token or username/password, not both")
    if password and not username:
        raise ValueError("A username is required when a password is set")
    if enabled and not topic:
        raise ValueError("Enter an ntfy topic before enabling notifications")
    return WebNotificationSettings(
        enabled=enabled,
        server_url=server_url,
        topic=topic,
        priority=priority,
        access_url_mode=access_url_mode,
        custom_access_url=custom_access_url,
        access_token=access_token,
        username=username,
        password=password,
    )


def ntfy_header_text(value: str) -> str:
    """Text for an ntfy header. HTTP headers are sent as Latin-1, so anything that isn't plain
    ASCII, such as an accented or emoji instance name, goes as an RFC 2047 encoded word, which
    ntfy decodes."""
    if value.isascii():
        return value
    return f"=?UTF-8?B?{base64.b64encode(value.encode('utf-8')).decode('ascii')}?="


def send_ntfy_notification(
    settings: WebNotificationSettings,
    *,
    title: str,
    message: str,
    priority: int | None = None,
    click_url: str = "",
) -> None:
    if not settings.topic:
        raise ValueError("Enter an ntfy topic before sending a notification")
    url = f"{settings.server_url}/{quote(settings.topic, safe='')}"
    headers = {
        "Title": ntfy_header_text(str(title)[:120]),
        "Priority": str(max(1, min(5, int(priority if priority is not None else settings.priority)))),
        "Content-Type": "text/plain; charset=utf-8",
    }
    click_url = normalize_notification_click_url(click_url, default_scheme="http") if click_url else ""
    if click_url:
        headers["Click"] = click_url
    if settings.access_token:
        headers["Authorization"] = f"Bearer {settings.access_token}"
    elif settings.username or settings.password:
        token = base64.b64encode(f"{settings.username}:{settings.password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = Request(
        url,
        data=str(message).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise ValueError(f"ntfy returned HTTP {response.status}")
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError("ntfy rejected the credentials or topic permissions") from exc
        if exc.code == 404:
            raise ValueError("ntfy server or topic endpoint was not found") from exc
        if exc.code == 429:
            raise ValueError("ntfy rate limited the notification request") from exc
        raise ValueError(f"ntfy returned HTTP {exc.code}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ValueError(f"Could not reach ntfy server: {reason}") from exc
    except TimeoutError as exc:
        raise ValueError("ntfy server took too long to respond") from exc


def sanitize_local_notification_path(raw: Any) -> str:
    value = str(raw or "").strip() or "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/"):
        return "/"
    return value


def notification_next_path(raw_query: str) -> str:
    query = parse_qs(raw_query)
    return sanitize_local_notification_path(query.get("next", ["/"])[0])


def notification_click_url(base_url: str, target_path: str = "/") -> str:
    base = normalize_notification_access_url(base_url, default_scheme="http").rstrip("/")
    target = sanitize_local_notification_path(target_path)
    return f"{base}/notification?next={quote(target, safe='')}"


def notification_app_url(base_url: str, target_path: str = "/") -> str:
    base = normalize_notification_access_url(base_url, default_scheme="http").rstrip("/")
    target = sanitize_local_notification_path(target_path)
    return f"{base}{target}"


def stream_settings_route_path(stream_id: str) -> str:
    return f"/?view=stream_settings&stream={quote(str(stream_id or ''), safe='')}"


def stream_output_route_path(stream_id: str, output_id: str) -> str:
    return (
        "/?view=stream_output"
        f"&stream={quote(str(stream_id or ''), safe='')}"
        f"&output={quote(str(output_id or ''), safe='')}"
    )


def eas_alert_detail_route_path(stream_id: str, alert_id: str) -> str:
    return (
        "/?view=eas_alert_detail"
        f"&stream={quote(str(stream_id or ''), safe='')}"
        f"&alert={quote(str(alert_id or ''), safe='')}"
    )


def notification_target_restricted_for_read_only(target_path: str) -> bool:
    target = sanitize_local_notification_path(target_path)
    parsed = urlparse(target)
    if parsed.path != "/":
        return False
    view = parse_qs(parsed.query).get("view", ["dashboard"])[0]
    return view in READ_ONLY_RESTRICTED_NOTIFICATION_VIEWS


def rtl_problem_messages(category: str, device_name: str) -> tuple[str, str]:
    """Failure and recovery notification text for an SDR problem."""
    device = sentence_case(device_name)
    if category == "permission":
        return f"Insufficient permissions to access {device_name}.", f"{device} can now be accessed."
    if category == "claim":
        return f"Failed to claim {device_name}.", f"Successfully claimed {device_name}."
    if category in {"error", "no-data"}:
        return f"{device} has stopped outputting data.", f"{device} is outputting data again."
    if category == "unreachable":
        return f"{device} is not reachable.", f"{device} is reachable again."
    return f"{device} is not connected.", f"{device} is now connected."


def sentence_case(text: str) -> str:
    """Capitalize the first letter, for names that can start a sentence."""
    return text[:1].upper() + text[1:]


def soundcard_notification_name(output: dict[str, Any]) -> str:
    soundcard = output.get("soundcard") if isinstance(output.get("soundcard"), dict) else {}
    for key in ("display_name", "card_long_name", "card_name", "device"):
        value = str(soundcard.get(key, "")).strip()
        if value:
            return value
    return "the configured sound card"


def stream_notification_failures(
    stream: dict[str, Any],
    snapshot: dict[str, Any],
    settings: WebStreamNotificationSettings,
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    stream_id = str(stream.get("id", ""))
    callsign = stream_callsign(stream, snapshot)
    for output in snapshot.get("outputs", []):
        if not isinstance(output, dict):
            continue
        status = str(output.get("status", snapshot.get("status", ""))).strip().lower()
        if status != "needs-attention":
            continue
        output_type = str(output.get("type", "icecast") or "icecast")
        output_id = str(output.get("id", ""))
        if output_type == "soundcard":
            if not settings.soundcard_failures:
                continue
            name = soundcard_notification_name(output)
            failures.append(
                {
                    "key": f"{stream_id}:soundcard:{output_id}",
                    "category": "soundcard",
                    "target_path": stream_output_route_path(stream_id, output_id),
                    "message": f"{callsign}: sound card {name} is not connected.",
                    "recovery_message": f"{callsign}: sound card {name} is now connected.",
                }
            )
        else:
            if not settings.icecast_failures:
                continue
            icecast = output.get("icecast") if isinstance(output.get("icecast"), dict) else {}
            destination = icecast_mountpoint_url(icecast)
            failures.append(
                {
                    "key": f"{stream_id}:icecast:{output_id}",
                    "category": "icecast",
                    "target_path": stream_output_route_path(stream_id, output_id),
                    "message": f"{callsign}: failed to connect to the Icecast mountpoint at {destination}.",
                    "recovery_message": f"{callsign}: successfully connected to the Icecast mountpoint at {destination}.",
                }
            )
    return failures


def stream_notification_healthy_keys(
    stream: dict[str, Any],
    snapshot: dict[str, Any],
    settings: WebStreamNotificationSettings,
) -> set[str]:
    """Keys of watched outputs currently seen working.

    A recovery notification needs this positive observation: an output that
    was removed, disabled, or had its notification toggle turned off stops
    reporting a failure too, and must not be announced as reconnected.
    """
    healthy: set[str] = set()
    stream_id = str(stream.get("id", ""))
    for output in snapshot.get("outputs", []):
        if not isinstance(output, dict):
            continue
        status = str(output.get("status", snapshot.get("status", ""))).strip().lower()
        if status != "enabled":
            continue
        output_type = str(output.get("type", "icecast") or "icecast")
        output_id = str(output.get("id", ""))
        if output_type == "soundcard":
            if settings.soundcard_failures:
                healthy.add(f"{stream_id}:soundcard:{output_id}")
        elif settings.icecast_failures:
            healthy.add(f"{stream_id}:icecast:{output_id}")
    return healthy


def stream_reception_healthy(
    stream: dict[str, Any],
    signal: dict[str, Any] | None,
    settings: WebStreamNotificationSettings,
) -> bool:
    return (
        settings.bad_reception
        and isinstance(signal, dict)
        and bool(signal.get("available"))
        and signal.get("reception_problem") is None
    )


def stream_reception_failure(
    stream: dict[str, Any],
    signal: dict[str, Any] | None,
    settings: WebStreamNotificationSettings,
) -> dict[str, Any] | None:
    problem = signal.get("reception_problem") if isinstance(signal, dict) else None
    if not settings.bad_reception or problem not in {"bad_reception", "no_signal"}:
        return None
    stream_id = str(stream.get("id", ""))
    snr_db = signal.get("snr_db")
    if problem == "no_signal" or not isinstance(snr_db, (int, float)):
        message = f"{stream_callsign(stream)}: no signal, only static is being received."
    else:
        message = f"{stream_callsign(stream)}: bad reception, the signal-to-noise ratio is {float(snr_db):.0f} dB."
    # One key for both problems so a signal wavering between them does not
    # send a fresh notification each time it crosses 0 dB.
    return {
        "key": f"{stream_id}:reception",
        "category": "reception",
        "target_path": stream_settings_route_path(stream_id),
        "message": message,
        "recovery_message": f"{stream_callsign(stream)}: reception has improved.",
    }
