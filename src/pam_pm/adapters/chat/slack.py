"""Slack adapter over the slack-sdk Web API.

Socket Mode note: realtime inbound messages arrive through a slack-bolt
App running under SocketModeHandler with SLACK_APP_TOKEN. This adapter
only wraps Web API calls (chat.postMessage, conversations.history,
files.info) so importing or constructing it never touches the network.
Tests must not call its methods; use InMemoryChat instead.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from pam_pm.adapters.chat.base import ChatAdapter, ChatButton, ChatMessage

logger = logging.getLogger(__name__)

_TOKEN_RES = (
    re.compile(r"xox[bap]-[A-Za-z0-9-]+"),
    re.compile(r"xapp-[A-Za-z0-9-]+"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+"),
)

_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,}$")


def redact(text: object, extra: str | None = None) -> str:
    """Redact Slack tokens and bearer values from log text."""
    out = str(text)
    for pattern in _TOKEN_RES:
        out = pattern.sub("[REDACTED]", out)
    saved_bot = ""
    saved_app = ""
    try:
        from pam_pm.core import secrets as secrets_mod

        saved_bot = secrets_mod.load_secret("SLACK_BOT_TOKEN")
        saved_app = secrets_mod.load_secret("SLACK_APP_TOKEN")
    except Exception:  # noqa: BLE001
        saved_bot = ""
        saved_app = ""
    secrets = [
        extra,
        os.environ.get("SLACK_BOT_TOKEN", ""),
        os.environ.get("SLACK_APP_TOKEN", ""),
        saved_bot,
        saved_app,
    ]
    for val in secrets:
        if val and len(val) >= 4 and val in out:
            out = out.replace(val, "[REDACTED]")
    return out


class SlackError(RuntimeError):
    """Slack call failure with tokens already redacted."""


class SlackAdapter(ChatAdapter):
    """Chat adapter backed by slack-sdk WebClient, built lazily."""

    def __init__(
        self,
        bot_token: str | None = None,
        app_token: str | None = None,
        timeout: int = 30,
        data_dir: str | Path | None = None,
    ) -> None:
        self._bot_token = self._resolve_token(
            "SLACK_BOT_TOKEN", bot_token, data_dir
        )
        self._app_token = self._resolve_token(
            "SLACK_APP_TOKEN", app_token, data_dir
        )
        self._timeout = timeout
        self._client: Any = None
        self._user_cache: dict[str, str] = {}

    @staticmethod
    def _resolve_token(name: str, explicit: str | None, data_dir: str | Path | None) -> str:
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
    def app_token_configured(self) -> bool:
        """True when an app token is available for Socket Mode."""
        return bool(self._app_token)

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._bot_token:
            raise SlackError("SLACK_BOT_TOKEN is missing; cannot call Slack.")
        try:
            from slack_sdk import WebClient
        except ImportError as exc:
            raise SlackError(f"slack-sdk is not installed: {exc}") from exc
        self._client = WebClient(token=self._bot_token, timeout=self._timeout)
        return self._client

    def _fail(self, op: str, exc: Exception) -> SlackError:
        detail = redact(exc, self._bot_token)
        logger.error("slack %s failed: %s", op, detail)
        return SlackError(f"{op} failed: {detail}")

    @staticmethod
    def _looks_like_user_id(value: str) -> bool:
        return bool(_USER_ID_RE.match(value.strip()))

    def _resolve_user_id(self, client: Any, name: str) -> str | None:
        """Resolve a display name to a Slack user ID via users.list."""
        key = name.strip().lower()
        if not key:
            return None
        cached = self._user_cache.get(key)
        if cached:
            return cached
        try:
            resp = client.users_list()
        except Exception:  # noqa: BLE001
            return None
        members = resp.get("members", []) if isinstance(resp, dict) else []
        for member in members or []:
            if not isinstance(member, dict):
                continue
            uid = str(member.get("id", "") or "")
            if not uid:
                continue
            profile = member.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}
            candidates = (
                str(profile.get("display_name", "") or ""),
                str(member.get("real_name", "") or ""),
            )
            if any(c.strip().lower() == key for c in candidates if c.strip()):
                self._user_cache[key] = uid
                return uid
        return None

    def _resolve_dm_channel(self, client: Any, target: str) -> str | None:
        """Return a postable channel: IDs pass through, names resolve."""
        text = (target or "").strip()
        if not text:
            return None
        if self._looks_like_user_id(text):
            return text
        return self._resolve_user_id(client, text)

    def _skip_unresolved(self, op: str, target: str) -> str:
        logger.error(
            "slack %s skipped: could not resolve '%s' to a Slack user ID",
            op,
            target,
        )
        return ""

    def send_dm(self, user_id: str, text: str) -> str:
        client = self._client_or_raise()
        try:
            channel = self._resolve_dm_channel(client, user_id)
            if not channel:
                return self._skip_unresolved("send_dm", user_id)
            resp = client.chat_postMessage(channel=channel, text=text)
            return str(resp.get("ts", ""))
        except Exception as exc:
            raise self._fail("send_dm", exc) from exc

    def read_replies(
        self, user_id: str, since: str | None = None
    ) -> list[ChatMessage]:
        client = self._client_or_raise()
        try:
            kwargs: dict[str, Any] = {"channel": user_id, "limit": 50}
            if since:
                kwargs["oldest"] = since
            resp = client.conversations_history(**kwargs)
            out: list[ChatMessage] = []
            for item in resp.get("messages", []) or []:
                if not isinstance(item, dict):
                    continue
                if item.get("bot_id") and not item.get("user"):
                    continue
                ts = str(item.get("ts", ""))
                out.append(
                    ChatMessage(
                        id=ts,
                        user_id=user_id,
                        text=str(item.get("text", "")),
                        ts=ts,
                        inbound=True,
                    )
                )
            return out
        except Exception as exc:
            raise self._fail("read_replies", exc) from exc

    def send_buttons(
        self,
        user_id: str,
        text: str,
        buttons: Sequence[ChatButton | dict[str, str]],
    ) -> str:
        client = self._client_or_raise()
        try:
            elements: list[dict[str, Any]] = []
            for item in buttons:
                if isinstance(item, ChatButton):
                    bid, label, value = item.id, item.label, item.value or item.id
                else:
                    bid = item.get("id", "")
                    label = item.get("label", "")
                    value = item.get("value", bid)
                elements.append(
                    {
                        "type": "button",
                        "action_id": bid,
                        "text": {"type": "plain_text", "text": label or bid},
                        "value": value,
                    }
                )
            blocks: list[dict[str, Any]] = [
                {"type": "section", "text": {"type": "mrkdwn", "text": text}},
                {"type": "actions", "elements": elements},
            ]
            channel = self._resolve_dm_channel(client, user_id)
            if not channel:
                return self._skip_unresolved("send_buttons", user_id)
            resp = client.chat_postMessage(
                channel=channel, text=text, blocks=blocks
            )
            return str(resp.get("ts", ""))
        except Exception as exc:
            raise self._fail("send_buttons", exc) from exc

    def file_info(self, file_id: str) -> dict[str, str]:
        client = self._client_or_raise()
        try:
            info = client.files_info(file=file_id).get("file", {})
            if not isinstance(info, dict):
                return {}
            name = str(info.get("name", "") or info.get("title", "") or "")
            return {
                "mimetype": str(info.get("mimetype", "") or ""),
                "filename": name,
            }
        except Exception as exc:
            raise self._fail("file_info", exc) from exc

    def download_file(self, file_id: str) -> bytes:
        client = self._client_or_raise()
        try:
            info = client.files_info(file=file_id).get("file", {})
            url = info.get("url_private_download") or info.get("url_private", "")
            if not url:
                raise SlackError(f"file has no download url: {file_id}")
            resp = httpx.get(
                url,
                headers={"Authorization": "Bearer " + self._bot_token},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            return resp.content
        except SlackError:
            raise
        except Exception as exc:
            raise self._fail("download_file", exc) from exc
