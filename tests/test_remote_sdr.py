from __future__ import annotations

import importlib
import queue
import sys
import tempfile
import threading
import time
import types
import unittest
import unittest.mock
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_remote_sdr():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.remote_sdr")


class FakeSource:
    """Produces a 24 kS/s tone centred on the requested frequency."""

    def __init__(self, sample_rate: int, center_frequency_hz: int, tone_hz: float = 1_000.0) -> None:
        self.sample_rate = sample_rate
        self.center_frequency_hz = center_frequency_hz
        self.tone_hz = tone_hz
        self.phase = 0
        self.closed = threading.Event()
        self.retunes: list[int] = []

    def read(self, timeout: float):
        if self.closed.is_set():
            return None
        time.sleep(0.01)
        count = self.sample_rate // 50
        n = np.arange(self.phase, self.phase + count)
        self.phase += count
        samples = (0.5 * np.exp(2j * np.pi * self.tone_hz * n / self.sample_rate)).astype(np.complex64)
        remote = load_remote_sdr()
        return remote.RemoteIqBatch(samples, self.sample_rate, self.center_frequency_hz)

    def set_frequency(self, frequency_hz: int) -> None:
        self.retunes.append(frequency_hz)
        self.center_frequency_hz = frequency_hz

    def close(self) -> None:
        self.closed.set()


class FakeBackend:
    def __init__(self) -> None:
        self.settings = {"gain": 32.8, "ppm_correction": 0, "bias_tee": False, "alias_filter_strength": 100}
        self.sources: list[FakeSource] = []

    def status(self):
        return {
            "device_name": "RTL-SDR Blog V4",
            "connected": True,
            "capture_active": True,
            "last_batch_age_seconds": 0.1,
            "gain_values": [0.0, 32.8, 49.6],
            "settings": dict(self.settings),
        }

    def update_settings(self, changes):
        if "gain" in changes and changes["gain"] not in (None, 0.0, 32.8, 49.6):
            raise ValueError("unsupported gain")
        self.settings.update(changes)
        return self.status()

    def open_channel(self, frequency_hz, name):
        source = FakeSource(24_000, frequency_hz)
        self.sources.append(source)
        return source

    def open_wideband(self, sample_rate, name):
        source = FakeSource(sample_rate, 162_475_000, tone_hz=10_000.0)
        self.sources.append(source)
        return source


def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition was not met before timeout")


class RemoteSdrNetworkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.remote = load_remote_sdr()

    def announce(self, host_id="h" * 64, tailscale="100.101.1.2", lan=("192.168.1.20",)):
        return {
            "magic": self.remote.REMOTE_SDR_DISCOVERY_MAGIC,
            "type": "announce",
            "v": 1,
            "id": host_id,
            "name": "weather-pi",
            "port": 47432,
            "addresses": {"tailscale": tailscale, "lan": list(lan)},
        }

    def test_host_answering_on_lan_and_tailscale_is_listed_once_via_tailscale(self) -> None:
        found = {}
        for source in ("192.168.1.20", "100.101.1.2", "192.168.1.20"):
            self.remote.merge_announcement(found, self.announce(), source, use_tailscale=True)

        self.assertEqual(len(found), 1)
        host = found["h" * 64]
        self.assertEqual(host["address"], "100.101.1.2")
        self.assertEqual(host["via"], "tailscale")
        self.assertEqual(host["addresses"], ["100.101.1.2", "192.168.1.20"])

    def test_lan_only_answer_still_learns_the_tailscale_address(self) -> None:
        found = {}
        self.remote.merge_announcement(found, self.announce(), "192.168.1.20", use_tailscale=True)

        host = found["h" * 64]
        self.assertEqual(host["address"], "192.168.1.20")  # the address that actually answered
        self.assertIn("100.101.1.2", host["addresses"])

    def test_tailscale_addresses_are_ignored_without_tailscale(self) -> None:
        found = {}
        self.remote.merge_announcement(found, self.announce(), "192.168.1.20", use_tailscale=False)

        self.assertEqual(found["h" * 64]["addresses"], ["192.168.1.20"])

    def test_address_order_prefers_tailscale_then_lan_then_loopback(self) -> None:
        self.assertEqual(
            self.remote.ordered_addresses(["127.0.0.1", "192.168.1.5", "", "100.64.0.9", "192.168.1.5"]),
            ["100.64.0.9", "192.168.1.5", "127.0.0.1"],
        )

    def test_tailscale_peers_are_online_non_mobile_ipv4(self) -> None:
        import json
        import subprocess
        from unittest.mock import patch

        status = {
            "Self": {"TailscaleIPs": ["100.113.206.20"]},
            "Peer": {
                "a": {"Online": True, "OS": "linux", "TailscaleIPs": ["100.105.221.80", "fd7a::1"]},
                "b": {"Online": False, "OS": "linux", "TailscaleIPs": ["100.114.42.106"]},
                "c": {"Online": True, "OS": "iOS", "TailscaleIPs": ["100.95.146.71"]},
                "d": {"Online": True, "OS": "", "TailscaleIPs": ["fd7a::2", "100.67.156.54"]},
            },
        }
        completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(status), stderr="")
        with patch.object(self.remote.subprocess, "run", return_value=completed):
            self.assertEqual(self.remote.tailscale_peer_addresses(), ["100.105.221.80", "100.67.156.54"])
        with patch.object(self.remote.subprocess, "run", side_effect=FileNotFoundError("tailscale")):
            self.assertEqual(self.remote.tailscale_peer_addresses(), [])

    def test_discovery_targets_cover_every_lan_and_tailscale_peers(self) -> None:
        from unittest.mock import patch

        interfaces = [
            self.remote.LocalInterface("wlp2s0", "192.168.1.79", "192.168.1.255", False),
            self.remote.LocalInterface("enp3s0", "10.0.0.5", "10.0.0.255", False),
            self.remote.LocalInterface("tailscale0", "100.113.206.20", "", True),
        ]
        with patch.object(self.remote, "local_interfaces", return_value=interfaces), patch.object(
            self.remote, "tailscale_peer_addresses", return_value=["100.105.221.80"]
        ):
            targets = self.remote.discovery_targets()
            addresses = self.remote.local_addresses()

        self.assertEqual(targets, ["255.255.255.255", "192.168.1.255", "10.0.0.255", "100.105.221.80", "127.0.0.1"])
        self.assertEqual(addresses, {"tailscale": "100.113.206.20", "lan": ["192.168.1.79", "10.0.0.5"]})


class SilentHostTests(unittest.TestCase):
    """A host that loses power never closes its connections; the client must notice anyway."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.remote = load_remote_sdr()

    def wait_until(self, condition, seconds=3.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.02)
        return False

    def test_client_gives_up_on_a_control_connection_that_goes_silent(self) -> None:
        remote = self.remote

        class SilentConnection:
            sock = types.SimpleNamespace(getpeername=lambda: ("192.0.2.10", remote.REMOTE_SDR_TCP_PORT))

            def recv_json(self, timeout=None):
                # Nothing ever arrives and the connection never closes.
                time.sleep(3600 if timeout is None else timeout)
                raise TimeoutError("no remote SDR message arrived in time")

            def close(self):
                pass

        dropped = []

        class Feed:
            def drop_connection(self):
                dropped.append(True)

        with tempfile.TemporaryDirectory() as directory:
            hosts = remote.PairedDeviceStore(Path(directory) / "hosts.json")
            client = remote.RemoteSdrClient("host-id", hosts, client_id="client-id")
            client.connect = lambda role, **params: SilentConnection()
            client.fanouts.add(Feed())
            with (
                unittest.mock.patch.object(remote, "CONTROL_IDLE_TIMEOUT_SECONDS", 0.2),
                self.assertLogs(remote.LOG, level="WARNING") as logs,
            ):
                client.start()
                try:
                    gave_up = self.wait_until(lambda: dropped and not client.status()["reachable"])
                finally:
                    client.stop()

        # The fake reconnects at once, which clears the error, so check what was reported.
        self.assertTrue(gave_up)
        self.assertTrue(any("stopped responding" in line for line in logs.output), logs.output)

    def test_iq_feed_reconnects_when_its_connection_goes_silent(self) -> None:
        remote = self.remote
        connects = []

        class SilentConnection:
            def recv_frame(self, timeout=None):
                time.sleep(3600 if timeout is None else timeout)
                raise TimeoutError("no remote SDR message arrived in time")

            def send_json(self, message):
                pass

            def close(self):
                pass

        client = types.SimpleNamespace(
            connect=lambda role, **params: connects.append(role) or SilentConnection(),
            forget_fanout=lambda fanout: None,
        )
        fanout = remote.RemoteIqFanout(client, remote.REMOTE_SDR_ROLE_CHANNEL, {"frequency_hz": 162_475_000}, name="test")
        with (
            unittest.mock.patch.object(remote, "REMOTE_IQ_IDLE_TIMEOUT_SECONDS", 0.2),
            unittest.mock.patch.object(remote, "RECONNECT_MIN_SECONDS", 0.05),
        ):
            fanout.subscribe()
            try:
                reconnected = self.wait_until(lambda: len(connects) >= 2)
            finally:
                fanout.stop()

        self.assertTrue(reconnected)


class HostStatusOrderTests(unittest.TestCase):
    """The host's regular status and its reply to a change can cross on the way."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.remote = load_remote_sdr()

    def setUp(self) -> None:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        hosts = self.remote.PairedDeviceStore(Path(tempdir.name) / "devices.json")
        self.seen: list[dict] = []
        self.client = self.remote.RemoteSdrClient("host-id", hosts, client_id="client-id", on_status=self.seen.append)

    @staticmethod
    def status(seq, gain):
        status = {"settings": {"gain": gain}}
        if seq is not None:
            status["status_seq"] = seq
        return status

    def receive(self, status) -> None:
        # What the control connection does with a regular status from the host.
        with self.client.lock:
            accepted = self.client._accept_host_status_locked(status)
        if accepted:
            self.client.on_status(status)

    def shown_gain(self):
        return self.client.status()["sdr"]["settings"]["gain"]

    def test_a_status_taken_before_a_change_does_not_put_the_old_value_back(self) -> None:
        self.receive(self.status(4, 32.8))
        with unittest.mock.patch.object(self.client, "_request", return_value=self.status(6, 49.6)):
            self.client.update_settings({"gain": 49.6})
        self.receive(self.status(5, 32.8))  # taken before the change, arrived after the reply

        self.assertEqual(self.shown_gain(), 49.6)
        self.assertEqual([status["status_seq"] for status in self.seen], [4])

    def test_every_newer_status_is_taken(self) -> None:
        # Someone at the host changes gain after this client's change.
        with unittest.mock.patch.object(self.client, "_request", return_value=self.status(6, 49.6)):
            self.client.update_settings({"gain": 49.6})
        for seq, gain in ((7, 49.6), (8, 0.0), (9, 0.0)):
            self.receive(self.status(seq, gain))

        self.assertEqual(self.shown_gain(), 0.0)
        self.assertEqual([status["status_seq"] for status in self.seen], [7, 8, 9])

    def test_hosts_that_do_not_number_their_status_are_always_taken(self) -> None:
        self.receive(self.status(None, 32.8))
        self.receive(self.status(None, 49.6))
        self.receive(self.status(None, 0.0))

        self.assertEqual(self.shown_gain(), 0.0)
        self.assertEqual(len(self.seen), 3)


class IqJitterBufferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.remote = load_remote_sdr()

    def buffer(self, target=2.0, maximum=5.0):
        return self.remote.IqJitterBuffer(target, maximum)

    def test_holds_output_until_the_target_is_buffered(self) -> None:
        buffer = self.buffer()
        for index in range(19):
            buffer.push(index, 0.1)
        self.assertEqual(buffer.pop_due(0.0), [])
        buffer.push(19, 0.1)

        self.assertEqual(buffer.pop_due(0.0), [0])
        self.assertEqual(buffer.stats()["state"], "playing")

    def test_releases_at_real_time_pace_after_a_burst(self) -> None:
        buffer = self.buffer()
        for index in range(20):
            buffer.push(index, 0.1)
        released = buffer.pop_due(0.0)
        # A catch-up burst after a network stall must not flood downstream.
        for index in range(20, 45):
            buffer.push(index, 0.1)
        released += buffer.pop_due(0.05)
        self.assertEqual(len(released), 1)
        released += buffer.pop_due(1.0)

        self.assertEqual(len(released), 11)  # about one second of audio per second

    def test_caps_latency_by_dropping_the_oldest_audio(self) -> None:
        buffer = self.buffer()
        for index in range(60):  # 6 s arriving at once
            buffer.push(index, 0.1)
            self.assertLessEqual(buffer.buffered_seconds, 5.0 + 1e-6)

        # Passing 5 s on the 51st push dropped the oldest 3.1 s back to the
        # 2 s target; the 0.9 s pushed after that is kept.
        self.assertAlmostEqual(buffer.buffered_seconds, 2.9, places=6)
        self.assertAlmostEqual(buffer.stats()["trimmed_seconds"], 3.1, places=3)
        self.assertEqual(buffer.pop_due(0.0), [31])  # oldest dropped, newest kept

    def test_rebuffers_after_running_dry(self) -> None:
        buffer = self.buffer(target=1.0)
        for index in range(10):
            buffer.push(index, 0.1)
        released = buffer.pop_due(0.0) + buffer.pop_due(5.0)  # network out for 5 s
        self.assertEqual(len(released), 10)
        self.assertEqual(buffer.stats()["underruns"], 1)
        buffer.push("late", 0.1)

        self.assertEqual(buffer.pop_due(5.1), [])  # must refill to the target first
        self.assertEqual(buffer.stats()["state"], "buffering")

    def test_flush_discards_and_refills(self) -> None:
        buffer = self.buffer(target=0.5)
        for index in range(10):
            buffer.push(index, 0.1)
        buffer.pop_due(0.0)
        buffer.flush()

        self.assertEqual(buffer.pop_due(10.0), [])
        self.assertEqual(buffer.buffered_seconds, 0.0)

    def test_lowering_the_target_skips_ahead_by_dropping_the_oldest(self) -> None:
        buffer = self.buffer(target=2.0)
        for index in range(20):
            buffer.push(index, 0.1)
        buffer.pop_due(0.0)  # playing; 1.9 s left
        buffer.set_target(0.5)

        self.assertAlmostEqual(buffer.buffered_seconds, 0.5, places=6)
        self.assertEqual(buffer.pop_due(0.2), [15])  # 1..14 skipped, order kept
        self.assertAlmostEqual(buffer.max_seconds, 0.5 + self.remote.REMOTE_IQ_BUFFER_HEADROOM_SECONDS)

    def test_raising_the_target_pauses_until_refilled(self) -> None:
        buffer = self.buffer(target=0.5)
        for index in range(5):
            buffer.push(index, 0.1)
        self.assertEqual(buffer.pop_due(0.0), [0])
        buffer.set_target(1.0)

        self.assertEqual(buffer.pop_due(5.0), [])  # held back while filling
        for index in range(5, 11):
            buffer.push(index, 0.1)
        self.assertEqual(buffer.pop_due(5.0), [1])

    def test_sample_order_survives_any_sequence_of_target_changes(self) -> None:
        buffer = self.buffer(target=0.5)
        released: list[int] = []
        now = 0.0
        next_index = 0
        targets = [0.5, 2.0, 0.1, 1.0, 0.3, 4.0, 0.2]
        for step in range(700):
            for _ in range(2):  # arrives a little faster than real time
                buffer.push(next_index, 0.05)
                next_index += 1
            if step % 100 == 50:
                buffer.set_target(targets[(step // 100) % len(targets)])
            now += 0.09
            released.extend(buffer.pop_due(now))

        self.assertGreater(len(released), 500)
        self.assertTrue(all(later > earlier for earlier, later in zip(released, released[1:])))

    def test_speeds_up_slightly_when_above_target_to_absorb_clock_drift(self) -> None:
        steady = self.buffer()
        ahead = self.buffer()
        for index in range(20):
            steady.push(index, 0.1)
        for index in range(40):  # 4 s buffered: 2 s above target
            ahead.push(index, 0.1)
        steady.pop_due(0.0)
        ahead.pop_due(0.0)

        self.assertAlmostEqual(steady.next_release_at, 0.1 * (1 - 0.01 * -0.1), places=6)
        self.assertLess(ahead.next_release_at, 0.1)
        self.assertGreaterEqual(ahead.next_release_at, 0.1 * (1 - self.remote.REMOTE_IQ_RATE_ADJUST_LIMIT))


class RemoteSdrProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.remote = load_remote_sdr()

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        remote = self.remote
        self.host_identity = remote.load_or_create_identity(root / "host", "weather-pi")
        self.client_identity = remote.load_or_create_identity(root / "client", "studio")
        self.backend = FakeBackend()
        self.server = remote.RemoteSdrServer(
            self.host_identity,
            remote.PairedDeviceStore(root / "host" / "devices.json"),
            self.backend,
            bind_address="127.0.0.1",
            port=0,
            discovery_port=0,
        )
        self.server.start()
        self.addCleanup(self.server.stop)
        self.hosts = remote.PairedDeviceStore(root / "client" / "devices.json")

    def pair(self) -> dict:
        code = self.server.begin_pairing()["code"]
        record = self.remote.pair_with_host(
            "127.0.0.1",
            self.server.port,
            code,
            client_id=self.client_identity.fingerprint,
            client_name="studio",
        )
        self.hosts.add(
            record["id"], record["name"], token=record["token"], peer_token=record["peer_token"],
            address=record["address"], port=record["port"],
        )
        return record

    def client(self):
        client = self.remote.RemoteSdrClient(
            self.host_identity.fingerprint,
            self.hosts,
            client_id=self.client_identity.fingerprint,
            discovery_port=self.server.discovery.port,
            discovery_targets=["127.0.0.1"],
        )
        client.start()
        self.addCleanup(client.stop)
        return client

    # -- building blocks ---------------------------------------------------

    def test_cs16_round_trip_is_within_one_lsb(self) -> None:
        rng = np.random.default_rng(1)
        samples = ((rng.random(4096) - 0.5) + 1j * (rng.random(4096) - 0.5)).astype(np.complex64)
        decoded = self.remote.cs16_to_complex(self.remote.complex_to_cs16(samples))

        self.assertEqual(decoded.dtype, np.complex64)
        self.assertLessEqual(float(np.max(np.abs(decoded - samples))), 1.0 / self.remote.CS16_SCALE)

    def test_cs16_clips_instead_of_wrapping(self) -> None:
        decoded = self.remote.cs16_to_complex(self.remote.complex_to_cs16(np.array([2 - 2j], dtype=np.complex64)))

        self.assertAlmostEqual(float(decoded[0].real), 1.0, places=3)
        self.assertAlmostEqual(float(decoded[0].imag), -1.0, places=3)

    def test_srp_agrees_only_with_the_same_code_and_context(self) -> None:
        remote = self.remote
        for client_code, client_context, should_match in (
            ("123456", b"ctx", True),
            ("123457", b"ctx", False),
            ("123456", b"other", False),
        ):
            with self.subTest(code=client_code, context=client_context):
                host = remote.SrpHost("123456", b"ctx")
                client = remote.SrpClient(client_code, client_context)
                host.process_client(client.public)
                client_proof = client.process_challenge(host.salt, host.public)
                host_proof = host.verify_client(client_proof)
                self.assertEqual(host_proof is not None, should_match)
                if host_proof is not None:
                    self.assertTrue(client.verify_host(host_proof))

    def test_identity_is_stable_and_private(self) -> None:
        again = self.remote.load_or_create_identity(self.host_identity.cert_path.parent, "weather-pi")

        self.assertEqual(again.fingerprint, self.host_identity.fingerprint)
        self.assertEqual(self.host_identity.key_path.stat().st_mode & 0o777, 0o600)

    def test_missing_openssl_is_reported_clearly(self) -> None:
        from unittest.mock import patch

        with patch.object(self.remote.subprocess, "run", side_effect=FileNotFoundError("openssl")):
            with self.assertRaisesRegex(self.remote.RemoteSdrError, "Install the openssl package"):
                self.remote.load_or_create_identity(Path(self.tempdir.name) / "fresh")
        self.assertEqual(list((Path(self.tempdir.name) / "fresh").iterdir()), [])  # nothing half-written

    # -- discovery and pairing ----------------------------------------------

    def discover(self, **kwargs) -> list:
        return self.remote.discover_hosts(port=self.server.discovery.port, targets=["127.0.0.1"], timeout=0.5, **kwargs)

    def test_host_is_hidden_until_it_shows_a_pairing_code(self) -> None:
        self.assertEqual(self.discover(), [])
        self.server.begin_pairing()
        self.assertEqual([host["id"] for host in self.discover()], [self.host_identity.fingerprint])
        self.server.cancel_pairing()
        self.assertEqual(self.discover(), [])

    def test_paired_devices_can_still_find_the_host_but_only_with_a_valid_proof(self) -> None:
        record = self.pair()
        host_id = self.host_identity.fingerprint
        client_id = self.client_identity.fingerprint
        query = self.remote.paired_discovery_query

        self.assertEqual([host["id"] for host in self.discover(paired_with=query(host_id, client_id, record["token"]))], [host_id])
        for wrong in (
            query(host_id, client_id, "not-the-token"),
            query(host_id, "e" * 64, record["token"]),  # not a paired device
            query("f" * 64, client_id, record["token"]),  # asking for another host
            {**query(host_id, client_id, record["token"]), "nonce": "replaced"},
        ):
            with self.subTest(query=wrong):
                self.assertEqual(self.discover(paired_with=wrong), [])

    def test_pairing_ends_when_the_page_showing_the_code_stops_checking_in(self) -> None:
        from unittest.mock import patch

        remote = self.remote
        started = time.time()
        with patch.object(remote.time, "time", return_value=started):
            self.server.begin_pairing()
        with patch.object(remote.time, "time", return_value=started + remote.PAIRING_PAGE_TIMEOUT_SECONDS - 1):
            self.assertTrue(self.server.keep_pairing_open()["active"])
        with patch.object(remote.time, "time", return_value=started + 2 * remote.PAIRING_PAGE_TIMEOUT_SECONDS - 2):
            self.assertTrue(self.server.pairing_status()["active"])
        with patch.object(remote.time, "time", return_value=started + 2 * remote.PAIRING_PAGE_TIMEOUT_SECONDS):
            self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "page-closed"})
            # Checking in again does not bring back a pairing that has ended.
            self.assertFalse(self.server.keep_pairing_open()["active"])

    def test_a_page_that_cannot_check_in_holds_pairing_until_the_code_expires(self) -> None:
        from unittest.mock import patch

        remote = self.remote
        started = time.time()
        with patch.object(remote.time, "time", return_value=started):
            self.server.begin_pairing(held=True)
        with patch.object(remote.time, "time", return_value=started + remote.PAIRING_CODE_TTL_SECONDS - 1):
            self.assertTrue(self.server.pairing_status()["active"])
        with patch.object(remote.time, "time", return_value=started + remote.PAIRING_CODE_TTL_SECONDS):
            self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "expired"})

    def test_discovery_finds_host_by_identity(self) -> None:
        self.server.begin_pairing()
        hosts = self.discover()

        self.assertEqual([host["id"] for host in hosts], [self.host_identity.fingerprint])
        self.assertEqual(hosts[0]["name"], "weather-pi")
        self.assertEqual(hosts[0]["port"], self.server.port)
        self.assertEqual(hosts[0]["sdr"], {"device_name": "RTL-SDR Blog V4", "connected": True})

    def test_pairing_with_the_right_code(self) -> None:
        record = self.pair()

        self.assertEqual(record["id"], self.host_identity.fingerprint)
        self.assertEqual(record["name"], "weather-pi")
        self.assertTrue(record["token"])
        self.assertTrue(record["peer_token"])
        self.assertEqual(
            [client["id"] for client in self.server.devices.public_list()],
            [self.client_identity.fingerprint],
        )
        stored = self.server.devices.path.read_text()
        self.assertNotIn(record["token"], stored)  # each side keeps only a hash of what it accepts
        self.assertIn(record["peer_token"], stored)  # and the token it will present itself
        self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "paired"})

    def test_pairing_rejects_wrong_code_and_closes_after_repeated_failures(self) -> None:
        remote = self.remote
        code = self.server.begin_pairing()["code"]
        wrong = "000000" if code != "000000" else "111111"
        for attempt in range(remote.PAIRING_MAX_ATTEMPTS):
            with self.assertRaisesRegex(remote.RemoteSdrAuthError, "incorrect"):
                remote.pair_with_host("127.0.0.1", self.server.port, wrong, client_id="c" * 64, client_name="x")
        with self.assertRaisesRegex(remote.RemoteSdrAuthError, "not open"):
            remote.pair_with_host("127.0.0.1", self.server.port, code, client_id="c" * 64, client_name="x")
        self.assertEqual(self.server.devices.all(), {})
        self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "too-many-attempts"})

    def test_pairing_mode_ends_after_one_device_pairs(self) -> None:
        remote = self.remote
        code = self.server.begin_pairing()["code"]
        self.assertEqual(self.server.begin_pairing()["code"], code)  # pressing again keeps the code
        remote.pair_with_host("127.0.0.1", self.server.port, code, client_id="a" * 64, client_name="a")
        with self.assertRaisesRegex(remote.RemoteSdrAuthError, "not open"):
            remote.pair_with_host("127.0.0.1", self.server.port, code, client_id="b" * 64, client_name="b")

        self.assertEqual(sorted(self.server.devices.all()), ["a" * 64])
        self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "paired"})

    def test_pairing_code_expires_after_five_minutes(self) -> None:
        from unittest.mock import patch

        remote = self.remote
        started = time.time()
        with patch.object(remote.time, "time", return_value=started):
            code = self.server.begin_pairing()["code"]
            self.assertEqual(self.server.pairing_status()["expires_at"], started + remote.PAIRING_CODE_TTL_SECONDS)
        with patch.object(remote.time, "time", return_value=started + remote.PAIRING_CODE_TTL_SECONDS + 1):
            self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "expired"})
        with self.assertRaisesRegex(remote.RemoteSdrAuthError, "not open"):
            remote.pair_with_host("127.0.0.1", self.server.port, code, client_id="a" * 64, client_name="a")

    def test_stopping_pairing_mode(self) -> None:
        self.server.begin_pairing()
        self.server.cancel_pairing()
        self.assertEqual(self.server.pairing_status(), {"active": False, "ended_reason": "stopped"})

    def test_pairing_requires_an_open_window(self) -> None:
        with self.assertRaisesRegex(self.remote.RemoteSdrAuthError, "not open"):
            self.remote.pair_with_host("127.0.0.1", self.server.port, "123456", client_id="c" * 64, client_name="x")

    def test_pairing_refuses_an_unexpected_host(self) -> None:
        code = self.server.begin_pairing()["code"]
        with self.assertRaisesRegex(self.remote.RemoteSdrAuthError, "different"):
            self.remote.pair_with_host(
                "127.0.0.1", self.server.port, code, client_id="c" * 64, client_name="x", expected_host_id="0" * 64
            )

    def test_one_pairing_works_when_roles_are_reversed(self) -> None:
        remote = self.remote
        self.pair()  # the client paired with the host once
        host_devices = self.server.devices
        # Now the former client shares its SDR and the former host uses it.
        reversed_server = remote.RemoteSdrServer(
            self.client_identity,
            self.hosts,
            FakeBackend(),
            bind_address="127.0.0.1",
            port=0,
            discovery_port=None,
        )
        reversed_server.start()
        self.addCleanup(reversed_server.stop)
        host_devices.update(self.client_identity.fingerprint, address="127.0.0.1", port=reversed_server.port)
        former_host = remote.RemoteSdrClient(
            self.client_identity.fingerprint,
            host_devices,
            client_id=self.host_identity.fingerprint,
            discovery_targets=["127.0.0.1"],
        )
        former_host.start()
        self.addCleanup(former_host.stop)

        wait_for(lambda: former_host.status()["reachable"])
        self.assertEqual(former_host.status()["host_name"], "studio")
        fanout = former_host.channel_fanout(162_550_000, buffer_seconds=0.1)
        self.addCleanup(fanout.stop)
        self.assertEqual(fanout.subscribe(max_chunks=4).get(timeout=5.0).center_frequency_hz, 162_550_000)

    def test_reversed_roles_still_reject_other_tokens(self) -> None:
        remote = self.remote
        record = self.pair()
        reversed_server = remote.RemoteSdrServer(
            self.client_identity, self.hosts, FakeBackend(), bind_address="127.0.0.1", port=0, discovery_port=None
        )
        reversed_server.start()
        self.addCleanup(reversed_server.stop)
        # Presenting the token meant for the other direction must not work.
        self.server.devices.update(
            self.client_identity.fingerprint, address="127.0.0.1", port=reversed_server.port, token=record["token"]
        )
        former_host = remote.RemoteSdrClient(
            self.client_identity.fingerprint, self.server.devices, client_id=self.host_identity.fingerprint
        )
        with self.assertRaisesRegex(remote.RemoteSdrAuthError, "Pair it again"):
            former_host.connect(remote.REMOTE_SDR_ROLE_CONTROL)

    # -- control ----------------------------------------------------------

    def test_control_status_and_settings(self) -> None:
        self.pair()
        client = self.client()
        wait_for(lambda: client.status()["reachable"])

        status = client.status()
        self.assertEqual(status["host_name"], "weather-pi")
        self.assertEqual(status["sdr"]["gain_values"], [0.0, 32.8, 49.6])
        updated = client.update_settings({"gain": 49.6, "bias_tee": True})
        self.assertEqual(updated["settings"]["gain"], 49.6)
        self.assertTrue(self.backend.settings["bias_tee"])
        with self.assertRaisesRegex(ValueError, "unsupported gain"):
            client.update_settings({"gain": 7.0})
        self.assertEqual([item["id"] for item in self.server.connected_clients()], [self.client_identity.fingerprint])

    def test_a_host_that_cannot_reset_says_so_and_stays_connected(self) -> None:
        self.pair()
        client = self.client()
        wait_for(lambda: client.status()["reachable"])

        with self.assertRaisesRegex(ValueError, "unknown request"):
            client.reset_sdr()
        self.assertEqual(client.update_settings({"gain": 49.6})["settings"]["gain"], 49.6)

    def test_unpaired_client_is_rejected(self) -> None:
        self.hosts.add(
            self.host_identity.fingerprint, "weather-pi", token="forged" * 8, peer_token="x" * 43,
            address="127.0.0.1", port=self.server.port,
        )
        client = self.remote.RemoteSdrClient(
            self.host_identity.fingerprint, self.hosts, client_id="intruder", discovery_targets=["127.0.0.1"]
        )
        with self.assertRaisesRegex(self.remote.RemoteSdrAuthError, "Pair it again"):
            client.connect(self.remote.REMOTE_SDR_ROLE_CONTROL)

    def test_revoked_client_loses_access(self) -> None:
        self.pair()
        client = self.client()
        wait_for(lambda: client.status()["reachable"])

        self.assertTrue(self.server.unpair(self.client_identity.fingerprint))
        wait_for(lambda: not client.status()["reachable"])
        with self.assertRaisesRegex(self.remote.RemoteSdrAuthError, "Pair it again"):
            client.connect(self.remote.REMOTE_SDR_ROLE_CONTROL)

    def test_host_found_again_after_its_address_changes(self) -> None:
        record = self.pair()
        self.hosts.update(record["id"], address="127.0.0.2", port=1)  # stale address
        client = self.remote.RemoteSdrClient(
            self.host_identity.fingerprint,
            self.hosts,
            client_id=self.client_identity.fingerprint,
            discovery_port=self.server.discovery.port,
            discovery_targets=["127.0.0.1"],
        )
        connection = client.connect(self.remote.REMOTE_SDR_ROLE_CONTROL)
        connection.close()

        self.assertEqual(self.hosts.get(record["id"])["address"], "127.0.0.1")
        self.assertEqual(self.hosts.get(record["id"])["port"], self.server.port)

    def test_connect_skips_tailscale_addresses_when_tailscale_is_down(self) -> None:
        from unittest.mock import patch

        record = self.pair()
        self.hosts.update(record["id"], addresses=["100.64.0.1", "127.0.0.1"], address="100.64.0.1")
        client = self.remote.RemoteSdrClient(
            self.host_identity.fingerprint, self.hosts, client_id=self.client_identity.fingerprint, discovery_targets=["127.0.0.1"]
        )
        started = time.monotonic()
        with patch.object(self.remote, "tailscale_is_up", return_value=False):
            connection = client.connect(self.remote.REMOTE_SDR_ROLE_CONTROL)
        connection.close()

        self.assertLess(time.monotonic() - started, 2.0)

    def test_after_falling_back_the_working_address_is_tried_first(self) -> None:
        from unittest.mock import patch

        record = self.pair()
        # A Tailscale address that times out (a blackholed TEST-NET address stands in).
        self.hosts.update(record["id"], addresses=["100.64.0.1", "127.0.0.1"], address="100.64.0.1")
        client = self.remote.RemoteSdrClient(
            self.host_identity.fingerprint, self.hosts, client_id=self.client_identity.fingerprint, discovery_targets=["127.0.0.1"]
        )
        attempts: list[str] = []
        original = self.remote.open_host_connection

        def recording_open(address, port, **kwargs):
            attempts.append(address)
            if address == "100.64.0.1":
                raise TimeoutError("timed out")
            return original(address, port, **kwargs)

        with patch.object(self.remote, "tailscale_is_up", return_value=True), patch.object(self.remote, "open_host_connection", recording_open):
            client.connect(self.remote.REMOTE_SDR_ROLE_CONTROL).close()
            first = list(attempts)
            attempts.clear()
            client.connect(self.remote.REMOTE_SDR_ROLE_CONTROL).close()

        self.assertEqual(first, ["100.64.0.1", "127.0.0.1"])
        self.assertEqual(attempts, ["127.0.0.1"])  # no second timeout on the dead address

    def test_pairing_shares_both_machines_addresses(self) -> None:
        from unittest.mock import patch

        with patch.object(
            self.remote, "local_addresses", return_value={"tailscale": "100.99.1.1", "lan": ["192.168.1.30"]}
        ):
            self.pair()
        stored = self.server.devices.get(self.client_identity.fingerprint)

        self.assertEqual(stored["addresses"], ["100.99.1.1", "192.168.1.30", "127.0.0.1"])

    # -- IQ feeds ---------------------------------------------------------

    def test_channel_feed_delivers_cs16_iq_and_retunes(self) -> None:
        self.pair()
        client = self.client()
        fanout = client.channel_fanout(162_550_000, name="KEC49", buffer_seconds=0.1)
        subscriber = fanout.subscribe(max_seconds=1.0)
        self.addCleanup(fanout.stop)

        batch = subscriber.get(timeout=5.0)
        self.assertEqual(batch.sample_rate, 24_000)
        self.assertEqual(batch.center_frequency_hz, 162_550_000)
        self.assertEqual(batch.data.dtype, np.complex64)
        self.assertAlmostEqual(float(np.mean(np.abs(batch.data))), 0.5, places=3)
        spectrum = np.abs(np.fft.fft(batch.data))
        peak_hz = np.fft.fftfreq(batch.data.size, 1 / 24_000)[int(np.argmax(spectrum))]
        self.assertAlmostEqual(peak_hz, 1_000.0, delta=60.0)

        generation = batch.source_generation
        wait_for(lambda: fanout.jitter.stats()["state"] == "playing")
        fanout.set_target_frequency(162_400_000)
        wait_for(lambda: self.backend.sources[-1].retunes == [162_400_000])
        deadline = time.time() + 5.0
        while time.time() < deadline:
            batch = subscriber.get(timeout=5.0)
            if batch.center_frequency_hz == 162_400_000:
                break
        self.assertEqual(batch.center_frequency_hz, 162_400_000)
        # A retune continues the same sample stream: the buffered audio of the
        # old channel plays out instead of being thrown away, so there is no gap.
        self.assertEqual(batch.source_generation, generation)
        self.assertEqual(fanout.jitter.stats()["state"], "playing")
        self.assertEqual(fanout.jitter.stats()["underruns"], 0)
        self.assertEqual(len(self.backend.sources), 1)  # retuned in place, not reopened

    def test_channel_feed_prebuffers_then_plays_in_real_time(self) -> None:
        self.pair()
        client = self.client()
        fanout = client.channel_fanout(162_550_000, buffer_seconds=1.0)
        subscriber = fanout.subscribe(max_seconds=2.0)
        self.addCleanup(fanout.stop)
        started = time.monotonic()
        first = subscriber.get(timeout=10.0)
        prebuffer_seconds = time.monotonic() - started

        audio_seconds = first.data.size / first.sample_rate
        window_started = time.monotonic()
        while time.monotonic() - window_started < 2.0:
            batch = subscriber.get(timeout=5.0)
            audio_seconds += batch.data.size / batch.sample_rate
        elapsed = time.monotonic() - window_started

        self.assertGreaterEqual(prebuffer_seconds, 0.45)  # the fake host sends 2x real time
        # The fake host produces IQ twice as fast as real time; playout must not.
        self.assertAlmostEqual(audio_seconds / elapsed, 1.0, delta=0.1)
        stats = fanout.subscriber_stats(subscriber)["buffer"]
        self.assertEqual(stats["state"], "playing")
        self.assertLessEqual(stats["buffered_seconds"], 1.0 + self.remote.REMOTE_IQ_BUFFER_HEADROOM_SECONDS)

    def stalled_feed_gaps(self, buffer_seconds: float) -> float:
        """Largest gap between batches reaching a consumer across a 1.5 s host stall."""
        backend = self.backend
        original_open = backend.open_channel

        def open_channel(frequency_hz, name):
            source = original_open(frequency_hz, name)
            source.sample_rate = 24_000
            original_read = source.read
            reads = {"count": 0}

            def read(timeout):
                reads["count"] += 1
                if reads["count"] == 300:  # ~3 s of real-time IQ in, the network stalls
                    time.sleep(1.5)
                time.sleep(0.01)  # otherwise real time: 20 ms of IQ every ~20 ms
                return original_read(timeout)

            source.read = read
            return source

        backend.open_channel = open_channel
        self.addCleanup(setattr, backend, "open_channel", original_open)
        self.pair()
        client = self.client()
        fanout = client.channel_fanout(162_550_000, buffer_seconds=buffer_seconds)
        subscriber = fanout.subscribe(max_seconds=2.0)
        self.addCleanup(fanout.stop)
        subscriber.get(timeout=10.0)
        largest_gap = 0.0
        last = time.monotonic()
        deadline = last + 7.0
        while time.monotonic() < deadline:
            try:
                subscriber.get(timeout=3.0)
            except queue.Empty:
                break
            now = time.monotonic()
            largest_gap = max(largest_gap, now - last)
            last = now
        return largest_gap

    def test_buffer_rides_out_a_network_stall(self) -> None:
        self.assertLess(self.stalled_feed_gaps(2.0), 0.3)

    def test_without_buffering_the_same_stall_leaves_a_gap(self) -> None:
        self.assertGreater(self.stalled_feed_gaps(0.05), 1.0)

    def test_wideband_feed_carries_requested_rate(self) -> None:
        self.pair()
        client = self.client()
        fanout = client.wideband_fanout(192_000, name="recording")
        subscriber = fanout.subscribe(max_chunks=8)
        self.addCleanup(fanout.stop)

        batch = subscriber.get(timeout=5.0)
        self.assertEqual(batch.sample_rate, 192_000)
        self.assertEqual(batch.center_frequency_hz, 162_475_000)
        self.assertEqual(batch.data.size, 192_000 // 50)

    def test_stopping_a_feed_closes_the_host_source(self) -> None:
        self.pair()
        client = self.client()
        fanout = client.channel_fanout(162_550_000, buffer_seconds=0.1)
        subscriber = fanout.subscribe(max_chunks=4)
        subscriber.get(timeout=5.0)

        fanout.unsubscribe(subscriber)
        wait_for(lambda: self.backend.sources[-1].closed.is_set())

    def test_feed_reconnects_after_host_restart(self) -> None:
        self.pair()
        client = self.client()
        fanout = client.channel_fanout(162_550_000, buffer_seconds=0.1)
        subscriber = fanout.subscribe(max_chunks=4)
        self.addCleanup(fanout.stop)
        first = subscriber.get(timeout=5.0)

        port = self.server.port
        self.server.stop()
        wait_for(lambda: not fanout.connected)
        restarted = self.remote.RemoteSdrServer(
            self.host_identity,
            self.server.devices,
            self.backend,
            bind_address="127.0.0.1",
            port=port,
            discovery_port=None,
        )
        restarted.start()
        self.addCleanup(restarted.stop)
        while True:
            batch = subscriber.get(timeout=20.0)
            if batch.source_generation > first.source_generation:
                break
        self.assertEqual(batch.center_frequency_hz, 162_550_000)


    def test_a_restarted_host_numbering_its_status_afresh_is_listened_to(self) -> None:
        class NumberedBackend(FakeBackend):
            def __init__(self, first_seq):
                super().__init__()
                self.seq = first_seq

            def status(self):
                self.seq += 1
                return {**super().status(), "status_seq": self.seq}

        self.server.stop()
        old_host = self.remote.RemoteSdrServer(
            self.host_identity, self.server.devices, NumberedBackend(5000),
            bind_address="127.0.0.1", port=0, discovery_port=0,
        )
        old_host.start()
        self.addCleanup(old_host.stop)
        self.server = old_host
        self.pair()
        client = self.client()
        wait_for(lambda: client.status()["sdr"].get("status_seq", 0) > 5000)

        port = old_host.port
        old_host.stop()
        wait_for(lambda: not client.status()["reachable"], timeout=30.0)
        restarted_backend = NumberedBackend(0)
        restarted_backend.settings["gain"] = 49.6
        restarted = self.remote.RemoteSdrServer(
            self.host_identity, old_host.devices, restarted_backend,
            bind_address="127.0.0.1", port=port, discovery_port=None,
        )
        restarted.start()
        self.addCleanup(restarted.stop)
        wait_for(lambda: client.status()["sdr"]["settings"]["gain"] == 49.6, timeout=30.0)


def load_web_control():
    import importlib.util

    load_remote_sdr()
    spec = importlib.util.spec_from_file_location("nwr_stream_manager.web_control", PACKAGE_PATH / "web_control.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.web_control"] = module
    spec.loader.exec_module(module)
    return module


def part_of(wc, name):
    """The module that holds part of web_control, for swapping out something only it uses."""
    return sys.modules[f"{wc.__package__}.{name}"]


class RemoteSdrServiceTests(unittest.TestCase):
    """Two full NWR Stream Manager services: one sharing an SDR, one using it."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control()

    def setUp(self) -> None:
        wc = self.wc
        # Neither service may touch a real RTL-SDR on this machine.
        for module, name in (
            (wc, "list_rtl_devices"),
            (wc, "list_usb_rtl_devices"),
            (part_of(wc, "service_devices"), "list_usb_rtl_devices"),
        ):
            self.addCleanup(setattr, module, name, getattr(module, name))
            setattr(module, name, lambda: [])
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def service(self, name: str, *, development: bool = False):
        wc = self.wc
        state = self.root / name
        state.mkdir(parents=True, exist_ok=True)
        service = wc.RtlControlService(state / "rtl-control.json", wc.RingLogHandler(), development_iq_sources_enabled=development)
        service.remote_sdr_port = 0
        service.remote_sdr_discovery_port = 0
        self.addCleanup(service.close)
        return service

    def synthetic_source(self, service) -> None:
        sample_rate = 192_000
        count = sample_rate * 30
        t = np.arange(count) / sample_rate
        rng = np.random.default_rng(5)
        noise_density = 10.0 ** (-100.0 / 10.0)
        power = noise_density * 16_000 * 10.0 ** (25.0 / 10.0)
        phase = np.cumsum(2 * np.pi * 5_000 / sample_rate * np.sin(2 * np.pi * 1_000 * t))
        iq = np.sqrt(power) * np.exp(1j * (phase + 2 * np.pi * 75_000 * t))
        sigma = np.sqrt(noise_density * sample_rate / 2)
        iq = iq + rng.normal(0, sigma, count) + 1j * rng.normal(0, sigma, count)
        directory = service.iq_test_sources_directory
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "nwr.cf32").write_bytes(iq.astype(np.complex64).tobytes())
        service.start_iq_test_source({"file_name": "nwr.cf32", "sample_rate": sample_rate})

    def test_client_streams_from_a_paired_host(self) -> None:
        host = self.service("host", development=True)
        self.synthetic_source(host)
        host.update({"serial": "00000001", "remote_access_enabled": True})
        self.assertIsNotNone(host.remote_server)
        code = host.start_remote_sdr_pairing()["sharing"]["pairing"]["code"]

        client = self.service("client")
        client.remote_sdr_discovery_port = host.remote_server.discovery.port
        hosts = client.discover_remote_sdrs()["hosts"]
        self.assertEqual([item["id"] for item in hosts], [host.remote_identity.fingerprint])
        self.assertFalse(hosts[0]["paired"])
        client.pair_remote_sdr({"address": "127.0.0.1", "port": host.remote_server.port, "code": code, "host_id": hosts[0]["id"]})
        client.update({"source_mode": "remote", "remote_host_id": hosts[0]["id"]})

        with client.lock:
            client.streams = [{
                "id": "s1",
                "enabled": True,
                "station": {"callsign": "KEC49", "frequency": "162.550"},
                "outputs": [],
                "eas_recording": {"enabled": True},
            }]
            client._sync_stream_workers_locked()
        wait_for(lambda: (client.status()["stream_signals"].get("s1") or {}).get("available"), timeout=20.0)
        time.sleep(4.0)  # let the noise floor settle
        status = client.status()

        self.assertEqual(status["source"]["kind"], "remote")
        self.assertTrue(status["active"])
        self.assertAlmostEqual(status["stream_signals"]["s1"]["snr_db"], 25.0, delta=2.0)
        devices = host.remote_sdr_status()["devices"]
        self.assertEqual([item["id"] for item in devices], [client.remote_identity.fingerprint])
        self.assertTrue(devices[0]["using_this_sdr"])
        self.assertTrue(client.remote_sdr_status()["devices"][0]["in_use"])

        client.update({"alias_filter_strength": 60})
        self.assertEqual(host.settings.alias_filter_strength, 60)
        self.assertEqual(client.settings.alias_filter_strength, 100)  # local SDR setting untouched
        self.assertEqual(client.status()["settings"]["alias_filter_strength"], 60)

        client.start_iq_recording({"mode": "spectrum", "sample_rate": 192_000, "duration_seconds": 0})
        time.sleep(2.0)
        client.stop_iq_recording()
        recording = client.iq_recordings()["recordings"][0]
        self.assertEqual(recording["sample_rate"], 192_000)

        # The weather radio receiver plays from the remote SDR too.
        source, _events, _status = client.open_receiver_audio_source(client_id="phone", frequency_hz=162_550_000)
        wait_for(lambda: client.receiver_status("phone").get("last_audio_at"), timeout=15.0)
        time.sleep(1.0)  # let the network buffer fill
        client.tune_receiver({"client_id": "phone", "frequency_hz": 162_475_000})
        # Changing channels must not interrupt the audio while the new
        # channel crosses the network buffer.
        longest_gap = 0.0
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            last_audio_at = client.receiver_status("phone").get("last_audio_at") or 0.0
            longest_gap = max(longest_gap, time.time() - last_audio_at)
            time.sleep(0.01)
        self.assertLess(longest_gap, 0.25)
        client.stop_receiver({"client_id": "phone"})

    def test_receiver_and_streams_survive_source_switches(self) -> None:
        hosts = []
        for name in ("host-a", "host-b"):
            host = self.service(name, development=True)
            self.synthetic_source(host)
            host.update({"serial": "00000001", "remote_access_enabled": True})
            hosts.append(host)
        client = self.service("client", development=True)
        for host in hosts:
            code = host.start_remote_sdr_pairing()["sharing"]["pairing"]["code"]
            client.pair_remote_sdr({"address": "127.0.0.1", "port": host.remote_server.port, "code": code})
        client.update({"serial": "00000001"})
        self.synthetic_source(client)  # this machine's own SDR

        with client.lock:
            client.streams = [{
                "id": "s1", "enabled": True, "station": {"callsign": "KEC49", "frequency": "162.550"},
                "outputs": [], "eas_recording": {"enabled": True},
            }]
            client._sync_stream_workers_locked()
        stream = client.stream_workers[next(iter(client.stream_workers))]
        client.open_receiver_audio_source(client_id="phone", frequency_hz=162_550_000)
        receiver = client.receiver_workers["phone"]

        def playing() -> bool:
            started = time.time()
            wait_for(
                lambda: (client.receiver_status("phone").get("last_audio_at") or 0) > started
                and (stream.last_audio_at or 0) > started,
                timeout=15.0,
            )
            return True

        self.assertTrue(playing())
        switches = [
            {"source_mode": "remote", "remote_host_id": hosts[0].remote_identity.fingerprint},
            {"source_mode": "remote", "remote_host_id": hosts[1].remote_identity.fingerprint},
            {"source_mode": "local", "serial": "00000001"},
        ]
        for switch in switches:
            with self.subTest(switch=switch):
                client.update(switch)
                if switch["source_mode"] == "local":
                    self.synthetic_source(client)
                self.assertIs(client.receiver_workers.get("phone"), receiver)
                self.assertIs(client.stream_workers.get(next(iter(client.stream_workers))), stream)
                self.assertTrue(playing())
        users = [host.remote_sdr_status()["devices"][0]["using_this_sdr"] for host in hosts]
        self.assertEqual(users, [False, False])  # the remote feeds were released
        client.stop_receiver({"client_id": "phone"})

    def test_roles_reverse_without_pairing_again(self) -> None:
        a = self.service("a", development=True)
        b = self.service("b", development=True)
        for service in (a, b):
            self.synthetic_source(service)
        a.update({"serial": "00000001", "remote_access_enabled": True})
        code = a.start_remote_sdr_pairing()["sharing"]["pairing"]["code"]
        b.pair_remote_sdr({"address": "127.0.0.1", "port": a.remote_server.port, "code": code})
        a_id, b_id = a.remote_identity.fingerprint, b.remote_identity.fingerprint
        b.update({"source_mode": "remote", "remote_host_id": a_id})
        wait_for(lambda: b.status()["active"], timeout=15.0)

        # Swap: B shares its own SDR and A uses it, with no new pairing code.
        b.update({"source_mode": "local", "serial": "00000002", "remote_access_enabled": True})
        self.assertIsNotNone(b.remote_server)
        a.remote_sdr_discovery_port = b.remote_server.discovery.port
        a.update({"remote_access_enabled": False, "source_mode": "remote", "remote_host_id": b_id})
        wait_for(lambda: a.status()["active"], timeout=20.0)

        self.assertEqual(a.status()["source"]["kind"], "remote")
        self.assertEqual([device["id"] for device in a.remote_sdr_status()["devices"]], [b_id])
        self.assertEqual([device["id"] for device in b.remote_sdr_status()["devices"]], [a_id])
        wait_for(lambda: b.remote_sdr_status()["devices"][0]["using_this_sdr"], timeout=5.0)

    def test_settings_rules_for_remote_mode(self) -> None:
        client = self.service("client")
        with self.assertRaisesRegex(ValueError, "Pair with a remote SDR"):
            client.update({"source_mode": "remote", "remote_host_id": "unknown"})
        with self.assertRaisesRegex(ValueError, "connected to this machine"):
            client.update({"remote_access_enabled": True})
        client.remote_devices.add("h" * 64, "weather-pi", token="t" * 43, peer_token="p" * 43, address="127.0.0.1", port=1)
        client.update({"source_mode": "remote", "remote_host_id": "h" * 64})

        self.assertTrue(client.settings.uses_remote_sdr)
        self.assertFalse(client.settings.remote_access_enabled)
        with self.assertRaisesRegex(ValueError, "Switch to another SDR"):
            client.unpair_remote_sdr_device({"device_id": "h" * 64})
        status = client.status()
        self.assertEqual(status["source"]["kind"], "remote")
        self.assertFalse(status["active"])
        self.assertIn("remote_sdr", status)
        self.assertEqual(client.status(read_only=True)["remote_sdr"], {})  # pairing codes stay private

    def test_switching_to_a_remote_sdr_keeps_the_hosts_settings(self) -> None:
        host = self.service("host", development=True)
        self.synthetic_source(host)
        host.update({"serial": "00000001", "remote_access_enabled": True, "gain": None, "ppm_correction": 5})
        code = host.start_remote_sdr_pairing()["sharing"]["pairing"]["code"]
        client = self.service("client")
        client.pair_remote_sdr({"address": "127.0.0.1", "port": host.remote_server.port, "code": code})
        # The page sends every field; these are the client's local values.
        client.update({
            "source_mode": "remote",
            "remote_host_id": host.remote_identity.fingerprint,
            "gain": 40.2,
            "ppm_correction": -3,
            "bias_tee": True,
            "alias_filter_strength": 20,
        })

        self.assertIsNone(host.settings.gain)
        self.assertEqual(host.settings.ppm_correction, 5)
        self.assertFalse(host.settings.bias_tee)
        self.assertEqual(host.settings.alias_filter_strength, 100)

    def test_the_host_numbers_each_status_it_sends(self) -> None:
        # Clients rely on a status taken after a change having a higher number.
        host = self.service("host", development=True)
        self.synthetic_source(host)
        before = host.remote_host_status()
        host.update({"ppm_correction": 7})
        after = host.remote_host_status()

        self.assertGreater(after["status_seq"], before["status_seq"])
        self.assertEqual(after["settings"]["ppm_correction"], 7)

    def paired_host_and_client(self):
        host = self.service("host", development=True)
        self.synthetic_source(host)
        host.update({"serial": "00000001", "remote_access_enabled": True})
        code = host.start_remote_sdr_pairing()["sharing"]["pairing"]["code"]
        client = self.service("client")
        client.pair_remote_sdr({"address": "127.0.0.1", "port": host.remote_server.port, "code": code})
        client.update({"source_mode": "remote", "remote_host_id": host.remote_identity.fingerprint})
        wait_for(lambda: client.status()["source"].get("reachable"), timeout=10.0)
        return host, client

    def test_sdr_settings_changes_reach_the_other_machine_at_once(self) -> None:
        remote = sys.modules["nwr_stream_manager.remote_sdr"]
        # With the host's regular status this rare, only an immediate one can arrive in time.
        with unittest.mock.patch.object(remote, "CONTROL_STATUS_INTERVAL_SECONDS", 30.0):
            host, client = self.paired_host_and_client()

            seen = client.settings_change_count
            host.update({"ppm_correction": 7})
            wait_for(lambda: client.status()["settings"]["ppm_correction"] == 7, timeout=1.0)
            wait_for(lambda: client.settings_change_count > seen, timeout=1.0)  # its browsers are told

            seen = host.settings_change_count
            client.update({"ppm_correction": -4})
            self.assertEqual(host.settings.ppm_correction, -4)
            self.assertGreater(host.settings_change_count, seen)

    def fake_usb_reset(self, *, error: Exception | None = None, reappears: bool = True):
        """Stand in for the USB reset; it finishes when the returned event is set."""
        wc = self.wc
        device = types.SimpleNamespace(serial="00000001", description="RTL-SDR Blog V4", vendor_id="0bda", product_id="2838")
        release = threading.Event()
        resets = []

        def reset(serial, timeout_seconds):
            resets.append(serial)
            release.wait(10.0)
            if error is not None:
                raise error
            return device, "usbreset"

        for name, value in (
            ("reset_usb_rtl_device", reset),
            ("list_usb_rtl_devices", lambda: [device] if reappears and release.is_set() else []),
            ("RTL_RESET_REAPPEAR_TIMEOUT_SECONDS", 0.5),
        ):
            devices = part_of(wc, "service_devices")
            original = getattr(devices, name)
            setattr(devices, name, value)
            self.addCleanup(setattr, devices, name, original)
        return release, resets

    def reset_in_background(self, service, serial=""):
        outcome = {}

        def run():
            try:
                outcome["result"] = service.reset_rtl_device(serial)
            except Exception as exc:
                outcome["error"] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    @staticmethod
    def shown_reset(service):
        reset = service.status()["sdr_reset"]
        return (reset["state"], reset["message"]) if reset else None

    def test_a_client_resets_the_hosts_sdr_and_both_show_it(self) -> None:
        host, client = self.paired_host_and_client()
        release, resets = self.fake_usb_reset()

        thread, outcome = self.reset_in_background(client)
        wait_for(lambda: self.shown_reset(client) == ("resetting", "Resetting RTL-SDR..."))
        self.assertEqual(self.shown_reset(host), ("resetting", "Resetting RTL-SDR..."))
        self.assertEqual(resets, ["00000001"])  # the host reset its own SDR

        release.set()
        thread.join(10.0)
        done = "RTL-SDR serial 00000001 was reset using usbreset and reappeared."
        self.assertNotIn("error", outcome)
        self.assertTrue(outcome["result"]["success"])
        self.assertEqual(outcome["result"]["message"], done)
        self.assertEqual(self.shown_reset(host), ("finished", done))
        self.assertEqual(self.shown_reset(client), ("finished", done))

    def test_a_failed_reset_on_the_host_is_reported_on_the_client(self) -> None:
        host, client = self.paired_host_and_client()
        release, _ = self.fake_usb_reset(error=RuntimeError("no USB device found for RTL-SDR serial 00000001"))
        release.set()

        result = client.reset_rtl_device("")
        self.assertFalse(result["success"])
        self.assertEqual(result["message"], "no USB device found for RTL-SDR serial 00000001")
        self.assertEqual(self.shown_reset(host), ("failed", result["message"]))
        self.assertEqual(self.shown_reset(client), ("failed", result["message"]))

    def test_an_sdr_that_does_not_come_back_counts_as_a_failed_reset(self) -> None:
        host, client = self.paired_host_and_client()
        release, _ = self.fake_usb_reset(reappears=False)
        release.set()

        result = client.reset_rtl_device("")
        self.assertFalse(result["success"])
        self.assertIn("did not reappear", result["message"])
        self.assertEqual(self.shown_reset(client), ("failed", result["message"]))

    def test_a_reset_started_on_the_host_shows_on_the_client(self) -> None:
        remote = sys.modules["nwr_stream_manager.remote_sdr"]
        # With the host's regular status this rare, only an immediate one can arrive in time.
        patcher = unittest.mock.patch.object(remote, "CONTROL_STATUS_INTERVAL_SECONDS", 30.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        host, client = self.paired_host_and_client()
        release, _ = self.fake_usb_reset()

        seen = client.settings_change_count
        thread, outcome = self.reset_in_background(host, "00000001")
        wait_for(lambda: self.shown_reset(client) == ("resetting", "Resetting RTL-SDR..."), timeout=1.0)
        wait_for(lambda: client.settings_change_count > seen, timeout=1.0)  # the client's pages are told at once
        with self.assertRaisesRegex(ValueError, "already in progress"):
            client.reset_rtl_device("")  # one reset at a time, from anywhere
        release.set()
        thread.join(10.0)
        self.assertTrue(outcome["result"]["success"])
        wait_for(lambda: (self.shown_reset(client) or ("",))[0] == "finished", timeout=1.0)

    def test_how_a_reset_ended_stops_showing_after_a_while(self) -> None:
        host, client = self.paired_host_and_client()
        release, _ = self.fake_usb_reset()
        release.set()
        client.reset_rtl_device("")
        self.assertIsNotNone(self.shown_reset(client))
        with unittest.mock.patch.object(part_of(self.wc, "service_devices"), "RTL_RESET_RESULT_SHOW_SECONDS", 0.0):
            self.assertIsNone(self.shown_reset(host))
            wait_for(lambda: self.shown_reset(client) is None, timeout=3.0)

    def test_resetting_through_an_older_host_explains_why_it_cannot(self) -> None:
        host, client = self.paired_host_and_client()
        with unittest.mock.patch.object(client.remote_client, "reset_sdr", side_effect=ValueError("unknown request 'reset_sdr'")):
            with self.assertRaisesRegex(ValueError, "needs to be updated before its SDR can be reset from here"):
                client.reset_rtl_device("")

    def test_browsers_are_told_when_sdr_settings_change(self) -> None:
        import base64
        import os
        import socket
        import struct
        import threading

        wc = self.wc
        service = self.service("web")
        service.accounts.create_admin("tester", "password123", "password123")
        handler = type("SettingsEventsTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)

        sock = socket.create_connection(server.server_address, timeout=5.0)
        self.addCleanup(sock.close)
        sock.sendall((
            "GET /api/settings-events HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.server_address[1]}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n"
            f"Authorization: Basic {base64.b64encode(b'tester:password123').decode()}\r\n\r\n"
        ).encode())
        reader = sock.makefile("rb")
        self.assertIn(b" 101 ", reader.readline())
        while reader.readline() not in (b"\r\n", b""):
            pass

        def next_frame():
            opcode, length = reader.read(2)
            length &= 0x7F
            if length == 126:
                length = struct.unpack("!H", reader.read(2))[0]
            return opcode & 0x0F, reader.read(length)

        service.update({"ppm_correction": 3})
        self.assertEqual(next_frame(), (0x1, b'{"type":"settings_changed"}'))
        service.begin_shutdown()
        opcode, payload = next_frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1012)

    def live_audio_server(self, service):
        import threading

        wc = self.wc
        service.accounts.create_admin("tester", "password123", "password123")
        handler = type("LiveAudioTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return server

    def listen(self, server, query: str):
        """Connect the way a page does; returns a function giving each next message."""
        import base64
        import os
        import socket
        import struct

        sock = socket.create_connection(server.server_address, timeout=5.0)
        self.addCleanup(sock.close)
        sock.sendall((
            f"GET /api/live-audio?{query} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{server.server_address[1]}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}\r\n"
            f"Authorization: Basic {base64.b64encode(b'tester:password123').decode()}\r\n\r\n"
        ).encode())
        reader = sock.makefile("rb")
        self.assertIn(b" 101 ", reader.readline())
        while reader.readline() not in (b"\r\n", b""):
            pass

        def next_message():
            header = reader.read(2)
            if len(header) < 2:
                return None
            opcode, length = header
            length &= 0x7F
            if length == 126:
                length = struct.unpack("!H", reader.read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", reader.read(8))[0]
            return opcode & 0x0F, reader.read(length)

        return sock, next_message

    def test_a_page_that_reconnects_keeps_listening(self) -> None:
        # A phone switching from Wi-Fi to cellular comes back on a new connection
        # before the server has finished with the old one.
        service = self.service("web", development=True)
        self.synthetic_source(service)
        service.update({"serial": "00000001"})
        server = self.live_audio_server(service)
        query = "mode=receiver&client_id=phone&frequency_hz=162550000&codec=pcm"

        def audio_keeps_coming(next_message, seconds: float) -> int:
            frames = 0
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                message = next_message()
                self.assertIsNotNone(message, "the connection was closed")
                if message[0] == 0x2:
                    frames += 1
            return frames

        old_sock, old_next = self.listen(server, query)
        self.assertGreater(audio_keeps_coming(old_next, 1.0), 10)
        _new_sock, new_next = self.listen(server, query)
        old_sock.close()
        self.assertGreater(audio_keeps_coming(new_next, 3.0), 100)
        self.assertIn("phone", service.receiver_workers)

    def test_a_quiet_connection_still_hears_from_the_server(self) -> None:
        import socket

        service = self.service("web")
        server = self.live_audio_server(service)
        # No SDR: the receiver has no audio to send.
        service._current_iq_source = lambda: object()
        service._channel_feed = lambda frequency_hz, name: self.wc.NullIqFanout()
        sock, next_message = self.listen(server, "mode=receiver&client_id=phone&frequency_hz=162550000&codec=pcm")
        sock.settimeout(self.wc.LIVE_AUDIO_KEEPALIVE_SECONDS + 1.0)
        seen = []
        deadline = time.monotonic() + 3.5
        while time.monotonic() < deadline:
            try:
                message = next_message()
            except socket.timeout:
                self.fail(f"nothing arrived for over {self.wc.LIVE_AUDIO_KEEPALIVE_SECONDS + 1.0} s; saw {seen}")
            seen.append(message[1])
        self.assertIn(b'{"type":"source_no_audio"}', seen)
        self.assertGreaterEqual(seen.count(b'{"type":"keepalive"}'), 2)

    def test_connections_to_vanished_devices_are_dropped(self) -> None:
        import socket

        options = {}
        connection = types.SimpleNamespace(setsockopt=lambda level, option, value: options.__setitem__((level, option), value))
        self.wc.LiveAudioWebSocketWriter(types.SimpleNamespace(connection=connection))._configure_socket()
        if not hasattr(socket, "TCP_USER_TIMEOUT"):
            self.skipTest("this system cannot limit unacknowledged data")
        self.assertEqual(options[(socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT)], 10_000)

    def test_remote_sdr_endpoints_are_reachable_over_http(self) -> None:
        import json
        import threading
        import urllib.request

        wc = self.wc
        service = self.service("web")
        service.accounts.create_admin("tester", "password123", "password123")
        handler = type("RemoteSdrTestHandler", (wc.RtlControlHandler,), {"service": service})
        server = wc.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        import base64
        auth = "Basic " + base64.b64encode(b"tester:password123").decode()

        def call(method: str, path: str, payload=None):
            data = None if payload is None else json.dumps(payload).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}{path}",
                data=data,
                method=method,
                headers={"Authorization": auth, "Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read())

        status, body = call("GET", "/api/remote-sdr")
        self.assertEqual(status, 200)
        self.assertEqual(body["identity"]["id"], service.remote_identity.fingerprint)
        for path, expected_error in (
            ("/api/remote-sdr/discover", None),
            ("/api/remote-sdr/pairing/start", "Turn on sharing"),
            ("/api/remote-sdr/pairing/cancel", None),
            ("/api/remote-sdr/pair", "Select an NWR Stream Manager"),
            ("/api/remote-sdr/unpair", None),
        ):
            with self.subTest(path=path):
                status, body = call("POST", path, {})
                if expected_error:
                    self.assertEqual(status, 400)
                    self.assertIn(expected_error, body["error"])
                else:
                    self.assertEqual(status, 200, body)

    def test_network_buffer_setting_defaults_validates_and_applies_live(self) -> None:
        host = self.service("host", development=True)
        self.synthetic_source(host)
        host.update({"serial": "00000001", "remote_access_enabled": True})
        code = host.start_remote_sdr_pairing()["sharing"]["pairing"]["code"]
        client = self.service("client")
        self.assertEqual(client.settings.remote_buffer_seconds, 0.5)
        client.pair_remote_sdr({"address": "127.0.0.1", "port": host.remote_server.port, "code": code})
        client.update({"source_mode": "remote", "remote_host_id": host.remote_identity.fingerprint})
        with client.lock:
            client.streams = [{
                "id": "s1", "enabled": True, "station": {"callsign": "KEC49", "frequency": "162.550"},
                "outputs": [], "eas_recording": {"enabled": True},
            }]
            client._sync_stream_workers_locked()
        worker = next(iter(client.stream_workers.values()))
        wait_for(lambda: worker.fanout.upstream is not None)
        remote_feed = worker.fanout.upstream
        self.assertEqual(remote_feed.jitter.target_seconds, 0.5)

        client.update({"remote_buffer_seconds": 3})
        self.assertEqual(remote_feed.jitter.target_seconds, 3.0)  # same feed, no restart
        self.assertIs(worker.fanout.upstream, remote_feed)
        self.assertIs(next(iter(client.stream_workers.values())), worker)
        for bad in (0, 10.5, "fast"):
            with self.subTest(value=bad), self.assertRaisesRegex(ValueError, "network buffer"):
                client.update({"remote_buffer_seconds": bad})
        reloaded = self.wc.load_settings(client.state_path)
        self.assertEqual(reloaded.remote_buffer_seconds, 3.0)

    def test_unreachable_remote_sdr_notification(self) -> None:
        client = self.service("client")
        client.remote_devices.add("h" * 64, "weather-pi", token="t" * 43, peer_token="p" * 43, address="127.0.0.1", port=1)
        client.update({"source_mode": "remote", "remote_host_id": "h" * 64, "notify_sdr_failures": True})
        with client.lock:
            # Still making its first connection: not unreachable yet.
            client.remote_client.connecting = True
            self.assertIsNone(client._rtl_notification_failure_locked(time.monotonic()))
            self.assertIsNone(client.status()["capture_error"])
        wait_for(lambda: not client.remote_client.status()["connecting"], timeout=15.0)
        with client.lock:
            failure = client._rtl_notification_failure_locked(time.monotonic())

        self.assertEqual(failure["message"], "The remote SDR on weather-pi is not reachable.")
        self.assertEqual(failure["recovery_message"], "The remote SDR on weather-pi is reachable again.")

    def notification_harness(self):
        """A client whose remote SDR reachability the test sets, and the
        SDR notifications it would send."""
        client = self.service("client")
        client.remote_devices.add("h" * 64, "weather-pi", token="t" * 43, peer_token="p" * 43, address="127.0.0.1", port=1)
        client.update({"notify_sdr_failures": True})
        sent: list[str] = []
        client._send_stream_problem_notification_async = lambda failure: sent.append(failure["message"])
        reachable = {"value": True}

        def use_remote() -> None:
            client.update({"source_mode": "remote", "remote_host_id": "h" * 64})
            client.remote_client.status = lambda: {
                "host_id": "h" * 64, "host_name": "weather-pi", "reachable": reachable["value"],
                "connecting": False, "sdr": {"device_name": "RTL-SDR", "problem": None},
            }

        def tick(now: float) -> None:
            with client.lock:
                client._process_rtl_notifications_locked(now)

        return client, sent, reachable, use_remote, tick

    def test_switching_sources_quickly_does_not_report_the_remote_sdr_unreachable(self) -> None:
        client, sent, reachable, use_remote, tick = self.notification_harness()
        grace = self.wc.STREAM_NOTIFICATION_FAILURE_GRACE_SECONDS
        use_remote()
        reachable["value"] = False  # a moment while it connected
        tick(1000.0)
        reachable["value"] = True
        tick(1001.0)
        client.update({"source_mode": "local"})
        tick(1005.0)
        use_remote()
        reachable["value"] = False  # connecting again, well after the first blip
        tick(1000.0 + grace + 5)
        reachable["value"] = True
        tick(1000.0 + grace + 6)
        self.assertEqual(sent, [])

        reachable["value"] = False  # really unreachable: reported after the grace period
        tick(2000.0)
        tick(2000.0 + grace - 1)
        self.assertEqual(sent, [])
        tick(2000.0 + grace)
        self.assertEqual(sent, ["The remote SDR on weather-pi is not reachable."])

    def test_a_brief_outage_does_not_shorten_the_grace_period_of_the_next(self) -> None:
        client, sent, reachable, use_remote, tick = self.notification_harness()
        grace = self.wc.STREAM_NOTIFICATION_FAILURE_GRACE_SECONDS
        use_remote()
        reachable["value"] = False
        tick(1000.0)
        reachable["value"] = True
        tick(1001.0)
        reachable["value"] = False
        tick(1000.0 + grace + 1)  # a new outage, not the first one continuing
        self.assertEqual(sent, [])


class LiveAudioUpstreamStallTests(unittest.TestCase):
    """Monitoring bitrate must not fall because audio stopped before the browser."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wc = load_web_control()

    def run_send_loop(
        self,
        *,
        stall_at_frame: int | None,
        bad_feedback_frames: range,
        frames: int = 60,
        frames_per_batch: int = 1,
        bad_feedback: dict | None = None,
    ):
        wc = self.wc
        frame_bytes = 960
        state = {"frame": 0}
        bitrate_changes: list[tuple[int, int]] = []

        class Source:
            sample_rate = 24_000

            def __init__(self) -> None:
                self.audio_source = types.SimpleNamespace(closed=threading.Event(), underrun_frames=0)

            def read_frame(self, timeout: float) -> bytes | None:
                state["frame"] += 1
                if state["frame"] >= frames:
                    self.audio_source.closed.set()
                if state["frame"] == stall_at_frame:
                    time.sleep(0.4)  # nothing came from the SDR
                elif state["frame"] % frames_per_batch == 0:
                    # The stream makes 20 ms frames, handed over a batch at a time.
                    time.sleep(0.02 * frames_per_batch)
                return b"\x00" * frame_bytes

            def is_paused(self) -> bool:
                return False

        class Encoder:
            bitrate_kbps = 128

            def encode(self, pcm: bytes) -> list[bytes]:
                return [b"opus"]

            def set_bitrate(self, kbps: int) -> None:
                self.bitrate_kbps = kbps
                bitrate_changes.append((state["frame"], kbps))

        class Service:
            def consume_live_audio_feedback(self, client_id: str, mode: str) -> dict:
                if state["frame"] in bad_feedback_frames:
                    return bad_feedback if bad_feedback is not None else {"media_delivery_ratio": 0.3}
                return {}

        writer = types.SimpleNamespace(send_json=lambda payload: None, send_audio=lambda **kwargs: 0.001)
        handler = types.SimpleNamespace(service=Service())
        live_audio = part_of(wc, "handler_live_audio")
        self.addCleanup(setattr, live_audio, "LIVE_AUDIO_UPSTREAM_HOLD_SECONDS", live_audio.LIVE_AUDIO_UPSTREAM_HOLD_SECONDS)
        live_audio.LIVE_AUDIO_UPSTREAM_HOLD_SECONDS = 0.3
        wc.RtlControlHandler._stream_live_audio_websocket(
            handler,
            writer,
            Source(),
            types.SimpleNamespace(queue=queue.Queue()),
            encoder=Encoder(),
            codec="opus",
            mode="monitor",
            client_id="phone",
        )
        return bitrate_changes

    def test_feedback_during_an_upstream_stall_is_ignored(self) -> None:
        # The browser reports the starved delivery right after the stall.
        self.assertEqual(self.run_send_loop(stall_at_frame=10, bad_feedback_frames=range(10, 20)), [])

    def test_browser_connection_problems_still_lower_the_bitrate(self) -> None:
        self.assertEqual(self.run_send_loop(stall_at_frame=None, bad_feedback_frames=range(10, 20))[0], (10, 96))

    def test_audio_handed_over_in_batches_is_not_mistaken_for_a_stall(self) -> None:
        # Streams deliver 100 ms of audio at a time; waiting for the next batch
        # is normal, so a slow connection still lowers the bitrate.
        changes = self.run_send_loop(stall_at_frame=None, bad_feedback_frames=range(10, 20), frames_per_batch=5)
        self.assertEqual(changes[0], (10, 96))

    def test_silence_filling_an_sdr_outage_keeps_the_bitrate(self) -> None:
        # Seen on a real outage: the stream filled it with silence, which Opus sends
        # in a few bytes, and the browser reported receiving 7.5 Kbps of a 96 Kbps
        # setting while all the audio arrived on time.
        quiet = {"network_receive_kbps": 7.5, "media_delivery_ratio": 1.0}
        self.assertEqual(
            self.run_send_loop(stall_at_frame=None, bad_feedback_frames=range(5, 55), bad_feedback=quiet),
            [],
        )

    def test_feedback_after_the_hold_counts_again(self) -> None:
        # 0.3 s hold is about 15 frames; frame 45 is well past it.
        changes = self.run_send_loop(stall_at_frame=10, bad_feedback_frames=range(45, 50))
        self.assertEqual(changes[0], (45, 96))

    def test_listener_is_told_when_the_server_has_no_audio_and_when_it_is_back(self) -> None:
        # Two outages: the SDR stops, comes back, then stops again.
        script = [b"a", None, None, None, b"b", b"c", None, b"d"]
        sent: list = []

        class Source:
            sample_rate = 24_000

            def __init__(self) -> None:
                self.audio_source = types.SimpleNamespace(closed=threading.Event())

            def read_frame(self, timeout: float) -> bytes | None:
                item = script.pop(0)
                if not script:
                    self.audio_source.closed.set()
                if item is None:
                    time.sleep(timeout)  # nothing to send yet
                return item

            def is_paused(self) -> bool:
                return False

        writer = types.SimpleNamespace(
            send_json=lambda payload: sent.append(payload["type"]),
            send_audio=lambda **kwargs: sent.append(kwargs["payload"]),
        )
        self.wc.RtlControlHandler._stream_live_audio_websocket(
            types.SimpleNamespace(service=types.SimpleNamespace()),
            writer,
            Source(),
            None,
            encoder=None,
            codec="pcm",
            mode="receiver",
            client_id="radio",
        )

        self.assertEqual(
            sent,
            [b"a", "source_no_audio", "source_audio_back", b"b", b"c", "source_no_audio", "source_audio_back", b"d"],
        )


if __name__ == "__main__":
    unittest.main()
