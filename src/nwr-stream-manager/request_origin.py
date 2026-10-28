"""Where requests come from: this server's own pages or another website, and directly or through a proxy.

A browser that has signed in here sends that sign-in with every request to this
server, even one another website makes it send, such as a hidden form that posts
here. Only changes matter: another website can't read what comes back.
"""

from __future__ import annotations

import ipaddress
from typing import Mapping
from urllib.parse import urlsplit


ANOTHER_SITE_MESSAGE = "This request came from another website, so nothing was changed."

# What a plain form can send. A request with any other type, such as JSON, makes the
# browser ask this server first, and it never agrees, so only pages from here can send one.
FORM_CONTENT_TYPES = {"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"}
DEFAULT_PORTS = {"http": 80, "https": 443}
# What tunnels and proxies add to the requests they pass on, such as Cloudflare Tunnel's
# CF-Connecting-IP and Tailscale's Tailscale-User-Login. A browser can't take these away.
PROXY_HEADERS = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
    "X-Real-IP",
    "CF-Connecting-IP",
    "CF-Ray",
    "True-Client-IP",
    "Tailscale-User-Login",
    "Via",
)
SETUP_THROUGH_PROXY_MESSAGE = (
    "NWR Stream Manager can't be set up for the first time through a tunnel or proxy. "
    "Open it from a device on the same network as it to set it up."
)


def request_over_https(headers: Mapping[str, str]) -> bool:
    """Whether the browser reached this server over HTTPS, through a tunnel or proxy that says so.

    This server only speaks plain HTTP itself. Anyone can claim HTTPS over plain HTTP,
    but that only makes their own browser refuse its cookies.
    """
    # A chain of proxies lists every hop; the first is the browser's.
    if str(headers.get("X-Forwarded-Proto", "")).split(",", 1)[0].strip().lower() == "https":
        return True
    first = str(headers.get("Forwarded", "")).split(",", 1)[0]
    if any(part.strip().lower().replace('"', "") == "proto=https" for part in first.split(";")):
        return True
    # Cloudflare's own header, as {"scheme":"https"}.
    return '"https"' in str(headers.get("CF-Visitor", "")).replace(" ", "").lower()


def browser_host_name(headers: Mapping[str, str]) -> str:
    """The name the browser used for this server, as a tunnel or proxy passes it on, without the port."""
    for header in ("X-Forwarded-Host", "Host"):
        value = str(headers.get(header, "")).split(",", 1)[0].strip()
        found = host_header_address(value) if value else None
        if found is not None:
            return found[0]
    return ""


def request_through_proxy(headers: Mapping[str, str]) -> bool:
    """Whether a request came through a tunnel or proxy rather than straight from the browser.

    The address a request comes from can't tell: a tunnel on this computer connects
    from this computer, and one elsewhere on the network from there.
    """
    return any(str(headers.get(name, "")).strip() for name in PROXY_HEADERS)


def forwarded_client_address(peer: str, headers: Mapping[str, str]) -> str:
    """The visitor's own address, which a tunnel or proxy on this computer, such as Tailscale
    Funnel or Cloudflare Tunnel, passes on; "" if there's none to believe.

    Only a proxy on this computer is believed: anything else on the network could claim any
    address. Of a chain, the last address is the one the proxy added itself; the others came
    with the request, and the visitor could have put them there.
    """
    try:
        if not ipaddress.ip_address(peer.strip("[]").split("%", 1)[0]).is_loopback:
            return ""
    except ValueError:
        return ""
    candidates = [str(headers.get("X-Forwarded-For", "")).split(",")[-1]]
    last = str(headers.get("Forwarded", "")).split(",")[-1]
    candidates += [part.split("=", 1)[1] for part in last.split(";") if part.strip().lower().startswith("for=")]
    candidates += [str(headers.get(name, "")) for name in ("X-Real-IP", "CF-Connecting-IP", "True-Client-IP")]
    for candidate in candidates:
        value = candidate.strip().strip('"')
        # Forwarded gives IPv6 addresses in brackets, with any port after.
        if value.startswith("["):
            value = value[1:].split("]", 1)[0]
        elif value.count(":") == 1:
            value = value.split(":", 1)[0]
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            continue
    return ""


def request_from_another_site(method: str, path: str, headers: Mapping[str, str]) -> bool:
    """Whether a request that could change something was sent by another website.

    Reading a page changes nothing, so only changes are checked, along with live
    audio, whose connection stays open to send requests over.
    """
    websocket = str(headers.get("Upgrade", "")).strip().lower() == "websocket"
    if method in {"GET", "HEAD"} and not websocket:
        return False
    # Current browsers say where a request came from.
    fetch_site = str(headers.get("Sec-Fetch-Site", "")).strip().lower()
    if fetch_site:
        return fetch_site not in {"same-origin", "none"}
    origin = str(headers.get("Origin", "")).strip()
    if origin:
        return not origin_is_this_server(origin, headers)
    # Older browsers may say neither. A form from another website can still post here:
    # the forms on these pages carry a token it can't know, so an API request sent the
    # way a form sends one is refused.
    if path.startswith("/api/") and not websocket:
        content_type = str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
        return content_type in FORM_CONTENT_TYPES
    return False


def origin_is_this_server(origin: str, headers: Mapping[str, str]) -> bool:
    """Whether the page a request came from is on this server's address.

    The address is the one the browser used, which a proxy such as a tunnel passes
    on as Host or X-Forwarded-Host. Only the host and port are compared: a proxy that
    takes HTTPS connections may pass them on here as plain HTTP.
    """
    try:
        parts = urlsplit(origin)
        if parts.scheme not in DEFAULT_PORTS or not parts.hostname:
            return False
        origin_address = address(parts.hostname, parts.port, DEFAULT_PORTS[parts.scheme])
    except ValueError:
        return False
    for header in ("Host", "X-Forwarded-Host"):
        # A chain of proxies lists every address; the first is the browser's.
        value = str(headers.get(header, "")).split(",", 1)[0].strip()
        if value and host_header_address(value) == origin_address:
            return True
    return False


def host_header_address(value: str) -> tuple[str, int | None] | None:
    try:
        parts = urlsplit(f"//{value}")
        if not parts.hostname:
            return None
        return address(parts.hostname, parts.port, None)
    except ValueError:
        return None


def address(host: str, port: int | None, default_port: int | None) -> tuple[str, int | None]:
    # The default ports, 80 and 443, are left out of a Host header, so they're left out here too.
    if port in DEFAULT_PORTS.values() or port == default_port:
        port = None
    return host.lower().rstrip("."), port
