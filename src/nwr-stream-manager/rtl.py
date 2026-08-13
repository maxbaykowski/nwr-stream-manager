from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import importlib
import logging
import os
import queue
import threading
import time
from ctypes import c_ubyte
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

from .dsp import DEFAULT_OUTPUT_SAMPLE_RATE, IqChannelizer, IqDcBlocker, rtl_u8_to_complex64


LOG = logging.getLogger(__name__)

NWR_CENTER_FREQUENCY_HZ = 162_475_000
DEFAULT_RTL_SAMPLE_RATE = 1_536_000
DEFAULT_READ_CHUNK_BYTES = 131_072
DEFAULT_READ_TIMEOUT_SECONDS = 5.0
RTL_ASYNC_BUFFER_COUNT = 15
RTL_ASYNC_BUFFER_SECONDS = 0.05
RTL_SAMPLE_RATE_RANGES = (
    (225_001, 300_000),
    (900_001, 3_200_000),
)

RTLSDR_READ_ASYNC_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_uint32,
    ctypes.c_void_p,
)


class RtlError(RuntimeError):
    """Base class for RTL-SDR capture failures."""


class RtlDependencyError(RtlError):
    """Raised when pyrtlsdr/librtlsdr is not available."""


class RtlDeviceError(RtlError):
    """Raised when a configured RTL-SDR cannot be opened or resolved."""


class RtlDeviceResolutionRetryableError(RtlDeviceError):
    """Raised when the configured RTL-SDR may appear on a later retry."""


class RtlDeviceResolutionFatalError(RtlDeviceError):
    """Raised when the configured RTL-SDR setting cannot recover by retrying."""


class RtlDeviceAccessFatalError(RtlDeviceError):
    """Raised when the process does not have permission to use RTL-SDR devices."""


class RtlDeviceBusyRetryableError(RtlDeviceError):
    """Raised when the configured RTL-SDR is present but opened elsewhere."""


class RtlConfigError(RtlError):
    """Raised when requested RTL-SDR settings are invalid."""


class RtlReadError(RtlError):
    """Raised when an active RTL-SDR capture stops unexpectedly."""


class CompatLibUSBError(IOError):
    _errno_map = {
        -1: ("LIBUSB_ERROR_IO", "Input/output error"),
        -2: ("LIBUSB_ERROR_INVALID_PARAM", "Invalid parameter"),
        -3: ("LIBUSB_ERROR_ACCESS", "Access denied"),
        -4: ("LIBUSB_ERROR_NO_DEVICE", "No such device"),
        -5: ("LIBUSB_ERROR_NOT_FOUND", "Entity not found"),
        -6: ("LIBUSB_ERROR_BUSY", "Resource busy"),
        -7: ("LIBUSB_ERROR_TIMEOUT", "Operation timed out"),
        -8: ("LIBUSB_ERROR_OVERFLOW", "Overflow"),
        -9: ("LIBUSB_ERROR_PIPE", "Pipe error"),
        -10: ("LIBUSB_ERROR_INTERRUPTED", "System call interrupted"),
        -11: ("LIBUSB_ERROR_NO_MEM", "Insufficient memory"),
        -12: ("LIBUSB_ERROR_NOT_SUPPORTED", "Operation not supported"),
        -99: ("LIBUSB_ERROR_OTHER", "Other error"),
    }

    def __init__(self, errno: int, msg: str = "") -> None:
        super().__init__(errno, msg)
        self.errno = errno
        self.msg = msg

    def __str__(self) -> str:
        mapped = self._errno_map.get(self.errno)
        if mapped is None:
            return f'Error code {self.errno}: "{self.msg}"'
        error_id, error_message = mapped
        return f'<{error_id} ({self.errno}): {error_message}> "{self.msg}"'


@dataclass(frozen=True)
class RtlDeviceInfo:
    index: int
    description: str
    serial: str | None = None


@dataclass(frozen=True)
class UsbDeviceInfo:
    path: Path
    vendor_id: str
    product_id: str
    serial: str | None
    description: str


@dataclass(frozen=True)
class RtlConfig:
    serial: str
    sample_rate: int = DEFAULT_RTL_SAMPLE_RATE
    center_frequency_hz: int = NWR_CENTER_FREQUENCY_HZ
    ppm_correction: int = 0
    gain: float | None = None
    bias_tee: bool = False
    read_chunk_bytes: int = DEFAULT_READ_CHUNK_BYTES
    read_timeout_seconds: float = DEFAULT_READ_TIMEOUT_SECONDS
    retry_delay_seconds: float = 0.5


def validate_rtl_sample_rate(sample_rate: int) -> int:
    sample_rate = int(sample_rate)
    if any(minimum <= sample_rate <= maximum for minimum, maximum in RTL_SAMPLE_RATE_RANGES):
        return sample_rate
    ranges = ", ".join(f"{minimum}-{maximum} S/s" for minimum, maximum in RTL_SAMPLE_RATE_RANGES)
    raise RtlConfigError(f"RTL-SDR sample rate must be in one of these ranges: {ranges}")


def validate_ppm_correction(ppm: int) -> int:
    ppm = int(ppm)
    if -200 <= ppm <= 200:
        return ppm
    raise RtlConfigError("RTL-SDR PPM correction must be between -200 and 200")


@dataclass
class RtlSampleBatch:
    data: bytes
    sample_rate: int
    center_frequency_hz: int
    captured_at: float = field(default_factory=time.monotonic)


def _read_sysfs_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


@contextlib.contextmanager
def _suppress_native_stderr(enabled: bool):
    if not enabled:
        yield
        return
    saved_stderr_fd = os.dup(2)
    devnull_fd = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull_fd, 2)
        yield
    finally:
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stderr_fd)
        os.close(devnull_fd)


def _required_librtlsdr_symbol(library: ctypes.CDLL, name: str):
    try:
        return getattr(library, name)
    except AttributeError as exc:
        raise RtlDependencyError(
            f"librtlsdr is missing required symbol {name}; install a complete librtlsdr package"
        ) from exc


def _configure_librtlsdr_functions(library: ctypes.CDLL) -> None:
    p_rtlsdr_dev = ctypes.c_void_p
    required_signatures = {
        "rtlsdr_get_device_count": (ctypes.c_uint, []),
        "rtlsdr_get_device_name": (ctypes.c_char_p, [ctypes.c_uint]),
        "rtlsdr_get_device_usb_strings": (
            ctypes.c_int,
            [
                ctypes.c_uint,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(ctypes.c_ubyte),
            ],
        ),
        "rtlsdr_open": (ctypes.c_int, [ctypes.POINTER(p_rtlsdr_dev), ctypes.c_uint]),
        "rtlsdr_close": (ctypes.c_int, [p_rtlsdr_dev]),
        "rtlsdr_set_center_freq": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_uint]),
        "rtlsdr_set_freq_correction": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_tuner_gain": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_get_tuner_gains": (
            ctypes.c_int,
            [p_rtlsdr_dev, ctypes.POINTER(ctypes.c_int)],
        ),
        "rtlsdr_set_tuner_gain_mode": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_sample_rate": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_uint]),
        "rtlsdr_reset_buffer": (ctypes.c_int, [p_rtlsdr_dev]),
        "rtlsdr_read_async": (
            ctypes.c_int,
            [
                p_rtlsdr_dev,
                RTLSDR_READ_ASYNC_CALLBACK,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ],
        ),
        "rtlsdr_cancel_async": (ctypes.c_int, [p_rtlsdr_dev]),
    }
    for name, (restype, argtypes) in required_signatures.items():
        function = _required_librtlsdr_symbol(library, name)
        function.restype = restype
        function.argtypes = argtypes

    optional_signatures = {
        "rtlsdr_set_testmode": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_dithering": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_agc_mode": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
        "rtlsdr_set_bias_tee": (ctypes.c_int, [p_rtlsdr_dev, ctypes.c_int]),
    }
    for name, (restype, argtypes) in optional_signatures.items():
        function = getattr(library, name, None)
        if function is not None:
            function.restype = restype
            function.argtypes = argtypes


def _load_system_librtlsdr() -> ctypes.CDLL:
    candidates = [
        ctypes.util.find_library("rtlsdr"),
        ctypes.util.find_library("librtlsdr"),
        "librtlsdr.so",
        "librtlsdr.so.0",
    ]
    errors: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            library = ctypes.CDLL(candidate)
            _configure_librtlsdr_functions(library)
            return library
        except OSError as exc:
            errors.append(f"{candidate}: {exc}")
    detail = "; ".join(errors) if errors else "ctypes could not locate librtlsdr"
    raise RtlDependencyError(
        "could not load librtlsdr. Install your distribution's librtlsdr package "
        f"or install pyrtlsdrlib on supported architectures. Details: {detail}"
    )


class CompatBaseRtlSdr:
    def __init__(
        self,
        device_index: int = 0,
        test_mode_enabled: bool = False,
        serial_number: str | None = None,
        dithering_enabled: bool = True,
    ) -> None:
        if serial_number is not None:
            raise NotImplementedError("serial_number is not supported by the compatibility wrapper")
        assert rtlsdr_lib is not None
        self.dev_p = ctypes.c_void_p(None)
        self.device_opened = False
        result = rtlsdr_lib.rtlsdr_open(ctypes.byref(self.dev_p), int(device_index))
        if result < 0:
            raise CompatLibUSBError(result, f"Could not open SDR (device index = {device_index})")
        self.device_opened = True
        try:
            self._set_optional_int("rtlsdr_set_testmode", int(test_mode_enabled))
            self._set_optional_int("rtlsdr_set_dithering", int(dithering_enabled))
            self._reset_buffer()
            self.gain_values = self.get_gains()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self.device_opened:
            return
        assert rtlsdr_lib is not None
        rtlsdr_lib.rtlsdr_close(self.dev_p)
        self.device_opened = False

    def _set_optional_int(self, name: str, value: int) -> None:
        function = getattr(rtlsdr_lib, name, None)
        if function is None:
            LOG.debug("librtlsdr does not expose optional %s; skipping", name)
            return
        result = function(self.dev_p, value)
        if result < 0:
            if name == "rtlsdr_set_dithering" and value == 0:
                LOG.debug("librtlsdr rejected disabling dithering with %s; continuing", result)
                return
            raise CompatLibUSBError(result, f"Could not set {name}")

    def _reset_buffer(self) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_reset_buffer(self.dev_p)
        if result < 0:
            raise CompatLibUSBError(result, "Could not reset buffer")

    @property
    def sample_rate(self) -> int:
        raise AttributeError("sample_rate is write-only in compatibility mode")

    @sample_rate.setter
    def sample_rate(self, rate: int) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_set_sample_rate(self.dev_p, int(rate))
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set sample rate to {int(rate)} Hz")

    @property
    def center_freq(self) -> int:
        raise AttributeError("center_freq is write-only in compatibility mode")

    @center_freq.setter
    def center_freq(self, freq: int) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_set_center_freq(self.dev_p, int(freq))
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set center frequency to {int(freq)} Hz")

    @property
    def freq_correction(self) -> int:
        raise AttributeError("freq_correction is write-only in compatibility mode")

    @freq_correction.setter
    def freq_correction(self, ppm: int) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_set_freq_correction(self.dev_p, int(ppm))
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set frequency correction to {int(ppm)} ppm")

    @property
    def gain(self) -> float | str:
        raise AttributeError("gain is write-only in compatibility mode")

    @gain.setter
    def gain(self, gain: float | str) -> None:
        assert rtlsdr_lib is not None
        if isinstance(gain, str) and gain == "auto":
            result = rtlsdr_lib.rtlsdr_set_tuner_gain_mode(self.dev_p, 0)
            if result < 0:
                raise CompatLibUSBError(result, "Could not set tuner gain mode")
            agc = getattr(rtlsdr_lib, "rtlsdr_set_agc_mode", None)
            if agc is not None:
                result = agc(self.dev_p, 1)
                if result < 0:
                    raise CompatLibUSBError(result, "Could not set AGC mode")
            return

        requested_tenths = int(round(float(gain) * 10))
        selected_gain = requested_tenths
        if self.gain_values:
            selected_gain = min(self.gain_values, key=lambda value: abs(value - requested_tenths))
        result = rtlsdr_lib.rtlsdr_set_tuner_gain_mode(self.dev_p, 1)
        if result < 0:
            raise CompatLibUSBError(result, "Could not set tuner gain mode")
        result = rtlsdr_lib.rtlsdr_set_tuner_gain(self.dev_p, selected_gain)
        if result < 0:
            self.close()
            raise CompatLibUSBError(result, f"Could not set gain to {gain}")

    def get_gains(self) -> list[int]:
        assert rtlsdr_lib is not None
        buffer = (ctypes.c_int * 50)()
        result = rtlsdr_lib.rtlsdr_get_tuner_gains(self.dev_p, buffer)
        if result <= 0:
            return []
        return [buffer[index] for index in range(result)]

    def set_bias_tee(self, enabled: bool) -> None:
        function = getattr(rtlsdr_lib, "rtlsdr_set_bias_tee", None)
        if function is None:
            if enabled:
                raise RtlDeviceError("this librtlsdr does not support bias tee control")
            return
        result = function(self.dev_p, int(enabled))
        if result < 0:
            raise CompatLibUSBError(result, "Could not set bias tee")


try:
    rtlsdr_librtlsdr_module = importlib.import_module("rtlsdr.librtlsdr")
    rtlsdr_lib = rtlsdr_librtlsdr_module.librtlsdr
    from rtlsdr.rtlsdr import BaseRtlSdr, LibUSBError

    _configure_librtlsdr_functions(rtlsdr_lib)
    RTLSDR_IMPORT_ERROR: Exception | None = None
except Exception as exc:
    try:
        rtlsdr_lib = _load_system_librtlsdr()
        BaseRtlSdr = CompatBaseRtlSdr
        LibUSBError = CompatLibUSBError
        RTLSDR_IMPORT_ERROR = None
        LOG.warning(
            "PyRTLSDR could not initialize its native wrapper (%s); using direct librtlsdr mode",
            exc,
        )
    except Exception as fallback_exc:
        rtlsdr_lib = None  # type: ignore[assignment]
        BaseRtlSdr = None  # type: ignore[assignment]
        LibUSBError = IOError  # type: ignore[assignment]
        RTLSDR_IMPORT_ERROR = fallback_exc


def _librtlsdr_supports_dithering() -> bool:
    return rtlsdr_lib is not None and getattr(rtlsdr_lib, "rtlsdr_set_dithering", None) is not None


def _open_rtlsdr_device(device_index: int, *, quiet: bool) -> BaseRtlSdr:
    if BaseRtlSdr is None:
        raise RtlDependencyError(str(RTLSDR_IMPORT_ERROR))
    if BaseRtlSdr is not CompatBaseRtlSdr and not _librtlsdr_supports_dithering():
        LOG.info(
            "librtlsdr does not support dithering control; using direct compatibility wrapper "
            "to keep dithering disabled-compatible"
        )
        with _suppress_native_stderr(quiet):
            return CompatBaseRtlSdr(device_index=device_index, dithering_enabled=False)
    try:
        with _suppress_native_stderr(quiet):
            return BaseRtlSdr(device_index=device_index, dithering_enabled=False)
    except AttributeError as exc:
        if BaseRtlSdr is CompatBaseRtlSdr or "rtlsdr_set_dithering" not in str(exc):
            raise
        LOG.info(
            "PyRTLSDR tried to use unsupported dithering control; retrying with direct "
            "librtlsdr compatibility wrapper"
        )
        with _suppress_native_stderr(quiet):
            return CompatBaseRtlSdr(device_index=device_index, dithering_enabled=False)


class RtlCaptureSource:
    """Recovering RTL-SDR byte source for one physical dongle."""

    def __init__(self, config: RtlConfig) -> None:
        self.config = config
        self.sdr: BaseRtlSdr | None = None
        self.sdr_lock = threading.Lock()
        self.output_queue: queue.Queue[RtlSampleBatch | Exception | None] = queue.Queue(
            maxsize=32
        )
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.device_wait_logged = False
        self.device_busy_logged = False
        self.gain_values_db: list[float] = []
        self.stats_lock = threading.Lock()
        self.offered_batches = 0
        self.offered_bytes = 0
        self.dropped_batches = 0
        self.dropped_bytes = 0
        self.last_offer_at = 0.0
        self.last_drop_log_at = 0.0

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="rtl-capture", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._cancel_sdr_async()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        self._close_sdr()
        try:
            self.output_queue.put_nowait(None)
        except queue.Full:
            try:
                self.output_queue.get_nowait()
                self.output_queue.put_nowait(None)
            except queue.Empty:
                pass

    def read(self, timeout: float | None = None) -> RtlSampleBatch:
        item = self.output_queue.get(timeout=timeout)
        if item is None:
            raise EOFError("RTL-SDR capture source stopped")
        if isinstance(item, Exception):
            raise item
        return item

    def stats(self) -> dict[str, int | float | None]:
        with self.stats_lock:
            return {
                "offered_batches": self.offered_batches,
                "offered_bytes": self.offered_bytes,
                "offered_samples": self.offered_bytes // 2,
                "dropped_batches": self.dropped_batches,
                "dropped_bytes": self.dropped_bytes,
                "dropped_samples": self.dropped_bytes // 2,
                "queue_depth": self.output_queue.qsize(),
                "queue_capacity": self.output_queue.maxsize,
                "last_offer_age_seconds": (
                    round(time.monotonic() - self.last_offer_at, 3)
                    if self.last_offer_at
                    else None
                ),
            }

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                sdr = self._open_configured_sdr(quiet=False)
                with self.sdr_lock:
                    self.sdr = sdr
                self._reader_loop(sdr)
            except RtlDeviceResolutionRetryableError as exc:
                if not self.device_wait_logged:
                    LOG.warning("%s", exc)
                    self.device_wait_logged = True
                self.stop_event.wait(self.config.retry_delay_seconds)
            except RtlDeviceBusyRetryableError as exc:
                if not self.device_busy_logged:
                    LOG.warning("%s", exc)
                    self.device_busy_logged = True
                self.stop_event.wait(self.config.retry_delay_seconds)
            except (RtlDeviceResolutionFatalError, RtlDeviceAccessFatalError) as exc:
                LOG.error("%s", exc)
                self._offer(exc)
                self.stop_event.set()
            except Exception as exc:
                if self.stop_event.is_set():
                    break
                LOG.warning("RTL-SDR capture failed: %s; retrying", exc)
                self._offer(exc)
                self.stop_event.wait(self.config.retry_delay_seconds)
            finally:
                self._close_sdr()

    def apply_config(self, config: RtlConfig) -> None:
        validate_rtl_sample_rate(config.sample_rate)
        validate_ppm_correction(config.ppm_correction)
        current = self.config
        restart_required = (
            config.serial != current.serial
            or config.sample_rate != current.sample_rate
            or config.center_frequency_hz != current.center_frequency_hz
            or config.read_chunk_bytes != current.read_chunk_bytes
            or config.read_timeout_seconds != current.read_timeout_seconds
        )
        if restart_required:
            self.config = config
            LOG.info(
                "RTL-SDR capture restart requested: serial=%s sample_rate=%s center=%s",
                config.serial,
                config.sample_rate,
                config.center_frequency_hz,
            )
            self._cancel_sdr_async()
            return

        with self.sdr_lock:
            sdr = self.sdr
            if sdr is None:
                self.config = config
                return
            config = replace(config, gain=self._nearest_supported_gain(config.gain))
            self.config = config
            if config.bias_tee != current.bias_tee:
                self._set_bias_tee(sdr, config.bias_tee)
                LOG.info("RTL-SDR bias tee %s", "enabled" if config.bias_tee else "disabled")
            if config.ppm_correction != current.ppm_correction:
                sdr.freq_correction = config.ppm_correction
                LOG.info("RTL-SDR PPM correction set to %s", config.ppm_correction)
            if config.gain != current.gain:
                sdr.gain = "auto" if config.gain is None else config.gain
                LOG.info(
                    "RTL-SDR gain set to %s",
                    "auto" if config.gain is None else f"{config.gain:g} dB",
                )

    def update_config(self, **changes) -> RtlConfig:
        config = replace(self.config, **changes)
        self.apply_config(config)
        return self.config

    def get_gain_values(self) -> list[float]:
        with self.sdr_lock:
            return list(self.gain_values_db)

    def _open_configured_sdr(self, *, quiet: bool) -> BaseRtlSdr:
        if BaseRtlSdr is None or rtlsdr_lib is None:
            raise RtlDependencyError(str(RTLSDR_IMPORT_ERROR))
        validate_rtl_sample_rate(self.config.sample_rate)
        validate_ppm_correction(self.config.ppm_correction)
        device_index = self._resolve_serial_to_device_index(self.config.serial)
        try:
            sdr = _open_rtlsdr_device(device_index, quiet=quiet)
        except LibUSBError as exc:
            if getattr(exc, "errno", None) == -3:
                raise RtlDeviceAccessFatalError(
                    "Access denied while opening RTL-SDR. Check udev permissions "
                    "or run with sufficient access."
                ) from exc
            if getattr(exc, "errno", None) == -6:
                raise RtlDeviceBusyRetryableError(
                    f"Configured RTL-SDR serial {self.config.serial} is busy "
                    f"at current librtlsdr index {device_index}. "
                    "Waiting for it to become available."
                ) from exc
            raise
        try:
            sdr.sample_rate = self.config.sample_rate
            sdr.center_freq = self.config.center_frequency_hz
            if self.config.ppm_correction:
                sdr.freq_correction = self.config.ppm_correction
            self._set_bias_tee(sdr, self.config.bias_tee)
            gain_values = self._read_tuner_gains(sdr)
            with self.sdr_lock:
                self.gain_values_db = gain_values
                selected_gain = self._nearest_supported_gain(self.config.gain)
                self.config = replace(self.config, gain=selected_gain)
            sdr.gain = "auto" if self.config.gain is None else self.config.gain
            self._reset_sdr_buffer(sdr)
            LOG.info(
                "RTL-SDR capture started on index %s at %s Hz, sample_rate=%s gains=%s",
                device_index,
                self.config.center_frequency_hz,
                self.config.sample_rate,
                gain_values,
            )
            if self.device_wait_logged:
                LOG.info("Configured RTL-SDR is now available.")
                self.device_wait_logged = False
            if self.device_busy_logged:
                LOG.info("Configured RTL-SDR is no longer busy.")
                self.device_busy_logged = False
            return sdr
        except Exception:
            sdr.close()
            raise

    def _reader_loop(self, sdr: BaseRtlSdr) -> None:
        assert rtlsdr_lib is not None
        loop_config = self.config
        async_buffer_size = self._rtl_async_buffer_size(loop_config)
        async_done = threading.Event()
        callback_errors: list[Exception] = []
        last_data_at = time.monotonic()

        def should_stop() -> bool:
            return self.stop_event.is_set()

        def cancel_async_read() -> None:
            try:
                rtlsdr_lib.rtlsdr_cancel_async(sdr.dev_p)
            except Exception:
                LOG.debug("failed to cancel RTL-SDR async read", exc_info=True)

        def timeout_watchdog() -> None:
            while not async_done.is_set():
                if self.stop_event.wait(0.1):
                    cancel_async_read()
                    return
                if time.monotonic() - last_data_at > loop_config.read_timeout_seconds:
                    callback_errors.append(RtlReadError("RTL-SDR produced no data before timeout"))
                    cancel_async_read()
                    return

        def async_callback(buffer, length: int, _context) -> None:
            nonlocal last_data_at
            if should_stop():
                return
            try:
                chunk = ctypes.string_at(buffer, int(length))
                if not chunk:
                    return
                last_data_at = time.monotonic()
                batch = RtlSampleBatch(
                    data=chunk,
                    sample_rate=loop_config.sample_rate,
                    center_frequency_hz=loop_config.center_frequency_hz,
                )
                if not self._offer(batch):
                    cancel_async_read()
                    return
            except Exception as exc:
                callback_errors.append(exc)
                cancel_async_read()

        callback = RTLSDR_READ_ASYNC_CALLBACK(async_callback)
        watchdog = threading.Thread(
            target=timeout_watchdog,
            name="rtl-timeout-watchdog",
            daemon=True,
        )
        try:
            watchdog.start()
            result = rtlsdr_lib.rtlsdr_read_async(
                sdr.dev_p,
                callback,
                None,
                RTL_ASYNC_BUFFER_COUNT,
                async_buffer_size,
            )
            if callback_errors:
                raise callback_errors[0]
            if result < 0 and not should_stop():
                raise LibUSBError(result, "RTL-SDR async read failed")
        finally:
            async_done.set()
            watchdog.join(timeout=1.0)

    def _offer(self, item: RtlSampleBatch | Exception | None) -> bool:
        if self.stop_event.is_set():
            return False
        while not self.stop_event.is_set():
            try:
                self.output_queue.put_nowait(item)
                self._record_offered_item(item)
                return True
            except queue.Full:
                try:
                    dropped = self.output_queue.get_nowait()
                except queue.Empty:
                    continue
                self._record_dropped_item(dropped)
        return False

    def _record_offered_item(self, item: RtlSampleBatch | Exception | None) -> None:
        if not isinstance(item, RtlSampleBatch):
            return
        with self.stats_lock:
            self.offered_batches += 1
            self.offered_bytes += len(item.data)
            self.last_offer_at = time.monotonic()

    def _record_dropped_item(self, item: RtlSampleBatch | Exception | None) -> None:
        if not isinstance(item, RtlSampleBatch):
            return
        should_log = False
        with self.stats_lock:
            self.dropped_batches += 1
            self.dropped_bytes += len(item.data)
            now = time.monotonic()
            if now - self.last_drop_log_at >= 60.0:
                self.last_drop_log_at = now
                should_log = True
                dropped_batches = self.dropped_batches
                dropped_bytes = self.dropped_bytes
        if should_log:
            LOG.warning(
                "RTL-SDR capture queue is dropping IQ batches: dropped_batches=%s dropped_bytes=%s",
                dropped_batches,
                dropped_bytes,
            )

    def _close_sdr(self) -> None:
        with self.sdr_lock:
            sdr = self.sdr
            self.sdr = None
        if sdr is None:
            return
        try:
            self._cancel_specific_sdr_async(sdr)
            sdr.close()
        except Exception:
            LOG.debug("failed to close RTL-SDR device", exc_info=True)

    def _cancel_sdr_async(self) -> None:
        with self.sdr_lock:
            sdr = self.sdr
        self._cancel_specific_sdr_async(sdr)

    @staticmethod
    def _cancel_specific_sdr_async(sdr: BaseRtlSdr | None) -> None:
        if rtlsdr_lib is None or sdr is None:
            return
        try:
            rtlsdr_lib.rtlsdr_cancel_async(sdr.dev_p)
        except Exception:
            LOG.debug("failed to cancel RTL-SDR async read", exc_info=True)

    @staticmethod
    def _reset_sdr_buffer(sdr: BaseRtlSdr) -> None:
        assert rtlsdr_lib is not None
        result = rtlsdr_lib.rtlsdr_reset_buffer(sdr.dev_p)
        if result < 0:
            raise LibUSBError(result, "Could not reset RTL-SDR buffer")

    @staticmethod
    def _set_bias_tee(sdr: BaseRtlSdr, enabled: bool) -> None:
        set_bias_tee = getattr(sdr, "set_bias_tee", None)
        if set_bias_tee is None:
            if enabled:
                raise RtlDeviceError("this RTL-SDR stack does not support bias tee control")
            return
        set_bias_tee(enabled)

    def _nearest_supported_gain(self, gain: float | None) -> float | None:
        if gain is None:
            return None
        if not self.gain_values_db:
            return float(gain)
        return min(self.gain_values_db, key=lambda value: abs(value - float(gain)))

    @staticmethod
    def _read_tuner_gains(sdr: BaseRtlSdr) -> list[float]:
        assert rtlsdr_lib is not None
        buffer = (ctypes.c_int * 256)()
        result = rtlsdr_lib.rtlsdr_get_tuner_gains(sdr.dev_p, buffer)
        if result <= 0:
            return []
        return [round(buffer[index] / 10.0, 1) for index in range(result)]

    @staticmethod
    def _rtl_async_buffer_size(config: RtlConfig) -> int:
        sample_rate = max(1, int(config.sample_rate))
        target_size = int(2.0 * float(sample_rate) * RTL_ASYNC_BUFFER_SECONDS)
        target_size = max(512, target_size)
        target_size -= target_size % 512
        return max(512, target_size)

    @staticmethod
    def _resolve_serial_to_device_index(serial: str) -> int:
        if not serial:
            raise RtlDeviceResolutionFatalError(
                "RTL-SDR serial is required. Device indexes are intentionally unsupported "
                "because librtlsdr indexes can change after reconnects."
            )
        usb_confirmed = RtlCaptureSource._wait_for_unique_usb_serial(serial)
        devices = list_rtl_devices()
        matches = [device for device in devices if device.serial == serial]
        if not matches:
            if not usb_confirmed:
                raise RtlDeviceResolutionRetryableError(
                    f"Configured RTL-SDR serial {serial} was not visible to librtlsdr. "
                    "Waiting for that device to appear."
                )
            raise RtlDeviceResolutionRetryableError(
                f"Configured RTL-SDR serial {serial} is present on USB but was not yet "
                "visible to librtlsdr. Waiting for librtlsdr to detect that device."
            )
        if len(matches) > 1:
            raise RtlDeviceResolutionFatalError(
                f"Multiple RTL-SDR devices were found by librtlsdr with serial {serial}. "
                "Assign unique serial numbers to each device using rtl_eeprom."
            )
        LOG.debug(
            "resolved RTL-SDR serial %s to device index %s",
            serial,
            matches[0].index,
        )
        return matches[0].index

    @staticmethod
    def _wait_for_unique_usb_serial(serial: str) -> bool:
        try:
            usb_devices = list_usb_rtl_devices()
        except OSError as exc:
            LOG.warning(
                "USB probe failed (%s); falling back to librtlsdr-only serial detection",
                exc,
            )
            return False

        matches = [device for device in usb_devices if device.serial == serial]
        if not matches:
            raise RtlDeviceResolutionRetryableError(
                f"Configured RTL-SDR serial {serial} was not found on USB. "
                "Waiting for that device to appear."
            )
        if len(matches) > 1:
            raise RtlDeviceResolutionFatalError(
                f"Multiple USB RTL-SDR devices were found with serial {serial}. "
                "Assign unique serial numbers to each device using rtl_eeprom."
            )
        return True


class ProcessedIqSource:
    """Converts raw RTL-SDR bytes into shifted 24 kS/s complex float32 IQ."""

    def __init__(
        self,
        rtl_source: RtlCaptureSource,
        target_frequency_hz: int = NWR_CENTER_FREQUENCY_HZ,
        output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
    ) -> None:
        self.rtl_source = rtl_source
        self.target_frequency_hz = target_frequency_hz
        self.output_rate = output_rate
        self.dc_blocker: IqDcBlocker | None = None
        self.channelizer: IqChannelizer | None = None
        self.channelizer_key: tuple[int, int, int] | None = None

    def read(self, timeout: float | None = None):
        batch = self.rtl_source.read(timeout=timeout)
        next_key = (
            batch.sample_rate,
            batch.center_frequency_hz,
            int(round(self.target_frequency_hz)),
        )
        if self.channelizer is None or self.channelizer_key != next_key:
            self.dc_blocker = IqDcBlocker(batch.sample_rate)
            self.channelizer = IqChannelizer(
                input_rate=batch.sample_rate,
                center_frequency_hz=batch.center_frequency_hz,
                target_frequency_hz=int(round(self.target_frequency_hz)),
                output_rate=self.output_rate,
            )
            self.channelizer_key = next_key
        centered_iq = rtl_u8_to_complex64(batch.data)
        if self.dc_blocker is None:
            self.dc_blocker = IqDcBlocker(batch.sample_rate)
        dc_blocked_iq = self.dc_blocker.process(centered_iq)
        assert self.channelizer is not None
        return self.channelizer.process_complex(dc_blocked_iq)


class IqFanout:
    """Small in-process fanout for later stream workers."""

    def __init__(self, source: ProcessedIqSource) -> None:
        self.source = source
        self.subscribers: set[queue.Queue] = set()
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None

    def subscribe(self, max_chunks: int = 32) -> queue.Queue:
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="iq-fanout", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                samples = self.source.read(timeout=0.5)
            except queue.Empty:
                continue
            except EOFError:
                return
            except RtlError as exc:
                LOG.warning("processed IQ source read failed: %s", exc)
                continue
            with self.subscribers_lock:
                subscribers = list(self.subscribers)
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait(samples)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                        subscriber.put_nowait(samples)
                    except queue.Empty:
                        pass


def list_rtl_devices() -> list[RtlDeviceInfo]:
    if rtlsdr_lib is None:
        raise RtlDependencyError(str(RTLSDR_IMPORT_ERROR))
    devices: list[RtlDeviceInfo] = []
    device_count = int(rtlsdr_lib.rtlsdr_get_device_count())
    for index in range(device_count):
        manufacturer = (c_ubyte * 256)()
        product = (c_ubyte * 256)()
        serial = (c_ubyte * 256)()
        result = rtlsdr_lib.rtlsdr_get_device_usb_strings(index, manufacturer, product, serial)
        if result != 0:
            if result == -3:
                raise RtlDeviceAccessFatalError(
                    f"Access denied while reading USB strings for RTL-SDR {index}. "
                    "Check udev permissions or run with sufficient access."
                )
            raise LibUSBError(result, f"while reading USB strings for RTL-SDR {index}")
        manufacturer_text = "".join(chr(value) for value in manufacturer if value > 0)
        product_text = "".join(chr(value) for value in product if value > 0)
        serial_text = "".join(chr(value) for value in serial if value > 0)
        name = rtlsdr_lib.rtlsdr_get_device_name(index)
        name_text = name.decode("utf-8", errors="replace") if name else ""
        devices.append(
            RtlDeviceInfo(
                index=index,
                description=", ".join(part for part in (manufacturer_text, product_text) if part)
                or name_text,
                serial=serial_text or None,
            )
        )
    return devices


def list_usb_rtl_devices() -> list[UsbDeviceInfo]:
    usb_root = Path("/sys/bus/usb/devices")
    devices: list[UsbDeviceInfo] = []
    for entry in usb_root.iterdir():
        vendor_id = _read_sysfs_text(entry / "idVendor")
        product_id = _read_sysfs_text(entry / "idProduct")
        if vendor_id is None or product_id is None:
            continue
        vendor_id = vendor_id.lower()
        product_id = product_id.lower()
        if vendor_id != "0bda" or product_id not in {"2832", "2838"}:
            continue
        serial = _read_sysfs_text(entry / "serial")
        manufacturer = _read_sysfs_text(entry / "manufacturer")
        product = _read_sysfs_text(entry / "product")
        description_parts = [part for part in (manufacturer, product) if part]
        devices.append(
            UsbDeviceInfo(
                path=entry,
                vendor_id=vendor_id,
                product_id=product_id,
                serial=serial,
                description=", ".join(description_parts) or entry.name,
            )
        )
    return devices


def run_processed_iq_loop(
    callback: Callable,
    *,
    config: RtlConfig,
    target_frequency_hz: int = NWR_CENTER_FREQUENCY_HZ,
    output_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
) -> None:
    source = RtlCaptureSource(config)
    processed = ProcessedIqSource(source, target_frequency_hz, output_rate)
    source.start()
    try:
        while True:
            callback(processed.read())
    finally:
        source.stop()
