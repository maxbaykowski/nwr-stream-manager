from __future__ import annotations

import importlib
import ctypes
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_alsa_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    package.__version__ = "0.0.0"  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    return importlib.import_module("nwr_stream_manager.alsa")


class FakeAlsaBackend:
    def __init__(self, alsa, cards, pcms) -> None:
        self.alsa = alsa
        self.cards = cards
        self.pcms = pcms

    def card_indices(self) -> list[int]:
        return sorted(self.cards)

    def card_info(self, card_index: int):
        return self.cards[card_index]

    def playback_pcm_devices(self, card_index: int):
        return self.pcms.get(card_index, [])


class AlsaDiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.alsa = load_alsa_module()

    def card(
        self,
        index: int,
        *,
        serial: str = "abc123",
        path: str = "pci/usb1/1-2",
        usb_port_path: str = "1-2",
    ) -> object:
        return self.alsa.AlsaCardInfo(
            index=index,
            card_id="Device",
            name="USB Audio Device",
            long_name="Example USB Audio at usb-0000:00:14.0-2",
            mixer_name="USB Mixer",
            components="USB1234:5678",
            sysfs=self.alsa.AlsaSysfsIdentity(
                bus="usb",
                vendor_id="1234",
                product_id="5678",
                serial=serial,
                usb_port_path=usb_port_path,
                device_path=path,
            ),
        )

    def pcm(self, device: int = 0):
        return self.alsa.AlsaPcmInfo(
            device=device,
            pcm_id="USB Audio",
            name="USB Audio Playback",
            subdevices_count=1,
            subdevices_available=1,
        )

    def test_discovery_returns_hw_devices_not_plughw(self) -> None:
        backend = FakeAlsaBackend(self.alsa, {2: self.card(2)}, {2: [self.pcm()]})

        devices = self.alsa.discover_playback_devices(backend)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].hw_device, "hw:2,0")
        self.assertNotIn("plughw", devices[0].hw_device)

    def test_stable_id_survives_card_index_change_when_usb_serial_exists(self) -> None:
        first = self.alsa.playback_device_from_info(self.card(2), self.pcm())
        second = self.alsa.playback_device_from_info(self.card(5), self.pcm())

        self.assertEqual(first.stable_id, second.stable_id)
        self.assertEqual(first.bus, "usb")
        self.assertEqual(first.usb_port_path, "1-2")
        self.assertEqual(first.hw_device, "hw:2,0")
        self.assertEqual(second.hw_device, "hw:5,0")

    def test_usb_without_serial_uses_sysfs_path_for_stability(self) -> None:
        device = self.alsa.playback_device_from_info(
            self.card(
                3,
                serial="",
                path="pci0000:00/0000:00:14.0/usb1/1-2",
                usb_port_path="1-2",
            ),
            self.pcm(),
        )

        self.assertIn("alsa:usb-path:1234:5678", device.stable_id)
        self.assertIn("1-2", device.stable_id)

    def test_identical_usb_devices_without_serial_are_distinguished_by_port(self) -> None:
        first = self.alsa.playback_device_from_info(
            self.card(2, serial="", path="pci/usb1/1-2", usb_port_path="1-2"),
            self.pcm(),
        )
        second = self.alsa.playback_device_from_info(
            self.card(3, serial="", path="pci/usb1/1-3", usb_port_path="1-3"),
            self.pcm(),
        )

        self.assertNotEqual(first.stable_id, second.stable_id)
        self.assertEqual(first.usb_port_path, "1-2")
        self.assertEqual(second.usb_port_path, "1-3")

    def test_identical_usb_devices_with_duplicate_serial_can_resolve_by_port(self) -> None:
        first = self.alsa.playback_device_from_info(
            self.card(2, serial="duplicate", path="pci/usb1/1-2", usb_port_path="1-2"),
            self.pcm(),
        )
        second = self.alsa.playback_device_from_info(
            self.card(3, serial="duplicate", path="pci/usb1/1-3", usb_port_path="1-3"),
            self.pcm(),
        )

        resolved = self.alsa.resolve_playback_device_by_usb_topology(
            vendor_id="1234",
            product_id="5678",
            usb_port_path="1-3",
            pcm_device=0,
            devices=[first, second],
        )

        self.assertEqual(resolved, second)

    def test_builtin_device_falls_back_to_card_and_pcm_metadata(self) -> None:
        card = self.alsa.AlsaCardInfo(
            index=0,
            card_id="PCH",
            name="HDA Intel PCH",
            long_name="HDA Intel PCH at 0xf7210000 irq 132",
            mixer_name="Realtek ALC",
            components="HDA",
            sysfs=self.alsa.AlsaSysfsIdentity(bus="pci", device_path="pci0000:00/0000:00:1f.3"),
        )

        device = self.alsa.playback_device_from_info(card, self.pcm())

        self.assertIn("alsa:card:pch", device.stable_id)
        self.assertEqual(device.bus, "pci")
        self.assertEqual(device.hw_device, "hw:0,0")

    def test_resolve_playback_device_uses_stable_id(self) -> None:
        old = self.alsa.playback_device_from_info(self.card(1), self.pcm())
        new = self.alsa.playback_device_from_info(self.card(4), self.pcm())

        resolved = self.alsa.resolve_playback_device(old.stable_id, [new])

        self.assertEqual(resolved, new)

    def test_resolve_playback_device_rejects_ambiguous_stable_id(self) -> None:
        first = self.alsa.playback_device_from_info(self.card(1), self.pcm())
        duplicate = self.alsa.playback_device_from_info(self.card(2), self.pcm())

        with self.assertRaises(self.alsa.AlsaError):
            self.alsa.resolve_playback_device(first.stable_id, [first, duplicate])

    def test_read_card_sysfs_identity_finds_usb_parent_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usb = root / "devices" / "pci0000:00" / "usb1" / "1-2"
            sound = usb / "sound" / "card7"
            sound.mkdir(parents=True)
            (usb / "idVendor").write_text("0d8c\n", encoding="utf-8")
            (usb / "idProduct").write_text("0014\n", encoding="utf-8")
            (usb / "serial").write_text("audio-serial\n", encoding="utf-8")
            sysfs_sound = root / "class" / "sound"
            card = sysfs_sound / "card7"
            card.mkdir(parents=True)
            (card / "device").symlink_to(usb)

            identity = self.alsa._read_card_sysfs_identity(sysfs_sound, 7)

        self.assertEqual(identity.vendor_id, "0d8c")
        self.assertEqual(identity.product_id, "0014")
        self.assertEqual(identity.serial, "audio-serial")
        self.assertEqual(identity.bus, "usb")
        self.assertEqual(identity.usb_port_path, "1-2")
        self.assertIn("1-2", identity.device_path)

    def test_playback_device_usb_node_uses_sysfs_bus_and_device_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usb = root / "sys" / "devices" / "pci0000:00" / "usb1" / "1-2"
            usb.mkdir(parents=True)
            (usb / "busnum").write_text("3\n", encoding="utf-8")
            (usb / "devnum").write_text("17\n", encoding="utf-8")
            device = self.alsa.playback_device_from_info(
                self.card(2, path="pci0000:00/usb1/1-2", usb_port_path="1-2"),
                self.pcm(),
            )

            node = self.alsa.playback_device_usb_node(
                device,
                sysfs_root=root / "sys",
                dev_bus_usb_root=root / "dev" / "bus" / "usb",
            )

        self.assertEqual(node, root / "dev" / "bus" / "usb" / "003" / "017")

    def test_read_card_sysfs_identity_marks_pci_without_usb_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pci = root / "devices" / "pci0000:00" / "0000:00:1f.3" / "skl_hda_dsp_generic"
            pci.mkdir(parents=True)
            (pci / "vendor").write_text("0x8086\n", encoding="utf-8")
            (pci / "device").write_text("0xa0c8\n", encoding="utf-8")
            sysfs_sound = root / "class" / "sound"
            card = sysfs_sound / "card1"
            card.mkdir(parents=True)
            (card / "device").symlink_to(pci)

            identity = self.alsa._read_card_sysfs_identity(sysfs_sound, 1)

        self.assertEqual(identity.bus, "pci")
        self.assertEqual(identity.vendor_id, "")
        self.assertEqual(identity.product_id, "")
        self.assertIn("0000:00:1f.3", identity.device_path)

    def test_procfs_backend_discovers_playback_devices_without_dev_snd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proc = root / "proc" / "asound"
            sysfs = root / "sys" / "class" / "sound"
            (proc / "card0" / "pcm0p").mkdir(parents=True)
            (proc / "card0" / "pcm0c").mkdir(parents=True)
            (proc / "card0" / "id").write_text("X\n", encoding="utf-8")
            (proc / "cards").write_text(
                " 0 [X              ]: USB-Audio - Yeti X\n"
                "                      Blue Microphones Yeti X at usb-0000:00:14.0-7.3, full speed\n",
                encoding="utf-8",
            )
            (proc / "card0" / "pcm0p" / "info").write_text(
                "card: 0\n"
                "device: 0\n"
                "stream: PLAYBACK\n"
                "id: USB Audio\n"
                "name: USB Audio\n"
                "subdevices_count: 1\n"
                "subdevices_avail: 0\n",
                encoding="utf-8",
            )
            (proc / "card0" / "pcm0c" / "info").write_text(
                "card: 0\n"
                "device: 0\n"
                "stream: CAPTURE\n",
                encoding="utf-8",
            )
            usb = root / "sys" / "devices" / "pci0000:00" / "usb3" / "3-7.3"
            card = sysfs / "card0"
            card.mkdir(parents=True)
            usb.mkdir(parents=True)
            (usb / "idVendor").write_text("b58e\n", encoding="utf-8")
            (usb / "idProduct").write_text("9e84\n", encoding="utf-8")
            (usb / "serial").write_text("yeti-x\n", encoding="utf-8")
            (card / "device").symlink_to(usb)

            backend = self.alsa.ProcfsAlsaBackend(proc_root=proc, sysfs_root=sysfs)
            devices = self.alsa.discover_playback_devices(backend)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].hw_device, "hw:0,0")
        self.assertEqual(devices[0].card_id, "X")
        self.assertEqual(devices[0].pcm_id, "USB Audio")
        self.assertEqual(devices[0].bus, "usb")
        self.assertEqual(devices[0].usb_port_path, "3-7.3")
        self.assertEqual(devices[0].serial, "yeti-x")

    def test_software_volume_scales_and_clips_s16le(self) -> None:
        pcm = np.array([-20000, -1000, 1000, 20000], dtype="<i2").tobytes()

        output = self.alsa.apply_software_volume_s16le(pcm, 2.0)
        samples = np.frombuffer(output, dtype="<i2")

        np.testing.assert_array_equal(
            samples,
            np.array([-32768, -2000, 2000, 32767], dtype="<i2"),
        )

    def test_software_volume_rejects_negative_values(self) -> None:
        with self.assertRaises(self.alsa.AlsaError):
            self.alsa.AlsaSoftwareVolume(-0.1)

    def test_channel_conversion_duplicates_mono_to_stereo(self) -> None:
        pcm = np.array([100, -200, 300], dtype="<i2").tobytes()

        output = self.alsa.convert_s16le_channels(
            pcm,
            input_channels=1,
            output_channels=2,
        )
        samples = np.frombuffer(output, dtype="<i2")

        np.testing.assert_array_equal(
            samples,
            np.array([100, 100, -200, -200, 300, 300], dtype="<i2"),
        )

    def test_stereo_routing_supports_left_right_and_both(self) -> None:
        pcm = np.array([100, -200], dtype="<i2").tobytes()

        both = np.frombuffer(self.alsa.mono_s16le_to_stereo(pcm, mode="both"), dtype="<i2")
        left = np.frombuffer(self.alsa.mono_s16le_to_stereo(pcm, mode="left"), dtype="<i2")
        right = np.frombuffer(self.alsa.mono_s16le_to_stereo(pcm, mode="right"), dtype="<i2")

        np.testing.assert_array_equal(both, np.array([100, 100, -200, -200], dtype="<i2"))
        np.testing.assert_array_equal(left, np.array([100, 0, -200, 0], dtype="<i2"))
        np.testing.assert_array_equal(right, np.array([0, 100, 0, -200], dtype="<i2"))

    def test_callback_buffer_resamples_routes_and_pads_underruns(self) -> None:
        buffer = self.alsa.AlsaPcmCallbackBuffer(
            source_sample_rate=24_000,
            output_sample_rate=24_000,
            output_channels=2,
            channel_mode="left",
            prefill_seconds=0.02,
            max_buffer_seconds=0.1,
        )
        pcm = np.full(240, 10_000, dtype="<i2").tobytes()

        buffer.push_mono_pcm(pcm)
        output = np.frombuffer(buffer.read(240), dtype="<f4")
        underrun = np.frombuffer(buffer.read(10), dtype="<f4")
        stats = buffer.stats()

        self.assertEqual(output.size, 480)
        self.assertTrue(np.any(output[0::2] != 0))
        self.assertTrue(np.all(output[1::2] == 0))
        self.assertTrue(np.all(underrun == 0))
        self.assertGreater(stats["underrun_frames"], 0)

    def test_callback_buffer_does_not_consume_partial_period_on_underrun(self) -> None:
        buffer = self.alsa.AlsaPcmCallbackBuffer(
            source_sample_rate=24_000,
            output_sample_rate=24_000,
            output_channels=2,
            channel_mode="both",
            prefill_seconds=0.02,
            max_buffer_seconds=0.1,
        )
        pcm = np.full(120, 10_000, dtype="<i2").tobytes()

        buffer.push_mono_pcm(pcm)
        first = np.frombuffer(buffer.read(240, timeout=0), dtype="<f4")
        stats_after_underrun = buffer.stats()
        second = np.frombuffer(buffer.read(120, timeout=0), dtype="<f4")

        self.assertTrue(np.all(first == 0))
        self.assertEqual(stats_after_underrun["buffered_seconds"], 0.005)
        self.assertTrue(np.any(second != 0))

    def test_shared_playback_tap_mixes_inputs_by_channel(self) -> None:
        tap = self.alsa.AlsaSharedPlaybackTap("alsa:usb:test", output_sample_rate=24_000)
        tap.register_input("left", self.alsa.AlsaSharedInputConfig(channel_mode="left", software_volume=1.0))
        tap.register_input("right", self.alsa.AlsaSharedInputConfig(channel_mode="right", software_volume=0.5))

        tap.push_float("left", np.ones(240, dtype=np.float32) * 0.25)
        tap.push_float("right", np.ones(240, dtype=np.float32) * 0.5)
        output = tap._mix_frame(240).reshape(-1, 2)

        np.testing.assert_allclose(output[:, 0], 0.25)
        np.testing.assert_allclose(output[:, 1], 0.25)

    def test_stream_tap_clears_stale_audio_when_playback_reopens(self) -> None:
        device = self.alsa.playback_device_from_info(self.card(2), self.pcm())

        class Playback:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def close(self) -> None:
                pass

            def snapshot(self):
                return {}

        tap = self.alsa.AlsaStreamPlaybackTap(
            self.alsa.AlsaStreamTapConfig(stable_id=device.stable_id, output_sample_rate=24_000),
            devices_provider=lambda: [device],
            playback_factory=Playback,
        )
        tap.push_float(np.ones(240, dtype=np.float32))
        self.assertGreater(tap.buffer.stats()["buffered_bytes"], 0)

        tap._open_playback()

        self.assertEqual(tap.buffer.stats()["buffered_bytes"], 0)
        tap._close_playback()

    def test_shared_tap_clears_stale_input_audio_when_playback_reopens(self) -> None:
        device = self.alsa.playback_device_from_info(self.card(2), self.pcm())

        class Playback:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            def close(self) -> None:
                pass

            def snapshot(self):
                return {}

        tap = self.alsa.AlsaSharedPlaybackTap(
            device.stable_id,
            output_sample_rate=24_000,
            devices_provider=lambda: [device],
            playback_factory=Playback,
        )
        tap.register_input("stream", self.alsa.AlsaSharedInputConfig(channel_mode="both", software_volume=1.0))
        tap.push_float("stream", np.ones(240, dtype=np.float32))
        self.assertGreater(tap.snapshot()["inputs"]["stream"]["buffered_samples"], 0)

        tap._open_playback()

        self.assertEqual(tap.snapshot()["inputs"]["stream"]["buffered_samples"], 0)
        tap._close_playback()

    def test_playback_close_drops_realtime_audio_instead_of_draining_when_available(self) -> None:
        class Lib:
            def __init__(self) -> None:
                self.calls = []

            def snd_pcm_drop(self, _handle):
                self.calls.append("drop")
                return 0

            def snd_pcm_drain(self, _handle):
                self.calls.append("drain")
                return 0

            def snd_pcm_close(self, _handle):
                self.calls.append("close")
                return 0

        playback = object.__new__(self.alsa.AlsaPcmPlayback)
        playback.lock = self.alsa.threading.RLock()
        playback.handle = ctypes.c_void_p(1234)
        playback.lib = Lib()

        playback.close()

        self.assertEqual(playback.lib.calls, ["drop", "close"])
        self.assertFalse(playback.handle)

    def test_channel_conversion_downmixes_stereo_to_mono(self) -> None:
        pcm = np.array([100, 300, -100, -300], dtype="<i2").tobytes()

        output = self.alsa.convert_s16le_channels(
            pcm,
            input_channels=2,
            output_channels=1,
        )
        samples = np.frombuffer(output, dtype="<i2")

        np.testing.assert_array_equal(samples, np.array([200, -200], dtype="<i2"))

    def test_float_channel_conversion_keeps_stereo_and_silences_extra_channels(self) -> None:
        stereo = np.array([0.25, -0.25, 0.5, -0.5], dtype=np.float32)

        output = self.alsa.convert_float32_channels(stereo, input_channels=2, output_channels=8)

        self.assertEqual(output.size, 16)
        frames = output.reshape(-1, 8)
        np.testing.assert_allclose(frames[:, :2], stereo.reshape(-1, 2))
        np.testing.assert_allclose(frames[:, 2:], 0.0)

    def test_float_channel_conversion_downmixes_to_mono(self) -> None:
        stereo = np.array([0.25, 0.75, -0.25, -0.75], dtype=np.float32)

        output = self.alsa.convert_float32_channels(stereo, input_channels=2, output_channels=1)

        np.testing.assert_allclose(output, np.array([0.5, -0.5], dtype=np.float32))

    def test_sample_format_conversion_supports_packed_24_bit(self) -> None:
        samples = np.array([0.5, -0.5], dtype=np.float32)

        output = self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_S24_3LE)

        self.assertEqual(output, bytes([0x00, 0x00, 0x40, 0x00, 0x00, 0xC0]))

    def test_sample_format_conversion_supports_32_bit_container_formats(self) -> None:
        samples = np.array([0.5], dtype=np.float32)

        s24 = self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_S24_LE)
        s32 = self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_S32_LE)

        self.assertEqual(s24, bytes([0x00, 0x00, 0x00, 0x40]))
        self.assertEqual(s32, bytes([0x00, 0x00, 0x00, 0x40]))

    def test_sample_format_conversion_supports_requested_major_formats(self) -> None:
        samples = np.array([-1.0, 0.0, 1.0], dtype=np.float32)

        self.assertEqual(
            np.frombuffer(self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_S8), dtype=np.int8).tolist(),
            [-127, 0, 127],
        )
        self.assertEqual(
            np.frombuffer(self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_U8), dtype=np.uint8).tolist(),
            [0, 128, 255],
        )
        self.assertEqual(
            np.frombuffer(self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_S16_LE), dtype="<i2").tolist(),
            [-32767, 0, 32767],
        )
        self.assertEqual(
            np.frombuffer(self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_U16_LE), dtype="<u2").tolist(),
            [0, 32768, 65535],
        )
        self.assertEqual(
            np.frombuffer(self.alsa.convert_float32_sample_format(samples, self.alsa.SND_PCM_FORMAT_FLOAT_LE), dtype="<f4").tolist(),
            [-1.0, 0.0, 1.0],
        )

    def test_mixer_unity_target_prefers_zero_db_without_boost(self) -> None:
        self.assertEqual(self.alsa.mixer_unity_target_mb(-6000, 1200), 0)
        self.assertEqual(self.alsa.mixer_unity_target_mb(-6000, -300), -300)
        self.assertIsNone(self.alsa.mixer_unity_target_mb(100, 1200))


if __name__ == "__main__":
    unittest.main()
