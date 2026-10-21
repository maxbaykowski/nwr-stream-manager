"""Radio data from the RTL-SDR, an I/Q file or a shared SDR, passed on to everything that uses it: the fanouts, channel feeds and remote SDR sources."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import IQ_SAMPLE_RATE
from .constants import (
    ALIAS_FILTER_MAX_TRANSITION_SCALE,
    ALIAS_FILTER_MIN_ATTENUATION_DB,
    ALIAS_FILTER_STRENGTH_DEFAULT,
    ALIAS_FILTER_STRENGTH_MAX,
    ALIAS_FILTER_STRENGTH_MIN,
    CHANNEL_IQ_ALIAS_TRANSITION_HZ,
    INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB,
    INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ,
    INTERMEDIATE_IQ_SAMPLE_RATE,
    INTERMEDIATE_SOURCE_QUEUE_CHUNKS,
    IQ_RECORDER_SAMPLE_RATES,
    IQ_TEST_SOURCE_CHUNK_SECONDS,
    IQ_TEST_SOURCE_MIN_SAMPLE_RATE,
    LIVE_IQ_QUEUE_SECONDS,
    NWR_RECEIVER_CHANNELS_HZ,
    REMOTE_HOST_CHANNEL_QUEUE_SECONDS,
    REMOTE_HOST_WIDEBAND_QUEUE_SECONDS,
    REMOTE_SDR_HOST_SETTINGS,
    STREAM_FRAME_SECONDS,
    STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS,
    STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS,
    STREAM_WORKER_RAW_QUEUE_SECONDS,
)
from .dsp import (
    ComplexArray,
    create_decimator,
    DEFAULT_ALIAS_ATTENUATION_DB,
    IqChannelizer,
    IqDcBlocker,
    rtl_u8_to_complex64,
    update_decimator_alias_filter,
)
from .models import IqFileSourceConfig, IqSampleBatch
from .rtl import (
    DEFAULT_RTL_SAMPLE_RATE,
    NWR_CENTER_FREQUENCY_HZ,
    RTL_ASYNC_BUFFER_SECONDS,
    RtlCaptureSource,
    RtlSampleBatch,
)


LOG = logging.getLogger(__name__)


def validate_alias_filter_strength(value: Any) -> int:
    strength = int(value)
    if not ALIAS_FILTER_STRENGTH_MIN <= strength <= ALIAS_FILTER_STRENGTH_MAX:
        raise ValueError(
            f"alias_filter_strength must be between {ALIAS_FILTER_STRENGTH_MIN} and {ALIAS_FILTER_STRENGTH_MAX}"
        )
    return strength


def alias_filter_transition_hz(base_transition_hz: float, strength: int | float) -> float:
    strength = validate_alias_filter_strength(strength)
    weak_fraction = 1.0 - (float(strength) / float(ALIAS_FILTER_STRENGTH_MAX))
    scale = 1.0 + (ALIAS_FILTER_MAX_TRANSITION_SCALE - 1.0) * weak_fraction * weak_fraction
    return float(base_transition_hz) * scale


def alias_filter_attenuation_db(base_attenuation_db: float, strength: int | float) -> float:
    strength = validate_alias_filter_strength(strength)
    strong_fraction = float(strength) / float(ALIAS_FILTER_STRENGTH_MAX)
    return ALIAS_FILTER_MIN_ATTENUATION_DB + (
        float(base_attenuation_db) - ALIAS_FILTER_MIN_ATTENUATION_DB
    ) * strong_fraction


def iq_batch_byte_count(batch: RtlSampleBatch | IqSampleBatch) -> int:
    data = batch.data
    if isinstance(data, np.ndarray):
        return int(data.nbytes)
    return len(data)


def iq_batch_sample_count_from_bytes(byte_count: int, source: Any) -> int:
    if isinstance(source, IqFileCaptureSource):
        return int(byte_count) // 8
    return int(byte_count) // 2


def clear_queue_items(target: queue.Queue) -> int:
    cleared = 0
    while True:
        try:
            target.get_nowait()
            cleared += 1
        except queue.Empty:
            return cleared


def set_batch_generation(batch: Any, generation: int) -> Any:
    try:
        setattr(batch, "source_generation", int(generation))
    except Exception:
        object.__setattr__(batch, "source_generation", int(generation))
    return batch


def batch_generation(batch: Any) -> int:
    return int(getattr(batch, "source_generation", 0))


class IqFileCaptureSource:
    def __init__(
        self,
        config: IqFileSourceConfig,
        *,
        chunk_seconds: float = IQ_TEST_SOURCE_CHUNK_SECONDS,
    ) -> None:
        if int(config.sample_rate) < IQ_TEST_SOURCE_MIN_SAMPLE_RATE:
            raise ValueError(
                f"I/Q file sample rate must be at least {IQ_TEST_SOURCE_MIN_SAMPLE_RATE} S/s"
            )
        self.config = config
        self.chunk_seconds = max(0.01, float(chunk_seconds))
        self.output_queue: queue.Queue[IqSampleBatch | Exception | None] = queue.Queue(maxsize=32)
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.stats_lock = threading.Lock()
        self.offered_batches = 0
        self.offered_bytes = 0
        self.dropped_batches = 0
        self.dropped_bytes = 0
        self.last_offer_at = 0.0
        self.file_size_bytes = 0
        self.seek_lock = threading.Lock()
        self.pending_seek_samples = 0
        self.current_sample_offset = 0

    @property
    def label(self) -> str:
        return self.config.path.name

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        path = self.config.path
        if not path.is_file():
            raise FileNotFoundError(f"I/Q source file was not found: {path.name}")
        self.file_size_bytes = path.stat().st_size
        if self.file_size_bytes < 8:
            raise ValueError("I/Q source file must contain at least one complex float32 sample")
        if self.file_size_bytes % 8 != 0:
            LOG.warning(
                "I/Q source file %s has trailing bytes that do not form a complete complex float32 sample",
                path,
            )
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="iq-file-source", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self._offer(None)
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def read(self, timeout: float | None = None) -> IqSampleBatch:
        item = self.output_queue.get(timeout=timeout)
        if item is None:
            raise EOFError("I/Q file source stopped")
        if isinstance(item, Exception):
            raise item
        return item

    def stats(self) -> dict[str, int | float | None]:
        with self.stats_lock:
            duration_seconds = (
                (self.file_size_bytes // 8) / float(self.config.sample_rate)
                if self.file_size_bytes > 0 and self.config.sample_rate > 0
                else 0.0
            )
            return {
                "offered_batches": self.offered_batches,
                "offered_bytes": self.offered_bytes,
                "offered_samples": self.offered_bytes // 8,
                "dropped_batches": self.dropped_batches,
                "dropped_bytes": self.dropped_bytes,
                "dropped_samples": self.dropped_bytes // 8,
                "queue_depth": self.output_queue.qsize(),
                "queue_capacity": self.output_queue.maxsize,
                "file_size_bytes": self.file_size_bytes,
                "position_samples": self.current_sample_offset,
                "position_seconds": round(self.current_sample_offset / float(self.config.sample_rate), 3),
                "duration_seconds": round(duration_seconds, 3),
                "last_offer_age_seconds": (
                    round(time.monotonic() - self.last_offer_at, 3)
                    if self.last_offer_at
                    else None
                ),
            }

    def get_gain_values(self) -> list[float]:
        return []

    def seek_relative(self, seconds: float) -> dict[str, int | float]:
        delta_samples = int(round(float(seconds) * float(self.config.sample_rate)))
        with self.seek_lock:
            self.pending_seek_samples += delta_samples
        self._clear_output_queue()
        return self.stats()

    @staticmethod
    def _rtl_async_buffer_size(config: IqFileSourceConfig) -> int:
        sample_rate = max(1, int(config.sample_rate))
        samples = max(1, int(round(sample_rate * IQ_TEST_SOURCE_CHUNK_SECONDS)))
        return samples * 8

    def _run(self) -> None:
        samples_per_chunk = max(1, int(round(self.config.sample_rate * self.chunk_seconds)))
        floats_per_chunk = samples_per_chunk * 2
        chunk_bytes = floats_per_chunk * 4
        next_emit_at = time.monotonic()
        try:
            with self.config.path.open("rb") as handle:
                while not self.stop_event.is_set():
                    self._apply_pending_seek(handle)
                    raw = self._read_looping_chunk(handle, chunk_bytes)
                    if not raw:
                        raise ValueError("I/Q source file is empty")
                    float_count = len(raw) // 4
                    if float_count < 2:
                        continue
                    if float_count % 2:
                        raw = raw[: (float_count - 1) * 4]
                    values = np.frombuffer(raw, dtype="<f4")
                    iq = (values[0::2] + 1j * values[1::2]).astype(np.complex64, copy=False)
                    if iq.size == 0:
                        continue
                    self._offer(
                        IqSampleBatch(
                            data=iq,
                            sample_rate=self.config.sample_rate,
                            center_frequency_hz=self.config.center_frequency_hz,
                        )
                    )
                    with self.stats_lock:
                        self.current_sample_offset = handle.tell() // 8
                    next_emit_at += float(iq.size) / float(self.config.sample_rate)
                    delay = next_emit_at - time.monotonic()
                    if delay > 0:
                        self.stop_event.wait(delay)
                    elif delay < -1.0:
                        next_emit_at = time.monotonic()
        except Exception as exc:
            if not self.stop_event.is_set():
                self._offer(exc)

    def _apply_pending_seek(self, handle) -> None:
        with self.seek_lock:
            delta_samples = self.pending_seek_samples
            self.pending_seek_samples = 0
        if not delta_samples:
            return
        total_samples = self.file_size_bytes // 8
        if total_samples <= 0:
            return
        current_samples = handle.tell() // 8
        target_samples = current_samples + delta_samples
        if target_samples >= total_samples:
            target_samples = 0
        elif target_samples < 0:
            target_samples = 0
        handle.seek(target_samples * 8)
        with self.stats_lock:
            self.current_sample_offset = target_samples
        LOG.info(
            "seeked I/Q test source %s to %.3f seconds",
            self.config.path.name,
            target_samples / float(self.config.sample_rate),
        )

    def _read_looping_chunk(self, handle, size: int) -> bytes:
        parts: list[bytes] = []
        remaining = size
        while remaining > 0 and not self.stop_event.is_set():
            data = handle.read(remaining)
            if data:
                parts.append(data)
                remaining -= len(data)
                continue
            handle.seek(0)
            if not parts and handle.tell() != 0:
                break
            if self.file_size_bytes <= 0:
                break
            if self.file_size_bytes < remaining and parts:
                break
        return b"".join(parts)

    def _offer(self, item: IqSampleBatch | Exception | None) -> None:
        try:
            self.output_queue.put_nowait(item)
            if isinstance(item, IqSampleBatch):
                self._record_offered_item(item)
            return
        except queue.Full:
            pass
        try:
            dropped = self.output_queue.get_nowait()
            if isinstance(dropped, IqSampleBatch):
                self._record_dropped_item(dropped)
        except queue.Empty:
            pass
        try:
            self.output_queue.put_nowait(item)
            if isinstance(item, IqSampleBatch):
                self._record_offered_item(item)
        except queue.Full:
            if isinstance(item, IqSampleBatch):
                self._record_dropped_item(item)

    def _clear_output_queue(self) -> None:
        while True:
            try:
                dropped = self.output_queue.get_nowait()
            except queue.Empty:
                return
            if isinstance(dropped, IqSampleBatch):
                self._record_dropped_item(dropped)

    def _record_offered_item(self, item: IqSampleBatch) -> None:
        with self.stats_lock:
            self.offered_batches += 1
            self.offered_bytes += item.data.nbytes
            self.last_offer_at = time.monotonic()

    def _record_dropped_item(self, item: IqSampleBatch) -> None:
        with self.stats_lock:
            self.dropped_batches += 1
            self.dropped_bytes += item.data.nbytes


class RawRtlFanout:
    def __init__(self, source: RtlCaptureSource | IqFileCaptureSource) -> None:
        self.source = source
        self.source_lock = threading.Lock()
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_names: dict[queue.Queue, str] = {}
        self.subscriber_drops: dict[queue.Queue, int] = {}
        self.subscriber_drop_bytes: dict[queue.Queue, int] = {}
        self.subscriber_max_depth: dict[queue.Queue, int] = {}
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.read_batches = 0
        self.read_bytes = 0
        self.total_dropped_batches = 0
        self.total_dropped_bytes = 0
        self.last_read_at = 0.0
        self.last_drop_log_at = 0.0
        self.generation = 0

    def subscribe(
        self,
        max_chunks: int | None = None,
        max_seconds: float | None = None,
        name: str = "subscriber",
    ) -> queue.Queue:
        if max_chunks is None:
            max_chunks = self._chunks_for_seconds(max_seconds or STREAM_WORKER_RAW_QUEUE_SECONDS)
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
            self.subscriber_names[subscriber] = name
            self.subscriber_drops[subscriber] = 0
            self.subscriber_drop_bytes[subscriber] = 0
            self.subscriber_max_depth[subscriber] = 0
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)
            self.subscriber_names.pop(subscriber, None)
            self.subscriber_drops.pop(subscriber, None)
            self.subscriber_drop_bytes.pop(subscriber, None)
            self.subscriber_max_depth.pop(subscriber, None)

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="rtl-raw-fanout", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def set_source(self, source: RtlCaptureSource | IqFileCaptureSource) -> None:
        with self.source_lock:
            if source is self.source:
                return
            self.source = source
        with self.subscribers_lock:
            self.generation += 1
            cleared = 0
            for subscriber in self.subscribers:
                cleared += clear_queue_items(subscriber)
        LOG.info("RTL-SDR raw fanout switched source generation to %s; cleared %s queued batches", self.generation, cleared)

    def _chunks_for_seconds(self, seconds: float) -> int:
        with self.source_lock:
            source = self.source
        sample_rate = max(1, int(source.config.sample_rate))
        chunk_bytes = max(512, source._rtl_async_buffer_size(source.config))
        chunk_seconds = max(0.001, chunk_bytes / (2.0 * float(sample_rate)))
        return max(
            STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS,
            min(
                STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS,
                int(math.ceil(max(0.0, seconds) / chunk_seconds)),
            ),
        )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            with self.source_lock:
                source = self.source
            try:
                batch = source.read(timeout=0.5)
            except queue.Empty:
                continue
            except EOFError:
                with self.source_lock:
                    if source is self.source:
                        return
                continue
            except Exception as exc:
                LOG.warning("RTL-SDR raw fanout read failed: %s", exc)
                continue
            with self.subscribers_lock:
                subscribers = list(self.subscribers)
                generation = self.generation
                self.read_batches += 1
                self.read_bytes += iq_batch_byte_count(batch)
                self.last_read_at = time.monotonic()
            batch = set_batch_generation(batch, generation)
            for subscriber in subscribers:
                try:
                    subscriber.put_nowait(batch)
                    self._record_subscriber_depth(subscriber)
                except queue.Full:
                    try:
                        subscriber.get_nowait()
                    except queue.Empty:
                        pass
                    self._record_subscriber_drop(subscriber, batch)
                    try:
                        subscriber.put_nowait(batch)
                        self._record_subscriber_depth(subscriber)
                    except queue.Full:
                        self._record_subscriber_drop(subscriber, batch)
                        pass

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        if subscriber is None:
            return {}
        with self.subscribers_lock:
            return self._subscriber_stats_locked(subscriber)

    def stats(self) -> dict[str, Any]:
        with self.subscribers_lock:
            with self.source_lock:
                source = self.source
            subscribers = [self._subscriber_stats_locked(subscriber) for subscriber in self.subscribers]
            return {
                "read_batches": self.read_batches,
                "read_bytes": self.read_bytes,
                "read_samples": iq_batch_sample_count_from_bytes(self.read_bytes, source),
                "subscriber_count": len(self.subscribers),
                "total_dropped_batches": self.total_dropped_batches,
                "total_dropped_bytes": self.total_dropped_bytes,
                "total_dropped_samples": iq_batch_sample_count_from_bytes(self.total_dropped_bytes, source),
                "last_read_age_seconds": (
                    round(time.monotonic() - self.last_read_at, 3)
                    if self.last_read_at
                    else None
                ),
                "subscribers": subscribers,
            }

    def _subscriber_stats_locked(self, subscriber: queue.Queue) -> dict[str, Any]:
        with self.source_lock:
            source = self.source
        return {
            "name": self.subscriber_names.get(subscriber, "subscriber"),
            "queue_depth": subscriber.qsize(),
            "queue_capacity": subscriber.maxsize,
            "max_queue_depth": self.subscriber_max_depth.get(subscriber, 0),
            "dropped_batches": self.subscriber_drops.get(subscriber, 0),
            "dropped_bytes": self.subscriber_drop_bytes.get(subscriber, 0),
            "dropped_samples": iq_batch_sample_count_from_bytes(
                self.subscriber_drop_bytes.get(subscriber, 0),
                source,
            ),
        }

    def _record_subscriber_depth(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_max_depth[subscriber] = max(
                self.subscriber_max_depth.get(subscriber, 0),
                subscriber.qsize(),
            )

    def _record_subscriber_drop(self, subscriber: queue.Queue, batch: RtlSampleBatch | IqSampleBatch) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_drops[subscriber] = self.subscriber_drops.get(subscriber, 0) + 1
            bytes_count = iq_batch_byte_count(batch)
            self.subscriber_drop_bytes[subscriber] = self.subscriber_drop_bytes.get(subscriber, 0) + bytes_count
            self.total_dropped_batches += 1
            self.total_dropped_bytes += bytes_count
            name = self.subscriber_names.get(subscriber, "subscriber")
            drops = self.subscriber_drops[subscriber]
            now = time.monotonic()
            should_log = now - self.last_drop_log_at >= 60.0
            if should_log:
                self.last_drop_log_at = now
        if should_log:
            LOG.warning(
                "RTL-SDR raw fanout is dropping IQ batches for %s: subscriber_drops=%s total_drops=%s",
                name,
                drops,
                self.total_dropped_batches,
            )


class ProcessedIqFanout:
    """Read batches from another fanout, process them once, and share the result.

    Subscribers get their own queue; a subscriber that falls behind loses its oldest
    batch rather than holding everyone else up.
    """

    label = "IQ fanout"
    thread_name = "iq-fanout"
    source_queue_name = "iq-fanout-source"

    def __init__(self, source_fanout, output_rate: int | None = None) -> None:
        self.source_fanout = source_fanout
        self.output_rate = None if output_rate is None else int(output_rate)
        self.source_queue = subscribe_raw_fanout(
            source_fanout,
            max_chunks=INTERMEDIATE_SOURCE_QUEUE_CHUNKS,
            name=self.source_queue_name,
        )
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_names: dict[queue.Queue, str] = {}
        self.subscriber_drops: dict[queue.Queue, int] = {}
        self.subscriber_drop_samples: dict[queue.Queue, int] = {}
        self.subscriber_max_depth: dict[queue.Queue, int] = {}
        self.subscribers_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.read_batches = 0
        self.read_samples = 0
        self.output_batches = 0
        self.output_samples = 0
        self.total_dropped_batches = 0
        self.total_dropped_samples = 0
        self.last_output_at = 0.0
        self.last_drop_log_at = 0.0
        self.generation = 0

    def subscribe(
        self,
        max_chunks: int | None = None,
        max_seconds: float | None = None,
        name: str = "subscriber",
    ) -> queue.Queue:
        if max_chunks is None:
            max_chunks = self._chunks_for_seconds(max_seconds or STREAM_WORKER_RAW_QUEUE_SECONDS)
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.subscribers_lock:
            self.subscribers.add(subscriber)
            self.subscriber_names[subscriber] = name
            self.subscriber_drops[subscriber] = 0
            self.subscriber_drop_samples[subscriber] = 0
            self.subscriber_max_depth[subscriber] = 0
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            self.subscribers.discard(subscriber)
            self.subscriber_names.pop(subscriber, None)
            self.subscriber_drops.pop(subscriber, None)
            self.subscriber_drop_samples.pop(subscriber, None)
            self.subscriber_max_depth.pop(subscriber, None)

    def _chunks_for_seconds(self, seconds: float) -> int:
        source_chunks_for_seconds = getattr(self.source_fanout, "_chunks_for_seconds", None)
        if callable(source_chunks_for_seconds):
            return int(source_chunks_for_seconds(seconds))
        return max(
            STREAM_WORKER_RAW_QUEUE_MIN_CHUNKS,
            min(
                STREAM_WORKER_RAW_QUEUE_MAX_CHUNKS,
                int(math.ceil(max(0.0, seconds) / RTL_ASYNC_BUFFER_SECONDS)),
            ),
        )

    def start(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name=self.thread_name, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.source_fanout.unsubscribe(self.source_queue)
        wake_queue(self.source_queue)
        if self.thread is not None:
            self.thread.join(timeout=2.0)

    def stats(self) -> dict[str, Any]:
        with self.subscribers_lock:
            subscribers = [self._subscriber_stats_locked(subscriber) for subscriber in self.subscribers]
            return {
                "sample_rate": self.output_rate,
                **self._extra_stats_locked(),
                "read_batches": self.read_batches,
                "read_samples": self.read_samples,
                "output_batches": self.output_batches,
                "output_samples": self.output_samples,
                "subscriber_count": len(self.subscribers),
                "total_dropped_batches": self.total_dropped_batches,
                "total_dropped_samples": self.total_dropped_samples,
                "last_output_age_seconds": (
                    round(time.monotonic() - self.last_output_at, 3)
                    if self.last_output_at
                    else None
                ),
                "subscribers": subscribers,
            }

    def _extra_stats_locked(self) -> dict[str, Any]:
        return {}

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        if subscriber is None:
            return {}
        with self.subscribers_lock:
            return self._subscriber_stats_locked(subscriber)

    def _reset_processing(self) -> None:
        """Forget processing state when the source switches to a different SDR or file."""

    def _process(self, source_batch, iq: np.ndarray) -> tuple[np.ndarray, int]:
        """Return the processed samples and their sample rate."""
        raise NotImplementedError

    def _run(self) -> None:
        source_generation = getattr(self.source_fanout, "generation", 0)
        last_slow_batch_log_at = 0.0
        while not self.stop_event.is_set():
            try:
                source_batch: RtlSampleBatch | IqSampleBatch = self.source_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            except Exception as exc:
                LOG.warning("%s failed to read IQ samples: %s", self.label, exc)
                continue
            if source_batch is QUEUE_WAKEUP:
                continue
            current_source_generation = getattr(self.source_fanout, "generation", source_generation)
            incoming_generation = batch_generation(source_batch)
            if incoming_generation < current_source_generation:
                continue
            if incoming_generation != source_generation:
                source_generation = incoming_generation
                self._reset_processing()
                with self.subscribers_lock:
                    self.generation += 1
                    cleared = 0
                    for subscriber in self.subscribers:
                        cleared += clear_queue_items(subscriber)
                LOG.info(
                    "%s switched source generation to %s; cleared %s queued batches",
                    self.label,
                    self.generation,
                    cleared,
                )
            iq = iq_batch_complex(source_batch)
            if iq.size == 0:
                continue
            process_started_at = time.monotonic()
            output, output_rate = self._process(source_batch, iq)
            process_seconds = time.monotonic() - process_started_at
            batch_seconds = float(iq.size) / float(max(1, source_batch.sample_rate))
            if process_seconds > batch_seconds and process_started_at - last_slow_batch_log_at >= 10.0:
                last_slow_batch_log_at = process_started_at
                LOG.warning(
                    "%s is slower than realtime: source_rate=%s output_rate=%s processed %.3fs IQ in %.3fs",
                    self.label,
                    source_batch.sample_rate,
                    output_rate,
                    batch_seconds,
                    process_seconds,
                )
            with self.subscribers_lock:
                self.read_batches += 1
                self.read_samples += int(iq.size)
                self.output_rate = int(output_rate)
            if output.size == 0:
                continue
            batch = IqSampleBatch(
                data=output,
                sample_rate=int(output_rate),
                center_frequency_hz=source_batch.center_frequency_hz,
                captured_at=source_batch.captured_at,
            )
            batch = set_batch_generation(batch, self.generation)
            self._publish(batch)

    def _publish(self, batch: IqSampleBatch) -> None:
        with self.subscribers_lock:
            subscribers = list(self.subscribers)
            self.output_batches += 1
            self.output_samples += int(batch.data.size)
            self.last_output_at = time.monotonic()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(batch)
                self._record_subscriber_depth(subscriber)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                self._record_subscriber_drop(subscriber, batch)
                try:
                    subscriber.put_nowait(batch)
                    self._record_subscriber_depth(subscriber)
                except queue.Full:
                    self._record_subscriber_drop(subscriber, batch)

    def _subscriber_stats_locked(self, subscriber: queue.Queue) -> dict[str, Any]:
        return {
            "name": self.subscriber_names.get(subscriber, "subscriber"),
            "queue_depth": subscriber.qsize(),
            "queue_capacity": subscriber.maxsize,
            "max_queue_depth": self.subscriber_max_depth.get(subscriber, 0),
            "dropped_batches": self.subscriber_drops.get(subscriber, 0),
            "dropped_samples": self.subscriber_drop_samples.get(subscriber, 0),
        }

    def _record_subscriber_depth(self, subscriber: queue.Queue) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_max_depth[subscriber] = max(
                self.subscriber_max_depth.get(subscriber, 0),
                subscriber.qsize(),
            )

    def _record_subscriber_drop(self, subscriber: queue.Queue, batch: IqSampleBatch) -> None:
        with self.subscribers_lock:
            if subscriber not in self.subscribers:
                return
            self.subscriber_drops[subscriber] = self.subscriber_drops.get(subscriber, 0) + 1
            self.subscriber_drop_samples[subscriber] = self.subscriber_drop_samples.get(subscriber, 0) + int(batch.data.size)
            self.total_dropped_batches += 1
            self.total_dropped_samples += int(batch.data.size)
            name = self.subscriber_names.get(subscriber, "subscriber")
            drops = self.subscriber_drops[subscriber]
            now = time.monotonic()
            should_log = now - self.last_drop_log_at >= 60.0
            if should_log:
                self.last_drop_log_at = now
        if should_log:
            LOG.warning(
                "%s is dropping batches for %s: subscriber_drops=%s total_drops=%s",
                self.label,
                name,
                drops,
                self.total_dropped_batches,
            )


class CleanIqFanout(ProcessedIqFanout):
    """The SDR's full-rate samples with its DC offset removed, shared by everything.

    Removing the offset once, here, means every stream, recording and remote feed
    starts from the same settled DC estimate instead of each settling its own.
    """

    label = "full-rate IQ fanout"
    thread_name = "iq-clean-fanout"
    source_queue_name = "iq-clean-source"

    def __init__(self, raw_fanout: RawRtlFanout) -> None:
        super().__init__(raw_fanout)
        self.dc_blocker: IqDcBlocker | None = None

    def _reset_processing(self) -> None:
        self.dc_blocker = None

    def _process(self, source_batch, iq: np.ndarray) -> tuple[np.ndarray, int]:
        sample_rate = int(source_batch.sample_rate)
        if self.dc_blocker is None or self.dc_blocker.sample_rate != sample_rate:
            self.dc_blocker = IqDcBlocker(sample_rate)
        return self.dc_blocker.process(iq), sample_rate


class IntermediateIqFanout(ProcessedIqFanout):
    """The full-rate feed decimated once to 192 ksps for streams and the receiver."""

    label = "intermediate IQ fanout"
    thread_name = "iq-intermediate-fanout"
    source_queue_name = "iq-intermediate-source"

    def __init__(
        self,
        source_fanout,
        output_rate: int = INTERMEDIATE_IQ_SAMPLE_RATE,
        *,
        alias_filter_strength: int = ALIAS_FILTER_STRENGTH_DEFAULT,
    ) -> None:
        super().__init__(source_fanout, output_rate)
        self.alias_filter_strength = validate_alias_filter_strength(alias_filter_strength)
        self.decimator = None
        self.decimator_key: tuple[int, int] | None = None
        self.decimator_alias_filter_strength: int | None = None

    def set_alias_filter_strength(self, value: int) -> None:
        value = validate_alias_filter_strength(value)
        with self.subscribers_lock:
            self.alias_filter_strength = value

    def _extra_stats_locked(self) -> dict[str, Any]:
        return {"alias_filter_strength": self.alias_filter_strength}

    def _reset_processing(self) -> None:
        self.decimator = None
        self.decimator_key = None
        self.decimator_alias_filter_strength = None

    def _process(self, source_batch, iq: np.ndarray) -> tuple[np.ndarray, int]:
        with self.subscribers_lock:
            alias_filter_strength = self.alias_filter_strength
        output_rate = int(INTERMEDIATE_IQ_SAMPLE_RATE if self.output_rate is None else self.output_rate)
        transition_hz = alias_filter_transition_hz(INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ, alias_filter_strength)
        attenuation_db = alias_filter_attenuation_db(INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB, alias_filter_strength)
        key = (int(source_batch.sample_rate), output_rate)
        if self.decimator is None or self.decimator_key != key:
            self.decimator = create_decimator(
                source_batch.sample_rate,
                output_rate,
                transition_hz=transition_hz,
                attenuation_db=attenuation_db,
            )
            self.decimator_key = key
            self.decimator_alias_filter_strength = alias_filter_strength
        elif self.decimator_alias_filter_strength != alias_filter_strength:
            update_decimator_alias_filter(
                self.decimator,
                source_batch.sample_rate,
                output_rate,
                transition_hz=transition_hz,
                attenuation_db=attenuation_db,
            )
            self.decimator_alias_filter_strength = alias_filter_strength
        return self.decimator.process(iq), output_rate


def subscribe_raw_fanout(
    fanout,
    *,
    max_chunks: int | None = None,
    max_seconds: float | None = None,
    name: str = "subscriber",
) -> queue.Queue:
    try:
        return fanout.subscribe(max_chunks=max_chunks, max_seconds=max_seconds, name=name)
    except TypeError:
        return fanout.subscribe(max_chunks=max_chunks, max_seconds=max_seconds)


class NullIqFanout:
    generation = 0

    def subscribe(
        self,
        *,
        max_chunks: int | None = None,
        max_seconds: float | None = None,
        name: str = "subscriber",
    ) -> queue.Queue:
        maxsize = max_chunks or max(1, int(round((max_seconds or 1.0) / STREAM_FRAME_SECONDS)))
        return queue.Queue(maxsize=maxsize)

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        clear_queue_items(subscriber)

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        if subscriber is None:
            return {}
        return {
            "name": "test-mode-null-fanout",
            "queue_depth": subscriber.qsize(),
            "queue_capacity": subscriber.maxsize,
            "max_queue_depth": 0,
            "dropped_batches": 0,
            "dropped_samples": 0,
        }


@dataclass
class ChannelFeedBatch:
    """A batch as one SdrChannelFeed consumer sees it.

    Local batches are shared by every subscriber of the intermediate fanout,
    so a feed passes on its own copy (sharing the samples) rather than
    re-stamping the shared batch's generation.
    """

    data: ComplexArray
    sample_rate: int
    center_frequency_hz: int
    captured_at: float
    source_generation: int = 0
    prechannelized: bool = False


class SdrChannelFeed:
    """One consumer's IQ, following whichever SDR the service is using.

    The weather radio receiver and each stream read from one of these instead
    of from an SDR directly, so switching RTL-SDRs, remote hosts, or between
    this machine's SDR and a remote one changes what feeds them without
    stopping them. `source_provider` returns this machine's intermediate
    fanout, a RemoteSdrClient, or None when there is no SDR. Local batches are
    the wideband intermediate IQ; remote batches arrive already channelized
    (`prechannelized`). A switch, or a restart of the underlying source, bumps
    `generation`, which makes the consumer reset its channel state.
    """

    def __init__(self, source_provider, frequency_hz: int, name: str) -> None:
        self.source_provider = source_provider
        self.frequency_hz = int(frequency_hz)
        self.name = name
        self.generation = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.subscriber: queue.Queue | None = None
        self.subscriber_max_seconds: float | None = None
        self.dropped_batches = 0
        self.source = None
        self.upstream = None
        self.upstream_queue: queue.Queue | None = None
        self.upstream_generation: int | None = None
        self.remote = False

    def current_source(self):
        try:
            return self.source_provider()
        except Exception:
            return None

    def subscribe(self, max_chunks: int | None = None, max_seconds: float | None = None, name: str = "subscriber") -> queue.Queue:
        if max_chunks is None:
            max_chunks = max(8, int(round((max_seconds or 1.5) * 20)))
        subscriber: queue.Queue = queue.Queue(maxsize=max_chunks)
        with self.lock:
            self.subscriber = subscriber
            self.subscriber_max_seconds = max_seconds
            if self.thread is None:
                self.stop_event.clear()
                self.thread = threading.Thread(target=self._run, name=f"sdr-feed-{self.name}", daemon=True)
                self.thread.start()
        return subscriber

    def unsubscribe(self, subscriber: queue.Queue) -> None:
        self.stop()

    def stop(self) -> None:
        self.stop_event.set()
        thread = self.thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._detach()

    def set_target_frequency(self, frequency_hz: int) -> None:
        frequency_hz = int(frequency_hz)
        with self.lock:
            if self.frequency_hz == frequency_hz:
                return
            self.frequency_hz = frequency_hz
            upstream = self.upstream if self.remote else None
        if upstream is not None:
            upstream.set_target_frequency(frequency_hz)

    def subscriber_stats(self, subscriber: queue.Queue | None) -> dict[str, Any]:
        with self.lock:
            upstream = self.upstream
            upstream_queue = self.upstream_queue
            dropped = self.dropped_batches
        stats: dict[str, Any] = {}
        upstream_stats = getattr(upstream, "subscriber_stats", None)
        if upstream_stats is not None and upstream_queue is not None:
            stats.update(upstream_stats(upstream_queue))
        stats.update(
            {
                "name": f"feed:{self.name}",
                "queue_depth": subscriber.qsize() if subscriber is not None else 0,
                "queue_capacity": subscriber.maxsize if subscriber is not None else 0,
                "feed_dropped_batches": dropped,
            }
        )
        return stats

    def _run(self) -> None:
        try:
            while not self.stop_event.is_set():
                self._follow_source()
                with self.lock:
                    upstream = self.upstream
                    upstream_queue = self.upstream_queue
                    remote = self.remote
                if upstream_queue is None:
                    self.stop_event.wait(0.1)
                    continue
                try:
                    batch = upstream_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                incoming = batch_generation(batch)
                if incoming < getattr(upstream, "generation", 0):
                    continue  # from before the source restarted
                with self.lock:
                    if upstream is not self.upstream:
                        continue
                    if incoming != self.upstream_generation:
                        self.upstream_generation = incoming
                        self.generation += 1
                    generation = self.generation
                    subscriber = self.subscriber
                if subscriber is None:
                    continue
                self._offer(
                    subscriber,
                    ChannelFeedBatch(
                        data=batch.data,
                        sample_rate=int(batch.sample_rate),
                        center_frequency_hz=int(batch.center_frequency_hz),
                        captured_at=float(getattr(batch, "captured_at", time.monotonic())),
                        source_generation=generation,
                        prechannelized=remote,
                    ),
                )
        finally:
            self._detach()

    def _follow_source(self) -> None:
        source = self.current_source()
        with self.lock:
            if source is self.source:
                return
        self._detach()
        upstream = None
        upstream_queue = None
        remote = source is not None and hasattr(source, "channel_fanout")
        with self.lock:
            frequency_hz = self.frequency_hz
            max_seconds = self.subscriber_max_seconds
        if source is not None:
            upstream = source.channel_fanout(frequency_hz, name=self.name) if remote else source
            upstream_queue = subscribe_raw_fanout(upstream, max_seconds=max_seconds or LIVE_IQ_QUEUE_SECONDS, name=self.name)
        with self.lock:
            self.source = source
            self.upstream = upstream
            self.upstream_queue = upstream_queue
            self.upstream_generation = None
            self.remote = remote
            self.generation += 1
            subscriber = self.subscriber
        if subscriber is not None:
            clear_queue_items(subscriber)
        if source is not None:
            LOG.info("%s now reads from %s", self.name, "a remote SDR" if remote else "this machine's SDR")

    def _detach(self) -> None:
        with self.lock:
            upstream = self.upstream
            upstream_queue = self.upstream_queue
            remote = self.remote
            self.source = None
            self.upstream = None
            self.upstream_queue = None
        if upstream is None:
            return
        try:
            if remote:
                upstream.stop()
            elif upstream_queue is not None:
                upstream.unsubscribe(upstream_queue)
        except Exception as exc:
            LOG.debug("releasing %s's SDR feed failed: %s", self.name, exc)

    def _offer(self, subscriber: queue.Queue, batch: ChannelFeedBatch) -> None:
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


def fanout_channel_profile(fanout: RawRtlFanout | IntermediateIqFanout) -> tuple[int, int] | None:
    if isinstance(fanout, SdrChannelFeed):
        fanout = fanout.current_source()
    if not isinstance(fanout, IntermediateIqFanout):
        return None
    center_frequency_hz = NWR_CENTER_FREQUENCY_HZ
    # Walk back through the processing stages to the fanout that reads the SDR itself.
    upstream = getattr(fanout, "source_fanout", None)
    while upstream is not None and not hasattr(upstream, "source"):
        upstream = getattr(upstream, "source_fanout", None)
    source = getattr(upstream, "source", None)
    config = getattr(source, "config", None)
    if config is not None:
        center_frequency_hz = int(getattr(config, "center_frequency_hz", center_frequency_hz))
    return int(fanout.output_rate), center_frequency_hz


def make_live_channelizer(
    *,
    fanout: RawRtlFanout | IntermediateIqFanout,
    target_frequency_hz: int,
    alias_filter_strength: int,
) -> tuple[IqChannelizer, tuple[int, int], int] | None:
    profile = fanout_channel_profile(fanout)
    if profile is None:
        return None
    input_rate, center_frequency_hz = profile
    transition_hz = alias_filter_transition_hz(
        CHANNEL_IQ_ALIAS_TRANSITION_HZ,
        alias_filter_strength,
    )
    attenuation_db = alias_filter_attenuation_db(
        DEFAULT_ALIAS_ATTENUATION_DB,
        alias_filter_strength,
    )
    return (
        IqChannelizer(
            input_rate=input_rate,
            center_frequency_hz=center_frequency_hz,
            target_frequency_hz=target_frequency_hz,
            output_rate=IQ_SAMPLE_RATE,
            transition_hz=transition_hz,
            alias_attenuation_db=attenuation_db,
        ),
        (input_rate, center_frequency_hz),
        alias_filter_strength,
    )


class HostChannelSource:
    """Host side of a remote channel feed: one 24 kS/s NWR channel.

    Channelizes this machine's intermediate IQ exactly as a local stream does
    and follows the capture if it restarts. Retuning happens in place.
    """

    def __init__(self, fanout_provider, frequency_hz: int, alias_filter_strength_provider, name: str) -> None:
        self.fanout_provider = fanout_provider
        self.frequency_hz = validate_receiver_frequency(frequency_hz)
        self.alias_filter_strength_provider = alias_filter_strength_provider
        self.name = name
        self.lock = threading.Lock()
        self.fanout = None
        self.queue: queue.Queue | None = None
        self.channelizer: IqChannelizer | None = None
        self.channelizer_key: tuple[int, int] | None = None
        self.channelizer_strength: int | None = None
        self.generation = 0

    def set_frequency(self, frequency_hz: int) -> None:
        frequency_hz = validate_receiver_frequency(frequency_hz)
        with self.lock:
            self.frequency_hz = frequency_hz

    def read(self, timeout: float):
        subscriber = self._subscription()
        if subscriber is None:
            time.sleep(timeout)
            return None
        try:
            batch = subscriber.get(timeout=timeout)
        except queue.Empty:
            return None
        fanout_generation = getattr(self.fanout, "generation", 0)
        incoming = batch_generation(batch)
        if incoming < fanout_generation:
            return None
        if incoming != self.generation:
            self.generation = incoming
            self.channelizer = None
        with self.lock:
            target = self.frequency_hz
        strength = self.alias_filter_strength_provider()
        transition_hz = alias_filter_transition_hz(CHANNEL_IQ_ALIAS_TRANSITION_HZ, strength)
        attenuation_db = alias_filter_attenuation_db(DEFAULT_ALIAS_ATTENUATION_DB, strength)
        key = (batch.sample_rate, batch.center_frequency_hz)
        if self.channelizer is None or self.channelizer_key != key:
            self.channelizer = IqChannelizer(
                input_rate=batch.sample_rate,
                center_frequency_hz=batch.center_frequency_hz,
                target_frequency_hz=target,
                output_rate=IQ_SAMPLE_RATE,
                transition_hz=transition_hz,
                alias_attenuation_db=attenuation_db,
            )
            self.channelizer_key = key
            self.channelizer_strength = strength
        elif self.channelizer_strength != strength:
            self.channelizer.update_alias_filter(transition_hz=transition_hz, attenuation_db=attenuation_db)
            self.channelizer_strength = strength
        if self.channelizer.target_frequency_hz != target:
            self.channelizer.set_target_frequency(target)
        channel_iq = self.channelizer.process_complex(iq_batch_complex(batch))
        return set_batch_generation(
            IqSampleBatch(data=channel_iq, sample_rate=IQ_SAMPLE_RATE, center_frequency_hz=target, captured_at=batch.captured_at),
            self.generation,
        )

    def _subscription(self) -> queue.Queue | None:
        fanout = self.fanout_provider()
        if fanout is not self.fanout:
            self._unsubscribe()
            self.fanout = fanout
            self.channelizer = None
            if fanout is not None:
                self.queue = subscribe_raw_fanout(fanout, max_seconds=REMOTE_HOST_CHANNEL_QUEUE_SECONDS, name=self.name)
                self.generation = getattr(fanout, "generation", 0)
        return self.queue

    def _unsubscribe(self) -> None:
        if self.fanout is not None and self.queue is not None:
            self.fanout.unsubscribe(self.queue)
        self.queue = None

    def close(self) -> None:
        self._unsubscribe()
        self.fanout = None


class HostWidebandSource:
    """Host side of a remote wideband feed, decimated like a local recording."""

    def __init__(self, fanout_provider, sample_rate: int, alias_filter_strength_provider, name: str) -> None:
        self.fanout_provider = fanout_provider
        self.sample_rate = validate_iq_recording_sample_rate(sample_rate)
        self.alias_filter_strength_provider = alias_filter_strength_provider
        self.name = name
        self.fanout = None
        self.queue: queue.Queue | None = None
        self.decimator = None
        self.decimator_key: tuple[int, int] | None = None
        self.decimator_strength: int | None = None
        self.generation = 0

    def set_frequency(self, frequency_hz: int) -> None:
        raise ValueError("wideband feeds cannot be retuned")

    def read(self, timeout: float):
        fanout = self.fanout_provider(self.sample_rate)
        if fanout is not self.fanout:
            self.close()
            self.fanout = fanout
            self.decimator = None
            if fanout is not None:
                self.queue = subscribe_raw_fanout(fanout, max_seconds=REMOTE_HOST_WIDEBAND_QUEUE_SECONDS, name=self.name)
                self.generation = getattr(fanout, "generation", 0)
        if self.queue is None:
            time.sleep(timeout)
            return None
        try:
            batch = self.queue.get(timeout=timeout)
        except queue.Empty:
            return None
        incoming = batch_generation(batch)
        if incoming < getattr(self.fanout, "generation", 0):
            return None
        if incoming != self.generation:
            self.generation = incoming
            self.decimator = None
        iq = iq_batch_complex(batch)
        if self.sample_rate > batch.sample_rate:
            raise ValueError("recording sample rate is higher than the RTL-SDR sample rate")
        if self.sample_rate == batch.sample_rate:
            output = iq
        else:
            strength = self.alias_filter_strength_provider()
            transition_hz = alias_filter_transition_hz(INTERMEDIATE_IQ_ALIAS_TRANSITION_HZ, strength)
            attenuation_db = alias_filter_attenuation_db(INTERMEDIATE_IQ_ALIAS_ATTENUATION_DB, strength)
            key = (batch.sample_rate, self.sample_rate)
            if self.decimator is None or self.decimator_key != key:
                self.decimator = create_decimator(
                    batch.sample_rate, self.sample_rate, transition_hz=transition_hz, attenuation_db=attenuation_db
                )
                self.decimator_key = key
                self.decimator_strength = strength
            elif self.decimator_strength != strength:
                update_decimator_alias_filter(
                    self.decimator, batch.sample_rate, self.sample_rate, transition_hz=transition_hz, attenuation_db=attenuation_db
                )
                self.decimator_strength = strength
            output = self.decimator.process(iq)
        return set_batch_generation(
            IqSampleBatch(data=output, sample_rate=self.sample_rate, center_frequency_hz=batch.center_frequency_hz, captured_at=batch.captured_at),
            self.generation,
        )

    def close(self) -> None:
        if self.fanout is not None and self.queue is not None:
            self.fanout.unsubscribe(self.queue)
        self.queue = None
        self.fanout = None


class LocalSdrRemoteBackend:
    """What the remote SDR server needs from this machine's SDR."""

    def __init__(self, service: "RtlControlService") -> None:
        self.service = service

    def status(self) -> dict[str, Any]:
        return self.service.remote_host_status()

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {key: changes[key] for key in REMOTE_SDR_HOST_SETTINGS if key in changes}
        if allowed:
            self.service.update(allowed)
        return self.service.remote_host_status()

    def reset_sdr(self) -> dict[str, Any]:
        self.service.start_sdr_reset_for_remote_client()
        return self.service.remote_host_status()

    def open_channel(self, frequency_hz: int, name: str) -> HostChannelSource:
        return HostChannelSource(
            lambda: self.service.intermediate_fanout,
            frequency_hz,
            self.service._alias_filter_strength,
            name,
        )

    def open_wideband(self, sample_rate: int, name: str) -> HostWidebandSource:
        def fanout_for(rate: int):
            if rate == INTERMEDIATE_IQ_SAMPLE_RATE:
                return self.service.intermediate_fanout
            return self.service.clean_fanout

        return HostWidebandSource(fanout_for, sample_rate, self.service._alias_filter_strength, name)


def stop_workers_together(workers: list[Any]) -> None:
    """Stop workers at the same time; each one's stop waits on its own threads."""
    if len(workers) == 1:
        workers[0].stop()
        return
    threads = [threading.Thread(target=worker.stop, name="worker-stop", daemon=True) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)


# Put on a worker's queue when it is told to stop, so a thread blocked waiting for data
# wakes at once instead of at the end of its polling timeout.
QUEUE_WAKEUP = object()


def wake_queue(target: queue.Queue | None) -> None:
    if target is None:
        return
    try:
        target.put_nowait(QUEUE_WAKEUP)
    except queue.Full:
        pass  # A full queue already wakes its reader.
    except Exception:
        # Waking is only a speed-up; it must never stop a shutdown from finishing.
        LOG.debug("could not wake queue while stopping", exc_info=True)


def drain_queue_to_latest(source: queue.Queue, first_item: Any) -> Any:
    latest = first_item
    while True:
        try:
            item = source.get_nowait()
        except queue.Empty:
            return latest
        if item is not QUEUE_WAKEUP:
            latest = item


def iq_batch_complex(batch: RtlSampleBatch | IqSampleBatch) -> ComplexArray:
    data = batch.data
    if isinstance(data, np.ndarray):
        return data.astype(np.complex64, copy=False)
    return rtl_u8_to_complex64(data)


def validate_iq_recording_sample_rate(raw: Any) -> int:
    try:
        sample_rate = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("I/Q recording sample rate must be a number") from exc
    if sample_rate not in IQ_RECORDER_SAMPLE_RATES:
        raise ValueError("select a supported I/Q recording sample rate")
    if sample_rate > DEFAULT_RTL_SAMPLE_RATE:
        raise ValueError("I/Q recording sample rate cannot be higher than the RTL-SDR sample rate")
    return sample_rate


def validate_receiver_frequency(raw: Any) -> int:
    try:
        frequency_hz = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("select a valid NWR receiver frequency") from exc
    if frequency_hz not in NWR_RECEIVER_CHANNELS_HZ:
        raise ValueError("select a valid NWR receiver frequency")
    return frequency_hz


def receiver_frequency_mhz(frequency_hz: int) -> str:
    return f"{frequency_hz / 1_000_000:.3f}"
