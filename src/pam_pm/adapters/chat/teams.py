"""Teams adapter over the Bot Framework REST API.

Realtime inbound messages arrive through the Bot Framework messaging
endpoint, which calls handle_activity (directly or through
publish_activity_to_bus onto the shared EventBus). This adapter only
wraps token plus conversation calls, so importing or constructing it
never touches the network. Tests must not call its live methods; use
httpx.MockTransport or InMemoryChat instead.
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
DEFAULT_SERVICE_URL = "https://smba.trafficmanager.net/amer"

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+")


def redact(text: object, *secrets: str) -> str:
    """Redact Bearer [REDACTED] and Teams secret material from log text."""
    out = _BEARER_RE.sub("Bearer [REDACTED]", str(text))
    saved: list[str] = []
    try:
        from pam_pm.core import secrets as secrets_mod

        for name in ("TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT_ID"):
            saved.append(secrets_mod.load_secret(name))
    except Exception:  # noqa: BLE001
        saved = []
    values = [
        *secrets,
        os.environ.get("TEAMS_APP_ID", ""),
        os.environ.get("TEAMS_APP_PASSWORD", ""),
        os.environ.get("TEAMS_TENANT_ID", ""),
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
        self._inbound: dict[str, deque[ChatMessage]] = {}

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

    @property
    def app_id_configured(self) -> bool:
        """True when all three Teams credentials are available."""
        return bool(self._app_id and self._app_password and self._tenant_id)

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

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
        return self._post_activity(user_id.strip(), {"type": "message", "text": text})

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
        return self._post_activity(
            user_id.strip(),
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
