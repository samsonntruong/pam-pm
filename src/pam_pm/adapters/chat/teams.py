"""Teams adapter: Bot Framework send plus Graph poll mode (Teams1 wired).

Outbound messages always go through the Bot Framework REST API.
Inbound has two modes. Send mode (default) receives Bot Framework
activities through handle_activity. Poll mode attaches a Graph
poller: poll_once drains watched 1:1 chats and channels into the
same inbound queues, so read_replies, proposals, and approvals work
unchanged with no public endpoint and no inbound ports.

Importing or constructing this adapter never touches the network.
Tests must not call its live methods; use httpx.MockTransport or
InMemoryChat instead.
"""

from __future__ import annotations

import logging
import os
import re
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from pam_pm.adapters.chat.base import ChatAdapter, ChatButton, ChatMessage

logger = logging.getLogger(__name__)

TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
TOKEN_SCOPE = "https://api.botframework.com/.default"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_CHATS_URL = "https://graph.microsoft.com/v1.0/chats"
DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/amer"

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: object, *secrets: str) -> str:
    """Redact Bearer [REDACTED] and Teams secret material from log text."""
    out = _BEARER_RE.sub("Bearer [REDACTED]", str(text))
    saved: list[str] = []
    try:
        from pam_pm.core import secrets as secrets_mod

        for name in (
            "TEAMS_APP_ID",
            "TEAMS_APP_PASSWORD",
            "TEAMS_TENANT_ID",
            "TEAMS_REFRESH_TOKEN",
        ):
            saved.append(secrets_mod.load_secret(name))
    except Exception:  # noqa: BLE001
        saved = []
    values = [
        *secrets,
        os.environ.get("TEAMS_APP_ID", ""),
        os.environ.get("TEAMS_APP_PASSWORD", ""),
        os.environ.get("TEAMS_TENANT_ID", ""),
        os.environ.get("TEAMS_REFRESH_TOKEN", ""),
        *saved,
    ]
    for val in values:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class TeamsError(RuntimeError):
    """Teams call failure with secrets already redacted."""


def activity_to_message(activity: dict[str, Any] | None) -> ChatMessage | None:
    """Convert a Bot Framework activity to an inbound ChatMessage.

    Returns None for non message activities (typing, endOfConversation).
    """
    if not isinstance(activity, dict):
        return None
    if str(activity.get("type", "")).lower() != "message":
        return None
    text = str(activity.get("text", "") or "")
    sender = activity.get("from") or {}
    user_id = str(sender.get("id", "") if isinstance(sender, dict) else "")
    convo = activity.get("conversation") or {}
    convo_id = str(convo.get("id", "") if isinstance(convo, dict) else "")
    activity_id = str(activity.get("id", "") or "")
    if not text and not user_id:
        return None
    return ChatMessage(
        id=activity_id or convo_id,
        user_id=user_id or convo_id,
        text=text,
        ts=activity_id,
        inbound=True,
    )


def publish_activity_to_bus(
    bus: Any, activity: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Publish an inbound activity onto the shared EventBus.

    Uses the same bus Slack Socket Mode messages flow through, so
    rules and the activity stream see one shape either way.
    """
    message = activity_to_message(activity)
    if message is None:
        return None
    event = {
        "source": "teams",
        "operation": "chat_message",
        "item_id": "",
        "user_id": message.user_id,
        "text": message.text,
        "evidence_quote": message.text[:200],
        "status": "received",
        "detail": f"Teams message from {message.user_id}.",
    }
    return bus.publish(event)


class TeamsAdapter(ChatAdapter):
    """Chat adapter backed by Bot Framework REST, built lazily."""

    def __init__(
        self,
        app_id: str | None = None,
        app_password: str | None = None,
        tenant_id: str | None = None,
        service_url: str | None = None,
        timeout: int = 30,
        transport: httpx.BaseTransport | None = None,
        data_dir: str | Path | None = None,
        mode: str = "send",
        chats: Sequence[dict[str, Any] | str] | None = None,
        roster: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> None:
        self._app_id = self._resolve_value("TEAMS_APP_ID", app_id, data_dir)
        self._app_password = self._resolve_value(
            "TEAMS_APP_PASSWORD", app_password, data_dir
        )
        self._tenant_id = self._resolve_value("TEAMS_TENANT_ID", tenant_id, data_dir)
        self._service_url = (service_url or "").strip() or DEFAULT_SERVICE_URL
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.Client | None = None
        self._token: str = ""
        self._graph_token: str = ""
        self._conversation_cache: dict[str, str] = {}
        self._inbound: dict[str, deque[ChatMessage]] = {}
        want = str(mode or "send").strip().lower()
        self._mode = "poll" if want == "poll" else "send"
        self._poller: Any | None = None
        self._chats: list[dict[str, Any]] = self._normalize_chats(chats)
        self._roster_map: dict[str, str] = self._normalize_roster(roster)

    @staticmethod
    def _resolve_value(
        name: str, explicit: str | None, data_dir: str | Path | None
    ) -> str:
        text = (explicit or "").strip()
        if text:
            return text
        env_value = os.environ.get(name, "").strip()
        if env_value:
            return env_value
        try:
            from pam_pm.core import secrets as secrets_mod

            return secrets_mod.load_secret(name, data_dir)
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _normalize_chats(
        chats: Sequence[dict[str, Any] | str] | None,
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        if not chats:
            return out
        for item in chats:
            if isinstance(item, str):
                cid = item.strip()
                if cid:
                    out.append({"id": cid})
            elif isinstance(item, dict):
                d = dict(item)
                cid = str(d.get("id", "") or "").strip()
                if not cid:
                    cid = str(d.get("chat_id", "") or "").strip()
                    if cid:
                        d["id"] = cid
                if cid:
                    out.append(d)
        return out

    @staticmethod
    def _normalize_roster(
        roster: Sequence[dict[str, Any]] | dict[str, Any] | None,
    ) -> dict[str, str]:
        mapping: dict[str, str] = {}
        if not roster:
            return mapping
        if isinstance(roster, dict):
            for key, val in roster.items():
                norm = str(key or "").strip().lower()
                if not norm:
                    continue
                if isinstance(val, str):
                    cid = val.strip()
                    if cid:
                        mapping[norm] = cid
                elif isinstance(val, dict):
                    cid = str(val.get("teams_chat_id", "") or "").strip()
                    if cid:
                        mapping[norm] = cid
            return mapping
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            norm = str(entry.get("name", "") or "").strip().lower()
            if not norm:
                continue
            cid = str(entry.get("teams_chat_id", "") or "").strip()
            if cid:
                mapping[norm] = cid
        return mapping

    def _roster_override(
        self,
        key: str,
        roster: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> str | None:
        if roster is not None:
            tmp = self._normalize_roster(roster)
            if key in tmp:
                return tmp[key]
        return self._roster_map.get(key)

    @property
    def known_chats(self) -> list[dict[str, Any]]:
        """Return a copy of the known watched chats."""
        return [dict(c) for c in self._chats]

    @property
    def app_id_configured(self) -> bool:
        """True when all three Teams credentials are available."""
        return bool(self._app_id and self._app_password and self._tenant_id)

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def poll_mode(self) -> bool:
        """True when inbound arrives through the attached Graph poller."""
        return self._mode == "poll"

    def attach_poller(self, poller: Any) -> None:
        """Attach a Graph poller used by poll_once in poll mode."""
        self._poller = poller
        self._mode = "poll"

    def poll_once(self) -> list[ChatMessage]:
        """Poll watched chats once, queueing inbound for read_replies.

        Send mode returns an empty list. Poll mode without an
        attached poller raises TeamsError.
        """
        if not self.poll_mode:
            return []
        if self._poller is None:
            raise TeamsError("poll mode needs an attached Graph poller.")
        found = list(self._poller.poll_once())
        for message in found:
            if isinstance(message, ChatMessage):
                self._inbound.setdefault(message.user_id, deque()).append(message)
        return [item for item in found if isinstance(item, ChatMessage)]

    def _client_or_raise(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        if not self._app_id or not self._app_password or not self._tenant_id:
            raise TeamsError(
                "TEAMS_APP_ID, TEAMS_APP_PASSWORD, and TEAMS_TENANT_ID "
                "are required; save them in Setup or set the env vars."
            )
        self._client = httpx.Client(timeout=self._timeout, transport=self._transport)
        return self._client

    def _fail(self, op: str, exc: Exception) -> TeamsError:
        detail = redact(exc, self._app_id, self._app_password, self._tenant_id)
        logger.error("teams %s failed: %s", op, detail)
        return TeamsError(f"{op} failed: {detail}")

    def _access_token(self) -> str:
        if self._token:
            return self._token
        client = self._client_or_raise()
        try:
            resp = client.post(
                TOKEN_URL_TEMPLATE.format(tenant=self._tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._app_id,
                    "client_secret": self._app_password,
                    "scope": TOKEN_SCOPE,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            token = str(payload.get("access_token", "") or "")
            if not token:
                raise TeamsError("token endpoint returned no access token.")
            self._token = token
            return token
        except TeamsError:
            raise
        except Exception as exc:
            raise self._fail("token", exc) from exc

    def _graph_access_token(self) -> str:
        if self._graph_token:
            return self._graph_token
        client = self._client_or_raise()
        try:
            resp = client.post(
                TOKEN_URL_TEMPLATE.format(tenant=self._tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._app_id,
                    "client_secret": self._app_password,
                    "scope": GRAPH_SCOPE,
                },
            )
            resp.raise_for_status()
            payload = resp.json()
            token = str(payload.get("access_token", "") or "")
            if not token:
                raise TeamsError("token endpoint returned no access token.")
            self._graph_token = token
            return token
        except TeamsError:
            raise
        except Exception as exc:
            raise self._fail("token", exc) from exc

    @staticmethod
    def _looks_like_conversation_id(value: str) -> bool:
        text = (value or "").strip()
        if not text:
            return False
        if ":" in text:
            return True
        return text.startswith(("19:", "a:"))

    def _conversation_id_for(self, user_id: str) -> str:
        text = (user_id or "").strip()
        if self._looks_like_conversation_id(text):
            return text
        return self.resolve_conversation(text)

    def resolve_conversation(
        self,
        person: str,
        roster: Sequence[dict[str, Any]] | dict[str, Any] | None = None,
    ) -> str:
        """Resolve a teammate display name to a 1:1 chat id via Graph.

        Iterates known watched chats and matches the other member
        displayName case-insensitively. Honors an explicit roster
        teams_chat_id override first. Results are cached per adapter.
        """
        name = (person or "").strip()
        if not name:
            raise TeamsError(
                "no 1:1 Teams chat found for ''; "
                "watch the 1:1 in Setup, then retry."
            )
        key = name.lower()
        override = self._roster_override(key, roster)
        if override:
            self._conversation_cache[key] = override
            return override
        cached = self._conversation_cache.get(key)
        if cached:
            return cached
        if not self._chats:
            raise TeamsError(
                f"no 1:1 Teams chat found for '{name}'; "
                "watch the 1:1 in Setup, then retry."
            )
        client = self._client_or_raise()
        token = self._graph_access_token()
        for chat in self._chats:
            chat_id = str(chat.get("id", "") or "").strip()
            if not chat_id:
                continue
            chat_type = str(
                chat.get("chatType", "") or chat.get("chat_type", "") or ""
            ).strip().lower()
            if chat_type == "channel":
                continue
            if chat_type in ("group",):
                continue
            if chat_type and chat_type != "oneonone":
                continue
            if "/" in chat_id:
                continue
            if str(chat.get("team_id", "") or chat.get("teamId", "") or "").strip():
                continue
            try:
                resp = client.get(
                    GRAPH_CHATS_URL + "/" + chat_id + "/members",
                    headers={"Authorization": "Bearer " + token},
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                raise self._fail("resolve", exc) from exc
            members = payload.get("value", []) if isinstance(payload, dict) else []
            if not isinstance(members, list):
                continue
            for member in members:
                if not isinstance(member, dict):
                    continue
                display = str(member.get("displayName", "") or "").strip()
                if display.lower() == key:
                    self._conversation_cache[key] = chat_id
                    return chat_id
        raise TeamsError(
            f"no 1:1 Teams chat found for '{name}'; "
            "watch the 1:1 in Setup, then retry."
        )

    def _post_activity(self, conversation_id: str, activity: dict[str, Any]) -> str:
        client = self._client_or_raise()
        token = self._access_token()
        url = (
            self._service_url.rstrip("/")
            + "/v3/conversations/"
            + conversation_id
            + "/activities"
        )
        try:
            resp = client.post(
                url,
                headers={"Authorization": "Bearer " + token},
                json=activity,
            )
            resp.raise_for_status()
            payload = resp.json()
            return str(payload.get("id", "") or "")
        except Exception as exc:
            raise self._fail("send", exc) from exc

    def send_dm(self, user_id: str, text: str) -> str:
        if not (user_id or "").strip():
            raise TeamsError("send_dm needs a conversation id.")
        conversation_id = self._conversation_id_for(user_id.strip())
        return self._post_activity(conversation_id, {"type": "message", "text": text})

    def read_replies(self, user_id: str, since: str | None = None) -> list[ChatMessage]:
        """Drain inbound messages queued by handle_activity for a user."""
        queue = self._inbound.get(user_id or "")
        out: list[ChatMessage] = []
        while queue:
            out.append(queue.popleft())
        return out

    def send_buttons(
        self,
        user_id: str,
        text: str,
        buttons: Sequence[ChatButton | dict[str, str]],
    ) -> str:
        if not (user_id or "").strip():
            raise TeamsError("send_buttons needs a conversation id.")
        actions: list[dict[str, str]] = []
        for item in buttons:
            if isinstance(item, ChatButton):
                bid, label, value = item.id, item.label, item.value or item.id
            else:
                bid = item.get("id", "")
                label = item.get("label", "")
                value = item.get("value", bid)
            actions.append({"type": "imBack", "title": label or bid, "value": value})
        conversation_id = self._conversation_id_for(user_id.strip())
        return self._post_activity(
            conversation_id,
            {
                "type": "message",
                "text": text,
                "suggestedActions": {"actions": actions},
            },
        )

    def download_file(self, file_id: str) -> bytes:
        client = self._client_or_raise()
        token = self._access_token()
        try:
            resp = client.get(
                file_id,
                headers={"Authorization": "Bearer " + token},
            )
            resp.raise_for_status()
            return resp.content
        except Exception as exc:
            raise self._fail("download_file", exc) from exc

    def handle_activity(self, activity: dict[str, Any] | None) -> ChatMessage | None:
        """Receive hook: queue one inbound activity for read_replies."""
        message = activity_to_message(activity)
        if message is None:
            return None
        self._inbound.setdefault(message.user_id, deque()).append(message)
        return message

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None
