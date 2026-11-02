"""Meta realtime transcription transport (LISTEN part A2).

WebSocket client for the Muse Voice Transcribe realtime endpoint,
PCM framing helpers, cumulative partial handling, finals persistence,
and the notes message builder. Key comes from the environment only
and is never logged. No em dashes in this file.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from pam_pm.core import router as router_mod
from pam_pm.core import store as store_mod
from pam_pm.core.masking import mask_text

logger = logging.getLogger(__name__)

REALTIME_URL = "wss://api.meta.ai/v1/asr/realtime"
TRANSCRIBE_MODEL = router_mod.TRANSCRIBE_MODEL

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_MS = 80
FRAME_BYTES = int(SAMPLE_RATE * (FRAME_MS / 1000.0) * SAMPLE_WIDTH * CHANNELS)
BYTES_PER_SECOND = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS

BUFFER_SECONDS = 30
BUFFER_MAX_BYTES = BYTES_PER_SECOND * BUFFER_SECONDS

HANDSHAKE_TIMEOUT = 10.0
MAX_SESSION_SECONDS = 7200.0

MODES = ("PUSH_TO_TALK", "ENDPOINTING", "DIARIZATION")
PARTIAL_MODES = ("CUMULATIVE", "DELTA")

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


def meta_api_key() -> str:
    """Return the Meta API key from the environment only."""
    return os.environ.get("META_API_KEY", "").strip()


def redact_text(text: object, key: str = "") -> str:
    """Redact bearer tokens and an explicit key value from log text."""
    out = _BEARER_RE.sub("Bearer [REDACTED]", str(text))
    candidate = (key or "").strip()
    if candidate and len(candidate) >= 4 and candidate in out:
        out = out.replace(candidate, "[REDACTED]")
    return out


def redact_for_log(payload: Any, key: str = "") -> Any:
    """Return a copy of payload with auth material redacted for logging."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for name, value in payload.items():
            lowered = str(name).lower()
            if lowered in ("accesstoken", "access_token", "authorization"):
                if isinstance(value, dict):
                    out[name] = {
                        k: ("[REDACTED]" if "token" in str(k).lower() else v)
                        for k, v in value.items()
                    }
                else:
                    out[name] = "[REDACTED]"
            else:
                out[name] = redact_for_log(value, key)
        return out
    if isinstance(payload, list):
        return [redact_for_log(item, key) for item in payload]
    if isinstance(payload, str):
        return redact_text(payload, key)
    return payload


def build_handshake(
    api_key: str | None = None,
    *,
    model: str = TRANSCRIBE_MODEL,
    mode: str = "ENDPOINTING",
    partial_mode: str = "CUMULATIVE",
    audio_encoding: str = "PCM_24KHZ",
    emit_audio_progress: bool = True,
    keywords: list[str] | None = None,
    language_bias: list[str] | None = None,
) -> dict[str, Any]:
    """Build the first-frame handshake dict for the realtime endpoint.

    Auth rides inside the handshake as authorization.accessToken with
    value "Bearer <key>". The key defaults to the environment and the
    returned dict must never be logged without redact_for_log.
    """
    key = (api_key if api_key is not None else meta_api_key()).strip()
    if not key:
        raise ValueError("META_API_KEY is missing.")
    try:
        router_mod.log_route(router_mod.CAP_TRANSCRIBE_REALTIME, model)
    except Exception:  # noqa: BLE001, S110
        pass
    want_mode = (mode or "ENDPOINTING").strip().upper()
    if want_mode not in MODES:
        want_mode = "ENDPOINTING"
    want_partial = (partial_mode or "CUMULATIVE").strip().upper()
    if want_partial not in PARTIAL_MODES:
        want_partial = "CUMULATIVE"
    handshake: dict[str, Any] = {
        "authorization": {"accessToken": "Bearer " + key},
        "audioEncoding": audio_encoding,
        "model": model,
        "mode": want_mode,
        "partialMode": want_partial,
        "emitAudioProgress": bool(emit_audio_progress),
    }
    if keywords:
        handshake["keywords"] = [str(k) for k in keywords if str(k).strip()]
    if language_bias:
        handshake["languageBias"] = [str(b) for b in language_bias if str(b).strip()]
    return handshake


def frame_pcm(data: bytes, frame_size: int = FRAME_BYTES) -> list[bytes]:
    """Split raw PCM bytes into fixed frames, keeping a short tail."""
    if frame_size <= 0:
        raise ValueError("frame_size must be positive")
    if not data:
        return []
    return [bytes(data[i : i + frame_size]) for i in range(0, len(data), frame_size)]


def pcm_silence(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Return PCM silence (zero bytes) for a duration in milliseconds."""
    if duration_ms <= 0:
        return b""
    count = int(sample_rate * (duration_ms / 1000.0)) * SAMPLE_WIDTH * CHANNELS
    return b"\x00" * count


def pcm_duration_ms(data: bytes, sample_rate: int = SAMPLE_RATE) -> float:
    """Return the duration of raw PCM16 mono bytes in milliseconds."""
    if sample_rate <= 0 or not data:
        return 0.0
    frames = len(data) / float(SAMPLE_WIDTH * CHANNELS)
    return frames / float(sample_rate) * 1000.0


def ensure_pcm16(data: bytes) -> bytes:
    """Pad odd length input so 16-bit sample pairing stays aligned."""
    if len(data) % 2 == 1:
        return bytes(data) + b"\x00"
    return bytes(data)


def transcription_cost(audio_seconds: float) -> float:
    """Estimate transcription cost at 3.00 per 1000 audio minutes."""
    return max(0.0, float(audio_seconds)) * (3.00 / 1000.0 / 60.0)


def backoff_delay(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """Return exponential backoff seconds for a reconnect attempt."""
    attempt = max(attempt, 0)
    return min(float(cap), float(base) * (2.0**attempt))


def is_retryable_close(code: int | None) -> bool:
    """Return True for close codes worth a reconnect with backoff."""
    return code in (1011, 1013)


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class TurnState:
    """One speech turn keyed by turn id."""

    turn_id: str
    text: str = ""
    speaker: str = ""
    start_ms: int = 0
    end_ms: int = 0
    complete: bool = False


@dataclass
class TranscriptUpdate:
    """One normalized update derived from a server event."""

    kind: str
    text: str = ""
    turn_id: str = ""
    speaker: str = ""
    start_ms: int = 0
    end_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


class TranscriptAccumulator:
    """Track cumulative partials, turns, speakers, and finals.

    CUMULATIVE partials replace the previous hypothesis. Finals arrive
    either as a transcript event with final true or as speechComplete
    for a turn id. Partials carry no turn id and are attributed to the
    turn opened by the most recent speechStart.
    """

    def __init__(self) -> None:
        self.turns: dict[str, TurnState] = {}
        self.order: list[str] = []
        self.current_turn: str | None = None
        self.partial_text: str = ""
        self._auto_turns = 0

    def _get_turn(self, turn_id: str) -> TurnState:
        key = (turn_id or "").strip()
        if not key:
            self._auto_turns += 1
            key = f"turn-{self._auto_turns}"
        turn = self.turns.get(key)
        if turn is None:
            turn = TurnState(turn_id=key)
            self.turns[key] = turn
            self.order.append(key)
        return turn

    def _active_turn_id(self, turn_id: str) -> str:
        if (turn_id or "").strip():
            return turn_id.strip()
        if self.current_turn:
            return self.current_turn
        self._auto_turns += 1
        key = f"turn-{self._auto_turns}"
        self.current_turn = key
        return key

    @staticmethod
    def _text_of(event: dict[str, Any]) -> str:
        for name in ("text", "transcript", "hypothesis"):
            value = event.get(name)
            if isinstance(value, str) and value.strip():
                return value
        return ""

    @staticmethod
    def _turn_of(event: dict[str, Any]) -> str:
        for name in ("turnId", "turn_id", "turn"):
            value = event.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _speaker_of(event: dict[str, Any]) -> str:
        for name in ("speaker", "label", "speakerLabel", "speaker_label"):
            value = event.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def handle_event(self, event: dict[str, Any]) -> TranscriptUpdate:
        """Normalize one server event into a transcript update."""
        if not isinstance(event, dict):
            return TranscriptUpdate(kind="ignored", raw={})
        if "type" not in event and "sessionId" in event:
            return TranscriptUpdate(
                kind="ack",
                text=str(event.get("sessionId", "")),
                raw=dict(event),
            )
        kind = str(event.get("type", "")).strip()
        lowered = kind.lower()
        turn_id = self._turn_of(event)
        text = self._text_of(event)
        speaker = self._speaker_of(event)
        start_ms = _as_int(event.get("startMs", event.get("start_ms", 0)))
        end_ms = _as_int(event.get("endMs", event.get("end_ms", 0)))
        audio_ms = _as_int(
            event.get("audioProcessedMs", event.get("audio_processed_ms", 0))
        )

        if lowered == "transcript":
            final = bool(event.get("final", False))
            if final:
                key = self._active_turn_id(turn_id)
                turn = self._get_turn(key)
                turn.text = text
                if speaker:
                    turn.speaker = speaker
                elif self.partial_text and not turn.speaker:
                    pass
                if start_ms:
                    turn.start_ms = start_ms
                if end_ms:
                    turn.end_ms = end_ms
                elif audio_ms:
                    turn.end_ms = audio_ms
                turn.complete = True
                self.partial_text = ""
                self.current_turn = key
                return TranscriptUpdate(
                    kind="final",
                    text=text,
                    turn_id=key,
                    speaker=turn.speaker,
                    start_ms=turn.start_ms,
                    end_ms=turn.end_ms,
                    raw=dict(event),
                )
            self.partial_text = text
            key = self._active_turn_id(turn_id)
            turn = self._get_turn(key)
            turn.text = text
            if start_ms and not turn.start_ms:
                turn.start_ms = start_ms
            if speaker:
                turn.speaker = speaker
            self.current_turn = key
            return TranscriptUpdate(
                kind="partial",
                text=text,
                turn_id=key,
                speaker=turn.speaker,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                raw=dict(event),
            )
        if lowered == "speechstart":
            key = self._active_turn_id(turn_id)
            turn = self._get_turn(key)
            if start_ms:
                turn.start_ms = start_ms
            elif audio_ms:
                turn.start_ms = audio_ms
            self.current_turn = key
            return TranscriptUpdate(
                kind="progress",
                turn_id=key,
                start_ms=turn.start_ms,
                raw=dict(event),
            )
        if lowered == "speaker":
            key = self._active_turn_id(turn_id)
            turn = self._get_turn(key)
            if speaker:
                turn.speaker = speaker
            self.current_turn = key
            return TranscriptUpdate(
                kind="speaker",
                turn_id=key,
                speaker=turn.speaker,
                raw=dict(event),
            )
        if lowered == "speechend":
            key = self._active_turn_id(turn_id)
            turn = self._get_turn(key)
            if end_ms:
                turn.end_ms = end_ms
            elif audio_ms:
                turn.end_ms = audio_ms
            self.current_turn = key
            return TranscriptUpdate(
                kind="progress",
                turn_id=key,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                raw=dict(event),
            )
        if lowered == "speechcomplete":
            key = self._active_turn_id(turn_id)
            turn = self._get_turn(key)
            turn.text = text
            if speaker:
                turn.speaker = speaker
            if start_ms:
                turn.start_ms = start_ms
            if end_ms:
                turn.end_ms = end_ms
            elif audio_ms:
                turn.end_ms = audio_ms
            turn.complete = True
            self.partial_text = ""
            self.current_turn = key
            return TranscriptUpdate(
                kind="final",
                text=text,
                turn_id=key,
                speaker=turn.speaker,
                start_ms=turn.start_ms,
                end_ms=turn.end_ms,
                raw=dict(event),
            )
        if lowered == "error":
            message = str(event.get("message", event.get("error", "")))
            return TranscriptUpdate(
                kind="error", text=message, turn_id=turn_id, raw=dict(event)
            )
        if lowered in ("audioprogress", "progress"):
            return TranscriptUpdate(
                kind="progress",
                turn_id=turn_id,
                start_ms=audio_ms,
                end_ms=audio_ms,
                raw=dict(event),
            )
        return TranscriptUpdate(kind="ignored", turn_id=turn_id, raw=dict(event))


class AudioBuffer:
    """Hold up to 30 seconds of sent audio for resend after reconnect."""

    def __init__(self, max_bytes: int = BUFFER_MAX_BYTES) -> None:
        self.max_bytes = max(1, int(max_bytes))
        self._chunks: deque[bytes] = deque()
        self._size = 0

    def __len__(self) -> int:
        return self._size

    def append(self, data: bytes) -> None:
        """Append bytes, dropping oldest chunks past the cap."""
        if not data:
            return
        item = bytes(data)
        if len(item) > self.max_bytes:
            item = item[-self.max_bytes :]
        self._chunks.append(item)
        self._size += len(item)
        while self._chunks and self._size > self.max_bytes:
            dropped = self._chunks.popleft()
            self._size -= len(dropped)

    def snapshot(self) -> bytes:
        """Return the buffered audio oldest first, capped at max bytes."""
        if not self._chunks:
            return b""
        blob = b"".join(self._chunks)
        if len(blob) > self.max_bytes:
            return blob[-self.max_bytes :]
        return blob

    def clear(self) -> None:
        """Drop all buffered audio."""
        self._chunks.clear()
        self._size = 0


LISTEN_NOTES_SYSTEM = """You are Pam, a meeting notes writer. Read the transcript and write notes.

Rules:
- Use only what the transcript says. Quote short evidence for each item.
- Write a short summary, decisions, action items with suggested owner and due date when stated, and RAID entries (risks, issues, dependencies, assumptions).
- Match notes to existing board items when the project context names them; propose updates instead of duplicates.
- Never invent names, dates, owners, or decisions. Flag missing owners as unassigned."""

LISTEN_TOOLS: list[dict[str, Any]] = []


def _render_volatile(volatile: dict[str, Any] | str | None) -> str:
    if volatile is None:
        return ""
    if isinstance(volatile, str):
        return volatile
    lines = [f"{key}: {volatile[key]}" for key in sorted(volatile)]
    header = "Live context (refreshes every call, never cached):"
    if not lines:
        return header
    return header + "\n" + "\n".join(lines)


def build_notes_messages(
    project_context: str,
    transcript: str,
    volatile: dict[str, Any] | str | None = None,
) -> dict[str, Any]:
    """Build the notes extraction messages with a stable cacheable prefix.

    Order follows the prompt caching playbook: stable system prompt,
    then stable project context, then the transcript in order, then the
    volatile block last. The first two messages are byte stable for one
    project no matter how the transcript or time changes. Transcript
    text is masked before it is placed in the messages.
    """
    safe_transcript = mask_text(transcript or "")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": LISTEN_NOTES_SYSTEM},
        {"role": "user", "content": project_context or ""},
        {"role": "user", "content": "Meeting transcript:\n" + safe_transcript},
    ]
    if volatile is not None:
        messages.append({"role": "user", "content": _render_volatile(volatile)})
    return {"messages": messages, "tools": LISTEN_TOOLS}


def persist_final(
    db: Any,
    session_id: str,
    project: str,
    update: TranscriptUpdate,
) -> int:
    """Persist one final update via store helpers, returning the row id."""
    return store_mod.insert_listen_segment(
        db,
        str(session_id),
        str(project or "default"),
        str(update.text or ""),
        speaker=str(update.speaker or ""),
        start_ms=int(update.start_ms or 0),
        end_ms=int(update.end_ms or 0),
        turn_id=str(update.turn_id or ""),
    )


ConnectFn = Callable[[str], Awaitable[Any]]


class RealtimeError(RuntimeError):
    """Realtime transport failure with secrets already redacted."""


class RealtimeClient:
    """Async WebSocket client for the Meta realtime transcribe endpoint.

    The handshake is sent as the first frame right after connecting and
    the client waits for the session ack before audio flows. Sent audio
    is buffered up to 30 seconds so a reconnect with backoff can resend
    it. Final segments are persisted to SQLite as they arrive.
    """

    def __init__(
        self,
        session_id: str,
        project: str = "default",
        db: Any = None,
        *,
        url: str = REALTIME_URL,
        model: str = TRANSCRIBE_MODEL,
        mode: str = "ENDPOINTING",
        partial_mode: str = "CUMULATIVE",
        max_seconds: float = MAX_SESSION_SECONDS,
        connect_fn: ConnectFn | None = None,
    ) -> None:
        self.session_id = str(session_id)
        self.project = (project or "default").strip() or "default"
        self._db = db
        self.url = url or REALTIME_URL
        self.model = model or TRANSCRIBE_MODEL
        self.mode = mode or "ENDPOINTING"
        self.partial_mode = partial_mode or "CUMULATIVE"
        self.max_seconds = float(max_seconds or MAX_SESSION_SECONDS)
        self._connect_fn = connect_fn
        self.accumulator = TranscriptAccumulator()
        self.buffer = AudioBuffer()
        self.server_session_id = ""
        self.audio_bytes_sent = 0
        self.audio_ms_sent = 0.0
        self._ws: Any = None
        self._start_mono: float | None = None
        self._closed = False

    def __repr__(self) -> str:
        return (
            f"RealtimeClient(session_id={self.session_id!r}, project={self.project!r}, mode={self.mode!r})"
        )

    @property
    def db(self) -> Any:
        """Return the configured db or the default console database."""
        if self._db is not None:
            return self._db
        from pam_pm.core.config import Config

        return Config().db_path()

    @property
    def connected(self) -> bool:
        """Return True when a socket is currently attached."""
        return self._ws is not None and not self._closed

    def elapsed_seconds(self) -> float:
        """Return seconds since connect, or 0 before connect."""
        if self._start_mono is None:
            return 0.0
        return max(0.0, time.monotonic() - self._start_mono)

    def remaining_seconds(self) -> float:
        """Return seconds left before the max session length."""
        return max(0.0, self.max_seconds - self.elapsed_seconds())

    def is_expired(self) -> bool:
        """Return True when the session passed its max length."""
        return self.elapsed_seconds() >= self.max_seconds

    async def _default_connect(self, url: str) -> Any:
        try:
            import websockets
        except ImportError as exc:
            raise RealtimeError(
                "websockets package is missing; install it to stream live."
            ) from exc
        connect = getattr(websockets, "connect", None)
        if connect is None:
            raise RealtimeError("websockets.connect is not available.")
        return await connect(url, max_size=None)

    async def connect(self) -> str:
        """Open the socket, send the handshake, and wait for the ack."""
        if self.is_expired():
            raise RealtimeError("listen session reached max length.")
        key = meta_api_key()
        if not key:
            raise RealtimeError("META_API_KEY is missing.")
        handshake = build_handshake(
            key,
            model=self.model,
            mode=self.mode,
            partial_mode=self.partial_mode,
        )
        factory = self._connect_fn or self._default_connect
        try:
            ws = await factory(self.url)
        except Exception as exc:
            raise RealtimeError(
                redact_text(f"realtime connect failed: {exc}", key)
            ) from exc
        self._ws = ws
        self._closed = False
        if self._start_mono is None:
            self._start_mono = time.monotonic()
        try:
            await asyncio.wait_for(
                self._send_json(handshake), timeout=HANDSHAKE_TIMEOUT
            )
        except TimeoutError as exc:
            raise RealtimeError("handshake send timed out.") from exc
        try:
            update = await self._recv_update(timeout=HANDSHAKE_TIMEOUT)
        except TimeoutError as exc:
            raise RealtimeError("handshake ack timed out.") from exc
        if update is None or update.kind != "ack":
            logger.warning(
                "realtime handshake ack missing: %s",
                redact_for_log(update.raw if update else {}, key),
            )
        else:
            self.server_session_id = update.text
        return self.server_session_id

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise RealtimeError("realtime socket is not connected.")
        await self._ws.send(json.dumps(payload))

    async def _send_bytes(self, data: bytes) -> None:
        if self._ws is None:
            raise RealtimeError("realtime socket is not connected.")
        await self._ws.send(bytes(data))

    async def send_audio(self, data: bytes) -> int:
        """Frame PCM bytes and send them, returning bytes accepted."""
        if self._ws is None:
            raise RealtimeError("realtime socket is not connected.")
        if self.is_expired():
            raise RealtimeError("listen session reached max length.")
        pcm = ensure_pcm16(data or b"")
        if not pcm:
            return 0
        sent = 0
        for frame in frame_pcm(pcm):
            await self._send_bytes(frame)
            self.buffer.append(frame)
            self.audio_bytes_sent += len(frame)
            self.audio_ms_sent += pcm_duration_ms(frame)
            sent += len(frame)
        return sent

    async def send_silence(self, duration_ms: int) -> int:
        """Send PCM silence to hold the stream open during pauses."""
        return await self.send_audio(pcm_silence(duration_ms))

    def _persist(self, update: TranscriptUpdate) -> int:
        if update.kind != "final" or not update.text.strip():
            return 0
        try:
            return persist_final(
                self.db, self.session_id, self.project, update
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("listen persist failed: %s", exc)
            return 0

    async def _recv_update(self, timeout: float | None = None) -> TranscriptUpdate | None:
        if self._ws is None:
            return None
        if timeout is not None:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        else:
            raw = await self._ws.recv()
        if isinstance(raw, bytes):
            return TranscriptUpdate(kind="ignored", raw={})
        try:
            event = json.loads(str(raw))
        except ValueError:
            return TranscriptUpdate(kind="ignored", raw={})
        if not isinstance(event, dict):
            return TranscriptUpdate(kind="ignored", raw={})
        update = self.accumulator.handle_event(event)
        if update.kind == "final":
            self._persist(update)
        return update

    async def recv_event(
        self, timeout: float | None = None
    ) -> TranscriptUpdate | None:
        """Read one server event, persisting finals, or None on close."""
        try:
            return await self._recv_update(timeout=timeout)
        except TimeoutError:
            raise
        except Exception as exc:
            name = type(exc).__name__
            if "closed" in name.lower() or "close" in name.lower():
                return None
            raise

    async def reconnect_with_resend(self, max_attempts: int = 5) -> bool:
        """Reconnect with backoff and resend the 30 s buffer once open."""
        pending = self.buffer.snapshot()
        attempts = max(1, int(max_attempts))
        for attempt in range(attempts):
            delay = backoff_delay(attempt)
            await asyncio.sleep(delay)
            try:
                await self.connect()
            except RealtimeError as exc:
                logger.warning("realtime reconnect %d failed: %s", attempt + 1, exc)
                continue
            if pending:
                try:
                    for frame in frame_pcm(bytes(pending)):
                        await self._send_bytes(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("realtime resend failed: %s", exc)
                    continue
            return True
        return False

    async def end_stream(self) -> None:
        """Half close input, then keep reading until the server closes."""
        if self._ws is None:
            return
        try:
            await self._send_json({"type": "endStream"})
        except Exception as exc:  # noqa: BLE001
            logger.warning("realtime endStream failed: %s", exc)

    async def close(self) -> None:
        """Close the socket without logging any secret material."""
        self._closed = True
        ws, self._ws = self._ws, None
        if ws is None:
            return
        try:
            await ws.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("realtime close failed: %s", exc)
