"""Whether a request provably came from this computer's own local network, or a tailnet it's on.

Anything that can't be proven local, such as a tunnel, a proxy or a port forwarded from the
internet, needs two-step verification on top of a password (see two_step).

Proven local means a direct connection, with nothing in it saying it was passed on, from an
address on one of this computer's own networks, or from a tailnet device through Tailscale; or a
request from a signed-in tailnet user through Tailscale Serve (see tailscale_serve_socket).
The address a connection comes from can't be faked once it's connected. Tailnet addresses are
also the ones internet providers use for carrier-grade NAT, so one only counts when the
connection came through Tailscale: by NWR Stream Manager's own connection to the tailnet, or one
that Tailscale on this computer sends replies to, which only that tailnet device would get. These
are never proven local, as anything could be behind them:

* A connection from this computer itself (127.0.0.1, ::1, or any of its own addresses), unless a
  web browser made it: a tunnel or proxy on it, such as ssh -R, connects from there too, and Linux
  says which program made a connection (see _connection_program).
* Anyone on a VPN this computer is connected to, such as a VPN service's other customers.
* A public IPv4 network, as servers in data centres are on, shared with other customers.
* Any network at all, but a tailnet, when this computer is directly on the internet, with a
  public IPv4 address, as a rented server is: data centres' private and IPv6 networks are often
  shared with other customers too, and look just like a home's.
* The network's router, which some routers make internet connections look like they came from
  when they pass them on from a forwarded port.
"""

from __future__ import annotations

import fcntl
import ipaddress
import os
import socket
import struct
import sys
import threading
import time
from typing import Callable, Mapping

from .request_origin import from_own_tailnet, from_tailscale_serve, request_through_proxy

TAILSCALE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))
# Containers' and virtual machines' own networks, which aren't the local network.
VIRTUAL_INTERFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "lxc", "lxd", "podman", "cni", "flannel", "vmnet", "vboxnet")
# VPNs and other tunnels: whoever else is on one, such as a VPN service's other customers, isn't on
# the local network either. Tailscale is told apart by its addresses instead.
TUNNEL_INTERFACE_PREFIXES = (
    "tun", "tap", "wg", "ppp", "zt", "nordlynx", "proton", "mullvad", "ipsec", "vti", "gre", "sit", "ip6tnl", "ip6gre", "utun", "wt",
)
NETWORKS_CACHE_SECONDS = 10.0
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
# Asking Linux which interface it sends to an address by (rtnetlink).
NETLINK_ROUTE = 0
RTM_NEWROUTE = 24
RTM_GETROUTE = 26
NLM_F_REQUEST = 1
RTA_DST = 1
RTA_OIF = 4

# Web browsers, by the name of the file each runs as, including the parts of them that make
# connections. A connection from this computer that one of these made is someone using it, rather than
# a tunnel or proxy passing someone else on. Any other program is asked for a code, which is the safe
# way to be wrong.
BROWSERS = frozenset({
    "firefox", "firefox-bin", "firefox-esr", "librewolf", "waterfox", "floorp", "zen", "seamonkey", "palemoon",
    "chrome", "chromium", "chromium-browser", "google-chrome", "brave", "msedge", "vivaldi-bin", "opera", "thorium",
    "WebKitNetworkProcess", "QtWebEngineProcess", "lynx", "w3m", "links", "elinks",
})
CONNECTION_PROGRAM_CACHE_SECONDS = 5.0
TCP_ESTABLISHED = "01"

Network = ipaddress.IPv4Network | ipaddress.IPv6Network
Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ipv4_ioctl(name: str, request: int) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        packed = fcntl.ioctl(probe.fileno(), request, struct.pack("256s", name[:15].encode()))
    return socket.inet_ntoa(packed[20:24])


def _ipv4_networks() -> list[tuple[str, Network]]:
    found = []
    try:
        names = [name for _index, name in socket.if_nameindex()]
    except OSError:
        return found
    for name in names:
        try:
            address = _ipv4_ioctl(name, SIOCGIFADDR)
            netmask = _ipv4_ioctl(name, SIOCGIFNETMASK)
        except OSError:
            continue  # no IPv4 address, or down
        found.append((name, ipaddress.ip_network(f"{address}/{netmask}", strict=False)))
    return found


def _ipv6_networks() -> list[tuple[str, Network]]:
    found = []
    try:
        with open("/proc/net/if_inet6", encoding="ascii") as listing:
            for line in listing:
                fields = line.split()
                if len(fields) < 6:
                    continue
                address = ipaddress.IPv6Address(int(fields[0], 16))
                found.append((fields[5], ipaddress.ip_network(f"{address}/{int(fields[2], 16)}", strict=False)))
    except (OSError, ValueError):
        pass
    return found


def _routers() -> set[Address]:
    """The default routers, from the routing tables."""
    routers: set[Address] = set()
    try:
        with open("/proc/net/route", encoding="ascii") as table:
            next(table, None)
            for line in table:
                fields = line.split()
                if len(fields) > 2 and fields[1] == "00000000" and fields[2] != "00000000":
                    routers.add(ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/net/ipv6_route", encoding="ascii") as table:
            for line in table:
                fields = line.split()
                if len(fields) > 4 and int(fields[1], 16) == 0 and int(fields[4], 16) != 0:
                    routers.add(ipaddress.IPv6Address(int(fields[4], 16)))
    except (OSError, ValueError):
        pass
    return routers


def _on_the_internet() -> bool:
    """Whether this computer is directly on the internet, rather than behind a router: it reaches the
    internet by an interface with a public IPv4 address of its own, as a rented server does. Behind
    a router, including an internet provider's carrier-grade NAT, it has a private one."""
    try:
        with open("/proc/net/route", encoding="ascii") as table:
            next(table, None)
            interfaces = {fields[0] for fields in map(str.split, table) if len(fields) > 1 and fields[1] == "00000000"}
    except (OSError, ValueError):
        return False
    for name in interfaces:
        if name.lower().startswith(("tailscale",) + VIRTUAL_INTERFACE_PREFIXES + TUNNEL_INTERFACE_PREFIXES):
            continue  # a VPN's address, not this computer's own connection's
        try:
            if ipaddress.IPv4Address(_ipv4_ioctl(name, SIOCGIFADDR)).is_global:
                return True
        except (OSError, ValueError):
            continue
    return False


def _route_interface(address: Address) -> str:
    """The interface this computer sends to address by, as Linux's routing (Tailscale's own rules
    included) decides it, or "" if that can't be found out."""
    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    destination = struct.pack("=HH", 4 + len(address.packed), RTA_DST) + address.packed
    body = struct.pack("=BBBBBBBBI", family, len(address.packed) * 8, 0, 0, 0, 0, 0, 0, 0) + destination
    message = struct.pack("=IHHII", 16 + len(body), RTM_GETROUTE, NLM_F_REQUEST, 1, 0) + body
    try:
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, NETLINK_ROUTE) as route:
            route.settimeout(1.0)
            route.send(message)
            reply = route.recv(65536)
        length, kind = struct.unpack_from("=IH", reply)
        if kind != RTM_NEWROUTE:
            return ""  # no route
        offset = 16 + 12  # the header, then the route's own
        while offset + 4 <= min(length, len(reply)):
            attribute_length, attribute = struct.unpack_from("=HH", reply, offset)
            if attribute_length < 4:
                break
            if attribute == RTA_OIF:
                return socket.if_indextoname(struct.unpack_from("=I", reply, offset + 4)[0])
            offset += (attribute_length + 3) & ~3
    except (OSError, struct.error):
        pass
    return ""


def _proc_net_address(text: str) -> tuple[Address, int]:
    """An address and port as /proc/net/tcp and tcp6 give them, such as 0100007F:1F90."""
    address, _separator, port = text.partition(":")
    raw = bytes.fromhex(address)
    # Each 32-bit word is in this computer's own byte order.
    packed = b"".join(raw[index:index + 4][::-1] for index in range(0, len(raw), 4)) if sys.byteorder == "little" else raw
    parsed = ipaddress.ip_address(packed)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    return parsed, int(port, 16)


def _connection_socket(address: Address, port: int) -> int:
    """The inode of the far end of a connection from address:port on this computer, if it's run by the
    same user as NWR Stream Manager; 0 if not, or it can't be found."""
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(table, encoding="ascii") as lines:
                next(lines, None)
                for line in lines:
                    fields = line.split()
                    if len(fields) < 10 or fields[3] != TCP_ESTABLISHED:
                        continue
                    if _proc_net_address(fields[1]) == (address, port):
                        return int(fields[9]) if int(fields[7]) == os.getuid() else 0
        except (OSError, ValueError):
            continue
    return 0


def _connection_program(address: Address, port: int) -> str:
    """The name of the file the program that made a connection from address:port on this computer
    runs as, such as firefox, if it's the same user's; "" if not, or it can't be found out. Linux only
    shows a program's connections to its own user (or root)."""
    inode = _connection_socket(address, port)
    if not inode:
        return ""
    wanted = f"socket:[{inode}]"
    uid = os.getuid()
    try:
        pids = [name for name in os.listdir("/proc") if name.isdigit()]
    except OSError:
        return ""
    for pid in pids:
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            for fd in os.listdir(f"/proc/{pid}/fd"):
                if os.readlink(f"/proc/{pid}/fd/{fd}") == wanted:
                    # A program updated while it runs is shown as "... (deleted)".
                    return os.path.basename(os.readlink(f"/proc/{pid}/exe").removesuffix(" (deleted)"))
        except OSError:
            continue
    return ""


def _home_network(network: Network) -> bool:
    """Whether a network this computer is on is one only its own devices are on.

    A home or office network has private IPv4 addresses. A public one, as servers in data centres
    have, is often shared with the provider's other customers. IPv6 networks are public, but a home
    router gives out a /64 of its own; anything wider is shared too.
    """
    if network.version == 4:
        return network.is_private
    return network.is_link_local or network.is_private or network.prefixlen >= 64


class LocalNetworks:
    """This computer's own networks and routers, looked up again every few seconds as they change."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.looked_at = float("-inf")
        self.networks: list[Network] = []
        self.tailscale_interface = False
        self.routers: set[Address] = set()
        # Which program made each recent connection from this computer, as finding it takes a moment.
        self.programs: dict[tuple[Address, int], tuple[float, str]] = {}

    def _refresh_locked(self) -> None:
        now = self.clock()
        if now - self.looked_at < NETWORKS_CACHE_SECONDS:
            return
        self.looked_at = now
        networks: list[Network] = []
        tailscale = False
        for name, network in _ipv4_networks() + _ipv6_networks():
            lowered = name.lower()
            if lowered.startswith("tailscale"):
                # The interface can be there while Tailscale is down; it has its address while it's up.
                tailscale = tailscale or any(network.network_address in tailnet for tailnet in TAILSCALE_NETWORKS)
                continue
            if network.is_loopback or lowered == "lo" or lowered.startswith(VIRTUAL_INTERFACE_PREFIXES + TUNNEL_INTERFACE_PREFIXES):
                continue
            if not _home_network(network):
                continue
            networks.append(network)
        if _on_the_internet():
            networks = []
        self.networks, self.tailscale_interface, self.routers = networks, tailscale, _routers()

    def is_local(self, host: str, headers: Mapping[str, str], port: int = 0) -> bool:
        """Whether a request from host and port (where it's connected from) is provably local."""
        if from_tailscale_serve(headers):
            # Through tailscaled itself, which takes these out of what a visitor sends and puts them in
            # itself: a signed-in user on the tailnet, and never Funnel.
            return bool(str(headers.get("Tailscale-User-Login", "")).strip()) and not str(headers.get("Tailscale-Funnel-Request", "")).strip()
        if request_through_proxy(headers):
            return False
        try:
            address = ipaddress.ip_address(str(host).strip("[]").split("%", 1)[0])
        except ValueError:
            return False
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_unspecified:
            return False
        with self.lock:
            self._refresh_locked()
            networks, tailscale, routers = self.networks, self.tailscale_interface, self.routers
        if address in routers:
            return False
        if address.is_loopback or _route_interface(address) == "lo":
            # This computer itself, by any of its addresses, as Linux sends to its own by lo.
            return self._browser_here(address, port)
        if any(address in network for network in TAILSCALE_NETWORKS):
            if from_own_tailnet(headers):
                return True
            # Through Tailscale on this computer, only if it sends replies there by Tailscale: a
            # connection can't be made without them, so only that tailnet device could have made it.
            # Otherwise it's an internet provider's carrier-grade NAT address, not a tailnet's.
            return tailscale and _route_interface(address).lower().startswith("tailscale")
        return any(address in network for network in networks)

    def _browser_here(self, address: Address, port: int) -> bool:
        """Whether a web browser on this computer made the connection from address:port."""
        if port <= 0:
            return False  # handed over from elsewhere, so not a connection from here
        now = self.clock()
        with self.lock:
            self.programs = {key: found for key, found in self.programs.items() if now - found[0] < CONNECTION_PROGRAM_CACHE_SECONDS}
            found = self.programs.get((address, port))
        if found is None:
            found = (now, _connection_program(address, port))
            with self.lock:
                self.programs[(address, port)] = found
        return found[1] in BROWSERS


local_networks = LocalNetworks()
