"""Slack DM display-name resolution on fakes.

Live Slack rejects a teammate display name as a chat.postMessage
channel with channel_not_found. The adapter must resolve the name
to a user ID via users.list before posting, and skip with a clear
error when the name is unknown.
"""

from __future__ import annotations

import logging
from typing import Any

from pam_pm.adapters.chat.slack import SlackAdapter


class FakeSlackClient:
    """Fake WebClient: users_list plus validating chat_postMessage."""

    def __init__(self, members: list[dict[str, Any]]) -> None:
        self._members = members
        self.posts: list[dict[str, Any]] = []
        self.users_list_calls = 0

    def users_list(self) -> dict[str, Any]:
        self.users_list_calls += 1
        return {"members": self._members}

    def chat_postMessage(
        self, channel: str, text: str, **kwargs: Any
    ) -> dict[str, str]:
        # Live Slack only accepts IDs here; names fail channel_not_found.
        if " " in channel or not channel.startswith(("U", "W")):
            raise RuntimeError("channel_not_found")
        self.posts.append({"channel": channel, "text": text})
        return {"ts": f"ts-{len(self.posts)}"}


def _adapter(
    members: list[dict[str, Any]],
) -> tuple[SlackAdapter, FakeSlackClient]:
    adapter = SlackAdapter(bot_token="test-token")
    fake = FakeSlackClient(members)
    adapter._client = fake
    return adapter, fake


def _maya_members() -> list[dict[str, Any]]:
    return [
        {
            "id": "U123MAYA",
            "real_name": "Maya Pam",
            "profile": {"display_name": "Maya Pam"},
        }
    ]


def test_display_name_resolves_to_id_before_post() -> None:
    adapter, fake = _adapter(_maya_members())
    ts = adapter.send_dm("Maya Pam", "Morning check-in: status?")
    assert ts
    assert len(fake.posts) == 1
    assert fake.posts[0]["channel"] == "U123MAYA"


def test_real_name_match_and_cached_on_repeat_send() -> None:
    members = [
        {
            "id": "U456ANA",
            "real_name": "Ana Reyes",
            "profile": {"display_name": ""},
        }
    ]
    adapter, fake = _adapter(members)
    first = adapter.send_dm("Ana Reyes", "one")
    second = adapter.send_dm("Ana Reyes", "two")
    assert first and second
    assert [post["channel"] for post in fake.posts] == [
        "U456ANA",
        "U456ANA",
    ]
    assert fake.users_list_calls == 1


def test_unknown_name_logs_and_skips_post(caplog: Any) -> None:
    adapter, fake = _adapter(_maya_members())
    with caplog.at_level(logging.ERROR, logger="pam_pm.adapters.chat.slack"):
        ts = adapter.send_dm("Nobody Here", "hello?")
    assert ts == ""
    assert fake.posts == []
    assert any(
        "Nobody Here" in rec.message for rec in caplog.records
    )


def test_raw_user_id_posts_without_lookup() -> None:
    adapter, fake = _adapter(_maya_members())
    ts = adapter.send_dm("U123MAYA", "direct")
    assert ts
    assert fake.posts[0]["channel"] == "U123MAYA"
    assert fake.users_list_calls == 0
