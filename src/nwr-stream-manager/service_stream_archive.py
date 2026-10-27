"""Stream archiving: settings, the archive folders, pruning and downloads."""

from __future__ import annotations

import logging
import math
import os
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .baseline_pages import STREAM_ARCHIVE_PLAY_PATH
from .eas_alerts import format_local_datetime
from .page_sections import format_uptime
from .storage import format_storage_bytes
from .stream_archive import (
    ARCHIVE_SAMPLE_RATE,
    EXPORT_FORMATS,
    EXPORT_MAX_SECONDS,
    MP3_FRAME_BYTES,
    MP3_FRAME_SAMPLES,
    PLAYBACK_BLOCK_FRAMES,
    PLAYBACK_DELAY_SAMPLES,
    PLAYBACK_PREROLL_FRAMES,
    RETENTION_LABELS,
    RETENTION_SECONDS,
    export_mp3,
    export_opus,
    export_wav,
    parse_local_datetime,
    playback_bytes,
    playback_files,
    playback_marks,
    playback_audio_spans,
    playback_mp3,
    playback_parts,
    playback_position,
    playback_samples,
    playback_skips,
    PlaybackPart,
    stream_archive_directory,
    stream_archive_settings_from_stream,
    StreamArchiveStore,
    validate_stream_archive_payload,
)
from .stream_config import enabled_output_count, save_streams

LOG = logging.getLogger(__name__)

PRUNE_INTERVAL_SECONDS = 300
# Delete archived audio: older than each of the times it can be kept, or all of it.
DELETE_CHOICES = tuple((value, f"Older than {label}") for value, label in RETENTION_LABELS.items()) + (
    ("all", "Delete all archived audio"),
)
# Shorter gaps, such as a moment's network dropout, are counted rather than listed.
GAP_LISTED_MIN_MS = 60_000
GAPS_LISTED = 5
# Players are remembered while they're used; a page whose player is gone opens another.
PLAYERS_KEPT = 32
PLAYER_IDLE_SECONDS = 12 * 3600


@dataclass
class _ArchivePlayer:
    """One opening of a stream's archive to play: its audio as it was then."""
    store: StreamArchiveStore
    parts: list[PlaybackPart]
    size: int
    used: float


class StreamArchiveConfirmationRequired(ValueError):
    """A change that would delete archived audio, sent without being confirmed."""


class StreamArchiveNoAudio(ValueError):
    """A time with nothing archived; next_seconds is where the player's audio resumes after it."""

    def __init__(self, message: str, next_seconds: float | None) -> None:
        super().__init__(message)
        self.next_seconds = next_seconds


class StreamArchivePlayerOutdated(ValueError):
    """A time archived since the player opened; a new player has it."""


def _local_text(epoch_ms: float) -> str:
    return format_local_datetime(datetime.fromtimestamp(epoch_ms / 1000, timezone.utc))


def _local_text_seconds(epoch_ms: float) -> str:
    """With seconds, for gaps: many last only seconds."""
    local = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc).astimezone()
    return f"{local:%B} {local.day}, {local.year}, {local.strftime('%I:%M:%S %p %Z').lstrip('0')}"


def _time_zone_name() -> str:
    """This computer's time zone, such as America/Detroit, for the page to show times in as
    they're typed; "" if it can't be told."""
    name = os.environ.get("TZ", "").lstrip(":")
    if "/" in name and not name.startswith("/"):
        return name
    try:
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            return target.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        return Path("/etc/timezone").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _gap_text(from_ms: int, to_ms: int) -> str:
    """Such as "October 23, 2026, 3:12:40 PM EDT to 3:20:05 PM EDT (7 minutes)"."""
    start = datetime.fromtimestamp(from_ms / 1000, timezone.utc).astimezone()
    end = datetime.fromtimestamp(to_ms / 1000, timezone.utc).astimezone()
    to_text = end.strftime("%I:%M:%S %p %Z").lstrip("0") if end.date() == start.date() else _local_text_seconds(to_ms)
    return f"{_local_text_seconds(from_ms)} to {to_text} ({format_uptime((to_ms - from_ms) / 1000)})"


def _gaps_note(earlier: int, short: int) -> str:
    notes = []
    if earlier:
        notes.append(f"{earlier} earlier {'gap isn' if earlier == 1 else 'gaps aren'}'t listed.")
    if short:
        notes.append(f"{short} {'gap' if short == 1 else 'gaps'} shorter than a minute {'isn' if short == 1 else 'aren'}'t listed.")
    return " ".join(notes)


def _no_audio_message(entries: list, start_ms: int, end_ms: int) -> str:
    """Why a download has nothing in it, and where the nearest archived audio is."""
    audio = [entry for entry in entries if entry.samples > 0]
    before = max((entry.end_ms for entry in audio if entry.end_ms <= start_ms), default=None)
    after = min((entry.start_ms for entry in audio if entry.start_ms >= end_ms), default=None)
    message = "There's no archived audio between those times."
    if before is not None and after is not None:
        return f"{message} Nothing was archived from {_local_text_seconds(before)} to {_local_text_seconds(after)}."
    if after is not None:
        return f"{message} The archive starts at {_local_text_seconds(after)}."
    if before is not None:
        return f"{message} The archive ends at {_local_text_seconds(before)}."
    return message


class StreamArchiveServiceMixin:
    """Mixed into RtlControlService."""

    def _init_stream_archives(self) -> None:
        self.stream_archive_stores: dict[str, StreamArchiveStore] = {}
        self.stream_archive_stores_lock = threading.Lock()
        self.stream_archive_players: dict[str, _ArchivePlayer] = {}
        self.stream_archive_stop = threading.Event()
        self.stream_archive_thread = threading.Thread(target=self._stream_archive_prune_loop, name="stream-archive-prune", daemon=True)

    def _start_stream_archives(self) -> None:
        self.stream_archive_thread.start()

    def _stop_stream_archives(self) -> None:
        self.stream_archive_stop.set()
        self.stream_archive_thread.join(timeout=2.0)

    def stream_archive_store(self, stream: dict[str, Any] | str) -> StreamArchiveStore:
        stream_id = str(stream.get("id", "") if isinstance(stream, dict) else stream)
        with self.stream_archive_stores_lock:
            store = self.stream_archive_stores.get(stream_id)
            if store is None:
                store = StreamArchiveStore(stream_archive_directory(self.state_path.parent, stream_id))
                self.stream_archive_stores[stream_id] = store
        store.load()
        return store

    def _stream_archive_prune_loop(self) -> None:
        self.prune_stream_archives()
        while not self.stream_archive_stop.wait(PRUNE_INTERVAL_SECONDS):
            self.prune_stream_archives()

    def prune_stream_archives(self) -> None:
        """Deletes archived audio older than each stream keeps, whether or not it's archiving now."""
        with self.lock:
            streams = [(str(stream.get("id", "")), stream_archive_settings_from_stream(stream)) for stream in self.streams]
        now_ms = time.time() * 1000
        for stream_id, settings in streams:
            directory = stream_archive_directory(self.state_path.parent, stream_id)
            if not directory.exists():
                continue
            try:
                freed = self.stream_archive_store(stream_id).prune(int(now_ms - RETENTION_SECONDS[settings.retention] * 1000))
            except Exception as exc:
                LOG.warning("couldn't delete old archived audio for stream %s: %s", stream_id, exc)
                continue
            if freed:
                LOG.info("deleted %s of archived audio older than %s for stream %s", format_storage_bytes(freed), RETENTION_LABELS[settings.retention], stream_id)

    def stream_archive_deletion(self, stream_id: str, retention: str) -> str:
        """What keeping a stream's audio for ``retention`` would delete now, or "" if nothing."""
        cutoff_ms = int(time.time() * 1000 - RETENTION_SECONDS[retention] * 1000)
        doomed = self.stream_archive_store(stream_id).bytes_older_than(cutoff_ms)
        if not doomed:
            return ""
        return (
            f"Keeping archived audio for {RETENTION_LABELS[retention]} deletes {format_storage_bytes(doomed)} "
            f"of archived audio from before {_local_text(cutoff_ms)}."
        )

    def update_stream_archive(self, payload: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(payload.get("stream_id", "")).strip()
        settings = validate_stream_archive_payload(payload.get("archive", payload))
        confirmed = bool(payload.get("confirm_delete"))
        with self.lock:
            stream = self._stream_locked(stream_id)
            current = stream_archive_settings_from_stream(stream)
        if settings.retention != current.retention and not confirmed:
            deletion = self.stream_archive_deletion(stream_id, settings.retention)
            if deletion:
                raise StreamArchiveConfirmationRequired(deletion)
        with self.lock:
            stream = self._stream_locked(stream_id)
            if current.enabled and not settings.enabled and enabled_output_count(stream, archive_enabled=False) <= 0:
                raise ValueError("At least one output must remain enabled for each stream.")
            stream["archive"] = asdict(settings)
            stream["updated_at"] = time.time()
            save_streams(self.streams_directory, self.streams)
            self._sync_stream_workers_locked()
        if settings.retention != current.retention:
            self.prune_stream_archives()
        LOG.info(
            "stream archiving for stream %s is %s, keeping %s",
            stream_id,
            "on" if settings.enabled else "off",
            RETENTION_LABELS[settings.retention],
        )
        return self.stream_status()

    def remove_stream_archive(self, stream_id: str) -> None:
        directory = stream_archive_directory(self.state_path.parent, stream_id)
        if not directory.exists():
            return
        self.stream_archive_store(stream_id).delete_all()
        with self.stream_archive_stores_lock:
            self.stream_archive_stores.pop(str(stream_id), None)
        LOG.info("deleted the archived audio of removed stream %s", stream_id)

    def _stream_archive_summary_locked(self, stream: dict[str, Any]) -> dict[str, Any]:
        stream_id = str(stream.get("id", ""))
        settings = stream_archive_settings_from_stream(stream)
        worker = self.stream_workers.get(stream_id)
        state = worker.archive_snapshot() if worker is not None else {"status": "disabled", "error": None}
        if settings.enabled and worker is None:
            state = {"status": "stopped", "error": None}
        summary: dict[str, Any] = {**asdict(settings), **state, "bytes": 0, "storage": "0 B", "ranges": []}
        if stream_archive_directory(self.state_path.parent, stream_id).exists():
            store = self.stream_archive_store(stream_id)
            ranges = store.ranges()
            size = store.total_bytes()
            summary.update(bytes=size, storage=format_storage_bytes(size), ranges=ranges)
            if ranges:
                summary["from_text"] = _local_text_seconds(ranges[0][0])
                summary["to_text"] = _local_text_seconds(ranges[-1][1])
        return summary

    def _stream_archives_available_locked(self) -> bool:
        """Whether any stream has archived audio, for the Stream archives link."""
        for stream in self.streams:
            stream_id = str(stream.get("id", ""))
            if stream_archive_directory(self.state_path.parent, stream_id).exists() and self.stream_archive_store(stream_id).has_audio():
                return True
        return False

    def stream_archives_status_locked(self) -> dict[str, dict[str, Any]]:
        return {str(stream.get("id", "")): self._stream_archive_summary_locked(stream) for stream in self.streams}

    def stream_archive_streams(self) -> dict[str, Any]:
        """The streams with archived audio, for the Stream archives page."""
        with self.lock:
            streams = list(self.streams)
            summaries = [(stream, self._stream_archive_summary_locked(stream)) for stream in streams]
        result = []
        for stream, summary in summaries:
            if not summary["ranges"]:
                continue
            station = stream.get("station") or {}
            first, last = summary["ranges"][0][0], summary["ranges"][-1][1]
            # The last hour archived, or as much as there is, in whole seconds within it.
            default_start = max(first, last - 3600_000)
            start = datetime.fromtimestamp(math.ceil(default_start / 1000)).astimezone()
            end = datetime.fromtimestamp(last // 1000).astimezone()
            if end <= start:
                start = datetime.fromtimestamp(default_start // 1000).astimezone()
            ranges = summary["ranges"]
            gaps = [(ranges[index][1], ranges[index + 1][0]) for index in range(len(ranges) - 1)]
            long_gaps = [gap for gap in gaps if gap[1] - gap[0] >= GAP_LISTED_MIN_MS]
            listed = long_gaps[-GAPS_LISTED:]
            result.append(
                {
                    "id": str(stream.get("id", "")),
                    "label": f"{station.get('callsign', 'Stream')} ({station.get('frequency', '')} MHz)",
                    "callsign": str(station.get("callsign", "")),
                    "from_text": summary["from_text"],
                    "to_text": summary["to_text"],
                    "storage": summary["storage"],
                    "gaps": [_gap_text(*gap) for gap in listed],
                    "gap_count": len(gaps),
                    "gaps_note": _gaps_note(len(long_gaps) - len(listed), len(gaps) - len(long_gaps)),
                    "default_start_date": start.strftime("%Y-%m-%d"),
                    "default_start_time": start.strftime("%H:%M:%S"),
                    "default_end_date": end.strftime("%Y-%m-%d"),
                    "default_end_time": end.strftime("%H:%M:%S"),
                    "time_zone": end.strftime("%Z"),
                }
            )
        return {"streams": result, "formats": [{"value": value, "label": label} for value, label in EXPORT_FORMATS]}

    def stream_archive_export(
        self, stream_id: str, start_date: str, start_time: str, end_date: str, end_time: str, fmt: str
    ) -> tuple[str, str, int, Iterator[bytes]]:
        """A download of a stream's archived audio: its name, type, length, and contents."""
        if fmt not in dict(EXPORT_FORMATS):
            raise ValueError("Choose Ogg Opus, MP3 or WAV.")
        with self.lock:
            stream = self._stream_locked(str(stream_id))
            callsign = str((stream.get("station") or {}).get("callsign") or "stream")
        start = parse_local_datetime(start_date, start_time)
        end = parse_local_datetime(end_date, end_time)
        if end <= start:
            raise ValueError("The end time must be after the start time.")
        if end - start > EXPORT_MAX_SECONDS:
            raise ValueError("Downloads can be up to 12 hours long. Choose a shorter time.")
        store = self.stream_archive_store(stream)
        start_ms, end_ms = int(start * 1000), int(end * 1000)
        entries = store.entries()
        if not any(entry.end_ms > start_ms and entry.start_ms < end_ms and entry.samples > 0 for entry in entries):
            raise ValueError(_no_audio_message(entries, start_ms, end_ms))
        directory: Path = store.directory
        local_start = datetime.fromtimestamp(start).astimezone()
        local_end = datetime.fromtimestamp(end).astimezone()
        name = f"{callsign}-archive-{local_start:%Y%m%d-%H%M}-{local_end:%Y%m%d-%H%M}"
        # Every format's size is known before any of it is made, so browsers and download
        # managers are told it; the audio is then converted as it's sent.
        export, extension, content_type = {
            "wav": (export_wav, "wav", "audio/wav"),
            "mp3": (export_mp3, "mp3", "audio/mpeg"),
            "opus": (export_opus, "opus", "audio/ogg"),
        }[fmt]
        # The files stay put until the download is done, however long that takes.
        held = [entry.name for entry in entries if entry.end_ms > start_ms - 3600_000 and entry.start_ms < end_ms]
        store.hold(held)
        try:
            length, chunks = export(directory, entries, start_ms, end_ms)
        except Exception:
            store.release(held)
            raise

        def released() -> Iterator[bytes]:
            try:
                yield from chunks
            finally:
                store.release(held)

        return f"{name}.{extension}", content_type, length, released()

    def stream_archive_player(self, stream_id: str) -> dict[str, Any]:
        """A player for a stream's archived audio, as it is now: where its MP3 is, how long
        it is, and the times in it. Gaps of over a minute are left out of it."""
        with self.lock:
            stream = self._stream_locked(str(stream_id))
        store = self.stream_archive_store(stream)
        parts = playback_parts(store.entries())
        if not parts:
            raise ValueError("Nothing has been archived for this stream yet.")
        player_id = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self.stream_archive_stores_lock:
            players = self.stream_archive_players
            for key in [key for key, player in players.items() if now - player.used > PLAYER_IDLE_SECONDS]:
                del players[key]
            while len(players) >= PLAYERS_KEPT:
                del players[min(players, key=lambda key: players[key].used)]
            players[player_id] = _ArchivePlayer(store, parts, playback_bytes(parts), now)
        rate = ARCHIVE_SAMPLE_RATE
        return {
            "id": player_id,
            "url": f"{STREAM_ARCHIVE_PLAY_PATH}?player={player_id}",
            "bytes": playback_bytes(parts),
            "seconds": playback_samples(parts) / rate,
            # The MP3's audio starts this much after its start: LAME's delay and the decoder's.
            "delay_seconds": PLAYBACK_DELAY_SAMPLES / rate,
            # When each place in it was heard; the times between run on at one second a second.
            "marks": [{"seconds": position / rate, "ms": ms} for position, ms in playback_marks(parts)],
            "skipped": [{"seconds": position / rate, "from_ms": from_ms, "to_ms": to_ms} for position, from_ms, to_ms in playback_skips(parts)],
            "time_zone": datetime.now().astimezone().strftime("%Z"),
            "time_zone_name": _time_zone_name(),
        }

    def stream_archive_player_bytes(self, player_id: str) -> int | None:
        with self.stream_archive_stores_lock:
            player = self.stream_archive_players.get(str(player_id))
            if player is None:
                return None
            player.used = time.monotonic()
            return player.size

    def stream_archive_play(self, player_id: str, first_byte: int, last_byte: int) -> Iterator[bytes]:
        """Bytes of a player's MP3, made as they're sent; its files stay put meanwhile."""
        with self.stream_archive_stores_lock:
            player = self.stream_archive_players.get(str(player_id))
        if player is None:
            raise ValueError("This player has closed. Open it again.")
        block = PLAYBACK_BLOCK_FRAMES * MP3_FRAME_BYTES
        first = max(0, (first_byte // block * PLAYBACK_BLOCK_FRAMES - PLAYBACK_PREROLL_FRAMES) * MP3_FRAME_SAMPLES)
        last = (last_byte // block + 1) * PLAYBACK_BLOCK_FRAMES * MP3_FRAME_SAMPLES
        held = playback_files(player.parts, first, last)
        player.store.hold(held)
        chunks = playback_mp3(player.store.directory, player.parts, first_byte, last_byte)

        def released() -> Iterator[bytes]:
            try:
                yield from chunks
            finally:
                player.store.release(held)

        return released()

    def stream_archive_player_time(self, player_id: str, date_text: str, time_text: str) -> dict[str, Any]:
        """Where a date and time is in a player's audio, for jumping there."""
        with self.stream_archive_stores_lock:
            player = self.stream_archive_players.get(str(player_id))
        if player is None:
            raise StreamArchivePlayerOutdated("This player has closed.")
        when_ms = parse_local_datetime(date_text, time_text) * 1000
        position = playback_position(player.parts, when_ms)
        if position is not None:
            return {"seconds": position / ARCHIVE_SAMPLE_RATE, "text": _local_text_seconds(when_ms)}
        if when_ms > time.time() * 1000:
            raise ValueError("That time hasn't happened yet.")
        spans = playback_audio_spans(player.parts)
        if when_ms >= spans[-1][1] and player.store.has_audio() and player.store.ranges()[-1][1] > spans[-1][1]:
            # Archived since the player opened: a new one has it.
            raise StreamArchivePlayerOutdated("That time was archived after this player opened.")
        before = max((end for _start, end in spans if end <= when_ms), default=None)
        after = min((start for start, _end in spans if start > when_ms), default=None)
        message = "There's no archived audio at that time."
        if before is not None and after is not None:
            message += f" Nothing was archived from {_local_text_seconds(before)} to {_local_text_seconds(after)}."
        elif after is not None:
            message += f" The archive starts at {_local_text_seconds(after)}."
        elif before is not None:
            message += f" The archive ends at {_local_text_seconds(before)}."
        raise StreamArchiveNoAudio(message, None if after is None else playback_position(player.parts, after) / ARCHIVE_SAMPLE_RATE)

    def stream_archive_delete_options(self, stream_id: str) -> dict[str, Any]:
        """What can be deleted from a stream's archive, and how much each would delete: only
        the choices that would delete something."""
        with self.lock:
            stream = self._stream_locked(str(stream_id))
        store = self.stream_archive_store(stream)
        now_ms = time.time() * 1000
        choices = []
        for value, label in DELETE_CHOICES:
            if value == "all":
                size = store.total_bytes()
            else:
                size = store.bytes_older_than(int(now_ms - RETENTION_SECONDS[value] * 1000))
            if size:
                choices.append({"id": value, "label": label, "bytes": size, "storage": format_storage_bytes(size)})
        return {"choices": choices, "total": store.total_bytes()}

    def delete_stream_archive_audio(self, stream_id: str, choice: str) -> dict[str, Any]:
        """Deletes a stream's archived audio older than a choice of time, or all of it."""
        if choice not in dict(DELETE_CHOICES):
            raise ValueError("Choose which archived audio to delete.")
        with self.lock:
            stream = self._stream_locked(str(stream_id))
            worker = self.stream_workers.get(str(stream_id))
        store = self.stream_archive_store(stream)
        if choice == "all":
            def delete_all() -> int:
                size = store.total_bytes()
                store.delete_all()
                return size

            # The file being written is finished first, and archiving carries on after.
            freed = worker.with_archive_stopped(delete_all) if worker is not None and worker.has_archive() else delete_all()
        else:
            freed = store.prune(int(time.time() * 1000 - RETENTION_SECONDS[choice] * 1000), include_held=True)
        if not freed:
            raise ValueError("There's no archived audio that old." if choice != "all" else "There's no archived audio to delete.")
        storage = format_storage_bytes(freed)
        which = "all of it" if choice == "all" else dict(DELETE_CHOICES)[choice].lower()
        LOG.info("deleted %s of archived audio (%s) for stream %s", storage, which, stream_id)
        return {"bytes": freed, "storage": storage, "message": f"{storage} of archived audio deleted."}
