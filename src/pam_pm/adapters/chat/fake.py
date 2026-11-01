"""In-memory chat adapter for tests only."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from typing import Any

from pam_pm.adapters.chat.base import ChatAdapter, ChatButton, ChatMessage


class InMemoryChat(ChatAdapter):
    """Chat fake: stores messages, scripted replies, and DM records.

    Outbound DMs are appended to messages and recorded in dms for
    assertions. Inbound replies come only from script_replies queues.
    """

    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []
        self.dms: list[dict[str, Any]] = []
        self._replies: dict[str, deque[str]] = {}
        self._counter = 0

    def script_replies(
        self, user_id: str, replies: Sequence[str] | str
    ) -> None:
        """Queue scripted inbound replies returned later by read_replies."""
        queue = self._replies.setdefault(user_id, deque())
        if isinstance(replies, str):
            queue.append(replies)
        else:
            queue.extend(replies)

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def send_dm(self, user_id: str, text: str) -> str:
        mid = self._next_id("dm")
        self.messages.append(
            ChatMessage(id=mid, user_id=user_id, text=text, ts=mid)
        )
        self.dms.append(
            {"kind": "dm", "id": mid, "user_id": user_id, "text": text}
        )
        return mid

    def read_replies(
        self, user_id: str, since: str | None = None
    ) -> list[ChatMessage]:
        """Pop all scripted replies for a user as inbound messages."""
        queue = self._replies.get(user_id)
        out: list[ChatMessage] = []
        while queue:
            text = queue.popleft()
            mid = self._next_id("in")
            msg = ChatMessage(
                id=mid, user_id=user_id, text=text, ts=mid, inbound=True
            )
            self.messages.append(msg)
            out.append(msg)
        return out

    def send_buttons(
        self,
        user_id: str,
        text: str,
        buttons: Sequence[ChatButton | dict[str, str]],
    ) -> str:
        mid = self._next_id("btn")
        norm: list[dict[str, str]] = []
        for item in buttons:
            if isinstance(item, ChatButton):
                norm.append(
                    {"id": item.id, "label": item.label, "value": item.value}
                )
            else:
                norm.append(
                    {
                        "id": item.get("id", ""),
                        "label": item.get("label", ""),
                        "value": item.get("value", ""),
                    }
                )
        self.messages.append(
            ChatMessage(id=mid, user_id=user_id, text=text, ts=mid)
        )
        self.dms.append(
            {
                "kind": "buttons",
                "id": mid,
                "user_id": user_id,
                "text": text,
                "buttons": norm,
            }
        )
        return mid

    def download_file(self, file_id: str) -> bytes:
        return ("fake-file:" + file_id).encode("utf-8")


FakeChatAdapter = InMemoryChat
