"""This machine's network addresses, Tailscale, and the addresses notifications can open."""

from __future__ import annotations

import fcntl
import json
import secrets
import socket
import struct
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


VIRTUAL_INTERFACE_PREFIXES = (
    "br-",
    "cali",
    "cni",
    "docker",
    "flannel",
    "kube",
    "lxc",
    "podman",
    "veth",
    "virbr",
)
VIRTUAL_INTERFACE_NAMES = {"lo", "tunl0"}
IFF_UP = 0x1
TAILSCALE_DNS_RESOLVER = "100.100.100.100"
TAILSCALE_SERVE_STATUS_TIMEOUT_SECONDS = 3.0
# The notification address list is rebuilt on every status refresh; don't run the
# tailscale command that often.
TAILSCALE_SERVE_CACHE_SECONDS = 30.0
LOCAL_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1"}
_tailscale_serve_cache: dict[int, tuple[float, dict[str, Any] | None]] = {}
_tailscale_serve_cache_lock = threading.Lock()
DNS_TYPE_PTR = 12
DNS_CLASS_IN = 1


def interface_ipv4_address(name: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        request = struct.pack("256s", name[:15].encode("utf-8"))
        result = fcntl.ioctl(sock.fileno(), 0x8915, request)
        return socket.inet_ntoa(result[20:24])
    except OSError:
        return ""
    finally:
        sock.close()


def interface_is_up(name: str) -> bool:
    interface_path = Path("/sys/class/net") / name
    try:
        if (interface_path / "operstate").read_text(encoding="utf-8").strip() == "up":
            return True
    except OSError:
        pass
    try:
        flags = int((interface_path / "flags").read_text(encoding="utf-8").strip(), 0)
        return bool(flags & IFF_UP)
    except (OSError, ValueError):
        return False


def interface_is_physical_or_tailscale(name: str) -> bool:
    lowered = name.lower()
    if lowered.startswith("tailscale"):
        return True
    if lowered in VIRTUAL_INTERFACE_NAMES:
        return False
    if lowered.startswith(VIRTUAL_INTERFACE_PREFIXES):
        return False
    if (Path("/sys/class/net") / name / "device").exists():
        return True
    return lowered.startswith(("en", "eth", "wl", "wlan", "ww", "usb"))


def dns_encode_name(name: str) -> bytes:
    encoded = bytearray()
    labels = name.rstrip(".").split(".")
    for label in labels:
        if not label or len(label) > 63:
            raise ValueError("invalid DNS label")
        encoded.append(len(label))
        encoded.extend(label.encode("ascii"))
    encoded.append(0)
    return bytes(encoded)


def dns_decode_name(message: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    jumped = False
    next_offset = offset
    seen_offsets: set[int] = set()
    while True:
        if offset >= len(message):
            raise ValueError("truncated DNS name")
        length = message[offset]
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(message):
                raise ValueError("truncated DNS pointer")
            pointer = ((length & 0x3F) << 8) | message[offset + 1]
            if pointer in seen_offsets:
                raise ValueError("DNS pointer loop")
            seen_offsets.add(pointer)
            if not jumped:
                next_offset = offset + 2
                jumped = True
            offset = pointer
            continue
        if length & 0xC0:
            raise ValueError("unsupported DNS name label")
        offset += 1
        if length == 0:
            if not jumped:
                next_offset = offset
            return ".".join(labels), next_offset
        if offset + length > len(message):
            raise ValueError("truncated DNS label")
        labels.append(message[offset : offset + length].decode("ascii", errors="strict"))
        offset += length


def reverse_ipv4_arpa(address: str) -> str:
    packed = socket.inet_aton(address)
    return ".".join(str(part) for part in reversed(packed)) + ".in-addr.arpa"


def dns_ptr_lookup(server: str, address: str, *, timeout: float = 0.25) -> list[str]:
    query_id = secrets.randbits(16)
    qname = dns_encode_name(reverse_ipv4_arpa(address))
    query = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", DNS_TYPE_PTR, DNS_CLASS_IN)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.sendto(query, (server, 53))
        response, _addr = sock.recvfrom(2048)
    if len(response) < 12:
        raise ValueError("truncated DNS response")
    rid, flags, qdcount, ancount, _nscount, _arcount = struct.unpack("!HHHHHH", response[:12])
    if rid != query_id:
        raise ValueError("mismatched DNS response")
    if flags & 0x000F:
        return []
    offset = 12
    for _idx in range(qdcount):
        _name, offset = dns_decode_name(response, offset)
        if offset + 4 > len(response):
            raise ValueError("truncated DNS question")
        offset += 4
    results: list[str] = []
    for _idx in range(ancount):
        _name, offset = dns_decode_name(response, offset)
        if offset + 10 > len(response):
            raise ValueError("truncated DNS answer")
        answer_type, answer_class, _ttl, rdlength = struct.unpack("!HHIH", response[offset : offset + 10])
        offset += 10
        rdata_offset = offset
        offset += rdlength
        if offset > len(response):
            raise ValueError("truncated DNS rdata")
        if answer_type == DNS_TYPE_PTR and answer_class == DNS_CLASS_IN:
            target, _unused = dns_decode_name(response, rdata_offset)
            if target:
                results.append(target.rstrip("."))
    return results


def tailscale_magicdns_name(address: str) -> str:
    try:
        names = dns_ptr_lookup(TAILSCALE_DNS_RESOLVER, address)
    except (OSError, ValueError, UnicodeError):
        return ""
    for name in names:
        normalized = name.strip().rstrip(".")
        if not normalized:
            continue
        lowered = normalized.lower()
        if lowered.endswith(".ts.net") or ".ts.net." in lowered:
            return normalized
    return ""


def tailscale_serve_address(serve_status: dict[str, Any], port: int) -> dict[str, Any] | None:
    """Find the https address Tailscale Serve proxies to this NWR Stream Manager, if any.

    serve_status is the output of `tailscale serve status --json`.
    """
    web = serve_status.get("Web") if isinstance(serve_status, dict) else None
    if not isinstance(web, dict):
        return None
    funnel = serve_status.get("AllowFunnel") if isinstance(serve_status.get("AllowFunnel"), dict) else {}
    for host_port, config in web.items():
        handlers = config.get("Handlers") if isinstance(config, dict) else None
        root = handlers.get("/") if isinstance(handlers, dict) else None
        proxy = str(root.get("Proxy", "")).strip() if isinstance(root, dict) else ""
        if not proxy:
            continue
        target = urlparse(proxy if "://" in proxy else f"http://{proxy}")
        try:
            target_port = target.port
        except ValueError:
            continue
        if proxy.isdigit():
            target_host, target_port = "127.0.0.1", int(proxy)
        else:
            target_host = target.hostname or ""
        if target.scheme != "http" or target_host not in LOCAL_PROXY_HOSTS or target_port != port:
            continue
        host, _separator, serve_port = str(host_port).rpartition(":")
        if not host or not serve_port.isdigit():
            continue
        url = f"https://{host}" if serve_port == "443" else f"https://{host}:{serve_port}"
        return {"url": url, "host": host, "public": bool(funnel.get(host_port))}
    return None


def tailscale_serve_url_for_port(port: int) -> dict[str, Any] | None:
    now = time.monotonic()
    with _tailscale_serve_cache_lock:
        cached = _tailscale_serve_cache.get(port)
        if cached is not None and now - cached[0] < TAILSCALE_SERVE_CACHE_SECONDS:
            return cached[1]
    found = None
    try:
        result = subprocess.run(
            ["tailscale", "serve", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_SERVE_STATUS_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            found = tailscale_serve_address(json.loads(result.stdout), port)
    except (OSError, subprocess.SubprocessError, ValueError):
        found = None  # Tailscale isn't installed, isn't running, or answered oddly.
    with _tailscale_serve_cache_lock:
        _tailscale_serve_cache[port] = (now, found)
    return found


def tailscale_serve_option(port: int) -> dict[str, str] | None:
    serve = tailscale_serve_url_for_port(port)
    if serve is None:
        return None
    detail = f"{serve['url']}, public on the internet through Tailscale Funnel" if serve["public"] else serve["url"]
    return {
        "id": f"tailscale-serve:{serve['host']}",
        "label": f"Tailscale Serve ({detail})",
        "url": serve["url"],
        "interface": "tailscale-serve",
    }


def default_route_ipv4_address() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            address = sock.getsockname()[0]
            return address if address and not address.startswith("127.") else ""
    except OSError:
        return ""


def notification_access_url_options(host: str, port: int) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    default_route_address = default_route_ipv4_address()
    # A secure Tailscale Serve address comes first, so Automatic uses it when there is one.
    serve_option = tailscale_serve_option(port)
    if host not in {"0.0.0.0", "::", ""}:
        url = f"http://{host}:{port}"
        bound = {"id": "auto", "label": f"Server bind address ({url})", "url": url, "interface": ""}
        return [serve_option, bound] if serve_option is not None else [bound]
    try:
        for _index, name in socket.if_nameindex():
            if not interface_is_up(name) or not interface_is_physical_or_tailscale(name):
                continue
            address = interface_ipv4_address(name)
            if not address or address.startswith("127."):
                continue
            url = f"http://{address}:{port}"
            if url in seen_urls:
                continue
            seen_urls.add(url)
            lowered = name.lower()
            if lowered.startswith("tailscale"):
                magicdns = tailscale_magicdns_name(address)
                if magicdns:
                    magic_url = f"http://{magicdns}:{port}"
                    if magic_url not in seen_urls:
                        seen_urls.add(magic_url)
                        options.append(
                            {
                                "id": f"tailscale-magicdns:{name}:{magicdns}",
                                "label": f"Tailscale MagicDNS ({magicdns})",
                                "url": magic_url,
                                "interface": name,
                            }
                        )
                label = f"Tailscale ({address})"
                option_id = f"tailscale:{name}:{address}"
            else:
                label = f"{name} ({address})"
                option_id = f"interface:{name}:{address}"
            options.append({"id": option_id, "label": label, "url": url, "interface": name})
    except OSError:
        pass
    if not options:
        if default_route_address:
            options.append(
                {
                    "id": f"interface:default:{default_route_address}",
                    "label": f"Default network route ({default_route_address})",
                    "url": f"http://{default_route_address}:{port}",
                    "interface": "default",
                }
            )

    def option_sort_key(option: dict[str, str]) -> tuple[int, str, str]:
        option_id = option["id"]
        if option_id.startswith("tailscale-magicdns:"):
            return (0, "", option["label"].lower())
        if option_id.startswith("tailscale:"):
            return (1, "", option["label"].lower())
        parsed = urlparse(option.get("url", ""))
        address = parsed.hostname or ""
        default_rank = 0 if default_route_address and address == default_route_address else 1
        return (2, str(default_rank), option["label"].lower())

    options.sort(key=option_sort_key)
    if serve_option is not None:
        options.insert(0, serve_option)
    return options


def access_urls(host: str, port: int) -> list[str]:
    if host not in {"0.0.0.0", "::", ""}:
        return [f"http://{host}:{port}"]

    hosts = {"127.0.0.1"}
    for option in notification_access_url_options(host, port):
        parsed = urlparse(option["url"])
        if parsed.hostname:
            hosts.add(parsed.hostname)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            hosts.add(sock.getsockname()[0])
    except OSError:
        pass

    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address:
                hosts.add(address)
    except OSError:
        pass

    ordered_hosts = sorted(hosts, key=lambda value: (value.startswith("127."), value))
    return [f"http://{address}:{port}" for address in ordered_hosts]
