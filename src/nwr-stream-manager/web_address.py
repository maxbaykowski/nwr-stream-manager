"""Where the web interface listens: the saved port and address, and the command line flags that override them."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .state_files import atomic_write_json


LOG = logging.getLogger(__name__)

WEB_ADDRESS_STATE_FILE_NAME = "web-address.json"
DEFAULT_WEB_PORT = 8080
ALL_INTERFACES_HOST = "0.0.0.0"
LOCALHOST_HOST = "127.0.0.1"


@dataclass(frozen=True)
class WebAddressSettings:
    """The Advanced settings. They take effect the next time NWR Stream Manager starts."""

    port: int = DEFAULT_WEB_PORT
    localhost_only: bool = False

    @property
    def host(self) -> str:
        return LOCALHOST_HOST if self.localhost_only else ALL_INTERFACES_HOST


@dataclass(frozen=True)
class WebAddress:
    """Where the web interface listens, and which command line flags chose it instead of the settings."""

    host: str
    port: int
    # The flag as it was given, such as "--port 8080", or "" when the setting is used.
    host_flag: str = ""
    port_flag: str = ""


def web_address_state_path(state_path: Path) -> Path:
    return state_path.with_name(WEB_ADDRESS_STATE_FILE_NAME)


def validate_web_port(value: Any) -> int:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Enter a port number from 1 to 65535.") from exc
    if not number.is_integer() or not 1 <= number <= 65535:
        raise ValueError("Enter a port number from 1 to 65535.")
    return int(number)


def validate_web_address_payload(raw: Any, current: WebAddressSettings | None = None) -> WebAddressSettings:
    """The settings from saved or sent values; anything left out keeps its current value."""
    if not isinstance(raw, dict):
        raise ValueError("advanced settings are required")
    current = current or WebAddressSettings()
    port = validate_web_port(raw["port"]) if "port" in raw else current.port
    localhost_only = raw["localhost_only"] if "localhost_only" in raw else current.localhost_only
    if not isinstance(localhost_only, bool):
        raise ValueError("localhost_only must be true or false")
    return WebAddressSettings(port=port, localhost_only=localhost_only)


def load_web_address_settings(path: Path) -> WebAddressSettings:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return WebAddressSettings()
    except Exception as exc:
        LOG.warning("failed to load the advanced settings from %s: %s", path, exc)
        return WebAddressSettings()
    try:
        return validate_web_address_payload(raw)
    except Exception as exc:
        LOG.warning("the advanced settings in %s are invalid: %s", path, exc)
        return WebAddressSettings()


def save_web_address_settings(path: Path, settings: WebAddressSettings) -> None:
    atomic_write_json(path, asdict(settings))


def resolve_web_address(
    settings: WebAddressSettings,
    *,
    host: str | None = None,
    port: int | None = None,
    localhost_only: bool = False,
    bind_to_all_interfaces: bool = False,
) -> WebAddress:
    """Where to listen: the command line's choice where it made one, otherwise the saved settings."""
    if host is not None:
        chosen_host, host_flag = host, f"--host {host}"
    elif localhost_only:
        chosen_host, host_flag = LOCALHOST_HOST, "--localhost-only"
    elif bind_to_all_interfaces:
        chosen_host, host_flag = ALL_INTERFACES_HOST, "--bind-to-all-interfaces"
    else:
        chosen_host, host_flag = settings.host, ""
    if port is not None:
        chosen_port, port_flag = port, f"--port {port}"
    else:
        chosen_port, port_flag = settings.port, ""
    return WebAddress(chosen_host, chosen_port, host_flag, port_flag)


def describe_web_host(host: str) -> str:
    if host in {ALL_INTERFACES_HOST, "::", ""}:
        return "every network address of this computer"
    if host in {LOCALHOST_HOST, "localhost", "::1"} or host.startswith("127."):
        return "this computer only"
    return host


def advanced_listening_text(payload: dict[str, Any]) -> str:
    """Where the web interface listens now, as Settings says it; the page script says the same."""
    listening = payload.get("listening") or {}
    host, port = str(listening.get("host") or ""), listening.get("port")
    where = describe_web_host(host)
    if where == "this computer only":
        text = f"Right now, NWR Stream Manager only accepts connections from this computer, on port {port}."
    elif where == host:
        text = f"Right now, NWR Stream Manager is listening on {host}, port {port}."
    else:
        text = f"Right now, NWR Stream Manager accepts connections from other devices on port {port}."
    if payload.get("restart_needed"):
        text += " Your saved changes take effect the next time it starts."
    return text


def advanced_saved_text(payload: dict[str, Any]) -> str:
    flags = [flag for flag in (payload.get("host_flag"), payload.get("port_flag")) if flag]
    if flags:
        return f"Changes saved. They take effect once NWR Stream Manager is started without {' or '.join(flags)}."
    return "Changes saved. They take effect the next time NWR Stream Manager starts."


def advanced_flag_text(flag: str) -> str:
    return f"NWR Stream Manager was started with {flag}, which is used instead of this setting until it's started without it." if flag else ""
