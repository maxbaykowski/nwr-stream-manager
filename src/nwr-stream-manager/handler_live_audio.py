"""Live audio and settings change events, sent to the browser over WebSockets."""

from __future__ import annotations

import logging
import queue
import time
from http import HTTPStatus
from urllib.parse import parse_qs

from .accounts import AccountRecord
from .constants import (
    LIVE_AUDIO_KEEPALIVE_SECONDS,
    LIVE_AUDIO_SHUTDOWN_CLOSE_CODE,
    SETTINGS_EVENTS_KEEPALIVE_SECONDS,
)
from .iq_pipeline import validate_receiver_frequency
from .live_audio import LiveAudioSource, LiveAudioWebSocketError, LiveAudioWebSocketWriter
from .rtl import NWR_CENTER_FREQUENCY_HZ
from .same_live import SameEventQueue
from .webrtc import (
    LIVE_AUDIO_UPSTREAM_HOLD_SECONDS,
    LIVE_AUDIO_UPSTREAM_STALL_SECONDS,
    OpusEncoder,
    TcpOpusBitrateController,
)


LOG = logging.getLogger(__name__)


class LiveAudioHandlerMixin:
    """Mixed into RtlControlHandler."""

    def _handle_live_audio_websocket(self, parsed) -> None:
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send_json({"error": "WebSocket upgrade is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        query = parse_qs(parsed.query)
        mode = query.get("mode", [""])[0].strip().lower()
        client_id = query.get("client_id", [""])[0].strip()
        codec = query.get("codec", ["opus"])[0].strip().lower()
        account = getattr(self, "current_account", None)
        account_id = account.id if isinstance(account, AccountRecord) else None
        source: LiveAudioSource | None = None
        cleanup_mode = ""
        encoder: OpusEncoder | None = None
        counted = False
        writer = LiveAudioWebSocketWriter(self)
        try:
            if mode == "monitor":
                stream_id = query.get("stream_id", [""])[0].strip()
                source, event_queue, status = self.service.open_monitor_audio_source(
                    client_id=client_id,
                    stream_id=stream_id,
                    account_id=account_id,
                )
                cleanup_mode = "monitor"
            elif mode == "receiver":
                frequency_hz = validate_receiver_frequency(query.get("frequency_hz", [NWR_CENTER_FREQUENCY_HZ])[0])
                source, event_queue, status = self.service.open_receiver_audio_source(
                    client_id=client_id,
                    frequency_hz=frequency_hz,
                    account_id=account_id,
                )
                cleanup_mode = "receiver"
            else:
                raise ValueError("select monitor or receiver audio")
            if codec not in {"opus", "pcm"}:
                codec = "opus"
            if codec == "opus":
                encoder = OpusEncoder(input_sample_rate=source.sample_rate, minimum_bitrate_kbps=6)
            writer.handshake()
            writer.send_json(
                {
                    "type": "start",
                    "mode": mode,
                    "codec": codec,
                    "sample_rate": 48_000 if codec == "opus" else source.sample_rate,
                    "input_sample_rate": source.sample_rate,
                    "channels": 1,
                    "frame_ms": 20,
                    "binary_audio": True,
                    "status": status,
                }
            )
            LOG.info("started %s live-audio WebSocket for client %s using %s", mode, client_id, codec)
            self._change_live_audio_connections(1)
            counted = True
            self._stream_live_audio_websocket(
                writer,
                source,
                event_queue,
                encoder=encoder,
                codec=codec,
                mode=mode,
                client_id=client_id,
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            LOG.info("live-audio WebSocket disconnected for client %s", client_id)
        except Exception as exc:
            LOG.warning("live-audio WebSocket failed for %s from %s: %s", client_id or "<missing>", self._client_address(), exc)
            if not getattr(self, "_headers_buffer", None):
                return
            try:
                self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            except Exception:
                pass
        finally:
            if counted:
                self._change_live_audio_connections(-1)
            if encoder is not None:
                encoder.close()
            self.service.clear_live_audio_feedback(client_id, mode)
            detached_receiver = None
            detached_monitor_source = None
            try:
                with self.service.lock:
                    # A page that reconnects (after switching networks, say) replaces this
                    # session with a new one; that one is left alone.
                    if cleanup_mode and not self.service._live_audio_session_is_current_locked(cleanup_mode, client_id, source):
                        cleanup_mode = ""
                    if cleanup_mode == "monitor":
                        self.service._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=False)
                        detached_monitor_source = self.service._remove_monitor_source_locked(client_id)
                    elif cleanup_mode == "receiver":
                        self.service._ensure_webrtc_client_owner_locked(client_id, account_id, require_existing=False)
                        detached_receiver = self.service._detach_receiver_locked(client_id)
            except Exception as exc:
                LOG.debug("live-audio cleanup failed for client %s: %s", client_id, exc)
            self.service._close_detached_monitor_sources([detached_monitor_source])
            if detached_receiver is not None:
                detached_receiver.stop()

    def _handle_settings_events_websocket(self) -> None:
        """Tell the page each time SDR settings change, so it can show them at once."""
        if self.headers.get("Upgrade", "").lower() != "websocket":
            self._send_json({"error": "WebSocket upgrade is required"}, status=HTTPStatus.BAD_REQUEST)
            return
        writer = LiveAudioWebSocketWriter(self)
        try:
            seen = self.service.settings_change_count
            writer.handshake()
            while not self.service.shutting_down.is_set():
                count = self.service.wait_for_settings_change(seen, SETTINGS_EVENTS_KEEPALIVE_SECONDS)
                if self.service.shutting_down.is_set():
                    break
                if count != seen:
                    seen = count
                    writer.send_json({"type": "settings_changed"})
                else:
                    writer.send_json({"type": "keepalive"})
            writer.send_close(LIVE_AUDIO_SHUTDOWN_CLOSE_CODE, "server restarting")
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except LiveAudioWebSocketError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            LOG.debug("settings events WebSocket failed for %s: %s", self._client_address(), exc)

    def _change_live_audio_connections(self, delta: int) -> None:
        with self.service.lock:
            self.service.live_audio_connections = getattr(self.service, "live_audio_connections", 0) + delta

    def _stream_live_audio_websocket(
        self,
        writer: LiveAudioWebSocketWriter,
        source: LiveAudioSource,
        event_queue: SameEventQueue | None,
        *,
        encoder: OpusEncoder | None,
        codec: str,
        mode: str,
        client_id: str,
    ) -> None:
        sequence = 0
        last_event_check = 0.0
        bitrate_controller = TcpOpusBitrateController() if codec == "opus" and encoder is not None else None
        # Frames go out as the stream makes them; the stream itself runs in real time,
        # and the browser's buffer smooths out any unevenness. Stalls in this machine's
        # own audio (SDR lost, remote SDR link behind) are not the browser's
        # connection; see TcpOpusBitrateController.
        waiting_since: float | None = None
        upstream_hold_until = 0.0
        # Tells the page when this machine itself has no audio to send, so it can tell
        # that apart from audio held up on the way. The notices share the connection
        # with the audio, so they always arrive in order with it.
        told_no_audio = False
        last_keepalive_at = 0.0
        shutting_down = getattr(self.service, "shutting_down", None)
        while True:
            if shutting_down is not None and shutting_down.is_set():
                # Let the page know right away, so it can wait for the server to come back.
                writer.send_json({"type": "server_shutdown"})
                writer.send_close(LIVE_AUDIO_SHUTDOWN_CLOSE_CODE, "server shutting down")
                return
            if source.audio_source.closed.is_set():
                return
            now = time.monotonic()
            if event_queue is not None and now - last_event_check >= 0.02:
                last_event_check = now
                for _ in range(8):
                    try:
                        event = event_queue.queue.get_nowait()
                    except queue.Empty:
                        break
                    writer.send_json({"type": "same_event", "event": event})
            if waiting_since is None:
                waiting_since = time.monotonic()
            pcm = source.read_frame(timeout=LIVE_AUDIO_UPSTREAM_STALL_SECONDS)
            if pcm is None:
                if not told_no_audio and not source.audio_source.closed.is_set():
                    writer.send_json({"type": "source_no_audio"})
                    told_no_audio = True
                    last_keepalive_at = time.monotonic()
                elif time.monotonic() - last_keepalive_at >= LIVE_AUDIO_KEEPALIVE_SECONDS:
                    # Lets the page tell a quiet connection from one that has died.
                    writer.send_json({"type": "keepalive"})
                    last_keepalive_at = time.monotonic()
                continue
            frame_at = time.monotonic()
            waited_seconds = frame_at - waiting_since
            waiting_since = None
            if told_no_audio:
                writer.send_json({"type": "source_audio_back", "seconds": round(waited_seconds, 3)})
                told_no_audio = False
            source_paused = source.is_paused()
            if waited_seconds >= LIVE_AUDIO_UPSTREAM_STALL_SECONDS:
                if frame_at >= upstream_hold_until:
                    LOG.info(
                        "live-audio WebSocket source for %s client %s stalled upstream; holding Opus bitrate",
                        mode,
                        client_id,
                    )
                upstream_hold_until = frame_at + LIVE_AUDIO_UPSTREAM_HOLD_SECONDS
            if codec == "opus" and encoder is not None:
                packets = encoder.encode(pcm)
                if not packets:
                    continue
                for packet in packets:
                    send_seconds = writer.send_audio(
                        codec="opus",
                        sequence=sequence,
                        duration_ms=20,
                        sample_rate=48_000,
                        payload=packet,
                    )
                    if bitrate_controller is not None and encoder is not None and not source_paused:
                        upstream_limited = time.monotonic() < upstream_hold_until
                        # Consumed either way, so feedback from a stall is not
                        # applied once the hold ends.
                        feedback = self.service.consume_live_audio_feedback(client_id, mode)
                        latency_drop_count = int(feedback.get("latency_drop_count", 0)) if feedback else 0
                        above_max_drop_count = int(feedback.get("above_max_drop_count", 0)) if feedback else 0
                        network_receive_kbps = feedback.get("network_receive_kbps") if feedback else None
                        media_delivery_ratio = feedback.get("media_delivery_ratio") if feedback else None
                        bitrate_decision = bitrate_controller.update(
                            send_seconds=send_seconds,
                            latency_drop_count=latency_drop_count,
                            above_max_drop_count=above_max_drop_count,
                            media_delivery_ratio=media_delivery_ratio,
                            upstream_limited=upstream_limited,
                        )
                        next_bitrate = bitrate_decision.bitrate_kbps
                        if next_bitrate != encoder.bitrate_kbps:
                            previous_bitrate = encoder.bitrate_kbps
                            encoder.set_bitrate(next_bitrate)
                            LOG.info(
                                "live-audio WebSocket adjusted Opus bitrate for %s client %s from %s to %s Kbps after %.3fs send (%s, latency_drops=%s, above_max_drops=%s, receive_kbps=%s, media_ratio=%s)",
                                mode,
                                client_id,
                                previous_bitrate,
                                next_bitrate,
                                send_seconds,
                                bitrate_decision.reason,
                                latency_drop_count,
                                above_max_drop_count,
                                None if network_receive_kbps is None else round(float(network_receive_kbps), 1),
                                None if media_delivery_ratio is None else round(float(media_delivery_ratio), 3),
                            )
                    sequence += 1
            else:
                writer.send_audio(
                    codec="pcm_s16le",
                    sequence=sequence,
                    duration_ms=20,
                    sample_rate=source.sample_rate,
                    payload=pcm,
                )
                sequence += 1
