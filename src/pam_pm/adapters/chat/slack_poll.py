"""Slack DM polling for inbound messages with no inbound ports.

Pam sends over the Slack Web API but live inbound never arrives without
Socket Mode or Events API webhooks. This poller closes the gap with
outbound only calls on the shared poll interval (Settings poll value,
default 60 seconds): it lists open DMs, reads each channel history
since a per-channel watermark, and emits inbound user messages onto
the same event bus Slack webhook events use.

Watermark and dedupe discipline mirrors the ADO poller: each channel
stores its own watermark timestamp in SQLite, every poll fetches only
newer messages, and message ids already seen are never emitted twice,
even when timestamps tie. Bot messages and Pam's own sends are
skipped. An optional seen hook shares dedupe with the webhook route
so a message is never handled twice if both paths ever fire.

Importing or constructing anything here never touches the network.
Tests use a fake WebClient.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from pam_pm.adapters.chat.base import ChatMessage

logger = logging.getLogger(__name__)

WATERMARK_PREFIX = "slack-watermark:"

_CHANNEL_RE = re.compile(r"^[CDBG][A-Z0-9]{2,}$")
_USER_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")

_SKIP_SUBTYPES = frozenset(
    {
        "bot_message",
        "message_changed",
        "message_deleted",
        "channel_join",
        "channel_leave",
        "channel_topic",
    }
)


def _watermark_key(channel_id: str) -> str:
    return WATERMARK_PREFIX + str(channel_id or "").strip()


def _unwrap(resp: Any) -> Any:
    data = getattr(resp, "data", None)
    if isinstance(data, dict):
        return data
    return resp


def should_handle_slack_message(
    message: dict[str, Any], bot_user_id: str = ""
) -> bool:
    """Return True when a polled Slack message becomes inbound.

    Bot messages (bot_id with no user, bot subtypes) are skipped, as
    are Pam's own sends and system subtype noise. A message with no
    text and no files is skipped. Everything else from a user counts.
    """
    if not isinstance(message, dict):
        return False
    subtype = str(message.get("subtype", "") or "").strip()
    if subtype in _SKIP_SUBTYPES:
        return False
    if message.get("bot_id") and not message.get("user"):
        return False
    user = str(message.get("user", "") or "").strip()
    if bot_user_id and user and user == bot_user_id.strip():
        return False
    text = str(message.get("text", "") or "").strip()
    return bool(text or (message.get("files") or []))


def slack_message_to_chat_message(
    message: dict[str, Any], channel_id: str = ""
) -> ChatMessage:
    """Convert a Slack history message to an inbound ChatMessage."""
    ts = str(message.get("ts", "") or "")
    user = str(message.get("user", "") or "").strip()
    return ChatMessage(
        id=ts or str(message.get("client_msg_id", "") or ""),
        user_id=user or str(channel_id or ""),
        text=str(message.get("text", "") or ""),
        ts=ts,
        inbound=True,
    )


def slack_event_id(channel_id: str, message: ChatMessage) -> str:
    """Return the shared dedupe id for a polled Slack message."""
    marker = (message.ts or message.id or "").strip()
    return f"slack:{str(channel_id or '').strip()}:{marker}"


def publish_slack_message_to_bus(
    bus: Any, message: ChatMessage, channel_id: str = ""
) -> dict[str, Any]:
    """Publish a polled Slack DM onto the shared EventBus.

    Uses the same bus and the same slack_message trigger shape the
    webhook route flows through, so rules and the activity stream see
    one shape either way.
    """
    event_id = slack_event_id(channel_id, message)
    event = {
        "id": event_id,
        "event_id": event_id,
        "trigger": "slack_message",
        "type": "slack_message",
        "source": "slack",
        "operation": "chat_message",
        "item_id": "",
        "user_id": message.user_id,
        "text": message.text,
        "evidence_quote": message.text[:200],
        "status": "received",
        "detail": f"Slack DM from {message.user_id} in {channel_id or 'dm'}.",
        "channel_id": str(channel_id or ""),
        "message_id": message.id,
        "ts": message.ts,
    }
    return bus.publish(event)


def bot_id_from_client(client: Any) -> str:
    """Return the bot user ID via auth.test, or blank on failure."""
    if client is None or not callable(getattr(client, "auth_test", None)):
        return ""
    try:
        payload = _unwrap(client.auth_test())
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(payload, dict):
        return str(payload.get("user_id", "") or "").strip()
    return ""


def _raw_client_of(source: Any) -> Any | None:
    if source is None:
        return None
    if callable(getattr(source, "conversations_history", None)):
        return source
    accessor = getattr(source, "_client_or_raise", None)
    if callable(accessor):
        try:
            return accessor()
        except Exception:  # noqa: BLE001
            return None
    return getattr(source, "_client", None)


def _tracked_targets_of(source: Any) -> list[str]:
    fn = getattr(source, "tracked_dm_targets", None)
    if not callable(fn):
        return []
    try:
        items = fn()
    except Exception:  # noqa: BLE001
        return []
    return [str(item) for item in (items or []) if str(item or "").strip()]


def list_dm_channels(client: Any) -> list[dict[str, str]]:
    """Return open DM channels via conversations_list types im.

    Returns an empty list when the call fails so tracked channels
    from prior sends still poll.
    """
    if client is None or not callable(getattr(client, "conversations_list", None)):
        return []
    try:
        payload = _unwrap(client.conversations_list(types="im", limit=200))
    except TypeError:
        try:
            payload = _unwrap(client.conversations_list(types="im"))
        except Exception:  # noqa: BLE001
            logger.info("slack poll: conversations_list failed, using tracked DMs")
            return []
    except Exception:  # noqa: BLE001
        logger.info("slack poll: conversations_list failed, using tracked DMs")
        return []
    raw = payload.get("channels", []) if isinstance(payload, dict) else []
    out: list[dict[str, str]] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("id", "") or "").strip()
        if cid:
            out.append({"id": cid, "user": str(entry.get("user", "") or "")})
    return out


def _looks_like_channel(value: str) -> bool:
    return bool(_CHANNEL_RE.match(value.strip()))


def _looks_like_user(value: str) -> bool:
    return bool(_USER_RE.match(value.strip()))


class SlackPoller:
    """Poll Slack DMs with per-channel watermarks.

    Channels come from conversations_list types im plus tracked DM
    targets from prior sends plus any explicit list. Each channel
    stores its own watermark ts in SQLite. Every poll fetches only
    newer history; inbound user messages publish to the bus and are
    returned. Messages already seen by ts are never emitted twice.
    """

    def __init__(
        self,
        source: Any,
        db: str | Path | sqlite3.Connection,
        channels: list[str | dict[str, Any]] | None = None,
        interval: int | None = None,
        bus: Any | None = None,
        bot_user_id: str = "",
        on_message: Callable[[ChatMessage], None] | None = None,
        seen_hook: Callable[[str], bool] | None = None,
    ) -> None:
        self.source = source
        self._db = db
        self.channels = list(channels or [])
        self._interval_override = interval
        if interval is None:
            from pam_pm.rules.poller import effective_poll_interval

            self.interval = effective_poll_interval(db)
        else:
            self.interval = interval
        self.bus = bus
        self.bot_user_id = (bot_user_id or "").strip()
        if not self.bot_user_id:
            try:
                learned = bot_id_from_client(_raw_client_of(source))
            except Exception:  # noqa: BLE001
                learned = ""
            if learned:
                self.bot_user_id = learned
            else:
                logger.warning("slack poll: auth.test failed, bot user id unknown")
        self.on_message = on_message
        self.seen_hook = seen_hook
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

    def get_watermark(self, channel_id: str) -> str | None:
        """Return the stored ts watermark for one channel, or None."""
        from pam_pm.rules.poller import POLLER_DDL

        conn = self._connect()
        try:
            conn.execute(POLLER_DDL)
            row = conn.execute(
                "SELECT value FROM poller_state WHERE key = ?",
                (_watermark_key(channel_id),),
            ).fetchone()
        finally:
            self._close(conn)
        if row is None or not row[0]:
            return None
        return str(row[0])

    def set_watermark(self, channel_id: str, value: str) -> None:
        """Store the ts watermark for one channel."""
        from pam_pm.rules.poller import POLLER_DDL

        conn = self._connect()
        try:
            conn.execute(POLLER_DDL)
            conn.execute(
                "INSERT INTO poller_state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_watermark_key(channel_id), str(value)),
            )
        finally:
            self._close(conn)

    def _resolve_user_id(self, client: Any, name: str) -> str | None:
        helper = getattr(self.source, "_resolve_user_id", None)
        if callable(helper):
            try:
                return helper(client, name)
            except Exception:  # noqa: BLE001
                return None
        return name if _looks_like_user(name) else None

    def _resolve_history_channel(self, client: Any, target: str) -> str:
        text = (target or "").strip()
        if not text:
            return ""
        if _looks_like_channel(text):
            return text
        user_id = text if _looks_like_user(text) else ""
        if not user_id:
            user_id = self._resolve_user_id(client, text) or ""
        if not user_id:
            return ""
        try:
            opened = _unwrap(client.conversations_open(users=user_id))
        except Exception:  # noqa: BLE001
            return ""
        if isinstance(opened, dict):
            channel = opened.get("channel", {})
            if isinstance(channel, dict):
                return str(channel.get("id", "") or "")
            if isinstance(channel, str):
                return channel
        return ""

    def channels_to_poll(self) -> list[str]:
        """Return deduped history channel ids from all sources."""
        ordered: list[str] = []
        for entry in self.channels:
            if isinstance(entry, dict):
                ident = str(entry.get("id", "") or "").strip()
            else:
                ident = str(entry or "").strip()
            if ident and ident not in ordered:
                ordered.append(ident)
        for target in _tracked_targets_of(self.source):
            if target not in ordered:
                ordered.append(target)
        client = _raw_client_of(self.source)
        for found in list_dm_channels(client):
            if found["id"] not in ordered:
                ordered.append(found["id"])
        resolved: list[str] = []
        for target in ordered:
            if _looks_like_channel(target):
                resolved.append(target)
                continue
            if client is None:
                continue
            channel_id = self._resolve_history_channel(client, target)
            if channel_id and channel_id not in resolved:
                resolved.append(channel_id)
        return resolved

    def _fetch_history(
        self, client: Any, channel_id: str, since: str | None
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"channel": channel_id, "limit": 50}
        if (since or "").strip():
            kwargs["oldest"] = since.strip()
        try:
            payload = _unwrap(client.conversations_history(**kwargs))
        except Exception as exc:  # noqa: BLE001
            logger.info("slack poll: history failed for channel: %s", exc)
            return []
        raw = payload.get("messages", []) if isinstance(payload, dict) else []
        return [item for item in (raw or []) if isinstance(item, dict)]

    def poll_channel(self, channel_id: str) -> list[ChatMessage]:
        """Poll one DM channel, returning new inbound user messages."""
        cid = (channel_id or "").strip()
        if not cid:
            return []
        client = _raw_client_of(self.source)
        if client is None:
            return []
        since = self.get_watermark(cid)
        raw_messages = self._fetch_history(client, cid, since)
        fresh: list[ChatMessage] = []
        newest = since or ""
        for item in raw_messages:
            ts = str(item.get("ts", "") or "")
            key = ts or str(item.get("client_msg_id", "") or "")
            if key and key in self._seen_ids:
                continue
            if since and ts and ts <= since:
                if key:
                    self._seen_ids.add(key)
                continue
            if key:
                self._seen_ids.add(key)
            if ts and ts > newest:
                newest = ts
            if not should_handle_slack_message(item, self.bot_user_id):
                continue
            message = slack_message_to_chat_message(item, cid)
            if self.seen_hook is not None:
                try:
                    is_new = self.seen_hook(slack_event_id(cid, message))
                except Exception:  # noqa: BLE001
                    is_new = True
                if not is_new:
                    continue
            fresh.append(message)
            if self.bus is not None:
                try:
                    publish_slack_message_to_bus(self.bus, message, cid)
                except Exception:  # noqa: BLE001
                    logger.info("slack bus publish failed, keeping local result")
            if self.on_message is not None:
                try:
                    self.on_message(message)
                except Exception:  # noqa: BLE001
                    logger.info("slack on_message failed, keeping local result")
        if (newest and newest != (since or "")) or (since is None and raw_messages):
            self.set_watermark(cid, newest)
        return fresh

    def poll_once(self) -> list[ChatMessage]:
        """Poll every DM channel once, returning new inbound messages."""
        out: list[ChatMessage] = []
        for channel_id in self.channels_to_poll():
            out.extend(self.poll_channel(channel_id))
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
    channel_id: str = "",
    provider: str = "meta",
    event_sink: Any = None,
    db: Any = None,
    model_client: Any = None,
) -> dict[str, Any]:
    """Flow one polled Slack DM through the shared router path.

    Queues the message for read_replies so the interviewer and
    blocker intake receive it exactly like a webhook reply, then
    routes files through the planner path and text through
    interview resume.
    """
    queue = getattr(chat, "_queue_inbound", None)
    if callable(queue):
        try:
            queue(message)
        except Exception:  # noqa: BLE001
            logger.info("slack inbound queue failed, keeping local result")
    from pam_pm.core import router as router_mod

    return router_mod.handle_slack_event(
        {
            "type": "message",
            "channel": channel_id or message.user_id,
            "user": message.user_id,
            "text": message.text,
            "ts": message.ts,
        },
        chat=chat,
        board=board,
        provider=provider,
        event_sink=event_sink,
        db=db,
        model_client=model_client,
    )
