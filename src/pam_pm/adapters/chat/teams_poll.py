"""Microsoft Graph polling for two-way Teams in Pam.

Pam checks watched 1:1 chats and channels on the existing poll
interval (Settings poll value, default 60 seconds). Each watched chat
keeps a per-chat watermark timestamp in SQLite; every poll fetches
only newer messages. A message becomes inbound when it mentions Pam
or lands in a watched 1:1 chat. Matches are published on the same
event bus Slack messages flow through, and flow through the router
handle path like Slack events.

No public endpoint and no inbound ports are used. Importing or
constructing anything here never touches the network. Tests use
httpx.MockTransport or a stub graph client.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx

from pam_pm.adapters.chat.base import ChatMessage

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

WATERMARK_PREFIX = "teams-watermark:"

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")
_HTML_RE = re.compile(r"<[^>]+>")
_PAM_WORD_RE = re.compile(r"\bpam\b", re.IGNORECASE)


def redact(text: object) -> str:
    """Redact Bearer tokens from Graph poller log text."""
    return _BEARER_RE.sub("Bearer [REDACTED]", str(text))


class TeamsPollError(RuntimeError):
    """Graph poll failure with tokens already redacted."""


def strip_html(text: str) -> str:
    """Return Teams message HTML as plain text."""
    out = _HTML_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", out).strip()


def message_text(message: dict[str, Any]) -> str:
    """Return the plain text of a Graph chat message."""
    body = message.get("body") or {}
    if isinstance(body, dict):
        content = str(body.get("content", "") or "")
        if str(body.get("contentType", "") or "").lower() == "html":
            return strip_html(content)
        return content.strip()
    return str(message.get("text", "") or "").strip()


def mentions_pam(
    message: dict[str, Any], bot_names: tuple[str, ...] = ("pam",)
) -> bool:
    """Return True when a Graph message mentions Pam.

    Structured mentions win: any mention whose display name matches a
    bot name counts. Otherwise fall back to an @pam or bare pam word
    in the plain text.
    """
    names = [str(name).strip().lower() for name in bot_names if str(name).strip()]
    if not names:
        names = ["pam"]
    mentions = message.get("mentions") or []
    if isinstance(mentions, list):
        for mention in mentions:
            if not isinstance(mention, dict):
                continue
            mentioned = mention.get("mentioned") or {}
            if not isinstance(mentioned, dict):
                continue
            for key in ("displayName", "name"):
                label = str(mentioned.get(key, "") or "").strip().lower()
                if label and any(label == name or name in label for name in names):
                    return True
    text = message_text(message)
    lowered = text.lower()
    return any(name in lowered for name in names) and bool(
        _PAM_WORD_RE.search(text) or "@pam" in lowered
    )


def is_dm(chat: dict[str, Any] | None) -> bool:
    """Return True when a watched chat is a 1:1 chat."""
    if not isinstance(chat, dict):
        return False
    return str(chat.get("chatType", "") or "").strip().lower() == "oneonone"


def _sender_id(message: dict[str, Any]) -> str:
    sender = message.get("from") or {}
    if isinstance(sender, dict):
        user = sender.get("user") or {}
        if isinstance(user, dict):
            return str(user.get("id", "") or "")
        app = sender.get("application") or {}
        if isinstance(app, dict):
            return str(app.get("id", "") or "")
    return ""


def is_own_message(message: dict[str, Any], bot_id: str = "") -> bool:
    """Return True when Pam itself sent the message."""
    if not bot_id:
        return False
    return _sender_id(message) == bot_id


def should_handle(
    message: dict[str, Any],
    chat_type: str = "",
    bot_id: str = "",
    bot_names: tuple[str, ...] = ("pam",),
) -> bool:
    """Return True when a polled message becomes an inbound event.

    System and event messages are skipped, as are Pam's own sends. A
    1:1 chat message always counts; a channel or group message counts
    only when it mentions Pam.
    """
    if not isinstance(message, dict):
        return False
    kind = str(message.get("messageType", "") or "message").strip().lower()
    if kind and kind != "message":
        return False
    if is_own_message(message, bot_id):
        return False
    if not message_text(message) and not (message.get("attachments") or []):
        return False
    if str(chat_type or "").strip().lower() == "oneonone":
        return True
    return mentions_pam(message, bot_names)


def graph_message_to_chat_message(
    message: dict[str, Any], chat_id: str = ""
) -> ChatMessage:
    """Convert a Graph chat message to an inbound ChatMessage."""
    mid = str(message.get("id", "") or "")
    created = str(message.get("createdDateTime", "") or "")
    return ChatMessage(
        id=mid or created,
        user_id=_sender_id(message) or str(chat_id or ""),
        text=message_text(message),
        ts=created,
        inbound=True,
    )


def publish_graph_message_to_bus(
    bus: Any, message: ChatMessage, chat_id: str = "", chat_type: str = ""
) -> dict[str, Any]:
    """Publish a polled Teams message onto the shared EventBus.

    Uses the same event shape Slack Socket Mode and Bot Framework
    activities flow through, so rules and the activity stream see one
    shape either way.
    """
    event = {
        "source": "teams",
        "operation": "chat_message",
        "item_id": "",
        "user_id": message.user_id,
        "text": message.text,
        "evidence_quote": message.text[:200],
        "status": "received",
        "detail": f"Teams message from {message.user_id} in {chat_id or 'chat'}.",
        "chat_id": str(chat_id or ""),
        "chat_type": str(chat_type or ""),
        "message_id": message.id,
    }
    return bus.publish(event)


class GraphClient:
    """Minimal Microsoft Graph chat reader over httpx."""

    def __init__(
        self,
        token_provider: Any,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        base_url: str = GRAPH_BASE,
    ) -> None:
        self._tokens = token_provider
        self._timeout = timeout
        self._transport = transport
        self._base = (base_url or GRAPH_BASE).rstrip("/")
        self._client: httpx.Client | None = None

    def _client_or_new(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, transport=self._transport)
        return self._client

    def _fail(self, op: str, exc: Exception) -> TeamsPollError:
        detail = redact(exc)
        logger.error("teams graph %s failed: %s", op, detail)
        return TeamsPollError(f"{op} failed: {detail}")

    def _resolve_token(self) -> str:
        provider: object = self._tokens
        getter = getattr(provider, "access_token", None)
        if callable(getter):
            return str(getter())
        if callable(provider):
            return str(provider())  # type: ignore[operator]
        raise TeamsPollError("token provider needs access_token or a callable.")

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        client = self._client_or_new()
        try:
            token = self._resolve_token()
        except Exception as exc:
            raise self._fail("token", exc) from exc
        try:
            resp = client.get(
                self._base + path,
                headers={"Authorization": "Bearer " + token},
                params=params or {},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            raise self._fail("get " + path, exc) from exc

    def get_chat(self, chat_id: str) -> dict[str, Any]:
        """Return chat metadata, including chatType for DM detection."""
        payload = self._get(f"/chats/{chat_id}")
        return dict(payload) if isinstance(payload, dict) else {}

    def list_chat_messages(
        self, chat_id: str, since: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return chat messages, only newer than since when given."""
        params: dict[str, str] = {
            "$top": str(max(1, min(int(limit), 50))),
            "$orderby": "createdDateTime",
        }
        if (since or "").strip():
            params["$filter"] = f"createdDateTime gt '{since.strip()}'"
        payload = self._get(f"/chats/{chat_id}/messages", params)
        items = payload.get("value", []) if isinstance(payload, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def list_channel_messages(
        self,
        team_id: str,
        channel_id: str,
        since: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Return channel messages, only newer than since when given."""
        params: dict[str, str] = {
            "$top": str(max(1, min(int(limit), 50))),
            "$orderby": "createdDateTime",
        }
        if (since or "").strip():
            params["$filter"] = f"createdDateTime gt '{since.strip()}'"
        payload = self._get(
            f"/teams/{team_id}/channels/{channel_id}/messages", params
        )
        items = payload.get("value", []) if isinstance(payload, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None


class AppTokenProvider:
    """Cached Graph app-only token minting with client credentials.

    The first call mints with app_token; later calls reuse the cached
    token until near expiry, then mint again. No refresh token is used.
    """

    def __init__(
        self,
        app_id: str = "",
        tenant_id: str = "",
        client_secret: str = "",
        data_dir: str | Path | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._app_id = (app_id or "").strip()
        self._tenant_id = (tenant_id or "").strip()
        self._client_secret = (client_secret or "").strip()
        self._data_dir = data_dir
        self._timeout = timeout
        self._transport = transport
        self._now = time_fn or time.time
        self._token = ""
        self._expires_at = 0.0

    def _resolve_config(self) -> None:
        if self._app_id and self._tenant_id and self._client_secret:
            return
        try:
            from pam_pm.core import secrets as secrets_mod

            effective = secrets_mod.effective_teams_config(self._data_dir)
        except Exception:  # noqa: BLE001
            effective = {}
        self._app_id = self._app_id or str(effective.get("app_id", "") or "")
        self._tenant_id = self._tenant_id or str(effective.get("tenant_id", "") or "")
        self._client_secret = self._client_secret or str(
            effective.get("app_password", "") or ""
        )

    def access_token(self) -> str:
        """Return a valid Graph app token, minting when needed."""
        if self._token and self._now() < self._expires_at:
            return self._token
        self._resolve_config()
        from pam_pm.adapters.chat.teams_auth import app_token

        token = app_token(
            self._app_id,
            self._tenant_id,
            self._client_secret,
            transport=self._transport,
            timeout=self._timeout,
        )
        self._token = token
        self._expires_at = self._now() + max(1, 3600 - 300)
        logger.info("teams graph app token minted")
        return self._token

    def __call__(self) -> str:
        return self.access_token()

    def clear_cache(self) -> None:
        """Drop the cached token so the next call mints again."""
        self._token = ""
        self._expires_at = 0.0


def _watermark_key(chat_id: str) -> str:
    return WATERMARK_PREFIX + str(chat_id or "").strip()


class TeamsPoller:
    """Poll watched chats and channels with per-chat watermarks.

    Each watched chat stores its own watermark timestamp in SQLite.
    Every poll fetches only newer messages; matches (DM or mention)
    are published to the bus and returned. Messages already seen by id
    are never emitted twice, even when timestamps tie.
    """

    def __init__(
        self,
        graph: Any,
        db: str | Path | sqlite3.Connection,
        chats: list[dict[str, Any]] | None = None,
        interval: int | None = None,
        bus: Any | None = None,
        bot_id: str = "",
        bot_names: tuple[str, ...] = ("pam",),
        on_message: Callable[[ChatMessage], None] | None = None,
    ) -> None:
        self.graph = graph
        self._db = db
        self.chats = [dict(chat) for chat in (chats or [])]
        self._interval_override = interval
        if interval is None:
            from pam_pm.rules.poller import effective_poll_interval

            self.interval = effective_poll_interval(db)
        else:
            self.interval = interval
        self.bus = bus
        self.bot_id = (bot_id or "").strip()
        self.bot_names = bot_names
        self.on_message = on_message
        self._seen_ids: set[str] = set()

    def _connect(self) -> sqlite3.Connection:
        if isinstance(self._db, sqlite3.Connection):
            return self._db
        path = str(self._db)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path)

    def _close(self, conn: sqlite3.Connection) -> None:
        if not isinstance(self._db, sqlite3.Connection):
            conn.commit()
            conn.close()
        else:
            conn.commit()

    def get_watermark(self, chat_id: str) -> str | None:
        """Return the stored watermark for one chat, or None when new."""
        from pam_pm.rules.poller import POLLER_DDL

        conn = self._connect()
        try:
            conn.execute(POLLER_DDL)
            row = conn.execute(
                "SELECT value FROM poller_state WHERE key = ?",
                (_watermark_key(chat_id),),
            ).fetchone()
        finally:
            self._close(conn)
        if row is None or not row[0]:
            return None
        return str(row[0])

    def set_watermark(self, chat_id: str, value: str) -> None:
        """Store the watermark for one chat."""
        from pam_pm.rules.poller import POLLER_DDL

        conn = self._connect()
        try:
            conn.execute(POLLER_DDL)
            conn.execute(
                "INSERT INTO poller_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_watermark_key(chat_id), str(value)),
            )
        finally:
            self._close(conn)

    def _fetch_chat(
        self, chat: dict[str, Any], since: str | None
    ) -> list[dict[str, Any]]:
        chat_id = str(chat.get("id", "") or "").strip()
        if not chat_id:
            return []
        chat_type = str(chat.get("chatType", "") or "").strip().lower()
        if chat_type == "channel":
            team_id = str(chat.get("team_id", "") or chat.get("teamId", "") or "")
            channel_id = str(chat.get("channel_id", "") or chat_id)
            if team_id and "/" not in chat_id:
                return list(
                    self.graph.list_channel_messages(team_id, channel_id, since)
                )
            if "/" in chat_id:
                team_part, _, channel_part = chat_id.partition("/")
                return list(
                    self.graph.list_channel_messages(
                        team_part.strip(), channel_part.strip(), since
                    )
                )
            return []
        return list(self.graph.list_chat_messages(chat_id, since))

    def _chat_type_of(self, chat: dict[str, Any]) -> str:
        raw = str(chat.get("chatType", "") or "").strip()
        if raw:
            return raw.lower()
        chat_id = str(chat.get("id", "") or "").strip()
        if chat_id and "/" in chat_id or str(chat.get("team_id", "") or ""):
            return "channel"
        try:
            meta = self.graph.get_chat(chat_id)
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(meta, dict):
            return str(meta.get("chatType", "") or "").strip().lower()
        return ""

    def poll_chat(self, chat: dict[str, Any]) -> list[ChatMessage]:
        """Poll one watched chat, returning new inbound matches."""
        chat_id = str(chat.get("id", "") or "").strip()
        if not chat_id:
            return []
        since = self.get_watermark(chat_id)
        try:
            raw_messages = self._fetch_chat(chat, since)
        except Exception as exc:  # noqa: BLE001
            logger.error("teams poll failed for chat: %s", redact(exc))
            return []
        chat_type = self._chat_type_of(chat)
        fresh: list[ChatMessage] = []
        newest = since or ""
        for item in raw_messages:
            created = str(item.get("createdDateTime", "") or "")
            mid = str(item.get("id", "") or "")
            if mid and mid in self._seen_ids:
                continue
            if since and created and created <= since:
                if mid:
                    self._seen_ids.add(mid)
                continue
            if mid:
                self._seen_ids.add(mid)
            if created and created > newest:
                newest = created
            if not should_handle(item, chat_type, self.bot_id, self.bot_names):
                continue
            message = graph_message_to_chat_message(item, chat_id)
            fresh.append(message)
            if self.bus is not None:
                try:
                    publish_graph_message_to_bus(self.bus, message, chat_id, chat_type)
                except Exception:  # noqa: BLE001
                    logger.info("teams bus publish failed, keeping local result")
            if self.on_message is not None:
                try:
                    self.on_message(message)
                except Exception:  # noqa: BLE001
                    logger.info("teams on_message failed, keeping local result")
        if (newest and newest != (since or "")) or (since is None and raw_messages):
            self.set_watermark(chat_id, newest)
        return fresh

    def poll_once(self) -> list[ChatMessage]:
        """Poll every watched chat once, returning new inbound matches."""
        out: list[ChatMessage] = []
        for chat in self.chats:
            out.extend(self.poll_chat(chat))
        return out

    def poll(
        self, max_rounds: int | None = None, sleep: bool = True
    ) -> Iterator[list[ChatMessage]]:
        """Poll forever (or max_rounds) on the shared poll interval."""
        rounds = 0
        while max_rounds is None or rounds < max_rounds:
            yield self.poll_once()
            rounds += 1
            if max_rounds is not None and rounds >= max_rounds:
                break
            if self._interval_override is None:
                from pam_pm.rules.poller import effective_poll_interval

                self.interval = effective_poll_interval(self._db)
            if sleep and self.interval > 0:
                time.sleep(self.interval)


def handle_inbound_message(
    message: ChatMessage,
    *,
    chat: Any,
    board: Any = None,
    chat_id: str = "",
    chat_type: str = "",
    provider: str = "meta",
    event_sink: Any = None,
) -> dict[str, Any]:
    """Flow one polled Teams message through the router handle path.

    Mirrors how Slack events reach the router: text is logged under a
    Teams capability and file attachments route to the planner, while
    proposals and approvals follow the unchanged shared code.
    """
    from pam_pm.core import router as router_mod

    return router_mod.handle_teams_event(
        {
            "type": "teams_message",
            "text": message.text,
            "user_id": message.user_id,
            "channel": chat_id or message.user_id,
            "message_id": message.id,
            "chat_type": chat_type,
        },
        chat=chat,
        board=board,
        provider=provider,
        event_sink=event_sink,
    )
