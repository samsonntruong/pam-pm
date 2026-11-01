"""End to end fake loop: interview, challenge, logged board write."""

from __future__ import annotations

import json
from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import Interviewer
from pam_pm.core import store
from pam_pm.core.schemas import InterviewResult
from pam_pm.core.writes import WriteService
from pam_pm.models.client import ChatResult, ChatToolCall


class FakeModelClient:
    """Scripted model returning questions then one tool call."""

    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) <= len(self._script):
            return self._script[len(self.calls) - 1]
        return self._script[-1]


def test_end_to_end_fake_challenges_vague_and_logs_write(tmp_path) -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix morning sync query", {"state": "Doing"})
        chat = InMemoryChat()
        person = "U123"
        vague = "Still working on it, almost done I think, soon."
        specific = "2 hours left, ETA 2026-11-06, finishing tests."
        chat.script_replies(person, [vague, specific])
        script = [
            ChatResult(
                text="What is the status of Fix morning sync query?",
                tool_calls=[],
            ),
            ChatResult(text="Can you tell me more?", tool_calls=[]),
            ChatResult(
                text="",
                tool_calls=[
                    ChatToolCall(
                        name="finish_interview",
                        arguments=json.dumps(
                            {
                                "status": "In progress",
                                "remaining": 2.0,
                                "eta": "2026-11-06",
                                "evidence_quote": specific,
                                "blocker": None,
                                "blocker_owner": None,
                                "confidence": 0.8,
                            }
                        ),
                    )
                ],
            ),
        ]
        fake = FakeModelClient(script)
        raw = Interviewer().run(person, [item], chat, board, fake)
        if isinstance(raw, dict):
            result = next(iter(raw.values()))
        else:
            result = raw
        assert isinstance(result, InterviewResult)
        assert result.remaining == 2.0
        assert result.eta is not None and "2026-11-06" in result.eta
        assert result.evidence_quote
        challenged = [
            dm
            for dm in chat.dms
            if "remain" in dm["text"].lower() and "eta" in dm["text"].lower()
        ]
        assert challenged, f"vague answer was not challenged: {chat.dms}"
        assert len(chat.dms) <= 6

        db_path = tmp_path / "state.db"
        writes = WriteService(db_path)
        write_id = writes.propose(
            {
                "operation": "update_state",
                "item_id": item.id,
                "evidence_quote": result.evidence_quote,
                "status": "proposed",
                "detail": "interview answer recorded from chat",
            }
        )
        assert write_id > 0
        rows = store.list_writes(db_path, status="proposed")
        assert len(rows) == 1
        assert rows[0].item_id == item.id
        assert rows[0].evidence_quote == specific
        assert "2 hours" in rows[0].evidence_quote

        updated = board.get_item(item.id)
        assert updated.fields.get("evidence_quote") == specific
    finally:
        board.close()
