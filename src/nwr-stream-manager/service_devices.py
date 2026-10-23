"""The service's devices: finding RTL-SDRs and sound cards, resetting them, and RTL-SDR diagnostics."""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import replace
from typing import Any

from .alsa import playback_device_usb_node
from .constants import (
    NWR_RECEIVER_CHANNELS_HZ,
    REMOTE_SDR_RESET_UNSUPPORTED_MESSAGE,
    RTL_DIAGNOSTIC_MAX_BATCHES,
    RTL_DIAGNOSTIC_MIN_BATCHES,
    RTL_DIAGNOSTIC_QUEUE_SECONDS,
    RTL_DIAGNOSTIC_SECONDS,
    RTL_RESET_COMMAND_TIMEOUT_SECONDS,
    RTL_RESET_REAPPEAR_TIMEOUT_SECONDS,
    RTL_RESET_RESULT_SHOW_SECONDS,
    RTL_RESET_STARTED_MESSAGE,
    RTL_RESET_TOTAL_SECONDS,
    USB_VENDOR_NAMES,
)
from .diagnostics import channel_audio_diagnostics, raw_iq_diagnostics
from .dsp import rtl_u8_to_complex64
from .iq_pipeline import subscribe_raw_fanout
from .remote_sdr import RemoteSdrError
from .rtl import (
    list_usb_rtl_devices,
    reset_usb_device_node,
    reset_usb_rtl_device,
    RtlCaptureSource,
    RtlSampleBatch,
)
from .state_files import save_settings, save_soundcard_name_cache
from .stream_config import (
    friendly_rtl_device_label,
    friendly_soundcard_device_label,
    save_streams,
    stream_outputs,
)


LOG = logging.getLogger(__name__)


class DeviceServiceMixin:
    """Mixed into RtlControlService."""

    def devices(self) -> dict[str, Any]:
        errors: list[str] = []
        snapshot = self.device_probe.snapshot()
        rtl_devices = list(snapshot.devices("rtl"))
        usb_devices = list(snapshot.devices("rtl_usb"))
        soundcards = list(snapshot.devices("alsa_playback"))
        if snapshot.error("rtl"):
            errors.append(f"librtlsdr probe failed: {snapshot.error('rtl')}")
        if snapshot.error("rtl_usb"):
            errors.append(f"USB probe failed: {snapshot.error('rtl_usb')}")
        # Shown with the sound card choices, where it matters, rather than with the RTL-SDR's.
        soundcard_problem = snapshot.error("alsa_playback")

        by_serial: dict[str, dict[str, Any]] = {}
        for device in usb_devices:
            if not device.serial:
                continue
            by_serial[device.serial] = {
                "serial": device.serial,
                "name": device.description,
                "vendor": USB_VENDOR_NAMES.get(device.vendor_id.lower(), device.vendor_id),
                "vendor_id": device.vendor_id,
                "product_id": device.product_id,
                "source": "usb",
            }
        for device in rtl_devices:
            if not device.serial:
                continue
            entry = by_serial.setdefault(
                device.serial,
                {
                    "serial": device.serial,
                    "name": device.description,
                    "vendor": "",
                    "vendor_id": "",
                    "product_id": "",
                    "source": "librtlsdr",
                },
            )
            entry["name"] = device.description or entry["name"]
            entry["librtlsdr_index"] = device.index
        devices = sorted(by_serial.values(), key=lambda item: (item["name"], item["serial"]))
        soundcard_payloads = [self._soundcard_device_payload(device) for device in soundcards]
        self._remember_device_names_locked(devices, soundcard_payloads)
        return {
            "devices": devices,
            "soundcards": soundcard_payloads,
            "soundcard_problem": soundcard_problem,
            "errors": errors,
            "probed_at": snapshot.probed_at,
        }

    def _remember_device_names_locked(
        self,
        rtl_devices: list[dict[str, Any]],
        soundcards: list[dict[str, Any]],
    ) -> None:
        soundcard_names_changed = False
        streams_changed = False
        with self.lock:
            for device in rtl_devices:
                serial = str(device.get("serial", "")).strip()
                label = friendly_rtl_device_label(device)
                if serial and label:
                    self.rtl_device_name_cache[serial] = label
            for device in soundcards:
                stable_id = str(device.get("stable_id", "")).strip()
                label = friendly_soundcard_device_label(device)
                if stable_id and label:
                    if self.soundcard_name_cache.get(stable_id) != label:
                        self.soundcard_name_cache[stable_id] = label
                        soundcard_names_changed = True
                    if self._apply_soundcard_label_to_streams_locked(stable_id, label):
                        streams_changed = True
            if soundcard_names_changed:
                save_soundcard_name_cache(self.soundcard_names_path, self.soundcard_name_cache)
            if streams_changed:
                save_streams(self.streams_directory, self.streams)

    def _enrich_stream_soundcard_names_from_cache_locked(self) -> None:
        changed = False
        with self.lock:
            for stable_id, label in list(getattr(self, "soundcard_name_cache", {}).items()):
                if self._apply_soundcard_label_to_streams_locked(stable_id, label):
                    changed = True
            if changed:
                save_streams(self.streams_directory, self.streams)

    def _apply_soundcard_label_to_streams_locked(self, stable_id: str, label: str) -> bool:
        stable_id = str(stable_id or "").strip()
        label = str(label or "").strip()
        if not stable_id or not label:
            return False
        changed = False
        for stream in list(self.streams) + list(getattr(self, "preview_streams", {}).values()):
            for output in stream_outputs(stream):
                if output.get("type") != "soundcard":
                    continue
                soundcard = output.get("soundcard") if isinstance(output.get("soundcard"), dict) else {}
                if str(soundcard.get("stable_id", "")).strip() != stable_id:
                    continue
                if str(soundcard.get("display_name", "")).strip() == label:
                    continue
                soundcard = dict(soundcard)
                soundcard["display_name"] = label
                output["soundcard"] = soundcard
                changed = True
        return changed

    def _remember_soundcard_devices_locked(self, devices: list[Any]) -> None:
        if not devices:
            return
        self._remember_device_names_locked([], [self._soundcard_device_payload(device) for device in devices])

    def _cached_rtl_devices(self) -> list[Any]:
        return list(self.device_probe.devices("rtl"))

    def _cached_usb_rtl_devices(self) -> list[Any]:
        return list(self.device_probe.devices("rtl_usb"))

    def _cached_soundcards(self) -> list[Any]:
        devices = list(self.device_probe.devices("alsa_playback"))
        self._remember_soundcard_devices_locked(devices)
        return devices

    def _refresh_soundcards(self) -> list[Any]:
        devices = list(self.device_probe.snapshot(force=True).devices("alsa_playback"))
        self._remember_soundcard_devices_locked(devices)
        return devices

    @staticmethod
    def _soundcard_device_payload(device) -> dict[str, Any]:
        return {
            "stable_id": device.stable_id,
            "hw_device": device.hw_device,
            "card_index": device.card_index,
            "pcm_device": device.pcm_device,
            "card_id": device.card_id,
            "card_name": device.card_name,
            "card_long_name": device.card_long_name,
            "pcm_id": device.pcm_id,
            "pcm_name": device.pcm_name,
            "display_name": device.display_name,
            "bus": device.bus,
            "vendor_id": device.vendor_id,
            "product_id": device.product_id,
            "serial": device.serial,
            "usb_port_path": device.usb_port_path,
            "device_path": device.device_path,
            "subdevices_count": device.subdevices_count,
            "subdevices_available": device.subdevices_available,
        }

    def _soundcard_by_stable_id(self, stable_id: str):
        stable_id = str(stable_id or "").strip()
        if not stable_id:
            raise ValueError("Select a sound card to reset.")
        matches = [device for device in self._cached_soundcards() if device.stable_id == stable_id]
        if not matches:
            matches = [device for device in self._refresh_soundcards() if device.stable_id == stable_id]
        if not matches:
            raise ValueError("Sound card is not connected.")
        if len(matches) > 1:
            raise ValueError("Sound card stable ID is ambiguous.")
        return matches[0]

    def _friendly_soundcard_name_for_stable_id_locked(self, stable_id: str) -> str:
        stable_id = str(stable_id or "").strip()
        if not stable_id:
            return ""
        if not hasattr(self, "device_probe"):
            return getattr(self, "soundcard_name_cache", {}).get(stable_id, "")
        matches = [device for device in self._cached_soundcards() if device.stable_id == stable_id]
        if not matches:
            matches = [device for device in self._refresh_soundcards() if device.stable_id == stable_id]
        if not matches:
            return getattr(self, "soundcard_name_cache", {}).get(stable_id, "")
        payload = self._soundcard_device_payload(matches[0])
        label = friendly_soundcard_device_label(payload)
        if label:
            getattr(self, "soundcard_name_cache", {})[stable_id] = label
        return label

    def reset_soundcard_device(self, stable_id: str) -> dict[str, Any]:
        if not self.reset_lock.acquire(blocking=False):
            raise ValueError("A USB device reset is already in progress.")
        try:
            return self._reset_soundcard_device(stable_id)
        finally:
            self.reset_lock.release()

    def _reset_soundcard_device(self, stable_id: str) -> dict[str, Any]:
        stable_id = str(stable_id or "").strip()
        device = self._soundcard_by_stable_id(stable_id)
        if device.bus != "usb":
            raise ValueError("Only USB sound cards can be reset.")
        usb_node = playback_device_usb_node(device)
        LOG.info("pausing soundcard outputs before USB reset for %s", stable_id)
        self.soundcard_manager.prepare_stable_id_for_reset(stable_id)
        LOG.info("resetting USB soundcard %s at %s", stable_id, usb_node)
        reset_queue: queue.Queue = queue.Queue(maxsize=1)

        def run_reset() -> None:
            try:
                reset_queue.put((reset_usb_device_node(usb_node, timeout_seconds=RTL_RESET_COMMAND_TIMEOUT_SECONDS), None))
            except BaseException as exc:
                reset_queue.put((None, exc))

        reset_thread = threading.Thread(
            target=run_reset,
            name=f"soundcard-usb-reset-{device.card_id or device.card_index}",
            daemon=True,
        )
        reset_thread.start()
        try:
            reset_method, reset_error = reset_queue.get(timeout=RTL_RESET_COMMAND_TIMEOUT_SECONDS + 1.0)
        except queue.Empty:
            message = (
                f"Sound card {device.display_name} did not finish its USB reset command. "
                "Physically unplug and replug it if it does not recover."
            )
            LOG.warning("%s", message)
            return {"success": False, "reappeared": False, "message": message, "status": self.status()}
        if reset_error is not None:
            raise reset_error
        reappeared = False
        deadline = time.monotonic() + RTL_RESET_REAPPEAR_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if any(candidate.stable_id == stable_id for candidate in self._refresh_soundcards()):
                    reappeared = True
                    break
            except Exception as exc:
                LOG.debug("ALSA probe after soundcard USB reset failed: %s", exc)
            time.sleep(0.25)
        if reappeared:
            message = f"Sound card {device.display_name} was reset using {reset_method} and reappeared."
            LOG.info("%s", message)
        else:
            message = (
                f"Sound card {device.display_name} was reset using {reset_method}, but it did not reappear. "
                "Physically unplug and replug it if it remains unavailable."
            )
            LOG.warning("%s", message)
        return {
            "success": True,
            "stable_id": stable_id,
            "reappeared": reappeared,
            "device": self._soundcard_device_payload(device),
            "message": message,
            "status": self.status(),
        }

    def reset_rtl_device(self, serial: str) -> dict[str, Any]:
        with self.lock:
            uses_remote_sdr = self.settings.uses_remote_sdr
        if uses_remote_sdr:
            return self._reset_remote_sdr()
        serial = str(serial or "").strip()
        if not serial:
            raise ValueError("Select an RTL-SDR to reset.")
        if not self.reset_lock.acquire(blocking=False):
            raise ValueError("A USB device reset is already in progress.")
        try:
            self._begin_sdr_reset(serial)
            return self._perform_sdr_reset(serial)
        finally:
            self.reset_lock.release()

    def start_sdr_reset_for_remote_client(self) -> None:
        """Reset this machine's SDR for a remote client, without waiting for it to finish.

        The client learns how it went from this machine's status, as every page here does.
        """
        with self.lock:
            serial = "" if self.settings.uses_remote_sdr else str(self.settings.serial or "")
        if not serial:
            raise ValueError("Select an RTL-SDR to reset.")
        if not self.reset_lock.acquire(blocking=False):
            raise ValueError("A USB device reset is already in progress.")
        try:
            self._begin_sdr_reset(serial)
        except BaseException:
            self.reset_lock.release()
            raise

        def run() -> None:
            try:
                self._perform_sdr_reset(serial)
            except Exception as exc:
                LOG.warning("RTL-SDR reset for a remote client failed: %s", exc)
            finally:
                self.reset_lock.release()

        threading.Thread(target=run, name=f"rtl-remote-reset-{serial}", daemon=True).start()

    def _begin_sdr_reset(self, serial: str) -> None:
        with self.lock:
            self.sdr_reset_count += 1
            self.sdr_reset = {
                "id": self.sdr_reset_count,
                "state": "resetting",
                "message": RTL_RESET_STARTED_MESSAGE,
                "serial": serial,
                "finished_at": None,
            }
        self.announce_settings_changed()

    def _finish_sdr_reset(self, succeeded: bool, message: str) -> None:
        with self.lock:
            if self.sdr_reset is not None:
                self.sdr_reset.update(
                    state="finished" if succeeded else "failed",
                    message=message,
                    finished_at=time.monotonic(),
                )
        self.announce_settings_changed()

    def _perform_sdr_reset(self, serial: str) -> dict[str, Any]:
        try:
            result = self._reset_rtl_device(serial)
        except Exception as exc:
            self._finish_sdr_reset(False, str(exc))
            raise
        # An SDR that did not come back is not available, so the reset did not work.
        self._finish_sdr_reset(bool(result.get("success") and result.get("reappeared")), str(result.get("message") or ""))
        result["status"] = self.status()
        return result

    def _sdr_reset_status_locked(self) -> dict[str, Any] | None:
        reset = self.sdr_reset
        if reset is None:
            return None
        finished_at = reset.get("finished_at")
        if finished_at is not None and time.monotonic() - finished_at > RTL_RESET_RESULT_SHOW_SECONDS:
            return None
        return {"id": reset["id"], "state": reset["state"], "message": reset["message"]}

    def _reset_remote_sdr(self) -> dict[str, Any]:
        """Ask the remote host to reset its SDR, then wait for it to say how that went."""
        with self.lock:
            client = self.remote_client
        if client is None or not client.status().get("reachable"):
            raise ValueError("The remote SDR is not reachable.")
        try:
            host_status = client.reset_sdr()
        except RemoteSdrError as exc:
            raise ValueError(str(exc)) from exc
        except ValueError as exc:
            if "unknown request" in str(exc):
                raise ValueError(REMOTE_SDR_RESET_UNSUPPORTED_MESSAGE) from exc
            raise
        reset_id = (host_status.get("reset") or {}).get("id")
        self.announce_settings_changed()
        deadline = time.monotonic() + RTL_RESET_TOTAL_SECONDS + 5.0
        seen = self.settings_change_count
        while True:
            remote_status = client.status()
            reset = (remote_status.get("sdr") or {}).get("reset") or {}
            if reset.get("id") == reset_id and reset.get("state") in {"finished", "failed"}:
                return {
                    "success": reset["state"] == "finished",
                    "reappeared": reset["state"] == "finished",
                    "message": reset.get("message") or "",
                    "status": self.status(),
                }
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not remote_status.get("reachable"):
                return {
                    "success": False,
                    "reappeared": False,
                    "message": (
                        "The remote SDR did not say whether its reset finished. "
                        "Check the RTL-SDR on the NWR Stream Manager sharing it."
                    ),
                    "status": self.status(),
                }
            seen = self.wait_for_settings_change(seen, min(remaining, 1.0))

    def _reset_rtl_device(self, serial: str) -> dict[str, Any]:
        with self.lock:
            configured_serial = self.settings.serial
            should_restart_capture = bool(configured_serial and configured_serial == serial)
            # Only a dongle's own reader holds it; an I/Q file source has nothing to release.
            capture = self.capture if should_restart_capture and isinstance(self.capture, RtlCaptureSource) else None
        if capture is not None:
            LOG.info("pausing RTL-SDR reader before USB reset for serial %s", serial)
            capture.restart_reader()
            if not capture.wait_until_reader_released(timeout=2.0):
                LOG.warning(
                    "RTL-SDR serial %s did not release before reset; forcing USB reset anyway",
                    serial,
                )
        LOG.info("resetting RTL-SDR USB device with serial %s", serial)
        reset_queue: queue.Queue = queue.Queue(maxsize=1)

        def run_reset() -> None:
            try:
                reset_queue.put((reset_usb_rtl_device(serial, timeout_seconds=RTL_RESET_COMMAND_TIMEOUT_SECONDS), None))
            except BaseException as exc:
                reset_queue.put((None, exc))

        reset_thread = threading.Thread(
            target=run_reset,
            name=f"rtl-usb-reset-{serial}",
            daemon=True,
        )
        reset_thread.start()
        try:
            reset_result, reset_error = reset_queue.get(timeout=RTL_RESET_COMMAND_TIMEOUT_SECONDS + 1.0)
        except queue.Empty:
            message = (
                f"RTL-SDR serial {serial} did not finish its USB reset command. "
                "The dongle may be locked up; physically unplug and replug it if it does not recover."
            )
            LOG.warning("%s", message)
            with self.lock:
                if self.settings.serial == serial:
                    self.capture_error = message
            return {
                "success": False,
                "serial": serial,
                "reappeared": False,
                "device": None,
                "message": message,
                "status": self.status(),
            }
        if reset_error is not None:
            if should_restart_capture:
                with self.lock:
                    if self.settings.serial == serial and self.capture is None:
                        self._start_or_update_capture_locked()
            raise reset_error
        if reset_result is None:
            raise RuntimeError("RTL-SDR reset failed without an error")
        reset_device, reset_method = reset_result
        reappeared = False
        deadline = time.monotonic() + RTL_RESET_REAPPEAR_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            try:
                if any(device.serial == serial for device in list_usb_rtl_devices()):
                    reappeared = True
                    break
            except Exception as exc:
                LOG.debug("USB probe after RTL-SDR reset failed: %s", exc)
            time.sleep(0.25)
        with self.lock:
            if self.settings.serial == serial:
                self.capture_error = None if reappeared else (
                    f"RTL-SDR serial {serial} did not reappear after USB reset. "
                    "Physically unplug and replug the dongle if it remains unavailable."
                )
                if self.capture is None:
                    self._start_or_update_capture_locked()
        if reappeared:
            message = f"RTL-SDR serial {serial} was reset using {reset_method} and reappeared."
            LOG.info("%s", message)
        else:
            message = (
                f"RTL-SDR serial {serial} was reset using {reset_method}, but it did not reappear. "
                "Physically unplug and replug the dongle if it remains unavailable."
            )
            LOG.warning("%s", message)
        return {
            "success": True,
            "serial": serial,
            "reappeared": reappeared,
            "device": {
                "serial": reset_device.serial,
                "name": reset_device.description,
                "vendor": USB_VENDOR_NAMES.get(reset_device.vendor_id.lower(), reset_device.vendor_id),
                "vendor_id": reset_device.vendor_id,
                "product_id": reset_device.product_id,
            },
            "message": message,
            "status": self.status(),
        }

    def rtl_diagnostics(self) -> dict[str, Any]:
        with self.lock:
            fanout = self.raw_fanout
            alias_filter_strength = self.settings.alias_filter_strength
        if fanout is None:
            raise ValueError("RTL-SDR capture is not active")
        queue_depth = max(RTL_DIAGNOSTIC_MIN_BATCHES, min(RTL_DIAGNOSTIC_MAX_BATCHES, 16))
        subscriber = subscribe_raw_fanout(
            fanout,
            max_chunks=queue_depth,
            max_seconds=RTL_DIAGNOSTIC_QUEUE_SECONDS,
            name="rtl-diagnostics",
        )
        started = time.monotonic()
        batches: list[RtlSampleBatch] = []
        sample_rate = 0
        center_frequency_hz = 0
        target_samples = 0
        try:
            while time.monotonic() - started < RTL_DIAGNOSTIC_QUEUE_SECONDS:
                remaining = max(0.01, RTL_DIAGNOSTIC_QUEUE_SECONDS - (time.monotonic() - started))
                try:
                    batch = subscriber.get(timeout=min(0.25, remaining))
                except queue.Empty:
                    continue
                if not isinstance(batch, RtlSampleBatch):
                    continue
                if sample_rate <= 0:
                    sample_rate = int(batch.sample_rate)
                    center_frequency_hz = int(batch.center_frequency_hz)
                    target_samples = max(1, int(sample_rate * RTL_DIAGNOSTIC_SECONDS))
                if batch.sample_rate != sample_rate or batch.center_frequency_hz != center_frequency_hz:
                    batches = []
                    sample_rate = int(batch.sample_rate)
                    center_frequency_hz = int(batch.center_frequency_hz)
                    target_samples = max(1, int(sample_rate * RTL_DIAGNOSTIC_SECONDS))
                batches.append(batch)
                sample_count = sum(len(item.data) // 2 for item in batches)
                if sample_count >= target_samples:
                    break
        finally:
            unsubscribe = getattr(fanout, "unsubscribe", None)
            if unsubscribe is not None:
                unsubscribe(subscriber)
        if not batches:
            raise ValueError("RTL-SDR produced no samples for diagnostics")
        raw = b"".join(batch.data for batch in batches)
        iq = rtl_u8_to_complex64(raw)
        if target_samples > 0 and iq.size > target_samples:
            iq = iq[-target_samples:]
        raw_stats = raw_iq_diagnostics(iq, sample_rate, center_frequency_hz, raw)
        channels = [
            channel_audio_diagnostics(
                iq,
                sample_rate,
                center_frequency_hz,
                frequency_hz,
                alias_filter_strength=alias_filter_strength,
            )
            for frequency_hz in NWR_RECEIVER_CHANNELS_HZ
        ]
        strongest = max(
            raw_stats["channels"],
            key=lambda item: float(item["raw_snr_db"] if item["raw_snr_db"] is not None else -999.0),
            default=None,
        )
        LOG.info(
            "RTL-SDR diagnostics: sample_rate=%s center=%s samples=%s raw_rms=%.4f dc=%.6f strongest=%s snr=%s",
            sample_rate,
            center_frequency_hz,
            raw_stats["sample_count"],
            raw_stats["rms"],
            raw_stats["dc_magnitude"],
            strongest.get("frequency_mhz") if strongest else "none",
            strongest.get("raw_snr_db") if strongest else None,
        )
        return {
            "sample_rate": sample_rate,
            "center_frequency_hz": center_frequency_hz,
            "duration_seconds": (iq.size / float(sample_rate)) if sample_rate > 0 else 0.0,
            "batch_count": len(batches),
            "raw": raw_stats,
            "channels": channels,
        }

    def _select_only_connected_device(self) -> None:
        try:
            devices = self.devices()["devices"]
        except Exception as exc:
            LOG.debug("initial RTL-SDR auto-selection failed: %s", exc)
            return
        if len(devices) != 1:
            return
        with self.lock:
            if self.settings.serial:
                return
            serial = str(devices[0]["serial"]).strip()
            if not serial:
                return
            self.settings = replace(self.settings, serial=serial)
            save_settings(self.state_path, self.settings)
            LOG.info("selected the only connected RTL-SDR: serial=%s", serial)
            self._start_or_update_capture_locked()
        self.announce_settings_changed()
