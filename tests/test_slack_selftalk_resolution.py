"""Slack self-talk and user resolution regression tests on fakes."""

from __future__ import annotations

import logging
from typing import Any

from pam_pm.adapters.chat.slack import SlackAdapter, clear_user_id_cache
from pam_pm.adapters.chat.slack_poll import SlackPoller, should_handle_slack_message


class FakeClient:
    def __init__(self) -> None:
        self.bot_id = "UBOT999"
        self.channels = [{"id": "D1", "user": "U123MAYA"}]
        self.infos = {
            "U123MAYA": {
                "user": {
                    "id": "U123MAYA",
                    "real_name": "Maya Pam",
                    "profile": {"display_name": "Maya Pam"},
                }
            }
        }
        self.members = [
            {
                "id": "U999OTHER",
                "real_name": "Maya Pam",
                "profile": {"display_name": "Maya Pam"},
            }
        ]
        self.users_list_calls = 0
        self.mode = "ok"

    def auth_test(self) -> dict[str, str]:
        return {"user_id": self.bot_id}

    def conversations_list(self, types: str = "im", limit: int = 200) -> dict[str, Any]:
        return {"channels": list(self.channels)}

    def users_info(self, user: str) -> dict[str, Any]:
        return self.infos.get(user, {"user": {}})

    def users_list(self) -> dict[str, Any]:
        self.users_list_calls += 1
        if self.mode == "ratelimit":
            raise RuntimeError("ratelimited: RetryAfter 12")
        return {"members": self.members}

    def conversations_history(self, channel: str, limit: int = 50,
                               oldest: str | None = None) -> dict[str, Any]:
        return {"messages": []}


class FakeSource:
    def __init__(self, client: Any) -> None:
        self._client = client

    def _client_or_raise(self) -> Any:
        return self._client

    def tracked_dm_targets(self) -> list[str]:
        return []


def test_poller_learns_bot_id_and_ignores_own(tmp_path) -> None:
    clear_user_id_cache()
    client = FakeClient()
    poller = SlackPoller(source=FakeSource(client),
                         db=str(tmp_path / "bot.db"), interval=60)
    assert poller.bot_user_id == "UBOT999"
    assert should_handle_slack_message(
        {"user": "UBOT999", "text": "own fallback"}, poller.bot_user_id) is False
    assert should_handle_slack_message(
        {"user": "U123MAYA", "text": "hello"}, poller.bot_user_id) is True


def test_resolution_prefers_channel_membership() -> None:
    clear_user_id_cache()
    client = FakeClient()
    adapter = SlackAdapter(bot_token="test-token")
    adapter._client = client
    uid = adapter._resolve_user_id(client, "Maya Pam")
    assert uid == "U123MAYA"
    assert client.users_list_calls == 0


def test_ratelimit_logs_reason_and_skips(caplog: Any) -> None:
    clear_user_id_cache()
    client = FakeClient()
    client.channels = []
    client.mode = "ratelimit"
    adapter = SlackAdapter(bot_token="test-token")
    adapter._client = client
    with caplog.at_level(logging.WARNING,
                         logger="pam_pm.adapters.chat.slack"):
        uid = adapter._resolve_user_id(client, "Maya Pam")
    assert uid is None
    assert any("ratelimited" in r.message.lower() or "RetryAfter" in r.message
               for r in caplog.records)
