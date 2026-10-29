"""Who tried to sign in with a wrong password or two-step verification code: what device they were
on, going by what their browser says, and roughly where their address is.

The account they tried is told the next time it signs in, if there were more than a few (see
handler_sign_in_alerts), and so is the owner, for every account and for usernames that don't exist.

Where an address is comes from ipinfo.io, which is sent the address, and only that. Addresses on
the local network or a tailnet, and private ones, are never sent: they're named as such, going by
whether the request was proven local (see local_network), not by the address alone.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from typing import Callable

from . import __version__

LOG = logging.getLogger(__name__)

LOCATION_SERVICE = "https://ipinfo.io/{address}/json"
LOCATION_TIMEOUT_SECONDS = 8.0
LOCATION_CACHE_SIZE = 2000
LOCATION_CACHE_SECONDS = 24 * 3600.0
# A guesser can send many wrong passwords a second; the service is asked about this many
# addresses a minute at most, and the rest are left without a location.
LOCATION_LOOKUPS_PER_MINUTE = 20
COUNTRY_NAMES_FILE = "/usr/share/iso-codes/json/iso_3166-1.json"
TAILSCALE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))

# Programs, rather than browsers, by what they call themselves.
PROGRAMS = (
    ("curl", "curl"), ("wget", "Wget"), ("python", "Python"), ("go-http-client", "Go"), ("okhttp", "OkHttp"),
    ("java/", "Java"), ("libwww-perl", "Perl"), ("httpie", "HTTPie"), ("postman", "Postman"), ("powershell", "PowerShell"),
    ("node-fetch", "Node.js"), ("axios", "Node.js"), ("hydra", "Hydra"), ("nmap", "Nmap"), ("zgrab", "ZGrab"), ("masscan", "Masscan"),
)


def describe_device(user_agent: str) -> str:
    """Such as "Apple iPhone, Safari", "Windows, Chrome" or "a program (curl)", from what the browser says it is.

    Anyone can make their browser say anything, so it's what the device claimed to be.
    """
    agent = str(user_agent or "").strip()
    lowered = agent.lower()
    if not agent:
        return "an unrecognized device, which didn't say what it was"
    for marker, name in PROGRAMS:
        if marker in lowered:
            return f"a program ({name}), not a web browser"
    if "bot" in lowered or "crawler" in lowered or "spider" in lowered:
        return "an automated program (a bot), not a web browser"
    if "iphone" in lowered:
        system = "Apple iPhone"
    elif "ipad" in lowered:
        system = "Apple iPad"
    elif "android" in lowered:
        system = "Android phone or tablet"
    elif "cros" in lowered:
        system = "Chromebook"
    elif "windows" in lowered:
        system = "Windows computer"
    elif "macintosh" in lowered or "mac os x" in lowered:
        # iPads say they're Macs too, unless asked for a phone-sized page.
        system = "Apple Mac or iPad"
    elif "linux" in lowered or "x11" in lowered:
        system = "Linux computer"
    else:
        return "an unrecognized device"
    browser = ""
    for marker, name in (
        ("edg/", "Microsoft Edge"), ("edga/", "Microsoft Edge"), ("edgios/", "Microsoft Edge"), ("opr/", "Opera"),
        ("samsungbrowser", "Samsung Internet"), ("firefox/", "Firefox"), ("fxios/", "Firefox"),
        ("crios/", "Chrome"), ("chrome/", "Chrome"), ("safari/", "Safari"),
    ):
        if marker in lowered:
            browser = name
            break
    return f"{system}, {browser}" if browser else system


_country_names: dict[str, str] | None = None


def country_name(code: str) -> str:
    """A country's name from its two-letter code, from the iso-codes package; the code without it."""
    global _country_names
    if _country_names is None:
        try:
            with open(COUNTRY_NAMES_FILE, encoding="utf-8") as listing:
                _country_names = {item["alpha_2"]: item["name"] for item in json.load(listing)["3166-1"]}
        except (OSError, ValueError, KeyError):
            _country_names = {}
    return _country_names.get(str(code).upper(), str(code))


CARRIER_GRADE_NAT = ipaddress.ip_network("100.64.0.0/10")


def nearby_without_lookup(address: str, local: bool) -> str | None:
    """Where address is, such as "on your local network", when that's known without asking; local
    is whether the request was proven to come from the local network or the tailnet."""
    try:
        parsed = ipaddress.ip_address(str(address).strip("[]").split("%", 1)[0])
    except ValueError:
        return "from an unknown place"
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    tailnet = any(parsed in network for network in TAILSCALE_NETWORKS)
    if local:
        return "on your tailnet" if tailnet else "on your local network"
    if parsed.is_loopback:
        return "on this computer (or a tunnel on it)"
    if parsed in CARRIER_GRADE_NAT:
        # The same addresses as a tailnet's, but not through Tailscale.
        return "on an internet provider's shared network"
    if tailnet:
        return "from an unknown place"
    if parsed.is_private or parsed.is_link_local:
        return "on a private network that isn't your local network, such as a VPN"
    if not parsed.is_global:
        return "from an unknown place"
    return None


def location_text(answer: dict) -> str:
    """Such as "near Kyiv, Kyiv City, Ukraine", from what the lookup service says; "" if it said nothing."""
    parts = [str(answer.get(name) or "").strip() for name in ("city", "region")]
    country = str(answer.get("country") or "").strip()
    if country:
        parts.append(country_name(country))
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return f"near {', '.join(seen)}" if seen else ""


class AddressLocations:
    """Roughly where addresses are, such as "near Kyiv, Ukraine", looked up in the background and remembered for a day."""

    def __init__(self, fetch: Callable[[str], dict] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.lock = threading.Lock()
        self.cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self.recent: list[float] = []

    def known(self, address: str) -> str | None:
        if nearby_without_lookup(address, False) is not None:
            return "from an unknown place"  # not an address the internet knows, so never sent
        with self.lock:
            cached = self.cache.get(address)
            if cached is not None and cached[1] > self.clock():
                return cached[0]
        return None

    def look_up(self, address: str, then: Callable[[str], None]) -> None:
        """Calls then with where address is, now if that's known, otherwise once it's been looked up."""
        known = self.known(address)
        if known is not None:
            then(known)
            return
        now = self.clock()
        with self.lock:
            self.recent = [at for at in self.recent if now - at < 60.0]
            if len(self.recent) >= LOCATION_LOOKUPS_PER_MINUTE:
                return
            self.recent.append(now)

        def run() -> None:
            try:
                text = location_text(self.fetch(address))
            except Exception as exc:
                LOG.info("couldn't look up where %s is: %s", address, exc)
                return
            if not text:
                return
            with self.lock:
                self.cache[address] = (text, self.clock() + LOCATION_CACHE_SECONDS)
                self.cache.move_to_end(address)
                while len(self.cache) > LOCATION_CACHE_SIZE:
                    self.cache.popitem(last=False)
            then(text)

        threading.Thread(target=run, name="sign-in-location", daemon=True).start()

    @staticmethod
    def _fetch(address: str) -> dict:
        if not re.fullmatch(r"[0-9A-Fa-f.:]+", address):
            raise ValueError("not an address")
        request = urllib.request.Request(
            LOCATION_SERVICE.format(address=address), headers={"Accept": "application/json", "User-Agent": f"NWR-Stream-Manager/{__version__}"}
        )
        with urllib.request.urlopen(request, timeout=LOCATION_TIMEOUT_SECONDS) as response:
            return json.loads(response.read(64 * 1024).decode("utf-8"))
