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
from collections import deque
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

# Process-wide name -> user id cache so repeated polls do not hammer the API.
_USER_ID_CACHE: dict[str, str] = {}


def clear_user_id_cache() -> None:
    """Clear the process-wide Slack user id cache (tests)."""
    _USER_ID_CACHE.clear()


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
        self._dm_targets: set[str] = set()
        self._inbound: dict[str, deque[ChatMessage]] = {}
        self._poller: Any = None

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

    @staticmethod
    def _unwrap(resp: Any) -> Any:
        data = getattr(resp, "data", None)
        if isinstance(data, dict):
            return data
        return resp

    def _resolve_via_dm_membership(self, client: Any, key: str) -> str | None:
        """Resolve a lowercase name via DM membership plus users.info."""
        list_fn = getattr(client, "conversations_list", None)
        info_fn = getattr(client, "users_info", None)
        if not callable(list_fn) or not callable(info_fn):
            return None
        try:
            payload = self._unwrap(list_fn(types="im", limit=200))
        except TypeError:
            try:
                payload = self._unwrap(list_fn(types="im"))
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None
        channels = payload.get("channels", []) if isinstance(payload, dict) else []
        for entry in channels or []:
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get("user", "") or "").strip()
            if not uid:
                continue
            try:
                detail = self._unwrap(info_fn(user=uid))
            except Exception:  # noqa: BLE001,S112
                continue
            user = detail.get("user", {}) if isinstance(detail, dict) else {}
            if not isinstance(user, dict):
                continue
            profile = user.get("profile", {})
            if not isinstance(profile, dict):
                profile = {}
            candidates = (
                str(profile.get("display_name", "") or ""),
                str(user.get("real_name", "") or ""),
            )
            if any(c.strip().lower() == key for c in candidates if c.strip()):
                return uid
        return None

    def _resolve_user_id(self, client: Any, name: str) -> str | None:
        """Resolve a display name to a Slack user ID.

        Tries DM channel membership first (authoritative user ids per
        channel, matched via users.info), then users.list display or
        real name match. Results cache process-wide.
        """
        stripped = name.strip()
        if not stripped:
            return None
        if self._looks_like_user_id(stripped):
            return stripped
        key = stripped.lower()
        cached = self._user_cache.get(key) or _USER_ID_CACHE.get(key)
        if cached:
            self._user_cache[key] = cached
            return cached
        try:
            via_dm = self._resolve_via_dm_membership(client, key)
        except Exception:  # noqa: BLE001
            via_dm = None
        if via_dm:
            self._user_cache[key] = via_dm
            _USER_ID_CACHE[key] = via_dm
            return via_dm
        try:
            resp = self._unwrap(client.users_list())
        except Exception as exc:  # noqa: BLE001
            logger.warning("slack resolve '%s' failed via users.list: %s", name, exc)
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
                str(profile.get("real_name", "") or ""),
            )
            if any(c.strip().lower() == key for c in candidates if c.strip()):
                self._user_cache[key] = uid
                _USER_ID_CACHE[key] = uid
                return uid
        logger.warning("slack resolve '%s' failed: no match in users.list", name)
        return None

    def _resolve_dm_channel(self, client: Any, target: str) -> str | None:
        """Return a postable channel: IDs pass through, names resolve."""
        text = (target or "").strip()
        if not text:
            return None
        if self._looks_like_user_id(text):
            return text
        if text.startswith(("C", "G")) and len(text) >= 8 and text.isalnum():
            return text
        return self._resolve_user_id(client, text)

    def _skip_unresolved(self, op: str, target: str) -> str:
        logger.error(
            "slack %s skipped: could not resolve '%s' to a Slack user ID",
            op,
            target,
        )
        return ""

    def tracked_dm_targets(self) -> list[str]:
        """Return user ids from prior sends, for DM poll fallback."""
        return sorted(self._dm_targets)

    def _queue_inbound(self, message: ChatMessage) -> None:
        """Queue one polled inbound message for read_replies."""
        if isinstance(message, ChatMessage) and message.inbound:
            self._inbound.setdefault(message.user_id, deque()).append(message)

    def attach_poller(self, poller: Any) -> None:
        """Attach a Slack DM poller used by poll_once."""
        self._poller = poller

    def poll_once(self) -> list[ChatMessage]:
        """Poll DMs once via the attached poller, queueing inbound.

        Returns an empty list without an attached poller.
        """
        if self._poller is None:
            return []
        found = list(self._poller.poll_once())
        for message in found:
            self._queue_inbound(message)
        return [item for item in found if isinstance(item, ChatMessage)]

    def send_dm(self, user_id: str, text: str) -> str:
        client = self._client_or_raise()
        try:
            channel = self._resolve_dm_channel(client, user_id)
            if not channel:
                return self._skip_unresolved("send_dm", user_id)
            self._dm_targets.add(channel)
            resp = self._unwrap(client.chat_postMessage(channel=channel, text=text))
            return str(resp.get("ts", "") if isinstance(resp, dict) else "")
        except Exception as exc:
            raise self._fail("send_dm", exc) from exc

    def _drain_inbound(
        self, user_id: str, since: str | None = None
    ) -> list[ChatMessage]:
        """Drain queued polled replies for a user, optionally after ts."""
        queue = self._inbound.get(user_id or "")
        out: list[ChatMessage] = []
        leftovers: deque[ChatMessage] = deque()
        while queue:
            message = queue.popleft()
            if since and message.ts and message.ts <= since:
                continue
            if since and not message.ts:
                leftovers.append(message)
                continue
            out.append(message)
        if leftovers:
            queue.extendleft(reversed(leftovers))
        if out:
            for target in (user_id or "").strip(),:
                if target:
                    self._dm_targets.add(target)
        return out

    def read_replies(
        self, user_id: str, since: str | None = None
    ) -> list[ChatMessage]:
        queued = self._drain_inbound(user_id, since)
        if self._poller is not None:
            return queued
        client = self._client_or_raise()
        try:
            resolved = self._resolve_user_id(client, user_id)
            if not resolved:
                logger.error(
                    "slack read_replies skipped: could not resolve '%s' to a Slack user ID",
                    user_id,
                )
                return []
            opened = self._unwrap(client.conversations_open(users=resolved))
            channel_id = ""
            if isinstance(opened, dict):
                channel = opened.get("channel", {})
                if isinstance(channel, dict):
                    channel_id = str(channel.get("id", "") or "")
                elif isinstance(channel, str):
                    channel_id = channel
            if not channel_id:
                logger.error(
                    "slack read_replies skipped: could not open DM for '%s'",
                    user_id,
                )
                return []
            kwargs: dict[str, Any] = {"channel": channel_id, "limit": 50}
            if since:
                kwargs["oldest"] = since
            resp = self._unwrap(client.conversations_history(**kwargs))
            out: list[ChatMessage] = list(queued)
            msgs = resp.get("messages", []) if isinstance(resp, dict) else []
            for item in msgs or []:
                if not isinstance(item, dict):
                    continue
                if item.get("bot_id") and not item.get("user"):
                    continue
                ts = str(item.get("ts", ""))
                out.append(
                    ChatMessage(
                        id=ts,
                        user_id=resolved,
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
            self._dm_targets.add(channel)
            resp = self._unwrap(
                client.chat_postMessage(channel=channel, text=text, blocks=blocks)
            )
            return str(resp.get("ts", "") if isinstance(resp, dict) else "")
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
