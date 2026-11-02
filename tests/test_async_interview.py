"""Async interview resume: open state, late replies, finish, fallback, dedupe."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from pam_pm.adapters.chat.base import ChatMessage
from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.chat.slack_poll import handle_inbound_message
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import Interviewer, resume_interview
from pam_pm.core import recovery as recovery_mod
from pam_pm.core import router as router_mod
from pam_pm.models.client import ChatResult, ChatToolCall
from pam_pm.web import app as web_app


class _ScriptedModel:
    """Scripted model returning one reply per call."""

    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        _ = max_tokens
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) <= len(self._script):
            return self._script[len(self.calls) - 1]
        return self._script[-1]


def _finish_call(status: str, remaining: float, eta: str, quote: str) -> ChatResult:
    return ChatResult(
        text="",
        tool_calls=[
            ChatToolCall(
                name="finish_interview",
                arguments=json.dumps(
                    {
                        "status": status,
                        "remaining": remaining,
                        "eta": eta,
                        "evidence_quote": quote,
                        "blocker": None,
                        "blocker_owner": None,
                        "confidence": 0.8,
                    }
                ),
            )
        ],
    )


def _seed_open(tmp_path: Any, person: str = "U1") -> tuple[Any, InMemoryChat, Any]:
    db = str(tmp_path / "async.db")
    board = SQLiteBoard()
    item = board.create_item("Fix login", {"state": "Doing"})
    chat = InMemoryChat()
    model = _ScriptedModel(
        [ChatResult(text="What is the status of Fix login?", tool_calls=[])]
    )
    out = Interviewer().run(person, [item], chat, board, model, db=db)
    assert out == {}
    return db, chat, board


def test_open_state_stored_on_send(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = InMemoryChat()
        db = str(tmp_path / "async.db")
        model = _ScriptedModel(
            [ChatResult(text="What is the status of Fix login?", tool_calls=[])]
        )
        out = Interviewer().run("U1", [item], chat, board, model, db=db)
        assert out == {}
        assert len(chat.dms) == 1
        convs = recovery_mod.list_open_conversations(db)
        assert len(convs) == 1
        state = convs[0]
        assert state["person"] == "U1"
        assert state["item_id"] == item.id
        assert state["status"] == "open"
        assert len(state["turns"]) == 1
        assert state["turns"][0]["role"] == "assistant"
        stored = board.get_item(item.id)
        assert not stored.fields.get("evidence_quote")
    finally:
        board.close()


def test_vague_reply_resumes_with_pushback(tmp_path) -> None:
    db, chat, board = _seed_open(tmp_path)
    try:
        first = chat.dms[0]["text"]
        vague = "almost done, working on it, soon"
        model = _ScriptedModel(
            [ChatResult(text="Can you tell me more?", tool_calls=[])]
        )
        outcome = resume_interview(
            "U1", vague, chat, board, model, db=db, message_id="111.222"
        )
        assert outcome["kind"] == "reply"
        assert outcome["replied"] is True
        assert len(model.calls) == 1
        seen = json.dumps(model.calls[0]["messages"])
        assert first in seen
        assert vague in seen
        reply = chat.dms[-1]["text"]
        assert reply != first
        assert "remain" in reply.lower() and "eta" in reply.lower()
        assert reply.count("?") == 1
        convs = recovery_mod.list_open_conversations(db)
        assert len(convs) == 1
        assert len(convs[0]["turns"]) == 3
        assert convs[0]["last_seen"] == "111.222"
    finally:
        board.close()


def test_specific_reply_finishes_with_board_write(tmp_path) -> None:
    db, chat, board = _seed_open(tmp_path)
    try:
        item_id = recovery_mod.list_open_conversations(db)[0]["item_id"]
        specific = "2 hours left, done Friday, ETA 2026-11-06"
        model = _ScriptedModel(
            [_finish_call("In progress on Fix login", 2.0, "2026-11-06", specific)]
        )
        outcome = resume_interview(
            "U1", specific, chat, board, model, db=db, message_id="333.444"
        )
        assert outcome["kind"] == "finished"
        stored = board.get_item(item_id)
        assert specific in str(stored.fields.get("evidence_quote", ""))
        assert recovery_mod.list_open_conversations(db) == []
        key = recovery_mod.thread_key("U1", item_id)
        closed = recovery_mod.load_conversation(db, key)
        assert closed is not None and closed["status"] == "finished"
        assert "recorded" in chat.dms[-1]["text"].lower()
        assert outcome["result"].remaining == 2.0
    finally:
        board.close()


def test_no_open_state_polite_fallback(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        chat = InMemoryChat()
        db = str(tmp_path / "async.db")
        model = _ScriptedModel([])
        outcome = resume_interview(
            "U9", "some late reply", chat, board, model, db=db
        )
        assert outcome["kind"] == "no_open_state"
        assert outcome["replied"] is True
        assert model.calls == []
        assert len(chat.dms) == 1
        assert "no open check-in" in chat.dms[0]["text"]
    finally:
        board.close()


def test_resume_dedupe_across_poller_and_webhook(tmp_path) -> None:
    db, chat, board = _seed_open(tmp_path)
    try:
        specific = "2 hours left, done Friday, ETA 2026-11-06"
        model = _ScriptedModel(
            [_finish_call("In progress on Fix login", 2.0, "2026-11-06", specific)]
        )
        first = handle_inbound_message(
            ChatMessage(
                id="222.333", user_id="U1", text=specific, ts="222.333",
                inbound=True,
            ),
            chat=chat,
            board=board,
            channel_id="D1",
            db=db,
            model_client=model,
        )
        assert first["kind"] == "interview_finished"
        assert first["replied"] is True
        envelope = {
            "type": "event_callback",
            "event_id": "Ev9",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "D1",
                "text": specific,
                "ts": "222.333",
            },
        }
        second = router_mod.handle_slack_event(
            envelope, chat=chat, board=board, db=db, model_client=model
        )
        assert second["kind"] == "interview_duplicate"
        assert second["replied"] is False
        assert len(model.calls) == 1
        assert len(chat.dms) == 2
    finally:
        board.close()


def test_webhook_duplicate_event_replies_once(tmp_path, monkeypatch) -> None:
    web_app.reset_slack_events()
    web_app.stop_slack_polling()
    web_app.reset_chat_state()
    web_app.reset_board_state()
    tmp_db = tmp_path / "hook.db"
    monkeypatch.setattr(web_app, "_db_path", lambda: tmp_db)
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = InMemoryChat()
        seed = _ScriptedModel(
            [ChatResult(text="What is the status of Fix login?", tool_calls=[])]
        )
        out = Interviewer().run("U1", [item], chat, board, seed, db=str(tmp_db))
        assert out == {}
        model = _ScriptedModel(
            [ChatResult(text="Can you tell me more?", tool_calls=[])]
        )
        web_app.set_chat_adapter("default", chat)
        web_app.set_board_adapter("default", board)
        web_app.set_model_client("default", model)
        client = TestClient(web_app.app)
        envelope = {
            "type": "event_callback",
            "event_id": "Ev1",
            "event": {
                "type": "message",
                "user": "U1",
                "channel": "D1",
                "text": "almost done, working on it, soon",
                "ts": "555.666",
            },
        }
        first = client.post("/api/slack/events", json=envelope)
        assert first.status_code == 200
        assert first.json()["duplicate"] is False
        assert first.json()["kind"] == "interview_reply"
        second = client.post("/api/slack/events", json=envelope)
        assert second.status_code == 200
        assert second.json()["duplicate"] is True
        assert len(chat.dms) == 2
        assert len(model.calls) == 1
    finally:
        board.close()
        web_app.reset_slack_events()
        web_app.stop_slack_polling()
        web_app.reset_chat_state()
        web_app.reset_board_state()
