"""Routing tests: one router, fake adapters, no network."""

from __future__ import annotations

import logging
from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.agents.lead import synthesize_review
from pam_pm.core import router as router_mod
from pam_pm.models.vision import VisionClient


class FakeTextClient:
    def __init__(self, model: str = "test-spark") -> None:
        self.model = model
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, tools=None, max_tokens=None, **kwargs):
        self.calls.append({"messages": messages, "capability": kwargs.get("capability")})
        return {"text": "# Live review\n\nDone."}


class CropReader:
    def __init__(self, readings: list[str]) -> None:
        self.readings = list(readings)
        self.full_calls = 0

    def chat(self, messages, **kwargs):
        return {"text": "\n".join(self.readings)}


def test_text_message_routes_to_meta_model(monkeypatch, caplog) -> None:
    monkeypatch.setenv("META_MODEL", "test-spark-text")
    with caplog.at_level(logging.INFO, logger="pam_pm.core.router"):
        route = router_mod.log_route(router_mod.CAP_SLACK_TEXT)
    assert route["capability"] == "slack_text"
    assert route["model"] == "test-spark-text"
    assert "capability=slack_text" in caplog.text
    assert "model=test-spark-text" in caplog.text


def test_image_in_dm_posts_proposal_with_approve(monkeypatch) -> None:
    monkeypatch.setenv("META_MODEL", "test-spark-text")
    monkeypatch.setenv("META_VISION_MODEL", "test-sam")
    chat = InMemoryChat()
    vision = VisionClient(CropReader(["Login form", "API keys"]))
    events: list[dict[str, Any]] = []
    result = router_mod.handle_slack_file(
        file_id="F1",
        channel="U1",
        chat=chat,
        mimetype="image/png",
        filename="board.png",
        file_bytes=b"fake-image",
        vision=vision,
        segmenter=lambda img: ["crop-a", "crop-b"],
        event_sink=events.append,
    )
    assert result["kind"] == "image_plan"
    assert result["path"] == "sam"
    assert len(result["proposal"]) >= 2
    buttons = chat.dms[-1]
    assert buttons["kind"] == "buttons"
    assert "Approve" in [b["label"] for b in buttons["buttons"]]
    assert events and events[0]["operation"] == "plan_from_photo"
    assert "sam" in events[0]["evidence_quote"]


def test_sam_failure_falls_back_to_whole_image(monkeypatch, caplog) -> None:
    monkeypatch.setenv("META_MODEL", "test-spark-text")
    chat = InMemoryChat()
    vision = VisionClient(CropReader(["Whole board read"]))

    def _boom(image):
        raise RuntimeError("SAM exploded")

    with caplog.at_level(logging.INFO):
        result = router_mod.handle_slack_file(
            file_id="F2",
            channel="U1",
            chat=chat,
            mimetype="image/jpeg",
            filename="notes.jpg",
            file_bytes=b"fake-image",
            vision=vision,
            segmenter=_boom,
        )
    assert result["path"] == "fallback"
    assert "SAM segment failed" in caplog.text
    assert "planner path=fallback" in caplog.text
    assert chat.dms[-1]["kind"] == "buttons"


def test_review_synthesis_routes_to_spark(monkeypatch, caplog) -> None:
    monkeypatch.setenv("META_MODEL", "test-spark-text")
    client = FakeTextClient("test-spark-text")
    items = [{"id": "1", "title": "Login", "state": "Done"}]
    interviews = {"1": {"status": "Done", "evidence_quote": "Shipped.", "confidence": 0.9}}
    with caplog.at_level(logging.INFO, logger="pam_pm.core.router"):
        result = synthesize_review(items, interviews, [], model_client=client)
    assert result["model_path"] == "live"
    assert result["max_tokens"] >= 6000
    assert client.calls
    assert "capability=review" in caplog.text
    assert "model=test-spark-text" in caplog.text


def test_slack_audio_file_gets_polite_reply_once() -> None:
    chat = InMemoryChat()
    result = router_mod.handle_slack_file(
        file_id="A1",
        channel="U9",
        chat=chat,
        mimetype="audio/mpeg",
        filename="standup.mp3",
        file_bytes=b"fake-audio",
    )
    assert result["kind"] == "audio_declined"
    assert result["model"] == ""
    assert len(chat.dms) == 1
    text = chat.dms[0]["text"]
    assert "cannot listen to audio in Slack yet" in text
    assert "Listen" in text
    assert "\u2014" not in text
