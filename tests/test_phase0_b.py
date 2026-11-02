"""Phase 0 batch B: slack events endpoint, per-note masks, review post."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.agents import lead as lead_mod
from pam_pm.agents.lead import create_retro_items, synthesize_review
from pam_pm.core import auth as auth_mod
from pam_pm.models.vision import (
    VisionClient,
    mask_label,
    pair_masks_with_readings,
    plan_from_photo,
)
from pam_pm.web import app as app_mod
from tests.test_both_adapters import _make_board

PHOTO_FIXTURE = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
    b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB\x60\x82"
)


class _FakeVision(VisionClient):
    def __init__(self, text: str = "Login form\nAPI keys") -> None:
        super().__init__()
        self.text = text

    def read_full(self, image: Any) -> str:
        _ = image
        return self.text


@pytest.fixture(params=["native", "ado-agile"], ids=["native", "ado-agile"])
def board_pair(request: Any) -> Any:
    board, _, _ = _make_board(str(request.param))
    try:
        yield (str(request.param), board)
    finally:
        board.close()


def _client(monkeypatch: Any, tmp_path: Any) -> TestClient:
    monkeypatch.setenv("PAM_HOME", str(tmp_path))
    for key in ("PAM_ALLOWED_HOST", "PAM_CONSOLE_PASSWORD", "PAM_SHARE_ON_LAN"):
        monkeypatch.delenv(key, raising=False)
    auth_mod.reset_sessions()
    auth_mod.reset_login_attempts()
    app_mod.reset_board_state()
    app_mod.reset_chat_state()
    app_mod.reset_slack_events()
    return TestClient(app_mod.app)


def _teardown_client() -> None:
    app_mod.reset_board_state()
    app_mod.reset_chat_state()
    app_mod.reset_slack_events()


def _review_inputs() -> tuple[
    list[dict[str, str]], dict[str, dict[str, Any]], list[dict[str, str]]
]:
    items = [
        {"id": "101", "title": "Login form", "state": "To Do"},
        {"id": "102", "title": "API keys", "state": "Doing"},
    ]
    interviews = {
        "101": {
            "status": "To Do",
            "evidence_quote": "Login form is done, only tests remain.",
            "confidence": 0.9,
        },
        "102": {
            "status": "Doing",
            "evidence_quote": "Waiting on API keys from Sam.",
            "confidence": 0.2,
        },
    }
    events = [
        {"operation": "comment", "item_id": "102", "detail": "Note blocker on 102."}
    ]
    return items, interviews, events


def test_slack_url_verification_returns_challenge(
    tmp_path: Any, monkeypatch: Any
) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        resp = client.post(
            "/api/slack/events",
            json={"type": "url_verification", "challenge": "ch-1"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"challenge": "ch-1"}
    finally:
        _teardown_client()


def test_slack_file_shared_posts_plan_once(
    board_pair: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    _, board = board_pair
    client = _client(monkeypatch, tmp_path)
    try:
        project = "b-events"
        app_mod.set_board_adapter(project, board)
        chat = InMemoryChat()
        chat.register_file(
            "F1", mimetype="image/png", filename="board.png", data=PHOTO_FIXTURE
        )
        app_mod.set_chat_adapter(project, chat)
        envelope = {
            "event_id": "b-ev-1",
            "event": {
                "type": "file_shared",
                "file_id": "F1",
                "channel_id": "C1",
            },
        }
        resp = client.post(f"/api/slack/events?project={project}", json=envelope)
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["kind"] == "image_plan"
        assert payload["duplicate"] is False
        assert len(chat.dms) == 1
        assert chat.dms[0]["user_id"] == "C1"
        labels = [b["label"] for b in chat.dms[0]["buttons"]]
        assert "Approve" in labels
        again = client.post(f"/api/slack/events?project={project}", json=envelope)
        assert again.json() == {"ok": True, "duplicate": True}
        assert len(chat.dms) == 1
    finally:
        _teardown_client()


def test_slack_message_with_image_routes_to_planner(
    board_pair: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    _, board = board_pair
    client = _client(monkeypatch, tmp_path)
    try:
        project = "b-events-msg"
        app_mod.set_board_adapter(project, board)
        chat = InMemoryChat()
        chat.register_file(
            "F1", mimetype="image/png", filename="board.png", data=PHOTO_FIXTURE
        )
        app_mod.set_chat_adapter(project, chat)
        envelope = {
            "event_id": "b-ev-2",
            "event": {
                "type": "message",
                "channel": "C2",
                "files": [
                    {"id": "F1", "mimetype": "image/png", "name": "board.png"}
                ],
            },
        }
        resp = client.post(f"/api/slack/events?project={project}", json=envelope)
        assert resp.status_code == 200
        assert resp.json()["kind"] == "image_plan"
        assert len(chat.dms) == 1
        assert chat.dms[0]["user_id"] == "C2"
    finally:
        _teardown_client()


def test_slack_unknown_event_ignored(
    board_pair: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    _, board = board_pair
    client = _client(monkeypatch, tmp_path)
    try:
        project = "b-events-unknown"
        app_mod.set_board_adapter(project, board)
        chat = InMemoryChat()
        app_mod.set_chat_adapter(project, chat)
        resp = client.post(
            f"/api/slack/events?project={project}",
            json={"event_id": "b-ev-3", "event": {"type": "reaction_added"}},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["kind"] == "ignored"
        assert payload["replied"] is False
        assert chat.dms == []
    finally:
        _teardown_client()


def test_slack_invalid_envelope_rejected(
    tmp_path: Any, monkeypatch: Any
) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        resp = client.post("/api/slack/events", json=[1, 2])
        assert resp.status_code == 400
    finally:
        _teardown_client()


def test_mask_label_forms() -> None:
    assert mask_label(0, 0) == "Mask 1"
    assert mask_label(7, 0) == "Mask 1 (id 7)"
    assert mask_label({"bbox": [1, 2, 3, 4]}, 0) == "Mask 1 (segmented)"
    assert mask_label({"id": "m2"}, 1) == "Mask 2 (m2)"
    assert mask_label(None, 0) == "Placeholder mask 1"


def test_pairs_sam_masks_with_readings_in_order() -> None:
    photo = {
        "readings": ["Fix login", "Write docs"],
        "masks": [0, 1],
        "path": "sam",
    }
    pairs = pair_masks_with_readings(photo)
    assert [entry["mask_label"] for entry in pairs] == ["Mask 1", "Mask 2"]
    assert [entry["reading"] for entry in pairs] == ["Fix login", "Write docs"]


def test_pairs_fallback_readings_with_placeholders() -> None:
    photo = {
        "readings": ["Login form", "API keys"],
        "masks": [],
        "path": "fallback",
    }
    pairs = pair_masks_with_readings(photo)
    assert [entry["mask_label"] for entry in pairs] == [
        "Placeholder mask 1",
        "Placeholder mask 2",
    ]
    assert [entry["reading"] for entry in pairs] == ["Login form", "API keys"]


def test_photo_to_pairs_both_paths() -> None:
    vision = _FakeVision()
    vision.read_crops = lambda crops: ["Fix login", "Write docs"]  # type: ignore[method-assign]

    def _segmenter(image: Any) -> list[str]:
        _ = image
        return ["crop-a", "crop-b"]

    sam_photo = plan_from_photo(PHOTO_FIXTURE, vision=vision, segmenter=_segmenter)
    assert sam_photo["path"] == "sam"
    pairs = pair_masks_with_readings(sam_photo)
    assert len(pairs) == 2
    assert pairs[0]["mask_label"] == "Mask 1"
    assert pairs[0]["reading"] == "Fix login"

    def _broken(image: Any) -> list[str]:
        raise RuntimeError("SAM 3.1 not installed")

    fallback = plan_from_photo(
        PHOTO_FIXTURE, vision=_FakeVision(), segmenter=_broken
    )
    assert fallback["path"] == "fallback"
    fb_pairs = pair_masks_with_readings(fallback)
    assert [entry["mask_label"] for entry in fb_pairs] == [
        "Placeholder mask 1",
        "Placeholder mask 2",
    ]


def test_plan_html_pairs_each_mask_beside_reading() -> None:
    sam_html = app_mod._plan_result_html(
        {"readings": ["Fix login", "Write docs"], "masks": [0, 1], "path": "sam"},
        [],
    )
    assert sam_html.count('class="note-pair"') == 2
    first_mask = sam_html.index("Mask 1")
    first_reading = sam_html.index("Fix login")
    second_mask = sam_html.index("Mask 2")
    assert first_mask < first_reading < second_mask
    fallback_html = app_mod._plan_result_html(
        {"readings": ["Solo"], "masks": [], "path": "fallback"}, []
    )
    assert "Placeholder mask 1" in fallback_html
    assert "Solo" in fallback_html
    assert fallback_html.index("Placeholder mask 1") < fallback_html.index("Solo")


def test_review_target_prefers_channel() -> None:
    assert lead_mod.review_target(channel="C9", lead="U1") == "C9"
    assert lead_mod.review_target(channel="  ", lead="U1") == "U1"
    assert lead_mod.review_target() == ""


def test_post_review_to_channel_and_lead_dm() -> None:
    items, interviews, events = _review_inputs()
    review = synthesize_review(items, interviews, events)
    assert review["retro_actions"]
    chat = InMemoryChat()
    assert lead_mod.post_review(chat, review, channel="C9", lead="U1") is True
    assert chat.dms[0]["user_id"] == "C9"
    assert "Sprint review" in chat.dms[0]["text"]
    assert "102" in chat.dms[0]["text"]
    dm_chat = InMemoryChat()
    assert lead_mod.post_review(dm_chat, review, lead="U1") is True
    assert dm_chat.dms[0]["user_id"] == "U1"


def test_post_review_needs_chat_and_target() -> None:
    items, interviews, events = _review_inputs()
    review = synthesize_review(items, interviews, events)
    assert lead_mod.post_review(None, review, channel="C9") is False
    assert lead_mod.post_review(InMemoryChat(), review) is False

    class _Boom:
        def send_dm(self, user_id: str, text: str) -> str:
            raise RuntimeError("slack down")

    assert lead_mod.post_review(_Boom(), review, channel="C9") is False


def test_review_flow_posts_after_retro_items(board_pair: Any) -> None:
    _, board = board_pair
    items, interviews, events = _review_inputs()
    review = synthesize_review(items, interviews, events)
    created = create_retro_items(board, review["retro_actions"])
    assert len(created) == len(review["retro_actions"])
    chat = InMemoryChat()
    assert lead_mod.post_review(chat, review, channel="C9") is True
    first_title = str(review["retro_actions"][0]["title"])[:40]
    assert first_title in chat.dms[0]["text"]


def test_review_run_route_posts_to_channel(
    tmp_path: Any, monkeypatch: Any
) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        project = "b-review"
        chat = InMemoryChat()
        app_mod.set_chat_adapter(project, chat)
        monkeypatch.setenv("SLACK_REVIEW_CHANNEL_ID", "C9")
        resp = client.post(f"/api/review/run?project={project}")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["posted"] is True
        assert chat.dms and chat.dms[0]["user_id"] == "C9"
    finally:
        _teardown_client()


def test_review_run_route_falls_back_to_lead_dm(
    tmp_path: Any, monkeypatch: Any
) -> None:
    client = _client(monkeypatch, tmp_path)
    try:
        project = "b-review-lead"
        chat = InMemoryChat()
        app_mod.set_chat_adapter(project, chat)
        monkeypatch.delenv("SLACK_REVIEW_CHANNEL_ID", raising=False)
        monkeypatch.setenv("PAM_PEOPLE", "Ulead")
        resp = client.post(f"/api/review/run?project={project}")
        assert resp.status_code == 200
        assert resp.json()["posted"] is True
        assert chat.dms[0]["user_id"] == "Ulead"
    finally:
        _teardown_client()
