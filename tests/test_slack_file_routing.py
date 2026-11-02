"""Slack file-attachment routing on fakes: poller keeps files, images plan, audio redirects."""

from __future__ import annotations

import logging
from typing import Any

from pam_pm.adapters.chat.base import ChatMessage
from pam_pm.adapters.chat.slack import SlackAdapter
from pam_pm.adapters.chat.slack_poll import (
    SlackPoller,
    handle_inbound_message,
    slack_message_to_chat_message,
)
from pam_pm.core import router as router_mod

PNG_FILE = {
    "id": "F111",
    "name": "board.png",
    "mimetype": "image/png",
    "url_private": "https://files.slack.test/F111/board.png",
}
WAV_FILE = {
    "id": "F222",
    "name": "standup.wav",
    "mimetype": "audio/wav",
    "url_private_download": "https://files.slack.test/F222/standup.wav",
}


class FakeWebClient:
    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = [dict(item) for item in messages]

    def conversations_list(self, types: str = "im", limit: int = 200) -> dict[str, Any]:
        return {"channels": [{"id": "D123", "user": "U123"}]}

    def conversations_history(
        self, channel: str, limit: int = 50, oldest: str | None = None
    ) -> dict[str, Any]:
        return {"messages": [dict(item) for item in self._messages]}

    def users_list(self) -> dict[str, Any]:
        return {"members": []}


class FakeBus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def publish(self, event: dict[str, Any]) -> dict[str, Any]:
        item = dict(event)
        self.events.append(item)
        return item


class FakeChat:
    """Fake chat adapter with scripted file bytes and sent-message log."""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files = dict(files or {})
        self.sent: list[tuple[str, str]] = []
        self.buttons: list[tuple[str, str]] = []

    def send_dm(self, user_id: str, text: str) -> str:
        self.sent.append((user_id, text))
        return f"msg-{len(self.sent)}"

    def send_buttons(self, user_id: str, text: str, buttons: Any = ()) -> str:
        self.buttons.append((user_id, text))
        return f"btn-{len(self.buttons)}"

    def download_file(self, file_id: str) -> bytes:
        return self._files.get(file_id, b"fake-image-bytes")

    def file_info(self, file_id: str) -> dict[str, str]:
        return {}

    def _queue_inbound(self, message: ChatMessage) -> None:
        return None


def _poller(tmp_path: Any, messages: list[dict[str, Any]]) -> tuple[SlackPoller, FakeBus]:
    adapter = SlackAdapter(bot_token="test-token")
    adapter._client = FakeWebClient(messages)
    bus = FakeBus()
    poller = SlackPoller(source=adapter, db=":memory:", bus=bus, interval=60)
    return poller, bus


def test_poller_preserves_files_into_message_and_bus(tmp_path: Any) -> None:
    poller, bus = _poller(
        tmp_path,
        [
            {
                "ts": "120.1",
                "user": "U123",
                "text": "sprint board",
                "files": [PNG_FILE],
            }
        ],
    )
    found = poller.poll_once()
    assert len(found) == 1
    assert found[0].files == [
        {
            "id": "F111",
            "name": "board.png",
            "mimetype": "image/png",
            "url": "https://files.slack.test/F111/board.png",
        }
    ]
    assert bus.events[0]["files"][0]["id"] == "F111"


def test_message_without_files_stays_text_only() -> None:
    message = slack_message_to_chat_message(
        {"ts": "120.9", "user": "U1", "text": "hello"}, "D123"
    )
    assert message.files == []
    assert message.text == "hello"


def test_image_message_reaches_planner_with_caption() -> None:
    chat = FakeChat({"F111": b"fake-png-bytes"})
    message = ChatMessage(
        id="120.1",
        user_id="U123",
        text="sprint board",
        ts="120.1",
        inbound=True,
        files=[
            {
                "id": "F111",
                "name": "board.png",
                "mimetype": "image/png",
                "url": "https://files.slack.test/F111",
            }
        ],
    )
    result = handle_inbound_message(
        message, chat=chat, channel_id="D123", event_sink=lambda event: None
    )
    assert result["kind"] == "image_plan"
    assert result["caption"] == "sprint board"
    assert chat.buttons, "planner proposal was not posted"
    assert "Context: sprint board" in chat.buttons[0][1]


def test_audio_message_gets_listen_redirect_not_shrug() -> None:
    chat = FakeChat({"F222": b"fake-wav-bytes"})
    message = ChatMessage(
        id="121.1",
        user_id="U123",
        text="",
        ts="121.1",
        inbound=True,
        files=[
            {
                "id": "F222",
                "name": "standup.wav",
                "mimetype": "audio/wav",
                "url": "https://files.slack.test/F222",
            }
        ],
    )
    result = handle_inbound_message(
        message, chat=chat, channel_id="D123", event_sink=lambda event: None
    )
    assert result["kind"] == "audio_declined"
    assert chat.sent, "audio redirect reply was not sent"
    assert chat.sent[0][1] == router_mod.SLACK_AUDIO_REPLY
    assert "Listen" in chat.sent[0][1]


def test_fallback_path_logged_when_sam_fails(caplog: Any) -> None:
    from pam_pm.models.vision import plan_from_photo

    def _boom(image: Any) -> Any:
        raise RuntimeError("SAM exploded")

    with caplog.at_level(logging.INFO, logger="pam_pm.models.vision"):
        photo = plan_from_photo(b"fake-bytes", segmenter=_boom)
    assert photo["path"] == "fallback"
    assert "fallback" in caplog.text


def test_resume_polled_path_carries_files(tmp_path: Any, monkeypatch: Any) -> None:
    from pam_pm.web import app as web_app

    web_app.reset_slack_events()
    web_app.stop_slack_polling()
    monkeypatch.setattr(web_app, "get_board_adapter", lambda project: None)
    monkeypatch.setattr(
        web_app, "_resume_model_client", lambda project: None
    )
    chat = FakeChat({"F111": b"fake-png-bytes"})
    poller = web_app.get_slack_poller(project="default", chat=chat, db=":memory:")
    assert poller is not None
    message = ChatMessage(
        id="122.1",
        user_id="D123",
        text="wall photo",
        ts="122.1",
        inbound=True,
        files=[
            {
                "id": "F111",
                "name": "board.png",
                "mimetype": "image/png",
                "url": "https://files.slack.test/F111",
            }
        ],
    )
    assert poller.on_message is not None
    poller.on_message(message)
    assert chat.buttons, "resume path did not route the image to the planner"
    assert "Context: wall photo" in chat.buttons[0][1]
    web_app.stop_slack_polling()
