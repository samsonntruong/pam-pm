"""Slack inbound DM polling on fakes: detect, watermark, bots, dedupe, interview."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.slack import SlackAdapter
from pam_pm.adapters.chat.slack_poll import SlackPoller
from pam_pm.web import app as web_app


class FakeWebClient:
    """Fake Slack WebClient with scripted DM channels and histories."""

    def __init__(
        self,
        channels: list[dict[str, str]],
        histories: dict[str, list[dict[str, Any]]],
    ) -> None:
        self._channels = [dict(entry) for entry in channels]
        self._histories = {
            key: [dict(item) for item in val] for key, val in histories.items()
        }
        self.history_calls: list[dict[str, Any]] = []

    def conversations_list(self, types: str = "im", limit: int = 200) -> dict[str, Any]:
        assert types == "im"
        assert limit > 0
        return {"channels": [dict(entry) for entry in self._channels]}

    def conversations_open(self, users: str) -> dict[str, Any]:
        return {"channel": {"id": "D" + str(users)[1:]}}

    def conversations_history(
        self, channel: str, limit: int = 50, oldest: str | None = None
    ) -> dict[str, Any]:
        self.history_calls.append({"channel": channel, "oldest": oldest})
        items = [dict(item) for item in self._histories.get(channel, [])]
        if (oldest or "").strip():
            items = [item for item in items if str(item.get("ts", "")) > str(oldest)]
        return {"messages": items[:limit]}

    def users_list(self) -> dict[str, Any]:
        return {"members": []}


class FakeBus:
    """Fake event bus recording published Slack DM events."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        item = dict(event)
        self.events.append(item)
        return item


@pytest.fixture(autouse=True)
def _clean_slack_state():
    web_app.reset_slack_events()
    web_app.stop_slack_polling()
    yield
    web_app.reset_slack_events()
    web_app.stop_slack_polling()


def _hook(store: set[str]):
    def _remember(event_id: str) -> bool:
        if event_id in store:
            return False
        store.add(event_id)
        return True

    return _remember


def _setup(tmp_path, messages, channel="D123", **kwargs):
    fake = FakeWebClient(
        [{"id": channel, "user": "U123MAYA"}], {channel: messages}
    )
    adapter = SlackAdapter(bot_token="test-token")
    adapter._client = fake
    bus = FakeBus()
    store: set[str] = set()
    poller = SlackPoller(
        source=adapter,
        db=str(tmp_path / "slack_poll.db"),
        bus=bus,
        bot_user_id=kwargs.get("bot_user_id", ""),
        seen_hook=kwargs.get("seen_hook") or _hook(store),
        interval=60,
    )
    adapter.attach_poller(poller)
    return poller, bus, adapter, fake, store


def test_new_dm_detected_and_published(tmp_path) -> None:
    poller, bus, _adapter, _fake, _store = _setup(
        tmp_path, [{"ts": "114.1", "user": "U123MAYA", "text": "status update"}]
    )
    found = poller.poll_once()
    assert len(found) == 1
    assert found[0].user_id == "U123MAYA"
    assert found[0].text == "status update"
    assert found[0].inbound is True
    assert len(bus.events) == 1
    assert bus.events[0]["trigger"] == "slack_message"
    assert bus.events[0]["text"] == "status update"
    assert poller.get_watermark("D123") == "114.1"


def test_watermark_advances_and_second_poll_is_empty(tmp_path) -> None:
    poller, _bus, _adapter, fake, _store = _setup(
        tmp_path, [{"ts": "114.2", "user": "U123MAYA", "text": "hello"}]
    )
    assert len(poller.poll_once()) == 1
    assert poller.poll_once() == []
    assert fake.history_calls[1]["oldest"] == "114.2"


def test_bot_messages_skipped(tmp_path) -> None:
    messages = [
        {"ts": "112.1", "bot_id": "B1", "text": "bot noise"},
        {"ts": "112.2", "user": "U1", "subtype": "bot_message", "text": "bot echo"},
        {"ts": "112.3", "user": "UBOT", "text": "own send"},
        {"ts": "112.4", "user": "U1", "text": "   "},
    ]
    poller, bus, _adapter, _fake, _store = _setup(
        tmp_path, messages, bot_user_id="UBOT"
    )
    assert poller.poll_once() == []
    assert bus.events == []
    assert poller.get_watermark("D123") == "112.4"


def test_dedupe_hook_skips_seen_message(tmp_path) -> None:
    store = {"slack:D123:113.1"}
    poller, bus, _adapter, _fake, _store = _setup(
        tmp_path,
        [{"ts": "113.1", "user": "U1", "text": "already handled"}],
        seen_hook=_hook(store),
    )
    assert poller.poll_once() == []
    assert bus.events == []
    assert poller.get_watermark("D123") == "113.1"


def test_interviewer_receives_reply_via_read_replies(tmp_path) -> None:
    _poller, _bus, adapter, _fake, _store = _setup(
        tmp_path, [{"ts": "116.1", "user": "U123MAYA", "text": "done, 2h left"}]
    )
    found = adapter.poll_once()
    assert [item.text for item in found] == ["done, 2h left"]
    replies = adapter.read_replies("U123MAYA")
    assert [item.text for item in replies] == ["done, 2h left"]
    assert adapter.read_replies("U123MAYA") == []


def test_webhook_and_poller_share_dedupe(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(web_app, "slack_configured", lambda: False)
    monkeypatch.setattr(web_app, "get_board_adapter", lambda project: None)
    envelope = {
        "event_id": "evt-dedupe-1",
        "event": {
            "type": "message",
            "channel": "D123",
            "user": "U1",
            "text": "hello",
            "ts": "115.1",
        },
    }
    with TestClient(web_app.app) as client:
        first = client.post("/api/slack/events?project=default", json=envelope)
        assert first.status_code == 200
        again = client.post("/api/slack/events?project=default", json=envelope)
        assert again.json().get("duplicate") is True
    poller, bus, _adapter, _fake, _store = _setup(
        tmp_path,
        [{"ts": "115.1", "user": "U1", "text": "hello"}],
        seen_hook=web_app._remember_slack_event,
    )
    assert poller.poll_once() == []
    assert bus.events == []
