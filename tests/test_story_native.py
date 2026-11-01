"""WP2b full story on the native board with fake chat and fake model.

One integration test: plan from notes, check-in with pushback, blocker
with dependency link, blocker clears with notification, review and retro
with retro actions. Each step asserts board state, evidence quotes, and
Undo availability. No network, no secrets.
"""

from __future__ import annotations

import json
from typing import Any

from pam_pm.adapters.chat.fake import InMemoryChat
from pam_pm.adapters.pm.base import is_closed
from pam_pm.adapters.pm.sqlite_board import NativeBoard
from pam_pm.agents.lead import create_retro_items, synthesize_review
from pam_pm.agents.planner import approve_proposal, propose_sprint
from pam_pm.core import store
from pam_pm.core.writes import WriteService
from pam_pm.models.client import ChatResult, ChatToolCall
from pam_pm.rules.templates.blocker import blocker_intake, on_blocker_closed
from pam_pm.rules.templates.follow_up import on_item_done
from pam_pm.rules.templates.morning import morning_checkin


class FakeModelClient:
    """Scripted model returning questions then one tool call."""

    def __init__(self, script: list[ChatResult]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
    ) -> ChatResult:
        _ = (messages, tools, max_tokens)
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) <= len(self._script):
            return self._script[len(self.calls) - 1]
        return self._script[-1]


def _max_write_id(db_path: Any) -> int:
    ids = [row.id or 0 for row in store.list_writes(db_path)]
    return max(ids) if ids else 0


def test_full_story_native(tmp_path: Any) -> None:
    board = NativeBoard()
    try:
        db_path = tmp_path / "state.db"
        writes = WriteService(db_path, dry_run=True)
        chat = InMemoryChat()

        def check_new_writes(marker: int, min_count: int = 1) -> list[Any]:
            fresh = [
                row for row in store.list_writes(db_path) if (row.id or 0) > marker
            ]
            assert len(fresh) >= min_count
            assert all(row.evidence_quote for row in fresh)
            first = fresh[0]
            assert first.id is not None
            assert writes.undo(first.id) is True
            undone = [row for row in store.list_writes(db_path) if row.id == first.id]
            assert undone and undone[0].status == "undone"
            assert store.set_write_status(db_path, first.id, "proposed") is True
            return fresh

        # Step 1: plan from notes with Approve.
        marker = _max_write_id(db_path)
        notes = [
            {"title": "Login form", "owner": "U1"},
            {"title": "API keys", "owner": "U2"},
        ]
        proposal = propose_sprint(notes)
        assert [draft["title"] for draft in proposal] == ["Login form", "API keys"]
        sprint = board.create_iteration(
            "Sprint 1", start_date="2026-01-05", end_date="2026-01-18"
        )
        created = approve_proposal(
            proposal,
            board,
            writes=writes,
            evidence_quote="sticky notes: Login form; API keys",
        )
        assert [item.title for item in created] == ["Login form", "API keys"]
        for item in created:
            board.set_item_iteration(item.id, sprint.id)
        for item in created:
            fetched = board.get_item(item.id)
            assert fetched.state == "New"
            assert fetched.fields.get("iteration_id") == sprint.id
        check_new_writes(marker, 2)

        # Step 2: morning check-in with pushback on a vague answer.
        marker = _max_write_id(db_path)
        vague = "Still working on it, almost done I think, soon."
        specific = "2 hours left, ETA 2026-11-06, finishing tests."
        chat.script_replies("U1", [vague, specific])
        model = FakeModelClient(
            [
                ChatResult(
                    text="What is the status of Login form?", tool_calls=[]
                ),
                ChatResult(text="Can you tell me more?", tool_calls=[]),
                ChatResult(
                    text="",
                    tool_calls=[
                        ChatToolCall(
                            name="finish_interview",
                            arguments=json.dumps(
                                {
                                    "status": "Active",
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
        )
        dms_before = len(chat.dms)
        out = morning_checkin(
            {"roster": ["U1"]}, chat, board, model, writes=writes
        )
        assert "U1" in out
        morning_dms = chat.dms[dms_before:]
        assert morning_dms
        assert len(morning_dms) <= 6
        challenged = [
            dm
            for dm in morning_dms
            if "remain" in dm["text"].lower() and "eta" in dm["text"].lower()
        ]
        assert challenged, f"vague answer was not challenged: {morning_dms}"
        login_id = created[0].id
        stored = board.get_item(login_id)
        assert not is_closed(stored.state)
        assert stored.fields.get("evidence_quote") == specific
        check_new_writes(marker, 1)

        # Step 3: blocker intake with a dependency link.
        marker = _max_write_id(db_path)
        chat.script_replies(
            "U1", ["Login form is blocked", "U2 owns the review", "Misses sprint goal"]
        )
        result = blocker_intake(
            "U1", chat, board, writes, blocked_item_id=login_id
        )
        blocking_id = str(result["blocking_item"].id)
        assert blocking_id != login_id
        assert "U2 owns the review" in str(result["evidence_quote"])
        links = board.list_links(login_id)
        assert len(links) == 1
        assert links[0].source_id == blocking_id
        assert links[0].target_id == login_id
        assert links[0].rel == "System.LinkTypes.Dependency-Forward"
        blocking = board.get_item(blocking_id)
        assert blocking.state == "New"
        assert blocking.fields.get("iteration_id") == sprint.id
        eta_dms = [dm for dm in chat.dms if dm["user_id"] == "U2"]
        assert len(eta_dms) == 1
        assert "eta" in eta_dms[0]["text"].lower()
        check_new_writes(marker, 2)

        # Step 4: blocker clears with notification plus follow-up on close.
        board.close_item(blocking_id)
        cleared = board.get_item(blocking_id)
        assert is_closed(cleared.state)
        dms_before = len(chat.dms)
        notify_id = on_blocker_closed(blocking_id, "U1", chat, board)
        assert notify_id
        sent = on_item_done(blocking_id, chat, board)
        assert len(sent) == 1
        clear_dms = chat.dms[dms_before:]
        assert [dm for dm in clear_dms if dm["user_id"] == "U1"]
        assert any("clear" in dm["text"].lower() for dm in clear_dms)
        assert any("status" in dm["text"].lower() for dm in clear_dms)
        proposed = store.list_writes(db_path, status="proposed")
        assert proposed
        assert proposed[0].id is not None
        assert writes.undo(proposed[0].id) is True
        assert store.set_write_status(db_path, proposed[0].id, "proposed") is True

        # Step 5: review and retro with retro actions on the board.
        marker = _max_write_id(db_path)
        interviews: dict[str, Any] = dict(out["U1"])
        interviews[created[1].id] = {
            "status": "Doing",
            "evidence_quote": "Waiting on API keys from Sam.",
            "confidence": 0.2,
        }
        events = [
            {
                "operation": row.operation,
                "item_id": row.item_id,
                "detail": row.detail,
            }
            for row in store.list_writes(db_path)
        ]
        review = synthesize_review(board.list_items(), interviews, events)
        assert "## Facts" in review["review_markdown"]
        assert "## Retro actions" in review["review_markdown"]
        actions = review["retro_actions"]
        assert actions
        assert all(action["tags"] == ["retro-action"] for action in actions)
        retro_items = create_retro_items(
            board,
            actions,
            writes=writes,
            iteration_id=sprint.id,
            evidence_quote="retro from sprint review",
        )
        assert len(retro_items) == len(actions)
        for item in retro_items:
            fetched = board.get_item(item.id)
            assert fetched.state == "New"
            assert fetched.fields.get("iteration_id") == sprint.id
            assert "retro-action" in str(fetched.fields.get("tags", ""))
        check_new_writes(marker, 1)
        assert "\u2014" not in review["review_markdown"]
    finally:
        board.close()
