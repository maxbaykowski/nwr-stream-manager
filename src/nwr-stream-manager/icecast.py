from __future__ import annotations

import base64
import logging
import socket
import ssl
from dataclasses import dataclass
from typing import BinaryIO
from urllib.parse import urlsplit

from . import __version__
from .config import IcecastConfig


LOG = logging.getLogger(__name__)


class IcecastError(RuntimeError):
    """Raised when Icecast rejects the source connection."""


@dataclass(frozen=True)
class IcecastAddress:
    host: str
    port: int
    mount: str
    tls: bool


def parse_icecast_address(server: str, port: int | str, mount: str = "") -> IcecastAddress:
    """Turn what someone typed for an Icecast server into a host, port, mount and TLS flag.

    Accepts a bare hostname or IP address, host:port, or a full http:// or https:// web
    address, optionally with the mountpoint on the end. https:// means an encrypted
    connection. A port in the address takes the place of the separate port setting.
    """
    text = str(server or "").strip()
    if not text:
        raise ValueError("Icecast host is required")
    if "://" in text:
        scheme = text.split("://", 1)[0].lower()
        if scheme not in {"http", "https"}:
            raise ValueError("The Icecast server address must start with http:// or https://, or be just the host name")
        parts = urlsplit(text)
    else:
        scheme = "http"
        parts = urlsplit(f"//{text}")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Enter the Icecast username and password in their own fields, not in the server address")
    if parts.query or parts.fragment:
        raise ValueError("The Icecast server address cannot contain ? or #")
    host = parts.hostname or ""
    if not host or any(character.isspace() for character in host):
        raise ValueError("The Icecast server address is not valid")
    try:
        address_port = parts.port
    except ValueError as exc:
        raise ValueError("The port in the Icecast server address is not valid") from exc
    try:
        port_number = int(address_port if address_port is not None else port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Icecast port must be a number") from exc
    if not 1 <= port_number <= 65535:
        raise ValueError("Icecast port must be from 1 through 65535")
    mount = str(mount or "").strip()
    if mount and not mount.startswith("/"):
        mount = f"/{mount}"
    address_mount = parts.path.rstrip("/")
    if address_mount:
        if not mount:
            mount = address_mount
        elif mount.rstrip("/") != address_mount:
            raise ValueError(
                f"The Icecast server address ends in {address_mount}, which is different from the mountpoint "
                f"{mount}. Enter just the server in the Host field."
            )
    return IcecastAddress(host=host, port=port_number, mount=mount, tls=scheme == "https")


@dataclass
class IcecastSource:
    config: IcecastConfig
    content_type: str
    timeout_seconds: float = 10.0
    _socket: socket.socket | None = None
    _file: BinaryIO | None = None
    response_status_code: int | None = None

    def connect(self) -> BinaryIO:
        raw_sock = socket.create_connection(
            (self.config.host, self.config.port), timeout=self.timeout_seconds
        )
        try:
            sock: socket.socket
            if self.config.tls:
                context = ssl.create_default_context()
                sock = context.wrap_socket(raw_sock, server_hostname=self.config.host)
            else:
                sock = raw_sock
            sock.sendall(self._request_headers())
            response = self._read_response(sock)
            status_code = self._response_status_code(response)
            LOG.debug(
                "Icecast response from %s:%s%s: %s",
                self.config.host,
                self.config.port,
                self.config.mount,
                response,
            )
            if status_code != 200:
                raise IcecastError(f"Icecast source connection rejected: {response}")
            self.response_status_code = status_code
            LOG.info(
                "Icecast accepted source connection at %s:%s%s with HTTP %s",
                self.config.host,
                self.config.port,
                self.config.mount,
                status_code,
            )
            self._socket = sock
            self._file = sock.makefile("wb", buffering=0)
            return self._file
        except Exception:
            raw_sock.close()
            raise

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass

    def _request_headers(self) -> bytes:
        auth = base64.b64encode(
            f"{self.config.username}:{self.config.password}".encode("utf-8")
        ).decode("ascii")
        # SOURCE is the method every Icecast 2 server accepts. PUT only arrived in
        # Icecast 2.4, and servers before it (Broadcastify's, for one) just close the
        # connection on it without answering.
        lines = [
            f"SOURCE {self.config.mount} HTTP/1.0",
            f"Host: {self.config.host}:{self.config.port}",
            f"Authorization: Basic {auth}",
            f"Content-Type: {self.content_type}",
            f"User-Agent: nwr-stream-manager/{__version__}",
            f"Ice-Public: {1 if self.config.public else 0}",
        ]
        if self.config.name:
            lines.append(f"Ice-Name: {self.config.name}")
        if self.config.genre:
            lines.append(f"Ice-Genre: {self.config.genre}")
        if self.config.description:
            lines.append(f"Ice-Description: {self.config.description}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

    @staticmethod
    def _read_response(sock: socket.socket) -> str:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(1)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 8192:
                raise IcecastError("Icecast response headers exceeded 8192 bytes")
        first_line = bytes(data).splitlines()[0:1]
        if not first_line:
            raise IcecastError("Icecast closed the connection without a response")
        return first_line[0].decode("iso-8859-1", errors="replace")

    @staticmethod
    def _response_status_code(response: str) -> int:
        parts = response.split(maxsplit=2)
        if len(parts) < 2:
            raise IcecastError(f"invalid Icecast response: {response}")
        try:
            return int(parts[1])
        except ValueError as exc:
            raise IcecastError(f"invalid Icecast response: {response}") from exc
