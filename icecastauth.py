#!/usr/bin/env python3

from __future__ import annotations

import base64
import http.client
import os
import select
import socket
import ssl
import sys
import termios
import tty
from dataclasses import dataclass
from urllib.parse import urlparse


DEFAULT_SERVER = "http://127.0.0.1"
DEFAULT_PORT = "8000"
DEFAULT_USERNAME = "source"
DEFAULT_PASSWORD = "hackme"
DEFAULT_MOUNTPOINT = ""
PASSWORD_TOGGLE_CHAR = "\x13"  # Ctrl+S
GWES_SUBMISSION_URL = "https://forms.office.com/r/MLx6hKmnCe"
WEATHER_USA_SUBMISSION_URL = "https://www.weatherusa.net/members/services/radio"


@dataclass
class IcecastSettings:
    server: str = DEFAULT_SERVER
    port: str = DEFAULT_PORT
    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    mountpoint: str = DEFAULT_MOUNTPOINT


@dataclass(frozen=True)
class IcecastAuthResult:
    success: bool
    message: str
    status_code: int | None = None


def default_manual_settings() -> IcecastSettings:
    return IcecastSettings()


def default_gwes_settings() -> IcecastSettings:
    return IcecastSettings(
        server="http://ingest.wxr.gwes-cdn.net",
        port="10000",
        username="",
        password="",
        mountpoint="",
    )


def default_weather_usa_settings() -> IcecastSettings:
    return IcecastSettings(
        server="http://radio-master.weatherusa.net",
        port="80",
        username="source",
        password="",
        mountpoint="",
    )


def normalize_server(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith(("http://", "https://")):
        return value
    return f"http://{value}"


def normalize_mountpoint(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value.startswith("/"):
        return value
    return f"/{value}"


def fields_complete(settings: IcecastSettings) -> bool:
    return all(
        (
            settings.server.strip(),
            settings.port.strip(),
            settings.username.strip(),
            settings.password,
            settings.mountpoint.strip(),
        )
    )


def masked_password(password: str) -> str:
    return "*" * len(password)


def parse_keypress() -> str:
    char = os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")
    if char != "\x1b":
        return char

    if not select.select([sys.stdin], [], [], 0.1)[0]:
        return char

    sequence = char + os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")

    if sequence in ("\x1b[", "\x1bO"):
        if select.select([sys.stdin], [], [], 0.1)[0]:
            sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")

    if sequence.startswith("\x1b[") and sequence[-1:].isdigit():
        if select.select([sys.stdin], [], [], 0.1)[0]:
            sequence += os.read(sys.stdin.fileno(), 1).decode("utf-8", errors="ignore")

    normalized_sequences = {
        "\x1bOA": "\x1b[A",
        "\x1bOB": "\x1b[B",
        "\x1bOC": "\x1b[C",
        "\x1bOD": "\x1b[D",
        "\x1bOH": "\x1b[H",
        "\x1bOF": "\x1b[F",
    }
    return normalized_sequences.get(sequence, sequence)


def render_text_prompt(prompt: str, buffer: list[str], cursor: int, visible: bool = True) -> None:
    display = "".join(buffer) if visible else "*" * len(buffer)
    sys.stdout.write("\r")
    sys.stdout.write("\033[2K")
    sys.stdout.write(f"{prompt}{display}")

    cursor_back = len(display) - cursor
    if cursor_back > 0:
        sys.stdout.write(f"\033[{cursor_back}D")
    sys.stdout.flush()


def prompt_text(prompt: str, prefill: str, hidden: bool = False, allow_toggle: bool = False) -> str:
    if not sys.stdin.isatty():
        return input(prompt)

    if allow_toggle:
        sys.stdout.write("\nPress Ctrl+S to show or hide the password.\n")
        sys.stdout.flush()

    buffer = list(prefill)
    cursor = len(buffer)
    visible = not hidden
    file_descriptor = sys.stdin.fileno()
    original_settings = termios.tcgetattr(file_descriptor)

    try:
        tty.setraw(file_descriptor)
        render_text_prompt(prompt, buffer, cursor, visible)

        while True:
            key = parse_keypress()

            if key in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(buffer)

            if allow_toggle and key == PASSWORD_TOGGLE_CHAR:
                visible = not visible
                render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key in ("\x03",):
                raise KeyboardInterrupt

            if key in ("\x7f", "\b"):
                if cursor > 0:
                    del buffer[cursor - 1]
                    cursor -= 1
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key == "\x1b[D":
                if cursor > 0:
                    cursor -= 1
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key == "\x1b[C":
                if cursor < len(buffer):
                    cursor += 1
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key == "\x1b[H":
                cursor = 0
                render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key == "\x1b[F":
                cursor = len(buffer)
                render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key == "\x1b[3~":
                if cursor < len(buffer):
                    del buffer[cursor]
                    render_text_prompt(prompt, buffer, cursor, visible)
                continue

            if key.isprintable():
                buffer.insert(cursor, key)
                cursor += 1
                render_text_prompt(prompt, buffer, cursor, visible)
    finally:
        termios.tcsetattr(file_descriptor, termios.TCSADRAIN, original_settings)


def prompt_server(settings: IcecastSettings) -> None:
    value = prompt_text("Server: ", settings.server).strip()
    normalized = normalize_server(value)
    if normalized:
        settings.server = normalized


def prompt_port(settings: IcecastSettings) -> None:
    while True:
        value = prompt_text("Port: ", settings.port).strip()
        if not value:
            settings.port = ""
            return
        if not value.isdigit():
            print("Enter the port as a whole number.")
            continue
        port = int(value)
        if not 1 <= port <= 65535:
            print("Port must be between 1 and 65535.")
            continue
        settings.port = str(port)
        return


def prompt_username(settings: IcecastSettings) -> None:
    settings.username = prompt_text("Username: ", settings.username).strip()


def prompt_password_field(settings: IcecastSettings) -> None:
    settings.password = prompt_text("Password: ", settings.password, hidden=True, allow_toggle=True)


def prompt_mountpoint(settings: IcecastSettings) -> None:
    value = prompt_text("Mountpoint: ", settings.mountpoint).strip()
    settings.mountpoint = normalize_mountpoint(value)


def build_menu_options(settings: IcecastSettings) -> list[str]:
    options = [
        f"Server: {settings.server or '(blank)'}",
        f"Port: {settings.port or '(blank)'}",
        f"Username: {settings.username or '(blank)'}",
        f"Password: {masked_password(settings.password) if settings.password else '(blank)'}",
        f"Mountpoint: {settings.mountpoint or '(blank)'}",
    ]
    if fields_complete(settings):
        options.append("Confirm")
    return options


def show_submission_notice(url: str) -> None:
    print()
    print(f"You must first submit a stream by visiting the following URL: {url}")
    print("Press Enter to enter credentials.")
    while True:
        key = input()
        if key == "":
            return


def parse_connection_target(settings: IcecastSettings) -> tuple[str, int, str, bool]:
    parsed = urlparse(settings.server)
    host = parsed.hostname
    if not host:
        raise ValueError("Server is invalid.")

    port = int(settings.port)
    scheme = parsed.scheme or "http"
    use_https = scheme == "https"
    path_prefix = parsed.path.rstrip("/")
    target = f"{path_prefix}{settings.mountpoint}" if path_prefix else settings.mountpoint
    return host, port, target, use_https


def authenticate_mountpoint(settings: IcecastSettings) -> tuple[bool, str]:
    result = test_mountpoint_authentication(settings)
    return result.success, result.message


def test_mountpoint_authentication(settings: IcecastSettings) -> IcecastAuthResult:
    try:
        host, port, target, use_https = parse_connection_target(settings)
    except ValueError as error:
        return IcecastAuthResult(False, str(error))

    auth_bytes = f"{settings.username}:{settings.password}".encode("utf-8")
    auth_header = base64.b64encode(auth_bytes).decode("ascii")
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Content-Length": "0",
        "Content-Type": "audio/mpeg",
        "User-Agent": "nwr_stream_builder/icecastauth",
    }

    connection_class = http.client.HTTPSConnection if use_https else http.client.HTTPConnection
    kwargs = {"timeout": 10}
    if use_https:
        kwargs["context"] = ssl.create_default_context()

    connection = None
    response = None
    try:
        connection = connection_class(host, port, **kwargs)
        connection.request("SOURCE", target, body=b"", headers=headers)
        response = connection.getresponse()
        status = response.status
    except TimeoutError:
        return IcecastAuthResult(False, "The Icecast server took too long to respond.")
    except socket.gaierror as error:
        if error.errno in {socket.EAI_NONAME, socket.EAI_FAIL, socket.EAI_AGAIN}:
            return IcecastAuthResult(False, "The Icecast server or mountpoint does not exist.")
        return IcecastAuthResult(False, "Icecast authentication failed.")
    except OSError as error:
        return IcecastAuthResult(False, f"Connection failed: {error}")
    except http.client.HTTPException as error:
        return IcecastAuthResult(False, f"Connection failed: {error}")
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
        try:
            connection.close()
        except Exception:
            pass

    return icecast_auth_result_for_status(status)


def icecast_auth_result_for_status(status: int) -> IcecastAuthResult:
    if status == 200:
        return IcecastAuthResult(True, "Icecast authentication successful.", status)
    if status == 401:
        return IcecastAuthResult(False, "Invalid Icecast username or password.", status)
    if status == 403:
        return IcecastAuthResult(False, "The Icecast mountpoint is already occupied.", status)
    if status == 404:
        return IcecastAuthResult(False, "The Icecast server or mountpoint does not exist.", status)
    if status == 504:
        return IcecastAuthResult(False, "The Icecast server took too long to respond.", status)
    if 200 <= status < 300:
        return IcecastAuthResult(True, "Icecast authentication successful.", status)
    return IcecastAuthResult(False, "Icecast authentication failed.", status)


def credentials_menu(settings: IcecastSettings) -> int | None:
    while True:
        print()
        print("Icecast Authentication")
        print("0. Back")
        options = build_menu_options(settings)
        for index, option in enumerate(options, start=1):
            print(f"{index}. {option}")

        selection = input("Select an option: ").strip()
        confirm_index = 6 if fields_complete(settings) else None

        if not selection:
            if confirm_index is None:
                print("One or more fields are left blank.")
                continue
            success, message = authenticate_mountpoint(settings)
            print()
            print(message)
            if success:
                return 0
            continue

        if not selection.isdigit():
            print("Enter the number for the option you want.")
            continue

        selected_index = int(selection)
        if selected_index == 0:
            return None
        if selected_index == 1:
            prompt_server(settings)
        elif selected_index == 2:
            prompt_port(settings)
        elif selected_index == 3:
            prompt_username(settings)
        elif selected_index == 4:
            prompt_password_field(settings)
        elif selected_index == 5:
            prompt_mountpoint(settings)
        elif selected_index == 6 and confirm_index == 6:
            success, message = authenticate_mountpoint(settings)
            print()
            print(message)
            if success:
                return 0
        else:
            print("That selection is not available.")


def menu_loop() -> int:
    while True:
        print()
        print("Streaming Platform")
        print("1. GWES Weather Radio")
        print("2. Weather USA")
        print("3. Enter Credentials Manually")

        selection = input("Select an option: ").strip()
        if not selection.isdigit():
            print("Enter the number for the option you want.")
            continue

        selected_index = int(selection)
        if selected_index == 1:
            show_submission_notice(GWES_SUBMISSION_URL)
            result = credentials_menu(default_gwes_settings())
            if result == 0:
                return 0
        elif selected_index == 2:
            show_submission_notice(WEATHER_USA_SUBMISSION_URL)
            result = credentials_menu(default_weather_usa_settings())
            if result == 0:
                return 0
        elif selected_index == 3:
            result = credentials_menu(default_manual_settings())
            if result == 0:
                return 0
        else:
            print("That selection is not available.")


if __name__ == "__main__":
    try:
        raise SystemExit(menu_loop())
    except KeyboardInterrupt:
        print()
        raise SystemExit(130)
