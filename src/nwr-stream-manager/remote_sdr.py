"""Remote SDR access between NWR Stream Manager instances on a LAN.

One instance (the host) shares its local RTL-SDR; others (clients) use it for
their streams, weather radio receiver and I/Q recordings as if it were plugged
in locally.

Transport
    TLS 1.3 over TCP (REMOTE_SDR_TCP_PORT). Every instance has a self-signed
    certificate created on first use; the SHA-256 fingerprint of that
    certificate is the instance's identity, so a host is recognised by its
    identity rather than its IP address. Clients pin the host fingerprint and
    authenticate themselves with a per-client token issued at pairing.

Discovery
    UDP broadcast on REMOTE_SDR_DISCOVERY_PORT. Clients broadcast a query and
    hosts with remote access enabled answer with their identity, name, TCP
    port and a short SDR summary. A paired host whose address changed is found
    again by identity.

Pairing
    The host shows a short numeric code. The client proves knowledge of it with
    SRP-6a (RFC 5054, 2048-bit group, SHA-256) whose transcript is bound to the
    host certificate fingerprint the client sees, so an eavesdropper cannot
    brute-force the code offline and a relaying man in the middle fails the
    proof. Only after verifying the host's proof does the client accept the
    token the host issues.

Framing
    Each message is a 5-byte header (big-endian payload length, frame kind)
    followed by the payload. JSON frames carry control messages; IQ frames
    carry a fixed header and interleaved little-endian CS16 samples.

Connections
    One control connection per client carries SDR status pushes and settings
    changes. Every IQ feed (a stream channel, the weather radio receiver or a
    recording) uses its own connection, so a wideband recording cannot delay
    a stream. The host does all channelizing and decimation; clients only
    convert CS16 back to complex float32 and demodulate, record or store it.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import queue
import secrets
import select
import shutil
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np


LOG = logging.getLogger(__name__)

REMOTE_SDR_PROTOCOL_VERSION = 1
REMOTE_SDR_TCP_PORT = 47432
REMOTE_SDR_DISCOVERY_PORT = 47433
REMOTE_SDR_DISCOVERY_MAGIC = "nwr-stream-manager-remote-sdr"
REMOTE_SDR_ROLE_PAIR = "pair"
REMOTE_SDR_ROLE_CONTROL = "control"
REMOTE_SDR_ROLE_CHANNEL = "channel"
REMOTE_SDR_ROLE_WIDEBAND = "wideband"

FRAME_JSON = 1
FRAME_IQ = 2
FRAME_HEADER = struct.Struct(">IB")
IQ_HEADER = struct.Struct(">QIQdI")  # sequence, sample rate, center Hz, host time, generation
MAX_FRAME_BYTES = 16 * 1024 * 1024
CS16_SCALE = 32767.0

HANDSHAKE_TIMEOUT_SECONDS = 10.0
CONTROL_STATUS_INTERVAL_SECONDS = 1.0
CONTROL_REQUEST_TIMEOUT_SECONDS = 10.0
SOCKET_IO_TIMEOUT_SECONDS = 15.0
# A host that loses power never closes its connections, so silence is the only sign it is
# gone. The host sends status every CONTROL_STATUS_INTERVAL_SECONDS; after this long with
# nothing, give up on the connection and reconnect.
CONTROL_IDLE_TIMEOUT_SECONDS = 10.0
# IQ arrives continuously while the host's SDR runs. This is a backstop: losing the control
# connection already drops the IQ feeds.
REMOTE_IQ_IDLE_TIMEOUT_SECONDS = 15.0
RECONNECT_MIN_SECONDS = 1.0
# Kept short so a returning host is picked up within seconds; each attempt is only a
# connection try and one discovery broadcast.
RECONNECT_MAX_SECONDS = 5.0
DISCOVERY_TIMEOUT_SECONDS = 2.0
DISCOVERY_SEND_ROUNDS = 3  # Wi-Fi drops broadcast packets; ask more than once
DISCOVERY_ROUND_SPACING_SECONDS = 0.3
CONNECT_TIMEOUT_SECONDS = 4.0
# Remote feeds usually cross Wi-Fi or Tailscale. Streams and the receiver play
# from a jitter buffer this far behind real time; users trade latency for
# riding out congestion and dropouts with the client's network buffer setting.
REMOTE_IQ_BUFFER_SECONDS = 0.5
REMOTE_IQ_MIN_BUFFER_SECONDS = 0.1
REMOTE_IQ_MAX_SETTING_SECONDS = 10.0
REMOTE_IQ_BUFFER_HEADROOM_SECONDS = 3.0  # above target + this, catch up to the target
REMOTE_IQ_RATE_ADJUST_LIMIT = 0.02  # max playout speed change for clock drift
REMOTE_IQ_RATE_ADJUST_GAIN = 0.01  # speed change per second away from target
REMOTE_IQ_BUFFER_EPSILON_SECONDS = 1e-6  # buffered time is a running float sum
# Recordings are not played back live: no pacing, just a deep queue.
REMOTE_IQ_WIDEBAND_QUEUE_SECONDS = 15.0
TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
TAILSCALE_STATUS_TIMEOUT_SECONDS = 3.0
# Tailscale peers that cannot be running NWR Stream Manager.
TAILSCALE_SKIPPED_OS = {"ios", "android", "ipados", "tvos"}
VIRTUAL_INTERFACE_PREFIXES = ("br-", "cali", "cni", "docker", "flannel", "kube", "lxc", "podman", "veth", "virbr", "vmnet", "vboxnet")

PAIRING_CODE_DIGITS = 6
# Pairing mode ends when a device pairs, after this long, or after this many
# incorrect codes, whichever comes first.
PAIRING_CODE_TTL_SECONDS = 300.0
PAIRING_MAX_ATTEMPTS = 5
SRP_IDENTITY = b"nwr-stream-manager"

# RFC 3526 / RFC 5054 2048-bit safe prime group, generator 2.
SRP_N = int(
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AACAA68FFFFFFFFFFFFFFFF",
    16,
)
SRP_G = 2


class RemoteSdrError(RuntimeError):
    """A remote SDR connection or request failed."""


class RemoteSdrAuthError(RemoteSdrError):
    """Pairing or authentication was rejected."""


class RemoteSdrProtocolError(RemoteSdrError):
    """The peer sent something this protocol version does not understand."""


# ---------------------------------------------------------------------------
# Identity and pairing records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RemoteSdrIdentity:
    fingerprint: str
    name: str
    cert_path: Path
    key_path: Path


def certificate_fingerprint(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


def default_instance_name() -> str:
    return socket.gethostname().split(".")[0] or "NWR Stream Manager"


def load_or_create_identity(directory: Path, name: str | None = None) -> RemoteSdrIdentity:
    """Load this instance's certificate, creating it on first use with the openssl tool."""
    directory.mkdir(parents=True, exist_ok=True)
    cert_path = directory / "identity-cert.pem"
    key_path = directory / "identity-key.pem"
    if not cert_path.exists() or not key_path.exists():
        _generate_identity(cert_path, key_path)
        LOG.info("created remote SDR identity in %s", directory)
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
    return RemoteSdrIdentity(
        fingerprint=certificate_fingerprint(der),
        name=name or default_instance_name(),
        cert_path=cert_path,
        key_path=key_path,
    )


def _generate_identity(cert_path: Path, key_path: Path) -> None:
    # Written into a private scratch directory first so the key is never
    # readable by others and a half-written pair is never left in place.
    scratch = Path(tempfile.mkdtemp(prefix=".identity-", dir=cert_path.parent))
    try:
        new_cert = scratch / "cert.pem"
        new_key = scratch / "key.pem"
        try:
            result = subprocess.run(
                [
                    "openssl", "req", "-x509",
                    "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
                    "-nodes", "-sha256", "-days", "36500",
                    "-subj", "/CN=NWR Stream Manager remote SDR",
                    "-keyout", str(new_key), "-out", str(new_cert),
                ],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RemoteSdrError("The openssl command is required for remote SDR access. Install the openssl package.") from exc
        if result.returncode != 0 or not new_cert.exists() or not new_key.exists():
            detail = (result.stderr or result.stdout or "").strip().splitlines()
            raise RemoteSdrError(f"openssl could not create the remote SDR identity: {detail[-1] if detail else result.returncode}")
        os.chmod(new_key, 0o600)
        os.chmod(new_cert, 0o600)
        os.replace(new_key, key_path)
        os.replace(new_cert, cert_path)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
    os.replace(temporary, path)


class _JsonRecordStore:
    """A small JSON object persisted atomically with owner-only permissions."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.key = key
        self.lock = threading.Lock()

    def _load_locked(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception as exc:
            LOG.warning("could not read %s: %s", self.path, exc)
            return {}
        records = raw.get(self.key) if isinstance(raw, dict) else None
        return {str(key): dict(value) for key, value in (records or {}).items() if isinstance(value, dict)}

    def _save_locked(self, records: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, self.key: records}, indent=2, sort_keys=True) + "\n"
        _write_private(self.path, payload.encode("utf-8"))

    def all(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            return self._load_locked()

    def get(self, record_id: str) -> dict[str, Any] | None:
        return self.all().get(str(record_id))

    def put(self, record_id: str, record: dict[str, Any]) -> None:
        with self.lock:
            records = self._load_locked()
            records[str(record_id)] = dict(record)
            self._save_locked(records)

    def update(self, record_id: str, **changes: Any) -> None:
        with self.lock:
            records = self._load_locked()
            if str(record_id) not in records:
                return
            records[str(record_id)].update(changes)
            self._save_locked(records)

    def remove(self, record_id: str) -> bool:
        with self.lock:
            records = self._load_locked()
            removed = records.pop(str(record_id), None) is not None
            if removed:
                self._save_locked(records)
            return removed


class PairedDeviceStore(_JsonRecordStore):
    """Instances this one is paired with, usable in either direction.

    One pairing covers both roles: `token` is what this instance presents when
    it uses the peer's SDR, and `peer_token_sha256` checks what the peer
    presents when it uses this instance's SDR. The record id is the peer's
    certificate fingerprint, which is pinned for every connection to it.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(path, "devices")

    def add(
        self,
        peer_id: str,
        name: str,
        *,
        token: str,
        peer_token: str,
        address: str = "",
        port: int = REMOTE_SDR_TCP_PORT,
        addresses: list[str] | None = None,
    ) -> None:
        ordered = ordered_addresses([address] + list(addresses or []))
        self.put(
            peer_id,
            {
                "name": name,
                "token": token,
                "peer_token_sha256": hashlib.sha256(peer_token.encode("ascii")).hexdigest(),
                "address": ordered[0] if ordered else "",
                "addresses": ordered,
                "port": int(port),
                "paired_at": time.time(),
                "last_seen_at": None,
            },
        )

    def note_address(self, peer_id: str, address: str, port: int | None = None, *, reached: bool = False) -> None:
        """Remember an address the peer was reached at (or connected from).

        `reached` marks the address this instance last connected to
        successfully, which is tried first next time so a dead Tailscale
        address does not cost a timeout on every connection.
        """
        with self.lock:
            records = self._load_locked()
            record = records.get(str(peer_id))
            if record is None:
                return
            addresses = ordered_addresses([address] + list(record.get("addresses") or []) + [record.get("address", "")])
            changes = {"addresses": addresses, "address": addresses[0]}
            if reached:
                changes["last_reached_address"] = address
            if port is not None:
                changes["port"] = int(port)
            if all(record.get(key) == value for key, value in changes.items()):
                return
            record.update(changes)
            self._save_locked(records)

    def authenticate(self, peer_id: str, token: str) -> dict[str, Any] | None:
        record = self.get(peer_id)
        if record is None or not token:
            return None
        expected = str(record.get("peer_token_sha256", ""))
        actual = hashlib.sha256(str(token).encode("ascii", "replace")).hexdigest()
        return record if hmac.compare_digest(expected, actual) else None

    def public_list(self) -> list[dict[str, Any]]:
        return [
            {
                "id": peer_id,
                "name": record.get("name", ""),
                "address": record.get("address", ""),
                "addresses": list(record.get("addresses") or [record.get("address", "")]),
                "via": "tailscale" if is_tailscale_address(str(record.get("address", ""))) else "lan",
                "port": record.get("port", REMOTE_SDR_TCP_PORT),
                "paired_at": record.get("paired_at"),
                "last_seen_at": record.get("last_seen_at"),
            }
            for peer_id, record in sorted(self.all().items(), key=lambda item: str(item[1].get("name", "")))
        ]


def is_identity_fingerprint(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


# ---------------------------------------------------------------------------
# SRP-6a
# ---------------------------------------------------------------------------


def _srp_hash(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def _srp_int(*parts: bytes) -> int:
    return int.from_bytes(_srp_hash(*parts), "big")


def _srp_pad(value: int) -> bytes:
    return value.to_bytes((SRP_N.bit_length() + 7) // 8, "big")


SRP_K = _srp_int(_srp_pad(SRP_N), _srp_pad(SRP_G))


def _srp_private_key(salt: bytes, password: str) -> int:
    return _srp_int(salt, _srp_hash(SRP_IDENTITY + b":" + password.encode("utf-8")))


def _srp_proofs(a_public: int, b_public: int, session_key: bytes, context: bytes) -> tuple[bytes, bytes]:
    client_proof = _srp_hash(_srp_pad(a_public), _srp_pad(b_public), session_key, context)
    host_proof = _srp_hash(_srp_pad(a_public), client_proof, session_key, context)
    return client_proof, host_proof


class SrpClient:
    def __init__(self, password: str, context: bytes) -> None:
        self.password = password
        self.context = context
        self._a = secrets.randbits(256) + 1
        self.public = pow(SRP_G, self._a, SRP_N)
        self._host_proof = b""

    def process_challenge(self, salt: bytes, b_public: int) -> bytes:
        if b_public % SRP_N == 0:
            raise RemoteSdrAuthError("host sent an invalid pairing challenge")
        u = _srp_int(_srp_pad(self.public), _srp_pad(b_public))
        if u == 0:
            raise RemoteSdrAuthError("host sent an invalid pairing challenge")
        x = _srp_private_key(salt, self.password)
        base = (b_public - SRP_K * pow(SRP_G, x, SRP_N)) % SRP_N
        shared = pow(base, self._a + u * x, SRP_N)
        key = _srp_hash(_srp_pad(shared))
        client_proof, self._host_proof = _srp_proofs(self.public, b_public, key, self.context)
        return client_proof

    def verify_host(self, host_proof: bytes) -> bool:
        return bool(self._host_proof) and hmac.compare_digest(self._host_proof, host_proof)


class SrpHost:
    def __init__(self, password: str, context: bytes) -> None:
        self.context = context
        self.salt = secrets.token_bytes(16)
        self._verifier = pow(SRP_G, _srp_private_key(self.salt, password), SRP_N)
        self._b = secrets.randbits(256) + 1
        self.public = (SRP_K * self._verifier + pow(SRP_G, self._b, SRP_N)) % SRP_N
        self._client_proof = b""
        self._host_proof = b""

    def process_client(self, a_public: int) -> None:
        if a_public % SRP_N == 0:
            raise RemoteSdrAuthError("client sent an invalid pairing request")
        u = _srp_int(_srp_pad(a_public), _srp_pad(self.public))
        shared = pow((a_public * pow(self._verifier, u, SRP_N)) % SRP_N, self._b, SRP_N)
        key = _srp_hash(_srp_pad(shared))
        self._client_proof, self._host_proof = _srp_proofs(a_public, self.public, key, self.context)

    def verify_client(self, client_proof: bytes) -> bytes | None:
        if self._client_proof and hmac.compare_digest(self._client_proof, client_proof):
            return self._host_proof
        return None


def pairing_context(host_fingerprint: str, client_id: str) -> bytes:
    return f"nwr-stream-manager-pair-v1|{host_fingerprint}|{client_id}".encode("utf-8")


# ---------------------------------------------------------------------------
# Framing and sample conversion
# ---------------------------------------------------------------------------


class FrameConnection:
    """A TLS socket carrying framed messages.

    OpenSSL does not allow one connection to be used by two threads at once,
    and most connections here have a reader and a sender thread. Waiting for
    data happens outside the lock; reading a whole frame and sending a frame
    each happen under it.
    """

    def __init__(self, sock: ssl.SSLSocket, peer_fingerprint: str = "") -> None:
        self.sock = sock
        self.peer_fingerprint = peer_fingerprint
        self.io_lock = threading.Lock()
        self.closed = False
        sock.settimeout(SOCKET_IO_TIMEOUT_SECONDS)

    def send_frame(self, kind: int, payload: bytes) -> None:
        if len(payload) > MAX_FRAME_BYTES:
            raise RemoteSdrProtocolError("frame is too large")
        with self.io_lock:
            if self.closed:
                raise ConnectionError("remote SDR connection closed")
            self.sock.sendall(FRAME_HEADER.pack(len(payload), kind) + payload)

    def send_json(self, message: dict[str, Any]) -> None:
        self.send_frame(FRAME_JSON, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def recv_frame(self, timeout: float | None = None) -> tuple[int, bytes]:
        """Read one frame; raises TimeoutError, without consuming data, if none starts in time."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.closed:
                raise ConnectionError("remote SDR connection closed")
            wait = 0.5 if deadline is None else max(0.0, min(0.5, deadline - time.monotonic()))
            if self._readable(wait):
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("no remote SDR message arrived in time")
        with self.io_lock:
            if self.closed:
                raise ConnectionError("remote SDR connection closed")
            header = self._recv_exact(FRAME_HEADER.size)
            length, kind = FRAME_HEADER.unpack(header)
            if length > MAX_FRAME_BYTES:
                raise RemoteSdrProtocolError("peer sent a frame that is too large")
            return kind, self._recv_exact(length)

    def _readable(self, timeout: float) -> bool:
        try:
            if self.sock.pending():
                return True
            readable, _, _ = select.select([self.sock], [], [], timeout)
        except (OSError, ValueError) as exc:
            raise ConnectionError("remote SDR connection closed") from exc
        return bool(readable)

    def recv_json(self, timeout: float | None = None) -> dict[str, Any]:
        kind, payload = self.recv_frame(timeout)
        if kind != FRAME_JSON:
            raise RemoteSdrProtocolError("expected a control message")
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteSdrProtocolError("peer sent invalid JSON") from exc
        if not isinstance(message, dict):
            raise RemoteSdrProtocolError("control messages must be JSON objects")
        return message

    def _recv_exact(self, count: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < count:
            chunk = self.sock.recv(count - len(chunks))
            if not chunk:
                raise ConnectionError("remote SDR connection closed")
            chunks.extend(chunk)
        return bytes(chunks)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        # Shut the TCP socket down underneath TLS to wake any thread blocked
        # on it, without touching the TLS state another thread may be using.
        try:
            socket.socket.shutdown(self.sock, socket.SHUT_RDWR)
        except OSError:
            pass
        if self.io_lock.acquire(timeout=SOCKET_IO_TIMEOUT_SECONDS):
            try:
                self.sock.close()
            except OSError:
                pass
            finally:
                self.io_lock.release()


def complex_to_cs16(samples: np.ndarray) -> bytes:
    samples = np.asarray(samples, dtype=np.complex64)
    interleaved = np.empty(samples.size * 2, dtype=np.float32)
    interleaved[0::2] = samples.real
    interleaved[1::2] = samples.imag
    np.multiply(interleaved, CS16_SCALE, out=interleaved)
    np.clip(interleaved, -32768.0, 32767.0, out=interleaved)
    return np.rint(interleaved).astype("<i2").tobytes()


def cs16_to_complex(payload: bytes | memoryview) -> np.ndarray:
    raw = np.frombuffer(payload, dtype="<i2")
    usable = raw.size - (raw.size % 2)
    scaled = raw[:usable].astype(np.float32) / CS16_SCALE
    return (scaled[0::2] + 1j * scaled[1::2]).astype(np.complex64)


@dataclass
class RemoteIqBatch:
    """Matches the attributes the stream pipeline reads from local batches."""

    data: np.ndarray
    sample_rate: int
    center_frequency_hz: int
    captured_at: float = field(default_factory=time.monotonic)
    source_generation: int = 0


def encode_iq_frame(sequence: int, sample_rate: int, center_frequency_hz: int, generation: int, samples: np.ndarray) -> bytes:
    header = IQ_HEADER.pack(sequence, int(sample_rate), int(center_frequency_hz), time.time(), int(generation) & 0xFFFFFFFF)
    return header + complex_to_cs16(samples)


def decode_iq_frame(payload: bytes) -> tuple[int, int, int, float, int, np.ndarray]:
    if len(payload) < IQ_HEADER.size:
        raise RemoteSdrProtocolError("IQ frame is too short")
    sequence, sample_rate, center, host_time, generation = IQ_HEADER.unpack_from(payload)
    return sequence, sample_rate, center, host_time, generation, cs16_to_complex(memoryview(payload)[IQ_HEADER.size :])


# ---------------------------------------------------------------------------
# TLS helpers
# ---------------------------------------------------------------------------


def _server_context(identity: RemoteSdrIdentity) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(identity.cert_path), str(identity.key_path))
    return context


def _client_context() -> ssl.SSLContext:
    # Hosts use self-signed certificates; the fingerprint is checked by hand
    # against the paired identity (or bound into the pairing proof).
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def open_host_connection(
    address: str,
    port: int,
    *,
    expected_fingerprint: str | None,
    timeout: float = HANDSHAKE_TIMEOUT_SECONDS,
) -> FrameConnection:
    raw = socket.create_connection((address, int(port)), timeout=timeout)
    try:
        raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock = _client_context().wrap_socket(raw, server_hostname=None)
    except Exception:
        raw.close()
        raise
    der = sock.getpeercert(binary_form=True) or b""
    fingerprint = certificate_fingerprint(der)
    if expected_fingerprint and not hmac.compare_digest(fingerprint, expected_fingerprint):
        sock.close()
        raise RemoteSdrAuthError("the remote SDR at this address is a different NWR Stream Manager instance")
    return FrameConnection(sock, fingerprint)


# ---------------------------------------------------------------------------
# Network interfaces and Tailscale
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalInterface:
    name: str
    address: str
    broadcast: str
    tailscale: bool


def is_tailscale_address(address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in TAILSCALE_NETWORK
    except ValueError:
        return False


def address_rank(address: str) -> int:
    """Lower is preferred: Tailscale, then LAN, then loopback."""
    if is_tailscale_address(address):
        return 0
    try:
        if ipaddress.ip_address(address).is_loopback:
            return 2
    except ValueError:
        pass
    return 1


def ordered_addresses(addresses: list[str]) -> list[str]:
    unique = list(dict.fromkeys(address for address in addresses if address))
    return sorted(unique, key=address_rank)


def _interface_ipv4(name: str, request: int) -> str:
    import fcntl

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        packed = struct.pack("256s", name[:15].encode("utf-8"))
        return socket.inet_ntoa(fcntl.ioctl(sock.fileno(), request, packed)[20:24])
    except OSError:
        return ""
    finally:
        sock.close()


def _interface_is_up(name: str) -> bool:
    try:
        return (Path("/sys/class/net") / name / "operstate").read_text(encoding="utf-8").strip() in {"up", "unknown"}
    except OSError:
        return False


def local_interfaces() -> list[LocalInterface]:
    """Up IPv4 interfaces worth discovering on (no loopback or container bridges)."""
    interfaces: list[LocalInterface] = []
    try:
        names = [name for _index, name in socket.if_nameindex()]
    except OSError:
        return interfaces
    for name in names:
        lowered = name.lower()
        if lowered == "lo" or lowered.startswith(VIRTUAL_INTERFACE_PREFIXES) or not _interface_is_up(name):
            continue
        address = _interface_ipv4(name, 0x8915)  # SIOCGIFADDR
        if not address or address.startswith("127."):
            continue
        tailscale = lowered.startswith("tailscale") or is_tailscale_address(address)
        broadcast = "" if tailscale else _interface_ipv4(name, 0x8919)  # SIOCGIFBRDADDR
        interfaces.append(LocalInterface(name, address, broadcast, tailscale))
    return interfaces


def local_addresses() -> dict[str, Any]:
    interfaces = local_interfaces()
    return {
        "tailscale": next((item.address for item in interfaces if item.tailscale), ""),
        "lan": [item.address for item in interfaces if not item.tailscale],
    }


def tailscale_is_up() -> bool:
    return any(item.tailscale for item in local_interfaces())


def tailscale_peer_addresses() -> list[str]:
    """IPv4 addresses of online tailnet peers that could run NWR Stream Manager.

    Tailscale carries no broadcast traffic, so discovery asks each peer
    directly. Uses the tailscale CLI, which ordinary users may run for status.
    """
    try:
        result = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=TAILSCALE_STATUS_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        status = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    peers = status.get("Peer") if isinstance(status, dict) else None
    addresses: list[str] = []
    for peer in (peers or {}).values():
        if not isinstance(peer, dict) or not peer.get("Online"):
            continue
        if str(peer.get("OS", "")).lower() in TAILSCALE_SKIPPED_OS:
            continue
        for address in peer.get("TailscaleIPs") or []:
            if is_tailscale_address(str(address)):
                addresses.append(str(address))
                break
    return addresses


def discovery_targets() -> list[str]:
    targets = ["255.255.255.255"]
    for interface in local_interfaces():
        if interface.broadcast and not interface.tailscale:
            targets.append(interface.broadcast)
    targets.extend(tailscale_peer_addresses())
    targets.append("127.0.0.1")
    return list(dict.fromkeys(targets))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class DiscoveryResponder:
    """Answers LAN discovery queries while remote access is enabled."""

    def __init__(
        self,
        identity: RemoteSdrIdentity,
        tcp_port: int,
        summary_provider: Callable[[], dict[str, Any]],
        *,
        port: int = REMOTE_SDR_DISCOVERY_PORT,
        bind_address: str = "",
    ) -> None:
        self.identity = identity
        self.tcp_port = tcp_port
        self.summary_provider = summary_provider
        self.port = port
        self.bind_address = bind_address
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.sock: socket.socket | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind((self.bind_address, self.port))
        sock.settimeout(0.5)
        self.port = sock.getsockname()[1]
        self.sock = sock
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="remote-sdr-discovery", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def announcement(self) -> dict[str, Any]:
        try:
            summary = dict(self.summary_provider() or {})
        except Exception as exc:
            LOG.debug("remote SDR discovery summary failed: %s", exc)
            summary = {}
        return {
            "magic": REMOTE_SDR_DISCOVERY_MAGIC,
            "type": "announce",
            "v": REMOTE_SDR_PROTOCOL_VERSION,
            "id": self.identity.fingerprint,
            "name": self.identity.name,
            "port": self.tcp_port,
            "sdr": summary,
            "addresses": local_addresses(),
        }

    def _run(self) -> None:
        assert self.sock is not None
        while not self.stop_event.is_set():
            try:
                data, address = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("magic") != REMOTE_SDR_DISCOVERY_MAGIC:
                continue
            if message.get("type") != "discover":
                continue
            try:
                self.sock.sendto(json.dumps(self.announcement()).encode("utf-8"), address)
            except OSError as exc:
                LOG.debug("remote SDR discovery reply to %s failed: %s", address, exc)


def discover_hosts(
    *,
    timeout: float = DISCOVERY_TIMEOUT_SECONDS,
    port: int = REMOTE_SDR_DISCOVERY_PORT,
    targets: list[str] | None = None,
    exclude_id: str = "",
) -> list[dict[str, Any]]:
    """Find hosts sharing an SDR on the LAN and over Tailscale.

    Each host appears once however many ways it answered. Its addresses are
    ordered Tailscale first, then LAN: the Tailscale address also works away
    from home, so it is the one to remember.
    """
    query = json.dumps(
        {"magic": REMOTE_SDR_DISCOVERY_MAGIC, "type": "discover", "v": REMOTE_SDR_PROTOCOL_VERSION}
    ).encode("utf-8")
    if targets is None:
        targets = discovery_targets()
    use_tailscale = tailscale_is_up() or any(is_tailscale_address(target) for target in targets)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict[str, Any]] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        deadline = time.monotonic() + timeout
        next_round_at = time.monotonic()
        rounds_sent = 0
        while True:
            now = time.monotonic()
            if rounds_sent < DISCOVERY_SEND_ROUNDS and now >= next_round_at:
                for target in targets:
                    try:
                        sock.sendto(query, (target, port))
                    except OSError as exc:
                        LOG.debug("remote SDR discovery query to %s failed: %s", target, exc)
                rounds_sent += 1
                next_round_at = now + DISCOVERY_ROUND_SPACING_SECONDS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            wait = remaining if rounds_sent >= DISCOVERY_SEND_ROUNDS else min(remaining, max(0.0, next_round_at - time.monotonic()))
            sock.settimeout(max(0.01, wait))
            try:
                data, (source, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                message = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            merge_announcement(found, message, source, use_tailscale=use_tailscale, exclude_id=exclude_id)
    finally:
        sock.close()
    return sorted(found.values(), key=lambda host: (host["name"], host["id"]))


def merge_announcement(
    found: dict[str, dict[str, Any]],
    message: Any,
    source: str,
    *,
    use_tailscale: bool,
    exclude_id: str = "",
) -> None:
    """Fold one discovery reply into `found`, keyed by host identity."""
    if not isinstance(message, dict) or message.get("magic") != REMOTE_SDR_DISCOVERY_MAGIC:
        return
    if message.get("type") != "announce" or not message.get("id"):
        return
    host_id = str(message["id"])
    if host_id == exclude_id:
        return
    advertised = message.get("addresses") if isinstance(message.get("addresses"), dict) else {}
    candidates = [source]
    if use_tailscale and advertised.get("tailscale"):
        candidates.append(str(advertised["tailscale"]))
    candidates.extend(str(address) for address in advertised.get("lan") or [])
    if not use_tailscale:
        candidates = [address for address in candidates if not is_tailscale_address(address) or address == source]
    host = found.get(host_id)
    if host is None:
        host = found[host_id] = {
            "id": host_id,
            "name": str(message.get("name", "")),
            "port": int(message.get("port", REMOTE_SDR_TCP_PORT)),
            "protocol_version": int(message.get("v", 0)),
            "sdr": message.get("sdr") if isinstance(message.get("sdr"), dict) else {},
            "addresses": [],
            "answered_from": [],
        }
    host["answered_from"] = ordered_addresses(host["answered_from"] + [source])
    # Addresses that actually answered come before ones only advertised.
    answered = host["answered_from"]
    host["addresses"] = ordered_addresses(answered) + [
        address for address in ordered_addresses(host["addresses"] + candidates) if address not in answered
    ]
    host["address"] = host["addresses"][0]
    host["via"] = "tailscale" if is_tailscale_address(host["address"]) else "lan" if address_rank(host["address"]) == 1 else "local"


# ---------------------------------------------------------------------------
# Host side
# ---------------------------------------------------------------------------


class RemoteIqSource(Protocol):
    def read(self, timeout: float) -> Any | None: ...

    def close(self) -> None: ...


class RemoteSdrHostBackend(Protocol):
    """What the host server needs from the application."""

    def status(self) -> dict[str, Any]: ...

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]: ...

    def open_channel(self, frequency_hz: int, name: str) -> Any: ...

    def open_wideband(self, sample_rate: int, name: str) -> Any: ...


@dataclass
class _PairingWindow:
    code: str
    expires_at: float
    attempts: int = 0


class RemoteSdrServer:
    def __init__(
        self,
        identity: RemoteSdrIdentity,
        devices: PairedDeviceStore,
        backend: RemoteSdrHostBackend,
        *,
        bind_address: str = "0.0.0.0",
        port: int = REMOTE_SDR_TCP_PORT,
        discovery_port: int | None = REMOTE_SDR_DISCOVERY_PORT,
    ) -> None:
        self.identity = identity
        self.devices = devices
        self.backend = backend
        self.bind_address = bind_address
        self.port = port
        self.discovery_port = discovery_port
        self.context = _server_context(identity)
        self.stop_event = threading.Event()
        self.listener: socket.socket | None = None
        self.accept_thread: threading.Thread | None = None
        self.discovery: DiscoveryResponder | None = None
        self.lock = threading.Lock()
        self.pairing: _PairingWindow | None = None
        self.pairing_ended_reason = ""
        self.connections: dict[int, dict[str, Any]] = {}
        self._connection_ids = 0

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.bind_address, self.port))
        listener.listen(16)
        listener.settimeout(0.5)
        self.port = listener.getsockname()[1]
        self.listener = listener
        self.stop_event.clear()
        self.accept_thread = threading.Thread(target=self._accept_loop, name="remote-sdr-accept", daemon=True)
        self.accept_thread.start()
        if self.discovery_port is not None:
            self.discovery = DiscoveryResponder(
                self.identity,
                self.port,
                self._discovery_summary,
                port=self.discovery_port,
            )
            try:
                self.discovery.start()
            except OSError as exc:
                LOG.warning("remote SDR discovery is unavailable: %s", exc)
                self.discovery = None
        LOG.info("remote SDR access listening on port %s", self.port)

    def stop(self) -> None:
        self.stop_event.set()
        if self.discovery is not None:
            self.discovery.stop()
            self.discovery = None
        if self.listener is not None:
            try:
                self.listener.close()
            except OSError:
                pass
        with self.lock:
            connections = [entry["connection"] for entry in self.connections.values()]
        for connection in connections:
            connection.close()
        if self.accept_thread is not None:
            self.accept_thread.join(timeout=2.0)
        LOG.info("remote SDR access stopped")

    # -- pairing ----------------------------------------------------------

    def begin_pairing(self) -> dict[str, Any]:
        """Enter pairing mode, keeping the current code if it is already on."""
        with self.lock:
            if self._active_pairing_locked() is None:
                code = "".join(secrets.choice("0123456789") for _ in range(PAIRING_CODE_DIGITS))
                self.pairing = _PairingWindow(code=code, expires_at=time.time() + PAIRING_CODE_TTL_SECONDS)
                self.pairing_ended_reason = ""
                LOG.info("remote SDR pairing mode started for %.0f seconds", PAIRING_CODE_TTL_SECONDS)
        return self.pairing_status()

    def cancel_pairing(self) -> None:
        with self.lock:
            if self._active_pairing_locked() is not None:
                LOG.info("remote SDR pairing mode stopped")
                self.pairing_ended_reason = "stopped"
            self.pairing = None

    def pairing_status(self) -> dict[str, Any]:
        with self.lock:
            window = self._active_pairing_locked()
            if window is None:
                return {"active": False, "ended_reason": self.pairing_ended_reason}
            return {
                "active": True,
                "code": window.code,
                "expires_at": window.expires_at,
                "attempts_left": PAIRING_MAX_ATTEMPTS - window.attempts,
            }

    def _active_pairing_locked(self) -> _PairingWindow | None:
        if self.pairing is not None and time.time() >= self.pairing.expires_at:
            self.pairing = None
            self.pairing_ended_reason = "expired"
        return self.pairing

    def unpair(self, client_id: str) -> bool:
        removed = self.devices.remove(client_id)
        with self.lock:
            victims = [entry["connection"] for entry in self.connections.values() if entry.get("client_id") == client_id]
        for connection in victims:
            connection.close()
        return removed

    # -- status -----------------------------------------------------------

    def connected_clients(self) -> list[dict[str, Any]]:
        with self.lock:
            entries = [dict(entry) for entry in self.connections.values() if entry.get("client_id")]
        summary: dict[str, dict[str, Any]] = {}
        for entry in entries:
            item = summary.setdefault(
                entry["client_id"],
                {"id": entry["client_id"], "name": entry.get("client_name", ""), "address": entry.get("address", ""), "feeds": 0},
            )
            if entry.get("role") in {REMOTE_SDR_ROLE_CHANNEL, REMOTE_SDR_ROLE_WIDEBAND}:
                item["feeds"] += 1
        return sorted(summary.values(), key=lambda item: item["name"])

    def _discovery_summary(self) -> dict[str, Any]:
        status = self.backend.status()
        return {
            "device_name": status.get("device_name", ""),
            "connected": bool(status.get("connected")),
        }

    # -- connections ------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self.listener is not None
        while not self.stop_event.is_set():
            try:
                raw, address = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(
                target=self._handle_connection,
                args=(raw, address),
                name=f"remote-sdr-{address[0]}",
                daemon=True,
            ).start()

    def _handle_connection(self, raw: socket.socket, address: tuple[str, int]) -> None:
        connection: FrameConnection | None = None
        with self.lock:
            self._connection_ids += 1
            connection_id = self._connection_ids
        try:
            raw.settimeout(HANDSHAKE_TIMEOUT_SECONDS)
            raw.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            connection = FrameConnection(self.context.wrap_socket(raw, server_side=True))
            with self.lock:
                self.connections[connection_id] = {"connection": connection, "address": address[0]}
            hello = connection.recv_json()
            if hello.get("type") != "hello":
                raise RemoteSdrProtocolError("expected hello")
            if int(hello.get("v", 0)) != REMOTE_SDR_PROTOCOL_VERSION:
                connection.send_json(
                    {"type": "error", "error": "This NWR Stream Manager uses a different remote SDR protocol version."}
                )
                return
            role = str(hello.get("role", ""))
            if role == REMOTE_SDR_ROLE_PAIR:
                self._handle_pairing(connection, hello, address)
                return
            record = self.devices.authenticate(str(hello.get("client_id", "")), str(hello.get("token", "")))
            if record is None:
                connection.send_json({"type": "error", "code": "unauthorized", "error": "This device is not paired with the remote SDR."})
                return
            client_id = str(hello["client_id"])
            with self.lock:
                self.connections[connection_id].update(
                    {"client_id": client_id, "client_name": record.get("name", ""), "role": role}
                )
            self.devices.update(client_id, last_seen_at=time.time())
            self.devices.note_address(client_id, address[0])
            if role == REMOTE_SDR_ROLE_CONTROL:
                self._handle_control(connection)
            elif role == REMOTE_SDR_ROLE_CHANNEL:
                frequency_hz = int(hello.get("frequency_hz", 0))
                source = self.backend.open_channel(frequency_hz, f"remote:{record.get('name', client_id)}")
                self._stream_iq(connection, source, retunable=True)
            elif role == REMOTE_SDR_ROLE_WIDEBAND:
                sample_rate = int(hello.get("sample_rate", 0))
                source = self.backend.open_wideband(sample_rate, f"remote:{record.get('name', client_id)}")
                self._stream_iq(connection, source, retunable=False)
            else:
                connection.send_json({"type": "error", "error": f"unknown role {role!r}"})
        except (ConnectionError, OSError, ssl.SSLError) as exc:
            LOG.debug("remote SDR connection from %s ended: %s", address[0], exc)
        except ValueError as exc:
            if connection is not None:
                try:
                    connection.send_json({"type": "error", "error": str(exc)})
                except Exception:
                    pass
        except Exception as exc:
            LOG.warning("remote SDR connection from %s failed: %s", address[0], exc)
        finally:
            with self.lock:
                self.connections.pop(connection_id, None)
            if connection is not None:
                connection.close()
            else:
                try:
                    raw.close()
                except OSError:
                    pass

    def _handle_pairing(self, connection: FrameConnection, hello: dict[str, Any], address: tuple[str, int]) -> None:
        client_id = str(hello.get("client_id", "")).strip()
        client_name = str(hello.get("client_name", "")).strip()[:80] or address[0]
        try:
            a_public = int(str(hello.get("A", "")), 16)
        except ValueError as exc:
            raise RemoteSdrProtocolError("invalid pairing request") from exc
        if not is_identity_fingerprint(client_id) or client_id == self.identity.fingerprint:
            raise RemoteSdrProtocolError("invalid pairing request")
        try:
            client_port = int(hello.get("client_port") or REMOTE_SDR_TCP_PORT)
        except (TypeError, ValueError):
            client_port = REMOTE_SDR_TCP_PORT
        with self.lock:
            window = self._active_pairing_locked()
            code = window.code if window is not None else ""
        if not code:
            connection.send_json(
                {
                    "type": "error",
                    "code": "pairing-closed",
                    "error": "Pairing is not open on the remote SDR. Start pairing on that NWR Stream Manager first.",
                }
            )
            return
        srp = SrpHost(code, pairing_context(self.identity.fingerprint, client_id))
        srp.process_client(a_public)
        connection.send_json({"type": "pair-challenge", "salt": srp.salt.hex(), "B": format(srp.public, "x")})
        reply = connection.recv_json()
        try:
            client_proof = bytes.fromhex(str(reply.get("M1", "")))
        except ValueError:
            client_proof = b""
        host_proof = srp.verify_client(client_proof)
        if host_proof is None:
            with self.lock:
                window = self._active_pairing_locked()
                if window is not None:
                    window.attempts += 1
                    if window.attempts >= PAIRING_MAX_ATTEMPTS:
                        self.pairing = None
                        self.pairing_ended_reason = "too-many-attempts"
                        LOG.warning("remote SDR pairing closed after %s failed attempts", PAIRING_MAX_ATTEMPTS)
            LOG.warning("remote SDR pairing attempt from %s used the wrong code", address[0])
            connection.send_json({"type": "error", "code": "wrong-code", "error": "The pairing code is incorrect."})
            return
        with self.lock:
            # One pairing per code: pairing mode ends as soon as a device pairs.
            self.pairing = None
            self.pairing_ended_reason = "paired"
        host_token = secrets.token_urlsafe(32)
        connection.send_json(
            {
                "type": "paired",
                "M2": host_proof.hex(),
                "token": host_token,
                "host": {"id": self.identity.fingerprint, "name": self.identity.name},
            }
        )
        # The client hands over its own token only after checking our proof,
        # so the pairing also lets this instance use the client's SDR later.
        complete = connection.recv_json(timeout=HANDSHAKE_TIMEOUT_SECONDS)
        client_token = str(complete.get("token", "")) if complete.get("type") == "pair-complete" else ""
        if len(client_token) < 32:
            raise RemoteSdrProtocolError("the client did not complete pairing")
        advertised = hello.get("client_addresses") if isinstance(hello.get("client_addresses"), dict) else {}
        self.devices.add(
            client_id,
            client_name,
            token=client_token,
            peer_token=host_token,
            address=address[0],
            port=client_port,
            addresses=[str(advertised.get("tailscale") or "")] + [str(item) for item in advertised.get("lan") or []],
        )
        connection.send_json({"type": "pair-stored"})
        LOG.info("paired with %s (%s)", client_name, address[0])

    def _handle_control(self, connection: FrameConnection) -> None:
        connection.send_json(
            {"type": "welcome", "host": {"id": self.identity.fingerprint, "name": self.identity.name}}
        )
        done = threading.Event()

        def reader() -> None:
            try:
                while not done.is_set():
                    message = connection.recv_json()
                    if message.get("type") != "request":
                        continue
                    request_id = message.get("id")
                    action = str(message.get("action", ""))
                    try:
                        if action == "update_settings":
                            changes = message.get("changes") if isinstance(message.get("changes"), dict) else {}
                            status = self.backend.update_settings(changes)
                        elif action == "status":
                            status = self.backend.status()
                        else:
                            raise ValueError(f"unknown request {action!r}")
                        connection.send_json({"type": "response", "id": request_id, "ok": True, "status": status})
                    except ValueError as exc:
                        connection.send_json({"type": "response", "id": request_id, "ok": False, "error": str(exc)})
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=reader, name="remote-sdr-control-reader", daemon=True).start()
        while not done.is_set() and not self.stop_event.is_set():
            connection.send_json({"type": "status", "status": self.backend.status()})
            done.wait(CONTROL_STATUS_INTERVAL_SECONDS)

    def _stream_iq(self, connection: FrameConnection, source: Any, *, retunable: bool) -> None:
        connection.send_json({"type": "welcome", "host": {"id": self.identity.fingerprint, "name": self.identity.name}})
        done = threading.Event()

        def reader() -> None:
            try:
                while not done.is_set():
                    message = connection.recv_json()
                    kind = message.get("type")
                    if kind == "retune" and retunable:
                        source.set_frequency(int(message.get("frequency_hz", 0)))
                    elif kind == "close":
                        break
            except Exception:
                pass
            finally:
                done.set()

        threading.Thread(target=reader, name="remote-sdr-iq-reader", daemon=True).start()
        sequence = 0
        try:
            while not done.is_set() and not self.stop_event.is_set():
                batch = source.read(0.5)
                if batch is None or getattr(batch, "data", None) is None or len(batch.data) == 0:
                    continue
                connection.send_frame(
                    FRAME_IQ,
                    encode_iq_frame(
                        sequence,
                        batch.sample_rate,
                        batch.center_frequency_hz,
                        int(getattr(batch, "source_generation", 0)),
                        batch.data,
                    ),
                )
                sequence += 1
        finally:
            done.set()
            source.close()


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------


def pair_with_host(
    address: str,
    port: int,
    code: str,
    *,
    client_id: str,
    client_name: str,
    client_port: int = REMOTE_SDR_TCP_PORT,
    expected_host_id: str = "",
) -> dict[str, Any]:
    """Pair with a host using the code it displays.

    Returns the record to store in PairedDeviceStore.add: the pairing works
    in both directions, so it includes the token this instance issued for the
    host to present if their roles are later reversed.
    """
    code = "".join(character for character in str(code) if character.isdigit())
    if len(code) != PAIRING_CODE_DIGITS:
        raise RemoteSdrAuthError(f"Enter the {PAIRING_CODE_DIGITS}-digit pairing code shown on the remote SDR.")
    connection = open_host_connection(address, port, expected_fingerprint=expected_host_id or None)
    try:
        srp = SrpClient(code, pairing_context(connection.peer_fingerprint, client_id))
        connection.send_json(
            {
                "type": "hello",
                "v": REMOTE_SDR_PROTOCOL_VERSION,
                "role": REMOTE_SDR_ROLE_PAIR,
                "client_id": client_id,
                "client_name": client_name,
                "client_port": int(client_port),
                "client_addresses": local_addresses(),
                "A": format(srp.public, "x"),
            }
        )
        challenge = connection.recv_json()
        if challenge.get("type") == "error":
            raise RemoteSdrAuthError(str(challenge.get("error", "Pairing was rejected.")))
        client_proof = srp.process_challenge(bytes.fromhex(str(challenge["salt"])), int(str(challenge["B"]), 16))
        connection.send_json({"type": "pair-proof", "M1": client_proof.hex()})
        result = connection.recv_json()
        if result.get("type") == "error":
            raise RemoteSdrAuthError(str(result.get("error", "Pairing was rejected.")))
        if not srp.verify_host(bytes.fromhex(str(result.get("M2", "")))):
            raise RemoteSdrAuthError("The remote SDR could not prove it knows the pairing code.")
        host = result.get("host") if isinstance(result.get("host"), dict) else {}
        if host.get("id") != connection.peer_fingerprint:
            raise RemoteSdrAuthError("The remote SDR identity does not match its connection.")
        peer_token = secrets.token_urlsafe(32)
        connection.send_json({"type": "pair-complete", "token": peer_token})
        stored = connection.recv_json(timeout=HANDSHAKE_TIMEOUT_SECONDS)
        if stored.get("type") != "pair-stored":
            raise RemoteSdrAuthError(str(stored.get("error", "The remote SDR did not finish pairing.")))
        return {
            "id": connection.peer_fingerprint,
            "name": str(host.get("name", "")),
            "address": address,
            "port": int(port),
            "token": str(result["token"]),
            "peer_token": peer_token,
        }
    except (KeyError, ValueError) as exc:
        raise RemoteSdrProtocolError("The remote SDR sent an invalid pairing response.") from exc
    finally:
        connection.close()


class RemoteSdrClient:
    """Keeps a control connection to one paired host and opens IQ feeds."""

    def __init__(
        self,
        host_id: str,
        hosts: PairedDeviceStore,
        *,
        client_id: str,
        discovery_port: int = REMOTE_SDR_DISCOVERY_PORT,
        discovery_targets: list[str] | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        buffer_seconds: float = REMOTE_IQ_BUFFER_SECONDS,
    ) -> None:
        self.buffer_seconds = float(buffer_seconds)
        self.fanouts: set["RemoteIqFanout"] = set()
        self.host_id = host_id
        self.hosts = hosts
        self.client_id = client_id
        self.discovery_port = discovery_port
        self.discovery_targets = discovery_targets
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.connection: FrameConnection | None = None
        self.host_status: dict[str, Any] = {}
        self.reachable = False
        # True until the first connection attempt settles either way, so a
        # client that has only just started is not reported as unreachable.
        self.connecting = True
        self.connected_address = ""
        self.last_status_at: float | None = None
        self.error = ""
        self.pending: dict[int, dict[str, Any]] = {}
        self.pending_condition = threading.Condition(self.lock)
        self._request_ids = 0

    def start(self) -> None:
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="remote-sdr-control", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            connection = self.connection
        if connection is not None:
            connection.close()
        if self.thread is not None:
            self.thread.join(timeout=3.0)

    def record(self) -> dict[str, Any]:
        record = self.hosts.get(self.host_id)
        if record is None:
            raise RemoteSdrAuthError("This remote SDR is no longer paired.")
        return record

    def status(self) -> dict[str, Any]:
        record = self.hosts.get(self.host_id) or {}
        with self.lock:
            return {
                "host_id": self.host_id,
                "host_name": record.get("name", ""),
                "address": record.get("address", ""),
                "reachable": self.reachable,
                "connecting": self.connecting and not self.reachable,
                "connected_address": self.connected_address if self.reachable else "",
                "last_status_at": self.last_status_at,
                "error": self.error,
                "sdr": dict(self.host_status),
            }

    def connect(self, role: str, **params: Any) -> FrameConnection:
        """Open an authenticated connection, rediscovering the host if needed.

        Remembered addresses are tried Tailscale first, then LAN. If none work
        the host is looked up again by identity on both networks.
        """
        record = self.record()
        port = int(record.get("port", REMOTE_SDR_TCP_PORT))
        tailscale = tailscale_is_up()

        def usable(addresses: list[str]) -> list[str]:
            return [address for address in ordered_addresses(addresses) if tailscale or not is_tailscale_address(address)]

        last_reached = str(record.get("last_reached_address") or "")
        remembered = usable(list(record.get("addresses") or []) + [record.get("address", "")])
        if last_reached in remembered:
            remembered = [last_reached] + [address for address in remembered if address != last_reached]
        attempts = [(address, port) for address in remembered]
        last_error: Exception | None = None
        for index in range(2):
            for address, attempt_port in attempts:
                try:
                    connection = open_host_connection(
                        address, attempt_port, expected_fingerprint=self.host_id, timeout=CONNECT_TIMEOUT_SECONDS
                    )
                except (RemoteSdrAuthError, OSError, ssl.SSLError) as exc:
                    last_error = exc
                    continue
                try:
                    connection.send_json(
                        {
                            "type": "hello",
                            "v": REMOTE_SDR_PROTOCOL_VERSION,
                            "role": role,
                            "client_id": self.client_id,
                            "token": record.get("token", ""),
                            **params,
                        }
                    )
                    welcome = connection.recv_json(timeout=HANDSHAKE_TIMEOUT_SECONDS)
                except Exception:
                    connection.close()
                    raise
                if welcome.get("type") == "error":
                    connection.close()
                    if welcome.get("code") == "unauthorized":
                        raise RemoteSdrAuthError("The remote SDR no longer recognises this device. Pair it again.")
                    raise RemoteSdrError(str(welcome.get("error", "The remote SDR refused the connection.")))
                self.hosts.note_address(self.host_id, address, attempt_port, reached=True)
                return connection
            if index == 0:
                # The host may have a new address; find it by identity.
                attempts = [
                    (address, int(host["port"]))
                    for host in discover_hosts(port=self.discovery_port, targets=self.discovery_targets)
                    if host["id"] == self.host_id
                    for address in usable(host["addresses"])
                ]
        raise RemoteSdrError(f"The remote SDR is not reachable: {last_error or 'not found on the network'}")

    def update_settings(self, changes: dict[str, Any], timeout: float = CONTROL_REQUEST_TIMEOUT_SECONDS) -> dict[str, Any]:
        status = self._request("update_settings", timeout=timeout, changes=changes)
        with self.lock:
            self.host_status = status
            self.last_status_at = time.time()
        return status

    def _request(self, action: str, *, timeout: float, **payload: Any) -> dict[str, Any]:
        with self.lock:
            connection = self.connection
            if connection is None:
                raise RemoteSdrError("The remote SDR is not reachable.")
            self._request_ids += 1
            request_id = self._request_ids
        connection.send_json({"type": "request", "id": request_id, "action": action, **payload})
        deadline = time.monotonic() + timeout
        with self.pending_condition:
            while request_id not in self.pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RemoteSdrError("The remote SDR did not respond.")
                self.pending_condition.wait(remaining)
            response = self.pending.pop(request_id)
        if not response.get("ok"):
            raise ValueError(str(response.get("error", "The remote SDR rejected the request.")))
        return dict(response.get("status") or {})

    def _run(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while not self.stop_event.is_set():
            try:
                connection = self.connect(REMOTE_SDR_ROLE_CONTROL)
            except Exception as exc:
                self._set_unreachable(str(exc))
                self.stop_event.wait(delay)
                delay = min(RECONNECT_MAX_SECONDS, delay * 2)
                continue
            delay = RECONNECT_MIN_SECONDS
            try:
                peer_address = connection.sock.getpeername()[0]
            except OSError:
                peer_address = ""
            with self.lock:
                self.connection = connection
                self.connected_address = peer_address
                self.error = ""
            try:
                while not self.stop_event.is_set():
                    try:
                        message = connection.recv_json(timeout=CONTROL_IDLE_TIMEOUT_SECONDS)
                    except TimeoutError as exc:
                        raise ConnectionError("the remote SDR stopped responding") from exc
                    kind = message.get("type")
                    if kind == "status":
                        status = message.get("status") if isinstance(message.get("status"), dict) else {}
                        with self.lock:
                            self.host_status = status
                            self.reachable = True
                            self.connecting = False
                            self.last_status_at = time.time()
                        if self.on_status is not None:
                            self.on_status(status)
                    elif kind == "response":
                        with self.pending_condition:
                            self.pending[int(message.get("id", 0))] = message
                            self.pending_condition.notify_all()
            except Exception as exc:
                if not self.stop_event.is_set():
                    LOG.warning("remote SDR control connection lost: %s", exc)
                    self._set_unreachable(str(exc))
                    # The IQ feeds ride on the same host; if it vanished they are dead too,
                    # and may never notice on their own if its connections went silent.
                    self._drop_feed_connections()
            finally:
                with self.lock:
                    self.connection = None
                connection.close()

    def _drop_feed_connections(self) -> None:
        with self.lock:
            fanouts = list(self.fanouts)
        for fanout in fanouts:
            fanout.drop_connection()

    def _set_unreachable(self, error: str) -> None:
        with self.lock:
            self.reachable = False
            self.connecting = False
            self.error = error

    def channel_fanout(self, frequency_hz: int, name: str = "channel", *, buffer_seconds: float | None = None) -> "RemoteIqFanout":
        fanout = RemoteIqFanout(
            self,
            REMOTE_SDR_ROLE_CHANNEL,
            {"frequency_hz": int(frequency_hz)},
            name=name,
            buffer_seconds=self.buffer_seconds if buffer_seconds is None else buffer_seconds,
        )
        with self.lock:
            self.fanouts.add(fanout)
        return fanout

    def set_buffer_seconds(self, seconds: float) -> None:
        """Apply a new network buffer depth to this client's live channel feeds."""
        with self.lock:
            self.buffer_seconds = float(seconds)
            fanouts = list(self.fanouts)
        for fanout in fanouts:
            fanout.set_buffer_seconds(seconds)

    def forget_fanout(self, fanout: "RemoteIqFanout") -> None:
        with self.lock:
            self.fanouts.discard(fanout)

    def wideband_fanout(self, sample_rate: int, name: str = "wideband") -> "RemoteIqFanout":
        fanout = RemoteIqFanout(self, REMOTE_SDR_ROLE_WIDEBAND, {"sample_rate": int(sample_rate)}, name=name)
        with self.lock:
            self.fanouts.add(fanout)
        return fanout


class IqJitterBuffer:
    """Plays out remote IQ at real-time pace from a buffer of target depth.

    Nothing is released until `target_seconds` are buffered. Batches are then
    released on a real-time schedule, so a burst that arrives after a network
    stall refills the buffer instead of flooding downstream. Release speed is
    nudged (within REMOTE_IQ_RATE_ADJUST_LIMIT) toward keeping the buffer at
    its target, which absorbs drift between the host's SDR sample clock and
    this machine's clock. Running dry means the network was out longer than
    the buffer lasts: the buffer fills to its target again before resuming.
    Above `max_seconds` the oldest audio is dropped back to the target so
    latency cannot grow without bound. Callers pass the time in, so the policy
    can be tested deterministically.
    """

    def __init__(
        self,
        target_seconds: float = REMOTE_IQ_BUFFER_SECONDS,
        max_seconds: float | None = None,
    ) -> None:
        self.target_seconds = float(target_seconds)
        self.max_seconds = self._max_for(self.target_seconds, max_seconds)
        self.items: deque[tuple[Any, float]] = deque()
        self.buffered_seconds = 0.0
        self.playing = False
        self.next_release_at = 0.0
        self.underruns = 0
        self.trimmed_seconds = 0.0

    def push(self, item: Any, duration: float) -> None:
        self.items.append((item, float(duration)))
        self.buffered_seconds += float(duration)
        if self.buffered_seconds > self.max_seconds + REMOTE_IQ_BUFFER_EPSILON_SECONDS:
            while self.items and self.buffered_seconds > self.target_seconds + REMOTE_IQ_BUFFER_EPSILON_SECONDS:
                _item, dropped = self.items.popleft()
                self.buffered_seconds -= dropped
                self.trimmed_seconds += dropped
            self._settle()

    def _settle(self) -> None:
        # Keep rounding error in the running total from accumulating.
        if not self.items:
            self.buffered_seconds = 0.0

    def flush(self) -> None:
        self.items.clear()
        self.buffered_seconds = 0.0
        self.playing = False

    @staticmethod
    def _max_for(target_seconds: float, max_seconds: float | None) -> float:
        if max_seconds is None:
            max_seconds = target_seconds + REMOTE_IQ_BUFFER_HEADROOM_SECONDS
        return max(float(max_seconds), target_seconds)

    def set_target(self, target_seconds: float, max_seconds: float | None = None) -> None:
        """Change the buffer depth while playing, keeping sample order intact.

        A smaller target drops the oldest buffered IQ (audio skips ahead); a
        larger one pauses playout until the buffer has filled to it (audio
        falls further behind). Data only ever leaves from the front, so
        nothing is reordered or repeated.
        """
        self.target_seconds = float(target_seconds)
        self.max_seconds = self._max_for(self.target_seconds, max_seconds)
        while self.items and self.buffered_seconds > self.target_seconds + REMOTE_IQ_BUFFER_EPSILON_SECONDS:
            _item, dropped = self.items.popleft()
            self.buffered_seconds -= dropped
            self.trimmed_seconds += dropped
        self._settle()
        if self.playing and self.buffered_seconds < self.target_seconds - REMOTE_IQ_BUFFER_EPSILON_SECONDS:
            self.playing = False

    def pop_due(self, now: float) -> list[Any]:
        if not self.playing:
            if self.buffered_seconds < self.target_seconds - REMOTE_IQ_BUFFER_EPSILON_SECONDS:
                return []
            self.playing = True
            self.next_release_at = now
        released: list[Any] = []
        while now >= self.next_release_at:
            if not self.items:
                self.playing = False
                self.underruns += 1
                break
            item, duration = self.items.popleft()
            self.buffered_seconds -= duration
            self._settle()
            released.append(item)
            excess = self.buffered_seconds - self.target_seconds
            speed_change = max(-REMOTE_IQ_RATE_ADJUST_LIMIT, min(REMOTE_IQ_RATE_ADJUST_LIMIT, excess * REMOTE_IQ_RATE_ADJUST_GAIN))
            self.next_release_at += duration * (1.0 - speed_change)
        return released

    def seconds_until_due(self, now: float) -> float | None:
        if not self.playing:
            return None
        return max(0.0, self.next_release_at - now)

    def stats(self) -> dict[str, Any]:
        return {
            "state": "playing" if self.playing else "buffering",
            "buffered_seconds": round(self.buffered_seconds, 3),
            "target_seconds": self.target_seconds,
            "underruns": self.underruns,
            "trimmed_seconds": round(self.trimmed_seconds, 3),
        }


class RemoteIqFanout:
    """One remote IQ feed exposed through the local fanout interface.

    Stream workers, the weather radio receiver and the I/Q recorder subscribe
    to it exactly as they would to a local fanout. Batches arrive already
    channelized or decimated by the host, so their local channelizers pass
    them through. Reconnecting bumps `generation`, which makes a consumer drop
    in-flight batches and reset its DSP state. Retuning does not: the host
    retunes its channelizer in place, so the new channel follows the old one
    in the same sample stream, and the audio already buffered keeps playing
    until the new channel reaches the front. Each batch carries the frequency
    it was channelized at.

    Channel feeds play out through an IqJitterBuffer; wideband feeds, used for
    recordings, are delivered as they arrive through a deep queue instead.
    """

    def __init__(
        self,
        client: RemoteSdrClient,
        role: str,
        params: dict[str, Any],
        *,
        name: str,
        buffer_seconds: float = REMOTE_IQ_BUFFER_SECONDS,
    ) -> None:
        self.client = client
        self.role = role
        self.params = dict(params)
        self.name = name
        self.generation = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.subscriber: queue.Queue | None = None
        self.thread: threading.Thread | None = None
        self.paced = role == REMOTE_SDR_ROLE_CHANNEL
        self.jitter = IqJitterBuffer(buffer_seconds) if self.paced else None
        self.jitter_condition = threading.Condition(self.lock)
        self.playout_thread: threading.Thread | None = None
        self.connection: FrameConnection | None = None
        self.received_batches = 0
        self.dropped_batches = 0
        self.connected = False
        self.error = ""

    @property
    def target_frequency_hz(self) -> int | None:
        return self.params.get("frequency_hz")

    def subscribe(self, max_chunks: int | None = None, max_seconds: float | None = None, name: str = "subscriber") -> queue.Queue:
        if max_chunks is None:
            max_chunks = max(8, int(round((max_seconds or 1.5) * 20)))
        if not self.paced:
            max_chunks = max(max_chunks, int(REMOTE_IQ_WIDEBAND_QUEUE_SECONDS * 25))
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.lock:
            self.subscriber = subscriber
            if self.thread is None:
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._run, name=f"remote-iq-{self.name}", daemon=True)
                self.thread.start()
                if self.paced:
                    self.playout_thread = threading.Thread(
                        target=self._playout, name=f"remote-iq-playout-{self.name}", daemon=True
                    )
                    self.playout_thread.start()
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        self.client.forget_fanout(self)
        with self.lock:
            self.jitter_condition.notify_all()
            connection = self.connection
        if connection is not None:
            try:
                connection.send_json({"type": "close"})
            except Exception:
                pass
            connection.close()

    def drop_connection(self) -> None:
        """Close the current IQ connection so the feed reconnects."""
        with self.lock:
            connection = self.connection
        if connection is not None:
            connection.close()

    def set_buffer_seconds(self, seconds: float) -> None:
        if self.jitter is None:
            return
        with self.jitter_condition:
            self.jitter.set_target(seconds)
            self.jitter_condition.notify_all()

    def set_target_frequency(self, frequency_hz: int) -> None:
        frequency_hz = int(frequency_hz)
        with self.lock:
            if self.params.get("frequency_hz") == frequency_hz:
                return
            self.params["frequency_hz"] = frequency_hz
            connection = self.connection
        if connection is not None:
            try:
                connection.send_json({"type": "retune", "frequency_hz": frequency_hz})
            except Exception as exc:
                LOG.debug("remote SDR retune failed; reconnect will apply it: %s", exc)

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        with self.lock:
            return {
                "name": f"remote:{self.name}",
                "queue_depth": subscriber.qsize() if subscriber is not None else 0,
                "queue_capacity": subscriber.maxsize if subscriber is not None else 0,
                "received_batches": self.received_batches,
                "dropped_batches": self.dropped_batches,
                "connected": self.connected,
                "error": self.error,
                "buffer": self.jitter.stats() if self.jitter is not None else None,
            }

    def _run(self) -> None:
        delay = RECONNECT_MIN_SECONDS
        while not self.stop_event.is_set():
            with self.lock:
                params = dict(self.params)
            try:
                connection = self.client.connect(self.role, **params)
            except Exception as exc:
                with self.lock:
                    self.connected = False
                    self.error = str(exc)
                self.stop_event.wait(delay)
                delay = min(RECONNECT_MAX_SECONDS, delay * 2)
                continue
            delay = RECONNECT_MIN_SECONDS
            with self.lock:
                self.connection = connection
                self.connected = True
                self.error = ""
                self.generation += 1
                if self.jitter is not None:
                    self.jitter.flush()  # continuity is broken; refill before playing
                if self.params != params and "frequency_hz" in self.params:
                    pending_frequency = self.params["frequency_hz"]
                else:
                    pending_frequency = None
            if pending_frequency is not None:
                connection.send_json({"type": "retune", "frequency_hz": pending_frequency})
            try:
                while not self.stop_event.is_set():
                    try:
                        kind, payload = connection.recv_frame(timeout=REMOTE_IQ_IDLE_TIMEOUT_SECONDS)
                    except TimeoutError as exc:
                        raise ConnectionError("no IQ arrived from the remote SDR") from exc
                    if kind != FRAME_IQ:
                        continue
                    _sequence, sample_rate, center, _host_time, _host_generation, samples = decode_iq_frame(payload)
                    with self.lock:
                        generation = self.generation
                    batch = RemoteIqBatch(samples, sample_rate, center, time.monotonic(), generation)
                    if self.jitter is None:
                        self._offer(batch)
                    else:
                        with self.jitter_condition:
                            if batch.source_generation == self.generation:
                                self.received_batches += 1
                                self.jitter.push(batch, samples.size / max(1, sample_rate))
                                self.jitter_condition.notify_all()
            except Exception as exc:
                if not self.stop_event.is_set():
                    LOG.debug("remote IQ feed %s lost: %s", self.name, exc)
                    with self.lock:
                        self.error = str(exc)
            finally:
                with self.lock:
                    self.connection = None
                    self.connected = False
                connection.close()

    def _playout(self) -> None:
        while not self.stop_event.is_set():
            with self.jitter_condition:
                assert self.jitter is not None
                released = self.jitter.pop_due(time.monotonic())
                if not released:
                    wait = self.jitter.seconds_until_due(time.monotonic())
                    self.jitter_condition.wait(0.25 if wait is None else min(0.25, wait))
            for batch in released:
                batch.captured_at = time.monotonic()
                self._offer(batch, count_received=False)

    def _offer(self, batch: RemoteIqBatch, *, count_received: bool = True) -> None:
        with self.lock:
            subscriber = self.subscriber
            if count_received:
                self.received_batches += 1
        if subscriber is None:
            return
        try:
            subscriber.put_nowait(batch)
        except queue.Full:
            try:
                subscriber.get_nowait()
            except queue.Empty:
                pass
            with self.lock:
                self.dropped_batches += 1
            try:
                subscriber.put_nowait(batch)
            except queue.Full:
                pass
