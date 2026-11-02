"""Chat adapter interface: DMs, replies, buttons, file download."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass
class ChatMessage:
    """One chat message in either direction."""

    id: str
    user_id: str
    text: str
    ts: str = ""
    inbound: bool = False
    files: list[dict[str, str]] = field(default_factory=list)


@dataclass
class ChatButton:
    """One button option for send_buttons."""

    id: str
    label: str
    value: str = ""


class ChatAdapter(ABC):
    """Send and read direct messages, buttons, and file downloads."""

    @abstractmethod
    def send_dm(self, user_id: str, text: str) -> str:
        """Send a DM, returning the new message id."""

    @abstractmethod
    def read_replies(
        self, user_id: str, since: str | None = None
    ) -> list[ChatMessage]:
        """Return inbound replies from a user, optionally after a marker."""

    @abstractmethod
    def send_buttons(
        self,
        user_id: str,
        text: str,
        buttons: Sequence[ChatButton | dict[str, str]],
    ) -> str:
        """Send a DM with buttons, returning the new message id."""

    @abstractmethod
    def download_file(self, file_id: str) -> bytes:
        """Download a file by id, returning its raw bytes."""

    def file_info(self, file_id: str) -> dict[str, str]:
        """Return file metadata (mimetype, filename) for a file id.

        Optional: the default returns an empty dict. Slack implements
        it through files.info so file_shared events without inline
        metadata still route to the planner.
        """
        _ = file_id
        return {}
