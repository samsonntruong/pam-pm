"""Stale-reply poisoning: only answers after the question count."""

from __future__ import annotations

import json
from typing import Any

from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import Interviewer, _next_answer
from pam_pm.core import recovery as recovery_mod
from pam_pm.models.client import ChatResult, ChatToolCall


class _TimestampedChat:
    """Fake chat with numeric timestamps on sends and replies."""

    def __init__(self, start: float = 1000.0) -> None:
        self.dms: list[dict[str, Any]] = []
        self._replies: dict[str, list[dict[str, Any]]] = {}
        self._clock = start

    def script_replies(self, user_id: str, replies: list[dict[str, Any]]) -> None:
        self._replies.setdefault(user_id, []).extend(replies)

    def send_dm(self, user_id: str, text: str) -> str:
        self._clock += 1.0
        self.dms.append({"user_id": user_id, "text": text, "ts": self._clock})
        return str(self._clock)

    def read_replies(
        self, user_id: str, since: str | None = None
    ) -> list[dict[str, Any]]:
        _ = since
        return self._replies.pop(user_id, [])


class _ScriptedModel:
    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        _ = (messages, tools, max_tokens)
        self.calls += 1
        if self.calls <= len(self._script):
            return self._script[self.calls - 1]
        return self._script[-1]


def _finish_result(text: str) -> ChatResult:
    return ChatResult(
        text="",
        tool_calls=[
            ChatToolCall(
                name="finish_interview",
                arguments=json.dumps(
                    {
                        "status": "In progress on Fix login",
                        "remaining": 2.0,
                        "eta": "2026-11-06",
                        "evidence_quote": text,
                        "blocker": None,
                        "blocker_owner": None,
                        "confidence": 0.8,
                    }
                ),
            )
        ],
    )


def test_stale_reply_ignored_stays_open(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        db = str(tmp_path / "stale.db")
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = _TimestampedChat()
        chat.script_replies(
            "U1",
            [{"ts": "500.0", "text": "stale from earlier round, 9 hours left"}],
        )
        model = _ScriptedModel(
            [ChatResult(text="What is the status of Fix login?", tool_calls=[])]
        )
        out = Interviewer().run("U1", [item], chat, board, model, db=db)
        assert out == {}
        assert len(chat.dms) == 1
        open_convs = recovery_mod.list_open_conversations(db)
        assert len(open_convs) == 1
        user_texts = [
            str(turn.get("content", ""))
            for turn in open_convs[0].get("turns", [])
            if turn.get("role") == "user"
        ]
        assert not any("stale from earlier round" in t for t in user_texts)
    finally:
        board.close()


def test_fresh_reply_after_question_consumed(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        db = str(tmp_path / "fresh.db")
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = _TimestampedChat()
        fresh = "2 hours left, ETA 2026-11-06, finishing tests"
        chat.script_replies("U1", [{"ts": "9999.0", "text": fresh}])
        model = _ScriptedModel(
            [
                ChatResult(text="What is the status of Fix login?", tool_calls=[]),
                _finish_result(fresh),
            ]
        )
        result = Interviewer().run("U1", [item], chat, board, model, db=db)
        assert result.remaining == 2.0
        assert "2026-11-06" in (result.eta or "")
        assert fresh in result.evidence_quote
    finally:
        board.close()


def test_buffer_stale_entry_skipped() -> None:
    chat = _TimestampedChat()
    assert _next_answer(chat, "U1", [(500.0, "stale buffered")], 1001.0) is None
    assert (
        _next_answer(chat, "U1", ["plain no-ts reply"], 1001.0) == "plain no-ts reply"
    )
