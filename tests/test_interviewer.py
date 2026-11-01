"""Interviewer pushback, evidence, and turn cap with fake model."""

from __future__ import annotations

import json
from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.sqlite_board import SQLiteBoard
from pam_pm.agents.interviewer import Interviewer
from pam_pm.core.schemas import InterviewResult
from pam_pm.models.client import ChatResult, ChatToolCall


class FakeModelClient:
    """Scripted model returning questions then tool calls."""

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


def _unwrap(result: Any) -> InterviewResult:
    if isinstance(result, InterviewResult):
        return result
    if isinstance(result, dict):
        first = next(iter(result.values()))
        assert isinstance(first, InterviewResult)
        return first
    if isinstance(result, (list, tuple)):
        assert result
        assert isinstance(result[0], InterviewResult)
        return result[0]
    raise AssertionError(f"unexpected result type: {type(result)}")


def test_vague_gets_challenged_and_result_has_fields() -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = InMemoryChat()
        person = "U1"
        vague = "almost done, working on it, soon"
        specific = "2 hours left, done Friday, ETA 2026-11-06"
        chat.script_replies(person, [vague, specific])
        script = [
            ChatResult(text="What is the status of Fix login?", tool_calls=[]),
            ChatResult(text="Can you tell me more?", tool_calls=[]),
            ChatResult(
                text="",
                tool_calls=[
                    ChatToolCall(
                        name="finish_interview",
                        arguments=json.dumps(
                            {
                                "status": "In progress on Fix login",
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
        result = _unwrap(Interviewer().run(person, [item], chat, board, fake))
        assert result.remaining == 2.0
        assert result.eta is not None and "2026-11-06" in result.eta
        assert result.evidence_quote
        assert specific in result.evidence_quote or "2 hours" in result.evidence_quote
        assert len(chat.dms) <= 6
        assert len(chat.dms) >= 2
        challenged = [
            dm
            for dm in chat.dms
            if "remain" in dm["text"].lower() and "eta" in dm["text"].lower()
        ]
        assert challenged, f"vague answer was not challenged: {chat.dms}"
        for dm in chat.dms:
            assert dm["text"].count("?") == 1, f"one question expected: {dm}"
        assert fake.calls
        for call in fake.calls:
            messages = call["messages"]
            assert messages[0]["role"] == "system"
            assert "Live context" in str(messages[-1]["content"])
    finally:
        board.close()


def test_max_6_turns_enforced() -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = InMemoryChat()
        person = "U1"
        chat.script_replies(person, ["almost done, soon"] * 10)
        script = [ChatResult(text="Can you tell me more?", tool_calls=[])] * 10
        fake = FakeModelClient(script)
        agent = Interviewer()
        result = _unwrap(agent.run(person, [item], chat, board, fake))
        assert len(chat.dms) <= 6
        assert agent.last_turns <= 6
        assert result.evidence_quote
        assert "almost done" in result.evidence_quote.lower()
    finally:
        board.close()


def test_fallback_extracts_remaining_and_eta() -> None:
    board = SQLiteBoard()
    try:
        item = board.create_item("Fix login", {"state": "Doing"})
        chat = InMemoryChat()
        person = "U1"
        specific = "3 hours left, ETA 2026-11-07, finishing tests"
        chat.script_replies(person, [specific])
        script = [ChatResult(text="Can you tell me more?", tool_calls=[])] * 10
        fake = FakeModelClient(script)
        result = _unwrap(Interviewer().run(person, [item], chat, board, fake))
        assert result.remaining == 3.0
        assert result.eta is not None and "2026-11-07" in result.eta
        assert result.evidence_quote
        assert len(chat.dms) <= 6
        assert len(fake.calls) >= 3
    finally:
        board.close()
