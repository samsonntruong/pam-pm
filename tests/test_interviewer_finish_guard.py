"""Finish guard: no finish without a user turn, no placeholder evidence."""

from __future__ import annotations

import json
from typing import Any

import pytest

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import Interviewer
from pam_pm.core import recovery as recovery_mod
from pam_pm.models.client import ChatResult, ChatToolCall


class FakeModelClient:
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


class RecordingBoard:
    """Wrap a real board and record every update_item write."""

    def __init__(self, real: SQLiteBoard) -> None:
        self._real = real
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def create_item(self, *args: Any, **kwargs: Any) -> Any:
        return self._real.create_item(*args, **kwargs)

    def get_item(self, *args: Any, **kwargs: Any) -> Any:
        return self._real.get_item(*args, **kwargs)

    def update_item(self, item_id: str, fields: dict[str, Any]) -> Any:
        self.writes.append((str(item_id), dict(fields)))
        return self._real.update_item(item_id, fields)

    def add_comment(self, *args: Any, **kwargs: Any) -> Any:
        return self._real.add_comment(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def _finish_text(quote: str) -> ChatResult:
    return ChatResult(
        text="",
        tool_calls=[
            ChatToolCall(
                name="finish_interview",
                arguments=json.dumps(
                    {
                        "status": "In progress",
                        "remaining": 2.0,
                        "eta": "2026-11-06",
                        "evidence_quote": quote,
                        "blocker": None,
                        "blocker_owner": None,
                        "confidence": 0.8,
                    }
                ),
            )
        ],
    )


def test_immediate_finish_empty_conversation_rejected(tmp_path) -> None:
    real = SQLiteBoard()
    try:
        item = real.create_item("Fix login", {"state": "Doing"})
        board = RecordingBoard(real)
        chat = InMemoryChat()
        model = FakeModelClient(
            [
                ChatResult(
                    text="What is the status of Fix login?",
                    tool_calls=_finish_text("placeholder").tool_calls,
                )
            ]
        )
        db = str(tmp_path / "guard.db")
        out = Interviewer().run("U1", [item], chat, board, model, db=db)
        assert out == {}
        assert len(chat.dms) == 1
        assert board.writes == []
        stored = real.get_item(item.id)
        assert "placeholder" not in str(
            stored.fields.get("evidence_quote", "")
        ).lower()
        convs = recovery_mod.list_open_conversations(db)
        assert len(convs) == 1
        assert convs[0]["status"] == "open"
    finally:
        real.close()


@pytest.mark.parametrize(
    "bad_quote",
    ["placeholder", "lorem ipsum dolor", "TODO: fill evidence", ""],
)
def test_placeholder_evidence_never_reaches_board(tmp_path, bad_quote) -> None:
    real = SQLiteBoard()
    try:
        item = real.create_item("Fix login", {"state": "Doing"})
        board = RecordingBoard(real)
        chat = InMemoryChat()
        answer = "2 hours left, ETA 2026-11-06, finishing tests"
        chat.script_replies("U1", [answer])
        model = FakeModelClient(
            [
                ChatResult(
                    text="What is the status of Fix login?", tool_calls=[]
                ),
                _finish_text(bad_quote),
                ChatResult(
                    text="What remains and what is your ETA?", tool_calls=[]
                ),
            ]
        )
        db = str(tmp_path / "guard2.db")
        out = Interviewer().run("U1", [item], chat, board, model, db=db)
        for _, fields in board.writes:
            evidence = str(fields.get("evidence_quote", ""))
            lowered = evidence.lower()
            assert "placeholder" not in lowered
            assert "lorem" not in lowered
            assert "todo" not in lowered
        assert out == {}
        assert len(chat.dms) == 2
        assert board.writes == []
    finally:
        real.close()
