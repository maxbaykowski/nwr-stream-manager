from __future__ import annotations

import ctypes
import ctypes.util
import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from .encoder import PcmResampler


LOG = logging.getLogger(__name__)

SND_PCM_STREAM_PLAYBACK = 0
SND_PCM_ACCESS_RW_INTERLEAVED = 3
SND_PCM_FORMAT_S8 = 0
SND_PCM_FORMAT_U8 = 1
SND_PCM_FORMAT_S16_LE = 2
SND_PCM_FORMAT_U16_LE = 4
SND_PCM_FORMAT_S24_LE = 6
SND_PCM_FORMAT_U24_LE = 8
SND_PCM_FORMAT_S32_LE = 10
SND_PCM_FORMAT_U32_LE = 12
SND_PCM_FORMAT_FLOAT_LE = 14
SND_PCM_FORMAT_S24_3LE = 32
SND_PCM_FORMAT_U24_3LE = 34
SND_PCM_NONBLOCK = 1
ALSA_STREAM_SOURCE_SAMPLE_RATE = 24_000
ALSA_STREAM_OUTPUT_CHANNELS = 2
ALSA_STREAM_FRAME_SECONDS = 0.02
ALSA_STREAM_PREFILL_SECONDS = 0.08
ALSA_STREAM_MAX_BUFFER_SECONDS = 0.30
ALSA_STREAM_RECONNECT_SECONDS = 0.5
ALSA_STREAM_DEFAULT_SAMPLE_RATE = 48_000
ALSA_HW_BUFFER_TIME_US = 120_000
ALSA_HW_PERIOD_TIME_US = 20_000
ALSA_CHANNEL_BOTH = "both"
ALSA_CHANNEL_LEFT = "left"
ALSA_CHANNEL_RIGHT = "right"
ALSA_CHANNEL_MODES = {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_LEFT, ALSA_CHANNEL_RIGHT}
ALSA_PLAYBACK_FORMATS = (
    (SND_PCM_FORMAT_FLOAT_LE, "FLOAT_LE", 4),
    (SND_PCM_FORMAT_S32_LE, "S32_LE", 4),
    (SND_PCM_FORMAT_U32_LE, "U32_LE", 4),
    (SND_PCM_FORMAT_S24_LE, "S24_LE", 4),
    (SND_PCM_FORMAT_U24_LE, "U24_LE", 4),
    (SND_PCM_FORMAT_S24_3LE, "S24_3LE", 3),
    (SND_PCM_FORMAT_U24_3LE, "U24_3LE", 3),
    (SND_PCM_FORMAT_S16_LE, "S16_LE", 2),
    (SND_PCM_FORMAT_U16_LE, "U16_LE", 2),
    (SND_PCM_FORMAT_S8, "S8", 1),
    (SND_PCM_FORMAT_U8, "U8", 1),
)


class AlsaError(RuntimeError):
    """Raised when ALSA hardware discovery cannot be completed."""


@dataclass(frozen=True)
class AlsaSysfsIdentity:
    bus: str = "unknown"
    vendor_id: str = ""
    product_id: str = ""
    serial: str = ""
    usb_port_path: str = ""
    device_path: str = ""


@dataclass(frozen=True)
class AlsaCardInfo:
    index: int
    card_id: str
    name: str
    long_name: str
    mixer_name: str
    components: str
    sysfs: AlsaSysfsIdentity


@dataclass(frozen=True)
class AlsaPcmInfo:
    device: int
    pcm_id: str
    name: str
    subdevices_count: int
    subdevices_available: int


@dataclass(frozen=True)
class AlsaPlaybackDevice:
    stable_id: str
    hw_device: str
    card_index: int
    pcm_device: int
    card_id: str
    card_name: str
    card_long_name: str
    pcm_id: str
    pcm_name: str
    subdevices_count: int
    subdevices_available: int
    bus: str = "unknown"
    vendor_id: str = ""
    product_id: str = ""
    serial: str = ""
    usb_port_path: str = ""
    device_path: str = ""

    @property
    def display_name(self) -> str:
        card = self.card_long_name or self.card_name or self.card_id or f"card {self.card_index}"
        pcm = self.pcm_name or self.pcm_id or f"PCM {self.pcm_device}"
        return f"{card} - {pcm}"


class AlsaDiscoveryBackend(Protocol):
    def card_indices(self) -> list[int]:
        ...

    def card_info(self, card_index: int) -> AlsaCardInfo:
        ...

    def playback_pcm_devices(self, card_index: int) -> list[AlsaPcmInfo]:
        ...


def discover_playback_devices(
    backend: AlsaDiscoveryBackend | None = None,
) -> list[AlsaPlaybackDevice]:
    if backend is None:
        devices = _discover_with_backend(CtypesAlsaBackend())
        if devices:
            return devices
        LOG.info(
            "ALSA libasound discovery returned no playback devices; falling back to /proc/asound"
        )
        return _discover_with_backend(ProcfsAlsaBackend())
    return _discover_with_backend(backend)


def _discover_with_backend(backend: AlsaDiscoveryBackend) -> list[AlsaPlaybackDevice]:
    devices: list[AlsaPlaybackDevice] = []
    for card_index in backend.card_indices():
        card = backend.card_info(card_index)
        for pcm in backend.playback_pcm_devices(card_index):
            devices.append(playback_device_from_info(card, pcm))
    return sorted(devices, key=lambda device: (device.card_index, device.pcm_device, device.stable_id))


def playback_device_from_info(card: AlsaCardInfo, pcm: AlsaPcmInfo) -> AlsaPlaybackDevice:
    stable_id = build_stable_playback_id(card, pcm)
    return AlsaPlaybackDevice(
        stable_id=stable_id,
        hw_device=f"hw:{card.index},{pcm.device}",
        card_index=card.index,
        pcm_device=pcm.device,
        card_id=card.card_id,
        card_name=card.name,
        card_long_name=card.long_name,
        pcm_id=pcm.pcm_id,
        pcm_name=pcm.name,
        subdevices_count=pcm.subdevices_count,
        subdevices_available=pcm.subdevices_available,
        bus=card.sysfs.bus,
        vendor_id=card.sysfs.vendor_id,
        product_id=card.sysfs.product_id,
        serial=card.sysfs.serial,
        usb_port_path=card.sysfs.usb_port_path,
        device_path=card.sysfs.device_path,
    )


def build_stable_playback_id(card: AlsaCardInfo, pcm: AlsaPcmInfo) -> str:
    sysfs = card.sysfs
    if sysfs.bus == "usb" and sysfs.vendor_id and sysfs.product_id and sysfs.serial:
        return _stable_id(
            "alsa",
            "usb",
            sysfs.vendor_id,
            sysfs.product_id,
            sysfs.serial,
            f"pcm{pcm.device}",
            pcm.pcm_id or pcm.name,
        )
    if sysfs.bus == "usb" and sysfs.vendor_id and sysfs.product_id and sysfs.device_path:
        return _stable_id(
            "alsa",
            "usb-path",
            sysfs.vendor_id,
            sysfs.product_id,
            sysfs.device_path,
            sysfs.usb_port_path,
            f"pcm{pcm.device}",
            pcm.pcm_id or pcm.name,
        )
    return _stable_id(
        "alsa",
        "card",
        card.card_id or card.name,
        card.long_name or card.name,
        f"pcm{pcm.device}",
        pcm.pcm_id or pcm.name,
    )


def resolve_playback_device(
    stable_id: str,
    devices: list[AlsaPlaybackDevice] | None = None,
    backend: AlsaDiscoveryBackend | None = None,
) -> AlsaPlaybackDevice | None:
    candidates = devices if devices is not None else discover_playback_devices(backend)
    matches = [device for device in candidates if device.stable_id == stable_id]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AlsaError(f"ALSA playback device stable ID is ambiguous: {stable_id}")
    return None


def resolve_playback_device_by_usb_topology(
    *,
    vendor_id: str,
    product_id: str,
    usb_port_path: str,
    pcm_device: int | None = None,
    devices: list[AlsaPlaybackDevice] | None = None,
    backend: AlsaDiscoveryBackend | None = None,
) -> AlsaPlaybackDevice | None:
    candidates = devices if devices is not None else discover_playback_devices(backend)
    matches = [
        device
        for device in candidates
        if device.bus == "usb"
        and _normalized_token(device.vendor_id) == _normalized_token(vendor_id)
        and _normalized_token(device.product_id) == _normalized_token(product_id)
        and device.usb_port_path == usb_port_path
        and (pcm_device is None or device.pcm_device == pcm_device)
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AlsaError(
            "USB ALSA playback device topology is ambiguous: "
            f"{vendor_id}:{product_id} at {usb_port_path}"
        )
    return None


def playback_device_usb_node(
    device: AlsaPlaybackDevice,
    *,
    sysfs_root: Path = Path("/sys"),
    dev_bus_usb_root: Path = Path("/dev/bus/usb"),
) -> Path:
    if device.bus != "usb":
        raise AlsaError("soundcard is not connected over USB")
    if not device.device_path:
        raise AlsaError("USB soundcard sysfs path is not available")
    path = Path(device.device_path)
    if not path.is_absolute():
        path = sysfs_root / "devices" / path
    path = path.resolve(strict=False)
    for candidate in (path, *path.parents):
        busnum = _read_int_file(candidate / "busnum")
        devnum = _read_int_file(candidate / "devnum")
        if busnum is None or devnum is None:
            continue
        return dev_bus_usb_root / f"{busnum:03d}" / f"{devnum:03d}"
    raise AlsaError("USB bus/device numbers were not available for this soundcard")


@dataclass(frozen=True)
class AlsaMixerAdjustment:
    control_name: str
    minimum_mb: int
    maximum_mb: int
    target_mb: int
    kind: str = "volume"

    @property
    def target_db(self) -> float:
        return self.target_mb / 100.0


class AlsaSoftwareVolume:
    def __init__(self, volume: float = 1.0) -> None:
        self.volume = volume

    @property
    def volume(self) -> float:
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise AlsaError(f"software volume must be numeric, got {value!r}") from exc
        if parsed < 0:
            raise AlsaError("software volume must not be negative")
        self._volume = parsed

    def apply(self, pcm_s16le: bytes) -> bytes:
        return apply_software_volume_s16le(pcm_s16le, self._volume)

    def apply_float32(self, samples: np.ndarray) -> np.ndarray:
        return apply_software_volume_float32(samples, self._volume)


class AlsaPcmCallbackBuffer:
    def __init__(
        self,
        *,
        source_sample_rate: int = ALSA_STREAM_SOURCE_SAMPLE_RATE,
        output_sample_rate: int = ALSA_STREAM_DEFAULT_SAMPLE_RATE,
        output_channels: int = ALSA_STREAM_OUTPUT_CHANNELS,
        channel_mode: str = ALSA_CHANNEL_BOTH,
        prefill_seconds: float = ALSA_STREAM_PREFILL_SECONDS,
        max_buffer_seconds: float = ALSA_STREAM_MAX_BUFFER_SECONDS,
    ) -> None:
        if source_sample_rate <= 0 or output_sample_rate <= 0:
            raise AlsaError("sample rates must be positive")
        if output_channels <= 0:
            raise AlsaError("output channel count must be positive")
        if channel_mode not in ALSA_CHANNEL_MODES:
            raise AlsaError(f"unsupported ALSA channel mode: {channel_mode}")
        self.source_sample_rate = int(source_sample_rate)
        self.output_sample_rate = int(output_sample_rate)
        self.output_channels = int(output_channels)
        self.channel_mode = channel_mode
        self.resampler = FloatPcmResampler(self.source_sample_rate, self.output_sample_rate)
        self.frame_bytes = self.output_channels * 4
        self.prefill_bytes = self._duration_bytes(prefill_seconds)
        self.max_buffer_bytes = max(self.prefill_bytes, self._duration_bytes(max_buffer_seconds))
        self.buffer = bytearray()
        self.lock = threading.Condition(threading.Lock())
        self.pushed_bytes = 0
        self.read_bytes = 0
        self.dropped_bytes = 0
        self.underrun_frames = 0
        self.max_buffered_bytes = 0

    def push_mono_pcm(self, pcm_s16le: bytes) -> None:
        if not pcm_s16le:
            return
        samples = s16le_to_float32(pcm_s16le)
        self.push_mono_float(samples)

    def push_mono_float(self, samples: np.ndarray) -> None:
        if samples.size == 0:
            return
        converted = self.resampler.process(samples)
        if converted.size == 0:
            return
        self._append_resampled_mono_float(converted)

    def _append_resampled_mono_float(self, converted: np.ndarray) -> None:
        routed = mono_float32_to_stereo(converted, mode=self.channel_mode)
        if self.output_channels != ALSA_STREAM_OUTPUT_CHANNELS:
            routed = convert_float32_channels(
                routed,
                input_channels=ALSA_STREAM_OUTPUT_CHANNELS,
                output_channels=self.output_channels,
            )
        routed_bytes = np.ascontiguousarray(routed.astype("<f4", copy=False)).tobytes()
        with self.lock:
            self.buffer.extend(routed_bytes)
            self.pushed_bytes += len(routed_bytes)
            if len(self.buffer) > self.max_buffer_bytes:
                extra = len(self.buffer) - self.max_buffer_bytes
                extra -= extra % self.frame_bytes
                if extra > 0:
                    del self.buffer[:extra]
                    self.dropped_bytes += extra
            self.max_buffered_bytes = max(self.max_buffered_bytes, len(self.buffer))
            self.lock.notify_all()

    def read(self, frame_count: int, *, timeout: float = ALSA_STREAM_FRAME_SECONDS) -> bytes:
        frame_count = max(0, int(frame_count))
        needed = frame_count * self.frame_bytes
        if needed <= 0:
            return b""
        with self.lock:
            if len(self.buffer) < needed:
                self.lock.wait_for(lambda: len(self.buffer) >= needed, timeout=max(0.0, float(timeout)))
            if len(self.buffer) >= needed:
                output = bytes(self.buffer[:needed])
                del self.buffer[:needed]
                self.read_bytes += needed
                return output
            self.underrun_frames += frame_count
            return b"\x00" * needed

    def close(self) -> None:
        try:
            tail = self.resampler.flush()
        except Exception:
            tail = np.empty(0, dtype=np.float32)
        if tail.size:
            self._append_resampled_mono_float(tail)

    def clear(self) -> None:
        with self.lock:
            self.buffer.clear()
            self.lock.notify_all()

    def stats(self) -> dict[str, int | float | str]:
        with self.lock:
            buffered = len(self.buffer)
            return {
                "source_sample_rate": self.source_sample_rate,
                "output_sample_rate": self.output_sample_rate,
                "output_channels": self.output_channels,
                "channel_mode": self.channel_mode,
                "buffered_bytes": buffered,
                "buffered_seconds": buffered / max(1, self.output_sample_rate * self.frame_bytes),
                "max_buffered_bytes": self.max_buffered_bytes,
                "pushed_bytes": self.pushed_bytes,
                "read_bytes": self.read_bytes,
                "dropped_bytes": self.dropped_bytes,
                "underrun_frames": self.underrun_frames,
            }

    def _duration_bytes(self, seconds: float) -> int:
        frames = max(1, round(float(seconds) * self.output_sample_rate))
        return frames * self.frame_bytes


@dataclass(frozen=True)
class AlsaStreamTapConfig:
    stable_id: str
    output_sample_rate: int = ALSA_STREAM_DEFAULT_SAMPLE_RATE
    channel_mode: str = ALSA_CHANNEL_BOTH
    software_volume: float = 1.0


class AlsaStreamPlaybackTap:
    def __init__(
        self,
        config: AlsaStreamTapConfig,
        *,
        devices_provider: Callable[[], list[AlsaPlaybackDevice]] = discover_playback_devices,
        playback_factory: Callable[..., AlsaPcmPlayback] | None = None,
    ) -> None:
        self.config = config
        self.devices_provider = devices_provider
        self.playback_factory = playback_factory or AlsaPcmPlayback
        self.buffer = AlsaPcmCallbackBuffer(
            source_sample_rate=ALSA_STREAM_SOURCE_SAMPLE_RATE,
            output_sample_rate=config.output_sample_rate,
            output_channels=ALSA_STREAM_OUTPUT_CHANNELS,
            channel_mode=config.channel_mode,
        )
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"alsa-stream-tap-{config.stable_id}", daemon=True)
        self.lock = threading.Lock()
        self.status = "disabled"
        self.error = ""
        self.device: AlsaPlaybackDevice | None = None
        self.playback: AlsaPcmPlayback | None = None
        self.reopen_block_until = 0.0

    def start(self) -> None:
        if self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        self._close_playback()
        self.buffer.close()
        self._set_status("disabled")

    def push_pcm(self, pcm_s16le: bytes) -> None:
        self.buffer.push_mono_pcm(pcm_s16le)

    def push_float(self, samples: np.ndarray) -> None:
        self.buffer.push_mono_float(samples)

    def set_channel_mode(self, channel_mode: str) -> None:
        if channel_mode not in ALSA_CHANNEL_MODES:
            raise AlsaError(f"unsupported ALSA channel mode: {channel_mode}")
        with self.lock:
            self.config = AlsaStreamTapConfig(
                stable_id=self.config.stable_id,
                output_sample_rate=self.config.output_sample_rate,
                channel_mode=channel_mode,
                software_volume=self.config.software_volume,
            )
            self.buffer.channel_mode = channel_mode

    def set_software_volume(self, volume: float) -> None:
        parsed = float(volume)
        AlsaSoftwareVolume(parsed)
        with self.lock:
            self.config = AlsaStreamTapConfig(
                stable_id=self.config.stable_id,
                output_sample_rate=self.config.output_sample_rate,
                channel_mode=self.config.channel_mode,
                software_volume=parsed,
            )
            playback = self.playback
        if playback is not None:
            playback.volume.volume = parsed

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            status = self.status
            error = self.error
            device = self.device
            playback = self.playback
        return {
            "status": status,
            "error": error,
            "stable_id": self.config.stable_id,
            "device": device.hw_device if device is not None else "",
            "buffer": self.buffer.stats(),
            "playback": playback.snapshot() if playback is not None else {},
        }

    def _run(self) -> None:
        frames_per_period = max(1, round(self.config.output_sample_rate * ALSA_STREAM_FRAME_SECONDS))
        while not self.stop_event.is_set():
            try:
                if self.playback is None:
                    self._open_playback()
                    continue
                pcm = self.buffer.read(frames_per_period)
                self.playback.write_float32(pcm, input_channels=ALSA_STREAM_OUTPUT_CHANNELS)
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self._set_status("needs-attention", str(exc))
                self._close_playback()
                self.stop_event.wait(ALSA_STREAM_RECONNECT_SECONDS)
        self._close_playback()

    def _open_playback(self) -> None:
        device = resolve_playback_device(self.config.stable_id, devices=self.devices_provider())
        if device is None:
            self._set_status("needs-attention", "soundcard is not currently connected")
            self.stop_event.wait(ALSA_STREAM_RECONNECT_SECONDS)
            return
        playback = self.playback_factory(
            device,
            sample_rate=self.config.output_sample_rate,
            channels=ALSA_STREAM_OUTPUT_CHANNELS,
            software_volume=self.config.software_volume,
        )
        self.buffer.clear()
        with self.lock:
            self.device = device
            self.playback = playback
        self._set_status("enabled")
        LOG.info("started ALSA stream tap on %s for %s", device.hw_device, self.config.stable_id)

    def _close_playback(self) -> None:
        with self.lock:
            playback = self.playback
            self.playback = None
            self.device = None
        if playback is not None:
            try:
                playback.close()
            except Exception:
                LOG.debug("failed to close ALSA stream tap playback", exc_info=True)

    def _set_status(self, status: str, error: str = "") -> None:
        with self.lock:
            self.status = status
            self.error = error


@dataclass(frozen=True)
class AlsaSharedInputConfig:
    channel_mode: str = ALSA_CHANNEL_BOTH
    software_volume: float = 1.0


class AlsaSharedInputBuffer:
    def __init__(self, source_sample_rate: int, output_sample_rate: int) -> None:
        self.resampler = FloatPcmResampler(source_sample_rate, output_sample_rate)
        self.buffer = np.empty(0, dtype=np.float32)
        self.offset = 0
        self.lock = threading.Lock()
        self.pushed_samples = 0
        self.read_samples = 0
        self.dropped_samples = 0
        self.max_samples = max(1, round(output_sample_rate * ALSA_STREAM_MAX_BUFFER_SECONDS))

    def push(self, samples: np.ndarray) -> None:
        converted = self.resampler.process(samples)
        if converted.size == 0:
            return
        converted = np.asarray(converted, dtype=np.float32)
        with self.lock:
            if self.offset:
                self.buffer = self.buffer[self.offset :]
                self.offset = 0
            self.buffer = converted if self.buffer.size == 0 else np.concatenate((self.buffer, converted))
            self.pushed_samples += int(converted.size)
            if self.buffer.size > self.max_samples:
                extra = self.buffer.size - self.max_samples
                self.buffer = self.buffer[extra:]
                self.dropped_samples += int(extra)

    def read(self, frame_count: int) -> np.ndarray:
        with self.lock:
            available = min(frame_count, self.buffer.size - self.offset)
            if available > 0:
                output = np.array(self.buffer[self.offset : self.offset + available], dtype=np.float32, copy=True)
                self.offset += available
                self.read_samples += int(available)
            else:
                output = np.empty(0, dtype=np.float32)
            if self.offset and self.offset >= self.buffer.size:
                self.buffer = np.empty(0, dtype=np.float32)
                self.offset = 0
        if output.size < frame_count:
            output = np.pad(output, (0, frame_count - output.size))
        return output

    def stats(self) -> dict[str, int | float]:
        with self.lock:
            buffered = max(0, self.buffer.size - self.offset)
            return {
                "buffered_samples": int(buffered),
                "pushed_samples": self.pushed_samples,
                "read_samples": self.read_samples,
                "dropped_samples": self.dropped_samples,
            }

    def clear(self) -> None:
        with self.lock:
            self.buffer = np.empty(0, dtype=np.float32)
            self.offset = 0


class AlsaSharedPlaybackTap:
    def __init__(
        self,
        stable_id: str,
        *,
        output_sample_rate: int = ALSA_STREAM_DEFAULT_SAMPLE_RATE,
        devices_provider: Callable[[], list[AlsaPlaybackDevice]] = discover_playback_devices,
        playback_factory: Callable[..., AlsaPcmPlayback] | None = None,
    ) -> None:
        self.stable_id = str(stable_id)
        self.output_sample_rate = int(output_sample_rate)
        self.devices_provider = devices_provider
        self.playback_factory = playback_factory or AlsaPcmPlayback
        self.inputs: dict[str, tuple[AlsaSharedInputConfig, AlsaSharedInputBuffer]] = {}
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"alsa-shared-tap-{self.stable_id}", daemon=True)
        self.lock = threading.Lock()
        self.status = "disabled"
        self.error = ""
        self.device: AlsaPlaybackDevice | None = None
        self.playback: AlsaPcmPlayback | None = None

    def start(self) -> None:
        if self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=2.0)
        self._close_playback()
        self._set_status("disabled")

    def register_input(self, input_id: str, config: AlsaSharedInputConfig) -> None:
        if config.channel_mode not in ALSA_CHANNEL_MODES:
            raise AlsaError(f"unsupported ALSA channel mode: {config.channel_mode}")
        AlsaSoftwareVolume(config.software_volume)
        with self.lock:
            existing = self.inputs.get(str(input_id))
            buffer = existing[1] if existing is not None else AlsaSharedInputBuffer(ALSA_STREAM_SOURCE_SAMPLE_RATE, self.output_sample_rate)
            self.inputs[str(input_id)] = (config, buffer)

    def unregister_input(self, input_id: str) -> None:
        with self.lock:
            self.inputs.pop(str(input_id), None)

    def has_inputs(self) -> bool:
        with self.lock:
            return bool(self.inputs)

    def push_float(self, input_id: str, samples: np.ndarray) -> None:
        with self.lock:
            item = self.inputs.get(str(input_id))
        if item is not None:
            item[1].push(samples)

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            status = self.status
            error = self.error
            device = self.device
            playback = self.playback
            input_stats = {key: buffer.stats() for key, (_config, buffer) in self.inputs.items()}
        return {
            "status": status,
            "error": error,
            "stable_id": self.stable_id,
            "device": device.hw_device if device is not None else "",
            "inputs": input_stats,
            "playback": playback.snapshot() if playback is not None else {},
        }

    def prepare_for_usb_reset(self, hold_seconds: float = 8.0) -> None:
        with self.lock:
            for _config, buffer in self.inputs.values():
                buffer.clear()
            self.reopen_block_until = max(self.reopen_block_until, time.monotonic() + max(0.0, float(hold_seconds)))
        self._set_status("needs-attention", "soundcard is being reset")
        self._close_playback()

    def _run(self) -> None:
        frames_per_period = max(1, round(self.output_sample_rate * ALSA_STREAM_FRAME_SECONDS))
        next_frame_at = time.monotonic()
        while not self.stop_event.is_set():
            try:
                if self.playback is None:
                    with self.lock:
                        reopen_block_until = self.reopen_block_until
                    now = time.monotonic()
                    if reopen_block_until > now:
                        self.stop_event.wait(min(reopen_block_until - now, ALSA_STREAM_RECONNECT_SECONDS))
                        continue
                    self._open_playback()
                    next_frame_at = time.monotonic()
                    continue
                now = time.monotonic()
                if next_frame_at > now:
                    self.stop_event.wait(next_frame_at - now)
                    continue
                self.playback.write_float32(self._mix_frame(frames_per_period), input_channels=ALSA_STREAM_OUTPUT_CHANNELS)
                next_frame_at += ALSA_STREAM_FRAME_SECONDS
                if next_frame_at < now - 0.25:
                    next_frame_at = now + ALSA_STREAM_FRAME_SECONDS
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                self._set_status("needs-attention", str(exc))
                self._close_playback()
                self.stop_event.wait(ALSA_STREAM_RECONNECT_SECONDS)
        self._close_playback()

    def _mix_frame(self, frame_count: int) -> np.ndarray:
        output = np.zeros((frame_count, ALSA_STREAM_OUTPUT_CHANNELS), dtype=np.float32)
        with self.lock:
            items = list(self.inputs.values())
        for config, buffer in items:
            mono = buffer.read(frame_count)
            if config.software_volume != 1.0:
                mono = apply_software_volume_float32(mono, config.software_volume)
            if config.channel_mode in {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_LEFT}:
                output[:, 0] += mono
            if config.channel_mode in {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_RIGHT}:
                output[:, 1] += mono
        np.clip(output, -1.0, 1.0, out=output)
        return np.ascontiguousarray(output).reshape(-1)

    def _open_playback(self) -> None:
        device = resolve_playback_device(self.stable_id, devices=self.devices_provider())
        if device is None:
            with self.lock:
                self.device = None
            self._set_status("needs-attention", "soundcard is not currently connected")
            self.stop_event.wait(ALSA_STREAM_RECONNECT_SECONDS)
            return
        playback = self.playback_factory(
            device,
            sample_rate=self.output_sample_rate,
            channels=ALSA_STREAM_OUTPUT_CHANNELS,
        )
        with self.lock:
            for _config, buffer in self.inputs.values():
                buffer.clear()
            self.device = device
            self.playback = playback
        self._set_status("enabled")
        LOG.info("started shared ALSA stream tap on %s for %s", device.hw_device, self.stable_id)

    def _close_playback(self) -> None:
        with self.lock:
            playback = self.playback
            self.playback = None
            self.device = None
        if playback is not None:
            try:
                playback.close()
            except Exception:
                LOG.debug("failed to close shared ALSA stream tap playback", exc_info=True)

    def _set_status(self, status: str, error: str = "") -> None:
        with self.lock:
            self.status = status
            self.error = error


class AlsaPcmPlayback:
    def __init__(
        self,
        device: AlsaPlaybackDevice | str,
        *,
        sample_rate: int,
        channels: int = 1,
        software_volume: float = 1.0,
        normalize_mixer: bool = True,
        library: ctypes.CDLL | None = None,
    ) -> None:
        if sample_rate <= 0:
            raise AlsaError("ALSA playback sample rate must be positive")
        if channels <= 0:
            raise AlsaError("ALSA playback channel count must be positive")
        self.device = device.hw_device if isinstance(device, AlsaPlaybackDevice) else str(device)
        self.card_index = device.card_index if isinstance(device, AlsaPlaybackDevice) else _card_index_from_hw_device(self.device)
        self.requested_sample_rate = int(sample_rate)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.pcm_format = SND_PCM_FORMAT_S16_LE
        self.pcm_format_name = "S16_LE"
        self.sample_bytes = 2
        self.volume = AlsaSoftwareVolume(software_volume)
        self.lib = library or _load_libasound()
        _configure_playback_signatures(self.lib)
        self.lock = threading.RLock()
        self.handle = ctypes.c_void_p()
        self.mixer_adjustments: tuple[AlsaMixerAdjustment, ...] = ()
        self.write_calls = 0
        self.nonzero_write_calls = 0
        self.frames_written = 0
        self.recoveries = 0
        self.last_write_at = 0.0
        if normalize_mixer and self.card_index is not None:
            self.mixer_adjustments = tuple(normalize_playback_mixer_to_unity(self.card_index, library=self.lib))
        self._open()

    def snapshot(self) -> dict[str, object]:
        with self.lock:
            return {
                "device": self.device,
                "requested_sample_rate": self.requested_sample_rate,
                "sample_rate": self.sample_rate,
                "channels": self.channels,
                "format": self.pcm_format_name,
                "sample_bytes": self.sample_bytes,
                "write_calls": self.write_calls,
                "nonzero_write_calls": self.nonzero_write_calls,
                "frames_written": self.frames_written,
                "recoveries": self.recoveries,
                "last_write_at": self.last_write_at,
                "mixer_adjustments": [
                    {
                        "name": adjustment.control_name,
                        "kind": adjustment.kind,
                        "target_db": adjustment.target_db,
                    }
                    for adjustment in self.mixer_adjustments
                ],
            }

    def write(self, pcm_s16le: bytes, *, input_channels: int = 1) -> None:
        if not pcm_s16le:
            return
        samples = s16le_to_float32(pcm_s16le)
        self.write_float32(samples, input_channels=input_channels)

    def write_float32(self, pcm_float32: bytes | np.ndarray, *, input_channels: int = 1) -> None:
        if isinstance(pcm_float32, bytes):
            if not pcm_float32:
                return
            samples = np.frombuffer(pcm_float32, dtype="<f4")
        else:
            samples = np.asarray(pcm_float32, dtype=np.float32)
            if samples.size == 0:
                return
        with self.lock:
            handle = self.handle
            if not handle:
                raise AlsaError("ALSA playback device is closed")
            routed = convert_float32_channels(
                self.volume.apply_float32(samples),
                input_channels=input_channels,
                output_channels=self.channels,
            )
            pcm = convert_float32_sample_format(routed, self.pcm_format)
            frame_bytes = self.channels * self.sample_bytes
            if len(pcm) % frame_bytes:
                pcm = pcm[: len(pcm) - (len(pcm) % frame_bytes)]
            if not pcm:
                return
            self.write_calls += 1
            if np.any(routed):
                self.nonzero_write_calls += 1
            frames_total = len(pcm) // frame_bytes
            offset_frames = 0
            view = memoryview(pcm)
            while offset_frames < frames_total:
                offset_bytes = offset_frames * frame_bytes
                chunk = view[offset_bytes:]
                buffer = (ctypes.c_char * len(chunk)).from_buffer_copy(chunk)
                written = self.lib.snd_pcm_writei(
                    handle,
                    ctypes.cast(buffer, ctypes.c_void_p),
                    frames_total - offset_frames,
                )
                if written < 0:
                    recovered = self.lib.snd_pcm_recover(handle, int(written), 1)
                    if recovered < 0:
                        raise AlsaError(f"snd_pcm_writei failed: {_decode(self.lib.snd_strerror(int(written)))}")
                    self.recoveries += 1
                    continue
                if written == 0:
                    raise AlsaError("snd_pcm_writei wrote zero frames")
                self.frames_written += int(written)
                self.last_write_at = time.time()
                offset_frames += int(written)

    def close(self) -> None:
        with self.lock:
            handle = self.handle
            self.handle = ctypes.c_void_p()
            if handle:
                try:
                    drop = getattr(self.lib, "snd_pcm_drop", None)
                    if drop is not None:
                        drop(handle)
                    else:
                        self.lib.snd_pcm_drain(handle)
                finally:
                    self.lib.snd_pcm_close(handle)

    def __enter__(self) -> AlsaPcmPlayback:
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.close()

    def _open(self) -> None:
        _check_alsa(
            self.lib.snd_pcm_open(
                ctypes.byref(self.handle),
                self.device.encode(),
                SND_PCM_STREAM_PLAYBACK,
                0,
            ),
            self.lib,
            f"snd_pcm_open({self.device!r})",
        )
        params = ctypes.c_void_p()
        try:
            _check_alsa(self.lib.snd_pcm_hw_params_malloc(ctypes.byref(params)), self.lib, "snd_pcm_hw_params_malloc")
            _check_alsa(self.lib.snd_pcm_hw_params_any(self.handle, params), self.lib, "snd_pcm_hw_params_any")
            _check_alsa(
                self.lib.snd_pcm_hw_params_set_access(
                    self.handle,
                    params,
                    SND_PCM_ACCESS_RW_INTERLEAVED,
                ),
                self.lib,
                "snd_pcm_hw_params_set_access",
            )
            self._set_supported_format(params)
            requested_channels = ctypes.c_uint(self.channels)
            _check_alsa(
                self.lib.snd_pcm_hw_params_set_channels_near(
                    self.handle,
                    params,
                    ctypes.byref(requested_channels),
                ),
                self.lib,
                "snd_pcm_hw_params_set_channels_near",
            )
            self.channels = int(requested_channels.value)
            requested_rate = ctypes.c_uint(self.sample_rate)
            direction = ctypes.c_int(0)
            _check_alsa(
                self.lib.snd_pcm_hw_params_set_rate_near(
                    self.handle,
                    params,
                    ctypes.byref(requested_rate),
                    ctypes.byref(direction),
                ),
                self.lib,
                "snd_pcm_hw_params_set_rate_near",
            )
            self.sample_rate = int(requested_rate.value)
            buffer_time_us = ctypes.c_uint(ALSA_HW_BUFFER_TIME_US)
            period_time_us = ctypes.c_uint(ALSA_HW_PERIOD_TIME_US)
            self.lib.snd_pcm_hw_params_set_buffer_time_near(
                self.handle,
                params,
                ctypes.byref(buffer_time_us),
                ctypes.byref(direction),
            )
            self.lib.snd_pcm_hw_params_set_period_time_near(
                self.handle,
                params,
                ctypes.byref(period_time_us),
                ctypes.byref(direction),
            )
            _check_alsa(self.lib.snd_pcm_hw_params(self.handle, params), self.lib, "snd_pcm_hw_params")
            _check_alsa(self.lib.snd_pcm_prepare(self.handle), self.lib, "snd_pcm_prepare")
            LOG.info(
                "opened ALSA playback %s requested_rate=%s actual_rate=%s channels=%s format=%s buffer_time_us=%s period_time_us=%s mixer=%s",
                self.device,
                self.requested_sample_rate,
                self.sample_rate,
                self.channels,
                self.pcm_format_name,
                int(buffer_time_us.value),
                int(period_time_us.value),
                [
                    {
                        "name": adjustment.control_name,
                        "kind": adjustment.kind,
                        "target_db": adjustment.target_db,
                    }
                    for adjustment in self.mixer_adjustments
                ],
            )
        finally:
            if params:
                self.lib.snd_pcm_hw_params_free(params)

    def _set_supported_format(self, params: ctypes.c_void_p) -> None:
        errors: list[str] = []
        for pcm_format, name, sample_bytes in ALSA_PLAYBACK_FORMATS:
            result = self.lib.snd_pcm_hw_params_set_format(self.handle, params, pcm_format)
            if result >= 0:
                self.pcm_format = pcm_format
                self.pcm_format_name = name
                self.sample_bytes = sample_bytes
                return
            errors.append(f"{name}: {_decode(self.lib.snd_strerror(int(result)))}")
        raise AlsaError("ALSA playback device does not support a usable PCM format: " + "; ".join(errors))


def apply_software_volume_s16le(pcm_s16le: bytes, volume: float) -> bytes:
    if not pcm_s16le or volume == 1.0:
        return pcm_s16le
    if volume < 0:
        raise AlsaError("software volume must not be negative")
    samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32)
    samples *= float(volume)
    np.clip(samples, -32768.0, 32767.0, out=samples)
    return samples.astype("<i2").tobytes()


def apply_software_volume_float32(samples: np.ndarray, volume: float) -> np.ndarray:
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0 or volume == 1.0:
        return samples
    if volume < 0:
        raise AlsaError("software volume must not be negative")
    output = samples.astype(np.float32, copy=True)
    output *= float(volume)
    np.clip(output, -1.0, 1.0, out=output)
    return output


class FloatPcmResampler:
    def __init__(self, input_rate: int, output_rate: int) -> None:
        self.input_rate = int(input_rate)
        self.output_rate = int(output_rate)
        self.stream = None
        if self.input_rate != self.output_rate:
            try:
                import soxr
            except ImportError as exc:
                raise AlsaError("soundcard sample-rate conversion requires the 'soxr' Python package") from exc
            self.stream = soxr.ResampleStream(
                self.input_rate,
                self.output_rate,
                1,
                dtype="float32",
            )

    def process(self, samples: np.ndarray) -> np.ndarray:
        samples = np.asarray(samples, dtype=np.float32)
        if samples.size == 0:
            return np.empty(0, dtype=np.float32)
        if self.stream is None:
            return samples
        return self.stream.resample_chunk(samples, last=False).astype(np.float32, copy=False)

    def flush(self) -> np.ndarray:
        if self.stream is None:
            return np.empty(0, dtype=np.float32)
        return self.stream.resample_chunk(np.empty(0, dtype=np.float32), last=True).astype(np.float32, copy=False)


def s16le_to_float32(pcm_s16le: bytes) -> np.ndarray:
    if not pcm_s16le:
        return np.empty(0, dtype=np.float32)
    return (np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32) / 32768.0).astype(np.float32, copy=False)


def mono_float32_to_stereo(samples: np.ndarray, *, mode: str = ALSA_CHANNEL_BOTH) -> np.ndarray:
    if mode not in ALSA_CHANNEL_MODES:
        raise AlsaError(f"unsupported ALSA channel mode: {mode}")
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)
    stereo = np.zeros((samples.size, 2), dtype=np.float32)
    if mode in {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_LEFT}:
        stereo[:, 0] = samples
    if mode in {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_RIGHT}:
        stereo[:, 1] = samples
    return np.ascontiguousarray(stereo).reshape(-1)


def convert_float32_channels(samples: np.ndarray, *, input_channels: int, output_channels: int) -> np.ndarray:
    if input_channels <= 0 or output_channels <= 0:
        raise AlsaError("channel counts must be positive")
    samples = np.asarray(samples, dtype=np.float32)
    frames = samples.size // input_channels
    if frames <= 0:
        return np.empty(0, dtype=np.float32)
    samples = samples[: frames * input_channels].reshape(frames, input_channels)
    if input_channels == output_channels:
        return np.ascontiguousarray(samples).reshape(-1)
    if input_channels == 1 and output_channels == 2:
        return np.ascontiguousarray(np.repeat(samples, 2, axis=1)).reshape(-1)
    if output_channels == 1:
        return np.ascontiguousarray(samples.mean(axis=1, dtype=np.float32)).reshape(-1)
    output = np.zeros((frames, output_channels), dtype=np.float32)
    shared = min(input_channels, output_channels)
    output[:, :shared] = samples[:, :shared]
    if input_channels == 1 and output_channels > 1:
        output[:, :2] = samples
    return np.ascontiguousarray(output).reshape(-1)


def mono_s16le_to_stereo(pcm_s16le: bytes, *, mode: str = ALSA_CHANNEL_BOTH) -> bytes:
    if mode not in ALSA_CHANNEL_MODES:
        raise AlsaError(f"unsupported ALSA channel mode: {mode}")
    samples = np.frombuffer(pcm_s16le, dtype="<i2")
    if samples.size == 0:
        return b""
    stereo = np.zeros((samples.size, 2), dtype="<i2")
    if mode in {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_LEFT}:
        stereo[:, 0] = samples
    if mode in {ALSA_CHANNEL_BOTH, ALSA_CHANNEL_RIGHT}:
        stereo[:, 1] = samples
    return np.ascontiguousarray(stereo).tobytes()


def convert_s16le_channels(pcm_s16le: bytes, *, input_channels: int, output_channels: int) -> bytes:
    if input_channels <= 0 or output_channels <= 0:
        raise AlsaError("channel counts must be positive")
    if input_channels == output_channels:
        return pcm_s16le
    samples = np.frombuffer(pcm_s16le, dtype="<i2")
    frames = len(samples) // input_channels
    if frames <= 0:
        return b""
    samples = samples[: frames * input_channels].reshape(frames, input_channels)
    if input_channels == 1:
        output = np.repeat(samples, output_channels, axis=1)
    elif output_channels == 1:
        output = np.rint(samples.astype(np.float32).mean(axis=1)).astype("<i2").reshape(frames, 1)
    else:
        output = np.zeros((frames, output_channels), dtype="<i2")
        shared = min(input_channels, output_channels)
        output[:, :shared] = samples[:, :shared]
        if output_channels > input_channels:
            output[:, input_channels:] = samples[:, input_channels - 1 : input_channels]
    return np.ascontiguousarray(output).astype("<i2", copy=False).tobytes()


def convert_s16le_sample_format(pcm_s16le: bytes, pcm_format: int) -> bytes:
    if pcm_format == SND_PCM_FORMAT_S16_LE:
        return pcm_s16le
    return convert_float32_sample_format(s16le_to_float32(pcm_s16le), pcm_format)


def convert_float32_sample_format(samples: np.ndarray, pcm_format: int) -> bytes:
    clipped = np.clip(np.asarray(samples, dtype=np.float32), -1.0, 1.0)
    if clipped.size == 0:
        return b""
    if pcm_format == SND_PCM_FORMAT_FLOAT_LE:
        return np.ascontiguousarray(clipped.astype("<f4", copy=False)).tobytes()
    if pcm_format == SND_PCM_FORMAT_S8:
        return np.rint(clipped * 127.0).astype(np.int8).tobytes()
    if pcm_format == SND_PCM_FORMAT_U8:
        return np.rint((clipped.astype(np.float32) + 1.0) * 127.5).astype(np.uint8).tobytes()
    if pcm_format == SND_PCM_FORMAT_S16_LE:
        return np.rint(clipped * 32767.0).astype("<i2").tobytes()
    if pcm_format == SND_PCM_FORMAT_U16_LE:
        return np.rint((clipped.astype(np.float64) + 1.0) * 32767.5).astype("<u2").tobytes()
    if pcm_format in {SND_PCM_FORMAT_S24_3LE, SND_PCM_FORMAT_S24_LE}:
        values = np.rint(clipped * 8_388_607.0).astype("<i4")
        if pcm_format == SND_PCM_FORMAT_S24_LE:
            return (values << 8).astype("<i4", copy=False).tobytes()
        unsigned = values.view(np.uint8).reshape(-1, 4)
        packed = np.empty(values.size * 3, dtype=np.uint8)
        packed[0::3] = unsigned[:, 0]
        packed[1::3] = unsigned[:, 1]
        packed[2::3] = unsigned[:, 2]
        return packed.tobytes()
    if pcm_format in {SND_PCM_FORMAT_U24_3LE, SND_PCM_FORMAT_U24_LE}:
        values = np.rint((clipped.astype(np.float64) + 1.0) * 8_388_607.5).astype("<u4")
        if pcm_format == SND_PCM_FORMAT_U24_LE:
            return (values << 8).astype("<u4", copy=False).tobytes()
        unsigned = values.view(np.uint8).reshape(-1, 4)
        packed = np.empty(values.size * 3, dtype=np.uint8)
        packed[0::3] = unsigned[:, 0]
        packed[1::3] = unsigned[:, 1]
        packed[2::3] = unsigned[:, 2]
        return packed.tobytes()
    if pcm_format == SND_PCM_FORMAT_S32_LE:
        return np.rint(clipped.astype(np.float64) * 2_147_483_647.0).astype("<i4").tobytes()
    if pcm_format == SND_PCM_FORMAT_U32_LE:
        return np.rint((clipped.astype(np.float64) + 1.0) * 2_147_483_647.5).astype("<u4").tobytes()
    raise AlsaError(f"unsupported ALSA PCM format: {pcm_format}")


def normalize_playback_mixer_to_unity(
    card_index: int,
    *,
    library: ctypes.CDLL | None = None,
) -> list[AlsaMixerAdjustment]:
    lib = library or _load_libasound()
    _configure_mixer_signatures(lib)
    handle = ctypes.c_void_p()
    name = f"hw:{int(card_index)}".encode()
    _check_alsa(lib.snd_mixer_open(ctypes.byref(handle), 0), lib, "snd_mixer_open")
    adjustments: list[AlsaMixerAdjustment] = []
    try:
        _check_alsa(lib.snd_mixer_attach(handle, name), lib, f"snd_mixer_attach({name!r})")
        _check_alsa(lib.snd_mixer_selem_register(handle, None, None), lib, "snd_mixer_selem_register")
        _check_alsa(lib.snd_mixer_load(handle), lib, "snd_mixer_load")
        element = lib.snd_mixer_first_elem(handle)
        while element:
            if (
                lib.snd_mixer_selem_is_active(element)
                and lib.snd_mixer_selem_has_playback_volume(element)
            ):
                minimum = ctypes.c_long()
                maximum = ctypes.c_long()
                if lib.snd_mixer_selem_get_playback_dB_range(element, ctypes.byref(minimum), ctypes.byref(maximum)) >= 0:
                    target = mixer_unity_target_mb(int(minimum.value), int(maximum.value))
                    if target is None:
                        LOG.info(
                            "not changing ALSA playback volume %s on hw:%s: available dB range %.2f..%.2f only boosts audio",
                            _decode(lib.snd_mixer_selem_get_name(element)),
                            int(card_index),
                            int(minimum.value) / 100.0,
                            int(maximum.value) / 100.0,
                        )
                    elif lib.snd_mixer_selem_set_playback_dB_all(element, int(target), -1) >= 0:
                        adjustments.append(
                            AlsaMixerAdjustment(
                                control_name=_decode(lib.snd_mixer_selem_get_name(element)),
                                minimum_mb=int(minimum.value),
                                maximum_mb=int(maximum.value),
                                target_mb=int(target),
                            )
                        )
            if (
                lib.snd_mixer_selem_is_active(element)
                and lib.snd_mixer_selem_has_playback_switch(element)
                and lib.snd_mixer_selem_set_playback_switch_all(element, 1) >= 0
            ):
                adjustments.append(
                    AlsaMixerAdjustment(
                        control_name=_decode(lib.snd_mixer_selem_get_name(element)),
                        minimum_mb=0,
                        maximum_mb=100,
                        target_mb=100,
                        kind="switch",
                    )
                )
            element = lib.snd_mixer_elem_next(element)
    finally:
        if handle:
            lib.snd_mixer_close(handle)
    return adjustments


def mixer_unity_target_mb(minimum_mb: int, maximum_mb: int) -> int | None:
    if minimum_mb <= 0 <= maximum_mb:
        return 0
    if maximum_mb < 0:
        return maximum_mb
    return None


def _stable_id(*parts: object) -> str:
    return ":".join(_stable_part(str(part)) for part in parts if str(part).strip())


def _stable_part(value: str) -> str:
    value = value.strip().casefold()
    output = []
    last_dash = False
    for char in value:
        if char.isalnum():
            output.append(char)
            last_dash = False
        elif not last_dash:
            output.append("-")
            last_dash = True
    return "".join(output).strip("-") or "unknown"


def _normalized_token(value: str) -> str:
    return value.strip().casefold()


def _card_index_from_hw_device(device: str) -> int | None:
    match = re.match(r"^hw:(?P<card>\d+)(?:,\d+)?$", device.strip())
    if match is None:
        return None
    return int(match.group("card"))


class CtypesAlsaBackend:
    def __init__(self, library: ctypes.CDLL | None = None, sysfs_root: Path = Path("/sys/class/sound")) -> None:
        self.lib = library or _load_libasound()
        self.sysfs_root = sysfs_root
        self._configure_signatures()

    def card_indices(self) -> list[int]:
        indices: list[int] = []
        card = ctypes.c_int(-1)
        while True:
            _check_alsa(self.lib.snd_card_next(ctypes.byref(card)), self.lib, "snd_card_next")
            if card.value < 0:
                break
            indices.append(int(card.value))
        return indices

    def card_info(self, card_index: int) -> AlsaCardInfo:
        handle = ctypes.c_void_p()
        name = f"hw:{int(card_index)}".encode()
        _check_alsa(self.lib.snd_ctl_open(ctypes.byref(handle), name, 0), self.lib, f"snd_ctl_open({name!r})")
        info = ctypes.c_void_p()
        try:
            _check_alsa(self.lib.snd_ctl_card_info_malloc(ctypes.byref(info)), self.lib, "snd_ctl_card_info_malloc")
            _check_alsa(self.lib.snd_ctl_card_info(handle, info), self.lib, "snd_ctl_card_info")
            return AlsaCardInfo(
                index=int(card_index),
                card_id=_decode(self.lib.snd_ctl_card_info_get_id(info)),
                name=_decode(self.lib.snd_ctl_card_info_get_name(info)),
                long_name=_decode(self.lib.snd_ctl_card_info_get_longname(info)),
                mixer_name=_decode(self.lib.snd_ctl_card_info_get_mixername(info)),
                components=_decode(self.lib.snd_ctl_card_info_get_components(info)),
                sysfs=_read_card_sysfs_identity(self.sysfs_root, int(card_index)),
            )
        finally:
            if info:
                self.lib.snd_ctl_card_info_free(info)
            if handle:
                self.lib.snd_ctl_close(handle)

    def playback_pcm_devices(self, card_index: int) -> list[AlsaPcmInfo]:
        handle = ctypes.c_void_p()
        name = f"hw:{int(card_index)}".encode()
        _check_alsa(self.lib.snd_ctl_open(ctypes.byref(handle), name, 0), self.lib, f"snd_ctl_open({name!r})")
        pcm_devices: list[AlsaPcmInfo] = []
        info = ctypes.c_void_p()
        try:
            _check_alsa(self.lib.snd_pcm_info_malloc(ctypes.byref(info)), self.lib, "snd_pcm_info_malloc")
            device = ctypes.c_int(-1)
            while True:
                _check_alsa(self.lib.snd_ctl_pcm_next_device(handle, ctypes.byref(device)), self.lib, "snd_ctl_pcm_next_device")
                if device.value < 0:
                    break
                self.lib.snd_pcm_info_set_device(info, int(device.value))
                self.lib.snd_pcm_info_set_subdevice(info, 0)
                self.lib.snd_pcm_info_set_stream(info, SND_PCM_STREAM_PLAYBACK)
                result = self.lib.snd_ctl_pcm_info(handle, info)
                if result < 0:
                    continue
                pcm_devices.append(
                    AlsaPcmInfo(
                        device=int(device.value),
                        pcm_id=_decode(self.lib.snd_pcm_info_get_id(info)),
                        name=_decode(self.lib.snd_pcm_info_get_name(info)),
                        subdevices_count=int(self.lib.snd_pcm_info_get_subdevices_count(info)),
                        subdevices_available=int(self.lib.snd_pcm_info_get_subdevices_avail(info)),
                    )
                )
        finally:
            if info:
                self.lib.snd_pcm_info_free(info)
            if handle:
                self.lib.snd_ctl_close(handle)
        return pcm_devices

    def _configure_signatures(self) -> None:
        self.lib.snd_strerror.argtypes = [ctypes.c_int]
        self.lib.snd_strerror.restype = ctypes.c_char_p
        self.lib.snd_card_next.argtypes = [ctypes.POINTER(ctypes.c_int)]
        self.lib.snd_card_next.restype = ctypes.c_int
        self.lib.snd_ctl_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_char_p, ctypes.c_int]
        self.lib.snd_ctl_open.restype = ctypes.c_int
        self.lib.snd_ctl_close.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_close.restype = ctypes.c_int
        self.lib.snd_ctl_card_info_malloc.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.snd_ctl_card_info_malloc.restype = ctypes.c_int
        self.lib.snd_ctl_card_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.snd_ctl_card_info.restype = ctypes.c_int
        self.lib.snd_ctl_card_info_free.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_card_info_free.restype = None
        self.lib.snd_ctl_card_info_get_id.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_card_info_get_id.restype = ctypes.c_char_p
        self.lib.snd_ctl_card_info_get_name.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_card_info_get_name.restype = ctypes.c_char_p
        self.lib.snd_ctl_card_info_get_longname.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_card_info_get_longname.restype = ctypes.c_char_p
        self.lib.snd_ctl_card_info_get_mixername.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_card_info_get_mixername.restype = ctypes.c_char_p
        self.lib.snd_ctl_card_info_get_components.argtypes = [ctypes.c_void_p]
        self.lib.snd_ctl_card_info_get_components.restype = ctypes.c_char_p
        self.lib.snd_ctl_pcm_next_device.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
        self.lib.snd_ctl_pcm_next_device.restype = ctypes.c_int
        self.lib.snd_pcm_info_malloc.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        self.lib.snd_pcm_info_malloc.restype = ctypes.c_int
        self.lib.snd_pcm_info_free.argtypes = [ctypes.c_void_p]
        self.lib.snd_pcm_info_free.restype = None
        self.lib.snd_pcm_info_set_device.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.lib.snd_pcm_info_set_device.restype = None
        self.lib.snd_pcm_info_set_subdevice.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self.lib.snd_pcm_info_set_subdevice.restype = None
        self.lib.snd_pcm_info_set_stream.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.snd_pcm_info_set_stream.restype = None
        self.lib.snd_ctl_pcm_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.lib.snd_ctl_pcm_info.restype = ctypes.c_int
        self.lib.snd_pcm_info_get_id.argtypes = [ctypes.c_void_p]
        self.lib.snd_pcm_info_get_id.restype = ctypes.c_char_p
        self.lib.snd_pcm_info_get_name.argtypes = [ctypes.c_void_p]
        self.lib.snd_pcm_info_get_name.restype = ctypes.c_char_p
        self.lib.snd_pcm_info_get_subdevices_count.argtypes = [ctypes.c_void_p]
        self.lib.snd_pcm_info_get_subdevices_count.restype = ctypes.c_uint
        self.lib.snd_pcm_info_get_subdevices_avail.argtypes = [ctypes.c_void_p]
        self.lib.snd_pcm_info_get_subdevices_avail.restype = ctypes.c_uint


class ProcfsAlsaBackend:
    PCM_DIR_RE = re.compile(r"^pcm(?P<device>\d+)p$")

    def __init__(
        self,
        proc_root: Path = Path("/proc/asound"),
        sysfs_root: Path = Path("/sys/class/sound"),
    ) -> None:
        self.proc_root = proc_root
        self.sysfs_root = sysfs_root

    def card_indices(self) -> list[int]:
        indices: list[int] = []
        try:
            children = list(self.proc_root.iterdir())
        except OSError:
            return []
        for child in children:
            if not child.name.startswith("card"):
                continue
            try:
                indices.append(int(child.name[4:]))
            except ValueError:
                continue
        return sorted(indices)

    def card_info(self, card_index: int) -> AlsaCardInfo:
        card_id = _read_text_file(self.proc_root / f"card{card_index}" / "id")
        short_name, long_name = self._card_names_from_cards_file(card_index)
        return AlsaCardInfo(
            index=int(card_index),
            card_id=card_id,
            name=short_name or card_id,
            long_name=long_name,
            mixer_name="",
            components="",
            sysfs=_read_card_sysfs_identity(self.sysfs_root, int(card_index)),
        )

    def playback_pcm_devices(self, card_index: int) -> list[AlsaPcmInfo]:
        card_path = self.proc_root / f"card{card_index}"
        try:
            children = list(card_path.iterdir())
        except OSError:
            return []
        devices: list[AlsaPcmInfo] = []
        for child in sorted(children, key=lambda path: path.name):
            match = self.PCM_DIR_RE.match(child.name)
            if match is None:
                continue
            info = _parse_proc_asound_info(child / "info")
            try:
                device = int(info.get("device", match.group("device")))
            except ValueError:
                continue
            devices.append(
                AlsaPcmInfo(
                    device=device,
                    pcm_id=info.get("id", ""),
                    name=info.get("name", ""),
                    subdevices_count=_parse_int(info.get("subdevices_count"), 0),
                    subdevices_available=_parse_int(info.get("subdevices_avail"), 0),
                )
            )
        return sorted(devices, key=lambda pcm: pcm.device)

    def _card_names_from_cards_file(self, card_index: int) -> tuple[str, str]:
        try:
            lines = (self.proc_root / "cards").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "", ""
        for index, line in enumerate(lines):
            if not re.match(rf"\s*{card_index}\s+\[", line):
                continue
            short_name = ""
            if ":" in line:
                short_name = line.split(":", 1)[1].strip()
            long_name = lines[index + 1].strip() if index + 1 < len(lines) else ""
            return short_name, long_name
        return "", ""


def _load_libasound() -> ctypes.CDLL:
    path = ctypes.util.find_library("asound")
    if not path:
        raise AlsaError("ALSA hardware discovery requires libasound.so, but it was not found")
    try:
        return ctypes.CDLL(path)
    except OSError as exc:
        raise AlsaError(f"failed to load ALSA library {path}: {exc}") from exc


def _configure_playback_signatures(lib: ctypes.CDLL) -> None:
    lib.snd_strerror.argtypes = [ctypes.c_int]
    lib.snd_strerror.restype = ctypes.c_char_p
    lib.snd_pcm_open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    lib.snd_pcm_open.restype = ctypes.c_int
    lib.snd_pcm_close.argtypes = [ctypes.c_void_p]
    lib.snd_pcm_close.restype = ctypes.c_int
    lib.snd_pcm_drain.argtypes = [ctypes.c_void_p]
    lib.snd_pcm_drain.restype = ctypes.c_int
    drop = getattr(lib, "snd_pcm_drop", None)
    if drop is not None:
        drop.argtypes = [ctypes.c_void_p]
        drop.restype = ctypes.c_int
    lib.snd_pcm_prepare.argtypes = [ctypes.c_void_p]
    lib.snd_pcm_prepare.restype = ctypes.c_int
    lib.snd_pcm_recover.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    lib.snd_pcm_recover.restype = ctypes.c_int
    lib.snd_pcm_writei.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    lib.snd_pcm_writei.restype = ctypes.c_long
    lib.snd_pcm_hw_params_malloc.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.snd_pcm_hw_params_malloc.restype = ctypes.c_int
    lib.snd_pcm_hw_params_free.argtypes = [ctypes.c_void_p]
    lib.snd_pcm_hw_params_free.restype = None
    lib.snd_pcm_hw_params_any.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.snd_pcm_hw_params_any.restype = ctypes.c_int
    lib.snd_pcm_hw_params_set_access.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.snd_pcm_hw_params_set_access.restype = ctypes.c_int
    lib.snd_pcm_hw_params_set_format.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
    lib.snd_pcm_hw_params_set_format.restype = ctypes.c_int
    lib.snd_pcm_hw_params_set_channels_near.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
    ]
    lib.snd_pcm_hw_params_set_channels_near.restype = ctypes.c_int
    lib.snd_pcm_hw_params_set_rate_near.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.snd_pcm_hw_params_set_rate_near.restype = ctypes.c_int
    lib.snd_pcm_hw_params_set_buffer_time_near.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.snd_pcm_hw_params_set_buffer_time_near.restype = ctypes.c_int
    lib.snd_pcm_hw_params_set_period_time_near.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.snd_pcm_hw_params_set_period_time_near.restype = ctypes.c_int
    lib.snd_pcm_hw_params.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    lib.snd_pcm_hw_params.restype = ctypes.c_int


def _configure_mixer_signatures(lib: ctypes.CDLL) -> None:
    lib.snd_strerror.argtypes = [ctypes.c_int]
    lib.snd_strerror.restype = ctypes.c_char_p
    lib.snd_mixer_open.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_int]
    lib.snd_mixer_open.restype = ctypes.c_int
    lib.snd_mixer_close.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_close.restype = ctypes.c_int
    lib.snd_mixer_attach.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    lib.snd_mixer_attach.restype = ctypes.c_int
    lib.snd_mixer_selem_register.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.snd_mixer_selem_register.restype = ctypes.c_int
    lib.snd_mixer_load.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_load.restype = ctypes.c_int
    lib.snd_mixer_first_elem.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_first_elem.restype = ctypes.c_void_p
    lib.snd_mixer_elem_next.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_elem_next.restype = ctypes.c_void_p
    lib.snd_mixer_selem_is_active.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_is_active.restype = ctypes.c_int
    lib.snd_mixer_selem_has_playback_volume.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_has_playback_volume.restype = ctypes.c_int
    lib.snd_mixer_selem_has_playback_volume_joined.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_has_playback_volume_joined.restype = ctypes.c_int
    lib.snd_mixer_selem_has_playback_switch.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_has_playback_switch.restype = ctypes.c_int
    lib.snd_mixer_selem_has_playback_switch_joined.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_has_playback_switch_joined.restype = ctypes.c_int
    lib.snd_mixer_selem_set_playback_switch_all.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.snd_mixer_selem_set_playback_switch_all.restype = ctypes.c_int
    lib.snd_mixer_selem_get_playback_dB_range.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_long),
        ctypes.POINTER(ctypes.c_long),
    ]
    lib.snd_mixer_selem_get_playback_dB_range.restype = ctypes.c_int
    lib.snd_mixer_selem_set_playback_dB_all.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_int]
    lib.snd_mixer_selem_set_playback_dB_all.restype = ctypes.c_int
    lib.snd_mixer_selem_get_name.argtypes = [ctypes.c_void_p]
    lib.snd_mixer_selem_get_name.restype = ctypes.c_char_p


def _check_alsa(result: int, lib: ctypes.CDLL, operation: str) -> None:
    if result >= 0:
        return
    error = _decode(lib.snd_strerror(int(result)))
    raise AlsaError(f"{operation} failed: {error or result}")


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _read_card_sysfs_identity(sysfs_root: Path, card_index: int) -> AlsaSysfsIdentity:
    card_path = sysfs_root / f"card{card_index}"
    device_path = _resolve_device_path(card_path)
    if device_path is None:
        return AlsaSysfsIdentity()
    bus = _sysfs_bus(device_path)
    vendor_id = ""
    product_id = ""
    serial = ""
    if bus == "usb":
        vendor_id = _read_first_parent_file(device_path, ("idVendor",))
        product_id = _read_first_parent_file(device_path, ("idProduct",))
        serial = _read_first_parent_file(device_path, ("serial",))
    usb_port_path = _usb_port_path(device_path) if bus == "usb" else ""
    return AlsaSysfsIdentity(
        bus=bus,
        vendor_id=vendor_id,
        product_id=product_id,
        serial=serial,
        usb_port_path=usb_port_path,
        device_path=_relative_sysfs_path(device_path),
    )


def _resolve_device_path(card_path: Path) -> Path | None:
    try:
        return (card_path / "device").resolve(strict=True)
    except OSError:
        return None


def _read_first_parent_file(start: Path, names: tuple[str, ...]) -> str:
    for path in (start, *start.parents):
        for name in names:
            value = _read_text_file(path / name)
            if value:
                return value
    return ""


def _read_text_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _read_int_file(path: Path) -> int | None:
    value = _read_text_file(path)
    if not value:
        return None
    try:
        return int(value, 10)
    except ValueError:
        return None


def _relative_sysfs_path(path: Path) -> str:
    parts = list(path.parts)
    if "devices" in parts:
        return "/".join(parts[parts.index("devices") + 1 :])
    return str(path)


def _sysfs_bus(path: Path) -> str:
    parts = path.parts
    if any(part.startswith("usb") or re.match(r"\d+-\d+(?:\.\d+)*", part) for part in parts):
        return "usb"
    if any(part.startswith("pci") or re.match(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:", part) for part in parts):
        return "pci"
    return "unknown"


def _usb_port_path(path: Path) -> str:
    ports = []
    for part in path.parts:
        if re.match(r"\d+-\d+(?:\.\d+)*$", part):
            ports.append(part)
    return "/".join(ports)


def _parse_proc_asound_info(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    values: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default
