"""Slack read_replies DM channel resolution on fakes.

Live Slack rejects display names and bare user IDs as a
conversations.history channel with channel_not_found. The adapter
must resolve the name to a user ID, open the DM for its channel
ID, then read history. Unknown names log and return [].
"""

from __future__ import annotations

import logging
from typing import Any

from pam_pm.adapters.chat.slack import SlackAdapter


class FakeHistoryClient:
    """Fake WebClient mirroring live channel validation."""

    def __init__(
        self, members: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> None:
        self._members = members
        self._messages = messages
        self.users_list_calls = 0
        self.open_calls: list[str] = []
        self.history_calls: list[str] = []

    def users_list(self) -> dict[str, Any]:
        self.users_list_calls += 1
        return {"members": self._members}

    def conversations_open(self, users: str) -> dict[str, Any]:
        if " " in users or not users.startswith(("U", "W")):
            raise RuntimeError("channel_not_found")
        self.open_calls.append(users)
        return {"channel": {"id": "D" + users[1:]}}

    def conversations_history(self, channel: str, **kwargs: Any) -> dict[str, Any]:
        # Live history rejects names and bare user IDs; only DM IDs pass.
        if " " in channel or channel.startswith(("U", "W")):
            raise RuntimeError("channel_not_found")
        self.history_calls.append(channel)
        return {"messages": self._messages}


def _maya_members() -> list[dict[str, Any]]:
    return [
        {
            "id": "U123MAYA",
            "real_name": "Maya Pam",
            "profile": {"display_name": "Maya Pam"},
        }
    ]


def _messages() -> list[dict[str, Any]]:
    return [
        {"ts": "1", "user": "U123MAYA", "text": "status: green"},
        {"ts": "2", "bot_id": "B1", "text": "bot noise"},
    ]


def _adapter(
    messages: list[dict[str, Any]] | None = None,
) -> tuple[SlackAdapter, FakeHistoryClient]:
    adapter = SlackAdapter(bot_token="test-token")
    fake = FakeHistoryClient(_maya_members(), messages or _messages())
    adapter._client = fake
    return adapter, fake


def test_display_name_resolves_opens_then_reads() -> None:
    adapter, fake = _adapter()
    out = adapter.read_replies("Maya Pam")
    assert [m.text for m in out] == ["status: green"]
    assert out[0].user_id == "U123MAYA"
    assert fake.open_calls == ["U123MAYA"]
    assert fake.history_calls == ["D123MAYA"]


def test_raw_user_id_skips_lookup_but_opens() -> None:
    adapter, fake = _adapter()
    out = adapter.read_replies("U123MAYA")
    assert len(out) == 1
    assert fake.users_list_calls == 0
    assert fake.open_calls == ["U123MAYA"]
    assert fake.history_calls == ["D123MAYA"]


def test_unknown_name_logs_and_returns_empty(caplog: Any) -> None:
    adapter, fake = _adapter()
    with caplog.at_level(logging.ERROR, logger="pam_pm.adapters.chat.slack"):
        out = adapter.read_replies("Nobody Here")
    assert out == []
    assert fake.open_calls == []
    assert fake.history_calls == []
    assert any("Nobody Here" in rec.message for rec in caplog.records)


class SlackResponseLike:
    """Mimics slack_sdk SlackResponse: payload in .data, no .get."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data


class FakeSdkHistoryClient(FakeHistoryClient):
    def users_list(self) -> Any:
        self.users_list_calls += 1
        return SlackResponseLike({"members": self._members})

    def conversations_open(self, users: str) -> Any:
        result = super().conversations_open(users)
        return SlackResponseLike(result)

    def conversations_history(self, channel: str, **kwargs: Any) -> Any:
        result = super().conversations_history(channel, **kwargs)
        return SlackResponseLike(result)


def test_sdk_response_objects_unwrap() -> None:
    adapter = SlackAdapter(bot_token="test-token")
    fake = FakeSdkHistoryClient(_maya_members(), _messages())
    adapter._client = fake
    out = adapter.read_replies("Maya Pam")
    assert [m.text for m in out] == ["status: green"]
    assert fake.history_calls == ["D123MAYA"]
